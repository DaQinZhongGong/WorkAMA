from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from psycopg_pool import AsyncConnectionPool
from psycopg.rows import dict_row

from workama_platform.core import Actor, get_actor, new_id, settings

# 本模块使用独立的连接池，避免与其他测试模块共享全局 pool 导致 PoolClosed 错误
pool = AsyncConnectionPool(
    settings.database_url,
    min_size=1,
    max_size=10,
    open=False,
    kwargs={"row_factory": dict_row},
)

from workama_platform.modules.enterprise import (
    ORG_DELETION_JOB_TYPE,
    ORG_DELETION_OPERATION_TYPE,
    OWNER_TRANSFER_JOB_TYPE,
    OWNER_TRANSFER_OPERATION_TYPE,
    ensure_enterprise_schema,
    router as enterprise_router,
)
from workama_platform.modules.jobs import ClaimedJob
from workama_platform.worker import process_org_deletion_job, process_owner_transfer_job


def _actor(
    user_id: str,
    org_id: str,
    workspace_id: str,
    *,
    role: str = "owner",
    capabilities: tuple[str, ...] = ("*",),
) -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id=org_id,
        role=role,
        email=f"{user_id}@example.com",
        display_name=f"User {user_id}",
        onboarding_completed=True,
        capabilities=capabilities,
        auth_strength=2,
    )


class _TestTenant:
    def __init__(self):
        self.owner_id = new_id("usr")
        self.target_id = new_id("usr")
        self.org_id = new_id("org")
        self.workspace_id = new_id("wsp")
        self.owner = _actor(self.owner_id, self.org_id, self.workspace_id, role="owner")
        self.target = _actor(self.target_id, self.org_id, self.workspace_id, role="member")

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
                    """
                    INSERT INTO id_user(id, email, password_hash, display_name, status, email_verified)
                    VALUES (%s, %s, 'test', %s, 'active', TRUE)
                    """,
                    (self.target_id, f"{self.target_id}@example.com", f"Target {self.target_id}"),
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
                await conn.execute(
                    "INSERT INTO id_member(id, org_id, workspace_id, user_id, role) VALUES (%s, %s, NULL, %s, 'member')",
                    (new_id("mem"), self.org_id, self.target_id),
                )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM id_enterprise_audit_event WHERE org_id=%s", (self.org_id,))
                await conn.execute("DELETE FROM id_org_owner_transfer_fact WHERE org_id=%s", (self.org_id,))
                await conn.execute("DELETE FROM id_org_owner_transfer WHERE org_id=%s", (self.org_id,))
                await conn.execute("DELETE FROM id_org_deletion_request WHERE org_id=%s", (self.org_id,))
                await conn.execute(
                    "DELETE FROM ops_job_run WHERE job_id IN (SELECT id FROM ops_job WHERE operation_id IN (SELECT id FROM ops_async_operation WHERE org_id=%s))",
                    (self.org_id,),
                )
                await conn.execute(
                    "DELETE FROM ops_job WHERE operation_id IN (SELECT id FROM ops_async_operation WHERE org_id=%s)",
                    (self.org_id,),
                )
                await conn.execute("DELETE FROM ops_async_operation WHERE org_id=%s", (self.org_id,))
                await conn.execute(
                    "DELETE FROM id_service_account_credential WHERE service_account_id IN (SELECT id FROM id_service_account WHERE org_id=%s)",
                    (self.org_id,),
                )
                await conn.execute("DELETE FROM id_service_account WHERE org_id=%s", (self.org_id,))
                await conn.execute("DELETE FROM id_api_key WHERE workspace_id=%s", (self.workspace_id,))
                await conn.execute("DELETE FROM ag_session WHERE workspace_id=%s", (self.workspace_id,))
                await conn.execute("DELETE FROM id_member WHERE org_id=%s", (self.org_id,))
                await conn.execute("DELETE FROM id_workspace WHERE id=%s", (self.workspace_id,))
                await conn.execute("DELETE FROM id_org WHERE id=%s", (self.org_id,))
                await conn.execute("DELETE FROM id_user WHERE id IN (%s, %s)", (self.owner_id, self.target_id))


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _db_pool():
    # 使用本模块独立的 pool，确保可重复打开和关闭
    await pool.open()
    # patch enterprise 和 worker 模块中的 pool 引用，使其指向本模块的独立 pool
    from workama_platform.modules import enterprise
    from workama_platform import worker
    enterprise.pool = pool
    worker.pool = pool
    try:
        async with pool.connection() as conn:
            async with conn.transaction():
                await ensure_enterprise_schema(conn)
        yield
    finally:
        await pool.close()
        # 恢复全局 pool 引用
        from workama_platform import core
        enterprise.pool = core.pool
        worker.pool = core.pool


