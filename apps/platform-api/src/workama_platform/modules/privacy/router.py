from __future__ import annotations

import hashlib
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from workama_platform.core import Actor, get_actor, json_dumps, new_id, pool
from workama_platform.modules.privacy.processor import ensure_processing_catalog
from workama_platform.modules.jobs import submit_operation

router = APIRouter(prefix="/api/v1/privacy", tags=["privacy"])
public_router = APIRouter(prefix="/api/v1/public", tags=["public-trust"])


class ConsentDecision(BaseModel):
    policy_type: str
    policy_version: str = "2026-07"
    accepted: bool
    locale: str = "zh-CN"
    display_text: str = Field(default="", max_length=20_000)
    source: str = "web"


class DataRequestCreate(BaseModel):
    request_type: Literal["access", "export", "delete", "correct"]
    scope: Literal["content", "account"] = "content"


@router.get("/processing-activities")
async def processing_activities(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        coverage = await ensure_processing_catalog(conn)
        result = await conn.execute(
            """
            SELECT table_name, classification, purpose, owner, region,
                   retention_days, deletion_behavior, reviewed_at
            FROM id_processing_activity ORDER BY classification DESC, table_name
            """
        )
        items = await result.fetchall()
        await conn.commit()
    return {**coverage, "coverage_percent": 100 if not coverage["missing_tables"] else round(coverage["registered_tables"] * 100 / max(coverage["total_tables"], 1), 2), "items": items}


@router.get("/consents")
async def list_consents(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT policy_type, policy_version, accepted, locale, display_text_hash,
                   source, evidence, withdrawn_at, decided_at
            FROM id_consent WHERE user_id = %s AND workspace_id = %s
            ORDER BY policy_type
            """,
            (actor.user_id, actor.workspace_id),
        )
        return {"items": await result.fetchall()}


@router.put("/consents/{policy_type}")
async def set_consent(
    policy_type: str,
    body: ConsentDecision,
    actor: Annotated[Actor, Depends(get_actor)],
):
    if policy_type != body.policy_type:
        raise HTTPException(status_code=422, detail="Policy type mismatch")
    text_hash = hashlib.sha256(body.display_text.encode()).hexdigest()
    evidence = {"actor": actor.user_id, "workspace": actor.workspace_id, "source": body.source}
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO id_consent(
                id, user_id, workspace_id, policy_type, policy_version, accepted,
                locale, display_text_hash, source, evidence, withdrawn_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, CASE WHEN %s THEN NULL ELSE now() END)
            ON CONFLICT(user_id, workspace_id, policy_type) DO UPDATE SET
                policy_version = EXCLUDED.policy_version, accepted = EXCLUDED.accepted,
                locale = EXCLUDED.locale, display_text_hash = EXCLUDED.display_text_hash,
                source = EXCLUDED.source, evidence = EXCLUDED.evidence,
                withdrawn_at = CASE WHEN EXCLUDED.accepted THEN NULL ELSE now() END,
                decided_at = now()
            """,
            (
                new_id("cns"), actor.user_id, actor.workspace_id, policy_type,
                body.policy_version, body.accepted, body.locale, text_hash, body.source,
                json_dumps(evidence), body.accepted,
            ),
        )
        await conn.commit()
    return {"policy_type": policy_type, "accepted": body.accepted, "display_text_hash": text_hash}


@router.get("/data-requests")
async def list_data_requests(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, request_type, scope, status, result_checksum, exceptions,
                   completed_at, created_at, updated_at
            FROM id_data_request WHERE user_id = %s AND workspace_id = %s
            ORDER BY created_at DESC
            """,
            (actor.user_id, actor.workspace_id),
        )
        return {"items": await result.fetchall()}


@router.post("/data-requests", status_code=202)
async def create_data_request(
    body: DataRequestCreate, actor: Annotated[Actor, Depends(get_actor)]
):
    if body.scope == "account":
        raise HTTPException(status_code=422, detail="Account deletion is not available in the P0 privacy baseline")
    request_id = new_id("dsr")
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO id_data_request(id, user_id, workspace_id, request_type, scope) VALUES (%s, %s, %s, %s, %s)",
                (request_id, actor.user_id, actor.workspace_id, body.request_type, body.scope),
            )
            operation = await submit_operation(
                conn, operation_type="privacy.data_request", workspace_id=actor.workspace_id,
                org_id=actor.org_id, actor_id=actor.user_id, actor_role=actor.role,
                idempotency_key=request_id, payload={"request_id": request_id},
                job_type="privacy.data_request.process", max_attempts=3,
            )
    return {"id": request_id, "operation_id": operation["id"], "status": "requested", "request_type": body.request_type, "scope": body.scope}


@router.get("/data-requests/{request_id}")
async def get_data_request(
    request_id: str, actor: Annotated[Actor, Depends(get_actor)]
):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, request_type, scope, status, identity_verified_at,
                   result_manifest, result_checksum, exceptions, completed_at,
                   created_at, updated_at
            FROM id_data_request WHERE id = %s AND user_id = %s AND workspace_id = %s
            """,
            (request_id, actor.user_id, actor.workspace_id),
        )
        request = await result.fetchone()
        if not request:
            raise HTTPException(status_code=404, detail="Data request not found")
        steps = await conn.execute(
            """
            SELECT step_name, status, resource_count, action, checksum, error,
                   started_at, completed_at
            FROM id_data_request_step WHERE request_id = %s ORDER BY started_at, step_name
            """,
            (request_id,),
        )
        request["steps"] = await steps.fetchall()
        return request


@router.get("/deletion-tombstones")
async def deletion_tombstones(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, request_id, scope, replay_version, resource_counts, created_at
            FROM id_deletion_tombstone WHERE user_id = %s AND workspace_id = %s
            ORDER BY created_at DESC
            """,
            (actor.user_id, actor.workspace_id),
        )
        return {"items": await result.fetchall()}


async def _service_status(client: httpx.AsyncClient, name: str, url: str) -> dict:
    try:
        response = await client.get(url)
        return {"name": name, "status": "operational" if response.is_success else "degraded"}
    except httpx.HTTPError:
        return {"name": name, "status": "unavailable"}


@public_router.get("/platform-info")
async def platform_info():
    async with pool.connection() as conn:
        coverage = await ensure_processing_catalog(conn)
        await conn.commit()
    async with httpx.AsyncClient(timeout=2) as client:
        services = [
            await _service_status(client, "Platform API", "http://localhost:8000/healthz"),
            await _service_status(client, "Gateway", "http://gateway:8080/healthz"),
            await _service_status(client, "Agent Server", "http://agent-server:8001/healthz"),
        ]
    return {
        "services": services,
        "overall_status": "operational" if all(item["status"] == "operational" for item in services) else "degraded",
        "privacy": {
            "classification_coverage_percent": 100 if not coverage["missing_tables"] else round(coverage["registered_tables"] * 100 / max(coverage["total_tables"], 1), 2),
            "registered_tables": coverage["registered_tables"],
            "data_classes": ["C0", "C1", "C2", "C3", "C4"],
            "customer_content_training": False,
        },
        "controls": [
            "Tenant isolation and role-based access",
            "Hashed API keys and encrypted provider credentials",
            "Content moderation and SSRF protection",
            "DSAR workflow and deletion tombstones",
        ],
        "support": {"path": "/help", "required_context": "request_id", "secret_warning": True},
    }
