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


router = APIRouter(prefix="/api/v1/automations", tags=["automations"])
webhook_router = APIRouter(prefix="/api/v1/automation-webhooks", tags=["automation-webhooks"])

TriggerType = Literal["cron", "webhook"]
AutomationStatus = Literal["active", "paused", "archived"]


def normalize_automation_target_id(value: str) -> str:
    """Only workspace resource IDs may be used as automation targets."""
    normalized = value.strip()
    if not normalized or "://" in normalized or normalized.lower().startswith(("http:", "https:", "mock:", "local:")):
        raise ValueError("automation targets must reference an internal workspace resource")
    return normalized


class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    trigger_type: TriggerType
    cron_expression: str | None = Field(default=None, max_length=120)
    timezone: str = Field(default="UTC", min_length=1, max_length=80)
    target_type: Literal["work_plan", "workflow", "agent"] = "agent"
    target_id: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_trigger(self) -> "ScheduleCreate":
        if self.trigger_type == "cron" and not self.cron_expression:
            raise ValueError("cron_expression is required for cron schedules")
        if self.trigger_type == "webhook" and self.cron_expression:
            raise ValueError("cron_expression is only valid for cron schedules")
        normalize_timezone(self.timezone)
        normalize_automation_target_id(self.target_id)
        if len(json.dumps(self.payload, ensure_ascii=False)) > 100_000:
            raise ValueError("payload is too large")
        if self.cron_expression:
            normalize_cron_expression(self.cron_expression)
        return self


class SchedulePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    cron_expression: str | None = Field(default=None, max_length=120)
    timezone: str | None = Field(default=None, min_length=1, max_length=80)
    target_id: str | None = Field(default=None, min_length=1, max_length=120)
    payload: dict[str, Any] | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def validate_patch(self) -> "SchedulePatch":
        if "cron_expression" in self.model_fields_set and self.cron_expression is None:
            raise ValueError("cron_expression cannot be cleared for a cron schedule")
        if self.cron_expression is not None:
            normalize_cron_expression(self.cron_expression)
        if self.timezone is not None:
            normalize_timezone(self.timezone)
        if self.target_id is not None:
            normalize_automation_target_id(self.target_id)
        if self.payload is not None and len(json.dumps(self.payload, ensure_ascii=False)) > 100_000:
            raise ValueError("payload is too large")
        return self


class TriggerRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS ops_automation_schedule (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
        workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        trigger_type TEXT NOT NULL CHECK (trigger_type IN ('cron','webhook')),
        cron_expression TEXT,
        timezone TEXT NOT NULL DEFAULT 'UTC',
        target_type TEXT NOT NULL CHECK (target_type IN ('work_plan','workflow','agent')),
        target_id TEXT NOT NULL,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        webhook_secret_hash TEXT,
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','archived')),
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
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
    CREATE TABLE IF NOT EXISTS ops_automation_run (
        id TEXT PRIMARY KEY,
        schedule_id TEXT NOT NULL REFERENCES ops_automation_schedule(id) ON DELETE CASCADE,
        workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
        trigger_source TEXT NOT NULL CHECK (trigger_source IN ('cron','webhook','manual')),
        idempotency_key TEXT NOT NULL,
        input_hash TEXT NOT NULL,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
        triggered_by TEXT REFERENCES id_user(id) ON DELETE SET NULL,
        operation_id TEXT REFERENCES ops_async_operation(id) ON DELETE SET NULL,
        error_code TEXT,
        error_message TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        completed_at TIMESTAMPTZ,
        UNIQUE(schedule_id, idempotency_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ops_automation_schedule_due ON ops_automation_schedule(workspace_id, enabled, status, next_run_at)",
    "CREATE INDEX IF NOT EXISTS idx_ops_automation_run_schedule_time ON ops_automation_run(schedule_id, created_at DESC)",
)


async def ensure_automation_schema(conn) -> None:
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


def _parse_field(value: str, minimum: int, maximum: int) -> frozenset[int]:
    values: set[int] = set()
    for raw_part in value.strip().split(","):
        part = raw_part.strip()
        if not part:
            raise ValueError("cron field contains an empty item")
        pieces = part.split("/", 1)
        if len(pieces) == 2:
            try:
                step = int(pieces[1])
            except ValueError as exc:
                raise ValueError("cron step must be an integer") from exc
            if step < 1:
                raise ValueError("cron step must be positive")
        else:
            step = 1
        base = pieces[0]
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            bounds = base.split("-", 1)
            try:
                start, end = int(bounds[0]), int(bounds[1])
            except ValueError as exc:
                raise ValueError("cron range must contain integers") from exc
        else:
            try:
                start = end = int(base)
            except ValueError as exc:
                raise ValueError("cron value must be an integer") from exc
        if start < minimum or end > maximum or start > end:
            raise ValueError(f"cron value must be between {minimum} and {maximum}")
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError("cron field is empty")
    return frozenset(values)


def parse_cron_expression(expression: str) -> tuple[frozenset[int], ...]:
    fields = expression.strip().split()
    if len(fields) != 5:
        raise ValueError("cron expression must have five fields")
    return (
        _parse_field(fields[0], 0, 59),
        _parse_field(fields[1], 0, 23),
        _parse_field(fields[2], 1, 31),
        _parse_field(fields[3], 1, 12),
        _parse_field(fields[4], 0, 7),
    )


def normalize_cron_expression(expression: str) -> str:
    normalized = " ".join(expression.strip().split())
    parse_cron_expression(normalized)
    return normalized


def normalize_timezone(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("timezone is required")
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone is not supported") from exc
    return normalized


def _cron_matches(value: datetime, fields: tuple[frozenset[int], ...]) -> bool:
    minute, hour, day, month, weekday = fields
    if value.minute not in minute or value.hour not in hour or value.month not in month:
        return False
    cron_weekday = (value.weekday() + 1) % 7
    weekday_matches = cron_weekday in weekday or (cron_weekday == 0 and 7 in weekday)
    day_matches = value.day in day
    day_wildcard = len(day) == 31
    weekday_wildcard = len(weekday) == 8
    if day_wildcard and weekday_wildcard:
        return True
    if day_wildcard:
        return weekday_matches
    if weekday_wildcard:
        return day_matches
    return day_matches or weekday_matches


def next_cron_at(expression: str, after: datetime, timezone: str = "UTC") -> datetime:
    fields = parse_cron_expression(expression)
    zone = ZoneInfo(normalize_timezone(timezone))
    reference = after if after.tzinfo else after.replace(tzinfo=UTC)
    candidate = reference.astimezone(zone).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(1_051_200):
        if _cron_matches(candidate, fields):
            return candidate.astimezone(UTC)
        candidate += timedelta(minutes=1)
    raise ValueError("cron expression has no occurrence within two years")


def _require(actor: Actor, action: Literal["read", "write"]) -> None:
    if actor.actor_type != "user":
        raise HTTPException(status_code=403, detail="User authentication is required")
    if capability_allows(actor.capabilities, f"automation:{action}"):
        return
    if action == "write" and actor.role in {"owner", "admin", "member"}:
        return
    raise HTTPException(status_code=403, detail=f"Missing capability: automation:{action}")


def _redact_payload(value: Any) -> Any:
    sensitive = {"authorization", "api_key", "access_token", "refresh_token", "password", "secret", "token"}
    if isinstance(value, dict):
        return {str(key): ("<redacted>" if str(key).lower() in sensitive else _redact_payload(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    return value


def schedule_view(row: dict[str, Any], *, webhook_secret: str | None = None) -> dict[str, Any]:
    result = {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "name": row["name"],
        "trigger_type": row["trigger_type"],
        "cron_expression": row.get("cron_expression"),
        "timezone": row["timezone"],
        "target_type": row["target_type"],
        "target_id": row["target_id"],
        "payload": _redact_payload(row.get("payload") or {}),
        "status": row["status"],
        "enabled": row["enabled"],
        "last_run_at": row.get("last_run_at"),
        "next_run_at": row.get("next_run_at"),
        "version": row.get("version", 1),
        "webhook_endpoint": f"/api/v1/automation-webhooks/{row['id']}" if row["trigger_type"] == "webhook" else None,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
    if webhook_secret:
        result["webhook_secret"] = webhook_secret
    return result


def run_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "schedule_id": row["schedule_id"],
        "workspace_id": row["workspace_id"],
        "trigger_source": row["trigger_source"],
        "idempotency_key": row["idempotency_key"],
        "status": row["status"],
        "payload": _redact_payload(row.get("payload") or {}),
        "operation_id": row.get("operation_id"),
        "error_code": row.get("error_code"),
        "error_message": row.get("error_message"),
        "created_at": row.get("created_at"),
        "completed_at": row.get("completed_at"),
    }


async def _get_schedule(conn, schedule_id: str, workspace_id: str, *, for_update: bool = False) -> dict[str, Any]:
    lock = " FOR UPDATE" if for_update else ""
    result = await conn.execute(
        f"SELECT * FROM ops_automation_schedule WHERE id=%s AND workspace_id=%s AND status <> 'archived'{lock}",
        (schedule_id, workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Automation schedule not found")
    return row


async def _enqueue_run(
    conn,
    schedule: dict[str, Any],
    *,
    payload: dict[str, Any],
    source: Literal["cron", "webhook", "manual"],
    idempotency_key: str,
    triggered_by: str | None,
) -> dict[str, Any]:
    # Existing rows are still allowed to reach the worker so it can record an
    # explicit unsupported/external terminal result instead of pinning Cron.
    target_id = str(schedule.get("target_id") or "").strip()
    safe_payload = _redact_payload(payload)
    input_hash = canonical_hash(safe_payload)
    existing_result = await conn.execute(
        "SELECT * FROM ops_automation_run WHERE schedule_id=%s AND idempotency_key=%s",
        (schedule["id"], idempotency_key),
    )
    existing = await existing_result.fetchone()
    if existing:
        if existing["input_hash"] != input_hash:
            raise HTTPException(status_code=409, detail="Idempotency key was already used with different input")
        return run_view(existing)
    run_id = new_id("autrun")
    result = await conn.execute(
        """
        INSERT INTO ops_automation_run(
          id,schedule_id,workspace_id,trigger_source,idempotency_key,input_hash,payload,triggered_by
        ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s) RETURNING *
        """,
        (run_id, schedule["id"], schedule["workspace_id"], source, idempotency_key, input_hash, json_dumps(safe_payload), triggered_by),
    )
    run = await result.fetchone()
    next_run = None
    if schedule["trigger_type"] == "cron" and schedule["enabled"] and schedule["status"] == "active":
        next_run = next_cron_at(schedule["cron_expression"], datetime.now(UTC), schedule["timezone"])
    await conn.execute(
        "UPDATE ops_automation_schedule SET last_run_at=now(),next_run_at=%s,version=version+1,updated_at=now() WHERE id=%s AND workspace_id=%s",
        (next_run, schedule["id"], schedule["workspace_id"]),
    )
    await conn.execute(
        "INSERT INTO ops_outbox(id,event_type,workspace_id,trace_id,payload) VALUES (%s,%s,%s,%s,%s::jsonb)",
        (new_id("out"), "automation.triggered.v1", schedule["workspace_id"], run_id, json_dumps({"run_id": run_id, "schedule_id": schedule["id"], "target_type": schedule["target_type"], "target_id": target_id, "payload": safe_payload})),
    )
    return run_view(run)


@router.get("")
async def list_schedules(actor: Annotated[Actor, Depends(get_actor)], limit: int = 50):
    _require(actor, "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM ops_automation_schedule WHERE workspace_id=%s AND status <> 'archived' ORDER BY updated_at DESC LIMIT %s",
            (actor.workspace_id, min(max(limit, 1), 200)),
        )
        data = [schedule_view(row) for row in await result.fetchall()]
    # Contract《720》listAutomations: ListQuery -> ListResponse<ScheduleDTO>
    # Backward-compatible envelope: keep legacy ``items`` alongside canonical fields.
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None},
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_schedule(body: ScheduleCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "write")
    cron = normalize_cron_expression(body.cron_expression) if body.cron_expression else None
    timezone = normalize_timezone(body.timezone)
    webhook_secret = secrets.token_urlsafe(32) if body.trigger_type == "webhook" else None
    next_run = next_cron_at(cron, datetime.now(UTC), timezone) if cron and body.enabled else None
    async with pool.connection() as conn:
        async with conn.transaction():
            duplicate = await conn.execute(
                "SELECT 1 FROM ops_automation_schedule WHERE workspace_id=%s AND name=%s AND status <> 'archived'",
                (actor.workspace_id, body.name.strip()),
            )
            if await duplicate.fetchone():
                raise HTTPException(status_code=409, detail="Automation schedule name already exists")
            result = await conn.execute(
                """
                INSERT INTO ops_automation_schedule(
                  id,org_id,workspace_id,name,trigger_type,cron_expression,timezone,target_type,target_id,
                  payload,webhook_secret_hash,enabled,status,next_run_at,created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s) RETURNING *
                """,
                (new_id("auto"), actor.org_id, actor.workspace_id, body.name.strip(), body.trigger_type, cron, timezone, body.target_type, body.target_id.strip(), json_dumps(_redact_payload(body.payload)), hash_secret(webhook_secret) if webhook_secret else None, body.enabled, "active" if body.enabled else "paused", next_run, actor.user_id),
            )
            row = await result.fetchone()
    return schedule_view(row, webhook_secret=webhook_secret)


@router.get("/{schedule_id}")
async def get_schedule(schedule_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "read")
    async with pool.connection() as conn:
        row = await _get_schedule(conn, schedule_id, actor.workspace_id)
    return schedule_view(row)


@router.patch("/{schedule_id}")
async def update_schedule(
    schedule_id: str,
    body: SchedulePatch,
    actor: Annotated[Actor, Depends(get_actor)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
):
    _require(actor, "write")
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="At least one schedule field is required")
    async with pool.connection() as conn:
        async with conn.transaction():
            current = await _get_schedule(conn, schedule_id, actor.workspace_id, for_update=True)
            if if_match is not None and if_match.strip() not in {"*", str(current["version"]), f'W/"{current["version"]}"', f'"{current["version"]}"'}:
                raise HTTPException(status_code=412, detail="Schedule version does not match If-Match")
            cron = normalize_cron_expression(changes.get("cron_expression", current["cron_expression"])) if current["trigger_type"] == "cron" else None
            timezone = normalize_timezone(changes.get("timezone", current["timezone"]))
            enabled = changes.get("enabled", current["enabled"])
            status_value = "active" if enabled else "paused"
            next_run = next_cron_at(cron, datetime.now(UTC), timezone) if cron and enabled else None
            assignments = ["version=version+1", "updated_at=now()", "cron_expression=%s", "timezone=%s", "enabled=%s", "status=%s", "next_run_at=%s"]
            params: list[Any] = [cron, timezone, enabled, status_value, next_run]
            for field in ("name", "target_id"):
                if field in changes:
                    assignments.append(f"{field}=%s")
                    params.append(str(changes[field]).strip())
            if "payload" in changes:
                assignments.append("payload=%s::jsonb")
                params.append(json_dumps(_redact_payload(changes["payload"])))
            params.extend([schedule_id, actor.workspace_id])
            result = await conn.execute(
                f"UPDATE ops_automation_schedule SET {','.join(assignments)} WHERE id=%s AND workspace_id=%s RETURNING *",
                tuple(params),
            )
            row = await result.fetchone()
    return schedule_view(row)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(schedule_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "write")
    async with pool.connection() as conn:
        result = await conn.execute(
            "UPDATE ops_automation_schedule SET status='archived',enabled=FALSE,next_run_at=NULL,version=version+1,updated_at=now() WHERE id=%s AND workspace_id=%s AND status <> 'archived' RETURNING id",
            (schedule_id, actor.workspace_id),
        )
        if not await result.fetchone():
            raise HTTPException(status_code=404, detail="Automation schedule not found")
        await conn.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{schedule_id}/runs")
async def list_runs(schedule_id: str, actor: Annotated[Actor, Depends(get_actor)], limit: int = 50):
    _require(actor, "read")
    async with pool.connection() as conn:
        await _get_schedule(conn, schedule_id, actor.workspace_id)
        result = await conn.execute(
            "SELECT * FROM ops_automation_run WHERE schedule_id=%s AND workspace_id=%s ORDER BY created_at DESC LIMIT %s",
            (schedule_id, actor.workspace_id, min(max(limit, 1), 200)),
        )
        data = [run_view(row) for row in await result.fetchall()]
    # Contract《720》listAutomationRuns: ListQuery -> ListResponse<ScheduleRunDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None},
    }


@router.post("/{schedule_id}/trigger", status_code=status.HTTP_202_ACCEPTED)
async def trigger_schedule(
    schedule_id: str,
    body: TriggerRequest,
    actor: Annotated[Actor, Depends(get_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            schedule = await _get_schedule(conn, schedule_id, actor.workspace_id, for_update=True)
            if not schedule["enabled"] or schedule["status"] != "active":
                raise HTTPException(status_code=409, detail="Automation schedule is disabled")
            run = await _enqueue_run(conn, schedule, payload={**(schedule.get("payload") or {}), **body.payload}, source="manual", idempotency_key=idempotency_key or f"manual:{new_id('idem')}", triggered_by=actor.user_id)
    # Contract《720》triggerAutomation: Empty -> OperationAccepted
    # Keep legacy run/schedule_id fields for backward compatibility.
    return {
        "run": run,
        "schedule_id": schedule_id,
        "operation_id": run.get("operation_id") or run.get("id"),
        "status": run.get("status", "queued"),
        "status_url": f"/api/v1/automations/{schedule_id}/runs",
        "submitted_at": run.get("created_at"),
    }


@webhook_router.post("/{schedule_id}", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(
    schedule_id: str,
    request: Request,
    x_webhook_secret: Annotated[str | None, Header(alias="X-Webhook-Secret")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    if not x_webhook_secret:
        raise HTTPException(status_code=401, detail="X-Webhook-Secret is required")
    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Webhook body must be JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Webhook body must be an object")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                "SELECT * FROM ops_automation_schedule WHERE id=%s AND trigger_type='webhook' AND status <> 'archived'",
                (schedule_id,),
            )
            schedule = await result.fetchone()
            if not schedule or not schedule.get("webhook_secret_hash") or not hmac.compare_digest(hash_secret(x_webhook_secret), schedule["webhook_secret_hash"]):
                raise HTTPException(status_code=404, detail="Automation webhook not found")
            if not schedule["enabled"] or schedule["status"] != "active":
                raise HTTPException(status_code=409, detail="Automation webhook is disabled")
            run = await _enqueue_run(conn, schedule, payload={**(schedule.get("payload") or {}), **payload}, source="webhook", idempotency_key=idempotency_key or f"webhook:{new_id('idem')}", triggered_by=None)
    # Contract《720》receiveAutomationWebhook: WebhookEnvelope -> OperationAccepted
    return {
        "run": run,
        "schedule_id": schedule_id,
        "operation_id": run.get("operation_id") or run.get("id"),
        "status": run.get("status", "queued"),
        "status_url": f"/api/v1/automations/{schedule_id}/runs",
        "submitted_at": run.get("created_at"),
    }


__all__ = [
    "SCHEMA_STATEMENTS",
    "ensure_automation_schema",
    "next_cron_at",
    "normalize_cron_expression",
    "normalize_automation_target_id",
    "parse_cron_expression",
    "router",
    "webhook_router",
]
