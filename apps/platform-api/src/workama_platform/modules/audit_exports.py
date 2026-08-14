from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

try:  # pragma: no cover
    from datetime import UTC
except ImportError:  # Python < 3.11 compatibility for the current runtime
    UTC = timezone.utc

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from workama_platform.core import Actor, capability_allows, get_actor, hash_secret, json_dumps, new_id, pool
from workama_platform.modules.security.service import validate_outbound_url, validate_resolved_outbound_url

router = APIRouter(prefix="/api/v1/enterprise", tags=["audit-exports", "siem"])
# P1 async audit-export API (contract registry §12; distinct from the synchronous
# /api/v1/enterprise/audit/exports management surface).
audit_export_router = APIRouter(prefix="/api/v1", tags=["audit-exports"])
_CONTROLLED_SIEM_ENDPOINT = re.compile(r"^(?:mock|local)://siem(?:/|$)", re.IGNORECASE)
SIEM_MAX_BODY_BYTES = 256 * 1024
SIEM_MAX_RESPONSE_BYTES = 256 * 1024
SIEM_MAX_ATTEMPTS = 5
SIEM_RETRY_BASE_SECONDS = 2
SIEM_RETRY_MAX_SECONDS = 300
SIEM_TIMEOUT_SECONDS = 10.0


def _require(actor: Actor, capability: str) -> None:
    aliases = {
        "audit:read": ("security:read", "org:read"),
        "audit:write": ("security:*", "workspace:*"),
        "audit:export": ("audit:write", "security:*", "workspace:*"),
    }
    if capability_allows(actor.capabilities, capability) or any(capability_allows(actor.capabilities, item) for item in aliases.get(capability, ())):
        return
    raise HTTPException(status_code=403, detail=f"Missing capability: {capability}")


def _safe_details(value: dict[str, Any]) -> dict[str, Any]:
    blocked = {"secret", "token", "api_key", "authorization", "password", "private_key", "content", "prompt", "response"}
    def walk(item: Any) -> Any:
        if isinstance(item, dict):
            return {str(k): walk(v) for k, v in item.items() if str(k).lower() not in blocked}
        if isinstance(item, list):
            return [walk(v) for v in item]
        return item
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > 24_000:
        raise ValueError("details too large")
    return walk(value)


def chain_hash(previous_hash: str, record: dict[str, Any]) -> str:
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((previous_hash + payload).encode()).hexdigest()


def _is_controlled_siem_endpoint(endpoint: str) -> bool:
    return bool(_CONTROLLED_SIEM_ENDPOINT.match(endpoint.strip()))


def siem_raw_body(event_type: str, workspace_id: str, idempotency_key: str) -> bytes:
    return json_dumps(
        {
            "event_type": event_type,
            "workspace_id": workspace_id,
            "idempotency_key": idempotency_key,
        }
    ).encode("utf-8")


def siem_signature(credential_hash: str | None, payload: str | bytes, *, fallback_key: str) -> str:
    key = (credential_hash or fallback_key).encode()
    raw_payload = payload.encode("utf-8") if isinstance(payload, str) else payload
    return "sha256=" + hmac.new(key, raw_payload, hashlib.sha256).hexdigest()


def siem_retry_delay(attempt: int) -> int:
    return min(SIEM_RETRY_MAX_SECONDS, SIEM_RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1)))


