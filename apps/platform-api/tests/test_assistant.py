"""助手模块 (assistant) 单元 + 端点测试。

v7.151: 26 个测试覆盖：
- 助手 CRUD：创建 / 列表 / 详情 / 更新 / 删除（5）
- 运行：成功 / 无 KB 无 memory / 有 KB / 有 memory / archived 状态不可运行（5）
- 运行历史：列表 / status 过滤（2）
- 克隆：成功 / 字段复制（2）
- workspace 隔离：跨区详情 403 / 跨区运行 403（2）
- 鉴权：未认证 401 / 无写权限 403（2）
- 边界：温度超界 422 / max_tokens 超界 / 空 system_prompt 422 / 空 update 422（4）
- LLM mock：mock 响应格式 / metadata.method=mock（2）
- 辅助函数：_build_context_text / _build_mock_response（2）
- 删除：成功 / 跨区 403（2）

所有测试使用 fake pool/connection，不依赖真实 DB / Redis / 网络 / LLM API。
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules import assistant as ast
from workama_platform.modules.assistant import (
    _build_context_text,
    _build_mock_response,
)


# ============================================================================
# 测试辅助：fake pool / connection / result
# ============================================================================


class _Result:
    """模拟 psycopg 查询结果。"""

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
    """记录 execute 调用并按序返回配置的结果。"""

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
    """模拟连接池。"""

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
    capabilities=("assistant:*",),
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


def _assistant_row(**overrides) -> dict:
    base = {
        "id": "ast_1",
        "workspace_id": "wsp_test",
        "name": "Helper",
        "description": "A helpful assistant",
        "system_prompt": "You are a helpful assistant.",
        "model": "gpt-4o-mini",
        "temperature": 0.7,
        "max_tokens": 2048,
        "tools": [],
        "knowledge_base_ids": [],
        "memory_enabled": True,
        "status": "active",
        "version": 1,
        "metadata": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _run_row(**overrides) -> dict:
    base = {
        "id": "astr_1",
        "assistant_id": "ast_1",
        "workspace_id": "wsp_test",
        "user_message": "Hello",
        "assistant_message": "Hi there",
        "model": "gpt-4o-mini",
        "tokens_used": 42,
        "duration_ms": 10,
        "status": "completed",
        "error": None,
        "metadata": {"method": "mock"},
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(ast.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


# ============================================================================
# 1. 助手 CRUD
# ============================================================================


class TestAssistantCrud:
    @pytest.mark.asyncio
    async def test_create_assistant_success(self, monkeypatch):
        """POST /api/v1/assistants 创建助手返回 201。"""
        conn = _RecordingConnection(results=[_Result(row=_assistant_row())])
        monkeypatch.setattr(ast, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/assistants",
                json={
                    "name": "Helper",
                    "system_prompt": "You are a helpful assistant.",
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "Helper"
        assert body["model"] == "gpt-4o-mini"
        assert any("INSERT INTO assistant" in q for q, _ in conn.calls)

    @pytest.mark.asyncio
    async def test_list_assistants_workspace_isolated(self, monkeypatch):
        """GET /api/v1/assistants 列表只返回当前 workspace 的助手。"""
        rows = [_assistant_row(id="ast_a"), _assistant_row(id="ast_b")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(ast, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/assistants")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert len(body["items"]) == 2
        # SQL 强制 workspace_id 过滤
        assert "workspace_id = %s" in conn.calls[0][0]

    @pytest.mark.asyncio
    async def test_get_assistant_detail(self, monkeypatch):
        """GET /api/v1/assistants/{id} 返回详情。"""
        conn = _RecordingConnection(results=[_Result(row=_assistant_row(id="ast_x"))])
        monkeypatch.setattr(ast, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/assistants/ast_x")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "ast_x"

    @pytest.mark.asyncio
    async def test_update_assistant_increments_version(self, monkeypatch):
        """PATCH /api/v1/assistants/{id} 部分更新且 version 自增。"""
        updated = _assistant_row(name="NewName", version=2)
        conn = _RecordingConnection(
            results=[_Result(row=_assistant_row()), _Result(row=updated)]
        )
        monkeypatch.setattr(ast, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/assistants/ast_1",
                json={"name": "NewName"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "NewName"
        assert body["version"] == 2
        # UPDATE 语句包含 version = version + 1
        update_sql = conn.calls[1][0]
        assert "version = version + 1" in update_sql

    @pytest.mark.asyncio
    async def test_delete_assistant_success(self, monkeypatch):
        """DELETE /api/v1/assistants/{id} 返回 200, deleted=true。"""
        conn = _RecordingConnection(
            results=[_Result(row=_assistant_row()), _Result(row={"id": "ast_1"})]
        )
        monkeypatch.setattr(ast, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/assistants/ast_1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted"] is True
        assert body["id"] == "ast_1"


# ============================================================================
# 2. 运行
# ============================================================================


class TestAssistantRun:
    @pytest.mark.asyncio
    async def test_run_assistant_success_mock_llm(self, monkeypatch):
        """POST /api/v1/assistants/{id}/run 成功运行（mock LLM）。"""
        # _owned_assistant + INSERT run + RETURNING
        # _rag_query / _memory_recall / _memory_extract 不调用 pool（mock 返回空）
        # 但 _memory_extract 会 try-import memory_vector 并调用 pool；这里 stub 掉
        async def _noop_memory_extract(*_a, **_kw):
            return []

        monkeypatch.setattr(ast, "_memory_extract", _noop_memory_extract)
        monkeypatch.setattr(ast, "_rag_query", lambda *_a, **_kw: _return_empty_list())
        monkeypatch.setattr(ast, "_memory_recall", lambda *_a, **_kw: _return_empty_list())
        conn = _RecordingConnection(
            results=[
                _Result(row=_assistant_row()),  # _owned_assistant
                _Result(row=_run_row()),  # INSERT run
            ]
        )
        monkeypatch.setattr(ast, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/assistants/ast_1/run",
                json={"user_message": "Hello"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "completed"
        assert "mock-llm" in body["assistant_message"]
        assert body["metadata"]["method"] == "mock"

    @pytest.mark.asyncio
    async def test_run_assistant_no_kb_no_memory(self, monkeypatch):
        """无 KB 无 memory 也能成功运行。"""
        monkeypatch.setattr(ast, "_memory_extract", lambda *_a, **_kw: _return_empty_list())
        monkeypatch.setattr(ast, "_rag_query", lambda *_a, **_kw: _return_empty_list())
        monkeypatch.setattr(ast, "_memory_recall", lambda *_a, **_kw: _return_empty_list())
        assistant_row = _assistant_row(
            knowledge_base_ids=[], memory_enabled=False, tools=[]
        )
        conn = _RecordingConnection(
            results=[_Result(row=assistant_row), _Result(row=_run_row())]
        )
        monkeypatch.setattr(ast, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/assistants/ast_1/run",
                json={"user_message": "Hello"},
            )
        assert resp.status_code == 200
        body = resp.json()
        # 没有调用 RAG / memory
        assert body["metadata"]["rag_chunks_count"] == 0
        assert body["metadata"]["memories_count"] == 0

    @pytest.mark.asyncio
    async def test_run_assistant_with_kb(self, monkeypatch):
        """有 knowledge_base_ids 时调用 RAG。"""
        async def _fake_rag(*_a, **_kw):
            return [{"chunk_id": "kbc_1", "content": "hello", "similarity": 0.9}]

        monkeypatch.setattr(ast, "_rag_query", _fake_rag)
        monkeypatch.setattr(ast, "_memory_recall", lambda *_a, **_kw: _return_empty_list())
        monkeypatch.setattr(ast, "_memory_extract", lambda *_a, **_kw: _return_empty_list())
        assistant_row = _assistant_row(knowledge_base_ids=["kb_1"])
        conn = _RecordingConnection(
            results=[_Result(row=assistant_row), _Result(row=_run_row())]
        )
        monkeypatch.setattr(ast, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/assistants/ast_1/run",
                json={"user_message": "Hello"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["metadata"]["rag_chunks_count"] == 1

    @pytest.mark.asyncio
    async def test_run_assistant_with_memory(self, monkeypatch):
        """memory_enabled 时召回记忆。"""
        async def _fake_recall(*_a, **_kw):
            return [{"memory_id": "mv_1", "content": "user likes Python", "similarity": 0.8}]

        monkeypatch.setattr(ast, "_rag_query", lambda *_a, **_kw: _return_empty_list())
        monkeypatch.setattr(ast, "_memory_recall", _fake_recall)
        monkeypatch.setattr(ast, "_memory_extract", lambda *_a, **_kw: _return_empty_list())
        conn = _RecordingConnection(
            results=[_Result(row=_assistant_row()), _Result(row=_run_row())]
        )
        monkeypatch.setattr(ast, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/assistants/ast_1/run",
                json={"user_message": "Hello"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["metadata"]["memories_count"] == 1

    @pytest.mark.asyncio
    async def test_run_archived_assistant_returns_409(self, monkeypatch):
        """archived 状态的助手不能运行。"""
        conn = _RecordingConnection(results=[_Result(row=_assistant_row(status="archived"))])
        monkeypatch.setattr(ast, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/assistants/ast_1/run",
                json={"user_message": "Hello"},
            )
        assert resp.status_code == 409


# ============================================================================
# 3. 运行历史
# ============================================================================


class TestAssistantRuns:
    @pytest.mark.asyncio
    async def test_list_runs(self, monkeypatch):
        """GET /api/v1/assistants/{id}/runs 运行历史。"""
        rows = [_run_row(id="astr_1"), _run_row(id="astr_2")]
        conn = _RecordingConnection(
            results=[_Result(row=_assistant_row()), _Result(rows=rows)]
        )
        monkeypatch.setattr(ast, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/assistants/ast_1/runs")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2

    @pytest.mark.asyncio
    async def test_list_runs_with_status_filter(self, monkeypatch):
        """GET /api/v1/assistants/{id}/runs?status=failed status 过滤。"""
        conn = _RecordingConnection(
            results=[_Result(row=_assistant_row()), _Result(rows=[])]
        )
        monkeypatch.setattr(ast, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/assistants/ast_1/runs?status=failed")
        assert resp.status_code == 200
        # SQL 含 status 过滤
        list_sql = conn.calls[1][0]
        assert "AND status = %s" in list_sql


# ============================================================================
# 4. 克隆
# ============================================================================


class TestAssistantClone:
    @pytest.mark.asyncio
    async def test_clone_assistant_success(self, monkeypatch):
        """POST /api/v1/assistants/{id}/clone 成功克隆。"""
        cloned = _assistant_row(id="ast_2", name="Helper Copy", version=1)
        conn = _RecordingConnection(
            results=[_Result(row=_assistant_row()), _Result(row=cloned)]
        )
        monkeypatch.setattr(ast, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/assistants/ast_1/clone",
                json={"name": "Helper Copy"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["id"] == "ast_2"
        assert body["name"] == "Helper Copy"
        assert body["version"] == 1

    @pytest.mark.asyncio
    async def test_clone_copies_system_prompt(self, monkeypatch):
        """克隆时 system_prompt / model / tools 被复制。"""
        cloned = _assistant_row(
            id="ast_2",
            name="Copy",
            system_prompt="Original prompt",
            model="gpt-4o",
            tools=["tool_1"],
        )
        conn = _RecordingConnection(
            results=[_Result(row=_assistant_row(system_prompt="Original prompt", model="gpt-4o", tools=["tool_1"])), _Result(row=cloned)]
        )
        monkeypatch.setattr(ast, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/assistants/ast_1/clone",
                json={"name": "Copy"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["system_prompt"] == "Original prompt"
        assert body["model"] == "gpt-4o"
        assert body["tools"] == ["tool_1"]
        # metadata 含 cloned_from
        assert body["metadata"]["cloned_from"] == "ast_1"


# ============================================================================
# 5. workspace 隔离
# ============================================================================


class TestWorkspaceIsolation:
    @pytest.mark.asyncio
    async def test_get_assistant_other_workspace_403(self, monkeypatch):
        """跨 workspace 查询助手返回 403。"""
        other_row = _assistant_row(workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=other_row)])
        monkeypatch.setattr(ast, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/assistants/ast_1")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_run_assistant_other_workspace_403(self, monkeypatch):
        """跨 workspace 运行助手返回 403。"""
        other_row = _assistant_row(workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=other_row)])
        monkeypatch.setattr(ast, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/assistants/ast_1/run",
                json={"user_message": "Hello"},
            )
        assert resp.status_code == 403


# ============================================================================
# 6. 鉴权
# ============================================================================


class TestAuth:
    @pytest.mark.asyncio
    async def test_unauthenticated_returns_401(self):
        """未认证请求返回 401（无 actor override）。"""
        app = _app(actor=None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/assistants")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_no_write_capability_returns_403(self, monkeypatch):
        """只有 read 能力的 actor 不能创建（403）。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(ast, "pool", _Pool(conn))

        app = _app(actor=_actor(capabilities=("assistant:read",)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/assistants",
                json={"name": "Helper", "system_prompt": "Hi"},
            )
        assert resp.status_code == 403


# ============================================================================
# 7. 边界
# ============================================================================


class TestValidation:
    @pytest.mark.asyncio
    async def test_temperature_out_of_range_422(self, monkeypatch):
        """temperature > 2.0 返回 422。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/assistants",
                json={
                    "name": "Helper",
                    "system_prompt": "Hi",
                    "temperature": 5.0,
                },
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_max_tokens_out_of_range_422(self, monkeypatch):
        """max_tokens > 32768 返回 422。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/assistants",
                json={
                    "name": "Helper",
                    "system_prompt": "Hi",
                    "max_tokens": 999999,
                },
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_system_prompt_422(self, monkeypatch):
        """空 system_prompt 返回 422。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/assistants",
                json={"name": "Helper", "system_prompt": ""},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_update_422(self, monkeypatch):
        """PATCH 空请求体返回 422。"""
        conn = _RecordingConnection(results=[_Result(row=_assistant_row())])
        monkeypatch.setattr(ast, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch("/api/v1/assistants/ast_1", json={})
        assert resp.status_code == 422


# ============================================================================
# 8. LLM mock
# ============================================================================


class TestMockLlm:
    @pytest.mark.asyncio
    async def test_mock_response_format(self, monkeypatch):
        """mock LLM 响应包含必需字段。"""
        monkeypatch.setattr(ast, "_memory_extract", lambda *_a, **_kw: _return_empty_list())
        monkeypatch.setattr(ast, "_rag_query", lambda *_a, **_kw: _return_empty_list())
        monkeypatch.setattr(ast, "_memory_recall", lambda *_a, **_kw: _return_empty_list())
        conn = _RecordingConnection(
            results=[_Result(row=_assistant_row()), _Result(row=_run_row())]
        )
        monkeypatch.setattr(ast, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/assistants/ast_1/run",
                json={"user_message": "What's up"},
            )
        assert resp.status_code == 200
        body = resp.json()
        msg = body["assistant_message"]
        assert "mock-llm" in msg
        assert "Helper" in msg  # assistant_name
        assert "What's up" in msg  # user_message

    @pytest.mark.asyncio
    async def test_metadata_method_is_mock_without_api_key(self, monkeypatch):
        """无 API key 时 metadata.method=mock。"""
        monkeypatch.setattr(ast, "_memory_extract", lambda *_a, **_kw: _return_empty_list())
        monkeypatch.setattr(ast, "_rag_query", lambda *_a, **_kw: _return_empty_list())
        monkeypatch.setattr(ast, "_memory_recall", lambda *_a, **_kw: _return_empty_list())
        conn = _RecordingConnection(
            results=[_Result(row=_assistant_row()), _Result(row=_run_row())]
        )
        monkeypatch.setattr(ast, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/assistants/ast_1/run",
                json={"user_message": "Hi"},
            )
        assert resp.status_code == 200
        assert resp.json()["metadata"]["method"] == "mock"


# ============================================================================
# 9. 辅助函数
# ============================================================================


class TestHelpers:
    def test_build_context_text_with_all_inputs(self):
        """_build_context_text 组装 RAG + memory + tools 上下文。"""
        chunks = [{"content": "chunk A"}, {"content": "chunk B"}]
        memories = [{"content": "user likes Python"}]
        tools = ["search", "calc"]
        text = _build_context_text(chunks, memories, tools)
        assert "[Retrieved Knowledge]" in text
        assert "chunk A" in text
        assert "[Recalled Memories]" in text
        assert "user likes Python" in text
        assert "[Available Tools]" in text
        assert "search" in text
        assert "calc" in text

    def test_build_context_text_empty(self):
        """空输入返回空字符串。"""
        assert _build_context_text([], [], []) == ""

    def test_build_mock_response_contains_metadata(self):
        """_build_mock_response 包含助手名/模型/用户消息。"""
        msg = _build_mock_response(
            assistant_name="Bot",
            model="gpt-4o",
            user_message="Hello",
            rag_chunks=[{"content": "x"}],
            memories=[{"content": "y"}],
            tools=["t1"],
        )
        assert "Bot" in msg
        assert "gpt-4o" in msg
        assert "Hello" in msg
        assert "rag_chunks=1" in msg
        assert "memories=1" in msg
        assert "tools=1" in msg


# ============================================================================
# 10. 删除隔离
# ============================================================================


class TestDeleteIsolation:
    @pytest.mark.asyncio
    async def test_delete_other_workspace_403(self, monkeypatch):
        """跨 workspace 删除助手返回 403。"""
        other_row = _assistant_row(workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=other_row)])
        monkeypatch.setattr(ast, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/assistants/ast_1")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_get_assistant_not_found_404(self, monkeypatch):
        """助手不存在返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(ast, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/assistants/missing")
        assert resp.status_code == 404


# ============================================================================
# 异步工具
# ============================================================================


async def _return_empty_list():
    """异步返回空 list。"""
    return []
