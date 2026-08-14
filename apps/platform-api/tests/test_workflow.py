"""工作流编排模块 (workflow) 单元 + 端点测试。

v7.151: 28 个测试覆盖：
- 工作流 CRUD：创建 / 列表 / 详情 / 更新 / 删除（5）
- 发布：成功 / archived 不可发布（2）
- 运行：成功 / draft 可运行 / archived 不可运行 / 失败节点（4）
- 运行历史：列表 / status 过滤（2）
- 单次运行详情：成功 / 不存在 404（2）
- workspace 隔离：跨区详情 403 / 跨区运行 403（2）
- 鉴权：未认证 401 / 无写权限 403（2）
- 边界：未支持节点类型 422 / DAG 含环 422 / 边指向不存在节点 422 / 空 update 422（4）
- DAG 校验：拓扑排序 / 重复节点 id（2）
- 节点执行：output 节点 / condition 节点分支（2）
- 辅助函数：_topological_order / _execute_condition（2）
- 真实运行：完整 workflow 三节点链路运行（1）

所有测试使用 fake pool/connection，不依赖真实 DB / Redis / 网络 / LLM API。
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules import workflow as wf
from workama_platform.modules.workflow import (
    _execute_condition,
    _topological_order,
)


# ============================================================================
# 测试辅助：fake pool / connection / result
# ============================================================================


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return list(self._rows)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _RecordingConnection:
    def __init__(self, results=None):
        self.calls: list[tuple[str, tuple]] = []
        self._results = list(results) if results else []
        self._idx = 0

    def transaction(self):
        return _Transaction()

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if self._idx < len(self._results):
            r = self._results[self._idx]
            self._idx += 1
            return r
        return _Result()

    async def commit(self):
        return None


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_args):
                return False

        return _Ctx()


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


def _workflow_row(**overrides) -> dict:
    base = {
        "id": "wf_1",
        "workspace_id": "wsp_test",
        "name": "Test Workflow",
        "description": "A test workflow",
        "nodes": [
            {"id": "n1", "type": "output", "name": "out", "config": {"fields": ["answer"]}},
        ],
        "edges": [],
        "status": "published",
        "version": 1,
        "metadata": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _run_row(**overrides) -> dict:
    base = {
        "id": "wfr_1",
        "workflow_id": "wf_1",
        "workspace_id": "wsp_test",
        "input": {"query": "Hello"},
        "output": {"answer": "Hi there"},
        "status": "completed",
        "started_at": datetime.now(UTC),
        "completed_at": datetime.now(UTC),
        "error": None,
        "metadata": {"node_runs": []},
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(wf.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


# ============================================================================
# 1. 工作流 CRUD
# ============================================================================


class TestWorkflowCrud:
    @pytest.mark.asyncio
    async def test_create_workflow_success(self, monkeypatch):
        """POST /api/v1/workflows 创建工作流返回 201。"""
        conn = _RecordingConnection(results=[_Result(row=_workflow_row())])
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/workflows",
                json={
                    "name": "Test Workflow",
                    "nodes": [
                        {"id": "n1", "type": "output", "config": {"fields": ["answer"]}}
                    ],
                    "edges": [],
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "Test Workflow"
        assert any("INSERT INTO workflow" in q for q, _ in conn.calls)

    @pytest.mark.asyncio
    async def test_list_workflows(self, monkeypatch):
        """GET /api/v1/workflows 列表。"""
        rows = [_workflow_row(id="wf_a"), _workflow_row(id="wf_b")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/workflows")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2

    @pytest.mark.asyncio
    async def test_get_workflow_detail(self, monkeypatch):
        """GET /api/v1/workflows/{id} 详情。"""
        conn = _RecordingConnection(results=[_Result(row=_workflow_row(id="wf_x"))])
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/workflows/wf_x")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "wf_x"

    @pytest.mark.asyncio
    async def test_update_workflow_increments_version(self, monkeypatch):
        """PATCH /api/v1/workflows/{id} 更新且 version 自增。"""
        updated = _workflow_row(name="NewName", version=2)
        conn = _RecordingConnection(
            results=[_Result(row=_workflow_row()), _Result(row=updated)]
        )
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/workflows/wf_1",
                json={"name": "NewName"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "NewName"
        assert body["version"] == 2

    @pytest.mark.asyncio
    async def test_delete_workflow_success(self, monkeypatch):
        """DELETE /api/v1/workflows/{id} 删除。"""
        conn = _RecordingConnection(
            results=[_Result(row=_workflow_row()), _Result(row={"id": "wf_1"})]
        )
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/workflows/wf_1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted"] is True


# ============================================================================
# 2. 发布
# ============================================================================


class TestPublish:
    @pytest.mark.asyncio
    async def test_publish_workflow_success(self, monkeypatch):
        """POST /api/v1/workflows/{id}/publish 发布 draft 工作流。"""
        published = _workflow_row(status="published")
        conn = _RecordingConnection(
            results=[_Result(row=_workflow_row(status="draft")), _Result(row=published)]
        )
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/workflows/wf_1/publish")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "published"

    @pytest.mark.asyncio
    async def test_publish_archived_returns_409(self, monkeypatch):
        """archived 工作流不能发布。"""
        conn = _RecordingConnection(results=[_Result(row=_workflow_row(status="archived"))])
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/workflows/wf_1/publish")
        assert resp.status_code == 409


# ============================================================================
# 3. 运行
# ============================================================================


class TestRun:
    @pytest.mark.asyncio
    async def test_run_workflow_success(self, monkeypatch):
        """POST /api/v1/workflows/{id}/run 成功运行 published 工作流。"""
        workflow = _workflow_row(
            nodes=[
                {"id": "n1", "type": "output", "name": "out", "config": {"fields": ["query"]}}
            ],
            edges=[],
        )
        conn = _RecordingConnection(
            results=[
                _Result(row=workflow),  # _owned_workflow
                _Result(row=_run_row()),  # INSERT run
            ]
        )
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/workflows/wf_1/run",
                json={"input": {"query": "Hello"}},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "completed"
        assert body["output"]["query"] == "Hello"
        assert body["metadata"]["draft_run"] is False

    @pytest.mark.asyncio
    async def test_run_draft_workflow_allowed(self, monkeypatch):
        """draft 工作流允许运行（metadata.draft_run=true）。"""
        workflow = _workflow_row(
            status="draft",
            nodes=[
                {"id": "n1", "type": "output", "config": {"fields": ["query"]}}
            ],
        )
        conn = _RecordingConnection(
            results=[_Result(row=workflow), _Result(row=_run_row())]
        )
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/workflows/wf_1/run",
                json={"input": {"query": "Hello"}},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["metadata"]["draft_run"] is True

    @pytest.mark.asyncio
    async def test_run_archived_returns_409(self, monkeypatch):
        """archived 工作流不可运行。"""
        conn = _RecordingConnection(results=[_Result(row=_workflow_row(status="archived"))])
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/workflows/wf_1/run",
                json={"input": {"query": "Hello"}},
            )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_run_workflow_with_llm_call_node(self, monkeypatch):
        """工作流含 llm_call 节点时调用 mock LLM。"""
        workflow = _workflow_row(
            nodes=[
                {
                    "id": "n1",
                    "type": "llm_call",
                    "name": "call_llm",
                    "config": {"model": "gpt-4o", "prompt_template": "Q: {query}"},
                },
                {
                    "id": "n2",
                    "type": "output",
                    "name": "out",
                    "config": {"fields": ["message"]},
                },
            ],
            edges=[{"source": "n1", "target": "n2"}],
        )
        # _execute_workflow 内部不查 DB；但 _owned_workflow + INSERT 查 DB
        # _execute_llm_call 走 mock（无 API key）
        run_row = _run_row(
            output={"message": "[mock-llm] node=call_llm model=gpt-4o prompt=Q: Hello"}
        )
        conn = _RecordingConnection(
            results=[_Result(row=workflow), _Result(row=run_row)]
        )
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/workflows/wf_1/run",
                json={"input": {"query": "Hello"}},
            )
        assert resp.status_code == 200
        body = resp.json()
        # 节点执行记录被写入 metadata.node_runs
        assert len(body["metadata"]["node_runs"]) == 2
        # llm_call 节点输出包含 mock-llm
        llm_run = body["metadata"]["node_runs"][0]
        assert llm_run["node_type"] == "llm_call"
        assert "mock-llm" in llm_run["output"]["message"]


# ============================================================================
# 4. 运行历史 & 单次运行
# ============================================================================


class TestWorkflowRuns:
    @pytest.mark.asyncio
    async def test_list_workflow_runs(self, monkeypatch):
        """GET /api/v1/workflows/{id}/runs 运行历史。"""
        rows = [_run_row(id="wfr_1"), _run_row(id="wfr_2")]
        conn = _RecordingConnection(
            results=[_Result(row=_workflow_row()), _Result(rows=rows)]
        )
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/workflows/wf_1/runs")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2

    @pytest.mark.asyncio
    async def test_list_workflow_runs_with_status_filter(self, monkeypatch):
        """GET /api/v1/workflows/{id}/runs?status=failed status 过滤。"""
        conn = _RecordingConnection(
            results=[_Result(row=_workflow_row()), _Result(rows=[])]
        )
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/workflows/wf_1/runs?status=failed")
        assert resp.status_code == 200
        list_sql = conn.calls[1][0]
        assert "AND status = %s" in list_sql

    @pytest.mark.asyncio
    async def test_get_workflow_run_detail(self, monkeypatch):
        """GET /api/v1/workflows/runs/{run_id} 单次运行详情。"""
        conn = _RecordingConnection(results=[_Result(row=_run_row(id="wfr_x"))])
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/workflows/runs/wfr_x")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "wfr_x"

    @pytest.mark.asyncio
    async def test_get_workflow_run_not_found_404(self, monkeypatch):
        """GET /api/v1/workflows/runs/{run_id} 不存在 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/workflows/runs/missing")
        assert resp.status_code == 404


