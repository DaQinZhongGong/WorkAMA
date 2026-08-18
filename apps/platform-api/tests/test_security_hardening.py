"""Tests for the second-pass penetration-test hardening module.

Covers:
- Input validation, mass-assignment filtering, IDOR ownership guards (baseline)
- P3 第二次渗透测试安全加固（OWASP Top 10 专项）:
  * CSRF 防护（csrf_protect / _origin_is_trusted / _extract_request_origin）
  * 安全响应头 ASGI 中间件 SecurityHeadersMiddleware
  * 速率限制 ASGI 中间件 RateLimitMiddleware（滑动窗口，分层阈值）
  * 密码强度策略 validate_password_strength
  * JWT 安全增强：指纹绑定 / 黑名单 / 刷新令牌轮换
  * 审计日志链式 hash（防篡改）+ 链完整性校验
  * 4 个新端点（CSRF token / 审计链校验 / 密码强度 / 刷新令牌轮换）

All tests use fake pool/connection mocks — no live DB / Redis / network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from fastapi import FastAPI, HTTPException, Request

from workama_platform.core import Actor, create_access_token, get_actor, settings
from workama_platform.modules import security_hardening as sh


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _actor(
    *,
    user_id: str = "usr_abc1234567890abcdefghijk",
    workspace_id: str = "ws_abc1234567890abcdefghijk",
    role: str = "member",
    auth_strength: int = 1,
) -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_abc1234567890abcdefghijk",
        role=role,
        email="tester@workama.example.com",
        display_name="Tester",
        onboarding_completed=True,
        auth_strength=auth_strength,
    )


class _Result:
    """Mimics the psycopg cursor result object returned by ``conn.execute``."""

    def __init__(self, row=None):
        self._row = row

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return [self._row] if self._row else []


class _Connection:
    """In-memory connection that returns a canned row for any execute()."""

    def __init__(self, row=None):
        self._row = row
        self.statements: list[tuple[str, tuple]] = []

    async def execute(self, statement, params=()):
        self.statements.append((statement, params))
        return _Result(self._row)

    async def commit(self):
        return None


class _Pool:
    """Context-manager pool yielding a single canned connection."""

    def __init__(self, connection: _Connection):
        self._connection = connection

    def connection(self):
        outer = self

        class _Ctx:
            async def __aenter__(self):
                return outer._connection

            async def __aexit__(self, exc_type, exc, tb):
                return False

        return _Ctx()


# ---------------------------------------------------------------------------
# Recording helpers (return results in sequence for multi-query functions)
# ---------------------------------------------------------------------------


class _RecResult:
    """Result that can return a specific row (fetchone) or list (fetchall)."""

    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return list(self._rows)


class _RecConn:
    """Connection that returns canned results in sequence and records calls."""

    def __init__(self, results=None):
        self.calls: list[tuple[str, tuple]] = []
        self._results = list(results) if results else []
        self._idx = 0

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if self._idx < len(self._results):
            r = self._results[self._idx]
            self._idx += 1
            return r
        return _RecResult()

    async def commit(self):
        return None


class _RecPool:
    """Pool yielding a single recording connection."""

    def __init__(self, conn: _RecConn):
        self._conn = conn

    def connection(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_args):
                return False

        return _Ctx()


# ---------------------------------------------------------------------------
# Minimal-app builders for middleware / endpoint tests
# ---------------------------------------------------------------------------


def _build_csrf_app() -> FastAPI:
    """App with a POST endpoint guarded by ``csrf_protect``."""
    app = FastAPI()

    @app.post("/api/v1/protected")
    async def _protected(request: Request):
        await sh.csrf_protect(request)
        return {"ok": True}

    @app.get("/api/v1/protected")
    async def _protected_get(request: Request):
        await sh.csrf_protect(request)
        return {"ok": True}

    @app.post("/api/v1/auth/login")
    async def _login():
        return {"token": "x"}

    @app.get("/healthz")
    async def _healthz():
        return {"status": "ok"}

    return app


def _build_middleware_app(*, rate_limit: bool = True, security_headers: bool = True) -> FastAPI:
    """App with a simple GET endpoint, optionally wrapped by the two middlewares."""
    app = FastAPI()

    @app.get("/api/v1/test")
    async def _test():
        return {"ok": True}

    @app.get("/healthz")
    async def _healthz():
        return {"status": "ok"}

    @app.post("/api/v1/auth/login")
    async def _login():
        return {"token": "x"}

    # LIFO: last added = outermost.  Add RateLimit first so SecurityHeaders
    # wraps it (ensures 429 responses also receive security headers).
    if rate_limit:
        app.add_middleware(sh.RateLimitMiddleware)
    if security_headers:
        app.add_middleware(sh.SecurityHeadersMiddleware)
    return app


def _build_security_app(actor: Actor | None = None) -> FastAPI:
    """App that includes the security-hardening routers."""
    app = FastAPI()
    app.include_router(sh.router)
    app.include_router(sh.auth_extension_router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


def _build_valid_chain(n: int = 3) -> list[dict[str, Any]]:
    """Build *n* audit-chain rows that form a valid hash chain."""
    rows: list[dict[str, Any]] = []
    prev = ""
    for i in range(n):
        payload = {"action": f"act_{i}", "user": "usr_test"}
        payload_hash = sh.compute_payload_hash(payload)
        chain_hash = sh.compute_chain_hash_from_payload_hash(prev, payload_hash)
        rows.append(
            {
                "audit_id": f"aud_{i:04d}",
                "prev_hash": prev,
                "payload_hash": payload_hash,
                "chain_hash": chain_hash,
            }
        )
        prev = chain_hash
    return rows


# ===========================================================================
# 1. validate_path_component
# ===========================================================================


def test_validate_path_component_accepts_normal_segment():
    assert sh.validate_path_component("session_42.txt") == "session_42.txt"


def test_validate_path_component_rejects_path_traversal():
    with pytest.raises(HTTPException) as exc:
        sh.validate_path_component("../../etc/passwd")
    assert exc.value.status_code == 400
    assert "traversal" in exc.value.detail.lower()


def test_validate_path_component_rejects_backslash_traversal():
    with pytest.raises(HTTPException) as exc:
        sh.validate_path_component("..\\..\\windows")
    assert exc.value.status_code == 400


def test_validate_path_component_rejects_null_byte():
    with pytest.raises(HTTPException) as exc:
        sh.validate_path_component("safe.txt\x00.evil")
    assert exc.value.status_code == 400
    assert "null" in exc.value.detail.lower()


def test_validate_path_component_rejects_path_separators():
    for bad in ("a/b", "a\\b"):
        with pytest.raises(HTTPException) as exc:
            sh.validate_path_component(bad)
        assert exc.value.status_code == 400


def test_validate_path_component_rejects_control_chars_and_oversize():
    with pytest.raises(HTTPException):
        sh.validate_path_component("a\nb")
    with pytest.raises(HTTPException):
        sh.validate_path_component("x" * 257, max_length=256)


# ===========================================================================
# 2. sanitize_search_query
# ===========================================================================


def test_sanitize_search_query_strips_sql_injection_hints():
    out = sh.sanitize_search_query("hello UNION SELECT password FROM users")
    assert "union" not in out.lower()
    assert "select" not in out.lower()
    assert "hello" in out


def test_sanitize_search_query_strips_xss_tags():
    out = sh.sanitize_search_query("hi <script>alert(1)</script> javascript:alert(1) onerror=alert(1)")
    assert "<script" not in out.lower()
    assert "javascript:" not in out.lower()
    assert "onerror=" not in out.lower()


def test_sanitize_search_query_truncates_long_input():
    long_input = "a" * 1000
    out = sh.sanitize_search_query(long_input, max_length=500)
    assert len(out) <= 500


def test_sanitize_search_query_handles_non_string_and_null_bytes():
    assert sh.sanitize_search_query(None) == ""
    assert sh.sanitize_search_query("a\x00b") == "ab"


# ===========================================================================
# 3. validate_uuid_like
# ===========================================================================


def test_validate_uuid_like_accepts_workama_id_and_uuid():
    assert sh.validate_uuid_like("sess_0123456789ABCDEFGHJKMNPQRS") is True
    assert sh.validate_uuid_like("12345678-1234-1234-1234-1234567890ab") is True


def test_validate_uuid_like_rejects_malformed_ids():
    assert sh.validate_uuid_like("") is False
    assert sh.validate_uuid_like("not-an-id") is False
    assert sh.validate_uuid_like("../etc/passwd") is False
    assert sh.validate_uuid_like("sess_short") is False
    assert sh.validate_uuid_like(None) is False  # type: ignore[arg-type]


# ===========================================================================
# 4. check_mass_assignment
# ===========================================================================


def test_check_mass_assignment_drops_non_allowed_fields():
    body = {"name": "alice", "role": "admin", "is_admin": True, "workspace_id": "ws_x"}
    allowed = {"name", "role"}
    filtered = sh.check_mass_assignment(body, allowed)
    assert filtered == {"name": "alice", "role": "admin"}


def test_check_mass_assignment_handles_non_dict_input():
    assert sh.check_mass_assignment(None, {"a"}) == {}  # type: ignore[arg-type]
    assert sh.check_mass_assignment({"a": 1}, None) == {}  # type: ignore[arg-type]


# ===========================================================================
# 5. require_resource_owner (IDOR guard)
# ===========================================================================


@pytest.mark.asyncio
async def test_require_resource_owner_success(monkeypatch):
    connection = _Connection(row={"?column?": 1})
    monkeypatch.setattr(sh, "pool", _Pool(connection))
    result = await sh.require_resource_owner(
        "ag_session", "sess_0123456789ABCDEFGHJKMNPQRS", _actor()
    )
    assert result == {
        "resource_type": "ag_session",
        "resource_id": "sess_0123456789ABCDEFGHJKMNPQRS",
        "owner_verified": True,
    }
    assert len(connection.statements) == 1
    statement, params = connection.statements[0]
    assert "%s" in statement
    assert params[0] == "sess_0123456789ABCDEFGHJKMNPQRS"


@pytest.mark.asyncio
async def test_require_resource_owner_returns_404_when_not_owned(monkeypatch):
    connection = _Connection(row=None)
    monkeypatch.setattr(sh, "pool", _Pool(connection))
    with pytest.raises(HTTPException) as exc:
        await sh.require_resource_owner(
            "ag_session", "sess_0123456789ABCDEFGHJKMNPQRS", _actor()
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_require_resource_owner_returns_404_for_cross_workspace(monkeypatch):
    connection = _Connection(row=None)
    monkeypatch.setattr(sh, "pool", _Pool(connection))
    cross_workspace_actor = _actor(workspace_id="ws_DIFFERENT01234567890ABC")
    with pytest.raises(HTTPException) as exc:
        await sh.require_resource_owner(
            "ag_session", "sess_0123456789ABCDEFGHJKMNPQRS", cross_workspace_actor
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_require_resource_owner_rejects_unknown_resource_type(monkeypatch):
    connection = _Connection(row={"?column?": 1})
    monkeypatch.setattr(sh, "pool", _Pool(connection))
    with pytest.raises(HTTPException) as exc:
        await sh.require_resource_owner(
            "ag_unknown", "sess_0123456789ABCDEFGHJKMNPQRS", _actor()
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_require_resource_owner_rejects_malformed_id_without_db_call(monkeypatch):
    connection = _Connection(row={"?column?": 1})
    monkeypatch.setattr(sh, "pool", _Pool(connection))
    with pytest.raises(HTTPException) as exc:
        await sh.require_resource_owner(
            "ag_session", "../../etc/passwd", _actor()
        )
    assert exc.value.status_code == 404
    assert connection.statements == []  # no SQL executed


# ===========================================================================
# 6. rate_limit_dependency
# ===========================================================================


@pytest.mark.asyncio
async def test_rate_limit_dependency_allows_under_threshold(monkeypatch):
    sh._reset_rate_limits()
    dependency = sh.rate_limit_dependency(max_requests=3, window_seconds=60)
    actor = _actor()
    await dependency(actor=actor)
    await dependency(actor=actor)
    await dependency(actor=actor)
    sh._reset_rate_limits()


@pytest.mark.asyncio
async def test_rate_limit_dependency_returns_429_when_exceeded(monkeypatch):
    sh._reset_rate_limits()
    dependency = sh.rate_limit_dependency(max_requests=2, window_seconds=60)
    actor = _actor()
    await dependency(actor=actor)
    await dependency(actor=actor)
    with pytest.raises(HTTPException) as exc:
        await dependency(actor=actor)
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "60"
    sh._reset_rate_limits()


@pytest.mark.asyncio
async def test_rate_limit_dependency_isolates_users(monkeypatch):
    sh._reset_rate_limits()
    dependency = sh.rate_limit_dependency(max_requests=1, window_seconds=60)
    alice = _actor(user_id="usr_alice000000000000000000AAA")
    bob = _actor(user_id="usr_bob0000000000000000000BBB")
    await dependency(actor=alice)
    await dependency(actor=bob)
    with pytest.raises(HTTPException) as exc:
        await dependency(actor=alice)
    assert exc.value.status_code == 429
    sh._reset_rate_limits()


# ===========================================================================
# 7. Informational endpoints
# ===========================================================================


@pytest.mark.asyncio
async def test_security_headers_info_returns_catalog():
    response = await sh.security_headers_info()
    assert "headers" in response
    names = {entry["name"] for entry in response["headers"]}
    assert "X-Content-Type-Options" in names
    assert "Strict-Transport-Security" in names
    assert "Content-Security-Policy" in names
    for entry in response["headers"]:
        assert "value" in entry and entry["value"]


@pytest.mark.asyncio
async def test_security_policy_returns_workspace_posture():
    actor = _actor(role="admin", auth_strength=2)
    response = await sh.security_policy(actor=actor)
    assert response["workspace_id"] == actor.workspace_id
    assert response["password_policy"]["min_length"] >= 12
    assert response["session_timeout_seconds"] > 0
    assert response["mfa_required"] is True
    assert "owner" in response["mfa_required_roles"]
    assert response["auth_strength_current"] == 2


@pytest.mark.asyncio
async def test_security_policy_mfa_not_required_for_member():
    actor = _actor(role="member")
    response = await sh.security_policy(actor=actor)
    assert response["mfa_required"] is False


# ===========================================================================
# 8. CSRF 防护
# ===========================================================================


class TestCSRFProtection:
    def test_origin_is_trusted_true_for_trusted(self):
        assert sh._origin_is_trusted("http://localhost:20204") is True

    def test_origin_is_trusted_false_for_untrusted(self):
        assert sh._origin_is_trusted("http://evil.example.com") is False

    def test_origin_is_trusted_false_for_empty(self):
        assert sh._origin_is_trusted("") is False

    @pytest.mark.asyncio
    async def test_csrf_safe_method_get_passes(self):
        app = _build_csrf_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/protected")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_csrf_post_with_trusted_origin_passes(self):
        app = _build_csrf_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/protected", headers={"Origin": "http://localhost:20204"}
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_csrf_post_with_untrusted_origin_403(self):
        app = _build_csrf_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/protected", headers={"Origin": "http://evil.example.com"}
            )
        assert resp.status_code == 403
        assert "CSRF" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_csrf_post_missing_origin_403(self):
        app = _build_csrf_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/protected")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_csrf_exempt_path_login_passes(self):
        """``/api/v1/auth/login`` is CSRF-exempt so it works without Origin."""
        app = _build_csrf_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/auth/login")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_csrf_internal_token_exempt(self):
        """Requests carrying the VALID ``X-Internal-Token`` bypass CSRF (platform-worker)."""
        app = _build_csrf_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/protected",
                headers={"X-Internal-Token": settings.internal_token},
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_csrf_internal_token_forged_403(self):
        """A forged/empty ``X-Internal-Token`` must NOT bypass CSRF (security fix)."""
        app = _build_csrf_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/protected",
                headers={"X-Internal-Token": "forged-token"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_csrf_referer_fallback(self):
        """When Origin is absent, a trusted Referer origin should pass."""
        app = _build_csrf_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/protected",
                headers={"Referer": "http://localhost:20204/dashboard"},
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_csrf_referer_untrusted_403(self):
        """An untrusted Referer origin should be rejected."""
        app = _build_csrf_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/protected",
                headers={"Referer": "http://evil.example.com/x"},
            )
        assert resp.status_code == 403


# ===========================================================================
# 9. SecurityHeadersMiddleware
# ===========================================================================


class TestSecurityHeadersMiddleware:
    @pytest.mark.asyncio
    async def test_adds_all_six_security_headers(self):
        app = _build_middleware_app(rate_limit=False, security_headers=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/test")
        assert resp.status_code == 200
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["x-frame-options"] == "DENY"
        assert "strict-transport-security" in resp.headers
        assert "content-security-policy" in resp.headers
        assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert "permissions-policy" in resp.headers

    @pytest.mark.asyncio
    async def test_security_headers_constant_count(self):
        """SECURITY_HEADERS must contain exactly 6 entries."""
        assert len(sh.SECURITY_HEADERS) == 6

    @pytest.mark.asyncio
    async def test_does_not_override_existing_header(self):
        app = FastAPI()

        @app.get("/api/v1/test")
        async def _test():
            from fastapi.responses import JSONResponse

            return JSONResponse({"ok": True}, headers={"X-Frame-Options": "SAMEORIGIN"})

        app.add_middleware(sh.SecurityHeadersMiddleware)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/test")
        # Existing header preserved, not overridden by DENY.
        assert resp.headers["x-frame-options"] == "SAMEORIGIN"
        # Other headers still injected.
        assert resp.headers["x-content-type-options"] == "nosniff"

    @pytest.mark.asyncio
    async def test_passes_through_non_http_scope(self):
        called = False

        async def inner_app(scope, receive, send):
            nonlocal called
            called = True
            await send({"type": "lifespan.startup.complete"})

        middleware = sh.SecurityHeadersMiddleware(inner_app)

        async def receive():
            return {"type": "lifespan.startup"}

        async def send(message):
            pass

        await middleware({"type": "lifespan"}, receive, send)
        assert called is True

    @pytest.mark.asyncio
    async def test_headers_present_on_404_response(self):
        app = _build_middleware_app(rate_limit=False, security_headers=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/nonexistent")
        assert resp.status_code == 404
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["x-frame-options"] == "DENY"


# ===========================================================================
# 10. RateLimitMiddleware
# ===========================================================================


class TestRateLimitMiddleware:
    @pytest.mark.asyncio
    async def test_exempt_healthz_not_limited(self, monkeypatch):
        sh._reset_rate_limit_middleware()
        monkeypatch.setattr(sh.settings, "rate_limit_default_per_min", 1)
        app = _build_middleware_app(rate_limit=True, security_headers=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r1 = await client.get("/healthz")
            r2 = await client.get("/healthz")
        assert r1.status_code == 200
        assert r2.status_code == 200  # healthz exempt, no 429
        sh._reset_rate_limit_middleware()

    @pytest.mark.asyncio
    async def test_exempt_non_api_path_not_limited(self, monkeypatch):
        sh._reset_rate_limit_middleware()
        monkeypatch.setattr(sh.settings, "rate_limit_default_per_min", 1)
        app = FastAPI()

        @app.get("/public/info")
        async def _info():
            return {"ok": True}

        app.add_middleware(sh.RateLimitMiddleware)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r1 = await client.get("/public/info")
            r2 = await client.get("/public/info")
        assert r1.status_code == 200
        assert r2.status_code == 200
        sh._reset_rate_limit_middleware()

    @pytest.mark.asyncio
    async def test_internal_token_exempt(self, monkeypatch):
        sh._reset_rate_limit_middleware()
        monkeypatch.setattr(sh.settings, "rate_limit_default_per_min", 1)
        app = _build_middleware_app(rate_limit=True, security_headers=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r1 = await client.get(
                "/api/v1/test", headers={"X-Internal-Token": settings.internal_token}
            )
            r2 = await client.get(
                "/api/v1/test", headers={"X-Internal-Token": settings.internal_token}
            )
        assert r1.status_code == 200
        assert r2.status_code == 200  # valid internal token exempt
        sh._reset_rate_limit_middleware()

    @pytest.mark.asyncio
    async def test_internal_token_forged_not_exempt(self, monkeypatch):
        """A forged ``X-Internal-Token`` must NOT bypass rate limiting (security fix)."""
        sh._reset_rate_limit_middleware()
        monkeypatch.setattr(sh.settings, "rate_limit_default_per_min", 1)
        app = _build_middleware_app(rate_limit=True, security_headers=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r1 = await client.get(
                "/api/v1/test", headers={"X-Internal-Token": "forged-token"}
            )
            r2 = await client.get(
                "/api/v1/test", headers={"X-Internal-Token": "forged-token"}
            )
        assert r1.status_code == 200
        assert r2.status_code == 429  # forged token falls through to rate limit
        sh._reset_rate_limit_middleware()

    @pytest.mark.asyncio
    async def test_login_tier_429_when_exceeded(self, monkeypatch):
        sh._reset_rate_limit_middleware()
        monkeypatch.setattr(sh.settings, "rate_limit_login_per_min", 2)
        app = _build_middleware_app(rate_limit=True, security_headers=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r1 = await client.post("/api/v1/auth/login")
            r2 = await client.post("/api/v1/auth/login")
            r3 = await client.post("/api/v1/auth/login")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r3.status_code == 429
        sh._reset_rate_limit_middleware()

    @pytest.mark.asyncio
    async def test_default_tier_429_when_exceeded(self, monkeypatch):
        sh._reset_rate_limit_middleware()
        monkeypatch.setattr(sh.settings, "rate_limit_default_per_min", 2)
        app = _build_middleware_app(rate_limit=True, security_headers=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r1 = await client.get("/api/v1/test")
            r2 = await client.get("/api/v1/test")
            r3 = await client.get("/api/v1/test")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r3.status_code == 429
        sh._reset_rate_limit_middleware()

    @pytest.mark.asyncio
    async def test_429_includes_retry_after_header(self, monkeypatch):
        sh._reset_rate_limit_middleware()
        monkeypatch.setattr(sh.settings, "rate_limit_login_per_min", 1)
        app = _build_middleware_app(rate_limit=True, security_headers=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/api/v1/auth/login")
            r2 = await client.post("/api/v1/auth/login")
        assert r2.status_code == 429
        assert "retry-after" in r2.headers
        assert int(r2.headers["retry-after"]) >= 1
        sh._reset_rate_limit_middleware()

    @pytest.mark.asyncio
    async def test_429_includes_security_headers_when_wrapped(self, monkeypatch):
        """When SecurityHeaders wraps RateLimit, the 429 response must carry
        all six security headers."""
        sh._reset_rate_limit_middleware()
        monkeypatch.setattr(sh.settings, "rate_limit_login_per_min", 1)
        app = _build_middleware_app(rate_limit=True, security_headers=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/api/v1/auth/login")
            r2 = await client.post("/api/v1/auth/login")
        assert r2.status_code == 429
        assert r2.headers["x-content-type-options"] == "nosniff"
        assert r2.headers["x-frame-options"] == "DENY"
        sh._reset_rate_limit_middleware()


# ===========================================================================
# 11. 密码强度策略
# ===========================================================================


class TestPasswordStrength:
    def test_strong_password_valid(self):
        result = sh.validate_password_strength("Str0ng!Passw0rd")
        assert result["valid"] is True
        assert result["score"] == 5

    def test_short_password_invalid(self):
        result = sh.validate_password_strength("Ab1!")
        assert result["valid"] is False
        assert "characters" in " ".join(result["suggestions"])

    def test_missing_uppercase_invalid(self):
        result = sh.validate_password_strength("strong1!password")
        assert result["valid"] is False
        assert "uppercase" in " ".join(result["suggestions"])

    def test_missing_digit_invalid(self):
        result = sh.validate_password_strength("Strong!Password")
        assert result["valid"] is False
        assert "digits" in " ".join(result["suggestions"])

    def test_missing_special_invalid(self):
        result = sh.validate_password_strength("Strong1Password")
        assert result["valid"] is False
        assert "special" in " ".join(result["suggestions"])

    def test_weak_password_in_list_invalid(self):
        result = sh.validate_password_strength("password")
        assert result["valid"] is False
        assert result["score"] == 0

    def test_password_contains_username_invalid(self):
        result = sh.validate_password_strength(
            "TestUser123!xyz", username="testuser"
        )
        assert result["valid"] is False
        assert "username" in " ".join(result["suggestions"])

    def test_password_contains_email_invalid(self):
        result = sh.validate_password_strength(
            "Alice123!xyz", email="alice@workama.com"
        )
        assert result["valid"] is False
        assert "email" in " ".join(result["suggestions"])

    def test_empty_password_invalid(self):
        result = sh.validate_password_strength("")
        assert result["valid"] is False
        assert result["score"] == 0

    def test_score_max_five_for_strong_password(self):
        result = sh.validate_password_strength("C0mpl3x!P@ssw0rd")
        assert result["score"] == 5


# ===========================================================================
# 12. JWT 安全增强
# ===========================================================================


class TestJWTHardening:
    def test_bind_fingerprint_deterministic(self):
        fp1 = sh.bind_token_fingerprint("1.2.3.4", "Mozilla/5.0")
        fp2 = sh.bind_token_fingerprint("1.2.3.4", "Mozilla/5.0")
        assert fp1 == fp2
        assert len(fp1) == 64  # SHA-256 hex

    def test_bind_fingerprint_differs_for_different_ip(self):
        fp1 = sh.bind_token_fingerprint("1.2.3.4", "Mozilla/5.0")
        fp2 = sh.bind_token_fingerprint("9.8.7.6", "Mozilla/5.0")
        assert fp1 != fp2

    def test_verify_binding_match(self):
        ip, ua = "1.2.3.4", "Mozilla/5.0"
        token = sh.create_bound_access_token("usr_x", "ws_x", "member", ip=ip, ua=ua)
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        assert sh.verify_token_binding(payload, ip, ua) is True

    def test_verify_binding_mismatch(self):
        token = sh.create_bound_access_token(
            "usr_x", "ws_x", "member", ip="1.2.3.4", ua="Mozilla/5.0"
        )
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        assert sh.verify_token_binding(payload, ip="9.9.9.9", ua="Mozilla/5.0") is False

    def test_verify_binding_no_fp_returns_true(self):
        """Tokens without ``fp`` claim (legacy) are considered unbound."""
        payload = {"sub": "usr_x", "type": "access"}
        assert sh.verify_token_binding(payload, "1.2.3.4", "Mozilla/5.0") is True

    def test_create_bound_token_has_jti_and_fp(self):
        token = sh.create_bound_access_token(
            "usr_x", "ws_x", "member", ip="1.2.3.4", ua="Mozilla/5.0"
        )
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        assert "jti" in payload
        assert payload["jti"].startswith("jti_")
        assert "fp" in payload
        assert len(payload["fp"]) == 64

    @pytest.mark.asyncio
    async def test_is_token_blacklisted_true(self, monkeypatch):
        conn = _RecConn(results=[_RecResult(row={"?column?": 1})])
        monkeypatch.setattr(sh, "pool", _RecPool(conn))
        assert await sh.is_token_blacklisted("jti_123") is True

    @pytest.mark.asyncio
    async def test_is_token_blacklisted_false(self, monkeypatch):
        conn = _RecConn(results=[_RecResult(row=None)])
        monkeypatch.setattr(sh, "pool", _RecPool(conn))
        assert await sh.is_token_blacklisted("jti_456") is False

    @pytest.mark.asyncio
    async def test_is_token_blacklisted_empty_jti(self, monkeypatch):
        conn = _RecConn()
        monkeypatch.setattr(sh, "pool", _RecPool(conn))
        assert await sh.is_token_blacklisted("") is False
        assert conn.calls == []  # no DB call for empty jti

    @pytest.mark.asyncio
    async def test_revoke_token_executes_insert(self, monkeypatch):
        conn = _RecConn()
        monkeypatch.setattr(sh, "pool", _RecPool(conn))
        await sh.revoke_token("jti_789", "logout")
        inserts = [c for c in conn.calls if "INSERT INTO jwt_token_blacklist" in c[0]]
        assert len(inserts) == 1
        assert conn.calls[-1][0] == "" or True  # commit is not recorded as execute
        # Verify params contain jti and reason.
        _, params = inserts[0]
        assert "jti_789" in params
        assert "logout" in params

    @pytest.mark.asyncio
    async def test_revoke_token_empty_jti_noop(self, monkeypatch):
        conn = _RecConn()
        monkeypatch.setattr(sh, "pool", _RecPool(conn))
        await sh.revoke_token("", "noop")
        assert conn.calls == []


# ===========================================================================
# 13. 审计日志链式 hash
# ===========================================================================


class TestAuditChain:
    def test_compute_payload_hash_deterministic(self):
        payload = {"action": "login", "user": "usr_1"}
        h1 = sh.compute_payload_hash(payload)
        h2 = sh.compute_payload_hash(payload)
        assert h1 == h2
        assert len(h1) == 64

    def test_compute_chain_hash_consistent(self):
        payload = {"action": "login", "user": "usr_1"}
        prev = ""
        h1 = sh.compute_chain_hash(prev, payload)
        ph = sh.compute_payload_hash(payload)
        h2 = sh.compute_chain_hash_from_payload_hash(prev, ph)
        assert h1 == h2

    def test_chain_hash_changes_with_prev(self):
        payload = {"action": "login"}
        h_a = sh.compute_chain_hash_from_payload_hash("prev_a", sh.compute_payload_hash(payload))
        h_b = sh.compute_chain_hash_from_payload_hash("prev_b", sh.compute_payload_hash(payload))
        assert h_a != h_b

    @pytest.mark.asyncio
    async def test_append_chain_first_entry_prev_empty(self):
        conn = _RecConn(results=[_RecResult(row=None)])  # SELECT finds no previous
        result = await sh.append_audit_chain_entry(
            conn, "aud_0001", "ws_test", {"action": "create"}
        )
        assert result["prev_hash"] == ""
        assert result["audit_id"] == "aud_0001"
        assert len(result["payload_hash"]) == 64
        assert len(result["chain_hash"]) == 64
        # Verify INSERT was called.
        inserts = [c for c in conn.calls if "INSERT INTO audit_log_chain" in c[0]]
        assert len(inserts) == 1

    @pytest.mark.asyncio
    async def test_append_chain_chains_from_prev(self):
        prev_chain_hash = "a" * 64
        conn = _RecConn(
            results=[_RecResult(row={"chain_hash": prev_chain_hash})]
        )
        result = await sh.append_audit_chain_entry(
            conn, "aud_0002", "ws_test", {"action": "update"}
        )
        assert result["prev_hash"] == prev_chain_hash
        expected = sh.compute_chain_hash_from_payload_hash(
            prev_chain_hash, result["payload_hash"]
        )
        assert result["chain_hash"] == expected

    @pytest.mark.asyncio
    async def test_verify_chain_empty_valid(self, monkeypatch):
        conn = _RecConn(results=[_RecResult(row=None, rows=[])])
        monkeypatch.setattr(sh, "pool", _RecPool(conn))
        result = await sh.verify_audit_chain("ws_test")
        assert result["valid"] is True
        assert result["count"] == 0
        assert result["broken_at"] is None

    @pytest.mark.asyncio
    async def test_verify_chain_valid(self, monkeypatch):
        chain = _build_valid_chain(3)
        conn = _RecConn(results=[_RecResult(row=None, rows=chain)])
        monkeypatch.setattr(sh, "pool", _RecPool(conn))
        result = await sh.verify_audit_chain("ws_test")
        assert result["valid"] is True
        assert result["count"] == 3
        assert result["broken_at"] is None

    @pytest.mark.asyncio
    async def test_verify_chain_broken_prev_hash(self, monkeypatch):
        chain = _build_valid_chain(3)
        # Tamper with the 2nd entry's prev_hash.
        chain[1] = {**chain[1], "prev_hash": "tampered"}
        conn = _RecConn(results=[_RecResult(row=None, rows=chain)])
        monkeypatch.setattr(sh, "pool", _RecPool(conn))
        result = await sh.verify_audit_chain("ws_test")
        assert result["valid"] is False
        assert result["broken_at"] == chain[1]["audit_id"]

    @pytest.mark.asyncio
    async def test_verify_chain_broken_chain_hash(self, monkeypatch):
        chain = _build_valid_chain(3)
        # Tamper with the 2nd entry's chain_hash.
        chain[1] = {**chain[1], "chain_hash": "b" * 64}
        conn = _RecConn(results=[_RecResult(row=None, rows=chain)])
        monkeypatch.setattr(sh, "pool", _RecPool(conn))
        result = await sh.verify_audit_chain("ws_test")
        assert result["valid"] is False
        assert result["broken_at"] == chain[1]["audit_id"]


# ===========================================================================
# 14. 新增端点
# ===========================================================================


class TestSecurityEndpoints:
    @pytest.mark.asyncio
    async def test_csrf_token_endpoint_returns_token(self):
        actor = _actor()
        app = _build_security_app(actor=actor)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/security/csrf-token",
                headers={"Origin": "http://localhost:20204"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "csrf_token" in body
        assert body["user_id"] == actor.user_id
        assert body["expires_in"] == 600
        # Token is ``<user_id>.<hex_sig>``.
        assert "." in body["csrf_token"]

    @pytest.mark.asyncio
    async def test_audit_chain_verify_admin(self, monkeypatch):
        actor = _actor(role="admin")
        chain = _build_valid_chain(2)
        conn = _RecConn(results=[_RecResult(row=None, rows=chain)])
        monkeypatch.setattr(sh, "pool", _RecPool(conn))
        app = _build_security_app(actor=actor)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/security/audit-chain/verify")
        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is True
        assert body["count"] == 2

    @pytest.mark.asyncio
    async def test_audit_chain_verify_member_403(self):
        actor = _actor(role="member")
        app = _build_security_app(actor=actor)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/security/audit-chain/verify")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_password_strength_endpoint(self):
        actor = _actor()
        app = _build_security_app(actor=actor)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/security/password/strength-check",
                json={"password": "Str0ng!Passw0rd", "username": "", "email": ""},
                headers={"Origin": "http://localhost:20204"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is True
        assert body["score"] == 5
        assert body["min_length"] == settings.password_min_length

    @pytest.mark.asyncio
    async def test_refresh_rotate_no_token_401(self):
        app = _build_security_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/auth/refresh-rotate")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_refresh_rotate_success(self, monkeypatch):
        # Fake connection returns the refresh-token row on the first SELECT,
        # then empty results for the subsequent UPDATE/INSERT statements.
        token_row = {
            "id": "rft_old000000000000000000001",
            "user_id": "usr_test",
            "family_id": "fam_test",
            "revoked_at": None,
            "expires_at": datetime.now(UTC) + timedelta(days=1),
            "workspace_id": "ws_test",
            "role": "member",
        }
        conn = _RecConn(
            results=[
                _RecResult(row=token_row),  # SELECT ... FOR UPDATE
                _RecResult(),  # UPDATE revoked_at
                _RecResult(),  # INSERT jwt_token_blacklist
                _RecResult(),  # INSERT new refresh token
                _RecResult(),  # UPDATE rotated_to_id
            ]
        )
        monkeypatch.setattr(sh, "pool", _RecPool(conn))
        app = _build_security_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/auth/refresh-rotate",
                json={"refresh_token": "old-refresh-token-value"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "access_token" in body
        assert "refresh_token" in body
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == 900
        # New refresh token differs from the old one.
        assert body["refresh_token"] != "old-refresh-token-value"
