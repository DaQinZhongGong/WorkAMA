from __future__ import annotations

import asyncio
import ast
import hashlib
import json
import re
import secrets
from datetime import UTC, datetime
from collections.abc import Awaitable, Callable
from time import monotonic
from urllib.parse import urlsplit
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from workama_platform.core import Actor, capability_allows, encrypt_secret, get_actor, hash_secret, json_dumps, new_id, pool, settings
from workama_platform.modules.jobs import canonical_hash, submit_operation


router = APIRouter(prefix="/api/v1", tags=["assistants-workflows"])
public_router = APIRouter(prefix="/api/v1/public", tags=["published-assistants"])

ASSISTANT_STATUSES = frozenset({"active", "archived"})
VERSION_STATUSES = frozenset({"draft", "published", "retired"})
WORKFLOW_STATUSES = frozenset({"draft", "published", "archived"})
RUN_STATUSES = frozenset({"queued", "running", "pending_approval", "succeeded", "failed", "cancelled"})
CORE_NODE_TYPES = frozenset({
    "input", "prompt", "llm", "knowledge_retrieval",
    "condition", "transform", "approval", "output", "http_request",
    "loop", "intent_classification", "variable_aggregate",
})
SANDBOX_NODE_TYPES = frozenset({"code"})
# 真实外部执行节点：HTTP 调用与子工作流调用
EXTERNAL_NODE_TYPES = frozenset({"http", "sub_workflow"})
WORKFLOW_NODE_TYPES = CORE_NODE_TYPES | SANDBOX_NODE_TYPES | EXTERNAL_NODE_TYPES
NODE_TYPE_ALIASES = {
    # Public design vocabulary is accepted alongside the original API names.
    "start": "input",
    "answer": "output",
    "template": "prompt",
    "classifier": "intent_classification",
    "variable_aggregator": "variable_aggregate",
    "human_approval": "approval",
    "code_interpreter": "code",
    # 真实外部节点的别名
    "http_request_real": "http",
    "subworkflow": "sub_workflow",
    "call_workflow": "sub_workflow",
}
CODE_MAX_LENGTH = 32768
CODE_MAX_INPUT_BYTES = 131072
CODE_DEFAULT_TIMEOUT_SECONDS = 10
MAX_NESTING_DEPTH = 3
MAX_LOOP_ITEMS = 50
APPROVAL_DEFAULT_TIMEOUT_SECONDS = 3600
_TEMPLATE_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.-]*)\}")
# {{node_id.field}} 双花括号插值正则，用于真实外部节点引用上游输出
_INTERPOLATE_RE = re.compile(r"\{\{\s*(.+?)\s*\}\}")
# 本地代码沙箱允许的 HTTP 方法集合
HTTP_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"})


class AssistantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)


class AssistantVersionCreate(BaseModel):
    system_prompt: str = Field(default="", max_length=12000)
    model: str = Field(default="workama-chat", min_length=1, max_length=120)
    generation_config: dict[str, Any] = Field(default_factory=dict, alias="model_config")
    toolset: list[str] = Field(default_factory=list, max_length=32)
    dataset_ids: list[str] = Field(default_factory=list, max_length=20)
    greeting: str = Field(default="", max_length=1000)


class AssistantInvoke(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    gateway_api_key: str = Field(min_length=8, max_length=256)


class WorkflowGraph(BaseModel):
    nodes: list[dict[str, Any]] = Field(min_length=2, max_length=100)
    edges: list[dict[str, Any]] = Field(default_factory=list, max_length=300)


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    graph: WorkflowGraph


class WorkflowUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    graph: WorkflowGraph | None = None
    status: Literal["draft", "published"] | None = None


class WorkflowRunCreate(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)
    gateway_api_key: str | None = Field(default=None, min_length=8, max_length=256)
    dry_run: bool = False


def ensure_workflow_schema_statements() -> tuple[str, ...]:
    return (
        """
        CREATE TABLE IF NOT EXISTS pf_assistant (
          id TEXT PRIMARY KEY,
          org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
          workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
          current_version_id TEXT,
          share_token_hash TEXT,
          created_by TEXT NOT NULL REFERENCES id_user(id),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE(workspace_id, name)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pf_assistant_version (
          id TEXT PRIMARY KEY,
          assistant_id TEXT NOT NULL REFERENCES pf_assistant(id) ON DELETE CASCADE,
          workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
          version INTEGER NOT NULL,
          system_prompt TEXT NOT NULL DEFAULT '',
          model TEXT NOT NULL,
          model_config JSONB NOT NULL DEFAULT '{}'::jsonb,
          toolset TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
          dataset_ids TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
          greeting TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','retired')),
          created_by TEXT NOT NULL REFERENCES id_user(id),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE(assistant_id, version)
        )
        """,
        "ALTER TABLE pf_assistant ADD COLUMN IF NOT EXISTS current_version_id TEXT",
        "ALTER TABLE pf_assistant ADD COLUMN IF NOT EXISTS share_token_hash TEXT",
        """
        CREATE TABLE IF NOT EXISTS pf_app_run (
          id TEXT PRIMARY KEY,
          app_id TEXT NOT NULL REFERENCES pf_assistant(id) ON DELETE CASCADE,
          app_type TEXT NOT NULL DEFAULT 'assistant' CHECK (app_type IN ('assistant','workflow','agent','external')),
          version_id TEXT NOT NULL REFERENCES pf_assistant_version(id) ON DELETE RESTRICT,
          org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
          workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
          actor_id TEXT NOT NULL REFERENCES id_user(id),
          trigger TEXT NOT NULL DEFAULT 'console' CHECK (trigger IN ('console','api','share')),
          status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
          input_meta JSONB NOT NULL DEFAULT '{}'::jsonb,
          output_meta JSONB NOT NULL DEFAULT '{}'::jsonb,
          error TEXT,
          credits NUMERIC,
          duration_ms INTEGER,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          started_at TIMESTAMPTZ,
          completed_at TIMESTAMPTZ
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pf_app_run_event (
          id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL REFERENCES pf_app_run(id) ON DELETE CASCADE,
          app_id TEXT NOT NULL REFERENCES pf_assistant(id) ON DELETE CASCADE,
          workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
          seq INTEGER NOT NULL,
          event_type TEXT NOT NULL,
          payload JSONB NOT NULL DEFAULT '{}'::jsonb,
          occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE(run_id, seq)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pf_workflow (
          id TEXT PRIMARY KEY,
          org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
          workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          version INTEGER NOT NULL DEFAULT 1,
          graph JSONB NOT NULL,
          status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','archived')),
          created_by TEXT NOT NULL REFERENCES id_user(id),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE(workspace_id, name)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pf_workflow_run (
          id TEXT PRIMARY KEY,
          workflow_id TEXT NOT NULL REFERENCES pf_workflow(id) ON DELETE CASCADE,
          org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
          workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
          created_by TEXT NOT NULL REFERENCES id_user(id),
          input JSONB NOT NULL DEFAULT '{}'::jsonb,
          workflow_version INTEGER NOT NULL DEFAULT 1,
          output JSONB NOT NULL DEFAULT '{}'::jsonb,
          trace JSONB NOT NULL DEFAULT '[]'::jsonb,
          status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','pending_approval','succeeded','failed','cancelled')),
          error TEXT,
          error_category TEXT,
          timeout_at TIMESTAMPTZ,
          iteration_count INTEGER,
          nesting_depth INTEGER NOT NULL DEFAULT 0,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          started_at TIMESTAMPTZ,
          completed_at TIMESTAMPTZ
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pf_workflow_event (
          id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL REFERENCES pf_workflow_run(id) ON DELETE CASCADE,
          workflow_id TEXT NOT NULL REFERENCES pf_workflow(id) ON DELETE CASCADE,
          workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
          seq INTEGER NOT NULL,
          event_type TEXT NOT NULL,
          payload JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE(run_id, seq)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pf_workflow_version (
          id TEXT PRIMARY KEY,
          workflow_id TEXT NOT NULL REFERENCES pf_workflow(id) ON DELETE CASCADE,
          workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
          version INTEGER NOT NULL,
          graph JSONB NOT NULL,
          created_by TEXT NOT NULL REFERENCES id_user(id),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE(workflow_id, version)
        )
        """,
        "ALTER TABLE pf_workflow_run ADD COLUMN IF NOT EXISTS workflow_version INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE pf_workflow_run ADD COLUMN IF NOT EXISTS operation_id TEXT REFERENCES ops_async_operation(id) ON DELETE SET NULL",
        """
        INSERT INTO pf_workflow_version(id,workflow_id,workspace_id,version,graph,created_by)
        SELECT 'wfv-' || id, id, workspace_id, version, graph, created_by
        FROM pf_workflow
        ON CONFLICT (workflow_id, version) DO NOTHING
        """,
        "CREATE INDEX IF NOT EXISTS idx_pf_assistant_workspace_status ON pf_assistant(workspace_id, status, updated_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_pf_app_run_app_time ON pf_app_run(app_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_pf_app_run_workspace_time ON pf_app_run(workspace_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_pf_app_run_event_run_seq ON pf_app_run_event(run_id, seq)",
        "CREATE INDEX IF NOT EXISTS idx_pf_workflow_workspace_status ON pf_workflow(workspace_id, status, updated_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_pf_workflow_run_workflow_time ON pf_workflow_run(workflow_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_pf_workflow_event_run_seq ON pf_workflow_event(run_id, seq)",
        "CREATE INDEX IF NOT EXISTS idx_pf_workflow_run_operation ON pf_workflow_run(operation_id)",
        "CREATE INDEX IF NOT EXISTS idx_pf_workflow_version_time ON pf_workflow_version(workflow_id, version DESC)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_pf_assistant_share_token ON pf_assistant(share_token_hash) WHERE share_token_hash IS NOT NULL",
    )


async def ensure_workflow_schema(conn) -> None:
    for statement in ensure_workflow_schema_statements():
        await conn.execute(statement)


async def append_workflow_event(
    conn,
    *,
    run_id: str,
    workflow_id: str,
    workspace_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> int:
    result = await conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM pf_workflow_event WHERE run_id=%s",
        (run_id,),
    )
    sequence = int((await result.fetchone())["next_seq"])
    await conn.execute(
        "INSERT INTO pf_workflow_event(id,run_id,workflow_id,workspace_id,seq,event_type,payload) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)",
        (new_id("wfe"), run_id, workflow_id, workspace_id, sequence, event_type, json_dumps(payload)),
    )
    return sequence


def _require(actor: Actor, domain: str, action: str) -> None:
    if not capability_allows(actor.capabilities, f"{domain}:{action}"):
        raise HTTPException(status_code=403, detail=f"Missing capability: {domain}:{action}")


def _assistant_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"], "name": row["name"], "description": row["description"],
        "status": row["status"], "current_version_id": row.get("current_version_id"),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _version_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"], "assistant_id": row["assistant_id"], "version": row["version"],
        "system_prompt": row["system_prompt"], "model": row["model"],
        "model_config": row.get("model_config") or {}, "toolset": row.get("toolset") or [],
        "dataset_ids": row.get("dataset_ids") or [], "greeting": row.get("greeting") or "",
        "status": row["status"], "created_at": row["created_at"],
    }


