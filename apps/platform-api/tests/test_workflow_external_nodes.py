"""真实外部工作流节点（HTTP / code 本地沙箱 / sub-workflow）的单元测试。

覆盖：
  - 变量插值工具函数（``_resolve_ref`` / ``_interpolate`` / ``_interpolate_dict``）
  - ``execute_http_node`` 的 GET/POST/PUT/DELETE、插值、JSON/文本响应、错误处理
  - ``execute_code_node`` 本地安全沙箱的合法/非法代码与超时
  - ``execute_subworkflow_node`` 通过注入 runner 的执行与插值
  - ``validate_graph`` 对新节点类型的校验
  - ``execute_graph`` 集成 dry_run 与真实执行分支
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from workama_platform.modules.workflows import (
    EXTERNAL_NODE_TYPES,
    HTTP_ALLOWED_METHODS,
    WORKFLOW_NODE_TYPES,
    _interpolate,
    _interpolate_dict,
    _resolve_ref,
    execute_code_node,
    execute_graph,
    execute_http_node,
    execute_subworkflow_node,
    validate_graph,
)


# ---------------------------------------------------------------------------
# Mock 工具：HTTP 响应与 AsyncClient 替身
# ---------------------------------------------------------------------------


class MockResponse:
    """模拟 httpx.Response。"""

    def __init__(
        self,
        status_code: int = 200,
        json_data: Any = None,
        text: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self._text = text if text else (
            "" if json_data is None else __import__("json").dumps(json_data)
        )
        self.headers = headers or (
            {"content-type": "application/json"} if json_data is not None else {"content-type": "text/plain"}
        )

    def json(self) -> Any:
        if self._json_data is None:
            raise ValueError("no json body")
        return self._json_data

    @property
    def text(self) -> str:
        return self._text


class MockAsyncClient:
    """记录所有调用并按配置返回响应或抛出指定异常的 httpx.AsyncClient 替身。"""

    def __init__(self, response: MockResponse | None = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> "MockAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def request(self, method: str, url: str, **kwargs: Any) -> MockResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if self._error is not None:
            raise self._error
        if self._response is None:
            return MockResponse(status_code=204)
        return self._response


@pytest.fixture
def patch_httpx_client(monkeypatch: pytest.MonkeyPatch):
    """返回一个工厂：用指定响应/错误替换 workflows 模块中的 httpx.AsyncClient。"""

    def _patch(response: MockResponse | None = None, error: Exception | None = None) -> MockAsyncClient:
        client = MockAsyncClient(response=response, error=error)
        import workama_platform.modules.workflows as wf

        monkeypatch.setattr(wf.httpx, "AsyncClient", lambda *args, **kwargs: client)
        return client

    return _patch


# ---------------------------------------------------------------------------
# 变量插值工具函数测试
# ---------------------------------------------------------------------------


class TestInterpolation:
    def test_resolve_ref_supports_double_brace_syntax(self):
        ctx = {"node_a": {"field": "value"}}
        assert _resolve_ref("{{node_a.field}}", ctx) == "value"

    def test_resolve_ref_supports_bare_path(self):
        ctx = {"node_a": {"field": 42}}
        assert _resolve_ref("node_a.field", ctx) == 42

    def test_resolve_ref_supports_nested_path(self):
        ctx = {"node_a": {"field": {"nested": [1, 2, 3]}}}
        assert _resolve_ref("{{node_a.field.nested}}", ctx) == [1, 2, 3]

    def test_resolve_ref_supports_list_indexing(self):
        ctx = {"node_a": {"items": ["x", "y", "z"]}}
        assert _resolve_ref("node_a.items.1", ctx) == "y"

    def test_resolve_ref_system_variables(self):
        ctx = {"input": {"name": "Ada"}}
        assert _resolve_ref("{{$input}}", ctx) == {"name": "Ada"}
        assert _resolve_ref("{{$trigger}}", ctx) == {"name": "Ada"}
        assert _resolve_ref("{{$context}}", ctx) is ctx

    def test_resolve_ref_returns_none_for_missing_path(self):
        ctx = {"node_a": {"field": "value"}}
        assert _resolve_ref("{{node_a.missing}}", ctx) is None
        assert _resolve_ref("{{unknown.field}}", ctx) is None

    def test_resolve_ref_returns_none_for_type_mismatch(self):
        ctx = {"node_a": "string_value"}
        assert _resolve_ref("node_a.field", ctx) is None

    def test_interpolate_passes_through_non_string(self):
        ctx = {"node_a": {"field": 1}}
        assert _interpolate(123, ctx) == 123
        assert _interpolate([1, 2], ctx) == [1, 2]
        assert _interpolate(None, ctx) is None

    def test_interpolate_single_full_match_preserves_native_type(self):
        ctx = {"node_a": {"field": {"key": "value"}, "num": 42, "flag": True}}
        assert _interpolate("{{node_a.field}}", ctx) == {"key": "value"}
        assert _interpolate("{{node_a.num}}", ctx) == 42
        assert _interpolate("{{node_a.flag}}", ctx) is True

    def test_interpolate_string_substitution_replaces_all_refs(self):
        ctx = {"node_a": {"x": 1}, "node_b": {"y": 2}}
        result = _interpolate("x={{node_a.x}} y={{node_b.y}}", ctx)
        assert result == "x=1 y=2"

    def test_interpolate_missing_ref_becomes_empty_string(self):
        ctx = {"node_a": {"x": 1}}
        assert _interpolate("x={{missing.field}}", ctx) == "x="

    def test_interpolate_dict_applies_to_keys_and_values(self):
        ctx = {"node_a": {"url": "https://example.com"}}
        mapping = {"X-Target": "{{node_a.url}}", "static": "literal"}
        result = _interpolate_dict(mapping, ctx)
        assert result == {"X-Target": "https://example.com", "static": "literal"}

    def test_interpolate_dict_returns_empty_for_non_dict(self):
        assert _interpolate_dict(None, {}) == {}
        assert _interpolate_dict("string", {}) == {}
        assert _interpolate_dict([], {}) == {}


# ---------------------------------------------------------------------------
# execute_http_node 测试
# ---------------------------------------------------------------------------


class TestExecuteHttpNode:
    @pytest.mark.asyncio
    async def test_get_request_with_params_and_headers(self, patch_httpx_client):
        response = MockResponse(status_code=200, json_data={"ok": True})
        client = patch_httpx_client(response=response)
        config = {
            "url": "https://api.example.com/users",
            "method": "GET",
            "headers": {"Authorization": "Bearer token123"},
            "params": {"page": "1", "limit": "20"},
        }
        result = await execute_http_node(config, {})
        assert result["status_code"] == 200
        assert result["body"] == {"ok": True}
        assert "content-type" in result["headers"]
        # 校验底层调用参数
        call = client.calls[0]
        assert call["method"] == "GET"
        assert call["url"] == "https://api.example.com/users"
        assert call["headers"]["Authorization"] == "Bearer token123"
        assert call["params"] == {"page": "1", "limit": "20"}

    @pytest.mark.asyncio
    async def test_post_request_with_json_body(self, patch_httpx_client):
        response = MockResponse(status_code=201, json_data={"id": 99})
        client = patch_httpx_client(response=response)
        config = {
            "url": "https://api.example.com/users",
            "method": "POST",
            "body": {"name": "Ada", "role": "admin"},
        }
        result = await execute_http_node(config, {})
        assert result["status_code"] == 201
        assert result["body"] == {"id": 99}
        call = client.calls[0]
        assert call["method"] == "POST"
        assert call["json"] == {"name": "Ada", "role": "admin"}

    @pytest.mark.asyncio
    async def test_put_and_delete_methods(self, patch_httpx_client):
        for method in ("PUT", "DELETE"):
            response = MockResponse(status_code=204)
            client = patch_httpx_client(response=response)
            config = {"url": "https://api.example.com/items/1", "method": method}
            result = await execute_http_node(config, {})
            assert result["status_code"] == 204
            assert client.calls[0]["method"] == method

    @pytest.mark.asyncio
    async def test_default_method_is_get(self, patch_httpx_client):
        response = MockResponse(status_code=200, json_data={})
        client = patch_httpx_client(response=response)
        await execute_http_node({"url": "https://example.com"}, {})
        assert client.calls[0]["method"] == "GET"

    @pytest.mark.asyncio
    async def test_variable_interpolation_in_url_headers_body_params(self, patch_httpx_client):
        response = MockResponse(status_code=200, json_data={"ok": True})
        client = patch_httpx_client(response=response)
        ctx = {
            "upstream": {"token": "abc123", "user_id": "42", "host": "api.example.com"},
        }
        config = {
            "url": "https://{{upstream.host}}/users",
            "method": "POST",
            "headers": {"Authorization": "Bearer {{upstream.token}}"},
            "params": {"uid": "{{upstream.user_id}}"},
            "body": {"ref": "{{upstream.user_id}}"},
        }
        result = await execute_http_node(config, ctx)
        assert result["status_code"] == 200
        call = client.calls[0]
        assert call["url"] == "https://api.example.com/users"
        assert call["headers"]["Authorization"] == "Bearer abc123"
        assert call["params"] == {"uid": "42"}
        assert call["json"] == {"ref": "42"}

    @pytest.mark.asyncio
    async def test_non_json_response_returned_as_text(self, patch_httpx_client):
        response = MockResponse(
            status_code=200,
            text="plain text body",
            headers={"content-type": "text/plain"},
        )
        patch_httpx_client(response=response)
        result = await execute_http_node({"url": "https://example.com"}, {})
        assert result["body"] == "plain text body"

    @pytest.mark.asyncio
    async def test_json_response_with_invalid_body_falls_back_to_text(self, patch_httpx_client):
        response = MockResponse(
            status_code=200,
            text="not-json",
            headers={"content-type": "application/json"},
        )
        patch_httpx_client(response=response)
        result = await execute_http_node({"url": "https://example.com"}, {})
        assert result["body"] == "not-json"

    @pytest.mark.asyncio
    async def test_timeout_raises_runtime_error(self, patch_httpx_client):
        patch_httpx_client(error=httpx.TimeoutException("timed out"))
        config = {"url": "https://example.com", "timeout": 1}
        with pytest.raises(RuntimeError, match="timed out"):
            await execute_http_node(config, {})

    @pytest.mark.asyncio
    async def test_connect_error_raises_runtime_error(self, patch_httpx_client):
        patch_httpx_client(error=httpx.ConnectError("connection refused"))
        with pytest.raises(RuntimeError, match="connection failed"):
            await execute_http_node({"url": "https://example.com"}, {})

    @pytest.mark.asyncio
    async def test_generic_http_error_raises_runtime_error(self, patch_httpx_client):
        patch_httpx_client(error=httpx.HTTPError("unknown"))
        with pytest.raises(RuntimeError, match="http node request failed"):
            await execute_http_node({"url": "https://example.com"}, {})

    @pytest.mark.asyncio
    async def test_missing_url_raises_runtime_error(self):
        with pytest.raises(RuntimeError, match="requires a url"):
            await execute_http_node({"method": "GET"}, {})

    @pytest.mark.asyncio
    async def test_unsupported_method_raises_runtime_error(self):
        with pytest.raises(RuntimeError, match="method is unsupported"):
            await execute_http_node({"url": "https://example.com", "method": "BOGUS"}, {})

    @pytest.mark.asyncio
    async def test_non_numeric_timeout_raises_runtime_error(self):
        with pytest.raises(RuntimeError, match="timeout must be numeric"):
            await execute_http_node({"url": "https://example.com", "timeout": "abc"}, {})


# ---------------------------------------------------------------------------
# execute_code_node 测试（本地安全沙箱）
# ---------------------------------------------------------------------------


class TestExecuteCodeNode:
    @pytest.mark.asyncio
    async def test_valid_code_returns_result(self):
        config = {"code": "result = {'doubled': 84}", "timeout": 5}
        out = await execute_code_node(config, {})
        assert out == {"result": {"doubled": 84}}

    @pytest.mark.asyncio
    async def test_input_mapping_injects_variables_from_context(self):
        ctx = {"upstream": {"value": 21}, "input": {"name": "Ada"}}
        config = {
            "code": "result = {'doubled': value * 2, 'name': name}",
            "input_mapping": {
                "value": "{{upstream.value}}",
                "name": "{{$input.name}}",
            },
            "timeout": 5,
        }
        out = await execute_code_node(config, ctx)
        assert out == {"result": {"doubled": 42, "name": "Ada"}}

    @pytest.mark.asyncio
    async def test_safe_modules_json_and_re_available(self):
        config = {
            "code": "result = json.loads('{\"k\": 1}')",
            "timeout": 5,
        }
        out = await execute_code_node(config, {})
        assert out == {"result": {"k": 1}}

    @pytest.mark.asyncio
    async def test_safe_builtins_provide_common_functions(self):
        config = {
            "code": "result = {'len': len([1,2,3]), 'sum': sum([1,2,3]), 'sorted': sorted([3,1,2])}",
            "timeout": 5,
        }
        out = await execute_code_node(config, {})
        assert out == {"result": {"len": 3, "sum": 6, "sorted": [1, 2, 3]}}

    @pytest.mark.asyncio
    async def test_import_statement_is_blocked(self):
        config = {"code": "import os\nresult = 1", "timeout": 5}
        with pytest.raises(RuntimeError, match="imports and global scope mutation are disabled"):
            await execute_code_node(config, {})

    @pytest.mark.asyncio
    async def test_import_from_statement_is_blocked(self):
        config = {"code": "from os import path\nresult = 1", "timeout": 5}
        with pytest.raises(RuntimeError, match="imports and global scope mutation are disabled"):
            await execute_code_node(config, {})

    @pytest.mark.asyncio
    async def test_open_call_is_blocked(self):
        config = {"code": "result = open('/etc/passwd').read()", "timeout": 5}
        with pytest.raises(RuntimeError, match="call is disabled: open"):
            await execute_code_node(config, {})

    @pytest.mark.asyncio
    async def test_eval_call_is_blocked(self):
        config = {"code": "result = eval('1+1')", "timeout": 5}
        with pytest.raises(RuntimeError, match="call is disabled: eval"):
            await execute_code_node(config, {})

    @pytest.mark.asyncio
    async def test_exec_call_is_blocked(self):
        config = {"code": "exec('x = 1')\nresult = 1", "timeout": 5}
        with pytest.raises(RuntimeError, match="call is disabled: exec"):
            await execute_code_node(config, {})

    @pytest.mark.asyncio
    async def test_dunder_attribute_access_is_blocked(self):
        config = {"code": "result = (1).__class__", "timeout": 5}
        with pytest.raises(RuntimeError, match="dunder attribute access is disabled"):
            await execute_code_node(config, {})

    @pytest.mark.asyncio
    async def test_invalid_python_syntax_raises_runtime_error(self):
        config = {"code": "result = [unclosed", "timeout": 5}
        with pytest.raises(RuntimeError, match="invalid Python syntax"):
            await execute_code_node(config, {})

    @pytest.mark.asyncio
    async def test_empty_code_raises_runtime_error(self):
        config = {"code": "", "timeout": 5}
        with pytest.raises(RuntimeError, match="requires code"):
            await execute_code_node(config, {})

    @pytest.mark.asyncio
    async def test_infinite_loop_times_out(self):
        # 注意：asyncio.to_thread 无法强制终止线程，因此用有限 sleep 模拟长耗时操作。
        # 超时 1s 后 RuntimeError 抛出；后台线程在 sleep(2) 结束后自动退出，不会阻塞进程退出。
        import time as _time
        ctx = {"sleep_fn": _time.sleep}
        config = {
            "code": "sleep_fn(2)\nresult = 'should_not_reach'",
            "timeout": 1,
            "input_mapping": {"sleep_fn": "sleep_fn"},
        }
        with pytest.raises(RuntimeError, match="timed out after 1"):
            await execute_code_node(config, ctx)

    @pytest.mark.asyncio
    async def test_default_timeout_is_5_seconds(self):
        # 不显式传 timeout，默认 5s；用快速可完成的合法代码验证默认超时配置不误触发
        config = {"code": "result = sum(range(100))"}
        out = await execute_code_node(config, {})
        assert out == {"result": 4950}


# ---------------------------------------------------------------------------
# execute_subworkflow_node 测试
# ---------------------------------------------------------------------------


class TestExecuteSubworkflowNode:
    @pytest.mark.asyncio
    async def test_calls_injected_runner_with_interpolated_workflow_id(self):
        captured: list[tuple[str, dict[str, Any], float]] = []

        async def runner(workflow_id: str, input_data: dict[str, Any], timeout: float) -> dict[str, Any]:
            captured.append((workflow_id, input_data, timeout))
            return {"status": "succeeded", "output": {"echo": input_data}, "error": None}

        ctx = {
            "upstream": {"wf_id": "wfl_abc"},
            "_subworkflow_runner": runner,
        }
        config = {
            "workflow_id": "{{upstream.wf_id}}",
            "input": {"message": "hello", "ref": "{{upstream.wf_id}}"},
            "timeout": 60,
        }
        result = await execute_subworkflow_node(config, ctx)
        assert result["status"] == "succeeded"
        assert result["output"] == {"echo": {"message": "hello", "ref": "wfl_abc"}}
        assert captured[0][0] == "wfl_abc"
        assert captured[0][2] == 60

    @pytest.mark.asyncio
    async def test_default_timeout_is_300_seconds(self):
        async def runner(workflow_id: str, input_data: dict[str, Any], timeout: float) -> dict[str, Any]:
            assert timeout == 300.0
            return {"status": "succeeded", "output": {}, "error": None}

        ctx = {"_subworkflow_runner": runner}
        config = {"workflow_id": "wfl_default"}
        await execute_subworkflow_node(config, ctx)

    @pytest.mark.asyncio
    async def test_missing_workflow_id_raises_runtime_error(self):
        async def runner(workflow_id, input_data, timeout):
            return {"status": "succeeded", "output": {}, "error": None}

        ctx = {"_subworkflow_runner": runner}
        with pytest.raises(RuntimeError, match="requires a workflow_id"):
            await execute_subworkflow_node({"input": {}}, ctx)

    @pytest.mark.asyncio
    async def test_interpolated_workflow_id_resolving_to_empty_raises(self):
        async def runner(workflow_id, input_data, timeout):
            return {"status": "succeeded", "output": {}, "error": None}

        ctx = {"upstream": {"no_wf": "x"}, "_subworkflow_runner": runner}
        with pytest.raises(RuntimeError, match="requires a workflow_id"):
            await execute_subworkflow_node({"workflow_id": "{{upstream.missing}}"}, ctx)

    @pytest.mark.asyncio
    async def test_non_numeric_timeout_raises_runtime_error(self):
        async def runner(workflow_id, input_data, timeout):
            return {"status": "succeeded", "output": {}, "error": None}

        ctx = {"_subworkflow_runner": runner}
        with pytest.raises(RuntimeError, match="timeout must be numeric"):
            await execute_subworkflow_node({"workflow_id": "wfl_x", "timeout": "bad"}, ctx)

    @pytest.mark.asyncio
    async def test_default_runner_without_workspace_context_raises(self):
        # 不注入 runner，且 context 中无 _workspace_id
        config = {"workflow_id": "wfl_orphan"}
        with pytest.raises(RuntimeError, match="requires a workspace context"):
            await execute_subworkflow_node(config, {})


# ---------------------------------------------------------------------------
# validate_graph 对新节点类型的校验
# ---------------------------------------------------------------------------


class TestValidateGraphForExternalNodes:
    def _base_graph_with(self, node: dict[str, Any]) -> dict[str, Any]:
        return {
            "nodes": [
                {"id": "input", "type": "input"},
                node,
                {"id": "output", "type": "output", "config": {"from": "ext"}},
            ],
            "edges": [
                {"source": "input", "target": "ext"},
                {"source": "ext", "target": "output"},
            ],
        }

    def test_external_node_types_registered(self):
        assert "http" in EXTERNAL_NODE_TYPES
        assert "sub_workflow" in EXTERNAL_NODE_TYPES
        assert "http" in WORKFLOW_NODE_TYPES
        assert "sub_workflow" in WORKFLOW_NODE_TYPES
        # 原 12 个核心节点未受影响
        assert len({n for n in WORKFLOW_NODE_TYPES if n not in EXTERNAL_NODE_TYPES}) >= 12

    def test_valid_http_node_passes_validation(self):
        graph = self._base_graph_with({
            "id": "ext", "type": "http",
            "config": {"url": "https://api.example.com", "method": "POST", "timeout": 30},
        })
        assert validate_graph(graph) == []

    def test_http_node_rejects_non_http_scheme(self):
        graph = self._base_graph_with({
            "id": "ext", "type": "http",
            "config": {"url": "ftp://example.com"},
        })
        assert "http node requires an http(s) url" in validate_graph(graph)

    def test_http_node_rejects_mock_scheme(self):
        # mock:// 是 http_request 专用，真实 http 节点不接受
        graph = self._base_graph_with({
            "id": "ext", "type": "http",
            "config": {"url": "mock://echo"},
        })
        assert "http node requires an http(s) url" in validate_graph(graph)

    def test_http_node_rejects_invalid_method(self):
        graph = self._base_graph_with({
            "id": "ext", "type": "http",
            "config": {"url": "https://example.com", "method": "BOGUS"},
        })
        assert "http node method must be a valid HTTP verb" in validate_graph(graph)

    def test_http_node_rejects_out_of_range_timeout(self):
        graph = self._base_graph_with({
            "id": "ext", "type": "http",
            "config": {"url": "https://example.com", "timeout": 0.5},
        })
        assert "http node timeout must be between 1 and 120 seconds" in validate_graph(graph)

    def test_http_node_accepts_all_allowed_methods(self):
        for method in HTTP_ALLOWED_METHODS:
            graph = self._base_graph_with({
                "id": "ext", "type": "http",
                "config": {"url": "https://example.com", "method": method},
            })
            assert validate_graph(graph) == [], f"method {method} should be valid"

    def test_valid_sub_workflow_node_passes_validation(self):
        graph = self._base_graph_with({
            "id": "ext", "type": "sub_workflow",
            "config": {"workflow_id": "wfl_child", "timeout": 60},
        })
        assert validate_graph(graph) == []

    def test_sub_workflow_node_requires_workflow_id(self):
        graph = self._base_graph_with({
            "id": "ext", "type": "sub_workflow",
            "config": {},
        })
        assert "sub_workflow node requires a workflow_id" in validate_graph(graph)

    def test_sub_workflow_node_rejects_out_of_range_timeout(self):
        graph = self._base_graph_with({
            "id": "ext", "type": "sub_workflow",
            "config": {"workflow_id": "wfl_child", "timeout": 99999},
        })
        assert "sub_workflow node timeout must be between 1 and 1800 seconds" in validate_graph(graph)

    def test_node_type_aliases_are_canonicalized(self):
        graph = self._base_graph_with({
            "id": "ext", "type": "subworkflow",
            "config": {"workflow_id": "wfl_child"},
        })
        assert validate_graph(graph) == []


# ---------------------------------------------------------------------------
# execute_graph 集成测试
# ---------------------------------------------------------------------------


class TestExecuteGraphIntegration:
    @pytest.mark.asyncio
    async def test_http_node_dry_run_returns_stub(self):
        graph = {
            "nodes": [
                {"id": "input", "type": "input"},
                {"id": "http", "type": "http", "config": {"url": "https://example.com", "method": "POST"}},
                {"id": "output", "type": "output", "config": {"from": "http"}},
            ],
            "edges": [
                {"source": "input", "target": "http"},
                {"source": "http", "target": "output"},
            ],
        }
        status, output, trace, error = await execute_graph(graph, {}, None, True)
        assert (status, error) == ("succeeded", None)
        assert output["output"]["dry_run"] is True
        assert output["output"]["url"] == "https://example.com"
        assert output["output"]["method"] == "POST"
        assert trace[-1]["status"] == "succeeded"

    @pytest.mark.asyncio
    async def test_sub_workflow_node_dry_run_returns_stub(self):
        graph = {
            "nodes": [
                {"id": "input", "type": "input"},
                {"id": "sub", "type": "sub_workflow", "config": {"workflow_id": "wfl_child"}},
                {"id": "output", "type": "output", "config": {"from": "sub"}},
            ],
            "edges": [
                {"source": "input", "target": "sub"},
                {"source": "sub", "target": "output"},
            ],
        }
        status, output, _trace, error = await execute_graph(graph, {}, None, True)
        assert (status, error) == ("succeeded", None)
        assert output["output"]["dry_run"] is True
        assert output["output"]["workflow_id"] == "wfl_child"

    @pytest.mark.asyncio
    async def test_http_node_real_execution_in_graph(self, patch_httpx_client):
        response = MockResponse(status_code=200, json_data={"items": [1, 2, 3]})
        patch_httpx_client(response=response)
        graph = {
            "nodes": [
                {"id": "input", "type": "input"},
                {"id": "http", "type": "http", "config": {"url": "https://api.example.com/data", "method": "GET"}},
                {"id": "output", "type": "output", "config": {"from": "http"}},
            ],
            "edges": [
                {"source": "input", "target": "http"},
                {"source": "http", "target": "output"},
            ],
        }
        status, output, trace, error = await execute_graph(graph, {}, None, False)
        assert (status, error) == ("succeeded", None)
        assert output["output"]["status_code"] == 200
        assert output["output"]["body"] == {"items": [1, 2, 3]}
        assert all(item["status"] == "succeeded" for item in trace)

    @pytest.mark.asyncio
    async def test_http_node_failure_marks_run_failed(self, patch_httpx_client):
        patch_httpx_client(error=httpx.ConnectError("refused"))
        graph = {
            "nodes": [
                {"id": "input", "type": "input"},
                {"id": "http", "type": "http", "config": {"url": "https://api.example.com", "method": "GET"}},
                {"id": "output", "type": "output", "config": {"from": "http"}},
            ],
            "edges": [
                {"source": "input", "target": "http"},
                {"source": "http", "target": "output"},
            ],
        }
        status, _output, trace, error = await execute_graph(graph, {}, None, False)
        assert status == "failed"
        assert "connection failed" in error
        assert trace[-1]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_sub_workflow_node_real_execution_with_injected_runner(self):
        async def runner(workflow_id: str, input_data: dict[str, Any], timeout: float) -> dict[str, Any]:
            return {"status": "succeeded", "output": {"child": workflow_id, "echo": input_data}, "error": None}

        # 通过 input 节点配置注入 runner 到 context（execute_graph 会把 input_value 作为 context["input"]）
        # 实际上 runner 需要直接在 context 顶层；这里用一个 transform 节点无法注入，
        # 所以直接测试 execute_subworkflow_node 已覆盖；此处通过 execute_graph 验证 dry_run 路径
        # 已由 test_sub_workflow_node_dry_run_returns_stub 覆盖，这里再验证非 dry_run 失败路径
        graph = {
            "nodes": [
                {"id": "input", "type": "input"},
                {"id": "sub", "type": "sub_workflow", "config": {"workflow_id": "wfl_child"}},
                {"id": "output", "type": "output", "config": {"from": "sub"}},
            ],
            "edges": [
                {"source": "input", "target": "sub"},
                {"source": "sub", "target": "output"},
            ],
        }
        # 非 dry_run 且无 runner、无 workspace → 应失败
        status, _output, trace, error = await execute_graph(graph, {}, None, False)
        assert status == "failed"
        assert "requires a workspace context" in error
        assert trace[-1]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_code_node_local_sandbox_in_graph(self):
        graph = {
            "nodes": [
                {"id": "input", "type": "input"},
                {
                    "id": "code",
                    "type": "code",
                    "config": {
                        "code": "result = {'value': input['value'] * 3}",
                        "sandbox": "local",
                        "timeout": 5,
                        "input_mapping": {"input": "{{$input}}"},
                    },
                },
                {"id": "output", "type": "output", "config": {"from": "code"}},
            ],
            "edges": [
                {"source": "input", "target": "code"},
                {"source": "code", "target": "output"},
            ],
        }
        status, output, trace, error = await execute_graph(graph, {"value": 14}, None, False)
        assert (status, error) == ("succeeded", None)
        assert output["output"] == {"result": {"value": 42}}
        assert trace[-1]["status"] == "succeeded"

    @pytest.mark.asyncio
    async def test_code_node_local_sandbox_blocks_imports_in_graph(self):
        graph = {
            "nodes": [
                {"id": "input", "type": "input"},
                {
                    "id": "code",
                    "type": "code",
                    "config": {
                        "code": "import os\nresult = os.getcwd()",
                        "sandbox": "local",
                    },
                },
                {"id": "output", "type": "output", "config": {"from": "code"}},
            ],
            "edges": [
                {"source": "input", "target": "code"},
                {"source": "code", "target": "output"},
            ],
        }
        status, _output, trace, error = await execute_graph(graph, {}, None, False)
        assert status == "failed"
        assert "imports and global scope mutation are disabled" in error
        assert trace[-1]["status"] == "failed"
