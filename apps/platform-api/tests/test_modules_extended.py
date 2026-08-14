"""platform-api 各模块纯函数扩展单元测试。

覆盖 notification/delivery、billing/grants、billing/reporting、billing/reservations、
privacy/processor(及 privacy/service)、audit_exports、open_platform 中的纯函数与
可 mock 函数：数据校验、辅助函数、重试/延迟计算、幂等性、状态转换、边界情况。

所有外部依赖（pool/redis/SMTP/HTTP）均通过 monkeypatch 或 fake 类隔离，
不依赖真实 DB/Redis/网络。测试风格参考 test_audit_exports.py 与 test_worker_extended.py。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import smtplib
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from workama_platform.modules import audit_exports, open_platform
from workama_platform.modules.billing.grants import GRANT_QUANTUM, month_period, quantize_credits
from workama_platform.modules.billing.reporting import (
    ReconciliationResult,
    hour_bucket,
    reconcile_totals,
)
from workama_platform.modules.billing.reservations import (
    ReservationResult,
    ReservationState,
    estimate_cost,
    settle_reservation_amounts,
)
from workama_platform.modules.notification.delivery import (
    classify_delivery_error,
    send_email,
    send_webhook_mock,
)
from workama_platform.modules.notification.service import (
    FORCED_IN_APP_PREFIXES,
    NOTIFICATION_CHANNELS,
    RETRY_DELAYS_SECONDS,
    is_forced_in_app,
    low_balance_dedupe_key,
    preference_change_allowed,
    retry_delay_seconds,
    should_notify_low_balance,
)
from workama_platform.modules.privacy.service import (
    build_export_manifest,
    deletion_steps,
    infer_processing_activity,
    transition_allowed,
)


# ----------------------------------------------------------------------
# notification/delivery.py 纯函数
# ----------------------------------------------------------------------


def test_classify_delivery_error_transient_for_oserror():
    """OSError 归类为瞬时提供方错误。"""
    assert classify_delivery_error(OSError("connection reset")) == "transient_provider_error"


def test_classify_delivery_error_transient_for_smtp_exception():
    """SMTPException 归类为瞬时提供方错误。"""
    assert classify_delivery_error(smtplib.SMTPException("554 bounce")) == "transient_provider_error"


def test_classify_delivery_error_transient_for_asyncio_timeout():
    """asyncio.TimeoutError 归类为瞬时提供方错误。"""
    assert classify_delivery_error(asyncio.TimeoutError()) == "transient_provider_error"


def test_classify_delivery_error_returns_message_for_generic_exception():
    """普通异常返回其字符串消息。"""
    assert classify_delivery_error(ValueError("bad address")) == "bad address"


def test_classify_delivery_error_truncates_long_messages_to_120_chars():
    """超长消息截断为 120 字符。"""
    long_msg = "x" * 500
    result = classify_delivery_error(ValueError(long_msg))
    assert len(result) == 120
    assert result == long_msg[:120]


def test_classify_delivery_error_returns_class_name_for_empty_message():
    """空消息异常返回类名作为兜底。"""
    assert classify_delivery_error(RuntimeError()) == "RuntimeError"


def test_send_email_mock_is_deterministic_and_prefixed():
    """mock=True 返回 mock-email: 前缀且幂等。"""
    first = send_email("user@example.com", "标题", "摘要", mock=True)
    second = send_email("user@example.com", "标题", "摘要", mock=True)
    assert first == second
    assert first.startswith("mock-email:")
    assert len(first) == len("mock-email:") + 24


def test_send_email_mock_differs_for_different_recipients():
    """不同收件人产生不同 provider id。"""
    a = send_email("a@example.com", "T", "S", mock=True)
    b = send_email("b@example.com", "T", "S", mock=True)
    assert a != b


def test_send_email_mock_differs_for_different_title():
    """不同标题产生不同 provider id。"""
    a = send_email("u@example.com", "T1", "S", mock=True)
    b = send_email("u@example.com", "T2", "S", mock=True)
    assert a != b


def test_send_email_without_smtp_config_raises_runtime_error():
    """未配置 SMTP 时非 mock 模式抛出 RuntimeError。"""
    with pytest.raises(RuntimeError, match="smtp_not_configured"):
        send_email("u@example.com", "T", "S", mock=False)


def test_send_webhook_mock_rejects_non_mock_url():
    """非 mock:// 开头的 URL 抛出 ValueError。"""
    with pytest.raises(ValueError, match="mock_webhook_url_required"):
        send_webhook_mock("https://example.com/hook", "secret", {}, "key-1")


def test_send_webhook_mock_signature_format_is_hmac_sha256():
    """签名格式为 t=<timestamp>,v1=<hex>。"""
    ts = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    result = send_webhook_mock("mock://wh", "secret", {"a": 1}, "key-1", occurred_at=ts)
    sig = result["signature"]
    assert sig.startswith(f"t={int(ts.timestamp())},v1=")
    # 验证签名确实为 HMAC-SHA256
    raw_body = result["body"]
    expected = hmac.new(b"secret", f"{int(ts.timestamp())}.{raw_body}".encode(), hashlib.sha256).hexdigest()
    assert sig == f"t={int(ts.timestamp())},v1={expected}"


def test_send_webhook_mock_uses_sort_keys_for_body():
    """body 使用 sort_keys 保证确定性。"""
    ts = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    r1 = send_webhook_mock("mock://wh", "s", {"b": 2, "a": 1}, "k", occurred_at=ts)
    r2 = send_webhook_mock("mock://wh", "s", {"a": 1, "b": 2}, "k", occurred_at=ts)
    assert r1["body"] == r2["body"]
    assert r1["signature"] == r2["signature"]


def test_send_webhook_mock_provider_id_is_idempotent():
    """相同 url + idempotency_key 产生相同 provider_id。"""
    ts = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    a = send_webhook_mock("mock://wh", "s", {"x": 1}, "key-1", occurred_at=ts)
    b = send_webhook_mock("mock://wh", "s", {"x": 1}, "key-1", occurred_at=ts)
    assert a["provider_id"] == b["provider_id"]
    assert a["provider_id"].startswith("mock-webhook:")


