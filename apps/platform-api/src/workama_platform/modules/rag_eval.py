from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field

from workama_platform.core import Actor, capability_allows, get_actor, json_dumps, new_id, pool
from workama_platform.modules.jobs import (
    ClaimedJob,
    IdempotencyConflict,
    canonical_hash,
    request_cancellation,
    submit_operation,
)


router = APIRouter(prefix="/api/v1/rag", tags=["rag-evaluation"])


class EvalSetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2_000)
    domain: str = Field(default="rag", min_length=1, max_length=80)
    version: int = Field(default=1, ge=1, le=10_000)
    dataset_id: str | None = Field(default=None, max_length=64)
    sampling_policy: dict[str, Any] = Field(default_factory=dict)


class EvalSetPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2_000)
    sampling_policy: dict[str, Any] | None = None


class EvalCaseCreate(BaseModel):
    query: str = Field(min_length=1, max_length=20_000)
    expected_chunk_ids: list[str] = Field(default_factory=list, max_length=100)
    expected_answer: str | None = Field(default=None, max_length=20_000)
    forbidden: list[str] = Field(default_factory=list, max_length=100)
    labels: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)


class EvalCaseImport(BaseModel):
    items: list[EvalCaseCreate] = Field(min_length=1, max_length=500)


class EvalRunCreate(BaseModel):
    eval_set_id: str = Field(min_length=1, max_length=64)
    dataset_id: str | None = Field(default=None, max_length=64)
    generation_id: str | None = Field(default=None, max_length=64)
    top_k: int = Field(default=5, ge=1, le=50)
    candidate_k: int = Field(default=20, ge=5, le=200)
    rrf_k: int = Field(default=60, ge=1, le=200)
    score_threshold: float = Field(default=0.0, ge=0.0, le=1.0)


class FeedbackCreate(BaseModel):
    dataset_id: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1, max_length=20_000)
    chunk_ids: list[str] = Field(default_factory=list, max_length=100)
    rating: Literal[-1, 0, 1]
    comment: str | None = Field(default=None, max_length=4_000)
    eval_run_id: str | None = Field(default=None, max_length=64)
    eval_case_id: str | None = Field(default=None, max_length=64)


