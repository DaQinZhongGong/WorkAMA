import hashlib
import copy
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import HTTPException

from workama_platform.modules import design


def test_controlled_design_refs_reject_paths_urls_and_secrets():
    assert design.validate_controlled_ref("mock://prompt/reference-1") == "mock://prompt/reference-1"
    assert design.validate_controlled_ref("local://artifact/source_1") == "local://artifact/source_1"
    for value in ("https://evil.example/image.png", "file:///etc/passwd", "local://artifact/../escape", "local://artifact/C:\\secret"):
        with pytest.raises(ValueError):
            design.validate_controlled_ref(value)


def test_design_artifact_refs_are_safe_and_workspace_scoped_by_contract():
    assert design.validate_design_artifact_ref("design://artifact/dsgasset_ABC-123") == "design://artifact/dsgasset_ABC-123"
    for value in (
        "https://evil.example/design.png",
        "design://artifact/../escape",
        "design://artifact/dsgasset_ABC/other",
        "design://artifact/dsgasset_ABC\\secret",
    ):
        with pytest.raises(ValueError):
            design.validate_design_artifact_ref(value)


def test_design_job_requires_parent_or_source_for_edit_and_normalizes_sources():
    with pytest.raises(ValueError):
        design.DesignJobCreate(operation="edit", prompt="edit this")
    body = design.DesignJobCreate(
        operation="edit",
        prompt="  update the layout  ",
        source_refs=["mock://source/a", "mock://source/a"],
        output_format="json",
    )
    assert body.prompt == "update the layout"
    assert body.source_refs == ["mock://source/a"]


def test_provenance_hash_is_stable_and_sources_are_explicitly_classified():
    manifest = {
        "schema_version": 1,
        "generator": "workama.mock.design.v1",
        "operation": "generate",
        "prompt_sha256": "a" * 64,
        "source_refs": [],
        "parent_asset_ids": [],
        "license_status": "unknown",
        "external_provider": "pending",
    }
    assert design.manifest_hash(manifest) == design.manifest_hash(dict(manifest))
    assert manifest["external_provider"] == "pending"


@pytest.mark.parametrize(
    ("output_format", "signature"),
    [("png", b"\x89PNG\r\n\x1a\n"), ("jpeg", b"\xff\xd8\xff")],
)
def test_binary_design_exports_are_real_images(output_format, signature):
    content = design._render_design_content(output_format, "dashboard", ["mock://source/brief"], "dsgasset_ABC-123")
    assert content.startswith(signature)
    assert not content.startswith(b"{")
    assert len(content) > len(signature)
    assert design._content_type(output_format).startswith("image/")


def test_provenance_manifest_contains_verifiable_detached_claim():
    content = design._render_design_content("png", "dashboard", [], "dsgasset_ABC-123")
    digest = hashlib.sha256(content).hexdigest()
    private_key = Ed25519PrivateKey.generate()
    manifest = design._build_provenance_manifest(
        workspace_id="workspace-A",
        asset_id="dsgasset_ABC-123",
        operation="generate",
        source_refs=[],
        parent_claims=[{"asset_id": "dsgasset_PARENT", "content_sha256": "a" * 64, "provenance_hash": "b" * 64}],
        content_type="image/png",
        content_sha256=digest,
        size_bytes=len(content),
        signing_key=private_key,
    )
    assert manifest["claim_hash"] == manifest["claim"]["claim_hash"]
    assert manifest["workspace_id"] == "workspace-A"
    assert manifest["operation"] == "generate"
    assert manifest["parents"][0]["asset_id"] == "dsgasset_PARENT"
    assert manifest["generator"] == design.DESIGN_GENERATOR
    assert manifest["created_at"].endswith("Z")
    assert manifest["assertions"]
    credentials = manifest["content_credentials"]
    assert credentials["standard"] == "c2pa-compatible"
    assert credentials["standard_embedded"] is False
    assert credentials["verifier_profile"] == "workama-content-credential-v1"
    assert credentials["signature_status"] == "signed_detached"
    assert credentials["signature"]["status"] == "signed_detached"
    assert credentials["signature"]["algorithm"] == "Ed25519"
    assert credentials["signature"]["public_key_fingerprint"] == design.design_public_key_fingerprint(
        private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    )
    assert design.verify_detached_claim(
        manifest,
        private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
    )
    assert "private_key_enc" not in json.dumps(manifest)
    claim_without_hash = dict(manifest["claim"])
    claim_without_hash.pop("claim_hash")
    assert design.manifest_hash(claim_without_hash) == manifest["claim_hash"]