def test_send_webhook_mock_status_code_is_202():
    """mock webhook 始终返回 202。"""
    result = send_webhook_mock("mock://wh", "s", {}, "k")
    assert result["status_code"] == 202


# ----------------------------------------------------------------------
# notification/service.py 纯函数
# ----------------------------------------------------------------------


def test_should_notify_low_balance_true_when_below_threshold():
    """余额低于阈值时触发通知。"""
    assert should_notify_low_balance(Decimal("50"), Decimal("100")) is True


def test_should_notify_low_balance_false_when_at_or_above_threshold():
    """余额大于等于阈值时不触发通知。"""
    assert should_notify_low_balance(Decimal("100"), Decimal("100")) is False
    assert should_notify_low_balance(Decimal("101"), Decimal("100")) is False


def test_low_balance_dedupe_key_includes_workspace_and_date():
    """去重键包含 workspace_id 和 UTC 日期。"""
    ts = datetime(2026, 7, 24, 23, 30, tzinfo=UTC)
    key = low_balance_dedupe_key("ws_1", ts)
    assert key == "billing.low_balance:ws_1:2026-07-24"


def test_low_balance_dedupe_key_converts_non_utc_timezone():
    """非 UTC 时区转换为 UTC 日期。"""
    # 北京时间 2026-07-25 02:00 = UTC 2026-07-24 18:00
    ts = datetime(2026, 7, 25, 2, 0, tzinfo=timezone(timedelta(hours=8)))
    key = low_balance_dedupe_key("ws_1", ts)
    assert key == "billing.low_balance:ws_1:2026-07-24"


def test_is_forced_in_app_only_for_forced_prefixes_on_in_app_channel():
    """仅 in_app 渠道且事件类型为强制前缀时返回 True。"""
    for prefix in FORCED_IN_APP_PREFIXES:
        assert is_forced_in_app(prefix + ".event", "in_app") is True
    # 非 in_app 渠道不强制
    assert is_forced_in_app("security.login", "email") is False
    # 非强制前缀不强制
    assert is_forced_in_app("agent.completed", "in_app") is False


def test_preference_change_allowed_blocks_disabling_forced_in_app():
    """禁止关闭强制 in_app 通知。"""
    # 关闭安全类 in_app 通知：不允许
    assert preference_change_allowed("security.login", "in_app", enabled=False) is False
    # 启用：允许
    assert preference_change_allowed("security.login", "in_app", enabled=True) is True
    # 关闭非强制 in_app 通知：允许
    assert preference_change_allowed("agent.completed", "in_app", enabled=False) is True
    # 关闭安全类 email 通知：允许
    assert preference_change_allowed("security.login", "email", enabled=False) is True


def test_retry_delay_seconds_returns_bounded_backoff_sequence():
    """1-based 重试延迟序列为 60/300/1800/7200/43200。"""
    assert [retry_delay_seconds(n) for n in range(1, 6)] == [60, 300, 1800, 7200, 43200]


def test_retry_delay_seconds_caps_at_max_for_large_attempt():
    """超大 attempt 封顶为 43200。"""
    assert retry_delay_seconds(99) == 43200
    assert retry_delay_seconds(1000) == 43200


def test_retry_delay_seconds_handles_zero_and_negative():
    """0 和负数 attempt 映射到第一个延迟。"""
    assert retry_delay_seconds(0) == 60
    assert retry_delay_seconds(-5) == 60


def test_notification_channels_is_frozenset_with_three_channels():
    """NOTIFICATION_CHANNELS 为包含三种渠道的 frozenset。"""
    assert NOTIFICATION_CHANNELS == frozenset({"in_app", "email", "webhook"})


def test_retry_delays_seconds_constant_has_five_entries():
    """RETRY_DELAYS_SECONDS 常量有 5 个条目。"""
    assert RETRY_DELAYS_SECONDS == (60, 300, 1800, 7200, 43200)
    assert len(RETRY_DELAYS_SECONDS) == 5


# ----------------------------------------------------------------------
# billing/grants.py 纯函数
# ----------------------------------------------------------------------


def test_grant_quantum_is_six_decimal_places():
    """GRANT_QUANTUM 为 0.000001。"""
    assert GRANT_QUANTUM == Decimal("0.000001")


def test_month_period_returns_first_and_next_month_start():
    """月中日期返回当月1号到下月1号。"""
    start, end = month_period(datetime(2026, 7, 15, 10, 30, tzinfo=UTC))
    assert start == datetime(2026, 7, 1, tzinfo=UTC)
    assert end == datetime(2026, 8, 1, tzinfo=UTC)


def test_month_period_rolls_year_boundary_in_december():
    """12 月跨年：start 为 12/1，end 为次年 1/1。"""
    start, end = month_period(datetime(2026, 12, 31, 23, 59, tzinfo=UTC))
    assert start == datetime(2026, 12, 1, tzinfo=UTC)
    assert end == datetime(2027, 1, 1, tzinfo=UTC)


def test_month_period_handles_january_input():
    """1 月输入返回 2 月 1 号作为 end。"""
    start, end = month_period(datetime(2026, 1, 10, tzinfo=UTC))
    assert start == datetime(2026, 1, 1, tzinfo=UTC)
    assert end == datetime(2026, 2, 1, tzinfo=UTC)


def test_month_period_converts_non_utc_timezone_to_utc():
    """非 UTC 时区先转换为 UTC 再计算。"""
    # 北京时间 2026-07-25 02:00 = UTC 2026-07-24 18:00，属于 7 月
    ts = datetime(2026, 7, 25, 2, 0, tzinfo=timezone(timedelta(hours=8)))
    start, end = month_period(ts)
    assert start == datetime(2026, 7, 1, tzinfo=UTC)
    assert end == datetime(2026, 8, 1, tzinfo=UTC)


