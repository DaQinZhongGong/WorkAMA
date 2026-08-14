"""WorkAMA Python SDK 扩展单元测试（P2 第三方集成）。

覆盖 P2 新增的 16 个方法（create_agent / send_chat_message / 工作流 / 知识库 /
文件 / 自动化 / 技能），以及 workspace_id 透传、403 ForbiddenError、
multipart 上传等行为。通过替换 ``_opener`` 字段 mock HTTP 层，
不发起真实网络请求。
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import pytest

from workama_sdk import (
    AuthenticationError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
    WorkAMAClient,
    WorkAMAError,
)


# ---------------------------------------------------------------------------
# Mock 基础设施（与 test_client.py 共享同一套实现，保持独立避免循环依赖）
# ---------------------------------------------------------------------------


class MockResponse(io.BytesIO):
    """模拟 ``urllib.response`` 的响应对象。"""

    def __init__(self, body: bytes, status: int = 200, headers: Optional[Dict[str, str]] = None):
        super().__init__(body)
        self.status = status
        self.code = status
        self._headers = headers or {}

    def info(self):  # noqa: D401
        return self._headers

    def read(self, *args, **kwargs):  # type: ignore[override]
        return super().read(*args, **kwargs)


class MockOpener:
    """替代 ``urllib.request.OpenerDirector``，按记录的请求返回预设响应。"""

    def __init__(self) -> None:
        self.calls: List[urllib.request.Request] = []
        self.responses: List[Any] = []
        self._default_response: Any = None

    def queue(self, response: Any) -> None:
        self.responses.append(response)

    def set_default(self, response: Any) -> None:
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
    body = json.dumps(payload).encode("utf-8") if not isinstance(payload, (bytes, str)) else (
        payload.encode("utf-8") if isinstance(payload, str) else payload
    )
    return MockResponse(body, status=status)


def make_http_error(status: int, payload: Any, reason: str = "error") -> urllib.error.HTTPError:
    body = json.dumps(payload).encode("utf-8") if not isinstance(payload, (bytes, str)) else (
        payload.encode("utf-8") if isinstance(payload, str) else payload
    )
    return urllib.error.HTTPError(
        url="http://mock/api/v1/mock",
        code=status,
        msg=reason,
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body),
    )


def new_client(
    base_url: str = "http://localhost:20200",
    api_key: Optional[str] = "wk_test",
    access_token: Optional[str] = None,
) -> Tuple[WorkAMAClient, MockOpener]:
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
    return opener.calls[index]


def header_value(req: urllib.request.Request, name: str) -> Optional[str]:
    """大小写不敏感地从 Request 上读取 header 值。"""
    target = name.lower()
    for key, val in req.headers.items():
        if key.lower() == target:
            return val
    return None


def parse_body(req: urllib.request.Request) -> Any:
    """解析请求体 JSON。"""
    return json.loads(req.data.decode("utf-8"))


# ---------------------------------------------------------------------------
# create_agent
# ---------------------------------------------------------------------------


class TestCreateAgent:
    def test_create_agent_posts_to_assistants(self):
        client, opener = new_client(access_token="tok")
        opener.set_default(make_ok({"id": "a1", "name": "demo"}))
        payload = {"name": "demo", "system_prompt": "hello", "model": "gpt-4o-mini"}
        resp = client.create_agent(payload)
        assert resp == {"id": "a1", "name": "demo"}
        req = get_req(opener)
        assert req.get_method() == "POST"
        assert req.full_url == "http://localhost:20200/api/v1/assistants"
        assert parse_body(req) == payload

    def test_create_agent_forwards_workspace_id_header(self):
        client, opener = new_client(access_token="tok")
        opener.set_default(make_ok({"id": "a1"}))
        client.create_agent({"name": "x"}, workspace_id="ws_123")
        req = get_req(opener)
        assert header_value(req, "X-Workspace-Id") == "ws_123"

    def test_create_agent_403_raises_forbidden_error(self):
        client, opener = new_client(access_token="tok")
        opener.queue(make_http_error(403, {"detail": "missing scope"}))
        with pytest.raises(ForbiddenError) as exc:
            client.create_agent({"name": "x"})
        assert exc.value.status_code == 403
        assert exc.value.body == {"detail": "missing scope"}


# ---------------------------------------------------------------------------
# send_chat_message
# ---------------------------------------------------------------------------


class TestSendChatMessage:
    def test_send_chat_message_posts_to_run(self):
        client, opener = new_client(access_token="tok")
        opener.set_default(make_ok({"id": "run1", "assistant_message": "hi"}))
        resp = client.send_chat_message("a1", "你好")
        assert resp["id"] == "run1"
        req = get_req(opener)
        assert req.get_method() == "POST"
        assert req.full_url.endswith("/api/v1/assistants/a1/run")
        body = parse_body(req)
        assert body["user_message"] == "你好"
        assert "metadata" not in body  # 无 conversation_id 时不带 metadata

    def test_send_chat_message_with_conversation_id(self):
        client, opener = new_client(access_token="tok")
        opener.set_default(make_ok({"id": "run2"}))
        client.send_chat_message("a1", "续接", conversation_id="conv_abc")
        body = parse_body(req) if (req := get_req(opener)) else {}
        assert body["metadata"] == {"conversation_id": "conv_abc"}

    def test_send_chat_message_workspace_id_propagated(self):
        client, opener = new_client(access_token="tok")
        opener.set_default(make_ok({}))
        client.send_chat_message("a1", "hi", workspace_id="ws_x")
        req = get_req(opener)
        assert header_value(req, "X-Workspace-Id") == "ws_x"


# ---------------------------------------------------------------------------
# 工作流
# ---------------------------------------------------------------------------


class TestWorkflowsExtended:
    def test_list_workflows_default_limit_20(self):
        client, opener = new_client()
        opener.set_default(make_ok({"items": [{"id": "w1"}]}))
        client.list_workflows()
        req = get_req(opener)
        assert "limit=20" in req.full_url
        assert req.full_url.startswith("http://localhost:20200/api/v1/workflows?")

    def test_list_workflows_with_workspace_and_cursor(self):
        client, opener = new_client()
        opener.set_default(make_ok({"items": []}))
        client.list_workflows(workspace_id="ws_1", limit=5, cursor="cur_x")
        req = get_req(opener)
        assert "limit=5" in req.full_url
        assert "cursor=cur_x" in req.full_url
        assert header_value(req, "X-Workspace-Id") == "ws_1"

    def test_create_workflow_posts_payload(self):
        client, opener = new_client(access_token="tok")
        opener.set_default(make_ok({"id": "wf1", "name": "demo"}))
        payload = {"name": "demo", "graph": {"nodes": [], "edges": []}}
        resp = client.create_workflow(payload, workspace_id="ws_1")
        assert resp["id"] == "wf1"
        req = get_req(opener)
        assert req.get_method() == "POST"
        assert req.full_url == "http://localhost:20200/api/v1/workflows"
        assert parse_body(req) == payload
        assert header_value(req, "X-Workspace-Id") == "ws_1"

    def test_run_workflow_wraps_inputs_in_input_field(self):
        """平台 schema 使用 ``input`` 字段，SDK 将 ``inputs`` 包装为 ``{"input": ...}``。"""
        client, opener = new_client()
        opener.set_default(make_ok({"run_id": "r1", "status": "queued"}))
        resp = client.run_workflow("w1", {"topic": "周报"})
        assert resp == {"run_id": "r1", "status": "queued"}
        req = get_req(opener)
        assert req.get_method() == "POST"
        assert req.full_url.endswith("/api/v1/workflows/w1/runs")
        body = parse_body(req)
        assert body == {"input": {"topic": "周报"}}


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------


class TestKnowledgeBases:
    def test_list_knowledge_bases(self):
        client, opener = new_client()
        opener.set_default(make_ok({"items": [{"id": "kb1"}]}))
        resp = client.list_knowledge_bases(workspace_id="ws_1", limit=10)
        assert resp == {"items": [{"id": "kb1"}]}
        req = get_req(opener)
        assert req.get_method() == "GET"
        assert "limit=10" in req.full_url
        assert req.full_url.startswith("http://localhost:20200/api/v1/knowledge-bases?")
        assert header_value(req, "X-Workspace-Id") == "ws_1"

    def test_create_knowledge_base(self):
        client, opener = new_client(access_token="tok")
        opener.set_default(make_ok({"id": "kb1", "name": "docs"}))
        payload = {"name": "docs", "kind": "vector", "embedding_model": "text-embedding-3-small"}
        resp = client.create_knowledge_base(payload)
        assert resp["id"] == "kb1"
        req = get_req(opener)
        assert req.full_url == "http://localhost:20200/api/v1/knowledge-bases"
        assert parse_body(req) == payload

    def test_ingest_document_uses_provided_title(self):
        client, opener = new_client(access_token="tok")
        opener.set_default(make_ok({"id": "doc1"}))
        client.ingest_document(
            "kb1",
            "正文内容",
            metadata={"title": "自定义标题", "source_type": "manual"},
        )
        req = get_req(opener)
        body = parse_body(req)
        assert body["content"] == "正文内容"
        assert body["title"] == "自定义标题"
        assert body["source_type"] == "manual"
        # title 已从 metadata 中弹出
        assert "title" not in body["metadata"]
        assert req.full_url.endswith("/api/v1/knowledge-bases/kb1/documents")

    def test_ingest_document_generates_default_title_when_missing(self):
        """缺失 title 时，SDK 自动生成 ``doc-<hex>`` 默认值。"""
        client, opener = new_client(access_token="tok")
        opener.set_default(make_ok({"id": "doc2"}))
        client.ingest_document("kb1", "正文", metadata={"category": "faq"})
        body = parse_body(get_req(opener))
        assert body["title"].startswith("doc-")
        assert len(body["title"]) == len("doc-") + 8
        # 其余 metadata 字段保留
        assert body["metadata"] == {"category": "faq"}

    def test_ingest_document_source_url_promoted_to_top_level(self):
        client, opener = new_client(access_token="tok")
        opener.set_default(make_ok({"id": "doc3"}))
        client.ingest_document(
            "kb1",
            "正文",
            metadata={"source_url": "https://example.com/a.html"},
        )
        body = parse_body(get_req(opener))
        assert body["source_url"] == "https://example.com/a.html"
        assert "source_url" not in body["metadata"]

    def test_query_knowledge_posts_query_and_top_k(self):
        client, opener = new_client()
        opener.set_default(make_ok({"results": [{"id": "c1", "similarity": 0.92}]}))
        resp = client.query_knowledge("kb1", "如何接入", top_k=3)
        assert resp["results"][0]["similarity"] == 0.92
        req = get_req(opener)
        assert req.get_method() == "POST"
        assert req.full_url.endswith("/api/v1/knowledge-bases/kb1/rag/query")
        assert parse_body(req) == {"query": "如何接入", "top_k": 3}


# ---------------------------------------------------------------------------
# 文件
# ---------------------------------------------------------------------------


class TestFiles:
    def test_list_files(self):
        client, opener = new_client()
        opener.set_default(make_ok({"items": [{"id": "f1", "name": "a.txt"}]}))
        resp = client.list_files(workspace_id="ws_1")
        assert resp["items"][0]["name"] == "a.txt"
        req = get_req(opener)
        assert req.get_method() == "GET"
        assert "limit=20" in req.full_url
        assert req.full_url.startswith("http://localhost:20200/api/v1/files?")
        assert header_value(req, "X-Workspace-Id") == "ws_1"

    def test_upload_file_builds_multipart_body(self):
        """upload_file 应构造 multipart/form-data 请求体，包含 file/kind/metadata 字段。"""
        client, opener = new_client(access_token="tok")
        opener.set_default(make_ok({"id": "f1", "name": "test.txt", "size_bytes": 5}))
        resp = client.upload_file(
            "test.txt",
            b"hello",
            kind="document",
            metadata={"category": "note"},
        )
        assert resp["id"] == "f1"
        req = get_req(opener)
        assert req.get_method() == "POST"
        assert req.full_url == "http://localhost:20200/api/v1/files/upload"
        content_type = header_value(req, "Content-Type") or ""
        assert content_type.startswith("multipart/form-data; boundary=")
        body_bytes = req.data
        # boundary 出现在 body 中
        boundary = content_type.split("boundary=", 1)[1]
        assert boundary.encode("utf-8") in body_bytes
        # 包含文件名、kind、metadata 字段
        assert b'name="file"; filename="test.txt"' in body_bytes
        assert b'name="kind"' in body_bytes
        assert b'"category": "note"' in body_bytes
        # 文件内容存在
        assert b"hello" in body_bytes

    def test_upload_file_without_kind_or_metadata(self):
        """kind/metadata 缺失时，multipart 中不应包含对应字段。"""
        client, opener = new_client(access_token="tok")
        opener.set_default(make_ok({"id": "f2"}))
        client.upload_file("data.bin", b"\x00\x01\x02")
        body_bytes = get_req(opener).data
        assert b'name="kind"' not in body_bytes
        assert b'name="metadata"' not in body_bytes
        assert b"\x00\x01\x02" in body_bytes


# ---------------------------------------------------------------------------
# 自动化
# ---------------------------------------------------------------------------


class TestAutomations:
    def test_list_automations(self):
        client, opener = new_client()
        opener.set_default(make_ok({"items": [{"trigger_id": "t1"}]}))
        resp = client.list_automations(workspace_id="ws_1", limit=5)
        assert resp["items"][0]["trigger_id"] == "t1"
        req = get_req(opener)
        assert req.get_method() == "GET"
        assert "limit=5" in req.full_url
        assert req.full_url.startswith(
            "http://localhost:20200/api/v1/automations/v2/triggers?"
        )
        assert header_value(req, "X-Workspace-Id") == "ws_1"

    def test_create_automation(self):
        client, opener = new_client(access_token="tok")
        opener.set_default(make_ok({"trigger_id": "t2", "name": "cron"}))
        payload = {"name": "cron", "type": "schedule", "schedule": "0 9 * * *"}
        resp = client.create_automation(payload)
        assert resp["trigger_id"] == "t2"
        req = get_req(opener)
        assert req.get_method() == "POST"
        assert req.full_url == "http://localhost:20200/api/v1/automations/v2/triggers"
        assert parse_body(req) == payload


# ---------------------------------------------------------------------------
# 技能
# ---------------------------------------------------------------------------


class TestSkills:
    def test_list_skills(self):
        client, opener = new_client()
        opener.set_default(make_ok({"items": [{"skill_id": "s1", "name": "翻译"}]}))
        resp = client.list_skills()
        assert resp["items"][0]["name"] == "翻译"
        req = get_req(opener)
        assert req.get_method() == "GET"
        assert req.full_url.startswith("http://localhost:20200/api/v1/skills?")

    def test_install_skill_posts_to_subscribe(self):
        client, opener = new_client(access_token="tok")
        opener.set_default(make_ok({"skill_id": "s2", "installed": True}))
        resp = client.install_skill("s2", workspace_id="ws_1")
        assert resp["installed"] is True
        req = get_req(opener)
        assert req.get_method() == "POST"
        assert req.full_url == (
            "http://localhost:20200/api/v1/skills/marketplace/s2/subscribe"
        )
        assert header_value(req, "X-Workspace-Id") == "ws_1"


# ---------------------------------------------------------------------------
# workspace_id 透传（跨方法验证）
# ---------------------------------------------------------------------------


class TestWorkspaceIdPropagation:
    def test_workspace_id_added_to_get_request(self):
        client, opener = new_client()
        opener.set_default(make_ok({}))
        client.list_workflows(workspace_id="ws_test")
        assert header_value(get_req(opener), "X-Workspace-Id") == "ws_test"

    def test_workspace_id_not_added_when_none(self):
        client, opener = new_client()
        opener.set_default(make_ok({}))
        client.list_workflows()
        assert header_value(get_req(opener), "X-Workspace-Id") is None

    def test_workspace_id_added_to_post_request(self):
        client, opener = new_client(access_token="tok")
        opener.set_default(make_ok({}))
        client.create_knowledge_base({"name": "x"}, workspace_id="ws_post")
        assert header_value(get_req(opener), "X-Workspace-Id") == "ws_post"


# ---------------------------------------------------------------------------
# 错误处理扩展（403）
# ---------------------------------------------------------------------------


class TestErrorHandlingExtended:
    def test_403_raises_forbidden_error(self):
        client, opener = new_client(access_token="tok")
        opener.queue(make_http_error(403, {"detail": "forbidden"}))
        with pytest.raises(ForbiddenError) as exc:
            client.list_workflows()
        assert exc.value.status_code == 403
        assert exc.value.body == {"detail": "forbidden"}

    def test_403_is_subclass_of_workama_error(self):
        client, opener = new_client(access_token="tok")
        opener.queue(make_http_error(403, {"detail": "no"}))
        with pytest.raises(WorkAMAError) as exc:
            client.list_knowledge_bases()
        assert isinstance(exc.value, ForbiddenError)
        assert isinstance(exc.value, WorkAMAError)

    def test_forbidden_error_message_prefers_body_detail(self):
        client, opener = new_client(access_token="tok")
        opener.queue(
            make_http_error(403, {"message": "missing capability"}, reason="Forbidden")
        )
        with pytest.raises(ForbiddenError) as exc:
            client.list_skills()
        assert "missing capability" in str(exc.value)


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


class TestCloseExtended:
    def test_close_is_safe_and_idempotent(self):
        client, _ = new_client()
        client.close()
        client.close()  # 重复调用不应抛出异常

    def test_close_does_not_affect_subsequent_client(self):
        client1, _ = new_client()
        client1.close()
        client2, opener2 = new_client()
        opener2.set_default(make_ok({"items": []}))
        resp = client2.list_workflows()
        assert resp == {"items": []}
