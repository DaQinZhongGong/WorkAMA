from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from workama_platform.modules import connectors


def _connector(
    *,
    connector_id="conn_1",
    provider="mock",
    auth_mode="none",
    name="Knowledge Mock",
    manifest=None,
    status="active",
):
    return {
        "id": connector_id,
        "org_id": "org_1",
        "workspace_id": "wsp_1",
        "name": name,
        "provider": provider,
        "auth_mode": auth_mode,
        "endpoint_ref": "mock://connector/knowledge-mock" if provider == "mock" else "local://artifact/art_1",
        "manifest": manifest or {},
        "status": status,
        "enabled": status == "active",
        "source_cursor": {},
        "version": 1,
    }


def test_endpoint_is_controlled_and_rejects_ssrf_path_and_credentials():
    assert connectors.validate_endpoint("mock://connector/wiki", "mock") == "mock://connector/wiki"
    assert connectors.validate_endpoint("local://artifact/art_1", "local") == "local://artifact/art_1"
    for provider, value in [
        ("mock", "https://127.0.0.1/admin"),
        ("mock", "mock://connector/wiki?token=secret"),
        ("local", "file:///etc/passwd"),
        ("local", "local://artifact/../escape"),
        ("local", "local://artifact/C:/secret"),
        ("local", "local://artifact/art_1#fragment"),
        ("mock", "mock://connector/user:pass@wiki"),
    ]:
        with pytest.raises(ValueError):
            connectors.validate_endpoint(value, provider)


def test_manifest_rejects_secrets_and_arbitrary_external_fields_but_allows_document_content():
    valid = connectors.validate_manifest(
        {
            "description": "Internal handbook",
            "documents": [
                {
                    "source_id": "source:one",
                    "source_version": 2,
                    "title": "Runbook",
                    "content": "The runbook may mention https://docs.example.test in its body.",
                    "acl": {"roles": ["member"]},
                }
            ],
        }
    )
    assert valid["documents"][0]["source_version"] == "2"
    assert valid["documents"][0]["acl"] == {"allow_users": [], "allow_groups": [], "allow_roles": ["member"]}
    for manifest in (
        {"endpoint": "https://evil.test"},
        {"credentials": {"client_secret": "raw"}},
        {"description": "file:///etc/passwd"},
        {"documents": [{"source_id": "../escape", "content": "safe"}]},
    ):
        with pytest.raises(ValueError):
            connectors.validate_manifest(manifest)


def test_credentials_are_allowlisted_hashed_and_never_returned_in_view():
    credentials = connectors.normalize_credentials(
        {"client_id": "client-1", "client_secret": "raw-secret"}, auth_mode="oauth"
    )
    digest = connectors.credential_hash(credentials)
    assert digest and digest != "raw-secret"
    with pytest.raises(ValueError):
        connectors.normalize_credentials({"secret": "raw-secret"}, auth_mode="oauth")
    with pytest.raises(ValueError):
        connectors.normalize_credentials({"client_secret": "raw-secret"}, auth_mode="none")
    view = connectors._connector_view(
        {**_connector(auth_mode="oauth", status="pending"), "credential_hash": digest, "credential_ref": None}
    )
    assert view["credential_configured"] is True
    assert "raw-secret" not in str(view)
    assert "credential_hash" not in view


def test_oauth_and_service_account_are_pending_and_not_executed():
    for auth_mode in ("oauth", "service_account"):
        body = connectors.ConnectorCreate(
            name=f"{auth_mode} connector",
            provider="mock",
            auth_mode=auth_mode,
            credentials={"client_id": "client-1"} if auth_mode == "oauth" else {"service_account_id": "sa-1"},
        )
        assert connectors._connector_status(body.auth_mode, body.enabled) == "pending"
        assert connectors.ConnectorCreate(name="disabled", provider="mock", auth_mode=auth_mode, enabled=False).enabled is False


def test_mock_full_and_incremental_cursors_are_deterministic_and_idempotent():
    connector = _connector()
    full, full_cursor = connectors.snapshot_for_sync(connector, "full")
    assert len(full) == 2
    assert full_cursor["sources"]
    connector["source_cursor"] = full_cursor
    incremental, same_cursor = connectors.snapshot_for_sync(connector, "incremental")
    assert incremental == []
    assert same_cursor == full_cursor
    changed = dict(full[0])
    changed["source_version"] = "2"
    connector["manifest"] = {"documents": [changed]}
    delta, delta_cursor = connectors.snapshot_for_sync(connector, "incremental")
    assert [item["source_id"] for item in delta] == [changed["source_id"]]
    assert delta_cursor["sources"][changed["source_id"]]["source_version"] == "2"