@pytest.mark.parametrize("field", ["workspace_id", "asset_id", "content_sha256", "claim_hash", "parents", "operation"])
def test_detached_claim_signature_is_bound_to_all_required_fields(field):
    private_key = Ed25519PrivateKey.generate()
    manifest = design._build_provenance_manifest(
        workspace_id="workspace-A",
        asset_id="dsgasset_ABC-123",
        operation="generate",
        source_refs=[],
        parent_claims=[],
        content_type="application/json",
        content_sha256="a" * 64,
        size_bytes=10,
        signing_key=private_key,
    )
    changed = copy.deepcopy(manifest)
    if field == "parents":
        changed[field] = [{"asset_id": "dsgasset_OTHER", "content_sha256": "b" * 64, "provenance_hash": "c" * 64}]
    elif field in {"content_sha256", "claim_hash"}:
        changed[field] = "f" * 64
    else:
        changed[field] = "workspace-B" if field == "workspace_id" else ("dsgasset_OTHER" if field == "asset_id" else "edit")
    public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    assert not design.verify_detached_claim(changed, public_key)


def test_download_manifest_headers_are_explicit_and_do_not_expose_key_material():
    manifest = design._build_provenance_manifest(
        workspace_id="workspace-A", asset_id="dsgasset_ABC-123", operation="generate", source_refs=[],
        parent_claims=[], content_type="image/png", content_sha256="a" * 64, size_bytes=1,
    )
    headers = design._download_manifest_headers(manifest)
    assert headers["X-WorkAMA-Content-Credential-Status"] == "signed_detached"
    assert headers["X-WorkAMA-Content-Credential-Profile"] == "workama-content-credential-v1"
    assert headers["X-WorkAMA-Content-Credential-Standard-Embedded"] == "false"
    assert "private_key" not in headers["X-WorkAMA-Content-Credential-Manifest"]


def test_design_content_integrity_check_fails_closed():
    row = {"content_bytes": b"png", "size_bytes": 3, "content_sha256": "0" * 64}
    with pytest.raises(HTTPException) as error:
        design._verified_design_content(row)
    assert error.value.status_code == 500


def test_design_routes_and_schema_include_workspace_bound_assets():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in design.router.routes}
    assert ("/api/v1/design/projects", ("GET",)) in paths
    assert ("/api/v1/design/projects", ("POST",)) in paths
    assert ("/api/v1/design/projects/{project_id}/jobs", ("POST",)) in paths
    assert ("/api/v1/design/jobs/{job_id}", ("GET",)) in paths
    assert ("/api/v1/design/artifacts", ("GET",)) in paths
    assert ("/api/v1/design/artifacts/download", ("GET",)) in paths


@pytest.mark.asyncio
async def test_schema_is_additive_and_tracks_provenance():
    statements = []

    class Connection:
        async def execute(self, statement):
            statements.append(statement)

    await design.ensure_design_schema(Connection())
    schema = "\n".join(statements)
    for table in ("ag_design_project", "ag_design_asset", "ag_design_job"):
        assert table in schema
    for field in ("artifact_ref", "content_bytes", "content_sha256", "provenance", "provenance_hash", "parent_asset_ids"):
        assert field in schema
    assert "ag_design_signing_key" in schema
    assert "private_key_enc" in schema and "public_key_fingerprint" in schema
    assert "BYTEA" in schema
    assert "'jpeg'" in schema
    assert "provenance are immutable" in schema
    assert "UNIQUE(project_id,idempotency_key)" in schema
    assert "ag_design_image_job" in schema
    assert "ag_design_canvas" in schema
    assert "ag_design_canvas_history" in schema
    assert "ag_design_export_job" in schema
    assert "kind" in schema and "past" in schema and "future" in schema


# --- image-jobs / canvas mocks -----------------------------------------

class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _SeqConnection:
    def __init__(self, results=None):
        self._results = list(results or [])
        self.calls = []

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if self._results:
            return self._results.pop(0)
        return _Result()

    async def commit(self):
        return None

    async def rollback(self):
        return None


