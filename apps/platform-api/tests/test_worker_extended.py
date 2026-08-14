"""worker.py 主循环纯函数的扩展单元测试。

覆盖 process_metering_event、handle_metering_message、ensure_stream、
process_outbox 以及若干纯辅助函数。所有外部依赖（pool/redis/nats JS）
均通过 fake 类与 monkeypatch 隔离，不依赖真实 DB/Redis/NATS。

测试风格参考 test_automation_worker.py：使用简单的 fake 类 + monkeypatch。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import quote

import pytest
from nats.js.errors import NotFoundError

from workama_platform import worker
from workama_platform.core import settings
from workama_platform.modules.billing.metering import MeteringEvent
from workama_platform.worker import (
    AutomationExecutionError,
    WorkExecutionError,
    _agent_websocket_uri,
    _utc_datetime,
    _workflow_node_types,
    automation_cron_idempotency_key,
    ensure_stream,
    handle_metering_message,
    process_metering_event,
    process_outbox,
)


# ----------------------------------------------------------------------
# 通用 fake 类：模拟 pool / connection / transaction / result
# ----------------------------------------------------------------------


class _Result:
    """模拟 DB 结果集。"""

    def __init__(self, rows=None):
        self.rows = list(rows or [])

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return self.rows[0] if self.rows else None


class _Transaction:
    """模拟事务上下文。"""

    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Pool:
    """模拟连接池，每次 connection() 返回同一个连接。"""

    def __init__(self, connection):
        self.connection_value = connection
        self._closed = False

    def connection(self):
        if self._closed:
            raise RuntimeError("pool is closed")
        connection_value = self.connection_value

        class _Context:
            async def __aenter__(_self):
                return connection_value

            async def __aexit__(_self, exc_type, exc, traceback):
                return False

        return _Context()

    async def close(self):
        self._closed = False  # 保持可用状态，不影响后续测试

    async def open(self):
        self._closed = False


class _AssertingPool:
    """访问即抛错的 pool，用于验证函数是否进入 DB 阶段。"""

    def connection(self):
        raise AssertionError("should reach pool only after validation passes")


class _FakeMessage:
    """模拟 NATS 消息。"""

    def __init__(self, data: bytes, headers=None):
        self.data = data
        self.subject = "metering.llm.v1"
        self.headers = headers
        self.acked = False
        self.terminated = False
        self.nak_delay: int | None = None

    async def ack(self):
        self.acked = True

    async def term(self):
        self.terminated = True

    async def nak(self, delay: int):
        self.nak_delay = delay


class _FakeJob:
    """模拟 job 对象。"""

    def __init__(self, payload, workspace_id="wsp_1", operation_id="op_1"):
        self.payload = payload
        self.workspace_id = workspace_id
        self.operation_id = operation_id


# ----------------------------------------------------------------------
# 测试数据辅助
# ----------------------------------------------------------------------


def _metering_event_payload(**overrides) -> dict:
    """构造合法的 MeteringEvent 字典。"""
    payload = {
        "schema_version": 1,
        "event_id": "evt_01KXTESTMETERING00000000000",
        "event_type": "metering.llm.v1",
        "occurred_at": datetime.now(UTC).isoformat(),
        "producer": "gateway",
        "workspace_id": "wsp_01KXTESTWORKSPACE0000000000",
        "trace_id": "req_01KXTESTREQUEST000000000000",
        "idempotency_key": "req_01KXTESTREQUEST000000000000",
        "classification": "C2",
        "payload": {
            "request_id": "req_01KXTESTREQUEST000000000000",
            "model": "workama-chat",
            "prompt_tokens": 120,
            "completion_tokens": 40,
            "latency_ms": 820,
            "status_code": 200,
        },
    }
    payload.update(overrides)
    return payload


def _metering_event(**overrides) -> MeteringEvent:
    return MeteringEvent.model_validate(_metering_event_payload(**overrides))


# ======================================================================
# 1. _utc_datetime 纯函数测试
# ======================================================================


def test_utc_datetime_returns_default_now_when_value_none_and_no_default():
    """value=None 且无 default 时应回退到当前 UTC 时间。"""
    before = datetime.now(UTC)
    result = _utc_datetime(None)
    after = datetime.now(UTC)
    assert result.tzinfo == UTC
    assert before <= result <= after


def test_utc_datetime_uses_explicit_default_when_value_none():
    """value=None 时应使用显式 default。"""
    default = datetime(2026, 1, 15, 10, 30, tzinfo=UTC)
    result = _utc_datetime(None, default=default)
    assert result == default


def test_utc_datetime_passthrough_for_aware_datetime():
    """aware datetime 应原样返回（已为 UTC）。"""
    value = datetime(2026, 7, 16, 2, 3, 45, tzinfo=UTC)
    result = _utc_datetime(value)
    assert result == value


def test_utc_datetime_assumes_utc_for_naive_datetime():
    """naive datetime 应被假定为 UTC。"""
    value = datetime(2026, 7, 16, 2, 3, 45)
    result = _utc_datetime(value)
    assert result.tzinfo == UTC
    assert result == value.replace(tzinfo=UTC)


def test_utc_datetime_parses_iso_string_with_z_suffix():
    """ISO 字符串带 Z 后缀应被正确解析为 UTC。"""
    result = _utc_datetime("2026-07-16T02:03:45Z")
    assert result == datetime(2026, 7, 16, 2, 3, 45, tzinfo=UTC)


def test_utc_datetime_parses_iso_string_with_offset():
    """带时区偏移的 ISO 字符串应被转换为 UTC。"""
    # 2026-07-16T04:03:45+02:00 == 02:03:45 UTC
    result = _utc_datetime("2026-07-16T04:03:45+02:00")
    assert result == datetime(2026, 7, 16, 2, 3, 45, tzinfo=UTC)


# ======================================================================
# 2. automation_cron_idempotency_key 纯函数测试
# ======================================================================


def test_automation_cron_idempotency_key_strips_seconds_and_microseconds():
    """秒和微秒应被清零，以保证同一分钟内稳定。"""
    due = datetime(2026, 7, 16, 2, 3, 45, 123456, tzinfo=UTC)
    key = automation_cron_idempotency_key("sched_1", due)
    assert key == "cron:sched_1:2026-07-16T02:03:00+00:00"


def test_automation_cron_idempotency_key_is_stable_within_same_minute():
    """同一分钟内不同秒数应产生相同的 key。"""
    due_a = datetime(2026, 7, 16, 2, 3, 10, tzinfo=UTC)
    due_b = datetime(2026, 7, 16, 2, 3, 59, tzinfo=UTC)
    assert automation_cron_idempotency_key("sched_1", due_a) == automation_cron_idempotency_key(
        "sched_1", due_b
    )


def test_automation_cron_idempotency_key_changes_across_minutes():
    """不同分钟应产生不同的 key。"""
    due_a = datetime(2026, 7, 16, 2, 3, 0, tzinfo=UTC)
    due_b = datetime(2026, 7, 16, 2, 4, 0, tzinfo=UTC)
    assert automation_cron_idempotency_key("sched_1", due_a) != automation_cron_idempotency_key(
        "sched_1", due_b
    )


def test_automation_cron_idempotency_key_includes_schedule_id_and_occurrence():
    """key 应包含 schedule_id 与 occurrence 时间。"""
    due = datetime(2026, 7, 16, 2, 3, 0, tzinfo=UTC)
    key = automation_cron_idempotency_key("sched_42", due)
    assert key.startswith("cron:sched_42:")
    assert "2026-07-16T02:03:00+00:00" in key


def test_automation_cron_idempotency_key_accepts_naive_datetime():
    """naive datetime 应被当作 UTC 处理。"""
    due_naive = datetime(2026, 7, 16, 2, 3, 0)
    due_aware = datetime(2026, 7, 16, 2, 3, 0, tzinfo=UTC)
    assert automation_cron_idempotency_key("sched_1", due_naive) == automation_cron_idempotency_key(
        "sched_1", due_aware
    )


# ======================================================================
# 3. _unsupported_automation_result 纯函数测试
# ======================================================================


@pytest.mark.parametrize(
    "run,expected_in_message",
    [
        ({"target_type": "work_plan"}, "work_plan"),
        ({"target_type": "workflow"}, "workflow"),
        ({"target_type": "agent"}, "agent"),
        ({}, "unknown"),
        ({"target_type": None}, "unknown"),
    ],
)
def test_unsupported_automation_result_contains_target_type(run, expected_in_message):
    """错误消息中应包含 target_type，缺失时回退到 'unknown'。"""
    result = worker._unsupported_automation_result(run)
    assert result["status"] == "failed"
    assert result["execution_status"] == "unsupported"
    assert result["executed"] is False
    assert result["error_code"] == "unsupported_target"
    assert expected_in_message in result["error_message"]


# ======================================================================
# 4. AutomationExecutionError 测试
# ======================================================================


def test_automation_execution_error_defaults():
    """默认 execution_status='failed', executed=False。"""
    err = AutomationExecutionError("code_1", "something failed")
    assert err.code == "code_1"
    assert str(err) == "something failed"
    assert err.execution_status == "failed"
    assert err.executed is False
    assert isinstance(err, RuntimeError)


def test_automation_execution_error_carries_custom_fields():
    """自定义 execution_status 和 executed 应被保留。"""
    err = AutomationExecutionError(
        "agent_timeout", "timed out", execution_status="incomplete", executed=True
    )
    assert err.code == "agent_timeout"
    assert err.execution_status == "incomplete"
    assert err.executed is True


# ======================================================================
# 5. _workflow_node_types 纯函数测试
# ======================================================================


def test_workflow_node_types_returns_empty_for_empty_graph():
    """空 graph 应返回空集合。"""
    assert _workflow_node_types({}) == set()
    assert _workflow_node_types({"nodes": []}) == set()


def test_workflow_node_types_canonicalizes_aliases():
    """节点类型应经过 canonical_node_type 规范化（如 start -> input）。"""
    graph = {
        "nodes": [
            {"type": "start"},  # 别名 -> input
            {"type": "answer"},  # 别名 -> output
            {"type": "transform"},  # 不在别名表,原样返回
        ]
    }
    result = _workflow_node_types(graph)
    assert result == {"input", "output", "transform"}


def test_workflow_node_types_ignores_non_dict_nodes():
    """非 dict 节点应被忽略。"""
    graph = {"nodes": [{"type": "transform"}, "not-a-dict", None, 42]}
    result = _workflow_node_types(graph)
    assert result == {"transform"}


# ======================================================================
# 6. _agent_websocket_uri 纯函数测试（依赖 settings.agent_server_url）
# ======================================================================


def test_agent_websocket_uri_builds_ws_for_http_scheme(monkeypatch):
    """HTTP agent_server_url 应映射为 ws:// 协议。"""
    monkeypatch.setattr(settings, "agent_server_url", "http://agent-server:8001")
    uri = _agent_websocket_uri("sess_123", "ticket_abc", 42)
    assert uri.startswith("ws://")
    assert "agent-server:8001" in uri
    assert "/ws/sessions/sess_123" in uri
    assert "ticket=ticket_abc" in uri
    assert "after=42" in uri