def _client(actor: Actor):
    app = FastAPI()
    app.include_router(enterprise_router)
    app.dependency_overrides[get_actor] = lambda: actor
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://workspaces-async.test")


async def _job_for_operation(operation_id: str):
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM ops_job WHERE operation_id=%s", (operation_id,))
        return await result.fetchone()


async def _operation(transfer_id: str, org_id: str):
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM ops_async_operation WHERE org_id=%s AND operation_type=%s AND idempotency_key=%s",
            (org_id, OWNER_TRANSFER_OPERATION_TYPE, f"owner-transfer:{transfer_id}"),
        )
        return await result.fetchone()


async def _claimed_job_from_operation(operation_id: str, payload: dict) -> ClaimedJob:
    row = await _job_for_operation(operation_id)
    assert row is not None
    return ClaimedJob(
        id=row["id"],
        operation_id=row["operation_id"],
        workspace_id=row["workspace_id"],
        job_type=row["job_type"],
        payload=payload,
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        lease_token="smoke-lease",
    )


async def _lock_job(operation_id: str) -> None:
    """Take a running lease on the job so the live platform-worker does not race with tests."""
    async with pool.connection() as conn:
        await conn.execute(
            """
            UPDATE ops_job
            SET status='running', lease_token='test-lease', lease_owner='test',
                lease_expires_at=now() + interval '1 hour', updated_at=now()
            WHERE operation_id=%s
            """,
            (operation_id,),
        )


