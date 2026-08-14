from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from workama_platform.core import Actor, capability_allows, encrypt_secret, get_actor, new_id, pool


router = APIRouter(prefix="/api/v1/code", tags=["code"])

RepositoryProvider = Literal["local", "github", "gitlab", "generic"]
TaskStatus = Literal["queued", "running", "paused", "succeeded", "failed", "cancelled"]
CodeEventType = Literal["diff", "terminal", "test"]

EVENT_TYPE_NAMES: dict[str, str] = {
    "diff": "code.diff",
    "terminal": "terminal.output",
    "test": "test.report",
}

TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"paused", "succeeded", "failed", "cancelled"}),
    "paused": frozenset({"running", "cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|credential|password|secret|private[_-]?key|token)",
    re.IGNORECASE,
)


def _require(actor: Actor, action: str) -> None:
    if not capability_allows(actor.capabilities, f"code:{action}"):
        raise HTTPException(status_code=403, detail=f"Missing capability: code:{action}")


class RepositoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    provider: RepositoryProvider = "local"
    remote_url: str | None = Field(default=None, max_length=2048)
    default_branch: str = Field(default="main", min_length=1, max_length=120)
    credential: str | None = Field(default=None, min_length=1, max_length=4096)


class TaskCreate(BaseModel):
    repository_id: str | None = Field(default=None, min_length=1, max_length=80)
    session_id: str | None = Field(default=None, min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=20000)
    branch: str = Field(default="workama/task", min_length=1, max_length=240)


class TaskStatusUpdate(BaseModel):
    status: TaskStatus
    reason: str | None = Field(default=None, max_length=1000)


class CodeEventCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    event_type: CodeEventType = Field(
        validation_alias=AliasChoices("event_type", "type"),
    )
    payload: dict[str, Any] = Field(default_factory=dict)


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS code_repository (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        provider TEXT NOT NULL DEFAULT 'local'
            CHECK (provider IN ('local', 'github', 'gitlab', 'generic')),
        remote_url TEXT,
        default_branch TEXT NOT NULL DEFAULT 'main',
        credential_enc TEXT,
        created_by TEXT NOT NULL REFERENCES id_user(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(workspace_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS code_task (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
        repository_id TEXT REFERENCES code_repository(id) ON DELETE SET NULL,
        session_id TEXT REFERENCES ag_session(id) ON DELETE SET NULL,
        title TEXT NOT NULL,
        prompt TEXT NOT NULL,
        branch TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued', 'running', 'paused', 'succeeded', 'failed', 'cancelled')),
        last_event_seq BIGINT NOT NULL DEFAULT 0,
        created_by TEXT NOT NULL REFERENCES id_user(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS code_event (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES code_task(id) ON DELETE CASCADE,
        workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
        seq BIGINT NOT NULL,
        type TEXT NOT NULL CHECK (type IN ('code.diff', 'terminal.output', 'test.report')),
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_by TEXT NOT NULL REFERENCES id_user(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(task_id, seq)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_code_repository_workspace_time ON code_repository(workspace_id, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_code_task_workspace_time ON code_task(workspace_id, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_code_task_session_time ON code_task(workspace_id, session_id, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_code_event_task_seq ON code_event(workspace_id, task_id, seq)",
)


async def ensure_code_schema(conn) -> None:
    """Apply the additive AMA-Code schema to an existing connection."""
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


def validate_task_transition(current: str, target: str) -> None:
    if target not in TASK_TRANSITIONS.get(current, frozenset()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Code task cannot transition from {current} to {target}",
        )


def redact_sensitive(value: Any) -> Any:
    """Remove secrets from event payloads before they reach storage or clients."""
    if isinstance(value, dict):
        return {
            str(key): "<redacted>" if _SENSITIVE_KEY.search(str(key)) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    return value


def repository_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "provider": row["provider"],
        "remote_url": row.get("remote_url"),
        "default_branch": row["default_branch"],
        "created_by": row["created_by"],
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def task_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "repository_id": row.get("repository_id"),
        "session_id": row.get("session_id"),
        "title": row["title"],
        "prompt": row["prompt"],
        "branch": row["branch"],
        "status": row["status"],
        "last_event_seq": row.get("last_event_seq", 0),
        "created_by": row["created_by"],
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def event_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "workspace_id": row["workspace_id"],
        "seq": row["seq"],
        "type": row["type"],
        "payload": redact_sensitive(row.get("payload") or {}),
        "created_by": row["created_by"],
        "created_at": row.get("created_at"),
    }


async def _owned_repository(conn, repository_id: str, actor: Actor) -> dict[str, Any]:
    result = await conn.execute(
        """
        SELECT id, workspace_id, name, provider, remote_url, default_branch,
               credential_enc, created_by, created_at, updated_at
        FROM code_repository
        WHERE id = %s AND workspace_id = %s
        """,
        (repository_id, actor.workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Code repository not found")
    return row


async def _owned_task(conn, task_id: str, actor: Actor, *, for_update: bool = False) -> dict[str, Any]:
    lock = " FOR UPDATE" if for_update else ""
    result = await conn.execute(
        f"""
        SELECT id, workspace_id, repository_id, session_id, title, prompt, branch,
               status, last_event_seq, created_by, created_at, updated_at
        FROM code_task
        WHERE id = %s AND workspace_id = %s{lock}
        """,
        (task_id, actor.workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Code task not found")
    return row


@router.get("/repositories")
async def list_repositories(
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=100, ge=1, le=200),
):
    _require(actor, "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, workspace_id, name, provider, remote_url, default_branch,
                   created_by, created_at, updated_at
            FROM code_repository
            WHERE workspace_id = %s
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (actor.workspace_id, limit),
        )
        rows = await result.fetchall()
    return {"items": [repository_view(row) for row in rows]}


@router.post("/repositories", status_code=status.HTTP_201_CREATED)
async def create_repository(
    body: RepositoryCreate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require(actor, "write")
    repository_id = new_id("repo")
    credential_enc = encrypt_secret(body.credential) if body.credential else None
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO code_repository(
                id, workspace_id, name, provider, remote_url, default_branch,
                credential_enc, created_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                repository_id,
                actor.workspace_id,
                body.name.strip(),
                body.provider,
                body.remote_url,
                body.default_branch.strip(),
                credential_enc,
                actor.user_id,
            ),
        )
        await conn.commit()
        row = await _owned_repository(conn, repository_id, actor)
    return repository_view(row)


@router.get("/repositories/{repository_id}")
async def get_repository(
    repository_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require(actor, "read")
    async with pool.connection() as conn:
        row = await _owned_repository(conn, repository_id, actor)
    return repository_view(row)


@router.get("/tasks")
async def list_code_tasks(
    actor: Annotated[Actor, Depends(get_actor)],
    session_id: str | None = None,
    repository_id: str | None = None,
    task_status: TaskStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=200),
):
    _require(actor, "read")
    clauses = ["workspace_id = %s"]
    params: list[Any] = [actor.workspace_id]
    if session_id:
        clauses.append("session_id = %s")
        params.append(session_id)
    if repository_id:
        clauses.append("repository_id = %s")
        params.append(repository_id)
    if task_status:
        clauses.append("status = %s")
        params.append(task_status)
    params.append(limit)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT id, workspace_id, repository_id, session_id, title, prompt, branch,
                   status, last_event_seq, created_by, created_at, updated_at
            FROM code_task
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            tuple(params),
        )
        rows = await result.fetchall()
    return {"items": [task_view(row) for row in rows]}


@router.post("/tasks", status_code=status.HTTP_201_CREATED)
async def create_code_task(
    body: TaskCreate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require(actor, "write")
    task_id = new_id("ctask")
    async with pool.connection() as conn:
        if body.repository_id:
            await _owned_repository(conn, body.repository_id, actor)
        if body.session_id:
            session = await conn.execute(
                "SELECT id FROM ag_session WHERE id = %s AND workspace_id = %s",
                (body.session_id, actor.workspace_id),
            )
            if not await session.fetchone():
                raise HTTPException(status_code=404, detail="Agent session not found")
        await conn.execute(
            """
            INSERT INTO code_task(
                id, workspace_id, repository_id, session_id, title, prompt, branch,
                status, created_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'queued', %s)
            """,
            (
                task_id,
                actor.workspace_id,
                body.repository_id,
                body.session_id,
                body.title.strip(),
                body.prompt,
                body.branch.strip(),
                actor.user_id,
            ),
        )
        await conn.commit()
        row = await _owned_task(conn, task_id, actor)
    return task_view(row)


@router.get("/tasks/{task_id}")
async def get_code_task(
    task_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require(actor, "read")
    async with pool.connection() as conn:
        row = await _owned_task(conn, task_id, actor)
    return task_view(row)


@router.post("/tasks/{task_id}/status")
async def update_code_task_status(
    task_id: str,
    body: TaskStatusUpdate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            current = await _owned_task(conn, task_id, actor, for_update=True)
            validate_task_transition(current["status"], body.status)
            result = await conn.execute(
                """
                UPDATE code_task
                SET status = %s, updated_at = now()
                WHERE id = %s AND workspace_id = %s
                RETURNING id, workspace_id, repository_id, session_id, title, prompt, branch,
                          status, last_event_seq, created_by, created_at, updated_at
                """,
                (body.status, task_id, actor.workspace_id),
            )
            updated = await result.fetchone()
    return {"previous_status": current["status"], "reason": body.reason, "task": task_view(updated)}


async def _append_code_event(
    task_id: str,
    event_type: CodeEventType,
    payload: dict[str, Any],
    actor: Actor,
) -> dict[str, Any]:
    canonical_type = EVENT_TYPE_NAMES[event_type]
    safe_payload = redact_sensitive(payload)
    async with pool.connection() as conn:
        async with conn.transaction():
            task = await _owned_task(conn, task_id, actor, for_update=True)
            next_seq = int(task.get("last_event_seq") or 0) + 1
            await conn.execute(
                """
                UPDATE code_task
                SET last_event_seq = %s, updated_at = now()
                WHERE id = %s AND workspace_id = %s
                """,
                (next_seq, task_id, actor.workspace_id),
            )
            result = await conn.execute(
                """
                INSERT INTO code_event(
                    id, task_id, workspace_id, seq, type, payload, created_by
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                RETURNING id, task_id, workspace_id, seq, type, payload, created_by, created_at
                """,
                (
                    new_id("cevt"),
                    task_id,
                    actor.workspace_id,
                    next_seq,
                    canonical_type,
                    json.dumps(safe_payload, ensure_ascii=False),
                    actor.user_id,
                ),
            )
            row = await result.fetchone()
    return event_view(row)


@router.post("/tasks/{task_id}/events", status_code=status.HTTP_201_CREATED)
async def append_code_event(
    task_id: str,
    body: CodeEventCreate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require(actor, "write")
    return await _append_code_event(task_id, body.event_type, body.payload, actor)


@router.post("/tasks/{task_id}/events/{event_type}", status_code=status.HTTP_201_CREATED)
async def append_typed_code_event(
    task_id: str,
    event_type: CodeEventType,
    payload: dict[str, Any],
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require(actor, "write")
    return await _append_code_event(task_id, event_type, payload, actor)


@router.get("/tasks/{task_id}/events")
async def list_code_events(
    task_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=1000),
):
    _require(actor, "read")
    async with pool.connection() as conn:
        await _owned_task(conn, task_id, actor)
        result = await conn.execute(
            """
            SELECT id, task_id, workspace_id, seq, type, payload, created_by, created_at
            FROM code_event
            WHERE task_id = %s AND workspace_id = %s AND seq > %s
            ORDER BY seq
            LIMIT %s
            """,
            (task_id, actor.workspace_id, after, limit),
        )
        rows = await result.fetchall()
    return {"items": [event_view(row) for row in rows]}