def test_agent_websocket_uri_builds_wss_for_https_scheme(monkeypatch):
    """HTTPS agent_server_url 应映射为 wss:// 协议。"""
    monkeypatch.setattr(settings, "agent_server_url", "https://agent.example.com")
    uri = _agent_websocket_uri("sess_1", "tick", 0)
    assert uri.startswith("wss://")
    assert "agent.example.com" in uri


def test_agent_websocket_uri_quotes_session_id_and_ticket(monkeypatch):
    """session_id 和 ticket 中特殊字符应被 URL 编码。"""
    monkeypatch.setattr(settings, "agent_server_url", "http://agent:8001")
    uri = _agent_websocket_uri("sess/with space", "tic+ket", 1)
    assert quote("sess/with space", safe="") in uri
    assert quote("tic+ket", safe="") in uri


def test_agent_websocket_uri_rejects_non_http_scheme(monkeypatch):
    """非 http/https 的 agent_server_url 应抛出 AutomationExecutionError。"""
    monkeypatch.setattr(settings, "agent_server_url", "ftp://agent-server")
    with pytest.raises(AutomationExecutionError) as exc_info:
        _agent_websocket_uri("sess_1", "tick", 0)
    # code 属性标识具体错误类型；message 描述原因
    assert exc_info.value.code == "agent_server_unconfigured"
    assert "internal HTTP service" in str(exc_info.value)


