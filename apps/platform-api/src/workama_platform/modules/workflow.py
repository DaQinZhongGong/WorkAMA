"""P1 工作流编排模块 (workflow) - DAG 节点编排 + 节点级执行引擎。

v7.151: 工作流 CRUD / 发布 / 运行 / 历史。

提供：
- 9 个 REST 端点（创建 / 列表 / 详情 / 更新 / 删除 / 发布 / 运行 / 运行历史 / 单次运行详情）
- 节点类型 7 种：``llm_call`` / ``tool_call`` / ``rag_query`` /
  ``memory_recall`` / ``memory_extract`` / ``condition`` / ``output``
- 拓扑执行：按 edges 拓扑顺序执行节点，每个节点产生输出传递给下游节点
- LLM / 工具调用支持真实 gateway（与 assistant.py 同样的 mock fallback 策略），
  默认配置下走确定性 mock 响应（不依赖外部服务）
- 与既有 ``pf_workflow`` 表独立共存（pf_workflow 由 workflows.py 使用）

设计文档：510-AI中台核心设计.md「§5 工作流引擎」；910-进度追踪与任务清单.md v7.151
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

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
from workama_platform.modules.workflow_http_node import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_MAX_RESPONSE_SIZE,
    DEFAULT_MAX_RETRIES,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_RETRY_BASE_DELAY,
    DEFAULT_RETRY_MAX_DELAY,
    classify_http_error,
    http_request_with_retry,
    max_subworkflow_depth,
    sanitize_headers,
    validate_code_output,
    validate_resolved_outbound_url,
)

# ============================================================================
# 常量
# ============================================================================

LOGGER = logging.getLogger("workama.platform-api.workflow")

router = APIRouter(prefix="/api/v1/workflows", tags=["workflow"])

WorkflowStatus = Literal["draft", "published", "archived"]
RunStatus = Literal["pending", "running", "completed", "failed"]

_VALID_STATUSES: frozenset[str] = frozenset({"draft", "published", "archived"})
_VALID_RUN_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "completed", "failed"}
)

MAX_NESTING_DEPTH = 3
MAX_LOOP_ITEMS = 100
APPROVAL_DEFAULT_TIMEOUT_SECONDS = 3600

# 节点默认超时（秒）
_NODE_DEFAULT_TIMEOUTS: dict[str, int] = {
    "http_request": 30,
    "code": 60,
    "sub_workflow": 300,
    "llm_call": 120,
}

# on_timeout 策略
_ON_TIMEOUT_POLICIES: frozenset[str] = frozenset({"fail", "skip", "retry_once"})

# 支持的节点类型
NODE_TYPES: frozenset[str] = frozenset(
    {
        "llm_call",
        "tool_call",
        "rag_query",
        "memory_recall",
        "memory_extract",
        "condition",
        "output",
        "http_request",
        "sub_workflow",
        "loop",
        "human_approval",
        "code",
        "start",
        "end",
        # M5 节点补全：transform / branch / webhook / delay / parallel
        "transform",
        "branch",
        "webhook",
        "delay",
        "parallel",
    }
)

# ============================================================================
# Schema 语句（T-M5-003 工作流执行器生产硬化）
# ============================================================================

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    ALTER TABLE workflow_run
        ADD COLUMN IF NOT EXISTS call_stack JSONB NOT NULL DEFAULT '[]'::jsonb
    """,
    """
    ALTER TABLE workflow_run
        ADD COLUMN IF NOT EXISTS timeout_seconds INTEGER
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_node_run (
        id TEXT PRIMARY KEY,
        workflow_run_id TEXT NOT NULL REFERENCES workflow_run(id) ON DELETE CASCADE,
        workspace_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        node_type TEXT NOT NULL,
        status TEXT NOT NULL,
        output JSONB NOT NULL DEFAULT '{}'::jsonb,
        error TEXT,
        error_code TEXT,
        duration_ms INTEGER NOT NULL DEFAULT 0,
        latency_ms INTEGER,
        attempts INTEGER,
        status_code INTEGER,
        headers JSONB,
        truncated BOOLEAN NOT NULL DEFAULT FALSE,
        timeout_seconds INTEGER,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS workflow_node_run_run_idx
        ON workflow_node_run(workflow_run_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS workflow_node_run_workspace_idx
        ON workflow_node_run(workspace_id)
    """,
    # M5 版本快照 / 回滚 / 对比（077_workflow_v2_versioning.sql）
    """
    CREATE TABLE IF NOT EXISTS workflow_v2_version_snapshot (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        workflow_id TEXT NOT NULL REFERENCES workflow(id) ON DELETE CASCADE,
        version INTEGER NOT NULL,
        snapshot JSONB NOT NULL,
        changelog TEXT,
        created_by TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(workspace_id, workflow_id, version)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS workflow_v2_version_snapshot_workspace_idx
        ON workflow_v2_version_snapshot(workspace_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS workflow_v2_version_snapshot_workflow_idx
        ON workflow_v2_version_snapshot(workflow_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS workflow_v2_version_snapshot_version_idx
        ON workflow_v2_version_snapshot(workflow_id, version DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS workflow_v2_version_snapshot_created_idx
        ON workflow_v2_version_snapshot(workflow_id, created_at DESC)
    """,
)


async def ensure_schema() -> None:
    """执行 SCHEMA_STATEMENTS 中的建表/补列语句。"""
    async with pool.connection() as conn:
        for statement in SCHEMA_STATEMENTS:
            await conn.execute(statement)

# ============================================================================
# HTTP 节点环境配置
# ============================================================================

_HTTP_ALLOWED_HOSTS_ENV = "WORKAMA_HTTP_NODE_ALLOWED_HOSTS"
_HTTP_TIMEOUT_ENV = "WORKAMA_HTTP_NODE_TIMEOUT_SECONDS"
_HTTP_MAX_SIZE_ENV = "WORKAMA_HTTP_NODE_MAX_RESPONSE_SIZE"

_VAR_RE = re.compile(r"\{\{\s*([^}]+)\s*\}\}")


def _http_allowed_hosts() -> set[str]:
    raw = os.getenv(_HTTP_ALLOWED_HOSTS_ENV, "").strip()
    if not raw:
        return set()
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _http_default_timeout() -> int:
    try:
        return int(os.getenv(_HTTP_TIMEOUT_ENV, "30"))
    except ValueError:
        return 30


def _http_max_response_size() -> int:
    try:
        return int(os.getenv(_HTTP_MAX_SIZE_ENV, "1048576"))
    except ValueError:
        return 1_048_576


# ============================================================================
# Pydantic 模型
# ============================================================================


class WorkflowNode(BaseModel):
    """工作流节点定义。"""

    id: str = Field(min_length=1, max_length=120)
    type: str = Field(min_length=1, max_length=60)
    name: str | None = Field(default=None, max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)


class WorkflowEdge(BaseModel):
    """工作流边定义。

    ``condition_value`` 仅用于 ``condition`` 节点的出边，表示该分支匹配的值。
    """

    source: str = Field(min_length=1, max_length=120)
    target: str = Field(min_length=1, max_length=120)
    condition_value: str | None = None


class WorkflowCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    nodes: list[WorkflowNode] = Field(default_factory=list)
    edges: list[WorkflowEdge] = Field(default_factory=list)
    status: WorkflowStatus = "draft"
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkflowUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    nodes: list[WorkflowNode] | None = None
    edges: list[WorkflowEdge] | None = None
    status: WorkflowStatus | None = None
    metadata: dict[str, Any] | None = None


