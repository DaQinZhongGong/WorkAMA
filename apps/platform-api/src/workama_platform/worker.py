from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from pathlib import Path
from time import perf_counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import nats
try:
    import websockets
except ImportError:  # pragma: no cover - uvicorn[standard] supplies this in production.
    websockets = None
from opentelemetry import metrics, trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from nats.js.api import ConsumerConfig
from nats.js.errors import NotFoundError
from pydantic import ValidationError
from workama_observability import configure_observability, request_id_var, workspace_id_var

from workama_platform.core import Actor, decrypt_secret, ensure_runtime_schema, json_dumps, new_id, pool, redis, settings
from workama_platform.modules.billing.metering import (
    MeteringEvent,
    MeterRequest,
    settle_meter_event,
    settle_meter_in_transaction,
)
from workama_platform.modules.config_center import config_watcher_loop
from workama_platform.modules.billing.reporting import run_daily_reconciliation
from workama_platform.modules.billing.grants import expire_credit_grants
from workama_platform.modules.notification.delivery import process_pending_email_deliveries
from workama_platform.modules.privacy.processor import process_pending_data_requests
from workama_platform.modules.jobs import cancel_claimed_job, claim_jobs, complete_job, fail_job, heartbeat
from workama_platform.modules.memory_vector import (
    MEMORY_EXTRACT_JOB_TYPE as MEM_EXTRACT_JT,
    MEMORY_FORGET_JOB_TYPE as MEM_FORGET_JT,
    MEMORY_REINDEX_JOB_TYPE as MEM_REINDEX_JT,
    extraction_worker as memory_extraction_worker,
    forgetting_worker as memory_forgetting_worker,
    vector_index as memory_vector_index,
)
from workama_platform.modules.enterprise import (
    ORG_DELETION_JOB_TYPE,
    ORG_DELETION_OPERATION_TYPE,
    OWNER_TRANSFER_JOB_TYPE,
    OWNER_TRANSFER_OPERATION_TYPE,
)
from workama_platform.modules.search import rebuild_search_projection
from workama_platform.modules.portability import apply_import, build_workspace_export, dry_run_import
from workama_platform.modules.platform_support import execute_lifecycle_run
from workama_platform.modules import automation, external_apps, work
from workama_platform.modules import channel_extensions
from workama_platform.modules import workflows
from workama_platform.modules.notification.service import create_automation_run_notification, create_notification
from workama_platform.modules.audit_log import audit_log_action
from workama_platform.modules.audit_exports import SIEM_MAX_ATTEMPTS, deliver_siem_attempt, siem_retry_delay
from workama_platform.modules.open_platform import (
    WEBHOOK_MAX_ATTEMPTS,
    deliver_webhook_attempt,
    webhook_retry_delay,
)
from workama_platform.object_store import put_object

LOGGER = logging.getLogger("workama.platform-worker")
STREAM_NAME = "WORKAMA_P0"
SUBJECT = "metering.llm.v1"
CONSUMER_NAME = "billing-metering-v1"
CONTROL_STREAM_NAME = "WORKAMA_CONTROL"
CONTROL_SUBJECTS = [
    "config.changed.v1",
    "feature_flag.changed.v1",
    "rag.document.accepted.v1",
    "rag.step.requested.v1",
    "rag.index.activated.v1",
    "automation.triggered.v1",
    "automation.run.updated.v1",
    "skill.installed.v1",
    "skill.enabled.v1",
    "skill.disabled.v1",
    "skill.reviewed.v1",
    "connector.created.v1",
    "connector.updated.v1",
    "connector.enabled.v1",
    "connector.disabled.v1",
    "connector.revoked.v1",
    "connector.sync.started.v1",
    "connector.sync.completed.v1",
    "connector.document.revoked.v1",
    "federation.sso.updated",
    "federation.authorization.pending",
    "federation.scim.user.upserted",
    "federation.scim.user.deprovisioned",
    "federation.scim.group.upserted",
]
AUTOMATION_RESULT_EVENT = "automation.run.updated.v1"
AUTOMATION_SCAN_LIMIT = 20
AUTOMATION_RUN_LIMIT = 20
AUTOMATION_POLL_SECONDS = 2
AUTOMATION_AGENT_TIMEOUT_SECONDS = 300
AUTOMATION_WORKFLOW_RUN_PREFIX = "wrun_automation_"
HEARTBEAT_PATH = Path("/tmp/workama-platform-worker.heartbeat")
AUTOMATION_DETERMINISTIC_WORKFLOW_NODES = frozenset(
    {
        "input", "prompt", "transform", "condition", "knowledge_retrieval",
        "approval", "loop", "intent_classification", "variable_aggregate", "output",
        "http_request",
    }
)
Processor = Callable[[MeteringEvent, str], Awaitable[None]]
TRACER = trace.get_tracer("platform-worker")
METER = metrics.get_meter("platform-worker")
BATCHES = METER.create_counter("wama_platform_worker_batch_total")
BATCH_DURATION = METER.create_histogram("wama_platform_worker_batch_duration_seconds", unit="s")
JOB_RUNS = METER.create_counter("wama_platform_worker_job_total")
AUTOMATION_RUNS = METER.create_counter("wama_platform_worker_automation_run_total")


async def process_metering_event(event: MeteringEvent, subject: str) -> None:
    await settle_meter_event(event, subject, CONSUMER_NAME)


async def handle_metering_message(message, processor: Processor = process_metering_event) -> None:
    try:
        event = MeteringEvent.model_validate_json(message.data)
    except ValidationError as exc:
        LOGGER.error("invalid metering event", extra={"subject": message.subject, "error": str(exc)})
        await message.term()
        return

    try:
        request_id_var.set(event.trace_id)
        workspace_id_var.set(event.workspace_id)
        parent = TraceContextTextMapPropagator().extract(dict(getattr(message, "headers", None) or {}))
        with TRACER.start_as_current_span("metering.llm.consume", context=parent) as span:
            span.set_attribute("wama.request_id", event.trace_id)
            span.set_attribute("wama.workspace_id", event.workspace_id)
            await processor(event, message.subject)
    except Exception:
        LOGGER.exception("metering event processing failed", extra={"event_id": event.event_id})
        await message.nak(delay=5)
        return
    await message.ack()


async def ensure_stream(js) -> None:
    try:
        await js.stream_info(STREAM_NAME)
    except NotFoundError:
        await js.add_stream(
            name=STREAM_NAME,
            subjects=[SUBJECT],
            max_age=7 * 24 * 60 * 60,
        )
    try:
        control_stream = await js.stream_info(CONTROL_STREAM_NAME)
        configured = set(control_stream.config.subjects or [])
        if not set(CONTROL_SUBJECTS).issubset(configured):
            control_stream.config.subjects = sorted(configured | set(CONTROL_SUBJECTS))
            await js.update_stream(control_stream.config)
    except NotFoundError:
        await js.add_stream(
            name=CONTROL_STREAM_NAME,
            subjects=CONTROL_SUBJECTS,
            max_age=30 * 24 * 60 * 60,
        )


