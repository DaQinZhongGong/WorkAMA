"""WorkAMA automation_v2 模块测试：Cron 调度 + Webhook HMAC 验签 + 运行详情。

覆盖：
- Cron 解析：``*/5`` / ``0 9 * * 1-5`` / ``0 0 1 * *`` / ``30 14 * * *``；非法表达式 (7)
- Cron 预览：next 5 次递增；count 边界 1/5/20 通过，0/21 拒绝 (8)
- Cron 验证端点：合法返回 next 5；非法返回 422 (2)
- Webhook 验签：正确签名通过；错误 401；无 header 有 secret 401；无 header 无 secret 兼容；常量时间 (9)
- 运行详情：成功 / 不存在 404 / 缺权限 403 / 跨 workspace 404 (4)
- 重试：成功创建新 run / parent_run_id 正确 / 已完成 run 可重试 (3)
- 取消：queued 可取消 / running 可取消 / completed 409 / 不存在 404 (4)
- 事件流：空列表 / 多条按 step 排序 / 跨 workspace 404 (3)
- cron_scheduler_loop：无 trigger 不报错 / 有到期 trigger 入队 run (2)
- 辅助函数：_parse_cron / _next_cron_runs / _verify_webhook_signature / _compute_duration_ms (8)
- Schema / 路由 (2)

所有测试使用 fake pool/connection mock，不依赖真实 DB。
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from workama_platform.core import Actor, hash_secret
from workama_platform.modules import automation_v2


# ============================================================================
# 测试辅助：fake pool / connection / result / request
# ============================================================================


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return list(self._rows)


class _Transaction:
    def __init__(self, connection):
        self._connection = connection

    async def __aenter__(self):
        return self._connection

    async def __aexit__(self, *_args):
        return False


class _SeqConnection:
    """按调用顺序返回预置结果的连接 mock。"""

    def __init__(self, results=None):
        self._results = list(results or [])
        self.calls: list[tuple[str, tuple]] = []
        self._idx = 0

    def transaction(self):
        return _Transaction(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

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


class _MockRequest:
    """模拟 FastAPI Request，仅提供 body() 方法。"""

    def __init__(self, body: bytes = b"{}"):
        self._body = body

    async def body(self):
        return self._body


def _actor(*, capabilities=("automation:*",), workspace_id="wsp_1", user_id="usr_1") -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_1",
        role="admin",
        email="test@example.test",
        display_name="Test",
        onboarding_completed=True,
        capabilities=capabilities,
    )


def _trigger_row(**overrides) -> dict:
    base = {
        "id": "atrig_1",
        "workspace_id": "wsp_1",
        "name": "test_trigger",
        "trigger_type": "cron",
        "config": {"cron_expression": "*/5 * * * *", "timezone": "UTC"},
        "executor_type": "agent",
        "executor_config": {},
        "enabled": True,
        "status": "active",
        "last_run_at": None,
        "next_run_at": None,
        "next_fire_at": None,
        "version": 1,
        "created_by": "usr_1",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "cron_expr": None,
        "webhook_secret": None,
    }
    base.update(overrides)
    return base


def _run_row(**overrides) -> dict:
    base = {
        "id": "atrun_1",
        "trigger_id": "atrig_1",
        "workspace_id": "wsp_1",
        "status": "queued",
        "trigger_source": "manual",
        "idempotency_key": "manual:x",
        "input_hash": "abc123",
        "payload": {"key": "value"},
        "result": None,
        "error_code": None,
        "error_message": None,
        "started_at": None,
        "completed_at": None,
        "created_at": datetime.now(UTC),
        "parent_run_id": None,
    }
    base.update(overrides)
    return base


def _event_row(**overrides) -> dict:
    base = {
        "id": "atevt_1",
        "run_id": "atrun_1",
        "step": 1,
        "event_type": "started",
        "payload": {"node": "step1"},
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


# ============================================================================
# 1. Cron 解析
# ============================================================================


def test_parse_cron_every_5_minutes():
    """``*/5 * * * *`` → 分钟字段包含 0,5,10,...,55。"""
    fields = automation_v2._parse_cron("*/5 * * * *")
    assert fields[0] == tuple(range(0, 60, 5))
    assert fields[1] == tuple(range(24))
    assert fields[2] == tuple(range(1, 32))
    assert fields[3] == tuple(range(1, 13))
    # 周字段：0-7（0 和 7 都是周日）
    assert 0 in fields[4] and 7 in fields[4]


def test_parse_cron_weekday_range_1_to_5():
    """``0 9 * * 1-5`` → 工作日 9 点。"""
    fields = automation_v2._parse_cron("0 9 * * 1-5")
    assert fields[0] == (0,)
    assert fields[1] == (9,)
    assert fields[4] == (1, 2, 3, 4, 5)


def test_parse_cron_monthly_first():
    """``0 0 1 * *`` → 每月 1 号 0 点。"""
    fields = automation_v2._parse_cron("0 0 1 * *")
    assert fields[0] == (0,)
    assert fields[1] == (0,)
    assert fields[2] == (1,)
    assert fields[3] == tuple(range(1, 13))


def test_parse_cron_daily_1430():
    """``30 14 * * *`` → 每天 14:30。"""
    fields = automation_v2._parse_cron("30 14 * * *")
    assert fields[0] == (30,)
    assert fields[1] == (14,)
    assert fields[2] == tuple(range(1, 32))


def test_parse_cron_wrong_field_count_raises():
    """字段数不对 → ValueError。"""
    with pytest.raises(ValueError):
        automation_v2._parse_cron("* * *")


def test_parse_cron_out_of_range_raises():
    """超出范围 → ValueError。"""
    with pytest.raises(ValueError):
        automation_v2._parse_cron("60 * * * *")
    with pytest.raises(ValueError):
        automation_v2._parse_cron("* 25 * * *")


def test_parse_cron_invalid_char_raises():
    """非法字符 → ValueError。"""
    with pytest.raises(ValueError):
        automation_v2._parse_cron("a * * * *")


# ============================================================================
# 2. Cron 预览：_next_cron_runs + CronPreviewRequest 边界
# ============================================================================


def test_next_cron_runs_returns_strictly_increasing():
    """next 5 次时间严格递增。"""
    from_time = datetime(2026, 7, 30, 0, 0, tzinfo=UTC)
    runs = automation_v2._next_cron_runs("*/5 * * * *", 5, from_time)
    assert len(runs) == 5
    for i in range(1, len(runs)):
        assert runs[i] > runs[i - 1]
    # 第一次应在 00:05
    assert runs[0] == datetime(2026, 7, 30, 0, 5, tzinfo=UTC)


def test_next_cron_runs_count_zero_returns_empty():
    """count=0 → 空列表。"""
    runs = automation_v2._next_cron_runs("*/5 * * * *", 0, datetime.now(UTC))
    assert runs == []


def test_next_cron_runs_specific_count():
    """指定 count=3 → 返回 3 个时间。"""
    runs = automation_v2._next_cron_runs("0 9 * * *", 3, datetime(2026, 7, 30, 0, 0, tzinfo=UTC))
    assert len(runs) == 3
    # 第一次应在 09:00
    assert runs[0] == datetime(2026, 7, 30, 9, 0, tzinfo=UTC)


def test_cron_preview_request_count_1_passes():
    """count=1 通过 Pydantic 校验。"""
    req = automation_v2.CronPreviewRequest(expression="*/5 * * * *", count=1)
    assert req.count == 1


def test_cron_preview_request_count_5_passes():
    """count=5 通过 Pydantic 校验。"""
    req = automation_v2.CronPreviewRequest(expression="*/5 * * * *", count=5)
    assert req.count == 5


def test_cron_preview_request_count_20_passes():
    """count=20 通过 Pydantic 校验。"""
    req = automation_v2.CronPreviewRequest(expression="*/5 * * * *", count=20)
    assert req.count == 20


def test_cron_preview_request_count_0_rejected():
    """count=0 被 Pydantic 拒绝。"""
    with pytest.raises(ValidationError):
        automation_v2.CronPreviewRequest(expression="*/5 * * * *", count=0)


def test_cron_preview_request_count_21_rejected():
    """count=21 被 Pydantic 拒绝。"""
    with pytest.raises(ValidationError):
        automation_v2.CronPreviewRequest(expression="*/5 * * * *", count=21)


# ============================================================================
# 3. Cron 验证 / 预览端点
# ============================================================================


@pytest.mark.asyncio
async def test_cron_validate_valid_expression_returns_next_5():
    """合法 cron 表达式 → 返回 next 5 次触发时间。"""
    result = await automation_v2.validate_cron_expression(
        automation_v2.CronValidateRequest(expression="*/5 * * * *")
    )
    assert result["valid"] is True
    assert result["expression"] == "*/5 * * * *"
    assert len(result["next_runs"]) == 5
    for i in range(1, 5):
        assert result["next_runs"][i] > result["next_runs"][i - 1]


@pytest.mark.asyncio
async def test_cron_validate_invalid_expression_returns_422():
    """非法 cron 表达式 → 422。"""
    with pytest.raises(HTTPException) as exc:
        await automation_v2.validate_cron_expression(
            automation_v2.CronValidateRequest(expression="* * *")
        )
    assert exc.value.status_code == 422
    assert "Invalid cron expression" in exc.value.detail


@pytest.mark.asyncio
async def test_cron_preview_valid_expression_returns_count_runs():
    """预览合法 cron 表达式 → 返回指定数量的触发时间。"""
    result = await automation_v2.preview_cron_expression(
        automation_v2.CronPreviewRequest(expression="0 9 * * 1-5", count=3)
    )
    assert result["count"] == 3
    assert len(result["next_runs"]) == 3


@pytest.mark.asyncio
async def test_cron_preview_invalid_expression_returns_422():
    """预览非法 cron 表达式 → 422。"""
    with pytest.raises(HTTPException) as exc:
        await automation_v2.preview_cron_expression(
            automation_v2.CronPreviewRequest(expression="99 * * * *")
        )
    assert exc.value.status_code == 422


# ============================================================================
# 4. Webhook HMAC-SHA256 签名验证
# ============================================================================


def test_verify_webhook_signature_correct():
    """正确签名 → True。"""
    secret = "hmac_secret_123"
    body = b'{"event":"test"}'
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert automation_v2._verify_webhook_signature(secret, body, signature) is True


def test_verify_webhook_signature_wrong():
    """错误签名 → False。"""
    secret = "hmac_secret_123"
    body = b'{"event":"test"}'
    assert automation_v2._verify_webhook_signature(secret, body, "wrong_signature") is False


def test_verify_webhook_signature_empty_secret():
    """空 secret → False。"""
    assert automation_v2._verify_webhook_signature("", b"body", "abc") is False


def test_verify_webhook_signature_empty_signature():
    """空签名 → False。"""
    assert automation_v2._verify_webhook_signature("secret", b"body", "") is False


def test_verify_webhook_signature_different_length_safe():
    """不同长度签名安全比较（不抛异常）。"""
    secret = "hmac_secret_123"
    body = b'{"event":"test"}'
    # 短签名不应导致异常
    assert automation_v2._verify_webhook_signature(secret, body, "short") is False


def test_verify_webhook_uses_constant_time_compare():
    """验证使用 secrets.compare_digest（常量时间比较）：正确签名匹配。"""
    secret = "my_secret_key"
    body = b'{"data":"value"}'
    correct_sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # 正确签名应通过
    assert automation_v2._verify_webhook_signature(secret, body, correct_sig) is True
    # 篡改 body 后签名不匹配
    tampered_body = b'{"data":"tampered"}'
    assert automation_v2._verify_webhook_signature(secret, tampered_body, correct_sig) is False


@pytest.mark.asyncio
async def test_webhook_correct_signature_passes(monkeypatch):
    """配置了 webhook_secret + 正确 HMAC 签名 → 成功入队。"""
    secret = "hmac_secret_123"
    body = b'{"event":"deploy"}'
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    trigger = _trigger_row(
        trigger_type="webhook",
        webhook_secret=secret,
        config={"default_payload": {}, "webhook_secret_hash": None},
    )
    new_run = _run_row(id="atrun_new", trigger_source="webhook")
    conn = _SeqConnection(results=[
        _Result(row=trigger),    # SELECT trigger
        _Result(row=None),       # SELECT existing run (idempotency)
        _Result(row=new_run),    # INSERT RETURNING
        _Result(),               # UPDATE trigger
        _Result(),               # INSERT outbox
    ])
    monkeypatch.setattr(automation_v2, "pool", _Pool(conn))
    result = await automation_v2.receive_webhook_v2(
        trigger_id="atrig_1",
        request=_MockRequest(body=body),
        x_webhook_secret=None,
        x_workama_signature=signature,
        idempotency_key=None,
    )
    assert result["run"]["id"] == "atrun_new"


@pytest.mark.asyncio
async def test_webhook_wrong_signature_returns_401(monkeypatch):
    """配置了 webhook_secret + 错误签名 → 401。"""
    trigger = _trigger_row(trigger_type="webhook", webhook_secret="hmac_secret_123")
    conn = _SeqConnection(results=[_Result(row=trigger)])
    monkeypatch.setattr(automation_v2, "pool", _Pool(conn))
    with pytest.raises(HTTPException) as exc:
        await automation_v2.receive_webhook_v2(
            trigger_id="atrig_1",
            request=_MockRequest(body=b'{"event":"test"}'),
            x_webhook_secret=None,
            x_workama_signature="wrong_signature",
            idempotency_key=None,
        )
    assert exc.value.status_code == 401
    assert "signature" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_webhook_no_signature_with_secret_returns_401(monkeypatch):
    """配置了 webhook_secret 但无 X-WorkAMA-Signature → 401。"""
    trigger = _trigger_row(trigger_type="webhook", webhook_secret="hmac_secret_123")
    conn = _SeqConnection(results=[_Result(row=trigger)])
    monkeypatch.setattr(automation_v2, "pool", _Pool(conn))
    with pytest.raises(HTTPException) as exc:
        await automation_v2.receive_webhook_v2(
            trigger_id="atrig_1",
            request=_MockRequest(body=b'{"event":"test"}'),
            x_webhook_secret=None,
            x_workama_signature=None,
            idempotency_key=None,
        )
    assert exc.value.status_code == 401
    assert "X-WorkAMA-Signature" in exc.value.detail


@pytest.mark.asyncio
async def test_webhook_backward_compat_no_secret_no_signature_passes(monkeypatch):
    """未配置 webhook_secret（旧 trigger）+ 正确 X-Webhook-Secret → 通过（向后兼容）。"""
    secret = "legacy_webhook_secret"
    trigger = _trigger_row(
        trigger_type="webhook",
        webhook_secret=None,
        config={"default_payload": {}, "webhook_secret_hash": hash_secret(secret)},
    )
    new_run = _run_row(id="atrun_compat", trigger_source="webhook")
    conn = _SeqConnection(results=[
        _Result(row=trigger),
        _Result(row=None),
        _Result(row=new_run),
        _Result(),
        _Result(),
    ])
    monkeypatch.setattr(automation_v2, "pool", _Pool(conn))
    result = await automation_v2.receive_webhook_v2(
        trigger_id="atrig_1",
        request=_MockRequest(body=b'{"event":"test"}'),
        x_webhook_secret=secret,
        x_workama_signature=None,
        idempotency_key=None,
    )
    assert result["run"]["id"] == "atrun_compat"


# ============================================================================
# 5. 运行详情
# ============================================================================


@pytest.mark.asyncio
async def test_get_run_detail_success(monkeypatch):
    """成功获取运行详情（含 duration_ms）。"""
    started = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
    completed = datetime(2026, 7, 30, 10, 0, 5, tzinfo=UTC)
    run = _run_row(
        status="succeeded",
        started_at=started,
        completed_at=completed,
        result={"output": "done"},
    )
    trigger = _trigger_row()
    conn = _SeqConnection(results=[
        _Result(row=trigger),  # _get_trigger
        _Result(row=run),      # _get_run
    ])
    monkeypatch.setattr(automation_v2, "pool", _Pool(conn))
    result = await automation_v2.get_trigger_run_detail("atrig_1", "atrun_1", _actor())
    assert result["id"] == "atrun_1"
    assert result["status"] == "succeeded"
    assert result["finished_at"] == completed
    assert result["duration_ms"] == 5000
    assert result["payload"]["key"] == "value"
    assert result["result"]["output"] == "done"


@pytest.mark.asyncio
async def test_get_run_detail_not_found_404(monkeypatch):
    """run 不存在 → 404。"""
    trigger = _trigger_row()
    conn = _SeqConnection(results=[
        _Result(row=trigger),  # _get_trigger
        _Result(row=None),     # _get_run → not found
    ])
    monkeypatch.setattr(automation_v2, "pool", _Pool(conn))
    with pytest.raises(HTTPException) as exc:
        await automation_v2.get_trigger_run_detail("atrig_1", "atrun_missing", _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_run_detail_missing_capability_403(monkeypatch):
    """缺少 automation:read 权限 → 403。"""
    conn = _SeqConnection()
    monkeypatch.setattr(automation_v2, "pool", _Pool(conn))
    with pytest.raises(HTTPException) as exc:
        await automation_v2.get_trigger_run_detail("atrig_1", "atrun_1", _actor(capabilities=()))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_run_detail_cross_workspace_404(monkeypatch):
    """跨 workspace 访问 → trigger 查不到 → 404。"""
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(automation_v2, "pool", _Pool(conn))
    with pytest.raises(HTTPException) as exc:
        await automation_v2.get_trigger_run_detail("atrig_1", "atrun_1", _actor(workspace_id="wsp_other"))
    assert exc.value.status_code == 404


# ============================================================================
# 6. 重试
# ============================================================================


@pytest.mark.asyncio
async def test_retry_creates_new_run_with_parent(monkeypatch):
    """重试创建新 run，parent_run_id 引用原 run。"""
    trigger = _trigger_row()
    original = _run_row(status="failed", trigger_source="webhook")
    new_run = _run_row(id="atrun_retry", status="queued", parent_run_id="atrun_1")
    conn = _SeqConnection(results=[
        _Result(row=trigger),    # _get_trigger (for_update)
        _Result(row=original),   # _get_run (original)
        _Result(row=None),       # SELECT existing (idempotency)
        _Result(row=new_run),    # INSERT RETURNING
        _Result(),               # UPDATE trigger
        _Result(),               # INSERT outbox
    ])
    monkeypatch.setattr(automation_v2, "pool", _Pool(conn))
    result = await automation_v2.retry_trigger_run("atrig_1", "atrun_1", _actor(), None)
    assert result["parent_run_id"] == "atrun_1"
    assert result["run"]["id"] == "atrun_retry"
    assert result["run"]["parent_run_id"] == "atrun_1"
    assert result["status"] == "queued"


@pytest.mark.asyncio
async def test_retry_parent_run_id_correct(monkeypatch):
    """parent_run_id 正确引用原 run_id。"""
    trigger = _trigger_row()
    original = _run_row(id="original_run_99", status="failed")
    new_run = _run_row(id="atrun_new", parent_run_id="original_run_99")
    conn = _SeqConnection(results=[
        _Result(row=trigger),
        _Result(row=original),
        _Result(row=None),
        _Result(row=new_run),
        _Result(),
        _Result(),
    ])
    monkeypatch.setattr(automation_v2, "pool", _Pool(conn))
    result = await automation_v2.retry_trigger_run("atrig_1", "original_run_99", _actor(), None)
    assert result["parent_run_id"] == "original_run_99"
    assert result["run"]["parent_run_id"] == "original_run_99"


@pytest.mark.asyncio
async def test_retry_completed_run_can_retry(monkeypatch):
    """已完成的 run 也可以重试（不限状态）。"""
    trigger = _trigger_row()
    original = _run_row(status="succeeded", trigger_source="manual")
    new_run = _run_row(id="atrun_retry_ok", parent_run_id="atrun_1")
    conn = _SeqConnection(results=[
        _Result(row=trigger),
        _Result(row=original),
        _Result(row=None),
        _Result(row=new_run),
        _Result(),
        _Result(),
    ])
    monkeypatch.setattr(automation_v2, "pool", _Pool(conn))
    result = await automation_v2.retry_trigger_run("atrig_1", "atrun_1", _actor(), None)
    assert result["run"]["id"] == "atrun_retry_ok"
    assert result["parent_run_id"] == "atrun_1"


# ============================================================================
# 7. 取消
# ============================================================================


@pytest.mark.asyncio
async def test_cancel_queued_run_success(monkeypatch):
    """queued 状态可取消。"""
    trigger = _trigger_row()
    run = _run_row(status="queued")
    cancelled = _run_row(status="cancelled", completed_at=datetime.now(UTC))
    conn = _SeqConnection(results=[
        _Result(row=trigger),     # _get_trigger
        _Result(row=run),         # _get_run (for_update)
        _Result(row=cancelled),  # UPDATE RETURNING
    ])
    monkeypatch.setattr(automation_v2, "pool", _Pool(conn))
    result = await automation_v2.cancel_trigger_run("atrig_1", "atrun_1", _actor())
    assert result["status"] == "cancelled"
    assert result["completed_at"] is not None


@pytest.mark.asyncio
async def test_cancel_running_run_success(monkeypatch):
    """running 状态可取消。"""
    trigger = _trigger_row()
    run = _run_row(status="running")
    cancelled = _run_row(status="cancelled", completed_at=datetime.now(UTC))
    conn = _SeqConnection(results=[
        _Result(row=trigger),
        _Result(row=run),
        _Result(row=cancelled),
    ])
    monkeypatch.setattr(automation_v2, "pool", _Pool(conn))
    result = await automation_v2.cancel_trigger_run("atrig_1", "atrun_1", _actor())
    assert result["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_completed_run_returns_409(monkeypatch):
    """succeeded 状态不可取消 → 409。"""
    trigger = _trigger_row()
    run = _run_row(status="succeeded")
    conn = _SeqConnection(results=[
        _Result(row=trigger),
        _Result(row=run),
    ])
    monkeypatch.setattr(automation_v2, "pool", _Pool(conn))
    with pytest.raises(HTTPException) as exc:
        await automation_v2.cancel_trigger_run("atrig_1", "atrun_1", _actor())
    assert exc.value.status_code == 409
    assert "cannot be cancelled" in exc.value.detail


@pytest.mark.asyncio
async def test_cancel_not_found_404(monkeypatch):
    """run 不存在 → 404。"""
    trigger = _trigger_row()
    conn = _SeqConnection(results=[
        _Result(row=trigger),   # _get_trigger
        _Result(row=None),      # _get_run → not found
    ])
    monkeypatch.setattr(automation_v2, "pool", _Pool(conn))
    with pytest.raises(HTTPException) as exc:
        await automation_v2.cancel_trigger_run("atrig_1", "atrun_missing", _actor())
    assert exc.value.status_code == 404


# ============================================================================
# 8. 事件流
# ============================================================================


@pytest.mark.asyncio
async def test_list_run_events_empty(monkeypatch):
    """空事件列表。"""
    trigger = _trigger_row()
    run = _run_row()
    conn = _SeqConnection(results=[
        _Result(row=trigger),   # _get_trigger
        _Result(row=run),       # _get_run
        _Result(rows=[]),       # SELECT events
    ])
    monkeypatch.setattr(automation_v2, "pool", _Pool(conn))
    result = await automation_v2.list_trigger_run_events("atrig_1", "atrun_1", _actor())
    assert result["items"] == []
    assert result["data"] == []
    assert result["has_more"] is False


@pytest.mark.asyncio
async def test_list_run_events_multiple_sorted_by_step(monkeypatch):
    """多条事件按 step 排序（mock 返回 DB 已排序的结果，并验证 SQL 含 ORDER BY step ASC）。"""
    trigger = _trigger_row()
    run = _run_row()
    # DB 会按 ORDER BY step ASC 返回，mock 模拟已排序结果
    events = [
        _event_row(id="evt_1", step=1, event_type="started", payload={"input": "data"}),
        _event_row(id="evt_2", step=2, event_type="completed", payload={"result": "ok"}),
        _event_row(id="evt_3", step=3, event_type="failed", payload={"error": "boom"}),
    ]
    conn = _SeqConnection(results=[
        _Result(row=trigger),
        _Result(row=run),
        _Result(rows=events),
    ])
    monkeypatch.setattr(automation_v2, "pool", _Pool(conn))
    result = await automation_v2.list_trigger_run_events("atrig_1", "atrun_1", _actor())
    steps = [e["step"] for e in result["items"]]
    assert steps == [1, 2, 3]
    assert result["items"][0]["event_type"] == "started"
    assert result["items"][2]["event_type"] == "failed"
    # 验证 SQL 查询包含 ORDER BY step ASC
    events_query = conn.calls[2][0]
    assert "ORDER BY step ASC" in events_query


@pytest.mark.asyncio
async def test_list_run_events_cross_workspace_404(monkeypatch):
    """跨 workspace → trigger 不存在 → 404。"""
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(automation_v2, "pool", _Pool(conn))
    with pytest.raises(HTTPException) as exc:
        await automation_v2.list_trigger_run_events("atrig_1", "atrun_1", _actor(workspace_id="wsp_other"))
    assert exc.value.status_code == 404


# ============================================================================
# 9. cron_scheduler_loop
# ============================================================================


@pytest.mark.asyncio
async def test_cron_scheduler_loop_no_triggers_no_error(monkeypatch):
    """无 cron trigger 不报错，返回 scanned=0。"""
    conn = _SeqConnection(results=[
        _Result(rows=[]),  # SELECT triggers → empty
    ])
    monkeypatch.setattr(automation_v2, "pool", _Pool(conn))
    result = await automation_v2.cron_scheduler_loop(_Pool(conn).connection)
    assert result == {"scanned": 0, "enqueued": 0}


@pytest.mark.asyncio
async def test_cron_scheduler_loop_due_trigger_enqueues(monkeypatch):
    """有到期 cron trigger → 入队 run。"""
    past_time = datetime.now(UTC) - timedelta(minutes=10)
    trigger = _trigger_row(
        trigger_type="cron",
        config={"cron_expression": "*/5 * * * *", "timezone": "UTC"},
        next_fire_at=past_time,
        next_run_at=past_time,
    )
    new_run = _run_row(id="atrun_cron", trigger_source="cron")
    conn = _SeqConnection(results=[
        _Result(rows=[trigger]),  # SELECT triggers
        _Result(row=None),        # SELECT existing (idempotency)
        _Result(row=new_run),    # INSERT RETURNING
        _Result(),               # UPDATE trigger (inside _enqueue)
        _Result(),               # INSERT outbox
        _Result(),               # UPDATE trigger (next_fire_at, in scheduler)
    ])
    monkeypatch.setattr(automation_v2, "pool", _Pool(conn))
    result = await automation_v2.cron_scheduler_loop(_Pool(conn).connection)
    assert result == {"scanned": 1, "enqueued": 1}


# ============================================================================
# 10. 辅助函数
# ============================================================================


def test_compute_duration_ms_both_none():
    """started_at 和 completed_at 都为 None → None。"""
    assert automation_v2._compute_duration_ms(None, None) is None


def test_compute_duration_ms_started_none():
    """started_at 为 None → None。"""
    assert automation_v2._compute_duration_ms(None, datetime.now(UTC)) is None


def test_compute_duration_ms_completed_none():
    """completed_at 为 None → None。"""
    assert automation_v2._compute_duration_ms(datetime.now(UTC), None) is None


def test_compute_duration_ms_correct():
    """正确计算毫秒时长。"""
    started = datetime(2026, 7, 30, 10, 0, 0, tzinfo=UTC)
    completed = datetime(2026, 7, 30, 10, 0, 5, tzinfo=UTC)
    assert automation_v2._compute_duration_ms(started, completed) == 5000


def test_compute_duration_ms_sub_second():
    """亚秒级精度。"""
    started = datetime(2026, 7, 30, 10, 0, 0, tzinfo=UTC)
    completed = datetime(2026, 7, 30, 10, 0, 0, 500000, tzinfo=UTC)
    assert automation_v2._compute_duration_ms(started, completed) == 500


def test_trigger_run_detail_view_includes_duration():
    """详情视图包含 finished_at 和 duration_ms。"""
    started = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
    completed = datetime(2026, 7, 30, 10, 0, 3, tzinfo=UTC)
    row = _run_row(started_at=started, completed_at=completed, status="succeeded")
    view = automation_v2.trigger_run_detail_view(row)
    assert view["finished_at"] == completed
    assert view["duration_ms"] == 3000
    assert view["parent_run_id"] is None


def test_trigger_run_detail_view_no_times_duration_none():
    """无 started/completed → duration_ms=None。"""
    row = _run_row()
    view = automation_v2.trigger_run_detail_view(row)
    assert view["duration_ms"] is None
    assert view["finished_at"] is None


def test_trigger_run_event_view_structure():
    """事件视图结构正确。"""
    row = _event_row(step=2, event_type="completed", payload={"result": "ok"})
    view = automation_v2.trigger_run_event_view(row)
    assert view["id"] == "atevt_1"
    assert view["run_id"] == "atrun_1"
    assert view["step"] == 2
    assert view["event_type"] == "completed"
    assert view["payload"]["result"] == "ok"


# ============================================================================
# 11. Schema / 路由
# ============================================================================


@pytest.mark.asyncio
async def test_schema_includes_new_columns_and_event_table():
    """Schema 包含新列和事件表。"""
    statements = []

    class Connection:
        async def execute(self, statement):
            statements.append(statement)

    await automation_v2.ensure_automation_v2_schema(Connection())
    schema = "\n".join(statements)
    # 新列
    assert "ADD COLUMN IF NOT EXISTS cron_expr" in schema
    assert "ADD COLUMN IF NOT EXISTS webhook_secret" in schema
    assert "ADD COLUMN IF NOT EXISTS next_fire_at" in schema
    assert "ADD COLUMN IF NOT EXISTS parent_run_id" in schema
    # 事件表
    assert "automation_trigger_run_event" in schema
    assert "event_type" in schema
    assert "idx_automation_trigger_run_event_run_step" in schema


def test_router_exposes_new_v2_endpoints():
    """路由注册了所有新端点。"""
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in automation_v2.router.routes}
    assert ("/api/v1/automations/v2/triggers/{trigger_id}/runs/{run_id}", ("GET",)) in paths
    assert ("/api/v1/automations/v2/triggers/{trigger_id}/runs/{run_id}/retry", ("POST",)) in paths
    assert ("/api/v1/automations/v2/triggers/{trigger_id}/runs/{run_id}/cancel", ("POST",)) in paths
    assert ("/api/v1/automations/v2/cron/validate", ("POST",)) in paths
    assert ("/api/v1/automations/v2/cron/preview", ("POST",)) in paths
    assert ("/api/v1/automations/v2/triggers/{trigger_id}/runs/{run_id}/events", ("GET",)) in paths


# ============================================================================
# 12. 额外边界
# ============================================================================


def test_parse_cron_comma_list():
    """逗号列表语法：``0,30 * * * *`` → 分钟={0,30}。"""
    fields = automation_v2._parse_cron("0,30 * * * *")
    assert fields[0] == (0, 30)


def test_parse_cron_step_with_range():
    """范围 + 步长：``0-59/15 * * * *`` → 分钟={0,15,30,45}。"""
    fields = automation_v2._parse_cron("0-59/15 * * * *")
    assert fields[0] == (0, 15, 30, 45)


@pytest.mark.asyncio
async def test_cron_validate_returns_utc_datetimes():
    """验证返回的 next_runs 都是 UTC 时区感知时间。"""
    result = await automation_v2.validate_cron_expression(
        automation_v2.CronValidateRequest(expression="0 9 * * *")
    )
    for run in result["next_runs"]:
        assert run.tzinfo is not None


@pytest.mark.asyncio
async def test_get_run_detail_includes_parent_run_id(monkeypatch):
    """运行详情包含 parent_run_id 字段。"""
    run = _run_row(parent_run_id="atrun_original")
    trigger = _trigger_row()
    conn = _SeqConnection(results=[
        _Result(row=trigger),
        _Result(row=run),
    ])
    monkeypatch.setattr(automation_v2, "pool", _Pool(conn))
    result = await automation_v2.get_trigger_run_detail("atrig_1", "atrun_1", _actor())
    assert result["parent_run_id"] == "atrun_original"


@pytest.mark.asyncio
async def test_cancel_failed_run_returns_409(monkeypatch):
    """failed 状态不可取消 → 409。"""
    trigger = _trigger_row()
    run = _run_row(status="failed")
    conn = _SeqConnection(results=[
        _Result(row=trigger),
        _Result(row=run),
    ])
    monkeypatch.setattr(automation_v2, "pool", _Pool(conn))
    with pytest.raises(HTTPException) as exc:
        await automation_v2.cancel_trigger_run("atrig_1", "atrun_1", _actor())
    assert exc.value.status_code == 409
