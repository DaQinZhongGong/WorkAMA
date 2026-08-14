from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Annotated

from workama_platform.core import Actor, get_actor, json_dumps, new_id, pool

operation_router = APIRouter(prefix="/api/v1/operations", tags=["async-operations"])
admin_router = APIRouter(prefix="/api/v1/admin", tags=["platform-jobs"])


TERMINAL_OPERATION_STATES = {"succeeded", "partially_succeeded", "failed", "cancelled", "expired"}
OPERATION_TRANSITIONS = {
    "queued": {"running", "cancelled", "expired"},
    "running": {"retry_wait", "cancel_requested", "succeeded", "partially_succeeded", "failed"},
    "retry_wait": {"running", "cancel_requested", "failed"},
    "cancel_requested": {"cancelled", "succeeded", "partially_succeeded", "failed"},
}


class IdempotencyConflict(ValueError):
    code = "E00008"


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def validate_operation_transition(current: str, target: str) -> None:
    if current == target:
        return
    if target not in OPERATION_TRANSITIONS.get(current, set()):
        raise ValueError(f"invalid operation transition: {current} -> {target}")


def retry_delay(attempt: int, *, base_seconds: int = 5, maximum_seconds: int = 300) -> int:
    return min(maximum_seconds, base_seconds * (2 ** max(attempt - 1, 0)))


@dataclass(frozen=True)
class ClaimedJob:
    id: str
    operation_id: str
    workspace_id: str
    job_type: str
    payload: dict[str, Any]
    attempt_count: int
    max_attempts: int
    lease_token: str


async def submit_operation(
    conn, *, operation_type: str, workspace_id: str, org_id: str, actor_id: str,
    actor_role: str, idempotency_key: str, payload: dict[str, Any], job_type: str,
    queue: str = "platform", max_attempts: int = 3, priority: int = 100,
    cancellable: bool = True, input_hash_override: str | None = None,
    scheduled_at: datetime | None = None,
) -> dict[str, Any]:
    input_hash = input_hash_override or canonical_hash(payload)
    existing_result = await conn.execute(
        "SELECT * FROM ops_async_operation WHERE workspace_id = %s AND operation_type = %s AND idempotency_key = %s",
        (workspace_id, operation_type, idempotency_key),
    )
    existing = await existing_result.fetchone()
    if existing:
        if existing["input_hash"] != input_hash:
            raise IdempotencyConflict("idempotency key was already used with different input")
        return existing

    operation_id = new_id("op")
    job_id = new_id("job")
    result = await conn.execute(
        """
        INSERT INTO ops_async_operation(
          id, operation_type, org_id, workspace_id, actor_id, actor_role,
          idempotency_key, input_hash, status, cancellable, max_attempts
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'queued',%s,%s) RETURNING *
        """,
        (operation_id, operation_type, org_id, workspace_id, actor_id, actor_role,
         idempotency_key, input_hash, cancellable, max_attempts),
    )
    operation = await result.fetchone()
    scheduled_value = scheduled_at if scheduled_at is not None else datetime.now(UTC)
    await conn.execute(
        """
        INSERT INTO ops_job(
          id, operation_id, workspace_id, job_type, payload, payload_hash,
          queue, priority, status, max_attempts, cancellable, scheduled_at
        ) VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,'queued',%s,%s,%s)
        """,
        (job_id, operation_id, workspace_id, job_type, json_dumps(payload), input_hash,
         queue, priority, max_attempts, cancellable, scheduled_value),
    )
    return operation


