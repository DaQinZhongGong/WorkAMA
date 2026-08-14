from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from workama_observability import current_trace_id, request_id_var

from workama_platform.core import Actor, get_actor, json_dumps, new_id, pool

EVENT_DOMAINS = {
    "acquisition": ["signup_started", "signup_completed", "onboarding_completed", "onboarding_skipped", "first_chat_sent", "first_api_call"],
    "agent": ["session_created", "session_completed", "session_failed", "tool_called", "approval_requested", "approval_decided", "artifact_created", "artifact_shared", "mcp_server_connected"],
    "gateway": ["channel_created", "channel_tested", "token_created", "sdk_example_copied", "usage_viewed"],
    "platform": ["dataset_created", "dataset_indexed", "connector_created", "connector_sync_completed", "app_created", "app_published", "workflow_run_completed"],
    "commercial": ["paywall_viewed", "plan_checkout_started", "plan_checkout_completed", "quota_blocked"],
    "retention": ["feedback_submitted", "citation_opened", "notification_opened", "account_deletion_requested"],
    "operations": ["global_search_used", "data_export_started", "data_export_completed", "import_dry_run_completed", "notification_delivery_failed", "support_ticket_created", "dynamic_config_changed", "client_diagnostic_created"],
}
EVENT_CATALOG = tuple(name for names in EVENT_DOMAINS.values() for name in names)

_SENSITIVE_PARTS = re.compile(r"(^|_)(authorization|cookie|password|secret|token|api_?key|prompt|response|content|body|attachment|extracted_text)($|_)", re.I)