def test_agent_websocket_uri_rejects_empty_url(monkeypatch):
    """空 agent_server_url 应抛出 AutomationExecutionError。"""
    monkeypatch.setattr(settings, "agent_server_url", "")
    with pytest.raises(AutomationExecutionError) as exc_info:
        _agent_websocket_uri("sess_1", "tick", 0)
    assert exc_info.value.code == "agent_server_unconfigured"
    assert "internal HTTP service" in str(exc_info.value)


# ======================================================================
# 7. process_metering_event（mock pool 与 settle_meter_in_transaction）
# ======================================================================


class _MeteringConnection:
    """模拟 process_metering_event 中的 DB 连接。"""

    def __init__(self, inbox_row=None):
        self.inbox_row = inbox_row
        self.statements = []

    def transaction(self):
        return _Transaction(self)

    async def execute(self, statement, params=()):
        normalized = " ".join(statement.split())
        self.statements.append((normalized, params))
        if normalized.startswith("INSERT INTO ops_inbox"):
            return _Result([self.inbox_row] if self.inbox_row else [])
        if normalized.startswith("UPDATE ops_inbox SET status = 'processed'"):
            return _Result([])
        # Handle credit grant expiration queries
        if "bill_credit_grant" in normalized:
            return _Result([])
        # Handle other billing-related queries
        if "bill_" in normalized:
            return _Result([])
        raise AssertionError(f"unexpected SQL: {normalized}")


