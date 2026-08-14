import asyncio
import hashlib
import socket
from zipfile import is_zipfile

import pytest
from fastapi import HTTPException
from pypdf import PdfReader

from workama_platform.core import Actor
from workama_platform.modules import work


class EmptyResult:
    async def fetchone(self):
        return None


class RecordingConnection:
    def __init__(self):
        self.calls = []

    async def execute(self, query, params):
        self.calls.append((query, params))
        return EmptyResult()


def actor(workspace_id: str):
    return Actor(
        user_id="usr_test",
        workspace_id=workspace_id,
        org_id="org_test",
        role="owner",
        email="owner@example.test",
        display_name="Owner",
        onboarding_completed=True,
    )


def test_work_state_machines_allow_progression_and_reject_terminal_reentry():
    work.validate_plan_transition("draft", "ready")
    work.validate_plan_transition("ready", "running")
    work.validate_plan_transition("running", "paused")
    work.validate_plan_transition("paused", "running")
    work.validate_task_transition("todo", "in_progress")
    work.validate_task_transition("in_progress", "done")

    with pytest.raises(HTTPException) as plan_error:
        work.validate_plan_transition("succeeded", "running")
    assert plan_error.value.status_code == 409

    with pytest.raises(HTTPException) as task_error:
        work.validate_task_transition("done", "in_progress")
    assert task_error.value.status_code == 409


def test_task_sorting_requires_exact_unique_workspace_task_set():
    assert work.normalize_task_order(["t1", "t2", "t3"], ["t3", "t1", "t2"]) == ["t3", "t1", "t2"]
    with pytest.raises(HTTPException) as duplicate:
        work.normalize_task_order(["t1", "t2"], ["t1", "t1"])
    assert duplicate.value.status_code == 422
    with pytest.raises(HTTPException) as missing:
        work.normalize_task_order(["t1", "t2"], ["t1", "t3"])
    assert missing.value.status_code == 422


def test_next_task_position_is_free_from_existing_positions():
    assert work.next_task_position([]) == 0
    assert work.next_task_position([{"position": 0}, {"position": 4}]) == 5


@pytest.mark.asyncio
async def test_plan_lookup_is_workspace_scoped():
    connection = RecordingConnection()
    with pytest.raises(HTTPException) as error:
        await work._owned_plan(connection, "plan_other", actor("workspace_current"))
    assert error.value.status_code == 404
    assert connection.calls[0][1] == ("plan_other", "workspace_current")
    assert "workspace_id=%s" in connection.calls[0][0]


def test_minimal_office_artifacts_are_valid_zip_packages_and_have_no_secrets():
    tasks = [
        {"title": "Collect data", "description": "Use approved sources", "status": "done"},
        {"title": "Draft report", "description": "Summarize findings", "status": "todo"},
    ]
    sources = [{"url": "mock://research/topic", "title": "Mock source", "content_sha256": "abc"}]
    for format in ("docx", "xlsx", "pptx"):
        artifact = work.generate_office_artifact(format, "Quarterly plan", "Prepare a report", tasks, sources)
        assert artifact.data.startswith(b"PK")
        assert is_zipfile(__import__("io").BytesIO(artifact.data))
        assert artifact.content_type.startswith("application/vnd.openxmlformats")
        assert b"api_key" not in artifact.data
        assert b"secret-value" not in artifact.data


def _public_resolver(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]


def test_research_url_validation_blocks_ssrf_and_allows_https_with_public_resolution():
    validated = work.validate_research_url("https://example.com/research", resolver=_public_resolver)
    assert validated["source_type"] == "https"
    assert validated["host"] == "example.com"

    for value in (
        "http://example.com/research",
        "https://localhost/private",
        "https://127.0.0.1/private",
        "https://example.com:99999/research",
        "https://example.com/?access_token=secret",
    ):
        with pytest.raises(HTTPException) as error:
            work.validate_research_url(value, resolver=_public_resolver)
        assert error.value.status_code == 422