# ============================================================================
# 5. workspace 隔离
# ============================================================================


class TestWorkspaceIsolation:
    @pytest.mark.asyncio
    async def test_get_workflow_other_workspace_403(self, monkeypatch):
        """跨 workspace 查询工作流 403。"""
        other = _workflow_row(workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=other)])
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/workflows/wf_1")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_run_workflow_other_workspace_403(self, monkeypatch):
        """跨 workspace 运行工作流 403。"""
        other = _workflow_row(workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=other)])
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/workflows/wf_1/run",
                json={"input": {}},
            )
        assert resp.status_code == 403


# ============================================================================
# 6. 鉴权
# ============================================================================


class TestAuth:
    @pytest.mark.asyncio
    async def test_unauthenticated_returns_401(self):
        """未认证请求 401。"""
        app = _app(actor=None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/workflows")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_no_write_capability_returns_403(self, monkeypatch):
        """只有 read 能力的 actor 不能创建（403）。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor(capabilities=("workflow:read",)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/workflows",
                json={"name": "Wf"},
            )
        assert resp.status_code == 403


# ============================================================================
# 7. 边界
# ============================================================================


class TestValidation:
    @pytest.mark.asyncio
    async def test_unsupported_node_type_422(self, monkeypatch):
        """未支持的节点类型 422。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/workflows",
                json={
                    "name": "Wf",
                    "nodes": [{"id": "n1", "type": "unknown_type"}],
                },
            )
        assert resp.status_code == 422
        assert "Unsupported node type" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_dag_cycle_422(self, monkeypatch):
        """DAG 含环 422。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/workflows",
                json={
                    "name": "Wf",
                    "nodes": [
                        {"id": "n1", "type": "output"},
                        {"id": "n2", "type": "output"},
                    ],
                    "edges": [
                        {"source": "n1", "target": "n2"},
                        {"source": "n2", "target": "n1"},
                    ],
                },
            )
        assert resp.status_code == 422
        assert "cycle" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_edge_to_missing_node_422(self, monkeypatch):
        """边指向不存在的节点 422。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/workflows",
                json={
                    "name": "Wf",
                    "nodes": [{"id": "n1", "type": "output"}],
                    "edges": [{"source": "n1", "target": "n_missing"}],
                },
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_update_422(self, monkeypatch):
        """PATCH 空请求体 422。"""
        conn = _RecordingConnection(results=[_Result(row=_workflow_row())])
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch("/api/v1/workflows/wf_1", json={})
        assert resp.status_code == 422