async def claim_jobs(
    conn, *, worker_id: str, queue: str = "platform", limit: int = 10,
    lease_seconds: int = 45,
) -> list[ClaimedJob]:
    result = await conn.execute(
        """
        WITH fair AS (
          SELECT DISTINCT ON (workspace_id) id, priority, scheduled_at
          FROM ops_job
          WHERE queue = %s AND status IN ('queued','retry_wait') AND scheduled_at <= now()
            AND (lease_expires_at IS NULL OR lease_expires_at < now())
          ORDER BY workspace_id, priority DESC, scheduled_at, created_at
        ), candidates AS (
          SELECT j.id FROM ops_job j JOIN fair ON fair.id = j.id
          ORDER BY fair.priority DESC, fair.scheduled_at
          LIMIT %s FOR UPDATE OF j SKIP LOCKED
        )
        UPDATE ops_job j SET status = 'running', lease_owner = %s,
          lease_token = concat('lease_', md5(random()::text || clock_timestamp()::text)),
          lease_expires_at = now() + make_interval(secs => %s), heartbeat_at = now(),
          attempt_count = attempt_count + 1, started_at = COALESCE(started_at, now()), updated_at = now()
        FROM candidates c WHERE j.id = c.id
        RETURNING j.*
        """,
        (queue, limit, worker_id, lease_seconds),
    )
    rows = await result.fetchall()
    claimed: list[ClaimedJob] = []
    for row in rows:
        await conn.execute(
            "INSERT INTO ops_job_run(id, job_id, attempt, worker_id, status, started_at, heartbeat_at) VALUES (%s,%s,%s,%s,'running',now(),now()) ON CONFLICT(job_id, attempt) DO NOTHING",
            (new_id("run"), row["id"], row["attempt_count"], worker_id),
        )
        await conn.execute(
            "UPDATE ops_async_operation SET status = 'running', attempt_count = GREATEST(attempt_count, %s), started_at = COALESCE(started_at, now()), updated_at = now() WHERE id = %s AND status IN ('queued','retry_wait')",
            (row["attempt_count"], row["operation_id"]),
        )
        claimed.append(ClaimedJob(row["id"], row["operation_id"], row["workspace_id"], row["job_type"], row["payload"], row["attempt_count"], row["max_attempts"], row["lease_token"]))
    return claimed


async def heartbeat(conn, job: ClaimedJob, *, progress: int | None = None, stage: str | None = None, lease_seconds: int = 45) -> bool:
    result = await conn.execute(
        """UPDATE ops_job SET heartbeat_at = now(), lease_expires_at = now() + make_interval(secs => %s),
             progress = COALESCE(%s, progress), stage = COALESCE(%s, stage), updated_at = now()
           WHERE id = %s AND lease_token = %s AND status = 'running' RETURNING id""",
        (lease_seconds, progress, stage, job.id, job.lease_token),
    )
    row = await result.fetchone()
    if row:
        await conn.execute(
            "UPDATE ops_async_operation SET progress = COALESCE(%s, progress), stage = COALESCE(%s, stage), updated_at = now() WHERE id = %s",
            (progress, stage, job.operation_id),
        )
    return bool(row)


async def complete_job(conn, job: ClaimedJob, result_summary: dict[str, Any] | None = None, *, partial: bool = False) -> bool:
    status = "partially_succeeded" if partial else "succeeded"
    result = await conn.execute(
        "UPDATE ops_job SET status = %s, progress = 100, lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, completed_at = now(), updated_at = now() WHERE id = %s AND lease_token = %s RETURNING id",
        (status, job.id, job.lease_token),
    )
    if not await result.fetchone():
        return False
    await conn.execute("UPDATE ops_job_run SET status = %s, ended_at = now(), heartbeat_at = now(), result_summary = %s::jsonb WHERE job_id = %s AND attempt = %s", (status, json_dumps(result_summary or {}), job.id, job.attempt_count))
    await conn.execute("UPDATE ops_async_operation SET status = %s, progress = 100, result_summary = %s::jsonb, completed_at = now(), updated_at = now() WHERE id = %s", (status, json_dumps(result_summary or {}), job.operation_id))
    return True


async def cancel_claimed_job(conn, job: ClaimedJob, reason: str) -> bool:
    result = await conn.execute(
        """
        UPDATE ops_job SET status='cancelled',lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
          completed_at=now(),last_error_code='cancelled',last_error=%s,updated_at=now()
        WHERE id=%s AND lease_token=%s RETURNING id
        """,
        (reason[:500], job.id, job.lease_token),
    )
    if not await result.fetchone():
        return False
    await conn.execute(
        "UPDATE ops_job_run SET status='cancelled',ended_at=now(),heartbeat_at=now(),error_code='cancelled',error_summary=%s WHERE job_id=%s AND attempt=%s",
        (reason[:500], job.id, job.attempt_count),
    )
    await conn.execute(
        "UPDATE ops_async_operation SET status='cancelled',completed_at=now(),error_code='cancelled',error_message=%s,updated_at=now() WHERE id=%s AND status NOT IN ('succeeded','partially_succeeded','failed','cancelled')",
        (reason[:500], job.operation_id),
    )
    return True