def test_deterministic_mock_browser_fetch_is_repeatable_and_untrusted():
    first = work.deterministic_mock_browser_fetch("mock://research/topic")
    second = work.deterministic_mock_browser_fetch("mock://research/topic")
    assert first == second
    assert first["untrusted"] is True
    assert first["content_sha256"]


def test_deep_research_report_has_numbered_markdown_and_valid_pdf():
    source = work.deterministic_mock_browser_fetch("mock://research/topic")
    markdown, pdf, validation = work.generate_research_artifacts(
        "Research brief",
        "Compare controlled evidence",
        [{"source": {"url": source["url"], "title": source["title"]}, "fetched": source}],
    )
    assert markdown.extension == "md"
    assert b"## References" in markdown.data
    assert b"[1]" in markdown.data
    assert validation["status"] == "consistent_fingerprint"
    reader = PdfReader(__import__("io").BytesIO(pdf.data))
    assert len(reader.pages) == 1
    assert "References" in (reader.pages[0].extract_text() or "")


def test_event_payload_redaction_and_schema_are_workspace_safe():
    payload = work.redact_sensitive({"url": "mock://research/topic", "authorization": "Bearer secret", "nested": {"api_key": "secret-value"}})
    assert payload["authorization"] == "<redacted>"
    assert payload["nested"]["api_key"] == "<redacted>"
    assert payload["url"].startswith("mock://")

    schema = "\n".join(work.SCHEMA_STATEMENTS)
    for table in ("work_plan", "work_execution", "work_task", "work_event", "work_citation", "work_artifact"):
        assert table in schema
    assert "workspace_id" in schema
    assert "s3_key" in schema


def test_execution_view_exposes_operation_progress_without_sensitive_payloads():
    view = work.execution_view({
        "id": "wexec_1",
        "workspace_id": "wsp_1",
        "plan_id": "wplan_1",
        "operation_id": "op_1",
        "source_ids": ["src_1"],
        "execution_mode": "deep_research",
        "status": "running",
        "operation_status": "running",
        "progress": 42,
        "stage": "task.execution",
    })
    assert view == {
        "id": "wexec_1",
        "workspace_id": "wsp_1",
        "plan_id": "wplan_1",
        "operation_id": "op_1",
        "source_ids": ["src_1"],
        "execution_mode": "deep_research",
        "status": "running",
        "operation_status": "running",
        "progress": 42,
        "stage": "task.execution",
        "started_at": None,
        "completed_at": None,
        "created_at": None,
        "updated_at": None,
    }


def test_work_router_exposes_plan_task_event_source_and_artifact_contracts():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in work.router.routes}
    assert ("/api/v1/work/plans", ("GET",)) in paths
    assert ("/api/v1/work/plans", ("POST",)) in paths
    assert ("/api/v1/work/plans/{plan_id}/tasks", ("GET",)) in paths
    assert ("/api/v1/work/plans/{plan_id}/tasks", ("POST",)) in paths
    assert ("/api/v1/work/plans/{plan_id}/tasks/reorder", ("POST",)) in paths
    assert ("/api/v1/work/plans/{plan_id}/executions", ("POST",)) in paths
    assert ("/api/v1/work/plans/{plan_id}/events", ("GET",)) in paths
    assert ("/api/v1/work/plans/{plan_id}/events/stream", ("GET",)) in paths
    assert ("/api/v1/work/plans/{plan_id}/sources", ("POST",)) in paths
    assert ("/api/v1/work/plans/{plan_id}/artifacts", ("POST",)) in paths
    assert ("/api/v1/work/plans/{plan_id}/artifacts", ("GET",)) in paths
    assert ("/api/v1/work/plans/{plan_id}/artifacts/{work_artifact_id}/content", ("GET",)) in paths


# --- sandbox_browser_fetch：通过 sandbox-browser 真实抓取 https 源 ------------


