"""审查/审计日志模块 (audit_log)。

v7.147: P1 身份/审查/MCP 模块 - 审计日志。

提供：
- 5 个 REST 端点（记录 / 查询 / 详情 / 导出 / 统计）
- 辅助函数 ``audit_log_action`` 供其他模块直接写入审计事件（不通过 HTTP）
- workspace 隔离 + admin/owner 写权限 + audit:read 查询权限

设计文档：910-进度追踪与任务清单.md「身份/审查/MCP」；600/620 sec_audit_log
"""
from __future__ import annotations

import asyncio
import csv
import io
import socket
import time
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from workama_platform.core import (
    Actor,
    capability_allows,
    encrypt_secret,
    get_actor,
    json_dumps,
    new_id,
    pool,
)
from workama_platform.modules.security.service import validate_outbound_url

# ============================================================================
# 常量
# ============================================================================

router = APIRouter(prefix="/api/v1/audit-logs", tags=["audit-log"])

AuditAction = Literal[
    "create",
    "update",
    "delete",
    "login",
    "logout",
    "enable",
    "disable",
    "export",
    "config_change",
]
AuditSeverity = Literal["info", "warning", "critical"]
ExportFormat = Literal["csv", "json"]
StatsGroupBy = Literal["action", "severity", "resource_type"]

_VALID_ACTIONS: frozenset[str] = frozenset(
    {
        "create",
        "update",
        "delete",
        "login",
        "logout",
        "enable",
        "disable",
        "export",
        "config_change",
    }
)
_VALID_SEVERITIES: frozenset[str] = frozenset({"info", "warning", "critical"})
_MANAGEMENT_ROLES: frozenset[str] = frozenset({"owner", "admin"})
_READ_ROLES: frozenset[str] = frozenset({"owner", "admin", "member", "viewer"})


# ============================================================================
# Pydantic 模型
# ============================================================================


class AuditEvent(BaseModel):
    """单条审计事件的最小表示（供内部传递）。"""

    action: str
    resource_type: str
    resource_id: str | None = None
    severity: AuditSeverity = "info"
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuditLogCreateRequest(BaseModel):
    """POST /api/v1/audit-logs 请求体。"""

    action: AuditAction
    resource_type: str = Field(min_length=1, max_length=120)
    resource_id: str | None = Field(default=None, max_length=200)
    severity: AuditSeverity = "info"
    description: str | None = Field(default=None, max_length=2000)
    source_ip: str | None = Field(default=None, max_length=64)
    user_agent: str | None = Field(default=None, max_length=512)
    request_id: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuditLogResponse(BaseModel):
    id: str
    workspace_id: str
    actor_id: str
    actor_email: str | None = None
    action: str
    resource_type: str
    resource_id: str | None = None
    severity: str
    description: str | None = None
    source_ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class AuditLogQuery(BaseModel):
    actor_id: str | None = None
    action: str | None = None
    resource_type: str | None = None
    severity: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


# ============================================================================
# 辅助函数
# ============================================================================


def _require_write(actor: Actor) -> None:
    """检查写权限：owner/admin 角色 或 audit:write/audit:*/* 能力。"""
    if actor.role in _MANAGEMENT_ROLES:
        return
    if any(
        capability_allows(actor.capabilities, cap)
        for cap in ("audit:write", "audit:*", "*")
    ):
        return
    raise HTTPException(status_code=403, detail="Missing capability: audit:write")


def _require_read(actor: Actor) -> None:
    """检查读权限：owner/admin/member/viewer 角色 或 audit:read/audit:*/* 能力。"""
    if actor.role in _READ_ROLES:
        return
    if any(
        capability_allows(actor.capabilities, cap)
        for cap in ("audit:read", "audit:*", "*")
    ):
        return
    raise HTTPException(status_code=403, detail="Missing capability: audit:read")


def _summary(row: dict) -> dict:
    """将数据库行转为 API 响应 dict。"""
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "actor_id": row["actor_id"],
        "actor_email": row.get("actor_email"),
        "action": row["action"],
        "resource_type": row["resource_type"],
        "resource_id": row.get("resource_id"),
        "severity": row["severity"],
        "description": row.get("description"),
        "source_ip": row.get("source_ip"),
        "user_agent": row.get("user_agent"),
        "request_id": row.get("request_id"),
        "metadata": row.get("metadata") or {},
        "created_at": row["created_at"],
    }


