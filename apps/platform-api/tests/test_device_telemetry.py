"""AMA-Work 设备协同与本地观测闭环 (device_telemetry) 单元 + 端点测试。

v7.142: 25 个测试覆盖：
- 注册：成功 / upsert 更新 / 字段校验 (3)
- 心跳：成功 / 设备不存在 404 / telemetry 更新 (3)
- 列表：分页 / workspace 隔离 / status 过滤 (3)
- 单设备详情：存在 / 不存在 404 / workspace 越权 403 (3)
- 注销：成功 / 不存在 404 / workspace 越权 403 (3)
- 事件上报：成功 / 设备不存在 404 / telemetry.events 数组追加 (3)
- 离线扫描：超时标记 offline / 未超时保持 online / Worker 调用 (3)
- 鉴权：未认证 401 (1)
- 边界/集成：register+heartbeat+events 全链路 / 缺少能力 403 (3)

所有测试使用 fake pool/connection，不依赖真实 DB / Redis / 网络。
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules import device_telemetry as dt
from workama_platform.modules.device_telemetry import (
    DEFAULT_OFFLINE_THRESHOLD_SECONDS,
    DEVICE_OFFLINE_SWEEP_JOB_TYPE,
    DeviceOfflineSweepWorker,
    _offline_threshold_seconds,
)


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
    capabilities=("device:*",),
    workspace_id="wsp_test",
    user_id="usr_test",
) -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_test",
        role="admin",
        email="test@example.com",
        display_name="Test",
        onboarding_completed=True,
        capabilities=capabilities,
    )


def _row(**overrides) -> dict:
    base = {
        "id": "dev_1",
        "workspace_id": "wsp_test",
        "device_id": "dev-001",
        "device_name": "My Laptop",
        "device_kind": "laptop",
        "os": "macOS 15",
        "app_version": "1.2.3",
        "last_heartbeat_at": datetime.now(UTC),
        "status": "online",
        "telemetry": {},
        "metadata": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(dt.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


# ============================================================================
# 1. 注册
# ============================================================================


class TestRegistration:
    """设备注册端点测试。"""

    @pytest.mark.asyncio
    async def test_register_device_success(self, monkeypatch):
        """POST /register 注册新设备返回 201。"""
        row = _row(device_id="dev-001", device_kind="laptop")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/devices/register",
                json={
                    "device_id": "dev-001",
                    "device_name": "My Laptop",
                    "device_kind": "laptop",
                    "os": "macOS 15",
                    "app_version": "1.2.3",
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["device_id"] == "dev-001"
        assert body["device_kind"] == "laptop"
        assert body["status"] == "online"
        # 确认 INSERT ... ON CONFLICT upsert 语句
        assert any(
            "INSERT INTO device_telemetry" in q and "ON CONFLICT" in q
            for q, _ in conn.calls
        )

    @pytest.mark.asyncio
    async def test_register_device_upsert_updates_existing(self, monkeypatch):
        """POST /register 对已存在 (workspace_id, device_id) 执行 upsert 更新。"""
        # upsert 更新分支：返回更新后的行（device_name 改变，id 不变）
        row = _row(id="dev_orig", device_id="dev-001", device_name="Renamed")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/devices/register",
                json={
                    "device_id": "dev-001",
                    "device_name": "Renamed",
                    "device_kind": "desktop",
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["id"] == "dev_orig"  # 原始 id 保留
        assert body["device_name"] == "Renamed"

    @pytest.mark.asyncio
    async def test_register_device_field_validation_rejects_invalid_kind(self):
        """POST /register 非法 device_kind 触发 422 校验错误。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/devices/register",
                json={"device_id": "dev-001", "device_kind": "tablet"},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_register_device_field_validation_rejects_empty_device_id(self):
        """POST /register 空 device_id 触发 422 校验错误。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/devices/register",
                json={"device_id": "", "device_kind": "desktop"},
            )
        assert resp.status_code == 422


# ============================================================================
# 2. 心跳
# ============================================================================


class TestHeartbeat:
    """心跳上报端点测试。"""

    @pytest.mark.asyncio
    async def test_heartbeat_success(self, monkeypatch):
        """POST /{device_id}/heartbeat 更新心跳并返回 200。"""
        existing = _row(device_id="dev-001", status="online")
        updated = _row(device_id="dev-001", status="online", telemetry={"cpu": 0.5})
        # _owned_device SELECT + UPDATE RETURNING
        conn = _RecordingConnection(
            results=[_Result(row=existing), _Result(row=updated)]
        )
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/devices/dev-001/heartbeat",
                json={"status": "online", "telemetry": {"cpu": 0.5}},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["device_id"] == "dev-001"
        assert body["telemetry"] == {"cpu": 0.5}
        # 第二条 SQL 应为 UPDATE
        assert "UPDATE device_telemetry" in conn.calls[1][0]

    @pytest.mark.asyncio
    async def test_heartbeat_returns_404_when_device_missing(self, monkeypatch):
        """POST /{device_id}/heartbeat 设备不存在返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/devices/missing/heartbeat",
                json={"status": "online"},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_heartbeat_updates_telemetry(self, monkeypatch):
        """POST /{device_id}/heartbeat telemetry 字段被完整覆盖更新。"""
        existing = _row(device_id="dev-001")
        updated = _row(
            device_id="dev-001",
            telemetry={"cpu": 0.8, "mem": 0.6, "disk": 0.3},
        )
        conn = _RecordingConnection(
            results=[_Result(row=existing), _Result(row=updated)]
        )
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/devices/dev-001/heartbeat",
                json={
                    "status": "warning",
                    "telemetry": {"cpu": 0.8, "mem": 0.6, "disk": 0.3},
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["telemetry"]["cpu"] == 0.8
        assert body["telemetry"]["mem"] == 0.6
        # 验证 UPDATE 参数包含 telemetry jsonb
        update_params = conn.calls[1][1]
        assert update_params[1] == '{"cpu":0.8,"mem":0.6,"disk":0.3}'


# ============================================================================
# 3. 列表
# ============================================================================


class TestList:
    """设备列表端点测试。"""

    @pytest.mark.asyncio
    async def test_list_devices_pagination(self, monkeypatch):
        """GET / 分页返回设备列表。"""
        rows = [_row(id="dev_1", device_id="d1"), _row(id="dev_2", device_id="d2")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/devices?limit=10&offset=0")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert body["limit"] == 10
        assert body["offset"] == 0
        assert len(body["items"]) == 2

    @pytest.mark.asyncio
    async def test_list_devices_workspace_isolation(self, monkeypatch):
        """GET / 列表 SQL 强制按 actor.workspace_id 过滤。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_isolated"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/devices")
        assert resp.status_code == 200
        # 验证 SQL 含 workspace_id 过滤，且参数首位为 actor 的 workspace_id
        query, params = conn.calls[0]
        assert "WHERE workspace_id = %s" in query
        assert params[0] == "wsp_isolated"

    @pytest.mark.asyncio
    async def test_list_devices_status_filter(self, monkeypatch):
        """GET /?status=online 仅返回 online 设备。"""
        rows = [_row(id="dev_1", status="online")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/devices?status=online")
        assert resp.status_code == 200
        body = resp.json()
        assert all(item["status"] == "online" for item in body["items"])
        query, params = conn.calls[0]
        assert "AND status = %s" in query
        assert "online" in params


# ============================================================================
# 4. 单设备详情
# ============================================================================


class TestGetDevice:
    """单设备详情端点测试。"""

    @pytest.mark.asyncio
    async def test_get_device_exists(self, monkeypatch):
        """GET /{device_id} 返回设备详情。"""
        row = _row(device_id="dev-001")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/devices/dev-001")
        assert resp.status_code == 200
        body = resp.json()
        assert body["device_id"] == "dev-001"
        assert body["device_kind"] == "laptop"

    @pytest.mark.asyncio
    async def test_get_device_returns_404_when_missing(self, monkeypatch):
        """GET /{device_id} 设备不存在返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/devices/missing")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_device_returns_403_cross_workspace(self, monkeypatch):
        """GET /{device_id} 设备属于其他 workspace 返回 403。"""
        row = _row(device_id="dev-001", workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/devices/dev-001")
        assert resp.status_code == 403


# ============================================================================
# 5. 注销
# ============================================================================


class TestDeleteDevice:
    """设备注销端点测试。"""

    @pytest.mark.asyncio
    async def test_delete_device_success(self, monkeypatch):
        """DELETE /{device_id} 注销设备返回 200。"""
        existing = _row(device_id="dev-001")
        # _owned_device SELECT + DELETE RETURNING
        conn = _RecordingConnection(
            results=[_Result(row=existing), _Result(row={"id": "dev_1"})]
        )
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/devices/dev-001")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "dev_1"
        assert body["deleted"] is True
        assert "DELETE FROM device_telemetry" in conn.calls[1][0]

    @pytest.mark.asyncio
    async def test_delete_device_returns_404_when_missing(self, monkeypatch):
        """DELETE /{device_id} 设备不存在返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/devices/missing")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_device_returns_403_cross_workspace(self, monkeypatch):
        """DELETE /{device_id} 设备属于其他 workspace 返回 403。"""
        row = _row(device_id="dev-001", workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/devices/dev-001")
        assert resp.status_code == 403


# ============================================================================
# 6. 事件上报
# ============================================================================


class TestEvents:
    """遥测事件上报端点测试。"""

    @pytest.mark.asyncio
    async def test_report_event_success(self, monkeypatch):
        """POST /{device_id}/events 上报事件返回 200。"""
        existing = _row(device_id="dev-001")
        updated = _row(
            device_id="dev-001",
            telemetry={"events": [{"event_type": "boot", "payload": {}}]},
        )
        conn = _RecordingConnection(
            results=[_Result(row=existing), _Result(row=updated)]
        )
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/devices/dev-001/events",
                json={"event_type": "boot", "payload": {"source": "system"}},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["events_count"] == 1
        # 验证 UPDATE SQL 使用 jsonb_set + COALESCE + ||
        update_sql = conn.calls[1][0]
        assert "jsonb_set" in update_sql
        assert "COALESCE" in update_sql

    @pytest.mark.asyncio
    async def test_report_event_returns_404_when_device_missing(self, monkeypatch):
        """POST /{device_id}/events 设备不存在返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/devices/missing/events",
                json={"event_type": "boot"},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_report_event_appends_to_events_array(self, monkeypatch):
        """POST /{device_id}/events 返回的 events_count 反映 telemetry.events 长度。"""
        existing = _row(device_id="dev-001")
        # 模拟已存在 2 条 events 的返回（追加后）
        updated = _row(
            device_id="dev-001",
            telemetry={
                "events": [
                    {"event_type": "boot", "payload": {}},
                    {"event_type": "cpu_spike", "payload": {"cpu": 0.99}},
                ]
            },
        )
        conn = _RecordingConnection(
            results=[_Result(row=existing), _Result(row=updated)]
        )
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/devices/dev-001/events",
                json={"event_type": "cpu_spike", "payload": {"cpu": 0.99}},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["events_count"] == 2
        assert len(body["telemetry"]["events"]) == 2


# ============================================================================
# 7. 离线扫描
# ============================================================================


class TestOfflineSweep:
    """离线扫描测试。"""

    @pytest.mark.asyncio
    async def test_offline_sweep_marks_timed_out_devices(self, monkeypatch):
        """sweep_offline_devices 将超时设备标记 offline，返回被扫描与被标记数。"""
        # count_result (fetchone) + UPDATE RETURNING (fetchall)
        conn = _RecordingConnection(
            results=[
                _Result(row={"cnt": 5}),
                _Result(rows=[{"id": "dev_a"}, {"id": "dev_b"}]),
            ]
        )
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        worker = DeviceOfflineSweepWorker()
        result = await worker.sweep_offline_devices(
            "wsp_test", threshold_seconds=300
        )
        assert result["scanned"] == 5
        assert result["swept"] == 2
        assert result["swept_ids"] == ["dev_a", "dev_b"]
        assert result["threshold_seconds"] == 300
        # UPDATE SQL 含阈值过滤
        update_sql = conn.calls[1][0]
        assert "status = 'offline'" in update_sql
        assert "last_heartbeat_at < now()" in update_sql

    @pytest.mark.asyncio
    async def test_offline_sweep_keeps_recent_devices_online(self, monkeypatch):
        """sweep_offline_devices 无超时设备时 swept=0。"""
        conn = _RecordingConnection(
            results=[
                _Result(row={"cnt": 3}),
                _Result(rows=[]),  # 无设备被 UPDATE 命中
            ]
        )
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        worker = DeviceOfflineSweepWorker()
        result = await worker.sweep_offline_devices("wsp_test")
        assert result["scanned"] == 3
        assert result["swept"] == 0
        assert result["swept_ids"] == []

    @pytest.mark.asyncio
    async def test_process_offline_sweep_job_delegates_with_payload(self, monkeypatch):
        """process_offline_sweep_job 按 payload 透传 workspace_id 与 threshold。"""
        conn = _RecordingConnection(
            results=[
                _Result(row={"cnt": 2}),
                _Result(rows=[{"id": "dev_x"}]),
            ]
        )
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        worker = DeviceOfflineSweepWorker()
        result = await worker.process_offline_sweep_job(
            {"workspace_id": "wsp_job", "threshold_seconds": 120}
        )
        assert result["swept"] == 1
        assert result["threshold_seconds"] == 120
        # 验证 count 与 UPDATE 都按 workspace_id 过滤
        count_sql, count_params = conn.calls[0]
        assert "WHERE workspace_id = %s" in count_sql
        assert count_params[0] == "wsp_job"

    @pytest.mark.asyncio
    async def test_offline_sweep_endpoint_invokes_worker(self, monkeypatch):
        """GET /offline-sweep 端点调用 Worker 并返回扫描结果。"""
        conn = _RecordingConnection(
            results=[
                _Result(row={"cnt": 1}),
                _Result(rows=[{"id": "dev_a"}]),
            ]
        )
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/devices/offline-sweep?threshold_seconds=60")
        assert resp.status_code == 200
        body = resp.json()
        assert body["swept"] == 1
        assert body["threshold_seconds"] == 60


# ============================================================================
# 8. 鉴权
# ============================================================================


class TestAuth:
    """鉴权测试。"""

    @pytest.mark.asyncio
    async def test_list_devices_requires_authentication(self):
        """未认证请求 GET / 返回 401。"""
        app = _app(actor=None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/devices")
        assert resp.status_code == 401


# ============================================================================
# 9. 边界 / 集成测试
# ============================================================================


class TestEdgeCases:
    """边界与集成测试。"""

    @pytest.mark.asyncio
    async def test_register_heartbeat_events_full_flow(self, monkeypatch):
        """集成：register → heartbeat → events 全链路使用同一 fake 连接。"""
        # register: INSERT RETURNING (1 result)
        reg_row = _row(id="dev_new", device_id="dev-flow", device_kind="desktop")
        # heartbeat: _owned_device SELECT + UPDATE RETURNING (2 results)
        hb_existing = _row(id="dev_new", device_id="dev-flow")
        hb_updated = _row(
            id="dev_new",
            device_id="dev-flow",
            status="online",
            telemetry={"cpu": 0.4},
        )
        # events: _owned_device SELECT + UPDATE RETURNING (2 results)
        ev_existing = _row(id="dev_new", device_id="dev-flow")
        ev_updated = _row(
            id="dev_new",
            device_id="dev-flow",
            telemetry={"cpu": 0.4, "events": [{"event_type": "login"}]},
        )
        conn = _RecordingConnection(
            results=[
                _Result(row=reg_row),  # register INSERT
                _Result(row=hb_existing),  # heartbeat SELECT
                _Result(row=hb_updated),  # heartbeat UPDATE
                _Result(row=ev_existing),  # events SELECT
                _Result(row=ev_updated),  # events UPDATE
            ]
        )
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            reg = await client.post(
                "/api/v1/devices/register",
                json={"device_id": "dev-flow", "device_kind": "desktop"},
            )
            hb = await client.post(
                "/api/v1/devices/dev-flow/heartbeat",
                json={"status": "online", "telemetry": {"cpu": 0.4}},
            )
            ev = await client.post(
                "/api/v1/devices/dev-flow/events",
                json={"event_type": "login", "payload": {}},
            )
        assert reg.status_code == 201
        assert hb.status_code == 200
        assert ev.status_code == 200
        assert reg.json()["id"] == "dev_new"
        assert hb.json()["telemetry"] == {"cpu": 0.4}
        assert ev.json()["events_count"] == 1
        # 共 5 次 execute（register 1 + heartbeat 2 + events 2）
        assert len(conn.calls) == 5

    @pytest.mark.asyncio
    async def test_register_forbidden_without_device_write_capability(self, monkeypatch):
        """缺少 device:write 能力时 register 返回 403。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        app = _app(actor=_actor(capabilities=()))  # 无任何 device 能力
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/devices/register",
                json={"device_id": "dev-001", "device_kind": "desktop"},
            )
        assert resp.status_code == 403
        # 能力不足时不应执行任何 SQL
        assert len(conn.calls) == 0

    @pytest.mark.asyncio
    async def test_delete_forbidden_with_only_read_capability(self, monkeypatch):
        """仅有 device:read 能力时 delete 返回 403。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(dt, "pool", _Pool(conn))

        app = _app(actor=_actor(capabilities=("device:read",)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/devices/dev-001")
        assert resp.status_code == 403

    def test_offline_threshold_reads_env_var(self, monkeypatch):
        """_offline_threshold_seconds 读取环境变量，非法值回退默认。"""
        monkeypatch.setenv("WORKAMA_DEVICE_OFFLINE_THRESHOLD_SECONDS", "120")
        assert _offline_threshold_seconds() == 120

        monkeypatch.setenv("WORKAMA_DEVICE_OFFLINE_THRESHOLD_SECONDS", "not-a-number")
        assert _offline_threshold_seconds() == DEFAULT_OFFLINE_THRESHOLD_SECONDS

        monkeypatch.setenv("WORKAMA_DEVICE_OFFLINE_THRESHOLD_SECONDS", "0")
        assert _offline_threshold_seconds() == DEFAULT_OFFLINE_THRESHOLD_SECONDS

    def test_job_type_constant_is_stable(self):
        """DEVICE_OFFLINE_SWEEP_JOB_TYPE 常量稳定，供 worker 路由。"""
        assert DEVICE_OFFLINE_SWEEP_JOB_TYPE == "device_offline_sweep"
