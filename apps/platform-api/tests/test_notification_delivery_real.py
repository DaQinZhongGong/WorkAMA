"""真实 SMTP / Webhook 投递的单元测试。

通过 mock 掉 ``aiosmtplib.send`` 与 ``httpx.AsyncClient`` 来验证
``send_email_real`` / ``send_webhook_real`` / ``deliver_email`` /
``deliver_webhook`` 的核心契约：
- 邮件：构造符合预期的 MIME，正确传递 SMTP 参数，返回 ``smtp:`` 前缀 provider_id。
- Webhook：HMAC-SHA256 签名格式、幂等头、重试逻辑（最多 3 次、指数退避）、
  状态码分流（< 400 成功、410 终止、其他触发重试）、超时/传输错误分类。
- 统一入口：根据 ``settings.smtp_mock`` / ``settings.notification_webhook_mock``
  自动切换 mock 与真实路径。
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest

from workama_platform.modules.notification.delivery import (
    _compute_webhook_signature,
    _webhook_raw_body,
    classify_delivery_error,
    deliver_email,
    deliver_webhook,
    send_email_real,
    send_webhook_mock,
    send_webhook_real,
)


# ----------------------------------------------------------------------
# 真实 SMTP 投递测试
# ----------------------------------------------------------------------


def _extract_mime_text(raw_msg) -> str:
    """从 MIMEMultipart 中提取纯文本部分用于断言。"""
    for part in raw_msg.get_payload():
        if part.get_content_subtype() == "plain":
            return part.get_payload(decode=True).decode("utf-8")
    return ""


def _extract_mime_html(raw_msg) -> str:
    for part in raw_msg.get_payload():
        if part.get_content_subtype() == "html":
            return part.get_payload(decode=True).decode("utf-8")
    return ""


@pytest.mark.asyncio
async def test_send_email_real_builds_mime_and_returns_smtp_provider_id():
    captured: dict = {}

    async def fake_send(msg, **kwargs):
        captured["msg"] = msg
        captured["kwargs"] = kwargs
        return {}

    with mock.patch("workama_platform.modules.notification.delivery.aiosmtplib.send", new=fake_send):
        provider_id = await send_email_real(
            "alice@example.com",
            "Welcome",
            "<p>Hello</p>",
            "Hello",
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_username="postmaster",
            smtp_password="secret",
            from_addr="no-reply@workama.local",
            use_tls=True,
        )

    assert provider_id.startswith("smtp:")
    msg = captured["msg"]
    assert msg["From"] == "no-reply@workama.local"
    assert msg["To"] == "alice@example.com"
    assert msg["Subject"] == "Welcome"
    assert msg["Message-ID"].startswith("<") and msg["Message-ID"].endswith("@workama>")
    assert _extract_mime_text(msg) == "Hello"
    assert _extract_mime_html(msg) == "<p>Hello</p>"
    assert captured["kwargs"]["hostname"] == "smtp.example.com"
    assert captured["kwargs"]["port"] == 587
    assert captured["kwargs"]["username"] == "postmaster"
    assert captured["kwargs"]["password"] == "secret"
    assert captured["kwargs"]["start_tls"] is True


@pytest.mark.asyncio
async def test_send_email_real_defaults_text_body_to_html_when_missing():
    captured: dict = {}

    async def fake_send(msg, **kwargs):
        captured["msg"] = msg
        captured["kwargs"] = kwargs
        return {}

    with mock.patch("workama_platform.modules.notification.delivery.aiosmtplib.send", new=fake_send):
        await send_email_real(
            "bob@example.com",
            "Subject",
            "<b>Body</b>",
            None,
            smtp_host="smtp.example.com",
            smtp_port=465,
            smtp_username="",
            smtp_password="",
            from_addr="no-reply@workama.local",
            use_tls=False,
        )

    # 文本回退为 HTML 内容
    assert _extract_mime_text(captured["msg"]) == "<b>Body</b>"
    # 空用户名/密码应转换为 None（aiosmtplib 行为约定）
    assert captured["kwargs"]["username"] is None
    assert captured["kwargs"]["password"] is None
    assert captured["kwargs"]["start_tls"] is False


@pytest.mark.asyncio
async def test_send_email_real_propagates_smtp_error():
    async def fake_send(msg, **kwargs):
        raise ConnectionRefusedError("smtp refused")

    with mock.patch("workama_platform.modules.notification.delivery.aiosmtplib.send", new=fake_send):
        with pytest.raises(ConnectionRefusedError):
            await send_email_real(
                "alice@example.com",
                "Subject",
                "<p>Hi</p>",
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username="",
                smtp_password="",
                from_addr="no-reply@workama.local",
            )


@pytest.mark.asyncio
async def test_deliver_email_uses_mock_when_smtp_mock_true():
    cfg = SimpleNamespace(
        smtp_mock=True,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_username="",
        smtp_password="",
        smtp_from="no-reply@workama.local",
        smtp_use_tls=True,
    )

    with mock.patch(
        "workama_platform.modules.notification.delivery.send_email",
        return_value="mock-email:abc",
    ) as fake:
        provider_id = await deliver_email(
            "alice@example.com", "Subject", "<p>Hi</p>", "Hi", settings=cfg
        )

    assert provider_id == "mock-email:abc"
    fake.assert_called_once_with("alice@example.com", "Subject", "Hi", mock=True)


@pytest.mark.asyncio
async def test_deliver_email_uses_mock_when_settings_missing():
    """settings=None 时默认走 mock 路径，保证向后兼容。"""
    with mock.patch(
        "workama_platform.modules.notification.delivery.send_email",
        return_value="mock-email:fallback",
    ) as fake:
        provider_id = await deliver_email(
            "alice@example.com", "Subject", "<p>Hi</p>", "Hi", settings=None
        )

    assert provider_id == "mock-email:fallback"
    fake.assert_called_once()


@pytest.mark.asyncio
async def test_deliver_email_routes_to_real_when_smtp_mock_false():
    cfg = SimpleNamespace(
        smtp_mock=False,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_username="postmaster",
        smtp_password="secret",
        smtp_from="no-reply@workama.local",
        smtp_use_tls=True,
    )

    async def fake_send(msg, **kwargs):
        return {}

    with mock.patch("workama_platform.modules.notification.delivery.aiosmtplib.send", new=fake_send):
        provider_id = await deliver_email(
            "alice@example.com", "Subject", "<p>Hi</p>", "Hi", settings=cfg
        )

    assert provider_id.startswith("smtp:")


@pytest.mark.asyncio
async def test_deliver_email_raises_when_real_mode_without_host():
    cfg = SimpleNamespace(
        smtp_mock=False,
        smtp_host="",
        smtp_port=587,
        smtp_username="",
        smtp_password="",
        smtp_from="no-reply@workama.local",
        smtp_use_tls=True,
    )

    with pytest.raises(RuntimeError, match="smtp_not_configured"):
        await deliver_email("alice@example.com", "Subject", "<p>Hi</p>", settings=cfg)


# ----------------------------------------------------------------------
# 真实 Webhook 投递测试
# ----------------------------------------------------------------------


def _build_response(status_code: int, *, request_id: str | None = None) -> httpx.Response:
    headers = {"x-request-id": request_id} if request_id else {}
    return httpx.Response(status_code=status_code, headers=headers, text="")


@pytest.mark.asyncio
async def test_send_webhook_real_signs_payload_and_returns_provider_id():
    payload = {"event_type": "agent.completed", "resource_ref": "run_1"}
    occurred_at = datetime(2026, 7, 25, 10, 0, 0, tzinfo=UTC)
    expected_ts = str(int(occurred_at.timestamp()))
    expected_signature = _compute_webhook_signature(
        expected_ts, "secret", _webhook_raw_body(payload)
    )

    captured: dict = {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            captured["follow_redirects"] = kwargs.get("follow_redirects")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, content=None, headers=None):
            captured["url"] = url
            captured["content"] = content
            captured["headers"] = headers
            return _build_response(200, request_id="req-123")

    with mock.patch(
        "workama_platform.modules.notification.delivery.httpx.AsyncClient",
        new=FakeAsyncClient,
    ):
        result = await send_webhook_real(
            "https://example.com/hook",
            "secret",
            payload,
            "idem-1",
            occurred_at=occurred_at,
        )

    assert result["status_code"] == 200
    assert result["provider_id"] == "req-123"
    assert result["timestamp"] == expected_ts
    assert result["signature"] == expected_signature
    assert result["body"] == _webhook_raw_body(payload)
    # 头部校验
    headers = captured["headers"]
    assert headers["X-Workama-Signature"] == expected_signature
    assert headers["X-Workama-Idempotency-Key"] == "idem-1"
    assert headers["X-Workama-Timestamp"] == expected_ts
    assert headers["Content-Type"] == "application/json"
    assert headers["User-Agent"] == "WorkAMA-Webhook/1"
    assert captured["timeout"] == 30.0
    assert captured["follow_redirects"] is False


@pytest.mark.asyncio
async def test_send_webhook_real_falls_back_to_hash_provider_id_when_no_request_id():
    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, content=None, headers=None):
            return _build_response(204)  # 无 x-request-id

    with mock.patch(
        "workama_platform.modules.notification.delivery.httpx.AsyncClient",
        new=FakeAsyncClient,
    ):
        result = await send_webhook_real(
            "https://example.com/hook",
            "secret",
            {"event": "x"},
            "idem-2",
        )

    assert result["provider_id"].startswith("webhook:")
    assert result["status_code"] == 204


@pytest.mark.asyncio
async def test_send_webhook_real_retries_on_5xx_then_succeeds():
    attempts = 0

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, content=None, headers=None):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return _build_response(500)
            return _build_response(200, request_id="req-retry")

    sleep_calls: list[int] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    with mock.patch(
        "workama_platform.modules.notification.delivery.httpx.AsyncClient",
        new=FakeAsyncClient,
    ), mock.patch(
        "workama_platform.modules.notification.delivery.asyncio.sleep",
        new=fake_sleep,
    ):
        result = await send_webhook_real(
            "https://example.com/hook",
            "secret",
            {"event": "x"},
            "idem-3",
            max_retries=3,
        )

    assert attempts == 3
    assert sleep_calls == [1, 2]  # 指数退避：2^0, 2^1
    assert result["status_code"] == 200


@pytest.mark.asyncio
async def test_send_webhook_real_retries_on_transport_error_until_exhausted():
    attempts = 0

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, content=None, headers=None):
            nonlocal attempts
            attempts += 1
            raise httpx.ConnectError("connection refused")

    async def fake_sleep(seconds):
        return None

    with mock.patch(
        "workama_platform.modules.notification.delivery.httpx.AsyncClient",
        new=FakeAsyncClient,
    ), mock.patch(
        "workama_platform.modules.notification.delivery.asyncio.sleep",
        new=fake_sleep,
    ):
        with pytest.raises(httpx.ConnectError):
            await send_webhook_real(
                "https://example.com/hook",
                "secret",
                {"event": "x"},
                "idem-4",
                max_retries=3,
            )

    assert attempts == 3


@pytest.mark.asyncio
async def test_send_webhook_real_410_is_terminal_and_not_retried():
    attempts = 0

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, content=None, headers=None):
            nonlocal attempts
            attempts += 1
            return _build_response(410)

    async def fake_sleep(seconds):
        return None

    with mock.patch(
        "workama_platform.modules.notification.delivery.httpx.AsyncClient",
        new=FakeAsyncClient,
    ), mock.patch(
        "workama_platform.modules.notification.delivery.asyncio.sleep",
        new=fake_sleep,
    ):
        with pytest.raises(RuntimeError, match="webhook endpoint gone"):
            await send_webhook_real(
                "https://example.com/hook",
                "secret",
                {"event": "x"},
                "idem-5",
                max_retries=3,
            )

    # 410 立即终止，不重试
    assert attempts == 1


@pytest.mark.asyncio
async def test_send_webhook_real_4xx_failure_raises_runtime_after_retries():
    attempts = 0

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, content=None, headers=None):
            nonlocal attempts
            attempts += 1
            return _build_response(422)

    async def fake_sleep(seconds):
        return None

    with mock.patch(
        "workama_platform.modules.notification.delivery.httpx.AsyncClient",
        new=FakeAsyncClient,
    ), mock.patch(
        "workama_platform.modules.notification.delivery.asyncio.sleep",
        new=fake_sleep,
    ):
        with pytest.raises(RuntimeError, match="webhook delivery failed"):
            await send_webhook_real(
                "https://example.com/hook",
                "secret",
                {"event": "x"},
                "idem-6",
                max_retries=3,
            )

    assert attempts == 3


@pytest.mark.asyncio
async def test_send_webhook_real_uses_now_when_occurred_at_missing():
    captured: dict = {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, content=None, headers=None):
            captured["headers"] = headers
            return _build_response(200)

    before = int(datetime.now(UTC).timestamp())

    with mock.patch(
        "workama_platform.modules.notification.delivery.httpx.AsyncClient",
        new=FakeAsyncClient,
    ):
        result = await send_webhook_real(
            "https://example.com/hook",
            "secret",
            {"event": "x"},
            "idem-7",
        )

    after = int(datetime.now(UTC).timestamp())
    ts = int(result["timestamp"])
    assert before <= ts <= after
    assert int(captured["headers"]["X-Workama-Timestamp"]) == ts


@pytest.mark.asyncio
async def test_deliver_webhook_uses_mock_when_notification_webhook_mock_true():
    cfg = SimpleNamespace(notification_webhook_mock=True)
    payload = {"event_type": "agent.completed"}

    with mock.patch(
        "workama_platform.modules.notification.delivery.send_webhook_mock",
        return_value={"provider_id": "mock-webhook:abc", "status_code": 202},
    ) as fake:
        result = await deliver_webhook(
            "mock://hook", "secret", payload, "idem", settings=cfg
        )

    assert result["provider_id"] == "mock-webhook:abc"
    fake.assert_called_once()


@pytest.mark.asyncio
async def test_deliver_webhook_routes_to_real_when_notification_webhook_mock_false():
    cfg = SimpleNamespace(notification_webhook_mock=False)

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, content=None, headers=None):
            return _build_response(200, request_id="req-real")

    with mock.patch(
        "workama_platform.modules.notification.delivery.httpx.AsyncClient",
        new=FakeAsyncClient,
    ):
        result = await deliver_webhook(
            "https://example.com/hook",
            "secret",
            {"event": "x"},
            "idem",
            settings=cfg,
        )

    assert result["provider_id"] == "req-real"
    assert result["status_code"] == 200


@pytest.mark.asyncio
async def test_deliver_webhook_defaults_to_mock_when_settings_missing():
    """settings=None 时默认走 mock 路径，保证向后兼容。"""
    with mock.patch(
        "workama_platform.modules.notification.delivery.send_webhook_mock",
        return_value={"provider_id": "mock-webhook:fallback", "status_code": 202},
    ) as fake:
        result = await deliver_webhook(
            "mock://hook", "secret", {"event": "x"}, "idem", settings=None
        )

    assert result["provider_id"] == "mock-webhook:fallback"
    fake.assert_called_once()


# ----------------------------------------------------------------------
# 签名一致性 & 错误分类回归
# ----------------------------------------------------------------------


def test_real_and_mock_signatures_match_for_same_payload_and_timestamp():
    """真实投递与 mock 投递共享签名计算，签名必须一致。"""
    payload = {"event_type": "agent.completed", "priority": "normal"}
    occurred_at = datetime(2026, 7, 25, 10, 0, 0, tzinfo=UTC)
    timestamp = str(int(occurred_at.timestamp()))

    mock_result = send_webhook_mock(
        "mock://hook", "shared-secret", payload, "idem", occurred_at=occurred_at
    )
    expected_signature = _compute_webhook_signature(
        timestamp, "shared-secret", _webhook_raw_body(payload)
    )

    assert mock_result["signature"] == expected_signature
    # 真实投递使用的签名前缀也一致
    assert expected_signature.startswith(f"t={timestamp},v1=")


def test_classify_delivery_error_covers_real_provider_errors():
    """真实投递引入的错误类型应被正确分类为 transient_provider_error。"""
    assert classify_delivery_error(httpx.ConnectTimeout("timeout")) == "transient_provider_error"
    assert classify_delivery_error(httpx.ReadTimeout("timeout")) == "transient_provider_error"
    assert classify_delivery_error(httpx.ConnectError("refused")) == "transient_provider_error"
    assert classify_delivery_error(httpx.RemoteProtocolError("oops")) == "transient_provider_error"
    # 业务层 RuntimeError 不应被识别为可重试错误
    assert classify_delivery_error(RuntimeError("webhook HTTP 500")) == "webhook HTTP 500"
