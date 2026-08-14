import pytest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from workama_platform.modules import external_apps
from workama_platform.modules.security.service import UrlValidationResult


_VALID_URL = UrlValidationResult(allowed=True, reason=None)


def _stream_ctx(response):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def test_external_http_live_timeout_is_10_seconds():
    assert external_apps._EXTERNAL_HTTP_LIVE_TIMEOUT_SECONDS == 10.0


def test_external_http_live_max_retries_is_2():
    assert external_apps._EXTERNAL_HTTP_LIVE_MAX_RETRIES == 2


def test_http_retryable_status_codes_include_expected():
    codes = external_apps._HTTP_RETRYABLE_STATUS_CODES
    assert 408 in codes
    assert 429 in codes
    assert 500 in codes
    assert 502 in codes
    assert 503 in codes
    assert 504 in codes


@pytest.mark.asyncio
async def test_external_http_execution_rejects_unknown_provider():
    result = await external_apps.external_http_execution(
        provider="unknown_provider",
        endpoint="https://example.com/api",
        operation="test",
        payload={},
        input_hash="hash123",
        config={"execution_mode": "external_http"},
    )
    assert result["success"] is False
    assert result["error_code"] == "unknown_provider"


@pytest.mark.asyncio
async def test_external_http_execution_rejects_invalid_config():
    result = await external_apps.external_http_execution(
        provider="dify",
        endpoint="https://example.com/api",
        operation="test",
        payload={},
        input_hash="hash123",
        config={"execution_mode": "invalid"},
    )
    assert result["success"] is False
    assert result["error_code"] == "invalid_execution_config"


@pytest.mark.asyncio
async def test_external_http_execution_rejects_unsafe_endpoint():
    result = await external_apps.external_http_execution(
        provider="dify",
        endpoint="http://192.168.1.1/internal",
        operation="test",
        payload={},
        input_hash="hash123",
        config={"execution_mode": "external_http"},
    )
    assert result["success"] is False
    assert result["error_code"] == "unsafe_endpoint"


@pytest.mark.asyncio
async def test_external_http_execution_success_on_first_attempt():
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-length": "100"}
    async def _aiter():
        yield b'{"ok": true}'
    mock_response.aiter_bytes = _aiter

    with patch.object(external_apps, "validate_resolved_outbound_url", new_callable=AsyncMock, return_value=_VALID_URL), \
         patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.stream = Mock(return_value=_stream_ctx(mock_response))
        mock_client_cls.return_value = mock_client

        result = await external_apps.external_http_execution(
            provider="dify",
            endpoint="https://api.dify.example/v1/chat/completions",
            operation="chat",
            payload={"messages": []},
            input_hash="hash123",
            config={"execution_mode": "external_http"},
        )
        assert result["success"] is True
        assert result["attempts"] == 1
        assert result["response_code"] == 200


