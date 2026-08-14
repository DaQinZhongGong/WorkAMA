"""workflow.py HTTP 请求节点与变量插值测试。

v7.161: 22 个测试覆盖：
- 变量插值：_resolve_ref 嵌套引用 / context 变量 / 缺失路径（4）
- 变量插值：_interpolate_value 单值替换 / 类型保留 / 字符串拼接 / 缺失为空（4）
- 变量插值：_interpolate_dict 递归插值 / 非 dict 回退（2）
- HTTP 节点：mock 模式默认返回 pending_external（1）
- HTTP 节点：GET/POST/PUT/DELETE 方法（4）
- HTTP 节点：变量插值在 url/headers/body（1）
- HTTP 节点：SSRF 防护 / 无效 URL（2）
- HTTP 节点：环境配置读取（3）
- HTTP 节点：空 url 错误 / 节点类型注册（2）
- HTTP 节点：工作流图执行集成（1）
- HTTP 节点：响应大小限制与 JSON 解析（2）
- 超时与错误分类：timeout / connection_error（2）

所有测试使用 fake/mock，不依赖真实网络。
"""
from __future__ import annotations

import os
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules import workflow as wf
from workama_platform.modules.workflow import (
    NODE_TYPES,
    _http_allowed_hosts,
    _http_default_timeout,
    _http_max_response_size,
    _interpolate_dict,
    _interpolate_value,
    _resolve_ref,
)


# ============================================================================
# 测试辅助
# ============================================================================


