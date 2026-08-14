from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from workama_platform.core import Actor, new_id, pool
from workama_platform.modules.enterprise import ensure_enterprise_schema
from workama_platform.modules.jobs import (
    ClaimedJob,
    IdempotencyConflict,
    canonical_hash,
    claim_jobs,
    request_cancellation,
    submit_operation,
)


def _actor(user_id: str, org_id: str, workspace_id: str) -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id=org_id,
        role="owner",
        email=f"{user_id}@example.com",
        display_name=f"User {user_id}",
        onboarding_completed=True,
        capabilities=("*",),
        auth_strength=2,
    )


class _TestTenant:
    def __init__(self):
        self.owner_id = new_id("usr")
        self.org_id = new_id("org")
        self.workspace_id = new_id("wsp")

    async def __aenter__(self):
        async with pool.connection() as conn:
            async with conn.transaction():
                await ensure_enterprise_schema(conn)
                await conn.execute(
                    """
                    INSERT INTO id_user(id, email, password_hash, display_name, status, email_verified)
                    VALUES (%s, %s, 'test', %s, 'active', TRUE)
                    """,
                    (self.owner_id, f"{self.owner_id}@example.com", f"Owner {self.owner_id}"),
                )
                await conn.execute(
                    "INSERT INTO id_org(id, name, owner_user_id, status) VALUES (%s, %s, %s, 'active')",
                    (self.org_id, f"Org {self.org_id}", self.owner_id),
                )
                await conn.execute(
                    "INSERT INTO id_workspace(id, org_id, name, slug, status) VALUES (%s, %s, %s, %s, 'active')",
                    (self.workspace_id, self.org_id, f"Workspace {self.workspace_id}", f"ws-{self.workspace_id[-8:].lower()}"),
                )
                await conn.execute(
                    "INSERT INTO id_member(id, org_id, workspace_id, user_id, role) VALUES (%s, %s, %s, %s, 'owner')",
                    (new_id("mem"), self.org_id, self.workspace_id, self.owner_id),
                )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM ops_job_run WHERE job_id IN (SELECT id FROM ops_job WHERE operation_id IN (SELECT id FROM ops_async_operation WHERE workspace_id=%s))",
                    (self.workspace_id,),
                )
                await conn.execute(
                    "DELETE FROM ops_job WHERE operation_id IN (SELECT id FROM ops_async_operation WHERE workspace_id=%s)",
                    (self.workspace_id,),
                )
                await conn.execute("DELETE FROM ops_async_operation WHERE workspace_id=%s", (self.workspace_id,))
                await conn.execute("DELETE FROM id_member WHERE org_id=%s", (self.org_id,))
                await conn.execute("DELETE FROM id_workspace WHERE id=%s", (self.workspace_id,))
                await conn.execute("DELETE FROM id_org WHERE id=%s", (self.org_id,))
                await conn.execute("DELETE FROM id_user WHERE id=%s", (self.owner_id,))


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _db_pool():
    await pool.open()
    try:
        async with pool.connection() as conn:
            async with conn.transaction():
                await ensure_enterprise_schema(conn)
        yield
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_submit_operation_creates_job_with_future_scheduled_at():
    async with _TestTenant() as tenant:
        actor = _actor(tenant.owner_id, tenant.org_id, tenant.workspace_id)
        scheduled = datetime.now(UTC) + timedelta(hours=1)
        async with pool.connection() as conn:
            async with conn.transaction():
                op = await submit_operation(
                    conn,
                    operation_type="smoke.scheduled",
                    workspace_id=tenant.workspace_id,
                    org_id=tenant.org_id,
                    actor_id=actor.user_id,
                    actor_role=actor.role,
                    idempotency_key="scheduled-test-1",
                    payload={"hello": "future"},
                    job_type="smoke.scheduled.run",
                    queue="platform",
                    scheduled_at=scheduled,
                )
        async with pool.connection() as conn:
            result = await conn.execute("SELECT * FROM ops_job WHERE operation_id=%s", (op["id"],))
            job = await result.fetchone()
        assert job is not None
        assert job["status"] == "queued"
        assert abs(job["scheduled_at"] - scheduled) < timedelta(seconds=1)