async def process_outbox(js, limit: int = 20) -> dict[str, int]:
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                SELECT id, event_type, workspace_id, trace_id, payload, attempts
                FROM ops_outbox
                WHERE status IN ('pending','failed') AND available_at <= now()
                ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED
                """,
                (limit,),
            )
            items = await result.fetchall()
            for item in items:
                await conn.execute(
                    "UPDATE ops_outbox SET status = 'pending', attempts = attempts + 1 WHERE id = %s",
                    (item["id"],),
                )
    published = 0
    for item in items:
        envelope = {
            "schema_version": 1,
            "event_id": item["id"],
            "event_type": item["event_type"],
            "occurred_at": datetime.now(UTC).isoformat(),
            "producer": "platform-api",
            "workspace_id": item["workspace_id"],
            "trace_id": item["trace_id"] or item["id"],
            "idempotency_key": item["id"],
            "classification": "C2",
            "payload": item["payload"],
        }
        try:
            await js.publish(
                item["event_type"], json_dumps(envelope).encode(), headers={"Nats-Msg-Id": item["id"]}
            )
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE ops_outbox SET status = 'published', published_at = now(), last_error = NULL WHERE id = %s",
                    (item["id"],),
                )
                await conn.commit()
            published += 1
        except Exception as exc:
            async with pool.connection() as conn:
                await conn.execute(
                    """
                    UPDATE ops_outbox SET status = 'failed', last_error = %s,
                      available_at = now() + make_interval(secs => LEAST(300, attempts * attempts * 2))
                    WHERE id = %s
                    """,
                    (str(exc)[:500], item["id"]),
                )
                await conn.commit()
    return {"claimed": len(items), "published": published}


async def outbox_loop(js) -> None:
    while True:
        started = perf_counter()
        result_label = "success"
        try:
            with TRACER.start_as_current_span("worker.control_outbox"):
                result = await process_outbox(js)
                if result["claimed"]:
                    LOGGER.info("control outbox processed", extra=result)
        except Exception:
            result_label = "error"
            LOGGER.exception("control outbox batch failed")
        BATCHES.add(1, {"operation": "control_outbox", "result": result_label})
        BATCH_DURATION.record(perf_counter() - started, {"operation": "control_outbox", "result": result_label})
        await asyncio.sleep(2)


def _utc_datetime(value: Any, *, default: datetime | None = None) -> datetime:
    """Normalize database/test timestamps before deriving an occurrence key."""
    if value is None:
        value = default or datetime.now(UTC)
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def automation_cron_idempotency_key(schedule_id: str, due_at: datetime) -> str:
    """Return a stable key for one scheduled minute, independent of worker count."""
    occurrence = _utc_datetime(due_at).replace(second=0, microsecond=0).isoformat()
    return f"cron:{schedule_id}:{occurrence}"


async def scan_due_automation_schedules(
    *, now: datetime | None = None, limit: int = AUTOMATION_SCAN_LIMIT
) -> dict[str, int]:
    """Claim due Cron rows and create their idempotent automation runs.

    The schedule row is locked before the run is inserted.  The existing
    automation enqueue helper writes the run and trigger outbox entry in the
    same transaction, so two worker instances cannot create the same
    occurrence.  A pre-check is only used for operational counters; the
    database unique constraint remains the source of truth.
    """
    if limit < 1:
        return {"scanned": 0, "enqueued": 0, "deduplicated": 0}
    reference = _utc_datetime(now)
    enqueued = deduplicated = 0
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                SELECT *
                FROM ops_automation_schedule
                WHERE trigger_type = 'cron'
                  AND enabled = TRUE
                  AND status = 'active'
                  AND next_run_at IS NOT NULL
                  AND next_run_at <= %s
                ORDER BY next_run_at, created_at
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (reference, min(limit, 100)),
            )
            schedules = await result.fetchall()
            for schedule in schedules:
                due_at = _utc_datetime(schedule["next_run_at"], default=reference)
                idempotency_key = automation_cron_idempotency_key(schedule["id"], due_at)
                existing_result = await conn.execute(
                    "SELECT id FROM ops_automation_run WHERE schedule_id=%s AND idempotency_key=%s",
                    (schedule["id"], idempotency_key),
                )
                existing = await existing_result.fetchone()
                await automation._enqueue_run(  # noqa: SLF001 - worker owns this queue boundary.
                    conn,
                    schedule,
                    payload=dict(schedule.get("payload") or {}),
                    source="cron",
                    idempotency_key=idempotency_key,
                    triggered_by=None,
                )
                if existing:
                    deduplicated += 1
                    # A manually repaired run can predate the schedule update;
                    # advance it so a duplicate row cannot pin the scheduler.
                    next_run = automation.next_cron_at(
                        schedule["cron_expression"], reference, schedule["timezone"]
                    )
                    await conn.execute(
                        """
                        UPDATE ops_automation_schedule
                        SET next_run_at=%s, updated_at=now()
                        WHERE id=%s AND next_run_at <= %s
                        """,
                        (next_run, schedule["id"], reference),
                    )
                else:
                    enqueued += 1
    return {"scanned": len(schedules), "enqueued": enqueued, "deduplicated": deduplicated}


def _unsupported_automation_result(run: dict[str, Any]) -> dict[str, Any]:
    """Return an explicit terminal result for targets outside the executor boundary."""
    target_type = str(run.get("target_type") or "unknown")
    return {
        "status": "failed",
        "execution_status": "unsupported",
        "executed": False,
        "error_code": "unsupported_target",
        "error_message": (
            f"Automation target '{target_type}' has no platform-worker executor; "
            "no work, workflow, or agent action was executed."
        ),
    }


class AutomationExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, *, execution_status: str = "failed", executed: bool = False):
        super().__init__(message)
        self.code = code
        self.execution_status = execution_status
        self.executed = executed


def _workflow_node_types(graph: dict[str, Any]) -> set[str]:
    return {
        workflows.canonical_node_type(node.get("type"))
        for node in graph.get("nodes", [])
        if isinstance(node, dict)
    }


async def _execute_workflow_target(run: dict[str, Any]) -> dict[str, Any]:
    target_id = str(run.get("target_id") or "")
    if not target_id or "://" in target_id:
        raise AutomationExecutionError(
            "external_target",
            "Automation workflow target must be an internal workspace resource.",
        )
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id,org_id,workspace_id,created_by,version,graph,status
            FROM pf_workflow
            WHERE id=%s AND workspace_id=%s AND status <> 'archived'
            """,
            (target_id, run["workspace_id"]),
        )
        workflow = await result.fetchone()
        if not workflow:
            raise AutomationExecutionError("workflow_not_found", "Internal workspace workflow was not found.")
        graph = workflow.get("graph") or {}
        node_types = _workflow_node_types(graph)
        unsupported_nodes = sorted(node_types - AUTOMATION_DETERMINISTIC_WORKFLOW_NODES)
        if unsupported_nodes:
            raise AutomationExecutionError(
                "unsupported_workflow_node",
                f"Automation workflow contains unsupported node types: {', '.join(unsupported_nodes)}.",
            )
        errors = workflows.validate_graph(graph)
        if errors:
            raise AutomationExecutionError("workflow_invalid", "; ".join(errors[:5]))
        payload = run.get("payload") or {}
        input_value = payload.get("input", payload) if isinstance(payload, dict) else {}
        if not isinstance(input_value, dict):
            raise AutomationExecutionError("workflow_input_invalid", "Workflow automation input must be an object.")

        workflow_run_id = f"{AUTOMATION_WORKFLOW_RUN_PREFIX}{run['id']}"
        existing_result = await conn.execute(
            "SELECT id,status,output,trace,error FROM pf_workflow_run WHERE id=%s AND workspace_id=%s",
            (workflow_run_id, run["workspace_id"]),
        )
        existing = await existing_result.fetchone()
        if existing and existing.get("status") in {"succeeded", "failed", "pending_approval"}:
            status_value = existing["status"]
            return {
                "status": "succeeded" if status_value == "succeeded" else "failed",
                "execution_status": status_value,
                "executed": True,
                "workflow_run_id": workflow_run_id,
                "output": automation._redact_payload(existing.get("output") or {}),
                "error_code": "workflow_execution_failed" if status_value != "succeeded" else None,
                "error_message": existing.get("error"),
            }
        await conn.execute(
            """
            INSERT INTO pf_workflow_run(
              id,workflow_id,org_id,workspace_id,created_by,input,workflow_version,status,started_at
            ) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,'running',now())
            ON CONFLICT(id) DO NOTHING
            """,
            (
                workflow_run_id, target_id, workflow["org_id"], run["workspace_id"],
                workflow["created_by"], json_dumps(input_value), workflow["version"],
            ),
        )
        await conn.commit()

    events: list[dict[str, Any]] = [{
        "event_type": "workflow.run.started",
        "payload": {"run_id": workflow_run_id, "workflow_id": target_id, "automation_run_id": run["id"]},
    }]
    try:
        run_status, output, trace_data, error = await workflows.execute_graph(
            graph,
            input_value,
            None,
            False,
            events,
            workspace_id=run["workspace_id"],
            sandbox_session_id=workflow_run_id,
        )
    except Exception as exc:
        run_status, output, trace_data, error = "failed", {}, [], str(exc)[:500]
    events.append({
        "event_type": "workflow.run.completed" if run_status == "succeeded" else f"workflow.run.{run_status}",
        "payload": {"run_id": workflow_run_id, "workflow_id": target_id, "status": run_status, "error": error},
    })
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                UPDATE pf_workflow_run
                SET output=%s::jsonb,trace=%s::jsonb,status=%s,error=%s,completed_at=now()
                WHERE id=%s AND workspace_id=%s AND status='running'
                RETURNING id,status,output,error
                """,
                (
                    json_dumps(automation._redact_payload(output)), json_dumps(automation._redact_payload(trace_data)),
                    run_status, error, workflow_run_id, run["workspace_id"],
                ),
            )
            saved = await result.fetchone()
            if not saved:
                raise AutomationExecutionError("workflow_state_changed", "Workflow run state changed before completion.")
            for sequence, event in enumerate(events, start=1):
                await conn.execute(
                    """
                    INSERT INTO pf_workflow_event(
                      id,run_id,workflow_id,workspace_id,seq,event_type,payload
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT(run_id,seq) DO NOTHING
                    """,
                    (
                        f"wfe_{workflow_run_id}_{sequence}", workflow_run_id, target_id,
                        run["workspace_id"], sequence, event["event_type"],
                        json_dumps(automation._redact_payload(event["payload"])),
                    ),
                )
    if run_status == "pending_approval":
        return {
            "status": "failed", "execution_status": "pending_approval", "executed": False,
            "workflow_run_id": workflow_run_id, "error_code": "approval_required",
            "error_message": "Workflow requires approval and cannot continue from an automation worker.",
        }
    return {
        "status": "succeeded" if run_status == "succeeded" else "failed",
        "execution_status": run_status,
        "executed": True,
        "workflow_run_id": workflow_run_id,
        "output": automation._redact_payload(output),
        "error_code": None if run_status == "succeeded" else "workflow_execution_failed",
        "error_message": error,
    }


def _agent_websocket_uri(session_id: str, ticket: str, last_seq: int) -> str:
    parsed = urlsplit(settings.agent_server_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AutomationExecutionError("agent_server_unconfigured", "Agent server URL is not an internal HTTP service.")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, f"/ws/sessions/{quote(session_id, safe='')}", f"ticket={quote(ticket)}&after={last_seq}", ""))


async def _execute_agent_target(run: dict[str, Any]) -> dict[str, Any]:
    target_id = str(run.get("target_id") or "")
    if not target_id or "://" in target_id:
        raise AutomationExecutionError("external_target", "Automation agent target must be an internal workspace session.")
    if websockets is None:
        raise AutomationExecutionError("agent_client_unavailable", "The platform worker cannot open the internal agent protocol.")
    payload = run.get("payload") or {}
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, str) or not message.strip():
        raise AutomationExecutionError("agent_message_required", "Agent automation requires a non-empty message.")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT s.id,s.user_id,s.workspace_id,COALESCE(m.role,'member') AS role,s.status,s.last_seq
            FROM ag_session s
            LEFT JOIN id_member m ON m.user_id=s.user_id AND m.workspace_id=s.workspace_id
            WHERE s.id=%s AND s.workspace_id=%s AND s.status <> 'archived'
            """,
            (target_id, run["workspace_id"]),
        )
        session = await result.fetchone()
    if not session:
        raise AutomationExecutionError("agent_session_not_found", "Internal workspace agent session was not found.")
    if session["status"] != "idle":
        raise AutomationExecutionError("agent_session_busy", f"Agent session is not idle: {session['status']}.")

    ticket = secrets.token_urlsafe(32)
    await redis.set(
        f"ws-ticket:{ticket}",
        json.dumps({"user_id": session["user_id"], "workspace_id": run["workspace_id"], "role": session.get("role", "member")}),
        ex=60,
    )
    request_id = f"automation_{run['id']}"
    last_seq = int(session.get("last_seq") or 0)
    received = 0
    completed_message: dict[str, Any] | None = None
    failure: tuple[str, str] | None = None
    terminal_status: str | None = None
    try:
        async with websockets.connect(
            _agent_websocket_uri(target_id, ticket, last_seq),
            open_timeout=10,
            close_timeout=10,
            ping_interval=None,
        ) as socket:
            deadline = asyncio.get_running_loop().time() + AUTOMATION_AGENT_TIMEOUT_SECONDS
            sent = False
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise AutomationExecutionError("agent_timeout", "Agent automation did not reach a terminal state.", executed=sent)
                raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
                event = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                event_type = event.get("type")
                if event_type == "connection.ready" and not sent:
                    attachment_ids = payload.get("attachment_ids", []) if isinstance(payload, dict) else []
                    if not isinstance(attachment_ids, list):
                        attachment_ids = []
                    await socket.send(json.dumps({
                        "type": "message.create", "content": message.strip(),
                        "attachment_ids": [str(value) for value in attachment_ids[:100]], "request_id": request_id,
                    }, ensure_ascii=False))
                    sent = True
                sequence = event.get("seq")
                if isinstance(sequence, int):
                    await socket.send(json.dumps({"type": "event.ack", "seq": sequence}))
                if not sent:
                    continue
                received += 1
                if event_type == "agent.message.completed":
                    completed_message = event.get("payload") or {}
                elif event_type == "error":
                    failure = (str(event.get("code") or "agent_execution_failed"), str(event.get("message") or "Agent execution failed")[:500])
                elif event_type == "session.status":
                    status_payload = event.get("payload") or {}
                    terminal_status = str(status_payload.get("to") or "")
                    if terminal_status in {"idle", "cancelled"}:
                        break
    except AutomationExecutionError:
        raise
    except Exception as exc:
        raise AutomationExecutionError("agent_execution_failed", str(exc)[:500], executed=bool(received)) from exc
    finally:
        try:
            await redis.delete(f"ws-ticket:{ticket}")
        except Exception:
            LOGGER.warning("failed to remove automation agent ticket", extra={"run_id": run["id"]})
    if failure:
        raise AutomationExecutionError(failure[0], failure[1], executed=True)
    if terminal_status == "cancelled":
        return {"status": "cancelled", "execution_status": "cancelled", "executed": True, "session_id": target_id, "events": received}
    if terminal_status != "idle":
        raise AutomationExecutionError("agent_execution_incomplete", "Agent session closed without a terminal state.", executed=True)
    return {
        "status": "succeeded", "execution_status": "succeeded", "executed": True,
        "session_id": target_id, "events": received,
        "message": automation._redact_payload(completed_message or {}),
    }


async def _execute_automation_target(run: dict[str, Any]) -> dict[str, Any]:
    target_type = str(run.get("target_type") or "")
    if target_type == "workflow":
        return await _execute_workflow_target(run)
    if target_type == "agent":
        return await _execute_agent_target(run)
    return _unsupported_automation_result(run)


