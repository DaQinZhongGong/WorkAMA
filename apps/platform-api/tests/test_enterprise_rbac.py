import pytest

from workama_platform.modules import enterprise_rbac


def test_enterprise_models_normalize_capabilities_and_reject_reserved_values():
    role = enterprise_rbac.RoleCreate(name="  Support Role ", capabilities=["dataset:read", "dataset:read", "memory:read"], idempotency_key="role-1")
    assert role.name == "Support Role"
    assert role.capabilities == ["dataset:read", "memory:read"]
    with pytest.raises(ValueError):
        enterprise_rbac.RoleCreate(name="Unsafe", capabilities=["platform:*"])
    with pytest.raises(ValueError):
        enterprise_rbac.GroupCreate(name="SCIM", source="scim")


def test_service_policy_normalizes_cidrs_and_fails_closed_inputs():
    policy = enterprise_rbac.ServiceAccountPolicyCreate(
        service_account_id="svc_1",
        allowed_scopes=["dataset:read", "dataset:read"],
        allowed_ip_cidrs=["10.0.0.1/24", "10.0.0.0/24"],
        idempotency_key="policy-1",
    )
    assert policy.allowed_scopes == ["dataset:read"]
    assert policy.allowed_ip_cidrs == ["10.0.0.0/24"]
    with pytest.raises(ValueError):
        enterprise_rbac.ServiceAccountPolicyCreate(service_account_id="svc_1", allowed_scopes=["platform:*"])
    with pytest.raises(ValueError):
        enterprise_rbac.ServiceAccountPolicyEvaluate(scope="dataset:read", source_ip="not-an-ip")


def test_conditions_never_accept_secret_like_keys_and_auth_policy_is_bounded():
    with pytest.raises(ValueError):
        enterprise_rbac.RoleBindingCreate(role_id="role_1", subject_type="user", subject_id="user_1", conditions={"api_key": "secret"})
    policy = enterprise_rbac.AuthStrengthPolicyCreate(operation="external_app.invoke", required_auth_strength=2, idempotency_key="auth-1")
    assert policy.operation == "external_app.invoke"
    with pytest.raises(ValueError):
        enterprise_rbac.AuthStrengthPolicyCreate(operation="bad op", required_auth_strength=2)


def test_hash_only_views_do_not_expose_request_hash_or_sensitive_fields():
    view = enterprise_rbac._service_policy_view({
        "id": "sap_1", "org_id": "org_1", "workspace_id": "ws_1", "service_account_id": "svc_1",
        "allowed_scopes": ["dataset:read"], "allowed_ip_cidrs": ["10.0.0.0/24"], "status": "active",
        "expires_at": None, "version": 1, "created_by": "user_1", "created_at": None, "updated_at": None,
        "request_hash": "private",
    })
    assert view["allowed_scopes"] == ["dataset:read"]
    assert "request_hash" not in view
    assert "secret" not in str(view).lower()


def test_routes_cover_group_role_binding_service_policy_and_auth_matrix():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in enterprise_rbac.router.routes}
    for expected in (
        ("/api/v1/enterprise/groups", ("GET",)),
        ("/api/v1/enterprise/groups", ("POST",)),
        ("/api/v1/enterprise/groups/{group_id}/members", ("POST",)),
        ("/api/v1/enterprise/roles", ("POST",)),
        ("/api/v1/enterprise/role-bindings", ("POST",)),
        ("/api/v1/enterprise/service-account-policies/{policy_id}/evaluate", ("POST",)),
        ("/api/v1/enterprise/auth-strength-matrix/evaluate", ("POST",)),
    ):
        assert expected in paths


@pytest.mark.asyncio
async def test_schema_is_additive_and_has_tenant_idempotency_indexes():
    statements = []

    class Connection:
        async def execute(self, statement):
            statements.append(statement)

    await enterprise_rbac.ensure_enterprise_rbac_schema(Connection())
    schema = "\n".join(statements)
    for table in ("id_group", "id_group_member", "id_role", "id_role_binding", "id_service_account_policy", "id_auth_strength_policy"):
        assert table in schema
    for marker in ("request_hash", "create_idempotency_key", "allowed_ip_cidrs", "required_auth_strength"):
        assert marker in schema
    assert "idx_id_role_binding_idempotency" in schema