def test_acl_allow_deny_revoke_and_default_fail_closed():
    allowed = {"status": "active", "acl": {"allow_users": ["user_1"], "allow_groups": [], "allow_roles": []}}
    group_allowed = {"status": "active", "acl": {"allow_users": [], "allow_groups": ["eng"], "allow_roles": []}}
    role_allowed = {"status": "active", "acl": {"allow_users": [], "allow_groups": [], "allow_roles": ["member"]}}
    revoked = {"status": "revoked", "acl": {"allow_users": ["user_1"]}}
    tombstone = {"status": "tombstone", "acl": {"allow_roles": ["member"]}}
    empty_acl = {"status": "active", "acl": {}}
    assert connectors.document_visible(allowed, user_id="user_1")
    assert not connectors.document_visible(allowed, user_id="user_2")
    assert connectors.document_visible(group_allowed, user_id="user_2", groups=["eng"])
    assert not connectors.document_visible(group_allowed, user_id="user_2", groups=["finance"])
    assert connectors.document_visible(role_allowed, user_id="user_2", roles=["member"])
    assert not connectors.document_visible(revoked, user_id="user_1", roles=["member"])
    assert not connectors.document_visible(tombstone, user_id="user_1", roles=["member"])
    assert not connectors.document_visible(empty_acl, user_id="user_1", roles=["owner"])
    assert connectors._source_item_status({"acl": {"allow_users": [], "allow_groups": [], "allow_roles": []}}) == "revoked"
    assert connectors._source_item_status({"acl": None, "content": "private by default"}) == "active"


def test_visible_documents_filters_cross_workspace_and_inaccessible_rows():
    rows = [
        {"id": "1", "workspace_id": "wsp_1", "status": "active", "acl": {"allow_users": ["u_1"]}},
        {"id": "2", "workspace_id": "wsp_2", "status": "active", "acl": {"allow_users": ["u_1"]}},
        {"id": "3", "workspace_id": "wsp_1", "status": "active", "acl": {"allow_roles": ["member"]}},
    ]
    visible = connectors.visible_documents(rows, user_id="u_1", roles=["viewer"], workspace_id="wsp_1")
    assert [row["id"] for row in visible] == ["1"]


def test_local_source_is_reference_only_and_does_not_accept_arbitrary_path():
    connector = _connector(provider="local")
    items = connectors.local_source_items(connector)
    assert items[0]["content"] is None
    assert items[0]["content_ref"] == "local://artifact/art_1"
    with pytest.raises(ValueError):
        connectors.validate_controlled_reference("local://artifact/../escape")


def test_connector_and_sync_run_views_do_not_expose_secret_or_raw_hash_fields():
    connector_view = connectors._connector_view(
        {**_connector(), "credential_hash": "hmac-value", "credential_ref": "vault-ref", "manifest": {"description": "safe"}}
    )
    assert connector_view["credential_configured"] is True
    assert "credential_hash" not in connector_view
    run_view = connectors._run_view(
        {
            "id": "run_1",
            "connector_id": "conn_1",
            "workspace_id": "wsp_1",
            "mode": "full",
            "idempotency_key": "client-key",
            "input_hash": "private-hash",
            "status": "succeeded",
            "execution_status": "executed",
            "executed": True,
            "error_message": "safe",
        }
    )
    assert run_view["idempotency_key"] == "client-key"
    assert "input_hash" not in run_view


def test_router_exposes_crud_sync_acl_and_document_routes():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in connectors.router.routes}
    assert ("/api/v1/connectors", ("GET",)) in paths
    assert ("/api/v1/connectors", ("POST",)) in paths
    assert ("/api/v1/connectors/{connector_id}", ("DELETE",)) in paths
    assert ("/api/v1/connectors/{connector_id}/sync", ("POST",)) in paths
    assert ("/api/v1/connectors/{connector_id}/sync-runs", ("GET",)) in paths
    assert ("/api/v1/connectors/{connector_id}/documents", ("GET",)) in paths
    assert ("/api/v1/connectors/{connector_id}/identity-mappings", ("POST",)) in paths


@pytest.mark.asyncio
async def test_schema_is_additive_and_has_workspace_idempotency_and_revoke_contract():
    statements = []

    class Connection:
        async def execute(self, statement):
            statements.append(statement)

    await connectors.ensure_connectors_schema(Connection())
    schema = "\n".join(statements)
    for table in (
        "pf_connector",
        "pf_connector_run",
        "pf_connector_document",
        "pf_connector_document_acl",
        "pf_connector_identity_mapping",
    ):
        assert table in schema
    for field in ("workspace_id", "source_cursor", "idempotency_key_hash", "content_sha256", "revoked_at"):
        assert field in schema
    assert "UNIQUE(connector_id,idempotency_key_hash)" in schema
    assert "status IN ('active','tombstone','revoked')" in schema