async def _persist_automation_result(run: dict[str, Any], execution: dict[str, Any], owner: str) -> bool:
    async with pool.connection() as conn:
        async with conn.transaction():
            finished_result = await conn.execute(
                """
                UPDATE ops_automation_run
                SET status=%s,error_code=%s,error_message=%s,completed_at=now()
                WHERE id=%s AND status='running'
                RETURNING *
                """,
                (execution["status"], execution.get("error_code"), execution.get("error_message"), run["id"]),
            )
            finished = await finished_result.fetchone()
            if not finished:
                LOGGER.warning("automation run state changed before completion", extra={"run_id": run["id"]})
                return False
            duration_ms = round((perf_counter() - float(run.get("_started_at", perf_counter()))) * 1000, 3)
            result_payload = {
                "run_id": run["id"], "schedule_id": run["schedule_id"], "workspace_id": run["workspace_id"],
                "target_type": run.get("target_type"), "target_id": run.get("target_id"),
                "status": execution["status"], "execution_status": execution.get("execution_status", execution["status"]),
                "executed": bool(execution.get("executed")), "error_code": execution.get("error_code"),
                "error_message": execution.get("error_message"), "worker_id": owner, "duration_ms": duration_ms,
            }
            result_payload.update({key: automation._redact_payload(value) for key, value in execution.items() if key not in {"status", "execution_status", "executed", "error_code", "error_message"}})
            await conn.execute(
                """
                INSERT INTO ops_outbox(id,event_type,workspace_id,trace_id,payload)
                VALUES (%s,%s,%s,%s,%s::jsonb) ON CONFLICT(id) DO NOTHING
                """,
                (f"out_{run['id']}_result", AUTOMATION_RESULT_EVENT, run["workspace_id"], run["id"], json_dumps(result_payload)),
            )
            recipient_id = run.get("triggered_by") or run.get("created_by")
            if recipient_id:
                await create_automation_run_notification(
                    conn, user_id=recipient_id, workspace_id=run["workspace_id"], run_id=run["id"],
                    target_type=str(run.get("target_type") or "unknown"), target_id=str(run.get("target_id") or ""),
                    status=execution["status"], error_code=execution.get("error_code"), error_message=execution.get("error_message"),
                )
    return True


