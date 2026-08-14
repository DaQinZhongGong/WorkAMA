"""Tests for the enterprise license middleware module.

Covers:
- require_valid_license: success / 402 (no license) / 402 (expired) / 402 (revoked) / cache hit
- require_feature: success (feature present) / success (wildcard) / 403 (missing)
- renew_license endpoint: success (extend + status active) / 404 (not found)
- get_current_license endpoint: returns status + days_remaining / missing payload
- license_state / days_remaining: active / expiring_soon / expired / revoked / floor

All tests use a fake pool/connection (no real DB / Redis / network).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from workama_platform.core import Actor, get_actor
from workama_platform.modules import license_middleware as lm


class _Result:
    """Simulates a psycopg query result."""

    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return list(self._rows)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _RecordingConnection:
    """Records execute() calls and returns configured results in order."""

    def __init__(self, results=None):
        self.calls: list[tuple[str, tuple]] = []
        self._results = list(results) if results else []
        self._idx = 0

    def transaction(self):
        return _Transaction()

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if self._idx < len(self._results):
            r = self._results[self._idx]
            self._idx += 1
            return r
        return _Result()

    async def commit(self):
        return None


class _Pool:
    """Simulates the async connection pool."""

    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_args):
                return False

        return _Ctx()


def _actor(*, role="owner", workspace_id="wsp_test", user_id="usr_test") -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email="test@example.com",
        display_name="Test",
        onboarding_completed=True,
        capabilities=("*",),
    )


def _license_row(**overrides) -> dict:
    base = {
        "id": "lic_1",
        "org_id": "org_test",
        "workspace_id": "wsp_test",
        "plan_code": "enterprise",
        "license_key_hash": "hash_xxx",
        "license_key_last_four": "abcd",
        "status": "active",
        "seats": 50,
        "credit_limit": 1_000_000,
        "concurrency_limit": 100,
        "features": ["advanced_rag", "priority_support"],
        "issued_by": "usr_test",
        "idempotency_key": None,
        "valid_from": datetime.now(UTC) - timedelta(days=10),
        "valid_until": datetime.now(UTC) + timedelta(days=30, hours=1),
        "revoked_at": None,
        "revoke_reason": None,
        "created_at": datetime.now(UTC) - timedelta(days=10),
        "updated_at": datetime.now(UTC) - timedelta(days=10),
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(lm.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


@pytest.fixture(autouse=True)
def _clear_license_cache():
    """Isolate tests from the module-level license cache."""
    lm._license_cache.clear()
    yield
    lm._license_cache.clear()


# ============================================================================
# 1. require_valid_license
# ============================================================================


class TestRequireValidLicense:
    @pytest.mark.asyncio
    async def test_require_valid_license_success(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(row=_license_row())])
        monkeypatch.setattr(lm, "pool", _Pool(conn))
        row = await lm.require_valid_license(_actor())
        assert row["id"] == "lic_1"
        assert row["status"] == "active"
        assert "bill_license" in conn.calls[0][0]

    @pytest.mark.asyncio
    async def test_require_valid_license_402_no_license(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(lm, "pool", _Pool(conn))
        with pytest.raises(HTTPException) as exc:
            await lm.require_valid_license(_actor())
        assert exc.value.status_code == 402
        assert "license required" in exc.value.detail

    @pytest.mark.asyncio
    async def test_require_valid_license_402_expired(self, monkeypatch):
        # The SQL clause `valid_until > now()` excludes expired licenses, so the
        # query returns no row and the dependency raises 402.
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(lm, "pool", _Pool(conn))
        with pytest.raises(HTTPException) as exc:
            await lm.require_valid_license(_actor())
        assert exc.value.status_code == 402
        assert "valid_until > now()" in conn.calls[0][0]

    @pytest.mark.asyncio
    async def test_require_valid_license_402_revoked(self, monkeypatch):
        # The SQL clause `status='active'` excludes revoked licenses, so the
        # query returns no row and the dependency raises 402.
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(lm, "pool", _Pool(conn))
        with pytest.raises(HTTPException) as exc:
            await lm.require_valid_license(_actor())
        assert exc.value.status_code == 402
        assert "status='active'" in conn.calls[0][0]

    @pytest.mark.asyncio
    async def test_require_valid_license_cache_hit(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(row=_license_row())])
        monkeypatch.setattr(lm, "pool", _Pool(conn))
        actor = _actor()
        row1 = await lm.require_valid_license(actor)
        row2 = await lm.require_valid_license(actor)
        assert row1["id"] == row2["id"] == "lic_1"
        # Second call must be served from cache -> only one DB execute total.
        assert len(conn.calls) == 1


# ============================================================================
# 2. require_feature
# ============================================================================


class TestRequireFeature:
    @pytest.mark.asyncio
    async def test_require_feature_success_with_feature(self):
        check = lm.require_feature("advanced_rag")
        license_row = {"id": "lic_1", "features": ["advanced_rag", "priority_support"]}
        result = await check(actor=_actor(), license_row=license_row)
        assert result is license_row

    @pytest.mark.asyncio
    async def test_require_feature_success_with_wildcard(self):
        check = lm.require_feature("anything_else")
        license_row = {"id": "lic_1", "features": ["*"]}
        result = await check(actor=_actor(), license_row=license_row)
        assert result is license_row

    @pytest.mark.asyncio
    async def test_require_feature_403_missing(self):
        check = lm.require_feature("advanced_rag")
        license_row = {"id": "lic_1", "features": ["basic"]}
        with pytest.raises(HTTPException) as exc:
            await check(actor=_actor(), license_row=license_row)
        assert exc.value.status_code == 403
        assert "advanced_rag" in exc.value.detail


# ============================================================================
# 3. renew_license endpoint
# ============================================================================


class TestRenewLicense:
    @pytest.mark.asyncio
    async def test_renew_license_success(self, monkeypatch):
        existing = _license_row(features=["basic"], status="expired")
        updated = _license_row(
            valid_until=datetime.now(UTC) + timedelta(days=60, hours=1),
            features=["advanced_rag"],
        )
        conn = _RecordingConnection(results=[_Result(row=existing), _Result(row=updated)])
        monkeypatch.setattr(lm, "pool", _Pool(conn))

        async def _noop_audit(*_args, **_kwargs):
            return True

        monkeypatch.setattr(lm, "append_audit_chain", _noop_audit)

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/enterprise/compliance/licenses/lic_1/renew",
                json={"extend_days": 30, "new_features": ["advanced_rag"]},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "active"
        assert body["features"] == ["advanced_rag"]
        # license_key_hash must never leak into the response
        assert "license_key_hash" not in body
        update_calls = [q for q, _ in conn.calls if "UPDATE bill_license" in q]
        assert update_calls
        assert "status='active'" in update_calls[0]
        assert "GREATEST(valid_until, now())" in update_calls[0]
        # The cache for this workspace must be invalidated after renewal.
        assert "wsp_test" not in lm._license_cache

    @pytest.mark.asyncio
    async def test_renew_license_404_not_found(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(lm, "pool", _Pool(conn))

        async def _noop_audit(*_args, **_kwargs):
            return True

        monkeypatch.setattr(lm, "append_audit_chain", _noop_audit)

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/enterprise/compliance/licenses/lic_missing/renew",
                json={"extend_days": 30},
            )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


# ============================================================================
# 4. get_current_license endpoint
# ============================================================================


class TestGetCurrentLicense:
    @pytest.mark.asyncio
    async def test_get_current_license_returns_status_and_days(self, monkeypatch):
        row = _license_row(valid_until=datetime.now(UTC) + timedelta(days=30, hours=1))
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(lm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/enterprise/compliance/licenses/current")
        assert resp.status_code == 200
        body = resp.json()
        assert body["license_id"] == "lic_1"
        assert body["status"] == "active"
        assert body["days_remaining"] == 30
        assert body["plan_code"] == "enterprise"
        assert body["features"] == ["advanced_rag", "priority_support"]

    @pytest.mark.asyncio
    async def test_get_current_license_missing(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(lm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/enterprise/compliance/licenses/current")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "missing"
        assert body["license_id"] is None
        assert body["days_remaining"] == 0
        assert body["plan_code"] is None


# ============================================================================
# 5. license_state / days_remaining
# ============================================================================


class TestLicenseState:
    def test_license_state_active(self):
        now = datetime.now(UTC)
        row = {"status": "active", "valid_until": now + timedelta(days=30)}
        assert lm.license_state(row, now) == "active"

    def test_license_state_expiring_soon(self):
        now = datetime.now(UTC)
        row = {"status": "active", "valid_until": now + timedelta(days=3)}
        assert lm.license_state(row, now) == "expiring_soon"

    def test_license_state_expired(self):
        now = datetime.now(UTC)
        row = {"status": "active", "valid_until": now - timedelta(days=1)}
        assert lm.license_state(row, now) == "expired"

    def test_license_state_revoked_takes_precedence(self):
        now = datetime.now(UTC)
        # revoked wins even if valid_until is still in the future
        row = {"status": "revoked", "valid_until": now + timedelta(days=30)}
        assert lm.license_state(row, now) == "revoked"
        # revoked wins even if valid_until is in the past
        row_past = {"status": "revoked", "valid_until": now - timedelta(days=5)}
        assert lm.license_state(row_past, now) == "revoked"

    def test_days_remaining_floored_at_zero(self):
        now = datetime.now(UTC)
        past = {"status": "active", "valid_until": now - timedelta(days=5)}
        assert lm.days_remaining(past, now) == 0
        future = {"status": "active", "valid_until": now + timedelta(days=10, hours=2)}
        assert lm.days_remaining(future, now) == 10
        missing = {"status": "active"}
        assert lm.days_remaining(missing, now) == 0


# ============================================================================
# 6. Router surface
# ============================================================================


def test_license_middleware_router_exposes_expected_routes():
    app = FastAPI()
    app.include_router(lm.router)
    paths = {route.path for route in lm.router.routes}
    assert "/api/v1/enterprise/compliance/licenses/{license_id}/renew" in paths
    assert "/api/v1/enterprise/compliance/licenses/current" in paths