@pytest.mark.asyncio
async def test_owner_transfer_confirms_and_enqueues_async_resource_migration():
    async with _TestTenant() as tenant:
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO id_api_key(id, workspace_id, actor_user_id, name, key_hash, last_four, scopes)
                    VALUES (%s, %s, %s, 'old-owner-key', %s, '1234', ARRAY['platform:read'])
                    """,
                    (new_id("apk"), tenant.workspace_id, tenant.owner_id, secrets.token_hex(32)),
                )
                await conn.execute(
                    "INSERT INTO ag_session(id, workspace_id, user_id, title) VALUES (%s, %s, %s, 'private')",
                    (new_id("ses"), tenant.workspace_id, tenant.owner_id),
                )
                await conn.execute(
                    """
                    INSERT INTO id_service_account(
                        id, org_id, workspace_id, name, owner_user_id, purpose, status,
                        active_credential_version, created_by
                    ) VALUES (%s, %s, %s, 'ci-old-owner', %s, 'test', 'active', 1, %s)
                    """,
                    (new_id("sac"), tenant.org_id, tenant.workspace_id, tenant.owner_id, tenant.owner_id),
                )

        async with _client(tenant.owner) as client:
            transfer_resp = await client.post(
                f"/api/v1/orgs/{tenant.org_id}/owner-transfers",
                json={"target_user_id": tenant.target_id, "reason": "test transfer", "expires_in_seconds": 300},
            )
        assert transfer_resp.status_code == 202, transfer_resp.text
        transfer = transfer_resp.json()
        assert transfer["status"] == "pending"
        transfer_id = transfer["id"]
        confirmation_token = transfer["confirmation_token"]

        async with _client(tenant.target) as client:
            confirm_resp = await client.post(
                f"/api/v1/orgs/{tenant.org_id}/owner-transfers/{transfer_id}/confirm",
                json={"token": confirmation_token},
            )
        assert confirm_resp.status_code == 200, confirm_resp.text
        assert confirm_resp.json()["organization"]["owner_user_id"] == tenant.target_id
        operation_id = confirm_resp.json()["operation_id"]

        op = await _operation(transfer_id, tenant.org_id)
        assert op is not None
        assert op["operation_type"] == OWNER_TRANSFER_OPERATION_TYPE
        assert op["cancellable"] is True
        job = await _job_for_operation(op["id"])
        assert job is not None
        assert job["job_type"] == OWNER_TRANSFER_JOB_TYPE
        assert job["status"] == "queued"
        assert job["scheduled_at"] <= datetime.now(UTC)

        await _lock_job(operation_id)
        job_payload = {
            "transfer_id": transfer_id,
            "from_owner_user_id": tenant.owner_id,
            "to_owner_user_id": tenant.target_id,
            "org_id": tenant.org_id,
        }
        job = await _claimed_job_from_operation(operation_id, job_payload)
        summary = await process_owner_transfer_job(job)
        assert summary["from_owner_user_id"] == tenant.owner_id
        assert summary["to_owner_user_id"] == tenant.target_id
        assert summary["revoked_api_keys"] == 1
        assert summary["archived_sessions"] == 1
        assert summary["transferred_service_accounts"] == 1

        async with pool.connection() as conn:
            result = await conn.execute(
                "SELECT revoked_at FROM id_api_key WHERE actor_user_id=%s AND workspace_id=%s",
                (tenant.owner_id, tenant.workspace_id),
            )
            row = await result.fetchone()
            assert row and row["revoked_at"] is not None

            result = await conn.execute(
                "SELECT status FROM ag_session WHERE user_id=%s AND workspace_id=%s",
                (tenant.owner_id, tenant.workspace_id),
            )
            row = await result.fetchone()
            assert row and row["status"] == "archived"

            result = await conn.execute(
                "SELECT owner_user_id FROM id_service_account WHERE org_id=%s AND name='ci-old-owner'",
                (tenant.org_id,),
            )
            row = await result.fetchone()
            assert row and row["owner_user_id"] == tenant.target_id


@pytest.mark.asyncio
async def test_owner_transfer_worker_is_idempotent():
    async with _TestTenant() as tenant:
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO id_service_account(
                        id, org_id, workspace_id, name, owner_user_id, purpose, status,
                        active_credential_version, created_by
                    ) VALUES (%s, %s, %s, 'ci-idempotent', %s, 'test', 'active', 1, %s)
                    """,
                    (new_id("sac"), tenant.org_id, tenant.workspace_id, tenant.owner_id, tenant.owner_id),
                )

        async with _client(tenant.owner) as client:
            transfer_resp = await client.post(
                f"/api/v1/orgs/{tenant.org_id}/owner-transfers",
                json={"target_user_id": tenant.target_id, "reason": "idempotent test", "expires_in_seconds": 300},
            )
        transfer = transfer_resp.json()
        async with _client(tenant.target) as client:
            confirm_resp = await client.post(
                f"/api/v1/orgs/{tenant.org_id}/owner-transfers/{transfer['id']}/confirm",
                json={"token": transfer["confirmation_token"]},
            )
        operation_id = confirm_resp.json()["operation_id"]
        await _lock_job(operation_id)
        job_payload = {
            "transfer_id": transfer["id"],
            "from_owner_user_id": tenant.owner_id,
            "to_owner_user_id": tenant.target_id,
            "org_id": tenant.org_id,
        }
        job = await _claimed_job_from_operation(operation_id, job_payload)
        first = await process_owner_transfer_job(job)
        assert first["transferred_service_accounts"] == 1

        second = await process_owner_transfer_job(job)
        assert second["revoked_api_keys"] == 0
        assert second["archived_sessions"] == 0
        assert second["transferred_service_accounts"] == 0