@pytest.mark.asyncio
async def test_process_metering_event_inserts_and_settles_on_new_inbox(monkeypatch):
    """新事件应插入 ops_inbox、调用 settle、并标记为 processed。"""
    connection = _MeteringConnection(inbox_row={"id": "inb_1"})
    pool_instance = _Pool(connection)
    monkeypatch.setattr(worker, "pool", pool_instance)
    # 同时 patch metering 模块中的 pool 引用
    from workama_platform.modules.billing import metering
    monkeypatch.setattr(metering, "pool", pool_instance)
    settle_calls = []

    async def settle(_conn, payload):
        settle_calls.append(payload)

    # settle_meter_in_transaction 在 metering 模块中定义和被调用
    monkeypatch.setattr(metering, "settle_meter_in_transaction", settle)

    event = _metering_event()
    await process_metering_event(event, "metering.llm.v1")

    # 第一条 SQL 应是 INSERT ... ops_inbox
    assert "INSERT INTO ops_inbox" in connection.statements[0][0]
    # 应有 UPDATE ops_inbox SET status = 'processed'
    assert any(
        "UPDATE ops_inbox SET status = 'processed'" in s for s, _ in connection.statements
    )
    # settle 应被调用一次，传入 event.payload
    assert len(settle_calls) == 1
    assert settle_calls[0] is event.payload


@pytest.mark.asyncio
async def test_process_metering_event_skips_settle_on_duplicate_inbox(monkeypatch):
    """重复事件（inbox 为 None）应早退，不调用 settle，不更新状态。"""
    connection = _MeteringConnection(inbox_row=None)
    pool_instance = _Pool(connection)
    monkeypatch.setattr(worker, "pool", pool_instance)
    # 同时 patch metering 模块中的 pool 引用
    from workama_platform.modules.billing import metering
    monkeypatch.setattr(metering, "pool", pool_instance)
    settle_calls = []

    async def settle(_conn, payload):
        settle_calls.append(payload)

    monkeypatch.setattr(metering, "settle_meter_in_transaction", settle)

    event = _metering_event()
    await process_metering_event(event, "metering.llm.v1")

    # 只应有 INSERT 一条 SQL（ON CONFLICT DO NOTHING 返回 None 后早退）
    assert len(connection.statements) == 1
    assert "INSERT INTO ops_inbox" in connection.statements[0][0]
    assert "ON CONFLICT" in connection.statements[0][0]
    assert settle_calls == []


@pytest.mark.asyncio
async def test_process_metering_event_passes_subject_and_consumer_name(monkeypatch):
    """INSERT 参数应包含传入的 subject 与模块常量 CONSUMER_NAME。"""
    connection = _MeteringConnection(inbox_row={"id": "inb_1"})
    pool_instance = _Pool(connection)
    monkeypatch.setattr(worker, "pool", pool_instance)
    # 同时 patch metering 模块中的 pool 引用
    from workama_platform.modules.billing import metering
    monkeypatch.setattr(metering, "pool", pool_instance)

    async def settle(_conn, _payload):
        pass

    monkeypatch.setattr(metering, "settle_meter_in_transaction", settle)

    event = _metering_event()
    await process_metering_event(event, "metering.custom.subject")

    insert_params = connection.statements[0][1]
    # 参数顺序: id, event_id, subject, consumer_name, payload
    assert insert_params[2] == "metering.custom.subject"
    assert insert_params[3] == worker.CONSUMER_NAME


# ======================================================================
# 8. handle_metering_message 扩展测试（mock processor）
# ======================================================================


