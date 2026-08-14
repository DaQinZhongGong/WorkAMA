from __future__ import annotations

import pytest
from fastapi import HTTPException

from workama_platform.modules import code


class EmptyResult:
    async def fetchone(self):
        return None


class RecordingConnection:
    def __init__(self):
        self.calls = []

    async def execute(self, query, params):
        self.calls.append((query, params))
        return EmptyResult()


def actor(workspace_id: str):
    from workama_platform.core import Actor

    return Actor(
        user_id="usr_test",
        workspace_id=workspace_id,
        org_id="org_test",
        role="owner",
        email="owner@example.test",
        display_name="Owner",
        onboarding_completed=True,
    )


def test_code_status_machine_accepts_progression_and_rejects_terminal_reentry():
    code.validate_task_transition("queued", "running")
    code.validate_task_transition("running", "paused")
    code.validate_task_transition("paused", "running")
    code.validate_task_transition("running", "succeeded")

    with pytest.raises(HTTPException) as exc:
        code.validate_task_transition("succeeded", "running")
    assert exc.value.status_code == 409


def test_code_capability_is_required_for_writes():
    from workama_platform.core import Actor

    viewer = Actor(
        user_id="usr_viewer", workspace_id="wsp_a", org_id="org_a", role="viewer",
        email="viewer@example.test", display_name="Viewer", onboarding_completed=True,
        capabilities=("code:read",),
    )
    code._require(viewer, "read")
    with pytest.raises(HTTPException) as exc:
        code._require(viewer, "write")
    assert exc.value.status_code == 403


def test_event_payload_redacts_credentials_recursively():
    payload = {
        "diff": {"files": ["README.md"]},
        "authorization": "Bearer secret",
        "nested": [{"api_key": "sk-secret", "message": "ok"}],
    }

    redacted = code.redact_sensitive(payload)

    assert redacted["authorization"] == "<redacted>"
    assert redacted["nested"][0]["api_key"] == "<redacted>"
    assert redacted["nested"][0]["message"] == "ok"
    assert payload["authorization"] == "Bearer secret"


def test_public_views_never_include_repository_credential_fields():
    repository = code.repository_view(
        {
            "id": "repo_1",
            "name": "demo",
            "provider": "local",
            "remote_url": "https://example.invalid/demo.git",
            "default_branch": "main",
            "credential_enc": "encrypted-secret",
            "created_by": "usr_1",
        }
    )
    event = code.event_view(
        {
            "id": "cevt_1",
            "task_id": "ctask_1",
            "workspace_id": "wsp_1",
            "seq": 1,
            "type": "terminal.output",
            "payload": {"token": "should-not-leak"},
            "created_by": "usr_1",
        }
    )

    assert "credential_enc" not in repository
    assert "credential" not in repository
    assert event["payload"]["token"] == "<redacted>"


@pytest.mark.asyncio
async def test_repository_lookup_is_tenant_scoped():
    connection = RecordingConnection()

    with pytest.raises(HTTPException) as exc:
        await code._owned_repository(connection, "repo_other", actor("wsp_current"))

    assert exc.value.status_code == 404
    assert connection.calls[0][1] == ("repo_other", "wsp_current")
    assert "workspace_id = %s" in connection.calls[0][0]


def test_schema_has_tenant_keys_and_canonical_event_types():
    schema = "\n".join(code.SCHEMA_STATEMENTS)

    assert "workspace_id" in schema
    assert "code_repository" in schema
    assert "code_task" in schema
    assert "code_event" in schema
    assert "code.diff" in schema
    assert "terminal.output" in schema
    assert "test.report" in schema
    assert "credential_enc" in schema


def test_event_request_accepts_contract_type_alias():
    assert code.CodeEventCreate(type="diff", payload={}).event_type == "diff"


def test_router_exposes_repository_task_status_and_event_contracts():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in code.router.routes}

    assert ("/api/v1/code/repositories", ("GET",)) in paths
    assert ("/api/v1/code/repositories", ("POST",)) in paths
    assert ("/api/v1/code/tasks", ("GET",)) in paths
    assert ("/api/v1/code/tasks", ("POST",)) in paths
    assert ("/api/v1/code/tasks/{task_id}/status", ("POST",)) in paths
    assert ("/api/v1/code/tasks/{task_id}/events", ("GET",)) in paths
    assert ("/api/v1/code/tasks/{task_id}/events", ("POST",)) in paths
    assert ("/api/v1/code/tasks/{task_id}/events/{event_type}", ("POST",)) in paths