async def process_automation_runs(
    worker_id: str | None = None, *, limit: int = AUTOMATION_RUN_LIMIT
) -> dict[str, int]:
    """Claim queued runs, execute internal actions, and persist one terminal result."""
    if limit < 1:
        return {"claimed": 0, "succeeded": 0, "failed": 0, "unsupported": 0, "pending": 0}
    owner = worker_id or f"platform-worker-{new_id('wrk')}"
    summary = {"claimed": 0, "succeeded": 0, "failed": 0, "unsupported": 0, "pending": 0}
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                SELECT r.*, s.target_type, s.target_id, s.created_by
                FROM ops_automation_run AS r
                JOIN ops_automation_schedule AS s ON s.id = r.schedule_id
                WHERE r.status = 'queued'
                ORDER BY r.created_at, r.id
                LIMIT %s
                FOR UPDATE OF r SKIP LOCKED
                """,
                (min(limit, 100),),
            )
            queued_runs = await result.fetchall()
            claimed_runs: list[dict[str, Any]] = []
            for queued in queued_runs:
                claimed_result = await conn.execute(
                    """
                    UPDATE ops_automation_run SET status='running'
                    WHERE id=%s AND status='queued' RETURNING *
                    """,
                    (queued["id"],),
                )
                claimed = await claimed_result.fetchone()
                if claimed:
                    claimed_runs.append({**queued, **claimed})
                    summary["claimed"] += 1
    for run in claimed_runs:
        run["_started_at"] = perf_counter()
        try:
            execution = await _execute_automation_target(run)
        except AutomationExecutionError as exc:
            execution = {
                "status": "failed", "execution_status": exc.execution_status, "executed": exc.executed,
                "error_code": exc.code, "error_message": str(exc),
            }
        except Exception as exc:
            LOGGER.exception("automation target execution failed", extra={"run_id": run["id"], "target_type": run.get("target_type")})
            execution = {
                "status": "failed", "execution_status": "failed", "executed": False,
                "error_code": "execution_failed", "error_message": str(exc)[:500],
            }
        if not await _persist_automation_result(run, execution, owner):
            continue
        if execution["status"] == "succeeded":
            summary["succeeded"] += 1
        elif execution["status"] == "cancelled":
            summary["failed"] += 1
        else:
            summary["failed"] += 1
        if execution.get("execution_status") == "unsupported":
            summary["unsupported"] += 1
        AUTOMATION_RUNS.add(1, {"result": execution["execution_status"], "target_type": str(run.get("target_type") or "unknown")})
    return summary


async def automation_loop() -> None:
    worker_id = f"platform-worker-{new_id('wrk')}"
    while True:
        started = perf_counter()
        result_label = "success"
        try:
            with TRACER.start_as_current_span("worker.automation") as span:
                scan = await scan_due_automation_schedules()
                runs = await process_automation_runs(worker_id)
                span.set_attribute("wama.automation.schedules_scanned", scan["scanned"])
                span.set_attribute("wama.automation.runs_claimed", runs["claimed"])
                if scan["scanned"] or runs["claimed"]:
                    LOGGER.info("automation batch processed", extra={**scan, **runs, "worker_id": worker_id})
        except Exception:
            result_label = "error"
            LOGGER.exception("automation batch failed")
        BATCHES.add(1, {"operation": "automation", "result": result_label})
        BATCH_DURATION.record(perf_counter() - started, {"operation": "automation", "result": result_label})
        await asyncio.sleep(AUTOMATION_POLL_SECONDS)


async def reconciliation_loop() -> None:
    while True:
        started = perf_counter()
        result_label = "success"
        business_date = datetime.now(UTC).date() - timedelta(days=1)
        try:
            with TRACER.start_as_current_span("worker.reconciliation"):
                expired = await expire_credit_grants()
                results = await run_daily_reconciliation(business_date)
                LOGGER.info("daily reconciliation completed", extra={"business_date": str(business_date), "workspaces": len(results), "expired_credit_grants": len(expired)})
        except Exception:
            result_label = "error"
            LOGGER.exception("daily reconciliation failed", extra={"business_date": str(business_date)})
        BATCHES.add(1, {"operation": "reconciliation", "result": result_label})
        BATCH_DURATION.record(perf_counter() - started, {"operation": "reconciliation", "result": result_label})
        await asyncio.sleep(3600)


async def notification_delivery_loop() -> None:
    while True:
        started = perf_counter()
        result_label = "success"
        try:
            with TRACER.start_as_current_span("worker.notification_delivery"):
                result = await process_pending_email_deliveries()
                if result["claimed"]:
                    LOGGER.info("notification deliveries processed", extra=result)
        except Exception:
            result_label = "error"
            LOGGER.exception("notification delivery batch failed")
        BATCHES.add(1, {"operation": "notification_delivery", "result": result_label})
        BATCH_DURATION.record(perf_counter() - started, {"operation": "notification_delivery", "result": result_label})
        await asyncio.sleep(30)


async def process_pending_siem_deliveries(
    limit: int = 20,
    *,
    transport=None,
    executor=None,
) -> dict[str, int]:
    """Claim and advance SIEM deliveries without holding DB locks over network I/O."""
    if limit < 1:
        return {"claimed": 0, "delivered": 0, "retried": 0, "failed": 0, "disabled": 0}

    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                SELECT d.id,d.config_id,d.workspace_id,d.event_type,d.idempotency_key,d.payload_hash,
                       d.status,d.attempt,d.next_attempt_at,d.claimed_at,c.endpoint,c.credential_hash
                FROM sec_siem_delivery d
                JOIN sec_siem_config c ON c.id=d.config_id
                WHERE c.enabled=TRUE
                  AND (
                    (d.status IN ('pending_external','retry_wait') AND COALESCE(d.next_attempt_at, now()) <= now())
                    OR (d.status='delivering' AND d.claimed_at < now() - make_interval(mins => 5))
                  )
                ORDER BY d.created_at,d.id
                LIMIT %s
                FOR UPDATE OF d SKIP LOCKED
                """,
                (min(limit, 100),),
            )
            items = await result.fetchall()
            for item in items:
                item["attempt"] = int(item["attempt"] or 0) + 1
                await conn.execute(
                    """
                    UPDATE sec_siem_delivery
                    SET status='delivering',attempt=%s,claimed_at=now(),updated_at=now()
                    WHERE id=%s
                    """,
                    (item["attempt"], item["id"]),
                )

    summary = {"claimed": len(items), "delivered": 0, "retried": 0, "failed": 0, "disabled": 0}
    for item in items:
        kwargs = {}
        if transport is not None:
            kwargs["transport"] = transport
        if executor is not None:
            kwargs["executor"] = executor
        try:
            outcome = await deliver_siem_attempt(item, **kwargs)
        except Exception:
            LOGGER.exception("siem delivery attempt failed", extra={"delivery_id": item["id"]})
            outcome = {
                "success": False,
                "response_code": None,
                "error_code": "siem_worker_internal_error",
                "retryable": True,
                "disable": False,
                "signature": None,
                "summary": {"status_code": None, "response_bytes": 0, "reason": "siem_worker_internal_error"},
            }

        attempt = item["attempt"]
        if outcome.get("success"):
            new_status = "delivered"
            next_attempt_at = None
            summary["delivered"] += 1
        elif outcome.get("disable"):
            new_status = "disabled"
            next_attempt_at = None
            summary["disabled"] += 1
        elif outcome.get("retryable") and attempt < SIEM_MAX_ATTEMPTS:
            new_status = "retry_wait"
            next_attempt_at = datetime.now(UTC) + timedelta(seconds=siem_retry_delay(attempt))
            summary["retried"] += 1
        else:
            new_status = "failed"
            next_attempt_at = None
            summary["failed"] += 1

        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE sec_siem_delivery
                    SET status=%s,next_attempt_at=%s,response_code=%s,error_code=%s,signature=%s,
                        response_summary=%s::jsonb,claimed_at=NULL,delivered_at=%s,updated_at=now()
                    WHERE id=%s AND status='delivering'
                    """,
                    (
                        new_status,
                        next_attempt_at,
                        outcome.get("response_code"),
                        outcome.get("error_code"),
                        outcome.get("signature"),
                        json_dumps(outcome.get("summary") or {}),
                        datetime.now(UTC) if new_status == "delivered" else None,
                        item["id"],
                    ),
                )
                if new_status == "disabled":
                    await conn.execute(
                        "UPDATE sec_siem_config SET enabled=FALSE,version=version+1,updated_at=now() WHERE id=%s AND enabled=TRUE",
                        (item["config_id"],),
                    )
    return summary


async def siem_delivery_loop() -> None:
    while True:
        started = perf_counter()
        result_label = "success"
        try:
            with TRACER.start_as_current_span("worker.siem_delivery"):
                result = await process_pending_siem_deliveries()
                if result["claimed"]:
                    LOGGER.info("siem deliveries processed", extra=result)
        except Exception:
            result_label = "error"
            LOGGER.exception("siem delivery batch failed")
        BATCHES.add(1, {"operation": "siem_delivery", "result": result_label})
        BATCH_DURATION.record(perf_counter() - started, {"operation": "siem_delivery", "result": result_label})
        await asyncio.sleep(5)


async def process_pending_webhook_deliveries(
    limit: int = 20,
    *,
    transport=None,
    executor=None,
) -> dict[str, int]:
    """Claim and advance webhook deliveries without holding DB locks over network I/O."""
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                SELECT d.id,d.webhook_id,d.event_type,d.idempotency_key,d.payload,d.status,d.attempt,
                       d.next_attempt_at,w.url,w.secret_hash,d.delivery_mode,d.claimed_at
                FROM pf_webhook_delivery d
                JOIN pf_webhook w ON w.id=d.webhook_id
                WHERE w.status='active'
                  AND (
                    (d.status IN ('pending','retry_wait','blocked_external') AND COALESCE(d.next_attempt_at, now()) <= now())
                    OR (d.status='delivering' AND d.claimed_at < now() - make_interval(mins => 5))
                  )
                ORDER BY d.created_at
                LIMIT %s
                FOR UPDATE OF d SKIP LOCKED
                """,
                (limit,),
            )
            items = await result.fetchall()
            for item in items:
                item["attempt"] = int(item["attempt"] or 0) + 1
                await conn.execute(
                    """
                    UPDATE pf_webhook_delivery
                    SET status='delivering', attempt=%s, claimed_at=now(), updated_at=now()
                    WHERE id=%s
                    """,
                    (item["attempt"], item["id"]),
                )

    summary = {"claimed": len(items), "delivered": 0, "retried": 0, "failed": 0, "disabled": 0}
    for item in items:
        kwargs = {}
        if transport is not None:
            kwargs["transport"] = transport
        if executor is not None:
            kwargs["executor"] = executor
        try:
            outcome = await deliver_webhook_attempt(item, **kwargs)
        except Exception:
            LOGGER.exception("webhook delivery attempt failed", extra={"delivery_id": item["id"]})
            outcome = {
                "success": False,
                "response_code": None,
                "error_code": "worker_internal_error",
                "retryable": True,
                "disable": False,
                "signature": None,
                "summary": {"status_code": None, "response_bytes": 0, "reason": "worker_internal_error"},
            }

        attempt = item["attempt"]
        if outcome.get("success"):
            new_status = "delivered"
            next_attempt_at = None
            summary["delivered"] += 1
        elif outcome.get("disable"):
            new_status = "disabled"
            next_attempt_at = None
            summary["disabled"] += 1
        elif outcome.get("retryable") and attempt < WEBHOOK_MAX_ATTEMPTS:
            new_status = "retry_wait"
            next_attempt_at = datetime.now(UTC) + timedelta(seconds=webhook_retry_delay(attempt))
            summary["retried"] += 1
        else:
            new_status = "failed"
            next_attempt_at = None
            summary["failed"] += 1

        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE pf_webhook_delivery
                    SET status=%s,next_attempt_at=%s,response_code=%s,error_code=%s,signature=%s,
                        response_summary=%s::jsonb,claimed_at=NULL,delivered_at=%s,updated_at=now()
                    WHERE id=%s AND status='delivering'
                    """,
                    (
                        new_status,
                        next_attempt_at,
                        outcome.get("response_code"),
                        outcome.get("error_code"),
                        outcome.get("signature"),
                        json_dumps(outcome.get("summary") or {}),
                        datetime.now(UTC) if new_status == "delivered" else None,
                        item["id"],
                    ),
                )
                if new_status == "delivered":
                    await conn.execute(
                        "UPDATE pf_webhook SET failure_count=0,last_delivered_at=now(),updated_at=now() WHERE id=%s",
                        (item["webhook_id"],),
                    )
                elif new_status == "disabled":
                    await conn.execute(
                        "UPDATE pf_webhook SET status='disabled',failure_count=failure_count+1,updated_at=now() WHERE id=%s AND status='active'",
                        (item["webhook_id"],),
                    )
                elif new_status == "failed":
                    await conn.execute(
                        "UPDATE pf_webhook SET failure_count=failure_count+1,updated_at=now() WHERE id=%s",
                        (item["webhook_id"],),
                    )
    return summary


async def webhook_delivery_loop() -> None:
    while True:
        started = perf_counter()
        result_label = "success"
        try:
            with TRACER.start_as_current_span("worker.webhook_delivery"):
                result = await process_pending_webhook_deliveries()
                if result["claimed"]:
                    LOGGER.info("webhook deliveries processed", extra=result)
        except Exception:
            result_label = "error"
            LOGGER.exception("webhook delivery batch failed")
        BATCHES.add(1, {"operation": "webhook_delivery", "result": result_label})
        BATCH_DURATION.record(perf_counter() - started, {"operation": "webhook_delivery", "result": result_label})
        await asyncio.sleep(5)


async def privacy_request_loop() -> None:
    while True:
        started = perf_counter()
        result_label = "success"
        try:
            with TRACER.start_as_current_span("worker.privacy_request"):
                result = await process_pending_data_requests()
                if result["claimed"]:
                    LOGGER.info("privacy data requests processed", extra=result)
        except Exception:
            result_label = "error"
            LOGGER.exception("privacy data request batch failed")
        BATCHES.add(1, {"operation": "privacy_request", "result": result_label})
        BATCH_DURATION.record(perf_counter() - started, {"operation": "privacy_request", "result": result_label})
        await asyncio.sleep(5)


async def process_pending_external_app_invocations(
    worker_id: str,
    limit: int = 10,
    *,
    transport=None,
) -> dict[str, int]:
    """Claim explicit external HTTP invocations and advance one bounded attempt.

    The lease is released before network I/O.  A retryable response is put back
    in the queue, so another worker can safely take the next attempt after the
    bounded delay.
    """
    if limit < 1:
        return {"claimed": 0, "succeeded": 0, "retried": 0, "failed": 0, "blocked": 0}

    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                SELECT i.id,i.app_id,i.workspace_id,i.operation,i.input_hash,i.payload,
                       i.status,i.execution_mode,i.result,i.error_code,i.attempt,i.max_attempts,
                       i.next_attempt_at,i.claimed_at,i.lease_owner,i.lease_expires_at,i.created_by,
                       a.provider,a.endpoint,a.config,a.credential_hash
                FROM pf_external_app_invocation i
                JOIN pf_external_app a ON a.id=i.app_id
                WHERE a.status='active'
                  AND a.enabled=TRUE
                  AND a.provider IN ('dify','fastgpt','ragflow')
                  AND i.execution_mode='external_http'
                  AND a.credential_hash IS NOT NULL
                  AND (
                    (i.status IN ('queued','pending_external') AND COALESCE(i.next_attempt_at, now()) <= now())
                    OR (i.status='running' AND i.lease_expires_at < now())
                  )
                ORDER BY i.created_at,i.id
                LIMIT %s
                FOR UPDATE OF i SKIP LOCKED
                """,
                (min(limit, 100),),
            )
            items = await result.fetchall()
            for item in items:
                item["attempt"] = int(item.get("attempt") or 0) + 1
                await conn.execute(
                    """
                    UPDATE pf_external_app_invocation
                    SET status='running',attempt=%s,last_attempt_at=now(),claimed_at=now(),
                        lease_owner=%s,lease_expires_at=now() + make_interval(secs => %s),
                        next_attempt_at=NULL
                    WHERE id=%s
                    """,
                    (item["attempt"], worker_id, external_apps._EXTERNAL_HTTP_LEASE_SECONDS, item["id"]),
                )

    summary = {"claimed": len(items), "succeeded": 0, "retried": 0, "failed": 0, "blocked": 0}
    for item in items:
        try:
            block_reason = external_apps.external_http_block_reason(item)
        except Exception:
            block_reason = "invalid_execution_config"
        if block_reason:
            outcome = {
                "success": False,
                "attempts": 0,
                "response_code": None,
                "error_code": block_reason,
                "retryable": False,
                "result": {
                    "execution": external_apps._EXTERNAL_HTTP_MODE,
                    "provider_request_sent": False,
                    "blocked": True,
                    "error_code": block_reason,
                },
            }
        else:
            kwargs = {"transport": transport} if transport is not None else {}
            try:
                outcome = await external_apps.external_http_execution(
                    item["provider"],
                    item["endpoint"],
                    item["operation"],
                    dict(item.get("payload") or {}),
                    item["input_hash"],
                    item.get("config"),
                    workspace_id=item["workspace_id"],
                    actor_id=item.get("created_by"),
                    invocation_id=item["id"],
                    **kwargs,
                )
            except Exception:
                LOGGER.exception("external app invocation attempt failed", extra={"invocation_id": item["id"]})
                outcome = {
                    "success": False,
                    "attempts": 1,
                    "response_code": None,
                    "error_code": "external_worker_internal_error",
                    "retryable": False,
                    "result": {
                        "execution": external_apps._EXTERNAL_HTTP_MODE,
                        "provider_request_sent": False,
                        "error_code": "external_worker_internal_error",
                    },
                }

        attempt = item["attempt"]
        max_attempts = int(item.get("max_attempts") or 1)
        if outcome.get("success"):
            new_status = "succeeded"
            next_attempt_at = None
            completed_at = datetime.now(UTC)
            summary["succeeded"] += 1
        elif block_reason:
            new_status = "pending_external"
            next_attempt_at = None
            completed_at = None
            summary["blocked"] += 1
        elif outcome.get("retryable") and attempt < max_attempts:
            new_status = "queued"
            next_attempt_at = datetime.now(UTC) + timedelta(
                seconds=external_apps.external_http_retry_delay(attempt, item.get("config"))
            )
            completed_at = None
            summary["retried"] += 1
        else:
            new_status = "failed"
            next_attempt_at = None
            completed_at = datetime.now(UTC)
            summary["failed"] += 1

        async with pool.connection() as conn:
            async with conn.transaction():
                finalized_result = await conn.execute(
                    """
                    UPDATE pf_external_app_invocation
                    SET status=%s,result=%s::jsonb,error_code=%s,response_code=%s,
                        next_attempt_at=%s,completed_at=%s,claimed_at=NULL,
                        lease_owner=NULL,lease_expires_at=NULL
                    WHERE id=%s AND status='running' AND lease_owner=%s
                    RETURNING id
                    """,
                    (
                        new_status,
                        json_dumps(outcome.get("result") or {}),
                        outcome.get("error_code") if new_status != "succeeded" else None,
                        outcome.get("response_code"),
                        next_attempt_at,
                        completed_at,
                        item["id"],
                        worker_id,
                    ),
                )
                finalized = await finalized_result.fetchone()
                if finalized and new_status == "succeeded":
                    await settle_meter_in_transaction(
                        conn,
                        MeterRequest(
                            request_id=item["id"],
                            workspace_id=item["workspace_id"],
                            model=f"external-app:{item['provider']}",
                            prompt_tokens=max(1, len(json_dumps(item.get("payload") or {}).encode()) // 4),
                            completion_tokens=max(1, len(json_dumps(outcome["result"]).encode()) // 4),
                            status_code=outcome.get("response_code") or 200,
                        ),
                    )
    return summary


async def channel_extension_cleanup_loop() -> None:
    worker_id = f"platform-worker-ch-ext-{new_id('wrk')}"
    while True:
        started = perf_counter()
        result_label = "success"
        try:
            with TRACER.start_as_current_span("worker.channel_extension_cleanup"):
                cleanup = await channel_extensions.cleanup_expired_sessions(worker_id)
                renew = await channel_extensions.renew_expired_leases(worker_id)
                if cleanup["cleaned"] or renew["claimed"]:
                    LOGGER.info("channel extension cleanup processed", extra={"cleanup": cleanup, "renew": renew})
        except Exception:
            result_label = "error"
            LOGGER.exception("channel extension cleanup batch failed")
        BATCHES.add(1, {"operation": "channel_extension_cleanup", "result": result_label})
        BATCH_DURATION.record(perf_counter() - started, {"operation": "channel_extension_cleanup", "result": result_label})
        await asyncio.sleep(10)


async def external_app_invocation_loop() -> None:
    worker_id = f"platform-worker-external-{new_id('wrk')}"
    while True:
        started = perf_counter()
        result_label = "success"
        try:
            with TRACER.start_as_current_span("worker.external_app_invocation"):
                result = await process_pending_external_app_invocations(worker_id)
                if result["claimed"]:
                    LOGGER.info("external app invocations processed", extra=result)
        except Exception:
            result_label = "error"
            LOGGER.exception("external app invocation batch failed")
        BATCHES.add(1, {"operation": "external_app_invocation", "result": result_label})
        BATCH_DURATION.record(perf_counter() - started, {"operation": "external_app_invocation", "result": result_label})
        await asyncio.sleep(2)


async def process_workflow_run_job(job) -> dict[str, Any]:
    payload = job.payload
    run_id = str(payload.get("run_id") or "")
    workflow_id = str(payload.get("workflow_id") or "")
    if not run_id or not workflow_id:
        raise ValueError("workflow job payload is missing run_id or workflow_id")

    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                SELECT r.*, w.graph, w.version AS current_workflow_version
                FROM pf_workflow_run r
                JOIN pf_workflow w ON w.id=r.workflow_id AND w.workspace_id=r.workspace_id
                WHERE r.id=%s AND r.workflow_id=%s AND r.workspace_id=%s
                FOR UPDATE
                """,
                (run_id, workflow_id, job.workspace_id),
            )
            run = await result.fetchone()
            if not run:
                raise ValueError("workflow run was not found for queued job")
            if run["status"] in {"succeeded", "failed", "cancelled", "pending_approval"}:
                if run["status"] == "pending_approval":
                    timeout_at = run.get("timeout_at")
                    if timeout_at and datetime.now(UTC) >= timeout_at:
                        await conn.execute(
                            """
                            UPDATE pf_workflow_run
                            SET status='failed', error='Workflow approval timed out', error_category='approval_timeout', completed_at=now()
                            WHERE id=%s AND workspace_id=%s AND status='pending_approval'
                            RETURNING id
                            """,
                            (run_id, job.workspace_id),
                        )
                        await workflows.append_workflow_event(
                            conn,
                            run_id=run_id,
                            workflow_id=workflow_id,
                            workspace_id=job.workspace_id,
                            event_type="workflow.run.failed",
                            payload={"run_id": run_id, "workflow_id": workflow_id, "status": "failed", "error": "Workflow approval timed out", "error_category": "approval_timeout"},
                        )
                        return {"run_id": run_id, "workflow_id": workflow_id, "status": "failed", "skipped": True, "error": "Workflow approval timed out"}
                return {"run_id": run_id, "workflow_id": workflow_id, "status": run["status"], "skipped": True}
            await conn.execute(
                "UPDATE pf_workflow_run SET status='running', started_at=COALESCE(started_at, now()) WHERE id=%s",
                (run_id,),
            )
            started = await conn.execute(
                "SELECT 1 FROM pf_workflow_event WHERE run_id=%s AND event_type='workflow.run.started' LIMIT 1",
                (run_id,),
            )
            if not await started.fetchone():
                await workflows.append_workflow_event(
                    conn,
                    run_id=run_id,
                    workflow_id=workflow_id,
                    workspace_id=job.workspace_id,
                    event_type="workflow.run.started",
                    payload={"run_id": run_id, "workflow_id": workflow_id, "dry_run": bool(payload.get("dry_run"))},
                )

    async def emit(event_type: str, event_payload: dict[str, Any]) -> None:
        async with pool.connection() as conn:
            async with conn.transaction():
                await workflows.append_workflow_event(
                    conn,
                    run_id=run_id,
                    workflow_id=workflow_id,
                    workspace_id=job.workspace_id,
                    event_type=event_type,
                    payload=event_payload,
                )

    async def is_cancelled() -> bool:
        async with pool.connection() as conn:
            if not await heartbeat(conn, job, stage="workflow.running"):
                raise RuntimeError("workflow job lease was lost")
            result = await conn.execute(
                "SELECT status FROM ops_async_operation WHERE id=%s AND workspace_id=%s",
                (job.operation_id, job.workspace_id),
            )
            operation = await result.fetchone()
            await conn.commit()
        return bool(operation and operation["status"] in {"cancel_requested", "cancelled"})

    try:
        gateway_api_key = decrypt_secret(payload.get("gateway_api_key_enc"))
        run_status, output, trace_data, error = await workflows.execute_graph(
            run["graph"],
            payload.get("input") if isinstance(payload.get("input"), dict) else {},
            gateway_api_key,
            bool(payload.get("dry_run")),
            emit,
            is_cancelled,
            workspace_id=job.workspace_id,
            sandbox_session_id=run_id,
        )
    except Exception:
        raise

    event_type = {
        "succeeded": "workflow.run.completed",
        "pending_approval": "workflow.run.pending_approval",
        "cancelled": "workflow.run.cancelled",
    }.get(run_status, "workflow.run.failed")
    timeout_at = None
    if run_status == "pending_approval" and isinstance(output, dict):
        approval_info = output.get("approval") or {}
        timeout_at = approval_info.get("timeout_at")
    async with pool.connection() as conn:
        async with conn.transaction():
            update_fields = "output=%s::jsonb, trace=%s::jsonb, status=%s, error=%s, completed_at=now()"
            params: list[Any] = [json_dumps(output), json_dumps(trace_data), run_status, error]
            if timeout_at:
                update_fields += ", timeout_at=%s"
                params.append(timeout_at)
            params.extend([run_id, job.workspace_id])
            result = await conn.execute(
                f"""
                UPDATE pf_workflow_run
                SET {update_fields}
                WHERE id=%s AND workspace_id=%s AND status='running'
                RETURNING id,status
                """,
                tuple(params),
            )
            if not await result.fetchone():
                raise ValueError("workflow run changed state before completion")
            await workflows.append_workflow_event(
                conn,
                run_id=run_id,
                workflow_id=workflow_id,
                workspace_id=job.workspace_id,
                event_type=event_type,
                payload={"run_id": run_id, "workflow_id": workflow_id, "status": run_status, "error": error},
            )
    return {
        "run_id": run_id,
        "workflow_id": workflow_id,
        "status": run_status,
        "error": error,
        "_operation_cancelled": run_status == "cancelled",
    }


async def mark_workflow_run_failed(job, error_message: str, error_category: str = "execution_error") -> None:
    payload = job.payload
    run_id = str(payload.get("run_id") or "")
    workflow_id = str(payload.get("workflow_id") or "")
    if not run_id or not workflow_id:
        return
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                UPDATE pf_workflow_run
                SET status='failed', error=%s, error_category=%s, completed_at=now()
                WHERE id=%s AND workflow_id=%s AND workspace_id=%s
                  AND status NOT IN ('succeeded','failed','cancelled','pending_approval')
                RETURNING id
                """,
                (error_message[:500], error_category, run_id, workflow_id, job.workspace_id),
            )
            if await result.fetchone():
                await workflows.append_workflow_event(
                    conn,
                    run_id=run_id,
                    workflow_id=workflow_id,
                    workspace_id=job.workspace_id,
                    event_type="workflow.run.failed",
                    payload={"run_id": run_id, "workflow_id": workflow_id, "status": "failed", "error": error_message[:500]},
                )


class WorkExecutionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _work_actor(job, created_by: str | None = None) -> Actor:
    payload = job.payload or {}
    return Actor(
        user_id=str(payload.get("actor_id") or created_by or ""),
        workspace_id=job.workspace_id,
        org_id=str(payload.get("org_id") or ""),
        role=str(payload.get("actor_role") or "member"),
        email="",
        display_name="WorkAMA worker",
        onboarding_completed=True,
        actor_type="system",
    )


async def _work_append_event(
    conn,
    job,
    plan_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    task_id: str | None = None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if plan is None:
        result = await conn.execute(
            """
            SELECT id,workspace_id,session_id,title,objective,status,last_event_seq,
                   created_by,created_at,updated_at
            FROM work_plan WHERE id=%s AND workspace_id=%s FOR UPDATE
            """,
            (plan_id, job.workspace_id),
        )
        plan = await result.fetchone()
    if not plan:
        raise WorkExecutionError("work_plan_not_found", "Work plan was not found for the queued execution.")
    return await work._append_event(  # noqa: SLF001 - worker owns the event-sourced execution boundary.
        conn,
        plan=plan,
        actor=_work_actor(job, plan.get("created_by")),
        task_id=task_id,
        event_type=event_type,
        payload=payload,
    )


async def _work_cancel_requested(job, *, progress: int | None = None, stage: str | None = None) -> bool:
    async with pool.connection() as conn:
        if not await heartbeat(conn, job, progress=progress, stage=stage):
            raise WorkExecutionError("work_lease_lost", "Work execution lease was lost.")
        result = await conn.execute(
            "SELECT status FROM ops_async_operation WHERE id=%s AND workspace_id=%s",
            (job.operation_id, job.workspace_id),
        )
        operation = await result.fetchone()
        await conn.commit()
    return bool(operation and operation["status"] in {"cancel_requested", "cancelled"})


async def _mark_work_plan_cancelled(job, reason: str) -> dict[str, Any]:
    payload = job.payload or {}
    plan_id = str(payload.get("plan_id") or "")
    if not plan_id:
        raise WorkExecutionError("work_payload_invalid", "Work execution payload is missing plan_id.")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                SELECT id,workspace_id,session_id,title,objective,status,last_event_seq,
                       created_by,created_at,updated_at
                FROM work_plan WHERE id=%s AND workspace_id=%s FOR UPDATE
                """,
                (plan_id, job.workspace_id),
            )
            plan = await result.fetchone()
            if not plan:
                raise WorkExecutionError("work_plan_not_found", "Work plan was not found for cancellation.")
            if plan["status"] not in {"succeeded", "failed", "cancelled"}:
                task_result = await conn.execute(
                    """
                    SELECT id,status FROM work_task
                    WHERE plan_id=%s AND workspace_id=%s AND status NOT IN ('done','cancelled')
                    ORDER BY position,id FOR UPDATE
                    """,
                    (plan_id, job.workspace_id),
                )
                for task in await task_result.fetchall():
                    await conn.execute(
                        "UPDATE work_task SET status='cancelled',updated_at=now() WHERE id=%s AND plan_id=%s AND workspace_id=%s",
                        (task["id"], plan_id, job.workspace_id),
                    )
                    await _work_append_event(
                        conn,
                        job,
                        plan_id,
                        "task.execution.cancelled",
                        {"previous_status": task["status"], "reason": reason[:500], "operation_id": job.operation_id},
                        task_id=task["id"],
                        plan=plan,
                    )
                await conn.execute(
                    "UPDATE work_plan SET status='cancelled',updated_at=now() WHERE id=%s AND workspace_id=%s",
                    (plan_id, job.workspace_id),
                )
                plan["status"] = "cancelled"
                await _work_append_event(
                    conn,
                    job,
                    plan_id,
                    "plan.execution.cancelled",
                    {"plan_id": plan_id, "status": "cancelled", "error": reason[:500], "operation_id": job.operation_id},
                    plan=plan,
                )
            await conn.execute(
                "UPDATE work_execution SET status='cancelled',completed_at=now(),updated_at=now() WHERE operation_id=%s AND workspace_id=%s",
                (job.operation_id, job.workspace_id),
            )
    return {"plan_id": plan_id, "status": "cancelled", "error": reason, "_operation_cancelled": True}


async def _mark_work_plan_failed(job, error_message: str, *, error_code: str = "work_execution_failed") -> None:
    payload = job.payload or {}
    plan_id = str(payload.get("plan_id") or "")
    if not plan_id:
        return
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                SELECT id,workspace_id,session_id,title,objective,status,last_event_seq,
                       created_by,created_at,updated_at
                FROM work_plan WHERE id=%s AND workspace_id=%s FOR UPDATE
                """,
                (plan_id, job.workspace_id),
            )
            plan = await result.fetchone()
            if not plan:
                return
            if plan["status"] not in {"succeeded", "failed", "cancelled"}:
                await conn.execute(
                    "UPDATE work_plan SET status='failed',updated_at=now() WHERE id=%s AND workspace_id=%s",
                    (plan_id, job.workspace_id),
                )
                plan["status"] = "failed"
                await _work_append_event(
                    conn,
                    job,
                    plan_id,
                    "plan.execution.failed",
                    {"plan_id": plan_id, "status": "failed", "error_code": error_code, "error": error_message[:500], "operation_id": job.operation_id},
                    plan=plan,
                )
            await conn.execute(
                "UPDATE work_execution SET status='failed',completed_at=now(),updated_at=now() WHERE operation_id=%s AND workspace_id=%s",
                (job.operation_id, job.workspace_id),
            )


async def _persist_work_research_artifact(
    job,
    plan_id: str,
    generated: work.OfficeArtifact,
    *,
    validation: dict[str, Any],
) -> dict[str, Any]:
    artifact_id = new_id("wart")
    filename = work._safe_filename(f"{plan_id}-deep-research", generated.extension)  # noqa: SLF001 - worker uses the Work naming boundary.
    s3_key = f"artifacts/{job.workspace_id}/{artifact_id}/v1/{filename}"
    content_sha256 = hashlib.sha256(generated.data).hexdigest()
    await put_object(work.ARTIFACT_BUCKET, s3_key, generated.data)
    preview = {
        "format": generated.extension,
        "kind": "research_report",
        "operation_id": job.operation_id,
        "content_sha256": content_sha256,
        "source_count": validation.get("source_count", 0),
        "distinct_fingerprint_count": validation.get("distinct_fingerprint_count", 0),
        "trust_boundary": validation.get("trust_boundary"),
    }
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                SELECT id,workspace_id,session_id,title,objective,status,last_event_seq,
                       created_by,created_at,updated_at
                FROM work_plan WHERE id=%s AND workspace_id=%s FOR UPDATE
                """,
                (plan_id, job.workspace_id),
            )
            plan = await result.fetchone()
            if not plan:
                raise WorkExecutionError("work_plan_not_found", "Work plan disappeared while writing the research report.")
            session_artifact_id = None
            if plan.get("session_id"):
                session_artifact_id = new_id("art")
                await conn.execute(
                    """
                    INSERT INTO ag_artifact(
                        id,session_id,workspace_id,name,content_type,content,kind,s3_key,
                        size_bytes,content_sha256,status,preview,created_at
                    ) VALUES (%s,%s,%s,%s,%s,'','research_report',%s,%s,%s,'ready',%s::jsonb,now())
                    """,
                    (
                        session_artifact_id,
                        plan["session_id"],
                        job.workspace_id,
                        filename,
                        generated.content_type,
                        s3_key,
                        len(generated.data),
                        content_sha256,
                        work.json_dumps(preview),
                    ),
                )
            await conn.execute(
                """
                INSERT INTO work_artifact(
                    id,workspace_id,plan_id,artifact_id,name,kind,content_type,s3_key,
                    size_bytes,content_sha256,status,preview,created_by
                ) VALUES (%s,%s,%s,%s,%s,'research_report',%s,%s,%s,%s,'ready',%s::jsonb,%s)
                """,
                (
                    artifact_id,
                    job.workspace_id,
                    plan_id,
                    session_artifact_id,
                    filename,
                    generated.content_type,
                    s3_key,
                    len(generated.data),
                    content_sha256,
                    work.json_dumps(preview),
                    str((job.payload or {}).get("actor_id") or plan["created_by"]),
                ),
            )
            event = await _work_append_event(
                conn,
                job,
                plan_id,
                "research.report.artifact.created",
                {
                    "artifact_id": artifact_id,
                    "format": generated.extension,
                    "name": filename,
                    "content_sha256": content_sha256,
                    "operation_id": job.operation_id,
                    "source_count": validation.get("source_count", 0),
                },
                plan=plan,
            )
    return {
        "id": artifact_id,
        "artifact_id": session_artifact_id,
        "name": filename,
        "format": generated.extension,
        "content_type": generated.content_type,
        "content_sha256": content_sha256,
        "event": event,
    }


async def process_work_plan_job(job) -> dict[str, Any]:
    payload = job.payload or {}
    plan_id = str(payload.get("plan_id") or "")
    if not plan_id:
        raise WorkExecutionError("work_payload_invalid", "Work execution payload is missing plan_id.")
    source_ids = payload.get("source_ids") or []
    if not isinstance(source_ids, list):
        raise WorkExecutionError("work_payload_invalid", "Work execution source_ids must be a list.")
    execution_mode = str(payload.get("execution_mode") or "plan")
    if execution_mode not in {"plan", "deep_research"}:
        raise WorkExecutionError("work_payload_invalid", "Work execution mode is not supported.")

    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                SELECT id,workspace_id,session_id,title,objective,status,last_event_seq,
                       created_by,created_at,updated_at
                FROM work_plan WHERE id=%s AND workspace_id=%s FOR UPDATE
                """,
                (plan_id, job.workspace_id),
            )
            plan = await result.fetchone()
            if not plan:
                raise WorkExecutionError("work_plan_not_found", "Work plan was not found for the queued execution.")
            if plan["status"] in {"succeeded", "failed", "cancelled"}:
                return {"plan_id": plan_id, "status": plan["status"], "skipped": True}
            await conn.execute(
                "UPDATE work_execution SET status='running',started_at=COALESCE(started_at,now()),updated_at=now() WHERE operation_id=%s AND workspace_id=%s",
                (job.operation_id, job.workspace_id),
            )
            started = await conn.execute(
                "SELECT 1 FROM work_event WHERE plan_id=%s AND event_type='plan.execution.started' AND payload->>'operation_id'=%s LIMIT 1",
                (plan_id, job.operation_id),
            )
            if not await started.fetchone():
                await _work_append_event(
                    conn,
                    job,
                    plan_id,
                    "plan.execution.started",
                    {"plan_id": plan_id, "status": "running", "operation_id": job.operation_id},
                    plan=plan,
                )
            if source_ids:
                source_result = await conn.execute(
                    """
                    SELECT id,task_id,source_type,url,title,excerpt,content_sha256
                    FROM work_citation WHERE plan_id=%s AND workspace_id=%s AND id=ANY(%s)
                    ORDER BY created_at,id
                    """,
                    (plan_id, job.workspace_id, source_ids),
                )
            else:
                source_result = await conn.execute(
                    """
                    SELECT id,task_id,source_type,url,title,excerpt,content_sha256
                    FROM work_citation WHERE plan_id=%s AND workspace_id=%s
                    ORDER BY created_at,id
                    """,
                    (plan_id, job.workspace_id),
                )
            sources = await source_result.fetchall()
            task_result = await conn.execute(
                """
                SELECT id,title,description,position,status
                FROM work_task WHERE plan_id=%s AND workspace_id=%s
                ORDER BY position,id
                """,
                (plan_id, job.workspace_id),
            )
            tasks = await task_result.fetchall()
            if execution_mode == "deep_research":
                await _work_append_event(
                    conn,
                    job,
                    plan_id,
                    "research.round.started",
                    {"round": 1, "stage": "source_collection", "source_count": len(sources), "operation_id": job.operation_id},
                    plan=plan,
                )

    total_steps = max(len(sources) + len(tasks) + (2 if execution_mode == "deep_research" else 0), 1)
    completed_steps = 0
    mock_source_count = 0
    external_source_ids: list[str] = []
    completed_task_count = 0
    research_records: list[dict[str, Any]] = []
    report_artifacts: list[dict[str, Any]] = []

    for source in sources:
        if await _work_cancel_requested(job, progress=round(completed_steps * 100 / total_steps), stage="research.source"):
            return await _mark_work_plan_cancelled(job, "Work execution cancellation requested.")
        if source["source_type"] == "mock":
            fetched = work.deterministic_mock_browser_fetch(source["url"])
            research_records.append({"source": dict(source), "fetched": fetched})
            mock_source_count += 1
            async with pool.connection() as conn:
                async with conn.transaction():
                    await _work_append_event(
                        conn,
                        job,
                        plan_id,
                        "research.source.fetched",
                        {
                            "citation_id": source["id"],
                            "url": fetched["url"],
                            "title": fetched["title"],
                            "content_sha256": fetched["content_sha256"],
                            "untrusted": True,
                            "operation_id": job.operation_id,
                        },
                    )
        else:
            external_source_ids.append(source["id"])
            async with pool.connection() as conn:
                async with conn.transaction():
                    await _work_append_event(
                        conn,
                        job,
                        plan_id,
                        "research.source.external_pending",
                        {
                            "citation_id": source["id"],
                            "url": source["url"],
                            "status": "pending_external",
                            "operation_id": job.operation_id,
                        },
                    )
        completed_steps += 1

    if external_source_ids:
        message = "HTTPS research execution requires an approved browser/provider boundary; no external request was made."
        await _mark_work_plan_failed(job, message, error_code="external_source_pending")
        raise WorkExecutionError("external_source_pending", message)

    research_validation: dict[str, Any] | None = None
    if execution_mode == "deep_research":
        async with pool.connection() as conn:
            async with conn.transaction():
                await _work_append_event(
                    conn,
                    job,
                    plan_id,
                    "research.round.completed",
                    {"round": 1, "stage": "source_collection", "source_count": len(research_records), "operation_id": job.operation_id},
                )
                await _work_append_event(
                    conn,
                    job,
                    plan_id,
                    "research.round.started",
                    {"round": 2, "stage": "cross_validation", "source_count": len(research_records), "operation_id": job.operation_id},
                )
                research_validation = work.research_cross_validation(research_records)
                await _work_append_event(
                    conn,
                    job,
                    plan_id,
                    "research.round.completed",
                    {"round": 2, "stage": "cross_validation", **research_validation, "operation_id": job.operation_id},
                )
        if await _work_cancel_requested(job, progress=round(completed_steps * 100 / total_steps), stage="research.report"):
            return await _mark_work_plan_cancelled(job, "Work execution cancellation requested.")
        report_markdown, report_pdf, research_validation = work.generate_research_artifacts(
            payload.get("plan_title") or "WorkAMA deep research",
            payload.get("plan_objective") or "",
            research_records,
        )
        for generated in (report_markdown, report_pdf):
            report_artifacts.append(
                await _persist_work_research_artifact(
                    job,
                    plan_id,
                    generated,
                    validation=research_validation,
                )
            )
        completed_steps += 2

    for task in tasks:
        if await _work_cancel_requested(job, progress=round(completed_steps * 100 / total_steps), stage="task.execution"):
            return await _mark_work_plan_cancelled(job, "Work execution cancellation requested.")
        if task["status"] == "done":
            completed_task_count += 1
            async with pool.connection() as conn:
                async with conn.transaction():
                    await _work_append_event(
                        conn,
                        job,
                        plan_id,
                        "task.execution.skipped",
                        {"task_id": task["id"], "reason": "already_done", "operation_id": job.operation_id},
                        task_id=task["id"],
                    )
            completed_steps += 1
            continue
        if task["status"] in {"blocked", "cancelled"}:
            message = f"Work task '{task['title']}' cannot execute from status {task['status']}."
            await _mark_work_plan_failed(job, message, error_code="task_not_runnable")
            raise WorkExecutionError("task_not_runnable", message)

        async with pool.connection() as conn:
            async with conn.transaction():
                current_result = await conn.execute(
                    "SELECT id,status FROM work_task WHERE id=%s AND plan_id=%s AND workspace_id=%s FOR UPDATE",
                    (task["id"], plan_id, job.workspace_id),
                )
                current = await current_result.fetchone()
                if not current or current["status"] in {"blocked", "cancelled"}:
                    message = f"Work task '{task['title']}' became non-runnable before execution."
                    raise WorkExecutionError("task_not_runnable", message)
                if current["status"] != "in_progress":
                    await conn.execute(
                        "UPDATE work_task SET status='in_progress',updated_at=now() WHERE id=%s AND plan_id=%s AND workspace_id=%s",
                        (task["id"], plan_id, job.workspace_id),
                    )
                await _work_append_event(
                    conn,
                    job,
                    plan_id,
                    "task.execution.started",
                    {"task_id": task["id"], "position": task["position"], "operation_id": job.operation_id},
                    task_id=task["id"],
                )

        await asyncio.sleep(0)
        if await _work_cancel_requested(job, progress=round(completed_steps * 100 / total_steps), stage=f"task:{task['id']}"):
            return await _mark_work_plan_cancelled(job, "Work execution cancellation requested.")
        task_cancelled = False
        async with pool.connection() as conn:
            async with conn.transaction():
                current_result = await conn.execute(
                    "SELECT id,status FROM work_task WHERE id=%s AND plan_id=%s AND workspace_id=%s FOR UPDATE",
                    (task["id"], plan_id, job.workspace_id),
                )
                current = await current_result.fetchone()
                if not current:
                    raise WorkExecutionError("task_not_found", f"Work task '{task['id']}' disappeared during execution.")
                if current["status"] == "cancelled":
                    task_cancelled = True
                else:
                    await conn.execute(
                        "UPDATE work_task SET status='done',updated_at=now() WHERE id=%s AND plan_id=%s AND workspace_id=%s",
                        (task["id"], plan_id, job.workspace_id),
                    )
                    await _work_append_event(
                        conn,
                        job,
                        plan_id,
                        "task.execution.completed",
                        {"task_id": task["id"], "status": "done", "operation_id": job.operation_id},
                        task_id=task["id"],
                    )
        if task_cancelled:
            return await _mark_work_plan_cancelled(job, "Work task cancellation requested.")
        completed_task_count += 1
        completed_steps += 1

    if await _work_cancel_requested(job, progress=95, stage="plan.finalize"):
        return await _mark_work_plan_cancelled(job, "Work execution cancellation requested.")
    plan_cancelled = False
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                SELECT id,workspace_id,session_id,title,objective,status,last_event_seq,
                       created_by,created_at,updated_at
                FROM work_plan WHERE id=%s AND workspace_id=%s FOR UPDATE
                """,
                (plan_id, job.workspace_id),
            )
            plan = await result.fetchone()
            if not plan:
                raise WorkExecutionError("work_plan_not_found", "Work plan disappeared before completion.")
            if plan["status"] == "cancelled":
                plan_cancelled = True
            else:
                await conn.execute(
                    "UPDATE work_plan SET status='succeeded',updated_at=now() WHERE id=%s AND workspace_id=%s",
                    (plan_id, job.workspace_id),
                )
                plan["status"] = "succeeded"
                await _work_append_event(
                    conn,
                    job,
                    plan_id,
                    "plan.execution.completed",
                    {
                        "plan_id": plan_id,
                        "status": "succeeded",
                        "task_count": len(tasks),
                        "completed_task_count": completed_task_count,
                        "mock_source_count": mock_source_count,
                        "execution_mode": execution_mode,
                        "research_validation": research_validation,
                        "report_artifact_ids": [item["id"] for item in report_artifacts],
                        "operation_id": job.operation_id,
                    },
                    plan=plan,
                )
                await conn.execute(
                    "UPDATE work_execution SET status='succeeded',completed_at=now(),updated_at=now() WHERE operation_id=%s AND workspace_id=%s",
                    (job.operation_id, job.workspace_id),
                )
    if plan_cancelled:
        return await _mark_work_plan_cancelled(job, "Work plan was cancelled before completion.")
    return {
        "plan_id": plan_id,
        "status": "succeeded",
        "task_count": len(tasks),
        "completed_task_count": completed_task_count,
        "mock_source_count": mock_source_count,
        "execution_mode": execution_mode,
        "research_validation": research_validation,
        "report_artifact_ids": [item["id"] for item in report_artifacts],
    }


async def _audit_from_worker(
    conn,
    *,
    org_id: str,
    action: str,
    resource_type: str,
    resource_id: str,
    actor_user_id: str | None = None,
    reason: str = "",
    details: dict[str, Any] | None = None,
    workspace_id: str | None = None,
) -> None:
    """Write an enterprise audit event from a platform worker context."""
    from workama_platform.modules.enterprise import _audit

    await _audit(
        conn,
        org_id=org_id,
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        reason=reason,
        details=details,
        workspace_id=workspace_id,
    )


async def process_owner_transfer_job(job) -> dict[str, Any]:
    """Migrate or tombstone the previous owner's private resources after a confirmed transfer.

    The handler is idempotent: already-migrated/revoked/archived resources are skipped
    and the summary reflects the actual delta.
    """
    payload = job.payload or {}
    org_id = str(payload.get("org_id") or "")
    from_owner_user_id = str(payload.get("from_owner_user_id") or "")
    to_owner_user_id = str(payload.get("to_owner_user_id") or "")
    transfer_id = str(payload.get("transfer_id") or "")
    if not org_id or not from_owner_user_id or not to_owner_user_id or not transfer_id:
        raise ValueError("owner transfer job payload is incomplete")

    async with pool.connection() as conn:
        async with conn.transaction():
            org_result = await conn.execute(
                "SELECT id, owner_user_id, status FROM id_org WHERE id=%s FOR UPDATE",
                (org_id,),
            )
            org = await org_result.fetchone()
            if not org:
                raise ValueError("organization not found")
            if org["owner_user_id"] != to_owner_user_id:
                raise ValueError("organization owner does not match the transfer target")
            transfer_result = await conn.execute(
                "SELECT status FROM id_org_owner_transfer WHERE id=%s AND org_id=%s",
                (transfer_id, org_id),
            )
            transfer = await transfer_result.fetchone()
            if not transfer or transfer["status"] != "confirmed":
                raise ValueError("owner transfer is not confirmed")

            # Revoke personal API keys created by the previous owner.
            api_key_result = await conn.execute(
                """
                UPDATE id_api_key
                SET revoked_at=COALESCE(revoked_at, now())
                WHERE actor_user_id=%s AND workspace_id IN (SELECT id FROM id_workspace WHERE org_id=%s)
                  AND revoked_at IS NULL
                RETURNING id
                """,
                (from_owner_user_id, org_id),
            )
            revoked_api_keys = [row["id"] for row in await api_key_result.fetchall()]

            # Archive the previous owner's private sessions.
            session_result = await conn.execute(
                """
                UPDATE ag_session
                SET status='archived', updated_at=now()
                WHERE user_id=%s AND workspace_id IN (SELECT id FROM id_workspace WHERE org_id=%s)
                  AND status <> 'archived'
                RETURNING id
                """,
                (from_owner_user_id, org_id),
            )
            archived_sessions = [row["id"] for row in await session_result.fetchall()]

            # Transfer service-account ownership to the new owner. If a service account is
            # revoked it is left revoked; active ones are migrated so they do not become
            # orphaned.
            service_account_result = await conn.execute(
                """
                UPDATE id_service_account
                SET owner_user_id=%s, updated_at=now()
                WHERE org_id=%s AND owner_user_id=%s AND status='active'
                RETURNING id
                """,
                (to_owner_user_id, org_id, from_owner_user_id),
            )
            transferred_service_accounts = [row["id"] for row in await service_account_result.fetchall()]

            summary = {
                "transfer_id": transfer_id,
                "from_owner_user_id": from_owner_user_id,
                "to_owner_user_id": to_owner_user_id,
                "revoked_api_keys": len(revoked_api_keys),
                "archived_sessions": len(archived_sessions),
                "transferred_service_accounts": len(transferred_service_accounts),
                "revoked_api_key_ids": revoked_api_keys,
                "archived_session_ids": archived_sessions,
                "transferred_service_account_ids": transferred_service_accounts,
            }
            await _audit_from_worker(
                conn,
                org_id=org_id,
                action="organization.owner_transfer_resources_processed",
                resource_type="owner_transfer",
                resource_id=transfer_id,
                actor_user_id=to_owner_user_id,
                reason="owner transfer resource migration",
                details=summary,
                workspace_id=job.workspace_id,
            )
    return summary


async def process_org_deletion_job(job) -> dict[str, Any]:
    """Execute the final organization deletion after the retention window expires.

    Idempotent: if the organization is already deleted the handler returns the
    existing state without re-running destructive steps.
    """
    payload = job.payload or {}
    org_id = str(payload.get("org_id") or "")
    request_id = str(payload.get("request_id") or "")
    if not org_id or not request_id:
        raise ValueError("organization deletion job payload is incomplete")

    async with pool.connection() as conn:
        async with conn.transaction():
            request_result = await conn.execute(
                "SELECT id, org_id, status, retention_until FROM id_org_deletion_request WHERE id=%s AND org_id=%s FOR UPDATE",
                (request_id, org_id),
            )
            deletion_request = await request_result.fetchone()
            if not deletion_request:
                raise ValueError("organization deletion request not found")
            if deletion_request["status"] == "deleted":
                return {"request_id": request_id, "org_id": org_id, "status": "deleted", "skipped": True}
            if deletion_request["status"] == "cancelled":
                return {"request_id": request_id, "org_id": org_id, "status": "cancelled", "skipped": True}
            if deletion_request["status"] not in {"retention", "deleting"}:
                raise ValueError(f"organization deletion request is in unexpected status: {deletion_request['status']}")
            if deletion_request["retention_until"] > datetime.now(UTC):
                raise ValueError("organization deletion retention window has not elapsed")

            org_result = await conn.execute(
                "SELECT id, status FROM id_org WHERE id=%s FOR UPDATE",
                (org_id,),
            )
            org = await org_result.fetchone()
            if not org:
                raise ValueError("organization not found")

            await conn.execute(
                "UPDATE id_org_deletion_request SET status='deleting', updated_at=now() WHERE id=%s",
                (request_id,),
            )

            # Disable every workspace in the organization.
            workspace_result = await conn.execute(
                "UPDATE id_workspace SET status='disabled', updated_at=now() WHERE org_id=%s AND status <> 'disabled' RETURNING id",
                (org_id,),
            )
            disabled_workspaces = [row["id"] for row in await workspace_result.fetchall()]

            # Revoke service account credentials so no new access is possible.
            await conn.execute(
                """
                UPDATE id_service_account_credential
                SET status='revoked', revoked_at=now(), revoke_reason='organization deletion'
                WHERE service_account_id IN (SELECT id FROM id_service_account WHERE org_id=%s)
                  AND status='active'
                """,
                (org_id,),
            )
            await conn.execute(
                "UPDATE id_service_account SET status='revoked', updated_at=now() WHERE org_id=%s AND status='active'",
                (org_id,),
            )

            # Mark the organization as deleted and finalize the request.
            await conn.execute(
                """
                UPDATE id_org
                SET status='deleted', deletion_scheduled_at=NULL, deletion_cancelled_at=NULL
                WHERE id=%s
                """,
                (org_id,),
            )
            await conn.execute(
                "UPDATE id_org_deletion_request SET status='deleted', updated_at=now() WHERE id=%s",
                (request_id,),
            )

            summary = {
                "request_id": request_id,
                "org_id": org_id,
                "status": "deleted",
                "disabled_workspaces": len(disabled_workspaces),
                "disabled_workspace_ids": disabled_workspaces,
            }
            await _audit_from_worker(
                conn,
                org_id=org_id,
                action="organization.deleted",
                resource_type="organization",
                resource_id=org_id,
                actor_user_id=None,
                reason="organization deletion retention window elapsed",
                details=summary,
                workspace_id=job.workspace_id,
            )
    return summary


async def process_platform_jobs(worker_id: str) -> dict[str, int]:
    async with pool.connection() as conn:
        async with conn.transaction():
            jobs = [
                *await claim_jobs(conn, worker_id=worker_id, queue="platform", limit=10),
                *await claim_jobs(conn, worker_id=worker_id, queue="workflow", limit=10),
            ]
    succeeded = failed = 0
    for job in jobs:
        request_id_var.set(job.operation_id)
        workspace_id_var.set(job.workspace_id)
        try:
            with TRACER.start_as_current_span("worker.job.run") as span:
                span.set_attribute("wama.operation_id", job.operation_id)
                span.set_attribute("wama.job_id", job.id)
                span.set_attribute("wama.job_type", job.job_type)
                async with pool.connection() as conn:
                    await heartbeat(conn, job, progress=10, stage="started")
                    await conn.commit()
                if job.job_type == "privacy.data_request.process":
                    result = await process_pending_data_requests()
                elif job.job_type == "notification.email_delivery.batch":
                    result = await process_pending_email_deliveries()
                elif job.job_type == "billing.daily_reconciliation":
                    business_date = datetime.fromisoformat(job.payload["business_date"]).date()
                    rows = await run_daily_reconciliation(business_date)
                    result = {"workspaces": len(rows), "business_date": str(business_date)}
                elif job.job_type == "search.index_rebuild":
                    async with pool.connection() as conn:
                        async with conn.transaction():
                            counts = await rebuild_search_projection(conn, job.workspace_id, job.payload.get("resource_types"))
                    result = {"resource_counts": counts, "documents": sum(counts.values())}
                elif job.job_type == "workspace.export":
                    async with pool.connection() as conn:
                        async with conn.transaction():
                            result = await build_workspace_export(conn, job.payload["export_id"], job.workspace_id, "system")
                elif job.job_type == "workspace.import.dry_run":
                    async with pool.connection() as conn:
                        async with conn.transaction(): result = await dry_run_import(conn, job.payload["import_id"], job.workspace_id)
                elif job.job_type == "workspace.import.apply":
                    async with pool.connection() as conn:
                        async with conn.transaction(): result = await apply_import(conn, job.payload["import_id"], job.workspace_id)
                elif job.job_type == "lifecycle.run":
                    async with pool.connection() as conn:
                        async with conn.transaction(): result = await execute_lifecycle_run(conn, job.payload["run_id"], job.workspace_id)
                elif job.job_type == "workflow.run.execute":
                    result = await process_workflow_run_job(job)
                elif job.job_type == "work.plan.execute":
                    result = await process_work_plan_job(job)
                elif job.job_type == OWNER_TRANSFER_JOB_TYPE:
                    result = await process_owner_transfer_job(job)
                elif job.job_type == ORG_DELETION_JOB_TYPE:
                    result = await process_org_deletion_job(job)
                elif job.job_type == MEM_EXTRACT_JT:
                    result = await memory_extraction_worker.process_extraction_job(job.payload)
                elif job.job_type == MEM_FORGET_JT:
                    payload = job.payload or {}
                    workspace_id = payload.get("workspace_id") or job.workspace_id
                    threshold_days = payload.get("threshold_days")
                    async with pool.connection() as conn:
                        async with conn.transaction():
                            result = await memory_forgetting_worker.run_forget_sweep(
                                conn, workspace_id, threshold_days
                            )
                            actor = Actor(
                                user_id="system",
                                workspace_id=workspace_id,
                                org_id=payload.get("org_id", ""),
                                role="system",
                                email="",
                                display_name="System",
                                onboarding_completed=True,
                                actor_type="system",
                                capabilities=("memory:*",),
                            )
                            await audit_log_action(
                                actor,
                                action="delete",
                                resource_type="memory_vector",
                                severity="info",
                                description=f"Memory forget sweep deleted {result['processed']} vectors",
                                metadata={"forgotten_ids": result.get("forgotten_ids", []), "count": result["processed"]},
                            )
                            if result["processed"] > 100:
                                owner_result = await conn.execute(
                                    "SELECT user_id FROM id_member WHERE workspace_id = %s AND role = 'owner' LIMIT 1",
                                    (workspace_id,),
                                )
                                owner_row = await owner_result.fetchone()
                                if owner_row:
                                    await create_notification(
                                        conn,
                                        user_id=owner_row["user_id"],
                                        workspace_id=workspace_id,
                                        event_type="memory.forget_sweep.batch_deleted",
                                        title="大量记忆向量已清理",
                                        summary=f"自动遗忘任务清理了 {result['processed']} 条过期记忆向量。",
                                        priority="warning",
                                    )
                elif job.job_type == MEM_REINDEX_JT:
                    payload = job.payload or {}
                    workspace_id = payload.get("workspace_id") or job.workspace_id
                    vector_ids = payload.get("vector_ids")
                    reindexed = await memory_vector_index.reindex_workspace(workspace_id, vector_ids)
                    result = {"processed": reindexed, "reindexed": reindexed}
                else:
                    raise ValueError(f"unknown job type: {job.job_type}")
                async with pool.connection() as conn:
                    async with conn.transaction():
                        if result.get("_operation_cancelled"):
                            await cancel_claimed_job(conn, job, result.get("error") or "Workflow run was cancelled.")
                        else:
                            await complete_job(conn, job, result, partial=result.get("status") == "partially_succeeded")
                succeeded += 1
                JOB_RUNS.add(1, {"job_type": job.job_type, "result": "succeeded"})
        except Exception as exc:
            LOGGER.exception("platform job failed", extra={"job_id": job.id, "job_type": job.job_type})
            async with pool.connection() as conn:
                async with conn.transaction():
                    failure_status = await fail_job(conn, job, type(exc).__name__, str(exc), retryable=False)
            if failure_status == "failed":
                if job.job_type == "workflow.run.execute":
                    await mark_workflow_run_failed(job, str(exc), workflows._classify_error(exc))
                elif job.job_type == "work.plan.execute":
                    await _mark_work_plan_failed(job, str(exc), error_code=getattr(exc, "code", type(exc).__name__))
            failed += 1
            JOB_RUNS.add(1, {"job_type": job.job_type, "result": "failed"})
    return {"claimed": len(jobs), "succeeded": succeeded, "failed": failed}


async def process_memory_governance() -> dict[str, int]:
    """Scan all workspaces for expired memories and soft-delete them based on governance policy."""
    summary = {"workspaces_scanned": 0, "memories_forgotten": 0}
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM memory_governance_policy")
        policies = await result.fetchall()

    now = datetime.now(UTC)
    for policy in policies:
        workspace_id = policy["workspace_id"]
        retention_map = policy.get("retention_days_by_importance") or {}
        default_importance = policy.get("default_importance", 3)
        if not retention_map:
            continue

        summary["workspaces_scanned"] += 1
        async with pool.connection() as conn:
            result = await conn.execute(
                "SELECT id, org_id, user_id, importance, updated_at FROM ag_memory WHERE workspace_id = %s AND status = 'active'",
                (workspace_id,),
            )
            rows = await result.fetchall()

        to_forget: list[dict[str, Any]] = []
        for row in rows:
            importance = float(row.get("importance", 0.5))
            level = min(5, max(1, int(importance * 4) + 1))
            days = retention_map.get(str(level), retention_map.get(str(default_importance)))
            if days is None:
                continue
            if row["updated_at"] + timedelta(days=int(days)) < now:
                to_forget.append(row)

        if to_forget:
            ids = [r["id"] for r in to_forget]
            async with pool.connection() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE ag_memory
                        SET status='deleted', forgotten_at=now(), updated_at=now()
                        WHERE id = ANY(%s) AND workspace_id = %s
                        """,
                        (ids, workspace_id),
                    )

            summary["memories_forgotten"] += len(to_forget)
            org_id = to_forget[0]["org_id"]
            actor = Actor(
                user_id="system",
                workspace_id=workspace_id,
                org_id=org_id,
                role="system",
                email="",
                display_name="Memory Governance Worker",
                onboarding_completed=True,
                actor_type="system",
                capabilities=("memory:*",),
            )
            await audit_log_action(
                actor,
                action="delete",
                resource_type="memory",
                severity="info",
                description=f"Memory governance auto-forgotten {len(to_forget)} memories",
                metadata={"forgotten_count": len(to_forget), "workspace_id": workspace_id},
            )

    return summary