async def _owned_event(conn: Any, event_id: str, actor: Actor) -> dict:
    """查询审计事件并校验 workspace 归属。

    - 不存在 → 404
    - 存在但 workspace 不匹配 → 403
    """
    result = await conn.execute(
        "SELECT * FROM audit_log WHERE id = %s",
        (event_id,),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Audit log not found")
    if row["workspace_id"] != actor.workspace_id:
        raise HTTPException(
            status_code=403, detail="Audit log belongs to another workspace"
        )
    return row


async def audit_log_action(
    actor: Actor,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    *,
    severity: str = "info",
    description: str | None = None,
    source_ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict:
    """辅助函数：供其他模块直接写入审计事件（不通过 HTTP）。

    - 不做权限校验（调用方应已完成鉴权）
    - action 非法时抛 ValueError
    - 返回新建审计事件的 summary dict
    """
    if action not in _VALID_ACTIONS:
        raise ValueError(f"Invalid audit action: {action}")
    if severity not in _VALID_SEVERITIES:
        raise ValueError(f"Invalid audit severity: {severity}")
    event_id = new_id("aud")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                INSERT INTO audit_log(
                    id, workspace_id, actor_id, actor_email, action, resource_type,
                    resource_id, severity, description, source_ip, user_agent,
                    request_id, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING *
                """,
                (
                    event_id,
                    actor.workspace_id,
                    actor.user_id,
                    actor.email,
                    action,
                    resource_type,
                    resource_id,
                    severity,
                    description,
                    source_ip,
                    user_agent,
                    request_id,
                    json_dumps(metadata or {}),
                ),
            )
            row = await result.fetchone()
    summary = _summary(row)
    # M12：审计事件创建后异步投递到 SIEM（仅当 workspace 已启用 SIEM 时，避免无谓 DB 查询）
    _trigger_siem_delivery(actor.workspace_id, summary)
    return summary


# ============================================================================
# Router 端点
# ============================================================================
#
# 路由顺序：固定路径（/export, /stats）必须在参数化路径 /{id} 之前声明。


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_audit_log(
    body: AuditLogCreateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """记录审计事件（admin/owner only）。"""
    _require_write(actor)
    return await audit_log_action(
        actor,
        body.action,
        body.resource_type,
        body.resource_id,
        severity=body.severity,
        description=body.description,
        source_ip=body.source_ip,
        user_agent=body.user_agent,
        request_id=body.request_id,
        metadata=body.metadata,
    )


@router.get("/export")
async def export_audit_logs(
    actor: Annotated[Actor, Depends(get_actor)],
    format: ExportFormat = Query(default="csv"),
    actor_id: str | None = Query(default=None),
    action: str | None = Query(default=None),
    resource_type: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=10000),
):
    """导出审计日志（CSV/JSON，admin only）。"""
    _require_write(actor)
    clauses = ["workspace_id = %s"]
    params: list[Any] = [actor.workspace_id]
    if actor_id:
        clauses.append("actor_id = %s")
        params.append(actor_id)
    if action:
        clauses.append("action = %s")
        params.append(action)
    if resource_type:
        clauses.append("resource_type = %s")
        params.append(resource_type)
    if severity:
        clauses.append("severity = %s")
        params.append(severity)
    params.append(limit)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT * FROM audit_log
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC LIMIT %s
            """,
            tuple(params),
        )
        rows = await result.fetchall()
    items = [_summary(row) for row in rows]
    if format == "json":
        return Response(
            content=json_dumps({"items": items, "count": len(items)}),
            media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="audit-logs.json"'
            },
        )
    # CSV
    buf = io.StringIO()
    fieldnames = [
        "id",
        "workspace_id",
        "actor_id",
        "actor_email",
        "action",
        "resource_type",
        "resource_id",
        "severity",
        "description",
        "source_ip",
        "user_agent",
        "request_id",
        "created_at",
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for item in items:
        writer.writerow(_sanitize_csv_row({**item, "metadata": json_dumps(item.get("metadata") or {})}))
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="audit-logs.csv"'},
    )


@router.get("/stats")
async def audit_log_stats(
    actor: Annotated[Actor, Depends(get_actor)],
    group_by: StatsGroupBy = Query(default="action"),
    action: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    resource_type: str | None = Query(default=None),
):
    """统计（按 action/severity/resource_type 分组计数）。"""
    _require_read(actor)
    clauses = ["workspace_id = %s"]
    params: list[Any] = [actor.workspace_id]
    if action:
        clauses.append("action = %s")
        params.append(action)
    if severity:
        clauses.append("severity = %s")
        params.append(severity)
    if resource_type:
        clauses.append("resource_type = %s")
        params.append(resource_type)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT {group_by} AS key, count(*) AS count
            FROM audit_log
            WHERE {' AND '.join(clauses)}
            GROUP BY {group_by}
            ORDER BY count DESC
            """,
            tuple(params),
        )
        rows = await result.fetchall()
    buckets = {str(row["key"]): int(row["count"]) for row in rows}
    return {
        "group_by": group_by,
        "buckets": buckets,
        "total": sum(buckets.values()),
    }


@router.get("")
async def list_audit_logs(
    actor: Annotated[Actor, Depends(get_actor)],
    actor_id: str | None = Query(default=None),
    action: str | None = Query(default=None),
    resource_type: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """查询审计日志（支持 actor_id/action/resource_type/severity/date_range 过滤，分页）。"""
    _require_read(actor)
    clauses = ["workspace_id = %s"]
    params: list[Any] = [actor.workspace_id]
    if actor_id:
        clauses.append("actor_id = %s")
        params.append(actor_id)
    if action:
        clauses.append("action = %s")
        params.append(action)
    if resource_type:
        clauses.append("resource_type = %s")
        params.append(resource_type)
    if severity:
        clauses.append("severity = %s")
        params.append(severity)
    if start:
        clauses.append("created_at >= %s")
        params.append(start)
    if end:
        clauses.append("created_at <= %s")
        params.append(end)
    params.append(limit)
    params.append(offset)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT * FROM audit_log
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



# ============================================================================
# M12 企业审计增强：SIEM 集成 / legal hold / 批量导出多格式
# ============================================================================
#
# 设计要点：
# - legal hold：对指定事件类型 + 时间范围设置合规保留，保留期内匹配事件不可删除（423）
# - 批量导出：支持 json / csv / syslog(RFC 5424) / cef(Common Event Format)
# - SIEM：每 workspace 一个配置；事件创建时若已启用则异步投递（标准库 socket，失败重试 3 次）
# - 自动投递使用模块级 `_SIEM_ENABLED_WORKSPACES` 集合做快速判断，避免在未配置 SIEM 时
#   触发额外 DB 查询（既保护既有测试的 mock 调用计数，也减少热路径开销）。

# 已启用 SIEM 投递的 workspace 集合（由 SIEM 配置端点维护，进程级缓存）
_SIEM_ENABLED_WORKSPACES: set[str] = set()


# ----------------------------------------------------------------------------
# 数据库表定义（幂等建表）
# ----------------------------------------------------------------------------

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS audit_legal_hold (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
        hold_reason TEXT NOT NULL,
        event_filter JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_by TEXT NOT NULL REFERENCES id_user(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        released_at TIMESTAMPTZ,
        released_by TEXT,
        release_reason TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_legal_hold_workspace_time ON audit_legal_hold(workspace_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS audit_siem_config (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
        endpoint TEXT NOT NULL,
        protocol TEXT NOT NULL DEFAULT 'tcp' CHECK (protocol IN ('tcp','udp')),
        format TEXT NOT NULL DEFAULT 'syslog' CHECK (format IN ('syslog','cef')),
        api_key TEXT,
        api_key_enc TEXT,
        api_key_last4 CHAR(4),
        enabled BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(workspace_id)
    )
    """,
    "ALTER TABLE audit_siem_config ADD COLUMN IF NOT EXISTS api_key_enc TEXT",
    "ALTER TABLE audit_siem_config ADD COLUMN IF NOT EXISTS api_key_last4 CHAR(4)",
    """
    CREATE TABLE IF NOT EXISTS audit_siem_delivery (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
        event_id TEXT NOT NULL REFERENCES audit_log(id) ON DELETE CASCADE,
        siem_config_id TEXT NOT NULL REFERENCES audit_siem_config(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','failed')),
        attempts INTEGER NOT NULL DEFAULT 0,
        last_attempt_at TIMESTAMPTZ,
        error_message TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_siem_delivery_workspace_status ON audit_siem_delivery(workspace_id, status)",
)


async def ensure_audit_enterprise_schema(conn: Any) -> None:
    """应用 M12 企业审计增强（legal hold / SIEM）的幂等建表语句。"""
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


# ----------------------------------------------------------------------------
# M12 Pydantic 模型
# ----------------------------------------------------------------------------

LegalHoldProtocol = Literal["tcp", "udp"]
SiemFormat = Literal["syslog", "cef"]
BatchExportFormat = Literal["json", "csv", "syslog", "cef"]


class LegalHoldCreateRequest(BaseModel):
    """POST /api/v1/audit-logs/legal-holds 请求体。"""

    hold_reason: str = Field(min_length=1, max_length=500)
    event_types: list[str] = Field(default_factory=list)
    start_time: datetime | None = None
    end_time: datetime | None = None


class LegalHoldResponse(BaseModel):
    id: str
    workspace_id: str
    hold_reason: str
    event_filter: dict[str, Any] = Field(default_factory=dict)
    created_by: str
    created_at: datetime
    released_at: datetime | None = None
    released_by: str | None = None
    release_reason: str | None = None


class LegalHoldReleaseRequest(BaseModel):
    """DELETE /api/v1/audit-logs/legal-holds/{hold_id} 请求体。"""

    release_reason: str = Field(min_length=1, max_length=500)


class BatchExportRequest(BaseModel):
    """POST /api/v1/audit-logs/export/batch 请求体。"""

    format: BatchExportFormat = "json"
    start: datetime | None = None
    end: datetime | None = None
    event_types: list[str] = Field(default_factory=list)
    limit: int = Field(default=1000, ge=1, le=10000)
    offset: int = Field(default=0, ge=0)


class SiemConfigRequest(BaseModel):
    """POST /api/v1/audit-logs/siem/config 请求体（upsert）。"""

    endpoint: str = Field(min_length=1, max_length=512)
    protocol: LegalHoldProtocol = "tcp"
    format: SiemFormat = "syslog"
    api_key: str | None = Field(default=None, max_length=512)
    enabled: bool = False


class SiemConfigResponse(BaseModel):
    id: str
    workspace_id: str
    endpoint: str
    protocol: str
    format: str
    api_key_last4: str | None = None
    enabled: bool
    created_at: datetime
    updated_at: datetime


class SiemTestResponse(BaseModel):
    success: bool
    latency_ms: int
    error: str | None = None
    message_sent: str | None = None


# ----------------------------------------------------------------------------
# M12 辅助函数
# ----------------------------------------------------------------------------


def _require_admin(actor: Actor) -> None:
    """检查管理员权限：仅 owner/admin 角色。legal hold 释放与 SIEM 配置需要管理员。"""
    if actor.role in _MANAGEMENT_ROLES:
        return
    if any(capability_allows(actor.capabilities, cap) for cap in ("audit:write", "audit:*", "*")):
        return
    raise HTTPException(status_code=403, detail="Missing capability: audit:write (admin required)")


# syslog 设施号 4（security/auth），严重度映射到 RFC 5424 数值
_SYSLOG_FACILITY = 4  # security/auth
_SYSLOG_SEVERITY_MAP = {"info": 6, "warning": 4, "critical": 2}
# CEF 严重度（1-10）
_CEF_SEVERITY_MAP = {"info": 3, "warning": 6, "critical": 9}


def _sanitize_csv_cell(value: Any) -> Any:
    """转义 CSV 单元格以防止公式注入。

    v7.171 修复：若字符串以 =/+/-/@ 开头，前缀加单引号 ``'``，
    防止 Excel/LibreOffice 等表格软件将其解释为公式执行。
    非字符串值原样返回。
    """
    if isinstance(value, str) and value and value[0] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def _sanitize_csv_row(row: dict) -> dict:
    """对字典所有字段应用 ``_sanitize_csv_cell``，返回新字典。"""
    return {key: _sanitize_csv_cell(val) for key, val in row.items()}


def _format_syslog(event: dict, hostname: str | None = None) -> str:
    """将审计事件格式化为 RFC 5424 syslog 报文。

    格式：``<pri>version timestamp hostname app-name procid msgid structured-data msg``
    """
    sev_num = _SYSLOG_SEVERITY_MAP.get(str(event.get("severity", "info")), 6)
    pri = _SYSLOG_FACILITY * 8 + sev_num
    ts = event.get("created_at")
    if isinstance(ts, datetime):
        timestamp = ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    else:
        timestamp = str(ts or "")
    host = hostname or "workama"
    procid = str(event.get("id", "-"))
    # 结构化数据：把关键字段放进 SD-NAME
    sd_pairs = " ".join(
        f'{k}="{str(event.get(k, "")).replace(chr(34), "")}"'
        for k in ("action", "resource_type", "resource_id", "severity", "actor_id")
    )
    structured_data = f"[workama@1 {sd_pairs}]" if sd_pairs.strip() else "-"
    msg = (str(event.get("description") or event.get("action") or "audit-event")).replace("\n", "\\n").replace("\r", "\\r")
    return (
        f"<{pri}>1 {timestamp} {host} workama {procid} AuditEvent {structured_data} {msg}"
    )


def _format_cef(event: dict) -> str:
    """将审计事件格式化为 Common Event Format (CEF) 报文。

    格式：``CEF:version|vendor|product|version|signature_id|name|severity|extension``
    """
    severity = _CEF_SEVERITY_MAP.get(str(event.get("severity", "info")), 3)
    signature_id = str(event.get("action", "audit"))
    name = str(event.get("resource_type") or event.get("action") or "AuditEvent")
    # 扩展字段：act=action, duser=actor_id, msg=description
    msg_value = str(event.get("description") or "").replace("\n", "\\n").replace("\r", "\\r")
    ext = (
        f"act={event.get('action', '')} "
        f"duser={event.get('actor_id', '')} "
        f"sev={event.get('severity', 'info')} "
        f"msg={msg_value}"
    )
    return f"CEF:0|WorkAMA|Platform|1.0|{signature_id}|{name}|{severity}|{ext}"


def _hold_matches_event(hold_filter: dict, event: dict) -> bool:
    """判断单个 legal hold 的 event_filter 是否匹配给定事件。

    匹配规则（AND 组合，空值表示不限制）：
    - event_types 非空时，事件 action 必须在列表中
    - start_time 设置时，事件 created_at 必须 >= start_time
    - end_time 设置时，事件 created_at 必须 <= end_time
    """
    event_types = hold_filter.get("event_types") or []
    if event_types and event.get("action") not in event_types:
        return False
    start_time = hold_filter.get("start_time")
    if start_time:
        event_ts = event.get("created_at")
        if isinstance(event_ts, datetime) and isinstance(start_time, str):
            start_time = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        if isinstance(event_ts, datetime) and event_ts < start_time:
            return False
    end_time = hold_filter.get("end_time")
    if end_time:
        event_ts = event.get("created_at")
        if isinstance(event_ts, datetime) and isinstance(end_time, str):
            end_time = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
        if isinstance(event_ts, datetime) and event_ts > end_time:
            return False
    return True


async def _check_legal_hold(conn: Any, event: dict, workspace_id: str) -> None:
    """检查事件是否处于 legal hold 保留期内，若是则抛 423 Locked。

    供删除/清理审计事件前调用。
    """
    result = await conn.execute(
        "SELECT * FROM audit_legal_hold WHERE workspace_id = %s AND released_at IS NULL",
        (workspace_id,),
    )
    rows = await result.fetchall()
    for row in rows:
        hold_filter = row.get("event_filter") or {}
        if _hold_matches_event(hold_filter, event):
            raise HTTPException(
                status_code=423,
                detail=f"Event is under legal hold: {row.get('hold_reason')}",
            )


def _parse_endpoint(endpoint: str) -> tuple[str, int]:
    """解析 ``host:port`` 形式的 SIEM endpoint，返回 (host, port)。"""
    if ":" not in endpoint:
        raise ValueError(f"Invalid SIEM endpoint (expected host:port): {endpoint}")
    host, port_str = endpoint.rsplit(":", 1)
    return host.strip(), int(port_str.strip())


def _send_to_siem_endpoint(
    endpoint: str, protocol: str, message: str, timeout: float = 5.0
) -> tuple[bool, str | None, int]:
    """通过标准库 socket 发送一条消息到 SIEM endpoint。

    返回 ``(success, error_message, latency_ms)``。不抛异常。

    v7.171 修复：在连接前对 host 做 ``validate_outbound_url`` SSRF 校验，
    阻断内网/loopback/元数据地址等不安全出站请求。
    """
    started = time.monotonic()
    try:
        host, port = _parse_endpoint(endpoint)
    except (ValueError, TypeError) as exc:
        return False, f"invalid endpoint: {exc}", 0
    # v7.171 修复：SSRF 校验——把 host 包装为 URL 形式校验，阻断内网/loopback/元数据地址。
    validation = validate_outbound_url(f"http://{host}")
    if not validation.allowed:
        return False, f"SIEM endpoint host is not allowed: {validation.reason}", 0
    try:
        payload = (message + "\n").encode("utf-8")
        if protocol == "udp":
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            try:
                sock.sendto(payload, (host, port))
            finally:
                sock.close()
        else:
            sock = socket.create_connection((host, port), timeout=timeout)
            try:
                sock.sendall(payload)
            finally:
                sock.close()
        latency = int((time.monotonic() - started) * 1000)
        return True, None, latency
    except OSError as exc:
        latency = int((time.monotonic() - started) * 1000)
        return False, str(exc), latency


async def _record_delivery(
    conn: Any,
    workspace_id: str,
    event_id: str,
    siem_config_id: str,
    status: str,
    attempts: int,
    error_message: str | None,
) -> None:
    """记录一条 SIEM 投递结果到 audit_siem_delivery。"""
    await conn.execute(
        """
        INSERT INTO audit_siem_delivery(
            id, workspace_id, event_id, siem_config_id, status, attempts,
            last_attempt_at, error_message)
        VALUES (%s, %s, %s, %s, %s, %s, now(), %s)
        """,
        (
            new_id("sdel"),
            workspace_id,
            event_id,
            siem_config_id,
            status,
            attempts,
            error_message,
        ),
    )


async def _deliver_to_siem(
    workspace_id: str, event_summary: dict, max_retries: int = 3
) -> None:
    """将单条审计事件投递到 workspace 的 SIEM endpoint（带重试 + 记录）。

    - 查询启用中的 SIEM 配置；未配置则直接返回
    - 按 format（syslog/cef）格式化报文
    - 最多重试 max_retries 次；最终记录到 audit_siem_delivery
    - 全程吞掉异常，避免影响后台任务稳定性
    """
    try:
        async with pool.connection() as conn:
            result = await conn.execute(
                "SELECT * FROM audit_siem_config WHERE workspace_id = %s AND enabled = TRUE",
                (workspace_id,),
            )
            cfg = await result.fetchone()
            if not cfg:
                return
            fmt = cfg.get("format", "syslog")
            if fmt == "cef":
                message = _format_cef(event_summary)
            else:
                message = _format_syslog(event_summary)
            attempts = 0
            last_error: str | None = None
            success = False
            for _ in range(max_retries):
                attempts += 1
                ok, err, _lat = _send_to_siem_endpoint(
                    cfg["endpoint"], cfg["protocol"], message
                )
                if ok:
                    success = True
                    last_error = None
                    break
                last_error = err
            await _record_delivery(
                conn,
                workspace_id,
                event_summary.get("id", ""),
                cfg["id"],
                "sent" if success else "failed",
                attempts,
                last_error,
            )
    except Exception:
        # 后台投递失败不应影响主流程
        return


def _trigger_siem_delivery(workspace_id: str, event_summary: dict) -> None:
    """审计事件创建后触发 SIEM 投递。

    v7.171 修复：移除对模块级 ``_SIEM_ENABLED_WORKSPACES`` 的判断，始终创建
    后台任务由 ``_deliver_to_siem`` 查 DB（``audit_siem_config WHERE enabled=TRUE``）
    决定是否投递。原模块级集合在多 worker 下跨进程不一致，会导致已启用 SIEM 的
    workspace 在其他 worker 上漏投递；改为以 DB 为唯一真相源。
    ``_SIEM_ENABLED_WORKSPACES`` 仍由 POST /siem/config 维护作为缓存，但不再
    作为投递的硬性判断依据。
    """
    try:
        asyncio.create_task(_deliver_to_siem(workspace_id, event_summary))
    except RuntimeError:
        # 无运行中的事件循环（非 async 上下文）时忽略
        return


def _siem_config_summary(row: dict) -> dict:
    """将 SIEM 配置行转为 API 响应 dict。

    v7.171 修复：不再返回明文 api_key，仅返回 api_key_last4。
    """
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "endpoint": row["endpoint"],
        "protocol": row["protocol"],
        "format": row["format"],
        "api_key_last4": row.get("api_key_last4"),
        "enabled": row["enabled"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _legal_hold_summary(row: dict) -> dict:
    """将 legal hold 行转为 API 响应 dict。"""
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "hold_reason": row["hold_reason"],
        "event_filter": row.get("event_filter") or {},
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "released_at": row.get("released_at"),
        "released_by": row.get("released_by"),
        "release_reason": row.get("release_reason"),
    }


# ----------------------------------------------------------------------------
# M12 Router 端点
# ----------------------------------------------------------------------------
#
# 路由顺序：固定路径（/legal-holds, /export/batch, /siem/...）均在参数化路径
# /{event_id} 之后声明；FastAPI 按声明顺序匹配，由于这些路径不含单段参数且更具体，
# 不会被 /{event_id} 遮蔽（{event_id} 仅匹配单段，而 /legal-holds 等是固定段）。


# 1. 创建 legal hold
@router.post("/legal-holds", status_code=status.HTTP_201_CREATED)
async def create_legal_hold(
    body: LegalHoldCreateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """创建 legal hold（admin only）。对匹配事件设置合规保留，保留期内不可删除。"""
    _require_admin(actor)
    hold_id = new_id("alh")
    event_filter = {
        "event_types": list(body.event_types),
        "start_time": body.start_time.isoformat() if body.start_time else None,
        "end_time": body.end_time.isoformat() if body.end_time else None,
    }
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                INSERT INTO audit_legal_hold(
                    id, workspace_id, hold_reason, event_filter, created_by)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                RETURNING *
                """,
                (
                    hold_id,
                    actor.workspace_id,
                    body.hold_reason,
                    json_dumps(event_filter),
                    actor.user_id,
                ),
            )
            row = await result.fetchone()
    return _legal_hold_summary(row)


