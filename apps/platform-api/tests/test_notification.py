"""notification 模块单元测试。

覆盖范围：
- notification.py（独立文件）：CRUD/未读数/标记已读/全部已读/删除，
  含 admin 鉴权、capability 校验、workspace 隔离、kind 非法回退、
  list 过滤参数（unread_only/kind/limit/offset）的边界。
- notification/ 包（service + router + delivery）：
  * 通知偏好设置（forced in-app / preference_change_allowed / GET / PUT）
  * 投递记录与状态机（_record_failure / _record_success / _claim_deliveries）
  * 邮件 mock 投递（send_email / deliver_email / process_pending_email_deliveries）
  * Webhook mock 投递（send_webhook_mock / deliver_webhook / process_pending_webhook_deliveries）

被包遮蔽的 notification.py 通过 importlib 加载（与 test_notification_center.py 一致）。
测试风格：内联 _Result/_SeqConnection/_Pool + monkeypatch，不依赖真实 DB/网络。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import HTTPException

from workama_platform.core import Actor


# ============================================================================
# 通过 importlib 加载被包遮蔽的 notification.py（独立文件）
# ============================================================================

_NOTIF_PATH = (
    Path(__file__).parent.parent
    / "src"
    / "workama_platform"
    / "modules"
    / "notification.py"
)
_spec = importlib.util.spec_from_file_location(
    "workama_platform.modules.notification_file_test", _NOTIF_PATH
)
n = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = n
_spec.loader.exec_module(n)


# ============================================================================
# 通知 CRUD（独立文件 notification.py）测试辅助
# ============================================================================


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = list(rows) if rows is not None else []
        self.rowcount = len(self._rows)

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return list(self._rows)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _SeqConnection:
    def __init__(self, results=None):
        self._results = list(results) if results else []
        self.calls: list[tuple[str, tuple]] = []
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
    def __init__(self, connection):
        self._connection = connection

    def connection(self):
        connection = self._connection

        class _Context:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *_args):
                return False

        return _Context()


def _actor(
    *,
    role="owner",
    capabilities=("*",),
    workspace_id="wsp_test",
    user_id="usr_test",
) -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email="test@example.com",
        display_name="Test",
        onboarding_completed=True,
        capabilities=capabilities,
    )


def _notif_row(**overrides) -> dict:
    base = {
        "id": "notif_1",
        "workspace_id": "wsp_test",
        "user_id": "usr_test",
        "kind": "info",
        "title": "Hello",
        "body": "World",
        "action_url": None,
        "action_label": None,
        "read": False,
        "metadata": {},
        "created_at": datetime.now(UTC),
        "read_at": None,
    }
    base.update(overrides)
    return base


# ============================================================================
# 1. notification.py create_notification 辅助函数
# ============================================================================


@pytest.mark.asyncio
async def test_create_notification_helper_inserts_with_metadata_jsonb(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=_notif_row())])
    monkeypatch.setattr(n, "pool", _Pool(conn))

    result = await n.create_notification(
        workspace_id="wsp_test",
        user_id="usr_test",
        kind="info",
        title="Hello",
        body="World",
        metadata={"source": "test"},
    )

    assert result["id"] == "notif_1"
    insert_q = next(q for q, _ in conn.calls if "INSERT INTO notification" in q)
    assert "%s::jsonb" in insert_q


@pytest.mark.asyncio
async def test_create_notification_helper_falls_back_to_info_for_invalid_kind(monkeypatch):
    """非法 kind 自动回退为 info。"""
    conn = _SeqConnection(results=[_Result(row=_notif_row(kind="info"))])
    monkeypatch.setattr(n, "pool", _Pool(conn))

    await n.create_notification(
        workspace_id="wsp_test",
        user_id="usr_test",
        kind="totally-bogus",
        title="Hello",
    )

    insert_q, insert_params = next(
        (q, p) for q, p in conn.calls if "INSERT INTO notification" in q
    )
    # 第 4 个参数是 kind
    assert insert_params[3] == "info"


@pytest.mark.asyncio
async def test_create_notification_helper_empty_metadata_uses_empty_dict(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=_notif_row(metadata={}))])
    monkeypatch.setattr(n, "pool", _Pool(conn))

    await n.create_notification(
        workspace_id="wsp_test",
        user_id="usr_test",
        title="Hello",
        metadata=None,
    )

    insert_q, insert_params = next(
        (q, p) for q, p in conn.calls if "INSERT INTO notification" in q
    )
    # 最后一个参数应是 json 字符串
    assert insert_params[-1] == "{}"


# ============================================================================
# 2. notification.py 端点：admin 鉴权 / capability 校验
# ============================================================================


@pytest.mark.asyncio
async def test_create_endpoint_rejects_member_role():
    """非 owner/admin 角色不能创建通知。"""
    body = n.NotificationCreateRequest(user_id="usr_other", title="Hello")
    with pytest.raises(HTTPException) as exc:
        await n.create_notification_endpoint(body, _actor(role="member"))
    assert exc.value.status_code == 403
    assert "Admin role" in exc.value.detail


@pytest.mark.asyncio
async def test_unread_count_requires_read_capability():
    actor = _actor(capabilities=())  # 无任何 capability
    with pytest.raises(HTTPException) as exc:
        await n.unread_count(actor)
    assert exc.value.status_code == 403
    assert "notification:read" in exc.value.detail


@pytest.mark.asyncio
async def test_mark_all_read_requires_write_capability():
    actor = _actor(capabilities=("notification:read",))
    with pytest.raises(HTTPException) as exc:
        await n.mark_all_read(actor)
    assert exc.value.status_code == 403
    assert "notification:write" in exc.value.detail


@pytest.mark.asyncio
async def test_mark_read_requires_write_capability():
    actor = _actor(capabilities=("notification:read",))
    with pytest.raises(HTTPException) as exc:
        await n.mark_read("notif_1", actor)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_delete_requires_delete_capability():
    actor = _actor(capabilities=("notification:read", "notification:write"))
    with pytest.raises(HTTPException) as exc:
        await n.delete_notification("notif_1", actor)
    assert exc.value.status_code == 403
    assert "notification:delete" in exc.value.detail


# ============================================================================
# 3. notification.py 端点：CRUD + workspace 隔离 + 边界
# ============================================================================


@pytest.mark.asyncio
async def test_unread_count_returns_zero_when_no_rows(monkeypatch):
    conn = _SeqConnection(results=[_Result(row={"count": 0})])
    monkeypatch.setattr(n, "pool", _Pool(conn))

    result = await n.unread_count(_actor())
    assert result["unread_count"] == 0


@pytest.mark.asyncio
async def test_mark_all_read_returns_updated_count(monkeypatch):
    conn = _SeqConnection(
        results=[_Result(rows=[{"id": "notif_1"}, {"id": "notif_2"}])]
    )
    monkeypatch.setattr(n, "pool", _Pool(conn))

    result = await n.mark_all_read(_actor())
    assert result["updated"] == 2


@pytest.mark.asyncio
async def test_list_notifications_applies_kind_filter(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(rows=[_notif_row(kind="error")]),  # SELECT 列表
            _Result(row={"count": 1}),  # total count
            _Result(row={"count": 1}),  # unread count
        ]
    )
    monkeypatch.setattr(n, "pool", _Pool(conn))

    result = await n.list_notifications(_actor(), unread_only=False, kind="error", limit=50, offset=0)

    assert len(result["items"]) == 1
    list_q = next(q for q, _ in conn.calls if "ORDER BY created_at DESC" in q)
    assert "kind = %s" in list_q
    assert result["total"] == 1
    assert result["unread_count"] == 1


@pytest.mark.asyncio
async def test_list_notifications_unread_only_filter(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(rows=[_notif_row()]),
            _Result(row={"count": 5}),
            _Result(row={"count": 3}),
        ]
    )
    monkeypatch.setattr(n, "pool", _Pool(conn))

    result = await n.list_notifications(_actor(), unread_only=True, kind=None, limit=10, offset=0)

    list_q = next(q for q, _ in conn.calls if "ORDER BY created_at DESC" in q)
    assert "read = FALSE" in list_q
    assert result["limit"] == 10
    assert result["offset"] == 0


@pytest.mark.asyncio
async def test_get_notification_returns_404_when_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(n, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await n.get_notification("notif_missing", _actor())
    assert exc.value.status_code == 404
    assert "Notification not found" in exc.value.detail


@pytest.mark.asyncio
async def test_get_notification_workspace_isolation_returns_404(monkeypatch):
    """跨 workspace 查询返回 404（不区分不存在与无权限）。"""
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(n, "pool", _Pool(conn))

    other_actor = _actor(workspace_id="wsp_other")
    with pytest.raises(HTTPException) as exc:
        await n.get_notification("notif_1", other_actor)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_mark_read_returns_404_when_notification_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(n, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await n.mark_read("notif_missing", _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_mark_read_returns_summary_when_success(monkeypatch):
    row = _notif_row(read=True, read_at=datetime.now(UTC))
    conn = _SeqConnection(results=[_Result(row=row)])
    monkeypatch.setattr(n, "pool", _Pool(conn))

    result = await n.mark_read("notif_1", _actor())
    assert result["read"] is True
    assert result["read_at"] is not None


@pytest.mark.asyncio
async def test_delete_notification_returns_deleted_id(monkeypatch):
    conn = _SeqConnection(results=[_Result(row={"id": "notif_1"})])
    monkeypatch.setattr(n, "pool", _Pool(conn))

    result = await n.delete_notification("notif_1", _actor())
    assert result == {"id": "notif_1", "deleted": True}
    delete_q = next(q for q, _ in conn.calls if "DELETE FROM notification" in q)
    assert "user_id = %s" in delete_q
    assert "workspace_id = %s" in delete_q


@pytest.mark.asyncio
async def test_delete_notification_returns_404_when_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(n, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await n.delete_notification("notif_missing", _actor())
    assert exc.value.status_code == 404


# ============================================================================
# 4. notification/ 包：通知偏好设置（service + router）
# ============================================================================


from workama_platform.modules.notification import service as notif_service
from workama_platform.modules.notification import router as notif_router


def test_notification_preference_change_blocked_for_forced_in_app():
    """security./auth./billing. 前缀事件的 in-app 渠道不可禁用。"""
    assert not notif_service.preference_change_allowed(
        "security.login_failure", "in_app", enabled=False
    )
    assert not notif_service.preference_change_allowed(
        "billing.low_balance", "in_app", enabled=False
    )
    assert not notif_service.preference_change_allowed(
        "auth.token_revoked", "in_app", enabled=False
    )


def test_notification_preference_change_allowed_for_other_events():
    assert notif_service.preference_change_allowed(
        "agent.completed", "in_app", enabled=False
    )
    assert notif_service.preference_change_allowed(
        "billing.low_balance", "email", enabled=False
    )
    assert notif_service.preference_change_allowed(
        "security.login_failure", "webhook", enabled=False
    )


@pytest.mark.asyncio
async def test_notification_channel_enabled_raises_for_unsupported_channel():
    with pytest.raises(ValueError, match="Unsupported notification channel"):
        await notif_service.notification_channel_enabled(
            object(),
            user_id="usr_test",
            workspace_id="wsp_test",
            event_type="agent.completed",
            channel="sms",
        )


@pytest.mark.asyncio
async def test_notification_channel_enabled_returns_true_for_forced_in_app():
    """forced in-app 事件总是返回 True，不查 DB。"""
    class _NoCallConn:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("should not query DB for forced in-app")

    result = await notif_service.notification_channel_enabled(
        _NoCallConn(),
        user_id="usr_test",
        workspace_id="wsp_test",
        event_type="security.login_failure",
        channel="in_app",
    )
    assert result is True


@pytest.mark.asyncio
async def test_update_preference_endpoint_returns_409_for_forced_in_app_disabled():
    """强制 in-app 渠道禁用时返回 409。"""
    body = notif_router.NotificationPreferenceUpsert(
        event_type="security.login_failure", channel="in_app", enabled=False
    )
    with pytest.raises(HTTPException) as exc:
        await notif_router.update_notification_preferences(body, _actor())
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_update_preference_endpoint_validates_event_type_pattern():
    """非法 event_type（含特殊字符）触发 422。"""
    body = notif_router.NotificationPreferenceUpsert(
        event_type="INVALID UPPER", channel="in_app", enabled=True
    )
    with pytest.raises(HTTPException) as exc:
        await notif_router.update_notification_preferences(body, _actor())
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_update_preference_endpoint_inserts_or_upserts(monkeypatch):
    conn = _SeqConnection(
        results=[_Result(row={"event_type": "agent.completed", "channel": "in_app", "enabled": True})]
    )
    monkeypatch.setattr(notif_router.pool, "connection", lambda: _Pool(conn).connection())

    body = notif_router.NotificationPreferenceUpsert(
        event_type="agent.completed", channel="in_app", enabled=True
    )
    result = await notif_router.update_notification_preferences(body, _actor())

    assert result["event_type"] == "agent.completed"
    assert result["enabled"] is True
    upsert_q = next(q for q, _ in conn.calls if "INSERT INTO id_notification_preference" in q)
    assert "ON CONFLICT" in upsert_q


# ============================================================================
# 5. notification/ 包：create_notification 去重逻辑
# ============================================================================


@pytest.mark.asyncio
async def test_create_notification_service_creates_with_idempotent_dedupe_key():
    """相同 dedupe_key 第二次插入返回 created=False。"""
    new_row = {"id": "ntf_new", "created_at": datetime.now(UTC)}
    conn = _SeqConnection(results=[_Result(row=new_row)])  # INSERT ON CONFLICT DO NOTHING RETURNING 命中
    result = await notif_service.create_notification(
        conn,
        user_id="usr_test",
        workspace_id="wsp_test",
        event_type="agent.completed",
        title="T",
        summary="S",
        dedupe_key="agent.completed:run_1",
        channels=("in_app", "email"),
    )
    assert result["id"] == "ntf_new"
    assert result["created"] is True


@pytest.mark.asyncio
async def test_create_notification_service_handles_duplicate_returns_existing():
    """ON CONFLICT DO NOTHING 时无 RETURNING → 走 SELECT 现有记录路径。"""
    class _ConflictResult:
        async def fetchone(self):
            return None  # INSERT ON CONFLICT DO NOTHING 返回 None

    class _ExistingResult:
        async def fetchone(self):
            return {"id": "ntf_existing", "created_at": datetime.now(UTC)}

    class _DualConn:
        def __init__(self):
            self._calls = 0

        async def execute(self, query, params=()):
            self._calls += 1
            # 第 1 次 INSERT (ON CONFLICT DO NOTHING RETURNING) 返回 None
            # 第 2 次查询 existing 应返回 existing
            if "ON CONFLICT" in query and self._calls == 1:
                return _ConflictResult()
            return _ExistingResult()

    conn = _DualConn()
    result = await notif_service.create_notification(
        conn,
        user_id="usr_test",
        workspace_id="wsp_test",
        event_type="agent.completed",
        title="T",
        summary="S",
        dedupe_key="dup",
        channels=("in_app",),
    )
    assert result["created"] is False
    assert result["id"] == "ntf_existing"


@pytest.mark.asyncio
async def test_create_notification_service_rejects_invalid_channels():
    conn = _SeqConnection()
    with pytest.raises(ValueError, match="Notification channels are invalid"):
        await notif_service.create_notification(
            conn,
            user_id="usr_test",
            workspace_id="wsp_test",
            event_type="agent.completed",
            title="T",
            summary="S",
            channels=("sms", "fax"),  # 非法渠道
        )


@pytest.mark.asyncio
async def test_create_mock_webhook_endpoint_rejects_non_mock_url():
    """非 mock:// URL 不能注册为 mock endpoint。"""
    conn = _SeqConnection()
    with pytest.raises(ValueError, match="mock://"):
        await notif_service.create_mock_webhook_endpoint(
            conn,
            org_id="org_test",
            workspace_id="wsp_test",
            owner_user_id="usr_test",
            name="prod",
            url="https://example.com/webhook",
            secret="s",
        )