class DeleteReason(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


def _etag(version: int) -> str:
    return f'W/"{version}"'


def _assert_if_match(value: str | None, version: int) -> None:
    if not value:
        raise HTTPException(status_code=428, detail="If-Match is required")
    if value.strip() not in {"*", str(version), _etag(version), f'"{version}"'}:
        raise HTTPException(status_code=412, detail="Resource version does not match If-Match")


def _require(actor: Actor, capability: str) -> None:
    if not capability_allows(actor.capabilities, capability):
        raise HTTPException(status_code=403, detail=f"Missing capability: {capability}")


async def _eval_set(conn, eval_set_id: str, workspace_id: str, *, include_archived: bool = False) -> dict[str, Any]:
    suffix = "" if include_archived else " AND status <> 'archived'"
    result = await conn.execute(
        f"SELECT * FROM pf_eval_set WHERE id=%s AND workspace_id=%s{suffix}",
        (eval_set_id, workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Evaluation set not found")
    return row


def _set_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "domain": row["domain"],
        "version": row["version"],
        "dataset_id": row["dataset_id"],
        "status": row["status"],
        "sampling_policy": row["sampling_policy"],
        "case_count": row["case_count"],
        "resource_version": row["resource_version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _case_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "eval_set_id": row["eval_set_id"],
        "query": row["query"],
        "expected_chunk_ids": row["expected_chunk_ids"],
        "expected_answer": row["expected_answer"],
        "forbidden": row["forbidden"],
        "labels": row["labels"],
        "provenance": row["provenance"],
        "status": row["status"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _run_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "eval_set_id": row["eval_set_id"],
        "dataset_id": row["dataset_id"],
        "generation_id": row["generation_id"],
        "operation_id": row["operation_id"],
        "status": row["status"],
        "config": row["config"],
        "metrics": row["metrics"],
        "evidence_ref": row["evidence_ref"],
        "error": row["error"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }


async def _outbox(conn, workspace_id: str, operation_id: str, payload: dict[str, Any]) -> None:
    await conn.execute(
        "INSERT INTO ops_outbox(id,event_type,workspace_id,trace_id,payload) VALUES (%s,%s,%s,%s,%s::jsonb)",
        (new_id("out"), "rag.eval.requested.v1", workspace_id, operation_id, json_dumps(payload)),
    )


def _case_hash(case: EvalCaseCreate) -> str:
    import hashlib
    import json

    payload = json.dumps(case.model_dump(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@router.get("/eval-sets")
async def list_eval_sets(
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=50, ge=1, le=200),
):
    _require(actor, "rag_eval:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT s.*, count(c.id)::int AS case_count
            FROM pf_eval_set s LEFT JOIN pf_eval_case c ON c.eval_set_id=s.id AND c.status='active'
            WHERE s.workspace_id=%s AND s.status <> 'archived'
            GROUP BY s.id ORDER BY s.updated_at DESC LIMIT %s
            """,
            (actor.workspace_id, limit),
        )
        return {"items": [_set_summary(row) for row in await result.fetchall()]}


@router.post("/eval-sets", status_code=201)
async def create_eval_set(
    body: EvalSetCreate,
    actor: Annotated[Actor, Depends(get_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    _require(actor, "rag_eval:create")
    key = idempotency_key or f"eval-set:{body.name}:{body.version}"
    async with pool.connection() as conn:
        async with conn.transaction():
            if body.dataset_id:
                dataset = await conn.execute(
                    "SELECT id FROM pf_dataset WHERE id=%s AND workspace_id=%s AND status='active'",
                    (body.dataset_id, actor.workspace_id),
                )
                if not await dataset.fetchone():
                    raise HTTPException(status_code=404, detail="Dataset not found")
            existing_result = await conn.execute(
                "SELECT * FROM pf_eval_set WHERE workspace_id=%s AND idempotency_key=%s",
                (actor.workspace_id, key),
            )
            existing = await existing_result.fetchone()
            if existing:
                if existing["input_hash"] != canonical_hash(body.model_dump()):
                    raise HTTPException(status_code=409, detail="Idempotency key was already used with different input")
                count_result = await conn.execute(
                    "SELECT count(*)::int AS case_count FROM pf_eval_case WHERE eval_set_id=%s AND status='active'",
                    (existing["id"],),
                )
                existing["case_count"] = (await count_result.fetchone())["case_count"]
                return _set_summary(existing)
            try:
                result = await conn.execute(
                    """
                    INSERT INTO pf_eval_set(
                      id,org_id,workspace_id,name,description,domain,version,dataset_id,sampling_policy,
                      status,idempotency_key,input_hash,created_by
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'draft',%s,%s,%s) RETURNING *
                    """,
                    (
                        new_id("eval"), actor.org_id, actor.workspace_id, body.name.strip(), body.description,
                        body.domain, body.version, body.dataset_id, json_dumps(body.sampling_policy), key,
                        canonical_hash(body.model_dump()), actor.user_id,
                    ),
                )
            except Exception as exc:
                if "unique" in str(exc).lower():
                    raise HTTPException(status_code=409, detail="Evaluation set name and version already exist") from exc
                raise
            row = await result.fetchone()
            row["case_count"] = 0
    return _set_summary(row)


@router.get("/eval-sets/{eval_set_id}")
async def get_eval_set(eval_set_id: str, response: Response, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "rag_eval:read")
    async with pool.connection() as conn:
        row = await _eval_set(conn, eval_set_id, actor.workspace_id, include_archived=True)
        count_result = await conn.execute(
            "SELECT count(*)::int AS case_count FROM pf_eval_case WHERE eval_set_id=%s AND status='active'",
            (eval_set_id,),
        )
        row["case_count"] = (await count_result.fetchone())["case_count"]
    response.headers["ETag"] = _etag(row["resource_version"])
    return _set_summary(row)


@router.patch("/eval-sets/{eval_set_id}")
async def update_eval_set(
    eval_set_id: str,
    body: EvalSetPatch,
    actor: Annotated[Actor, Depends(get_actor)],
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
):
    _require(actor, "rag_eval:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            current = await _eval_set(conn, eval_set_id, actor.workspace_id)
            _assert_if_match(if_match, current["resource_version"])
            result = await conn.execute(
                """
                UPDATE pf_eval_set SET name=COALESCE(%s,name), description=COALESCE(%s,description),
                  sampling_policy=COALESCE(%s::jsonb,sampling_policy), resource_version=resource_version+1, updated_at=now()
                WHERE id=%s AND workspace_id=%s AND resource_version=%s RETURNING *
                """,
                (
                    body.name.strip() if body.name is not None else None, body.description,
                    json_dumps(body.sampling_policy) if body.sampling_policy is not None else None,
                    eval_set_id, actor.workspace_id, current["resource_version"],
                ),
            )
            row = await result.fetchone()
            if not row:
                raise HTTPException(status_code=412, detail="Resource version does not match If-Match")
            count_result = await conn.execute(
                "SELECT count(*)::int AS case_count FROM pf_eval_case WHERE eval_set_id=%s AND status='active'",
                (eval_set_id,),
            )
            row["case_count"] = (await count_result.fetchone())["case_count"]
    response.headers["ETag"] = _etag(row["resource_version"])
    return _set_summary(row)


@router.delete("/eval-sets/{eval_set_id}")
async def delete_eval_set(
    eval_set_id: str,
    body: DeleteReason,
    actor: Annotated[Actor, Depends(get_actor)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
):
    _require(actor, "rag_eval:delete")
    async with pool.connection() as conn:
        async with conn.transaction():
            current = await _eval_set(conn, eval_set_id, actor.workspace_id)
            _assert_if_match(if_match, current["resource_version"])
            result = await conn.execute(
                "UPDATE pf_eval_set SET status='archived',deleted_at=now(),delete_reason=%s,resource_version=resource_version+1,updated_at=now() WHERE id=%s AND workspace_id=%s AND resource_version=%s RETURNING id",
                (body.reason, eval_set_id, actor.workspace_id, current["resource_version"]),
            )
            if not await result.fetchone():
                raise HTTPException(status_code=412, detail="Resource version does not match If-Match")
    return Response(status_code=204)


@router.get("/eval-sets/{eval_set_id}/cases")
async def list_eval_cases(
    eval_set_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=200, ge=1, le=500),
):
    _require(actor, "rag_eval:read")
    async with pool.connection() as conn:
        await _eval_set(conn, eval_set_id, actor.workspace_id)
        result = await conn.execute(
            "SELECT * FROM pf_eval_case WHERE eval_set_id=%s AND workspace_id=%s AND status='active' ORDER BY created_at LIMIT %s",
            (eval_set_id, actor.workspace_id, limit),
        )
        return {"items": [_case_summary(row) for row in await result.fetchall()]}


@router.post("/eval-sets/{eval_set_id}/cases", status_code=201)
async def create_eval_case(eval_set_id: str, body: EvalCaseCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "rag_eval:write")
    case_hash = _case_hash(body)
    async with pool.connection() as conn:
        async with conn.transaction():
            await _eval_set(conn, eval_set_id, actor.workspace_id)
            existing_result = await conn.execute(
                "SELECT * FROM pf_eval_case WHERE eval_set_id=%s AND case_hash=%s",
                (eval_set_id, case_hash),
            )
            existing = await existing_result.fetchone()
            if existing:
                return _case_summary(existing)
            result = await conn.execute(
                """
                INSERT INTO pf_eval_case(
                  id,eval_set_id,workspace_id,query,expected_chunk_ids,expected_answer,forbidden,labels,provenance,case_hash,created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s) RETURNING *
                """,
                (
                    new_id("case"), eval_set_id, actor.workspace_id, body.query,
                    body.expected_chunk_ids, body.expected_answer, body.forbidden,
                    json_dumps(body.labels), json_dumps(body.provenance), case_hash, actor.user_id,
                ),
            )
            row = await result.fetchone()
            await conn.execute("UPDATE pf_eval_set SET updated_at=now() WHERE id=%s", (eval_set_id,))
    return _case_summary(row)


@router.post("/eval-sets/{eval_set_id}/case-imports", status_code=202)
async def import_eval_cases(
    eval_set_id: str,
    body: EvalCaseImport,
    actor: Annotated[Actor, Depends(get_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    _require(actor, "rag_eval:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _eval_set(conn, eval_set_id, actor.workspace_id)
            payload = {"eval_set_id": eval_set_id, "actor_id": actor.user_id, "items": [item.model_dump() for item in body.items]}
            try:
                operation = await submit_operation(
                    conn,
                    operation_type="rag.eval.import",
                    workspace_id=actor.workspace_id,
                    org_id=actor.org_id,
                    actor_id=actor.user_id,
                    actor_role=actor.role,
                    idempotency_key=idempotency_key or f"rag-eval-import:{eval_set_id}:{new_id('batch')}",
                    payload=payload,
                    job_type="rag.eval.import",
                    queue="rag",
                    max_attempts=2,
                    priority=80,
                )
            except IdempotencyConflict as exc:
                raise HTTPException(status_code=409, detail="E00008 Idempotency key was already used with different input") from exc
            await _outbox(conn, actor.workspace_id, operation["id"], payload)
    return {"operation": operation, "eval_set_id": eval_set_id}


@router.delete("/eval-sets/{eval_set_id}/cases/{case_id}")
async def delete_eval_case(
    eval_set_id: str,
    case_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
):
    _require(actor, "rag_eval:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _eval_set(conn, eval_set_id, actor.workspace_id)
            result = await conn.execute(
                "SELECT * FROM pf_eval_case WHERE id=%s AND eval_set_id=%s AND workspace_id=%s AND status='active'",
                (case_id, eval_set_id, actor.workspace_id),
            )
            current = await result.fetchone()
            if not current:
                raise HTTPException(status_code=404, detail="Evaluation case not found")
            _assert_if_match(if_match, current["version"])
            await conn.execute(
                "UPDATE pf_eval_case SET status='deleted',deleted_at=now(),version=version+1,updated_at=now() WHERE id=%s AND version=%s",
                (case_id, current["version"]),
            )
    return Response(status_code=204)


@router.get("/eval-runs")
async def list_eval_runs(
    actor: Annotated[Actor, Depends(get_actor)],
    eval_set_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
):
    _require(actor, "rag_eval:read")
    clauses = ["workspace_id=%s"]
    params: list[Any] = [actor.workspace_id]
    if eval_set_id:
        clauses.append("eval_set_id=%s")
        params.append(eval_set_id)
    params.append(limit)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"SELECT * FROM pf_eval_run WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT %s",
            tuple(params),
        )
        return {"items": [_run_summary(row) for row in await result.fetchall()]}


@router.post("/eval-runs", status_code=202)
async def create_eval_run(
    body: EvalRunCreate,
    actor: Annotated[Actor, Depends(get_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    _require(actor, "rag_eval:run")
    async with pool.connection() as conn:
        async with conn.transaction():
            eval_set = await _eval_set(conn, body.eval_set_id, actor.workspace_id)
            dataset_id = body.dataset_id or eval_set["dataset_id"]
            if not dataset_id:
                raise HTTPException(status_code=422, detail="dataset_id is required for an evaluation run")
            dataset_result = await conn.execute(
                "SELECT * FROM pf_dataset WHERE id=%s AND workspace_id=%s AND status='active'",
                (dataset_id, actor.workspace_id),
            )
            dataset = await dataset_result.fetchone()
            if not dataset or not dataset["active_generation_id"]:
                raise HTTPException(status_code=409, detail="Dataset index is not ready")
            generation_id = body.generation_id or dataset["active_generation_id"]
            generation_result = await conn.execute(
                "SELECT id,status FROM pf_index_generation WHERE id=%s AND dataset_id=%s AND workspace_id=%s",
                (generation_id, dataset_id, actor.workspace_id),
            )
            generation = await generation_result.fetchone()
            if not generation or generation["status"] not in {"active", "ready"}:
                raise HTTPException(status_code=409, detail="Evaluation generation is not ready")
            case_count_result = await conn.execute(
                "SELECT count(*)::int AS count FROM pf_eval_case WHERE eval_set_id=%s AND status='active'",
                (body.eval_set_id,),
            )
            if (await case_count_result.fetchone())["count"] == 0:
                raise HTTPException(status_code=422, detail="Evaluation set has no active cases")
            run_id = new_id("evalrun")
            payload = {
                "run_id": run_id,
                "eval_set_id": body.eval_set_id,
                "dataset_id": dataset_id,
                "generation_id": generation_id,
                "config": body.model_dump(exclude={"eval_set_id", "dataset_id", "generation_id"}),
            }
            try:
                operation = await submit_operation(
                    conn,
                    operation_type="rag.eval.run",
                    workspace_id=actor.workspace_id,
                    org_id=actor.org_id,
                    actor_id=actor.user_id,
                    actor_role=actor.role,
                    idempotency_key=idempotency_key or f"rag-eval-run:{body.eval_set_id}:{generation_id}:{body.top_k}:{body.candidate_k}",
                    payload=payload,
                    job_type="rag.eval.run",
                    queue="rag",
                    max_attempts=2,
                    priority=70,
                )
            except IdempotencyConflict as exc:
                raise HTTPException(status_code=409, detail="E00008 Idempotency key was already used with different input") from exc
            existing_run_result = await conn.execute(
                "SELECT * FROM pf_eval_run WHERE operation_id=%s AND workspace_id=%s",
                (operation["id"], actor.workspace_id),
            )
            existing_run = await existing_run_result.fetchone()
            if existing_run:
                return {"run": _run_summary(existing_run), "operation": operation}
            result = await conn.execute(
                """
                INSERT INTO pf_eval_run(
                  id,eval_set_id,dataset_id,workspace_id,generation_id,operation_id,config,status,created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,'queued',%s) RETURNING *
                """,
                (
                    run_id, body.eval_set_id, dataset_id, actor.workspace_id, generation_id,
                    operation["id"], json_dumps(payload["config"]), actor.user_id,
                ),
            )
            row = await result.fetchone()
            await _outbox(conn, actor.workspace_id, operation["id"], payload)
    return {"run": _run_summary(row), "operation": operation}


@router.get("/eval-runs/{run_id}")
async def get_eval_run(run_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "rag_eval:read")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM pf_eval_run WHERE id=%s AND workspace_id=%s", (run_id, actor.workspace_id))
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Evaluation run not found")
    return _run_summary(row)


@router.post("/eval-runs/{run_id}/cancel")
async def cancel_eval_run(run_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "rag_eval:run")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute("SELECT * FROM pf_eval_run WHERE id=%s AND workspace_id=%s", (run_id, actor.workspace_id))
            row = await result.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Evaluation run not found")
            operation = await request_cancellation(
                conn, operation_id=row["operation_id"], workspace_id=actor.workspace_id, reason="Evaluation run cancelled"
            )
            if operation and operation["status"] == "cancelled":
                await conn.execute("UPDATE pf_eval_run SET status='cancelled',completed_at=now(),updated_at=now() WHERE id=%s", (run_id,))
            result = await conn.execute("SELECT * FROM pf_eval_run WHERE id=%s", (run_id,))
            row = await result.fetchone()
    return _run_summary(row)


@router.post("/feedback", status_code=201)
async def create_rag_feedback(
    body: FeedbackCreate,
    actor: Annotated[Actor, Depends(get_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    _require(actor, "rag_feedback:create")
    async with pool.connection() as conn:
        async with conn.transaction():
            dataset_result = await conn.execute(
                "SELECT id FROM pf_dataset WHERE id=%s AND workspace_id=%s AND status='active'",
                (body.dataset_id, actor.workspace_id),
            )
            if not await dataset_result.fetchone():
                raise HTTPException(status_code=404, detail="Dataset not found")
            key = idempotency_key or f"feedback:{body.dataset_id}:{new_id('fbkey')}"
            existing_result = await conn.execute(
                "SELECT * FROM pf_rag_feedback WHERE workspace_id=%s AND idempotency_key=%s",
                (actor.workspace_id, key),
            )
            existing = await existing_result.fetchone()
            if existing:
                return existing
            result = await conn.execute(
                """
                INSERT INTO pf_rag_feedback(
                  id,workspace_id,dataset_id,query,chunk_ids,rating,comment,eval_run_id,eval_case_id,idempotency_key,created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
                """,
                (
                    new_id("fb"), actor.workspace_id, body.dataset_id, body.query, body.chunk_ids, body.rating,
                    body.comment, body.eval_run_id, body.eval_case_id, key, actor.user_id,
                ),
            )
            row = await result.fetchone()
    return {
        "id": row["id"],
        "dataset_id": row["dataset_id"],
        "query": row["query"],
        "chunk_ids": row["chunk_ids"],
        "rating": row["rating"],
        "comment": row["comment"],
        "created_at": row["created_at"],
    }


async def _eval_not_cancelled(job: ClaimedJob) -> None:
    async with pool.connection() as conn:
        result = await conn.execute("SELECT status FROM ops_async_operation WHERE id=%s", (job.operation_id,))
        operation = await result.fetchone()
    if not operation or operation["status"] in {"cancel_requested", "cancelled"}:
        from workama_platform.modules.knowledge import RagJobCancelled

        raise RagJobCancelled("RAG evaluation operation was cancelled")


def _fuse_rows(keyword: list[dict[str, Any]], vector: list[dict[str, Any]], *, rrf_k: int, top_k: int) -> list[dict[str, Any]]:
    scores: dict[str, dict[str, Any]] = {}
    for rank, row in enumerate(keyword, 1):
        item = scores.setdefault(row["id"], {"row": dict(row), "score": 0.0})
        item["score"] += 1 / (rrf_k + rank)
    for rank, row in enumerate(vector, 1):
        item = scores.setdefault(row["id"], {"row": dict(row), "score": 0.0})
        item["score"] += 1 / (rrf_k + rank)
    ordered = sorted(scores.values(), key=lambda item: item["score"], reverse=True)
    return [item["row"] for item in ordered[:top_k]]


async def _process_import(job: ClaimedJob) -> dict[str, Any]:
    from workama_platform.modules.knowledge import RagJobCancelled

    items = job.payload.get("items") or []
    eval_set_id = job.payload["eval_set_id"]
    created = skipped = 0
    async with pool.connection() as conn:
        async with conn.transaction():
            set_result = await conn.execute(
                "SELECT id FROM pf_eval_set WHERE id=%s AND workspace_id=%s AND status <> 'archived'",
                (eval_set_id, job.workspace_id),
            )
            if not await set_result.fetchone():
                raise ValueError("Evaluation set not found")
            for item in items:
                await _eval_not_cancelled(job)
                case = EvalCaseCreate.model_validate(item)
                case_hash = _case_hash(case)
                result = await conn.execute(
                    """
                    INSERT INTO pf_eval_case(
                      id,eval_set_id,workspace_id,query,expected_chunk_ids,expected_answer,forbidden,labels,provenance,case_hash,created_by
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)
                    ON CONFLICT(eval_set_id,case_hash) DO NOTHING RETURNING id
                    """,
                    (
                        new_id("case"), eval_set_id, job.workspace_id, case.query,
                        case.expected_chunk_ids, case.expected_answer, case.forbidden,
                        json_dumps(case.labels), json_dumps(case.provenance), case_hash, job.payload["actor_id"],
                    ),
                )
                if await result.fetchone():
                    created += 1
                else:
                    skipped += 1
            await conn.execute("UPDATE pf_eval_set SET updated_at=now() WHERE id=%s", (eval_set_id,))
    return {"eval_set_id": eval_set_id, "created": created, "skipped": skipped}


async def _process_run(job: ClaimedJob) -> dict[str, Any]:
    from workama_platform.modules.knowledge import _retrieve_rows

    payload = job.payload
    run_id = payload["run_id"]
    config = payload["config"]
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM pf_eval_run WHERE id=%s AND workspace_id=%s", (run_id, job.workspace_id))
        run = await result.fetchone()
        if not run:
            raise ValueError("Evaluation run not found")
        await conn.execute("UPDATE pf_eval_run SET status='running',started_at=COALESCE(started_at,now()),updated_at=now() WHERE id=%s", (run_id,))
        dataset_result = await conn.execute("SELECT * FROM pf_dataset WHERE id=%s AND workspace_id=%s", (payload["dataset_id"], job.workspace_id))
        dataset = await dataset_result.fetchone()
        cases_result = await conn.execute(
            "SELECT * FROM pf_eval_case WHERE eval_set_id=%s AND workspace_id=%s AND status='active' ORDER BY created_at",
            (payload["eval_set_id"], job.workspace_id),
        )
        cases = await cases_result.fetchall()
        await conn.commit()
    if not dataset:
        raise ValueError("Dataset not found")
    dataset = dict(dataset)
    dataset["active_generation_id"] = payload["generation_id"]
    hits = reciprocal = scored = errors = 0
    case_results: list[dict[str, Any]] = []
    for index, case in enumerate(cases, 1):
        await _eval_not_cancelled(job)
        try:
            keyword, vector = await _retrieve_rows(dataset, job.workspace_id, case["query"], config["candidate_k"])
            rows = _fuse_rows(keyword, vector, rrf_k=config["rrf_k"], top_k=config["top_k"])
            expected = set(case["expected_chunk_ids"] or [])
            positions = [position for position, row in enumerate(rows, 1) if row["id"] in expected]
            hit = bool(positions)
            reciprocal_score = 1 / positions[0] if positions else 0.0
            if expected:
                scored += 1
                hits += int(hit)
                reciprocal += reciprocal_score
            case_results.append({"case_id": case["id"], "hit": hit, "reciprocal_rank": reciprocal_score, "returned_chunk_ids": [row["id"] for row in rows]})
        except Exception as exc:
            errors += 1
            case_results.append({"case_id": case["id"], "error": str(exc)[:300]})
        async with pool.connection() as conn:
            from workama_platform.modules.jobs import heartbeat

            await heartbeat(conn, job, progress=min(95, int(index * 100 / max(len(cases), 1))), stage="scoring", lease_seconds=180)
            await conn.execute("UPDATE pf_eval_run SET metrics=jsonb_build_object('processed',%s,'total',%s),updated_at=now() WHERE id=%s", (index, len(cases), run_id))
            await conn.commit()
    metrics = {
        "total_cases": len(cases),
        "scored_cases": scored,
        "error_cases": errors,
        "hit_rate_at_k": hits / scored if scored else 0.0,
        "mrr": reciprocal / scored if scored else 0.0,
        "top_k": config["top_k"],
    }
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE pf_eval_run SET status='succeeded',metrics=%s::jsonb,evidence_ref=%s::jsonb,completed_at=now(),updated_at=now() WHERE id=%s",
                (json_dumps(metrics), json_dumps({"cases": case_results}), run_id),
            )
    return {"run_id": run_id, "metrics": metrics}


async def process_eval_job(job: ClaimedJob) -> dict[str, Any]:
    if job.job_type == "rag.eval.import":
        return await _process_import(job)
    if job.job_type == "rag.eval.run":
        try:
            return await _process_run(job)
        except Exception as exc:
            run_id = job.payload.get("run_id")
            if run_id:
                from workama_platform.modules.knowledge import RagJobCancelled

                status = "cancelled" if isinstance(exc, RagJobCancelled) else "failed"
                async with pool.connection() as conn:
                    await conn.execute(
                        "UPDATE pf_eval_run SET status=%s,error=%s,completed_at=now(),updated_at=now() WHERE id=%s AND status NOT IN ('succeeded','cancelled')",
                        (status, str(exc)[:500], run_id),
                    )
                    await conn.commit()
            raise
    raise ValueError(f"Unknown RAG evaluation job type: {job.job_type}")
