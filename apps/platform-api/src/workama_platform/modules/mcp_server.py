"""MCP 服务端模块 (mcp_server)。

v7.147: P1 身份/审查/MCP 模块 - MCP 工具注册/清单/调用。

提供：
- 7 个 REST 端点（注册 / 列表 / 详情 / 注销 / 调用 / schema / manifest）
- 内置工具：``get_current_time`` / ``echo`` / ``get_workspace_info``（启动时自动注册）
- MCP 协议兼容的 ``list_tools`` 响应格式（``/manifest``）
- 通过 ``importlib`` + ``getattr`` 动态解析 handler 并调用
- workspace 隔离：内置工具在 ``workspace_id='system'`` 下，对所有 workspace 可见可调用

设计文档：910-进度追踪与任务清单.md「身份/审查/MCP」；500 LLM 网关 / 520 Agent 引擎
"""
from __future__ import annotations

import importlib
import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

import hashlib
import json

from workama_platform.core import (
    Actor,
    _cache_key,
    cache_delete,
    cache_delete_pattern,
    cache_get,
    cache_set,
    capability_allows,
    get_actor,
    json_dumps,
    new_id,
    pool,
)

# ============================================================================
# 常量
# ============================================================================

LOGGER = logging.getLogger("workama.platform-api.mcp_server")

router = APIRouter(prefix="/api/v1/mcp", tags=["mcp"])

# 内置工具所在的 system workspace（与 internal_channel 一致）
INTERNAL_WORKSPACE_ID = "system"

MCPToolKind = Literal["function", "resource", "prompt"]
MCPToolStatus = Literal["active", "disabled"]

_MANAGEMENT_ROLES: frozenset[str] = frozenset({"owner", "admin"})
_READ_ROLES: frozenset[str] = frozenset({"owner", "admin", "member", "viewer"})
_VALID_KINDS: frozenset[str] = frozenset({"function", "resource", "prompt"})
_VALID_STATUSES: frozenset[str] = frozenset({"active", "disabled"})

# 内置工具 handler 模块路径（本模块）
_BUILTIN_HANDLER_MODULE = "workama_platform.modules.mcp_server"


# ============================================================================
# Pydantic 模型
# ============================================================================


class MCPTool(BaseModel):
    """MCP 工具的完整表示。"""

    id: str
    workspace_id: str
    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    kind: str = "function"
    handler: str | None = None
    status: str = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class MCPToolCreate(BaseModel):
    """POST /api/v1/mcp/tools 请求体。"""

    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    kind: MCPToolKind = "function"
    handler: str | None = Field(default=None, max_length=512)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MCPToolCall(BaseModel):
    """POST /api/v1/mcp/tools/{tool_id}/invoke 请求体。"""

    arguments: dict[str, Any] = Field(default_factory=dict)


class MCPToolResponse(BaseModel):
    """POST /api/v1/mcp/tools/{tool_id}/invoke 响应体。"""

    tool_id: str
    tool_name: str
    result: Any
    is_error: bool = False


class MCPResource(BaseModel):
    """MCP 资源（保留扩展用）。"""

    uri: str
    name: str
    description: str | None = None
    mime_type: str | None = None


class MCPPrompt(BaseModel):
    """MCP Prompt（保留扩展用）。"""

    name: str
    description: str | None = None
    arguments: list[dict[str, Any]] = Field(default_factory=list)


# ============================================================================
# 辅助函数
# ============================================================================


def _require_read(actor: Actor) -> None:
    if actor.role in _READ_ROLES:
        return
    if any(
        capability_allows(actor.capabilities, cap)
        for cap in ("mcp:read", "mcp:*", "*")
    ):
        return
    raise HTTPException(status_code=403, detail="Missing capability: mcp:read")


def _require_manage(actor: Actor) -> None:
    if actor.role in _MANAGEMENT_ROLES:
        return
    if any(
        capability_allows(actor.capabilities, cap)
        for cap in ("mcp:write", "mcp:*", "*")
    ):
        return
    raise HTTPException(status_code=403, detail="Missing capability: mcp:write")


def _summary(row: dict) -> dict:
    """将数据库行转为 API 响应 dict。"""
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "name": row["name"],
        "description": row.get("description"),
        "input_schema": row.get("input_schema") or {},
        "output_schema": row.get("output_schema"),
        "kind": row["kind"],
        "handler": row.get("handler"),
        "status": row["status"],
        "metadata": row.get("metadata") or {},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _visible_workspaces(actor: Actor) -> tuple[str, str]:
    """返回 (system, actor.workspace_id) —— 内置工具 + 当前 workspace。"""
    return (INTERNAL_WORKSPACE_ID, actor.workspace_id)