@pytest.mark.asyncio
async def test_create_mock_webhook_endpoint_persists_hashed_secret(monkeypatch):
    """secret 必须以 hash 形式持久化，不存储明文。"""
    conn = _SeqConnection(results=[_Result()])
    captured_params: list[tuple] = []

    async def capture_execute(query, params=()):
        captured_params.append(params)
        return _Result()

    conn.execute = capture_execute  # type: ignore[assignment]
    result = await notif_service.create_mock_webhook_endpoint(
        conn,
        org_id="org_test",
        workspace_id="wsp_test",
        owner_user_id="usr_test",
        name="mock-endpoint",
        url="mock://notifications",
        secret="super-secret",
    )

    assert result["id"].startswith("whk_")
    insert_params = captured_params[0]
    # secret_hash 不应等于明文 secret
    assert insert_params[6] != "super-secret"
    assert insert_params[6]  # 非空


# ============================================================================
# 6. notification/ 包：投递记录与状态机（delivery.py）
# ============================================================================


from workama_platform.modules.notification import delivery as notif_delivery


@pytest.mark.asyncio
async def test_record_failure_marks_failed_when_attempt_exceeds_max(monkeypatch):
    """非重试错误或达到 max_attempts 时，状态为 failed。"""
    row = {
        "id": "ndl_1",
        "attempt": 2,  # attempt + 1 = 3
        "max_attempts": 3,
        "_schema_extended": True,
    }
    conn = _SeqConnection(results=[_Result()])
    monkeypatch.setattr(notif_delivery, "pool", _Pool(conn))

    final = await notif_delivery._record_failure(row, ValueError("bad address"), retryable=False)

    assert final is True
    update_q = next(q for q, _ in conn.calls if "UPDATE id_notification_delivery" in q)
    assert "status=%s" in update_q


