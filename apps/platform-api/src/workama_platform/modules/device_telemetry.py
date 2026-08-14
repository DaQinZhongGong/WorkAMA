"""AMA-Work 设备协同与本地观测闭环 (device_telemetry)。

v7.142: 设备注册 / 心跳上报 / 遥测事件 / 离线扫描。

提供：
- 7 个 REST 端点（注册 / 心跳 / 列表 / 详情 / 注销 / 事件上报 / 离线扫描）
- Worker 类 ``DeviceOfflineSweepWorker``，供 platform-worker 通过 job 队列调用
- 离线检测：``last_heartbeat_at`` 超过阈值（默认 300 秒）标记 offline

设计文档：910-进度追踪与任务清单.md「AMA-Work 设备协同与本地观测闭环」
"""
from __future__ import annotations

import os
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

# ============================================================================
# 常量
# ============================================================================

router = APIRouter(prefix="/api/v1/devices", tags=["device-telemetry"])

# Worker job 类型常量（platform-worker 通过这些常量路由 job）
DEVICE_OFFLINE_SWEEP_JOB_TYPE = "device_offline_sweep"

# 默认离线阈值：5 分钟无心跳即标记 offline
DEFAULT_OFFLINE_THRESHOLD_SECONDS = 300

DeviceKind = Literal["desktop", "laptop", "server", "edge", "iot"]
DeviceStatus = Literal["online", "offline", "warning"]

_VALID_KINDS: frozenset[str] = frozenset(
    {"desktop", "laptop", "server", "edge", "iot"}
)
_VALID_STATUSES: frozenset[str] = frozenset({"online", "offline", "warning"})


# ============================================================================
# Pydantic 模型
# ============================================================================


class DeviceRegistration(BaseModel):
    device_id: str = Field(min_length=1, max_length=200)
    device_name: str | None = Field(default=None, max_length=200)
    device_kind: DeviceKind = "desktop"
    os: str | None = Field(default=None, max_length=100)
    app_version: str | None = Field(default=None, max_length=50)
    status: DeviceStatus = "online"
    telemetry: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DeviceHeartbeat(BaseModel):
    status: DeviceStatus = "online"
    telemetry: dict[str, Any] = Field(default_factory=dict)


class TelemetryEvent(BaseModel):
    event_type: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None


class DeviceResponse(BaseModel):
    id: str
    workspace_id: str
    device_id: str
    device_name: str | None = None
    device_kind: str
    os: str | None = None
    app_version: str | None = None
    last_heartbeat_at: datetime | None = None
    status: str
    telemetry: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


# ============================================================================
# 辅助函数
# ============================================================================


def _offline_threshold_seconds() -> int:
    """读取离线阈值秒数（环境变量 ``WORKAMA_DEVICE_OFFLINE_THRESHOLD_SECONDS``）。"""
    raw = os.getenv(
        "WORKAMA_DEVICE_OFFLINE_THRESHOLD_SECONDS",
        str(DEFAULT_OFFLINE_THRESHOLD_SECONDS),
    ).strip()
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_OFFLINE_THRESHOLD_SECONDS
    return val if val > 0 else DEFAULT_OFFLINE_THRESHOLD_SECONDS


def _require(actor: Actor, action: str) -> None:
    """检查 actor 是否拥有 device:{action} 能力。"""
    if not capability_allows(actor.capabilities, f"device:{action}"):
        raise HTTPException(
            status_code=403, detail=f"Missing capability: device:{action}"
        )