class WorkflowResponse(BaseModel):
    id: str
    workspace_id: str
    name: str
    description: str | None = None
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    status: str
    version: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class WorkflowRunRequest(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkflowRunResponse(BaseModel):
    id: str
    workflow_id: str
    workspace_id: str
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class WorkflowRunNode(BaseModel):
    """运行时单个节点的执行记录（写入 workflow_run.metadata.node_runs）。"""

    node_id: str
    node_type: str
    status: str
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    duration_ms: int = 0


# ============================================================================
# 辅助函数
# ============================================================================


def _require(actor: Actor, action: str) -> None:
    """检查 actor 是否拥有 workflow:{action} 能力。"""
    if not capability_allows(actor.capabilities, f"workflow:{action}"):
        raise HTTPException(
            status_code=403, detail=f"Missing capability: workflow:{action}"
        )


def _summary(row: dict) -> dict:
    """将 workflow 行转为 API 响应 dict。"""
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "name": row["name"],
        "description": row.get("description"),
        "nodes": list(row.get("nodes") or []),
        "edges": list(row.get("edges") or []),
        "status": row["status"],
        "version": int(row["version"]),
        "metadata": row.get("metadata") or {},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _run_summary(row: dict) -> dict:
    """将 workflow_run 行转为 API 响应 dict。"""
    return {
        "id": row["id"],
        "workflow_id": row["workflow_id"],
        "workspace_id": row["workspace_id"],
        "input": row.get("input") or {},
        "output": row.get("output") or {},
        "status": row["status"],
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
        "error": row.get("error"),
        "metadata": row.get("metadata") or {},
        "created_at": row["created_at"],
    }


async def _owned_workflow(conn: Any, workflow_id: str, actor: Actor) -> dict:
    """查询工作流并校验 workspace 归属。

    - 不存在 → 404
    - 存在但 workspace 不匹配 → 403
    """
    result = await conn.execute(
        "SELECT * FROM workflow WHERE id = %s",
        (workflow_id,),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if row["workspace_id"] != actor.workspace_id:
        raise HTTPException(
            status_code=403, detail="Workflow belongs to another workspace"
        )
    return row


def _validate_dag(nodes: list[dict], edges: list[dict]) -> None:
    """校验 DAG 合法性：

    - 节点 id 唯一
    - 节点 type 在 NODE_TYPES 内
    - 边的 source/target 必须在 nodes 中
    - 无环（拓扑排序能完成）
    """
    node_ids = {n["id"] for n in nodes}
    if len(node_ids) != len(nodes):
        raise HTTPException(
            status_code=422, detail="Duplicate node id in workflow nodes"
        )
    for n in nodes:
        if n["type"] not in NODE_TYPES:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported node type: {n['type']}",
            )
    for e in edges:
        if e["source"] not in node_ids or e["target"] not in node_ids:
            raise HTTPException(
                status_code=422,
                detail="Edge source/target not in nodes",
            )
    # 拓扑排序校验无环
    _topological_order(nodes, edges)


def _topological_order(nodes: list[dict], edges: list[dict]) -> list[str]:
    """返回拓扑顺序的 node_id 列表；存在环时抛 HTTPException 422。"""
    in_degree: dict[str, int] = {n["id"]: 0 for n in nodes}
    adj: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        adj[e["source"]].append(e["target"])
        in_degree[e["target"]] += 1
    queue: deque[str] = deque([nid for nid, d in in_degree.items() if d == 0])
    order: list[str] = []
    while queue:
        nid = queue.popleft()
        order.append(nid)
        for nxt in adj.get(nid, []):
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)
    if len(order) != len(nodes):
        raise HTTPException(
            status_code=422, detail="Workflow DAG contains a cycle"
        )
    return order


# ============================================================================
# 变量插值工具
# ============================================================================


def _resolve_ref(path: str, context: dict[str, Any]) -> Any:
    """按点号路径从 context 取值，支持 {{...}} 包裹。"""
    path = path.strip()
    if path.startswith("{{") and path.endswith("}}"):
        path = path[2:-2].strip()
    if path.startswith("context."):
        path = path[len("context."):]
    parts = path.split(".")
    value: Any = context
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None
    return value


def _interpolate_value(value: Any, context: dict[str, Any]) -> Any:
    """对单个值进行变量插值。"""
    if not isinstance(value, str):
        return value
    matches = list(_VAR_RE.finditer(value))
    if not matches:
        return value
    # 如果整个字符串就是一个变量引用，保留原始类型
    if len(matches) == 1 and matches[0].group(0) == value:
        resolved = _resolve_ref(matches[0].group(1), context)
        return resolved if resolved is not None else ""
    # 否则做字符串替换
    result = value
    for m in matches:
        ref = m.group(1).strip()
        resolved = _resolve_ref(ref, context)
        result = result.replace(m.group(0), str(resolved) if resolved is not None else "")
    return result


def _interpolate_dict(mapping: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """对 dict 的键值递归插值。"""
    if not isinstance(mapping, dict):
        return {}
    result: dict[str, Any] = {}
    for key, value in mapping.items():
        new_key = _interpolate_value(key, context)
        if isinstance(value, dict):
            result[new_key] = _interpolate_dict(value, context)
        elif isinstance(value, list):
            result[new_key] = [_interpolate_value(v, context) for v in value]
        else:
            result[new_key] = _interpolate_value(value, context)
    return result


# ============================================================================
# 节点执行器（mock fallback + 真实调用结构）
# ============================================================================


def _llm_disabled() -> bool:
    """WORKAMA_WORKFLOW_DISABLE_LLM=1 时强制走 mock。"""
    return os.getenv("WORKAMA_WORKFLOW_DISABLE_LLM", "").strip() in (
        "1",
        "true",
        "yes",
    )


async def _execute_llm_call(
    node: dict, inputs: dict[str, Any], *, workspace_id: str, actor: Actor
) -> dict[str, Any]:
    """执行 llm_call 节点。

    ``node.config`` 支持：
    - ``model``: 模型名（默认 gpt-4o-mini）
    - ``prompt_template``: 提示词模板（含 ``{input}`` 占位符）
    - ``temperature``: 温度（默认 0.7）
    - ``max_tokens``: 最大 token（默认 2048）

    v7.159：改用 ``gateway.llm_client.call_llm`` 统一入口。
    无 API key 时走确定性 mock。
    """
    config = node.get("config") or {}
    model = config.get("model") or "gpt-4o-mini"
    template = config.get("prompt_template") or "{input}"
    try:
        prompt = template.format(**inputs)
    except (KeyError, IndexError):
        prompt = template
    temperature = float(config.get("temperature") or 0.7)
    max_tokens = int(config.get("max_tokens") or 2048)

    mock_message = (
        f"[mock-llm] node={node.get('name') or node['id']} model={model} "
        f"prompt={prompt[:200]}"
    )
    if _llm_disabled():
        return {"message": mock_message, "model": model, "method": "mock"}
    api_key = os.getenv("WORKAMA_INTERNAL_LLM_API_KEY", "").strip()
    if not api_key:
        return {"message": mock_message, "model": model, "method": "mock"}

    messages = [{"role": "user", "content": prompt}]
    result = await call_llm(
        messages=messages,
        model=model,
        workspace_id=workspace_id,
        actor=actor,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if result["method"] != "llm":
        return {"message": mock_message, "model": model, "method": "mock"}
    return {
        "message": result["content"],
        "model": model,
        "method": "llm",
        "tokens_used": int(result.get("tokens_used") or 0),
    }


async def _execute_tool_call(node: dict, inputs: dict[str, Any]) -> dict[str, Any]:
    """执行 tool_call 节点（简化：返回 mock 工具调用结果）。

    ``node.config`` 支持：
    - ``tool_id``: MCP 工具 id
    - ``arguments``: 调用参数（dict）
    """
    config = node.get("config") or {}
    tool_id = config.get("tool_id") or "unknown_tool"
    arguments = config.get("arguments") or {}
    return {
        "tool_id": tool_id,
        "result": f"[mock-tool] tool={tool_id} args={arguments}",
        "method": "mock",
    }


async def _execute_rag_query(
    node: dict, inputs: dict[str, Any], *, workspace_id: str
) -> dict[str, Any]:
    """执行 rag_query 节点：从 knowledge_chunk 表召回。

    ``node.config`` 支持：
    - ``knowledge_base_id``: KB id（必填）
    - ``query_key``: 从 inputs 取查询文本的 key（默认 ``query``）
    - ``top_k``: 召回数量（默认 5）
    """
    config = node.get("config") or {}
    kb_id = config.get("knowledge_base_id")
    query_key = config.get("query_key") or "query"
    top_k = int(config.get("top_k") or 5)
    query = str(inputs.get(query_key) or "")
    if not kb_id or not query:
        return {"chunks": [], "count": 0, "method": "mock"}
    chunks: list[dict] = []
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
                        "content": row["content"],
                        "similarity": float(row.get("similarity") or 0.0),
                    }
                )
    except Exception as exc:  # pragma: no cover
        LOGGER.warning("workflow rag_query failed: %s", exc)
    return {"chunks": chunks, "count": len(chunks), "method": "llm" if chunks else "mock"}


async def _execute_memory_recall(
    node: dict, inputs: dict[str, Any], *, workspace_id: str
) -> dict[str, Any]:
    """执行 memory_recall 节点：从 memory_vector 表召回。"""
    config = node.get("config") or {}
    query_key = config.get("query_key") or "query"
    top_k = int(config.get("top_k") or 5)
    query = str(inputs.get(query_key) or "")
    if not query:
        return {"memories": [], "count": 0, "method": "mock"}
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
    except Exception as exc:  # pragma: no cover
        LOGGER.warning("workflow memory_recall failed: %s", exc)
    return {
        "memories": memories,
        "count": len(memories),
        "method": "llm" if memories else "mock",
    }


