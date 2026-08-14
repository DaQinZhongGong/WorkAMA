from __future__ import annotations

import json
import secrets
import hashlib
import hmac
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator

from workama_platform.core import Actor, get_actor, hash_secret, new_id, pool, redis, require_internal, settings
from workama_platform.object_store import delete_object, get_object, put_object

router = APIRouter(prefix="/api/v1", tags=["sessions"])
public_router = APIRouter(prefix="/public", tags=["public"])
internal_router = APIRouter(prefix="/internal/artifacts", tags=["artifact-internal"])
ARTIFACT_BUCKET = "workama-artifacts"
ATTACHMENT_BUCKET = "workama-attachments"
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
ALLOWED_ATTACHMENT_TYPES = {"text/plain", "text/markdown", "text/csv", "application/json", "application/xml", "text/xml", "application/csv"}


class InternalArtifactCreate(BaseModel):
    workspace_id: str
    session_id: str
    name: str = Field(min_length=1, max_length=240)
    content_type: str = "text/plain"
    content: str = Field(max_length=5 * 1024 * 1024)
    kind: str = "file"


class DeleteArtifactRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class AttachmentUploadPrepare(BaseModel):
    filename: str = Field(min_length=1, max_length=240)
    content_type: str
    size_bytes: int = Field(ge=0, le=MAX_ATTACHMENT_BYTES)
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


class AttachmentUploadComplete(BaseModel):
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


class SessionControlRequest(BaseModel):
    reason: str = Field(default="User requested", min_length=1, max_length=500)


class ShareCreate(BaseModel):
    expires_in_seconds: int = Field(default=86400, ge=60, le=30 * 86400)
    max_downloads: int | None = Field(default=None, ge=1, le=10000)


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")[:160] or "artifact"


def _share_hash(token: str) -> str:
    return hash_secret(token)