def _assistant_run_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"], "app_id": row["app_id"], "app_type": row["app_type"],
        "version_id": row["version_id"], "actor_id": row["actor_id"], "trigger": row["trigger"],
        "status": row["status"], "input_meta": row.get("input_meta") or {},
        "output_meta": row.get("output_meta") or {}, "error": row.get("error"),
        "credits": row.get("credits"), "duration_ms": row.get("duration_ms"),
        "created_at": row["created_at"], "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
    }


async def append_assistant_run_event(
    conn,
    *,
    run_id: str,
    app_id: str,
    workspace_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> int:
    result = await conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM pf_app_run_event WHERE run_id=%s",
        (run_id,),
    )
    sequence = int((await result.fetchone())["next_seq"])
    await conn.execute(
        "INSERT INTO pf_app_run_event(id,run_id,app_id,workspace_id,seq,event_type,payload) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)",
        (new_id("ape"), run_id, app_id, workspace_id, sequence, event_type, json_dumps(payload)),
    )
    return sequence


async def finish_assistant_run(
    *,
    run_id: str,
    app_id: str,
    workspace_id: str,
    status_value: Literal["succeeded", "failed", "cancelled"],
    duration_ms: int,
    output_meta: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE pf_app_run SET status=%s, output_meta=%s::jsonb, error=%s, duration_ms=%s, completed_at=now() WHERE id=%s AND app_id=%s AND workspace_id=%s",
            (status_value, json_dumps(output_meta or {}), error, duration_ms, run_id, app_id, workspace_id),
        )
        await append_assistant_run_event(
            conn,
            run_id=run_id,
            app_id=app_id,
            workspace_id=workspace_id,
            event_type=f"run_{status_value}",
            payload={"duration_ms": duration_ms, "error": error} if error else {"duration_ms": duration_ms, "output": output_meta or {}},
        )
        await conn.commit()


def _workflow_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"], "name": row["name"], "description": row["description"],
        "version": row["version"], "graph": row.get("graph") or {"nodes": [], "edges": []},
        "status": row["status"], "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _node_id(node: dict[str, Any]) -> str:
    return str(node.get("id", "")).strip()


def canonical_node_type(value: Any) -> str:
    """Normalize design-facing node names before validation or execution."""
    raw = str(value or "").strip()
    return NODE_TYPE_ALIASES.get(raw, raw)