def test_month_period_first_day_returns_same_month():
    """月第一天返回当月1号到下月1号。"""
    start, end = month_period(datetime(2026, 3, 1, 0, 0, tzinfo=UTC))
    assert start == datetime(2026, 3, 1, tzinfo=UTC)
    assert end == datetime(2026, 4, 1, tzinfo=UTC)


def test_quantize_credits_truncates_to_six_decimals():
    """超过 6 位小数被量化到 6 位。"""
    assert quantize_credits("12.3456789") == Decimal("12.345679")


def test_quantize_credits_handles_int_input():
    """整数输入被量化。"""
    assert quantize_credits(100) == Decimal("100.000000")


def test_quantize_credits_handles_decimal_input():
    """Decimal 输入被量化。"""
    assert quantize_credits(Decimal("0.5")) == Decimal("0.500000")


def test_quantize_credits_handles_zero():
    """0 被量化为 0.000000。"""
    assert quantize_credits(0) == Decimal("0.000000")


def test_quantize_credits_handles_negative_value():
    """负数被量化。"""
    assert quantize_credits("-3.2") == Decimal("-3.200000")


# ----------------------------------------------------------------------
# billing/reporting.py 纯函数
# ----------------------------------------------------------------------


def test_hour_bucket_strips_minutes_seconds_microseconds():
    """hour_bucket 去掉分/秒/微秒。"""
    value = datetime(2026, 7, 14, 8, 35, 42, 999, tzinfo=UTC)
    assert hour_bucket(value) == datetime(2026, 7, 14, 8, 0, 0, tzinfo=UTC)


def test_hour_bucket_converts_non_utc_timezone():
    """非 UTC 时区先转换为 UTC 再取整。"""
    # 北京时间 2026-07-25 10:30 = UTC 2026-07-25 02:30 -> 02:00
    value = datetime(2026, 7, 25, 10, 30, tzinfo=timezone(timedelta(hours=8)))
    assert hour_bucket(value) == datetime(2026, 7, 25, 2, 0, 0, tzinfo=UTC)


def test_reconcile_totals_passes_for_exact_match():
    """完全一致时状态为 passed。"""
    result = reconcile_totals(Decimal("100.000000"), Decimal("100.000000"))
    assert result.status == "passed"
    assert result.difference == Decimal("0.000000")
    assert result.difference_ratio == Decimal("0.000000")


def test_reconcile_totals_passes_within_point_one_percent():
    """差异在 0.1% 以内为 passed。"""
    result = reconcile_totals(Decimal("100.000000"), Decimal("99.950000"))
    assert result.status == "passed"
    assert result.difference == Decimal("0.050000")


def test_reconcile_totals_flags_material_difference():
    """差异超过 0.1% 为 mismatch。"""
    result = reconcile_totals(Decimal("100.000000"), Decimal("99.000000"))
    assert result.status == "mismatch"
    assert result.difference_ratio == Decimal("0.010000")


def test_reconcile_totals_zero_totals_no_division_error():
    """双零不触发除零错误。"""
    result = reconcile_totals(Decimal("0"), Decimal("0"))
    assert result.status == "passed"
    assert result.difference_ratio == Decimal("0.000000")


def test_reconcile_totals_quantizes_inputs_to_six_decimals():
    """输入被量化为 6 位小数。"""
    result = reconcile_totals(Decimal("1.1234567"), Decimal("1.1234561"))
    # 量化后 usage=1.123457 ledger=1.123456 difference=0.000001 ratio=0.000001
    assert result.usage_credits == Decimal("1.123457")
    assert result.ledger_credits == Decimal("1.123456")


def test_reconciliation_result_is_frozen_dataclass():
    """ReconciliationResult 为不可变 dataclass。"""
    result = reconcile_totals(Decimal("1"), Decimal("1"))
    with pytest.raises(Exception):
        result.status = "mismatch"  # type: ignore[misc]


# ----------------------------------------------------------------------
# billing/reservations.py 纯函数
# ----------------------------------------------------------------------


def test_estimate_cost_with_zero_markup():
    """零加价时成本为纯 token 计费。"""
    cost = estimate_cost(
        prompt_tokens=1000,
        max_tokens=1000,
        price={
            "input_per_million": Decimal("1"),
            "output_per_million": Decimal("2"),
            "markup_percent": Decimal("0"),
        },
    )
    # (1000*1 + 1000*2)/1_000_000 = 0.003
    assert cost == Decimal("0.003000")


def test_estimate_cost_with_zero_tokens():
    """零 token 时成本为 0。"""
    cost = estimate_cost(
        prompt_tokens=0,
        max_tokens=0,
        price={
            "input_per_million": Decimal("1"),
            "output_per_million": Decimal("2"),
            "markup_percent": Decimal("10"),
        },
    )
    assert cost == Decimal("0.000000")


def test_estimate_cost_quantizes_to_six_decimals():
    """成本被量化为 6 位小数。"""
    cost = estimate_cost(
        prompt_tokens=1,
        max_tokens=1,
        price={
            "input_per_million": Decimal("0.000001"),
            "output_per_million": Decimal("0.000001"),
            "markup_percent": Decimal("0"),
        },
    )
    assert cost == Decimal("0.000000")  # 2e-12 量化后为 0


def test_settle_reservation_rejects_none_actual_without_release():
    """无 release 且 actual=None 时抛出 ValueError。"""
    with pytest.raises(ValueError, match="actual settlement is required"):
        settle_reservation_amounts(ReservationState(estimated=Decimal("10"), actual=None, frozen=Decimal("10")))


def test_settle_reservation_rejects_negative_actual():
    """actual 为负数时抛出 ValueError。"""
    with pytest.raises(ValueError, match="actual settlement is required"):
        settle_reservation_amounts(ReservationState(estimated=Decimal("10"), actual=Decimal("-1"), frozen=Decimal("10")))


def test_settle_reservation_rejects_actual_above_frozen():
    """actual 超过 frozen 时抛出 ValueError。"""
    with pytest.raises(ValueError, match="frozen reservation"):
        settle_reservation_amounts(ReservationState(estimated=Decimal("5"), actual=Decimal("6"), frozen=Decimal("5")))