async def _execute_memory_extract(
    node: dict, inputs: dict[str, Any], *, workspace_id: str, actor: Actor
) -> dict[str, Any]:
    """执行 memory_extract 节点：从输入文本抽取记忆写入 memory_vector。"""
    config = node.get("config") or {}
    text_key = config.get("text_key") or "text"
    text = str(inputs.get(text_key) or "")
    if not text:
        return {"extracted_ids": [], "count": 0, "method": "mock"}
    extracted_ids: list[str] = []
    try:
        from workama_platform.modules.memory_vector import (
            _insert_extracted_memory,
            _mock_extract_entries,
        )

        entries = _mock_extract_entries(text)
        async with pool.connection() as conn:
            async with conn.transaction():
                for entry in entries:
                    vid = await _insert_extracted_memory(
                        conn, workspace_id, entry
                    )
                    if vid:
                        extracted_ids.append(vid)
    except Exception as exc:  # pragma: no cover
        LOGGER.warning("workflow memory_extract failed: %s", exc)
    return {
        "extracted_ids": extracted_ids,
        "count": len(extracted_ids),
        "method": "llm" if extracted_ids else "mock",
    }


def _execute_condition(node: dict, inputs: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """执行 condition 节点：根据 ``node.config.value_key`` 取输入值，
    与 ``node.config.branches`` 中的 ``value`` 比对，返回匹配的 branch_value。

    返回 ``(output_dict, branch_value)``。``branch_value`` 用于在拓扑执行时
    选择对应的出边（``edge.condition_value == branch_value``）。

    无匹配分支时返回 ``"default"``。
    """
    config = node.get("config") or {}
    value_key = config.get("value_key") or "value"
    branches = config.get("branches") or []
    actual = str(inputs.get(value_key) or "")
    for branch in branches:
        if str(branch.get("value")) == actual:
            return {"matched": actual, "branch": actual}, actual
    return {"matched": None, "branch": "default"}, "default"


def _execute_output(node: dict, inputs: dict[str, Any]) -> dict[str, Any]:
    """执行 output 节点：将 inputs 透传为 workflow_run.output。"""
    config = node.get("config") or {}
    fields = config.get("fields") or list(inputs.keys())
    output: dict[str, Any] = {}
    for f in fields:
        if f in inputs:
            output[f] = inputs[f]
    return output


async def _execute_http_node(
    node: dict,
    inputs: dict[str, Any],
    *,
    workspace_id: str,
    actor: Actor,
) -> dict[str, Any]:
    """执行 http_request 节点。

    ``node.config`` 支持：
    - ``method``: HTTP 方法（默认 GET）
    - ``url``: 请求 URL（支持变量插值）
    - ``headers``: 请求头 dict（支持插值）
    - ``body``: 请求体 dict（支持插值）
    - ``timeout``: 超时秒数（默认读取环境变量或 30s）

    默认 mock 模式：未配置外部 API key 或允许列表为空时，
    返回 ``pending_external`` 占位结果，不发起真实 HTTP 请求。
    """
    config = node.get("config") or {}
    method = (config.get("method") or "GET").upper()
    raw_url = config.get("url") or ""
    headers = config.get("headers") or {}
    body = config.get("body")
    timeout = config.get("timeout")

    # 构建插值上下文：合并 inputs + 节点输出引用 + actor 上下文
    interp_context: dict[str, Any] = dict(inputs)
    interp_context.setdefault("context", {
        "actor_id": actor.user_id,
        "workspace_id": workspace_id,
        "org_id": actor.org_id,
    })

    url = str(_interpolate_value(raw_url, interp_context) or "")
    headers = _interpolate_dict(headers, interp_context)
    if isinstance(body, dict):
        body = _interpolate_dict(body, interp_context)
    elif isinstance(body, str):
        body = _interpolate_value(body, interp_context)

    if not url:
        return {"error": "http_request node requires a url", "method": "mock"}

    # mock 模式边界：未配置允许主机或缺少 API key 时走 mock
    allowed_hosts = _http_allowed_hosts()
    if not allowed_hosts:
        return {
            "status_code": 202,
            "body": {"pending_external": True, "url": url, "method": method},
            "headers": {},
            "method": "mock",
        }

    # SSRF 防护
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
    except Exception as exc:
        return {"error": f"invalid_url: {exc}", "method": "mock"}
    if host not in allowed_hosts:
        return {"error": "forbidden: host not in allowed list", "method": "mock"}

    # 超时
    try:
        timeout_val = float(timeout) if timeout is not None else _http_default_timeout()
    except (TypeError, ValueError):
        timeout_val = _http_default_timeout()

    max_size = _http_max_response_size()

    try:
        import httpx
    except ImportError:  # pragma: no cover
        return {"error": "httpx not available", "method": "mock"}

    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                httpx.request,
                method=method,
                url=url,
                headers=headers,
                json=body if isinstance(body, dict) else None,
                data=body if isinstance(body, str) else None,
                timeout=timeout_val,
            ),
            timeout=timeout_val,
        )
    except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
        return {"error": f"timeout: {exc}", "method": "mock"}
    except httpx.ConnectError as exc:
        return {"error": f"connection_error: {exc}", "method": "mock"}
    except Exception as exc:
        return {"error": f"http node request failed: {exc}", "method": "mock"}

    resp_headers = dict(response.headers)
    raw_body = response.text
    if len(raw_body) > max_size:
        LOGGER.warning(
            "http_request response truncated: url=%s size=%d max=%d",
            url,
            len(raw_body),
            max_size,
        )
        raw_body = raw_body[:max_size]

    # 尝试 JSON 解析
    content_type = resp_headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            parsed_body = response.json()
        except Exception:
            parsed_body = raw_body
    else:
        parsed_body = raw_body

    return {
        "status_code": response.status_code,
        "headers": resp_headers,
        "body": parsed_body,
        "method": "http",
    }


async def _execute_sub_workflow(
    node: dict,
    inputs: dict[str, Any],
    *,
    workspace_id: str,
    actor: Actor,
) -> dict[str, Any]:
    """执行 sub_workflow 节点：调用指定子工作流并传递参数。

    ``node.config`` 支持：
    - ``workflow_id``: 子工作流 ID（必填）
    - ``input_mapping``: 输入字段映射 dict（key=子工作流输入字段, value=上游引用）
    """
    config = node.get("config") or {}
    sub_workflow_id = config.get("workflow_id")
    if not sub_workflow_id:
        return {"error": "sub_workflow node requires workflow_id", "method": "mock"}
    input_mapping = config.get("input_mapping") or {}
    sub_input: dict[str, Any] = {}
    for key, ref in input_mapping.items():
        sub_input[key] = _resolve_ref(ref, inputs)
    # mock 模式：不依赖真实 DB 查询子工作流定义，返回确定性占位
    return {
        "sub_workflow_id": sub_workflow_id,
        "input": sub_input,
        "output": {"result": f"[mock-sub-workflow] id={sub_workflow_id}"},
        "method": "mock",
    }


async def _execute_loop(
    node: dict,
    inputs: dict[str, Any],
    *,
    workspace_id: str,
    actor: Actor,
) -> dict[str, Any]:
    """执行 loop 节点：循环执行内部逻辑，上限 100 次，支持 break 条件。

    ``node.config`` 支持：
    - ``max_iterations``: 最大循环次数（默认 10，上限 100）
    - ``break_condition``: 退出条件 key（当上游输出包含该 key 且为真时退出）
    - ``loop_body``: 内部节点描述（mock 模式下仅记录迭代次数）
    """
    config = node.get("config") or {}
    max_iterations = min(int(config.get("max_iterations") or 10), 100)
    if max_iterations > MAX_LOOP_ITEMS:
        raise RuntimeError(f"loop limit exceeded: {max_iterations} iterations exceeds maximum of {MAX_LOOP_ITEMS}")
    break_condition = config.get("break_condition")
    loop_body = config.get("loop_body") or []
    if isinstance(loop_body, list) and len(loop_body) > 50:
        raise RuntimeError("loop body exceeds 50 nodes limit")
    enable_state_check = bool(loop_body)
    iterations = 0
    loop_outputs: list[dict[str, Any]] = []
    previous_state: dict[str, Any] | None = None
    for i in range(max_iterations):
        iterations = i + 1
        if break_condition and inputs.get(break_condition):
            break
        # 变量隔离：复制 inputs，避免修改原始上下文
        iteration_inputs = dict(inputs)
        iteration_inputs["_iteration"] = i + 1
        # 检测连续两次迭代上下文无变化（仅在配置 loop_body 时启用）
        if enable_state_check:
            current_state = {k: v for k, v in iteration_inputs.items() if not k.startswith("_")}
            if previous_state is not None and previous_state == current_state:
                loop_outputs.append({"iteration": i + 1, "status": "break_no_state_change"})
                break
            previous_state = current_state
        loop_outputs.append({"iteration": i + 1, "status": "ok"})
    return {
        "iterations": iterations,
        "max_iterations": max_iterations,
        "outputs": loop_outputs,
        "method": "mock",
    }


