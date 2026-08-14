"""Chaos engineering primitives: rate limiting, circuit breaker, degradation, and fault injection.

Exports:
- ``TokenBucketRateLimiter``: in-memory per-key token bucket rate limiter.
- ``CircuitBreaker``: closed → open → half_open → closed state machine.
- ``DegradationManager``: progressive feature degradation switches.
- ``rate_limit_middleware``: ASGI middleware factory returning 429 on overflow.
- ``router``: FastAPI router with ``POST /chaos/inject`` and ``GET /chaos/status``.
- ``degradation`` / ``breaker``: module-level singletons used by the router.

Fault injection is gated behind ``settings.environment != 'production'`` (accessed
via ``getattr`` for safety) and requires owner/admin role.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Annotated, Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from workama_platform.core import Actor, get_actor, require_roles, settings

router = APIRouter(prefix="/api/v1", tags=["chaos"])

_VALID_FAULT_TYPES = {"db_delay", "db_error", "redis_error", "degrade"}


# ============================================================================
# 1. Token Bucket Rate Limiter
# ============================================================================


class TokenBucketRateLimiter:
    """In-memory token bucket rate limiter (per workspace_id or per IP).

    Multi-worker deployments count independently per worker; this is acceptable
    because production traffic is load-balanced across workers.
    """

    def __init__(self, rate: float = 10.0, burst: int = 20):
        self.rate = rate
        self.burst = burst
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_refill)

    def allow(self, key: str) -> bool:
        """Return True if a request is allowed (consuming one token)."""
        now = time.monotonic()
        tokens, last = self._buckets.get(key, (float(self.burst), now))
        # Refill proportional to elapsed time.
        elapsed = now - last
        tokens = min(float(self.burst), tokens + elapsed * self.rate)
        if tokens >= 1.0:
            tokens -= 1.0
            self._buckets[key] = (tokens, now)
            return True
        self._buckets[key] = (tokens, now)
        return False


# ============================================================================
# 2. Circuit Breaker
# ============================================================================


class CircuitBreaker:
    """Circuit breaker: closed → open (failure threshold) → half_open (cooldown) → closed.

    In ``half_open`` state only ``half_open_max`` concurrent requests are allowed;
    a success closes the circuit, a failure re-opens it.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max: int = 3,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max = half_open_max
        self._state: dict[str, str] = {}  # key -> "closed"|"open"|"half_open"
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self._half_open_passes: dict[str, int] = {}

    def _current_state(self, key: str) -> str:
        """Return state, transitioning open → half_open after recovery_timeout."""
        state = self._state.get(key, "closed")
        if state == "open":
            opened_at = self._opened_at.get(key, 0.0)
            if time.monotonic() - opened_at >= self.recovery_timeout:
                self._state[key] = "half_open"
                self._half_open_passes[key] = 0
                return "half_open"
        return state

    def allow(self, key: str) -> bool:
        """Return True if a request is allowed through.

        In ``half_open`` only ``half_open_max`` probe requests pass.
        """
        state = self._current_state(key)
        if state == "closed":
            return True
        if state == "half_open":
            passes = self._half_open_passes.get(key, 0)
            if passes < self.half_open_max:
                self._half_open_passes[key] = passes + 1
                return True
            return False
        return False  # open

    def record_success(self, key: str) -> None:
        """Record a success; in half_open this closes the circuit."""
        state = self._current_state(key)
        if state == "half_open":
            self._state[key] = "closed"
            self._failures.pop(key, None)
            self._opened_at.pop(key, None)
            self._half_open_passes.pop(key, None)
        elif state == "closed":
            # Reset consecutive failures on success.
            self._failures.pop(key, None)

    def record_failure(self, key: str) -> None:
        """Record a failure; exceeding threshold opens the circuit.

        A failure in half_open immediately re-opens the circuit.
        """
        state = self._current_state(key)
        if state == "half_open":
            self._state[key] = "open"
            self._opened_at[key] = time.monotonic()
            self._half_open_passes.pop(key, None)
            return
        failures = self._failures.get(key, 0) + 1
        self._failures[key] = failures
        if failures >= self.failure_threshold:
            self._state[key] = "open"
            self._opened_at[key] = time.monotonic()

    def state(self, key: str) -> str:
        """Return the current state for ``key``."""
        return self._current_state(key)

    def snapshot(self) -> dict[str, str]:
        """Return a copy of all known key states (evaluating cooldown transitions)."""
        keys = set(self._state) | set(self._failures) | set(self._opened_at) | set(self._half_open_passes)
        return {key: self._current_state(key) for key in sorted(keys)}


# ============================================================================
# 3. Degradation Manager
# ============================================================================


class DegradationManager:
    """Manage feature degradation switches.

    When a dependency (DB/Redis/external API) is unavailable, progressively
    disable non-core features while keeping core functionality alive.
    """

    def __init__(self):
        self._degraded: set[str] = set()

    def enable(self, feature: str) -> None:
        """Enable degradation for ``feature``."""
        self._degraded.add(feature)

    def disable(self, feature: str) -> None:
        """Restore ``feature`` from degradation."""
        self._degraded.discard(feature)

    def is_degraded(self, feature: str) -> bool:
        """Return True if ``feature`` is currently degraded."""
        return feature in self._degraded

    def list_degraded(self) -> list[str]:
        """Return a sorted list of degraded feature names."""
        return sorted(self._degraded)