@pytest.mark.asyncio
async def test_record_failure_marks_pending_when_retryable_and_under_max(monkeypatch):
    """可重试且未达 max_attempts 时，状态为 pending（带 next_attempt_at）。"""
    row = {
        "id": "ndl_1",
        "attempt": 0,  # attempt + 1 = 1
        "max_attempts": 5,
        "_schema_extended": True,
    }
    conn = _SeqConnection(results=[_Result()])
    monkeypatch.setattr(notif_delivery, "pool", _Pool(conn))

    final = await notif_delivery._record_failure(
        row, TimeoutError("provider timeout"), retryable=True
    )

    assert final is False
    update_q = next(q for q, _ in conn.calls if "UPDATE id_notification_delivery" in q)
    assert "next_attempt_at" in update_q


@pytest.mark.asyncio
async def test_record_success_marks_sent_with_provider_id(monkeypatch):
    row = {"id": "ndl_1", "_schema_extended": True}
    conn = _SeqConnection(results=[_Result()])
    monkeypatch.setattr(notif_delivery, "pool", _Pool(conn))

    await notif_delivery._record_success(row, "smtp:abc123", response_code=250)

    update_q = next(q for q, _ in conn.calls if "UPDATE id_notification_delivery" in q)
    assert "status='sent'" in update_q
    update_params = next(p for q, p in conn.calls if "status='sent'" in q)
    assert update_params[0] == "smtp:abc123"


