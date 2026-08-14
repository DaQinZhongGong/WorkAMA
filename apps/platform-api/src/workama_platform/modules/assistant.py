"""P1 助手模块 (assistant) - 整合 gateway LLM + knowledge_base RAG + memory_vector + mcp_server。

v7.151: 助手 CRUD / 运行 / 历史 / 克隆。

提供：
- 8 个 REST 端点（创建 / 列表 / 详情 / 更新 / 删除 / 运行 / 历史 / 克隆）
- 运行逻辑：RAG 上下文召回 + memory_vector 记忆召回 + gateway LLM 调用 + memory 抽取写入
- LLM 调用支持真实 gateway（``WORKAMA_GATEWAY_URL`` + ``WORKAMA_INTERNAL_LLM_API_KEY``），
  未配置或失败时回退到确定性 mock 响应（不依赖外部服务）
- MCP 工具：简化实现，仅在 metadata 中记录可用工具，不真实调用
- 与既有 ``pf_assistant`` 表独立共存（pf_assistant 由 workflows.py 使用）

设计文档：510-AI中台核心设计.md「assistant 对话助手」；910-进度追踪与任务清单.md v7.151
"""
from __future__ import annotations

import json
import logging
import os
import time
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
from workama_platform.modules.gateway.llm_client import call_llm

# ============================================================================
# 常量
# ============================================================================

LOGGER = logging.getLogger("workama.platform-api.assistant")

router = APIRouter(prefix="/api/v1/assistants", tags=["assistant"])

AssistantStatus = Literal["active", "archived"]
RunStatus = Literal["pending", "running", "completed", "failed"]

_VALID_STATUSES: frozenset[str] = frozenset({"active", "archived"})
_VALID_RUN_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "completed", "failed"}
)

# mock LLM 响应模板（确定性，不依赖外部服务）
_MOCK_RESPONSE_TEMPLATE = (
    "[mock-llm] assistant={assistant_name} model={model} "
    "rag_chunks={rag_chunks} memories={memories} tools={tools} | user_message={user_message}"
)


# ============================================================================
# Pydantic 模型
# ============================================================================


class AssistantCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    system_prompt: str = Field(min_length=1, max_length=16000)
    model: str = Field(default="gpt-4o-mini", max_length=200)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, ge=1, le=32768)
    tools: list[str] = Field(default_factory=list)
    knowledge_base_ids: list[str] = Field(default_factory=list)
    memory_enabled: bool = True
    status: AssistantStatus = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssistantUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    system_prompt: str | None = Field(default=None, min_length=1, max_length=16000)
    model: str | None = Field(default=None, max_length=200)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1, le=32768)
    tools: list[str] | None = None
    knowledge_base_ids: list[str] | None = None
    memory_enabled: bool | None = None
    status: AssistantStatus | None = None
    metadata: dict[str, Any] | None = None


class AssistantResponse(BaseModel):
    id: str
    workspace_id: str
    name: str
    description: str | None = None
    system_prompt: str
    model: str
    temperature: float
    max_tokens: int
    tools: list[str] = Field(default_factory=list)
    knowledge_base_ids: list[str] = Field(default_factory=list)
    memory_enabled: bool
    status: str
    version: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class AssistantVersion(BaseModel):
    """助手版本快照（用于克隆时记录历史版本）。"""

    version: int
    snapshot: dict[str, Any]


class AssistantRunRequest(BaseModel):
    user_message: str = Field(min_length=1, max_length=32000)
    model: str | None = Field(default=None, max_length=200)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1, le=32768)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssistantRunResponse(BaseModel):
    id: str
    assistant_id: str
    workspace_id: str
    user_message: str
    assistant_message: str
    model: str
    tokens_used: int
    duration_ms: int
    status: str
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class CloneRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


# ============================================================================
# 辅助函数
# ============================================================================


def _require(actor: Actor, action: str) -> None:
    """检查 actor 是否拥有 assistant:{action} 能力。"""
    if not capability_allows(actor.capabilities, f"assistant:{action}"):
        raise HTTPException(
            status_code=403, detail=f"Missing capability: assistant:{action}"
        )