@pytest.mark.asyncio
async def test_org_deletion_request_creates_delayed_async_operation():
    async with _TestTenant() as tenant:
        async with _client(tenant.owner) as client:
            deletion_resp = await client.post(
                f"/api/v1/orgs/{tenant.org_id}/deletion-requests",
                json={"reason": "delayed test", "retention_days": 30},
            )
        assert deletion_resp.status_code == 202, deletion_resp.text
        body = deletion_resp.json()
        assert body["status"] == "retention"
        request_id = body["request_id"]
        operation_id = body["operation_id"]

        async with pool.connection() as conn:
            result = await conn.execute(
                "SELECT * FROM ops_async_operation WHERE id=%s",
                (operation_id,),
            )
            op = await result.fetchone()
            assert op["operation_type"] == ORG_DELETION_OPERATION_TYPE
            assert op["cancellable"] is True

            result = await conn.execute(
                "SELECT * FROM ops_job WHERE operation_id=%s",
                (operation_id,),
            )
            job = await result.fetchone()
            assert job["job_type"] == ORG_DELETION_JOB_TYPE
            assert job["status"] == "queued"
            assert job["scheduled_at"] is not None
            retention_until = datetime.fromisoformat(body["retention_until"])
            assert abs(job["scheduled_at"] - retention_until) < timedelta(seconds=2)

            result = await conn.execute(
                "SELECT status, retention_until FROM id_org_deletion_request WHERE id=%s",
                (request_id,),
            )
            req = await result.fetchone()
            assert req["status"] == "retention"


@pytest.mark.asyncio
async def test_org_deletion_request_is_idempotent():
    async with _TestTenant() as tenant:
        async with _client(tenant.owner) as client:
            first = await client.post(
                f"/api/v1/orgs/{tenant.org_id}/deletion-requests",
                json={"reason": "idempotent test", "retention_days": 30},
            )
            second = await client.post(
                f"/api/v1/orgs/{tenant.org_id}/deletion-requests",
                json={"reason": "idempotent test", "retention_days": 30},
            )
        assert first.status_code == 202, first.text
        assert second.status_code == 202, second.text
        assert second.json()["idempotent_replay"] is True
        assert second.json()["operation_id"] == first.json()["operation_id"]
        assert second.json()["request_id"] == first.json()["request_id"]


@pytest.mark.asyncio
async def test_org_deletion_can_be_cancelled_before_retention():
    async with _TestTenant() as tenant:
        async with _client(tenant.owner) as client:
            deletion = await client.post(
                f"/api/v1/orgs/{tenant.org_id}/deletion-requests",
                json={"reason": "cancel test", "retention_days": 30},
            )
            request_id = deletion.json()["request_id"]
            cancel = await client.post(
                f"/api/v1/orgs/{tenant.org_id}/deletion-requests/{request_id}/cancel",
                json={"reason": "changed mind"},
            )
        assert cancel.status_code == 200, cancel.text
        assert cancel.json()["status"] == "cancelled"
        assert cancel.json()["organization_status"] == "active"

        async with pool.connection() as conn:
            result = await conn.execute(
                "SELECT status FROM id_org WHERE id=%s",
                (tenant.org_id,),
            )
            org = await result.fetchone()
            assert org["status"] == "active"

            result = await conn.execute(
                "SELECT id FROM ops_async_operation WHERE org_id=%s AND operation_type=%s AND idempotency_key=%s",
                (tenant.org_id, ORG_DELETION_OPERATION_TYPE, f"org-deletion:{request_id}"),
            )
            op = await result.fetchone()
            assert op is not None
            result = await conn.execute(
                "SELECT status FROM ops_job WHERE operation_id=%s",
                (op["id"],),
            )
            job = await result.fetchone()
            assert job["status"] == "cancelled"


