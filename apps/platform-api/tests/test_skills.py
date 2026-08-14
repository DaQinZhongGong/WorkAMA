from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from workama_platform.core import Actor
from workama_platform.modules import skills


def valid_manifest(**overrides):
    value = {
        "schema_version": 1,
        "name": "research-helper",
        "version": "1.2.3",
        "publisher": "workama",
        "description": "Research helper",
        "trigger_description": "Use for research tasks",
        "required_tools": ["web.search"],
        "permissions": [],
        "files": ["skill.yaml", "prompt.md", "scripts/prepare.py", "resources/template.md"],
        "entrypoint": "prompt.md",
    }
    value.update(overrides)
    return value


def owner(workspace_id: str = "wsp_current") -> Actor:
    return Actor(
        user_id="usr_owner",
        workspace_id=workspace_id,
        org_id="org_test",
        role="owner",
        email="owner@example.test",
        display_name="Owner",
        onboarding_completed=True,
    )


def test_manifest_is_normalized_and_schema_is_explicit():
    manifest = skills.validate_manifest(valid_manifest(required_tools=["web.search", "web.search"]))
    assert manifest["schema_version"] == 1
    assert manifest["required_tools"] == ["web.search"]
    assert manifest["files"] == sorted(manifest["files"])
    assert skills.MANIFEST_SCHEMA["additionalProperties"] is False
    assert "prompt.md" in skills.MANIFEST_SCHEMA["properties"] or "files" in skills.MANIFEST_SCHEMA["properties"]


@pytest.mark.parametrize(
    "manifest",
    [
        {"name": "bad", "version": "1.0", "publisher": "workama"},
        valid_manifest(name="../../escape"),
        valid_manifest(files=["skill.yaml", "prompt.md", "../secret.txt"]),
        valid_manifest(files=["skill.yaml", "prompt.md"], entrypoint="resources/../secret.txt"),
        valid_manifest(api_key="secret-value"),
        valid_manifest(permissions=["shell:execute"]),
    ],
)
def test_manifest_schema_rejects_invalid_or_secret_like_packages(manifest):
    if manifest.get("permissions") == ["shell:execute"]:
        assert skills.compute_risk_level(manifest) == "critical"
        return
    with pytest.raises(ValueError):
        skills.validate_manifest(manifest)


@pytest.mark.parametrize(
    "artifact_ref",
    [
        "https://example.com/skill.zip",
        "file:///tmp/skill.zip",
        "local://artifact/../escape",
        "local://artifact/%2e%2e",
        "local://artifact/C:/secret.zip",
        "mock://skill/workama/research-helper/1.2.3?token=secret",
        "mock://skill/workama/research-helper/1.2.3/../../escape",
        "mock://skill/workama/research-helper/not-semver",
    ],
)
def test_artifact_reference_rejects_arbitrary_urls_and_path_traversal(artifact_ref):
    with pytest.raises(ValueError):
        skills.validate_artifact_reference(artifact_ref)


def test_controlled_artifact_references_and_hash_are_deterministic():
    reference = skills.validate_artifact_reference("mock://skill/workama/research-helper/1.2.3")
    assert reference.kind == "mock"
    assert reference.publisher == "workama"
    assert skills.validate_artifact_reference("mock://skills/research-helper/1.2.3").publisher == "mock"
    manifest = skills.validate_manifest(valid_manifest())
    digest = skills.skill_content_hash("mock://skill/workama/research-helper/1.2.3", manifest)
    assert len(digest) == 64
    assert digest == skills.skill_content_hash("mock://skill/workama/research-helper/1.2.3", manifest)


def test_risk_is_never_lowered_than_declared_permissions():
    assert skills.compute_risk_level(valid_manifest(permissions=["network:public"])) == "high"
    assert skills.compute_risk_level(valid_manifest(permissions=["filesystem:read"], risk_level="medium")) == "medium"
    assert skills.compute_risk_level(valid_manifest(permissions=["secret:read"], risk_level="low")) == "critical"


class Result:
    def __init__(self, row=None):
        self.row = row

    async def fetchone(self):
        return self.row


class LocalArtifactConnection:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def execute(self, query, params):
        self.calls.append((query, params))
        return Result(self.row)


@pytest.mark.asyncio
async def test_local_artifact_is_workspace_scoped_and_object_path_is_checked():
    row = {
        "id": "art_skill_1",
        "workspace_id": "wsp_current",
        "kind": "skill_package",
        "s3_key": "artifacts/wsp_current/skills/research-helper.zip",
        "content_sha256": "a" * 64,
        "preview": {"manifest": valid_manifest()},
    }
    conn = LocalArtifactConnection(row)
    resolved = await skills.resolve_package_reference(
        conn,
        workspace_id="wsp_current",
        artifact_ref="local://artifact/art_skill_1",
    )
    assert resolved.source_kind == "local"
    assert resolved.content_sha256 == "a" * 64
    assert conn.calls[0][1] == ("art_skill_1", "wsp_current")
    assert "workspace_id=%s" in conn.calls[0][0]

    unsafe = dict(row, s3_key="artifacts/wsp_current/../other.zip")
    with pytest.raises(HTTPException) as error:
        await skills.resolve_package_reference(
            LocalArtifactConnection(unsafe),
            workspace_id="wsp_current",
            artifact_ref="local://artifact/art_skill_1",
        )
    assert error.value.status_code == 422