async def fail_job(conn, job: ClaimedJob, error_code: str, error_message: str, *, retryable: bool = True) -> str:
    summary = error_message[:500]
    retry = retryable and job.attempt_count < job.max_attempts
    status = "retry_wait" if retry else "failed"
    scheduled_at = datetime.now(UTC) + timedelta(seconds=retry_delay(job.attempt_count)) if retry else None
    result = await conn.execute(
        "UPDATE ops_job SET status = %s, scheduled_at = COALESCE(%s, scheduled_at), last_error_code = %s, last_error = %s, lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, completed_at = CASE WHEN %s THEN NULL ELSE now() END, updated_at = now() WHERE id = %s AND lease_token = %s RETURNING id",
        (status, scheduled_at, error_code, summary, retry, job.id, job.lease_token),
    )
    if not await result.fetchone():
        return "lease_lost"
    await conn.execute("UPDATE ops_job_run SET status = %s, ended_at = now(), error_code = %s, error_summary = %s WHERE job_id = %s AND attempt = %s", (status, error_code, summary, job.id, job.attempt_count))
    await conn.execute("UPDATE ops_async_operation SET status = %s, error_code = %s, error_message = %s, completed_at = CASE WHEN %s THEN NULL ELSE now() END, updated_at = now() WHERE id = %s", (status, error_code, summary, retry, job.operation_id))
    if not retry:
        await conn.execute("INSERT INTO ops_job_dlq(id, job_id, operation_id, workspace_id, job_type, payload_hash, attempts, error_code, error_summary) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(job_id) DO UPDATE SET attempts = EXCLUDED.attempts, error_code = EXCLUDED.error_code, error_summary = EXCLUDED.error_summary, failed_at = now()", (new_id("dlq"), job.id, job.operation_id, job.workspace_id, job.job_type, canonical_hash(job.payload), job.attempt_count, error_code, summary))
    return status