# ============================================================================
# Module-level singletons
# ============================================================================

degradation = DegradationManager()
breaker = CircuitBreaker()
_active_faults: dict[str, dict[str, Any]] = {}
_recovery_tasks: set[asyncio.Task] = set()


def _is_production() -> bool:
    """Return True when running in production (fault injection forbidden)."""
    env = getattr(settings, "environment", "development")
    return env == "production"


# ============================================================================
# 4. Fault injection endpoints
# ============================================================================


class FaultInjection(BaseModel):
    """Request body for ``POST /chaos/inject``."""

    type: str = Field(
        ..., description="Fault type: db_delay|db_error|redis_error|degrade"
    )
    duration_seconds: int = Field(default=30, ge=1, le=3600)
    feature: str | None = Field(
        default=None, description="Feature name (required for 'degrade' type)"
    )
    delay_seconds: float | None = Field(
        default=None, ge=0.1, le=60.0, description="Delay seconds for 'db_delay'"
    )


async def _schedule_recovery(fault_id: str, body: FaultInjection) -> None:
    """Auto-recover a fault after ``duration_seconds``."""
    await asyncio.sleep(body.duration_seconds)
    fault = _active_faults.pop(fault_id, None)
    if fault and fault["type"] == "degrade" and fault.get("feature"):
        degradation.disable(fault["feature"])


@router.post("/chaos/inject")
async def inject_fault(
    body: FaultInjection,
    actor: Annotated[Actor, Depends(require_roles("owner", "admin"))],
):
    """Inject a fault (non-production environments only, admin role required).

    Supported types:
    - ``db_delay``: DB queries should be delayed ``delay_seconds``.
    - ``db_error``: DB queries should raise.
    - ``redis_error``: Redis operations should raise.
    - ``degrade``: enable degradation for ``feature``.

    The fault auto-recovers after ``duration_seconds``.
    """
    if _is_production():
        raise HTTPException(
            status_code=403, detail="Fault injection is disabled in production"
        )
    if body.type not in _VALID_FAULT_TYPES:
        raise HTTPException(
            status_code=422, detail=f"Invalid fault type: {body.type}"
        )
    if body.type == "degrade" and not body.feature:
        raise HTTPException(
            status_code=422, detail="feature is required for 'degrade' type"
        )

    fault_id = uuid.uuid4().hex
    now = time.time()
    fault_record: dict[str, Any] = {
        "id": fault_id,
        "type": body.type,
        "feature": body.feature,
        "delay_seconds": body.delay_seconds,
        "duration_seconds": body.duration_seconds,
        "injected_by": actor.user_id,
        "injected_at": now,
        "expires_at": now + body.duration_seconds,
    }
    _active_faults[fault_id] = fault_record

    if body.type == "degrade":
        degradation.enable(body.feature)

    # Schedule auto-recovery; keep a strong reference to avoid GC.
    task = asyncio.create_task(_schedule_recovery(fault_id, body))
    _recovery_tasks.add(task)
    task.add_done_callback(_recovery_tasks.discard)

    return {
        "fault_id": fault_id,
        "status": "injected",
        "type": body.type,
        "feature": body.feature,
        "duration_seconds": body.duration_seconds,
        "expires_at": fault_record["expires_at"],
    }


@router.get("/chaos/status")
async def chaos_status(
    actor: Annotated[Actor, Depends(require_roles("owner", "admin"))],
):
    """Query current degradation state, circuit breaker states, and active faults."""
    return {
        "degraded_features": degradation.list_degraded(),
        "circuit_breakers": breaker.snapshot(),
        "active_faults": list(_active_faults.values()),
    }


# ============================================================================
# 5. Rate limit middleware factory
# ============================================================================


def _default_key_func(request: Request) -> str:
    """Derive a rate-limit key from workspace header or client IP."""
    workspace_id = request.headers.get("X-Workspace-Id")
    if workspace_id:
        return f"ws:{workspace_id}"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return f"ip:{forwarded.split(',')[0].strip()}"
    client = request.client
    if client:
        return f"ip:{client.host}"
    return "anonymous"


def rate_limit_middleware(
    bucket: TokenBucketRateLimiter,
    key_func: Callable[[Request], str] | None = None,
):
    """Return an ASGI middleware class that rate-limits by ``key_func(request)``.

    When the bucket denies a request, a ``429 Too Many Requests`` JSON response
    is returned without invoking the downstream application.

    Usage::

        app.add_middleware(rate_limit_middleware(bucket))
    """
    resolver = key_func or _default_key_func

    class _RateLimitMiddleware:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
            if scope.get("type") != "http":
                return await self.app(scope, receive, send)
            request = Request(scope)
            try:
                key = resolver(request)
            except Exception:
                key = "anonymous"
            if not bucket.allow(key):
                response = JSONResponse(
                    status_code=429,
                    content={"detail": "Too Many Requests", "code": "rate_limited"},
                )
                return await response(scope, receive, send)
            return await self.app(scope, receive, send)

    return _RateLimitMiddleware

