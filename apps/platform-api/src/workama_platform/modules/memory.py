from __future__ import annotations

import hashlib
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator

from workama_platform.core import Actor, capability_allows, get_actor, json_dumps, new_id, pool


router = APIRouter(prefix="/api/v1/memories", tags=["memory"])


MemoryKind = Literal["profile", "episodic", "semantic"]
RetentionPolicy = Literal["standard", "session", "indefinite"]
SEMANTIC_DIMENSIONS = 64
_SEMANTIC_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


class MemoryCreate(BaseModel):
    kind: MemoryKind
    key: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=4000)
    source_session_id: str | None = Field(default=None, max_length=80)
    metadata: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = None
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    retention_policy: RetentionPolicy = "standard"


class MemoryUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    metadata: dict[str, Any] | None = None
    expires_at: datetime | None = None
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    retention_policy: RetentionPolicy | None = None


class MemoryRestore(BaseModel):
    reason: str = Field(default="user_requested", min_length=1, max_length=200)


class MemoryForget(BaseModel):
    reason: str = Field(default="user_requested", min_length=1, max_length=200)


class GovernancePolicyUpdate(BaseModel):
    retention_days_by_importance: dict[str, int]
    default_importance: int = Field(default=3, ge=1, le=5)

    @field_validator("retention_days_by_importance")
    @classmethod
    def _check_retention_map(cls, v: dict[str, int]) -> dict[str, int]:
        for k, days in v.items():
            try:
                ik = int(k)
            except ValueError as exc:
                raise ValueError(f"Importance key must be integer 1-5, got {k!r}") from exc
            if not (1 <= ik <= 5):
                raise ValueError(f"Importance level must be between 1 and 5, got {ik}")
            if not isinstance(days, int) or days < 0:
                raise ValueError(f"Retention days must be non-negative integer, got {days}")
        return v


def normalize_memory_key(value: str) -> str:
    return " ".join(value.strip().split()).lower()


def semantic_tokens(value: str) -> list[str]:
    return [token.lower() for token in _SEMANTIC_TOKEN_RE.findall(value or "")]


def semantic_embedding(*parts: str) -> list[float]:
    """Build a deterministic local embedding without sending memory content away."""
    values = [0.0] * SEMANTIC_DIMENSIONS
    for token in semantic_tokens(" ".join(parts)):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        slot = int.from_bytes(digest[:2], "big") % SEMANTIC_DIMENSIONS
        sign = 1.0 if digest[2] & 1 else -1.0
        values[slot] += sign * (1.0 + min(len(token), 8) * 0.05)
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0:
        return [0.0] * SEMANTIC_DIMENSIONS
    return [round(value / norm, 8) for value in values]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    return max(-1.0, min(1.0, sum(a * b for a, b in zip(left, right, strict=True))))


def memory_matches(row: dict, query: str) -> bool:
    needle = query.strip().lower()
    if not needle:
        return True
    return needle in str(row.get("memory_key", "")).lower() or needle in str(row.get("content", "")).lower()


def rank_memories(
    rows: list[dict],
    query: str,
    *,
    mode: Literal["lexical", "semantic", "hybrid"],
    limit: int,
) -> list[dict]:
    query_embedding = semantic_embedding(query)
    ranked: list[dict] = []
    for row in rows:
        lexical = float(row.get("relevance") or 0.0)
        if lexical == 0.0 and memory_matches(row, query):
            lexical = 1.0
        stored = row.get("semantic_embedding") or semantic_embedding(
            str(row.get("memory_key") or ""), str(row.get("content") or "")
        )
        try:
            semantic = max(0.0, cosine_similarity([float(value) for value in stored], query_embedding))
        except (TypeError, ValueError):
            semantic = 0.0
        if mode == "lexical":
            score = lexical
        elif mode == "semantic":
            score = semantic
        else:
            score = (0.65 * semantic) + (0.35 * min(lexical, 1.0))
        if score > 0.0 or mode == "lexical":
            ranked.append({**row, "semantic_score": round(semantic, 6), "relevance": round(score, 6)})
    ranked.sort(key=lambda row: float(row.get("relevance") or 0.0), reverse=True)
    return ranked[:limit]