@pytest.mark.asyncio
async def test_process_pending_email_deliveries_uses_mock_path(monkeypatch):
    """mock=True 路径调用 send_email，应产出 sent 计数。"""
    claimed_row = {
        "id": "ndl_1",
        "notification_id": "ntf_1",
        "webhook_id": None,
        "attempt": 0,
        "max_attempts": 3,
        "idempotency_key": "key-1",
        "title": "Hello",
        "summary": "World",
        "event_type": "agent.completed",
        "priority": "normal",
        "action_url": None,
        "resource_ref": None,
        "workspace_id": "wsp_test",
        "email": "user@example.com",
        "_schema_extended": True,
    }

    # _claim_deliveries 返回 1 行；_record_success 走 mock 路径
    claim_conn = _SeqConnection(
        results=[
            _Result(row={"available": True}),  # extension_check
            _Result(rows=[claimed_row]),  # SELECT claimed
            _Result(),  # UPDATE status='sending'
            _Result(),  # commit
        ]
    )

    success_conn = _SeqConnection(results=[_Result()])

    # 切换 pool 在不同阶段返回不同 conn
    pool_index = {"i": 0}
    conns = [claim_conn, success_conn]

    class _SwitchingPool:
        def connection(self):
            idx = pool_index["i"]
            pool_index["i"] += 1
            conn = conns[idx]

            class _Context:
                async def __aenter__(self):
                    return conn

                async def __aexit__(self, *_args):
                    return False

            return _Context()

    monkeypatch.setattr(notif_delivery, "pool", _SwitchingPool())

    result = await notif_delivery.process_pending_email_deliveries(limit=20, mock=True)

    assert result["claimed"] == 1
    assert result["sent"] == 1
    assert result["failed"] == 0


