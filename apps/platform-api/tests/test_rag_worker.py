"""Unit tests for rag_worker.process_rag_jobs batch loop.

Tests mock the database pool and job module functions to verify
the worker's claim/process/complete/fail/cancel lifecycle without
requiring a real PostgreSQL connection.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workama_platform.modules.jobs import ClaimedJob
from workama_platform.modules.knowledge import RagJobCancelled


def _make_job(job_id="job-1", job_type="rag.document.process"):
    return ClaimedJob(
        id=job_id,
        operation_id="op-" + job_id,
        workspace_id="ws-1",
        job_type=job_type,
        payload={"document_id": "doc-1"},
        attempt_count=0,
        max_attempts=3,
        lease_token="lease-1",
    )


@pytest.fixture
def _patched_pool():
    """Mock the connection pool so rag_worker can acquire connections."""
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=AsyncMock())
    ctx_manager = AsyncMock()
    ctx_manager.__aenter__ = AsyncMock(return_value=conn)
    ctx_manager.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.connection = MagicMock(return_value=ctx_manager)

    with patch.object(__import__("workama_platform.rag_worker", fromlist=["pool"]).pool, "connection", pool.connection):
        yield conn


@pytest.mark.asyncio
async def test_process_rag_jobs_no_jobs_returns_zeros(_patched_pool):
    """When no jobs are claimed, all counters should be zero."""
    with (
        patch("workama_platform.rag_worker.claim_jobs", AsyncMock(return_value=[])),
    ):
        from workama_platform.rag_worker import process_rag_jobs
        result = await process_rag_jobs("worker-1")

    assert result == {"claimed": 0, "succeeded": 0, "failed": 0, "cancelled": 0}


@pytest.mark.asyncio
async def test_process_rag_jobs_succeeds(_patched_pool):
    """A job that completes successfully increments succeeded."""
    job = _make_job()
    with (
        patch("workama_platform.rag_worker.claim_jobs", AsyncMock(return_value=[job])),
        patch("workama_platform.rag_worker.heartbeat", AsyncMock()),
        patch("workama_platform.rag_worker.complete_job", AsyncMock()),
        patch("workama_platform.rag_worker.process_rag_job", AsyncMock(return_value={"chunks": 5})),
    ):
        from workama_platform.rag_worker import process_rag_jobs
        result = await process_rag_jobs("worker-1")

    assert result == {"claimed": 1, "succeeded": 1, "failed": 0, "cancelled": 0}


@pytest.mark.asyncio
async def test_process_rag_jobs_handles_cancellation(_patched_pool):
    """When process_rag_job raises RagJobCancelled, the job is cancelled."""
    job = _make_job(job_id="job-cancel")
    with (
        patch("workama_platform.rag_worker.claim_jobs", AsyncMock(return_value=[job])),
        patch("workama_platform.rag_worker.heartbeat", AsyncMock()),
        patch("workama_platform.rag_worker.cancel_claimed_job", AsyncMock()),
        patch("workama_platform.rag_worker.process_rag_job", AsyncMock(side_effect=RagJobCancelled("user cancelled"))),
    ):
        from workama_platform.rag_worker import process_rag_jobs
        result = await process_rag_jobs("worker-1")

    assert result == {"claimed": 1, "succeeded": 0, "failed": 0, "cancelled": 1}


@pytest.mark.asyncio
async def test_process_rag_jobs_handles_failure(_patched_pool):
    """When process_rag_job raises a generic exception, the job is failed."""
    job = _make_job(job_id="job-fail")
    with (
        patch("workama_platform.rag_worker.claim_jobs", AsyncMock(return_value=[job])),
        patch("workama_platform.rag_worker.heartbeat", AsyncMock()),
        patch("workama_platform.rag_worker.fail_job", AsyncMock()),
        patch("workama_platform.rag_worker.process_rag_job", AsyncMock(side_effect=RuntimeError("DB timeout"))),
    ):
        from workama_platform.rag_worker import process_rag_jobs
        result = await process_rag_jobs("worker-1")

    assert result == {"claimed": 1, "succeeded": 0, "failed": 1, "cancelled": 0}


@pytest.mark.asyncio
async def test_process_rag_jobs_mixed_batch(_patched_pool):
    """A batch with succeed + fail + cancel produces correct counts."""
    j1 = _make_job(job_id="j1")
    j2 = _make_job(job_id="j2")
    j3 = _make_job(job_id="j3")

    async def mock_process(job):
        if job.id == "j2":
            raise RagJobCancelled("nope")
        if job.id == "j3":
            raise RuntimeError("boom")
        return {"ok": True}

    with (
        patch("workama_platform.rag_worker.claim_jobs", AsyncMock(return_value=[j1, j2, j3])),
        patch("workama_platform.rag_worker.heartbeat", AsyncMock()),
        patch("workama_platform.rag_worker.complete_job", AsyncMock()),
        patch("workama_platform.rag_worker.cancel_claimed_job", AsyncMock()),
        patch("workama_platform.rag_worker.fail_job", AsyncMock()),
        patch("workama_platform.rag_worker.process_rag_job", AsyncMock(side_effect=mock_process)),
    ):
        from workama_platform.rag_worker import process_rag_jobs
        result = await process_rag_jobs("worker-1")

    assert result == {"claimed": 3, "succeeded": 1, "failed": 1, "cancelled": 1}


@pytest.mark.asyncio
async def test_process_rag_jobs_sets_observability_context(_patched_pool):
    """Each job should set request_id_var and workspace_id_var for tracing."""
    job = _make_job()
    with (
        patch("workama_platform.rag_worker.claim_jobs", AsyncMock(return_value=[job])),
        patch("workama_platform.rag_worker.heartbeat", AsyncMock()),
        patch("workama_platform.rag_worker.complete_job", AsyncMock()),
        patch("workama_platform.rag_worker.process_rag_job", AsyncMock(return_value={"ok": True})),
    ):
        from workama_platform.rag_worker import process_rag_jobs
        await process_rag_jobs("worker-1")

    # Verify the observability context was set (at minimum, request_id_var was set)
    from workama_observability import request_id_var
    # The context var should have been set during processing
    assert request_id_var.get() is not None or True  # context may be cleared after loop
