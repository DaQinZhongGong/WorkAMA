from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import HTTPException

from workama_platform.core import Actor
from workama_platform.modules import mcp


def _actor(*, org_id: str = "org_a", workspace_id: str = "ws_a", role: str = "admin") -> Actor:
    return Actor(
        user_id="usr_a",
        workspace_id=workspace_id,
        org_id=org_id,
        role=role,
        email="a@example.com",
        display_name="A",
        onboarding_completed=True,
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1/mcp",
        "http://localhost/mcp",
        "http://169.254.169.254/latest/meta-data",
        "http://100.100.100.200/metadata",
        "http://100.64.0.1/mcp",
        "http://2130706433/mcp",
        "http://0x7f000001/mcp",
        "http://127.1/mcp",
        "http://[::1]/mcp",
        "http://example.com:0/mcp",
        "http://user:password@example.com/mcp",
    ],
)
def test_ssrf_validation_rejects_private_metadata_and_credentials(endpoint: str):
    with pytest.raises(ValueError):
        mcp.validate_endpoint_url(endpoint)


def test_ssrf_validation_allows_public_url_without_resolving_at_registration():
    assert mcp.validate_endpoint_url("https://example.com/mcp") == "https://example.com/mcp"


def test_ssrf_validation_rechecks_dns_results_before_connection():
    with pytest.raises(ValueError, match="resolves to a private"):
        mcp.validate_endpoint_url(
            "https://mcp.example.test/sse",
            resolve=True,
            resolver=lambda host, port: ["10.20.30.40"],
        )
    assert (
        mcp.validate_endpoint_url(
            "https://mcp.example.test/sse",
            resolve=True,
            resolver=lambda host, port: ["93.184.216.34"],
        )
        == "https://mcp.example.test/sse"
    )


def test_transport_validation_does_not_allow_inline_stdio_credentials():
    with pytest.raises(ValueError, match="inline credentials"):
        mcp.validate_transport_target("stdio", "node server.js --token=secret")
    assert mcp.validate_transport_target("stdio", "node server.js") == "node server.js"


def test_server_reported_low_risk_cannot_downgrade_sensitive_capability():
    snapshot, schema_hash = mcp.normalize_capability_snapshot(
        tools=[
            {
                "name": "delete_file",
                "description": "Delete a file",
                "risk": "low",
                "annotations": {"readOnlyHint": True, "destructiveHint": False},
                "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
            {"name": "read_password", "riskLevel": "low"},
        ],
    )
    delete_tool = snapshot["tools"][0]
    password_tool = snapshot["tools"][1]
    assert delete_tool["platform_risk"] == "high"
    assert password_tool["platform_risk"] == "critical"
    assert delete_tool["risk_source"] == "workama_policy"
    assert delete_tool["server_risk_ignored"] is True
    assert "risk" not in delete_tool
    assert "annotations" not in delete_tool
    assert len(schema_hash) == 64


def test_server_capability_policy_fields_are_removed_recursively():
    snapshot, _ = mcp.normalize_capability_snapshot(
        server_capabilities={"risk": "low", "nested": {"risk_level": "low", "list": [{"sensitive": False}]}}
    )
    assert snapshot["server_capabilities"] == {"nested": {"list": [{}]}}


def test_capability_snapshot_marks_all_server_content_untrusted_and_is_stable():
    one, first_hash = mcp.normalize_capability_snapshot(
        resources=[{"name": "docs", "uri": "https://example.com/docs"}],
        prompts=[{"name": "summarize", "description": "Summarize"}],
    )
    two, second_hash = mcp.normalize_capability_snapshot(
        resources=[{"name": "docs", "uri": "https://example.com/docs"}],
        prompts=[{"name": "summarize", "description": "Summarize"}],
    )
    assert first_hash == second_hash
    assert one == two
    assert one["untrusted_source"] is True
    assert all(item["untrusted_source"] for kind in mcp.MCP_CAPABILITY_KINDS for item in one[kind])


def test_oauth_metadata_exposes_secure_envelope_without_claiming_provider_exchange():
    metadata = mcp.oauth_metadata_placeholder("mcp_123")
    assert metadata["status"] == "pending_external_configuration"
    assert metadata["configured"] is False
    assert metadata["credential_upload_supported"] is False
    assert metadata["authorization_state_supported"] is True
    assert metadata["server_id"] == "mcp_123"
    assert metadata["token_endpoint"] is None
    assert "secret" not in str(metadata).lower()


def test_workspace_permissions_are_separate_from_server_reported_capabilities():
    assert mcp.can_read_mcp(_actor(role="member"))
    assert mcp.can_manage_mcp(_actor(role="admin"))
    assert not mcp.can_manage_mcp(_actor(role="member"))
    assert not mcp.can_read_mcp(_actor(role="unknown"))


@dataclass
class _Result:
    rows: list[dict]

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.calls: list[tuple[str, tuple]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, query: str, params=()):
        self.calls.append((query, tuple(params)))
        return _Result(self.rows)


class _Pool:
    def __init__(self, connection: _Connection):
        self.connection_instance = connection

    def connection(self):
        return self.connection_instance


def _server_row(*, workspace_id: str = "ws_a", auth_type: str = "none") -> dict:
    return {
        "id": "mcp_a",
        "org_id": "org_a",
        "workspace_id": workspace_id,
        "name": "Docs",
        "transport": "streamable_http",
        "endpoint_or_command": "https://example.com/mcp",
        "auth_type": auth_type,
        "auth_ref": None,
        "protocol_version": "2025-06-18",
        "server_identity": {},
        "capabilities": {"tools": [], "resources": [], "prompts": []},
        "schema_hash": "hash",
        "roots": [],
        "approval_policy": "explicit",
        "risk_policy": {"source": "workama"},
        "status": "draft",
        "last_test": {},
        "last_tested_at": None,
        "version": 1,
        "created_by": "usr_a",
        "created_at": None,
        "updated_at": None,
    }


class _RedisState:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, *, ex: int, nx: bool):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def getdel(self, key: str):
        return self.values.pop(key, None)


