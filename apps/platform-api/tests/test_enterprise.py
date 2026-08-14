from datetime import UTC, datetime, timedelta
import os

import httpx
import pytest
from fastapi import HTTPException
from fastapi import FastAPI

from workama_platform.core import Actor
from workama_platform.modules.enterprise import (
    DELETION_STATUSES,
    SCHEMA_STATEMENTS,
    SERVICE_ACCOUNT_TOKEN_PREFIX,
    _action_hash,
    _service_account_payload,
    _with_secret,
    capability_granted,
    generate_service_account_token,
    normalize_service_account_scopes,
    router,
    service_account_token_hash,
    ensure_enterprise_schema,
    OwnerTransferConfirmRequest,
    OrganizationDeletionRequest,
    ServiceAccountCreate,
    authenticate_service_account_token,
    new_id,
    pool,
)
from workama_platform.core import get_actor


def _actor(
    *,
    role: str = "admin",
    capabilities: tuple[str, ...] = ("api_key:*", "workspace:*"),
    auth_strength: int = 2,
    actor_type: str = "user",
) -> Actor:
    return Actor(
        user_id="usr_owner",
        workspace_id="wsp_test",
        org_id="org_test",
        role=role,
        email="owner@example.com",
        display_name="Owner",
        onboarding_completed=True,
        actor_type=actor_type,
        capabilities=capabilities,
        auth_strength=auth_strength,
    )


class _RecordingConnection:
    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)


def test_service_account_scopes_are_normalized_and_high_risk_scopes_rejected():
    assert normalize_service_account_scopes(["session:write", "platform:read", "session:write"]) == [
        "platform:read",
        "session:write",
    ]
    with pytest.raises(HTTPException) as exc:
        normalize_service_account_scopes(["org:delete"])
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        normalize_service_account_scopes(["invalid-scope"])
    assert exc.value.status_code == 422


def test_service_account_token_is_hash_only_and_prefixed():
    token, digest = generate_service_account_token()
    assert token.startswith(SERVICE_ACCOUNT_TOKEN_PREFIX)
    assert digest == service_account_token_hash(token)
    assert token not in digest
    with pytest.raises(ValueError):
        service_account_token_hash("not-a-service-token")


def test_existing_api_key_capability_is_a_compatibility_gate_for_service_accounts():
    assert capability_granted(_actor(), "service_account:create")
    assert capability_granted(_actor(capabilities=("service_account:read",)), "service_account:read")
    assert not capability_granted(_actor(role="member", capabilities=("session:*",)), "service_account:create")
    assert capability_granted(_actor(actor_type="api_key"), "service_account:create")