class _FakeResponse:
    """模拟 httpx.Response，仅暴露 status_code / json / raise_for_status。"""

    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _RecordingAsyncClient:
    """记录所有调用并以预设响应序列回复的假 httpx.AsyncClient。

    用于 sandbox_browser_fetch 测试：按调用顺序消费 ``responses`` 队列，
    DELETE 调用记录到 ``deletes``，便于断言沙箱释放行为。
    """

    def __init__(self, responses: list[_FakeResponse] | None = None):
        self._responses = list(responses or [])
        self.calls: list[tuple[str, str, dict | None]] = []
        self.deletes: list[str] = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def _pop(self, method: str, url: str) -> _FakeResponse:
        if not self._responses:
            raise AssertionError(f"unexpected {method} {url}: no queued response")
        return self._responses.pop(0)

    async def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, json))
        return self._pop("POST", url)

    async def delete(self, url, headers=None):
        self.calls.append(("DELETE", url, None))
        self.deletes.append(url)
        return self._pop("DELETE", url)

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_sandbox_browser_fetch_navigates_and_extracts(monkeypatch):
    # Arrange: 注入假的 httpx，按顺序返回 acquire / navigate / eval(title) /
    # eval(text) / screenshot / delete 响应
    screenshot_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    title_text = "Example Domain"
    body_text = "Example Domain\nThis domain is for use in illustrative examples."
    client = _RecordingAsyncClient(
        responses=[
            _FakeResponse(200, {"id": "sbx_abc"}),
            _FakeResponse(200, {"ok": True, "meta": {"url": "https://example.com/", "title": title_text}}),
            _FakeResponse(200, {"ok": True, "meta": {"result": title_text}}),
            _FakeResponse(200, {"ok": True, "meta": {"result": body_text}}),
            _FakeResponse(200, {"ok": True, "screenshot": screenshot_b64}),
            _FakeResponse(204, {}),
        ]
    )

    # 阻断真实 DNS 解析：返回公网地址
    monkeypatch.setattr(work.socket, "getaddrinfo", _public_resolver)

    # Act
    result = await work.sandbox_browser_fetch("https://example.com/research", client=client)

    # Assert: 返回字段齐全，且统一标记 untrusted
    assert result["url"] == "https://example.com/research"
    assert result["title"] == title_text
    assert result["text"] == body_text
    assert result["content_sha256"] == hashlib.sha256(body_text.encode()).hexdigest()
    assert result["screenshot"] == screenshot_b64
    assert result["untrusted"] is True

    # Assert: 调用顺序正确（acquire → navigate → eval title → eval text → screenshot → delete）
    methods_and_paths = [(m, u) for (m, u, _j) in client.calls]
    assert methods_and_paths[0] == ("POST", "http://sandbox-fleet:8002/internal/sandboxes")
    assert "/browser" in client.calls[1][1]
    assert "/browser" in client.calls[2][1]
    assert "/browser" in client.calls[3][1]
    assert "/browser" in client.calls[4][1]
    assert client.calls[5][0] == "DELETE"
    assert client.deletes == ["http://sandbox-fleet:8002/internal/sandboxes/sbx_abc"]


@pytest.mark.asyncio
async def test_sandbox_browser_fetch_releases_sandbox_on_failure(monkeypatch):
    # Arrange: navigate 返回 ok=false，应抛 HTTPException(503) 并触发 DELETE 释放
    client = _RecordingAsyncClient(
        responses=[
            _FakeResponse(200, {"id": "sbx_fail"}),
            _FakeResponse(200, {"ok": False, "error": "navigation timeout"}),
            _FakeResponse(204, {}),  # DELETE 响应
        ]
    )
    monkeypatch.setattr(work.socket, "getaddrinfo", _public_resolver)

    # Act + Assert: navigate 失败被映射为 503
    with pytest.raises(HTTPException) as exc:
        await work.sandbox_browser_fetch("https://example.com/page", client=client)
    assert exc.value.status_code == 503
    assert "navigate error=navigation timeout" in exc.value.detail

    # Assert: 沙箱在 finally 块中被释放
    assert client.deletes == ["http://sandbox-fleet:8002/internal/sandboxes/sbx_fail"]


