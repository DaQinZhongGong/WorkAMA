"""M12 企业审计增强（SIEM 集成 + legal hold + 批量导出多格式）测试。

覆盖（49 个测试）：
- legal hold：创建 / 列表 / 释放 / 释放需 admin / 释放需 reason / 保留期删除 423 /
  不存在 404 / 跨 workspace 403 (8)
- 批量导出：json / csv / syslog / cef / 时间范围过滤 / 事件类型过滤 / 空结果 / 大结果分页 (8)
- SIEM 配置：创建 / 获取 / 更新(upsert) / 禁用(删除) / 不存在 404 / 跨 workspace 隔离 / admin 鉴权 (7)
- SIEM 测试连接：TCP 成功 / TCP 失败 / UDP 成功 / endpoint 格式校验 / syslog 格式 / cef 格式 (6)
- SIEM 投递：创建触发投递 / 投递成功 / 投递失败重试 / 投递记录写入 / 未配置不投递 (5)
- 辅助函数：_format_syslog / _format_cef / _hold_matches_event / _check_legal_hold /
  _send_to_siem_endpoint / _trigger_siem_delivery / _require_admin / _record_delivery (15)

所有测试使用 fake pool/connection，socket 用 monkeypatch mock，不依赖真实 DB/网络。
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from workama_platform.core import Actor, get_actor
from workama_platform.modules import audit_log as al


# ============================================================================
# 测试辅助：fake pool / connection / result（与 test_audit_log.py 同风格）
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


class _FakeSocket:
    """模拟 socket：记录发送内容，支持 TCP/UDP 接口。"""

    def __init__(self):
        self.sent: bytes | None = None
        self.closed = False

    def settimeout(self, _t):
        pass

    def sendall(self, data):
        self.sent = data

    def sendto(self, data, _addr):
        self.sent = data

    def close(self):
        self.closed = True


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
    """审计事件行。"""
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


def _hold_row(**overrides) -> dict:
    """legal hold 行。"""
    base = {
        "id": "alh_1",
        "workspace_id": "wsp_test",
        "hold_reason": "litigation hold",
        "event_filter": {"event_types": ["delete"], "start_time": None, "end_time": None},
        "created_by": "usr_test",
        "created_at": datetime.now(UTC),
        "released_at": None,
        "released_by": None,
        "release_reason": None,
    }
    base.update(overrides)
    return base


def _siem_row(**overrides) -> dict:
    """SIEM 配置行。"""
    base = {
        "id": "siem_1",
        "workspace_id": "wsp_test",
        "endpoint": "siem.example.com:514",
        "protocol": "tcp",
        "format": "syslog",
        "api_key": None,
        "enabled": True,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(al.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


@pytest.fixture(autouse=True)
def _clear_siem_registry():
    """每个测试前后清空 SIEM 启用集合，保证隔离。"""
    al._SIEM_ENABLED_WORKSPACES.clear()
    yield
    al._SIEM_ENABLED_WORKSPACES.clear()


# ============================================================================
# 1. legal hold
# ============================================================================


class TestLegalHold:
    """legal hold 创建 / 列表 / 释放 / 鉴权 / 423 / 404 / 403。"""

    @pytest.mark.asyncio
    async def test_create_legal_hold_success(self, monkeypatch):
        """POST /legal-holds 创建成功返回 201 并写入 event_filter。"""
        hold = _hold_row()
        conn = _RecordingConnection(results=[_Result(row=hold)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/audit-logs/legal-holds",
                json={"hold_reason": "litigation hold", "event_types": ["delete"]},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["hold_reason"] == "litigation hold"
        assert body["event_filter"]["event_types"] == ["delete"]
        assert "INSERT INTO audit_legal_hold" in conn.calls[0][0]

    @pytest.mark.asyncio
    async def test_list_legal_holds_pagination(self, monkeypatch):
        """GET /legal-holds 分页返回列表。"""
        rows = [_hold_row(id="alh_1"), _hold_row(id="alh_2")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/audit-logs/legal-holds?limit=10&offset=0")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert body["limit"] == 10
        assert body["offset"] == 0

    @pytest.mark.asyncio
    async def test_release_legal_hold_success(self, monkeypatch):
        """DELETE /legal-holds/{id} 释放成功并记录 release_reason。"""
        hold = _hold_row()
        released = _hold_row(released_at=datetime.now(UTC), released_by="usr_test",
                             release_reason="case closed")
        conn = _RecordingConnection(results=[_Result(row=hold), _Result(row=released)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.request(
                "DELETE",
                "/api/v1/audit-logs/legal-holds/alh_1",
                json={"release_reason": "case closed"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["release_reason"] == "case closed"
        assert body["released_by"] == "usr_test"
        # 第二次 execute 应为 UPDATE
        assert "UPDATE audit_legal_hold" in conn.calls[1][0]

    @pytest.mark.asyncio
    async def test_release_legal_hold_requires_admin(self, monkeypatch):
        """member 角色释放 legal hold 返回 403。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member", capabilities=("audit:read",)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.request(
                "DELETE",
                "/api/v1/audit-logs/legal-holds/alh_1",
                json={"release_reason": "x"},
            )
        assert resp.status_code == 403
        assert len(conn.calls) == 0

    @pytest.mark.asyncio
    async def test_release_legal_hold_requires_reason(self):
        """缺少 release_reason 触发 422 校验错误。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.request(
                "DELETE",
                "/api/v1/audit-logs/legal-holds/alh_1",
                json={},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_release_legal_hold_not_found(self, monkeypatch):
        """释放不存在的 legal hold 返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.request(
                "DELETE",
                "/api/v1/audit-logs/legal-holds/missing",
                json={"release_reason": "x"},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_release_legal_hold_cross_workspace_403(self, monkeypatch):
        """释放属于其他 workspace 的 legal hold 返回 403。"""
        hold = _hold_row(workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=hold)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.request(
                "DELETE",
                "/api/v1/audit-logs/legal-holds/alh_1",
                json={"release_reason": "x"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_check_legal_hold_blocks_with_423(self, monkeypatch):
        """保留期内匹配事件触发 423 Locked（直接测试 _check_legal_hold）。"""
        hold = _hold_row(event_filter={"event_types": ["delete"], "start_time": None,
                                       "end_time": None})
        conn = _RecordingConnection(results=[_Result(rows=[hold])])
        event = {"action": "delete", "created_at": datetime.now(UTC)}
        with pytest.raises(HTTPException) as exc:
            await al._check_legal_hold(conn, event, "wsp_test")
        assert exc.value.status_code == 423
        assert "legal hold" in exc.value.detail.lower()


# ============================================================================
# 2. 批量导出
# ============================================================================


class TestBatchExport:
    """POST /export/batch 多格式导出 + 过滤。"""

    @pytest.mark.asyncio
    async def test_batch_export_json(self, monkeypatch):
        """json 格式返回 JSON 数组附件。"""
        rows = [_row(id="aud_1"), _row(id="aud_2")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/audit-logs/export/batch",
                json={"format": "json"},
            )
        assert resp.status_code == 200
        assert "application/json" in resp.headers["content-type"]
        body = resp.json()
        assert body["count"] == 2
        assert body["items"][0]["id"] == "aud_1"

    @pytest.mark.asyncio
    async def test_batch_export_csv(self, monkeypatch):
        """csv 格式返回含表头的 CSV。"""
        rows = [_row(id="aud_1")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/audit-logs/export/batch",
                json={"format": "csv"},
            )
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        assert "id,workspace_id,actor_id" in resp.text
        assert "aud_1" in resp.text

    @pytest.mark.asyncio
    async def test_batch_export_syslog(self, monkeypatch):
        """syslog 格式返回 RFC 5424 报文（每事件一行）。"""
        rows = [_row(id="aud_1"), _row(id="aud_2")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/audit-logs/export/batch",
                json={"format": "syslog"},
            )
        assert resp.status_code == 200
        assert "syslog" in resp.headers["content-type"]
        lines = resp.text.strip().split("\n")
        assert len(lines) == 2
        assert lines[0].startswith("<")  # syslog 以 <priority> 开头

    @pytest.mark.asyncio
    async def test_batch_export_cef(self, monkeypatch):
        """cef 格式返回 Common Event Format 报文。"""
        rows = [_row(id="aud_1")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/audit-logs/export/batch",
                json={"format": "cef"},
            )
        assert resp.status_code == 200
        assert "cef" in resp.headers["content-type"]
        assert resp.text.startswith("CEF:0|WorkAMA|Platform|")

    @pytest.mark.asyncio
    async def test_batch_export_time_range_filter(self, monkeypatch):
        """时间范围过滤：SQL 包含 created_at >= / <=。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/audit-logs/export/batch",
                json={
                    "format": "json",
                    "start": "2026-01-01T00:00:00Z",
                    "end": "2026-12-31T23:59:59Z",
                },
            )
        assert resp.status_code == 200
        query, params = conn.calls[0]
        assert "AND created_at >= %s" in query
        assert "AND created_at <= %s" in query

    @pytest.mark.asyncio
    async def test_batch_export_event_type_filter(self, monkeypatch):
        """事件类型过滤：SQL 包含 action = ANY(%s)。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/audit-logs/export/batch",
                json={"format": "json", "event_types": ["login", "delete"]},
            )
        assert resp.status_code == 200
        query, params = conn.calls[0]
        assert "AND action = ANY(%s)" in query
        assert params[1] == ["login", "delete"]

    @pytest.mark.asyncio
    async def test_batch_export_empty_result(self, monkeypatch):
        """空结果返回 count=0。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/audit-logs/export/batch",
                json={"format": "json"},
            )
        assert resp.status_code == 200
        assert resp.json()["count"] == 0
        assert resp.json()["items"] == []

    @pytest.mark.asyncio
    async def test_batch_export_large_result_pagination(self, monkeypatch):
        """大结果分页：limit/offset 透传并回显。"""
        rows = [_row(id=f"aud_{i}") for i in range(3)]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/audit-logs/export/batch",
                json={"format": "json", "limit": 3, "offset": 100},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 3
        # SQL 应包含 LIMIT 与 OFFSET
        query, params = conn.calls[0]
        assert "LIMIT %s OFFSET %s" in query
        assert params[-2] == 3
        assert params[-1] == 100


# ============================================================================
# 3. SIEM 配置
# ============================================================================


class TestSiemConfig:
    """SIEM 配置创建 / 获取 / 更新 / 禁用 / 404 / 跨 workspace / admin 鉴权。"""

    @pytest.mark.asyncio
    async def test_create_siem_config_success(self, monkeypatch):
        """POST /siem/config 创建配置成功，并加入启用集合。"""
        cfg = _siem_row(enabled=True)
        conn = _RecordingConnection(results=[_Result(row=cfg)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/audit-logs/siem/config",
                json={"endpoint": "siem.example.com:514", "protocol": "tcp",
                      "format": "syslog", "enabled": True},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["endpoint"] == "siem.example.com:514"
        assert body["enabled"] is True
        assert "wsp_test" in al._SIEM_ENABLED_WORKSPACES
        assert "ON CONFLICT" in conn.calls[0][0]

    @pytest.mark.asyncio
    async def test_get_siem_config_success(self, monkeypatch):
        """GET /siem/config 返回当前 workspace 配置。"""
        cfg = _siem_row()
        conn = _RecordingConnection(results=[_Result(row=cfg)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/audit-logs/siem/config")
        assert resp.status_code == 200
        assert resp.json()["id"] == "siem_1"

    @pytest.mark.asyncio
    async def test_update_siem_config_upsert(self, monkeypatch):
        """POST /siem/config 对已存在配置执行 upsert 更新。"""
        cfg = _siem_row(endpoint="new.example.com:601", format="cef")
        conn = _RecordingConnection(results=[_Result(row=cfg)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/audit-logs/siem/config",
                json={"endpoint": "new.example.com:601", "format": "cef"},
            )
        assert resp.status_code == 201
        assert resp.json()["endpoint"] == "new.example.com:601"
        query, _ = conn.calls[0]
        assert "ON CONFLICT (workspace_id) DO UPDATE" in query

    @pytest.mark.asyncio
    async def test_disable_siem_config_removes_from_registry(self, monkeypatch):
        """enabled=False 的 upsert 从启用集合移除（相当于禁用/删除投递）。"""
        al._SIEM_ENABLED_WORKSPACES.add("wsp_test")
        cfg = _siem_row(enabled=False)
        conn = _RecordingConnection(results=[_Result(row=cfg)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/audit-logs/siem/config",
                json={"endpoint": "siem.example.com:514", "enabled": False},
            )
        assert resp.status_code == 201
        assert resp.json()["enabled"] is False
        assert "wsp_test" not in al._SIEM_ENABLED_WORKSPACES

    @pytest.mark.asyncio
    async def test_get_siem_config_not_found(self, monkeypatch):
        """GET /siem/config 未配置返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/audit-logs/siem/config")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_siem_config_cross_workspace_isolation(self, monkeypatch):
        """GET /siem/config 按 actor.workspace_id 查询，跨 workspace 隔离。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_other", role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/audit-logs/siem/config")
        # wsp_other 无配置 → 404（证明不会泄露其他 workspace 配置）
        assert resp.status_code == 404
        _, params = conn.calls[0]
        assert params[0] == "wsp_other"

    @pytest.mark.asyncio
    async def test_create_siem_config_requires_admin(self, monkeypatch):
        """member 角色配置 SIEM 返回 403。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member", capabilities=("audit:read",)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/audit-logs/siem/config",
                json={"endpoint": "siem.example.com:514"},
            )
        assert resp.status_code == 403
        assert len(conn.calls) == 0


# ============================================================================
# 4. SIEM 测试连接
# ============================================================================


class TestSiemTestConnection:
    """POST /siem/test 连接测试：TCP/UDP 成功失败 + 格式。"""

    @pytest.mark.asyncio
    async def test_siem_test_tcp_success(self, monkeypatch):
        """TCP 连接成功返回 success=True。"""
        cfg = _siem_row(protocol="tcp")
        conn = _RecordingConnection(results=[_Result(row=cfg)])
        monkeypatch.setattr(al, "pool", _Pool(conn))
        fake_sock = _FakeSocket()
        monkeypatch.setattr(al.socket, "create_connection",
                            lambda addr, timeout=None: fake_sock)

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/audit-logs/siem/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["error"] is None
        assert body["latency_ms"] >= 0
        assert fake_sock.sent is not None

    @pytest.mark.asyncio
    async def test_siem_test_tcp_failure(self, monkeypatch):
        """TCP 连接失败返回 success=False 与错误信息。"""

        def _raise(_addr, timeout=None):
            raise OSError("connection refused")

        cfg = _siem_row(protocol="tcp")
        conn = _RecordingConnection(results=[_Result(row=cfg)])
        monkeypatch.setattr(al, "pool", _Pool(conn))
        monkeypatch.setattr(al.socket, "create_connection", _raise)

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/audit-logs/siem/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "connection refused" in body["error"]

    @pytest.mark.asyncio
    async def test_siem_test_udp_success(self, monkeypatch):
        """UDP 发送成功返回 success=True。"""
        cfg = _siem_row(protocol="udp")
        conn = _RecordingConnection(results=[_Result(row=cfg)])
        monkeypatch.setattr(al, "pool", _Pool(conn))
        fake_sock = _FakeSocket()
        monkeypatch.setattr(al.socket, "socket", lambda *a, **k: fake_sock)

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/audit-logs/siem/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert fake_sock.sent is not None

    @pytest.mark.asyncio
    async def test_siem_test_invalid_endpoint_format(self, monkeypatch):
        """endpoint 缺少端口时返回 success=False 与错误。"""
        cfg = _siem_row(endpoint="no-port-host")
        conn = _RecordingConnection(results=[_Result(row=cfg)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/audit-logs/siem/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "invalid endpoint" in body["error"]

    @pytest.mark.asyncio
    async def test_siem_test_syslog_format(self, monkeypatch):
        """format=syslog 时 message_sent 为 RFC 5424 格式。"""
        cfg = _siem_row(format="syslog")
        conn = _RecordingConnection(results=[_Result(row=cfg)])
        monkeypatch.setattr(al, "pool", _Pool(conn))
        monkeypatch.setattr(al.socket, "create_connection",
                            lambda addr, timeout=None: _FakeSocket())

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/audit-logs/siem/test")
        body = resp.json()
        assert body["message_sent"].startswith("<")
        assert "AuditEvent" in body["message_sent"]

    @pytest.mark.asyncio
    async def test_siem_test_cef_format(self, monkeypatch):
        """format=cef 时 message_sent 为 CEF 格式。"""
        cfg = _siem_row(format="cef")
        conn = _RecordingConnection(results=[_Result(row=cfg)])
        monkeypatch.setattr(al, "pool", _Pool(conn))
        monkeypatch.setattr(al.socket, "create_connection",
                            lambda addr, timeout=None: _FakeSocket())

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/audit-logs/siem/test")
        body = resp.json()
        assert body["message_sent"].startswith("CEF:0|WorkAMA|Platform|")


# ============================================================================
# 5. SIEM 投递
# ============================================================================


class TestSiemDelivery:
    """SIEM 投递：触发 / 成功 / 失败重试 / 记录 / 未配置不投递。"""

    @pytest.mark.asyncio
    async def test_event_creation_triggers_delivery(self, monkeypatch):
        """创建审计事件时调用 _trigger_siem_delivery。"""
        triggers: list[tuple] = []
        monkeypatch.setattr(al, "_trigger_siem_delivery",
                            lambda ws, s: triggers.append((ws, s)))
        row = _row(id="aud_new")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/audit-logs",
                json={"action": "create", "resource_type": "user"},
            )
        assert resp.status_code == 201
        assert len(triggers) == 1
        assert triggers[0][0] == "wsp_test"
        assert triggers[0][1]["id"] == "aud_new"

    @pytest.mark.asyncio
    async def test_deliver_to_siem_success(self, monkeypatch):
        """投递成功：记录 status=sent, attempts=1。"""
        cfg = _siem_row(enabled=True, format="syslog")
        conn = _RecordingConnection(results=[_Result(row=cfg)])
        monkeypatch.setattr(al, "pool", _Pool(conn))
        monkeypatch.setattr(al.socket, "create_connection",
                            lambda addr, timeout=None: _FakeSocket())

        await al._deliver_to_siem("wsp_test", _row(id="aud_1"))
        # 第二次 execute 为投递记录 INSERT
        assert len(conn.calls) == 2
        insert_sql, insert_params = conn.calls[1]
        assert "INSERT INTO audit_siem_delivery" in insert_sql
        assert insert_params[4] == "sent"  # status
        assert insert_params[5] == 1  # attempts

    @pytest.mark.asyncio
    async def test_deliver_to_siem_failure_retries(self, monkeypatch):
        """投递失败重试 3 次后记录 status=failed, attempts=3。"""

        def _raise(_addr, timeout=None):
            raise OSError("connection refused")

        cfg = _siem_row(enabled=True)
        conn = _RecordingConnection(results=[_Result(row=cfg)])
        monkeypatch.setattr(al, "pool", _Pool(conn))
        monkeypatch.setattr(al.socket, "create_connection", _raise)

        await al._deliver_to_siem("wsp_test", _row(id="aud_1"), max_retries=3)
        insert_sql, insert_params = conn.calls[1]
        assert "INSERT INTO audit_siem_delivery" in insert_sql
        assert insert_params[4] == "failed"
        assert insert_params[5] == 3  # 重试 3 次
        assert "connection refused" in insert_params[6]  # error_message

    @pytest.mark.asyncio
    async def test_record_delivery_writes_insert(self, monkeypatch):
        """_record_delivery 写入 audit_siem_delivery INSERT。"""
        conn = _RecordingConnection()
        await al._record_delivery(conn, "wsp_test", "aud_1", "siem_1", "sent", 1, None)
        assert len(conn.calls) == 1
        sql, params = conn.calls[0]
        assert "INSERT INTO audit_siem_delivery" in sql
        assert params[1] == "wsp_test"
        assert params[2] == "aud_1"
        assert params[3] == "siem_1"
        assert params[4] == "sent"

    @pytest.mark.asyncio
    async def test_no_delivery_when_siem_not_configured(self, monkeypatch):
        """v7.171：workspace 未启用 SIEM 时，_deliver_to_siem 查 DB 后直接返回，不写投递记录。

        修复后 _trigger_siem_delivery 始终创建后台任务（以 DB 为真相源），
        由 _deliver_to_siem 查 audit_siem_config WHERE enabled=TRUE 决定是否投递，
        不再依赖模块级 _SIEM_ENABLED_WORKSPACES 缓存（多 worker 一致性）。
        """
        row = _row(id="aud_new")
        # conn 只配置 1 个 result（INSERT audit_log RETURNING）；
        # _deliver_to_siem 后台查 audit_siem_config 会拿到默认 _Result(row=None) → cfg None → 不投递
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/audit-logs",
                json={"action": "create", "resource_type": "user"},
            )
        assert resp.status_code == 201
        # 让后台投递任务有机会运行
        await asyncio.sleep(0)
        # _deliver_to_siem 查 audit_siem_config 返回 None（未配置）→ 不写投递记录
        delivery_calls = [c for c in conn.calls if "INSERT INTO audit_siem_delivery" in c[0]]
        assert delivery_calls == []


# ============================================================================
# 6. 辅助函数
# ============================================================================


class TestHelpers:
    """辅助函数单元测试。"""

    def test_format_syslog_basic(self):
        """_format_syslog 生成 RFC 5424 报文，含 priority 与结构化数据。"""
        event = _row()
        line = al._format_syslog(event)
        assert line.startswith("<")
        assert "1 " in line  # version
        assert "workama" in line  # hostname/app-name
        assert "AuditEvent" in line  # msgid
        assert "[workama@1" in line  # structured-data

    def test_format_syslog_severity_priority(self):
        """不同 severity 映射到不同 syslog priority。"""
        info = al._format_syslog(_row(severity="info"))
        critical = al._format_syslog(_row(severity="critical"))
        # facility=4 → 32; info sev=6 → 38; critical sev=2 → 34
        info_pri = int(info[1:info.index(">")])
        crit_pri = int(critical[1:critical.index(">")])
        assert info_pri == 38
        assert crit_pri == 34

    def test_format_cef_basic(self):
        """_format_cef 生成 CEF 报文，含 8 个管道分隔字段。"""
        event = _row()
        line = al._format_cef(event)
        assert line.startswith("CEF:0|WorkAMA|Platform|1.0|")
        # CEF 头部 7 个 '|' 分隔，扩展部分为第 8 段
        assert line.count("|") >= 7
        assert "act=create" in line
        assert "duser=usr_test" in line

    def test_format_cef_severity(self):
        """不同 severity 映射到不同 CEF severity。"""
        info = al._format_cef(_row(severity="info"))
        critical = al._format_cef(_row(severity="critical"))
        # info→3, critical→9
        info_sev = info.split("|")[6]
        crit_sev = critical.split("|")[6]
        assert info_sev == "3"
        assert crit_sev == "9"

    def test_hold_matches_event_by_type(self):
        """event_types 过滤：匹配/不匹配。"""
        hold_filter = {"event_types": ["delete", "export"]}
        assert al._hold_matches_event(hold_filter, {"action": "delete"}) is True
        assert al._hold_matches_event(hold_filter, {"action": "create"}) is False

    def test_hold_matches_event_by_time_range(self):
        """时间范围过滤：事件 created_at 在范围内才匹配。"""
        ts = datetime(2026, 6, 15, tzinfo=UTC)
        hold_filter = {
            "event_types": [],
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-12-31T23:59:59Z",
        }
        assert al._hold_matches_event(hold_filter, {"created_at": ts}) is True
        out_of_range = datetime(2025, 1, 1, tzinfo=UTC)
        assert al._hold_matches_event(hold_filter, {"created_at": out_of_range}) is False

    def test_hold_matches_event_empty_filter_matches_all(self):
        """空 event_filter 匹配所有事件。"""
        assert al._hold_matches_event({}, {"action": "anything"}) is True

    @pytest.mark.asyncio
    async def test_check_legal_hold_passes_when_no_hold(self, monkeypatch):
        """无活跃 legal hold 时不抛异常。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        event = {"action": "delete", "created_at": datetime.now(UTC)}
        # 不应抛异常
        await al._check_legal_hold(conn, event, "wsp_test")

    @pytest.mark.asyncio
    async def test_check_legal_hold_no_match_passes(self, monkeypatch):
        """有 hold 但不匹配事件时不抛异常。"""
        hold = _hold_row(event_filter={"event_types": ["delete"]})
        conn = _RecordingConnection(results=[_Result(rows=[hold])])
        event = {"action": "create", "created_at": datetime.now(UTC)}
        await al._check_legal_hold(conn, event, "wsp_test")

    def test_send_to_siem_endpoint_tcp_success(self, monkeypatch):
        """_send_to_siem_endpoint TCP 成功。"""
        fake = _FakeSocket()
        monkeypatch.setattr(al.socket, "create_connection",
                            lambda addr, timeout=None: fake)
        ok, err, lat = al._send_to_siem_endpoint("host:514", "tcp", "msg")
        assert ok is True
        assert err is None
        assert lat >= 0
        assert fake.sent == b"msg\n"

    def test_send_to_siem_endpoint_invalid_endpoint(self):
        """_send_to_siem_endpoint 无端口 endpoint 返回失败。"""
        ok, err, lat = al._send_to_siem_endpoint("no-port", "tcp", "msg")
        assert ok is False
        assert "invalid endpoint" in err
        assert lat == 0

    def test_send_to_siem_endpoint_udp_success(self, monkeypatch):
        """_send_to_siem_endpoint UDP 成功。"""
        fake = _FakeSocket()
        monkeypatch.setattr(al.socket, "socket", lambda *a, **k: fake)
        ok, err, _lat = al._send_to_siem_endpoint("host:514", "udp", "msg")
        assert ok is True
        assert err is None
        assert fake.sent == b"msg\n"

    def test_trigger_siem_delivery_creates_task_even_when_not_in_cache(self, monkeypatch):
        """v7.171：workspace 不在 _SIEM_ENABLED_WORKSPACES 缓存时仍创建后台任务（多 worker 场景）。

        修复后投递判断以 DB 为准（_deliver_to_siem 查 audit_siem_config），
        不再依赖模块级缓存，避免多 worker 下漏投递。
        """
        created: list = []

        def _fake_create_task(coro):
            created.append(coro)
            coro.close()  # 关闭未 await 的协程，避免告警
            return None

        monkeypatch.setattr(al.asyncio, "create_task", _fake_create_task)
        # 集合已被 autouse fixture 清空（模拟其他 worker 未缓存该 workspace）
        al._trigger_siem_delivery("wsp_test", {"id": "aud_1"})
        # 仍应创建任务，由 _deliver_to_siem 查 DB 决定是否投递
        assert len(created) == 1

    def test_trigger_siem_delivery_creates_task_when_enabled(self, monkeypatch):
        """workspace 在启用集合时 _trigger_siem_delivery 创建后台任务。"""
        al._SIEM_ENABLED_WORKSPACES.add("wsp_test")
        tasks: list = []

        def _fake_create_task(coro):
            tasks.append(coro)
            coro.close()
            return None

        monkeypatch.setattr(al.asyncio, "create_task", _fake_create_task)
        al._trigger_siem_delivery("wsp_test", {"id": "aud_1"})
        assert len(tasks) == 1

    def test_require_admin_admin_passes(self):
        """admin 角色通过 _require_admin。"""
        al._require_admin(_actor(role="admin"))  # 不抛异常

    def test_require_admin_member_blocked(self):
        """member（仅 audit:read）被 _require_admin 拒绝 403。"""
        with pytest.raises(HTTPException) as exc:
            al._require_admin(_actor(role="member", capabilities=("audit:read",)))
        assert exc.value.status_code == 403

    def test_parse_endpoint_valid(self):
        """_parse_endpoint 正确解析 host:port。"""
        host, port = al._parse_endpoint("siem.example.com:514")
        assert host == "siem.example.com"
        assert port == 514

    def test_parse_endpoint_invalid(self):
        """_parse_endpoint 无端口时抛 ValueError。"""
        with pytest.raises(ValueError):
            al._parse_endpoint("no-port")


# ============================================================================
# 7. DELETE /api/v1/audit-logs/{event_id}（v7.172 legal hold 423 接线）
# ============================================================================


class TestDeleteAuditLog:
    """DELETE 单条审计事件：admin 鉴权 / 404 / 跨 workspace 403 / legal hold 423 / 成功 204。"""

    @pytest.mark.asyncio
    async def test_delete_audit_log_success(self, monkeypatch):
        """admin 删除非 legal-hold 事件返回 204 No Content。"""
        event = _row()
        # _owned_event SELECT 返回事件；_check_legal_hold SELECT 返回空（无活跃保留）
        conn = _RecordingConnection(results=[_Result(row=event), _Result(rows=[])])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/audit-logs/aud_1")
        assert resp.status_code == 204
        # 验证 DELETE FROM audit_log 被调用且参数正确
        delete_calls = [c for c in conn.calls if "DELETE FROM audit_log" in c[0]]
        assert len(delete_calls) == 1
        assert delete_calls[0][1] == ("aud_1", "wsp_test")

    @pytest.mark.asyncio
    async def test_delete_audit_log_requires_admin(self, monkeypatch):
        """非 admin（仅 audit:read）调用 DELETE 返回 403，不触达数据库。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member", capabilities=("audit:read",)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/audit-logs/aud_1")
        assert resp.status_code == 403
        assert len(conn.calls) == 0

    @pytest.mark.asyncio
    async def test_delete_audit_log_not_found(self, monkeypatch):
        """事件不存在返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/audit-logs/missing")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_audit_log_cross_workspace_403(self, monkeypatch):
        """事件属于其他 workspace 返回 403。"""
        event = _row(workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=event)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/audit-logs/aud_1")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_delete_audit_log_blocked_by_legal_hold_returns_423(self, monkeypatch):
        """事件处于 legal hold 保留期返回 423 Locked，DELETE 不被执行。"""
        event = _row()

        async def _raise_423(_conn, _event, _workspace_id):
            raise HTTPException(status_code=423, detail="Event is under legal hold: litigation")

        monkeypatch.setattr(al, "_check_legal_hold", _raise_423)
        # _owned_event SELECT 返回事件；_check_legal_hold mock 抛 423，DELETE 不会触达
        conn = _RecordingConnection(results=[_Result(row=event)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/audit-logs/aud_1")
        assert resp.status_code == 423
        # DELETE 不应被执行
        delete_calls = [c for c in conn.calls if "DELETE FROM audit_log" in c[0]]
        assert len(delete_calls) == 0

    @pytest.mark.asyncio
    async def test_delete_audit_log_passes_when_legal_hold_released(self, monkeypatch):
        """legal hold 已释放（_check_legal_hold 不抛异常）删除成功 204。"""
        event = _row()

        async def _noop(_conn, _event, _workspace_id):
            # legal hold 已释放，检查通过不抛异常
            return None

        monkeypatch.setattr(al, "_check_legal_hold", _noop)
        # _owned_event SELECT 返回事件；_check_legal_hold mock 不抛；DELETE 执行
        conn = _RecordingConnection(results=[_Result(row=event)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/audit-logs/aud_1")
        assert resp.status_code == 204
        delete_calls = [c for c in conn.calls if "DELETE FROM audit_log" in c[0]]
        assert len(delete_calls) == 1
        assert delete_calls[0][1] == ("aud_1", "wsp_test")


# ============================================================================
# 8. v7.171 安全修复：api_key 加密 / SSRF 校验 / CSV 公式注入 / 多 worker 投递
# ============================================================================


class TestV171SecurityFixes:
    """v7.171 安全修复验证：SIEM api_key 加密存储 / SSRF 校验 / CSV 公式注入转义。"""

    @pytest.mark.asyncio
    async def test_siem_config_api_key_encrypted_and_last4_returned(self, monkeypatch):
        """v7.171：POST /siem/config 带 api_key 时加密存储，响应仅返回 api_key_last4。"""
        cfg = _siem_row(enabled=True, api_key="sk-secret-1234", api_key_last4="1234")
        conn = _RecordingConnection(results=[_Result(row=cfg)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/audit-logs/siem/config",
                json={"endpoint": "siem.example.com:514", "api_key": "sk-secret-1234", "enabled": True},
            )
        assert resp.status_code == 201
        body = resp.json()
        # 响应不含明文 api_key 字段，仅返回 last4
        assert "api_key" not in body
        assert body["api_key_last4"] == "1234"
        # SQL 写入 api_key_enc / api_key_last4 列，不写明文 api_key
        insert_sql, insert_params = conn.calls[0]
        assert "api_key_enc" in insert_sql
        assert "api_key_last4" in insert_sql
        # 明文 api_key 不应出现在 SQL 参数中（加密后是 token）
        assert "sk-secret-1234" not in insert_params
        # last4 明文出现在参数中
        assert "1234" in insert_params

    @pytest.mark.asyncio
    async def test_get_siem_config_returns_only_last4(self, monkeypatch):
        """v7.171：GET /siem/config 不返回明文 api_key，仅返回 api_key_last4。"""
        cfg = _siem_row(api_key="sk-secret-1234", api_key_last4="1234")
        conn = _RecordingConnection(results=[_Result(row=cfg)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/audit-logs/siem/config")
        assert resp.status_code == 200
        body = resp.json()
        assert "api_key" not in body
        assert body["api_key_last4"] == "1234"

    def test_send_to_siem_endpoint_rejects_internal_host(self, monkeypatch):
        """v7.171：_send_to_siem_endpoint 对 loopback 127.0.0.1 SSRF 拦截，不建立连接。"""
        def _guard(*_a, **_k):
            raise AssertionError("create_connection should not be reached when SSRF rejects")
        monkeypatch.setattr(al.socket, "create_connection", _guard)
        ok, err, _lat = al._send_to_siem_endpoint("127.0.0.1:514", "tcp", "msg")
        assert ok is False
        assert "not allowed" in err

    def test_send_to_siem_endpoint_rejects_metadata_ip(self, monkeypatch):
        """v7.171：_send_to_siem_endpoint 对云元数据地址 169.254.169.254 SSRF 拦截。"""
        def _guard(*_a, **_k):
            raise AssertionError("should not connect")
        monkeypatch.setattr(al.socket, "create_connection", _guard)
        ok, err, _lat = al._send_to_siem_endpoint("169.254.169.254:514", "tcp", "msg")
        assert ok is False
        assert "not allowed" in err

    def test_sanitize_csv_cell_escapes_formula_prefix(self):
        """v7.171：_sanitize_csv_cell 对 =/+/-/@ 开头字符串前缀加单引号。"""
        assert al._sanitize_csv_cell("=cmd") == "'=cmd"
        assert al._sanitize_csv_cell("+1023") == "'+1023"
        assert al._sanitize_csv_cell("-123") == "'-123"
        assert al._sanitize_csv_cell("@sum") == "'@sum"
        assert al._sanitize_csv_cell("normal") == "normal"
        assert al._sanitize_csv_cell("") == ""
        assert al._sanitize_csv_cell(None) is None
        assert al._sanitize_csv_cell(123) == 123

    @pytest.mark.asyncio
    async def test_csv_export_escapes_formula_injection(self, monkeypatch):
        """v7.171：CSV 导出对 =/+ 开头单元格前缀加单引号，防止公式注入。"""
        rows = [_row(description="=cmd|/c calc!A1", source_ip="+1023")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(al, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/audit-logs/export/batch",
                json={"format": "csv"},
            )
        assert resp.status_code == 200
        # = / + 开头的单元格被前缀加单引号
        assert "'=cmd|/c calc!A1" in resp.text
        assert "'+1023" in resp.text

    @pytest.mark.asyncio
    async def test_siem_schema_statements_include_api_key_enc_columns(self):
        """v7.171：SCHEMA_STATEMENTS 包含 api_key_enc / api_key_last4 列定义与幂等 ALTER。"""
        joined = "\n".join(al.SCHEMA_STATEMENTS)
        assert "api_key_enc TEXT" in joined
        assert "api_key_last4 CHAR(4)" in joined
        assert "ALTER TABLE audit_siem_config ADD COLUMN IF NOT EXISTS api_key_enc TEXT" in joined
        assert "ALTER TABLE audit_siem_config ADD COLUMN IF NOT EXISTS api_key_last4 CHAR(4)" in joined