def test_service_account_payload_never_contains_secret_or_hash():
    row = {
        "id": "sac_test",
        "org_id": "org_test",
        "workspace_id": "wsp_test",
        "name": "ci",
        "owner_user_id": "usr_owner",
        "purpose": "builds",
        "status": "active",
        "effective_status": "active",
        "expires_at": None,
        "network_policy": {},
        "scopes": ["platform:read"],
        "active_credential_version": 1,
        "credential_version": 1,
        "last_four": "1234",
        "last_used_at": None,
        "created_by": "usr_owner",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    payload = _service_account_payload(row)
    assert "token" not in payload
    assert "token_hash" not in payload
    one_time = _with_secret(payload, "sa-wama-one-time-secret", 1, "cret")
    assert one_time["token"] == "sa-wama-one-time-secret"
    assert one_time["credential"]["token"] == "sa-wama-one-time-secret"


def test_high_risk_request_models_preserve_retention_and_confirmation_contracts():
    confirmation = OwnerTransferConfirmRequest.model_validate({"token": "x" * 32})
    assert confirmation.confirmation_token == "x" * 32
    confirmation_alias = OwnerTransferConfirmRequest.model_validate({"confirmation_token": "y" * 32})
    assert confirmation_alias.confirmation_token == "y" * 32
    deletion = OrganizationDeletionRequest(reason="customer request", retention_days=30)
    assert deletion.retention_days == 30
    assert DELETION_STATUSES == {"retention", "cancelled", "deleting", "deleted"}


def test_action_hash_is_deterministic_and_does_not_need_plaintext_secret():
    payload = {"org_id": "org_test", "to_owner_user_id": "usr_target"}
    assert _action_hash(payload) == _action_hash(payload)
    assert len(_action_hash(payload)) == 64


def test_router_contains_enterprise_identity_and_lifecycle_operations():
    routes = {(route.path, tuple(sorted(route.methods or ()))) for route in router.routes}
    assert ("/api/v1/service-accounts", ("GET",)) in routes
    assert ("/api/v1/service-accounts", ("POST",)) in routes
    assert ("/api/v1/service-accounts/{service_account_id}", ("DELETE",)) in routes
    assert ("/api/v1/service-accounts/{service_account_id}/credential-rotations", ("POST",)) in routes
    assert ("/api/v1/orgs/{org_id}/owner-transfers", ("POST",)) in routes
    assert ("/api/v1/orgs/{org_id}/owner-transfers/{transfer_id}/confirm", ("POST",)) in routes
    assert ("/api/v1/orgs/{org_id}/deletion-requests", ("POST",)) in routes
    assert ("/api/v1/orgs/{org_id}/deletion-requests/{request_id}/cancel", ("POST",)) in routes


def test_router_can_be_loaded_and_openapi_can_be_generated_without_main_integration():
    app = FastAPI()
    app.include_router(router)
    schema = app.openapi()
    assert "/api/v1/service-accounts" in schema["paths"]
    assert "/api/v1/orgs/{org_id}/owner-transfers/{transfer_id}/confirm" in schema["paths"]
    assert "/api/v1/orgs/{org_id}/deletion-requests/{request_id}/cancel" in schema["paths"]


@pytest.mark.asyncio
async def test_ensure_enterprise_schema_executes_additive_statements():
    conn = _RecordingConnection()
    await ensure_enterprise_schema(conn)
    assert len(conn.statements) == len(SCHEMA_STATEMENTS)
    joined = "\n".join(conn.statements)
    assert "id_service_account" in joined
    assert "id_service_account_credential" in joined
    assert "id_org_owner_transfer_fact" in joined
    assert "id_org_deletion_request" in joined
    assert "token_hash" in joined


def test_expiry_values_used_by_the_contract_are_time_bounded():
    expires_at = datetime.now(UTC) + timedelta(days=30)
    body = ServiceAccountCreate(name="ci", expires_at=expires_at)
    assert body.expires_at == expires_at


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("RUN_ENTERPRISE_LIVE") != "1", reason="requires an explicit live WorkAMA database")
async def test_live_enterprise_identity_lifecycle():
    owner_id = new_id("usr")
    target_id = new_id("usr")
    org_id = new_id("org")
    workspace_id = new_id("wsp")
    service_account_id = None
    await pool.open()
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO id_user(id, email, password_hash, display_name, status, email_verified) VALUES (%s, %s, 'test', 'Enterprise Owner', 'active', TRUE)",
                (owner_id, f"{owner_id}@example.com"),
            )
            await conn.execute(
                "INSERT INTO id_user(id, email, password_hash, display_name, status, email_verified) VALUES (%s, %s, 'test', 'Enterprise Target', 'active', TRUE)",
                (target_id, f"{target_id}@example.com"),
            )
            await conn.execute(
                "INSERT INTO id_org(id, name, owner_user_id, status) VALUES (%s, 'Enterprise Live Test', %s, 'active')",
                (org_id, owner_id),
            )
            await conn.execute(
                "INSERT INTO id_workspace(id, org_id, name, slug, status) VALUES (%s, %s, 'Enterprise Live Test', %s, 'active')",
                (workspace_id, org_id, f"live-{workspace_id[-8:].lower()}"),
            )
            await conn.execute(
                "INSERT INTO id_member(id, org_id, workspace_id, user_id, role) VALUES (%s, %s, %s, %s, 'owner')",
                (new_id("mem"), org_id, workspace_id, owner_id),
            )
            await conn.execute(
                "INSERT INTO id_member(id, org_id, workspace_id, user_id, role) VALUES (%s, %s, NULL, %s, 'member')",
                (new_id("mem"), org_id, target_id),
            )
            await conn.commit()

        owner = Actor(
            user_id=owner_id,
            workspace_id=workspace_id,
            org_id=org_id,
            role="owner",
            email=f"{owner_id}@example.com",
            display_name="Enterprise Owner",
            onboarding_completed=True,
            capabilities=("*",),
            auth_strength=2,
        )
        target = Actor(
            user_id=target_id,
            workspace_id=workspace_id,
            org_id=org_id,
            role="member",
            email=f"{target_id}@example.com",
            display_name="Enterprise Target",
            onboarding_completed=True,
            capabilities=("*",),
            auth_strength=2,
        )
        current_actor = owner
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_actor] = lambda: current_actor

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://enterprise.test"
        ) as client:
            created = await client.post(
                "/api/v1/service-accounts",
                json={
                    "name": "live-ci",
                    "workspace_id": workspace_id,
                    "purpose": "enterprise lifecycle test",
                    "scopes": ["platform:read"],
                },
            )
            assert created.status_code == 201, created.text
            created_payload = created.json()
            service_account_id = created_payload["id"]
            old_token = created_payload["token"]
            assert old_token.startswith(SERVICE_ACCOUNT_TOKEN_PREFIX)
            assert "token_hash" not in created_payload
            resolved = await authenticate_service_account_token(old_token)
            assert resolved and resolved["service_account_id"] == service_account_id

            fetched = await client.get(f"/api/v1/service-accounts/{service_account_id}")
            assert fetched.status_code == 200
            assert "token" not in fetched.json()
            updated = await client.patch(
                f"/api/v1/service-accounts/{service_account_id}",
                json={"purpose": "updated purpose"},
            )
            assert updated.status_code == 200
            assert updated.json()["purpose"] == "updated purpose"

            rotated = await client.post(
                f"/api/v1/service-accounts/{service_account_id}/credential-rotations",
                json={"reason": "scheduled rotation"},
            )
            assert rotated.status_code == 200
            new_token = rotated.json()["token"]
            assert new_token != old_token
            assert await authenticate_service_account_token(old_token) is None
            assert (await authenticate_service_account_token(new_token))["credential_version"] == 2

            transfer = await client.post(
                f"/api/v1/orgs/{org_id}/owner-transfers",
                json={"target_user_id": target_id, "reason": "live transfer"},
            )
            assert transfer.status_code == 202, transfer.text
            confirmation_token = transfer.json()["confirmation_token"]
            current_actor = target
            confirmed = await client.post(
                f"/api/v1/orgs/{org_id}/owner-transfers/{transfer.json()['id']}/confirm",
                json={"token": confirmation_token},
            )
            assert confirmed.status_code == 200, confirmed.text
            assert confirmed.json()["organization"]["owner_user_id"] == target_id

            deletion = await client.post(
                f"/api/v1/orgs/{org_id}/deletion-requests",
                json={"reason": "live retention test", "retention_days": 30},
            )
            assert deletion.status_code == 202, deletion.text
            request_id = deletion.json()["operation_id"]
            assert deletion.json()["status"] == "retention"
            cancelled = await client.post(
                f"/api/v1/orgs/{org_id}/deletion-requests/{request_id}/cancel",
                json={"reason": "live cancellation test"},
            )
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["organization_status"] == "active"

            revoked = await client.request(
                "DELETE",
                f"/api/v1/service-accounts/{service_account_id}",
                json={"reason": "cleanup"},
            )
            assert revoked.status_code == 204, revoked.text
            assert await authenticate_service_account_token(new_token) is None
    finally:
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM id_member WHERE org_id=%s", (org_id,))
            await conn.execute("DELETE FROM id_workspace WHERE id=%s", (workspace_id,))
            await conn.execute("DELETE FROM id_org WHERE id=%s", (org_id,))
            await conn.execute("DELETE FROM id_user WHERE id IN (%s, %s)", (owner_id, target_id))
            await conn.commit()
        await pool.close()