async def _owned_tool(conn: Any, tool_id: str, actor: Actor) -> dict:
    """查询工具并校验可见性。

    - 不存在 → 404
    - 存在但 workspace 既非 system 也非 actor.workspace_id → 403
    """
    result = await conn.execute(
        "SELECT * FROM mcp_tool WHERE id = %s",
        (tool_id,),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="MCP tool not found")
    if row["workspace_id"] not in _visible_workspaces(actor):
        raise HTTPException(
            status_code=403, detail="MCP tool belongs to another workspace"
        )
    return row


def _resolve_handler(handler_path: str | None) -> Callable[..., Any] | None:
    """通过 ``importlib`` + ``getattr`` 动态解析 handler 函数，找不到返回 None。"""
    if not handler_path or "." not in handler_path:
        return None
    module_path, _, func_name = handler_path.rpartition(".")
    try:
        module = importlib.import_module(module_path)
    except ImportError:
        return None
    return getattr(module, func_name, None)


def _mcp_manifest_entry(row: dict) -> dict[str, Any]:
    """转 MCP 协议 ``list_tools`` 响应条目。"""
    return {
        "name": row["name"],
        "description": row.get("description") or "",
        "inputSchema": row.get("input_schema") or {},
    }


# ============================================================================
# 内置工具 handler（启动时自动注册）
# ============================================================================


def builtin_get_current_time(arguments: dict[str, Any], actor: Actor) -> dict[str, Any]:
    """返回当前 UTC 时间（ISO 8601）。"""
    now = datetime.now(UTC)
    return {
        "iso": now.isoformat(),
        "epoch": int(now.timestamp()),
        "timezone": "UTC",
    }


def builtin_echo(arguments: dict[str, Any], actor: Actor) -> dict[str, Any]:
    """回显参数。"""
    return {"echo": arguments, "received_at": datetime.now(UTC).isoformat()}


def builtin_get_workspace_info(
    arguments: dict[str, Any], actor: Actor
) -> dict[str, Any]:
    """返回当前调用者的 workspace 信息。"""
    return {
        "workspace_id": actor.workspace_id,
        "org_id": actor.org_id,
        "user_id": actor.user_id,
        "role": actor.role,
        "email": actor.email,
    }


# 内置工具定义（name -> (description, input_schema, handler dotted path)）
BUILTIN_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "get_current_time",
        "description": "Return the current UTC time in ISO 8601 format.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "handler": f"{_BUILTIN_HANDLER_MODULE}.builtin_get_current_time",
    },
    {
        "name": "echo",
        "description": "Echo back the provided arguments.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
            },
        },
        "handler": f"{_BUILTIN_HANDLER_MODULE}.builtin_echo",
    },
    {
        "name": "get_workspace_info",
        "description": "Return the caller's workspace/org/user information.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "handler": f"{_BUILTIN_HANDLER_MODULE}.builtin_get_workspace_info",
    },
)


async def ensure_builtin_mcp_tools() -> None:
    """启动时幂等注册内置 MCP 工具到 ``workspace_id='system'``。

    任何异常只记 warning，不抛出 —— 内置工具是可选的，不能阻断 platform-api 启动。
    """
    try:
        async with pool.connection() as conn:
            async with conn.transaction():
                for spec in BUILTIN_TOOLS:
                    existing = await conn.execute(
                        """
                        SELECT id FROM mcp_tool
                        WHERE workspace_id = %s AND name = %s
                        LIMIT 1
                        """,
                        (INTERNAL_WORKSPACE_ID, spec["name"]),
                    )
                    if await existing.fetchone():
                        continue
                    await conn.execute(
                        """
                        INSERT INTO mcp_tool(
                            id, workspace_id, name, description, input_schema,
                            kind, handler, status, metadata)
                        VALUES (%s, %s, %s, %s, %s::jsonb, 'function', %s, 'active', '{}'::jsonb)
                        """,
                        (
                            new_id("mcp"),
                            INTERNAL_WORKSPACE_ID,
                            spec["name"],
                            spec["description"],
                            json_dumps(spec["input_schema"]),
                            spec["handler"],
                        ),
                    )
    except Exception as exc:  # noqa: BLE001 — 启动钩子不能因可选工具失败而中断
        LOGGER.warning("ensure_builtin_mcp_tools failed: %s", exc)
        return
    LOGGER.info(
        "ensure_builtin_mcp_tools: ensured %d builtin tools.", len(BUILTIN_TOOLS)
    )


# ============================================================================
# Router 端点
# ============================================================================
#
# 路由顺序：固定路径（/tools, /manifest）在 /tools/{tool_id} 之前声明。


