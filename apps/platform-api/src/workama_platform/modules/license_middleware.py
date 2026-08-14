"""Enterprise license middleware: validation dependency, feature gating, and license management endpoints.

Exports:
- ``require_valid_license``: FastAPI dependency validating the current workspace
  has an active, non-expired license (caches the row for 60s per workspace).
- ``require_feature(feature_name)``: FastAPI dependency factory for feature gating.
- ``router``: endpoints for license renewal (``POST /licenses/{id}/renew``) and
  current-status queries (``GET /licenses/current``).
- ``license_state`` / ``days_remaining``: enhanced license-state helpers.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from workama_platform.core import Actor, get_actor, json_dumps, new_id, pool
from workama_platform.modules.audit_exports import append_audit_chain
from workama_platform.modules.compliance import _admin, _license_view


router = APIRouter(prefix="/api/v1/enterprise/compliance", tags=["enterprise-license"])

# Module-level license cache: workspace_id -> (row, monotonic_expiry).
# Simple in-process TTL cache so we do not hit the DB on every request.
_LICENSE_CACHE_TTL_SECONDS = 60
_license_cache: dict[str, tuple[dict[str, Any], float]] = {}

_EXPIRING_SOON_WINDOW = timedelta(days=7)


def _now() -> datetime:
    return datetime.now(UTC)


def days_remaining(row: dict[str, Any], now: datetime | None = None) -> int:
    """Whole days until ``valid_until``; floored at 0 for past or missing dates."""
    current = now or _now()
    valid_until = row.get("valid_until")
    if not valid_until:
        return 0
    delta = valid_until - current
    return max(0, int(delta.total_seconds() // 86400))


def license_state(row: dict[str, Any], now: datetime | None = None) -> str:
    """Enhanced license state enumeration.

    - ``revoked``: status == 'revoked' (takes precedence over expiry).
    - ``expired``: status active but ``valid_until <= now``.
    - ``expiring_soon``: status active and ``valid_until <= now + 7d``.
    - ``active``: status active and ``valid_until > now + 7d``.
    - any other status (e.g. 'suspended') is returned as-is.
    """
    current = now or _now()
    status = row.get("status")
    if status == "revoked":
        return "revoked"
    if status != "active":
        return str(status)
    valid_until = row.get("valid_until")
    if valid_until and valid_until <= current:
        return "expired"
    if valid_until and valid_until <= current + _EXPIRING_SOON_WINDOW:
        return "expiring_soon"
    return "active"


def _cache_get(workspace_id: str) -> dict[str, Any] | None:
    entry = _license_cache.get(workspace_id)
    if entry is None:
        return None
    row, expires_at = entry
    if time.monotonic() > expires_at:
        _license_cache.pop(workspace_id, None)
        return None
    return row


def _cache_set(workspace_id: str, row: dict[str, Any]) -> None:
    _license_cache[workspace_id] = (row, time.monotonic() + _LICENSE_CACHE_TTL_SECONDS)


def _cache_invalidate(workspace_id: str) -> None:
    _license_cache.pop(workspace_id, None)


async def _fetch_active_license(workspace_id: str) -> dict[str, Any] | None:
    """Fetch the most recent active, non-expired license for a workspace."""
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM bill_license WHERE workspace_id=%s AND status='active' AND valid_until > now() ORDER BY valid_until DESC LIMIT 1",
            (workspace_id,),
        )
        row = await result.fetchone()
    return dict(row) if row else None


async def require_valid_license(actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    """Validate the current workspace has an active, non-expired license.

    Raises ``HTTPException(402, "license required")`` when no active license is
    found (covers missing / expired / revoked, all of which are filtered out by
    the ``status='active' AND valid_until > now()`` clause).
    Returns the license row (features included), cached for 60s per workspace.
    """
    cached = _cache_get(actor.workspace_id)
    if cached is not None:
        return cached
    row = await _fetch_active_license(actor.workspace_id)
    if row is None:
        raise HTTPException(status_code=402, detail="license required")
    _cache_set(actor.workspace_id, row)
    return row


def require_feature(feature_name: str):
    """Build a FastAPI dependency that checks the workspace license grants ``feature_name``.

    A license whose features contain ``"*"`` is treated as granting every feature.
    Usage::

        @router.get("/...", dependencies=[Depends(require_feature("advanced_rag"))])
    """
    async def _check_feature(
        actor: Annotated[Actor, Depends(get_actor)],
        license_row: Annotated[dict[str, Any], Depends(require_valid_license)],
    ) -> dict[str, Any]:
        features = license_row.get("features") or []
        if feature_name not in features and "*" not in features:
            raise HTTPException(status_code=403, detail=f"feature not licensed: {feature_name}")
        return license_row

    return _check_feature


class RenewRequest(BaseModel):
    extend_days: int = Field(ge=1, le=365)
    new_features: dict[str, Any] | list[str] | None = None


@router.post("/licenses/{license_id}/renew")
async def renew_license(license_id: str, body: RenewRequest, actor: Annotated[Actor, Depends(get_actor)]):
    """Renew a license: extend ``valid_until`` from now (or current expiry, whichever is later)
    and reset ``status='active'``. Optionally replace ``features``.
    """
    _admin(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            existing = await conn.execute(
                "SELECT * FROM bill_license WHERE id=%s AND org_id=%s AND workspace_id=%s",
                (license_id, actor.org_id, actor.workspace_id),
            )
            existing_row = await existing.fetchone()
            if not existing_row:
                raise HTTPException(status_code=404, detail="License not found")
            features = body.new_features if body.new_features is not None else (existing_row.get("features") or {})
            result = await conn.execute(
                "UPDATE bill_license SET valid_until = GREATEST(valid_until, now()) + (%s * interval '1 day'), status='active', features=%s::jsonb, updated_at=now() WHERE id=%s RETURNING *",
                (body.extend_days, json_dumps(features), license_id),
            )
            row = await result.fetchone()
            await append_audit_chain(
                conn,
                event_id=new_id("aud"),
                org_id=actor.org_id,
                workspace_id=actor.workspace_id,
                actor_user_id=actor.user_id,
                action="enterprise.license.renewed",
                resource_type="license",
                resource_id=license_id,
                details={"extend_days": body.extend_days},
            )
    _cache_invalidate(actor.workspace_id)
    return _license_view(row)


@router.get("/licenses/current")
async def get_current_license(actor: Annotated[Actor, Depends(get_actor)]):
    """Return the current workspace license with state and remaining days.

    When no active license exists, returns a ``status='missing'`` payload instead
    of raising, so clients can distinguish "no license" from "expired license".
    """
    row = await _fetch_active_license(actor.workspace_id)
    if row is None:
        return {
            "license_id": None,
            "status": "missing",
            "valid_until": None,
            "days_remaining": 0,
            "features": {},
            "plan_code": None,
        }
    return {
        "license_id": row.get("id"),
        "status": license_state(row),
        "valid_until": row.get("valid_until"),
        "days_remaining": days_remaining(row),
        "features": row.get("features") or {},
        "plan_code": row.get("plan_code"),
    }