def _require(actor: Actor, action: str) -> None:
    if not capability_allows(actor.capabilities, f"memory:{action}"):
        raise HTTPException(status_code=403, detail=f"Missing capability: memory:{action}")


def _summary(row: dict) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "key": row["memory_key"],
        "content": row["content"],
        "metadata": row.get("metadata") or {},
        "source_session_id": row.get("source_session_id"),
        "status": row["status"],
        "expires_at": row.get("expires_at"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "importance": float(row.get("importance", 0.5)),
        "confidence": float(row.get("confidence", 0.5)),
        "retention_policy": row.get("retention_policy", "standard"),
        "semantic_version": row.get("semantic_version", "local-hash-v1"),
        "last_recalled_at": row.get("last_recalled_at"),
    }


async def _owned_memory(conn, memory_id: str, actor: Actor, *, include_deleted: bool = False) -> dict:
    status_clause = "" if include_deleted else "AND m.status = 'active'"
    result = await conn.execute(
        f"""
        SELECT m.* FROM ag_memory m
        WHERE m.id = %s AND m.workspace_id = %s AND m.user_id = %s
          {status_clause}
        """,
        (memory_id, actor.workspace_id, actor.user_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Memory not found")
    if row["expires_at"] and row["expires_at"] <= datetime.now(UTC) and row["status"] == "active":
        await conn.execute("UPDATE ag_memory SET status='expired', updated_at=now() WHERE id=%s", (memory_id,))
        raise HTTPException(status_code=404, detail="Memory expired")
    return row


@router.get("")
async def list_memories(
    actor: Annotated[Actor, Depends(get_actor)],
    kind: MemoryKind | None = None,
    query: str | None = Query(default=None, max_length=200),
    include_deleted: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
):
    _require(actor, "read")
    status_clause = "" if include_deleted else "AND m.status = 'active'"
    params: list[object] = [actor.workspace_id, actor.user_id]
    kind_clause = ""
    if kind:
        kind_clause = "AND m.kind = %s"
        params.append(kind)
    params.append(limit)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT m.* FROM ag_memory m
            WHERE m.workspace_id = %s AND m.user_id = %s {status_clause} {kind_clause}
              AND (m.expires_at IS NULL OR m.expires_at > now())
            ORDER BY m.updated_at DESC LIMIT %s
            """,
            tuple(params),
        )
        rows = [row for row in await result.fetchall() if memory_matches(row, query or "")]
    return {"items": [_summary(row) for row in rows], "data": [_summary(row) for row in rows], "next_cursor": None, "has_more": False, "meta": {"request_id": None, "count": len(rows)}}


@router.get("/recall")
async def recall_memories(
    actor: Annotated[Actor, Depends(get_actor)],
    query: str = Query(min_length=1, max_length=200),
    kind: MemoryKind | None = None,
    mode: Literal["lexical", "semantic", "hybrid"] = "hybrid",
    limit: int = Query(default=10, ge=1, le=50),
):
    _require(actor, "read")
    async with pool.connection() as conn:
        kind_clause = ""
        params: list[object] = [actor.workspace_id, actor.user_id]
        if kind:
            kind_clause = "AND m.kind = %s"
            params.append(kind)
        if mode == "lexical":
            result = await conn.execute(
                f"""
                SELECT m.*, ts_rank_cd(to_tsvector('simple', m.memory_key || ' ' || m.content),
                                       plainto_tsquery('simple', %s)) AS relevance
                FROM ag_memory m
                WHERE m.workspace_id = %s AND m.user_id = %s AND m.status = 'active'
                  AND (m.expires_at IS NULL OR m.expires_at > now())
                  {kind_clause}
                  AND to_tsvector('simple', m.memory_key || ' ' || m.content) @@ plainto_tsquery('simple', %s)
                ORDER BY relevance DESC, m.updated_at DESC LIMIT %s
                """,
                (query, actor.workspace_id, actor.user_id, *(([kind] if kind else [])), query, limit),
            )
            rows = await result.fetchall()
        else:
            params.append(max(limit * 10, 50))
            result = await conn.execute(
                f"""
                SELECT m.* FROM ag_memory m
                WHERE m.workspace_id = %s AND m.user_id = %s AND m.status = 'active'
                  AND (m.expires_at IS NULL OR m.expires_at > now())
                  {kind_clause}
                ORDER BY m.updated_at DESC LIMIT %s
                """,
                tuple(params),
            )
            rows = rank_memories(await result.fetchall(), query, mode=mode, limit=limit)
        if rows:
            await conn.execute(
                "UPDATE ag_memory SET last_recalled_at=now() WHERE id=ANY(%s) AND workspace_id=%s AND user_id=%s",
                ([row["id"] for row in rows], actor.workspace_id, actor.user_id),
            )
    return {
        "query": query,
        "mode": mode,
        "items": [
            {**_summary(row), "relevance": float(row.get("relevance") or 0), "semantic_score": float(row.get("semantic_score") or 0)}
            for row in rows
        ],
        "data": [
            {**_summary(row), "relevance": float(row.get("relevance") or 0), "semantic_score": float(row.get("semantic_score") or 0)}
            for row in rows
        ],
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None},
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_memory(body: MemoryCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "write")
    key = normalize_memory_key(body.key)
    if not key:
        raise HTTPException(status_code=422, detail="Memory key cannot be empty")
    if body.source_session_id:
        async with pool.connection() as conn:
            check = await conn.execute(
                "SELECT 1 FROM ag_session WHERE id=%s AND workspace_id=%s AND user_id=%s",
                (body.source_session_id, actor.workspace_id, actor.user_id),
            )
            if not await check.fetchone():
                raise HTTPException(status_code=404, detail="Source session not found")
    memory_id = new_id("mem")
    expires_at = body.expires_at
    if body.retention_policy == "session" and expires_at is None:
        expires_at = datetime.now(UTC) + timedelta(hours=24)
    embedding = semantic_embedding(key, body.content)
    async with pool.connection() as conn:
        async with conn.transaction():
            existing_result = await conn.execute(
                "SELECT id, status FROM ag_memory WHERE workspace_id=%s AND user_id=%s AND kind=%s AND memory_key=%s FOR UPDATE",
                (actor.workspace_id, actor.user_id, body.kind, key),
            )
            existing = await existing_result.fetchone()
            if existing and existing["status"] == "active":
                raise HTTPException(status_code=409, detail="A memory with this key already exists")
            if existing:
                result = await conn.execute(
                    """
                    UPDATE ag_memory SET content=%s, metadata=%s::jsonb, source_session_id=%s,
                        expires_at=%s, importance=%s, confidence=%s, retention_policy=%s,
                        semantic_embedding=%s::jsonb, semantic_version='local-hash-v1',
                        status='active', updated_at=now(), deleted_at=NULL, forgotten_at=NULL, forget_reason=NULL
                    WHERE id=%s RETURNING *
                    """,
                    (body.content, json_dumps(body.metadata), body.source_session_id, expires_at, body.importance,
                     body.confidence, body.retention_policy, json_dumps(embedding), existing["id"]),
                )
            else:
                result = await conn.execute(
                    """
                    INSERT INTO ag_memory(id, org_id, workspace_id, user_id, kind, memory_key, content,
                        source_session_id, metadata, expires_at, importance, confidence, retention_policy,
                        semantic_embedding, semantic_version, created_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s) RETURNING *
                    """,
                    (memory_id, actor.org_id, actor.workspace_id, actor.user_id, body.kind, key, body.content,
                     body.source_session_id, json_dumps(body.metadata), expires_at, body.importance, body.confidence,
                     body.retention_policy, json_dumps(embedding), "local-hash-v1", actor.user_id),
                )
            row = await result.fetchone()
    return _summary(row)


@router.get("/governance-policy")
async def get_governance_policy(actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM memory_governance_policy WHERE workspace_id = %s",
            (actor.workspace_id,),
        )
        row = await result.fetchone()
    if not row:
        return {
            "workspace_id": actor.workspace_id,
            "retention_days_by_importance": {},
            "default_importance": 3,
            "created_at": None,
            "updated_at": None,
        }
    return {
        "workspace_id": row["workspace_id"],
        "retention_days_by_importance": row.get("retention_days_by_importance") or {},
        "default_importance": row["default_importance"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.put("/governance-policy")
async def update_governance_policy(body: GovernancePolicyUpdate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "write")
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Only owner or admin can update governance policy")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                INSERT INTO memory_governance_policy(workspace_id, retention_days_by_importance, default_importance, created_at, updated_at)
                VALUES (%s, %s::jsonb, %s, now(), now())
                ON CONFLICT(workspace_id) DO UPDATE SET
                    retention_days_by_importance = EXCLUDED.retention_days_by_importance,
                    default_importance = EXCLUDED.default_importance,
                    updated_at = now()
                RETURNING *
                """,
                (actor.workspace_id, json_dumps(body.retention_days_by_importance), body.default_importance),
            )
            row = await result.fetchone()
    return {
        "workspace_id": row["workspace_id"],
        "retention_days_by_importance": row.get("retention_days_by_importance") or {},
        "default_importance": row["default_importance"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.get("/{memory_id}")
async def get_memory(memory_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "read")
    async with pool.connection() as conn:
        return _summary(await _owned_memory(conn, memory_id, actor))


@router.patch("/{memory_id}")
async def update_memory(memory_id: str, body: MemoryUpdate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "write")
    if all(value is None for value in (body.content, body.metadata, body.expires_at, body.importance, body.confidence, body.retention_policy)):
        raise HTTPException(status_code=422, detail="At least one mutable field is required")
    async with pool.connection() as conn:
        async with conn.transaction():
            current = await _owned_memory(conn, memory_id, actor)
            next_content = body.content if body.content is not None else current["content"]
            result = await conn.execute(
                """
                UPDATE ag_memory SET content=COALESCE(%s,content), metadata=COALESCE(%s::jsonb,metadata),
                    expires_at=%s, importance=COALESCE(%s,importance), confidence=COALESCE(%s,confidence),
                    retention_policy=COALESCE(%s,retention_policy), semantic_embedding=%s::jsonb,
                    semantic_version='local-hash-v1', updated_at=now()
                WHERE id=%s AND workspace_id=%s AND user_id=%s RETURNING *
                """,
                (body.content, json_dumps(body.metadata) if body.metadata is not None else None,
                 body.expires_at, body.importance, body.confidence, body.retention_policy,
                 json_dumps(semantic_embedding(current["memory_key"], next_content)), memory_id,
                 actor.workspace_id, actor.user_id),
            )
            return _summary(await result.fetchone())


@router.delete("/{memory_id}", status_code=status.HTTP_200_OK)
async def delete_memory(memory_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "delete")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _owned_memory(conn, memory_id, actor)
            result = await conn.execute(
                "UPDATE ag_memory SET status='deleted', deleted_at=now(), updated_at=now() WHERE id=%s AND workspace_id=%s AND user_id=%s RETURNING id, status, updated_at",
                (memory_id, actor.workspace_id, actor.user_id),
            )
            row = await result.fetchone()
    return {"id": row["id"], "status": row["status"], "updated_at": row["updated_at"]}


@router.post("/{memory_id}/forget")
async def forget_memory(memory_id: str, body: MemoryForget, actor: Annotated[Actor, Depends(get_actor)]):
    """Explicit privacy action; retain only a non-sensitive reason for audit."""
    _require(actor, "delete")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _owned_memory(conn, memory_id, actor)
            result = await conn.execute(
                """UPDATE ag_memory SET status='deleted', deleted_at=now(), forgotten_at=now(),
                   forget_reason=%s, updated_at=now()
                   WHERE id=%s AND workspace_id=%s AND user_id=%s RETURNING id,status,updated_at""",
                (body.reason, memory_id, actor.workspace_id, actor.user_id),
            )
            row = await result.fetchone()
    return {"id": row["id"], "status": row["status"], "forgotten": True, "updated_at": row["updated_at"]}


@router.post("/{memory_id}/restore")
async def restore_memory(memory_id: str, body: MemoryRestore, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await _owned_memory(conn, memory_id, actor, include_deleted=True)
            if row["status"] not in {"deleted", "expired"}:
                raise HTTPException(status_code=409, detail="Only deleted or expired memories can be restored")
            result = await conn.execute(
                "UPDATE ag_memory SET status='active', deleted_at=NULL, updated_at=now() WHERE id=%s RETURNING *",
                (memory_id,),
            )
            restored = await result.fetchone()
    return {**_summary(restored), "restore_reason": body.reason}