def test_settle_reservation_exact_actual_returns_zero_refund():
    """actual 等于 frozen 时退款为 0。"""
    result = settle_reservation_amounts(
        ReservationState(estimated=Decimal("10"), actual=Decimal("10"), frozen=Decimal("10"))
    )
    assert result.status == "settled"
    assert result.frozen == Decimal("0")
    assert result.refund == Decimal("0")


def test_settle_reservation_partial_actual_refunds_remainder():
    """actual 小于 frozen 时退还未使用部分。"""
    result = settle_reservation_amounts(
        ReservationState(estimated=Decimal("10"), actual=Decimal("3"), frozen=Decimal("10"))
    )
    assert result.status == "settled"
    assert result.frozen == Decimal("0")
    assert result.refund == Decimal("7")


def test_release_returns_full_estimate_regardless_of_actual():
    """release=True 时无视 actual，退回全部 estimated。"""
    result = settle_reservation_amounts(
        ReservationState(estimated=Decimal("10"), actual=None, frozen=Decimal("10")),
        release=True,
    )
    assert result.status == "released"
    assert result.frozen == Decimal("0")
    assert result.refund == Decimal("10")


def test_reservation_state_and_result_are_frozen_dataclasses():
    """ReservationState 与 ReservationResult 为不可变 dataclass。"""
    state = ReservationState(estimated=Decimal("1"), actual=Decimal("1"), frozen=Decimal("1"))
    with pytest.raises(Exception):
        state.estimated = Decimal("2")  # type: ignore[misc]
    result = ReservationResult(status="settled", frozen=Decimal("0"), refund=Decimal("0"))
    with pytest.raises(Exception):
        result.status = "released"  # type: ignore[misc]


# ----------------------------------------------------------------------
# privacy/service.py 纯函数
# ----------------------------------------------------------------------


def test_infer_processing_activity_c4_for_secret_tables():
    """敏感凭证表归类为 C4。"""
    for table in ("id_refresh_token", "gw_token", "user_api_key", "mfa_factor"):
        activity = infer_processing_activity(table)
        assert activity.classification == "C4"
        assert activity.deletion_behavior == "revoke_and_delete"
        assert activity.retention_days == 30


def test_infer_processing_activity_c3_for_user_and_billing_tables():
    """用户/账单/审计表归类为 C3。"""
    activity = infer_processing_activity("id_user")
    assert activity.classification == "C3"
    assert activity.owner == "privacy"
    assert activity.retention_days == 2555
    activity = infer_processing_activity("bill_transaction")
    assert activity.classification == "C3"


def test_infer_processing_activity_c2_for_ag_tables():
    """ag_ 前缀表归类为 C2。"""
    activity = infer_processing_activity("ag_session")
    assert activity.classification == "C2"
    assert activity.owner == "product"


def test_infer_processing_activity_c1_for_ops_tables():
    """ops_ 前缀表归类为 C1。"""
    activity = infer_processing_activity("ops_job")
    assert activity.classification == "C1"
    assert activity.owner == "platform"
    assert activity.deletion_behavior == "expire"


def test_infer_processing_activity_c2_for_id_prefix_tables():
    """id_ 前缀非特例表归类为 C2。"""
    activity = infer_processing_activity("id_workspace")
    assert activity.classification == "C2"


def test_infer_processing_activity_default_c3_for_unclassified():
    """未识别表归类为 C3 待审。"""
    activity = infer_processing_activity("unknown_future_table")
    assert activity.classification == "C3"
    assert activity.deletion_behavior == "review_before_delete"


def test_transition_allowed_accepts_valid_forward_transitions():
    """合法前进状态转换被接受。"""
    assert transition_allowed("requested", "identity_verification") is True
    assert transition_allowed("identity_verification", "scoped") is True
    assert transition_allowed("scoped", "approved") is True
    assert transition_allowed("approved", "executing") is True
    assert transition_allowed("executing", "verification") is True
    assert transition_allowed("verification", "completed") is True


def test_transition_allowed_rejects_invalid_transitions():
    """非法状态转换被拒绝。"""
    assert transition_allowed("requested", "executing") is False
    assert transition_allowed("requested", "completed") is False
    assert transition_allowed("scoped", "executing") is False


def test_transition_allowed_rejects_terminal_reentry():
    """终态不可再转换。"""
    for terminal in ("completed", "partially_completed", "rejected"):
        assert transition_allowed(terminal, "requested") is False
        assert transition_allowed(terminal, "executing") is False


def test_transition_allowed_rejects_unknown_current_state():
    """未知当前状态拒绝任何转换。"""
    assert transition_allowed("unknown_state", "requested") is False


def test_deletion_steps_for_non_content_scope_returns_minimal_steps():
    """非 content 作用域返回最小步骤。"""
    steps = deletion_steps("metadata")
    assert steps == ["scope_resources", "verify_absence"]


def test_deletion_steps_for_content_scope_returns_six_steps():
    """content 作用域返回 6 个步骤。"""
    steps = deletion_steps("content")
    assert len(steps) == 6
    assert steps[0] == "revoke_access"
    assert steps[-1] == "verify_absence"


def test_build_export_manifest_checksum_is_stable_for_same_inputs():
    """相同输入产生相同 checksum。"""
    a = build_export_manifest("dsr_1", "usr_1", {"sessions": 2}, ["billing_ledger"])
    b = build_export_manifest("dsr_1", "usr_1", {"sessions": 2}, ["billing_ledger"])
    assert a.checksum == b.checksum


def test_build_export_manifest_checksum_changes_with_counts():
    """资源数量变化导致 checksum 变化。"""
    a = build_export_manifest("dsr_1", "usr_1", {"sessions": 2}, ["billing_ledger"])
    b = build_export_manifest("dsr_1", "usr_1", {"sessions": 3}, ["billing_ledger"])
    assert a.checksum != b.checksum


def test_build_export_manifest_sorts_resource_counts_in_manifest():
    """manifest 中 resource_counts 被排序。"""
    manifest = build_export_manifest("dsr_1", "usr_1", {"z": 1, "a": 2, "m": 3}, [])
    keys = list(manifest.manifest["resource_counts"].keys())
    assert keys == ["a", "m", "z"]