@pytest.mark.asyncio
async def test_org_deletion_worker_executes_after_retention_and_is_idempotent():
    async with _TestTenant() as tenant:
        sa_id = new_id("sac")
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO id_service_account(
                        id, org_id, workspace_id, name, owner_user_id, purpose, status,
                        active_credential_version, created_by
                    ) VALUES (%s, %s, %s, 'ci-deletion', %s, 'test', 'active', 1, %s)
                    """,
                    (sa_id, tenant.org_id, tenant.workspace_id, tenant.owner_id, tenant.owner_id),
                )
                await conn.execute(
                    """
                    INSERT INTO id_service_account_credential(
                        id, service_account_id, version, token_hash, last_four, status, created_by
                    ) VALUES (%s, %s, 1, %s, '1234', 'active', %s)
                    """,
                    (new_id("sacred"), sa_id, secrets.token_hex(32), tenant.owner_id),
                )

        async with _client(tenant.owner) as client:
            deletion = await client.post(
                f"/api/v1/orgs/{tenant.org_id}/deletion-requests",
                json={"reason": "worker test", "retention_days": 30},
            )
        request_id = deletion.json()["request_id"]
        operation_id = deletion.json()["operation_id"]

        async with pool.connection() as conn:
            async with conn.transaction():
                past = datetime.now(UTC) - timedelta(seconds=5)
                await conn.execute(
                    "UPDATE id_org_deletion_request SET retention_until=%s WHERE id=%s",
                    (past, request_id),
                )
                await conn.execute(
                    "UPDATE id_org SET deletion_scheduled_at=%s WHERE id=%s",
                    (past, tenant.org_id),
                )
                await conn.execute(
                    """
                    UPDATE ops_job SET scheduled_at=%s, status='running', lease_token='test-lease',
                       lease_owner='test', lease_expires_at=now() + interval '1 hour', updated_at=now()
                     WHERE operation_id=%s
                    """,
                    (past, operation_id),
                )

        await _lock_job(operation_id)
        job_payload = {"request_id": request_id, "org_id": tenant.org_id}
        job = await _claimed_job_from_operation(operation_id, job_payload)
        summary = await process_org_deletion_job(job)
        assert summary["request_id"] == request_id
        assert summary["org_id"] == tenant.org_id
        assert summary["status"] == "deleted"
        assert summary.get("skipped") is not True
        assert summary["disabled_workspaces"] == 1

        async with pool.connection() as conn:
            result = await conn.execute(
                "SELECT status FROM id_workspace WHERE id=%s",
                (tenant.workspace_id,),
            )
            ws = await result.fetchone()
            assert ws["status"] == "disabled"

            result = await conn.execute(
                "SELECT status FROM id_service_account WHERE org_id=%s",
                (tenant.org_id,),
            )
            sa = await result.fetchone()
            assert sa["status"] == "revoked"

            result = await conn.execute(
                "SELECT status FROM id_org WHERE id=%s",
                (tenant.org_id,),
            )
            org = await result.fetchone()
            assert org["status"] == "deleted"

        second = await process_org_deletion_job(job)
        assert second["status"] == "deleted"
        assert second["skipped"] is True


@pytest.mark.asyncio
async def test_cross_tenant_actor_receives_404():
    async with _TestTenant() as tenant_a, _TestTenant() as tenant_b:
        async with _client(tenant_b.owner) as client:
            transfer = await client.post(
                f"/api/v1/orgs/{tenant_a.org_id}/owner-transfers",
                json={"target_user_id": tenant_a.target_id, "reason": "x", "expires_in_seconds": 300},
            )
            assert transfer.status_code == 404

            deletion = await client.post(
                f"/api/v1/orgs/{tenant_a.org_id}/deletion-requests",
                json={"reason": "x", "retention_days": 30},
            )
            assert deletion.status_code == 404


@pytest.mark.asyncio
async def test_non_owner_cannot_request_deletion():
    async with _TestTenant() as tenant:
        admin_id = new_id("usr")
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO id_user(id, email, password_hash, display_name, status, email_verified)
                    VALUES (%s, %s, 'test', %s, 'active', TRUE)
                    """,
                    (admin_id, f"{admin_id}@example.com", f"Admin {admin_id}"),
                )
                await conn.execute(
                    "INSERT INTO id_member(id, org_id, workspace_id, user_id, role) VALUES (%s, %s, %s, %s, 'admin')",
                    (new_id("mem"), tenant.org_id, tenant.workspace_id, admin_id),
                )
        admin = _actor(admin_id, tenant.org_id, tenant.workspace_id, role="admin", capabilities=("*",))
        async with _client(admin) as client:
            deletion = await client.post(
                f"/api/v1/orgs/{tenant.org_id}/deletion-requests",
                json={"reason": "admin attempt", "retention_days": 30},
            )
            assert deletion.status_code == 403