@pytest.mark.asyncio
async def test_external_http_execution_retries_on_500():
    mock_response_500 = AsyncMock()
    mock_response_500.status_code = 500
    mock_response_500.headers = {}

    mock_response_200 = AsyncMock()
    mock_response_200.status_code = 200
    mock_response_200.headers = {"content-length": "100"}
    async def _aiter_200():
        yield b'{"ok": true}'
    mock_response_200.aiter_bytes = _aiter_200

    call_count = 0

    def mock_stream(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _stream_ctx(mock_response_500)
        return _stream_ctx(mock_response_200)

    with patch.object(external_apps, "validate_resolved_outbound_url", new_callable=AsyncMock, return_value=_VALID_URL), \
         patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.stream = mock_stream
        mock_client_cls.return_value = mock_client

        result = await external_apps.external_http_execution(
            provider="dify",
            endpoint="https://api.dify.example/v1/chat/completions",
            operation="chat",
            payload={"messages": []},
            input_hash="hash123",
            config={"execution_mode": "external_http"},
        )
        assert result["success"] is True
        assert result["attempts"] == 2


@pytest.mark.asyncio
async def test_external_http_execution_fails_after_max_retries():
    mock_response = AsyncMock()
    mock_response.status_code = 503
    mock_response.headers = {}

    with patch.object(external_apps, "validate_resolved_outbound_url", new_callable=AsyncMock, return_value=_VALID_URL), \
         patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.stream = Mock(return_value=_stream_ctx(mock_response))
        mock_client_cls.return_value = mock_client

        result = await external_apps.external_http_execution(
            provider="dify",
            endpoint="https://api.dify.example/v1/chat/completions",
            operation="chat",
            payload={"messages": []},
            input_hash="hash123",
            config={"execution_mode": "external_http"},
        )
        assert result["success"] is False
        assert result["attempts"] == 3
        assert result["error_code"] == "provider_http_503"


@pytest.mark.asyncio
async def test_external_http_execution_retries_on_timeout():
    with patch.object(external_apps, "validate_resolved_outbound_url", new_callable=AsyncMock, return_value=_VALID_URL), \
         patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        call_count = 0

        def mock_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise httpx.TimeoutException("Timeout")
            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_response.headers = {"content-length": "100"}
            async def _aiter():
                yield b'{"ok": true}'
            mock_response.aiter_bytes = _aiter
            return _stream_ctx(mock_response)

        mock_client.stream = mock_stream
        mock_client_cls.return_value = mock_client

        result = await external_apps.external_http_execution(
            provider="dify",
            endpoint="https://api.dify.example/v1/chat/completions",
            operation="chat",
            payload={"messages": []},
            input_hash="hash123",
            config={"execution_mode": "external_http"},
        )
        assert result["success"] is True
        assert result["attempts"] == 3


@pytest.mark.asyncio
async def test_external_http_execution_fails_on_request_error():
    with patch.object(external_apps, "validate_resolved_outbound_url", new_callable=AsyncMock, return_value=_VALID_URL), \
         patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.stream = Mock(side_effect=httpx.RequestError("Connection failed"))
        mock_client_cls.return_value = mock_client

        result = await external_apps.external_http_execution(
            provider="dify",
            endpoint="https://api.dify.example/v1/chat/completions",
            operation="chat",
            payload={"messages": []},
            input_hash="hash123",
            config={"execution_mode": "external_http"},
        )
        assert result["success"] is False
        assert result["error_code"] == "provider_network_error"
        assert result["attempts"] == 3


@pytest.mark.asyncio
async def test_external_http_execution_rejects_response_too_large():
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-length": str(10 * 1024 * 1024)}
    mock_response.aiter_bytes = AsyncMock(return_value=iter([b"x" * 1024]))

    with patch.object(external_apps, "validate_resolved_outbound_url", new_callable=AsyncMock, return_value=_VALID_URL), \
         patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.stream = Mock(return_value=_stream_ctx(mock_response))
        mock_client_cls.return_value = mock_client

        result = await external_apps.external_http_execution(
            provider="dify",
            endpoint="https://api.dify.example/v1/chat/completions",
            operation="chat",
            payload={"messages": []},
            input_hash="hash123",
            config={"execution_mode": "external_http"},
        )
        assert result["success"] is False
        assert result["error_code"] == "response_too_large"


@pytest.mark.asyncio
async def test_external_http_execution_with_workspace_id_audits():
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-length": "100"}
    async def _aiter():
        yield b'{"ok": true}'
    mock_response.aiter_bytes = _aiter

    with patch.object(external_apps, "validate_resolved_outbound_url", new_callable=AsyncMock, return_value=_VALID_URL), \
         patch("httpx.AsyncClient") as mock_client_cls, \
         patch.object(external_apps, "_audit_external_app_call", new_callable=AsyncMock) as mock_audit:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.stream = Mock(return_value=_stream_ctx(mock_response))
        mock_client_cls.return_value = mock_client

        result = await external_apps.external_http_execution(
            provider="dify",
            endpoint="https://api.dify.example/v1/chat/completions",
            operation="chat",
            payload={"messages": []},
            input_hash="hash123",
            config={"execution_mode": "external_http"},
            workspace_id="ws_123",
            actor_id="user_456",
            invocation_id="inv_789",
        )
        assert result["success"] is True
        mock_audit.assert_awaited()
        call_kwargs = mock_audit.await_args.kwargs
        assert call_kwargs["workspace_id"] == "ws_123"
        assert call_kwargs["actor_id"] == "user_456"
        assert call_kwargs["invocation_id"] == "inv_789"


@pytest.mark.asyncio
async def test_external_http_execution_without_workspace_id_skips_audit():
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-length": "100"}
    async def _aiter():
        yield b'{"ok": true}'
    mock_response.aiter_bytes = _aiter

    with patch.object(external_apps, "validate_resolved_outbound_url", new_callable=AsyncMock, return_value=_VALID_URL), \
         patch("httpx.AsyncClient") as mock_client_cls, \
         patch.object(external_apps, "_audit_external_app_call", new_callable=AsyncMock) as mock_audit:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.stream = Mock(return_value=_stream_ctx(mock_response))
        mock_client_cls.return_value = mock_client

        result = await external_apps.external_http_execution(
            provider="dify",
            endpoint="https://api.dify.example/v1/chat/completions",
            operation="chat",
            payload={"messages": []},
            input_hash="hash123",
            config={"execution_mode": "external_http"},
        )
        assert result["success"] is True
        mock_audit.assert_not_awaited()


def test_external_http_failure_structure():
    failure = external_apps._external_http_failure(
        attempts=2,
        response_code=500,
        error_code="provider_http_500",
        provider_request_sent=True,
    )
    assert failure["success"] is False
    assert failure["attempts"] == 2
    assert failure["response_code"] == 500
    assert failure["error_code"] == "provider_http_500"
    assert failure["retryable"] is True
    assert failure["result"]["provider_request_sent"] is True


def test_redact_provider_value_masks_sensitive_keys():
    payload = {
        "api_key": "sk-secret",
        "password": "my-pass",
        "token": "tktk",
        "normal": "value",
        "nested": {"secret": "hidden"},
    }
    redacted = external_apps._redact_provider_value(payload)
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["password"] == "[REDACTED]"
    assert redacted["token"] == "[REDACTED]"
    assert redacted["normal"] == "value"
    assert redacted["nested"]["secret"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_audit_external_app_call_handles_db_errors_gracefully():
    with patch.object(external_apps.pool, "connection", side_effect=Exception("DB down")):
        await external_apps._audit_external_app_call(
            workspace_id="ws_123",
            actor_id="user_456",
            invocation_id="inv_789",
            app_id="app_001",
            action="chat",
            endpoint="https://api.example.com",
            request_payload={"msg": "hi"},
            response_summary={"ok": True},
            status_code=200,
            error_code=None,
            attempt=1,
        )
