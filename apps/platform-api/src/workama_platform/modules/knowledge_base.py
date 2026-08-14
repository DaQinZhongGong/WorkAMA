"""知识库 / RAG 增强模块 (knowledge_base)。

v7.146: 知识库 CRUD / 文档上传与分块 / 确定性 embedding / RAG 查询 / 重建索引。

提供：
- 10 个 REST 端点（知识库 CRUD + 文档 CRUD + RAG 查询 + 重建索引）
- Worker 类 ``KnowledgeReindexWorker``，供 platform-worker 通过 job 队列调用
- 复用 ``memory_vector.vector_embedding`` 的 1536 维确定性 hash-based embedding
- 文档分块：按字符切分（默认 chunk_size=800, chunk_overlap=100），支持中英文混合
- RAG 查询：pgvector 余弦相似度（``1 - (embedding <=> query::vector)``），返回 top_k

设计文档：910-进度追踪与任务清单.md「P1 知识库/RAG 增强模块」
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from workama_platform.core import (
    Actor,
    capability_allows,
    get_actor,
    json_dumps,
    new_id,
    pool,
)
from workama_platform.modules.memory_vector import (
    EMBEDDING_DIMENSION,
    _vector_literal,
    vector_embedding,
)
from workama_platform.modules.search import rebuild_search_projection

# v7.249: knowledge ingest → search index 重建。默认启用，可通过
# ``WORKAMA_KB_AUTO_INDEX`` 环境变量关闭以保留原手工 /admin/operations 重建路径。
import os as _os

KB_AUTO_INDEX_ENABLED = _os.environ.get("WORKAMA_KB_AUTO_INDEX", "true").lower() not in {"0", "false", "no", "off"}


async def _trigger_search_index_rebuild(workspace_id: str) -> dict[str, int] | None:
    """v7.249: 知识库/文档变更后异步重建 search projection。

    单独连接保证事务独立，避免占用 KB 写入事务的资源；同步执行便于
    下一次 /search 请求能立即命中新创建的 KB/doc。
    如果 ``KB_AUTO_INDEX_ENABLED`` 为 False 则跳过（保留手工重建路径）。
    """
    if not KB_AUTO_INDEX_ENABLED:
        return None
    try:
        async with pool.connection() as conn:
            return await rebuild_search_projection(conn, workspace_id)
    except Exception:
        # 重建失败不阻塞 KB/doc 主流程；用户可手动到 /admin/operations 触发。
        return None

# ============================================================================
# 常量
# ============================================================================

router = APIRouter(prefix="/api/v1/knowledge-bases", tags=["knowledge-base"])

# Worker job 类型常量（platform-worker 通过这些常量路由 job）
KB_REINDEX_JOB_TYPE = "knowledge_reindex"

DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 100
DEFAULT_TOP_K = 5
MAX_TOP_K = 50
MAX_CONTENT_LENGTH = 1_000_000  # 单文档内容上限 1MB 字符

KBKind = Literal["general", "code", "faq", "product", "policy"]
SourceType = Literal["manual", "upload", "api", "crawl"]
DocumentStatus = Literal["pending", "processing", "ready", "failed"]

_VALID_KINDS: frozenset[str] = frozenset(
    {"general", "code", "faq", "product", "policy"}
)
_VALID_SOURCE_TYPES: frozenset[str] = frozenset(
    {"manual", "upload", "api", "crawl"}
)


# ============================================================================
# Pydantic 模型
# ============================================================================


class KBCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    kind: KBKind = "general"
    embedding_model: str = Field(default="text-embedding-3-small", max_length=100)
    embedding_dimensions: int = Field(default=EMBEDDING_DIMENSION, ge=1, le=4096)
    chunk_size: int = Field(default=DEFAULT_CHUNK_SIZE, ge=100, le=8000)
    chunk_overlap: int = Field(default=DEFAULT_CHUNK_OVERLAP, ge=0, le=4000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class KBResponse(BaseModel):
    id: str
    workspace_id: str
    name: str
    description: str | None = None
    kind: str
    embedding_model: str
    embedding_dimensions: int
    chunk_size: int
    chunk_overlap: int
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class DocumentUploadRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)
    source_type: SourceType = "manual"
    source_url: str | None = Field(default=None, max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentResponse(BaseModel):
    id: str
    knowledge_base_id: str
    workspace_id: str
    title: str
    source_type: str
    source_url: str | None = None
    content: str
    content_hash: str | None = None
    chunk_count: int
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ChunkResponse(BaseModel):
    id: str
    document_id: str
    knowledge_base_id: str
    workspace_id: str
    content: str
    chunk_index: int
    token_count: int
    similarity: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class RAGQueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=MAX_TOP_K)


class RAGResponse(BaseModel):
    query: str
    results: list[ChunkResponse]
    count: int
    top_k: int


# ============================================================================
# 辅助函数
# ============================================================================


def _require(actor: Actor, action: str) -> None:
    """检查 actor 是否拥有 knowledge_base:{action} 能力。"""
    if not capability_allows(actor.capabilities, f"knowledge_base:{action}"):
        raise HTTPException(
            status_code=403, detail=f"Missing capability: knowledge_base:{action}"
        )


def _kb_summary(row: dict) -> dict:
    """将 knowledge_base 行转为 API 响应 dict。"""
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "name": row["name"],
        "description": row.get("description"),
        "kind": row["kind"],
        "embedding_model": row["embedding_model"],
        "embedding_dimensions": row["embedding_dimensions"],
        "chunk_size": row["chunk_size"],
        "chunk_overlap": row["chunk_overlap"],
        "status": row["status"],
        "metadata": row.get("metadata") or {},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _doc_summary(row: dict) -> dict:
    """将 knowledge_document 行转为 API 响应 dict。"""
    return {
        "id": row["id"],
        "knowledge_base_id": row["knowledge_base_id"],
        "workspace_id": row["workspace_id"],
        "title": row["title"],
        "source_type": row["source_type"],
        "source_url": row.get("source_url"),
        "content": row["content"],
        "content_hash": row.get("content_hash"),
        "chunk_count": row["chunk_count"],
        "status": row["status"],
        "metadata": row.get("metadata") or {},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _chunk_summary(row: dict) -> dict:
    """将 knowledge_chunk 行转为 API 响应 dict。"""
    return {
        "id": row["id"],
        "document_id": row["document_id"],
        "knowledge_base_id": row["knowledge_base_id"],
        "workspace_id": row["workspace_id"],
        "content": row["content"],
        "chunk_index": row["chunk_index"],
        "token_count": row["token_count"],
        "similarity": float(row["similarity"]) if row.get("similarity") is not None else None,
        "metadata": row.get("metadata") or {},
        "created_at": row["created_at"],
    }


def _content_hash(content: str) -> str:
    """计算内容的 SHA256 哈希（十六进制）。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _split_chunks(content: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """按字符切分文档为 chunks。

    - chunk_size: 每个 chunk 的最大字符数
    - chunk_overlap: 相邻 chunk 的重叠字符数
    - step = chunk_size - chunk_overlap，必须 > 0
    - 支持中英文混合（按 Unicode 字符计）
    - 空内容返回 []，单段内容返回 [content]
    """
    if not content:
        return []
    size = max(1, chunk_size)
    overlap = max(0, min(chunk_overlap, size - 1))
    step = size - overlap
    if step <= 0:
        step = 1
    chunks: list[str] = []
    pos = 0
    total = len(content)
    while pos < total:
        chunk = content[pos : pos + size]
        chunks.append(chunk)
        if pos + size >= total:
            break
        pos += step
    return chunks


def _estimate_token_count(text: str) -> int:
    """估算 token 数：英文按 4 字符/token，中文按 1.5 字符/token 的粗略混合估计。

    简单实现：max(1, len(text) // 4)。
    """
    return max(1, len(text) // 4)


async def _owned_kb(conn: Any, kb_id: str, actor: Actor) -> dict:
    """查询知识库并校验 workspace 归属。

    - 不存在 → 404
    - 存在但 workspace 不匹配 → 403
    """
    result = await conn.execute(
        "SELECT * FROM knowledge_base WHERE id = %s",
        (kb_id,),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    if row["workspace_id"] != actor.workspace_id:
        raise HTTPException(
            status_code=403, detail="Knowledge base belongs to another workspace"
        )
    return row


async def _owned_document(
    conn: Any, kb_id: str, doc_id: str, actor: Actor
) -> dict:
    """查询文档并校验 workspace 归属。

    - 不存在 → 404
    - 存在但 workspace 不匹配 → 403
    """
    result = await conn.execute(
        "SELECT * FROM knowledge_document WHERE id = %s AND knowledge_base_id = %s",
        (doc_id, kb_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Document not found")
    if row["workspace_id"] != actor.workspace_id:
        raise HTTPException(
            status_code=403, detail="Document belongs to another workspace"
        )
    return row


async def _index_document(
    conn: Any,
    *,
    doc_id: str,
    kb_id: str,
    workspace_id: str,
    content: str,
    chunk_size: int,
    chunk_overlap: int,
) -> int:
    """为文档分块、生成 embedding 并写入 knowledge_chunk 表。

    返回写入的 chunk 数。调用方需在事务中调用。
    """
    chunks = _split_chunks(content, chunk_size, chunk_overlap)
    for idx, chunk_text in enumerate(chunks):
        embedding = vector_embedding(chunk_text)
        embedding_str = _vector_literal(embedding)
        chunk_id = new_id("kbc")
        token_count = _estimate_token_count(chunk_text)
        await conn.execute(
            """
            INSERT INTO knowledge_chunk(
                id, document_id, knowledge_base_id, workspace_id,
                content, chunk_index, embedding, token_count, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s, %s::jsonb)
            """,
            (
                chunk_id,
                doc_id,
                kb_id,
                workspace_id,
                chunk_text,
                idx,
                embedding_str,
                token_count,
                json_dumps({"chunk_size": len(chunk_text)}),
            ),
        )
    return len(chunks)


# ============================================================================
# Worker 类（供 platform-worker 通过 job 队列调用）
# ============================================================================


class KnowledgeReindexWorker:
    """重建知识库索引 Worker：重新分块 + embedding。

    供 platform-worker 通过 job 队列调用：
    ``await worker.process_reindex_job({"knowledge_base_id": "kb_xxx"})``
    """

    async def reindex_knowledge_base(self, kb_id: str) -> dict:
        """重建指定知识库下所有文档的索引。

        流程：
        1. 查询知识库（含 chunk_size / chunk_overlap）
        2. 删除该 KB 下所有旧 chunks
        3. 遍历所有 ready/failed 文档，重新分块 + embedding
        4. 更新每个文档的 chunk_count 与 status
        """
        async with pool.connection() as conn:
            async with conn.transaction():
                kb_result = await conn.execute(
                    "SELECT * FROM knowledge_base WHERE id = %s",
                    (kb_id,),
                )
                kb_row = await kb_result.fetchone()
                if not kb_row:
                    return {"kb_id": kb_id, "reindexed": 0, "deleted_chunks": 0, "error": "knowledge_base not found"}
                workspace_id = kb_row["workspace_id"]
                chunk_size = kb_row["chunk_size"]
                chunk_overlap = kb_row["chunk_overlap"]

                # 删除旧 chunks
                del_result = await conn.execute(
                    "DELETE FROM knowledge_chunk WHERE knowledge_base_id = %s RETURNING id",
                    (kb_id,),
                )
                deleted_rows = await del_result.fetchall()
                deleted_chunks = len(deleted_rows)

                # 重新索引所有文档
                doc_result = await conn.execute(
                    "SELECT * FROM knowledge_document WHERE knowledge_base_id = %s",
                    (kb_id,),
                )
                doc_rows = await doc_result.fetchall()
                doc_count = len(doc_rows)
                total_reindexed = 0
                for doc_row in doc_rows:
                    count = await _index_document(
                        conn,
                        doc_id=doc_row["id"],
                        kb_id=kb_id,
                        workspace_id=workspace_id,
                        content=doc_row["content"],
                        chunk_size=chunk_size,
                        chunk_overlap=chunk_overlap,
                    )
                    await conn.execute(
                        "UPDATE knowledge_document SET chunk_count = %s, status = 'ready', updated_at = now() WHERE id = %s",
                        (count, doc_row["id"]),
                    )
                    total_reindexed += count
        return {
            "kb_id": kb_id,
            "reindexed": total_reindexed,
            "deleted_chunks": deleted_chunks,
            "documents": doc_count,
        }

    async def process_reindex_job(self, payload: dict) -> dict:
        """处理重建索引 job（由 platform-worker 调用）。

        payload 必填字段：
        - ``knowledge_base_id``: 重建目标知识库 ID
        """
        kb_id = payload.get("knowledge_base_id") or ""
        if not kb_id:
            return {"error": "knowledge_base_id is required", "reindexed": 0}
        return await self.reindex_knowledge_base(kb_id)


# 模块级 Worker 实例（platform-worker 直接 import 使用）
reindex_worker = KnowledgeReindexWorker()


# ============================================================================
# Router 端点
# ============================================================================
#
# 路由声明顺序：具体路径（/ 等）与参数化路径（/{kb_id}）共存，
# FastAPI 按声明顺序匹配；空路径用于创建/列表，/{kb_id} 用于详情/删除。


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    body: KBCreateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """创建知识库。"""
    _require(actor, "write")
    if body.chunk_overlap >= body.chunk_size:
        raise HTTPException(
            status_code=422,
            detail="chunk_overlap must be smaller than chunk_size",
        )
    kb_id = new_id("kb")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                INSERT INTO knowledge_base(
                    id, workspace_id, name, description, kind,
                    embedding_model, embedding_dimensions, chunk_size, chunk_overlap,
                    status, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s::jsonb)
                RETURNING *
                """,
                (
                    kb_id,
                    actor.workspace_id,
                    body.name,
                    body.description,
                    body.kind,
                    body.embedding_model,
                    body.embedding_dimensions,
                    body.chunk_size,
                    body.chunk_overlap,
                    json_dumps(body.metadata),
                ),
            )
            row = await result.fetchone()
    # v7.249: 创建 KB 后自动重建 search projection，让 /search 立即命中新 KB。
    await _trigger_search_index_rebuild(actor.workspace_id)
    return _kb_summary(row)


@router.get("")
async def list_knowledge_bases(
    actor: Annotated[Actor, Depends(get_actor)],
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """列表：分页查询，支持 status 过滤和 workspace 隔离。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        status_clause = ""
        params: list[object] = [actor.workspace_id]
        if status_filter:
            status_clause = "AND status = %s"
            params.append(status_filter)
        params.append(limit)
        params.append(offset)
        result = await conn.execute(
            f"""
            SELECT * FROM knowledge_base
            WHERE workspace_id = %s {status_clause}
            ORDER BY created_at DESC LIMIT %s OFFSET %s
            """,
            tuple(params),
        )
        rows = await result.fetchall()
    items = [_kb_summary(row) for row in rows]
    return {
        "items": items,
        "data": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
    }


@router.get("/{kb_id}")
async def get_knowledge_base(
    kb_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """查询知识库详情。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        return _kb_summary(await _owned_kb(conn, kb_id, actor))


@router.delete("/{kb_id}", status_code=status.HTTP_200_OK)
async def delete_knowledge_base(
    kb_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """删除知识库（硬删除，级联删除文档与 chunks）。"""
    _require(actor, "delete")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _owned_kb(conn, kb_id, actor)
            result = await conn.execute(
                "DELETE FROM knowledge_base WHERE id = %s AND workspace_id = %s RETURNING id",
                (kb_id, actor.workspace_id),
            )
            row = await result.fetchone()
    # v7.249: 删除 KB 后触发 search projection 重建以 tombstone 旧文档。
    await _trigger_search_index_rebuild(actor.workspace_id)
    return {"id": row["id"], "deleted": True}


@router.post("/{kb_id}/documents", status_code=status.HTTP_201_CREATED)
async def upload_document(
    kb_id: str,
    body: DocumentUploadRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """上传/创建文档：自动分块 + 确定性 embedding 写入 knowledge_chunk。"""
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            kb = await _owned_kb(conn, kb_id, actor)
            doc_id = new_id("kbd")
            content_hash = _content_hash(body.content)
            # 先创建文档（status=processing），分块后更新为 ready
            result = await conn.execute(
                """
                INSERT INTO knowledge_document(
                    id, knowledge_base_id, workspace_id, title, source_type,
                    source_url, content, content_hash, chunk_count, status, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, 'processing', %s::jsonb)
                RETURNING *
                """,
                (
                    doc_id,
                    kb_id,
                    actor.workspace_id,
                    body.title,
                    body.source_type,
                    body.source_url,
                    body.content,
                    content_hash,
                    json_dumps(body.metadata),
                ),
            )
            doc_row = await result.fetchone()

            # 分块 + embedding
            chunk_count = await _index_document(
                conn,
                doc_id=doc_id,
                kb_id=kb_id,
                workspace_id=actor.workspace_id,
                content=body.content,
                chunk_size=kb["chunk_size"],
                chunk_overlap=kb["chunk_overlap"],
            )

            # 更新文档状态
            upd_result = await conn.execute(
                """
                UPDATE knowledge_document
                SET chunk_count = %s, status = 'ready', updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (chunk_count, doc_id),
            )
            doc_row = await upd_result.fetchone()
    # v7.249: 文档写入完成后自动重建 search projection，让 /search 命中新内容。
    await _trigger_search_index_rebuild(actor.workspace_id)
    return _doc_summary(doc_row)


@router.get("/{kb_id}/documents")
async def list_documents(
    kb_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """文档列表：分页查询，支持 status 过滤和 workspace 隔离。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        await _owned_kb(conn, kb_id, actor)
        status_clause = ""
        params: list[object] = [kb_id, actor.workspace_id]
        if status_filter:
            status_clause = "AND status = %s"
            params.append(status_filter)
        params.append(limit)
        params.append(offset)
        result = await conn.execute(
            f"""
            SELECT * FROM knowledge_document
            WHERE knowledge_base_id = %s AND workspace_id = %s {status_clause}
            ORDER BY created_at DESC LIMIT %s OFFSET %s
            """,
            tuple(params),
        )
        rows = await result.fetchall()
    items = [_doc_summary(row) for row in rows]
    return {
        "items": items,
        "data": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
    }


@router.get("/{kb_id}/documents/{doc_id}")
async def get_document(
    kb_id: str,
    doc_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """查询文档详情。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        return _doc_summary(await _owned_document(conn, kb_id, doc_id, actor))


@router.delete("/{kb_id}/documents/{doc_id}", status_code=status.HTTP_200_OK)
async def delete_document(
    kb_id: str,
    doc_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """删除文档（硬删除，级联删除其 chunks）。"""
    _require(actor, "delete")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _owned_document(conn, kb_id, doc_id, actor)
            result = await conn.execute(
                "DELETE FROM knowledge_document WHERE id = %s AND knowledge_base_id = %s "
                "AND workspace_id = %s RETURNING id",
                (doc_id, kb_id, actor.workspace_id),
            )
            row = await result.fetchone()
    # v7.249: 删除文档后触发 search projection 重建以 tombstone 旧文档。
    await _trigger_search_index_rebuild(actor.workspace_id)
    return {"id": row["id"], "deleted": True}


@router.post("/{kb_id}/rag/query")
async def rag_query(
    kb_id: str,
    body: RAGQueryRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """RAG 查询：用 query 的 embedding 与 knowledge_chunk.embedding 做余弦相似度，返回 top_k。

    相似度 = 1 - cosine_distance（pgvector `<=>` 操作符）。
    """
    _require(actor, "read")
    query_embedding = vector_embedding(body.query)
    query_vec_str = _vector_literal(query_embedding)
    async with pool.connection() as conn:
        await _owned_kb(conn, kb_id, actor)
        result = await conn.execute(
            """
            SELECT *, 1 - (embedding <=> %s::vector) AS similarity
            FROM knowledge_chunk
            WHERE knowledge_base_id = %s AND workspace_id = %s
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (query_vec_str, kb_id, actor.workspace_id, query_vec_str, body.top_k),
        )
        rows = await result.fetchall()
    items = [_chunk_summary(row) for row in rows]
    return {
        "query": body.query,
        "results": items,
        "data": items,
        "count": len(items),
        "top_k": body.top_k,
    }


@router.post("/{kb_id}/reindex", status_code=status.HTTP_200_OK)
async def reindex_knowledge_base(
    kb_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """重建知识库索引：重新分块 + embedding。"""
    _require(actor, "write")
    async with pool.connection() as conn:
        await _owned_kb(conn, kb_id, actor)
    return await reindex_worker.reindex_knowledge_base(kb_id)
