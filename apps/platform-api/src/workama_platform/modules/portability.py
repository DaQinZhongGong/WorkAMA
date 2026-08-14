from __future__ import annotations

import asyncio
import hashlib
import json
import hmac
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import quote, urlparse

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from workama_platform.core import Actor, get_actor, json_dumps, new_id, pool, settings
from workama_platform.modules.jobs import submit_operation
from workama_platform.object_store import get_object as get_stored_object, put_object as put_stored_object

router = APIRouter(prefix="/api/v1", tags=["workspace-portability"])
BUCKET = "workama-portability"


def _signing_key(secret: str, date: str, region: str = "us-east-1") -> bytes:
    key = hmac.new(("AWS4" + secret).encode(), date.encode(), hashlib.sha256).digest()
    key = hmac.new(key, region.encode(), hashlib.sha256).digest()
    key = hmac.new(key, b"s3", hashlib.sha256).digest()
    return hmac.new(key, b"aws4_request", hashlib.sha256).digest()


async def _s3(method: str, key: str | None = None, data: bytes = b"") -> httpx.Response:
    endpoint = settings.minio_endpoint if "://" in settings.minio_endpoint else f"http://{settings.minio_endpoint}"
    parsed = urlparse(endpoint)
    path = f"/{BUCKET}" + (f"/{quote(key, safe='/')}" if key else "")
    now = datetime.now(UTC); amz_date = now.strftime("%Y%m%dT%H%M%SZ"); date = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(data).hexdigest()
    canonical_headers = f"host:{parsed.netloc}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical = f"{method}\n{path}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    scope = f"{date}/us-east-1/s3/aws4_request"
    string_to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    signature = hmac.new(_signing_key(settings.minio_secret_key, date), string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers = {
        "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash,
        "Authorization": f"AWS4-HMAC-SHA256 Credential={settings.minio_access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        return await client.request(method, f"{parsed.scheme}://{parsed.netloc}{path}", headers=headers, content=data)


async def put_object(key: str, data: bytes) -> None:
    bucket = await _s3("PUT")
    if bucket.status_code not in {200, 409}:
        raise RuntimeError(f"object bucket unavailable: {bucket.status_code}")
    response = await _s3("PUT", key, data)
    response.raise_for_status()


async def get_object(key: str) -> bytes:
    response = await _s3("GET", key)
    response.raise_for_status()
    return response.content


def canonical_package(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()


def validate_package(payload: dict) -> list[str]:
    errors: list[str] = []
    manifest = payload.get("manifest")
    resources = payload.get("resources")
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != 1:
        errors.append("unsupported manifest_version")
    if not isinstance(resources, dict):
        errors.append("resources must be an object")
        return errors
    allowed = {"sessions", "artifacts", "channels", "dynamic_configs", "feature_flags"}
    unknown = set(resources) - allowed
    if unknown:
        errors.append(f"unsupported resource types: {sorted(unknown)}")
    for channel in resources.get("channels", []):
        if "credential_enc" in channel or "credential" in channel:
            errors.append("channel credentials are forbidden")
    expected = manifest.get("resource_counts", {}) if isinstance(manifest, dict) else {}
    actual = {name: len(items) for name, items in resources.items() if isinstance(items, list)}
    if expected != actual:
        errors.append("resource_counts mismatch")
    return errors


async def build_workspace_export(conn, export_id: str, workspace_id: str, actor_id: str) -> dict:
    queries = {
        "sessions": "SELECT id,title,model,status,created_at,updated_at FROM ag_session WHERE workspace_id=%s ORDER BY id",
        "artifacts": "SELECT a.id,a.session_id,a.name,a.content_type,a.content,a.s3_key,a.created_at FROM ag_artifact a WHERE a.workspace_id=%s AND a.deleted_at IS NULL ORDER BY a.id",
        "channels": "SELECT id,name,provider,base_url,models,weight,status,created_at,updated_at FROM gw_channel WHERE workspace_id=%s ORDER BY id",
        "dynamic_configs": "SELECT config_key,version,schema_version,value_schema,config_value,status,risk_level,effective_at,expires_at,content_hash FROM ops_dynamic_config WHERE workspace_id=%s ORDER BY config_key,version",
        "feature_flags": "SELECT flag_key,version,flag_type,default_value,safe_value,targeting,status,owner,runbook,metrics,content_hash FROM ops_feature_flag WHERE workspace_id=%s ORDER BY flag_key,version",
    }
    resources: dict[str, list] = {}
    for name, query in queries.items():
        result = await conn.execute(query, (workspace_id,))
        resources[name] = await result.fetchall()
    for artifact in resources.get("artifacts", []):
        if artifact.get("s3_key"):
            artifact["content"] = (await get_stored_object("workama-artifacts", artifact["s3_key"])).decode("utf-8", errors="replace")
        artifact.pop("s3_key", None)
    package = {
        "manifest": {
            "manifest_version": 1, "product_version": "0.1.0", "schema_version": 1,
            "source_region": "local", "tenant": {"workspace_id": workspace_id},
            "created_at": datetime.now(UTC).isoformat(), "created_by": actor_id,
            "encryption": {"mode": "at_rest"},
            "resource_counts": {name: len(items) for name, items in resources.items()},
            "dependencies": ["sessions -> artifacts"],
            "warnings": ["channel credentials are excluded and must be reconfigured"],
        },
        "resources": resources,
    }
    resource_data = canonical_package(resources)
    package["manifest"]["files"] = [{"path": "resources.json", "type": "application/json", "count": sum(len(v) for v in resources.values()), "size": len(resource_data), "sha256": hashlib.sha256(resource_data).hexdigest()}]
    data = canonical_package(package)
    checksum = hashlib.sha256(data).hexdigest()
    key = f"exports/{workspace_id}/{export_id}/workspace.json"
    await put_object(key, data)
    await conn.execute("UPDATE ops_workspace_export SET status='completed',manifest=%s::jsonb,object_ref=%s,checksum=%s,size_bytes=%s,expires_at=now()+interval '24 hours',completed_at=now() WHERE id=%s", (json_dumps(package["manifest"]), key, checksum, len(data), export_id))
    return {"export_id": export_id, "checksum": checksum, "size_bytes": len(data), "resource_counts": package["manifest"]["resource_counts"]}


async def dry_run_import(conn, import_id: str, workspace_id: str) -> dict:
    result = await conn.execute("SELECT object_ref,upload_checksum FROM ops_workspace_import WHERE id=%s AND workspace_id=%s", (import_id, workspace_id))
    row = await result.fetchone()
    if not row or not row["object_ref"]:
        raise ValueError("import upload is incomplete")
    data = await get_object(row["object_ref"])
    if hashlib.sha256(data).hexdigest() != row["upload_checksum"]:
        raise ValueError("upload checksum mismatch")
    payload = json.loads(data)
    errors = validate_package(payload)
    resources = payload.get("resources", {})
    conflicts: dict[str, int] = {}
    for name, table in {"sessions": "ag_session", "artifacts": "ag_artifact", "channels": "gw_channel"}.items():
        ids = [item.get("id") for item in resources.get(name, []) if item.get("id")]
        if not ids:
            conflicts[name] = 0
            continue
        found = await conn.execute(f"SELECT count(*) count FROM {table} WHERE workspace_id=%s AND id=ANY(%s)", (workspace_id, ids))
        conflicts[name] = (await found.fetchone())["count"]
    report = {"valid": not errors, "errors": errors, "resource_counts": payload.get("manifest", {}).get("resource_counts", {}), "conflicts": conflicts, "credential_reconfiguration": len(resources.get("channels", [])), "strategies": {"sessions": "create_new", "artifacts": "create_new", "channels": "create_new", "dynamic_configs": "merge_safe", "feature_flags": "merge_safe"}}
    await conn.execute("UPDATE ops_workspace_import SET status=%s,manifest=%s::jsonb,dry_run_report=%s::jsonb WHERE id=%s", ("dry_run_ready" if not errors else "invalid", json_dumps(payload.get("manifest", {})), json_dumps(report), import_id))
    return report


async def apply_import(conn, import_id: str, workspace_id: str) -> dict:
    result = await conn.execute("SELECT object_ref,status FROM ops_workspace_import WHERE id=%s AND workspace_id=%s FOR UPDATE", (import_id, workspace_id))
    row = await result.fetchone()
    if not row or row["status"] != "dry_run_ready":
        raise ValueError("successful dry-run is required")
    payload = json.loads(await get_object(row["object_ref"]))
    resources = payload["resources"]
    mapping: dict[str, str] = {}
    counts = {"sessions": 0, "artifacts": 0, "channels": 0}
    for item in resources.get("sessions", []):
        new = new_id("sess"); mapping[item["id"]] = new
        await conn.execute("""INSERT INTO ag_session(id,workspace_id,user_id,title,model,status)
          SELECT %s,%s,m.user_id,%s,%s,'idle' FROM id_member m
          WHERE m.workspace_id=%s AND m.role='owner' ORDER BY m.created_at LIMIT 1""",
          (new, workspace_id, item["title"], item["model"], workspace_id)); counts["sessions"] += 1
    for item in resources.get("artifacts", []):
        session_id = mapping.get(item["session_id"])
        if not session_id: continue
        new = new_id("art"); mapping[item["id"]] = new
        content = str(item.get("content", "")).encode(); digest = hashlib.sha256(content).hexdigest(); key = f"artifacts/{workspace_id}/{new}/v1/{item['name']}"
        await put_stored_object("workama-artifacts", key, content)
        await conn.execute("INSERT INTO ag_artifact(id,session_id,workspace_id,name,content_type,content,s3_key,size_bytes,content_sha256,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'ready')", (new, session_id, workspace_id, item["name"], item["content_type"], "", key, len(content), digest)); counts["artifacts"] += 1
    for item in resources.get("channels", []):
        new = new_id("chn"); mapping[item["id"]] = new
        await conn.execute("INSERT INTO gw_channel(id,workspace_id,name,provider,base_url,credential_enc,models,weight,status,last_health) VALUES (%s,%s,%s,%s,%s,NULL,%s,%s,'disabled','credential_required')", (new, workspace_id, item["name"] + " (imported)", item["provider"], item["base_url"], item["models"], item["weight"])); counts["channels"] += 1
    skipped = {"dynamic_configs": len(resources.get("dynamic_configs", [])), "feature_flags": len(resources.get("feature_flags", []))}
    summary = {"status": "partially_succeeded" if any(skipped.values()) else "succeeded", "created": counts, "skipped": skipped, "credential_reconfiguration": counts["channels"]}
    await conn.execute("UPDATE ops_workspace_import SET status='completed',id_mapping=%s::jsonb,result_summary=%s::jsonb,completed_at=now() WHERE id=%s", (json_dumps(mapping), json_dumps(summary), import_id))
    return summary


class ExportRequest(BaseModel):
    include_history: bool = True


class UploadCompleteRequest(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ImportApplyRequest(BaseModel):
    confirm: Literal[True]


def _require_admin(actor: Actor) -> None:
    if actor.role not in {"owner", "admin"}: raise HTTPException(status_code=403, detail="Admin role required")


@router.post("/workspaces/{workspace_id}/exports", status_code=202)
async def create_export(workspace_id: str, body: ExportRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    if workspace_id != actor.workspace_id: raise HTTPException(status_code=404, detail="Workspace not found")
    export_id = new_id("exp")
    async with pool.connection() as conn:
        async with conn.transaction():
            operation = await submit_operation(conn, operation_type="workspace.export", workspace_id=workspace_id, org_id=actor.org_id, actor_id=actor.user_id, actor_role=actor.role, idempotency_key=export_id, payload={"export_id": export_id}, job_type="workspace.export", max_attempts=3)
            await conn.execute("INSERT INTO ops_workspace_export(id,operation_id,workspace_id,created_by) VALUES (%s,%s,%s,%s)", (export_id, operation["id"], workspace_id, actor.user_id))
    return {"id": export_id, "operation_id": operation["id"], "status": "queued"}


@router.get("/workspace-exports/{export_id}")
async def get_export(export_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM ops_workspace_export WHERE id=%s AND workspace_id=%s", (export_id, actor.workspace_id)); row = await result.fetchone()
    if not row: raise HTTPException(status_code=404, detail="Export not found")
    return row


@router.get("/workspace-exports/{export_id}/content")
async def download_export(export_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    row = await get_export(export_id, actor)
    if row["status"] != "completed": raise HTTPException(status_code=409, detail="Export is not ready")
    return Response(await get_object(row["object_ref"]), media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{export_id}.json"'})


@router.post("/workspace-imports/uploads", status_code=201)
async def prepare_import(actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor); import_id = new_id("imp"); upload_id = new_id("upl")
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO ops_workspace_import(id,workspace_id,upload_id,created_by) VALUES (%s,%s,%s,%s)", (import_id, actor.workspace_id, upload_id, actor.user_id)); await conn.commit()
    return {"id": import_id, "upload_id": upload_id, "upload_url": f"/api/v1/workspace-imports/uploads/{upload_id}/content"}


@router.post("/workspace-imports/uploads/{upload_id}/content", status_code=204)
async def upload_import(upload_id: str, actor: Annotated[Actor, Depends(get_actor)], file: UploadFile = File(...)):
    _require_admin(actor); data = await file.read()
    if len(data) > 25_000_000: raise HTTPException(status_code=413, detail="Import package exceeds 25 MB")
    key = f"quarantine/{actor.workspace_id}/{upload_id}/workspace.json"; await put_object(key, data)
    async with pool.connection() as conn:
        result = await conn.execute("UPDATE ops_workspace_import SET object_ref=%s,upload_checksum=%s,uploaded_at=now(),status='uploaded' WHERE upload_id=%s AND workspace_id=%s RETURNING id", (key, hashlib.sha256(data).hexdigest(), upload_id, actor.workspace_id)); row = await result.fetchone(); await conn.commit()
    if not row: raise HTTPException(status_code=404, detail="Upload not found")
    return Response(status_code=204)


@router.post("/workspace-imports/uploads/{upload_id}/complete")
async def complete_upload(upload_id: str, body: UploadCompleteRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,upload_checksum,status FROM ops_workspace_import WHERE upload_id=%s AND workspace_id=%s", (upload_id, actor.workspace_id)); row = await result.fetchone()
    if not row: raise HTTPException(status_code=404, detail="Upload not found")
    if row["status"] != "uploaded" or row["upload_checksum"] != body.sha256: raise HTTPException(status_code=422, detail="Upload checksum mismatch")
    return {"id": row["id"], "status": "uploaded", "checksum": row["upload_checksum"]}


async def _submit_import_job(import_id: str, job_type: str, actor: Actor):
    async with pool.connection() as conn:
        async with conn.transaction():
            found = await conn.execute("SELECT 1 FROM ops_workspace_import WHERE id=%s AND workspace_id=%s", (import_id, actor.workspace_id))
            if not await found.fetchone(): raise HTTPException(status_code=404, detail="Import not found")
            operation = await submit_operation(conn, operation_type=job_type, workspace_id=actor.workspace_id, org_id=actor.org_id, actor_id=actor.user_id, actor_role=actor.role, idempotency_key=f"{import_id}-{new_id('req')}", payload={"import_id": import_id}, job_type=job_type, max_attempts=3)
    return {"operation_id": operation["id"], "status": operation["status"]}


@router.post("/workspace-imports/{import_id}/dry-runs", status_code=202)
async def import_dry_run(import_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor); return await _submit_import_job(import_id, "workspace.import.dry_run", actor)


@router.post("/workspace-imports/{import_id}/applications", status_code=202)
async def import_apply(import_id: str, body: ImportApplyRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor); return await _submit_import_job(import_id, "workspace.import.apply", actor)


@router.get("/workspace-imports/{import_id}")
async def get_import(import_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM ops_workspace_import WHERE id=%s AND workspace_id=%s", (import_id, actor.workspace_id)); row = await result.fetchone()
    if not row: raise HTTPException(status_code=404, detail="Import not found")
    return row
