from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, model_validator

from workama_platform.core import Actor, capability_allows, get_actor, hash_secret, json_dumps, new_id, pool
from workama_platform.modules.jobs import canonical_hash
from workama_platform.modules.automation import (
    next_cron_at,
    normalize_cron_expression,
    normalize_timezone,
    parse_cron_expression,
    _redact_payload,
)

router = APIRouter(prefix="/api/v1/automations/v2", tags=["automations"])
webhook_v2_router = APIRouter(prefix="/api/v1/automation-webhooks/v2", tags=["automation-webhooks"])

TriggerType = Literal["cron", "event", "webhook"]
ExecutorType = Literal["agent", "workflow", "script"]
TriggerStatus = Literal["active", "paused", "archived"]
RunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


def _require(actor: Actor, action: Literal["read", "write"]) -> None:
    if actor.actor_type != "user":
        raise HTTPException(status_code=403, detail="User authentication is required")
    if capability_allows(actor.capabilities, f"automation:{action}"):
        return
    if action == "write" and actor.role in {"owner", "admin", "member"}:
        return
    raise HTTPException(status_code=403, detail=f"Missing capability: automation:{action}")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS automation_trigger (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        trigger_type TEXT NOT NULL CHECK (trigger_type IN ('cron','event','webhook')),
        config JSONB NOT NULL DEFAULT '{}'::jsonb,
        executor_type TEXT NOT NULL CHECK (executor_type IN ('agent','workflow','script')),
        executor_config JSONB NOT NULL DEFAULT '{}'::jsonb,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','archived')),
        last_run_at TIMESTAMPTZ,
        next_run_at TIMESTAMPTZ,
        version INTEGER NOT NULL DEFAULT 1,
        created_by TEXT NOT NULL REFERENCES id_user(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(workspace_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS automation_trigger_run (
        id TEXT PRIMARY KEY,
        trigger_id TEXT NOT NULL REFERENCES automation_trigger(id) ON DELETE CASCADE,
        workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
        trigger_source TEXT NOT NULL CHECK (trigger_source IN ('cron','event','webhook','manual')),
        idempotency_key TEXT NOT NULL,
        input_hash TEXT NOT NULL,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        result JSONB,
        error_code TEXT,
        error_message TEXT,
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(trigger_id, idempotency_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_automation_trigger_due ON automation_trigger(workspace_id, enabled, status, next_run_at)",
    "CREATE INDEX IF NOT EXISTS idx_automation_trigger_run_trigger_time ON automation_trigger_run(trigger_id, created_at DESC)",
    # 向 automation_trigger 追加 cron_expr / webhook_secret / next_fire_at 列（幂等）
    "ALTER TABLE automation_trigger ADD COLUMN IF NOT EXISTS cron_expr TEXT",
    "ALTER TABLE automation_trigger ADD COLUMN IF NOT EXISTS webhook_secret TEXT",
    "ALTER TABLE automation_trigger ADD COLUMN IF NOT EXISTS next_fire_at TIMESTAMPTZ",
    # 向 automation_trigger_run 追加 parent_run_id 列（用于重试场景引用原 run）
    "ALTER TABLE automation_trigger_run ADD COLUMN IF NOT EXISTS parent_run_id TEXT REFERENCES automation_trigger_run(id) ON DELETE SET NULL",
    # 运行事件流表：步骤级事件
    """
    CREATE TABLE IF NOT EXISTS automation_trigger_run_event (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES automation_trigger_run(id) ON DELETE CASCADE,
        step INTEGER NOT NULL,
        event_type TEXT NOT NULL CHECK (event_type IN ('started','completed','failed','skipped')),
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_automation_trigger_run_event_run_step ON automation_trigger_run_event(run_id, step)",
)


async def ensure_automation_v2_schema(conn) -> None:
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class TriggerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    trigger_type: TriggerType
    config: dict[str, Any] = Field(default_factory=dict)
    executor_type: ExecutorType
    executor_config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_trigger(self) -> "TriggerCreate":
        if self.trigger_type == "cron":
            expr = self.config.get("cron_expression")
            if not expr or not isinstance(expr, str):
                raise ValueError("cron_expression is required in config for cron triggers")
            normalize_cron_expression(expr)
            tz = self.config.get("timezone", "UTC")
            normalize_timezone(tz)
        elif self.trigger_type == "event":
            event_type = self.config.get("event_type")
            if not event_type or not isinstance(event_type, str):
                raise ValueError("event_type is required in config for event triggers")
        elif self.trigger_type == "webhook":
            # webhook config is optional; secret auto-generated
            pass
        # Validate executor_config size
        if len(json.dumps(self.executor_config, ensure_ascii=False)) > 100_000:
            raise ValueError("executor_config is too large")
        if len(json.dumps(self.config, ensure_ascii=False)) > 100_000:
            raise ValueError("config is too large")
        target_id = self.executor_config.get("target_id")
        if target_id:
            normalized = str(target_id).strip()
            if not normalized or "://" in normalized or normalized.lower().startswith(("http:", "https:", "mock:", "local:")):
                raise ValueError("executor target_id must reference an internal workspace resource")
        return self


class TriggerPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    config: dict[str, Any] | None = None
    executor_type: ExecutorType | None = None
    executor_config: dict[str, Any] | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def validate_patch(self) -> "TriggerPatch":
        if self.config is not None:
            if len(json.dumps(self.config, ensure_ascii=False)) > 100_000:
                raise ValueError("config is too large")
            if "cron_expression" in self.config and self.config["cron_expression"] is not None:
                normalize_cron_expression(self.config["cron_expression"])
            if "timezone" in self.config and self.config["timezone"] is not None:
                normalize_timezone(self.config["timezone"])
        if self.executor_config is not None:
            if len(json.dumps(self.executor_config, ensure_ascii=False)) > 100_000:
                raise ValueError("executor_config is too large")
            target_id = self.executor_config.get("target_id")
            if target_id is not None:
                normalized = str(target_id).strip()
                if not normalized or "://" in normalized or normalized.lower().startswith(("http:", "https:", "mock:", "local:")):
                    raise ValueError("executor target_id must reference an internal workspace resource")
        return self


class TestTriggerRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# View helpers
# ---------------------------------------------------------------------------

def trigger_view(row: dict[str, Any], *, webhook_secret: str | None = None) -> dict[str, Any]:
    result = {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "name": row["name"],
        "trigger_type": row["trigger_type"],
        "config": _redact_payload(row.get("config") or {}),
        "executor_type": row["executor_type"],
        "executor_config": _redact_payload(row.get("executor_config") or {}),
        "enabled": row["enabled"],
        "status": row["status"],
        "last_run_at": row.get("last_run_at"),
        "next_run_at": row.get("next_run_at"),
        "version": row.get("version", 1),
        "webhook_endpoint": f"/api/v1/automation-webhooks/v2/{row['id']}" if row["trigger_type"] == "webhook" else None,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
    if webhook_secret:
        result["webhook_secret"] = webhook_secret
    return result


def trigger_run_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "trigger_id": row["trigger_id"],
        "workspace_id": row["workspace_id"],
        "status": row["status"],
        "trigger_source": row["trigger_source"],
        "idempotency_key": row["idempotency_key"],
        "input_hash": row["input_hash"],
        "payload": _redact_payload(row.get("payload") or {}),
        "result": _redact_payload(row.get("result") or {}),
        "error_code": row.get("error_code"),
        "error_message": row.get("error_message"),
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
        "created_at": row.get("created_at"),
        "parent_run_id": row.get("parent_run_id"),
    }


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _get_trigger(conn, trigger_id: str, workspace_id: str, *, for_update: bool = False) -> dict[str, Any]:
    lock = " FOR UPDATE" if for_update else ""
    result = await conn.execute(
        f"SELECT * FROM automation_trigger WHERE id=%s AND workspace_id=%s AND status <> 'archived'{lock}",
        (trigger_id, workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Automation trigger not found")
    return row


async def _enqueue_trigger_run(
    conn,
    trigger: dict[str, Any],
    *,
    payload: dict[str, Any],
    source: Literal["cron", "event", "webhook", "manual"],
    idempotency_key: str,
    triggered_by: str | None,
    parent_run_id: str | None = None,
) -> dict[str, Any]:
    safe_payload = _redact_payload(payload)
    input_hash = canonical_hash(safe_payload)
    existing_result = await conn.execute(
        "SELECT * FROM automation_trigger_run WHERE trigger_id=%s AND idempotency_key=%s",
        (trigger["id"], idempotency_key),
    )
    existing = await existing_result.fetchone()
    if existing:
        if existing["input_hash"] != input_hash:
            raise HTTPException(status_code=409, detail="Idempotency key was already used with different input")
        return trigger_run_view(existing)
    run_id = new_id("atrun")
    result = await conn.execute(
        """
        INSERT INTO automation_trigger_run(
          id, trigger_id, workspace_id, trigger_source, idempotency_key, input_hash, payload, status, parent_run_id
        ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,'queued',%s) RETURNING *
        """,
        (run_id, trigger["id"], trigger["workspace_id"], source, idempotency_key, input_hash, json_dumps(safe_payload), parent_run_id),
    )
    run = await result.fetchone()
    # Update trigger schedule metadata
    next_run = None
    if trigger["trigger_type"] == "cron" and trigger["enabled"] and trigger["status"] == "active":
        cfg = trigger.get("config") or {}
        cron_expr = cfg.get("cron_expression")
        tz = cfg.get("timezone", "UTC")
        if cron_expr:
            next_run = next_cron_at(cron_expr, datetime.now(UTC), tz)
    await conn.execute(
        "UPDATE automation_trigger SET last_run_at=now(), next_run_at=%s, version=version+1, updated_at=now() WHERE id=%s AND workspace_id=%s",
        (next_run, trigger["id"], trigger["workspace_id"]),
    )
    # Write outbox for worker processing
    await conn.execute(
        "INSERT INTO ops_outbox(id,event_type,workspace_id,trace_id,payload) VALUES (%s,%s,%s,%s,%s::jsonb)",
        (
            new_id("out"),
            "automation.triggered.v2",
            trigger["workspace_id"],
            run_id,
            json_dumps({
                "run_id": run_id,
                "trigger_id": trigger["id"],
                "executor_type": trigger["executor_type"],
                "executor_config": trigger.get("executor_config") or {},
                "payload": safe_payload,
            }),
        ),
    )
    return trigger_run_view(run)


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------

@router.get("/triggers")
async def list_triggers(actor: Annotated[Actor, Depends(get_actor)], limit: int = 50):
    _require(actor, "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM automation_trigger WHERE workspace_id=%s AND status <> 'archived' ORDER BY updated_at DESC LIMIT %s",
            (actor.workspace_id, min(max(limit, 1), 200)),
        )
        data = [trigger_view(row) for row in await result.fetchall()]
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None},
    }


@router.post("/triggers", status_code=status.HTTP_201_CREATED)
async def create_trigger(body: TriggerCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "write")
    cfg = dict(body.config) if body.config else {}
    cron = None
    next_run = None
    if body.trigger_type == "cron":
        cron = normalize_cron_expression(cfg.get("cron_expression", ""))
        tz = cfg.get("timezone", "UTC")
        normalize_timezone(tz)
        if body.enabled:
            next_run = next_cron_at(cron, datetime.now(UTC), tz)
    webhook_secret = secrets.token_urlsafe(32) if body.trigger_type == "webhook" else None
    if webhook_secret:
        cfg["webhook_secret_hash"] = hash_secret(webhook_secret)
    async with pool.connection() as conn:
        async with conn.transaction():
            duplicate = await conn.execute(
                "SELECT 1 FROM automation_trigger WHERE workspace_id=%s AND name=%s AND status <> 'archived'",
                (actor.workspace_id, body.name.strip()),
            )
            if await duplicate.fetchone():
                raise HTTPException(status_code=409, detail="Automation trigger name already exists")
            result = await conn.execute(
                """
                INSERT INTO automation_trigger(
                  id, workspace_id, name, trigger_type, config, executor_type, executor_config,
                  enabled, status, next_run_at, created_by
                ) VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s,%s) RETURNING *
                """,
                (
                    new_id("atrig"), actor.workspace_id, body.name.strip(), body.trigger_type,
                    json_dumps(_redact_payload(cfg)), body.executor_type,
                    json_dumps(_redact_payload(body.executor_config)),
                    body.enabled, "active" if body.enabled else "paused", next_run, actor.user_id,
                ),
            )
            row = await result.fetchone()
    return trigger_view(row, webhook_secret=webhook_secret)


@router.get("/triggers/{trigger_id}")
async def get_trigger(trigger_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "read")
    async with pool.connection() as conn:
        row = await _get_trigger(conn, trigger_id, actor.workspace_id)
    return trigger_view(row)


@router.patch("/triggers/{trigger_id}")
async def update_trigger(
    trigger_id: str,
    body: TriggerPatch,
    actor: Annotated[Actor, Depends(get_actor)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
):
    _require(actor, "write")
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="At least one trigger field is required")
    async with pool.connection() as conn:
        async with conn.transaction():
            current = await _get_trigger(conn, trigger_id, actor.workspace_id, for_update=True)
            if if_match is not None and if_match.strip() not in {"*", str(current["version"]), f'W/"{current["version"]}"', f'"{current["version"]}"'}:
                raise HTTPException(status_code=412, detail="Trigger version does not match If-Match")
            cfg = dict(current.get("config") or {})
            if body.config is not None:
                cfg.update(body.config)
            cron = cfg.get("cron_expression")
            tz = cfg.get("timezone", "UTC")
            enabled = changes.get("enabled", current["enabled"])
            status_value = "active" if enabled else "paused"
            next_run = None
            if current["trigger_type"] == "cron" and cron and enabled:
                next_run = next_cron_at(normalize_cron_expression(cron), datetime.now(UTC), tz)
            assignments = ["version=version+1", "updated_at=now()", "enabled=%s", "status=%s", "next_run_at=%s"]
            params: list[Any] = [enabled, status_value, next_run]
            if "name" in changes:
                assignments.append("name=%s")
                params.append(str(changes["name"]).strip())
            if "config" in changes:
                assignments.append("config=%s::jsonb")
                params.append(json_dumps(_redact_payload(cfg)))
            if "executor_type" in changes:
                assignments.append("executor_type=%s")
                params.append(changes["executor_type"])
            if "executor_config" in changes:
                assignments.append("executor_config=%s::jsonb")
                params.append(json_dumps(_redact_payload(changes["executor_config"])))
            params.extend([trigger_id, actor.workspace_id])
            result = await conn.execute(
                f"UPDATE automation_trigger SET {','.join(assignments)} WHERE id=%s AND workspace_id=%s RETURNING *",
                tuple(params),
            )
            row = await result.fetchone()
    return trigger_view(row)


@router.delete("/triggers/{trigger_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_trigger(trigger_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "write")
    async with pool.connection() as conn:
        result = await conn.execute(
            "UPDATE automation_trigger SET status='archived', enabled=FALSE, next_run_at=NULL, version=version+1, updated_at=now() WHERE id=%s AND workspace_id=%s AND status <> 'archived' RETURNING id",
            (trigger_id, actor.workspace_id),
        )
        if not await result.fetchone():
            raise HTTPException(status_code=404, detail="Automation trigger not found")
        await conn.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/triggers/{trigger_id}/test", status_code=status.HTTP_202_ACCEPTED)
async def test_trigger(
    trigger_id: str,
    body: TestTriggerRequest,
    actor: Annotated[Actor, Depends(get_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            trigger = await _get_trigger(conn, trigger_id, actor.workspace_id, for_update=True)
            if not trigger["enabled"] or trigger["status"] != "active":
                raise HTTPException(status_code=409, detail="Automation trigger is disabled")
            run = await _enqueue_trigger_run(
                conn,
                trigger,
                payload={**(trigger.get("config") or {}).get("default_payload", {}), **body.payload},
                source="manual",
                idempotency_key=idempotency_key or f"manual:{new_id('idem')}",
                triggered_by=actor.user_id,
            )
    return {
        "run": run,
        "trigger_id": trigger_id,
        "operation_id": run.get("id"),
        "status": run.get("status", "queued"),
        "status_url": f"/api/v1/automations/v2/triggers/{trigger_id}/runs",
        "submitted_at": run.get("created_at"),
    }


@router.get("/triggers/{trigger_id}/runs")
async def list_trigger_runs(trigger_id: str, actor: Annotated[Actor, Depends(get_actor)], limit: int = 50):
    _require(actor, "read")
    async with pool.connection() as conn:
        await _get_trigger(conn, trigger_id, actor.workspace_id)
        result = await conn.execute(
            "SELECT * FROM automation_trigger_run WHERE trigger_id=%s AND workspace_id=%s ORDER BY created_at DESC LIMIT %s",
            (trigger_id, actor.workspace_id, min(max(limit, 1), 200)),
        )
        data = [trigger_run_view(row) for row in await result.fetchall()]
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None},
    }


# ---------------------------------------------------------------------------
# Webhook v2 receiver
# ---------------------------------------------------------------------------

@webhook_v2_router.post("/{trigger_id}", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook_v2(
    trigger_id: str,
    request: Request,
    x_webhook_secret: Annotated[str | None, Header(alias="X-Webhook-Secret")] = None,
    x_workama_signature: Annotated[str | None, Header(alias="X-WorkAMA-Signature")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    # 读取原始 body（用于 HMAC 验签），再解析 JSON
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body) if raw_body else None
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Webhook body must be JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Webhook body must be an object")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                "SELECT * FROM automation_trigger WHERE id=%s AND trigger_type='webhook' AND status <> 'archived'",
                (trigger_id,),
            )
            trigger = await result.fetchone()
            if not trigger:
                raise HTTPException(status_code=404, detail="Automation webhook not found")
            cfg = trigger.get("config") or {}
            webhook_secret = trigger.get("webhook_secret")
            if webhook_secret:
                # trigger 配置了 webhook_secret → 使用 HMAC-SHA256 验签
                if not x_workama_signature:
                    raise HTTPException(status_code=401, detail="X-WorkAMA-Signature is required")
                if not _verify_webhook_signature(webhook_secret, raw_body, x_workama_signature):
                    raise HTTPException(status_code=401, detail="Webhook signature verification failed")
            else:
                # 向后兼容：未配置 webhook_secret 时走 X-Webhook-Secret 校验
                if not x_webhook_secret:
                    raise HTTPException(status_code=401, detail="X-Webhook-Secret is required")
                secret_hash = cfg.get("webhook_secret_hash")
                if not secret_hash or not hmac.compare_digest(hash_secret(x_webhook_secret), secret_hash):
                    raise HTTPException(status_code=404, detail="Automation webhook not found")
            if not trigger["enabled"] or trigger["status"] != "active":
                raise HTTPException(status_code=409, detail="Automation webhook is disabled")
            run = await _enqueue_trigger_run(
                conn,
                trigger,
                payload={**(cfg.get("default_payload") or {}), **payload},
                source="webhook",
                idempotency_key=idempotency_key or f"webhook:{new_id('idem')}",
                triggered_by=None,
            )
    return {
        "run": run,
        "trigger_id": trigger_id,
        "operation_id": run.get("id"),
        "status": run.get("status", "queued"),
        "status_url": f"/api/v1/automations/v2/triggers/{trigger_id}/runs",
        "submitted_at": run.get("created_at"),
    }


# ===========================================================================
# Cron 解析与下次触发时间计算（标准库实现，不依赖第三方 cron 库）
# ===========================================================================

def _parse_cron(expr: str) -> list[tuple[int, ...]]:
    """解析 5 字段 cron 表达式，返回 5 个排序后的取值元组。

    返回顺序：[分钟, 小时, 日, 月, 周]。
    支持语法：``*/5``、``0 9 * * 1-5``、``0 0 1 * *``、``30 14 * * *``、逗号列表、范围。
    非法表达式抛出 ValueError。
    """
    fields = parse_cron_expression(expr)
    return [tuple(sorted(f)) for f in fields]


def _next_cron_runs(
    expr: str,
    count: int,
    from_time: datetime | None = None,
) -> list[datetime]:
    """计算从 from_time 起未来 count 次触发时间（UTC）。

    每次基于上一次的触发时间向前推进，保证返回时间严格递增。
    """
    if count < 1:
        return []
    start = from_time or datetime.now(UTC)
    normalized = normalize_cron_expression(expr)
    results: list[datetime] = []
    current = start
    for _ in range(count):
        nxt = next_cron_at(normalized, current, "UTC")
        results.append(nxt)
        current = nxt + timedelta(minutes=1)
    return results


# ===========================================================================
# Webhook HMAC-SHA256 签名验证
# ===========================================================================

def _verify_webhook_signature(secret: str, body: bytes, signature: str) -> bool:
    """验证 webhook HMAC-SHA256 签名（常量时间比较）。

    :param secret: trigger.webhook_secret 作为 HMAC key
    :param body: 原始请求 body 字节
    :param signature: X-WorkAMA-Signature header 值（hex）
    :return: 签名是否匹配
    """
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return secrets.compare_digest(expected, signature)


def _compute_duration_ms(started_at: datetime | None, completed_at: datetime | None) -> int | None:
    """计算运行时长（毫秒），任一时间为空则返回 None。"""
    if not started_at or not completed_at:
        return None
    return int((completed_at - started_at).total_seconds() * 1000)


# ===========================================================================
# 详情 / 事件视图
# ===========================================================================

def trigger_run_detail_view(row: dict[str, Any]) -> dict[str, Any]:
    """运行详情视图：含 input/output/error/started_at/finished_at/duration_ms。"""
    base = trigger_run_view(row)
    started = row.get("started_at")
    completed = row.get("completed_at")
    base["finished_at"] = completed
    base["duration_ms"] = _compute_duration_ms(started, completed)
    return base


def trigger_run_event_view(row: dict[str, Any]) -> dict[str, Any]:
    """运行事件视图：含 step/event_type/payload/timestamp。"""
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "step": row["step"],
        "event_type": row["event_type"],
        "payload": _redact_payload(row.get("payload") or {}),
        "created_at": row.get("created_at"),
    }


# ===========================================================================
# 运行查询辅助
# ===========================================================================

async def _get_run(
    conn,
    run_id: str,
    trigger_id: str,
    workspace_id: str,
    *,
    for_update: bool = False,
) -> dict[str, Any]:
    """按 run_id + trigger_id + workspace_id 查询 run，确保 workspace 隔离。"""
    lock = " FOR UPDATE" if for_update else ""
    result = await conn.execute(
        f"SELECT * FROM automation_trigger_run WHERE id=%s AND trigger_id=%s AND workspace_id=%s{lock}",
        (run_id, trigger_id, workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Automation run not found")
    return row


# ===========================================================================
# Cron 校验 / 预览 Pydantic 模型
# ===========================================================================

class CronValidateRequest(BaseModel):
    expression: str = Field(min_length=1, max_length=120)


class CronPreviewRequest(BaseModel):
    expression: str = Field(min_length=1, max_length=120)
    count: int = Field(default=5, ge=1, le=20)


# ===========================================================================
# 新增端点
# ===========================================================================

@router.get("/triggers/{trigger_id}/runs/{run_id}")
async def get_trigger_run_detail(trigger_id: str, run_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    """单次运行详情（含 input/output/error/started_at/finished_at/duration_ms）。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        await _get_trigger(conn, trigger_id, actor.workspace_id)
        row = await _get_run(conn, run_id, trigger_id, actor.workspace_id)
    return trigger_run_detail_view(row)


@router.post("/triggers/{trigger_id}/runs/{run_id}/retry", status_code=status.HTTP_201_CREATED)
async def retry_trigger_run(
    trigger_id: str,
    run_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    """重试失败的运行：创建新 run，parent_run_id 引用原 run。"""
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            trigger = await _get_trigger(conn, trigger_id, actor.workspace_id, for_update=True)
            original = await _get_run(conn, run_id, trigger_id, actor.workspace_id)
            run = await _enqueue_trigger_run(
                conn,
                trigger,
                payload={**(trigger.get("config") or {}).get("default_payload", {}), **(original.get("payload") or {})},
                source=original.get("trigger_source", "manual"),
                idempotency_key=idempotency_key or f"retry:{run_id}:{new_id('idem')}",
                triggered_by=actor.user_id,
                parent_run_id=run_id,
            )
    return {
        "run": run,
        "parent_run_id": run_id,
        "operation_id": run.get("id"),
        "status": run.get("status", "queued"),
        "status_url": f"/api/v1/automations/v2/triggers/{trigger_id}/runs",
        "submitted_at": run.get("created_at"),
    }


@router.post("/triggers/{trigger_id}/runs/{run_id}/cancel")
async def cancel_trigger_run(
    trigger_id: str,
    run_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """取消运行中的 run（仅 queued/running 可取消，已完成返回 409）。"""
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _get_trigger(conn, trigger_id, actor.workspace_id, for_update=True)
            row = await _get_run(conn, run_id, trigger_id, actor.workspace_id, for_update=True)
            if row["status"] not in ("queued", "running"):
                raise HTTPException(status_code=409, detail=f"Run in status '{row['status']}' cannot be cancelled")
            result = await conn.execute(
                "UPDATE automation_trigger_run SET status='cancelled', completed_at=now() WHERE id=%s AND trigger_id=%s AND workspace_id=%s RETURNING *",
                (run_id, trigger_id, actor.workspace_id),
            )
            updated = await result.fetchone()
    return trigger_run_detail_view(updated)


@router.post("/cron/validate")
async def validate_cron_expression(body: CronValidateRequest):
    """验证 cron 表达式，返回 next 5 次触发时间。非法表达式返回 422。"""
    try:
        normalized = normalize_cron_expression(body.expression)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid cron expression: {exc}") from exc
    next_runs = _next_cron_runs(normalized, 5, datetime.now(UTC))
    return {
        "expression": normalized,
        "valid": True,
        "next_runs": next_runs,
    }


@router.post("/cron/preview")
async def preview_cron_expression(body: CronPreviewRequest):
    """预览 cron 表达式未来 N 次触发时间（默认 5 次，最多 20 次）。"""
    try:
        normalized = normalize_cron_expression(body.expression)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid cron expression: {exc}") from exc
    next_runs = _next_cron_runs(normalized, body.count, datetime.now(UTC))
    return {
        "expression": normalized,
        "count": body.count,
        "next_runs": next_runs,
    }


@router.get("/triggers/{trigger_id}/runs/{run_id}/events")
async def list_trigger_run_events(trigger_id: str, run_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    """运行事件流（步骤级事件，含 step/status/payload/timestamp）。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        await _get_trigger(conn, trigger_id, actor.workspace_id)
        await _get_run(conn, run_id, trigger_id, actor.workspace_id)
        result = await conn.execute(
            "SELECT * FROM automation_trigger_run_event WHERE run_id=%s ORDER BY step ASC, created_at ASC",
            (run_id,),
        )
        rows = await result.fetchall()
    data = [trigger_run_event_view(row) for row in rows]
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None},
    }


# ===========================================================================
# Cron 调度器 Worker 函数（逻辑实现，不启动后台任务，由 worker.py 集成）
# ===========================================================================

async def cron_scheduler_loop(conn_factory) -> dict[str, int]:
    """Cron 调度器：扫描到期 cron trigger 并入队 run。

    :param conn_factory: 可调用对象，返回 async context manager（如 ``pool.connection``）
    :return: {"scanned": N, "enqueued": M}
    """
    now = datetime.now(UTC)
    async with conn_factory() as conn:
        result = await conn.execute(
            "SELECT * FROM automation_trigger WHERE trigger_type='cron' AND enabled=TRUE AND status='active' AND status <> 'archived'"
        )
        triggers = await result.fetchall()
        enqueued = 0
        for trigger in triggers:
            cfg = trigger.get("config") or {}
            cron_expr = cfg.get("cron_expression") or trigger.get("cron_expr")
            if not cron_expr:
                continue
            # 判断是否到期：next_fire_at 或 next_run_at <= now
            next_fire = trigger.get("next_fire_at") or trigger.get("next_run_at")
            if next_fire and next_fire > now:
                continue
            tz = cfg.get("timezone", "UTC")
            try:
                await _enqueue_trigger_run(
                    conn,
                    trigger,
                    payload={**(cfg.get("default_payload") or {})},
                    source="cron",
                    idempotency_key=f"cron:{trigger['id']}:{now.strftime('%Y%m%dT%H%M')}",
                    triggered_by=None,
                )
                # 更新下次触发时间
                nxt = next_cron_at(cron_expr, now, tz)
                await conn.execute(
                    "UPDATE automation_trigger SET next_fire_at=%s, next_run_at=%s, last_run_at=now(), version=version+1, updated_at=now() WHERE id=%s",
                    (nxt, nxt, trigger["id"]),
                )
                enqueued += 1
            except HTTPException:
                # 幂等冲突（已入队）时跳过
                continue
        return {"scanned": len(triggers), "enqueued": enqueued}


__all__ = [
    "SCHEMA_STATEMENTS",
    "ensure_automation_v2_schema",
    "router",
    "webhook_v2_router",
    "trigger_view",
    "trigger_run_view",
    "trigger_run_detail_view",
    "trigger_run_event_view",
    "_enqueue_trigger_run",
    "_get_trigger",
    "_get_run",
    "_parse_cron",
    "_next_cron_runs",
    "_verify_webhook_signature",
    "_compute_duration_ms",
    "cron_scheduler_loop",
    "CronValidateRequest",
    "CronPreviewRequest",
]