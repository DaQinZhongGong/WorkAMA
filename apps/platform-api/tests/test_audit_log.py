"""审查/审计日志模块 (audit_log) 单元 + 端点测试。

v7.147: 20 个测试覆盖：
- 记录：成功 / actor 字段填充 / 字段校验 / 缺写权限 403 / 未认证 401 (5)
- 查询：分页 / workspace 隔离 / action 过滤 / severity + date_range 过滤 (4)
- 详情：存在 / 不存在 404 / workspace 越权 403 (3)
- 导出：CSV / JSON / 缺写权限 403 (3)
- 统计：group_by=action / group_by=severity + 过滤 (2)
- 辅助函数：audit_log_action 成功 / 非法 action 抛 ValueError (2)
- 集成：record → list → get 全链路 (1)

所有测试使用 fake pool/connection，不依赖真实 DB / Redis / 网络。
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules import audit_log as al


# ============================================================================
# 测试辅助：fake pool / connection / result
# ============================================================================


class _Result:
    """模拟 psycopg 查询结果。"""

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
    """记录 execute 调用并按序返回配置的结果。"""

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
    """模拟连接池。"""

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


def _actor(
    *,
    capabilities=("audit:*",),
    workspace_id="wsp_test",
    user_id="usr_test",
    role="admin",
    email="admin@workama.example.com",
) -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email=email,
        display_name="Admin",
        onboarding_completed=True,
        capabilities=capabilities,
    )


def _row(**overrides) -> dict:
    base = {
        "id": "aud_1",
        "workspace_id": "wsp_test",
        "actor_id": "usr_test",
        "actor_email": "admin@workama.example.com",
        "action": "create",
        "resource_type": "user",
        "resource_id": "usr_target",
        "severity": "info",
        "description": "created user",
        "source_ip": "10.0.0.1",
        "user_agent": "curl/8",
        "request_id": "req_1",
        "metadata": {"foo": "bar"},
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(al.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


# ============================================================================
# 1. 记录审计事件
# ============================================================================


class TestRecord:
    """POST /api/v1/audit-logs 记录审计事件。"""

    @pytest.mark.asyncio
    async def test_record_audit_log_success(self, monkeypatch):
        """POST 成功返回 201 并包含完整字段。"""
        row = _row()
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(al, "pool", _Pool(conn))
        # 基础 record/list/get 流程不耦合后台 SIEM 投递；SIEM 由专项测试覆盖。
        monkeypatch.setattr(al, "_trigger_siem_delivery", lambda *args, **kwargs: None)

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/audit-logs",
                json={
                    "action": "create",
                    "resource_type": "user",
                    "resource_id": "usr_target",
                    "severity": "info",
                    "description": "created user",
                    "metadata": {"foo": "bar"},
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["action"] == "create"
        assert body["resource_type"] == "user"
        assert body["severity"] == "info"
        assert body["metadata"] == {"foo": "bar"}
        assert "INSERT INTO audit_log" in conn.calls[0][0]

    @pytest.mark.asyncio
    async def test_record_audit_log_sets_actor_from_token(self, monkeypatch):
        """POST actor_id/actor_email/workspace_id 来自 actor 而非请求体。"""
        row = _row(actor_id="usr_test", workspace_id="wsp_test")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(al, "pool", _Pool(conn))
        # 基础 record/list/get 流程不耦合后台 SIEM 投递；SIEM 由专项测试覆盖。
        monkeypatch.setattr(al, "_trigger_siem_delivery", lambda *args, **kwargs: None)

        app = _app(actor=_actor(user_id="usr_test", workspace_id="wsp_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/audit-logs",
                json={"action": "login", "resource_type": "session"},
            )
        assert resp.status_code == 201
        insert_params = conn.calls[0][1]
        assert insert_params[1] == "wsp_test"
        assert insert_params[2] == "usr_test"

    @pytest.mark.asyncio
    async def test_record_audit_log_rejects_invalid_action(self):
        """POST 非法 action 触发 422 校验错误。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/audit-logs",
                json={"action": "hack", "resource_type": "user"},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_record_audit_log_requires_write_capability(self, monkeypatch):
        """member 角色（仅 audit:read）写审计日志返回 403。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(al, "pool", _Pool(conn))
        # 基础 record/list/get 流程不耦合后台 SIEM 投递；SIEM 由专项测试覆盖。
        monkeypatch.setattr(al, "_trigger_siem_delivery", lambda *args, **kwargs: None)

        app = _app(actor=_actor(role="member", capabilities=("audit:read",)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/audit-logs",
                json={"action": "create", "resource_type": "user"},
            )
        assert resp.status_code == 403
        assert len(conn.calls) == 0

    @pytest.mark.asyncio
    async def test_record_audit_log_requires_authentication(self):
        """未认证请求返回 401。"""
        app = _app(actor=None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/audit-logs",
                json={"action": "create", "resource_type": "user"},
            )
        assert resp.status_code == 401


# ============================================================================
# 2. 查询审计日志
# ============================================================================


class TestList:
    """GET /api/v1/audit-logs 查询审计日志。"""

    @pytest.mark.asyncio
    async def test_list_audit_logs_pagination(self, monkeypatch):
        """GET 分页返回审计日志列表。"""
        rows = [_row(id="aud_1"), _row(id="aud_2")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(al, "pool", _Pool(conn))
        # 基础 record/list/get 流程不耦合后台 SIEM 投递；SIEM 由专项测试覆盖。
        monkeypatch.setattr(al, "_trigger_siem_delivery", lambda *args, **kwargs: None)

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/audit-logs?limit=10&offset=0")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert body["limit"] == 10
        assert body["offset"] == 0
        assert len(body["items"]) == 2

    @pytest.mark.asyncio
    async def test_list_audit_logs_workspace_isolation(self, monkeypatch):
        """GET SQL 强制按 actor.workspace_id 过滤。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(al, "pool", _Pool(conn))
        # 基础 record/list/get 流程不耦合后台 SIEM 投递；SIEM 由专项测试覆盖。
        monkeypatch.setattr(al, "_trigger_siem_delivery", lambda *args, **kwargs: None)

        app = _app(actor=_actor(workspace_id="wsp_isolated", role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/audit-logs")
        assert resp.status_code == 200
        query, params = conn.calls[0]
        assert "WHERE workspace_id = %s" in query
        assert params[0] == "wsp_isolated"

    @pytest.mark.asyncio
    async def test_list_audit_logs_action_filter(self, monkeypatch):
        """GET ?action=login 仅返回 login 事件。"""
        rows = [_row(id="aud_1", action="login")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(al, "pool", _Pool(conn))
        # 基础 record/list/get 流程不耦合后台 SIEM 投递；SIEM 由专项测试覆盖。
        monkeypatch.setattr(al, "_trigger_siem_delivery", lambda *args, **kwargs: None)

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/audit-logs?action=login")
        assert resp.status_code == 200
        body = resp.json()
        assert all(item["action"] == "login" for item in body["items"])
        query, params = conn.calls[0]
        assert "AND action = %s" in query
        assert "login" in params

    @pytest.mark.asyncio
    async def test_list_audit_logs_severity_and_date_range(self, monkeypatch):
        """GET ?severity=critical&start=...&end=... 组合过滤。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(al, "pool", _Pool(conn))
        # 基础 record/list/get 流程不耦合后台 SIEM 投递；SIEM 由专项测试覆盖。
        monkeypatch.setattr(al, "_trigger_siem_delivery", lambda *args, **kwargs: None)

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/audit-logs?severity=critical"
                "&start=2026-01-01T00:00:00Z&end=2026-12-31T23:59:59Z"
            )
        assert resp.status_code == 200
        query, params = conn.calls[0]
        assert "AND severity = %s" in query
        assert "AND created_at >= %s" in query
        assert "AND created_at <= %s" in query
        assert "critical" in params


# ============================================================================
# 3. 单条详情
# ============================================================================


class TestGetDetail:
    """GET /api/v1/audit-logs/{id} 单条审计事件详情。"""

    @pytest.mark.asyncio
    async def test_get_audit_log_exists(self, monkeypatch):
        """GET /{id} 返回审计事件详情。"""
        row = _row(id="aud_1")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(al, "pool", _Pool(conn))
        # 基础 record/list/get 流程不耦合后台 SIEM 投递；SIEM 由专项测试覆盖。
        monkeypatch.setattr(al, "_trigger_siem_delivery", lambda *args, **kwargs: None)

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/audit-logs/aud_1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "aud_1"
        assert body["action"] == "create"

    @pytest.mark.asyncio
    async def test_get_audit_log_returns_404_when_missing(self, monkeypatch):
        """GET /{id} 不存在返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(al, "pool", _Pool(conn))
        # 基础 record/list/get 流程不耦合后台 SIEM 投递；SIEM 由专项测试覆盖。
        monkeypatch.setattr(al, "_trigger_siem_delivery", lambda *args, **kwargs: None)

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/audit-logs/missing")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_audit_log_returns_403_cross_workspace(self, monkeypatch):
        """GET /{id} 事件属于其他 workspace 返回 403。"""
        row = _row(id="aud_1", workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(al, "pool", _Pool(conn))
        # 基础 record/list/get 流程不耦合后台 SIEM 投递；SIEM 由专项测试覆盖。
        monkeypatch.setattr(al, "_trigger_siem_delivery", lambda *args, **kwargs: None)

        app = _app(actor=_actor(workspace_id="wsp_test", role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/audit-logs/aud_1")
        assert resp.status_code == 403


# ============================================================================
# 4. 导出
# ============================================================================


class TestExport:
    """GET /api/v1/audit-logs/export 导出审计日志。"""

    @pytest.mark.asyncio
    async def test_export_audit_logs_csv(self, monkeypatch):
        """GET /export?format=csv 返回 CSV 附件。"""
        rows = [_row(id="aud_1"), _row(id="aud_2")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(al, "pool", _Pool(conn))
        # 基础 record/list/get 流程不耦合后台 SIEM 投递；SIEM 由专项测试覆盖。
        monkeypatch.setattr(al, "_trigger_siem_delivery", lambda *args, **kwargs: None)

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/audit-logs/export?format=csv")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        assert "attachment" in resp.headers["content-disposition"]
        body = resp.text
        assert "id,workspace_id,actor_id" in body
        assert "aud_1" in body and "aud_2" in body

    @pytest.mark.asyncio
    async def test_export_audit_logs_json(self, monkeypatch):
        """GET /export?format=json 返回 JSON 附件。"""
        rows = [_row(id="aud_1")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(al, "pool", _Pool(conn))
        # 基础 record/list/get 流程不耦合后台 SIEM 投递；SIEM 由专项测试覆盖。
        monkeypatch.setattr(al, "_trigger_siem_delivery", lambda *args, **kwargs: None)

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/audit-logs/export?format=json")
        assert resp.status_code == 200
        assert "application/json" in resp.headers["content-type"]
        body = resp.json()
        assert body["count"] == 1
        assert body["items"][0]["id"] == "aud_1"

    @pytest.mark.asyncio
    async def test_export_audit_logs_requires_write(self, monkeypatch):
        """member 角色导出审计日志返回 403。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(al, "pool", _Pool(conn))
        # 基础 record/list/get 流程不耦合后台 SIEM 投递；SIEM 由专项测试覆盖。
        monkeypatch.setattr(al, "_trigger_siem_delivery", lambda *args, **kwargs: None)

        app = _app(actor=_actor(role="member", capabilities=("audit:read",)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/audit-logs/export")
        assert resp.status_code == 403
        assert len(conn.calls) == 0


# ============================================================================
# 5. 统计
# ============================================================================


class TestStats:
    """GET /api/v1/audit-logs/stats 统计。"""

    @pytest.mark.asyncio
    async def test_stats_group_by_action(self, monkeypatch):
        """GET /stats?group_by=action 按 action 分组计数。"""
        rows = [
            {"key": "create", "count": 5},
            {"key": "login", "count": 3},
        ]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(al, "pool", _Pool(conn))
        # 基础 record/list/get 流程不耦合后台 SIEM 投递；SIEM 由专项测试覆盖。
        monkeypatch.setattr(al, "_trigger_siem_delivery", lambda *args, **kwargs: None)

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/audit-logs/stats?group_by=action")
        assert resp.status_code == 200
        body = resp.json()
        assert body["group_by"] == "action"
        assert body["buckets"] == {"create": 5, "login": 3}
        assert body["total"] == 8
        query, _ = conn.calls[0]
        assert "GROUP BY action" in query

    @pytest.mark.asyncio
    async def test_stats_group_by_severity_with_filter(self, monkeypatch):
        """GET /stats?group_by=severity&severity=warning 带过滤。"""
        rows = [{"key": "warning", "count": 2}]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(al, "pool", _Pool(conn))
        # 基础 record/list/get 流程不耦合后台 SIEM 投递；SIEM 由专项测试覆盖。
        monkeypatch.setattr(al, "_trigger_siem_delivery", lambda *args, **kwargs: None)

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/audit-logs/stats?group_by=severity&severity=warning"
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["buckets"] == {"warning": 2}
        query, params = conn.calls[0]
        assert "GROUP BY severity" in query
        assert "AND severity = %s" in query
        assert "warning" in params


# ============================================================================
# 6. 辅助函数 audit_log_action
# ============================================================================


class TestAuditLogActionHelper:
    """audit_log_action 辅助函数测试。"""

    @pytest.mark.asyncio
    async def test_helper_inserts_and_returns_summary(self, monkeypatch):
        """audit_log_action 写入并返回 summary dict。"""
        row = _row(id="aud_new", action="delete", resource_type="document")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(al, "pool", _Pool(conn))
        # 基础 record/list/get 流程不耦合后台 SIEM 投递；SIEM 由专项测试覆盖。
        monkeypatch.setattr(al, "_trigger_siem_delivery", lambda *args, **kwargs: None)

        result = await al.audit_log_action(
            _actor(),
            "delete",
            "document",
            "doc_1",
            severity="critical",
            description="deleted doc",
            metadata={"reason": "policy"},
        )
        assert result["id"] == "aud_new"
        assert result["action"] == "delete"
        assert result["resource_type"] == "document"
        assert "INSERT INTO audit_log" in conn.calls[0][0]
        insert_params = conn.calls[0][1]
        assert "critical" in insert_params

    @pytest.mark.asyncio
    async def test_helper_rejects_invalid_action(self):
        """audit_log_action 非法 action 抛 ValueError。"""
        with pytest.raises(ValueError):
            await al.audit_log_action(_actor(), "hack", "user")


# ============================================================================
# 7. 集成测试
# ============================================================================


class TestIntegration:
    """record → list → get 全链路集成测试。"""

    @pytest.mark.asyncio
    async def test_record_list_get_full_flow(self, monkeypatch):
        """record (INSERT) → list (SELECT) → get (SELECT) 全链路。"""
        recorded = _row(id="aud_new", action="login", resource_type="session")
        listed = [_row(id="aud_new", action="login")]
        detail = _row(id="aud_new", action="login", resource_type="session")
        conn = _RecordingConnection(
            results=[
                _Result(row=recorded),
                _Result(rows=listed),
                _Result(row=detail),
            ]
        )
        monkeypatch.setattr(al, "pool", _Pool(conn))
        # 基础 record/list/get 流程不耦合后台 SIEM 投递；SIEM 由专项测试覆盖。
        monkeypatch.setattr(al, "_trigger_siem_delivery", lambda *args, **kwargs: None)

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            rec = await client.post(
                "/api/v1/audit-logs",
                json={"action": "login", "resource_type": "session"},
            )
            lst = await client.get("/api/v1/audit-logs")
            get = await client.get("/api/v1/audit-logs/aud_new")
        assert rec.status_code == 201
        assert lst.status_code == 200
        assert get.status_code == 200
        assert rec.json()["id"] == "aud_new"
        assert lst.json()["count"] == 1
        assert get.json()["action"] == "login"
        assert len(conn.calls) == 3
