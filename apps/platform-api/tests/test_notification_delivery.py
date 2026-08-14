from datetime import UTC, datetime

from workama_platform.modules.notification.delivery import (
    classify_delivery_error,
    send_email,
    send_webhook_mock,
)
from workama_platform.modules.notification.service import (
    is_forced_in_app,
    preference_change_allowed,
    retry_delay_seconds,
)


def test_notification_preferences_keep_security_and_billing_visible():
    assert is_forced_in_app("security.login_failure", "in_app")
    assert is_forced_in_app("billing.low_balance", "in_app")
    assert not is_forced_in_app("agent.completed", "in_app")
    assert not preference_change_allowed("security.login_failure", "in_app", False)
    assert preference_change_allowed("security.login_failure", "email", False)
    assert preference_change_allowed("agent.completed", "in_app", False)


def test_delivery_backoff_is_bounded_and_error_classification_is_stable():
    assert [retry_delay_seconds(n) for n in range(1, 6)] == [60, 300, 1800, 7200, 43200]
    assert retry_delay_seconds(99) == 43200
    assert classify_delivery_error(TimeoutError("provider timeout")) == "transient_provider_error"
    assert classify_delivery_error(ValueError("bad address")) == "bad address"


def test_mock_email_is_deterministic_without_smtp():
    first = send_email("person@example.com", "Title", "Summary", mock=True)
    second = send_email("person@example.com", "Title", "Summary", mock=True)
    assert first == second
    assert first.startswith("mock-email:")


def test_mock_webhook_contains_hmac_and_idempotent_provider_id():
    timestamp = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)
    payload = {"event_type": "agent.completed", "resource_ref": "run_1"}
    first = send_webhook_mock("mock://notifications", "secret-hash", payload, "delivery-1", occurred_at=timestamp)
    second = send_webhook_mock("mock://notifications", "secret-hash", payload, "delivery-1", occurred_at=timestamp)
    assert first["status_code"] == 202
    assert first["provider_id"] == second["provider_id"]
    assert first["signature"].startswith("t=1784102400,v1=")
    assert "secret-hash" not in first["body"]