async def _execute_human_approval(
    node: dict,
    inputs: dict[str, Any],
    *,
    workspace_id: str,
    actor: Actor,
) -> dict[str, Any]:
    """执行 human_approval 节点：创建审批记录并等待。

    复用现有 approvals 模块的 ag_approval 表结构。
    ``node.config`` 支持：
    - ``tool_name``: 触发审批的工具名（默认 "workflow_approval"）
    - ``preview``: 预览内容 dict
    - ``ttl_seconds``: 审批超时（默认 120）
    """
    config = node.get("config") or {}
    tool_name = config.get("tool_name") or "workflow_approval"
    preview = config.get("preview") or {}
    ttl_seconds = int(config.get("ttl_seconds") or 120)
    approval_id = new_id("apr")
    try:
        async with pool.connection() as conn:
            result = await conn.execute(
                """INSERT INTO ag_approval(id,workspace_id,session_id,call_id,requester_id,tool_name,action_hash,risk,preview,expires_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                   ON CONFLICT(workspace_id,call_id) DO NOTHING
                   RETURNING *""",
                (
                    approval_id,
                    workspace_id,
                    actor.user_id,
                    approval_id,
                    actor.user_id,
                    tool_name,
                    "0" * 64,
                    "A3",
                    json_dumps(preview),
                    datetime.now(UTC) + __import__("datetime").timedelta(seconds=ttl_seconds),
                ),
            )
            row = await result.fetchone()
            await conn.commit()
    except Exception as exc:
        return {"error": f"approval creation failed: {exc}", "method": "mock"}
    return {
        "approval_id": approval_id,
        "tool_name": tool_name,
        "status": "pending",
        "method": "mock",
    }


def _execute_start(node: dict, inputs: dict[str, Any]) -> dict[str, Any]:
    """执行 start 节点：透传输入作为工作流起始状态。"""
    return {"input": inputs, "method": "mock"}


def _execute_end(node: dict, inputs: dict[str, Any]) -> dict[str, Any]:
    """执行 end 节点：收集上游输出并标记工作流结束。"""
    return {"output": inputs, "method": "mock"}


# ============================================================================
# M5 节点补全：transform / branch / webhook / delay / parallel
# ============================================================================

# delay 节点最大等待秒数（防止滥用）
_MAX_DELAY_SECONDS = 300

# webhook 节点默认超时
_WEBHOOK_DEFAULT_TIMEOUT = 10.0

# branch 受限 eval 允许的 AST 节点白名单
_BRANCH_ALLOWED_AST: frozenset[type] = frozenset(
    {
        ast.Expression,
        ast.BoolOp,
        ast.UnaryOp,
        ast.Compare,
        ast.Constant,
        ast.Name,
        ast.Attribute,
        ast.And,
        ast.Or,
        ast.Not,
        ast.Gt,
        ast.Lt,
        ast.GtE,
        ast.LtE,
        ast.Eq,
        ast.NotEq,
        ast.Load,
    }
)