@pytest.mark.asyncio
async def test_handle_metering_message_extracts_headers_for_trace_context():
    """带 headers 的消息应成功提取 trace 上下文并调用 processor。"""
    event = _metering_event()
    message = _FakeMessage(
        event.model_dump_json().encode(),
        headers={"traceparent": "00-" + "0" * 32 + "-" + "0" * 16 + "-01"},
    )
    captured = []

    async def processor(ev, subject):
        captured.append((ev.event_id, subject))

    await handle_metering_message(message, processor=processor)

    assert message.acked is True
    assert message.terminated is False
    assert message.nak_delay is None
    assert captured == [(event.event_id, "metering.llm.v1")]


@pytest.mark.asyncio
async def test_handle_metering_message_handles_missing_headers_attribute():
    """消息 headers 为 None 时应不抛异常，正常 ack。"""
    event = _metering_event()
    message = _FakeMessage(event.model_dump_json().encode())
    message.headers = None  # 模拟 getattr 返回 None
    called = []

    async def processor(ev, subject):
        called.append(ev.event_id)

    await handle_metering_message(message, processor=processor)

    assert message.acked is True
    assert called == [event.event_id]


@pytest.mark.asyncio
async def test_handle_metering_message_terms_on_invalid_event_type():
    """event_type 不在 Literal 范围内应触发 ValidationError -> term。"""
    payload = _metering_event_payload()
    payload["event_type"] = "metering.unknown.v1"
    message = _FakeMessage(json.dumps(payload).encode())

    async def processor(_ev, _subject):
        raise AssertionError("processor should not be called for invalid event")

    await handle_metering_message(message, processor=processor)

    assert message.terminated is True
    assert message.acked is False


@pytest.mark.asyncio
async def test_handle_metering_message_naks_on_processor_exception():
    """processor 抛非 ValidationError 异常时应 nak(delay=5)。"""
    event = _metering_event()
    message = _FakeMessage(event.model_dump_json().encode())

    async def failing_processor(_ev, _subject):
        raise RuntimeError("db down")

    await handle_metering_message(message, processor=failing_processor)

    assert message.nak_delay == 5
    assert message.acked is False
    assert message.terminated is False


# ======================================================================
# 9. ensure_stream（mock NATS JS context）
# ======================================================================


class _StreamConfig:
    """模拟 nats.js.api.StreamConfig 的可变 subjects。"""

    def __init__(self, subjects=None):
        self.subjects = list(subjects or [])


class _StreamInfo:
    def __init__(self, subjects=None):
        self.config = _StreamConfig(subjects)


class _FakeJS:
    """模拟 NATS JetStream 上下文。"""

    def __init__(self, *, main_exists=True, control_exists=True, control_subjects=None):
        self.main_exists = main_exists
        self.control_exists = control_exists
        self._control_stream_info = _StreamInfo(control_subjects) if control_exists else None
        self.add_stream_calls = []
        self.update_stream_calls = []

    async def stream_info(self, name):
        if name == worker.STREAM_NAME:
            if self.main_exists:
                return _StreamInfo([worker.SUBJECT])
            raise NotFoundError("not found")
        if name == worker.CONTROL_STREAM_NAME:
            if self.control_exists:
                return self._control_stream_info
            raise NotFoundError("not found")
        raise NotFoundError("not found")

    async def add_stream(self, **kwargs):
        self.add_stream_calls.append(kwargs)

    async def update_stream(self, config):
        self.update_stream_calls.append(config)


@pytest.mark.asyncio
async def test_ensure_stream_creates_missing_main_stream():
    """主流不存在时应调用 add_stream 创建。"""
    js = _FakeJS(
        main_exists=False, control_exists=True, control_subjects=worker.CONTROL_SUBJECTS
    )
    await ensure_stream(js)
    main_adds = [c for c in js.add_stream_calls if c.get("name") == worker.STREAM_NAME]
    assert len(main_adds) == 1
    assert main_adds[0]["subjects"] == [worker.SUBJECT]
    assert main_adds[0]["max_age"] == 7 * 24 * 60 * 60


@pytest.mark.asyncio
async def test_ensure_stream_skips_when_main_stream_exists():
    """主流已存在时不应调用 add_stream。"""
    js = _FakeJS(
        main_exists=True, control_exists=True, control_subjects=worker.CONTROL_SUBJECTS
    )
    await ensure_stream(js)
    main_adds = [c for c in js.add_stream_calls if c.get("name") == worker.STREAM_NAME]
    assert main_adds == []


@pytest.mark.asyncio
async def test_ensure_stream_creates_missing_control_stream():
    """控制流不存在时应调用 add_stream 创建。"""
    js = _FakeJS(main_exists=True, control_exists=False)
    await ensure_stream(js)
    control_adds = [
        c for c in js.add_stream_calls if c.get("name") == worker.CONTROL_STREAM_NAME
    ]
    assert len(control_adds) == 1
    assert control_adds[0]["subjects"] == worker.CONTROL_SUBJECTS
    assert control_adds[0]["max_age"] == 30 * 24 * 60 * 60