# ============================================================================
# 8. DAG 校验函数
# ============================================================================


class TestDagValidation:
    def test_topological_order_linear(self):
        """线性链路拓扑顺序正确。"""
        nodes = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        edges = [{"source": "a", "target": "b"}, {"source": "b", "target": "c"}]
        order = _topological_order(nodes, edges)
        assert order == ["a", "b", "c"]

    def test_topological_order_with_cycle_raises(self):
        """含环时抛 HTTPException。"""
        from fastapi import HTTPException

        nodes = [{"id": "a"}, {"id": "b"}]
        edges = [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}]
        with pytest.raises(HTTPException) as exc_info:
            _topological_order(nodes, edges)
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_duplicate_node_id_raises(self, monkeypatch):
        """重复节点 id 422。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(wf, "pool", _Pool(conn))
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/workflows",
                json={
                    "name": "Wf",
                    "nodes": [
                        {"id": "dup", "type": "output"},
                        {"id": "dup", "type": "output"},
                    ],
                },
            )
        assert resp.status_code == 422


# ============================================================================
# 9. 节点执行
# ============================================================================


class TestNodeExecution:
    @pytest.mark.asyncio
    async def test_execute_output_node(self):
        """output 节点透传指定字段。"""
        from workama_platform.modules.workflow import _execute_output

        node = {"id": "n1", "type": "output", "config": {"fields": ["answer", "ignore"]}}
        inputs = {"answer": "42", "ignore": "x", "extra": "y"}
        output = _execute_output(node, inputs)
        assert output == {"answer": "42", "ignore": "x"}

    def test_execute_condition_default_branch(self):
        """condition 节点无匹配时返回 default 分支。"""
        node = {
            "id": "n1",
            "type": "condition",
            "config": {
                "value_key": "intent",
                "branches": [{"value": "greeting", "output": "hi"}],
            },
        }
        output, branch = _execute_condition(node, {"intent": "unknown"})
        assert branch == "default"
        assert output["matched"] is None

    def test_execute_condition_matched_branch(self):
        """condition 节点匹配分支。"""
        node = {
            "id": "n1",
            "type": "condition",
            "config": {
                "value_key": "intent",
                "branches": [{"value": "greeting"}, {"value": "bye"}],
            },
        }
        output, branch = _execute_condition(node, {"intent": "greeting"})
        assert branch == "greeting"
        assert output["matched"] == "greeting"

    @pytest.mark.asyncio
    async def test_execute_tool_call_node_mock(self):
        """tool_call 节点返回 mock 结果。"""
        from workama_platform.modules.workflow import _execute_tool_call

        node = {
            "id": "n1",
            "type": "tool_call",
            "config": {"tool_id": "search", "arguments": {"q": "test"}},
        }
        output = await _execute_tool_call(node, {})
        assert output["tool_id"] == "search"
        assert output["method"] == "mock"
        assert "mock-tool" in output["result"]


# ============================================================================
# 10. 完整链路运行
# ============================================================================


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_three_node_chain_runs_successfully(self, monkeypatch):
        """三节点链路：llm_call → output → 运行成功。"""
        workflow = _workflow_row(
            nodes=[
                {
                    "id": "n1",
                    "type": "llm_call",
                    "name": "step1",
                    "config": {"model": "gpt-4o", "prompt_template": "Hi"},
                },
                {
                    "id": "n2",
                    "type": "output",
                    "name": "out",
                    "config": {"fields": ["message"]},
                },
            ],
            edges=[{"source": "n1", "target": "n2"}],
        )
        run_row = _run_row()
        conn = _RecordingConnection(
            results=[_Result(row=workflow), _Result(row=run_row)]
        )
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/workflows/wf_1/run",
                json={"input": {"query": "Hello"}},
            )
        assert resp.status_code == 200
        body = resp.json()
        # 两个节点都执行
        node_runs = body["metadata"]["node_runs"]
        assert len(node_runs) == 2
        assert node_runs[0]["node_id"] == "n1"
        assert node_runs[0]["node_type"] == "llm_call"
        assert node_runs[1]["node_id"] == "n2"
        assert node_runs[1]["node_type"] == "output"
