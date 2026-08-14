"""Go 网关反向代理 (relay redirect) 的单元测试。

覆盖 relay.py 中 GATEWAY_GO_ENABLED feature flag 启用时的反向代理行为，
以及禁用时回退到 Python relay 原有逻辑的行为：

- feature flag 启用 (GATEWAY_GO_ENABLED=True)：
  - POST /v1/chat/completions 非流式 → 透传 Go 网关响应
  - POST /v1/chat/completions 流式 (stream=true) → 透传 SSE
  - GET /v1/models → 透传 Go 网关响应
  - Authorization 头透传
  - Go 网关不可用 → 502 (E01050)
  - Go 网关流式不可用 → SSE 错误 + [DONE]
- feature flag 禁用 (GATEWAY_GO_ENABLED=False)：
  - POST /v1/chat/completions → 走 Python relay 原有逻辑
  - GET /v1/models → 走 Python relay 原有逻辑

所有外部依赖（httpx / pool / resolve_route）均通过 monkeypatch 隔离。
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from workama_platform.modules.gateway import relay


# ----------------------------------------------------------------------
# 测试辅助：mock httpx（用于 Go 网关反向代理）
# ----------------------------------------------------------------------


class _MockProxyResponse:
    """模拟 httpx.Response（Go 网关非流式响应）。

    提供 relay._proxy_chat_completions_to_go_gateway 与
    _proxy_models_to_go_gateway 读取的属性：content / status_code / headers。
    """

    def __init__(self, status_code=200, content=b"{}", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {"content-type": "application/json"}

    def json(self):
        import json as _json
        return _json.loads(self.content) if isinstance(self.content, (bytes, bytearray)) else _json.loads(self.content)

    @property
    def is_success(self):
        return 200 <= self.status_code < 400


class _MockProxyStreamResponse:
    """模拟 httpx 流式响应，支持 aiter_lines()。"""

    def __init__(self, status_code=200, lines=None):
        self.status_code = status_code
        self._lines = lines or []

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _MockProxyStreamCM:
    """模拟 client.stream() 返回的异步上下文管理器。"""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *_args):
        return False


class _RecordingAsyncClient:
    """记录 Go 网关代理调用的 httpx.AsyncClient 替身。

    记录 post / get / stream 的 url、headers、content，便于断言透传行为。
    """

    def __init__(self, *, post_response=None, get_response=None,
                 stream_response=None, post_error=None, get_error=None,
                 stream_error=None):
        self._post_response = post_response
        self._get_response = get_response
        self._stream_response = stream_response
        self._post_error = post_error
        self._get_error = get_error
        self._stream_error = stream_error
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, content=None, headers=None):
        self.calls.append({
            "method": "POST", "url": url, "content": content, "headers": headers,
        })
        if self._post_error is not None:
            raise self._post_error
        return self._post_response

    async def get(self, url, headers=None):
        self.calls.append({
            "method": "GET", "url": url, "headers": headers,
        })
        if self._get_error is not None:
            raise self._get_error
        return self._get_response

    def stream(self, method, url, content=None, headers=None):
        self.calls.append({
            "method": "STREAM", "url": url, "content": content, "headers": headers,
        })
        if self._stream_error is not None:
            raise self._stream_error
        return _MockProxyStreamCM(self._stream_response)


def _install_go_gateway_httpx(
    monkeypatch,
    *,
    post_response=None,
    get_response=None,
    stream_response=None,
    post_error=None,
    get_error=None,
    stream_error=None,
):
    """替换 relay.httpx，使 Go 网关反向代理走 mock，返回记录客户端。"""

    client = _RecordingAsyncClient(
        post_response=post_response,
        get_response=get_response,
        stream_response=stream_response,
        post_error=post_error,
        get_error=get_error,
        stream_error=stream_error,
    )

    fake = SimpleNamespace(
        AsyncClient=lambda *a, **kw: client,
        HTTPError=httpx.HTTPError,
    )
    monkeypatch.setattr(relay, "httpx", fake)
    return client


# ----------------------------------------------------------------------
# 测试辅助：mock pool / resolve_route（用于 fallback 路径）
# ----------------------------------------------------------------------


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _RelayConnection:
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
    async def _fake_resolve(body):
        if raises is not None:
            raise raises
        return result or _resolve_result()

    monkeypatch.setattr(relay, "resolve_route", _fake_resolve)


def _install_pool(monkeypatch, *, token_row=None, channel_rows=None):
    conn = _RelayConnection(token_row=token_row, channel_rows=channel_rows)
    monkeypatch.setattr(relay, "pool", _Pool(conn))
    return conn


def _install_upstream_httpx_for_fallback(monkeypatch, *, post_response=None,
                                          stream_response=None):
    """为 fallback 路径安装 httpx mock（与 test_relay.py 一致）。"""

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
            return _MockProxyStreamCM(stream_response)

    fake = SimpleNamespace(AsyncClient=_AsyncClient, HTTPError=httpx.HTTPError)
    monkeypatch.setattr(relay, "httpx", fake)


def _enable_go_gateway(monkeypatch, enabled=True):
    """切换 GATEWAY_GO_ENABLED feature flag。"""
    monkeypatch.setattr(relay, "GATEWAY_GO_ENABLED", enabled)


def _app():
    app = FastAPI()
    app.include_router(relay.router)
    return app


# ----------------------------------------------------------------------
# 1. feature flag 默认值与配置
# ----------------------------------------------------------------------


def test_gateway_go_enabled_flag_is_boolean():
    """GATEWAY_GO_ENABLED 在导入时解析为 bool。"""
    assert isinstance(relay.GATEWAY_GO_ENABLED, bool)


def test_gateway_go_url_default_points_to_gateway_service():
    """GATEWAY_GO_URL 默认指向 docker-compose 的 gateway 服务。"""
    assert relay.GATEWAY_GO_URL == "http://gateway:8080"


# ----------------------------------------------------------------------
# 2. Go 网关启用：POST /v1/chat/completions 非流式透传
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_go_enabled_chat_completions_proxies_non_stream(monkeypatch):
    """GATEWAY_GO_ENABLED=True 时，非流式请求透传到 Go 网关。"""
    _enable_go_gateway(monkeypatch, enabled=True)

    go_response_body = b'{"id":"chatcmpl-go-1","object":"chat.completion","choices":[]}'
    client = _install_go_gateway_httpx(
        monkeypatch,
        post_response=_MockProxyResponse(
            status_code=200,
            content=go_response_body,
            headers={"content-type": "application/json"},
        ),
    )

    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
    ) as http_client:
        response = await http_client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers={"Authorization": "Bearer sk-wama-valid"},
        )

    # 响应透传
    assert response.status_code == 200
    assert response.content == go_response_body
    assert response.headers["content-type"] == "application/json"

    # 调用了 Go 网关，且 URL 正确
    post_calls = [c for c in client.calls if c["method"] == "POST"]
    assert len(post_calls) == 1
    assert post_calls[0]["url"] == "http://gateway:8080/v1/chat/completions"
    # Authorization 头透传
    assert post_calls[0]["headers"]["Authorization"] == "Bearer sk-wama-valid"
    # Content-Type 头透传
    assert "application/json" in post_calls[0]["headers"].get("Content-Type", "")


@pytest.mark.asyncio
async def test_go_enabled_chat_completions_forwards_status_code(monkeypatch):
    """Go 网关返回 4xx/5xx 时，状态码透传给客户端。"""
    _enable_go_gateway(monkeypatch, enabled=True)

    _install_go_gateway_httpx(
        monkeypatch,
        post_response=_MockProxyResponse(
            status_code=429,
            content=b'{"error":{"code":"E01010","message":"rate limited"}}',
            headers={"content-type": "application/json"},
        ),
    )

    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
    ) as http_client:
        response = await http_client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Hi"}]},
            headers={"Authorization": "Bearer sk-wama-valid"},
        )

    assert response.status_code == 429
    assert b"rate limited" in response.content


# ----------------------------------------------------------------------
# 3. Go 网关启用：POST /v1/chat/completions 流式 SSE 透传
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_go_enabled_chat_completions_proxies_stream_sse(monkeypatch):
    """GATEWAY_GO_ENABLED=True 且 stream=true 时，透传 Go 网关 SSE。"""
    _enable_go_gateway(monkeypatch, enabled=True)

    sse_lines = [
        'data: {"id":"chatcmpl-go-stream","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}',
        'data: {"id":"chatcmpl-go-stream","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    client = _install_go_gateway_httpx(
        monkeypatch,
        stream_response=_MockProxyStreamResponse(status_code=200, lines=sse_lines),
    )

    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
    ) as http_client:
        response = await http_client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
            headers={"Authorization": "Bearer sk-wama-valid"},
        )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    body = response.text
    assert "data: " in body
    assert "[DONE]" in body
    assert "Hello" in body

    # 确认走了 stream 路径而非 post
    stream_calls = [c for c in client.calls if c["method"] == "STREAM"]
    post_calls = [c for c in client.calls if c["method"] == "POST"]
    assert len(stream_calls) == 1
    assert len(post_calls) == 0
    assert stream_calls[0]["url"] == "http://gateway:8080/v1/chat/completions"
    assert stream_calls[0]["headers"]["Authorization"] == "Bearer sk-wama-valid"


@pytest.mark.asyncio
async def test_go_enabled_stream_unavailable_returns_sse_error(monkeypatch):
    """Go 网关流式连接异常时，返回 SSE 错误块 + [DONE]。"""
    _enable_go_gateway(monkeypatch, enabled=True)

    _install_go_gateway_httpx(
        monkeypatch,
        stream_error=httpx.ConnectError("go gateway down"),
    )

    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
    ) as http_client:
        response = await http_client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
            headers={"Authorization": "Bearer sk-wama-valid"},
        )

    assert response.status_code == 200
    body = response.text
    assert "E01050" in body
    assert "Go gateway unavailable" in body
    assert "[DONE]" in body


# ----------------------------------------------------------------------
# 4. Go 网关启用：GET /v1/models 透传
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_go_enabled_models_proxies_to_go_gateway(monkeypatch):
    """GATEWAY_GO_ENABLED=True 时，GET /v1/models 透传到 Go 网关。"""
    _enable_go_gateway(monkeypatch, enabled=True)

    go_models_body = (
        b'{"object":"list","data":[{"id":"gpt-4o-mini","object":"model","owned_by":"workama"}]}'
    )
    client = _install_go_gateway_httpx(
        monkeypatch,
        get_response=_MockProxyResponse(
            status_code=200,
            content=go_models_body,
            headers={"content-type": "application/json"},
        ),
    )

    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
    ) as http_client:
        response = await http_client.get(
            "/v1/models",
            headers={"Authorization": "Bearer sk-wama-valid"},
        )

    assert response.status_code == 200
    assert response.content == go_models_body
    get_calls = [c for c in client.calls if c["method"] == "GET"]
    assert len(get_calls) == 1
    assert get_calls[0]["url"] == "http://gateway:8080/v1/models"
    assert get_calls[0]["headers"]["Authorization"] == "Bearer sk-wama-valid"


@pytest.mark.asyncio
async def test_go_enabled_models_unavailable_returns_502(monkeypatch):
    """Go 网关 models 端点不可用时返回 502 (E01050)。"""
    _enable_go_gateway(monkeypatch, enabled=True)

    _install_go_gateway_httpx(
        monkeypatch,
        get_error=httpx.ConnectError("go gateway down"),
    )

    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
    ) as http_client:
        response = await http_client.get(
            "/v1/models",
            headers={"Authorization": "Bearer sk-wama-valid"},
        )

    assert response.status_code == 502
    payload = response.json()
    assert payload["error"]["code"] == "E01050"
    assert "Go gateway unavailable" in payload["error"]["message"]


# ----------------------------------------------------------------------
# 5. Go 网关启用：非流式连接异常返回 502
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_go_enabled_chat_completions_unavailable_returns_502(monkeypatch):
    """Go 网关非流式连接异常时返回 502 (E01050)。"""
    _enable_go_gateway(monkeypatch, enabled=True)

    _install_go_gateway_httpx(
        monkeypatch,
        post_error=httpx.ConnectError("go gateway down"),
    )

    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
    ) as http_client:
        response = await http_client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Hi"}]},
            headers={"Authorization": "Bearer sk-wama-valid"},
        )

    assert response.status_code == 502
    payload = response.json()
    assert payload["error"]["code"] == "E01050"


# ----------------------------------------------------------------------
# 6. Go 网关禁用：回退到 Python relay 原有逻辑
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_go_disabled_chat_completions_uses_python_fallback(monkeypatch):
    """GATEWAY_GO_ENABLED=False 时，走 Python relay 原有逻辑。"""
    _enable_go_gateway(monkeypatch, enabled=False)

    # 安装 Python relay fallback 路径所需的 mock
    upstream_response = _MockProxyResponse(
        status_code=200,
        content=b'{"id":"chatcmpl-fallback","object":"chat.completion","choices":[{"index":0,"message":{"role":"assistant","content":"From Python relay"},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}',
        headers={"content-type": "application/json"},
    )
    # fallback 路径使用 httpx.AsyncClient.post(url, headers=, json=)
    _install_upstream_httpx_for_fallback(monkeypatch, post_response=upstream_response)
    _install_resolve(monkeypatch, result=_resolve_result())
    _install_pool(monkeypatch)

    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
    ) as http_client:
        response = await http_client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 50,
            },
            headers={"Authorization": "Bearer sk-wama-valid"},
        )

    # Python relay fallback 返回 OpenAI 兼容结构
    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "chatcmpl-fallback"
    assert payload["choices"][0]["message"]["content"] == "From Python relay"


@pytest.mark.asyncio
async def test_go_disabled_models_uses_python_fallback(monkeypatch):
    """GATEWAY_GO_ENABLED=False 时，GET /v1/models 走 Python relay 原有逻辑。"""
    _enable_go_gateway(monkeypatch, enabled=False)

    token_row = {
        "id": "tok_fallback",
        "workspace_id": "wsp_fallback",
        "model_whitelist": [],
        "pinned_channel_id": None,
        "group_id": None,
    }
    channel_rows = [{"model": "gpt-4o-mini"}, {"model": "claude-3.5-sonnet"}]
    _install_pool(monkeypatch, token_row=token_row, channel_rows=channel_rows)

    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
    ) as http_client:
        response = await http_client.get(
            "/v1/models",
            headers={"Authorization": "Bearer sk-wama-valid"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    model_ids = [m["id"] for m in payload["data"]]
    assert set(model_ids) == {"gpt-4o-mini", "claude-3.5-sonnet"}


@pytest.mark.asyncio
async def test_go_disabled_chat_completions_missing_token_returns_401(monkeypatch):
    """GATEWAY_GO_ENABLED=False 时，Python relay fallback 仍校验令牌。"""
    _enable_go_gateway(monkeypatch, enabled=False)
    _install_resolve(
        monkeypatch,
        raises=HTTPException(status_code=401, detail="E01001"),
    )
    _install_pool(monkeypatch)

    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
    ) as http_client:
        response = await http_client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Hi"}]},
            headers={"Authorization": "Bearer sk-wama-invalid"},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "E01001"


# ----------------------------------------------------------------------
# 7. Go 网关启用：缺少 Authorization 头仍透传（由 Go 网关校验）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_go_enabled_chat_completions_no_auth_header_still_proxies(monkeypatch):
    """Go 网关启用时，缺失 Authorization 头直接透传（由 Go 网关返回 401）。

    这是 Go 网关接管认证的设计意图——Python relay 不再校验令牌。
    """
    _enable_go_gateway(monkeypatch, enabled=True)

    _install_go_gateway_httpx(
        monkeypatch,
        post_response=_MockProxyResponse(
            status_code=401,
            content=b'{"error":{"code":"E01001","message":"Missing Authorization"}}',
            headers={"content-type": "application/json"},
        ),
    )

    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay.test"
    ) as http_client:
        response = await http_client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Hi"}]},
        )

    # Go 网关返回 401，透传给客户端
    assert response.status_code == 401
    assert b"E01001" in response.content
