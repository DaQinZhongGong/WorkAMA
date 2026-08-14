from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from opentelemetry import metrics, trace
from workama_observability import configure_observability, request_id_var, workspace_id_var

from workama_platform.core import ensure_runtime_schema, new_id, pool
from workama_platform.modules.jobs import (
    cancel_claimed_job,
    claim_jobs,
    complete_job,
    fail_job,
    heartbeat,
)
from workama_platform.modules.knowledge import RagJobCancelled, process_rag_job


LOGGER = logging.getLogger("workama.rag-worker")
TRACER = trace.get_tracer("rag-worker")
METER = metrics.get_meter("rag-worker")
JOB_RUNS = METER.create_counter("wama_rag_worker_job_total")
HEARTBEAT_PATH = Path("/tmp/workama-rag-worker.heartbeat")


async def process_rag_jobs(worker_id: str) -> dict[str, int]:
    async with pool.connection() as conn:
        async with conn.transaction():
            jobs = await claim_jobs(conn, worker_id=worker_id, queue="rag", limit=4, lease_seconds=180)
    succeeded = failed = cancelled = 0
    for job in jobs:
        request_id_var.set(job.operation_id)
        workspace_id_var.set(job.workspace_id)
        try:
            with TRACER.start_as_current_span("rag.job.run") as span:
                span.set_attribute("wama.operation_id", job.operation_id)
                span.set_attribute("wama.job_id", job.id)
                span.set_attribute("wama.job_type", job.job_type)
                async with pool.connection() as conn:
                    await heartbeat(conn, job, progress=5, stage="accepted", lease_seconds=180)
                    await conn.commit()
                result = await process_rag_job(job)
                async with pool.connection() as conn:
                    async with conn.transaction():
                        await heartbeat(conn, job, progress=95, stage="finalizing", lease_seconds=180)
                        await complete_job(conn, job, result)
                succeeded += 1
                JOB_RUNS.add(1, {"job_type": job.job_type, "result": "succeeded"})
        except RagJobCancelled as exc:
            async with pool.connection() as conn:
                async with conn.transaction():
                    await cancel_claimed_job(conn, job, str(exc))
            cancelled += 1
            JOB_RUNS.add(1, {"job_type": job.job_type, "result": "cancelled"})
        except Exception as exc:
            LOGGER.exception("RAG job failed", extra={"job_id": job.id, "job_type": job.job_type})
            async with pool.connection() as conn:
                async with conn.transaction():
                    await fail_job(conn, job, type(exc).__name__, str(exc))
            failed += 1
            JOB_RUNS.add(1, {"job_type": job.job_type, "result": "failed"})
    return {"claimed": len(jobs), "succeeded": succeeded, "failed": failed, "cancelled": cancelled}


async def loop() -> None:
    worker_id = f"rag-worker-{new_id('wrk')}"
    while True:
        try:
            result = await process_rag_jobs(worker_id)
            if result["claimed"]:
                LOGGER.info("RAG jobs processed", extra=result)
        except Exception:
            LOGGER.exception("RAG job batch failed")
        await asyncio.sleep(1)


async def heartbeat_loop() -> None:
    while True:
        try:
            HEARTBEAT_PATH.touch()
        except OSError:
            pass
        await asyncio.sleep(5)


async def run() -> None:
    configure_observability("rag-worker")
    await pool.open()
    await ensure_runtime_schema()
    try:
        await asyncio.gather(loop(), heartbeat_loop())
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(run())