def content_hash(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
        default=lambda item: item.isoformat() if isinstance(item, datetime) else str(item),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def stable_bucket(flag_key: str, version: int, subject_id: str, salt: str) -> int:
    digest = hashlib.sha256(f"{flag_key}:{version}:{subject_id}:{salt}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 10_000


def validate_flag(flag: dict[str, Any], *, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    flag_type = flag.get("flag_type")
    if flag_type not in {"release", "experiment", "ops", "entitlement", "compliance"}:
        raise ValueError("unsupported flag type")
    if flag_type == "ops" and not str(flag.get("runbook") or "").strip():
        raise ValueError("ops flag requires runbook")
    if flag_type == "experiment":
        if not (flag.get("metrics") or {}).get("primary"):
            raise ValueError("experiment requires primary metrics")
        if not flag.get("ends_at"):
            raise ValueError("experiment requires end date")
    percentage = (flag.get("targeting") or {}).get("percentage", 0)
    if not isinstance(percentage, int) or percentage < 0 or percentage > 10_000:
        raise ValueError("percentage must be between 0 and 10000")
    if flag.get("expires_at") and flag["expires_at"] <= now:
        raise ValueError("expiry must be in the future")


def evaluate_flag(flag: dict[str, Any], subject_id: str, workspace_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    version = flag["version"]
    safe = flag.get("safe_value", flag.get("default_value", False))
    if flag.get("status") != "enabled":
        return {"value": safe, "reason": "disabled", "version": version}
    if flag.get("starts_at") and flag["starts_at"] > now:
        return {"value": safe, "reason": "not_started", "version": version}
    if flag.get("ends_at") and flag["ends_at"] <= now:
        return {"value": safe, "reason": "expired", "version": version}
    targeting = flag.get("targeting") or {}
    if workspace_id in targeting.get("workspace_ids", []):
        return {"value": True, "reason": "workspace_target", "version": version}
    percentage = targeting.get("percentage", 0)
    if percentage and stable_bucket(flag["key"], version, subject_id, flag["salt"]) < percentage:
        return {"value": True, "reason": "percentage", "version": version}
    return {"value": flag.get("default_value", False), "reason": "default", "version": version}


def _contains_sensitive(value: Any, path: str = "") -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            next_path = f"{path}.{key}" if path else str(key)
            if _SENSITIVE_PARTS.search(str(key)):
                return next_path
            found = _contains_sensitive(item, next_path)
            if found:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = _contains_sensitive(item, f"{path}[{index}]")
            if found:
                return found
    return None


def validate_event_properties(properties: dict[str, Any], *, allowed: set[str]) -> None:
    sensitive = _contains_sensitive(properties)
    if sensitive:
        raise ValueError(f"sensitive field is forbidden: {sensitive}")
    unknown = set(properties) - allowed
    if unknown:
        raise ValueError(f"event properties not allowed: {sorted(unknown)}")


def validate_config_value(schema: dict[str, Any], value: Any) -> list[str]:
    errors: list[str] = []
    sensitive = _contains_sensitive(value)
    if sensitive:
        errors.append(f"sensitive field is forbidden: {sensitive}")
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            return errors + ["value must be an object"]
        for required in schema.get("required", []):
            if required not in value:
                errors.append(f"missing required property: {required}")
        allowed = schema.get("properties", {})
        for key in set(value) - set(allowed):
            errors.append(f"unknown property: {key}")
        for key, child in allowed.items():
            if key in value:
                errors.extend(f"{key}: {error}" for error in validate_config_value(child, value[key]))
    elif expected == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append("value must be an integer")
        else:
            if "minimum" in schema and value < schema["minimum"]:
                errors.append(f"value must be >= {schema['minimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                errors.append(f"value must be <= {schema['maximum']}")
    elif expected == "number" and not isinstance(value, (int, float)):
        errors.append("value must be a number")
    elif expected == "boolean" and not isinstance(value, bool):
        errors.append("value must be a boolean")
    elif expected == "string":
        if not isinstance(value, str):
            errors.append("value must be a string")
        elif "enum" in schema and value not in schema["enum"]:
            errors.append("value is not in enum")
    return errors


def resolve_config(versions: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any] | None:
    now = now or datetime.now(UTC)
    for item in sorted(versions, key=lambda row: row["version"], reverse=True):
        if item.get("status") != "enabled":
            continue
        if item.get("effective_at") and item["effective_at"] > now:
            continue
        if item.get("expires_at") and item["expires_at"] <= now:
            continue
        return item
    return None


router = APIRouter(prefix="/api/v1/admin", tags=["operations-governance"])
event_router = APIRouter(prefix="/api/v1", tags=["product-events"])
COMMON_EVENT_PROPERTIES = {
    "source", "surface", "outcome", "model", "provider", "status", "duration_ms",
    "count", "reason_code", "feature", "version", "request_ref", "scope",
}


class FeatureFlagUpsert(BaseModel):
    flag_type: Literal["release", "experiment", "ops", "entitlement", "compliance"]
    default_value: Any = False
    safe_value: Any = False
    targeting: dict[str, Any] = {}
    status: Literal["draft", "enabled", "disabled", "archived"] = "draft"
    owner: str = Field(min_length=1, max_length=120)
    runbook: str | None = Field(default=None, max_length=500)
    metrics: dict[str, Any] = {}
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    expires_at: datetime | None = None


class RollbackRequest(BaseModel):
    target_version: int = Field(ge=1)


class FlagEvaluationRequest(BaseModel):
    subject_id: str = Field(min_length=1, max_length=160)
    workspace_id: str | None = None


class DynamicConfigUpsert(BaseModel):
    schema_version: int = Field(default=1, ge=1)
    value_schema: dict[str, Any]
    config_value: Any
    status: Literal["draft", "enabled", "disabled", "archived"] = "draft"
    risk_level: Literal["normal", "high"] = "normal"
    effective_at: datetime | None = None
    expires_at: datetime | None = None
    approved_by: str | None = None


class ProductEventCreate(BaseModel):
    event_name: str
    client: str = Field(default="web", max_length=40)
    client_version: str | None = Field(default=None, max_length=80)
    locale: str | None = Field(default=None, max_length=20)
    region: str | None = Field(default=None, max_length=40)
    session_ref: str | None = Field(default=None, max_length=160)
    experiment_assignments: dict[str, Any] = {}
    properties: dict[str, Any] = {}


class ReleaseEvidenceCreate(BaseModel):
    release_version: str = Field(min_length=1, max_length=80)
    environment: Literal["dev", "ci", "staging", "preprod", "prod"]
    status: Literal["draft", "verified", "approved", "released", "rolled_back"] = "draft"
    commit_ref: str | None = Field(default=None, max_length=160)
    image_refs: dict[str, str] = {}
    test_summary: dict[str, Any] = {}
    migration_summary: dict[str, Any] = {}
    security_summary: dict[str, Any] = {}
    rollback_summary: dict[str, Any] = {}
    approvals: list[dict[str, Any]] = []


def _require_admin(actor: Actor) -> None:
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")


def _require_owner(actor: Actor) -> None:
    if actor.role != "owner":
        raise HTTPException(status_code=403, detail="Owner role required")


async def ensure_event_catalog(conn) -> None:
    allowed = sorted(COMMON_EVENT_PROPERTIES)
    for domain, names in EVENT_DOMAINS.items():
        for name in names:
            digest = content_hash({"name": name, "domain": domain, "version": 1, "allowed": allowed})
            await conn.execute(
                """
                INSERT INTO ops_event_catalog(event_name, domain, allowed_properties, content_hash)
                VALUES (%s, %s, %s::jsonb, %s)
                ON CONFLICT(event_name) DO UPDATE SET domain = EXCLUDED.domain,
                  allowed_properties = EXCLUDED.allowed_properties, content_hash = EXCLUDED.content_hash,
                  updated_at = now()
                """,
                (name, domain, json_dumps(allowed), digest),
            )


async def _outbox(conn, event_type: str, actor: Actor, payload: dict[str, Any]) -> None:
    await conn.execute(
        """
        INSERT INTO ops_outbox(id, event_type, workspace_id, trace_id, payload)
        VALUES (%s, %s, %s, %s, %s::jsonb)
        """,
        (new_id("out"), event_type, actor.workspace_id, current_trace_id() or request_id_var.get(), json_dumps(payload)),
    )


def _flag_payload(key: str, body: FeatureFlagUpsert) -> dict[str, Any]:
    data = body.model_dump()
    data["key"] = key
    validate_flag(data)
    if body.flag_type in {"entitlement", "compliance"}:
        raise HTTPException(status_code=422, detail="Entitlement and compliance flags require their owning policy service")
    return data


@router.post("/feature-flag-validations")
async def validate_feature_flag(body: FeatureFlagUpsert, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    try:
        validate_flag(body.model_dump())
    except ValueError as exc:
        return {"valid": False, "errors": [str(exc)]}
    if body.flag_type in {"entitlement", "compliance"}:
        return {"valid": False, "errors": ["flag type is managed by its owning policy service"]}
    return {"valid": True, "errors": []}


@router.get("/feature-flags")
async def list_feature_flags(actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT DISTINCT ON (flag_key) id, flag_key AS key, version, flag_type, default_value,
              safe_value, targeting, status, owner, runbook, metrics, starts_at, ends_at,
              expires_at, previous_version, content_hash, created_by, created_at
            FROM ops_feature_flag WHERE workspace_id = %s ORDER BY flag_key, version DESC
            """, (actor.workspace_id,),
        )
        return {"items": await result.fetchall()}


@router.get("/feature-flags/{flag_key}")
async def get_feature_flag(flag_key: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM ops_feature_flag WHERE workspace_id = %s AND flag_key = %s ORDER BY version DESC",
            (actor.workspace_id, flag_key),
        )
        items = await result.fetchall()
    if not items:
        raise HTTPException(status_code=404, detail="Feature flag not found")
    return {"current": items[0], "versions": items}


@router.put("/feature-flags/{flag_key}")
async def update_feature_flag(flag_key: str, body: FeatureFlagUpsert, actor: Annotated[Actor, Depends(get_actor)]):
    _require_owner(actor)
    try:
        data = _flag_payload(flag_key, body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    async with pool.connection() as conn:
        async with conn.transaction():
            latest = await conn.execute(
                "SELECT version FROM ops_feature_flag WHERE workspace_id = %s AND flag_key = %s ORDER BY version DESC LIMIT 1 FOR UPDATE",
                (actor.workspace_id, flag_key),
            )
            latest_row = await latest.fetchone()
            previous = latest_row["version"] if latest_row else 0
            version = previous + 1
            salt = secrets.token_hex(16)
            digest = content_hash({**data, "version": version, "salt": salt})
            result = await conn.execute(
                """
                INSERT INTO ops_feature_flag(
                  id, org_id, workspace_id, flag_key, version, flag_type, default_value,
                  safe_value, targeting, salt, status, owner, runbook, metrics, starts_at,
                  ends_at, expires_at, previous_version, content_hash, created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (new_id("flg"), actor.org_id, actor.workspace_id, flag_key, version, body.flag_type,
                 json_dumps(body.default_value), json_dumps(body.safe_value), json_dumps(body.targeting), salt,
                 body.status, body.owner, body.runbook, json_dumps(body.metrics), body.starts_at, body.ends_at,
                 body.expires_at, previous or None, digest, actor.user_id),
            )
            row = await result.fetchone()
            await _outbox(conn, "feature_flag.changed.v1", actor, {"key": flag_key, "version": version, "scope": actor.workspace_id, "content_hash": digest})
        return row


@router.post("/feature-flags/{flag_key}/rollbacks")
async def rollback_feature_flag(flag_key: str, body: RollbackRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _require_owner(actor)
    async with pool.connection() as conn:
        target_result = await conn.execute(
            "SELECT * FROM ops_feature_flag WHERE workspace_id = %s AND flag_key = %s AND version = %s",
            (actor.workspace_id, flag_key, body.target_version),
        )
        target = await target_result.fetchone()
    if not target:
        raise HTTPException(status_code=404, detail="Target flag version not found")
    upsert = FeatureFlagUpsert(
        flag_type=target["flag_type"], default_value=target["default_value"], safe_value=target["safe_value"],
        targeting=target["targeting"], status=target["status"], owner=target["owner"], runbook=target["runbook"],
        metrics=target["metrics"], starts_at=target["starts_at"], ends_at=target["ends_at"], expires_at=target["expires_at"],
    )
    return await update_feature_flag(flag_key, upsert, actor)


@router.post("/feature-flags/{flag_key}/evaluations")
async def evaluate_feature_flag(flag_key: str, body: FlagEvaluationRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    workspace_id = body.workspace_id or actor.workspace_id
    if workspace_id != actor.workspace_id:
        raise HTTPException(status_code=404, detail="Feature flag not found")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT flag_key AS key, version, status, default_value, safe_value, targeting, salt, starts_at, ends_at FROM ops_feature_flag WHERE workspace_id = %s AND flag_key = %s ORDER BY version DESC LIMIT 1",
            (actor.workspace_id, flag_key),
        )
        flag = await result.fetchone()
    if not flag:
        raise HTTPException(status_code=404, detail="Feature flag not found")
    return evaluate_flag(flag, body.subject_id, workspace_id)


@router.post("/dynamic-config-validations")
async def validate_dynamic_config(body: DynamicConfigUpsert, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    errors = validate_config_value(body.value_schema, body.config_value)
    if body.risk_level == "high" and (not body.approved_by or body.approved_by == actor.user_id):
        errors.append("high-risk config requires a different approving member")
    return {"valid": not errors, "errors": errors}


@router.get("/dynamic-configs")
async def list_dynamic_configs(actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT DISTINCT ON (config_key) * FROM ops_dynamic_config WHERE workspace_id = %s ORDER BY config_key, version DESC",
            (actor.workspace_id,),
        )
        return {"items": await result.fetchall()}


@router.get("/dynamic-configs/{config_key}")
async def get_dynamic_config(config_key: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM ops_dynamic_config WHERE workspace_id = %s AND config_key = %s ORDER BY version DESC",
            (actor.workspace_id, config_key),
        )
        items = await result.fetchall()
    if not items:
        raise HTTPException(status_code=404, detail="Dynamic config not found")
    return {"current": items[0], "versions": items}


@router.get("/dynamic-configs/{config_key}/resolved")
async def resolve_dynamic_config(config_key: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM ops_dynamic_config WHERE workspace_id = %s AND config_key = %s ORDER BY version DESC",
            (actor.workspace_id, config_key),
        )
        resolved = resolve_config(await result.fetchall())
    if not resolved:
        raise HTTPException(status_code=404, detail="No effective dynamic config")
    return {
        "key": config_key, "version": resolved["version"], "value": resolved["config_value"],
        "schema_version": resolved["schema_version"], "content_hash": resolved["content_hash"],
    }


@router.put("/dynamic-configs/{config_key}")
async def update_dynamic_config(config_key: str, body: DynamicConfigUpsert, actor: Annotated[Actor, Depends(get_actor)]):
    _require_owner(actor)
    errors = validate_config_value(body.value_schema, body.config_value)
    if body.risk_level == "high":
        if not body.approved_by or body.approved_by == actor.user_id:
            errors.append("high-risk config requires a different approving member")
        else:
            async with pool.connection() as conn:
                member = await conn.execute(
                    "SELECT 1 FROM id_member WHERE workspace_id = %s AND user_id = %s AND role IN ('owner','admin')",
                    (actor.workspace_id, body.approved_by),
                )
                if not await member.fetchone():
                    errors.append("approver must be an owner or admin in this workspace")
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    async with pool.connection() as conn:
        async with conn.transaction():
            latest = await conn.execute(
                "SELECT version FROM ops_dynamic_config WHERE workspace_id = %s AND config_key = %s ORDER BY version DESC LIMIT 1 FOR UPDATE",
                (actor.workspace_id, config_key),
            )
            latest_row = await latest.fetchone()
            previous = latest_row["version"] if latest_row else 0
            version = previous + 1
            digest = content_hash({"key": config_key, "version": version, **body.model_dump()})
            result = await conn.execute(
                """
                INSERT INTO ops_dynamic_config(
                  id, org_id, workspace_id, config_key, version, schema_version, value_schema,
                  config_value, status, risk_level, effective_at, expires_at, approved_by,
                  previous_version, content_hash, created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
                """,
                (new_id("cfg"), actor.org_id, actor.workspace_id, config_key, version, body.schema_version,
                 json_dumps(body.value_schema), json_dumps(body.config_value), body.status, body.risk_level,
                 body.effective_at, body.expires_at, body.approved_by, previous or None, digest, actor.user_id),
            )
            row = await result.fetchone()
            await _outbox(conn, "config.changed.v1", actor, {"key": config_key, "version": version, "scope": actor.workspace_id, "content_hash": digest})
        return row


@router.post("/dynamic-configs/{config_key}/rollbacks")
async def rollback_dynamic_config(config_key: str, body: RollbackRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _require_owner(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM ops_dynamic_config WHERE workspace_id = %s AND config_key = %s AND version = %s",
            (actor.workspace_id, config_key, body.target_version),
        )
        target = await result.fetchone()
    if not target:
        raise HTTPException(status_code=404, detail="Target config version not found")
    upsert = DynamicConfigUpsert(
        schema_version=target["schema_version"], value_schema=target["value_schema"], config_value=target["config_value"],
        status=target["status"], risk_level=target["risk_level"], effective_at=target["effective_at"],
        expires_at=target["expires_at"], approved_by=target["approved_by"],
    )
    return await update_dynamic_config(config_key, upsert, actor)


@router.get("/event-catalog")
async def list_event_catalog(actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        await ensure_event_catalog(conn)
        result = await conn.execute("SELECT * FROM ops_event_catalog ORDER BY domain, event_name")
        items = await result.fetchall()
        await conn.commit()
    return {"count": len(items), "items": items}


@event_router.post("/events", status_code=202)
async def collect_product_event(body: ProductEventCreate, actor: Annotated[Actor, Depends(get_actor)]):
    if body.event_name not in EVENT_CATALOG:
        raise HTTPException(status_code=422, detail="Unknown product event")
    try:
        validate_event_properties(body.properties, allowed=COMMON_EVENT_PROPERTIES)
        validate_event_properties(body.experiment_assignments, allowed=set(body.experiment_assignments))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    event_id = new_id("pev")
    async with pool.connection() as conn:
        await ensure_event_catalog(conn)
        await conn.execute(
            """
            INSERT INTO ops_product_event(
              id, event_name, user_id, org_id, workspace_id, client, client_version,
              locale, region, session_ref, experiment_assignments, properties
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
            """,
            (event_id, body.event_name, actor.user_id, actor.org_id, actor.workspace_id, body.client,
             body.client_version, body.locale, body.region, body.session_ref,
             json_dumps(body.experiment_assignments), json_dumps(body.properties)),
        )
        await conn.commit()
    return {"id": event_id, "event_name": body.event_name, "accepted": True}


@router.get("/release-evidence")
async def list_release_evidence(actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM ops_release_evidence WHERE workspace_id = %s ORDER BY created_at DESC",
            (actor.workspace_id,),
        )
        return {"items": await result.fetchall()}


@router.post("/release-evidence", status_code=201)
async def create_release_evidence(body: ReleaseEvidenceCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require_owner(actor)
    payload = body.model_dump()
    digest = content_hash(payload)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            INSERT INTO ops_release_evidence(
              id, workspace_id, release_version, environment, status, commit_ref, image_refs,
              test_summary, migration_summary, security_summary, rollback_summary, approvals,
              content_hash, created_by
            ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s)
            ON CONFLICT(workspace_id, release_version, environment) DO UPDATE SET
              status = EXCLUDED.status, commit_ref = EXCLUDED.commit_ref, image_refs = EXCLUDED.image_refs,
              test_summary = EXCLUDED.test_summary, migration_summary = EXCLUDED.migration_summary,
              security_summary = EXCLUDED.security_summary, rollback_summary = EXCLUDED.rollback_summary,
              approvals = EXCLUDED.approvals, content_hash = EXCLUDED.content_hash, updated_at = now()
            RETURNING *
            """,
            (new_id("rel"), actor.workspace_id, body.release_version, body.environment, body.status,
             body.commit_ref, json_dumps(body.image_refs), json_dumps(body.test_summary),
             json_dumps(body.migration_summary), json_dumps(body.security_summary),
             json_dumps(body.rollback_summary), json_dumps(body.approvals), digest, actor.user_id),
        )
        row = await result.fetchone()
        await conn.commit()
    return row