@pytest.mark.asyncio
async def test_ensure_stream_updates_control_stream_when_subjects_missing():
    """控制流存在但缺少部分 subjects 时应调用 update_stream 补齐。"""
    js = _FakeJS(
        main_exists=True,
        control_exists=True,
        control_subjects=["config.changed.v1"],  # 只有部分
    )
    await ensure_stream(js)
    assert len(js.update_stream_calls) == 1
    updated_config = js.update_stream_calls[0]
    # 更新后的 subjects 应包含所有 CONTROL_SUBJECTS
    assert set(worker.CONTROL_SUBJECTS).issubset(set(updated_config.subjects))
    assert "config.changed.v1" in updated_config.subjects  # 原有 subject 保留


@pytest.mark.asyncio
async def test_ensure_stream_leaves_control_stream_when_subjects_complete():
    """控制流已包含所有 subjects 时不应调用 update_stream。"""
    js = _FakeJS(
        main_exists=True,
        control_exists=True,
        control_subjects=list(worker.CONTROL_SUBJECTS),
    )
    await ensure_stream(js)
    assert js.update_stream_calls == []


# ======================================================================
# 10. process_outbox（mock pool 与 js）
# ======================================================================


class _OutboxConnection:
    """模拟 process_outbox 中的 DB 连接。"""

    def __init__(self, items=None):
        self.items = list(items or [])
        self.statements = []
        self.commits = 0

    def transaction(self):
        return _Transaction(self)

    async def execute(self, statement, params=()):
        normalized = " ".join(statement.split())
        self.statements.append((normalized, params))
        if normalized.startswith(
            "SELECT id, event_type, workspace_id, trace_id, payload, attempts"
        ):
            return _Result(list(self.items))
        return _Result([])

    async def commit(self):
        self.commits += 1


class _PublishingJS:
    """模拟 JetStream publish 行为，可控制成功/失败。"""

    def __init__(self, fail_indices=None):
        self.fail_indices = set(fail_indices or [])
        self.publish_calls = []

    async def publish(self, subject, payload, headers=None):
        idx = len(self.publish_calls)
        self.publish_calls.append(
            {"subject": subject, "payload": payload, "headers": headers}
        )
        if idx in self.fail_indices:
            raise RuntimeError("nats publish failed")


def _outbox_item(item_id="out_1", event_type="config.changed.v1", trace_id="trace_1"):
    return {
        "id": item_id,
        "event_type": event_type,
        "workspace_id": "wsp_1",
        "trace_id": trace_id,
        "payload": {"key": "value"},
        "attempts": 0,
    }


@pytest.mark.asyncio
async def test_process_outbox_returns_zero_when_no_items(monkeypatch):
    """无待发布项时应返回 claimed=0, published=0。"""
    connection = _OutboxConnection(items=[])
    monkeypatch.setattr(worker, "pool", _Pool(connection))
    js = _PublishingJS()

    result = await process_outbox(js)

    assert result == {"claimed": 0, "published": 0}
    assert js.publish_calls == []


@pytest.mark.asyncio
async def test_process_outbox_publishes_and_marks_published(monkeypatch):
    """成功发布应将状态更新为 published，并增加 published 计数。"""
    items = [_outbox_item("out_1"), _outbox_item("out_2", event_type="feature_flag.changed.v1")]
    connection = _OutboxConnection(items=items)
    monkeypatch.setattr(worker, "pool", _Pool(connection))
    js = _PublishingJS()

    result = await process_outbox(js)

    assert result == {"claimed": 2, "published": 2}
    assert len(js.publish_calls) == 2
    # 每条 publish 应包含 Nats-Msg-Id header
    assert js.publish_calls[0]["headers"] == {"Nats-Msg-Id": "out_1"}
    assert js.publish_calls[1]["headers"] == {"Nats-Msg-Id": "out_2"}
    # envelope 应包含必要字段
    envelope = json.loads(js.publish_calls[0]["payload"].decode())
    assert envelope["event_id"] == "out_1"
    assert envelope["event_type"] == "config.changed.v1"
    assert envelope["workspace_id"] == "wsp_1"
    assert envelope["classification"] == "C2"
    assert envelope["producer"] == "platform-api"
    assert envelope["idempotency_key"] == "out_1"
    # 应有 UPDATE status='published' SQL
    assert any("status = 'published'" in s for s, _ in connection.statements)