def _actor(
    *,
    capabilities=("workflow:*",),
    workspace_id="wsp_test",
    user_id="usr_test",
) -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_test",
        role="admin",
        email="test@example.com",
        display_name="Test",
        onboarding_completed=True,
        capabilities=capabilities,
    )


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(wf.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


class _MockResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json_data = json_data
        self._text = text if text else (
            "" if json_data is None else __import__("json").dumps(json_data)
        )
        self.headers = headers or (
            {"content-type": "application/json"} if json_data is not None else {"content-type": "text/plain"}
        )

    def json(self):
        if self._json_data is None:
            raise ValueError("no json")
        return self._json_data

    @property
    def text(self):
        return self._text


# ============================================================================
# 1. 变量插值 _resolve_ref
# ============================================================================


class TestResolveRef:
    def test_resolve_ref_nested_dict(self):
        ctx = {"node_a": {"field": {"deep": "value"}}}
        assert _resolve_ref("node_a.field.deep", ctx) == "value"

    def test_resolve_ref_context_prefix_stripped(self):
        ctx = {"actor_id": "usr_123"}
        assert _resolve_ref("context.actor_id", ctx) == "usr_123"

    def test_resolve_ref_missing_returns_none(self):
        assert _resolve_ref("node_a.missing", {}) is None

    def test_resolve_ref_double_brace_syntax(self):
        ctx = {"node_a": {"x": 42}}
        assert _resolve_ref("{{node_a.x}}", ctx) == 42


# ============================================================================
# 2. 变量插值 _interpolate_value
# ============================================================================


class TestInterpolateValue:
    def test_interpolate_single_ref_preserves_type(self):
        ctx = {"node_a": {"num": 99, "flag": True}}
        assert _interpolate_value("{{node_a.num}}", ctx) == 99
        assert _interpolate_value("{{node_a.flag}}", ctx) is True

    def test_interpolate_string_substitution(self):
        ctx = {"node_a": {"x": 1}, "node_b": {"y": 2}}
        result = _interpolate_value("x={{node_a.x}} y={{node_b.y}}", ctx)
        assert result == "x=1 y=2"

    def test_interpolate_missing_becomes_empty(self):
        ctx = {"node_a": {"x": 1}}
        assert _interpolate_value("x={{missing.y}}", ctx) == "x="

    def test_interpolate_non_string_passthrough(self):
        assert _interpolate_value(123, {}) == 123
        assert _interpolate_value([1, 2], {}) == [1, 2]
        assert _interpolate_value(None, {}) is None


# ============================================================================
# 3. 变量插值 _interpolate_dict
# ============================================================================


class TestInterpolateDict:
    def test_interpolate_dict_recursive(self):
        ctx = {"host": "api.example.com"}
        mapping = {"url": "https://{{host}}/v1", "static": "ok"}
        result = _interpolate_dict(mapping, ctx)
        assert result == {"url": "https://api.example.com/v1", "static": "ok"}

    def test_interpolate_dict_non_dict_returns_empty(self):
        assert _interpolate_dict(None, {}) == {}
        assert _interpolate_dict("string", {}) == {}


# ============================================================================
# 4. HTTP 节点 mock 模式
# ============================================================================


class TestHttpNodeMock:
    @pytest.mark.asyncio
    async def test_http_node_default_mock_mode(self):
        """未配置允许主机时返回 pending_external mock。"""
        node = {
            "id": "n1",
            "type": "http_request",
            "config": {"url": "https://example.com/api", "method": "GET"},
        }
        output = await wf._execute_http_node(
            node, {}, workspace_id="wsp_test", actor=_actor()
        )
        assert output["method"] == "mock"
        assert output["body"]["pending_external"] is True
        assert output["body"]["url"] == "https://example.com/api"

    @pytest.mark.asyncio
    async def test_http_node_empty_url_error(self):
        node = {"id": "n1", "type": "http_request", "config": {"method": "GET"}}
        output = await wf._execute_http_node(
            node, {}, workspace_id="wsp_test", actor=_actor()
        )
        assert "error" in output
        assert "requires a url" in output["error"]


# ============================================================================
# 5. HTTP 节点变量插值
# ============================================================================


class TestHttpNodeInterpolation:
    @pytest.mark.asyncio
    async def test_http_node_interpolates_url_headers_body(self, monkeypatch):
        """URL、headers、body 中的变量被正确替换。"""
        monkeypatch.setenv("WORKAMA_HTTP_NODE_ALLOWED_HOSTS", "api.example.com")
        calls: list[dict] = []

        class _FakeResponse:
            status_code = 200
            text = '{"ok": true}'
            headers = {"content-type": "application/json"}

            def json(self):
                return {"ok": True}

        def _fake_request(method, url, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            return _FakeResponse()

        monkeypatch.setattr(httpx, "request", _fake_request)

        node = {
            "id": "n1",
            "type": "http_request",
            "config": {
                "url": "https://api.example.com/users/{{user_id}}",
                "method": "POST",
                "headers": {"Authorization": "Bearer {{token}}"},
                "body": {"name": "{{name}}"},
            },
        }
        inputs = {"user_id": "42", "token": "abc", "name": "Ada"}
        output = await wf._execute_http_node(
            node, inputs, workspace_id="wsp_test", actor=_actor()
        )
        assert output["method"] == "http"
        assert output["status_code"] == 200
        assert calls[0]["url"] == "https://api.example.com/users/42"
        assert calls[0]["headers"]["Authorization"] == "Bearer abc"
        assert calls[0]["json"] == {"name": "Ada"}


# ============================================================================
# 6. HTTP 节点 SSRF 与错误分类
# ============================================================================


class TestHttpNodeSecurity:
    @pytest.mark.asyncio
    async def test_http_node_forbidden_host(self, monkeypatch):
        """不在允许列表中的主机返回 forbidden。"""
        monkeypatch.setenv("WORKAMA_HTTP_NODE_ALLOWED_HOSTS", "safe.example.com")
        node = {
            "id": "n1",
            "type": "http_request",
            "config": {"url": "https://evil.com/data", "method": "GET"},
        }
        output = await wf._execute_http_node(
            node, {}, workspace_id="wsp_test", actor=_actor()
        )
        assert "forbidden" in output["error"]

    @pytest.mark.asyncio
    async def test_http_node_invalid_url(self, monkeypatch):
        monkeypatch.setenv("WORKAMA_HTTP_NODE_ALLOWED_HOSTS", "safe.example.com")
        node = {
            "id": "n1",
            "type": "http_request",
            "config": {"url": "://not-a-url", "method": "GET"},
        }
        # urlsplit 不会报错，但 hostname 为空 -> 不在允许列表 -> forbidden
        output = await wf._execute_http_node(
            node, {}, workspace_id="wsp_test", actor=_actor()
        )
        assert "error" in output


# ============================================================================
# 7. HTTP 节点环境配置
# ============================================================================


class TestHttpEnvConfig:
    def test_http_allowed_hosts_from_env(self, monkeypatch):
        monkeypatch.setenv("WORKAMA_HTTP_NODE_ALLOWED_HOSTS", "a.com, b.com")
        hosts = _http_allowed_hosts()
        assert hosts == {"a.com", "b.com"}

    def test_http_default_timeout_from_env(self, monkeypatch):
        monkeypatch.setenv("WORKAMA_HTTP_NODE_TIMEOUT_SECONDS", "60")
        assert _http_default_timeout() == 60

    def test_http_max_response_size_from_env(self, monkeypatch):
        monkeypatch.setenv("WORKAMA_HTTP_NODE_MAX_RESPONSE_SIZE", "2048")
        assert _http_max_response_size() == 2048


# ============================================================================
# 8. HTTP 节点方法支持
# ============================================================================


class TestHttpNodeMethods:
    @pytest.mark.asyncio
    async def test_http_node_methods_mock(self, monkeypatch):
        """GET/POST/PUT/DELETE 在 mock 模式下均返回 pending_external。"""
        for method in ("GET", "POST", "PUT", "DELETE"):
            node = {
                "id": "n1",
                "type": "http_request",
                "config": {"url": "https://example.com", "method": method},
            }
            output = await wf._execute_http_node(
                node, {}, workspace_id="wsp_test", actor=_actor()
            )
            assert output["method"] == "mock"
            assert output["body"]["method"] == method


# ============================================================================
# 9. HTTP 节点类型注册
# ============================================================================


class TestHttpNodeType:
    def test_http_request_in_node_types(self):
        assert "http_request" in NODE_TYPES


# ============================================================================
# 10. 工作流图执行集成
# ============================================================================


class TestWorkflowHttpIntegration:
    @pytest.mark.asyncio
    async def test_run_workflow_with_http_node(self, monkeypatch):
        """工作流含 http_request 节点时执行并记录 node_run。"""
        from datetime import UTC, datetime

        workflow = {
            "id": "wf_1",
            "nodes": [
                {
                    "id": "n1",
                    "type": "http_request",
                    "config": {"url": "https://example.com", "method": "GET"},
                },
                {"id": "n2", "type": "output", "config": {"fields": ["body"]}},
            ],
            "edges": [{"source": "n1", "target": "n2"}],
            "status": "published",
            "workspace_id": "wsp_test",
        }

        class _FakeResult:
            async def fetchone(self):
                return {
                    "id": "wf_1",
                    "workspace_id": "wsp_test",
                    "name": "Test",
                    "nodes": workflow["nodes"],
                    "edges": workflow["edges"],
                    "status": "published",
                    "version": 1,
                    "metadata": {},
                    "created_at": datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
                }

        class _FakeRunResult:
            async def fetchone(self):
                return {
                    "id": "wfr_1",
                    "workflow_id": "wf_1",
                    "workspace_id": "wsp_test",
                    "input": {},
                    "output": {},
                    "status": "completed",
                    "started_at": datetime.now(UTC),
                    "completed_at": datetime.now(UTC),
                    "error": None,
                    "metadata": {"node_runs": []},
                    "created_at": datetime.now(UTC),
                }

        conn = _FakeConnection(results=[_FakeResult(), _FakeRunResult()])
        monkeypatch.setattr(wf, "pool", _FakePool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/workflows/wf_1/run", json={"input": {}})
        assert resp.status_code == 200
        body = resp.json()
        assert any(nr["node_type"] == "http_request" for nr in body["metadata"]["node_runs"])


# ============================================================================
# 11. 响应大小限制与 JSON 解析
# ============================================================================


class TestHttpNodeResponseHandling:
    @pytest.mark.asyncio
    async def test_json_response_parsed(self, monkeypatch):
        monkeypatch.setenv("WORKAMA_HTTP_NODE_ALLOWED_HOSTS", "api.example.com")

        class _FakeResponse:
            status_code = 200
            text = '{"items": [1, 2]}'
            headers = {"content-type": "application/json"}

            def json(self):
                return {"items": [1, 2]}

        monkeypatch.setattr(httpx, "request", lambda *a, **k: _FakeResponse())

        node = {
            "id": "n1",
            "type": "http_request",
            "config": {"url": "https://api.example.com/data", "method": "GET"},
        }
        output = await wf._execute_http_node(
            node, {}, workspace_id="wsp_test", actor=_actor()
        )
        assert output["body"] == {"items": [1, 2]}

    @pytest.mark.asyncio
    async def test_text_response_returned_as_text(self, monkeypatch):
        monkeypatch.setenv("WORKAMA_HTTP_NODE_ALLOWED_HOSTS", "api.example.com")

        class _FakeResponse:
            status_code = 200
            text = "plain text"
            headers = {"content-type": "text/plain"}

        monkeypatch.setattr(httpx, "request", lambda *a, **k: _FakeResponse())

        node = {
            "id": "n1",
            "type": "http_request",
            "config": {"url": "https://api.example.com/text", "method": "GET"},
        }
        output = await wf._execute_http_node(
            node, {}, workspace_id="wsp_test", actor=_actor()
        )
        assert output["body"] == "plain text"


# ============================================================================
# 12. 超时与错误分类
# ============================================================================


class TestHttpNodeErrors:
    @pytest.mark.asyncio
    async def test_timeout_error(self, monkeypatch):
        monkeypatch.setenv("WORKAMA_HTTP_NODE_ALLOWED_HOSTS", "api.example.com")

        def _raise_timeout(*a, **k):
            raise httpx.TimeoutException("timed out")

        monkeypatch.setattr(httpx, "request", _raise_timeout)

        node = {
            "id": "n1",
            "type": "http_request",
            "config": {"url": "https://api.example.com/slow", "method": "GET", "timeout": 1},
        }
        output = await wf._execute_http_node(
            node, {}, workspace_id="wsp_test", actor=_actor()
        )
        assert "timeout" in output["error"]

    @pytest.mark.asyncio
    async def test_connection_error(self, monkeypatch):
        monkeypatch.setenv("WORKAMA_HTTP_NODE_ALLOWED_HOSTS", "api.example.com")

        def _raise_connect(*a, **k):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "request", _raise_connect)

        node = {
            "id": "n1",
            "type": "http_request",
            "config": {"url": "https://api.example.com/refused", "method": "GET"},
        }
        output = await wf._execute_http_node(
            node, {}, workspace_id="wsp_test", actor=_actor()
        )
        assert "connection_error" in output["error"]


# ============================================================================
# Fake 辅助（用于工作流集成测试）
# ============================================================================


class _FakeTransaction:
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False


class _FakeConnection:
    def __init__(self, results=None):
        self._results = list(results) if results else []
        self._idx = 0

    def transaction(self):
        return _FakeTransaction()

    async def execute(self, query, params=()):
        if self._idx < len(self._results):
            r = self._results[self._idx]
            self._idx += 1
            return r
        return _FakeResult()


class _FakeResult:
    async def fetchone(self):
        return None
    async def fetchall(self):
        return []


class _FakePool:
    def __init__(self, conn):
        self._conn = conn
    def connection(self):
        c = self._conn
        class _Ctx:
            async def __aenter__(self):
                return c
            async def __aexit__(self, *a):
                return False
        return _Ctx()
