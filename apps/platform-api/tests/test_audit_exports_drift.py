from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from workama_platform.core import Actor
from workama_platform.modules import audit_exports

try:  # pragma: no cover
    from datetime import UTC
except ImportError:  # Python < 3.11 compatibility for the current runtime
    UTC = timezone.utc


def owner(workspace_id: str = "wsp_current") -> Actor:
    return Actor(
        user_id="usr_owner",
        workspace_id=workspace_id,
        org_id="org_test",
        role="owner",
        email="owner@example.test",
        display_name="Owner",
        onboarding_completed=True,
        capabilities=("*",),
    )


def test_audit_export_router_exposes_p1_async_endpoints():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in audit_exports.audit_export_router.routes}
    assert ("/api/v1/audit-exports", ("POST",)) in paths
    assert ("/api/v1/audit-exports/{export_id}", ("GET",)) in paths


def test_audit_export_request_schema_is_bounded():
    request = audit_exports.AuditExportRequest(format="jsonl", limit=100, action="role.updated")
    assert request.format == "jsonl"
    assert request.limit == 100
    with pytest.raises(ValueError):
        audit_exports.AuditExportRequest(limit=501)
    with pytest.raises(ValueError):
        audit_exports.AuditExportRequest(format="xml")


class Result:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = rows or []

    async def fetchone(self):
        return self.row

    async def fetchall(self):
        return self.rows


class Transaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, tb):
        return False


class Connection:
    def __init__(self, results=None):
        self.results = results or {}
        self.statements = []

    def transaction(self):
        return Transaction(self)

    async def execute(self, statement, params=None):
        self.statements.append((statement, params))
        key = None
        for candidate in ("sec_audit_export", "sec_audit_chain"):
            if candidate in statement:
                key = candidate
        return Result(self.results.get(key))

    async def commit(self):
        return

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_create_audit_export_operation_requires_export_capability():
    viewer = Actor(
        user_id="usr_viewer",
        workspace_id="wsp_current",
        org_id="org_test",
        role="viewer",
        email="viewer@example.test",
        display_name="Viewer",
        onboarding_completed=True,
    )
    with pytest.raises(HTTPException) as error:
        await audit_exports.create_audit_export_operation(viewer, audit_exports.AuditExportRequest())
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_create_audit_export_operation_queues_export(monkeypatch):
    actor = owner()
    now = datetime.now(UTC)
    row = {
        "id": "aexp_1",
        "status": "queued",
        "format": "jsonl",
        "record_count": 0,
        "content_hash": "",
        "manifest": {"provider_execution": "pending_external"},
        "created_at": now,
        "expires_at": now,
    }
    conn = Connection({"sec_audit_export": row})
    monkeypatch.setattr(audit_exports.pool, "connection", lambda: conn)
    body = audit_exports.AuditExportRequest(format="jsonl", action="role.updated")
    result = await audit_exports.create_audit_export_operation(actor, body)
    assert result["operation_id"].startswith("aexp_")
    assert result["id"] == "aexp_1"
    # Contract《720》status 字段对齐 OperationAccepted（queued/running）
    assert result["status"] == "queued"
    assert result["execution_mode"] == "controlled_mock"
    assert any("INSERT INTO sec_audit_export" in statement for statement, _ in conn.statements)


@pytest.mark.asyncio
async def test_get_audit_export_operation_is_workspace_scoped(monkeypatch):
    actor = owner()
    now = datetime.now(UTC)
    row = {
        "id": "aexp_1",
        "status": "queued",
        "format": "jsonl",
        "record_count": 0,
        "content_hash": "",
        "manifest": {},
        "created_at": now,
        "expires_at": now,
    }
    conn = Connection({"sec_audit_export": row})
    monkeypatch.setattr(audit_exports.pool, "connection", lambda: conn)
    result = await audit_exports.get_audit_export_operation("aexp_1", actor)
    assert result["id"] == "aexp_1"
    assert conn.statements[-1][1] == ("aexp_1", actor.workspace_id)

    conn_missing = Connection({"sec_audit_export": None})
    monkeypatch.setattr(audit_exports.pool, "connection", lambda: conn_missing)
    with pytest.raises(HTTPException) as error:
        await audit_exports.get_audit_export_operation("aexp_missing", actor)
    assert error.value.status_code == 404


def test_audit_export_capability_alias_allows_owner_and_admin():
    # owner capabilities include '*' so any audit capability is allowed.
    assert audit_exports._require(owner(), "audit:export") is None
    admin = Actor(
        user_id="usr_admin",
        workspace_id="wsp_current",
        org_id="org_test",
        role="admin",
        email="admin@example.test",
        display_name="Admin",
        onboarding_completed=True,
        capabilities=("audit:*",),
    )
    assert audit_exports._require(admin, "audit:export") is None