def test_build_export_manifest_dedupes_retained_items():
    """retained_items 去重并排序。"""
    manifest = build_export_manifest("dsr_1", "usr_1", {}, ["b", "a", "b"])
    assert manifest.manifest["retained_items"] == ["a", "b"]


def test_build_export_manifest_schema_version_is_one():
    """manifest schema_version 固定为 1。"""
    manifest = build_export_manifest("dsr_1", "usr_1", {}, [])
    assert manifest.manifest["schema_version"] == "1"
    assert manifest.manifest["request_id"] == "dsr_1"
    assert manifest.manifest["subject_ref"] == "usr_1"


# ----------------------------------------------------------------------
# audit_exports.py 纯函数
# ----------------------------------------------------------------------


def test_safe_details_strips_sensitive_keys_recursively():
    """_safe_details 递归移除敏感字段。"""
    details = {
        "action": "role.updated",
        "api_key": "secret",
        "nested": {"content": "private", "count": 2, "password": "pw"},
    }
    result = audit_exports._safe_details(details)
    assert "api_key" not in result
    assert "password" not in result["nested"]
    assert "content" not in result["nested"]
    assert result["action"] == "role.updated"
    assert result["nested"]["count"] == 2


def test_safe_details_strips_sensitive_keys_case_insensitive():
    """敏感字段名大小写不敏感地被移除。"""
    details = {"API_KEY": "x", "Token": "y", "safe": "z"}
    result = audit_exports._safe_details(details)
    assert "API_KEY" not in result
    assert "Token" not in result
    assert result["safe"] == "z"


def test_safe_details_handles_lists():
    """_safe_details 处理列表中的字典。"""
    details = {"items": [{"api_key": "x", "ok": 1}, {"ok": 2}]}
    result = audit_exports._safe_details(details)
    assert "api_key" not in result["items"][0]
    assert result["items"][0]["ok"] == 1
    assert result["items"][1]["ok"] == 2


def test_safe_details_rejects_oversized_payload():
    """超过 24000 字节的 payload 抛出 ValueError。"""
    big = {"data": "x" * 24_001}
    with pytest.raises(ValueError, match="details too large"):
        audit_exports._safe_details(big)


def test_chain_hash_is_stable_for_same_inputs():
    """相同输入产生相同 hash。"""
    record = {"sequence": 1, "event_type": "audit.test"}
    assert audit_exports.chain_hash("prev", record) == audit_exports.chain_hash("prev", dict(record))


def test_chain_hash_changes_with_previous_hash():
    """previous_hash 变化导致结果变化。"""
    record = {"sequence": 1, "event_type": "audit.test"}
    assert audit_exports.chain_hash("prev1", record) != audit_exports.chain_hash("prev2", record)


def test_chain_hash_changes_with_record_content():
    """record 内容变化导致结果变化。"""
    a = audit_exports.chain_hash("", {"seq": 1})
    b = audit_exports.chain_hash("", {"seq": 2})
    assert a != b


def test_chain_hash_returns_64_char_hex():
    """chain_hash 返回 64 字符的十六进制 SHA256。"""
    result = audit_exports.chain_hash("", {"a": 1})
    assert len(result) == 64
    int(result, 16)  # 验证是合法十六进制


def test_is_controlled_siem_endpoint_accepts_mock_and_local():
    """mock:// 和 local:// siem 端点为受控端点。"""
    assert audit_exports._is_controlled_siem_endpoint("mock://siem/test") is True
    assert audit_exports._is_controlled_siem_endpoint("local://siem/ingest") is True
    assert audit_exports._is_controlled_siem_endpoint("mock://siem") is True


def test_is_controlled_siem_endpoint_rejects_public_urls():
    """公网 URL 不是受控端点。"""
    assert audit_exports._is_controlled_siem_endpoint("https://siem.example.com") is False
    assert audit_exports._is_controlled_siem_endpoint("http://127.0.0.1:9200") is False
    assert audit_exports._is_controlled_siem_endpoint("mock://other/test") is False


def test_is_controlled_siem_endpoint_is_case_insensitive():
    """端点匹配大小写不敏感。"""
    assert audit_exports._is_controlled_siem_endpoint("MOCK://SIEM/test") is True
    assert audit_exports._is_controlled_siem_endpoint("Local://Siem/x") is True


def test_siem_raw_body_is_deterministic():
    """siem_raw_body 为确定性 bytes。"""
    a = audit_exports.siem_raw_body("audit.test", "ws_1", "key-1")
    b = audit_exports.siem_raw_body("audit.test", "ws_1", "key-1")
    assert a == b
    assert isinstance(a, bytes)


def test_siem_raw_body_changes_with_inputs():
    """输入变化导致 raw_body 变化。"""
    a = audit_exports.siem_raw_body("audit.test", "ws_1", "key-1")
    b = audit_exports.siem_raw_body("audit.test2", "ws_1", "key-1")
    c = audit_exports.siem_raw_body("audit.test", "ws_2", "key-1")
    d = audit_exports.siem_raw_body("audit.test", "ws_1", "key-2")
    assert a != b != c != d


def test_siem_signature_uses_fallback_when_credential_hash_none():
    """credential_hash 为 None 时使用 fallback_key。"""
    result = audit_exports.siem_signature(None, "payload", fallback_key="fallback")
    expected = "sha256=" + hmac.new(b"fallback", b"payload", hashlib.sha256).hexdigest()
    assert result == expected


def test_siem_signature_prefers_credential_hash_over_fallback():
    """有 credential_hash 时优先使用，忽略 fallback_key。"""
    result = audit_exports.siem_signature("real-hash", "payload", fallback_key="fallback")
    expected = "sha256=" + hmac.new(b"real-hash", b"payload", hashlib.sha256).hexdigest()
    assert result == expected


def test_siem_signature_accepts_bytes_payload():
    """siem_signature 接受 bytes 类型 payload。"""
    result = audit_exports.siem_signature("k", b"bytes-payload", fallback_key="fb")
    expected = "sha256=" + hmac.new(b"k", b"bytes-payload", hashlib.sha256).hexdigest()
    assert result == expected