def _download_token(artifact_id: str, expires: datetime) -> str:
    payload = f"{artifact_id}.{int(expires.timestamp())}"
    signature = hmac.new(settings.key_pepper.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _verify_download_token(token: str) -> str | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    artifact_id, expiry, signature = parts
    payload = f"{artifact_id}.{expiry}"
    expected = hmac.new(settings.key_pepper.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        if int(expiry) < int(datetime.now(UTC).timestamp()):
            return None
    except ValueError:
        return None
    return artifact_id


async def _artifact_content(row: dict) -> bytes:
    if row.get("s3_key"):
        try:
            return await get_object(ARTIFACT_BUCKET, row["s3_key"])
        except Exception:
            if row.get("content"):
                return row["content"].encode()
            raise
    return (row.get("content") or "").encode()


class SessionCreate(BaseModel):
    title: str = Field(default="New conversation", min_length=1, max_length=120)
    model: str = Field(default="workama-chat", min_length=1, max_length=120)
    agent_kind: Literal["ama_chat"] = "ama_chat"
    parameters: dict = Field(default_factory=lambda: {"temperature": 0.7}, alias="model_config")
    toolset: list[Literal["web_search", "file.read", "file.write", "file.search", "code_interpreter", "terminal"]] = Field(default_factory=lambda: ["web_search", "file.read", "file.write", "file.search", "code_interpreter", "terminal"])
    canvas_enabled: bool = True
    prompt_version_id: str | None = None
    max_steps: int = Field(default=50, ge=1, le=50)
    max_credits: float = Field(default=500, gt=0, le=10000)
    max_duration_seconds: int = Field(default=3600, ge=30, le=3600)

    @field_validator("parameters")
    @classmethod
    def validate_parameters(cls, value: dict) -> dict:
        if set(value) - {"temperature", "top_p", "max_tokens"}: raise ValueError("model_config contains unsupported parameters")
        if "temperature" in value and not 0 <= float(value["temperature"]) <= 2: raise ValueError("temperature must be between 0 and 2")
        if "top_p" in value and not 0 < float(value["top_p"]) <= 1: raise ValueError("top_p must be between 0 and 1")
        if "max_tokens" in value and not 1 <= int(value["max_tokens"]) <= 32768: raise ValueError("max_tokens must be between 1 and 32768")
        return value


@router.get("/sessions")
async def list_sessions(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id,title,model,agent_kind,model_config,toolset,canvas_enabled,prompt_version_id,max_steps,max_credits,max_duration_seconds,used_steps,used_credits,started_at,status,last_seq,created_at,updated_at
            FROM ag_session WHERE workspace_id = %s AND status <> 'archived'
            ORDER BY updated_at DESC LIMIT 100
            """,
            (actor.workspace_id,),
        )
        rows = await result.fetchall()
        return {"items": rows, "data": rows, "next_cursor": None, "has_more": False, "meta": {"request_id": None}}

@router.post("/sessions", status_code=201)
async def create_session(
    body: SessionCreate, actor: Annotated[Actor, Depends(get_actor)]
):
    session_id = new_id("sess")
    async with pool.connection() as conn:
        if body.prompt_version_id:
            prompt = await conn.execute("SELECT 1 FROM sec_prompt_version WHERE id=%s AND workspace_id=%s AND status='published'", (body.prompt_version_id,actor.workspace_id))
            if not await prompt.fetchone(): raise HTTPException(status_code=422, detail="Prompt version must be published in this workspace")
        await conn.execute(
            """
            INSERT INTO ag_session(id,workspace_id,user_id,title,model,agent_kind,model_config,toolset,canvas_enabled,prompt_version_id,max_steps,max_credits,max_duration_seconds)
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s)
            """,
            (session_id,actor.workspace_id,actor.user_id,body.title,body.model,body.agent_kind,json.dumps(body.parameters),body.toolset,body.canvas_enabled,body.prompt_version_id,body.max_steps,body.max_credits,body.max_duration_seconds),
        )
        await conn.commit()
    return {"id":session_id,"title":body.title,"model":body.model,"agent_kind":body.agent_kind,"model_config":body.parameters,"toolset":body.toolset,"canvas_enabled":body.canvas_enabled,"prompt_version_id":body.prompt_version_id,"max_steps":body.max_steps,"max_credits":body.max_credits,"max_duration_seconds":body.max_duration_seconds,"used_steps":0,"used_credits":0,"status":"idle"}


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str, actor: Annotated[Actor, Depends(get_actor)]
):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id,title,model,agent_kind,model_config,toolset,canvas_enabled,prompt_version_id,max_steps,max_credits,max_duration_seconds,used_steps,used_credits,started_at,status,last_seq,created_at,updated_at
            FROM ag_session WHERE id = %s AND workspace_id = %s
            """,
            (session_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    return row


async def _control_session(session_id: str, actor: Actor, action: Literal["pause", "resume", "cancel"], reason: str):
    allowed = {"pause": {"running"}, "resume": {"paused"}, "cancel": {"running", "paused", "waiting_approval"}}
    target = {"pause": "paused", "resume": "running", "cancel": "cancelling"}[action]
    async with pool.connection() as conn:
        result = await conn.execute("SELECT status FROM ag_session WHERE id=%s AND workspace_id=%s FOR UPDATE", (session_id,actor.workspace_id)); row=await result.fetchone()
        if not row: raise HTTPException(status_code=404, detail="Session not found")
        if row["status"] not in allowed[action]: raise HTTPException(status_code=409, detail=f"Session cannot {action} from {row['status']}")
        await conn.execute("UPDATE ag_session SET status=%s,updated_at=now() WHERE id=%s",(target,session_id)); await conn.commit()
    await redis.set(f"agent-control:{session_id}",json.dumps({"action":action,"reason":reason,"actor_id":actor.user_id}),ex=3600)
    return {"session_id":session_id,"command":action,"status":target,"accepted":True}


@router.post("/sessions/{session_id}/pause", status_code=202)
async def pause_session(session_id: str, body: SessionControlRequest, actor: Annotated[Actor, Depends(get_actor)]): return await _control_session(session_id,actor,"pause",body.reason)


@router.post("/sessions/{session_id}/resume", status_code=202)
async def resume_session(session_id: str, body: SessionControlRequest, actor: Annotated[Actor, Depends(get_actor)]): return await _control_session(session_id,actor,"resume",body.reason)


@router.post("/sessions/{session_id}/cancel", status_code=202)
async def cancel_session(session_id: str, body: SessionControlRequest, actor: Annotated[Actor, Depends(get_actor)]): return await _control_session(session_id,actor,"cancel",body.reason)


@router.delete("/sessions/{session_id}", status_code=204)
async def archive_session(
    session_id: str, actor: Annotated[Actor, Depends(get_actor)]
):
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE ag_session SET status = 'archived', updated_at = now() WHERE id = %s AND workspace_id = %s",
            (session_id, actor.workspace_id),
        )
        await conn.commit()


@router.get("/sessions/{session_id}/events")
async def list_events(
    session_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    after: int = 0,
):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, seq, type, payload, created_at
            FROM ag_event WHERE session_id = %s AND workspace_id = %s AND seq > %s
            ORDER BY seq LIMIT 500
            """,
            (session_id, actor.workspace_id, after),
        )
        rows = await result.fetchall()
        return {"items": rows, "data": rows, "next_cursor": None, "has_more": False, "meta": {"request_id": None}}

def _validate_attachment(content: bytes, content_type: str) -> tuple[str, str]:
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="Attachment exceeds 5 MiB")
    if content_type not in ALLOWED_ATTACHMENT_TYPES:
        raise HTTPException(status_code=415, detail="Attachment format is not supported for temporary Q&A")
    if b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in content.upper():
        raise HTTPException(status_code=422, detail="Attachment failed malware scanning")
    try:
        extracted = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="Attachment must be valid UTF-8") from exc
    return hashlib.sha256(content).hexdigest(), extracted


async def _persist_attachment(conn, *, session_id: str, workspace_id: str, filename: str, content_type: str, content: bytes, key: str | None = None):
    digest, extracted = _validate_attachment(content, content_type)
    attachment_id = new_id("att")
    object_key = key or f"attachments/{workspace_id}/{session_id}/{attachment_id}/{_safe_name(filename)}"
    await put_object(ATTACHMENT_BUCKET, object_key, content)
    await conn.execute("""INSERT INTO ag_attachment(id,session_id,workspace_id,filename,content_type,size_bytes,extracted_text,status,s3_key,content_sha256,expires_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,'ready',%s,%s,now()+interval '24 hours')""",
        (attachment_id, session_id, workspace_id, filename, content_type, len(content), extracted, object_key, digest))
    return {"id": attachment_id, "filename": filename, "content_type": content_type, "size_bytes": len(content), "status": "ready", "content_sha256": digest, "expires_at": datetime.now(UTC) + timedelta(hours=24)}


@router.post("/sessions/{session_id}/attachments", status_code=201)
async def upload_attachment(session_id: str, actor: Annotated[Actor, Depends(get_actor)], file: UploadFile = File(...)):
    content = await file.read(MAX_ATTACHMENT_BYTES + 1)
    async with pool.connection() as conn:
        session = await conn.execute("SELECT 1 FROM ag_session WHERE id=%s AND workspace_id=%s", (session_id, actor.workspace_id))
        if not await session.fetchone(): raise HTTPException(status_code=404, detail="Session not found")
        row = await _persist_attachment(conn, session_id=session_id, workspace_id=actor.workspace_id, filename=file.filename or "attachment", content_type=file.content_type or "application/octet-stream", content=content)
        await conn.commit()
    return row


@router.post("/sessions/{session_id}/attachment-uploads", status_code=201)
async def prepare_attachment_upload(session_id: str, body: AttachmentUploadPrepare, actor: Annotated[Actor, Depends(get_actor)]):
    if body.content_type not in ALLOWED_ATTACHMENT_TYPES: raise HTTPException(status_code=415, detail="Attachment format is not supported for temporary Q&A")
    upload_id, token = new_id("atu"), secrets.token_urlsafe(32)
    key = f"attachments/{actor.workspace_id}/{session_id}/uploads/{upload_id}/{_safe_name(body.filename)}"
    async with pool.connection() as conn:
        session = await conn.execute("SELECT 1 FROM ag_session WHERE id=%s AND workspace_id=%s", (session_id, actor.workspace_id))
        if not await session.fetchone(): raise HTTPException(status_code=404, detail="Session not found")
        await conn.execute("INSERT INTO ag_attachment_upload(id,session_id,workspace_id,filename,content_type,expected_size,expected_sha256,s3_key,token_hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", (upload_id,session_id,actor.workspace_id,body.filename,body.content_type,body.size_bytes,body.content_sha256,key,_share_hash(token)))
        await conn.commit()
    return {"upload_id": upload_id, "upload_url": f"/api/v1/attachment-uploads/{upload_id}/content?token={token}", "expires_at": datetime.now(UTC)+timedelta(minutes=15), "max_size_bytes": MAX_ATTACHMENT_BYTES}


@router.put("/attachment-uploads/{upload_id}/content", status_code=204)
async def put_attachment_upload(upload_id: str, token: str, request: Request):
    content = await request.body()
    if len(content) > MAX_ATTACHMENT_BYTES: raise HTTPException(status_code=413, detail="Attachment exceeds 5 MiB")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT s3_key,expected_size FROM ag_attachment_upload WHERE id=%s AND token_hash=%s AND status='prepared' AND expires_at>now()", (upload_id,_share_hash(token)))
        row = await result.fetchone()
        if not row: raise HTTPException(status_code=404, detail="Upload is invalid or expired")
        if len(content) != row["expected_size"]: raise HTTPException(status_code=422, detail="Attachment size does not match upload declaration")
        await put_object(ATTACHMENT_BUCKET, row["s3_key"], content)
        await conn.execute("UPDATE ag_attachment_upload SET status='uploaded' WHERE id=%s", (upload_id,)); await conn.commit()
    return Response(status_code=204)


@router.post("/sessions/{session_id}/attachment-uploads/{upload_id}/complete", status_code=201)
async def complete_attachment_upload(session_id: str, upload_id: str, body: AttachmentUploadComplete, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM ag_attachment_upload WHERE id=%s AND session_id=%s AND workspace_id=%s AND status='uploaded' AND expires_at>now() FOR UPDATE", (upload_id,session_id,actor.workspace_id)); upload = await result.fetchone()
        if not upload: raise HTTPException(status_code=404, detail="Uploaded attachment is unavailable")
        content = await get_object(ATTACHMENT_BUCKET, upload["s3_key"]); digest = hashlib.sha256(content).hexdigest()
        expected = body.content_sha256 or upload["expected_sha256"]
        if expected and not hmac.compare_digest(expected.lower(), digest):
            await delete_object(ATTACHMENT_BUCKET, upload["s3_key"]); raise HTTPException(status_code=422, detail="Attachment checksum does not match")
        try:
            row = await _persist_attachment(conn, session_id=session_id, workspace_id=actor.workspace_id, filename=upload["filename"], content_type=upload["content_type"], content=content, key=upload["s3_key"])
        except HTTPException:
            await delete_object(ATTACHMENT_BUCKET, upload["s3_key"])
            raise
        await conn.execute("UPDATE ag_attachment_upload SET status='completed' WHERE id=%s", (upload_id,)); await conn.commit()
    return row


@router.get("/sessions/{session_id}/attachments")
async def list_attachments(
    session_id: str, actor: Annotated[Actor, Depends(get_actor)]
):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, filename, content_type, size_bytes, status, content_sha256, expires_at, parse_error, created_at
            FROM ag_attachment WHERE session_id = %s AND workspace_id = %s AND status = 'ready' AND expires_at > now()
            ORDER BY created_at
            """,
            (session_id, actor.workspace_id),
        )
        rows = await result.fetchall()
        return {"items": rows, "data": rows, "next_cursor": None, "has_more": False, "meta": {"request_id": None}}

@router.get("/attachments/{attachment_id}")
async def get_attachment(attachment_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,session_id,filename,content_type,size_bytes,status,content_sha256,expires_at,parse_error,created_at FROM ag_attachment WHERE id=%s AND workspace_id=%s AND status = 'ready' AND expires_at > now()", (attachment_id,actor.workspace_id)); row = await result.fetchone()
    if not row: raise HTTPException(status_code=404, detail="Attachment not found")
    return row


@router.get("/attachments/{attachment_id}/content")
async def download_attachment(attachment_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute("SELECT filename,content_type,s3_key,extracted_text FROM ag_attachment WHERE id=%s AND workspace_id=%s AND status = 'ready' AND expires_at > now()", (attachment_id,actor.workspace_id)); row = await result.fetchone()
    if not row: raise HTTPException(status_code=404, detail="Attachment not found")
    content = await get_object(ATTACHMENT_BUCKET,row["s3_key"]) if row["s3_key"] else row["extracted_text"].encode()
    return Response(content, media_type=row["content_type"], headers={"Content-Disposition": f'attachment; filename="{_safe_name(row["filename"])}"'})


@router.delete("/attachments/{attachment_id}", status_code=204)
async def delete_attachment(attachment_id: str, body: DeleteArtifactRequest, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,s3_key FROM ag_attachment WHERE id=%s AND workspace_id=%s FOR UPDATE", (attachment_id,actor.workspace_id)); row = await result.fetchone()
        if not row: raise HTTPException(status_code=404, detail="Attachment not found")
        referenced = await conn.execute("SELECT 1 FROM ag_event WHERE workspace_id=%s AND payload::text LIKE %s LIMIT 1", (actor.workspace_id,f'%{attachment_id}%'))
        if await referenced.fetchone(): raise HTTPException(status_code=409, detail="Attachment is referenced by a message and cannot be deleted")
        await conn.execute("DELETE FROM ag_attachment WHERE id=%s", (attachment_id,))
        await conn.commit()
    if row["s3_key"]: await delete_object(ATTACHMENT_BUCKET,row["s3_key"])
    return Response(status_code=204)


@router.get("/sessions/{session_id}/artifacts")
async def list_artifacts(
    session_id: str, actor: Annotated[Actor, Depends(get_actor)]
):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, name, kind, content_type, size_bytes, content_sha256, status, preview,
                   version, provenance_status, share_token, share_expires_at, created_at, deleted_at
            FROM ag_artifact WHERE session_id = %s AND workspace_id = %s AND deleted_at IS NULL
            ORDER BY created_at DESC
            """,
            (session_id, actor.workspace_id),
        )
        rows = await result.fetchall()
        return {"items": rows, "data": rows, "next_cursor": None, "has_more": False, "meta": {"request_id": None}}

@router.get("/artifacts/{artifact_id}/download", response_class=PlainTextResponse)
async def download_artifact(
    artifact_id: str, actor: Annotated[Actor, Depends(get_actor)]
):
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT content, content_type, s3_key, deleted_at FROM ag_artifact WHERE id = %s AND workspace_id = %s",
            (artifact_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row or row["deleted_at"]:
        raise HTTPException(status_code=404, detail="Artifact not found")
    content = await _artifact_content(row)
    return Response(content, media_type=row["content_type"], headers={"Content-Disposition": "attachment"})


@router.get("/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,session_id,workspace_id,name,kind,content_type,size_bytes,content_sha256,status,preview,version,provenance_status,created_at,deleted_at FROM ag_artifact WHERE id=%s AND workspace_id=%s", (artifact_id, actor.workspace_id))
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return row


@router.post("/artifacts/{artifact_id}/downloads")
async def create_artifact_download(artifact_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    expires = datetime.now(UTC) + timedelta(minutes=15)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id FROM ag_artifact WHERE id=%s AND workspace_id=%s AND deleted_at IS NULL", (artifact_id, actor.workspace_id))
        if not await result.fetchone():
            raise HTTPException(status_code=404, detail="Artifact not found")
    token = _download_token(artifact_id, expires)
    return {"token": token, "expires_at": expires, "url": f"/api/v1/artifact-downloads/{token}"}


@router.get("/artifact-downloads/{token}", response_class=Response)
async def signed_artifact_download(token: str):
    artifact_id = _verify_download_token(token)
    if not artifact_id:
        raise HTTPException(status_code=404, detail="Download token is invalid or expired")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT content,content_type,s3_key,deleted_at FROM ag_artifact WHERE id=%s", (artifact_id,))
        row = await result.fetchone()
    if not row or row["deleted_at"]:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return Response(await _artifact_content(row), media_type=row["content_type"])


@router.post("/artifacts/{artifact_id}/share")
async def share_artifact(
    artifact_id: str, actor: Annotated[Actor, Depends(get_actor)]
):
    return await create_artifact_share(artifact_id, ShareCreate(), actor)


@router.post("/artifacts/{artifact_id}/shares")
async def create_artifact_share(artifact_id: str, body: ShareCreate, actor: Annotated[Actor, Depends(get_actor)]):
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(seconds=body.expires_in_seconds)
    share_id = new_id("ash")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id FROM ag_artifact WHERE id=%s AND workspace_id=%s AND deleted_at IS NULL", (artifact_id, actor.workspace_id))
        if not await result.fetchone():
            raise HTTPException(status_code=404, detail="Artifact not found")
        await conn.execute("INSERT INTO ag_artifact_share(id,artifact_id,workspace_id,token_hash,expires_at,max_downloads,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s)", (share_id,artifact_id,actor.workspace_id,_share_hash(token),expires_at,body.max_downloads,actor.user_id))
        await conn.commit()
    return {"id": share_id, "token": token, "expires_at": expires_at, "url": f"/public/artifacts/{token}/content"}


@router.get("/artifact-shares/{share_id}")
async def get_artifact_share(share_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,artifact_id,expires_at,max_downloads,download_count,created_at,revoked_at FROM ag_artifact_share WHERE id=%s AND workspace_id=%s", (share_id, actor.workspace_id))
        row = await result.fetchone()
    if not row: raise HTTPException(status_code=404, detail="Share not found")
    return row


@router.delete("/artifact-shares/{share_id}", status_code=204)
async def revoke_artifact_share(share_id: str, body: DeleteArtifactRequest, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute("UPDATE ag_artifact_share SET revoked_at=now(),revoke_reason=%s WHERE id=%s AND workspace_id=%s AND revoked_at IS NULL RETURNING id", (body.reason,share_id,actor.workspace_id))
        if not await result.fetchone(): raise HTTPException(status_code=404, detail="Active share not found")
        await conn.commit()
    return Response(status_code=204)


@router.delete("/artifacts/{artifact_id}", status_code=202)
async def delete_artifact(artifact_id: str, body: DeleteArtifactRequest, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute("UPDATE ag_artifact SET status='deleted',deleted_at=now(),purge_after=now()+interval '30 days',delete_reason=%s WHERE id=%s AND workspace_id=%s AND deleted_at IS NULL RETURNING id", (body.reason,artifact_id,actor.workspace_id))
        if not await result.fetchone(): raise HTTPException(status_code=404, detail="Active artifact not found")
        await conn.commit()
    return {"id": artifact_id, "status": "deleted", "purge_after": datetime.now(UTC) + timedelta(days=30)}


@router.post("/artifacts/{artifact_id}/restore")
async def restore_artifact(artifact_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute("UPDATE ag_artifact SET status='ready',deleted_at=NULL,purge_after=NULL,delete_reason=NULL WHERE id=%s AND workspace_id=%s AND deleted_at IS NOT NULL RETURNING id,status", (artifact_id,actor.workspace_id))
        row = await result.fetchone()
        if not row: raise HTTPException(status_code=404, detail="Deleted artifact not found")
        await conn.commit()
    return row


async def _resolve_public_share(token: str, *, consume: bool):
    async with pool.connection() as conn:
        query = """UPDATE ag_artifact_share SET download_count=download_count+1 WHERE token_hash=%s AND revoked_at IS NULL AND expires_at>now() AND (max_downloads IS NULL OR download_count < max_downloads) RETURNING artifact_id""" if consume else """SELECT artifact_id FROM ag_artifact_share WHERE token_hash=%s AND revoked_at IS NULL AND expires_at>now() AND (max_downloads IS NULL OR download_count < max_downloads)"""
        result = await conn.execute(query, (_share_hash(token),))
        share = await result.fetchone()
        if share:
            artifact = await conn.execute("SELECT id,name,kind,content,content_type,s3_key,size_bytes,content_sha256,preview,deleted_at FROM ag_artifact WHERE id=%s", (share["artifact_id"],))
            row = await artifact.fetchone()
        else:
            legacy = await conn.execute("SELECT id,name,kind,content,content_type,s3_key,size_bytes,content_sha256,preview,deleted_at FROM ag_artifact WHERE share_token=%s AND share_expires_at>now()", (token,))
            row = await legacy.fetchone()
        await conn.commit()
    if not row or row["deleted_at"]:
        raise HTTPException(status_code=404, detail="Shared artifact is unavailable")
    return row

@public_router.get("/artifacts/{token}")
async def public_artifact_metadata(token: str):
    row = await _resolve_public_share(token, consume=False)
    return {"id": row["id"], "name": row["name"], "kind": row["kind"], "content_type": row["content_type"], "size_bytes": row["size_bytes"], "content_sha256": row["content_sha256"], "preview": row["preview"]}

@public_router.get("/artifacts/{token}/content", response_class=Response)
async def public_artifact_content(token: str):
    row = await _resolve_public_share(token, consume=True)
    return Response(await _artifact_content(row), media_type=row["content_type"])


@internal_router.post("", dependencies=[Depends(require_internal)])
async def internal_create_artifact(body: InternalArtifactCreate):
    artifact_id = new_id("art")
    content = body.content.encode()
    digest = hashlib.sha256(content).hexdigest()
    key = f"artifacts/{body.workspace_id}/{artifact_id}/v1/{_safe_name(body.name)}"
    await put_object(ARTIFACT_BUCKET, key, content)
    preview = {"text": body.content[:1000]} if body.content_type.startswith("text/") else {}
    async with pool.connection() as conn:
        valid = await conn.execute("SELECT 1 FROM ag_session WHERE id=%s AND workspace_id=%s", (body.session_id, body.workspace_id))
        if not await valid.fetchone(): raise HTTPException(status_code=404, detail="Session not found")
        await conn.execute("INSERT INTO ag_artifact(id,session_id,workspace_id,name,kind,content_type,content,s3_key,size_bytes,content_sha256,status,preview) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ready',%s::jsonb)", (artifact_id,body.session_id,body.workspace_id,body.name,body.kind,body.content_type,"",key,len(content),digest,json.dumps(preview)))
        await conn.commit()
    return {"id": artifact_id, "name": body.name, "content_type": body.content_type, "kind": body.kind, "size_bytes": len(content), "content_sha256": digest, "status": "ready", "preview": preview, "storage_ref": key}


@router.post("/sessions/{session_id}/ws-tickets")
@router.post("/ws-tickets", include_in_schema=False)
async def create_ws_ticket(
    actor: Annotated[Actor, Depends(get_actor)], session_id: str | None = None
):
    if not session_id:
        raise HTTPException(status_code=410, detail="Session-bound WebSocket ticket endpoint required")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT 1 FROM ag_session WHERE id=%s AND workspace_id=%s AND status <> 'archived'",
            (session_id, actor.workspace_id),
        )
        if not await result.fetchone():
            raise HTTPException(status_code=404, detail="Session not found")
    ticket = secrets.token_urlsafe(32)
    await redis.set(
        f"ws-ticket:{ticket}",
        json.dumps(
            {
                "user_id": actor.user_id,
                "workspace_id": actor.workspace_id,
                "role": actor.role,
                "session_id": session_id,
            }
        ),
        ex=60,
    )
    return {"ticket": ticket, "expires_in": 60}