# 2. legal hold 列表（分页）
@router.get("/legal-holds")
async def list_legal_holds(
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    active_only: bool = Query(default=False),
):
    """查询 legal hold 列表（分页）。"""
    _require_read(actor)
    clauses = ["workspace_id = %s"]
    params: list[Any] = [actor.workspace_id]
    if active_only:
        clauses.append("released_at IS NULL")
    params.append(limit)
    params.append(offset)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT * FROM audit_legal_hold
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC LIMIT %s OFFSET %s
            """,
            tuple(params),
        )
        rows = await result.fetchall()
    items = [_legal_hold_summary(row) for row in rows]
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


# 3. 释放 legal hold（admin + reason）
@router.delete("/legal-holds/{hold_id}")
async def release_legal_hold(
    hold_id: str,
    body: LegalHoldReleaseRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """释放 legal hold（admin only，需提供 release_reason）。"""
    _require_admin(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                "SELECT * FROM audit_legal_hold WHERE id = %s",
                (hold_id,),
            )
            row = await result.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Legal hold not found")
            if row["workspace_id"] != actor.workspace_id:
                raise HTTPException(
                    status_code=403, detail="Legal hold belongs to another workspace"
                )
            upd = await conn.execute(
                """
                UPDATE audit_legal_hold
                SET released_at = now(), released_by = %s, release_reason = %s
                WHERE id = %s
                RETURNING *
                """,
                (actor.user_id, body.release_reason, hold_id),
            )
            updated = await upd.fetchone()
    return _legal_hold_summary(updated)


# 4. 批量导出（json/csv/syslog/cef）
@router.post("/export/batch")
async def batch_export_audit_logs(
    body: BatchExportRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """批量导出审计日志，支持 json/csv/syslog/cef 格式 + 时间范围与事件类型过滤。"""
    _require_write(actor)
    clauses = ["workspace_id = %s"]
    params: list[Any] = [actor.workspace_id]
    if body.start:
        clauses.append("created_at >= %s")
        params.append(body.start)
    if body.end:
        clauses.append("created_at <= %s")
        params.append(body.end)
    if body.event_types:
        clauses.append("action = ANY(%s)")
        params.append(list(body.event_types))
    params.append(body.limit)
    params.append(body.offset)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT * FROM audit_log
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC LIMIT %s OFFSET %s
            """,
            tuple(params),
        )
        rows = await result.fetchall()
    items = [_summary(row) for row in rows]

    if body.format == "json":
        return Response(
            content=json_dumps({"items": items, "count": len(items)}),
            media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="audit-logs-batch.json"'
            },
        )
    if body.format == "csv":
        buf = io.StringIO()
        fieldnames = [
            "id", "workspace_id", "actor_id", "actor_email", "action",
            "resource_type", "resource_id", "severity", "description",
            "source_ip", "user_agent", "request_id", "created_at",
        ]
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for item in items:
            writer.writerow(_sanitize_csv_row(item))
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="audit-logs-batch.csv"'
            },
        )
    if body.format == "syslog":
        lines = [_format_syslog(item) for item in items]
        return Response(
            content="\n".join(lines),
            media_type="application/syslog",
            headers={
                "Content-Disposition": 'attachment; filename="audit-logs.syslog"'
            },
        )
    # cef
    lines = [_format_cef(item) for item in items]
    return Response(
        content="\n".join(lines),
        media_type="application/cef",
        headers={"Content-Disposition": 'attachment; filename="audit-logs.cef"'},
    )