async def request_cancellation(conn, *, operation_id: str, workspace_id: str, reason: str) -> dict[str, Any] | None:
    result = await conn.execute("SELECT * FROM ops_async_operation WHERE id = %s AND workspace_id = %s FOR UPDATE", (operation_id, workspace_id))
    operation = await result.fetchone()
    if not operation:
        return None
    if not operation["cancellable"] or operation["status"] in TERMINAL_OPERATION_STATES:
        return operation
    # A queued/retry-wait operation has no running worker that can observe a
    # cancellation request, so it can transition directly to the terminal
    # cancelled state.  Leaving retry_wait as cancel_requested would strand the
    # operation forever after its job is marked cancelled below.
    target = "cancelled" if operation["status"] in {"queued", "retry_wait"} else "cancel_requested"
    validate_operation_transition(operation["status"], target)
    await conn.execute("UPDATE ops_async_operation SET status = %s, cancellation_reason = %s, cancel_requested_at = now(), completed_at = CASE WHEN %s = 'cancelled' THEN now() ELSE completed_at END, updated_at = now() WHERE id = %s", (target, reason[:500], target, operation_id))
    await conn.execute("UPDATE ops_job SET status = CASE WHEN status IN ('queued','retry_wait') THEN 'cancelled' ELSE status END, cancel_requested_at = now(), cancellation_reason = %s, completed_at = CASE WHEN status IN ('queued','retry_wait') THEN now() ELSE completed_at END, updated_at = now() WHERE operation_id = %s AND status NOT IN ('succeeded','failed','cancelled','partially_succeeded')", (reason[:500], operation_id))
    if target == "cancelled":
        workflow_result = await conn.execute(
            """
            UPDATE pf_workflow_run
            SET status='cancelled', error=%s, completed_at=now()
            WHERE operation_id=%s AND status='queued'
            RETURNING id,workflow_id,workspace_id
            """,
            (reason[:500], operation_id),
        )
        workflow_run = await workflow_result.fetchone()
        if workflow_run:
            from workama_platform.modules.workflows import append_workflow_event

            await append_workflow_event(
                conn,
                run_id=workflow_run["id"],
                workflow_id=workflow_run["workflow_id"],
                workspace_id=workflow_run["workspace_id"],
                event_type="workflow.run.cancelled",
                payload={
                    "run_id": workflow_run["id"],
                    "workflow_id": workflow_run["workflow_id"],
                    "status": "cancelled",
                    "error": reason[:500],
                },
            )
        work_result = await conn.execute(
            """
            SELECT e.id,e.plan_id,e.workspace_id,p.status,p.last_event_seq,p.created_by
            FROM work_execution e
            JOIN work_plan p ON p.id=e.plan_id AND p.workspace_id=e.workspace_id
            WHERE e.operation_id=%s AND e.workspace_id=%s
            FOR UPDATE
            """,
            (operation_id, workspace_id),
        )
        work_execution = await work_result.fetchone()
        if work_execution:
            from workama_platform.modules import work

            work_actor = Actor(
                user_id=operation["actor_id"],
                workspace_id=workspace_id,
                org_id=operation["org_id"],
                role=operation["actor_role"],
                email="",
                display_name="WorkAMA worker",
                onboarding_completed=True,
                actor_type="system",
            )
            plan_result = await conn.execute(
                """
                SELECT id,workspace_id,session_id,title,objective,status,last_event_seq,
                       created_by,created_at,updated_at
                FROM work_plan WHERE id=%s AND workspace_id=%s FOR UPDATE
                """,
                (work_execution["plan_id"], workspace_id),
            )
            plan = await plan_result.fetchone()
            if plan and plan["status"] not in {"succeeded", "failed", "cancelled"}:
                task_result = await conn.execute(
                    """
                    SELECT id,status FROM work_task
                    WHERE plan_id=%s AND workspace_id=%s AND status NOT IN ('done','cancelled')
                    ORDER BY position,id FOR UPDATE
                    """,
                    (work_execution["plan_id"], workspace_id),
                )
                for task in await task_result.fetchall():
                    await conn.execute(
                        "UPDATE work_task SET status='cancelled',updated_at=now() WHERE id=%s AND plan_id=%s AND workspace_id=%s",
                        (task["id"], work_execution["plan_id"], workspace_id),
                    )
                    await work._append_event(
                        conn,
                        plan=plan,
                        actor=work_actor,
                        task_id=task["id"],
                        event_type="task.execution.cancelled",
                        payload={"previous_status": task["status"], "reason": reason[:500], "operation_id": operation_id},
                    )
                await conn.execute(
                    "UPDATE work_plan SET status='cancelled',updated_at=now() WHERE id=%s AND workspace_id=%s",
                    (work_execution["plan_id"], workspace_id),
                )
                plan["status"] = "cancelled"
                await work._append_event(
                    conn,
                    plan=plan,
                    actor=work_actor,
                    event_type="plan.execution.cancelled",
                    payload={"plan_id": work_execution["plan_id"], "status": "cancelled", "error": reason[:500], "operation_id": operation_id},
                )
            await conn.execute(
                "UPDATE work_execution SET status='cancelled',completed_at=now(),updated_at=now() WHERE operation_id=%s AND workspace_id=%s",
                (operation_id, workspace_id),
            )
    operation["status"] = target
    return operation


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


def _require_admin(actor: Actor) -> None:
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")