# ============================================================================
# 7. notification/ 包：邮件/Webhook mock 投递
# ============================================================================


def test_send_email_mock_returns_deterministic_provider_id():
    """相同 (recipient, title, summary) 三元组产生相同的 mock provider_id。"""
    first = notif_delivery.send_email(
        "user@example.com", "Subject", "Body", mock=True
    )
    second = notif_delivery.send_email(
        "user@example.com", "Subject", "Body", mock=True
    )
    assert first == second
    assert first.startswith("mock-email:")
    assert len(first) > len("mock-email:")


def test_send_email_mock_distinct_for_different_recipients():
    first = notif_delivery.send_email("a@example.com", "S", "B", mock=True)
    second = notif_delivery.send_email("b@example.com", "S", "B", mock=True)
    assert first != second


def test_send_email_real_raises_when_smtp_not_configured():
    """mock=False 且无 smtp_host 时应抛 RuntimeError。"""
    # 默认 settings.smtp_host 是空字符串
    with pytest.raises(RuntimeError, match="smtp_not_configured"):
        notif_delivery.send_email("a@example.com", "S", "B", mock=False)


@pytest.mark.asyncio
async def test_deliver_email_uses_mock_when_settings_smtp_mock_true(monkeypatch):
    """settings.smtp_mock=True 时使用 mock 路径。"""
    captured: dict = {}

    class _FakeSettings:
        smtp_mock = True
        smtp_host = ""

    def fake_send_email(recipient, title, summary, *, mock=False):
        captured["mock"] = mock
        return "mock-email:abc"

    monkeypatch.setattr(notif_delivery, "send_email", fake_send_email)

    provider_id = await notif_delivery.deliver_email(
        "user@example.com", "Subject", "<html>Body</html>", "Body text",
        settings=_FakeSettings(),
    )
    assert provider_id == "mock-email:abc"