def test_sandbox_browser_fetch_rejects_invalid_url():
    # mock:// 源应被 422 拒绝（sandbox_browser_fetch 仅处理 https）
    with pytest.raises(HTTPException) as exc:
        asyncio.run(work.sandbox_browser_fetch("mock://x"))
    assert exc.value.status_code == 422


# --- add_source endpoint：https 源走 sandbox_browser_fetch ----------------


class _SourceResult:
    """模拟 INSERT INTO work_citation ... RETURNING 的结果。"""

    def __init__(self, row):
        self._row = row

    async def fetchone(self):
        return self._row


class _AddSourceConnection:
    """支持 add_source 路由所需的查询/事务上下文的连接 mock。

    add_source 调用顺序：
      1. _owned_plan → SELECT FROM work_plan
      2. INSERT INTO work_citation ... RETURNING → 返回 source 行
      3. _append_event → UPDATE work_plan + INSERT INTO work_event RETURNING
    """

    def __init__(self, plan_row, source_row, event_row):
        self._plan_row = plan_row
        self._source_row = source_row
        self._event_row = event_row

    async def execute(self, query, params=()):
        if "FROM work_plan" in query and "work_event" not in query:
            return _SourceResult(self._plan_row)
        if "INSERT INTO work_citation" in query:
            return _SourceResult(self._source_row)
        if "INSERT INTO work_event" in query:
            return _SourceResult(self._event_row)
        return EmptyResult()

    class _Transaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return False

    def transaction(self):
        return _AddSourceConnection._Transaction()


class _AddSourcePool:
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


@pytest.mark.asyncio
async def test_add_source_with_https_fetch_uses_sandbox_browser(monkeypatch):
    # Arrange: 注入假的 pool / sandbox_browser_fetch，避免真实 DB 与外部 HTTP
    plan_row = {
        "id": "wplan_1", "workspace_id": "wsp_test", "session_id": None,
        "title": "Plan", "objective": "", "status": "draft",
        "last_event_seq": 0, "created_by": "usr_test",
        "created_at": None, "updated_at": None,
    }
    source_row = {
        "id": "wsrc_1", "plan_id": "wplan_1", "task_id": None,
        "source_type": "https", "url": "https://example.com/research",
        "title": "Example Domain", "excerpt": "body text",
        "content_sha256": "abc", "created_by": "usr_test", "created_at": None,
    }
    event_row = {
        "id": "wevt_1", "workspace_id": "wsp_test", "plan_id": "wplan_1",
        "task_id": None, "seq": 1, "event_type": "citation.created",
        "payload": {}, "created_by": "usr_test", "created_at": None,
    }
    monkeypatch.setattr(
        work, "pool", _AddSourcePool(_AddSourceConnection(plan_row, source_row, event_row))
    )

    fetched_payload = {
        "url": "https://example.com/research",
        "title": "Example Domain",
        "text": "Example body text",
        "content_sha256": "deadbeef",
        "screenshot": "iVBORw0KGgoAAAANSUhEUg==",
        "untrusted": True,
    }

    async def _fake_fetch(url, *, client=None):
        assert url == "https://example.com/research"
        return dict(fetched_payload)

    monkeypatch.setattr(work, "sandbox_browser_fetch", _fake_fetch)
    # 阻断真实 DNS 解析
    monkeypatch.setattr(work.socket, "getaddrinfo", _public_resolver)

    body = work.ResearchSourceCreate(url="https://example.com/research", fetch=True)

    # Act
    result = await work.add_source("wplan_1", body, actor("wsp_test"))

    # Assert: 返回体包含 fetched 字段，且 fetched.untrusted=True / screenshot 存在
    assert result["fetched"] is not None
    assert result["fetched"]["untrusted"] is True
    assert result["fetched"]["screenshot"] == fetched_payload["screenshot"]
    assert result["source_type"] == "https"
    assert result["url"] == "https://example.com/research"