@pytest.mark.asyncio
async def test_submit_operation_is_idempotent_for_same_input_and_conflicts_for_different_input():
    async with _TestTenant() as tenant:
        actor = _actor(tenant.owner_id, tenant.org_id, tenant.workspace_id)
        async with pool.connection() as conn:
            async with conn.transaction():
                first = await submit_operation(
                    conn,
                    operation_type="smoke.idempotent",
                    workspace_id=tenant.workspace_id,
                    org_id=tenant.org_id,
                    actor_id=actor.user_id,
                    actor_role=actor.role,
                    idempotency_key="idem-test-1",
                    payload={"value": 1},
                    job_type="smoke.idempotent.run",
                )
                second = await submit_operation(
                    conn,
                    operation_type="smoke.idempotent",
                    workspace_id=tenant.workspace_id,
                    org_id=tenant.org_id,
                    actor_id=actor.user_id,
                    actor_role=actor.role,
                    idempotency_key="idem-test-1",
                    payload={"value": 1},
                    job_type="smoke.idempotent.run",
                )
        assert first["id"] == second["id"]

        with pytest.raises(IdempotencyConflict):
            async with pool.connection() as conn:
                async with conn.transaction():
                    await submit_operation(
                        conn,
                        operation_type="smoke.idempotent",
                        workspace_id=tenant.workspace_id,
                        org_id=tenant.org_id,
                        actor_id=actor.user_id,
                        actor_role=actor.role,
                        idempotency_key="idem-test-1",
                        payload={"value": 2},
                        job_type="smoke.idempotent.run",
                    )


@pytest.mark.asyncio
async def test_request_cancellation_cancels_queued_job():
    async with _TestTenant() as tenant:
        actor = _actor(tenant.owner_id, tenant.org_id, tenant.workspace_id)
        async with pool.connection() as conn:
            async with conn.transaction():
                op = await submit_operation(
                    conn,
                    operation_type="smoke.cancellable",
                    workspace_id=tenant.workspace_id,
                    org_id=tenant.org_id,
                    actor_id=actor.user_id,
                    actor_role=actor.role,
                    idempotency_key="cancel-test-1",
                    payload={},
                    job_type="smoke.cancellable.run",
                )
                cancelled = await request_cancellation(
                    conn,
                    operation_id=op["id"],
                    workspace_id=tenant.workspace_id,
                    reason="test cancellation",
                )
        assert cancelled["status"] == "cancelled"

        async with pool.connection() as conn:
            result = await conn.execute("SELECT status FROM ops_job WHERE operation_id=%s", (op["id"],))
            job = await result.fetchone()
        assert job["status"] == "cancelled"


@pytest.mark.asyncio
async def test_claim_jobs_respects_scheduled_at():
    async with _TestTenant() as tenant:
        actor = _actor(tenant.owner_id, tenant.org_id, tenant.workspace_id)
        future = datetime.now(UTC) + timedelta(hours=1)
        async with pool.connection() as conn:
            async with conn.transaction():
                op = await submit_operation(
                    conn,
                    operation_type="smoke.claim",
                    workspace_id=tenant.workspace_id,
                    org_id=tenant.org_id,
                    actor_id=actor.user_id,
                    actor_role=actor.role,
                    idempotency_key="claim-test-1",
                    payload={},
                    job_type="smoke.claim.run",
                    queue="test",
                    scheduled_at=future,
                )
                # Should not be claimable before scheduled_at
                claimed = await claim_jobs(conn, worker_id="test-worker", queue="test", limit=10)
        assert not claimed

        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE ops_job SET scheduled_at=now() - interval '1 second' WHERE operation_id=%s",
                    (op["id"],),
                )
                claimed = await claim_jobs(conn, worker_id="test-worker", queue="test", limit=10)
        assert len(claimed) == 1
        assert claimed[0].operation_id == op["id"]


def test_payload_hash_is_canonical_and_stable():
    assert canonical_hash({"b": 2, "a": 1}) == canonical_hash({"a": 1, "b": 2})
    assert len(canonical_hash({})) == 64