@pytest.mark.asyncio
async def test_process_outbox_marks_failed_when_publish_raises(monkeypatch):
    """publish 抛异常时应将状态更新为 failed，记录 last_error。"""
    items = [_outbox_item("out_1")]
    connection = _OutboxConnection(items=items)
    monkeypatch.setattr(worker, "pool", _Pool(connection))
    js = _PublishingJS(fail_indices={0})

    result = await process_outbox(js)

    assert result == {"claimed": 1, "published": 0}
    # 应有 UPDATE status='failed' SQL
    assert any("status = 'failed'" in s for s, _ in connection.statements)
    # 应有 last_error = %s
    assert any("last_error = %s" in s for s, _ in connection.statements)
    # 应有 available_at 重试计算
    assert any("available_at" in s for s, _ in connection.statements)


@pytest.mark.asyncio
async def test_process_outbox_respects_limit_parameter(monkeypatch):
    """limit 参数应被传入 SELECT SQL。"""
    items = [_outbox_item("out_1")]
    connection = _OutboxConnection(items=items)
    monkeypatch.setattr(worker, "pool", _Pool(connection))
    js = _PublishingJS()

    await process_outbox(js, limit=5)

    # 第一条 SELECT 的参数应为 (5,)
    select_statement = connection.statements[0]
    assert "LIMIT %s" in select_statement[0]
    assert select_statement[1] == (5,)


@pytest.mark.asyncio
async def test_process_outbox_envelope_falls_back_to_id_when_trace_id_none(monkeypatch):
    """trace_id 为 None 时 envelope 应回退到 item['id']。"""
    items = [_outbox_item("out_1", trace_id=None)]
    connection = _OutboxConnection(items=items)
    monkeypatch.setattr(worker, "pool", _Pool(connection))
    js = _PublishingJS()

    await process_outbox(js)

    envelope = json.loads(js.publish_calls[0]["payload"].decode())
    # trace_id 为 None 时应回退到 item id
    assert envelope["trace_id"] == "out_1"


# ======================================================================
# 11. 早期返回路径测试（limit < 1）
# ======================================================================


@pytest.mark.asyncio
async def test_scan_due_automation_schedules_returns_zeros_for_limit_below_one():
    """limit < 1 时 scan_due_automation_schedules 应直接返回零计数。"""
    result = await worker.scan_due_automation_schedules(now=datetime.now(UTC), limit=0)
    assert result == {"scanned": 0, "enqueued": 0, "deduplicated": 0}


@pytest.mark.asyncio
async def test_process_automation_runs_returns_zeros_for_limit_below_one():
    """limit < 1 时 process_automation_runs 应直接返回零计数。"""
    result = await worker.process_automation_runs("worker-test", limit=0)
    assert result == {
        "claimed": 0,
        "succeeded": 0,
        "failed": 0,
        "unsupported": 0,
        "pending": 0,
    }


@pytest.mark.asyncio
async def test_process_pending_siem_deliveries_returns_zeros_for_limit_below_one():
    """limit < 1 时 process_pending_siem_deliveries 应直接返回零计数。"""
    result = await worker.process_pending_siem_deliveries(limit=0)
    assert result == {"claimed": 0, "delivered": 0, "retried": 0, "failed": 0, "disabled": 0}


@pytest.mark.asyncio
async def test_process_pending_external_app_invocations_returns_zeros_for_limit_below_one():
    """limit < 1 时 process_pending_external_app_invocations 应直接返回零计数。"""
    result = await worker.process_pending_external_app_invocations("worker-test", limit=0)
    assert result == {"claimed": 0, "succeeded": 0, "retried": 0, "failed": 0, "blocked": 0}


# ======================================================================
# 12. _work_actor 纯函数测试
# ======================================================================


def test_work_actor_builds_from_payload_with_defaults():
    """_work_actor 应从 payload 构建 Actor，缺失字段使用默认值。"""
    job = _FakeJob(
        {"actor_id": "usr_1", "org_id": "org_1", "actor_role": "admin"},
        workspace_id="wsp_test",
    )
    actor = worker._work_actor(job)
    assert actor.user_id == "usr_1"
    assert actor.workspace_id == "wsp_test"
    assert actor.org_id == "org_1"
    assert actor.role == "admin"
    assert actor.display_name == "WorkAMA worker"
    assert actor.actor_type == "system"
    assert actor.onboarding_completed is True