def test_siem_signature_accepts_str_payload():
    """siem_signature 接受 str 类型 payload 并编码为 utf-8。"""
    result = audit_exports.siem_signature("k", "str-payload", fallback_key="fb")
    expected = "sha256=" + hmac.new(b"k", "str-payload".encode("utf-8"), hashlib.sha256).hexdigest()
    assert result == expected


def test_siem_retry_delay_is_exponential_from_base_two():
    """siem_retry_delay 以 2 为底指数增长：2,4,8,16..."""
    assert [audit_exports.siem_retry_delay(n) for n in range(1, 6)] == [2, 4, 8, 16, 32]


def test_siem_retry_delay_caps_at_max_seconds():
    """siem_retry_delay 封顶为 SIEM_RETRY_MAX_SECONDS。"""
    assert audit_exports.siem_retry_delay(20) == audit_exports.SIEM_RETRY_MAX_SECONDS
    assert audit_exports.siem_retry_delay(100) == audit_exports.SIEM_RETRY_MAX_SECONDS


def test_siem_retry_delay_handles_zero_and_negative_as_base():
    """0 和负数 attempt 返回 base seconds。"""
    assert audit_exports.siem_retry_delay(0) == audit_exports.SIEM_RETRY_BASE_SECONDS
    assert audit_exports.siem_retry_delay(-5) == audit_exports.SIEM_RETRY_BASE_SECONDS


def test_safe_siem_delivery_summary_bounds_bytes_at_max():
    """_safe_siem_delivery_summary 将 bytes 截断到 SIEM_MAX_RESPONSE_BYTES。"""
    summary = audit_exports._safe_siem_delivery_summary(
        status_code=200, bytes_read=audit_exports.SIEM_MAX_RESPONSE_BYTES + 100
    )
    assert summary["response_bytes"] == audit_exports.SIEM_MAX_RESPONSE_BYTES
    assert summary["status_code"] == 200


def test_safe_siem_delivery_summary_omits_reason_when_none():
    """reason 为 None 时不包含 reason 字段。"""
    summary = audit_exports._safe_siem_delivery_summary(status_code=204, bytes_read=10)
    assert "reason" not in summary
    assert summary["status_code"] == 204
    assert summary["response_bytes"] == 10


def test_safe_siem_delivery_summary_includes_reason_when_provided():
    """提供 reason 时包含该字段。"""
    summary = audit_exports._safe_siem_delivery_summary(status_code=None, reason="timeout")
    assert summary["reason"] == "timeout"
    assert summary["status_code"] is None


def test_siem_external_execution_maps_status_correctly():
    """_siem_external_execution 正确映射状态。"""
    # controlled=True 总是 completed
    assert audit_exports._siem_external_execution("delivering", controlled=True) == "completed"
    assert audit_exports._siem_external_execution("pending_external", controlled=True) == "completed"
    # delivered -> completed
    assert audit_exports._siem_external_execution("delivered", controlled=False) == "completed"
    # failed/disabled -> failed
    assert audit_exports._siem_external_execution("failed", controlled=False) == "failed"
    assert audit_exports._siem_external_execution("disabled", controlled=False) == "failed"
    # 其他 -> pending
    assert audit_exports._siem_external_execution("pending_external", controlled=False) == "pending"
    assert audit_exports._siem_external_execution("retry_wait", controlled=False) == "pending"


def test_audit_query_defaults_are_bounded():
    """AuditQuery 默认 limit=100，接受 1-500。"""
    q = audit_exports.AuditQuery()
    assert q.limit == 100
    assert q.cursor is None
    assert q.action is None


def test_audit_query_rejects_limit_above_max():
    """limit 超过 500 被拒绝。"""
    with pytest.raises(ValueError):
        audit_exports.AuditQuery(limit=501)


def test_audit_query_rejects_limit_below_min():
    """limit 小于 1 被拒绝。"""
    with pytest.raises(ValueError):
        audit_exports.AuditQuery(limit=0)


def test_siem_config_upsert_accepts_controlled_endpoint():
    """SiemConfigUpsert 接受受控 mock/local 端点。"""
    config = audit_exports.SiemConfigUpsert(name="SIEM", endpoint="mock://siem/test", credential="secret")
    assert config.endpoint == "mock://siem/test"


def test_siem_config_upsert_rejects_invalid_name():
    """SiemConfigUpsert 拒绝过短 name。"""
    with pytest.raises(ValueError):
        audit_exports.SiemConfigUpsert(name="S", endpoint="mock://siem/test")


def test_siem_test_request_requires_idempotency_key():
    """SiemTestRequest 必须提供非空 idempotency_key。"""
    with pytest.raises(ValueError):
        audit_exports.SiemTestRequest(idempotency_key="")


def test_siem_test_request_defaults_event_type():
    """SiemTestRequest 默认 event_type 为 audit.test。"""
    req = audit_exports.SiemTestRequest(idempotency_key="key-1")
    assert req.event_type == "audit.test"


def test_siem_constants_are_set():
    """SIEM 常量值正确。"""
    assert audit_exports.SIEM_MAX_BODY_BYTES == 256 * 1024
    assert audit_exports.SIEM_MAX_RESPONSE_BYTES == 256 * 1024
    assert audit_exports.SIEM_MAX_ATTEMPTS == 5
    assert audit_exports.SIEM_RETRY_BASE_SECONDS == 2
    assert audit_exports.SIEM_RETRY_MAX_SECONDS == 300
    assert audit_exports.SIEM_TIMEOUT_SECONDS == 10.0


# ----------------------------------------------------------------------
# open_platform.py 纯函数
# ----------------------------------------------------------------------


def test_normalize_list_dedupes_sorts_and_lowercases():
    """_normalize_list 去重、排序、转小写。"""
    result = open_platform._normalize_list(
        ["B", "a", "b", "A"], pattern=open_platform._SCOPE_RE, name="scopes", max_items=32
    )
    assert result == ["a", "b"]


