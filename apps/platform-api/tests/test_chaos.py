"""Tests for the chaos engineering module (rate limiter, circuit breaker, degradation, fault injection).

Covers:
- TokenBucketRateLimiter: first pass / over-limit reject / token refill
- CircuitBreaker: closed→open→half_open→closed, half_open failure→re-open
- DegradationManager: enable/disable/is_degraded/list_degraded
- POST /chaos/inject: success (non-production), 403 (production), db_delay type, degrade type
- GET /chaos/status: returns degraded features + circuit breakers + active faults
- rate_limit_middleware: 429 on over-limit
- Fault injection auto-recovery after duration_seconds

All tests use httpx ASGI transport (no real DB / Redis / network).
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules import chaos


# ============================================================================
# Helpers
# ============================================================================


def _actor(*, role: str = "owner", workspace_id: str = "wsp_test") -> Actor:
    return Actor(
        user_id="usr_test",
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email="test@example.com",
        display_name="Test",
        onboarding_completed=True,
        capabilities=("*",),
    )


def _app(actor: Actor | None = None) -> FastAPI:
    """Build a minimal FastAPI app with the chaos router and optional actor override."""
    app = FastAPI()
    app.include_router(chaos.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


@pytest.fixture(autouse=True)
def _reset_chaos_state():
    """Isolate tests from module-level singletons."""
    chaos.degradation._degraded.clear()
    chaos.breaker._state.clear()
    chaos.breaker._failures.clear()
    chaos.breaker._opened_at.clear()
    chaos.breaker._half_open_passes.clear()
    chaos._active_faults.clear()
    # Cancel any pending recovery tasks.
    for task in list(chaos._recovery_tasks):
        if not task.done():
            try:
                task.cancel()
            except RuntimeError:
                pass

    chaos._recovery_tasks.clear()
    yield
    chaos.degradation._degraded.clear()
    chaos.breaker._state.clear()
    chaos.breaker._failures.clear()
    chaos.breaker._opened_at.clear()
    chaos.breaker._half_open_passes.clear()
    chaos._active_faults.clear()
    for task in list(chaos._recovery_tasks):
        if not task.done():
            try:
                task.cancel()
            except RuntimeError:
                pass

    chaos._recovery_tasks.clear()


# ============================================================================
# 1. TokenBucketRateLimiter
# ============================================================================


class TestTokenBucketRateLimiter:
    @pytest.mark.asyncio
    async def test_first_request_allowed(self):
        bucket = chaos.TokenBucketRateLimiter(rate=1.0, burst=2)
        assert bucket.allow("k1") is True

    @pytest.mark.asyncio
    async def test_over_limit_rejected(self):
        bucket = chaos.TokenBucketRateLimiter(rate=0.0, burst=2)
        assert bucket.allow("k1") is True
        assert bucket.allow("k1") is True
        # Burst exhausted, rate=0 means no refill.
        assert bucket.allow("k1") is False

    @pytest.mark.asyncio
    async def test_token_refill_after_delay(self):
        # rate=10 tokens/s, burst=1 → after consuming 1, wait ~0.2s for refill.
        bucket = chaos.TokenBucketRateLimiter(rate=10.0, burst=1)
        assert bucket.allow("k1") is True
        assert bucket.allow("k1") is False
        await asyncio.sleep(0.25)
        assert bucket.allow("k1") is True

    @pytest.mark.asyncio
    async def test_independent_keys(self):
        bucket = chaos.TokenBucketRateLimiter(rate=0.0, burst=1)
        assert bucket.allow("a") is True
        assert bucket.allow("b") is True
        assert bucket.allow("a") is False
        assert bucket.allow("b") is False


# ============================================================================
# 2. CircuitBreaker
# ============================================================================


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_closed_to_open_on_threshold(self):
        cb = chaos.CircuitBreaker(failure_threshold=3, recovery_timeout=30.0)
        assert cb.state("svc") == "closed"
        # Below threshold: still closed.
        cb.record_failure("svc")
        cb.record_failure("svc")
        assert cb.state("svc") == "closed"
        # Reaching threshold: open.
        cb.record_failure("svc")
        assert cb.state("svc") == "open"
        assert cb.allow("svc") is False

    @pytest.mark.asyncio
    async def test_open_to_half_open_after_cooldown(self):
        cb = chaos.CircuitBreaker(failure_threshold=1, recovery_timeout=0.1)
        cb.record_failure("svc")
        assert cb.state("svc") == "open"
        assert cb.allow("svc") is False
        # Wait for cooldown.
        await asyncio.sleep(0.15)
        state = cb.state("svc")
        assert state == "half_open"
        # In half_open, probe requests are allowed (up to half_open_max).
        assert cb.allow("svc") is True

    @pytest.mark.asyncio
    async def test_half_open_to_closed_on_success(self):
        cb = chaos.CircuitBreaker(failure_threshold=1, recovery_timeout=0.1)
        cb.record_failure("svc")  # open
        await asyncio.sleep(0.15)
        cb.state("svc")  # triggers half_open
        assert cb.state("svc") == "half_open"
        cb.record_success("svc")
        assert cb.state("svc") == "closed"

    @pytest.mark.asyncio
    async def test_half_open_failure_reopens(self):
        cb = chaos.CircuitBreaker(failure_threshold=1, recovery_timeout=0.1, half_open_max=2)
        cb.record_failure("svc")  # open
        await asyncio.sleep(0.15)
        assert cb.state("svc") == "half_open"
        # Allow a probe.
        assert cb.allow("svc") is True
        # Failure during half_open re-opens immediately.
        cb.record_failure("svc")
        assert cb.state("svc") == "open"

    @pytest.mark.asyncio
    async def test_half_open_concurrency_limit(self):
        cb = chaos.CircuitBreaker(failure_threshold=1, recovery_timeout=0.1, half_open_max=2)
        cb.record_failure("svc")
        await asyncio.sleep(0.15)
        cb.state("svc")  # half_open
        # Only half_open_max probes allowed.
        assert cb.allow("svc") is True
        assert cb.allow("svc") is True
        assert cb.allow("svc") is False


# ============================================================================
# 3. DegradationManager
# ============================================================================


class TestDegradationManager:
    @pytest.mark.asyncio
    async def test_enable_disable_is_degraded_list(self):
        dm = chaos.DegradationManager()
        assert dm.list_degraded() == []
        assert dm.is_degraded("rag") is False

        dm.enable("rag")
        assert dm.is_degraded("rag") is True
        assert dm.list_degraded() == ["rag"]

        dm.enable("memory")
        assert dm.list_degraded() == ["memory", "rag"]

        dm.disable("rag")
        assert dm.is_degraded("rag") is False
        assert dm.list_degraded() == ["memory"]

        # Disabling a non-degraded feature is a no-op.
        dm.disable("nonexistent")
        assert dm.list_degraded() == ["memory"]


# ============================================================================
# 4. POST /chaos/inject
# ============================================================================


class TestInjectFault:
    @pytest.mark.asyncio
    async def test_inject_success_non_production(self):
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/chaos/inject",
                json={"type": "db_error", "duration_seconds": 5},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "injected"
        assert body["type"] == "db_error"
        assert "fault_id" in body
        assert len(chaos._active_faults) == 1

    @pytest.mark.asyncio
    async def test_inject_403_in_production(self, monkeypatch):
        # Simulate production environment: settings.environment == 'production'.
        monkeypatch.setattr(
            chaos, "settings", SimpleNamespace(environment="production")
        )
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/chaos/inject",
                json={"type": "db_error", "duration_seconds": 5},
            )
        assert resp.status_code == 403
        assert "production" in resp.json()["detail"].lower()
        assert len(chaos._active_faults) == 0

    @pytest.mark.asyncio
    async def test_inject_db_delay_type(self):
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/chaos/inject",
                json={
                    "type": "db_delay",
                    "duration_seconds": 5,
                    "delay_seconds": 2.0,
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == "db_delay"
        fault = list(chaos._active_faults.values())[0]
        assert fault["delay_seconds"] == 2.0

    @pytest.mark.asyncio
    async def test_inject_degrade_enables_degradation(self):
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/chaos/inject",
                json={
                    "type": "degrade",
                    "feature": "rag",
                    "duration_seconds": 5,
                },
            )
        assert resp.status_code == 200
        assert chaos.degradation.is_degraded("rag") is True

    @pytest.mark.asyncio
    async def test_inject_degrade_requires_feature(self):
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/chaos/inject",
                json={"type": "degrade", "duration_seconds": 5},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_inject_rejects_non_admin(self):
        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/chaos/inject",
                json={"type": "db_error", "duration_seconds": 5},
            )
        assert resp.status_code == 403


# ============================================================================
# 5. GET /chaos/status
# ============================================================================


class TestChaosStatus:
    @pytest.mark.asyncio
    async def test_status_returns_state(self):
        # Seed some state.
        chaos.degradation.enable("rag")
        chaos.breaker.record_failure("svc_x")
        chaos.breaker.record_failure("svc_x")
        chaos._active_faults["f1"] = {"id": "f1", "type": "db_error"}

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/chaos/status")
        assert resp.status_code == 200
        body = resp.json()
        assert "rag" in body["degraded_features"]
        assert "svc_x" in body["circuit_breakers"]
        assert body["circuit_breakers"]["svc_x"] == "closed"
        assert any(f["id"] == "f1" for f in body["active_faults"])


# ============================================================================
# 6. Rate limit middleware
# ============================================================================


class TestRateLimitMiddleware:
    @pytest.mark.asyncio
    async def test_middleware_returns_429_on_over_limit(self):
        bucket = chaos.TokenBucketRateLimiter(rate=0.0, burst=2)
        app = FastAPI()
        app.add_middleware(chaos.rate_limit_middleware(bucket))

        @app.get("/ping")
        async def ping():
            return {"ok": True}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r1 = await client.get("/ping")
            r2 = await client.get("/ping")
            r3 = await client.get("/ping")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r3.status_code == 429
        assert r3.json()["code"] == "rate_limited"


# ============================================================================
# 7. Fault injection auto-recovery
# ============================================================================


class TestAutoRecovery:
    @pytest.mark.asyncio
    async def test_degrade_auto_recovers_after_duration(self):
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/chaos/inject",
                json={
                    "type": "degrade",
                    "feature": "memory",
                    "duration_seconds": 1,
                },
            )
        assert resp.status_code == 200
        assert chaos.degradation.is_degraded("memory") is True
        assert len(chaos._active_faults) == 1

        # Wait for auto-recovery.
        await asyncio.sleep(1.3)
        # Yield control so the recovery task callback completes.
        await asyncio.sleep(0.1)

        assert chaos.degradation.is_degraded("memory") is False
        assert len(chaos._active_faults) == 0

    @pytest.mark.asyncio
    async def test_db_error_auto_clears_after_duration(self):
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/chaos/inject",
                json={"type": "db_error", "duration_seconds": 1},
            )
        assert resp.status_code == 200
        assert len(chaos._active_faults) == 1

        await asyncio.sleep(1.3)
        await asyncio.sleep(0.1)

        assert len(chaos._active_faults) == 0