def test_work_actor_uses_created_by_when_actor_id_missing():
    """payload 缺少 actor_id 时应回退到 created_by 参数。"""
    job = _FakeJob({"org_id": "org_1"}, workspace_id="wsp_test")
    actor = worker._work_actor(job, created_by="fallback_user")
    assert actor.user_id == "fallback_user"


def test_work_actor_defaults_role_to_member():
    """缺失 actor_role 时应默认为 'member'。"""
    job = _FakeJob({}, workspace_id="wsp_test")
    actor = worker._work_actor(job)
    assert actor.role == "member"
    assert actor.org_id == ""


# ======================================================================
# 13. 输入校验/早退路径测试（不依赖真实 DB）
# ======================================================================


@pytest.mark.asyncio
async def test_mark_workflow_run_failed_returns_early_when_run_id_missing():
    """mark_workflow_run_failed 在 run_id 缺失时应早退，不访问 DB。"""
    job = _FakeJob({})  # 既无 run_id 也无 workflow_id
    # 不 monkeypatch pool，若函数尝试访问 pool 会抛错，证明早退
    await worker.mark_workflow_run_failed(job, "some error")


@pytest.mark.asyncio
async def test_mark_workflow_run_failed_returns_early_when_workflow_id_missing():
    """mark_workflow_run_failed 在 workflow_id 缺失时应早退。"""
    job = _FakeJob({"run_id": "r1"})  # 只有 run_id
    await worker.mark_workflow_run_failed(job, "some error")


@pytest.mark.asyncio
async def test_mark_work_plan_failed_returns_early_when_plan_id_missing():
    """_mark_work_plan_failed 在 plan_id 缺失时应早退。"""
    job = _FakeJob({})
    await worker._mark_work_plan_failed(job, "error msg")


@pytest.mark.asyncio
async def test_process_workflow_run_job_raises_on_missing_run_id():
    """process_workflow_run_job 在 run_id 缺失时应抛 ValueError。"""
    job = _FakeJob({"workflow_id": "wf_1"})
    with pytest.raises(ValueError, match="missing run_id or workflow_id"):
        await worker.process_workflow_run_job(job)


@pytest.mark.asyncio
async def test_process_workflow_run_job_raises_on_missing_workflow_id():
    """process_workflow_run_job 在 workflow_id 缺失时应抛 ValueError。"""
    job = _FakeJob({"run_id": "r1"})
    with pytest.raises(ValueError, match="missing run_id or workflow_id"):
        await worker.process_workflow_run_job(job)


@pytest.mark.asyncio
async def test_process_work_plan_job_raises_on_missing_plan_id(monkeypatch):
    """process_work_plan_job 在 plan_id 缺失时应抛 WorkExecutionError。"""
    monkeypatch.setattr(worker, "pool", _AssertingPool())
    job = _FakeJob({})
    with pytest.raises(WorkExecutionError, match="missing plan_id"):
        await worker.process_work_plan_job(job)


@pytest.mark.asyncio
async def test_process_work_plan_job_rejects_non_list_source_ids(monkeypatch):
    """source_ids 为非 list 时应抛 WorkExecutionError。"""
    monkeypatch.setattr(worker, "pool", _AssertingPool())
    job = _FakeJob({"plan_id": "plan_1", "source_ids": "not-a-list"})
    with pytest.raises(WorkExecutionError, match="source_ids must be a list"):
        await worker.process_work_plan_job(job)


@pytest.mark.asyncio
async def test_process_work_plan_job_rejects_unsupported_execution_mode(monkeypatch):
    """execution_mode 不在白名单时应抛 WorkExecutionError。"""
    monkeypatch.setattr(worker, "pool", _AssertingPool())
    job = _FakeJob(
        {"plan_id": "plan_1", "source_ids": [], "execution_mode": "unsupported_mode"}
    )
    with pytest.raises(WorkExecutionError, match="execution mode is not supported"):
        await worker.process_work_plan_job(job)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["plan", "deep_research"])
async def test_process_work_plan_job_accepts_supported_execution_mode(monkeypatch, mode):
    """合法 execution_mode 应通过校验进入 DB 阶段。"""
    monkeypatch.setattr(worker, "pool", _AssertingPool())
    job = _FakeJob(
        {"plan_id": "plan_1", "source_ids": [], "execution_mode": mode}
    )
    # 通过校验后会访问 pool，_AssertingPool 会抛 AssertionError
    with pytest.raises(AssertionError, match="should reach pool"):
        await worker.process_work_plan_job(job)