@operation_router.get("/{operation_id}")
async def get_operation(operation_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM ops_async_operation WHERE id = %s AND workspace_id = %s", (operation_id, actor.workspace_id))
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Operation not found")
    return row


@operation_router.post("/{operation_id}/cancellations", status_code=202)
async def cancel_operation(operation_id: str, body: ReasonRequest, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await request_cancellation(conn, operation_id=operation_id, workspace_id=actor.workspace_id, reason=body.reason)
    if not row:
        raise HTTPException(status_code=404, detail="Operation not found")
    return {"id": operation_id, "status": row["status"]}


@admin_router.get("/operations")
async def list_operations(actor: Annotated[Actor, Depends(get_actor)], limit: int = Query(50, ge=1, le=200)):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM ops_async_operation WHERE workspace_id = %s ORDER BY created_at DESC LIMIT %s", (actor.workspace_id, limit))
        data = await result.fetchall()
    # Contract 720 listOperations ListQuery -> ListResponse<OperationDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@admin_router.get("/jobs")
async def list_jobs(actor: Annotated[Actor, Depends(get_actor)], limit: int = Query(50, ge=1, le=200)):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("""SELECT j.*, o.operation_type FROM ops_job j JOIN ops_async_operation o ON o.id = j.operation_id
          WHERE j.workspace_id = %s ORDER BY j.created_at DESC LIMIT %s""", (actor.workspace_id, limit))
        data = await result.fetchall()
    # Contract 720 listJobs ListQuery -> ListResponse<JobDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@admin_router.get("/jobs/{job_id}")
async def get_job(job_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM ops_job WHERE id=%s AND workspace_id=%s", (job_id, actor.workspace_id))
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return row


@admin_router.get("/jobs/{job_id}/runs")
async def list_job_runs(job_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        owned = await conn.execute("SELECT 1 FROM ops_job WHERE id=%s AND workspace_id=%s", (job_id, actor.workspace_id))
        if not await owned.fetchone():
            raise HTTPException(status_code=404, detail="Job not found")
        result = await conn.execute("SELECT * FROM ops_job_run WHERE job_id=%s ORDER BY attempt DESC", (job_id,))
        data = await result.fetchall()
    # Contract 720 listJobRuns ListQuery -> ListResponse<JobRunDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


async def list_dlq(actor: Annotated[Actor, Depends(get_actor)], limit: int = Query(50, ge=1, le=200)):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM ops_job_dlq WHERE workspace_id = %s ORDER BY failed_at DESC LIMIT %s", (actor.workspace_id, limit))
        data = await result.fetchall()
    # Contract 720 listDeadLetters ListQuery -> ListResponse<DeadLetterDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@admin_router.get("/dead-letters")
async def list_dead_letters(actor: Annotated[Actor, Depends(get_actor)], limit: int = Query(50, ge=1, le=200)):
    return await list_dlq(actor, limit)


@admin_router.get("/dead-letters/{dead_letter_id}")
async def get_dead_letter(dead_letter_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM ops_job_dlq WHERE id=%s AND workspace_id=%s", (dead_letter_id, actor.workspace_id))
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Dead letter not found")
    return row


@admin_router.post("/dead-letters/{dead_letter_id}/replays", status_code=202)
async def replay_dead_letter(dead_letter_id: str, body: ReasonRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT job_id FROM ops_job_dlq WHERE id=%s AND workspace_id=%s", (dead_letter_id, actor.workspace_id))
        dead_letter = await result.fetchone()
    if not dead_letter:
        raise HTTPException(status_code=404, detail="Dead letter not found")
    return await retry_job(dead_letter["job_id"], body, actor)


@admin_router.post("/jobs/{job_id}/cancellations", status_code=202)
async def cancel_job(job_id: str, body: ReasonRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute("SELECT operation_id FROM ops_job WHERE id = %s AND workspace_id = %s", (job_id, actor.workspace_id))
            job = await result.fetchone()
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")
            operation = await request_cancellation(conn, operation_id=job["operation_id"], workspace_id=actor.workspace_id, reason=body.reason)
    return {"id": job_id, "operation_id": job["operation_id"], "status": operation["status"]}


@admin_router.post("/jobs/{job_id}/retries", status_code=202)
async def retry_job(job_id: str, body: ReasonRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute("SELECT * FROM ops_job WHERE id = %s AND workspace_id = %s FOR UPDATE", (job_id, actor.workspace_id))
            source = await result.fetchone()
            if not source:
                raise HTTPException(status_code=404, detail="Job not found")
            if source["status"] not in {"failed", "cancelled"}:
                raise HTTPException(status_code=409, detail="Only failed or cancelled jobs can be retried")
            new_job_id = new_id("job")
            await conn.execute("""INSERT INTO ops_job(id, operation_id, workspace_id, job_type, schema_version, queue, priority,
              payload, payload_hash, status, max_attempts, timeout_seconds, heartbeat_seconds, cancellable)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,'queued',%s,%s,%s,%s)""",
              (new_job_id, source["operation_id"], source["workspace_id"], source["job_type"], source["schema_version"],
               source["queue"], source["priority"], json_dumps(source["payload"]), source["payload_hash"], source["max_attempts"],
               source["timeout_seconds"], source["heartbeat_seconds"], source["cancellable"]))
            await conn.execute("UPDATE ops_async_operation SET status = 'queued', progress = 0, error_code = NULL, error_message = NULL, completed_at = NULL, updated_at = now() WHERE id = %s", (source["operation_id"],))
            await conn.execute("UPDATE ops_job_dlq SET replayed_at = now(), replayed_by = %s, replay_reason = %s, replay_job_id = %s WHERE job_id = %s", (actor.user_id, body.reason, new_job_id, job_id))
    return {"id": new_job_id, "operation_id": source["operation_id"], "status": "queued"}