def _summary(row: dict) -> dict:
    """将数据库行转为 API 响应 dict。"""
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "device_id": row["device_id"],
        "device_name": row.get("device_name"),
        "device_kind": row["device_kind"],
        "os": row.get("os"),
        "app_version": row.get("app_version"),
        "last_heartbeat_at": row.get("last_heartbeat_at"),
        "status": row["status"],
        "telemetry": row.get("telemetry") or {},
        "metadata": row.get("metadata") or {},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def _owned_device(conn: Any, device_id: str, actor: Actor) -> dict:
    """查询设备并校验 workspace 归属。

    - 不存在 → 404
    - 存在但 workspace 不匹配 → 403
    """
    result = await conn.execute(
        "SELECT * FROM device_telemetry WHERE device_id = %s",
        (device_id,),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Device not found")
    if row["workspace_id"] != actor.workspace_id:
        raise HTTPException(
            status_code=403, detail="Device belongs to another workspace"
        )
    return row


# ============================================================================
# Worker 类（供 platform-worker 通过 job 队列调用）
# ============================================================================


class DeviceOfflineSweepWorker:
    """离线扫描 Worker：将超过阈值未心跳的设备标记为 offline。

    供 platform-worker 通过 job 队列调用：
    ``await worker.process_offline_sweep_job({"workspace_id": "wsp_xxx"})``
    payload.workspace_id 为空时扫描全表。
    """

    async def sweep_offline_devices(
        self, workspace_id: str | None = None, *, threshold_seconds: int | None = None
    ) -> dict:
        """扫描并标记离线设备。

        返回 ``{"scanned": int, "swept": int, "swept_ids": list[str]}``。
        """
        threshold = (
            threshold_seconds
            if threshold_seconds is not None
            else _offline_threshold_seconds()
        )
        async with pool.connection() as conn:
            async with conn.transaction():
                if workspace_id:
                    count_result = await conn.execute(
                        "SELECT count(*) AS cnt FROM device_telemetry WHERE workspace_id = %s",
                        (workspace_id,),
                    )
                    count_row = await count_result.fetchone()
                    scanned = int(count_row["cnt"]) if count_row else 0
                    result = await conn.execute(
                        """
                        UPDATE device_telemetry
                        SET status = 'offline', updated_at = now()
                        WHERE workspace_id = %s
                          AND status <> 'offline'
                          AND (last_heartbeat_at IS NULL
                               OR last_heartbeat_at < now() - interval '1 second' * %s)
                        RETURNING id, device_id
                        """,
                        (workspace_id, threshold),
                    )
                else:
                    count_result = await conn.execute(
                        "SELECT count(*) AS cnt FROM device_telemetry"
                    )
                    count_row = await count_result.fetchone()
                    scanned = int(count_row["cnt"]) if count_row else 0
                    result = await conn.execute(
                        """
                        UPDATE device_telemetry
                        SET status = 'offline', updated_at = now()
                        WHERE status <> 'offline'
                          AND (last_heartbeat_at IS NULL
                               OR last_heartbeat_at < now() - interval '1 second' * %s)
                        RETURNING id, device_id
                        """,
                        (threshold,),
                    )
                rows = await result.fetchall()
        swept_ids = [r["id"] for r in rows]
        return {
            "scanned": scanned,
            "swept": len(swept_ids),
            "swept_ids": swept_ids,
            "threshold_seconds": threshold,
        }

    async def process_offline_sweep_job(self, payload: dict) -> dict:
        """处理离线扫描 job（由 platform-worker 调用）。

        payload 可选字段：
        - ``workspace_id``: 限定工作区扫描，为空则全表扫描
        - ``threshold_seconds``: 覆盖默认阈值
        """
        workspace_id = payload.get("workspace_id") or None
        threshold_raw = payload.get("threshold_seconds")
        threshold_seconds: int | None = None
        if threshold_raw is not None:
            try:
                threshold_seconds = int(threshold_raw)
            except (TypeError, ValueError):
                threshold_seconds = None
        return await self.sweep_offline_devices(
            workspace_id, threshold_seconds=threshold_seconds
        )


# 模块级 Worker 实例（platform-worker 直接 import 使用）
offline_sweep_worker = DeviceOfflineSweepWorker()


# ============================================================================
# Router 端点
# ============================================================================
#
# 路由声明顺序很重要：具体路径（/register, /offline-sweep）必须在参数化路径
# /{device_id} 之前声明，否则 FastAPI 会将 "register" 等当作 device_id 参数匹配。


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register_device(
    body: DeviceRegistration,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """注册设备（upsert）：按 (workspace_id, device_id) 唯一键插入或更新。"""
    _require(actor, "write")
    device_pk = new_id("dev")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                INSERT INTO device_telemetry(
                    id, workspace_id, device_id, device_name, device_kind, os,
                    app_version, last_heartbeat_at, status, telemetry, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now(), %s, %s::jsonb, %s::jsonb)
                ON CONFLICT (workspace_id, device_id) DO UPDATE SET
                    device_name = EXCLUDED.device_name,
                    device_kind = EXCLUDED.device_kind,
                    os = EXCLUDED.os,
                    app_version = EXCLUDED.app_version,
                    status = EXCLUDED.status,
                    metadata = EXCLUDED.metadata,
                    last_heartbeat_at = COALESCE(
                        device_telemetry.last_heartbeat_at, now()),
                    updated_at = now()
                RETURNING *
                """,
                (
                    device_pk,
                    actor.workspace_id,
                    body.device_id,
                    body.device_name,
                    body.device_kind,
                    body.os,
                    body.app_version,
                    body.status,
                    json_dumps(body.telemetry),
                    json_dumps(body.metadata),
                ),
            )
            row = await result.fetchone()
    return _summary(row)


@router.post("/{device_id}/heartbeat")
async def heartbeat(
    device_id: str,
    body: DeviceHeartbeat,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """心跳上报：更新 last_heartbeat_at / status / telemetry。"""
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _owned_device(conn, device_id, actor)
            result = await conn.execute(
                """
                UPDATE device_telemetry
                SET last_heartbeat_at = now(), status = %s,
                    telemetry = %s::jsonb, updated_at = now()
                WHERE device_id = %s AND workspace_id = %s
                RETURNING *
                """,
                (
                    body.status,
                    json_dumps(body.telemetry),
                    device_id,
                    actor.workspace_id,
                ),
            )
            row = await result.fetchone()
    return _summary(row)


@router.post("/{device_id}/events")
async def report_event(
    device_id: str,
    body: TelemetryEvent,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """上报遥测事件：追加到 telemetry JSONB 的 events 数组。"""
    _require(actor, "write")
    event_record: dict[str, Any] = {
        "event_type": body.event_type,
        "payload": body.payload,
    }
    if body.occurred_at is not None:
        event_record["occurred_at"] = body.occurred_at.isoformat()
    else:
        event_record["occurred_at"] = datetime.now(UTC).isoformat()
    event_json = json_dumps([event_record])
    async with pool.connection() as conn:
        async with conn.transaction():
            await _owned_device(conn, device_id, actor)
            result = await conn.execute(
                """
                UPDATE device_telemetry
                SET telemetry = jsonb_set(
                        telemetry,
                        '{events}',
                        COALESCE(telemetry->'events', '[]'::jsonb) || %s::jsonb
                    ),
                    updated_at = now()
                WHERE device_id = %s AND workspace_id = %s
                RETURNING *
                """,
                (event_json, device_id, actor.workspace_id),
            )
            row = await result.fetchone()
    summary = _summary(row)
    events = (summary.get("telemetry") or {}).get("events") or []
    return {**summary, "events_count": len(events)}


@router.get("/offline-sweep")
async def offline_sweep(
    actor: Annotated[Actor, Depends(get_actor)],
    threshold_seconds: int | None = Query(default=None, ge=1, le=86400),
):
    """扫描离线设备：将超过阈值未心跳的设备标记为 offline。

    阈值默认由 ``WORKAMA_DEVICE_OFFLINE_THRESHOLD_SECONDS`` 环境变量决定（300 秒）。
    仅扫描当前 workspace 的设备。
    """
    _require(actor, "read")
    return await offline_sweep_worker.sweep_offline_devices(
        actor.workspace_id, threshold_seconds=threshold_seconds
    )


@router.get("")
async def list_devices(
    actor: Annotated[Actor, Depends(get_actor)],
    status_filter: DeviceStatus | None = Query(default=None, alias="status"),
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
            SELECT * FROM device_telemetry
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


@router.get("/{device_id}")
async def get_device(
    device_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """查询单设备详情。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        return _summary(await _owned_device(conn, device_id, actor))


@router.delete("/{device_id}", status_code=status.HTTP_200_OK)
async def delete_device(
    device_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """注销设备（硬删除）。"""
    _require(actor, "delete")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _owned_device(conn, device_id, actor)
            result = await conn.execute(
                "DELETE FROM device_telemetry WHERE device_id = %s "
                "AND workspace_id = %s RETURNING id",
                (device_id, actor.workspace_id),
            )
            row = await result.fetchone()
    return {"id": row["id"], "deleted": True}
