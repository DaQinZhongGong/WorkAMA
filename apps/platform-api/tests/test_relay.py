"""OpenAI 兼容转发层 (relay) 的单元测试。

覆盖：
- 无 Authorization 头返回 401
- 无效令牌返回 401 (E01001)
- 模型不存在返回 404 (E01006)
- 有效请求转发到 mock 渠道（openai 协议）
- 流式响应 (stream=true)
- GET /v1/models 返回模型列表
- 协议适配：openai / anthropic / gemini

所有外部依赖（pool / resolve_route / httpx）均通过 monkeypatch 隔离。
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from workama_platform.modules.gateway import relay


# 强制走 Python relay fallback 路径，不走 Go 网关代理
@pytest.fixture(autouse=True)
def _disable_go_gateway(monkeypatch):
    monkeypatch.setattr(relay, "GATEWAY_GO_ENABLED", False)


# ----------------------------------------------------------------------
# 测试辅助：mock httpx
# ----------------------------------------------------------------------


class _MockResponse:
    """模拟 httpx.Response（非流式）。"""

    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json

    @property
    def is_success(self):
        return 200 <= self.status_code < 400


class _MockStreamResponse:
    """模拟 httpx 流式响应，支持 aiter_lines()。"""

    def __init__(self, status_code=200, lines=None):
        self.status_code = status_code
        self._lines = lines or []

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _MockStreamCM:
    """模拟 client.stream() 返回的异步上下文管理器。"""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *_args):
        return False


def _install_fake_httpx(
    monkeypatch,
    *,
    post_response=None,
    stream_response=None,
):
    """替换 relay 模块中的 httpx 引用，避免真实外部 HTTP 调用。"""

    class _AsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, headers=None, json=None):
            return post_response

        def stream(self, method, url, headers=None, json=None):
            return _MockStreamCM(stream_response)

    fake = SimpleNamespace(AsyncClient=_AsyncClient, HTTPError=httpx.HTTPError)
    monkeypatch.setattr(relay, "httpx", fake)


# ----------------------------------------------------------------------
# 测试辅助：mock pool
# ----------------------------------------------------------------------


class _Result:
    """模拟 psycopg 查询结果。"""

    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _RelayConnection:
    """记录 execute 调用，按 SQL 关键字返回不同模拟结果。

    - SELECT ... gw_token → self._token_row
    - SELECT ... gw_channel → self._channel_rows
    - INSERT → 空结果（_log_usage）
    """

    def __init__(self, token_row=None, channel_rows=None):
        self._token_row = token_row
        self._channel_rows = channel_rows or []
        self.calls: list[str] = []

    async def execute(self, query, params=()):
        self.calls.append(query)
        upper = query.upper()
        if "GW_TOKEN" in upper:
            return _Result(row=self._token_row)
        if "GW_CHANNEL" in upper:
            return _Result(rows=self._channel_rows)
        return _Result()

    async def commit(self):
        return None


class _Pool:
    """模拟 psycopg AsyncConnectionPool。"""

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


def _mock_channel(
    provider="openai",
    base_url="https://api.openai.com/v1",
    api_key="sk-upstream-secret",
    upstream_model="gpt-4o-mini",
    channel_id="chn_test_001",
):
    """构造一个 resolve_route 返回的渠道字典。"""
    return {
        "id": channel_id,
        "provider": provider,
        "base_url": base_url,
        "api_key": api_key,
        "weight": 100,
        "upstream_model": upstream_model,
        "pinned": False,
    }


def _resolve_result(channel=None, workspace_id="wsp_test", token_id="tok_test_001"):
    """构造 resolve_route 的返回值。"""
    channel = channel or _mock_channel()
    return {
        "workspace_id": workspace_id,
        "token_id": token_id,
        "group_id": None,
        "rpm_limit": 1000,
        "tpm_limit": 1000000,
        "group_rpm_limit": 0,
        "group_tpm_limit": 0,
        "channel": channel,
        "channels": [channel],
        "fallbacks": [],
    }


def _install_resolve(monkeypatch, *, result=None, raises=None):
    """替换 relay.resolve_route。"""

    async def _fake_resolve(body):
        if raises is not None:
            raise raises
        return result or _resolve_result()

    monkeypatch.setattr(relay, "resolve_route", _fake_resolve)


def _install_pool(monkeypatch, *, token_row=None, channel_rows=None):
    """替换 relay.pool。"""
    conn = _RelayConnection(token_row=token_row, channel_rows=channel_rows)
    monkeypatch.setattr(relay, "pool", _Pool(conn))
    return conn


def _app():
    """构造挂载了 relay router 的 FastAPI 应用。"""
    app = FastAPI()
    app.include_router(relay.router)
    return app


# ----------------------------------------------------------------------
# 1. 认证：无 Authorization / 无效令牌
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_authorization_returns_401(monkeypatch):
    """缺少 Authorization 头返回 401，错误结构为 OpenAI 兼容格式。"""
    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 401
    payload = response.json()
    assert payload["error"]["code"] == "E01001"
    assert "Authorization" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_invalid_token_returns_401(monkeypatch):
    """无效令牌（resolve_route 抛 E01001）返回 401。"""
    _install_resolve(
        monkeypatch,
        raises=HTTPException(status_code=401, detail="E01001"),
    )
    _install_pool(monkeypatch)

    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer sk-wama-invalid"},
        )
    assert response.status_code == 401
    payload = response.json()
    assert payload["error"]["code"] == "E01001"


# ----------------------------------------------------------------------
# 2. 模型不存在返回 404 (E01006)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_not_found_returns_404(monkeypatch):
    """模型不存在（resolve_route 抛 E01006）返回 404。"""
    _install_resolve(
        monkeypatch,
        raises=HTTPException(status_code=404, detail="E01006"),
    )
    _install_pool(monkeypatch)

    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "nonexistent-model", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer sk-wama-valid"},
        )
    assert response.status_code == 404
    payload = response.json()
    assert payload["error"]["code"] == "E01006"


# ----------------------------------------------------------------------
# 3. 有效请求转发到 mock 渠道（openai 协议）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_request_forwards_to_openai_channel(monkeypatch):
    """有效请求通过 openai 协议转发到上游并返回 200。"""
    upstream_response = _MockResponse(
        status_code=200,
        json_data={
            "id": "chatcmpl-upstream-001",
            "object": "chat.completion",
            "created": 1700000000,
            "model": "gpt-4o-mini",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        },
    )
    _install_fake_httpx(monkeypatch, post_response=upstream_response)
    _install_resolve(monkeypatch, result=_resolve_result())
    _install_pool(monkeypatch)

    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 50,
            },
            headers={"Authorization": "Bearer sk-wama-valid"},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "chatcmpl-upstream-001"
    assert payload["choices"][0]["message"]["content"] == "Hello!"
    assert payload["usage"]["total_tokens"] == 7


# ----------------------------------------------------------------------
# 4. 流式响应
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_response_passthrough(monkeypatch):
    """stream=true 时透传上游 SSE 流。"""
    sse_lines = [
        'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"Hi"},"finish_reason":null}]}',
        'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    stream_response = _MockStreamResponse(status_code=200, lines=sse_lines)
    _install_fake_httpx(monkeypatch, stream_response=stream_response)
    _install_resolve(monkeypatch, result=_resolve_result())
    _install_pool(monkeypatch)

    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
            headers={"Authorization": "Bearer sk-wama-valid"},
        )
    assert response.status_code == 200
    body = response.text
    assert "data: " in body
    assert "[DONE]" in body
    assert "Hi" in body


# ----------------------------------------------------------------------
# 5. GET /v1/models 返回模型列表
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_models_returns_model_list(monkeypatch):
    """GET /v1/models 返回令牌可用模型列表。"""
    token_row = {
        "id": "tok_test_001",
        "workspace_id": "wsp_test",
        "model_whitelist": [],
        "pinned_channel_id": None,
        "group_id": None,
    }
    channel_rows = [
        {"model": "gpt-4o-mini"},
        {"model": "gpt-4o"},
        {"model": "deepseek-chat"},
    ]
    _install_pool(monkeypatch, token_row=token_row, channel_rows=channel_rows)

    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
    ) as client:
        response = await client.get(
            "/v1/models",
            headers={"Authorization": "Bearer sk-wama-valid"},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    model_ids = [m["id"] for m in payload["data"]]
    assert "gpt-4o-mini" in model_ids
    assert "deepseek-chat" in model_ids
    assert len(payload["data"]) == 3


@pytest.mark.asyncio
async def test_get_models_applies_token_whitelist(monkeypatch):
    """令牌的 model_whitelist 会过滤返回的模型列表。"""
    token_row = {
        "id": "tok_test_001",
        "workspace_id": "wsp_test",
        "model_whitelist": ["gpt-4o-mini"],
        "pinned_channel_id": None,
        "group_id": None,
    }
    channel_rows = [
        {"model": "gpt-4o-mini"},
        {"model": "gpt-4o"},
        {"model": "deepseek-chat"},
    ]
    _install_pool(monkeypatch, token_row=token_row, channel_rows=channel_rows)

    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
    ) as client:
        response = await client.get(
            "/v1/models",
            headers={"Authorization": "Bearer sk-wama-valid"},
        )
    assert response.status_code == 200
    model_ids = [m["id"] for m in response.json()["data"]]
    assert model_ids == ["gpt-4o-mini"]


@pytest.mark.asyncio
async def test_get_models_without_auth_returns_401(monkeypatch):
    """GET /v1/models 缺少 Authorization 头返回 401。"""
    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
    ) as client:
        response = await client.get("/v1/models")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "E01001"


@pytest.mark.asyncio
async def test_get_models_invalid_token_returns_401(monkeypatch):
    """GET /v1/models 无效令牌返回 401。"""
    _install_pool(monkeypatch, token_row=None)

    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
    ) as client:
        response = await client.get(
            "/v1/models",
            headers={"Authorization": "Bearer sk-wama-invalid"},
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "E01001"


# ----------------------------------------------------------------------
# 6. 协议适配：openai / anthropic / gemini
# ----------------------------------------------------------------------


def test_openai_protocol_adaptation():
    """openai 协议：直接转发，Authorization Bearer 头。"""
    channel = _mock_channel(provider="openai", api_key="sk-secret")
    url, headers, payload = relay._adapt_request(
        channel, {"model": "gpt-4o-mini", "messages": [], "max_tokens": 10}, "openai"
    )
    assert url == "https://api.openai.com/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-secret"
    assert headers["Content-Type"] == "application/json"
    assert payload["model"] == "gpt-4o-mini"
    assert payload["max_tokens"] == 10


def test_anthropic_protocol_adaptation():
    """anthropic 协议：转换为 Messages 格式，x-api-key 头。"""
    channel = _mock_channel(
        provider="anthropic",
        base_url="https://api.anthropic.com",
        api_key="sk-ant-secret",
        upstream_model="claude-3-5-sonnet-20241022",
    )
    body = {
        "model": "claude-3.5-sonnet",
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ],
        "max_tokens": 100,
        "temperature": 0.7,
    }
    url, headers, payload = relay._adapt_request(channel, body, "anthropic")
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == "sk-ant-secret"
    assert headers["anthropic-version"] == "2023-06-01"
    assert payload["model"] == "claude-3-5-sonnet-20241022"
    assert payload["system"] == "You are helpful."
    assert payload["messages"] == [{"role": "user", "content": "Hi"}]
    assert payload["max_tokens"] == 100
    assert payload["temperature"] == 0.7


def test_gemini_protocol_adaptation():
    """gemini 协议：转换为 generateContent 格式，?key= 参数。"""
    channel = _mock_channel(
        provider="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="AIza-test-key",
        upstream_model="gemini-2.0-flash",
    )
    body = {
        "model": "gemini-2.0-flash",
        "messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ],
        "max_tokens": 256,
        "temperature": 0.5,
    }
    url, headers, payload = relay._adapt_request(channel, body, "gemini")
    assert "models/gemini-2.0-flash:generateContent" in url
    assert "key=AIza-test-key" in url
    assert headers["Content-Type"] == "application/json"
    contents = payload["contents"]
    assert contents[0] == {"role": "user", "parts": [{"text": "Hello"}]}
    assert contents[1] == {"role": "model", "parts": [{"text": "Hi there"}]}
    assert payload["generationConfig"]["maxOutputTokens"] == 256
    assert payload["generationConfig"]["temperature"] == 0.5


def test_anthropic_response_conversion():
    """Anthropic 响应正确转换为 OpenAI ChatCompletion 格式。"""
    anthropic_data = {
        "id": "msg_001",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "Hello from Claude"}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    result = relay._convert_anthropic_response(anthropic_data, "claude-3.5-sonnet")
    assert result["object"] == "chat.completion"
    assert result["choices"][0]["message"]["content"] == "Hello from Claude"
    assert result["choices"][0]["finish_reason"] == "stop"
    assert result["usage"]["prompt_tokens"] == 10
    assert result["usage"]["completion_tokens"] == 5
    assert result["usage"]["total_tokens"] == 15


def test_gemini_response_conversion():
    """Gemini 响应正确转换为 OpenAI ChatCompletion 格式。"""
    gemini_data = {
        "candidates": [
            {
                "content": {"parts": [{"text": "Hello from Gemini"}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 8,
            "candidatesTokenCount": 4,
            "totalTokenCount": 12,
        },
    }
    result = relay._convert_gemini_response(gemini_data, "gemini-2.0-flash")
    assert result["object"] == "chat.completion"
    assert result["choices"][0]["message"]["content"] == "Hello from Gemini"
    assert result["choices"][0]["finish_reason"] == "stop"
    assert result["usage"]["prompt_tokens"] == 8
    assert result["usage"]["completion_tokens"] == 4
    assert result["usage"]["total_tokens"] == 12


# ----------------------------------------------------------------------
# 7. anthropic / gemini 协议的非流式转发
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_non_stream_forward(monkeypatch):
    """anthropic 协议渠道：转发并转换响应为 OpenAI 格式。"""
    upstream_response = _MockResponse(
        status_code=200,
        json_data={
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Hi from Claude"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )
    _install_fake_httpx(monkeypatch, post_response=upstream_response)
    _install_resolve(
        monkeypatch,
        result=_resolve_result(
            channel=_mock_channel(
                provider="anthropic",
                base_url="https://api.anthropic.com",
                api_key="sk-ant-secret",
                upstream_model="claude-3-5-sonnet-20241022",
            )
        ),
    )
    _install_pool(monkeypatch)

    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "claude-3.5-sonnet",
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 50,
            },
            headers={"Authorization": "Bearer sk-wama-valid"},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["content"] == "Hi from Claude"
    assert payload["usage"]["prompt_tokens"] == 10
    assert payload["usage"]["completion_tokens"] == 5


@pytest.mark.asyncio
async def test_gemini_non_stream_forward(monkeypatch):
    """gemini 协议渠道：转发并转换响应为 OpenAI 格式。"""
    upstream_response = _MockResponse(
        status_code=200,
        json_data={
            "candidates": [
                {
                    "content": {"parts": [{"text": "Hi from Gemini"}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 8,
                "candidatesTokenCount": 4,
                "totalTokenCount": 12,
            },
        },
    )
    _install_fake_httpx(monkeypatch, post_response=upstream_response)
    _install_resolve(
        monkeypatch,
        result=_resolve_result(
            channel=_mock_channel(
                provider="gemini",
                base_url="https://generativelanguage.googleapis.com/v1beta",
                api_key="AIza-test-key",
                upstream_model="gemini-2.0-flash",
            )
        ),
    )
    _install_pool(monkeypatch)

    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-2.0-flash",
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 50,
            },
            headers={"Authorization": "Bearer sk-wama-valid"},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["content"] == "Hi from Gemini"
    assert payload["usage"]["total_tokens"] == 12


# ----------------------------------------------------------------------
# 8. 路由契约
# ----------------------------------------------------------------------


def test_relay_router_exposes_openai_compatible_endpoints():
    """router 注册了 POST /v1/chat/completions 与 GET /v1/models。"""
    paths = {
        (route.path, tuple(sorted(route.methods or ())))
        for route in relay.router.routes
    }
    assert ("/v1/chat/completions", ("POST",)) in paths
    assert ("/v1/models", ("GET",)) in paths
    assert relay.router.prefix == "/v1"

# ----------------------------------------------------------------------
# 9. 端到端集成测试：完整流程验证
# ----------------------------------------------------------------------


class TestRelayEndToEnd:
    """转发层端到端集成测试（使用 mock 渠道验证完整流程）。

    覆盖：令牌认证 → 模型解析 → 协议适配 → 渠道转发 → 响应返回 → 计量记录
    所有外部依赖（pool / resolve_route / httpx）均通过 monkeypatch 隔离。
    """

    @pytest.mark.asyncio
    async def test_e2e_full_flow_with_usage_logging(self, monkeypatch):
        """完整流程：认证→解析→转发→响应→计量记录全部贯通。"""
        upstream_response = _MockResponse(
            status_code=200,
            json_data={
                "id": "chatcmpl-e2e-001",
                "object": "chat.completion",
                "created": 1700000000,
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "E2E works!"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )
        _install_fake_httpx(monkeypatch, post_response=upstream_response)
        _install_resolve(monkeypatch, result=_resolve_result())
        conn = _install_pool(monkeypatch)

        app = _app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "max_tokens": 50,
                },
                headers={"Authorization": "Bearer sk-wama-valid"},
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload["id"] == "chatcmpl-e2e-001"
        assert payload["choices"][0]["message"]["content"] == "E2E works!"
        assert payload["usage"]["total_tokens"] == 15
        insert_calls = [c for c in conn.calls if "INSERT INTO gw_request_log" in c]
        assert len(insert_calls) == 1

    @pytest.mark.asyncio
    async def test_e2e_streaming_with_usage_logging(self, monkeypatch):
        """流式响应端到端：SSE 透传并在流结束后记录用量。"""
        sse_lines = [
            'data: {"id":"chatcmpl-stream-e2e","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"Stream"},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-stream-e2e","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":8,"completion_tokens":3,"total_tokens":11}}',
            "data: [DONE]",
        ]
        stream_response = _MockStreamResponse(status_code=200, lines=sse_lines)
        _install_fake_httpx(monkeypatch, stream_response=stream_response)
        _install_resolve(monkeypatch, result=_resolve_result())
        conn = _install_pool(monkeypatch)

        app = _app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
                headers={"Authorization": "Bearer sk-wama-valid"},
            )
        assert response.status_code == 200
        body = response.text
        assert "data: " in body
        assert "[DONE]" in body
        assert "Stream" in body
        insert_calls = [c for c in conn.calls if "INSERT INTO gw_request_log" in c]
        assert len(insert_calls) == 1

    @pytest.mark.asyncio
    async def test_e2e_models_list_aggregates_multiple_channels(self, monkeypatch):
        """GET /v1/models 端到端：聚合多个渠道的模型并返回 OpenAI 兼容结构。"""
        token_row = {
            "id": "tok_e2e",
            "workspace_id": "wsp_e2e",
            "model_whitelist": [],
            "pinned_channel_id": None,
            "group_id": None,
        }
        channel_rows = [
            {"model": "gpt-4o-mini"},
            {"model": "gpt-4o"},
            {"model": "claude-3.5-sonnet"},
            {"model": "deepseek-chat"},
            {"model": "qwen-max"},
        ]
        _install_pool(monkeypatch, token_row=token_row, channel_rows=channel_rows)

        app = _app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
        ) as client:
            response = await client.get(
                "/v1/models",
                headers={"Authorization": "Bearer sk-wama-valid"},
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload["object"] == "list"
        model_ids = [m["id"] for m in payload["data"]]
        assert len(model_ids) == 5
        assert set(model_ids) == {
            "gpt-4o-mini", "gpt-4o", "claude-3.5-sonnet", "deepseek-chat", "qwen-max"
        }
        for m in payload["data"]:
            assert m["object"] == "model"
            assert m["owned_by"] == "workama"

    @pytest.mark.asyncio
    async def test_e2e_anthropic_protocol_full_flow(self, monkeypatch):
        """Anthropic 协议端到端：请求适配→转发→响应转换→计量记录。"""
        upstream_response = _MockResponse(
            status_code=200,
            json_data={
                "id": "msg_e2e_anthropic",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Anthropic E2E"}],
                "usage": {"input_tokens": 12, "output_tokens": 6},
            },
        )
        _install_fake_httpx(monkeypatch, post_response=upstream_response)
        _install_resolve(
            monkeypatch,
            result=_resolve_result(
                channel=_mock_channel(
                    provider="anthropic",
                    base_url="https://api.anthropic.com",
                    api_key="sk-ant-e2e",
                    upstream_model="claude-3-5-sonnet-20241022",
                )
            ),
        )
        conn = _install_pool(monkeypatch)

        app = _app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "claude-3.5-sonnet",
                    "messages": [
                        {"role": "system", "content": "Be helpful."},
                        {"role": "user", "content": "Hi"},
                    ],
                    "max_tokens": 100,
                },
                headers={"Authorization": "Bearer sk-wama-valid"},
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload["object"] == "chat.completion"
        assert payload["choices"][0]["message"]["content"] == "Anthropic E2E"
        assert payload["usage"]["prompt_tokens"] == 12
        assert payload["usage"]["completion_tokens"] == 6
        assert payload["usage"]["total_tokens"] == 18
        insert_calls = [c for c in conn.calls if "INSERT INTO gw_request_log" in c]
        assert len(insert_calls) == 1

    @pytest.mark.asyncio
    async def test_e2e_gemini_protocol_full_flow(self, monkeypatch):
        """Gemini 协议端到端：请求适配→转发→响应转换→计量记录。"""
        upstream_response = _MockResponse(
            status_code=200,
            json_data={
                "candidates": [
                    {
                        "content": {"parts": [{"text": "Gemini E2E"}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 9,
                    "candidatesTokenCount": 4,
                    "totalTokenCount": 13,
                },
            },
        )
        _install_fake_httpx(monkeypatch, post_response=upstream_response)
        _install_resolve(
            monkeypatch,
            result=_resolve_result(
                channel=_mock_channel(
                    provider="gemini",
                    base_url="https://generativelanguage.googleapis.com/v1beta",
                    api_key="AIza-e2e-key",
                    upstream_model="gemini-2.0-flash",
                )
            ),
        )
        conn = _install_pool(monkeypatch)

        app = _app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gemini-2.0-flash",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "max_tokens": 80,
                },
                headers={"Authorization": "Bearer sk-wama-valid"},
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload["object"] == "chat.completion"
        assert payload["choices"][0]["message"]["content"] == "Gemini E2E"
        assert payload["usage"]["total_tokens"] == 13
        insert_calls = [c for c in conn.calls if "INSERT INTO gw_request_log" in c]
        assert len(insert_calls) == 1

    @pytest.mark.asyncio
    async def test_e2e_upstream_http_error_returns_502(self, monkeypatch):
        """上游返回 HTTP 5xx 时，转发层返回 502 (E01051) 并记录用量。"""
        upstream_response = _MockResponse(
            status_code=500,
            json_data={"error": "internal"},
            text="Internal Server Error",
        )
        _install_fake_httpx(monkeypatch, post_response=upstream_response)
        _install_resolve(monkeypatch, result=_resolve_result())
        conn = _install_pool(monkeypatch)

        app = _app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "max_tokens": 10,
                },
                headers={"Authorization": "Bearer sk-wama-valid"},
            )
        assert response.status_code == 502
        payload = response.json()
        assert payload["error"]["code"] == "E01051"
        insert_calls = [c for c in conn.calls if "INSERT INTO gw_request_log" in c]
        assert len(insert_calls) == 1

    @pytest.mark.asyncio
    async def test_e2e_upstream_connection_error_returns_502(self, monkeypatch):
        """上游连接异常 (httpx.HTTPError) 时返回 502 (E01050) 并记录用量。"""

        class _ErrorAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, url, headers=None, json=None):
                raise httpx.ConnectError("connection refused")

            def stream(self, method, url, headers=None, json=None):
                raise httpx.ConnectError("connection refused")

        fake = SimpleNamespace(AsyncClient=_ErrorAsyncClient, HTTPError=httpx.HTTPError)
        monkeypatch.setattr(relay, "httpx", fake)
        _install_resolve(monkeypatch, result=_resolve_result())
        conn = _install_pool(monkeypatch)

        app = _app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "max_tokens": 10,
                },
                headers={"Authorization": "Bearer sk-wama-valid"},
            )
        assert response.status_code == 502
        payload = response.json()
        assert payload["error"]["code"] == "E01050"
        insert_calls = [c for c in conn.calls if "INSERT INTO gw_request_log" in c]
        assert len(insert_calls) == 1

    @pytest.mark.asyncio
    async def test_e2e_invalid_json_body_returns_400(self, monkeypatch):
        """请求体非 JSON 时返回 400 (E01007)。"""
        _install_resolve(monkeypatch, result=_resolve_result())
        _install_pool(monkeypatch)

        app = _app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                content="not a json",
                headers={
                    "Authorization": "Bearer sk-wama-valid",
                    "Content-Type": "application/json",
                },
            )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "E01007"

    @pytest.mark.asyncio
    async def test_e2e_missing_model_field_returns_404(self, monkeypatch):
        """请求体缺少 model 字段时返回 404 (E01006)。"""
        _install_resolve(monkeypatch, result=_resolve_result())
        _install_pool(monkeypatch)

        app = _app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "Hi"}]},
                headers={"Authorization": "Bearer sk-wama-valid"},
            )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "E01006"

    @pytest.mark.asyncio
    async def test_e2e_resolve_failure_logs_usage_with_error_code(self, monkeypatch):
        """模型解析失败时仍记录用量（含错误码）并返回相应错误。"""
        _install_resolve(
            monkeypatch,
            raises=HTTPException(status_code=404, detail="E01006"),
        )
        conn = _install_pool(monkeypatch)

        app = _app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "nonexistent-model",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
                headers={"Authorization": "Bearer sk-wama-valid"},
            )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "E01006"
        insert_calls = [c for c in conn.calls if "INSERT INTO gw_request_log" in c]
        assert len(insert_calls) == 1

    @pytest.mark.asyncio
    async def test_e2e_non_openai_stream_synthesizes_sse(self, monkeypatch):
        """非 openai 协议请求 stream=true 时，合成单条 SSE 流返回。"""
        upstream_response = _MockResponse(
            status_code=200,
            json_data={
                "id": "msg_e2e_stream_synth",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Synthesized stream"}],
                "usage": {"input_tokens": 7, "output_tokens": 3},
            },
        )
        _install_fake_httpx(monkeypatch, post_response=upstream_response)
        _install_resolve(
            monkeypatch,
            result=_resolve_result(
                channel=_mock_channel(
                    provider="anthropic",
                    base_url="https://api.anthropic.com",
                    api_key="sk-ant-synth",
                    upstream_model="claude-3-5-sonnet-20241022",
                )
            ),
        )
        conn = _install_pool(monkeypatch)

        app = _app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "claude-3.5-sonnet",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "max_tokens": 50,
                    "stream": True,
                },
                headers={"Authorization": "Bearer sk-wama-valid"},
            )
        assert response.status_code == 200
        body = response.text
        assert "data: " in body
        assert "[DONE]" in body
        assert "Synthesized stream" in body
        insert_calls = [c for c in conn.calls if "INSERT INTO gw_request_log" in c]
        assert len(insert_calls) == 1
