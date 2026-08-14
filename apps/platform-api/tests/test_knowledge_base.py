"""知识库 / RAG 增强模块 (knowledge_base) 单元 + 端点测试。

v7.146: 28 个测试覆盖：
- 知识库 CRUD：创建 / 列表 / 详情 / 删除 / 字段校验 (5)
- 文档上传：成功 / 分块 / 字段校验 / KB 不存在 404 (4)
- 文档列表 / 详情 / 删除 (3)
- RAG 查询：成功 / top_k 限制 / 无结果 / 相似度排序 (4)
- 重建索引：成功 / KB 不存在 404 (2)
- workspace 隔离：跨区 KB 详情 403 / 跨区文档 403 / 跨区列表隔离 (3)
- 鉴权：未认证 401 (1)
- 边界：空 content / 超大 content / 重复创建 (3)
- Worker：reindex job 透传 / chunk 切分函数 (3)

所有测试使用 fake pool/connection，不依赖真实 DB / Redis / 网络 / LLM API。
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules import knowledge_base as kb
from workama_platform.modules.knowledge_base import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_TOP_K,
    KB_REINDEX_JOB_TYPE,
    KnowledgeReindexWorker,
    _split_chunks,
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
    capabilities=("knowledge_base:*",),
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


def _kb_row(**overrides) -> dict:
    base = {
        "id": "kb_1",
        "workspace_id": "wsp_test",
        "name": "Test KB",
        "description": "A test knowledge base",
        "kind": "general",
        "embedding_model": "text-embedding-3-small",
        "embedding_dimensions": 1536,
        "chunk_size": DEFAULT_CHUNK_SIZE,
        "chunk_overlap": DEFAULT_CHUNK_OVERLAP,
        "status": "active",
        "metadata": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _doc_row(**overrides) -> dict:
    base = {
        "id": "kbd_1",
        "knowledge_base_id": "kb_1",
        "workspace_id": "wsp_test",
        "title": "Doc 1",
        "source_type": "manual",
        "source_url": None,
        "content": "Hello world",
        "content_hash": "abc123",
        "chunk_count": 1,
        "status": "ready",
        "metadata": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _chunk_row(**overrides) -> dict:
    base = {
        "id": "kbc_1",
        "document_id": "kbd_1",
        "knowledge_base_id": "kb_1",
        "workspace_id": "wsp_test",
        "content": "Hello world",
        "chunk_index": 0,
        "token_count": 2,
        "metadata": {},
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(kb.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


def _doc_upload_results(
    kb_row: dict,
    doc_row: dict,
    content: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list:
    """构造 upload_document 端点所需的 _Result 序列。

    调用顺序：
    1. _owned_kb SELECT (1 row)
    2. INSERT knowledge_document RETURNING (1 row)
    3..n. _index_document 每个 chunk 1 次 INSERT（无返回）
    4. UPDATE knowledge_document RETURNING (1 row)
    """
    chunks = _split_chunks(content, chunk_size, chunk_overlap)
    results: list = [
        _Result(row=kb_row),  # _owned_kb
        _Result(row=doc_row),  # INSERT RETURNING
    ]
    # 每个 chunk 一次 INSERT（_Result() 默认空）
    for _ in chunks:
        results.append(_Result())
    # UPDATE RETURNING
    results.append(_Result(row={**doc_row, "chunk_count": len(chunks), "status": "ready"}))
    return results


# ============================================================================
# 1. 知识库 CRUD
# ============================================================================


class TestKBCRUD:
    """知识库 CRUD 端点测试。"""

    @pytest.mark.asyncio
    async def test_create_knowledge_base_success(self, monkeypatch):
        """POST / 创建知识库返回 201。"""
        row = _kb_row(id="kb_new", name="My KB")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(kb, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/knowledge-bases",
                json={"name": "My KB", "kind": "general"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "My KB"
        assert body["kind"] == "general"
        assert body["status"] == "active"
        assert body["embedding_dimensions"] == 1536
        # 确认 INSERT 语句
        assert any("INSERT INTO knowledge_base" in q for q, _ in conn.calls)

    @pytest.mark.asyncio
    async def test_list_knowledge_bases_pagination(self, monkeypatch):
        """GET / 分页返回知识库列表。"""
        rows = [_kb_row(id="kb_1"), _kb_row(id="kb_2")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(kb, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/knowledge-bases?limit=10&offset=0")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert body["limit"] == 10
        assert body["offset"] == 0
        assert len(body["items"]) == 2

    @pytest.mark.asyncio
    async def test_get_knowledge_base_exists(self, monkeypatch):
        """GET /{kb_id} 返回知识库详情。"""
        row = _kb_row(id="kb_1", name="Detail KB")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(kb, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/knowledge-bases/kb_1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "kb_1"
        assert body["name"] == "Detail KB"

    @pytest.mark.asyncio
    async def test_delete_knowledge_base_success(self, monkeypatch):
        """DELETE /{kb_id} 删除知识库返回 200。"""
        existing = _kb_row(id="kb_1")
        conn = _RecordingConnection(
            results=[_Result(row=existing), _Result(row={"id": "kb_1"})]
        )
        monkeypatch.setattr(kb, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/knowledge-bases/kb_1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "kb_1"
        assert body["deleted"] is True
        assert "DELETE FROM knowledge_base" in conn.calls[1][0]

    @pytest.mark.asyncio
    async def test_create_knowledge_base_rejects_invalid_kind(self):
        """POST / 非法 kind 触发 422 校验错误。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/knowledge-bases",
                json={"name": "Bad", "kind": "invalid_kind"},
            )
        assert resp.status_code == 422