def _safe_eval_condition(expr: str, context: dict[str, Any]) -> Any:
    """受限条件表达式求值（用于 branch 节点）。

    只支持：``>`` / ``<`` / ``==`` / ``!=`` / ``>=`` / ``<=`` /
    ``and`` / ``or`` / ``not`` / 字面量 / 字段路径（``a.b.c``）。
    不允许 import / exec / 调用 / subscript / 列表字典构造。

    字段路径解析：``input.score`` → ``context["input"]["score"]``；
    不可解析的路径视为 ``None``。
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid condition expression: {exc}") from exc

    for node in ast.walk(tree):
        if type(node) not in _BRANCH_ALLOWED_AST:
            raise ValueError(
                f"disallowed expression element: {type(node).__name__}"
            )

    def _eval(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return context.get(node.id)
        if isinstance(node, ast.Attribute):
            value = _eval(node.value)
            if isinstance(value, dict):
                return value.get(node.attr)
            return getattr(value, node.attr, None)
        if isinstance(node, ast.BoolOp):
            values = [_eval(v) for v in node.values]
            if isinstance(node.op, ast.And):
                result: Any = True
                for v in values:
                    if not v:
                        return v
                    result = v
                return result
            # Or
            for v in values:
                if v:
                    return v
            return values[-1] if values else False
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not _eval(node.operand)
        if isinstance(node, ast.Compare):
            left = _eval(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                right = _eval(comparator)
                if isinstance(op, ast.Gt):
                    ok = left is not None and right is not None and left > right
                elif isinstance(op, ast.Lt):
                    ok = left is not None and right is not None and left < right
                elif isinstance(op, ast.GtE):
                    ok = left is not None and right is not None and left >= right
                elif isinstance(op, ast.LtE):
                    ok = left is not None and right is not None and left <= right
                elif isinstance(op, ast.Eq):
                    ok = left == right
                elif isinstance(op, ast.NotEq):
                    ok = left != right
                else:  # pragma: no cover
                    raise ValueError("unsupported comparator")
                if not ok:
                    return False
                left = right
            return True
        raise ValueError(f"unsupported expression node: {type(node).__name__}")

    return _eval(tree)


def _transform_interpolate(value: Any, context: dict[str, Any]) -> Any:
    """transform 节点专用插值：不可解析的字段保留原字符串。

    与 ``_interpolate_value`` 的区别：
    - 单变量引用 ``{{a.b}}`` 不可解析时返回原字符串（而非 ``""``）
    - 混合字符串中不可解析的变量保留 ``{{...}}`` 模板（而非替换为空）
    """
    if not isinstance(value, str):
        return value
    matches = list(_VAR_RE.finditer(value))
    if not matches:
        return value
    # 整个字符串就是一个变量引用：保留原始类型，不可解析则保留原字符串
    if len(matches) == 1 and matches[0].group(0) == value:
        resolved = _resolve_ref(matches[0].group(1), context)
        return resolved if resolved is not None else value
    # 否则做字符串替换，不可解析的变量保留原模板
    result = value
    for m in matches:
        ref = m.group(1).strip()
        resolved = _resolve_ref(ref, context)
        if resolved is not None:
            result = result.replace(m.group(0), str(resolved))
    return result


def _execute_transform(node: dict, inputs: dict[str, Any]) -> dict[str, Any]:
    """执行 transform 节点：对输入数据做字段映射 / 模板插值。

    ``node.config`` 支持：
    - ``mapping``: ``{output_field: "{{input.field}}"}``，值支持 ``{{path.to.field}}``
      模板插值（正则提取，支持嵌套路径）；不可解析的字段保留原字符串。

    插值上下文为 ``inputs``（上游节点输出 + 原始 input）。
    """
    config = node.get("config") or {}
    mapping = config.get("mapping") or {}
    output: dict[str, Any] = {}
    for key, template in mapping.items():
        if isinstance(template, str):
            output[key] = _transform_interpolate(template, inputs)
        elif isinstance(template, dict):
            output[key] = {
                _transform_interpolate(k, inputs): _transform_interpolate(v, inputs)
                for k, v in template.items()
            }
        elif isinstance(template, list):
            output[key] = [_transform_interpolate(v, inputs) for v in template]
        else:
            output[key] = template
    return {"transformed": output, "method": "transform"}


def _execute_branch(
    node: dict, inputs: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """执行 branch 节点：根据条件表达式选择下游分支。

    ``node.config.cases`` 形如：
    ``[{when: "input.score > 0.8", next: "node_a"}, {when: "*", next: "node_b"}]``

    - 顺序匹配 ``when``，``"*"`` 为默认分支
    - 命中则返回 ``(output, next)``，``next`` 作为 branch_value 用于选择出边
    - 全部不命中且无 ``"*"`` 时返回 ``(output, None)``
    """
    config = node.get("config") or {}
    cases = config.get("cases") or []
    for case in cases:
        when = case.get("when")
        nxt = case.get("next")
        if when == "*":
            return {"matched": when, "branch": nxt}, nxt
        if not isinstance(when, str):
            continue
        try:
            if _safe_eval_condition(when, inputs):
                return {"matched": when, "branch": nxt}, nxt
        except ValueError:
            continue
    return {"matched": None, "branch": None}, None


async def _execute_webhook(
    node: dict,
    inputs: dict[str, Any],
    *,
    workspace_id: str,
    actor: Actor,
) -> dict[str, Any]:
    """执行 webhook 节点：调用外部 webhook URL。

    ``node.config`` 支持：
    - ``url``: webhook URL（支持变量插值）
    - ``method``: HTTP 方法（默认 POST）
    - ``headers``: 请求头 dict
    - ``body``: 请求体 dict（支持插值）
    - ``timeout``: 超时秒数（默认 10s）
    - ``continue_on_error``: 失败不阻断 workflow（默认 True）

    SSRF 防护：复用 ``validate_resolved_outbound_url``，仅允许白名单主机
    （由 ``WORKAMA_HTTP_NODE_ALLOWED_HOSTS`` 环境变量配置）。
    """
    config = node.get("config") or {}
    method = (config.get("method") or "POST").upper()
    raw_url = config.get("url") or ""
    headers = config.get("headers") or {}
    body = config.get("body")
    continue_on_error = bool(config.get("continue_on_error", True))

    interp_context: dict[str, Any] = dict(inputs)
    interp_context.setdefault(
        "context",
        {
            "actor_id": actor.user_id,
            "workspace_id": workspace_id,
            "org_id": actor.org_id,
        },
    )
    url = str(_interpolate_value(raw_url, interp_context) or "")
    headers = _interpolate_dict(headers, interp_context)
    if isinstance(body, dict):
        body = _interpolate_dict(body, interp_context)
    elif isinstance(body, str):
        body = _interpolate_value(body, interp_context)

    if not url:
        return {"error": "webhook node requires a url", "method": "mock"}

    # SSRF 防护：白名单校验
    allowed_hosts = _http_allowed_hosts()
    err = validate_resolved_outbound_url(url, allowed_hosts)
    if err is not None:
        return {
            "error": err,
            "method": "mock",
            "continue_on_error": continue_on_error,
        }

    # 超时
    try:
        timeout_val = float(config.get("timeout") or _WEBHOOK_DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout_val = _WEBHOOK_DEFAULT_TIMEOUT

    try:
        import httpx
    except ImportError:  # pragma: no cover
        return {
            "error": "httpx not available",
            "method": "mock",
            "continue_on_error": continue_on_error,
        }

    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                httpx.request,
                method=method,
                url=url,
                headers=headers,
                json=body if isinstance(body, dict) else None,
                data=body if isinstance(body, str) else None,
                timeout=timeout_val,
            ),
            timeout=timeout_val,
        )
    except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
        return {
            "error": f"timeout: {exc}",
            "method": "mock",
            "continue_on_error": continue_on_error,
        }
    except httpx.ConnectError as exc:
        return {
            "error": f"connection_error: {exc}",
            "method": "mock",
            "continue_on_error": continue_on_error,
        }
    except Exception as exc:
        return {
            "error": f"webhook request failed: {exc}",
            "method": "mock",
            "continue_on_error": continue_on_error,
        }

    resp_headers = dict(response.headers)
    raw_body = response.text
    content_type = resp_headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            parsed_body = response.json()
        except Exception:
            parsed_body = raw_body
    else:
        parsed_body = raw_body

    return {
        "status_code": response.status_code,
        "headers": resp_headers,
        "body": parsed_body,
        "method": "http",
        "continue_on_error": continue_on_error,
    }


async def _execute_delay(
    node: dict, inputs: dict[str, Any]
) -> dict[str, Any]:
    """执行 delay 节点：异步等待指定秒数。

    ``node.config`` 支持：
    - ``seconds``: 等待秒数（必填，上限 300 防止滥用）

    生产环境应使用 job 队列调度；本实现使用 ``asyncio.sleep`` 便于测试。
    """
    config = node.get("config") or {}
    try:
        seconds = float(config.get("seconds") or 0)
    except (TypeError, ValueError):
        return {"error": "delay node requires numeric 'seconds'", "method": "mock"}
    if seconds < 0:
        return {"error": "delay 'seconds' must be non-negative", "method": "mock"}
    if seconds > _MAX_DELAY_SECONDS:
        return {
            "error": f"delay exceeds maximum of {_MAX_DELAY_SECONDS} seconds",
            "method": "mock",
        }
    if seconds > 0:
        await asyncio.sleep(seconds)
    return {
        "waited_seconds": seconds,
        "method": "delay",
    }


async def _execute_parallel(
    node: dict,
    inputs: dict[str, Any],
    *,
    workspace_id: str,
    actor: Actor,
    workflow_nodes: list[dict] | None = None,
) -> dict[str, Any]:
    """执行 parallel 节点：并行执行多个子节点。

    ``node.config.branches`` 为子节点 id 列表 ``[node_id1, node_id2, ...]``。
    所有分支共享同一 ``inputs`` 上下文，结果合并到 ``parallel_results``，
    key 为子节点 id。部分分支失败不影响其他分支（``return_exceptions=True``）。
    """
    config = node.get("config") or {}
    branch_ids = config.get("branches") or []
    if not branch_ids:
        return {"parallel_results": {}, "method": "parallel"}
    nodes_map: dict[str, dict] = {
        n["id"]: n for n in (workflow_nodes or []) if isinstance(n, dict) and "id" in n
    }

    async def _run_one(branch_id: str) -> tuple[str, dict[str, Any]]:
        sub_node = nodes_map.get(branch_id)
        if sub_node is None:
            return branch_id, {"error": f"branch node not found: {branch_id}"}
        try:
            output, _ = await _execute_node_with_retry(
                sub_node,
                inputs,
                workspace_id=workspace_id,
                actor=actor,
                max_retries=0,
                workflow_nodes=workflow_nodes,
            )
            return branch_id, output
        except HTTPException as exc:
            return branch_id, {"error": str(exc.detail)}
        except Exception as exc:  # pragma: no cover
            return branch_id, {"error": str(exc)}

    results = await asyncio.gather(
        *[_run_one(bid) for bid in branch_ids], return_exceptions=True
    )
    parallel_results: dict[str, Any] = {}
    for item in results:
        if isinstance(item, BaseException):
            parallel_results["_error"] = str(item)
            continue
        bid, out = item
        parallel_results[bid] = out
    return {"parallel_results": parallel_results, "method": "parallel"}


async def _execute_node_with_retry(
    node: dict,
    inputs: dict[str, Any],
    *,
    workspace_id: str,
    actor: Actor,
    max_retries: int = 3,
    workflow_nodes: list[dict] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """调度单个节点执行，带指数退避重试（最多 3 次）。"""
    started = time.monotonic()
    node_type = node["type"]
    branch_value: str | None = None
    last_error: str | None = None
    retries = 0

    while retries <= max_retries:
        try:
            if node_type == "llm_call":
                output = await _execute_llm_call(
                    node, inputs, workspace_id=workspace_id, actor=actor
                )
            elif node_type == "tool_call":
                output = await _execute_tool_call(node, inputs)
            elif node_type == "rag_query":
                output = await _execute_rag_query(
                    node, inputs, workspace_id=workspace_id
                )
            elif node_type == "memory_recall":
                output = await _execute_memory_recall(
                    node, inputs, workspace_id=workspace_id
                )
            elif node_type == "memory_extract":
                output = await _execute_memory_extract(
                    node, inputs, workspace_id=workspace_id, actor=actor
                )
            elif node_type == "condition":
                output, branch_value = _execute_condition(node, inputs)
            elif node_type == "output":
                output = _execute_output(node, inputs)
            elif node_type == "http_request":
                output = await _execute_http_node(
                    node, inputs, workspace_id=workspace_id, actor=actor
                )
            elif node_type == "sub_workflow":
                output = await _execute_sub_workflow(
                    node, inputs, workspace_id=workspace_id, actor=actor
                )
            elif node_type == "loop":
                output = await _execute_loop(
                    node, inputs, workspace_id=workspace_id, actor=actor
                )
            elif node_type == "human_approval":
                output = await _execute_human_approval(
                    node, inputs, workspace_id=workspace_id, actor=actor
                )
            elif node_type == "start":
                output = _execute_start(node, inputs)
            elif node_type == "end":
                output = _execute_end(node, inputs)
            elif node_type == "transform":
                output = _execute_transform(node, inputs)
            elif node_type == "branch":
                output, branch_value = _execute_branch(node, inputs)
            elif node_type == "webhook":
                output = await _execute_webhook(
                    node, inputs, workspace_id=workspace_id, actor=actor
                )
            elif node_type == "delay":
                output = await _execute_delay(node, inputs)
            elif node_type == "parallel":
                output = await _execute_parallel(
                    node,
                    inputs,
                    workspace_id=workspace_id,
                    actor=actor,
                    workflow_nodes=workflow_nodes,
                )
            else:
                raise HTTPException(
                    status_code=422,
                    detail=f"Unsupported node type: {node_type}",
                )
            output["duration_ms"] = int((time.monotonic() - started) * 1000)
            if retries > 0:
                output["retries"] = retries
            return output, branch_value
        except HTTPException:
            raise
        except Exception as exc:
            last_error = str(exc)
            retries += 1
            if retries <= max_retries:
                await asyncio.sleep(0.1 * (2 ** (retries - 1)))

    return (
        {
            "error": last_error or "unknown error",
            "duration_ms": int((time.monotonic() - started) * 1000),
            "retries": retries - 1,
        },
        branch_value,
    )


async def _execute_node(
    node: dict,
    inputs: dict[str, Any],
    *,
    workspace_id: str,
    actor: Actor,
    workflow_nodes: list[dict] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """调度单个节点执行（无重试包装，向后兼容）。"""
    return await _execute_node_with_retry(
        node,
        inputs,
        workspace_id=workspace_id,
        actor=actor,
        max_retries=0,
        workflow_nodes=workflow_nodes,
    )


async def _execute_workflow(
    workflow: dict,
    input_data: dict[str, Any],
    *,
    workspace_id: str,
    actor: Actor,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """按拓扑顺序执行 workflow 节点，返回 ``(final_output, node_runs)``。

    - 每个节点接收上游节点的合并输出作为 inputs（key = 节点 id）
    - ``condition`` 节点根据 branch_value 选择对应的出边
    - ``output`` 节点的输出会合并到 final_output
    - 节点失败（输出含 error）记录为 failed，支持 continue_on_error
    - 默认使用节点级重试（指数退避，最多 3 次）
    """
    nodes = list(workflow.get("nodes") or [])
    edges = list(workflow.get("edges") or [])
    if not nodes:
        return input_data, []
    node_map = {n["id"]: n for n in nodes}
    order = _topological_order(nodes, edges)

    # 邻接表：source -> [(target, condition_value)]
    adj: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
    for e in edges:
        adj[e["source"]].append((e["target"], e.get("condition_value")))

    # 节点输出累积：node_id -> output dict
    outputs: dict[str, dict[str, Any]] = {"_input": dict(input_data)}
    node_runs: list[dict[str, Any]] = []
    executed: set[str] = set()
    failed_nodes: set[str] = set()

    for nid in order:
        node = node_map[nid]
        # 合并上游节点输出作为 inputs
        inputs: dict[str, Any] = dict(input_data)
        # 检查上游是否所有必要来源都已执行（这里简化：所有入边来源已执行即可）
        upstream_outputs: dict[str, Any] = {}
        for src in [e["source"] for e in edges if e["target"] == nid]:
            if src in outputs:
                upstream_outputs[src] = outputs[src]
        inputs.update(upstream_outputs)
        inputs.setdefault("_input", input_data)

        # 如果上游有失败节点且当前节点未开启 continue_on_error，则跳过
        config = node.get("config") or {}
        continue_on_error = bool(config.get("continue_on_error"))
        upstream_failed = any(src in failed_nodes for src in [e["source"] for e in edges if e["target"] == nid])
        if upstream_failed and not continue_on_error:
            skip_output = {"skipped": True, "reason": "upstream_failed"}
            outputs[nid] = skip_output
            executed.add(nid)
            node_runs.append(
                {
                    "node_id": nid,
                    "node_type": node["type"],
                    "status": "skipped",
                    "output": skip_output,
                    "duration_ms": 0,
                }
            )
            continue

        try:
            output, branch_value = await _execute_node_with_retry(
                node,
                inputs,
                workspace_id=workspace_id,
                actor=actor,
                max_retries=3,
                workflow_nodes=nodes,
            )
            status_str = "failed" if "error" in output else "completed"
        except HTTPException as exc:
            output = {"error": exc.detail}
            status_str = "failed"
            branch_value = None
        outputs[nid] = output
        executed.add(nid)
        if status_str == "failed":
            failed_nodes.add(nid)
        node_runs.append(
            {
                "node_id": nid,
                "node_type": node["type"],
                "status": status_str,
                "output": output,
                "duration_ms": int(output.get("duration_ms") or 0),
                "error": output.get("error"),
                "retries": output.get("retries"),
            }
        )

    # 合并所有 output 节点的输出
    final_output: dict[str, Any] = {}
    for nr in node_runs:
        if nr["node_type"] == "output" and nr["status"] == "completed":
            final_output.update(nr["output"])

    # 没有 output 节点时：使用最后一个节点的输出
    if not final_output and node_runs:
        last = node_runs[-1]
        if "error" not in last["output"]:
            final_output = dict(last["output"])
    return final_output, node_runs


# ============================================================================
# Router 端点
# ============================================================================
#
# 路由声明顺序：具体路径（/runs/{run_id}）必须与参数化路径 /{workflow_id} 共存。
# FastAPI 按声明顺序匹配，更具体的路径先声明。


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_workflow(
    body: WorkflowCreateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """创建工作流。"""
    _require(actor, "write")
    nodes_dict = [n.model_dump() for n in body.nodes]
    edges_dict = [e.model_dump() for e in body.edges]
    _validate_dag(nodes_dict, edges_dict)
    workflow_id = new_id("wf")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                INSERT INTO workflow(
                    id, workspace_id, name, description, nodes, edges,
                    status, version, metadata)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, 1, %s::jsonb)
                RETURNING *
                """,
                (
                    workflow_id,
                    actor.workspace_id,
                    body.name,
                    body.description,
                    json_dumps(nodes_dict),
                    json_dumps(edges_dict),
                    body.status,
                    json_dumps(body.metadata),
                ),
            )
            row = await result.fetchone()
    return _summary(row)


