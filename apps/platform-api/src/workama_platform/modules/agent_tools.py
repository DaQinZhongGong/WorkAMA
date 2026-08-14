from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException

from workama_platform.core import Actor, get_actor, pool, settings


router = APIRouter(prefix="/api/v1", tags=["agent-tools"])


@router.get("/sessions/{session_id}/sandbox")
async def get_session_sandbox(session_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT 1 FROM ag_session WHERE id=%s AND workspace_id=%s",
            (session_id, actor.workspace_id),
        )
        if not await result.fetchone():
            raise HTTPException(status_code=404, detail="Session not found")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(
                settings.sandbox_fleet_url.rstrip("/") + "/internal/sandboxes",
                headers={"X-Internal-Token": settings.internal_token},
                params={"session_id": session_id, "workspace_id": actor.workspace_id},
            )
        if response.status_code == 404:
            return {"status": "none"}
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Sandbox fleet is unavailable") from exc
    payload = response.json()
    return {
        "id": payload["id"], "status": payload["status"], "runtime": payload["runtime"],
        "gvisor_compliant": payload["gvisor_compliant"], "meter_seconds": payload["meter_seconds"],
        "started_at": payload["started_at"], "last_active_at": payload["last_active_at"],
    }


@router.get("/tools")
async def list_tools(actor: Annotated[Actor, Depends(get_actor)]):
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(settings.agent_server_url.rstrip("/") + "/internal/tools", headers={"X-Internal-Token": settings.internal_token})
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Agent tool registry is unavailable") from exc
    payload = response.json()
    return {**payload, "workspace_id": actor.workspace_id}