@router.post("/tools", status_code=status.HTTP_201_CREATED)
async def register_tool(
    body: MCPToolCreate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """注册 MCP 工具。"""
    _require_manage(actor)
    tool_id = new_id("mcp")
    async with pool.connection() as conn:
        async with conn.transaction():
            duplicate = await conn.execute(
                "SELECT 1 FROM mcp_tool WHERE workspace_id = %s AND name = %s",
                (actor.workspace_id, body.name),
            )
            if await duplicate.fetchone():
                raise HTTPException(
                    status_code=409,
                    detail="MCP tool name already exists in this workspace",
                )
            result = await conn.execute(
                """
                INSERT INTO mcp_tool(
                    id, workspace_id, name, description, input_schema,
                    output_schema, kind, handler, status, metadata)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, 'active', %s::jsonb)
                RETURNING *
                """,
                (
                    tool_id,
                    actor.workspace_id,
                    body.name,
                    body.description,
                    json_dumps(body.input_schema),
                    json_dumps(body.output_schema) if body.output_schema is not None else None,
                    body.kind,
                    body.handler,
                    json_dumps(body.metadata),
                ),
            )
            row = await result.fetchone()
    await cache_delete_pattern(f"workama:cache:*:mcp_manifest:*")
    return _summary(row)


@router.get("/tools")
async def list_tools(
    actor: Annotated[Actor, Depends(get_actor)],
    kind: MCPToolKind | None = Query(default=None),
    status_filter: MCPToolStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """列出工具：返回当前 workspace + system 内置工具。"""
    _require_read(actor)
    clauses = ["workspace_id = ANY(%s)"]
    params: list[Any] = [list(_visible_workspaces(actor))]
    if kind:
        clauses.append("kind = %s")
        params.append(kind)
    if status_filter:
        clauses.append("status = %s")
        params.append(status_filter)
    params.append(limit)
    params.append(offset)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT * FROM mcp_tool
            WHERE {' AND '.join(clauses)}
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


@router.get("/manifest")
async def get_manifest(
    actor: Annotated[Actor, Depends(get_actor)],
):
    """获取 MCP 服务清单（所有 active 工具，符合 MCP 协议 ``list_tools`` 响应格式）。"""
    _require_read(actor)
    visible = sorted(_visible_workspaces(actor))
    ws_hash = hashlib.sha256(",".join(visible).encode()).hexdigest()[:16]
    cache_key = _cache_key(actor.workspace_id, "mcp_manifest", ws_hash)
    cached = await cache_get(cache_key)
    if cached:
        return json.loads(cached)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT name, description, input_schema FROM mcp_tool
            WHERE workspace_id = ANY(%s) AND status = 'active'
            ORDER BY created_at ASC
            """,
            (visible,),
        )
        rows = await result.fetchall()
    response = {
        "tools": [_mcp_manifest_entry(row) for row in rows],
        "protocolVersion": "2025-06-18",
    }
    await cache_set(cache_key, json.dumps(response))
    return response


@router.get("/tools/{tool_id}")
async def get_tool(
    tool_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """工具详情。"""
    _require_read(actor)
    async with pool.connection() as conn:
        return _summary(await _owned_tool(conn, tool_id, actor))


@router.get("/tools/{tool_id}/schema")
async def get_tool_schema(
    tool_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """获取工具的 JSON Schema（MCP 协议兼容）。"""
    _require_read(actor)
    async with pool.connection() as conn:
        row = await _owned_tool(conn, tool_id, actor)
    return {
        "name": row["name"],
        "description": row.get("description") or "",
        "inputSchema": row.get("input_schema") or {},
        "outputSchema": row.get("output_schema"),
        "kind": row["kind"],
    }


@router.post("/tools/{tool_id}/invoke")
async def invoke_tool(
    tool_id: str,
    body: MCPToolCall,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """调用工具：通过 ``getattr`` 动态查找 handler 函数，找不到返回 404。"""
    _require_read(actor)
    async with pool.connection() as conn:
        row = await _owned_tool(conn, tool_id, actor)
    if row["status"] != "active":
        raise HTTPException(status_code=409, detail="MCP tool is not active")
    handler = _resolve_handler(row.get("handler"))
    if handler is None:
        raise HTTPException(
            status_code=404, detail="MCP tool handler not resolvable"
        )
    try:
        result = handler(body.arguments, actor)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — handler 执行异常包装为错误结果
        return MCPToolResponse(
            tool_id=row["id"],
            tool_name=row["name"],
            result={"error": str(exc)},
            is_error=True,
        ).model_dump()
    return MCPToolResponse(
        tool_id=row["id"],
        tool_name=row["name"],
        result=result,
        is_error=False,
    ).model_dump()


@router.delete("/tools/{tool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tool(
    tool_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """注销工具（硬删除）。内置工具（system workspace）不允许通过该端点删除。"""
    _require_manage(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await _owned_tool(conn, tool_id, actor)
            if row["workspace_id"] == INTERNAL_WORKSPACE_ID:
                raise HTTPException(
                    status_code=403,
                    detail="Builtin MCP tools cannot be deleted",
                )
            result = await conn.execute(
                "DELETE FROM mcp_tool WHERE id = %s RETURNING id",
                (tool_id,),
            )
            if not await result.fetchone():
                raise HTTPException(status_code=404, detail="MCP tool not found")
    await cache_delete_pattern(f"workama:cache:*:mcp_manifest:*")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