def _summary(row: dict) -> dict:
    """将 assistant 行转为 API 响应 dict。"""
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "name": row["name"],
        "description": row.get("description"),
        "system_prompt": row["system_prompt"],
        "model": row["model"],
        "temperature": float(row["temperature"]),
        "max_tokens": int(row["max_tokens"]),
        "tools": list(row.get("tools") or []),
        "knowledge_base_ids": list(row.get("knowledge_base_ids") or []),
        "memory_enabled": bool(row["memory_enabled"]),
        "status": row["status"],
        "version": int(row["version"]),
        "metadata": row.get("metadata") or {},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _run_summary(row: dict) -> dict:
    """将 assistant_run 行转为 API 响应 dict。"""
    return {
        "id": row["id"],
        "assistant_id": row["assistant_id"],
        "workspace_id": row["workspace_id"],
        "user_message": row["user_message"],
        "assistant_message": row.get("assistant_message"),
        "model": row.get("model"),
        "tokens_used": int(row.get("tokens_used") or 0),
        "duration_ms": int(row.get("duration_ms") or 0),
        "status": row["status"],
        "error": row.get("error"),
        "metadata": row.get("metadata") or {},
        "created_at": row["created_at"],
    }


async def _owned_assistant(conn: Any, assistant_id: str, actor: Actor) -> dict:
    """查询助手并校验 workspace 归属。

    - 不存在 → 404
    - 存在但 workspace 不匹配 → 403
    """
    result = await conn.execute(
        "SELECT * FROM assistant WHERE id = %s",
        (assistant_id,),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Assistant not found")
    if row["workspace_id"] != actor.workspace_id:
        raise HTTPException(
            status_code=403, detail="Assistant belongs to another workspace"
        )
    return row


# ============================================================================
# LLM / RAG / Memory 调用（确定性 mock fallback + 真实 gateway 调用结构）
# ============================================================================


def _llm_disabled() -> bool:
    """WORKAMA_ASSISTANT_DISABLE_LLM=1 时强制走 mock。"""
    return os.getenv("WORKAMA_ASSISTANT_DISABLE_LLM", "").strip() in (
        "1",
        "true",
        "yes",
    )


def _build_mock_response(
    *,
    assistant_name: str,
    model: str,
    user_message: str,
    rag_chunks: list[dict],
    memories: list[dict],
    tools: list[str],
) -> str:
    """生成确定性 mock LLM 响应（不依赖外部服务）。"""
    return _MOCK_RESPONSE_TEMPLATE.format(
        assistant_name=assistant_name,
        model=model,
        rag_chunks=len(rag_chunks),
        memories=len(memories),
        tools=len(tools),
        user_message=user_message[:200],
    )


async def _rag_query(
    workspace_id: str,
    knowledge_base_ids: list[str],
    query: str,
    top_k: int = 5,
) -> list[dict]:
    """从 knowledge_base 模块召回 RAG 上下文。

    返回 list[dict]：每个 dict 含 {chunk_id, content, similarity}。
    失败时返回空列表（不阻断助手运行）。
    """
    if not knowledge_base_ids:
        return []
    chunks: list[dict] = []
    try:
        from workama_platform.modules.memory_vector import (
            _vector_literal,
            vector_embedding,
        )

        embedding = vector_embedding(query)
        embedding_str = _vector_literal(embedding)
        async with pool.connection() as conn:
            for kb_id in knowledge_base_ids:
                result = await conn.execute(
                    """
                    SELECT id, content, 1 - (embedding <=> %s::vector) AS similarity
                    FROM knowledge_chunk
                    WHERE knowledge_base_id = %s AND workspace_id = %s
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (embedding_str, kb_id, workspace_id, embedding_str, top_k),
                )
                rows = await result.fetchall()
                for row in rows:
                    chunks.append(
                        {
                            "chunk_id": row["id"],
                            "knowledge_base_id": kb_id,
                            "content": row["content"],
                            "similarity": float(row.get("similarity") or 0.0),
                        }
                    )
    except Exception as exc:  # pragma: no cover - 防御性容错
        LOGGER.warning("RAG query failed: %s", exc)
    return chunks


async def _memory_recall(workspace_id: str, user_id: str, query: str, top_k: int = 5) -> list[dict]:
    """从 memory_vector 模块召回相关记忆。

    返回 list[dict]：每个 dict 含 {memory_id, content, similarity}。
    失败时返回空列表（不阻断助手运行）。
    """
    memories: list[dict] = []
    try:
        from workama_platform.modules.memory_vector import (
            _vector_literal,
            vector_embedding,
        )

        embedding = vector_embedding(query)
        embedding_str = _vector_literal(embedding)
        async with pool.connection() as conn:
            result = await conn.execute(
                """
                SELECT memory_id, content,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM memory_vector
                WHERE workspace_id = %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (embedding_str, workspace_id, embedding_str, top_k),
            )
            rows = await result.fetchall()
            for row in rows:
                memories.append(
                    {
                        "memory_id": row["memory_id"],
                        "content": row["content"],
                        "similarity": float(row.get("similarity") or 0.0),
                    }
                )
    except Exception as exc:  # pragma: no cover - 防御性容错
        LOGGER.warning("Memory recall failed: %s", exc)
    return memories


async def _call_gateway_llm(
    *,
    workspace_id: str,
    actor: Actor,
    assistant_name: str,
    model: str,
    system_prompt: str,
    user_message: str,
    context_text: str,
    temperature: float,
    max_tokens: int,
) -> tuple[str, str, int]:
    """调用 gateway LLM，返回 ``(assistant_message, method, tokens_used)``。

    - method="llm"：真实调用 gateway 成功
    - method="mock"：LLM 被禁用或调用失败，返回确定性 mock 响应

    v7.159：改用 ``gateway.llm_client.call_llm`` 统一入口，不再直接构造
    httpx 请求。失败回退到 mock 响应（不抛错），保证调用方稳定。
    """
    mock_message = _build_mock_response(
        assistant_name=assistant_name,
        model=model,
        user_message=user_message,
        rag_chunks=[],
        memories=[],
        tools=[],
    )
    if context_text:
        mock_message = f"{mock_message}\n[context] {context_text[:500]}"

    if _llm_disabled():
        return mock_message, "mock", len(mock_message) // 4

    api_key = os.getenv("WORKAMA_INTERNAL_LLM_API_KEY", "").strip()
    if not api_key:
        LOGGER.warning(
            "WORKAMA_INTERNAL_LLM_API_KEY not set; using mock LLM response."
        )
        return mock_message, "mock", len(mock_message) // 4

    full_system = system_prompt
    if context_text:
        full_system = f"{system_prompt}\n\n[Context]\n{context_text}"
    messages = [
        {"role": "system", "content": full_system},
        {"role": "user", "content": user_message},
    ]
    result = await call_llm(
        messages=messages,
        model=model,
        workspace_id=workspace_id,
        actor=actor,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if result["method"] != "llm":
        return mock_message, "mock", len(mock_message) // 4

    content = result["content"]
    tokens_used = int(result.get("tokens_used") or len(content) // 4)
    return content, "llm", tokens_used


async def _memory_extract(
    workspace_id: str,
    actor: Actor,
    conversation_text: str,
) -> list[str]:
    """从对话抽取记忆写入 memory_vector，返回写入的 memory_vector.id 列表。

    失败时返回空列表（不阻断助手运行）。
    """
    extracted: list[str] = []
    try:
        from workama_platform.modules.memory_vector import (
            _insert_extracted_memory,
            _mock_extract_entries,
        )

        entries = _mock_extract_entries(conversation_text)
        async with pool.connection() as conn:
            async with conn.transaction():
                for entry in entries:
                    vid = await _insert_extracted_memory(
                        conn, workspace_id, entry
                    )
                    if vid:
                        extracted.append(vid)
    except Exception as exc:  # pragma: no cover - 防御性容错
        LOGGER.warning("Memory extract failed: %s", exc)
    return extracted


def _build_context_text(
    rag_chunks: list[dict], memories: list[dict], tools: list[str]
) -> str:
    """组装 RAG 上下文 + 记忆 + 工具列表为一段文本，注入 system_prompt。"""
    parts: list[str] = []
    if rag_chunks:
        parts.append("[Retrieved Knowledge]")
        for idx, chunk in enumerate(rag_chunks, start=1):
            parts.append(f"[{idx}] {chunk.get('content', '')[:500]}")
    if memories:
        parts.append("[Recalled Memories]")
        for idx, mem in enumerate(memories, start=1):
            parts.append(f"<mem-{idx}> {mem.get('content', '')[:500]}")
    if tools:
        parts.append("[Available Tools] " + ", ".join(tools))
    return "\n".join(parts)


# ============================================================================
# Router 端点
# ============================================================================
#
# 路由声明顺序：具体路径（/{id}/run, /{id}/runs, /{id}/clone）必须与参数化路径
# /{assistant_id} 共存。FastAPI 按声明顺序匹配，更具体的路径先声明。


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_assistant(
    body: AssistantCreateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """创建助手。"""
    _require(actor, "write")
    assistant_id = new_id("ast")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                INSERT INTO assistant(
                    id, workspace_id, name, description, system_prompt,
                    model, temperature, max_tokens, tools, knowledge_base_ids,
                    memory_enabled, status, version, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                        %s, %s, 1, %s::jsonb)
                RETURNING *
                """,
                (
                    assistant_id,
                    actor.workspace_id,
                    body.name,
                    body.description,
                    body.system_prompt,
                    body.model,
                    body.temperature,
                    body.max_tokens,
                    json_dumps(body.tools),
                    json_dumps(body.knowledge_base_ids),
                    body.memory_enabled,
                    body.status,
                    json_dumps(body.metadata),
                ),
            )
            row = await result.fetchone()
    return _summary(row)


@router.get("")
async def list_assistants(
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
            SELECT * FROM assistant
            WHERE workspace_id = %s {status_clause}
            ORDER BY created_at DESC LIMIT %s OFFSET %s
            """,
            tuple(params),
        )
        rows = await result.fetchall()
    items = [_summary(row) for row in rows]
    return {
        "items": items,
        "data": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
    }


@router.get("/{assistant_id}")
async def get_assistant(
    assistant_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """查询助手详情。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        row = await _owned_assistant(conn, assistant_id, actor)
    return _summary(row)


@router.patch("/{assistant_id}")
async def update_assistant(
    assistant_id: str,
    body: AssistantUpdateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """更新助手配置（部分字段；任意字段提供即更新；version 自增）。"""
    _require(actor, "write")
    updates: dict[str, Any] = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")
    if "status" in updates and updates["status"] not in _VALID_STATUSES:
        raise HTTPException(
            status_code=422, detail=f"Invalid status: {updates['status']}"
        )
    async with pool.connection() as conn:
        async with conn.transaction():
            await _owned_assistant(conn, assistant_id, actor)
            set_clauses: list[str] = []
            params: list[object] = []
            for key, value in updates.items():
                if key in ("tools", "knowledge_base_ids", "metadata"):
                    set_clauses.append(f"{key} = %s::jsonb")
                    params.append(json_dumps(value))
                else:
                    set_clauses.append(f"{key} = %s")
                    params.append(value)
            set_clauses.append("version = version + 1")
            set_clauses.append("updated_at = now()")
            params.append(assistant_id)
            result = await conn.execute(
                f"""
                UPDATE assistant SET {', '.join(set_clauses)}
                WHERE id = %s RETURNING *
                """,
                tuple(params),
            )
            row = await result.fetchone()
    return _summary(row)


@router.delete("/{assistant_id}")
async def delete_assistant(
    assistant_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """删除助手（硬删除）。"""
    _require(actor, "delete")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _owned_assistant(conn, assistant_id, actor)
            result = await conn.execute(
                "DELETE FROM assistant WHERE id = %s RETURNING id",
                (assistant_id,),
            )
            row = await result.fetchone()
    return {"id": row["id"], "deleted": True}


@router.post("/{assistant_id}/run")
async def run_assistant(
    assistant_id: str,
    body: AssistantRunRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """运行助手：RAG 召回 + memory 召回 + gateway LLM + memory 抽取写入。

    流程：
    1. 加载助手配置（校验 workspace 隔离 + status=active）
    2. 如果有 knowledge_base_ids，做 RAG 查询补充上下文
    3. 如果 memory_enabled，从 memory_vector 召回相关记忆
    4. 调用 gateway LLM（system_prompt + context + user_message）
    5. 如果有 tools（MCP），仅在 metadata 中记录可用工具（简化）
    6. 记录 assistant_run
    7. 如果 memory_enabled，从对话抽取记忆写入 memory_vector
    """
    _require(actor, "write")
    started = time.monotonic()
    run_id = new_id("astr")
    async with pool.connection() as conn:
        assistant = await _owned_assistant(conn, assistant_id, actor)
        if assistant["status"] != "active":
            raise HTTPException(
                status_code=409,
                detail=f"Assistant status is {assistant['status']}, cannot run",
            )
        model = body.model or assistant["model"]
        temperature = body.temperature if body.temperature is not None else float(
            assistant["temperature"]
        )
        max_tokens = body.max_tokens if body.max_tokens is not None else int(
            assistant["max_tokens"]
        )
        knowledge_base_ids = list(assistant.get("knowledge_base_ids") or [])
        tools = list(assistant.get("tools") or [])
        memory_enabled = bool(assistant["memory_enabled"])

    # 1) RAG 召回
    rag_chunks: list[dict] = []
    if knowledge_base_ids:
        rag_chunks = await _rag_query(
            actor.workspace_id, knowledge_base_ids, body.user_message
        )

    # 2) memory 召回
    memories: list[dict] = []
    if memory_enabled:
        memories = await _memory_recall(
            actor.workspace_id, actor.user_id, body.user_message
        )

    # 3) 组装上下文 + 调用 LLM
    context_text = _build_context_text(rag_chunks, memories, tools)
    assistant_message, method, tokens_used = await _call_gateway_llm(
        workspace_id=actor.workspace_id,
        actor=actor,
        assistant_name=assistant["name"],
        model=model,
        system_prompt=assistant["system_prompt"],
        user_message=body.user_message,
        context_text=context_text,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    duration_ms = int((time.monotonic() - started) * 1000)

    # 4) 写入 assistant_run
    run_metadata: dict[str, Any] = {
        "method": method,
        "rag_chunks_count": len(rag_chunks),
        "memories_count": len(memories),
        "tools": tools,
        "knowledge_base_ids": knowledge_base_ids,
        "memory_enabled": memory_enabled,
        "extracted_memory_ids": [],
        **(body.metadata or {}),
    }
    if memory_enabled:
        conversation_text = (
            f"User: {body.user_message}\nAssistant: {assistant_message}"
        )
        extracted_ids = await _memory_extract(
            actor.workspace_id, actor, conversation_text
        )
        run_metadata["extracted_memory_ids"] = extracted_ids

    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                INSERT INTO assistant_run(
                    id, assistant_id, workspace_id, user_message, assistant_message,
                    model, tokens_used, duration_ms, status, error, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'completed', NULL, %s::jsonb)
                RETURNING *
                """,
                (
                    run_id,
                    assistant_id,
                    actor.workspace_id,
                    body.user_message,
                    assistant_message,
                    model,
                    tokens_used,
                    duration_ms,
                    json_dumps(run_metadata),
                ),
            )
            run_row = await result.fetchone()
    response = _run_summary(run_row)
    # 使用实际计算的 run_metadata（DB 行在测试中被 mock，可能不含动态字段）
    response["metadata"] = run_metadata
    response["assistant_message"] = assistant_message
    response["tokens_used"] = tokens_used
    response["duration_ms"] = duration_ms
    response["model"] = model
    return response


@router.get("/{assistant_id}/runs")
async def list_assistant_runs(
    assistant_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """运行历史：分页查询，支持 status 过滤。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        await _owned_assistant(conn, assistant_id, actor)
        status_clause = ""
        params: list[object] = [assistant_id, actor.workspace_id]
        if status_filter:
            status_clause = "AND status = %s"
            params.append(status_filter)
        params.append(limit)
        params.append(offset)
        result = await conn.execute(
            f"""
            SELECT * FROM assistant_run
            WHERE assistant_id = %s AND workspace_id = %s {status_clause}
            ORDER BY created_at DESC LIMIT %s OFFSET %s
            """,
            tuple(params),
        )
        rows = await result.fetchall()
    items = [_run_summary(row) for row in rows]
    return {
        "items": items,
        "data": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
    }


@router.post("/{assistant_id}/clone", status_code=status.HTTP_201_CREATED)
async def clone_assistant(
    assistant_id: str,
    body: CloneRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """克隆助手：复制配置（system_prompt/model/tools/...），新 name/version=1。"""
    _require(actor, "write")
    new_id_str = new_id("ast")
    async with pool.connection() as conn:
        async with conn.transaction():
            source = await _owned_assistant(conn, assistant_id, actor)
            cloned_metadata = {
                "cloned_from": assistant_id,
                **(source.get("metadata") or {}),
            }
            result = await conn.execute(
                """
                INSERT INTO assistant(
                    id, workspace_id, name, description, system_prompt,
                    model, temperature, max_tokens, tools, knowledge_base_ids,
                    memory_enabled, status, version, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                        %s, 'active', 1, %s::jsonb)
                RETURNING *
                """,
                (
                    new_id_str,
                    actor.workspace_id,
                    body.name,
                    body.description if body.description is not None else source.get("description"),
                    source["system_prompt"],
                    source["model"],
                    float(source["temperature"]),
                    int(source["max_tokens"]),
                    json_dumps(list(source.get("tools") or [])),
                    json_dumps(list(source.get("knowledge_base_ids") or [])),
                    bool(source["memory_enabled"]),
                    json_dumps(cloned_metadata),
                ),
            )
            row = await result.fetchone()
    response = _summary(row)
    # 使用实际计算的 cloned_metadata（DB 行在测试中被 mock，可能不含动态字段）
    response["metadata"] = cloned_metadata
    return response