@router.get("")
async def list_workflows(
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
            SELECT * FROM workflow
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


@router.get("/runs/{run_id}")
async def get_workflow_run(
    run_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """查询单次工作流运行详情。

    路径声明在 /{workflow_id} 之前以避免 ``runs`` 被识别为 workflow_id。
    """
    _require(actor, "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM workflow_run WHERE id = %s AND workspace_id = %s",
            (run_id, actor.workspace_id),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Workflow run not found")
    return _run_summary(row)


@router.get("/{workflow_id}")
async def get_workflow(
    workflow_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """查询工作流详情。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        row = await _owned_workflow(conn, workflow_id, actor)
    return _summary(row)


@router.patch("/{workflow_id}")
async def update_workflow(
    workflow_id: str,
    body: WorkflowUpdateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """更新工作流（部分字段；任意字段提供即更新；version 自增）。"""
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
            current = await _owned_workflow(conn, workflow_id, actor)
            if "nodes" in updates:
                updates["nodes"] = [
                    n.model_dump() if hasattr(n, "model_dump") else n
                    for n in updates["nodes"]
                ]
            if "edges" in updates:
                updates["edges"] = [
                    e.model_dump() if hasattr(e, "model_dump") else e
                    for e in updates["edges"]
                ]
            new_nodes = updates.get("nodes", current.get("nodes") or [])
            new_edges = updates.get("edges", current.get("edges") or [])
            _validate_dag(list(new_nodes), list(new_edges))
            set_clauses: list[str] = []
            params: list[object] = []
            for key, value in updates.items():
                if key in ("nodes", "edges", "metadata"):
                    set_clauses.append(f"{key} = %s::jsonb")
                    params.append(json_dumps(value))
                else:
                    set_clauses.append(f"{key} = %s")
                    params.append(value)
            set_clauses.append("version = version + 1")
            set_clauses.append("updated_at = now()")
            params.append(workflow_id)
            result = await conn.execute(
                f"""
                UPDATE workflow SET {', '.join(set_clauses)}
                WHERE id = %s RETURNING *
                """,
                tuple(params),
            )
            row = await result.fetchone()
    return _summary(row)


@router.delete("/{workflow_id}")
async def delete_workflow(
    workflow_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """删除工作流（硬删除）。"""
    _require(actor, "delete")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _owned_workflow(conn, workflow_id, actor)
            result = await conn.execute(
                "DELETE FROM workflow WHERE id = %s RETURNING id",
                (workflow_id,),
            )
            row = await result.fetchone()
    return {"id": row["id"], "deleted": True}


@router.post("/{workflow_id}/publish")
async def publish_workflow(
    workflow_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """发布工作流：status → published。"""
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            current = await _owned_workflow(conn, workflow_id, actor)
            if current["status"] == "archived":
                raise HTTPException(
                    status_code=409,
                    detail="Cannot publish an archived workflow",
                )
            result = await conn.execute(
                """
                UPDATE workflow SET status = 'published', updated_at = now()
                WHERE id = %s RETURNING *
                """,
                (workflow_id,),
            )
            row = await result.fetchone()
    return _summary(row)


@router.post("/{workflow_id}/run")
async def run_workflow(
    workflow_id: str,
    body: WorkflowRunRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """运行工作流：按 nodes/edges 拓扑顺序执行节点。

    流程：
    1. 加载工作流配置（校验 workspace 隔离 + status=published）
       - draft 状态允许运行（便于开发期测试，metadata 记录 draft_run=true）
       - archived 状态禁止运行（409）
    2. 拓扑执行节点（_execute_workflow）
    3. 写入 workflow_run（含每个节点的执行记录到 metadata.node_runs）
    """
    _require(actor, "write")
    started_at = datetime.now(UTC)
    started_mono = time.monotonic()
    run_id = new_id("wfr")
    async with pool.connection() as conn:
        workflow = await _owned_workflow(conn, workflow_id, actor)
        if workflow["status"] == "archived":
            raise HTTPException(
                status_code=409, detail="Cannot run an archived workflow"
            )
        is_draft = workflow["status"] == "draft"

        # 嵌套深度检查
        nesting_depth = 0
        parent_run_id = body.input.get("_parent_run_id")
        if parent_run_id:
            parent_result = await conn.execute(
                "SELECT nesting_depth FROM workflow_run WHERE id=%s AND workspace_id=%s",
                (parent_run_id, actor.workspace_id),
            )
            parent_row = await parent_result.fetchone()
            if parent_row:
                nesting_depth = (parent_row.get("nesting_depth") or 0) + 1

        if nesting_depth > MAX_NESTING_DEPTH:
            run_error = "Maximum workflow nesting depth exceeded"
            result = await conn.execute(
                """
                INSERT INTO workflow_run(
                    id, workflow_id, workspace_id, input, output, status,
                    started_at, completed_at, error, error_category, nesting_depth, metadata)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING *
                """,
                (
                    run_id,
                    workflow_id,
                    actor.workspace_id,
                    json_dumps(body.input),
                    json_dumps({}),
                    "failed",
                    started_at,
                    datetime.now(UTC),
                    run_error,
                    "nesting_exceeded",
                    nesting_depth,
                    json_dumps({"draft_run": is_draft}),
                ),
            )
            run_row = await result.fetchone()
            response = _run_summary(run_row)
            response["output"] = {}
            response["metadata"] = {"draft_run": is_draft}
            response["status"] = "failed"
            response["error"] = run_error
            response["started_at"] = started_at
            response["completed_at"] = datetime.now(UTC)
            return response

    try:
        final_output, node_runs = await _execute_workflow(
            workflow,
            body.input,
            workspace_id=actor.workspace_id,
            actor=actor,
        )
        run_status = "completed"
        run_error = None
    except HTTPException as exc:
        final_output = {}
        node_runs = []
        run_status = "failed"
        run_error = str(exc.detail)
    completed_at = datetime.now(UTC)
    duration_ms = int((time.monotonic() - started_mono) * 1000)

    run_metadata: dict[str, Any] = {
        "node_runs": node_runs,
        "duration_ms": duration_ms,
        "draft_run": is_draft,
        **(body.metadata or {}),
    }
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                INSERT INTO workflow_run(
                    id, workflow_id, workspace_id, input, output, status,
                    started_at, completed_at, error, metadata, nesting_depth)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s::jsonb, %s)
                RETURNING *
                """,
                (
                    run_id,
                    workflow_id,
                    actor.workspace_id,
                    json_dumps(body.input),
                    json_dumps(final_output),
                    run_status,
                    started_at,
                    completed_at,
                    run_error,
                    json_dumps(run_metadata),
                    nesting_depth,
                ),
            )
            run_row = await result.fetchone()
    response = _run_summary(run_row)
    # 使用实际计算的 final_output / run_metadata / run_status / run_error
    # （DB 行在测试中被 mock，可能不含动态字段）
    response["output"] = final_output
    response["metadata"] = run_metadata
    response["status"] = run_status
    response["error"] = run_error
    response["started_at"] = started_at
    response["completed_at"] = completed_at
    return response


@router.get("/{workflow_id}/runs")
async def list_workflow_runs(
    workflow_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """运行历史：分页查询，支持 status 过滤。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        await _owned_workflow(conn, workflow_id, actor)
        status_clause = ""
        params: list[object] = [workflow_id, actor.workspace_id]
        if status_filter:
            status_clause = "AND status = %s"
            params.append(status_filter)
        params.append(limit)
        params.append(offset)
        result = await conn.execute(
            f"""
            SELECT * FROM workflow_run
            WHERE workflow_id = %s AND workspace_id = %s {status_clause}
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


# ============================================================================
# M5 版本快照 / 回滚 / 对比
# ============================================================================


class SnapshotCreateRequest(BaseModel):
    """创建版本快照请求（changelog 可选）。"""

    changelog: str | None = Field(default=None, max_length=4000)


class RollbackRequest(BaseModel):
    """回滚到指定版本请求。"""

    version: int = Field(ge=1)


def _snapshot_summary(row: dict) -> dict:
    """将 workflow_v2_version_snapshot 行转为 API 响应 dict。"""
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "workflow_id": row["workflow_id"],
        "version": int(row["version"]),
        "snapshot": row.get("snapshot") or {},
        "changelog": row.get("changelog"),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


def _workflow_snapshot_payload(row: dict) -> dict:
    """从 workflow 行构造完整定义快照（用于写入 snapshot JSONB）。"""
    return {
        "name": row["name"],
        "description": row.get("description"),
        "nodes": list(row.get("nodes") or []),
        "edges": list(row.get("edges") or []),
        "status": row["status"],
        "version": int(row["version"]),
        "metadata": row.get("metadata") or {},
    }


def _edge_key(edge: dict) -> str:
    """边的唯一标识：``source->target[condition_value]``。"""
    src = edge.get("source") or ""
    tgt = edge.get("target") or ""
    cv = edge.get("condition_value")
    return f"{src}->{tgt}" + (f"[{cv}]" if cv else "")


def _diff_snapshots(from_snap: dict, to_snap: dict) -> dict:
    """对比两个版本快照，返回差异 JSON。

    结构：
    - ``nodes.added`` / ``nodes.removed`` / ``nodes.changed``（按 node id）
    - ``edges.added`` / ``edges.removed`` / ``edges.changed``（按 source->target）
    - ``metadata.changed``（key → {from, to}）
    - ``fields.changed``（name/description/status/version 等顶层字段变更）
    """
    from_nodes = {n["id"]: n for n in (from_snap.get("nodes") or [])}
    to_nodes = {n["id"]: n for n in (to_snap.get("nodes") or [])}
    nodes_added = [
        {"id": nid, "node": to_nodes[nid]}
        for nid in sorted(set(to_nodes) - set(from_nodes))
    ]
    nodes_removed = [
        {"id": nid, "node": from_nodes[nid]}
        for nid in sorted(set(from_nodes) - set(to_nodes))
    ]
    nodes_changed = []
    for nid in sorted(set(from_nodes) & set(to_nodes)):
        if from_nodes[nid] != to_nodes[nid]:
            nodes_changed.append(
                {
                    "id": nid,
                    "from": from_nodes[nid],
                    "to": to_nodes[nid],
                }
            )

    from_edges = {_edge_key(e): e for e in (from_snap.get("edges") or [])}
    to_edges = {_edge_key(e): e for e in (to_snap.get("edges") or [])}
    edges_added = [
        {"key": k, "edge": to_edges[k]}
        for k in sorted(set(to_edges) - set(from_edges))
    ]
    edges_removed = [
        {"key": k, "edge": from_edges[k]}
        for k in sorted(set(from_edges) - set(to_edges))
    ]
    edges_changed = []
    for k in sorted(set(from_edges) & set(to_edges)):
        if from_edges[k] != to_edges[k]:
            edges_changed.append({"key": k, "from": from_edges[k], "to": to_edges[k]})

    from_meta = from_snap.get("metadata") or {}
    to_meta = to_snap.get("metadata") or {}
    metadata_changed: dict[str, Any] = {}
    for key in sorted(set(from_meta) | set(to_meta)):
        if from_meta.get(key) != to_meta.get(key):
            metadata_changed[key] = {
                "from": from_meta.get(key),
                "to": to_meta.get(key),
            }

    fields_changed: dict[str, Any] = {}
    for field in ("name", "description", "status", "version"):
        if from_snap.get(field) != to_snap.get(field):
            fields_changed[field] = {
                "from": from_snap.get(field),
                "to": to_snap.get(field),
            }

    return {
        "nodes": {
            "added": nodes_added,
            "removed": nodes_removed,
            "changed": nodes_changed,
        },
        "edges": {
            "added": edges_added,
            "removed": edges_removed,
            "changed": edges_changed,
        },
        "metadata_changed": metadata_changed,
        "fields_changed": fields_changed,
        "has_changes": bool(
            nodes_added
            or nodes_removed
            or nodes_changed
            or edges_added
            or edges_removed
            or edges_changed
            or metadata_changed
            or fields_changed
        ),
    }


async def _owned_snapshot(
    conn: Any, workflow_id: str, version: int, actor: Actor
) -> dict:
    """查询指定版本快照并校验 workspace 归属。

    - workflow 不存在 → 404
    - 快照不存在 → 404
    - 跨 workspace → 403
    """
    # 先校验 workflow 归属（_owned_workflow 已处理 404/403）
    await _owned_workflow(conn, workflow_id, actor)
    result = await conn.execute(
        """
        SELECT * FROM workflow_v2_version_snapshot
        WHERE workflow_id = %s AND workspace_id = %s AND version = %s
        """,
        (workflow_id, actor.workspace_id, version),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Snapshot version not found")
    return row


@router.post(
    "/{workflow_id}/snapshots", status_code=status.HTTP_201_CREATED
)
async def create_workflow_snapshot(
    workflow_id: str,
    body: SnapshotCreateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """创建版本快照：version 自增，snapshot 存当前 workflow 定义。

    快照 version 在该 workflow 内部自增（基于已有最大 version + 1）。
    """
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            workflow = await _owned_workflow(conn, workflow_id, actor)
            max_result = await conn.execute(
                """
                SELECT COALESCE(max(version), 0) + 1 AS next_version
                FROM workflow_v2_version_snapshot
                WHERE workflow_id = %s AND workspace_id = %s
                """,
                (workflow_id, actor.workspace_id),
            )
            max_row = await max_result.fetchone()
            version = int((max_row or {}).get("next_version") or 1)
            snapshot_id = new_id("wfvs")
            snapshot_payload = _workflow_snapshot_payload(workflow)
            result = await conn.execute(
                """
                INSERT INTO workflow_v2_version_snapshot(
                    id, workspace_id, workflow_id, version, snapshot, changelog, created_by)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                RETURNING *
                """,
                (
                    snapshot_id,
                    actor.workspace_id,
                    workflow_id,
                    version,
                    json_dumps(snapshot_payload),
                    body.changelog,
                    actor.user_id,
                ),
            )
            row = await result.fetchone()
    return _snapshot_summary(row)


@router.get("/{workflow_id}/snapshots")
async def list_workflow_snapshots(
    workflow_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """列表快照：分页 + version DESC 排序 + workspace 隔离。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        await _owned_workflow(conn, workflow_id, actor)
        result = await conn.execute(
            """
            SELECT * FROM workflow_v2_version_snapshot
            WHERE workflow_id = %s AND workspace_id = %s
            ORDER BY version DESC LIMIT %s OFFSET %s
            """,
            (workflow_id, actor.workspace_id, limit, offset),
        )
        rows = await result.fetchall()
    items = [_snapshot_summary(row) for row in rows]
    return {
        "items": items,
        "data": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
    }


@router.get("/{workflow_id}/snapshots/{version}")
async def get_workflow_snapshot(
    workflow_id: str,
    version: int,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """获取特定版本快照详情。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        row = await _owned_snapshot(conn, workflow_id, version, actor)
    return _snapshot_summary(row)


@router.post("/{workflow_id}/rollback")
async def rollback_workflow(
    workflow_id: str,
    body: RollbackRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """回滚到指定 version。

    流程：
    1. 校验 workflow 归属
    2. 先对当前 workflow 状态创建一个快照（保存当前状态）
    3. 从目标快照恢复 workflow 定义（nodes/edges/metadata/name/description/status）
    4. version 自增
    5. 为恢复后的状态新建一个快照
    6. 返回新快照版本号
    """
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            current = await _owned_workflow(conn, workflow_id, actor)
            target_snapshot = await _owned_snapshot(
                conn, workflow_id, body.version, actor
            )
            target_def = target_snapshot.get("snapshot") or {}

            # 1. 先快照当前状态（保存回滚前的状态）
            max_result = await conn.execute(
                """
                SELECT COALESCE(max(version), 0) + 1 AS next_version
                FROM workflow_v2_version_snapshot
                WHERE workflow_id = %s AND workspace_id = %s
                """,
                (workflow_id, actor.workspace_id),
            )
            max_row = await max_result.fetchone()
            pre_rollback_version = int((max_row or {}).get("next_version") or 1)
            await conn.execute(
                """
                INSERT INTO workflow_v2_version_snapshot(
                    id, workspace_id, workflow_id, version, snapshot, changelog, created_by)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                """,
                (
                    new_id("wfvs"),
                    actor.workspace_id,
                    workflow_id,
                    pre_rollback_version,
                    json_dumps(_workflow_snapshot_payload(current)),
                    f"pre-rollback snapshot (target=v{body.version})",
                    actor.user_id,
                ),
            )

            # 2. 从目标快照恢复 workflow 定义
            restore_nodes = target_def.get("nodes") or []
            restore_edges = target_def.get("edges") or []
            restore_metadata = target_def.get("metadata") or {}
            restore_name = target_def.get("name") or current["name"]
            restore_description = target_def.get("description")
            restore_status = target_def.get("status") or "draft"
            # 校验恢复后的 DAG 合法性
            _validate_dag(list(restore_nodes), list(restore_edges))
            result = await conn.execute(
                """
                UPDATE workflow SET
                    name = %s,
                    description = %s,
                    nodes = %s::jsonb,
                    edges = %s::jsonb,
                    status = %s,
                    metadata = %s::jsonb,
                    version = version + 1,
                    updated_at = now()
                WHERE id = %s RETURNING *
                """,
                (
                    restore_name,
                    restore_description,
                    json_dumps(restore_nodes),
                    json_dumps(restore_edges),
                    restore_status,
                    json_dumps(restore_metadata),
                    workflow_id,
                ),
            )
            restored = await result.fetchone()

            # 3. 为恢复后的状态新建快照
            post_version = pre_rollback_version + 1
            result = await conn.execute(
                """
                INSERT INTO workflow_v2_version_snapshot(
                    id, workspace_id, workflow_id, version, snapshot, changelog, created_by)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                RETURNING *
                """,
                (
                    new_id("wfvs"),
                    actor.workspace_id,
                    workflow_id,
                    post_version,
                    json_dumps(_workflow_snapshot_payload(restored)),
                    f"rollback to v{body.version}",
                    actor.user_id,
                ),
            )
            new_snapshot = await result.fetchone()
    return {
        "workflow": _summary(restored),
        "rolled_back_to_version": body.version,
        "previous_version": int(current["version"]),
        "new_snapshot_version": post_version,
        "new_snapshot": _snapshot_summary(new_snapshot),
    }


@router.get("/{workflow_id}/compare")
async def compare_workflow_versions(
    workflow_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    from_version: int = Query(ge=1),
    to_version: int = Query(ge=1),
):
    """对比两个版本：返回差异 JSON（节点增删改 / 边增删改 / metadata 变更）。"""
    _require(actor, "read")
    if from_version == to_version:
        return {
            "workflow_id": workflow_id,
            "from_version": from_version,
            "to_version": to_version,
            "diff": _diff_snapshots({}, {}),
        }
    async with pool.connection() as conn:
        from_snapshot = await _owned_snapshot(
            conn, workflow_id, from_version, actor
        )
        to_snapshot = await _owned_snapshot(conn, workflow_id, to_version, actor)
    diff = _diff_snapshots(
        from_snapshot.get("snapshot") or {},
        to_snapshot.get("snapshot") or {},
    )
    return {
        "workflow_id": workflow_id,
        "from_version": from_version,
        "to_version": to_version,
        "diff": diff,
    }