def test_normalize_list_rejects_too_many_items():
    """_normalize_list 超过 max_items 抛出 ValueError。"""
    with pytest.raises(ValueError, match="too many items"):
        open_platform._normalize_list(["a", "b"], pattern=open_platform._SCOPE_RE, name="scopes", max_items=1)


def test_normalize_list_rejects_invalid_pattern():
    """_normalize_list 拒绝不匹配模式的值。"""
    with pytest.raises(ValueError, match="invalid value"):
        open_platform._normalize_list(
            ["1invalid"], pattern=open_platform._SCOPE_RE, name="scopes", max_items=32
        )


def test_is_controlled_webhook_url_accepts_mock_and_local():
    """mock:// 和 local:// webhook URL 为受控。"""
    assert open_platform._is_controlled_webhook_url("mock://webhook/controlled") is True
    assert open_platform._is_controlled_webhook_url("local://webhook/controlled") is True


def test_is_controlled_webhook_url_accepts_nested_paths():
    """受控 webhook URL 支持多级路径。"""
    assert open_platform._is_controlled_webhook_url("mock://webhook/org/ws/hook/extra") is True


def test_is_controlled_webhook_url_rejects_invalid_schemes():
    """非 mock/local scheme 被拒绝。"""
    assert open_platform._is_controlled_webhook_url("https://webhook/controlled") is False
    assert open_platform._is_controlled_webhook_url("mock://other/controlled") is False


def test_is_controlled_webhook_url_rejects_path_traversal():
    """路径遍历被拒绝。"""
    assert open_platform._is_controlled_webhook_url("mock://webhook/../private") is False


def test_pkce_challenge_is_base64url_without_padding():
    """_pkce_challenge 返回无填充的 base64url 编码 SHA256。"""
    verifier = "a" * 64
    challenge = open_platform._pkce_challenge(verifier)
    # SHA256 = 32 字节 -> base64 44 字符 -> 去掉 1 个 = 为 43 字符
    assert len(challenge) == 43
    assert "=" not in challenge
    # 幂等
    assert challenge == open_platform._pkce_challenge(verifier)


def test_last4_returns_last_four_characters():
    """_last4 返回最后 4 个字符。"""
    assert open_platform._last4("wama_secret_abc123") == "c123"
    assert open_platform._last4("1234") == "1234"


def test_token_has_prefix():
    """_token 返回带前缀的 token。"""
    token = open_platform._token("wama_at_")
    assert token.startswith("wama_at_")
    assert len(token) > len("wama_at_")


def test_webhook_signature_format_is_timestamp_v1():
    """webhook_signature 格式为 t=<ts>,v1=<hex>。"""
    sig = open_platform.webhook_signature("secret", "payload", timestamp=1700000000)
    assert sig.startswith("t=1700000000,v1=")
    digest = hmac.new(b"secret", b"1700000000.payload", hashlib.sha256).hexdigest()
    assert sig == f"t=1700000000,v1={digest}"


def test_webhook_signature_is_deterministic_with_explicit_timestamp():
    """相同 secret/payload/timestamp 产生相同签名。"""
    a = open_platform.webhook_signature("s", "p", timestamp=1700000000)
    b = open_platform.webhook_signature("s", "p", timestamp=1700000000)
    assert a == b


def test_webhook_raw_body_is_deterministic_bytes():
    """webhook_raw_body 为确定性 bytes。"""
    a = open_platform.webhook_raw_body("artifact.created", {"id": "art_1"})
    b = open_platform.webhook_raw_body("artifact.created", {"id": "art_1"})
    assert a == b
    assert isinstance(a, bytes)


def test_webhook_raw_body_changes_with_payload():
    """payload 变化导致 raw_body 变化。"""
    a = open_platform.webhook_raw_body("artifact.created", {"id": "art_1"})
    b = open_platform.webhook_raw_body("artifact.created", {"id": "art_2"})
    assert a != b


def test_webhook_retry_delay_is_bounded_exponential():
    """webhook_retry_delay 以 2 为底指数增长。"""
    assert [open_platform.webhook_retry_delay(n) for n in range(1, 5)] == [2, 4, 8, 16]


def test_webhook_retry_delay_caps_at_max():
    """webhook_retry_delay 封顶为 WEBHOOK_RETRY_MAX_SECONDS。"""
    assert open_platform.webhook_retry_delay(20) == open_platform.WEBHOOK_RETRY_MAX_SECONDS


def test_safe_delivery_summary_bounds_bytes_at_max():
    """_safe_delivery_summary 将 bytes 截断到 WEBHOOK_MAX_RESPONSE_BYTES。"""
    summary = open_platform._safe_delivery_summary(
        status_code=200, bytes_read=open_platform.WEBHOOK_MAX_RESPONSE_BYTES + 10
    )
    assert summary["response_bytes"] == open_platform.WEBHOOK_MAX_RESPONSE_BYTES


def test_safe_delivery_summary_omits_reason_when_none():
    """reason 为 None 时不包含 reason 字段。"""
    summary = open_platform._safe_delivery_summary(status_code=200)
    assert "reason" not in summary


def test_masked_url_strips_query_fragment_and_userinfo():
    """_masked_url 仅保留 scheme://host/path。"""
    masked = open_platform._masked_url("https://user:pass@host.example.com/path?query=1#frag")
    assert masked == "https://host.example.com/path"


def test_masked_url_defaults_path_to_slash():
    """无 path 时默认为 /。"""
    masked = open_platform._masked_url("https://host.example.com")
    assert masked == "https://host.example.com/"


def test_client_public_redacts_secret_hash():
    """_client_public 不泄露 client_secret_hash。"""
    row = {
        "id": "id_1", "client_id": "cid", "name": "n",
        "redirect_uris": [], "scopes": ["openid"], "grant_types": ["authorization_code"],
        "status": "active", "client_secret_hash": "hidden", "client_secret_last4": "ab12",
        "version": 1, "created_at": "t1", "updated_at": "t1",
    }
    public = open_platform._client_public(row)
    assert "client_secret_hash" not in public
    assert public["secret_last4"] == "ab12"
    assert public["secret_status"] == "configured"


