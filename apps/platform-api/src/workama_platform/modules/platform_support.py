from __future__ import annotations

import hashlib
import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from workama_platform.core import Actor, get_actor, json_dumps, new_id, pool
from workama_platform.modules.jobs import submit_operation
from workama_platform.modules.portability import _s3
from workama_platform.object_store import delete_object

router = APIRouter(prefix="/api/v1/admin", tags=["platform-support"])
PLACEHOLDER = re.compile(r"{{\s*([a-zA-Z][a-zA-Z0-9_]*)\s*}}")


def validate_template(subject: str, body: str, schema: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if schema.get("type") != "object": errors.append("variables_schema type must be object")
    properties = set((schema.get("properties") or {}).keys())
    used = set(PLACEHOLDER.findall(subject + "\n" + body))
    unknown = used - properties
    if unknown: errors.append(f"unknown template variables: {sorted(unknown)}")
    unused_required = set(schema.get("required") or []) - used
    if unused_required: errors.append(f"required variables are not rendered: {sorted(unused_required)}")
    if "{{{" in subject + body or "|safe" in subject + body: errors.append("unsafe template syntax is forbidden")
    return errors


def render_template(value: str, variables: dict[str, Any], schema: dict[str, Any]) -> str:
    missing = set(schema.get("required") or []) - set(variables)
    unknown = set(variables) - set((schema.get("properties") or {}).keys())
    if missing or unknown: raise ValueError(f"template variables invalid: missing={sorted(missing)}, unknown={sorted(unknown)}")
    return PLACEHOLDER.sub(lambda match: str(variables.get(match.group(1), "")), value)


class TemplateUpsert(BaseModel):
    locale: str = "zh-CN"
    channel: Literal["in_app", "email", "webhook"]
    subject_template: str = Field(min_length=1, max_length=500)
    body_template: str = Field(min_length=1, max_length=10_000)
    variables_schema: dict[str, Any]
    sensitive_level: Literal["C1", "C2", "C3"] = "C2"
    status: Literal["draft", "published", "retired"] = "draft"


class TemplateTest(BaseModel):
    variables: dict[str, Any] = Field(default_factory=dict)


class LifecyclePolicyUpsert(BaseModel):
    retention_days: int = Field(ge=1, le=3650)
    batch_size: int = Field(default=100, ge=1, le=1000)
    status: Literal["enabled", "disabled"] = "enabled"
    runbook: str = Field(min_length=3, max_length=1000)


class LifecycleRunRequest(BaseModel):
    resource_type: Literal["notification", "workspace_export", "artifact", "attachment"]
    dry_run: bool = True


def _require_admin(actor: Actor) -> None:
    if actor.role not in {"owner", "admin"}: raise HTTPException(status_code=403, detail="Admin role required")


@router.get("/notification-templates")
async def list_templates(actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM ops_notification_template WHERE workspace_id=%s ORDER BY template_id,version DESC", (actor.workspace_id,))
        data = await result.fetchall()
    # Contract 720 listNotificationTemplates ListQuery -> ListResponse<NotificationTemplateDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.get("/notification-templates/{template_id}")
async def get_template(template_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM ops_notification_template WHERE workspace_id=%s AND template_id=%s ORDER BY version DESC", (actor.workspace_id, template_id)); items = await result.fetchall()
    if not items: raise HTTPException(status_code=404, detail="Template not found")
    return {"current": items[0], "versions": items}


@router.post("/notification-template-validations")
async def validate_notification_template(body: TemplateUpsert, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor); errors = validate_template(body.subject_template, body.body_template, body.variables_schema)
    return {"valid": not errors, "errors": errors, "variables": sorted(set(PLACEHOLDER.findall(body.subject_template + body.body_template)))}


@router.put("/notification-templates/{template_id}")
async def update_template(template_id: str, body: TemplateUpsert, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor); errors = validate_template(body.subject_template, body.body_template, body.variables_schema)
    if errors: raise HTTPException(status_code=422, detail=errors)
    digest = hashlib.sha256(json_dumps(body.model_dump()).encode()).hexdigest()
    async with pool.connection() as conn:
        async with conn.transaction():
            latest = await conn.execute("SELECT COALESCE(max(version),0)+1 version FROM ops_notification_template WHERE workspace_id=%s AND template_id=%s AND locale=%s AND channel=%s", (actor.workspace_id, template_id, body.locale, body.channel)); version = (await latest.fetchone())["version"]
            result = await conn.execute("""INSERT INTO ops_notification_template(id,workspace_id,template_id,version,locale,channel,subject_template,body_template,variables_schema,sensitive_level,status,content_hash,created_by,published_at)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,CASE WHEN %s='published' THEN now() END) RETURNING *""",
              (new_id("tpl"), actor.workspace_id, template_id, version, body.locale, body.channel, body.subject_template, body.body_template, json_dumps(body.variables_schema), body.sensitive_level, body.status, digest, actor.user_id, body.status)); return await result.fetchone()


@router.post("/notification-templates/{template_id}/tests", status_code=201)
async def test_template(template_id: str, body: TemplateTest, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM ops_notification_template WHERE workspace_id=%s AND template_id=%s AND status='published' ORDER BY version DESC LIMIT 1", (actor.workspace_id, template_id)); template = await result.fetchone()
        if not template: raise HTTPException(status_code=404, detail="Published template not found")
        try: title = render_template(template["subject_template"], body.variables, template["variables_schema"]); summary = render_template(template["body_template"], body.variables, template["variables_schema"])
        except ValueError as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc
        notification_id = new_id("ntf")
        await conn.execute("INSERT INTO id_notification(id,user_id,workspace_id,event_type,priority,title,summary,payload_min,dedupe_key,expires_at) VALUES (%s,%s,%s,%s,'normal',%s,%s,%s::jsonb,%s,now()+interval '7 days')", (notification_id, actor.user_id, actor.workspace_id, f"template.test.{template_id}", title, summary, json_dumps({"template_id": template_id, "version": template["version"]}), f"template-test:{template_id}:{new_id('dedupe')}"))
        if template["channel"] == "email": await conn.execute("INSERT INTO id_notification_delivery(id,notification_id,channel,status) VALUES (%s,%s,'email','pending')", (new_id("dlv"), notification_id))
        await conn.commit()
    return {"notification_id": notification_id, "title": title, "summary": summary, "channel": template["channel"]}


@router.get("/lifecycle-policies")
async def list_lifecycle_policies(actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM ops_lifecycle_policy WHERE workspace_id=%s ORDER BY resource_type", (actor.workspace_id,))
        data = await result.fetchall()
    # Contract 720 listLifecyclePolicies ListQuery -> ListResponse<LifecyclePolicyDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.put("/lifecycle-policies/{resource_type}")
async def upsert_lifecycle_policy(resource_type: Literal["notification", "workspace_export", "artifact", "attachment"], body: LifecyclePolicyUpsert, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("""INSERT INTO ops_lifecycle_policy(id,workspace_id,resource_type,retention_days,batch_size,status,runbook,updated_by)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(workspace_id,resource_type) DO UPDATE SET retention_days=EXCLUDED.retention_days,batch_size=EXCLUDED.batch_size,status=EXCLUDED.status,runbook=EXCLUDED.runbook,updated_by=EXCLUDED.updated_by,updated_at=now() RETURNING *""", (new_id("lcp"), actor.workspace_id, resource_type, body.retention_days, body.batch_size, body.status, body.runbook, actor.user_id)); await conn.commit(); return await result.fetchone()


@router.post("/lifecycle-runs", status_code=202)
async def create_lifecycle_run(body: LifecycleRunRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor); run_id = new_id("lcr")
    async with pool.connection() as conn:
        async with conn.transaction():
            policy = await conn.execute("SELECT 1 FROM ops_lifecycle_policy WHERE workspace_id=%s AND resource_type=%s AND status='enabled'", (actor.workspace_id, body.resource_type))
            if not await policy.fetchone(): raise HTTPException(status_code=409, detail="Enabled lifecycle policy required")
            operation = await submit_operation(conn, operation_type="lifecycle.run", workspace_id=actor.workspace_id, org_id=actor.org_id, actor_id=actor.user_id, actor_role=actor.role, idempotency_key=run_id, payload={"run_id": run_id}, job_type="lifecycle.run", max_attempts=3)
            await conn.execute("INSERT INTO ops_lifecycle_run(id,operation_id,workspace_id,resource_type,dry_run,created_by) VALUES (%s,%s,%s,%s,%s,%s)", (run_id, operation["id"], actor.workspace_id, body.resource_type, body.dry_run, actor.user_id))
    return {"id": run_id, "operation_id": operation["id"], "status": "queued"}


@router.get("/lifecycle-runs")
async def list_lifecycle_runs(actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM ops_lifecycle_run WHERE workspace_id=%s ORDER BY created_at DESC LIMIT 100", (actor.workspace_id,))
        data = await result.fetchall()
    # Contract 720 listLifecycleRuns ListQuery -> ListResponse<LifecycleRunDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


async def execute_lifecycle_run(conn, run_id: str, workspace_id: str) -> dict[str, Any]:
    result = await conn.execute("""SELECT r.*,p.retention_days,p.batch_size FROM ops_lifecycle_run r JOIN ops_lifecycle_policy p ON p.workspace_id=r.workspace_id AND p.resource_type=r.resource_type WHERE r.id=%s AND r.workspace_id=%s FOR UPDATE OF r""", (run_id, workspace_id)); run = await result.fetchone()
    if not run: raise ValueError("lifecycle run not found")
    hold = await conn.execute("SELECT count(*) count FROM sec_legal_hold WHERE workspace_id=%s AND resource_type IN (%s,'workspace') AND status='active' AND (expires_at IS NULL OR expires_at>now())", (workspace_id, run["resource_type"])); hold_count = (await hold.fetchone())["count"]
    if run["resource_type"] == "notification":
        rows = await conn.execute("SELECT id FROM id_notification WHERE workspace_id=%s AND (expires_at<now() OR archived_at<now()-make_interval(days=>%s)) ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED", (workspace_id, run["retention_days"], run["batch_size"])); items = await rows.fetchall()
        processed = 0
        if not run["dry_run"] and not hold_count:
            for item in items: await conn.execute("DELETE FROM id_notification WHERE id=%s", (item["id"],)); processed += 1
    elif run["resource_type"] == "workspace_export":
        rows = await conn.execute("SELECT id,object_ref FROM ops_workspace_export WHERE workspace_id=%s AND expires_at<now() AND object_ref IS NOT NULL ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED", (workspace_id, run["batch_size"])); items = await rows.fetchall(); processed = 0
        if not run["dry_run"] and not hold_count:
            for item in items:
                response = await _s3("DELETE", item["object_ref"])
                if response.status_code not in {200, 204}: raise RuntimeError(f"object delete failed: {response.status_code}")
                await conn.execute("UPDATE ops_workspace_export SET status='expired',object_ref=NULL WHERE id=%s", (item["id"],)); processed += 1
    elif run["resource_type"] == "artifact":
        rows = await conn.execute("SELECT id,s3_key FROM ag_artifact WHERE workspace_id=%s AND deleted_at IS NOT NULL AND (purge_after<now() OR deleted_at<now()-make_interval(days=>%s)) ORDER BY deleted_at LIMIT %s FOR UPDATE SKIP LOCKED", (workspace_id, run["retention_days"], run["batch_size"])); items = await rows.fetchall(); processed = 0
        if not run["dry_run"] and not hold_count:
            for item in items:
                if item["s3_key"]: await delete_object("workama-artifacts", item["s3_key"])
                await conn.execute("DELETE FROM ag_artifact WHERE id=%s", (item["id"],)); processed += 1
    else:
        rows = await conn.execute("SELECT id,s3_key FROM ag_attachment WHERE workspace_id=%s AND (expires_at<now() OR created_at<now()-make_interval(days=>%s)) ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED", (workspace_id,run["retention_days"],run["batch_size"])); items=await rows.fetchall(); processed=0
        if not run["dry_run"] and not hold_count:
            for item in items:
                if item["s3_key"]: await delete_object("workama-attachments",item["s3_key"])
                await conn.execute("DELETE FROM ag_attachment WHERE id=%s",(item["id"],)); processed+=1
    verification = {"policy_rechecked": True, "legal_hold_checked": True, "reference_check": "not_applicable", "eligible_ids_hash": hashlib.sha256("|".join(item["id"] for item in items).encode()).hexdigest(), "dry_run": run["dry_run"]}
    await conn.execute("UPDATE ops_lifecycle_run SET status='completed',eligible_count=%s,processed_count=%s,skipped_hold_count=%s,verification=%s::jsonb,completed_at=now() WHERE id=%s", (len(items), processed, len(items) if hold_count else 0, json_dumps(verification), run_id))
    return {"status": "succeeded", "eligible": len(items), "processed": processed, "skipped_hold": len(items) if hold_count else 0, "verification": verification}