class _Pool:
    def __init__(self, connection):
        self._connection = connection

    def connection(self):
        connection = self._connection

        class _Context:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *_args):
                return False

        return _Context()


def _actor():
    from workama_platform.core import Actor, ROLE_CAPABILITIES
    return Actor(
        user_id="usr_test",
        workspace_id="wsp_test",
        org_id="org_test",
        role="admin",
        email="admin@example.test",
        display_name="Admin",
        onboarding_completed=True,
        capabilities=ROLE_CAPABILITIES["admin"],
    )


@pytest.mark.asyncio
async def test_create_image_job_returns_placeholder_urls(monkeypatch):
    row = {
        "id": "dimg_1", "workspace_id": "wsp_test", "project_id": None,
        "prompt": "a cat", "style": "", "size": "1024x1024", "num_images": 2,
        "status": "succeeded", "result_urls": [], "model": "workama.mock.image.v1",
        "metadata": {}, "created_at": None, "updated_at": None, "completed_at": None,
    }
    conn = _SeqConnection(results=[_Result(row=row)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.create_image_job(design.ImageJobCreate(prompt="a cat", num_images=2), _actor())
    assert result["id"] == "dimg_1"
    assert result["placeholder_urls"]
    assert all(u.startswith("mock://image/") for u in result["placeholder_urls"])
    assert result["external_provider"] == "pending"


@pytest.mark.asyncio
async def test_create_image_job_rejects_archived_project(monkeypatch):
    conn = _SeqConnection(results=[_Result(row={"id": "p1", "status": "archived"})])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.create_image_job(design.ImageJobCreate(prompt="a cat", project_id="p1"), _actor())
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_list_image_jobs_is_workspace_scoped(monkeypatch):
    rows = [
        {"id": "dimg_1", "prompt": "a"},
        {"id": "dimg_2", "prompt": "b"},
    ]
    conn = _SeqConnection(results=[_Result(rows=rows), _Result(row={"total": 2})])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.list_image_jobs(_actor(), limit=10, offset=0)
    assert result["items"] == rows
    assert result["total"] == 2
    query, params = conn.calls[0]
    assert "workspace_id=%s" in query
    assert params[0] == "wsp_test"


@pytest.mark.asyncio
async def test_get_image_job_returns_row(monkeypatch):
    row = {"id": "dimg_1", "prompt": "x"}
    conn = _SeqConnection(results=[_Result(row=row)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.get_image_job("dimg_1", _actor())
    assert result["id"] == "dimg_1"


@pytest.mark.asyncio
async def test_get_image_job_returns_404_when_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.get_image_job("dimg_missing", _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_image_job_cancels_and_returns_status(monkeypatch):
    conn = _SeqConnection(results=[_Result(row={"id": "dimg_1"})])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.delete_image_job("dimg_1", _actor())
    assert result["status"] == "cancelled"
    query, _ = conn.calls[0]
    assert "status='cancelled'" in query


@pytest.mark.asyncio
async def test_delete_image_job_returns_404_when_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.delete_image_job("dimg_missing", _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_edit_image_job_creates_child_with_provenance(monkeypatch):
    parent = {"id": "dimg_1", "prompt": "cat", "style": "", "size": "512x512", "metadata": {}}
    child = {"id": "dimg_2", "workspace_id": "wsp_test", "project_id": "p1", "prompt": "cat with hat", "style": "", "size": "512x512", "num_images": 1, "status": "succeeded", "result_urls": [], "model": "", "metadata": {}, "created_at": None, "updated_at": None, "completed_at": None}
    conn = _SeqConnection(results=[_Result(row=parent), _Result(row=child)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.edit_image_job("dimg_1", design.ImageJobEdit(prompt="cat with hat"), _actor())
    assert result["id"] == "dimg_2"
    assert result["placeholder_urls"]
    assert result["external_provider"] == "pending"


@pytest.mark.asyncio
async def test_edit_image_job_returns_404_when_parent_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.edit_image_job("dimg_missing", design.ImageJobEdit(prompt="x"), _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_variate_image_job_creates_variations(monkeypatch):
    parent = {"id": "dimg_1", "prompt": "dog", "style": "cartoon", "size": "1024x1024", "metadata": {}}
    child = {"id": "dimg_3", "workspace_id": "wsp_test", "project_id": None, "prompt": "dog", "style": "cartoon", "size": "1024x1024", "num_images": 3, "status": "succeeded", "result_urls": [], "model": "", "metadata": {}, "created_at": None, "updated_at": None, "completed_at": None}
    conn = _SeqConnection(results=[_Result(row=parent), _Result(row=child)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.variate_image_job("dimg_1", design.ImageJobVariate(num_variations=3), _actor())
    assert result["num_images"] == 3
    assert len(result["placeholder_urls"]) == 3


@pytest.mark.asyncio
async def test_variate_image_job_returns_404_when_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.variate_image_job("dimg_missing", design.ImageJobVariate(), _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_sync_canvas_upserts_state(monkeypatch):
    project_row = {"id": "proj_1"}
    existing_canvas = {"id": "dcanvas_1", "state": {"layers": [0]}, "version": 1}
    canvas_row = {"id": "dcanvas_1", "project_id": "proj_1", "state": {"layers": [1]}, "version": 2, "created_at": None, "updated_at": None}
    conn = _SeqConnection(results=[
        _Result(row=project_row),
        _Result(row=existing_canvas),
        _Result(),  # delete future
        _Result(),  # insert history
        _Result(),  # delete old history
        _Result(row=canvas_row),
    ])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.sync_canvas("proj_1", design.CanvasSync(state={"layers": [1]}), _actor())
    assert result["project_id"] == "proj_1"
    assert result["version"] == 2


@pytest.mark.asyncio
async def test_sync_canvas_rejects_missing_project(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.sync_canvas("proj_missing", design.CanvasSync(state={}), _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_canvas_returns_state(monkeypatch):
    row = {"id": "dcanvas_1", "project_id": "proj_1", "state": {"x": 1}, "version": 1, "created_at": None, "updated_at": None}
    conn = _SeqConnection(results=[_Result(row=row)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.get_canvas("proj_1", _actor())
    assert result["state"] == {"x": 1}


@pytest.mark.asyncio
async def test_get_canvas_returns_404_when_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.get_canvas("proj_missing", _actor())
    assert exc.value.status_code == 404


def test_generate_image_placeholder_urls_are_deterministic():
    urls1 = design._generate_image_placeholder_urls("j1", "prompt", "style", "1024x1024", 3)
    urls2 = design._generate_image_placeholder_urls("j1", "prompt", "style", "1024x1024", 3)
    assert urls1 == urls2
    assert len(urls1) == 3
    assert all(u.startswith("mock://image/") for u in urls1)


def test_design_router_exposes_image_job_and_canvas_contracts():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in design.router.routes}
    assert ("/api/v1/design/image-jobs", ("GET",)) in paths
    assert ("/api/v1/design/image-jobs", ("POST",)) in paths
    assert ("/api/v1/design/image-jobs/{job_id}", ("DELETE",)) in paths
    assert ("/api/v1/design/image-jobs/{job_id}", ("GET",)) in paths
    assert ("/api/v1/design/image-jobs/{job_id}/edit", ("POST",)) in paths
    assert ("/api/v1/design/image-jobs/{job_id}/variate", ("POST",)) in paths
    assert ("/api/v1/design/projects/{project_id}/canvas", ("GET",)) in paths
    assert ("/api/v1/design/projects/{project_id}/canvas/sync", ("POST",)) in paths
    assert ("/api/v1/design/projects/{project_id}/canvas/layers", ("POST",)) in paths
    assert ("/api/v1/design/projects/{project_id}/canvas/layers/reorder", ("POST",)) in paths
    assert ("/api/v1/design/projects/{project_id}/canvas/layers/{layer_id}", ("DELETE",)) in paths
    assert ("/api/v1/design/projects/{project_id}/canvas/layers/{layer_id}", ("PATCH",)) in paths
    assert ("/api/v1/design/projects/{project_id}/canvas/align", ("POST",)) in paths
    assert ("/api/v1/design/projects/{project_id}/export", ("POST",)) in paths
    assert ("/api/v1/design/projects/{project_id}/exports/{job_id}", ("GET",)) in paths
    assert ("/api/v1/design/projects/{project_id}/canvas/undo", ("POST",)) in paths
    assert ("/api/v1/design/projects/{project_id}/canvas/redo", ("POST",)) in paths