def test_send_webhook_mock_rejects_non_mock_url():
    with pytest.raises(ValueError, match="mock_webhook_url_required"):
        notif_delivery.send_webhook_mock(
            "https://example.com", "secret", {}, "key-1"
        )


def test_send_webhook_mock_signature_format():
    """mock webhook 签名应为 t=<ts>,v1=<hex> 格式。"""
    timestamp = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    result = notif_delivery.send_webhook_mock(
        "mock://notifications",
        "secret-hash",
        {"event_type": "agent.completed"},
        "delivery-1",
        occurred_at=timestamp,
    )
    assert result["status_code"] == 202
    assert result["provider_id"].startswith("mock-webhook:")
    assert result["signature"].startswith(f"t={int(timestamp.timestamp())},v1=")
    assert "secret-hash" not in result["body"]  # 明文 secret 不应出现在 body


def test_send_webhook_mock_idempotent_for_same_inputs():
    """相同输入应产生相同的 provider_id 与 signature。"""
    timestamp = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
    payload = {"event_type": "agent.completed", "resource_ref": "run_1"}
    first = notif_delivery.send_webhook_mock(
        "mock://ep", "secret", payload, "key-1", occurred_at=timestamp
    )
    second = notif_delivery.send_webhook_mock(
        "mock://ep", "secret", payload, "key-1", occurred_at=timestamp
    )
    assert first["provider_id"] == second["provider_id"]
    assert first["signature"] == second["signature"]


@pytest.mark.asyncio
async def test_deliver_webhook_uses_mock_when_settings_notification_webhook_mock_true():
    """settings.notification_webhook_mock=True 时使用 mock 路径。"""
    class _FakeSettings:
        notification_webhook_mock = True

    result = await notif_delivery.deliver_webhook(
        "mock://ep", "secret", {"event_type": "x"}, "key-1",
        occurred_at=datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
        settings=_FakeSettings(),
    )
    assert result["status_code"] == 202
    assert result["provider_id"].startswith("mock-webhook:")


# ============================================================================
# 8. notification/ 包：错误分类与退避策略
# ============================================================================


def test_classify_delivery_error_returns_transient_for_network_errors():
    assert (
        notif_delivery.classify_delivery_error(TimeoutError("slow"))
        == "transient_provider_error"
    )
    import httpx

    assert (
        notif_delivery.classify_delivery_error(httpx.ConnectError("nope"))
        == "transient_provider_error"
    )


def test_classify_delivery_error_returns_message_string_for_other_errors():
    assert (
        notif_delivery.classify_delivery_error(ValueError("bad address"))
        == "bad address"
    )


def test_retry_delay_seconds_bounded_at_12_hours():
    """退避序列：1m / 5m / 30m / 2h / 12h，封顶 12h。"""
    assert notif_service.retry_delay_seconds(1) == 60
    assert notif_service.retry_delay_seconds(2) == 300
    assert notif_service.retry_delay_seconds(3) == 1800
    assert notif_service.retry_delay_seconds(4) == 7200
    assert notif_service.retry_delay_seconds(5) == 43200
    assert notif_service.retry_delay_seconds(99) == 43200  # 封顶