def test_webhook_public_masks_url_and_redacts_secret():
    """_webhook_public 掩码 URL 且不泄露 secret_hash。"""
    row = {
        "id": "id_1", "url": "https://user:pass@host.example.com/hook?token=x",
        "events": ["artifact.created"], "description": "d",
        "secret_hash": "hidden", "secret_last4": "cd34",
        "status": "active", "failure_count": 0,
        "version": 1, "created_at": "t1", "updated_at": "t1",
    }
    public = open_platform._webhook_public(row)
    assert "secret_hash" not in public
    assert public["url"] == "https://host.example.com/hook"
    assert public["secret_last4"] == "cd34"


def test_oauth_client_create_normalizes_name_whitespace():
    """OAuthClientCreate 折叠多余空白。"""
    client = open_platform.OAuthClientCreate(
        name="  My   Console  ",
        redirect_uris=["https://app.example.com/callback"],
    )
    assert client.name == "My Console"


def test_oauth_client_create_rejects_empty_name_after_normalization():
    """OAuthClientCreate 拒绝空白 name。"""
    with pytest.raises(ValueError):
        open_platform.OAuthClientCreate(
            name="   ",
            redirect_uris=["https://app.example.com/callback"],
        )


def test_oauth_client_create_rejects_duplicate_redirects():
    """OAuthClientCreate 拒绝重复 redirect_uri。"""
    with pytest.raises(ValueError, match="unique"):
        open_platform.OAuthClientCreate(
            name="Console",
            redirect_uris=["https://app.example.com/callback", "https://app.example.com/callback"],
        )


def test_oauth_client_create_requires_authorization_code_grant():
    """OAuthClientCreate 必须包含 authorization_code grant。"""
    with pytest.raises(ValueError, match="authorization_code grant is required"):
        open_platform.OAuthClientCreate(
            name="Console",
            redirect_uris=["https://app.example.com/callback"],
            grant_types=["refresh_token"],
        )


def test_oauth_client_create_dedupes_and_sorts_scopes():
    """OAuthClientCreate 去重并排序 scopes。"""
    client = open_platform.OAuthClientCreate(
        name="Console",
        redirect_uris=["https://app.example.com/callback"],
        scopes=["profile", "openid", "profile"],
    )
    assert client.scopes == ["openid", "profile"]


def test_oauth_authorize_query_validates_client_id_pattern():
    """OAuthAuthorizeQuery 校验 client_id 格式。"""
    with pytest.raises(ValueError):
        open_platform.OAuthAuthorizeQuery(
            client_id="invalid_id",
            redirect_uri="https://app.example.com/callback",
            code_challenge="b" * 43,
        )


def test_oauth_token_request_refresh_grant_requires_refresh_token():
    """refresh_token grant 必须提供 refresh_token。"""
    with pytest.raises(ValueError, match="refresh_token grant requires"):
        open_platform.OAuthTokenRequest(
            grant_type="refresh_token",
            client_id="wama_client_abcdefghijklmnop",
            client_secret="x",
        )


def test_webhook_create_rejects_too_many_events():
    """WebhookCreate 拒绝超过上限的 events。"""
    events = ["artifact.created"] * 33
    with pytest.raises(ValueError):
        open_platform.WebhookCreate(url="mock://webhook/controlled", events=events)


def test_webhook_test_request_rejects_unsupported_event():
    """WebhookTestRequest 拒绝不支持的 event_type。"""
    with pytest.raises(ValueError):
        open_platform.WebhookTestRequest(event_type="user.deleted")


def test_webhook_test_request_rejects_star_event():
    """WebhookTestRequest 拒绝 * 通配事件。"""
    with pytest.raises(ValueError):
        open_platform.WebhookTestRequest(event_type="*")


def test_webhook_events_allowlist_includes_wildcard():
    """WEBHOOK_EVENTS 包含 * 通配符。"""
    assert "*" in open_platform.WEBHOOK_EVENTS
    assert "artifact.created" in open_platform.WEBHOOK_EVENTS
    assert "assistant.created" in open_platform.WEBHOOK_EVENTS


def test_open_platform_constants_are_set():
    """open_platform 常量值正确。"""
    assert open_platform.WEBHOOK_MAX_BODY_BYTES == 256 * 1024
    assert open_platform.WEBHOOK_MAX_ATTEMPTS == 5
    assert open_platform.WEBHOOK_RETRY_BASE_SECONDS == 2
    assert open_platform.WEBHOOK_RETRY_MAX_SECONDS == 300
    assert open_platform.OAUTH_CODE_TTL == timedelta(minutes=5)
    assert open_platform.ACCESS_TOKEN_TTL == timedelta(minutes=15)
    assert open_platform.REFRESH_TOKEN_TTL == timedelta(days=30)


def test_validate_redirect_uri_accepts_localhost_http():
    """validate_redirect_uri 接受 localhost HTTP 回调。"""
    assert open_platform.validate_redirect_uri("http://localhost:3000/callback") == "http://localhost:3000/callback"
    assert open_platform.validate_redirect_uri("http://127.0.0.1:3000/callback") == "http://127.0.0.1:3000/callback"


def test_validate_redirect_uri_rejects_userinfo():
    """validate_redirect_uri 拒绝包含 userinfo 的 URL。"""
    with pytest.raises(ValueError, match="fragment or userinfo"):
        open_platform.validate_redirect_uri("https://user:pass@example.com/cb")


def test_validate_redirect_uri_rejects_fragment():
    """validate_redirect_uri 拒绝包含 fragment 的 URL。"""
    with pytest.raises(ValueError, match="fragment or userinfo"):
        open_platform.validate_redirect_uri("https://example.com/cb#frag")


def test_validate_redirect_uri_rejects_http_non_localhost():
    """validate_redirect_uri 拒绝非 localhost 的 HTTP。"""
    with pytest.raises(ValueError, match="HTTPS except for localhost"):
        open_platform.validate_redirect_uri("http://evil.example.com/cb")