MEMORY_GOVERNANCE_INTERVAL = 1800


async def memory_governance_loop() -> None:
    while True:
        started = perf_counter()
        result_label = "success"
        try:
            with TRACER.start_as_current_span("worker.memory_governance"):
                result = await process_memory_governance()
                if result["memories_forgotten"]:
                    LOGGER.info("memory governance processed", extra=result)
        except Exception:
            result_label = "error"
            LOGGER.exception("memory governance batch failed")
        BATCHES.add(1, {"operation": "memory_governance", "result": result_label})
        BATCH_DURATION.record(perf_counter() - started, {"operation": "memory_governance", "result": result_label})
        await asyncio.sleep(MEMORY_GOVERNANCE_INTERVAL)


async def platform_job_loop() -> None:
    worker_id = f"platform-worker-{new_id('wrk')}"
    while True:
        try:
            result = await process_platform_jobs(worker_id)
            if result["claimed"]:
                LOGGER.info("platform jobs processed", extra=result)
        except Exception:
            LOGGER.exception("platform job batch failed")
        await asyncio.sleep(2)


async def heartbeat_loop() -> None:
    while True:
        HEARTBEAT_PATH.touch()
        await asyncio.sleep(5)


async def run() -> None:
    configure_observability("platform-worker")
    await pool.open()
    await ensure_runtime_schema()
    nc = await nats.connect(
        settings.nats_url,
        name="workama-platform-worker",
        reconnect_time_wait=1,
        max_reconnect_attempts=-1,
    )
    js = nc.jetstream()
    await ensure_stream(js)
    await js.subscribe(
        SUBJECT,
        durable=CONSUMER_NAME,
        manual_ack=True,
        cb=handle_metering_message,
        config=ConsumerConfig(
            durable_name=CONSUMER_NAME,
            ack_wait=30,
            max_deliver=5,
        ),
    )
    reconciliation_task = asyncio.create_task(reconciliation_loop())
    notification_task = asyncio.create_task(notification_delivery_loop())
    siem_task = asyncio.create_task(siem_delivery_loop())
    webhook_task = asyncio.create_task(webhook_delivery_loop())
    channel_ext_task = asyncio.create_task(channel_extension_cleanup_loop())
    external_app_task = asyncio.create_task(external_app_invocation_loop())
    job_task = asyncio.create_task(platform_job_loop())
    outbox_task = asyncio.create_task(outbox_loop(js))
    automation_task = asyncio.create_task(automation_loop())
    memory_governance_task = asyncio.create_task(memory_governance_loop())
    heartbeat_task = asyncio.create_task(heartbeat_loop())
    # v7.181: 跨进程热收敛——UI 配置发布后 ≤1s 重应用到本进程 settings。
    config_watcher_task = asyncio.create_task(config_watcher_loop())
    LOGGER.info("platform worker listening", extra={"subject": SUBJECT})
    try:
        await asyncio.Future()
    finally:
        reconciliation_task.cancel()
        notification_task.cancel()
        siem_task.cancel()
        webhook_task.cancel()
        channel_ext_task.cancel()
        external_app_task.cancel()
        job_task.cancel()
        outbox_task.cancel()
        automation_task.cancel()
        memory_governance_task.cancel()
        heartbeat_task.cancel()
        config_watcher_task.cancel()
        await asyncio.gather(
            reconciliation_task, notification_task, siem_task, webhook_task, channel_ext_task, external_app_task, job_task, outbox_task, automation_task,
            memory_governance_task, heartbeat_task, config_watcher_task,
            return_exceptions=True
        )
        await nc.drain()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(run())