def code_validation_errors(source: Any, config: dict[str, Any]) -> list[str]:
    if str(config.get("language", "python")).lower() != "python":
        return ["code node only permits Python"]
    text = str(source or "")
    if not text:
        return ["code node requires code"]
    if len(text) > CODE_MAX_LENGTH:
        return [f"code node is limited to {CODE_MAX_LENGTH} characters"]
    try:
        tree = ast.parse(text, mode="exec")
    except SyntaxError:
        return ["code node contains invalid Python syntax"]
    denied_names = {"__import__", "breakpoint", "compile", "eval", "exec", "input", "open"}
    for item in ast.walk(tree):
        if isinstance(item, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)):
            return ["code node imports and global scope mutation are disabled"]
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Name) and item.func.id in denied_names:
            return [f"code node call is disabled: {item.func.id}"]
        if isinstance(item, ast.Attribute) and item.attr.startswith("__"):
            return ["code node dunder attribute access is disabled"]
    try:
        timeout = int(config.get("timeout_seconds", CODE_DEFAULT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        timeout = 0
    if timeout < 1 or timeout > 120:
        return ["code node timeout_seconds must be between 1 and 120"]
    return []


def validate_graph(graph: dict[str, Any] | WorkflowGraph) -> list[str]:
    raw = graph.model_dump() if isinstance(graph, WorkflowGraph) else graph
    nodes = raw.get("nodes") or []
    edges = raw.get("edges") or []
    errors: list[str] = []
    ids = [_node_id(node) for node in nodes]
    if len(ids) != len(set(ids)):
        errors.append("node ids must be unique")
    if any(not node_id for node_id in ids):
        errors.append("every node must have an id")
    if any(canonical_node_type(node.get("type")) not in WORKFLOW_NODE_TYPES for node in nodes):
        errors.append("workflow contains an unsupported core node type")
    for node in nodes:
        node_type = canonical_node_type(node.get("type"))
        config = node.get("config") or {}
        if node_type == "code":
            errors.extend(code_validation_errors(config.get("code"), config))
        if node_type == "http_request":
            parsed = urlsplit(str(config.get("url", "")))
            if parsed.scheme != "mock" or not parsed.netloc:
                errors.append("http_request only permits mock:// endpoints")
        if node_type == "http":
            # 真实 HTTP 节点：仅允许 http/https 且必须带 host
            parsed = urlsplit(str(config.get("url", "")))
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append("http node requires an http(s) url")
            method = str(config.get("method", "GET")).upper()
            if method not in HTTP_ALLOWED_METHODS:
                errors.append("http node method must be a valid HTTP verb")
            try:
                http_timeout = float(config.get("timeout", 30.0))
            except (TypeError, ValueError):
                http_timeout = 0
            if http_timeout < 1 or http_timeout > 120:
                errors.append("http node timeout must be between 1 and 120 seconds")
        if node_type == "sub_workflow":
            if not str(config.get("workflow_id", "")).strip():
                errors.append("sub_workflow node requires a workflow_id")
            try:
                sub_timeout = float(config.get("timeout", 300.0))
            except (TypeError, ValueError):
                sub_timeout = 0
            if sub_timeout < 1 or sub_timeout > 1800:
                errors.append("sub_workflow node timeout must be between 1 and 1800 seconds")
        if node_type == "loop":
            try:
                max_iterations = int(config.get("max_iterations", 100))
            except (TypeError, ValueError):
                max_iterations = 0
            if max_iterations < 1 or max_iterations > 100:
                errors.append("loop max_iterations must be between 1 and 100")
    if not any(canonical_node_type(node.get("type")) == "input" for node in nodes):
        errors.append("workflow must contain an input node")
    if not any(canonical_node_type(node.get("type")) == "output" for node in nodes):
        errors.append("workflow must contain an output node")
    node_ids = set(ids)
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in ids}
    indegree = {node_id: 0 for node_id in ids}
    for edge in edges:
        source = str(edge.get("source", "")).strip()
        target = str(edge.get("target", "")).strip()
        if source not in node_ids or target not in node_ids:
            errors.append("workflow edge references an unknown node")
            continue
        adjacency[source].append(target)
        indegree[target] += 1
    queue = [node_id for node_id, degree in indegree.items() if degree == 0]
    visited = 0
    while queue:
        current = queue.pop(0)
        visited += 1
        for target in adjacency[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if visited != len(ids):
        errors.append("workflow graph must be acyclic")
    return sorted(set(errors))


def topological_order(graph: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    by_id = {_node_id(node): node for node in nodes}
    outgoing = {node_id: [] for node_id in by_id}
    indegree = {node_id: 0 for node_id in by_id}
    for edge in edges:
        source, target = str(edge["source"]), str(edge["target"])
        outgoing[source].append(target)
        indegree[target] += 1
    queue = [node_id for node_id, degree in indegree.items() if degree == 0]
    ordered: list[dict[str, Any]] = []
    while queue:
        current = queue.pop(0)
        ordered.append(by_id[current])
        for target in outgoing[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    return ordered


def render_template(value: str, context: dict[str, Any]) -> str:
    def replacement(match: re.Match[str]) -> str:
        current: Any = context
        for part in match.group(1).split("."):
            if isinstance(current, dict):
                current = current.get(part, "")
            else:
                current = ""
        return str(current if current is not None else "")
    return _TEMPLATE_RE.sub(replacement, value)


def context_value(path: str, context: dict[str, Any]) -> Any:
    current: Any = context
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _safe_digest(value: Any) -> str:
    return hashlib.sha256(json_dumps(value).encode("utf-8")).hexdigest()


def _classify_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if "nesting depth exceeded" in msg:
        return "nesting_exceeded"
    if "approval timeout" in msg:
        return "approval_timeout"
    if "loop limit exceeded" in msg:
        return "loop_limit_exceeded"
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    if "connection" in msg or "network" in msg or "unavailable" in msg:
        return "network"
    if any(k in msg for k in ("required", "requires", "invalid", "must be", "missing", "not found")):
        return "user"
    return "system"


async def _assistant(conn, assistant_id: str, workspace_id: str) -> dict[str, Any]:
    result = await conn.execute(
        "SELECT * FROM pf_assistant WHERE id=%s AND workspace_id=%s AND status='active'",
        (assistant_id, workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Assistant not found")
    return row


async def _workflow(conn, workflow_id: str, workspace_id: str) -> dict[str, Any]:
    result = await conn.execute(
        "SELECT * FROM pf_workflow WHERE id=%s AND workspace_id=%s AND status <> 'archived'",
        (workflow_id, workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return row


@router.get("/assistants")
async def list_assistants(actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "assistant", "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM pf_assistant WHERE workspace_id=%s ORDER BY updated_at DESC",
            (actor.workspace_id,),
        )
        rows = await result.fetchall()
    return {"items": [_assistant_summary(row) for row in rows], "data": [_assistant_summary(row) for row in rows], "next_cursor": None, "has_more": False, "meta": {"request_id": None}}


@router.post("/assistants", status_code=status.HTTP_201_CREATED)
async def create_assistant(body: AssistantCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "assistant", "write")
    assistant_id = new_id("ast")
    async with pool.connection() as conn:
        try:
            result = await conn.execute(
                """
                INSERT INTO pf_assistant(id,org_id,workspace_id,name,description,created_by)
                VALUES (%s,%s,%s,%s,%s,%s) RETURNING *
                """,
                (assistant_id, actor.org_id, actor.workspace_id, body.name.strip(), body.description, actor.user_id),
            )
            row = await result.fetchone()
            await conn.commit()
        except Exception as exc:
            await conn.rollback()
            if "unique" in str(exc).lower():
                raise HTTPException(status_code=409, detail="Assistant name already exists") from exc
            raise
    return _assistant_summary(row)


@router.get("/assistants/{assistant_id}")
async def get_assistant(assistant_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "assistant", "read")
    async with pool.connection() as conn:
        assistant = await _assistant(conn, assistant_id, actor.workspace_id)
        versions_result = await conn.execute(
            "SELECT * FROM pf_assistant_version WHERE assistant_id=%s ORDER BY version DESC",
            (assistant_id,),
        )
        versions = await versions_result.fetchall()
    return {**_assistant_summary(assistant), "versions": [_version_summary(row) for row in versions]}


@router.get("/assistants/{assistant_id}/runs")
async def list_assistant_runs(
    assistant_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=50, ge=1, le=200),
):
    _require(actor, "assistant", "read")
    async with pool.connection() as conn:
        await _assistant(conn, assistant_id, actor.workspace_id)
        result = await conn.execute(
            "SELECT * FROM pf_app_run WHERE app_id=%s AND workspace_id=%s ORDER BY created_at DESC LIMIT %s",
            (assistant_id, actor.workspace_id, limit),
        )
        rows = await result.fetchall()
    return {"items": [_assistant_run_summary(row) for row in rows], "data": [_assistant_run_summary(row) for row in rows], "next_cursor": None, "has_more": False, "meta": {"request_id": None}}


@router.get("/assistants/{assistant_id}/runs/{run_id}")
async def get_assistant_run(assistant_id: str, run_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "assistant", "read")
    async with pool.connection() as conn:
        await _assistant(conn, assistant_id, actor.workspace_id)
        result = await conn.execute(
            "SELECT * FROM pf_app_run WHERE id=%s AND app_id=%s AND workspace_id=%s",
            (run_id, assistant_id, actor.workspace_id),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Assistant run not found")
        events_result = await conn.execute(
            "SELECT id,run_id,seq,event_type,payload,occurred_at FROM pf_app_run_event WHERE run_id=%s AND workspace_id=%s ORDER BY seq ASC",
            (run_id, actor.workspace_id),
        )
        events = await events_result.fetchall()
    return {"run": _assistant_run_summary(row), "events": events}


@router.get("/assistants/{assistant_id}/runs/{run_id}/events")
async def list_assistant_run_events(assistant_id: str, run_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "assistant", "read")
    async with pool.connection() as conn:
        await _assistant(conn, assistant_id, actor.workspace_id)
        result = await conn.execute(
            "SELECT id,run_id,seq,event_type,payload,occurred_at FROM pf_app_run_event WHERE run_id=%s AND app_id=%s AND workspace_id=%s ORDER BY seq ASC",
            (run_id, assistant_id, actor.workspace_id),
        )
        events = await result.fetchall()
    return {"items": events, "data": events, "next_cursor": None, "has_more": False, "meta": {"request_id": None}}


@router.post("/assistants/{assistant_id}/versions", status_code=status.HTTP_201_CREATED)
async def create_assistant_version(assistant_id: str, body: AssistantVersionCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "assistant", "write")
    async with pool.connection() as conn:
        await _assistant(conn, assistant_id, actor.workspace_id)
        version_result = await conn.execute(
            "SELECT COALESCE(max(version), 0) + 1 AS next_version FROM pf_assistant_version WHERE assistant_id=%s",
            (assistant_id,),
        )
        version = (await version_result.fetchone())["next_version"]
        result = await conn.execute(
            """
            INSERT INTO pf_assistant_version(id,assistant_id,workspace_id,version,system_prompt,model,model_config,toolset,dataset_ids,greeting,created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s) RETURNING *
            """,
            (new_id("asv"), assistant_id, actor.workspace_id, version, body.system_prompt, body.model,
             json_dumps(body.generation_config), body.toolset, body.dataset_ids, body.greeting, actor.user_id),
        )
        row = await result.fetchone()
        await conn.commit()
    return _version_summary(row)


@router.post("/assistants/{assistant_id}/versions/{version_id}/publish")
async def publish_assistant_version(assistant_id: str, version_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "assistant", "write")
    raw_share_token = secrets.token_urlsafe(32)
    async with pool.connection() as conn:
        assistant = await _assistant(conn, assistant_id, actor.workspace_id)
        result = await conn.execute(
            "SELECT * FROM pf_assistant_version WHERE id=%s AND assistant_id=%s AND workspace_id=%s",
            (version_id, assistant_id, actor.workspace_id),
        )
        version = await result.fetchone()
        if not version:
            raise HTTPException(status_code=404, detail="Assistant version not found")
        async with conn.transaction():
            await conn.execute("UPDATE pf_assistant_version SET status='retired' WHERE assistant_id=%s AND status='published'", (assistant_id,))
            published_result = await conn.execute(
                "UPDATE pf_assistant_version SET status='published' WHERE id=%s RETURNING *",
                (version_id,),
            )
            published = await published_result.fetchone()
            await conn.execute(
                "UPDATE pf_assistant SET current_version_id=%s, share_token_hash=%s, updated_at=now() WHERE id=%s",
                (version_id, hash_secret(raw_share_token), assistant_id),
            )
        await conn.commit()
    return {"assistant": _assistant_summary({**assistant, "current_version_id": version_id}), "version": _version_summary(published), "share_token": raw_share_token}


@public_router.get("/assistants/{share_token}")
async def get_public_assistant(share_token: str):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT a.id, a.name, a.description, a.current_version_id,
                   v.version, v.model, v.greeting
            FROM pf_assistant a JOIN pf_assistant_version v ON v.id=a.current_version_id
            WHERE a.share_token_hash=%s AND a.status='active' AND v.status='published'
            """,
            (hash_secret(share_token),),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Published assistant not found")
    return {"id": row["id"], "name": row["name"], "description": row["description"], "version": row["version"], "model": row["model"], "greeting": row["greeting"]}


@router.post("/assistants/{assistant_id}/invoke")
async def invoke_assistant(assistant_id: str, body: AssistantInvoke, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "assistant", "read")
    async with pool.connection() as conn:
        assistant = await _assistant(conn, assistant_id, actor.workspace_id)
        result = await conn.execute(
            "SELECT * FROM pf_assistant_version WHERE id=%s AND status='published'",
            (assistant.get("current_version_id"),),
        )
        version = await result.fetchone()
    if not version:
        raise HTTPException(status_code=409, detail="Assistant has no published version")
    run_id = new_id("apr")
    started = monotonic()
    message_bytes = body.message.encode("utf-8")
    input_meta = {
        "message_sha256": hashlib.sha256(message_bytes).hexdigest(),
        "message_bytes": len(message_bytes),
    }
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO pf_app_run(id,app_id,app_type,version_id,org_id,workspace_id,actor_id,trigger,status,input_meta,started_at) VALUES (%s,%s,'assistant',%s,%s,%s,%s,'console','running',%s::jsonb,now())",
            (run_id, assistant_id, version["id"], actor.org_id, actor.workspace_id, actor.user_id, json_dumps(input_meta)),
        )
        await append_assistant_run_event(
            conn,
            run_id=run_id,
            app_id=assistant_id,
            workspace_id=actor.workspace_id,
            event_type="run_started",
            payload={"version": version["version"], "model": version["model"], "input": input_meta},
        )
        await conn.commit()
    messages = []
    if version["system_prompt"]:
        messages.append({"role": "system", "content": version["system_prompt"]})
    messages.append({"role": "user", "content": body.message})
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{settings.gateway_url.rstrip('/')}/v1/chat/completions",
                headers={"Authorization": f"Bearer {body.gateway_api_key}"},
                json={"model": version["model"], "messages": messages, "stream": False, **(version.get("model_config") or {})},
            )
    except httpx.HTTPError as exc:
        await finish_assistant_run(
            run_id=run_id,
            app_id=assistant_id,
            workspace_id=actor.workspace_id,
            status_value="failed",
            duration_ms=max(0, int((monotonic() - started) * 1000)),
            error="Assistant gateway request failed",
        )
        raise HTTPException(status_code=502, detail="Assistant gateway request failed") from exc
    if response.status_code >= 400:
        await finish_assistant_run(
            run_id=run_id,
            app_id=assistant_id,
            workspace_id=actor.workspace_id,
            status_value="failed",
            duration_ms=max(0, int((monotonic() - started) * 1000)),
            error="Assistant gateway request was rejected",
        )
        raise HTTPException(status_code=502, detail="Assistant gateway request was rejected")
    try:
        response_payload = response.json()
    except ValueError as exc:
        await finish_assistant_run(
            run_id=run_id,
            app_id=assistant_id,
            workspace_id=actor.workspace_id,
            status_value="failed",
            duration_ms=max(0, int((monotonic() - started) * 1000)),
            error="Assistant gateway returned invalid JSON",
        )
        raise HTTPException(status_code=502, detail="Assistant gateway returned invalid JSON") from exc
    choices = response_payload.get("choices") if isinstance(response_payload, dict) else []
    first_choice = choices[0] if isinstance(choices, list) and choices else {}
    output_meta = {
        "response_id": response_payload.get("id") if isinstance(response_payload, dict) else None,
        "model": response_payload.get("model", version["model"]) if isinstance(response_payload, dict) else version["model"],
        "usage": response_payload.get("usage") if isinstance(response_payload, dict) else {},
        "finish_reason": first_choice.get("finish_reason") if isinstance(first_choice, dict) else None,
    }
    await finish_assistant_run(
        run_id=run_id,
        app_id=assistant_id,
        workspace_id=actor.workspace_id,
        status_value="succeeded",
        duration_ms=max(0, int((monotonic() - started) * 1000)),
        output_meta=output_meta,
    )
    return {"assistant_id": assistant_id, "run_id": run_id, "version": version["version"], "response": response_payload}


@router.get("/workflows")
async def list_workflows(actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "workflow", "read")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM pf_workflow WHERE workspace_id=%s ORDER BY updated_at DESC", (actor.workspace_id,))
        rows = await result.fetchall()
    return {"items": [_workflow_summary(row) for row in rows], "data": [_workflow_summary(row) for row in rows], "next_cursor": None, "has_more": False, "meta": {"request_id": None}}


@router.post("/workflows", status_code=status.HTTP_201_CREATED)
async def create_workflow(body: WorkflowCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "workflow", "write")
    graph = body.graph.model_dump()
    errors = validate_graph(graph)
    if errors:
        raise HTTPException(status_code=422, detail={"code": "E05001", "errors": errors})
    workflow_id = new_id("wfl")
    async with pool.connection() as conn:
        try:
            result = await conn.execute(
                """
                INSERT INTO pf_workflow(id,org_id,workspace_id,name,description,graph,created_by)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s) RETURNING *
                """,
                (workflow_id, actor.org_id, actor.workspace_id, body.name.strip(), body.description, json_dumps(graph), actor.user_id),
            )
            row = await result.fetchone()
            await conn.execute(
                "INSERT INTO pf_workflow_version(id,workflow_id,workspace_id,version,graph,created_by) VALUES (%s,%s,%s,%s,%s::jsonb,%s)",
                (new_id("wfv"), workflow_id, actor.workspace_id, row["version"], json_dumps(graph), actor.user_id),
            )
            await conn.commit()
        except Exception as exc:
            await conn.rollback()
            if "unique" in str(exc).lower():
                raise HTTPException(status_code=409, detail="Workflow name already exists") from exc
            raise
    return _workflow_summary(row)


@router.get("/workflows/{workflow_id}")
async def get_workflow(workflow_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "workflow", "read")
    async with pool.connection() as conn:
        row = await _workflow(conn, workflow_id, actor.workspace_id)
    return _workflow_summary(row)


@router.patch("/workflows/{workflow_id}")
async def update_workflow(workflow_id: str, body: WorkflowUpdate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "workflow", "write")
    if body.name is None and body.description is None and body.graph is None and body.status is None:
        raise HTTPException(status_code=422, detail="At least one workflow field is required")
    graph = body.graph.model_dump() if body.graph else None
    if graph is not None:
        errors = validate_graph(graph)
        if errors:
            raise HTTPException(status_code=422, detail={"code": "E05001", "errors": errors})
    async with pool.connection() as conn:
        current = await _workflow(conn, workflow_id, actor.workspace_id)
        if body.status == "published":
            errors = validate_graph(graph or current["graph"])
            if errors:
                raise HTTPException(status_code=422, detail={"code": "E05001", "errors": errors})
        result = await conn.execute(
            """
            UPDATE pf_workflow SET name=COALESCE(%s,name), description=COALESCE(%s,description),
              graph=COALESCE(%s::jsonb,graph), version=version+1, status=COALESCE(%s,status), updated_at=now()
            WHERE id=%s AND workspace_id=%s RETURNING *
            """,
            (body.name, body.description, json_dumps(graph) if graph is not None else None, body.status, workflow_id, actor.workspace_id),
        )
        row = await result.fetchone()
        await conn.execute(
            "INSERT INTO pf_workflow_version(id,workflow_id,workspace_id,version,graph,created_by) VALUES (%s,%s,%s,%s,%s::jsonb,%s)",
            (new_id("wfv"), workflow_id, actor.workspace_id, row["version"], json_dumps(row["graph"]), actor.user_id),
        )
        await conn.commit()
    return _workflow_summary(row)


@router.post("/workflows/{workflow_id}/validate")
async def validate_workflow(workflow_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "workflow", "read")
    async with pool.connection() as conn:
        row = await _workflow(conn, workflow_id, actor.workspace_id)
    errors = validate_graph(row["graph"])
    return {"workflow_id": workflow_id, "valid": not errors, "errors": errors, "node_types": sorted({node.get("type") for node in row["graph"].get("nodes", [])})}


@router.get("/workflows/{workflow_id}/versions")
async def list_workflow_versions(workflow_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "workflow", "read")
    async with pool.connection() as conn:
        await _workflow(conn, workflow_id, actor.workspace_id)
        result = await conn.execute(
            "SELECT id,workflow_id,version,created_by,created_at FROM pf_workflow_version WHERE workflow_id=%s AND workspace_id=%s ORDER BY version DESC",
            (workflow_id, actor.workspace_id),
        )
        rows = await result.fetchall()
    return {"items": rows, "data": rows, "next_cursor": None, "has_more": False, "meta": {"request_id": None}}


@router.post("/workflows/{workflow_id}/versions/{version}/rollback")
async def rollback_workflow(workflow_id: str, version: int, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "workflow", "write")
    async with pool.connection() as conn:
        current = await _workflow(conn, workflow_id, actor.workspace_id)
        result = await conn.execute(
            "SELECT graph FROM pf_workflow_version WHERE workflow_id=%s AND workspace_id=%s AND version=%s",
            (workflow_id, actor.workspace_id, version),
        )
        snapshot = await result.fetchone()
        if not snapshot:
            raise HTTPException(status_code=404, detail="Workflow version not found")
        result = await conn.execute(
            """
            UPDATE pf_workflow SET graph=%s::jsonb, version=version+1, status='draft', updated_at=now()
            WHERE id=%s AND workspace_id=%s RETURNING *
            """,
            (json_dumps(snapshot["graph"]), workflow_id, actor.workspace_id),
        )
        row = await result.fetchone()
        await conn.execute(
            "INSERT INTO pf_workflow_version(id,workflow_id,workspace_id,version,graph,created_by) VALUES (%s,%s,%s,%s,%s::jsonb,%s)",
            (new_id("wfv"), workflow_id, actor.workspace_id, row["version"], json_dumps(row["graph"]), actor.user_id),
        )
        await conn.commit()
    return {"workflow": _workflow_summary(row), "rolled_back_to_version": version, "previous_version": current["version"]}


@router.get("/workflows/{workflow_id}/runs/compare")
async def compare_workflow_runs(
    workflow_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    left_run_id: str = Query(min_length=1, max_length=120),
    right_run_id: str = Query(min_length=1, max_length=120),
):
    _require(actor, "workflow", "read")
    if left_run_id == right_run_id:
        raise HTTPException(status_code=422, detail="left_run_id and right_run_id must differ")
    async with pool.connection() as conn:
        await _workflow(conn, workflow_id, actor.workspace_id)
        result = await conn.execute(
            "SELECT * FROM pf_workflow_run WHERE workflow_id=%s AND workspace_id=%s AND id=ANY(%s)",
            (workflow_id, actor.workspace_id, [left_run_id, right_run_id]),
        )
        rows = {row["id"]: row for row in await result.fetchall()}
    if set(rows) != {left_run_id, right_run_id}:
        raise HTTPException(status_code=404, detail="One or more workflow runs not found")
    left, right = rows[left_run_id], rows[right_run_id]
    left_trace = {item.get("node_id"): item for item in left.get("trace") or [] if item.get("node_id")}
    right_trace = {item.get("node_id"): item for item in right.get("trace") or [] if item.get("node_id")}
    changes = []
    for node_id in sorted(set(left_trace) | set(right_trace)):
        left_item, right_item = left_trace.get(node_id), right_trace.get(node_id)
        if (left_item or {}).get("status") != (right_item or {}).get("status") or (left_item or {}).get("output_digest") != (right_item or {}).get("output_digest"):
            changes.append({"node_id": node_id, "left": left_item, "right": right_item})
    return {
        "workflow_id": workflow_id,
        "left": {"id": left["id"], "status": left["status"], "workflow_version": left.get("workflow_version", 1), "output_digest": _safe_digest(left.get("output"))},
        "right": {"id": right["id"], "status": right["status"], "workflow_version": right.get("workflow_version", 1), "output_digest": _safe_digest(right.get("output"))},
        "changed_nodes": changes,
    }


async def _run_llm(model: str, config: dict[str, Any], messages: list[dict[str, str]], api_key: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{settings.gateway_url.rstrip('/')}/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": messages, "stream": False, **config},
        )
    if response.status_code >= 400:
        raise RuntimeError("workflow LLM node was rejected by the gateway")
    return response.json()


async def _run_code_in_sandbox(
    source: str,
    context: dict[str, Any],
    config: dict[str, Any],
    *,
    workspace_id: str | None,
    sandbox_session_id: str | None,
) -> Any:
    if not workspace_id or not sandbox_session_id:
        raise RuntimeError("workflow code node requires a managed sandbox context")
    errors = code_validation_errors(source, config)
    if errors:
        raise RuntimeError(errors[0])
    input_json = json_dumps(context.get("input", context))
    context_json = json_dumps(context)
    if len(input_json.encode("utf-8")) + len(context_json.encode("utf-8")) > CODE_MAX_INPUT_BYTES:
        raise RuntimeError("workflow code node input exceeds 128 KiB")
    headers = {"X-Internal-Token": settings.internal_token}
    sandbox_id: str | None = None
    timeout_seconds = int(config.get("timeout_seconds", CODE_DEFAULT_TIMEOUT_SECONDS))
    require_gvisor = settings.workama_env.lower() in {"staging", "preprod", "production"} or bool(config.get("require_gvisor", False))
    async with httpx.AsyncClient(timeout=max(30, timeout_seconds + 5), follow_redirects=False, trust_env=False) as client:
        try:
            acquired = await client.post(
                settings.sandbox_fleet_url.rstrip("/") + "/internal/sandboxes",
                headers=headers,
                json={
                    "workspace_id": workspace_id,
                    "session_id": sandbox_session_id,
                    "scope_type": "workflow",
                    "scope_id": sandbox_session_id,
                    "image": "sandbox-code",
                },
            )
            if acquired.status_code >= 400:
                raise RuntimeError("workflow code sandbox could not be acquired")
            sandbox = acquired.json()
            sandbox_id = str(sandbox.get("id") or "")
            if not sandbox_id:
                raise RuntimeError("workflow code sandbox returned no id")
            if require_gvisor and not bool(sandbox.get("gvisor_compliant")):
                raise RuntimeError("workflow code node requires a gVisor-compliant sandbox")
            wrapper = (
                "import json as _workama_json, sys as _workama_sys\n"
                "input = _workama_json.loads(_workama_sys.argv[1])\n"
                "context = _workama_json.loads(_workama_sys.argv[2])\n"
                f"{source}\n"
                "print(_workama_json.dumps(result, ensure_ascii=False, separators=(',', ':')))\n"
            )
            response = await client.post(
                f"{settings.sandbox_fleet_url.rstrip('/')}/internal/sandboxes/{sandbox_id}/exec",
                headers=headers,
                json={
                    "argv": ["python", "-I", "-S", "-c", wrapper, input_json, context_json],
                    "timeout_seconds": timeout_seconds,
                },
            )
            if response.status_code >= 400:
                raise RuntimeError("workflow code sandbox execution was rejected")
            execution = response.json()
            if int(execution.get("exit_code", 1)) != 0:
                raise RuntimeError("workflow code node execution failed")
            output_lines = [line.strip() for line in str(execution.get("output") or "").splitlines() if line.strip()]
            if not output_lines:
                raise RuntimeError("workflow code node did not produce a JSON result")
            try:
                return json.loads(output_lines[-1])
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError("workflow code node result was not valid JSON") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError("workflow code sandbox is unavailable") from exc
        finally:
            if sandbox_id:
                try:
                    await client.delete(
                        f"{settings.sandbox_fleet_url.rstrip('/')}/internal/sandboxes/{sandbox_id}",
                        headers=headers,
                    )
                except httpx.HTTPError:
                    pass


# ---------------------------------------------------------------------------
# 真实外部节点执行：变量插值 + HTTP / 本地 code 沙箱 / sub-workflow
# ---------------------------------------------------------------------------

# 本地代码沙箱允许的内置函数（屏蔽 __import__/open/eval/exec/compile 等危险调用）
_SAFE_BUILTINS: dict[str, Any] = {
    "abs": abs, "all": all, "any": any, "ascii": ascii, "bin": bin, "bool": bool,
    "bytearray": bytearray, "bytes": bytes, "callable": callable, "chr": chr,
    "complex": complex, "dict": dict, "divmod": divmod, "enumerate": enumerate,
    "filter": filter, "float": float, "format": format, "frozenset": frozenset,
    "hash": hash, "hex": hex, "int": int, "isinstance": isinstance,
    "issubclass": issubclass, "iter": iter, "len": len, "list": list, "map": map,
    "max": max, "min": min, "next": next, "oct": oct, "ord": ord, "pow": pow,
    "print": print, "range": range, "repr": repr, "reversed": reversed,
    "round": round, "set": set, "slice": slice, "sorted": sorted, "str": str,
    "sum": sum, "tuple": tuple, "type": type, "zip": zip,
    "True": True, "False": False, "None": None,
    "ValueError": ValueError, "TypeError": TypeError, "KeyError": KeyError,
    "IndexError": IndexError, "ZeroDivisionError": ZeroDivisionError,
    "Exception": Exception, "StopIteration": StopIteration,
}

# 沙箱中预注入的安全标准库模块（用户无需 import 即可使用）
_SAFE_MODULES: dict[str, Any] = {
    "json": json,
    "re": re,
}


def _resolve_ref(ref: Any, context: dict[str, Any]) -> Any:
    """从上下文解析变量引用。

    支持以下格式：
      - ``{{node_id.field}}`` 或 ``{{node_id.field.nested}}`` 双花括号引用
      - 裸路径 ``node_id.field`` 直接遍历
      - 系统变量 ``{{$input}}`` / ``{{$trigger}}`` / ``{{$context}}`` 及其子路径
        （如 ``{{$input.name}}`` 解析为 ``context["input"]["name"]``）
    未找到引用时返回 None。
    """
    ref_text = str(ref).strip()
    full_match = re.fullmatch(r"\{\{\s*(.+?)\s*\}\}", ref_text)
    path = full_match.group(1).strip() if full_match else ref_text
    parts = path.split(".")
    if not parts:
        return None
    # 系统变量前缀：$input / $trigger 映射到 context["input"]，$context 映射到 context 本身
    head = parts[0]
    if head in {"$input", "$trigger"}:
        current: Any = context.get("input")
        remaining = parts[1:]
    elif head == "$context":
        current = context
        remaining = parts[1:]
    else:
        current = context
        remaining = parts
    # 按点分路径遍历 dict / list
    for part in remaining:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if current is None:
            return None
    return current


def _interpolate(value: Any, context: dict[str, Any]) -> Any:
    """对值进行 ``{{node_id.field}}`` 变量插值。

    - 非字符串值原样返回。
    - 字符串恰好为单个引用时返回原生值（保留 dict/list/数字等类型）。
    - 否则做字符串替换，未找到的引用替换为空字符串。
    """
    if not isinstance(value, str):
        return value
    full_match = re.fullmatch(r"\{\{\s*(.+?)\s*\}\}", value)
    if full_match:
        return _resolve_ref(full_match.group(1), context)

    def repl(match: re.Match[str]) -> str:
        resolved = _resolve_ref(match.group(1), context)
        return "" if resolved is None else str(resolved)

    return _INTERPOLATE_RE.sub(repl, value)


def _interpolate_dict(mapping: Any, context: dict[str, Any]) -> dict[str, Any]:
    """对字典的键值进行 ``{{...}}`` 插值。非字典输入返回空字典。"""
    if not isinstance(mapping, dict):
        return {}
    result: dict[str, Any] = {}
    for key, value in mapping.items():
        # 值可能是嵌套 dict / list / str，递归插值
        if isinstance(value, dict):
            interpolated_value: Any = _interpolate_dict(value, context)
        elif isinstance(value, list):
            interpolated_value = _interpolate_list(value, context)
        else:
            interpolated_value = _interpolate(value, context)
        result[str(_interpolate(key, context))] = interpolated_value
    return result


def _interpolate_list(items: Any, context: dict[str, Any]) -> list[Any]:
    """对列表元素进行 ``{{...}}`` 插值。非列表输入返回空列表。"""
    if not isinstance(items, list):
        return []
    result: list[Any] = []
    for item in items:
        if isinstance(item, dict):
            result.append(_interpolate_dict(item, context))
        elif isinstance(item, list):
            result.append(_interpolate_list(item, context))
        else:
            result.append(_interpolate(item, context))
    return result


async def execute_http_node(node_config: dict, context: dict) -> dict:
    """执行 HTTP 请求节点。

    参数:
        node_config: 节点配置，包含 url / method / headers / body / params / timeout
        context: 工作流执行上下文，包含上游节点输出

    返回:
        ``{"status_code": int, "body": Any, "headers": dict}``

    异常:
        RuntimeError: URL 缺失、方法不支持、超时、连接失败或其他 HTTP 错误
    """
    url = _interpolate(str(node_config.get("url", "")), context)
    method = str(node_config.get("method", "GET")).upper()
    headers = _interpolate_dict(node_config.get("headers") or {}, context)
    params = _interpolate_dict(node_config.get("params") or {}, context)
    # body 可能是 dict / list / str / None，需要递归插值
    raw_body = node_config.get("body")
    if isinstance(raw_body, dict):
        body: Any = _interpolate_dict(raw_body, context)
    elif isinstance(raw_body, list):
        body = _interpolate_list(raw_body, context)
    else:
        body = _interpolate(raw_body, context)
    try:
        timeout = float(node_config.get("timeout", 30.0))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("http node timeout must be numeric") from exc

    if not url:
        raise RuntimeError("http node requires a url")
    if method not in HTTP_ALLOWED_METHODS:
        raise RuntimeError(f"http node method is unsupported: {method}")

    request_kwargs: dict[str, Any] = {}
    if headers:
        request_kwargs["headers"] = {str(k): str(v) for k, v in headers.items()}
    if params:
        request_kwargs["params"] = params
    if body is not None:
        request_kwargs["json"] = body

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.request(method, str(url), **request_kwargs)
    except httpx.TimeoutException as exc:
        raise RuntimeError(f"http node timed out after {timeout}s") from exc
    except httpx.ConnectError as exc:
        raise RuntimeError("http node connection failed") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError("http node request failed") from exc

    content_type = resp.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            parsed_body: Any = resp.json()
        except ValueError:
            parsed_body = resp.text
    else:
        parsed_body = resp.text

    return {
        "status_code": resp.status_code,
        "body": parsed_body,
        "headers": dict(resp.headers),
    }


async def execute_code_node(node_config: dict, context: dict) -> dict:
    """执行代码节点（本地安全沙箱）。

    使用受限的 exec 在线程中执行 Python 代码：
      - 复用 ``code_validation_errors`` 进行 AST 级别安全校验
      - 屏蔽 ``__builtins__``，仅暴露白名单函数与 json/re 模块
      - 通过 ``input_mapping`` 从上下文注入变量
      - 在独立线程中执行以支持超时中断

    返回:
        ``{"result": Any}``，其中 result 为代码中赋值的 ``result`` 变量
    """
    code = str(node_config.get("code", ""))
    input_mapping = node_config.get("input_mapping") or {}
    try:
        timeout = float(node_config.get("timeout", node_config.get("timeout_seconds", 5.0)))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("code node timeout must be numeric") from exc

    # 复用现有 AST 校验逻辑确保代码安全（timeout_seconds 限制在 1-120 之间）
    validation_config = {"timeout_seconds": max(1, min(120, int(timeout)))}
    errors = code_validation_errors(code, validation_config)
    if errors:
        raise RuntimeError(errors[0])

    # 准备沙箱全局命名空间
    sandbox_globals: dict[str, Any] = {
        "__builtins__": _SAFE_BUILTINS,
        "result": None,
    }
    sandbox_globals.update(_SAFE_MODULES)
    # 注入输入变量（从上下文解析引用）
    for var_name, var_ref in input_mapping.items():
        sandbox_globals[str(var_name)] = _resolve_ref(var_ref, context)

    # 解析 AST（校验已通过，这里不会抛 SyntaxError）
    tree = ast.parse(code, mode="exec")

    # 在线程中执行以支持超时控制（exec 本身是同步阻塞的）
    def _run() -> None:
        exec(compile(tree, "<workflow_code>", "exec"), sandbox_globals)

    try:
        await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"code node execution timed out after {timeout}s") from exc
    except Exception as exc:
        raise RuntimeError(f"code node execution failed: {exc}") from exc

    return {"result": sandbox_globals.get("result")}


async def _default_subworkflow_runner(
    workflow_id: str,
    input_data: dict[str, Any],
    timeout: float,
    context: dict[str, Any],
) -> dict[str, Any]:
    """默认子工作流执行器：从数据库加载图并递归调用 ``execute_graph``。"""
    workspace_id = context.get("_workspace_id")
    if not workspace_id:
        raise RuntimeError("sub_workflow node requires a workspace context")
    async with pool.connection() as conn:
        row = await _workflow(conn, workflow_id, workspace_id)
        graph = row["graph"]
    call_stack = list(context.get("_call_stack", []))
    call_stack.append(workflow_id)
    try:
        status, output, _trace, error = await asyncio.wait_for(
            execute_graph(
                graph,
                input_data,
                None,
                False,
                workspace_id=workspace_id,
                sandbox_session_id=context.get("_sandbox_session_id"),
                call_stack=call_stack,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"sub_workflow node timed out after {timeout}s") from exc
    return {"status": status, "output": output, "error": error}


async def execute_subworkflow_node(node_config: dict, context: dict) -> dict:
    """执行子工作流节点。

    参数:
        node_config: 节点配置，包含 workflow_id / input / timeout
        context: 工作流执行上下文

    返回:
        ``{"status": str, "output": Any, "error": str | None}``

    备注:
        测试时可通过 ``context["_subworkflow_runner"]`` 注入自定义执行器，
        签名为 ``async def runner(workflow_id, input_data, timeout) -> dict``。
    """
    sub_workflow_id = _interpolate(str(node_config.get("workflow_id", "")), context)
    if not sub_workflow_id:
        raise RuntimeError("sub_workflow node requires a workflow_id")

    call_stack = context.get("_call_stack", [])
    if sub_workflow_id in call_stack:
        raise RuntimeError("Circular workflow call detected")
    if len(call_stack) >= MAX_NESTING_DEPTH:
        raise RuntimeError("Maximum workflow nesting depth exceeded")

    input_data = _interpolate_dict(node_config.get("input") or {}, context)
    try:
        timeout = float(node_config.get("timeout", 300.0))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("sub_workflow node timeout must be numeric") from exc

    # 测试可通过 context 注入 runner 绕过数据库依赖
    runner = context.get("_subworkflow_runner") if isinstance(context, dict) else None
    if runner is not None:
        return await runner(sub_workflow_id, input_data, timeout)
    return await _default_subworkflow_runner(sub_workflow_id, input_data, timeout, context)


async def execute_graph(
    graph: dict[str, Any],
    input_value: dict[str, Any],
    gateway_api_key: str | None,
    dry_run: bool,
    event_sink: list[dict[str, Any]] | Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    cancellation_check: Callable[[], Awaitable[bool]] | None = None,
    *,
    workspace_id: str | None = None,
    sandbox_session_id: str | None = None,
    call_stack: list[str] | None = None,
) -> tuple[str, dict[str, Any], list[dict[str, Any]], str | None]:
    async def emit(event_type: str, payload: dict[str, Any]) -> None:
        if event_sink is not None:
            if isinstance(event_sink, list):
                event_sink.append({"event_type": event_type, "payload": payload})
            else:
                await event_sink(event_type, payload)

    async def is_cancelled() -> bool:
        return bool(cancellation_check and await cancellation_check())

    context: dict[str, Any] = {"input": input_value}
    # 暴露 workspace/sandbox 上下文供真实外部节点（如 sub_workflow）使用
    if workspace_id:
        context["_workspace_id"] = workspace_id
    if sandbox_session_id:
        context["_sandbox_session_id"] = sandbox_session_id
    context["_call_stack"] = list(call_stack) if call_stack else []
    trace: list[dict[str, Any]] = []
    final_output: Any = context
    for node in topological_order(graph):
        if await is_cancelled():
            return "cancelled", {"context": context}, trace, "Workflow run was cancelled."
        node_id = _node_id(node)
        node_type = canonical_node_type(node.get("type"))
        config = node.get("config") or {}
        node_started = datetime.now(UTC)
        await emit("workflow.node.started", {"node_id": node_id, "node_type": node_type, "started_at": node_started.isoformat()})
        try:
            if node_type == "input":
                value = input_value
            elif node_type == "prompt":
                value = render_template(str(config.get("template", "")), context)
            elif node_type == "transform":
                key = str(config.get("key", "value"))
                value = render_template(str(config.get("value", "")), context)
                context[key] = value
            elif node_type == "condition":
                actual = context.get(str(config.get("key", "")))
                value = actual == config.get("equals") if "equals" in config else bool(actual)
            elif node_type == "knowledge_retrieval":
                value = {"dataset_id": config.get("dataset_id"), "query": render_template(str(config.get("query", "")), context), "items": []}
            elif node_type == "approval":
                timeout_seconds = int(config.get("timeout_seconds", APPROVAL_DEFAULT_TIMEOUT_SECONDS))
                timeout_at = (datetime.now(UTC) + __import__("datetime").timedelta(seconds=timeout_seconds)).isoformat()
                approval_action = context.get("_approval_action") or context.get("input", {}).get("_approval_action")
                fallback_branch = config.get("fallback_branch")
                if approval_action == "timeout" and fallback_branch:
                    value = {
                        "timed_out": True,
                        "fallback_branch": fallback_branch,
                        "reason": config.get("reason", "Workflow approval required"),
                    }
                    context[node_id] = value
                    node_ended = datetime.now(UTC)
                    duration_ms = int((node_ended - node_started).total_seconds() * 1000)
                    trace.append({"node_id": node_id, "type": node_type, "status": "timed_out", "output_digest": _safe_digest(value), "started_at": node_started.isoformat(), "ended_at": node_ended.isoformat(), "duration_ms": duration_ms})
                    await emit("workflow.node.timed_out", {"node_id": node_id, "node_type": node_type, "output_digest": _safe_digest(value), "started_at": node_started.isoformat(), "ended_at": node_ended.isoformat(), "duration_ms": duration_ms})
                    continue
                else:
                    value = {"approval_required": True, "reason": config.get("reason", "Workflow approval required"), "timeout_seconds": timeout_seconds, "timeout_at": timeout_at}
                    trace.append({"node_id": node_id, "type": node_type, "status": "pending_approval", "output_digest": _safe_digest(value), "timeout_at": timeout_at})
                    await emit("workflow.node.pending_approval", {"node_id": node_id, "node_type": node_type, "output_digest": _safe_digest(value), "timeout_at": timeout_at})
                    return "pending_approval", {"context": context, "approval": value}, trace, None
            elif node_type == "llm":
                if dry_run:
                    value = {"dry_run": True, "model": config.get("model", "workama-chat"), "messages": config.get("messages", [])}
                elif not gateway_api_key:
                    raise RuntimeError("gateway_api_key is required for an llm node")
                else:
                    prompt = render_template(str(config.get("prompt", "{input}")), context)
                    value = await _run_llm(str(config.get("model", "workama-chat")), config.get("model_config") or {}, [{"role": "user", "content": prompt}], gateway_api_key)
            elif node_type == "code":
                source = str(config.get("code", ""))
                if dry_run:
                    value = {
                        "dry_run": True,
                        "language": "python",
                        "code_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                        "input_keys": sorted(context),
                    }
                elif config.get("sandbox") == "local":
                    # 本地安全沙箱执行（无需外部 sandbox-fleet 服务）
                    value = await execute_code_node(config, context)
                else:
                    value = await _run_code_in_sandbox(
                        source,
                        context,
                        config,
                        workspace_id=workspace_id,
                        sandbox_session_id=sandbox_session_id,
                    )
            elif node_type == "http_request":
                endpoint = str(config.get("url", ""))
                parsed = urlsplit(endpoint)
                if parsed.scheme != "mock" or not parsed.netloc:
                    raise RuntimeError("http_request only permits mock:// endpoints in this build")
                value = {
                    "status_code": 200,
                    "url": endpoint,
                    "json": config.get("response") if isinstance(config.get("response"), dict) else {},
                    "dry_run": dry_run,
                }
            elif node_type == "http":
                # 真实 HTTP 请求节点：调用外部 URL 并返回状态码/响应体/响应头
                if dry_run:
                    value = {
                        "dry_run": True,
                        "url": config.get("url"),
                        "method": str(config.get("method", "GET")).upper(),
                    }
                else:
                    value = await execute_http_node(config, context)
            elif node_type == "sub_workflow":
                # 子工作流调用节点：递归执行另一个工作流并等待完成
                sub_workflow_id = _interpolate(str(config.get("workflow_id", "")), context)
                call_stack = context.get("_call_stack", [])
                if sub_workflow_id in call_stack:
                    raise RuntimeError("Circular workflow call detected")
                if len(call_stack) >= MAX_NESTING_DEPTH:
                    raise RuntimeError("Maximum workflow nesting depth exceeded")
                if dry_run:
                    if not sub_workflow_id:
                        raise RuntimeError("sub_workflow node requires a workflow_id")
                    value = {
                        "dry_run": True,
                        "workflow_id": config.get("workflow_id"),
                    }
                else:
                    value = await execute_subworkflow_node(config, context)
            elif node_type == "loop":
                items = context_value(str(config.get("items_from", "input.items")), context)
                if not isinstance(items, list):
                    raise RuntimeError("loop items_from must resolve to a list")
                max_iterations = min(int(config.get("max_iterations", 100)), 100)
                if max_iterations < 1:
                    raise RuntimeError("loop max_iterations must be positive")
                if len(items) > MAX_LOOP_ITEMS:
                    raise RuntimeError(f"loop limit exceeded: {len(items)} items exceeds maximum of {MAX_LOOP_ITEMS}")
                limit = min(len(items), max_iterations)
                value = {"items": items[:limit], "count": limit, "truncated": len(items) > limit}
            elif node_type == "intent_classification":
                text = render_template(str(config.get("text", "{input}")), context).lower()
                labels = config.get("labels") if isinstance(config.get("labels"), dict) else {}
                label = str(config.get("default", "unknown"))
                for candidate, keywords in labels.items():
                    words = keywords if isinstance(keywords, list) else [keywords]
                    if any(str(word).lower() in text for word in words):
                        label = str(candidate)
                        break
                value = {"label": label, "confidence": 1.0 if label != str(config.get("default", "unknown")) else 0.0}
            elif node_type == "variable_aggregate":
                fields = config.get("fields") if isinstance(config.get("fields"), list) else []
                if len(fields) > 50:
                    raise RuntimeError("variable_aggregate supports at most 50 fields")
                value = {str(field): context_value(str(field), context) for field in fields if str(field)}
            elif node_type == "output":
                value = context.get(str(config.get("from", "input")), context)
                final_output = value
            else:
                raise RuntimeError(f"unsupported workflow node type: {node_type}")
            context[node_id] = value
            node_ended = datetime.now(UTC)
            duration_ms = int((node_ended - node_started).total_seconds() * 1000)
            output_size = len(json_dumps(value))
            trace.append({"node_id": node_id, "type": node_type, "status": "succeeded", "output_digest": _safe_digest(value), "started_at": node_started.isoformat(), "ended_at": node_ended.isoformat(), "duration_ms": duration_ms, "output_size": output_size})
            await emit("workflow.node.succeeded", {"node_id": node_id, "node_type": node_type, "output_digest": _safe_digest(value), "started_at": node_started.isoformat(), "ended_at": node_ended.isoformat(), "duration_ms": duration_ms, "output_size": output_size})
        except Exception as exc:
            node_ended = datetime.now(UTC)
            duration_ms = int((node_ended - node_started).total_seconds() * 1000)
            error_category = _classify_error(exc)
            trace.append({"node_id": node_id, "type": node_type, "status": "failed", "error": str(exc)[:300], "error_category": error_category, "started_at": node_started.isoformat(), "ended_at": node_ended.isoformat(), "duration_ms": duration_ms})
            await emit("workflow.node.failed", {"node_id": node_id, "node_type": node_type, "error": str(exc)[:300], "error_category": error_category, "started_at": node_started.isoformat(), "ended_at": node_ended.isoformat(), "duration_ms": duration_ms})
            return "failed", {"context": context}, trace, str(exc)[:500]
        if await is_cancelled():
            return "cancelled", {"context": context}, trace, "Workflow run was cancelled."
    return "succeeded", {"context": context, "output": final_output}, trace, None


def _workflow_run_response(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workflow_id": row["workflow_id"],
        "operation_id": row.get("operation_id"),
        "status": row["status"],
        "output": row.get("output") or {},
        "trace": row.get("trace") or [],
        "error": row.get("error"),
        "created_at": row.get("created_at"),
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
    }


@router.post("/workflows/{workflow_id}/runs", status_code=status.HTTP_202_ACCEPTED)
async def run_workflow(
    workflow_id: str,
    body: WorkflowRunCreate,
    actor: Annotated[Actor, Depends(get_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=200)] = None,
):
    _require(actor, "workflow", "write")
    request_key = idempotency_key or new_id("workflow-idem")
    async with pool.connection() as conn:
        workflow = await _workflow(conn, workflow_id, actor.workspace_id)
        errors = validate_graph(workflow["graph"])
        if errors:
            raise HTTPException(status_code=422, detail={"code": "E05001", "errors": errors})
        run_id = new_id("wrun")
        stable_input_hash = canonical_hash({
            "workflow_id": workflow_id,
            "input": body.input,
            "dry_run": body.dry_run,
            "gateway_api_key": hash_secret(body.gateway_api_key) if body.gateway_api_key else None,
        })

        # 嵌套深度检查
        nesting_depth = 0
        parent_run_id = body.input.get("_parent_run_id")
        if parent_run_id:
            parent_result = await conn.execute(
                "SELECT nesting_depth FROM pf_workflow_run WHERE id=%s AND workspace_id=%s",
                (parent_run_id, actor.workspace_id),
            )
            parent_row = await parent_result.fetchone()
            if parent_row:
                nesting_depth = (parent_row.get("nesting_depth") or 0) + 1

        if nesting_depth > MAX_NESTING_DEPTH:
            await conn.execute(
                """
                INSERT INTO pf_workflow_run(id,workflow_id,org_id,workspace_id,created_by,input,workflow_version,status,error,error_category,nesting_depth,completed_at)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,'failed','Maximum workflow nesting depth exceeded','nesting_exceeded',%s,now())
                """,
                (run_id, workflow_id, actor.org_id, actor.workspace_id, actor.user_id, json_dumps(body.input), workflow["version"], nesting_depth),
            )
            await append_workflow_event(
                conn,
                run_id=run_id,
                workflow_id=workflow_id,
                workspace_id=actor.workspace_id,
                event_type="workflow.run.failed",
                payload={"run_id": run_id, "workflow_id": workflow_id, "status": "failed", "error": "Maximum workflow nesting depth exceeded", "error_category": "nesting_exceeded"},
            )
            await conn.commit()
            result = await conn.execute("SELECT * FROM pf_workflow_run WHERE id=%s AND workspace_id=%s", (run_id, actor.workspace_id))
            return _workflow_run_response(await result.fetchone())

        payload = {
            "run_id": run_id,
            "workflow_id": workflow_id,
            "input": body.input,
            "dry_run": body.dry_run,
            "gateway_api_key_enc": encrypt_secret(body.gateway_api_key),
        }
        try:
            operation = await submit_operation(
                conn,
                operation_type="workflow.run.execute",
                workspace_id=actor.workspace_id,
                org_id=actor.org_id,
                actor_id=actor.user_id,
                actor_role=actor.role,
                idempotency_key=request_key,
                payload=payload,
                input_hash_override=stable_input_hash,
                job_type="workflow.run.execute",
                queue="workflow",
                max_attempts=1,
                priority=120,
                cancellable=True,
            )
        except Exception as exc:
            if "idempotency" in str(exc).lower() or "unique" in str(exc).lower():
                raise HTTPException(status_code=409, detail="Idempotency key was already used with different input") from exc
            raise
        job_result = await conn.execute("SELECT payload FROM ops_job WHERE operation_id=%s", (operation["id"],))
        job = await job_result.fetchone()
        if job and isinstance(job.get("payload"), dict) and job["payload"].get("run_id"):
            run_id = str(job["payload"]["run_id"])
        existing_result = await conn.execute(
            "SELECT * FROM pf_workflow_run WHERE operation_id=%s AND workspace_id=%s",
            (operation["id"], actor.workspace_id),
        )
        existing = await existing_result.fetchone()
        if existing:
            if operation.get("input_hash") != stable_input_hash:
                raise HTTPException(status_code=409, detail="Idempotency key was already used with different input")
            return _workflow_run_response(existing)
        if operation.get("status") == "cancelled":
            await conn.execute(
                """
                INSERT INTO pf_workflow_run(id,workflow_id,org_id,workspace_id,created_by,input,workflow_version,operation_id,status,error,error_category,nesting_depth,completed_at)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,'cancelled','Workflow run was cancelled before execution.','cancelled',%s,now())
                """,
                (run_id, workflow_id, actor.org_id, actor.workspace_id, actor.user_id, json_dumps(body.input), workflow["version"], operation["id"], nesting_depth),
            )
            await append_workflow_event(
                conn,
                run_id=run_id,
                workflow_id=workflow_id,
                workspace_id=actor.workspace_id,
                event_type="workflow.run.cancelled",
                payload={"run_id": run_id, "workflow_id": workflow_id, "status": "cancelled", "error": "Workflow run was cancelled before execution."},
            )
            await conn.commit()
            result = await conn.execute("SELECT * FROM pf_workflow_run WHERE id=%s AND workspace_id=%s", (run_id, actor.workspace_id))
            return _workflow_run_response(await result.fetchone())
        await conn.execute(
            """
            INSERT INTO pf_workflow_run(id,workflow_id,org_id,workspace_id,created_by,input,workflow_version,operation_id,status,nesting_depth)
            VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,'queued',%s)
            """,
            (run_id, workflow_id, actor.org_id, actor.workspace_id, actor.user_id, json_dumps(body.input), workflow["version"], operation["id"], nesting_depth),
        )
        await conn.commit()
        result = await conn.execute("SELECT * FROM pf_workflow_run WHERE id=%s AND workspace_id=%s", (run_id, actor.workspace_id))
        row = await result.fetchone()
    return _workflow_run_response(row)


@router.get("/workflows/{workflow_id}/runs")
async def list_workflow_runs(workflow_id: str, actor: Annotated[Actor, Depends(get_actor)], limit: int = Query(default=50, ge=1, le=200)):
    _require(actor, "workflow", "read")
    async with pool.connection() as conn:
        await _workflow(conn, workflow_id, actor.workspace_id)
        result = await conn.execute("SELECT id,workflow_id,status,error,created_at,started_at,completed_at FROM pf_workflow_run WHERE workflow_id=%s AND workspace_id=%s ORDER BY created_at DESC LIMIT %s", (workflow_id, actor.workspace_id, limit))
        rows = await result.fetchall()
    return {"items": rows, "data": rows, "next_cursor": None, "has_more": False, "meta": {"request_id": None}}


@router.get("/workflow-runs/{run_id}")
async def get_workflow_run(run_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "workflow", "read")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM pf_workflow_run WHERE id=%s AND workspace_id=%s", (run_id, actor.workspace_id))
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    return row


async def _run_events(
    run_id: str,
    actor: Actor,
    after: int,
    limit: int,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    _require(actor, "workflow", "read")
    async with pool.connection() as conn:
        query = """
            SELECT e.id, e.run_id, e.workflow_id, e.seq, e.event_type, e.payload, e.created_at
            FROM pf_workflow_event e
            JOIN pf_workflow_run r ON r.id=e.run_id AND r.workspace_id=e.workspace_id
            WHERE e.run_id=%s AND e.workspace_id=%s AND e.seq>%s
        """
        params: list[Any] = [run_id, actor.workspace_id, after]
        if workflow_id is not None:
            query += " AND e.workflow_id=%s"
            params.append(workflow_id)
        query += " ORDER BY e.seq ASC LIMIT %s"
        params.append(limit + 1)
        result = await conn.execute(query, tuple(params))
        rows = await result.fetchall()
    has_more = len(rows) > limit
    items = rows[:limit]
    return {
        "items": items,
        "data": items,
        "next_cursor": items[-1]["seq"] if items else None,
        "next_after": items[-1]["seq"] if items else after,
        "has_more": has_more,
        "meta": {"request_id": None},
    }


@router.get("/workflow-runs/{run_id}/events")
async def list_workflow_run_events(
    run_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    return await _run_events(run_id, actor, after, limit)


@router.get("/workflows/{workflow_id}/runs/{run_id}/events")
async def list_workflow_events_for_workflow(
    workflow_id: str,
    run_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    return await _run_events(run_id, actor, after, limit, workflow_id)


class ApprovalOverride(BaseModel):
    action: Literal["approve", "reject", "timeout_override"] = "timeout_override"
    timeout_seconds: int | None = None


@router.post("/workflow-runs/{run_id}/approval-override")
async def override_approval_timeout(
    run_id: str,
    body: ApprovalOverride,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require(actor, "workflow", "write")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM pf_workflow_run WHERE id=%s AND workspace_id=%s",
            (run_id, actor.workspace_id),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Workflow run not found")
        if row["status"] != "pending_approval":
            raise HTTPException(status_code=409, detail="Workflow run is not pending approval")
        payload: dict[str, Any] = {"run_id": run_id, "action": body.action}
        if body.timeout_seconds is not None:
            payload["timeout_seconds"] = body.timeout_seconds
        await append_workflow_event(
            conn,
            run_id=run_id,
            workflow_id=row["workflow_id"],
            workspace_id=actor.workspace_id,
            event_type="workflow.approval.override",
            payload=payload,
        )
        await conn.commit()
    return {"run_id": run_id, "action": body.action, "status": "overridden"}


def _sse_event(row: dict[str, Any]) -> str:
    payload = {
        "id": row["id"],
        "run_id": row["run_id"],
        "workflow_id": row["workflow_id"],
        "seq": row["seq"],
        "event_type": row["event_type"],
        "payload": row["payload"],
        "created_at": row["created_at"].isoformat() if hasattr(row.get("created_at"), "isoformat") else row.get("created_at"),
    }
    return f"id: {row['seq']}\nevent: {row['event_type']}\ndata: {json_dumps(payload)}\n\n"


@router.get("/workflow-runs/{run_id}/events/stream")
async def stream_workflow_run_events(
    run_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    after: int = Query(default=0, ge=0),
    timeout_seconds: int = Query(default=60, ge=1, le=120),
):
    _require(actor, "workflow", "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT id FROM pf_workflow_run WHERE id=%s AND workspace_id=%s",
            (run_id, actor.workspace_id),
        )
        if not await result.fetchone():
            raise HTTPException(status_code=404, detail="Workflow run not found")

    async def event_stream():
        cursor = after
        deadline = monotonic() + timeout_seconds
        terminal_types = {
            "workflow.run.completed",
            "workflow.run.failed",
            "workflow.run.cancelled",
            "workflow.run.pending_approval",
        }
        while monotonic() < deadline:
            page = await _run_events(run_id, actor, cursor, 100)
            if page["items"]:
                for row in page["items"]:
                    cursor = row["seq"]
                    yield _sse_event(row)
                    if row["event_type"] in terminal_types:
                        return
                continue
            yield ": workama-heartbeat\n\n"
            await asyncio.sleep(0.5)
        yield "event: workflow.stream.timeout\ndata: {\"run_id\": " + json_dumps(run_id) + "}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