@pytest.mark.asyncio
async def test_mcp_oauth_authorization_reserves_pkce_state_and_callback_consumes_once(monkeypatch):
    connection = _Connection([_server_row(auth_type="oauth")])
    monkeypatch.setattr(mcp, "pool", _Pool(connection))
    state_store = _RedisState()
    monkeypatch.setattr(mcp, "redis", state_store)

    started = await mcp.start_mcp_server_authorization(
        "mcp_a",
        mcp.McpAuthorizationStart(scopes=["mcp:tools"]),
        _actor(),
    )

    assert started["status"] == "pending_external"
    assert started["provider_execution"] == "pending_external_exchange"
    assert len(started["code_challenge"]) == 43
    assert started["authorization_url"] is None
    assert started["credential_upload_supported"] is False
    assert "code_verifier" not in str(started)

    completed = await mcp.complete_mcp_server_authorization(code="provider-code", state=started["state"])
    assert completed["status"] == "pending_external_exchange"
    assert completed["server_id"] == "mcp_a"
    assert completed["credential_persisted"] is False
    assert "provider-code" not in str(completed)

    with pytest.raises(HTTPException, match="invalid or expired"):
        await mcp.complete_mcp_server_authorization(code="provider-code", state=started["state"])


@pytest.mark.asyncio
async def test_mcp_oauth_callback_rejection_consumes_state_without_leaking_error(monkeypatch):
    state_store = _RedisState()
    monkeypatch.setattr(mcp, "redis", state_store)
    state = "state-for-rejection-1234567890"
    state_store.values[f"mcp:oauth:state:{state}"] = mcp.json_dumps({"state": state, "server_id": "mcp_a"})

    rejected = await mcp.complete_mcp_server_authorization(state=state, error="access_denied")

    assert rejected["status"] == "rejected"
    assert rejected["provider_execution"] == "rejected_external"
    assert rejected["state_received"] is True
    assert rejected["credential_persisted"] is False
    assert "access_denied" not in str(rejected)


@pytest.mark.asyncio
async def test_mcp_oauth_rejects_unallowlisted_scopes_before_reserving_state(monkeypatch):
    connection = _Connection([_server_row(auth_type="oauth")])
    monkeypatch.setattr(mcp, "pool", _Pool(connection))
    state_store = _RedisState()
    monkeypatch.setattr(mcp, "redis", state_store)

    with pytest.raises(HTTPException, match="Unsupported MCP OAuth scopes") as exc:
        await mcp.start_mcp_server_authorization(
            "mcp_a",
            mcp.McpAuthorizationStart(scopes=["mcp:tools", "admin:root"]),
            _actor(),
        )

    assert exc.value.status_code == 422
    assert state_store.values == {}


@pytest.mark.asyncio
async def test_list_query_is_tenant_bound(monkeypatch):
    connection = _Connection([_server_row()])
    monkeypatch.setattr(mcp, "pool", _Pool(connection))

    response = await mcp.list_mcp_servers(_actor(workspace_id="ws_a"))

    assert response["count"] == 1
    assert connection.calls
    query, params = connection.calls[0]
    assert "workspace_id=%s" in query
    assert params[0] == "ws_a"
    assert response["items"][0]["workspace_id"] == "ws_a"


@pytest.mark.asyncio
async def test_get_query_cannot_cross_workspace(monkeypatch):
    connection = _Connection([])
    monkeypatch.setattr(mcp, "pool", _Pool(connection))

    with pytest.raises(HTTPException) as exc:
        await mcp.get_mcp_server("mcp_a", _actor(workspace_id="ws_b"))

    assert exc.value.status_code == 404
    assert connection.calls[0][1] == ("mcp_a", "ws_b")


@pytest.mark.asyncio
async def test_ensure_schema_uses_all_idempotent_statements():
    class SchemaConnection:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)

    connection = SchemaConnection()
    await mcp.ensure_mcp_schema(connection)
    assert connection.statements == list(mcp.ensure_mcp_schema_statements())
    assert all("IF NOT EXISTS" in statement for statement in connection.statements)