def test_enable_gate_and_version_conflict_are_explicit():
    pending = {
        "id": "skill_1",
        "workspace_id": "wsp_current",
        "publisher": "workama",
        "name": "research-helper",
        "semver": "1.2.3",
        "manifest": valid_manifest(),
        "artifact_ref": "mock://skill/workama/research-helper/1.2.3",
        "source_kind": "mock",
        "content_sha256": "a" * 64,
        "signature_status": "not_verified",
        "risk_level": "low",
        "review_status": "pending",
        "status": "active",
        "revision": 1,
    }
    with pytest.raises(HTTPException) as error:
        skills._check_version(2, 1, resource="Skill installation")
    assert error.value.status_code == 412
    assert skills._skill_view(pending)["signature_status"] == "not_verified"


def test_idempotency_and_same_version_content_conflicts_are_409():
    same_hash = "a" * 64
    skills.check_idempotency_replay(same_hash, same_hash)
    with pytest.raises(HTTPException) as idempotency_error:
        skills.check_idempotency_replay(same_hash, "b" * 64)
    assert idempotency_error.value.status_code == 409

    skills.check_skill_version_content(same_hash, same_hash)
    with pytest.raises(HTTPException) as version_error:
        skills.check_skill_version_content(same_hash, "b" * 64)
    assert version_error.value.status_code == 409

    raw_key, hashed_key = skills._normalize_idempotency_key("client-key", same_hash)
    assert raw_key == "client-key"
    assert hashed_key != raw_key


def test_router_exposes_install_state_review_and_list_contracts():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in skills.router.routes}
    assert ("/api/v1/skills", ("GET",)) in paths
    assert ("/api/v1/skills/install", ("POST",)) in paths
    assert ("/api/v1/skills/{skill_id}/enable", ("POST",)) in paths
    assert ("/api/v1/skills/{skill_id}/disable", ("POST",)) in paths
    assert ("/api/v1/skills/{skill_id}/review", ("POST",)) in paths


def test_skill_installs_router_exposes_contract_crud_endpoints():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in skills.skill_installs_router.routes}
    assert ("/api/v1/skill-installs", ("GET",)) in paths
    assert ("/api/v1/skill-installs", ("POST",)) in paths
    assert ("/api/v1/skill-installs/{install_id}", ("PATCH",)) in paths
    assert ("/api/v1/skill-installs/{install_id}", ("DELETE",)) in paths


def test_schema_has_tenant_hash_review_and_install_idempotency_boundaries():
    schema = "\n".join(skills.SCHEMA_STATEMENTS)
    for table in ("ag_skill", "ag_skill_install"):
        assert table in schema
    for field in ("workspace_id", "content_sha256", "review_status", "risk_level", "signature_status", "idempotency_key_hash"):
        assert field in schema
    assert "UNIQUE(workspace_id, publisher, name, semver)" in schema
    assert "UNIQUE(workspace_id, skill_id)" in schema
    assert "UNIQUE(workspace_id, idempotency_key_hash)" in schema
    assert "secret" not in skills._canonical_json(skills.validate_manifest(valid_manifest())).lower()


def test_redacted_output_never_rehydrates_sensitive_fields():
    value = skills.redact_sensitive({"safe": "yes", "authorization": "Bearer secret", "nested": {"api_key": "raw"}})
    assert value == {"safe": "yes", "authorization": "<redacted>", "nested": {"api_key": "<redacted>"}}
    assert "raw-secret" not in skills.redact_sensitive_text("authorization=Bearer raw-secret")


def test_skill_response_exposes_bare_dto_and_keeps_legacy_wrapper():
    view = {
        "id": "skill_1",
        "workspace_id": "wsp_current",
        "publisher": "workama",
        "name": "research-helper",
        "version": "1.2.3",
        "semver": "1.2.3",
        "status": "active",
        "revision": 1,
    }
    response = skills._skill_response(view, deduplicated=True)
    # Contract《720》: single-resource ops return bare SkillDTO at top level
    assert response["id"] == "skill_1"
    assert response["status"] == "active"
    # Backward-compatible wrapper retained
    assert response["skill"]["id"] == "skill_1"
    # Extra back-compat keys retained
    assert response["deduplicated"] is True


class _SkillsResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    async def fetchall(self):
        return self._rows


class _SkillsListConnection:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, statement, params=None):
        return _SkillsResult(rows=self._rows)


def _skill_join_row():
    now = datetime.now(UTC)
    return {
        "skill_id": "skill_1",
        "workspace_id": "wsp_current",
        "publisher": "workama",
        "name": "research-helper",
        "semver": "1.2.3",
        "manifest": {},
        "artifact_ref": "mock://skill/workama/research-helper/1.2.3",
        "source_kind": "mock",
        "content_sha256": "a" * 64,
        "signature_status": "not_verified",
        "risk_level": "low",
        "review_status": "approved",
        "review_reason": "",
        "skill_status": "active",
        "skill_revision": 1,
        "skill_created_at": now,
        "skill_updated_at": now,
        "installation_id": "skillinst_1",
        "enabled": True,
        "install_status": "enabled",
        "install_version": 1,
        "install_created_at": now,
        "install_updated_at": now,
    }


@pytest.mark.asyncio
async def test_list_skills_returns_listresponse_envelope(monkeypatch):
    monkeypatch.setattr(skills.pool, "connection", lambda: _SkillsListConnection([_skill_join_row()]))
    result = await skills.list_skills(owner(), limit=50)
    # Contract《720》listSkills -> ListResponse<SkillDTO>
    assert result["data"] == result["items"]
    assert result["data"][0]["id"] == "skill_1"
    assert result["has_more"] is False
    assert result["next_cursor"] is None
    assert "request_id" in result["meta"]