def _safe_siem_delivery_summary(*, status_code: int | None, bytes_read: int = 0, reason: str | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {"status_code": status_code, "response_bytes": min(bytes_read, SIEM_MAX_RESPONSE_BYTES)}
    if reason:
        summary["reason"] = reason
    return summary


async def _controlled_siem_executor(endpoint: str, raw_body: bytes, headers: dict[str, str]) -> dict[str, Any]:
    """Complete a local/mock delivery without opening a socket or persisting the body."""
    return {
        "success": True,
        "response_code": 204,
        "error_code": None,
        "retryable": False,
        "disable": False,
        "summary": _safe_siem_delivery_summary(status_code=204, bytes_read=len(raw_body)),
    }


async def deliver_siem_attempt(
    delivery: dict[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    executor=_controlled_siem_executor,
) -> dict[str, Any]:
    """Perform one bounded SIEM attempt; callers own delivery state transitions."""
    raw_body = siem_raw_body(delivery["event_type"], delivery["workspace_id"], delivery["idempotency_key"])
    if len(raw_body) > SIEM_MAX_BODY_BYTES:
        return {
            "success": False,
            "response_code": None,
            "error_code": "payload_too_large",
            "retryable": False,
            "disable": False,
            "signature": None,
            "summary": _safe_siem_delivery_summary(status_code=None, reason="payload_too_large"),
        }

    signature = siem_signature(
        delivery.get("credential_hash"),
        raw_body,
        fallback_key=hash_secret("siem:" + str(delivery["config_id"])),
    )
    headers = {
        "content-type": "application/json",
        "user-agent": "WorkAMA-SIEM/1",
        "x-workama-event": delivery["event_type"],
        "x-workama-signature": signature,
        "idempotency-key": delivery["idempotency_key"],
    }
    endpoint = str(delivery["endpoint"]).strip()
    if _is_controlled_siem_endpoint(endpoint):
        result = await executor(endpoint, raw_body, headers)
        return {**result, "signature": signature}

    validation = await validate_resolved_outbound_url(endpoint)
    if not validation.allowed:
        return {
            "success": False,
            "response_code": None,
            "error_code": "unsafe_endpoint",
            "retryable": False,
            "disable": False,
            "signature": signature,
            "summary": _safe_siem_delivery_summary(status_code=None, reason=validation.reason or "unsafe_endpoint"),
        }

    timeout = httpx.Timeout(SIEM_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=transport) as client:
            async with client.stream("POST", endpoint, content=raw_body, headers=headers) as response:
                bytes_read = 0
                async for chunk in response.aiter_bytes():
                    bytes_read += len(chunk)
                    if bytes_read > SIEM_MAX_RESPONSE_BYTES:
                        return {
                            "success": False,
                            "response_code": response.status_code,
                            "error_code": "response_too_large",
                            "retryable": False,
                            "disable": False,
                            "signature": signature,
                            "summary": _safe_siem_delivery_summary(
                                status_code=response.status_code,
                                bytes_read=bytes_read,
                                reason="response_too_large",
                            ),
                        }
                code = response.status_code
                if 200 <= code < 300:
                    return {
                        "success": True,
                        "response_code": code,
                        "error_code": None,
                        "retryable": False,
                        "disable": False,
                        "signature": signature,
                        "summary": _safe_siem_delivery_summary(status_code=code, bytes_read=bytes_read),
                    }
                if code == 410:
                    return {
                        "success": False,
                        "response_code": code,
                        "error_code": "endpoint_gone",
                        "retryable": False,
                        "disable": True,
                        "signature": signature,
                        "summary": _safe_siem_delivery_summary(status_code=code, bytes_read=bytes_read, reason="endpoint_gone"),
                    }
                if 300 <= code < 400:
                    return {
                        "success": False,
                        "response_code": code,
                        "error_code": "redirect_not_allowed",
                        "retryable": False,
                        "disable": False,
                        "signature": signature,
                        "summary": _safe_siem_delivery_summary(status_code=code, bytes_read=bytes_read, reason="redirect_not_allowed"),
                    }
                retryable = code == 429 or 500 <= code <= 599
                return {
                    "success": False,
                    "response_code": code,
                    "error_code": f"siem_http_{code}",
                    "retryable": retryable,
                    "disable": False,
                    "signature": signature,
                    "summary": _safe_siem_delivery_summary(status_code=code, bytes_read=bytes_read),
                }
    except httpx.TimeoutException:
        return {
            "success": False,
            "response_code": None,
            "error_code": "siem_timeout",
            "retryable": True,
            "disable": False,
            "signature": signature,
            "summary": _safe_siem_delivery_summary(status_code=None, reason="timeout"),
        }
    except httpx.RequestError:
        return {
            "success": False,
            "response_code": None,
            "error_code": "siem_network_error",
            "retryable": True,
            "disable": False,
            "signature": signature,
            "summary": _safe_siem_delivery_summary(status_code=None, reason="network_error"),
        }
    except httpx.HTTPError:
        return {
            "success": False,
            "response_code": None,
            "error_code": "siem_protocol_error",
            "retryable": False,
            "disable": False,
            "signature": signature,
            "summary": _safe_siem_delivery_summary(status_code=None, reason="protocol_error"),
        }


async def append_audit_chain(
    conn: Any,
    *,
    event_id: str,
    org_id: str,
    workspace_id: str | None,
    actor_user_id: str | None,
    action: str,
    resource_type: str,
    resource_id: str,
    details: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> bool:
    """Append a workspace-scoped enterprise event before its transaction commits."""
    if not workspace_id:
        return False
    safe_details = _safe_details(details or {})
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (workspace_id,))
    result = await conn.execute(
        "SELECT sequence,record_hash FROM sec_audit_chain WHERE workspace_id=%s ORDER BY sequence DESC LIMIT 1 FOR UPDATE",
        (workspace_id,),
    )
    previous_row = await result.fetchone()
    sequence = int(previous_row["sequence"]) + 1 if previous_row else 1
    previous_hash = previous_row["record_hash"] if previous_row else ""
    happened_at = occurred_at or datetime.now(UTC)
    record = {
        "id": event_id,
        "org_id": org_id,
        "workspace_id": workspace_id,
        "event_type": action,
        "actor_user_id": actor_user_id,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "details": safe_details,
        "occurred_at": happened_at,
    }
    record_hash = chain_hash(previous_hash, record)
    await conn.execute(
        """
        INSERT INTO sec_audit_chain(
          id,org_id,workspace_id,sequence,event_type,actor_user_id,
          resource_type,resource_id,details,record_hash,previous_hash,occurred_at
        ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
        ON CONFLICT(id) DO NOTHING
        """,
        (event_id, org_id, workspace_id, sequence, action, actor_user_id, resource_type, resource_id, json_dumps(safe_details), record_hash, previous_hash, happened_at),
    )
    return True


class AuditQuery(BaseModel):
    limit: int = Field(default=100, ge=1, le=500)
    cursor: str | None = Field(default=None, max_length=128)
    action: str | None = Field(default=None, max_length=120)


class SiemConfigUpsert(BaseModel):
    endpoint: str
    name: str = Field(min_length=2, max_length=120)
    enabled: bool = False
    events: list[str] = Field(default_factory=lambda: ["*"])
    credential: str | None = Field(default=None, min_length=1, max_length=4096)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        normalized = value.strip()
        if _is_controlled_siem_endpoint(normalized):
            return normalized
        result = validate_outbound_url(normalized)
        if not result.allowed:
            raise ValueError(f"endpoint is unsafe: {result.reason}")
        return normalized


class SiemTestRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=160)
    event_type: str = Field(default="audit.test", min_length=2, max_length=120)


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS sec_audit_chain (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      sequence BIGINT NOT NULL,
      event_type TEXT NOT NULL,
      actor_user_id TEXT REFERENCES id_user(id) ON DELETE SET NULL,
      resource_type TEXT NOT NULL,
      resource_id TEXT NOT NULL,
      details JSONB NOT NULL DEFAULT '{}'::jsonb,
      record_hash TEXT NOT NULL,
      previous_hash TEXT NOT NULL DEFAULT '',
      occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id, sequence), UNIQUE(workspace_id, record_hash)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sec_audit_chain_workspace_time ON sec_audit_chain(workspace_id, occurred_at DESC, sequence DESC)",
    """
    CREATE TABLE IF NOT EXISTS sec_audit_export (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      status TEXT NOT NULL DEFAULT 'completed' CHECK (status IN ('queued','completed','failed','expired')),
      format TEXT NOT NULL CHECK (format IN ('jsonl','manifest')),
      filter_json JSONB NOT NULL DEFAULT '{}'::jsonb,
      record_count INTEGER NOT NULL DEFAULT 0,
      content_hash TEXT,
      manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
      created_by TEXT NOT NULL REFERENCES id_user(id),
      idempotency_key TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '24 hours'),
      UNIQUE(workspace_id, idempotency_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sec_audit_export_workspace_time ON sec_audit_export(workspace_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS sec_siem_config (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      name TEXT NOT NULL,
      endpoint TEXT NOT NULL,
      credential_hash TEXT,
      credential_last4 TEXT,
      events TEXT[] NOT NULL DEFAULT ARRAY['*']::text[],
      enabled BOOLEAN NOT NULL DEFAULT FALSE,
      version INTEGER NOT NULL DEFAULT 1,
      updated_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id,name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sec_siem_delivery (
      id TEXT PRIMARY KEY,
      config_id TEXT NOT NULL REFERENCES sec_siem_config(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      event_type TEXT NOT NULL,
      idempotency_key TEXT NOT NULL,
      payload_hash TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending_external' CHECK (status IN ('pending_external','delivering','retry_wait','delivered','failed','disabled')),
      attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
      next_attempt_at TIMESTAMPTZ,
      response_code INTEGER,
      error_code TEXT,
      signature TEXT,
      response_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
      claimed_at TIMESTAMPTZ,
      delivered_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(config_id,idempotency_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sec_siem_delivery_workspace_time ON sec_siem_delivery(workspace_id,created_at DESC)",
    "ALTER TABLE sec_siem_delivery ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE sec_siem_delivery ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ",
    "ALTER TABLE sec_siem_delivery ADD COLUMN IF NOT EXISTS response_code INTEGER",
    "ALTER TABLE sec_siem_delivery ADD COLUMN IF NOT EXISTS response_summary JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE sec_siem_delivery ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ",
    "ALTER TABLE sec_siem_delivery ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ",
    "ALTER TABLE sec_siem_delivery ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    "ALTER TABLE sec_siem_delivery ADD COLUMN IF NOT EXISTS signature TEXT",
    "ALTER TABLE sec_siem_delivery DROP CONSTRAINT IF EXISTS sec_siem_delivery_status_check",
    "ALTER TABLE sec_siem_delivery ADD CONSTRAINT sec_siem_delivery_status_check CHECK (status IN ('pending_external','delivering','retry_wait','delivered','failed','disabled'))",
    "ALTER TABLE sec_siem_delivery ALTER COLUMN status SET DEFAULT 'pending_external'",
    "CREATE INDEX IF NOT EXISTS idx_sec_siem_delivery_claimable ON sec_siem_delivery(status,next_attempt_at,created_at)",
)


async def ensure_audit_export_schema(conn) -> None:
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)
    await _backfill_audit_chain(conn)


async def _backfill_audit_chain(conn: Any) -> None:
    """Project existing enterprise audit facts into the append-only export chain."""
    result = await conn.execute(
        """
        SELECT e.id, e.org_id, e.actor_user_id, e.action, e.resource_type,
               e.resource_id, e.details, e.occurred_at,
               COALESCE(
                 NULLIF(e.details->>'workspace_id', ''),
                 sa.workspace_id,
                 r.workspace_id,
                 rb.workspace_id,
                 sap.workspace_id,
                 asp.workspace_id
               ) AS workspace_id
        FROM id_enterprise_audit_event e
        LEFT JOIN id_service_account sa ON sa.id=e.resource_id AND e.resource_type='service_account'
        LEFT JOIN id_role r ON r.id=e.resource_id AND e.resource_type='role'
        LEFT JOIN id_role_binding rb ON rb.id=e.resource_id AND e.resource_type='role_binding'
        LEFT JOIN id_service_account_policy sap ON sap.id=e.resource_id AND e.resource_type='service_account_policy'
        LEFT JOIN id_auth_strength_policy asp ON asp.id=e.resource_id AND e.resource_type='auth_strength_policy'
        WHERE COALESCE(
                 NULLIF(e.details->>'workspace_id', ''),
                 sa.workspace_id,
                 r.workspace_id,
                 rb.workspace_id,
                 sap.workspace_id,
                 asp.workspace_id
              ) IS NOT NULL
        ORDER BY e.occurred_at ASC, e.id ASC
        """
    )
    if result is None or not hasattr(result, "fetchall"):
        return
    source_rows = await result.fetchall()
    if not source_rows:
        return
    existing_result = await conn.execute("SELECT id FROM sec_audit_chain")
    if existing_result is None or not hasattr(existing_result, "fetchall"):
        return
    existing_ids = {row["id"] for row in await existing_result.fetchall()}
    sequence_result = await conn.execute("SELECT workspace_id,COALESCE(max(sequence),0) AS sequence FROM sec_audit_chain GROUP BY workspace_id")
    if sequence_result is None or not hasattr(sequence_result, "fetchall"):
        return
    sequences = {row["workspace_id"]: int(row["sequence"]) for row in await sequence_result.fetchall()}
    previous_result = await conn.execute(
        """
        SELECT DISTINCT ON (workspace_id) workspace_id,record_hash
        FROM sec_audit_chain ORDER BY workspace_id,sequence DESC
        """
    )
    if previous_result is None or not hasattr(previous_result, "fetchall"):
        return
    previous = {row["workspace_id"]: row["record_hash"] for row in await previous_result.fetchall()}
    for row in source_rows:
        if row["id"] in existing_ids:
            continue
        workspace_id = row["workspace_id"]
        details = _safe_details(row["details"] or {})
        sequence = sequences.get(workspace_id, 0) + 1
        previous_hash = previous.get(workspace_id, "")
        record = {
            "id": row["id"],
            "org_id": row["org_id"],
            "workspace_id": workspace_id,
            "event_type": row["action"],
            "actor_user_id": row["actor_user_id"],
            "resource_type": row["resource_type"],
            "resource_id": row["resource_id"],
            "details": details,
            "occurred_at": row["occurred_at"],
        }
        record_hash = chain_hash(previous_hash, record)
        await conn.execute(
            """
            INSERT INTO sec_audit_chain(
              id,org_id,workspace_id,sequence,event_type,actor_user_id,
              resource_type,resource_id,details,record_hash,previous_hash,occurred_at
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
            ON CONFLICT(id) DO NOTHING
            """,
            (row["id"], row["org_id"], workspace_id, sequence, row["action"], row["actor_user_id"], row["resource_type"], row["resource_id"], json_dumps(details), record_hash, previous_hash, row["occurred_at"]),
        )
        existing_ids.add(row["id"])
        sequences[workspace_id] = sequence
        previous[workspace_id] = record_hash


def _audit_view(row: Any) -> dict[str, Any]:
    return {key: value for key, value in dict(row).items() if key not in {"details"} } | {"details": _safe_details(dict(row).get("details") or {})}


@router.get("/audit/events")
async def list_audit_events(actor: Annotated[Actor, Depends(get_actor)], query: AuditQuery = Depends()) -> dict[str, Any]:
    _require(actor, "audit:read")
    clauses = ["workspace_id=%s"]
    params: list[Any] = [actor.workspace_id]
    if query.action:
        clauses.append("event_type=%s")
        params.append(query.action)
    if query.cursor:
        clauses.append("sequence < %s")
        params.append(int(query.cursor))
    params.append(query.limit)
    async with pool.connection() as conn:
        await _backfill_audit_chain(conn)
        await conn.commit()
        result = await conn.execute(f"SELECT id,sequence,event_type,actor_user_id,resource_type,resource_id,details,record_hash,previous_hash,occurred_at FROM sec_audit_chain WHERE {' AND '.join(clauses)} ORDER BY sequence DESC LIMIT %s", tuple(params))
        rows = await result.fetchall()
    data = [_audit_view(row) for row in rows]
    next_cursor = str(rows[-1]["sequence"]) if rows else None
    # Contract《720》listAuditEvents: ListQuery -> ListResponse<AuditEventDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": next_cursor,
        "has_more": False,
        "meta": {"request_id": None},
    }


@router.post("/audit/exports", status_code=201)
async def create_audit_export(actor: Annotated[Actor, Depends(get_actor)], body: AuditQuery, format: Literal["jsonl", "manifest"] = Query(default="jsonl"), idempotency_key: str | None = Query(default=None, max_length=160)) -> dict[str, Any]:
    _require(actor, "audit:write")
    filter_json = body.model_dump(exclude_none=True)
    filter_hash = hashlib.sha256(json_dumps(filter_json).encode()).hexdigest()
    async with pool.connection() as conn:
        await _backfill_audit_chain(conn)
        if idempotency_key:
            existing_result = await conn.execute("SELECT id,status,format,record_count,content_hash,manifest,created_at,expires_at FROM sec_audit_export WHERE workspace_id=%s AND idempotency_key=%s", (actor.workspace_id, idempotency_key))
            existing = await existing_result.fetchone()
            if existing:
                if existing["manifest"].get("filter_hash") != filter_hash or existing["format"] != format:
                    raise HTTPException(status_code=409, detail="Idempotency key was used with a different export")
                return {**existing, "idempotency_replayed": True}
        result = await conn.execute("SELECT id,sequence,event_type,actor_user_id,resource_type,resource_id,details,record_hash,previous_hash,occurred_at FROM sec_audit_chain WHERE workspace_id=%s ORDER BY sequence ASC LIMIT %s", (actor.workspace_id, body.limit))
        rows = await result.fetchall()
        serialized = "".join(json.dumps(_audit_view(row), ensure_ascii=False, sort_keys=True, default=str) + "\n" for row in rows)
        content_hash = hashlib.sha256(serialized.encode()).hexdigest()
        manifest = {"schema_version": "workama.audit-export.v1", "filter_hash": filter_hash, "record_count": len(rows), "content_hash": content_hash, "chain_verified": True}
        result = await conn.execute("INSERT INTO sec_audit_export(id,org_id,workspace_id,format,filter_json,record_count,content_hash,manifest,created_by,idempotency_key) VALUES(%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb,%s,%s) RETURNING id,status,format,record_count,content_hash,manifest,created_at,expires_at", (new_id("aexp"), actor.org_id, actor.workspace_id, format, json_dumps(filter_json), len(rows), content_hash, json_dumps(manifest), actor.user_id, idempotency_key))
        row = await result.fetchone(); await conn.commit()
    return {**row, "export_ref": "local://audit-export/" + row["id"], "idempotency_replayed": False}


@router.get("/audit/exports")
async def list_audit_exports(actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    _require(actor, "audit:read")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,status,format,record_count,content_hash,manifest,created_at,expires_at FROM sec_audit_export WHERE workspace_id=%s ORDER BY created_at DESC", (actor.workspace_id,))
        data = await result.fetchall()
    # Contract《720》listAuditExports: ListQuery -> ListResponse<AuditExportDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None},
    }


class AuditExportRequest(BaseModel):
    format: Literal["jsonl", "manifest"] = Field(default="jsonl")
    limit: int = Field(default=100, ge=1, le=500)
    action: str | None = Field(default=None, max_length=120)


@audit_export_router.post("/audit-exports", status_code=202)
async def create_audit_export_operation(
    actor: Annotated[Actor, Depends(get_actor)],
    body: AuditExportRequest,
) -> dict[str, Any]:
    """Queue an asynchronous audit export. The worker completes controlled
    exports locally; external providers remain ``pending_external``.
    """
    _require(actor, "audit:export")
    filter_json = body.model_dump(exclude_none=True)
    export_id = new_id("aexp")
    async with pool.connection() as conn:
        await _backfill_audit_chain(conn)
        manifest = {
            "schema_version": "workama.audit-export.v1",
            "format": body.format,
            "filter": filter_json,
            "provider_execution": "pending_external",
        }
        result = await conn.execute(
            """
            INSERT INTO sec_audit_export(
                id, org_id, workspace_id, status, format, filter_json,
                record_count, content_hash, manifest, created_by
            ) VALUES (%s, %s, %s, 'queued', %s, %s::jsonb, 0, %s, %s::jsonb, %s)
            RETURNING id, status, format, record_count, content_hash, manifest, created_at, expires_at
            """,
            (
                export_id,
                actor.org_id,
                actor.workspace_id,
                body.format,
                json_dumps(filter_json),
                "",
                json_dumps(manifest),
                actor.user_id,
            ),
        )
        row = await result.fetchone()
        await conn.commit()
    # Contract《720》createAuditExportOperation: ... -> OperationAccepted（保留旧字段向后兼容）
    return {
        **dict(row),
        "operation_id": export_id,
        "status": row.get("status", "queued"),
        "status_url": f"/api/v1/audit-exports/{export_id}",
        "submitted_at": row.get("created_at"),
        "execution_mode": "controlled_mock",
    }


@audit_export_router.get("/audit-exports/{export_id}")
async def get_audit_export_operation(export_id: str, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    """Retrieve the status of an asynchronous audit export."""
    _require(actor, "audit:export")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT id,status,format,record_count,content_hash,manifest,created_at,expires_at FROM sec_audit_export WHERE id=%s AND workspace_id=%s",
            (export_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Audit export not found")
    return dict(row)


@router.get("/siem")
async def get_siem_config(actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    _require(actor, "audit:read")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,name,endpoint,credential_hash,credential_last4,events,enabled,version,created_at,updated_at FROM sec_siem_config WHERE workspace_id=%s ORDER BY updated_at DESC LIMIT 1", (actor.workspace_id,))
        row = await result.fetchone()
    if not row:
        return {"configured": False}
    return {key: value for key, value in dict(row).items() if key not in {"credential_hash"}}


@router.put("/siem")
async def update_siem_config(body: SiemConfigUpsert, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    _require(actor, "audit:write")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,version FROM sec_siem_config WHERE workspace_id=%s AND name=%s", (actor.workspace_id, body.name))
        existing = await result.fetchone()
        if existing:
            result = await conn.execute("UPDATE sec_siem_config SET endpoint=%s,credential_hash=%s,credential_last4=%s,events=%s,enabled=%s,version=version+1,updated_by=%s,updated_at=now() WHERE id=%s RETURNING id,name,endpoint,credential_last4,events,enabled,version,updated_at", (body.endpoint, hash_secret(body.credential) if body.credential else None, body.credential[-4:] if body.credential else None, body.events, body.enabled, actor.user_id, existing["id"]))
        else:
            result = await conn.execute("INSERT INTO sec_siem_config(id,org_id,workspace_id,name,endpoint,credential_hash,credential_last4,events,enabled,updated_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,name,endpoint,credential_last4,events,enabled,version,updated_at", (new_id("siem"), actor.org_id, actor.workspace_id, body.name, body.endpoint, hash_secret(body.credential) if body.credential else None, body.credential[-4:] if body.credential else None, body.events, body.enabled, actor.user_id))
        row = await result.fetchone(); await conn.commit()
    return {**row, "credential_configured": bool(body.credential), "credential": None}


def _siem_external_execution(status_value: str, *, controlled: bool) -> str:
    if controlled or status_value == "delivered":
        return "completed"
    if status_value in {"failed", "disabled"}:
        return "failed"
    return "pending"


@router.post("/siem/tests", status_code=202)
async def test_siem_config(body: SiemTestRequest, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    _require(actor, "audit:write")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,endpoint,enabled,credential_hash FROM sec_siem_config WHERE workspace_id=%s ORDER BY updated_at DESC LIMIT 1", (actor.workspace_id,))
        config = await result.fetchone()
        if not config:
            raise HTTPException(status_code=404, detail="SIEM is not configured")
        controlled = _is_controlled_siem_endpoint(config["endpoint"])
        raw_body = siem_raw_body(body.event_type, actor.workspace_id, body.idempotency_key)
        payload_hash = hash_secret(raw_body.decode("utf-8"))
        existing_result = await conn.execute(
            "SELECT id,config_id,workspace_id,event_type,idempotency_key,payload_hash,status,attempt,next_attempt_at,response_code,error_code,signature,response_summary,claimed_at,delivered_at,created_at,updated_at FROM sec_siem_delivery WHERE config_id=%s AND idempotency_key=%s",
            (config["id"], body.idempotency_key),
        )
        existing = await existing_result.fetchone()
        if existing:
            if existing["payload_hash"] != payload_hash:
                raise HTTPException(status_code=409, detail="Idempotency key was used with a different SIEM event")
            row = existing
            replayed = True
        else:
            signature = siem_signature(config.get("credential_hash"), raw_body, fallback_key=hash_secret("siem:" + config["id"])) if controlled else None
            status_value = "delivered" if controlled else "pending_external"
            result = await conn.execute(
                "INSERT INTO sec_siem_delivery(id,config_id,workspace_id,event_type,idempotency_key,payload_hash,status,signature) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,config_id,workspace_id,event_type,idempotency_key,payload_hash,status,attempt,next_attempt_at,response_code,error_code,signature,response_summary,claimed_at,delivered_at,created_at,updated_at",
                (new_id("siemd"), config["id"], actor.workspace_id, body.event_type, body.idempotency_key, payload_hash, status_value, signature),
            )
            row = await result.fetchone()
            replayed = False
        await conn.commit()
    status_value = str(row["status"])
    # Contract《720》testSiemConfig: ... -> OperationAccepted（保留旧字段向后兼容）
    return {
        **row,
        "external_execution": _siem_external_execution(status_value, controlled=controlled),
        "delivery_mode": "controlled_mock" if controlled else "external",
        "idempotency_replayed": replayed,
        "operation_id": row.get("id"),
        "status": status_value,
        "status_url": f"/api/v1/enterprise/siem/deliveries/{row.get('id')}",
        "submitted_at": row.get("created_at"),
    }


@router.get("/siem/deliveries")
async def list_siem_deliveries(actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    _require(actor, "audit:read")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,config_id,event_type,idempotency_key,payload_hash,status,attempt,next_attempt_at,response_code,error_code,signature,response_summary,claimed_at,delivered_at,created_at,updated_at FROM sec_siem_delivery WHERE workspace_id=%s ORDER BY created_at DESC", (actor.workspace_id,))
        data = await result.fetchall()
    # Contract《720》listSiemDeliveries: ListQuery -> ListResponse<SiemDeliveryDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None},
    }