# 5. 配置 SIEM 集成（upsert，admin only）
@router.post("/siem/config", status_code=status.HTTP_201_CREATED)
async def upsert_siem_config(
    body: SiemConfigRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """配置（创建或更新）当前 workspace 的 SIEM 集成（admin only）。"""
    _require_admin(actor)
    config_id = new_id("siem")
    # v7.171 修复：api_key 加密存储（api_key_enc）+ 仅保留 last4；不写明文 api_key 列。
    api_key_enc = encrypt_secret(body.api_key) if body.api_key else None
    api_key_last4 = body.api_key[-4:] if body.api_key else None
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                INSERT INTO audit_siem_config(
                    id, workspace_id, endpoint, protocol, format, api_key_enc, api_key_last4, enabled)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (workspace_id) DO UPDATE SET
                    endpoint = EXCLUDED.endpoint,
                    protocol = EXCLUDED.protocol,
                    format = EXCLUDED.format,
                    api_key = NULL,
                    api_key_enc = EXCLUDED.api_key_enc,
                    api_key_last4 = EXCLUDED.api_key_last4,
                    enabled = EXCLUDED.enabled,
                    updated_at = now()
                RETURNING *
                """,
                (
                    config_id,
                    actor.workspace_id,
                    body.endpoint,
                    body.protocol,
                    body.format,
                    api_key_enc,
                    api_key_last4,
                    body.enabled,
                ),
            )
            row = await result.fetchone()
    # 维护进程级启用集合（仍保留作为缓存；投递判断已改为查 DB，见 _trigger_siem_delivery）
    if row["enabled"]:
        _SIEM_ENABLED_WORKSPACES.add(actor.workspace_id)
    else:
        _SIEM_ENABLED_WORKSPACES.discard(actor.workspace_id)
    return _siem_config_summary(row)


# 6. 获取 SIEM 配置
@router.get("/siem/config")
async def get_siem_config(
    actor: Annotated[Actor, Depends(get_actor)],
):
    """获取当前 workspace 的 SIEM 配置。"""
    _require_read(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM audit_siem_config WHERE workspace_id = %s",
            (actor.workspace_id,),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="SIEM config not found")
    return _siem_config_summary(row)


# 7. 测试 SIEM 连接（admin only）
@router.post("/siem/test")
async def test_siem_connection(
    actor: Annotated[Actor, Depends(get_actor)],
):
    """测试当前 workspace 的 SIEM 连接（发送一条测试事件到配置的 endpoint）。"""
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM audit_siem_config WHERE workspace_id = %s",
            (actor.workspace_id,),
        )
        cfg = await result.fetchone()
    if not cfg:
        raise HTTPException(status_code=404, detail="SIEM config not found")
    test_event = {
        "id": "siem-test",
        "action": "siem_test",
        "resource_type": "siem",
        "severity": "info",
        "description": "SIEM connectivity test from WorkAMA",
        "actor_id": actor.user_id,
    }
    if cfg["format"] == "cef":
        message = _format_cef(test_event)
    else:
        message = _format_syslog(test_event)
    ok, err, latency = _send_to_siem_endpoint(cfg["endpoint"], cfg["protocol"], message)
    return {
        "success": ok,
        "latency_ms": latency,
        "error": err,
        "message_sent": message,
    }


# 单条审计事件详情（参数化路由，必须放在所有固定路径端点之后，避免遮蔽 /legal-holds 等单段路径）
@router.get("/{event_id}")
async def get_audit_log(
    event_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """单条审计事件详情。"""
    _require_read(actor)
    async with pool.connection() as conn:
        return _summary(await _owned_event(conn, event_id, actor))


# v7.172 修复：补全 legal hold 423 Locked 接线。
# 原先 ``_check_legal_hold`` 已实现但无任何删除/清理路径调用它，
# 导致"法律保留期内阻止删除"的核心安全功能形同虚设。
# 现新增 ``DELETE /api/v1/audit-logs/{event_id}`` 端点（admin only），
# 在删除审计事件前先调用 ``_check_legal_hold``，命中保留期则返回 423 Locked。
@router.delete("/{event_id}")
async def delete_audit_log(
    event_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """删除单条审计事件（admin only）。

    - 事件不存在 → 404
    - workspace 不匹配 → 403
    - 事件处于 legal hold 保留期 → 423 Locked
    - 删除成功 → 204 No Content
    """
    _require_admin(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            event = await _owned_event(conn, event_id, actor)
            # legal hold 检查：命中则抛 423 Locked
            await _check_legal_hold(conn, event, actor.workspace_id)
            await conn.execute(
                "DELETE FROM audit_log WHERE id = %s AND workspace_id = %s",
                (event_id, actor.workspace_id),
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = [
    "router",
    "audit_log_action",
    "SCHEMA_STATEMENTS",
    "ensure_audit_enterprise_schema",
    "AuditEvent",
    "AuditLogCreateRequest",
    "AuditLogResponse",
    "AuditLogQuery",
    "LegalHoldCreateRequest",
    "LegalHoldResponse",
    "LegalHoldReleaseRequest",
    "BatchExportRequest",
    "SiemConfigRequest",
    "SiemConfigResponse",
    "SiemTestResponse",
]