# ============================================================================
# 2. 文档上传
# ============================================================================


class TestDocumentUpload:
    """文档上传端点测试。"""

    @pytest.mark.asyncio
    async def test_upload_document_success(self, monkeypatch):
        """POST /{kb_id}/documents 上传文档返回 201，自动分块+embedding。"""
        kb_row = _kb_row(id="kb_1", chunk_size=800, chunk_overlap=100)
        content = "Hello world, this is a test document."
        doc_row = _doc_row(
            id="kbd_new", title="My Doc", content=content, status="processing"
        )
        results = _doc_upload_results(
            kb_row, doc_row, content, chunk_size=800, chunk_overlap=100
        )
        conn = _RecordingConnection(results=results)
        monkeypatch.setattr(kb, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/knowledge-bases/kb_1/documents",
                json={"title": "My Doc", "content": content},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["title"] == "My Doc"
        assert body["status"] == "ready"
        assert body["chunk_count"] == 1
        # 验证 SQL 含 INSERT INTO knowledge_document 和 knowledge_chunk
        sqls = [q for q, _ in conn.calls]
        assert any("INSERT INTO knowledge_document" in q for q in sqls)
        assert any("INSERT INTO knowledge_chunk" in q for q in sqls)

    @pytest.mark.asyncio
    async def test_upload_document_chunks_long_content(self, monkeypatch):
        """POST /{kb_id}/documents 长内容被切分为多个 chunk。"""
        # chunk_size=100, overlap=20 -> step=80
        kb_row = _kb_row(id="kb_1", chunk_size=100, chunk_overlap=20)
        content = "A" * 250  # 250 字符 -> 约 3-4 chunks
        doc_row = _doc_row(id="kbd_new", content=content)
        results = _doc_upload_results(
            kb_row, doc_row, content, chunk_size=100, chunk_overlap=20
        )
        conn = _RecordingConnection(results=results)
        monkeypatch.setattr(kb, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/knowledge-bases/kb_1/documents",
                json={"title": "Long Doc", "content": content},
            )
        assert resp.status_code == 201
        body = resp.json()
        # 250 字符 / step=80 -> positions 0,80,160,240 -> 4 chunks
        expected_chunks = len(_split_chunks(content, 100, 20))
        assert body["chunk_count"] == expected_chunks
        assert expected_chunks >= 3
        # 验证 chunk INSERT 次数
        chunk_inserts = [q for q, _ in conn.calls if "INSERT INTO knowledge_chunk" in q]
        assert len(chunk_inserts) == expected_chunks

    @pytest.mark.asyncio
    async def test_upload_document_field_validation_rejects_empty_title(self):
        """POST /{kb_id}/documents 空 title 触发 422。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/knowledge-bases/kb_1/documents",
                json={"title": "", "content": "hello"},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_upload_document_returns_404_when_kb_missing(self, monkeypatch):
        """POST /{kb_id}/documents KB 不存在返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(kb, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/knowledge-bases/missing/documents",
                json={"title": "Doc", "content": "hello"},
            )
        assert resp.status_code == 404


# ============================================================================
# 3. 文档列表 / 详情 / 删除
# ============================================================================


class TestDocumentCRUD:
    """文档列表 / 详情 / 删除端点测试。"""

    @pytest.mark.asyncio
    async def test_list_documents(self, monkeypatch):
        """GET /{kb_id}/documents 返回文档列表。"""
        kb_row = _kb_row(id="kb_1")
        docs = [_doc_row(id="kbd_1"), _doc_row(id="kbd_2")]
        conn = _RecordingConnection(
            results=[_Result(row=kb_row), _Result(rows=docs)]
        )
        monkeypatch.setattr(kb, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/knowledge-bases/kb_1/documents")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert len(body["items"]) == 2

    @pytest.mark.asyncio
    async def test_get_document_exists(self, monkeypatch):
        """GET /{kb_id}/documents/{doc_id} 返回文档详情。"""
        doc = _doc_row(id="kbd_1", title="Detail Doc")
        conn = _RecordingConnection(results=[_Result(row=doc)])
        monkeypatch.setattr(kb, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/knowledge-bases/kb_1/documents/kbd_1"
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "kbd_1"
        assert body["title"] == "Detail Doc"

    @pytest.mark.asyncio
    async def test_delete_document_success(self, monkeypatch):
        """DELETE /{kb_id}/documents/{doc_id} 删除文档返回 200。"""
        doc = _doc_row(id="kbd_1")
        conn = _RecordingConnection(
            results=[_Result(row=doc), _Result(row={"id": "kbd_1"})]
        )
        monkeypatch.setattr(kb, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete(
                "/api/v1/knowledge-bases/kb_1/documents/kbd_1"
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "kbd_1"
        assert body["deleted"] is True
        assert "DELETE FROM knowledge_document" in conn.calls[1][0]


# ============================================================================
# 4. RAG 查询
# ============================================================================


class TestRAGQuery:
    """RAG 查询端点测试。"""

    @pytest.mark.asyncio
    async def test_rag_query_success(self, monkeypatch):
        """POST /{kb_id}/rag/query 返回相关 chunks。"""
        kb_row = _kb_row(id="kb_1")
        chunk = _chunk_row(
            id="kbc_1", content="Hello world", similarity=0.95
        )
        conn = _RecordingConnection(
            results=[_Result(row=kb_row), _Result(rows=[chunk])]
        )
        monkeypatch.setattr(kb, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/knowledge-bases/kb_1/rag/query",
                json={"query": "hello", "top_k": 5},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["query"] == "hello"
        assert body["count"] == 1
        assert body["top_k"] == 5
        assert body["results"][0]["similarity"] == pytest.approx(0.95)
        # 验证 SQL 含 cosine 距离操作符
        rag_sql = conn.calls[1][0]
        assert "<=>" in rag_sql
        assert "ORDER BY embedding" in rag_sql

    @pytest.mark.asyncio
    async def test_rag_query_respects_top_k(self, monkeypatch):
        """POST /{kb_id}/rag/query top_k 限制返回数量。"""
        kb_row = _kb_row(id="kb_1")
        # 返回 2 条（即使库里有更多，SQL LIMIT 2）
        chunks = [
            _chunk_row(id="kbc_1", similarity=0.9),
            _chunk_row(id="kbc_2", similarity=0.8),
        ]
        conn = _RecordingConnection(
            results=[_Result(row=kb_row), _Result(rows=chunks)]
        )
        monkeypatch.setattr(kb, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/knowledge-bases/kb_1/rag/query",
                json={"query": "test", "top_k": 2},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert body["top_k"] == 2
        # 验证 SQL LIMIT 参数为 2
        rag_params = conn.calls[1][1]
        assert rag_params[-1] == 2

    @pytest.mark.asyncio
    async def test_rag_query_no_results(self, monkeypatch):
        """POST /{kb_id}/rag/query 无匹配 chunks 返回空数组。"""
        kb_row = _kb_row(id="kb_1")
        conn = _RecordingConnection(
            results=[_Result(row=kb_row), _Result(rows=[])]
        )
        monkeypatch.setattr(kb, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/knowledge-bases/kb_1/rag/query",
                json={"query": "empty"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 0
        assert body["results"] == []

    @pytest.mark.asyncio
    async def test_rag_query_similarity_ordering(self, monkeypatch):
        """POST /{kb_id}/rag/query 结果按相似度降序（SQL ORDER BY embedding <=>）。"""
        kb_row = _kb_row(id="kb_1")
        chunks = [
            _chunk_row(id="kbc_1", similarity=0.95),
            _chunk_row(id="kbc_2", similarity=0.80),
            _chunk_row(id="kbc_3", similarity=0.65),
        ]
        conn = _RecordingConnection(
            results=[_Result(row=kb_row), _Result(rows=chunks)]
        )
        monkeypatch.setattr(kb, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/knowledge-bases/kb_1/rag/query",
                json={"query": "search"},
            )
        assert resp.status_code == 200
        body = resp.json()
        sims = [r["similarity"] for r in body["results"]]
        assert sims == sorted(sims, reverse=True)
        # SQL 必须含 ORDER BY ... <=> （余弦距离升序 = 相似度降序）
        rag_sql = conn.calls[1][0]
        assert "ORDER BY embedding <=> %s::vector" in rag_sql


# ============================================================================
# 5. 重建索引
# ============================================================================


class TestReindex:
    """重建索引端点测试。"""

    @pytest.mark.asyncio
    async def test_reindex_knowledge_base_success(self, monkeypatch):
        """POST /{kb_id}/reindex 重建索引成功。"""
        # reindex 端点: _owned_kb SELECT (1)
        # reindex_worker.reindex_knowledge_base:
        #   1. SELECT kb (1)
        #   2. DELETE chunks RETURNING (1, fetchall)
        #   3. SELECT docs (1, fetchall)
        #   4..n. per doc: _index_document (n_chunks INSERTs) + UPDATE (1)
        kb_row = _kb_row(id="kb_1", chunk_size=800)
        doc_row = _doc_row(id="kbd_1", content="Hello world")
        # 预计算 chunks 数量
        chunks_count = len(_split_chunks("Hello world", 800, 100))
        results = [
            _Result(row=kb_row),  # reindex 端点 _owned_kb
            _Result(row=kb_row),  # worker SELECT kb
            _Result(rows=[{"id": "kbc_old"}]),  # DELETE RETURNING
            _Result(rows=[doc_row]),  # SELECT docs
        ]
        # 每个 chunk 1 次 INSERT
        for _ in range(chunks_count):
            results.append(_Result())
        # UPDATE knowledge_document (无返回需要)
        results.append(_Result())
        conn = _RecordingConnection(results=results)
        monkeypatch.setattr(kb, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/knowledge-bases/kb_1/reindex")
        assert resp.status_code == 200
        body = resp.json()
        assert body["kb_id"] == "kb_1"
        assert body["reindexed"] == chunks_count
        assert body["documents"] == 1
        assert body["deleted_chunks"] == 1

    @pytest.mark.asyncio
    async def test_reindex_returns_404_when_kb_missing(self, monkeypatch):
        """POST /{kb_id}/reindex KB 不存在返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(kb, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/knowledge-bases/missing/reindex")
        assert resp.status_code == 404


# ============================================================================
# 6. workspace 隔离
# ============================================================================


class TestWorkspaceIsolation:
    """workspace 隔离测试。"""

    @pytest.mark.asyncio
    async def test_get_kb_returns_403_cross_workspace(self, monkeypatch):
        """GET /{kb_id} KB 属于其他 workspace 返回 403。"""
        row = _kb_row(id="kb_1", workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(kb, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/knowledge-bases/kb_1")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_get_document_returns_403_cross_workspace(self, monkeypatch):
        """GET /{kb_id}/documents/{doc_id} 文档属于其他 workspace 返回 403。"""
        doc = _doc_row(id="kbd_1", workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=doc)])
        monkeypatch.setattr(kb, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/knowledge-bases/kb_1/documents/kbd_1"
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_list_kbs_filters_by_workspace(self, monkeypatch):
        """GET / 列表 SQL 强制按 actor.workspace_id 过滤。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(kb, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_isolated"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/knowledge-bases")
        assert resp.status_code == 200
        query, params = conn.calls[0]
        assert "WHERE workspace_id = %s" in query
        assert params[0] == "wsp_isolated"


# ============================================================================
# 7. 鉴权
# ============================================================================


class TestAuth:
    """鉴权测试。"""

    @pytest.mark.asyncio
    async def test_list_kbs_requires_authentication(self):
        """未认证请求 GET / 返回 401。"""
        app = _app(actor=None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/knowledge-bases")
        assert resp.status_code == 401


# ============================================================================
# 8. 边界 / 集成测试
# ============================================================================


class TestEdgeCases:
    """边界与集成测试。"""

    @pytest.mark.asyncio
    async def test_upload_document_rejects_empty_content(self):
        """POST /{kb_id}/documents 空 content 触发 422。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/knowledge-bases/kb_1/documents",
                json={"title": "Empty", "content": ""},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_upload_document_rejects_oversized_content(self):
        """POST /{kb_id}/documents 超大 content（> 1MB）触发 422。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/knowledge-bases/kb_1/documents",
                json={"title": "Big", "content": "x" * 1_000_001},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_create_duplicate_knowledge_bases_independent_ids(
        self, monkeypatch
    ):
        """连续两次创建知识库生成独立 ID（new_id 不碰撞）。"""
        row1 = _kb_row(id="kb_a", name="KB A")
        row2 = _kb_row(id="kb_b", name="KB B")
        # v7.263: 每次 POST 会执行 INSERT（消耗 1 个结果）+ v7.249 自动索引
        # rebuild_search_projection（7 类 × SELECT + UPDATE tombstone = 14 个结果，
        # 源表为空时无 INSERT）；mock 需为副作用查询预留空结果，否则后续 POST 的
        # fetchone 拿到 None。
        conn = _RecordingConnection(
            results=[_Result(row=row1)] + [_Result()] * 14
                    + [_Result(row=row2)] + [_Result()] * 14
        )
        monkeypatch.setattr(kb, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r1 = await client.post(
                "/api/v1/knowledge-bases", json={"name": "KB A"}
            )
            r2 = await client.post(
                "/api/v1/knowledge-bases", json={"name": "KB B"}
            )
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["id"] == "kb_a"
        assert r2.json()["id"] == "kb_b"
        assert r1.json()["id"] != r2.json()["id"]


# ============================================================================
# 9. Worker 与工具函数
# ============================================================================


class TestWorkerAndHelpers:
    """Worker 与工具函数测试。"""

    @pytest.mark.asyncio
    async def test_process_reindex_job_delegates_with_payload(self, monkeypatch):
        """process_reindex_job 按 payload 透传 knowledge_base_id。"""
        kb_row = _kb_row(id="kb_job", chunk_size=800)
        results = [
            _Result(row=kb_row),  # SELECT kb
            _Result(rows=[]),  # DELETE RETURNING (空)
            _Result(rows=[]),  # SELECT docs (空)
        ]
        conn = _RecordingConnection(results=results)
        monkeypatch.setattr(kb, "pool", _Pool(conn))

        worker = KnowledgeReindexWorker()
        result = await worker.process_reindex_job({"knowledge_base_id": "kb_job"})
        assert result["kb_id"] == "kb_job"
        assert result["reindexed"] == 0
        assert result["documents"] == 0
        # 验证 SELECT/DELETE/SELECT 都按 kb_id 过滤
        select_kb_sql, select_kb_params = conn.calls[0]
        assert "FROM knowledge_base WHERE id = %s" in select_kb_sql
        assert select_kb_params[0] == "kb_job"

    @pytest.mark.asyncio
    async def test_process_reindex_job_requires_kb_id(self, monkeypatch):
        """process_reindex_job 缺少 knowledge_base_id 返回 error。"""
        worker = KnowledgeReindexWorker()
        result = await worker.process_reindex_job({})
        assert "error" in result
        assert result["reindexed"] == 0

    def test_split_chunks_basic(self):
        """_split_chunks 按字符切分并带 overlap。"""
        content = "abcdefghij" * 30  # 300 字符
        chunks = _split_chunks(content, chunk_size=100, chunk_overlap=20)
        assert len(chunks) >= 3
        # 每个 chunk 长度 <= chunk_size
        assert all(len(c) <= 100 for c in chunks)
        # 第一个 chunk 是前 100 字符
        assert chunks[0] == content[:100]

    def test_split_chunks_short_content_single_chunk(self):
        """_split_chunks 内容短于 chunk_size 返回单 chunk。"""
        content = "short"
        chunks = _split_chunks(content, chunk_size=800, chunk_overlap=100)
        assert chunks == ["short"]

    def test_split_chunks_empty_content(self):
        """_split_chunks 空内容返回空列表。"""
        assert _split_chunks("", chunk_size=800, chunk_overlap=100) == []

    def test_split_chunks_chinese_mixed(self):
        """_split_chunks 支持中英文混合（按 Unicode 字符计）。"""
        content = "Hello你好" * 50  # 500 字符（中英混合）
        chunks = _split_chunks(content, chunk_size=100, chunk_overlap=20)
        assert len(chunks) >= 5
        # 每个 chunk 长度 <= chunk_size（按字符，不是字节）
        assert all(len(c) <= 100 for c in chunks)

    def test_job_type_constant_is_stable(self):
        """KB_REINDEX_JOB_TYPE 常量稳定，供 worker 路由。"""
        assert KB_REINDEX_JOB_TYPE == "knowledge_reindex"

    def test_default_constants(self):
        """默认常量值稳定。"""
        assert DEFAULT_CHUNK_SIZE == 800
        assert DEFAULT_CHUNK_OVERLAP == 100
        assert DEFAULT_TOP_K == 5
