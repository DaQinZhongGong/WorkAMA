"""WorkAMA Python SDK 单元测试。

通过替换 :class:`WorkAMAClient` 的 ``_opener`` 字段来 mock HTTP 层，
不发起真实网络请求，也不依赖任何第三方 mock 库。
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any, Dict, List, Optional, Tuple

import pytest

from workama_sdk import (
    AuthenticationError,
    NotFoundError,
    RateLimitError,
    WorkAMAClient,
    WorkAMAError,
)


# ---------------------------------------------------------------------------
# Mock 基础设施
# ---------------------------------------------------------------------------


class MockResponse(io.BytesIO):
    """模拟 ``urllib.response`` 的响应对象。"""

    def __init__(self, body: bytes, status: int = 200, headers: Optional[Dict[str, str]] = None):
        super().__init__(body)
        self.status = status
        self.code = status
        self._headers = headers or {}

    # urllib 通过 .headers / .info() 读取，这里只做最小实现
    def info(self):  # noqa: D401 - 简单 mock
        return self._headers

    def read(self, *args, **kwargs):  # type: ignore[override]
        return super().read(*args, **kwargs)


class MockOpener:
    """替代 ``urllib.request.OpenerDirector``，按记录的请求返回预设响应。"""

    def __init__(self) -> None:
        self.calls: List[Tuple[Any, ...]] = []
        self.responses: List[Any] = []  # 每个元素可以是 MockResponse 或异常实例
        self._default_response: Any = None

    def queue(self, response: Any) -> None:
        """入队一个响应（MockResponse 或异常）。"""
        self.responses.append(response)

    def set_default(self, response: Any) -> None:
        """设置默认兜底响应（队列为空时使用）。"""
        self._default_response = response

    def open(self, request: urllib.request.Request, timeout: Optional[float] = None):
        self.calls.append(request)
        if self.responses:
            resp = self.responses.pop(0)
        else:
            resp = self._default_response
        if resp is None:
            raise AssertionError("MockOpener 没有可用响应")
        if isinstance(resp, Exception):
            raise resp
        return resp


def make_ok(payload: Any, status: int = 200) -> MockResponse:
    """构造一个 200 的 JSON 响应。"""
    body = json.dumps(payload).encode("utf-8") if not isinstance(payload, (bytes, str)) else (
        payload.encode("utf-8") if isinstance(payload, str) else payload
    )
    return MockResponse(body, status=status)


def make_http_error(status: int, payload: Any, reason: str = "error") -> urllib.error.HTTPError:
    """构造一个 HTTPError，模拟服务端错误响应。"""
    body = json.dumps(payload).encode("utf-8") if not isinstance(payload, (bytes, str)) else (
        payload.encode("utf-8") if isinstance(payload, str) else payload
    )
    url = "http://mock/api/v1/mock"
    return urllib.error.HTTPError(
        url=url,
        code=status,
        msg=reason,
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body),
    )


def make_url_error(reason: str = "connection refused") -> urllib.error.URLError:
    """构造网络层 URLError。"""
    return urllib.error.URLError(reason)


def new_client(
    base_url: str = "http://localhost:20200",
    api_key: Optional[str] = "wk_test",
    access_token: Optional[str] = None,
) -> Tuple[WorkAMAClient, MockOpener]:
    """构造一个绑定了 MockOpener 的客户端。"""
    client = WorkAMAClient(
        base_url=base_url,
        api_key=api_key,
        access_token=access_token,
        timeout=10.0,
    )
    opener = MockOpener()
    client._opener = opener  # type: ignore[assignment]
    return client, opener


def get_req(opener: MockOpener, index: int = 0) -> urllib.request.Request:
    """取出第 index 次请求对象。"""
    return opener.calls[index]


def header_value(req: urllib.request.Request, name: str) -> Optional[str]:
    """大小写不敏感地从 Request 上读取一个 header 值。

    urllib 内部会对 header 名做 ``str.capitalize()`` 转换，
    例如 ``X-WorkAMA-API-Key`` 会被存为 ``X-workama-api-key``，
    因此 ``get_header`` 在大小写不匹配时会返回 None。
    这里直接遍历 ``req.headers`` 做小写比较，避免对存储格式产生依赖。
    """
    target = name.lower()
    for key, val in req.headers.items():
        if key.lower() == target:
            return val
    return None


# ---------------------------------------------------------------------------
# 初始化与鉴权 header
# ---------------------------------------------------------------------------


class TestInit:
    def test_init_with_api_key_sets_header(self):
        client, opener = new_client(api_key="wk_secret")
        opener.set_default(make_ok({}))
        client.list_agents()
        req = get_req(opener)
        # 大小写不敏感比较，避免依赖 urllib 的 capitalize 存储格式
        assert header_value(req, "X-WorkAMA-API-Key") == "wk_secret"
        assert header_value(req, "Authorization") is None

    def test_init_with_access_token_takes_precedence(self):
        # 同时提供 api_key 与 access_token 时，应使用 Bearer
        client, opener = new_client(api_key="wk_x", access_token="tok_abc")
        opener.set_default(make_ok({}))
        client.list_agents()
        req = get_req(opener)
        assert header_value(req, "Authorization") == "Bearer tok_abc"
        # 不应同时附带 API Key
        assert header_value(req, "X-WorkAMA-API-Key") is None

    def test_init_trims_trailing_slash(self):
        client, opener = new_client(base_url="http://localhost:20200///")
        opener.set_default(make_ok({}))
        client.list_agents()
        req = get_req(opener)
        assert req.full_url == "http://localhost:20200/api/v1/assistants?limit=20"

    def test_user_agent_set(self):
        client, opener = new_client()
        opener.set_default(make_ok({}))
        client.list_agents()
        req = get_req(opener)
        assert header_value(req, "User-Agent") == "workama-sdk-python/0.1.0"


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------


class TestChat:
    def test_chat_success_sends_post_body(self):
        client, opener = new_client(access_token="tok")
        opener.set_default(make_ok({"agent_id": "a1", "message": "hi"}))
        resp = client.chat("a1", "hello", session_id="s1")

        assert resp == {"agent_id": "a1", "message": "hi"}
        req = get_req(opener)
        assert req.get_method() == "POST"
        assert req.full_url == "http://localhost:20200/api/v1/agents/a1/chat"
        body = json.loads(req.data.decode("utf-8"))
        assert body == {"message": "hello", "stream": False, "session_id": "s1"}
        assert header_value(req, "Content-Type") == "application/json"

    def test_chat_without_session_id_omits_field(self):
        client, opener = new_client(access_token="tok")
        opener.set_default(make_ok({"message": "ok"}))
        client.chat("a1", "hi")
        body = json.loads(get_req(opener).data.decode("utf-8"))
        assert "session_id" not in body
        assert body["stream"] is False

    def test_chat_stream_flag_propagated(self):
        client, opener = new_client(access_token="tok")
        opener.set_default(make_ok({"message": "streaming"}))
        client.chat("a1", "hi", stream=True)
        body = json.loads(get_req(opener).data.decode("utf-8"))
        assert body["stream"] is True

    def test_chat_401_raises_authentication_error(self):
        client, opener = new_client(access_token="bad")
        opener.queue(make_http_error(401, {"detail": "invalid token"}))
        with pytest.raises(AuthenticationError) as exc:
            client.chat("a1", "hi")
        assert exc.value.status_code == 401
        assert exc.value.body == {"detail": "invalid token"}


# ---------------------------------------------------------------------------
# list_agents
# ---------------------------------------------------------------------------


class TestListAgents:
    def test_list_agents_default_pagination(self):
        client, opener = new_client()
        payload = {"items": [{"id": "a1"}], "next_cursor": "cur_1", "total": 1}
        opener.set_default(make_ok(payload))
        resp = client.list_agents()
        assert resp == payload
        req = get_req(opener)
        assert req.get_method() == "GET"
        assert "limit=20" in req.full_url

    def test_list_agents_with_cursor(self):
        client, opener = new_client()
        opener.set_default(make_ok({"items": []}))
        client.list_agents(limit=10, cursor="abc")
        url = get_req(opener).full_url
        assert "limit=10" in url
        assert "cursor=abc" in url


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


class TestMemory:
    def test_create_memory_default(self):
        client, opener = new_client()
        opener.set_default(make_ok({"id": "m1"}))
        resp = client.create_memory(content="hello")
        assert resp == {"id": "m1"}
        body = json.loads(get_req(opener).data.decode("utf-8"))
        assert body == {"content": "hello", "importance": 3}
        assert get_req(opener).full_url.endswith("/api/v1/memory-vectors")

    def test_create_memory_with_metadata_and_importance(self):
        client, opener = new_client()
        opener.set_default(make_ok({"id": "m2"}))
        client.create_memory(
            content="prefers dark",
            metadata={"category": "ui"},
            importance=5,
        )
        body = json.loads(get_req(opener).data.decode("utf-8"))
        assert body["importance"] == 5
        assert body["metadata"] == {"category": "ui"}

    def test_recall_memory(self):
        client, opener = new_client()
        opener.set_default(make_ok({"items": [{"content": "x"}]}))
        resp = client.recall_memory(query="q", limit=8)
        assert resp == {"items": [{"content": "x"}]}
        body = json.loads(get_req(opener).data.decode("utf-8"))
        assert body == {"query": "q", "limit": 8}
        assert get_req(opener).full_url.endswith("/api/v1/memory-vectors/recall")


# ---------------------------------------------------------------------------
# knowledge
# ---------------------------------------------------------------------------


class TestKnowledge:
    def test_search_knowledge_with_dataset(self):
        client, opener = new_client()
        opener.set_default(make_ok({"items": [], "total": 0}))
        resp = client.search_knowledge(query="pricing", dataset_id="ds1", limit=5)
        assert resp == {"items": [], "total": 0}
        body = json.loads(get_req(opener).data.decode("utf-8"))
        assert body == {"query": "pricing", "limit": 5, "dataset_id": "ds1"}
        assert get_req(opener).full_url.endswith("/api/v1/knowledge/search")

    def test_search_knowledge_without_dataset(self):
        client, opener = new_client()
        opener.set_default(make_ok({"items": []}))
        client.search_knowledge(query="q")
        body = json.loads(get_req(opener).data.decode("utf-8"))
        assert "dataset_id" not in body


# ---------------------------------------------------------------------------
# workflows
# ---------------------------------------------------------------------------


class TestWorkflows:
    def test_list_workflows(self):
        client, opener = new_client()
        opener.set_default(make_ok({"items": [{"id": "w1"}]}))
        resp = client.list_workflows(limit=20)
        assert resp == {"items": [{"id": "w1"}]}
        req = get_req(opener)
        assert req.get_method() == "GET"
        assert "limit=20" in req.full_url
        # url 带 query string，不能用 endswith，改用路径前缀检查
        assert "/api/v1/workflows" in req.full_url
        assert req.full_url.startswith("http://localhost:20200/api/v1/workflows?")

    def test_run_workflow(self):
        client, opener = new_client()
        opener.set_default(make_ok({"run_id": "r1", "status": "succeeded"}))
        resp = client.run_workflow("w1", {"topic": "周报"})
        assert resp == {"run_id": "r1", "status": "succeeded"}
        req = get_req(opener)
        assert req.get_method() == "POST"
        assert req.full_url.endswith("/api/v1/workflows/w1/runs")
        body = json.loads(req.data.decode("utf-8"))
        assert body == {"input": {"topic": "周报"}}


# ---------------------------------------------------------------------------
# 错误处理
# ---------------------------------------------------------------------------


class TestErrors:
    def test_404_raises_not_found_error(self):
        client, opener = new_client()
        opener.queue(make_http_error(404, {"detail": "agent not found"}))
        with pytest.raises(NotFoundError) as exc:
            client.chat("missing", "hi")
        assert exc.value.status_code == 404
        assert exc.value.body == {"detail": "agent not found"}

    def test_429_raises_rate_limit_error(self):
        client, opener = new_client()
        opener.queue(make_http_error(429, {"message": "too many requests"}))
        with pytest.raises(RateLimitError) as exc:
            client.list_agents()
        assert exc.value.status_code == 429
        assert "too many requests" in str(exc.value)

    def test_500_raises_generic_workama_error(self):
        client, opener = new_client()
        opener.queue(make_http_error(500, {"error": "boom"}))
        with pytest.raises(WorkAMAError) as exc:
            client.list_agents()
        # 500 应映射到基类异常，而非子类
        assert not isinstance(exc.value, (AuthenticationError, NotFoundError, RateLimitError))
        assert exc.value.status_code == 500
        assert exc.value.body == {"error": "boom"}

    def test_url_error_raises_workama_error_with_null_status(self):
        client, opener = new_client()
        opener.queue(make_url_error("connection refused"))
        with pytest.raises(WorkAMAError) as exc:
            client.list_agents()
        assert exc.value.status_code is None

    def test_error_message_prefers_body_message(self):
        client, opener = new_client()
        opener.queue(make_http_error(401, {"message": "token expired"}, reason="Unauthorized"))
        with pytest.raises(AuthenticationError) as exc:
            client.list_agents()
        # message 应取自 body.message，而非 reason
        assert "token expired" in str(exc.value)


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


class TestClose:
    def test_close_is_noop_and_safe(self):
        client, _ = new_client()
        # 不应抛出异常
        client.close()
        # 重复调用同样安全
        client.close()
