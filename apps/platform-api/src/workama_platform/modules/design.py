from __future__ import annotations

import hashlib
from io import BytesIO
import json
import re
import base64
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator, model_validator

from workama_platform.core import Actor, capability_allows, decrypt_secret, encrypt_secret, get_actor, json_dumps, new_id, pool


router = APIRouter(prefix="/api/v1/design", tags=["ama-design"])

_CONTROLLED_REF = re.compile(r"^(?:mock|local)://[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}$")
_DESIGN_ARTIFACT_REF = re.compile(r"^design://artifact/[A-Za-z0-9_:-]{3,160}$")
_PROJECT_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
MAX_PROMPT = 16_000
MAX_SOURCES = 32
MAX_CANVAS = 8192
DESIGN_GENERATOR = "workama.mock.design.v2"
CONTENT_CREDENTIAL_PROFILE = "workama-content-credential-v1"
CONTENT_CREDENTIAL_ALGORITHM = "Ed25519"
CONTENT_CREDENTIAL_STATUS = "signed_detached"


def _require(actor: Actor, capability: str) -> None:
    if not capability_allows(actor.capabilities, capability):
        raise HTTPException(status_code=403, detail=f"Missing capability: {capability}")


def validate_controlled_ref(value: str) -> str:
    value = value.strip()
    if not _CONTROLLED_REF.fullmatch(value) or ".." in value or "\\" in value:
        raise ValueError("source_ref must be a controlled mock:// or local:// reference")
    return value


def validate_design_artifact_ref(value: str) -> str:
    value = value.strip()
    if not _DESIGN_ARTIFACT_REF.fullmatch(value) or ".." in value or "\\" in value:
        raise ValueError("artifact_ref must be a controlled design://artifact reference")
    return value


def manifest_hash(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


class ProjectCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    slug: str | None = Field(default=None, max_length=64)
    description: str = Field(default="", max_length=2_000)
    canvas_width: int = Field(default=1440, ge=1, le=MAX_CANVAS)
    canvas_height: int = Field(default=900, ge=1, le=MAX_CANVAS)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("name is required")
        return value

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if not _PROJECT_SLUG.fullmatch(value):
            raise ValueError("slug is invalid")
        return value


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=2_000)
    canvas_width: int | None = Field(default=None, ge=1, le=MAX_CANVAS)
    canvas_height: int | None = Field(default=None, ge=1, le=MAX_CANVAS)
    status: Literal["active", "archived"] | None = None


class DesignJobCreate(BaseModel):
    operation: Literal["generate", "edit", "prototype"] = "generate"
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT)
    source_refs: list[str] = Field(default_factory=list, max_length=MAX_SOURCES)
    parent_asset_ids: list[str] = Field(default_factory=list, max_length=16)
    output_format: Literal["svg", "png", "jpeg", "json"] = "json"
    idempotency_key: str | None = Field(default=None, max_length=160)

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value: str) -> str:
        value = value.strip()
        if "\x00" in value:
            raise ValueError("prompt contains an invalid control character")
        return value

    @field_validator("source_refs")
    @classmethod
    def validate_sources(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            value = validate_controlled_ref(value)
            if value not in normalized:
                normalized.append(value)
        return normalized

    @field_validator("parent_asset_ids")
    @classmethod
    def validate_parents(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            value = value.strip()
            if not value or not re.fullmatch(r"^[A-Za-z0-9_:-]{3,160}$", value):
                raise ValueError("parent_asset_ids contains an invalid id")
            if value not in normalized:
                normalized.append(value)
        return normalized

    @model_validator(mode="after")
    def validate_operation(self) -> "DesignJobCreate":
        if self.operation == "edit" and not self.source_refs and not self.parent_asset_ids:
            raise ValueError("edit requires a source_ref or parent asset")
        return self


class ImageJobCreate(BaseModel):
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT)
    style: str = Field(default="", max_length=120)
    size: str = Field(default="1024x1024")
    num_images: int = Field(default=1, ge=1, le=8)
    project_id: str | None = Field(default=None, max_length=160)

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value: str) -> str:
        value = value.strip()
        if "\x00" in value:
            raise ValueError("prompt contains an invalid control character")
        return value


class ImageJobEdit(BaseModel):
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT)
    style: str = Field(default="", max_length=120)


class ImageJobVariate(BaseModel):
    num_variations: int = Field(default=2, ge=1, le=4)


class CanvasSync(BaseModel):
    state: dict[str, Any] = Field(default_factory=dict)


class LayerCreate(BaseModel):
    layer_id: str = Field(min_length=1, max_length=160)
    type: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    transform: dict[str, Any] = Field(default_factory=dict)
    properties: dict[str, Any] = Field(default_factory=dict)


class LayerPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    transform: dict[str, Any] | None = None
    properties: dict[str, Any] | None = None


class LayerReorder(BaseModel):
    layer_ids: list[str] = Field(min_length=1)


class AlignRequest(BaseModel):
    layer_ids: list[str] = Field(min_length=1)
    alignment: Literal["left", "right", "center", "top", "bottom", "middle"]


class ExportRequest(BaseModel):
    format: Literal["svg", "png", "jpeg", "pdf"]
    include_layers: bool = True


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS ag_design_project (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      name TEXT NOT NULL,
      slug TEXT NOT NULL,
      description TEXT NOT NULL DEFAULT '',
      canvas_width INTEGER NOT NULL DEFAULT 1440 CHECK (canvas_width > 0 AND canvas_width <= 8192),
      canvas_height INTEGER NOT NULL DEFAULT 900 CHECK (canvas_height > 0 AND canvas_height <= 8192),
      status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
      created_by TEXT NOT NULL REFERENCES id_user(id),
      version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id, slug)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ag_design_project_workspace ON ag_design_project(workspace_id,status,updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS ag_design_asset (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES ag_design_project(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      name TEXT NOT NULL,
      kind TEXT NOT NULL CHECK (kind IN ('image','prototype','canvas')),
      content_type TEXT NOT NULL,
      artifact_ref TEXT NOT NULL,
      content_sha256 TEXT NOT NULL,
      size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
      provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
      provenance_hash TEXT NOT NULL,
      parent_asset_ids TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
      content_bytes BYTEA,
      status TEXT NOT NULL DEFAULT 'ready' CHECK (status IN ('ready','deleted')),
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ag_design_asset_project ON ag_design_asset(project_id,created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS ag_design_signing_key (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      algorithm TEXT NOT NULL CHECK (algorithm IN ('Ed25519')),
      private_key_enc TEXT NOT NULL,
      public_key_fingerprint TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ag_design_signing_key_workspace ON ag_design_signing_key(workspace_id,status)",
    """
    CREATE TABLE IF NOT EXISTS ag_design_job (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES ag_design_project(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      operation TEXT NOT NULL CHECK (operation IN ('generate','edit','prototype')),
      prompt_hash TEXT NOT NULL,
      source_refs TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
      parent_asset_ids TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
      output_format TEXT NOT NULL CHECK (output_format IN ('svg','png','jpeg','json')),
      status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
      asset_id TEXT REFERENCES ag_design_asset(id) ON DELETE SET NULL,
      error_code TEXT,
      error_message TEXT,
      idempotency_key TEXT,
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      completed_at TIMESTAMPTZ,
      UNIQUE(project_id,idempotency_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ag_design_job_workspace_time ON ag_design_job(workspace_id,created_at DESC)",
    "ALTER TABLE ag_design_asset ADD COLUMN IF NOT EXISTS content_bytes BYTEA",
    "ALTER TABLE ag_design_job DROP CONSTRAINT IF EXISTS ag_design_job_output_format_check",
    "ALTER TABLE ag_design_job ADD CONSTRAINT ag_design_job_output_format_check CHECK (output_format IN ('svg','png','jpeg','json'))",
    """
    CREATE OR REPLACE FUNCTION ag_design_asset_provenance_immutable()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.provenance IS DISTINCT FROM OLD.provenance
         OR NEW.provenance_hash IS DISTINCT FROM OLD.provenance_hash
         OR NEW.parent_asset_ids IS DISTINCT FROM OLD.parent_asset_ids
         OR NEW.content_bytes IS DISTINCT FROM OLD.content_bytes
         OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256
         OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
         OR NEW.content_type IS DISTINCT FROM OLD.content_type
         OR NEW.artifact_ref IS DISTINCT FROM OLD.artifact_ref THEN
        RAISE EXCEPTION 'design asset content and provenance are immutable';
      END IF;
      RETURN NEW;
    END;
    $$
    """,
    "DROP TRIGGER IF EXISTS ag_design_asset_provenance_immutable_trigger ON ag_design_asset",
    """
    CREATE TRIGGER ag_design_asset_provenance_immutable_trigger
    BEFORE UPDATE ON ag_design_asset
    FOR EACH ROW EXECUTE FUNCTION ag_design_asset_provenance_immutable()
    """,
    """
    CREATE TABLE IF NOT EXISTS ag_design_image_job (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      project_id TEXT REFERENCES ag_design_project(id) ON DELETE CASCADE,
      prompt TEXT NOT NULL,
      style TEXT NOT NULL DEFAULT '',
      size TEXT NOT NULL DEFAULT '1024x1024',
      num_images INTEGER NOT NULL DEFAULT 1 CHECK (num_images > 0 AND num_images <= 8),
      status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
      result_urls TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
      model TEXT NOT NULL DEFAULT 'workama.mock.image.v1',
      metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      completed_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ag_design_image_job_workspace ON ag_design_image_job(workspace_id,status,created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS ag_design_canvas (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES ag_design_project(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      state JSONB NOT NULL DEFAULT '{}'::jsonb,
      version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(project_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ag_design_canvas_workspace ON ag_design_canvas(workspace_id,updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS ag_design_canvas_history (
      id TEXT PRIMARY KEY,
      canvas_id TEXT NOT NULL REFERENCES ag_design_canvas(id) ON DELETE CASCADE,
      project_id TEXT NOT NULL REFERENCES ag_design_project(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      state JSONB NOT NULL DEFAULT '{}'::jsonb,
      action TEXT NOT NULL DEFAULT '',
      kind TEXT NOT NULL DEFAULT 'past' CHECK (kind IN ('past','future')),
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "ALTER TABLE ag_design_canvas_history ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'past'",
    "CREATE INDEX IF NOT EXISTS idx_ag_design_canvas_history_canvas ON ag_design_canvas_history(canvas_id,kind,created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS ag_design_export_job (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES ag_design_project(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      format TEXT NOT NULL CHECK (format IN ('svg','png','jpeg','pdf')),
      include_layers BOOLEAN NOT NULL DEFAULT true,
      status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed')),
      result_bytes BYTEA,
      result_sha256 TEXT,
      error_message TEXT,
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      completed_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ag_design_export_job_workspace ON ag_design_export_job(workspace_id,status,created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ag_design_export_job_project ON ag_design_export_job(project_id,created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS ag_design_provenance_manifest (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      project_id TEXT NOT NULL REFERENCES ag_design_project(id) ON DELETE CASCADE,
      asset_id TEXT NOT NULL REFERENCES ag_design_asset(id) ON DELETE CASCADE,
      manifest_version TEXT NOT NULL DEFAULT '1.0',
      generator JSONB NOT NULL DEFAULT '{}'::jsonb,
      prompt_hash TEXT NOT NULL DEFAULT '',
      source_assets JSONB NOT NULL DEFAULT '[]'::jsonb,
      claim_hash TEXT NOT NULL,
      parent_claim_hash TEXT,
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
      UNIQUE(workspace_id, asset_id, manifest_version)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ag_design_provenance_workspace ON ag_design_provenance_manifest(workspace_id,created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ag_design_provenance_asset ON ag_design_provenance_manifest(asset_id,created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ag_design_provenance_claim_hash ON ag_design_provenance_manifest(claim_hash)",
    "CREATE INDEX IF NOT EXISTS idx_ag_design_provenance_parent_claim ON ag_design_provenance_manifest(parent_claim_hash)",
)


async def ensure_design_schema(conn) -> None:
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


def _project_public(row: dict[str, Any]) -> dict[str, Any]:
    return dict(row)


def _asset_public(row: dict[str, Any]) -> dict[str, Any]:
    public = dict(row)
    public.pop("content_bytes", None)
    credentials = (public.get("provenance") or {}).get("content_credentials") or {}
    if credentials:
        public.update(
            {
                "signature_status": credentials.get("signature_status"),
                "verifier_profile": credentials.get("verifier_profile"),
                "standard_embedded": credentials.get("standard_embedded"),
            }
        )
    return public


def _content_type(output_format: str) -> str:
    return {
        "json": "application/json",
        "svg": "image/svg+xml",
        "png": "image/png",
        "jpeg": "image/jpeg",
    }[output_format]


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> bytes:
    try:
        normalized = value.strip().replace("-", "+").replace("_", "/")
        return base64.b64decode(normalized + "=" * (-len(normalized) % 4), validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("value is not valid base64url") from exc


def design_public_key_fingerprint(public_key: bytes) -> str:
    raw = bytes(public_key)
    if len(raw) != 32:
        raise ValueError("Ed25519 public key must be exactly 32 bytes")
    return hashlib.sha256(raw).hexdigest()


def content_credential_signature_payload(
    *, workspace_id: str, asset_id: str, content_sha256: str, claim_hash: str,
    parents: list[dict[str, str]], operation: str,
) -> bytes:
    signed_fields = {
        "version": 1,
        "workspace_id": workspace_id,
        "asset_id": asset_id,
        "content_sha256": content_sha256,
        "claim_hash": claim_hash,
        "parents": parents,
        "operation": operation,
    }
    return json.dumps(signed_fields, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _manifest_signature_fields(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "workspace_id": manifest["workspace_id"],
        "asset_id": manifest["asset_id"],
        "content_sha256": manifest["content_sha256"],
        "claim_hash": manifest["claim_hash"],
        "parents": manifest["parents"],
        "operation": manifest["operation"],
    }


def _sign_detached_claim(manifest: dict[str, Any], private_key: Ed25519PrivateKey) -> dict[str, Any]:
    public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    payload = content_credential_signature_payload(**_manifest_signature_fields(manifest))
    signature = private_key.sign(payload)
    signed_manifest = dict(manifest)
    signed_manifest["content_credentials"] = {
        "standard": "c2pa-compatible",
        "standard_embedded": False,
        "verifier_profile": CONTENT_CREDENTIAL_PROFILE,
        "signature_status": CONTENT_CREDENTIAL_STATUS,
        "signature": {
            "status": CONTENT_CREDENTIAL_STATUS,
            "algorithm": CONTENT_CREDENTIAL_ALGORITHM,
            "value": _base64url_encode(signature),
            "public_key_fingerprint": design_public_key_fingerprint(public_key),
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "detached": True,
        },
        "embedding_note": "C2PA JUMBF media embedding is unavailable; this is a WorkAMA detached claim.",
    }
    return signed_manifest


def verify_detached_claim(manifest: dict[str, Any], public_key: bytes) -> bool:
    credentials = manifest.get("content_credentials") or {}
    signature = credentials.get("signature") or {}
    try:
        if credentials.get("standard") != "c2pa-compatible" or credentials.get("standard_embedded") is not False:
            return False
        if credentials.get("verifier_profile") != CONTENT_CREDENTIAL_PROFILE or credentials.get("signature_status") != CONTENT_CREDENTIAL_STATUS:
            return False
        if signature.get("status") != CONTENT_CREDENTIAL_STATUS or signature.get("algorithm") != CONTENT_CREDENTIAL_ALGORITHM:
            return False
        raw_public_key = bytes(public_key)
        if design_public_key_fingerprint(raw_public_key) != signature.get("public_key_fingerprint"):
            return False
        payload = content_credential_signature_payload(**_manifest_signature_fields(manifest))
        if hashlib.sha256(payload).hexdigest() != signature.get("payload_sha256"):
            return False
        Ed25519PublicKey.from_public_bytes(raw_public_key).verify(_base64url_decode(signature["value"]), payload)
        return True
    except (InvalidSignature, KeyError, TypeError, ValueError):
        return False


def _render_design_content(output_format: str, prompt: str, source_refs: list[str], asset_id: str) -> bytes:
    if output_format == "json":
        return json_dumps({"type": "workama.design.mock", "prompt": prompt, "sources": source_refs, "format": output_format}).encode()
    if output_format == "svg":
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200" viewBox="0 0 320 200">'
            '<rect width="320" height="200" fill="#f4f7fb"/><rect x="20" y="20" width="280" height="28" rx="4" fill="#1d3557"/>'
            '<rect x="20" y="68" width="180" height="92" rx="4" fill="#dbe7f3"/><rect x="212" y="68" width="88" height="92" rx="4" fill="#b9d6c2"/>'
            '<text x="32" y="39" fill="#ffffff" font-family="sans-serif" font-size="12">WorkAMA Design</text></svg>'
        ).encode()
    from PIL import Image, ImageDraw

    seed = hashlib.sha256(f"{asset_id}\0{prompt}\0{','.join(source_refs)}".encode()).digest()
    background = (seed[0] // 3 + 180, seed[1] // 4 + 180, seed[2] // 5 + 180)
    image = Image.new("RGB", (320, 200), background)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((20, 20, 300, 48), radius=4, fill=(29, 53, 87))
    draw.rounded_rectangle((20, 68, 200, 160), radius=4, fill=(219, 231, 243))
    draw.rounded_rectangle((212, 68, 300, 160), radius=4, fill=(185, 214, 194))
    draw.rectangle((36, 82, 165, 90), fill=(125, 153, 181))
    draw.rectangle((36, 104, 150, 112), fill=(165, 187, 207))
    draw.rectangle((228, 84, 284, 128), fill=(104, 151, 116))
    output = BytesIO()
    if output_format == "png":
        image.save(output, format="PNG", optimize=False)
    else:
        image.save(output, format="JPEG", quality=90, optimize=False, progressive=False, subsampling=0)
    return output.getvalue()


def _build_provenance_manifest(
    *, workspace_id: str = "", asset_id: str, operation: str, source_refs: list[str], parent_claims: list[dict[str, str]],
    content_type: str, content_sha256: str, size_bytes: int,
    signing_key: Ed25519PrivateKey | None = None,
) -> dict[str, Any]:
    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    assertions = [
        {"label": "c2pa.actions", "action": operation},
        {"label": "workama.sources", "refs": source_refs},
        {"label": "workama.content", "mime_type": content_type, "sha256": content_sha256, "size_bytes": size_bytes},
    ]
    claim_fields = {
        "claim_version": "c2pa-compatible-v1",
        "workspace_id": workspace_id,
        "asset_id": asset_id,
        "operation": operation,
        "content_sha256": content_sha256,
        "parents": parent_claims,
        "generator": DESIGN_GENERATOR,
        "created_at": created_at,
        "assertions": assertions,
    }
    claim_hash = manifest_hash(claim_fields)
    manifest = {
        "schema_version": 3,
        "manifest_id": asset_id,
        "workspace_id": workspace_id,
        "asset_id": asset_id,
        "operation": operation,
        "content_sha256": content_sha256,
        "claim_hash": claim_hash,
        "parents": parent_claims,
        "generator": DESIGN_GENERATOR,
        "created_at": created_at,
        "assertions": assertions,
        "claim": {**claim_fields, "claim_hash": claim_hash},
        "external_provider": "pending",
        "license_status": "declared" if source_refs else "unknown",
    }
    return _sign_detached_claim(manifest, signing_key or Ed25519PrivateKey.generate())


async def _design_signing_key(conn: Any, *, org_id: str, workspace_id: str, created_by: str) -> Ed25519PrivateKey:
    result = await conn.execute(
        "SELECT private_key_enc,public_key_fingerprint FROM ag_design_signing_key WHERE workspace_id=%s AND status='active'",
        (workspace_id,),
    )
    row = await result.fetchone()
    if not row:
        private_key = Ed25519PrivateKey.generate()
        private_bytes = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        public_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        await conn.execute(
            """INSERT INTO ag_design_signing_key(id,org_id,workspace_id,algorithm,private_key_enc,public_key_fingerprint,created_by)
               VALUES(%s,%s,%s,'Ed25519',%s,%s,%s) ON CONFLICT(workspace_id) DO NOTHING""",
            (new_id("dsgkey"), org_id, workspace_id, encrypt_secret(_base64url_encode(private_bytes)), design_public_key_fingerprint(public_bytes), created_by),
        )
        result = await conn.execute(
            "SELECT private_key_enc,public_key_fingerprint FROM ag_design_signing_key WHERE workspace_id=%s AND status='active'",
            (workspace_id,),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=500, detail="Design signing key is unavailable")
    try:
        private_bytes = _base64url_decode(decrypt_secret(row["private_key_enc"]) or "")
        private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
        public_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        if design_public_key_fingerprint(public_bytes) != row["public_key_fingerprint"]:
            raise ValueError("signing key fingerprint mismatch")
        return private_key
    except (TypeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=500, detail="Design signing key is invalid") from exc


def _download_manifest_headers(manifest: dict[str, Any]) -> dict[str, str]:
    encoded_manifest = _base64url_encode(json.dumps(manifest, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
    credentials = manifest["content_credentials"]
    return {
        "X-WorkAMA-Content-Credential-Manifest": encoded_manifest,
        "X-WorkAMA-Content-Credential-Status": credentials["signature_status"],
        "X-WorkAMA-Content-Credential-Profile": credentials["verifier_profile"],
        "X-WorkAMA-Content-Credential-Standard": credentials["standard"],
        "X-WorkAMA-Content-Credential-Standard-Embedded": str(credentials["standard_embedded"]).lower(),
    }


async def _design_asset_by_ref(conn, artifact_ref: str, workspace_id: str) -> dict[str, Any]:
    try:
        artifact_ref = validate_design_artifact_ref(artifact_ref)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result = await conn.execute(
        "SELECT id,name,kind,content_type,artifact_ref,content_bytes,content_sha256,size_bytes,provenance,provenance_hash,parent_asset_ids,status,created_at FROM ag_design_asset WHERE artifact_ref=%s AND workspace_id=%s",
        (artifact_ref, workspace_id),
    )
    row = await result.fetchone()
    if not row or row["status"] != "ready":
        raise HTTPException(status_code=404, detail="Design artifact not found")
    return row


def _verified_design_content(row: dict[str, Any]) -> bytes:
    content = row.get("content_bytes")
    if isinstance(content, memoryview):
        content = content.tobytes()
    if not isinstance(content, bytes):
        raise HTTPException(status_code=404, detail="Design artifact content is unavailable")
    if len(content) != row["size_bytes"] or hashlib.sha256(content).hexdigest() != row["content_sha256"]:
        raise HTTPException(status_code=500, detail="Design artifact integrity check failed")
    return content


# ------------------------------------------------------------------------------
# Canvas helpers (layers, history, export)
# ------------------------------------------------------------------------------

def _get_layers(state: dict[str, Any]) -> list[dict[str, Any]]:
    return state.get("layers") or []


def _set_layers(state: dict[str, Any], layers: list[dict[str, Any]]) -> dict[str, Any]:
    state = dict(state)
    state["layers"] = layers
    return state


def _find_layer_index(layers: list[dict[str, Any]], layer_id: str) -> int:
    for i, layer in enumerate(layers):
        if layer.get("id") == layer_id:
            return i
    return -1


def _align_layers(layers: list[dict[str, Any]], alignment: str) -> list[dict[str, Any]]:
    if not layers:
        return layers
    boxes: list[tuple[float, float, float, float]] = []
    for layer in layers:
        t = layer.get("transform") or {}
        x = float(t.get("x", 0))
        y = float(t.get("y", 0))
        w = float(t.get("width", 0))
        h = float(t.get("height", 0))
        boxes.append((x, y, w, h))
    min_x = min(b[0] for b in boxes)
    max_right = max(b[0] + b[2] for b in boxes)
    min_y = min(b[1] for b in boxes)
    max_bottom = max(b[1] + b[3] for b in boxes)
    center_x = (min_x + max_right) / 2.0
    middle_y = (min_y + max_bottom) / 2.0
    result: list[dict[str, Any]] = []
    for layer, (x, y, w, h) in zip(layers, boxes):
        new_layer = dict(layer)
        new_t = dict(new_layer.get("transform") or {})
        if alignment == "left":
            new_t["x"] = min_x
        elif alignment == "right":
            new_t["x"] = max_right - w
        elif alignment == "center":
            new_t["x"] = center_x - w / 2.0
        elif alignment == "top":
            new_t["y"] = min_y
        elif alignment == "bottom":
            new_t["y"] = max_bottom - h
        elif alignment == "middle":
            new_t["y"] = middle_y - h / 2.0
        new_layer["transform"] = new_t
        result.append(new_layer)
    return result


def _render_export_content(format: str, state: dict[str, Any], include_layers: bool) -> bytes:
    layers = (state.get("layers") or []) if include_layers else []
    seed = hashlib.sha256(json.dumps(layers, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
    if format == "svg":
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200" viewBox="0 0 320 200">'
            f'<rect width="320" height="200" fill="#f4f7fb"/>'
            f'<text x="10" y="20" font-size="12">WorkAMA Export {seed}</text></svg>'
        )
        return svg.encode()
    if format == "pdf":
        return (
            b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            + f"% {seed}\n".encode()
        )
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (320, 200), (240, 247, 251))
    draw = ImageDraw.Draw(image)
    draw.text((10, 10), f"Export {seed}", fill=(0, 0, 0))
    output = BytesIO()
    if format == "png":
        image.save(output, format="PNG")
    else:
        image.save(output, format="JPEG", quality=90)
    return output.getvalue()


async def _save_canvas_history(
    conn: Any,
    *,
    canvas_id: str,
    project_id: str,
    workspace_id: str,
    state: dict[str, Any],
    action: str,
    kind: str,
    created_by: str,
) -> None:
    history_id = new_id("dchist")
    await conn.execute(
        """INSERT INTO ag_design_canvas_history(id,canvas_id,project_id,workspace_id,state,action,kind,created_by)
           VALUES(%s,%s,%s,%s,%s::jsonb,%s,%s,%s)""",
        (history_id, canvas_id, project_id, workspace_id, json_dumps(state), action, kind, created_by),
    )
    # Limit to 50 entries per canvas
    await conn.execute(
        """DELETE FROM ag_design_canvas_history
           WHERE id NOT IN (
             SELECT id FROM ag_design_canvas_history
             WHERE canvas_id=%s ORDER BY created_at DESC LIMIT 50
           ) AND canvas_id=%s""",
        (canvas_id, canvas_id),
    )


async def _push_canvas_history(
    conn: Any,
    *,
    canvas_id: str,
    project_id: str,
    workspace_id: str,
    state: dict[str, Any],
    action: str,
    created_by: str,
) -> None:
    # New action clears redo stack
    await conn.execute(
        "DELETE FROM ag_design_canvas_history WHERE canvas_id=%s AND kind='future'",
        (canvas_id,),
    )
    await _save_canvas_history(
        conn,
        canvas_id=canvas_id,
        project_id=project_id,
        workspace_id=workspace_id,
        state=state,
        action=action,
        kind="past",
        created_by=created_by,
    )


async def _undo_canvas(
    conn: Any, *, project_id: str, workspace_id: str, created_by: str
) -> tuple[dict[str, Any], int]:
    canvas_result = await conn.execute(
        "SELECT id,state,version FROM ag_design_canvas WHERE project_id=%s AND workspace_id=%s",
        (project_id, workspace_id),
    )
    canvas = await canvas_result.fetchone()
    if not canvas:
        raise HTTPException(status_code=404, detail="Canvas not found")
    past_result = await conn.execute(
        """SELECT id,state FROM ag_design_canvas_history
           WHERE canvas_id=%s AND kind='past'
           ORDER BY created_at DESC LIMIT 1""",
        (canvas["id"],),
    )
    past = await past_result.fetchone()
    if not past:
        raise HTTPException(status_code=400, detail="Nothing to undo")
    # Save current state to future stack
    await _save_canvas_history(
        conn,
        canvas_id=canvas["id"],
        project_id=project_id,
        workspace_id=workspace_id,
        state=dict(canvas["state"]),
        action="undo",
        kind="future",
        created_by=created_by,
    )
    new_state = dict(past["state"])
    await conn.execute(
        "UPDATE ag_design_canvas SET state=%s::jsonb, updated_at=now() WHERE id=%s",
        (json_dumps(new_state), canvas["id"]),
    )
    await conn.execute(
        "DELETE FROM ag_design_canvas_history WHERE id=%s",
        (past["id"],),
    )
    version_result = await conn.execute(
        "SELECT version FROM ag_design_canvas WHERE id=%s",
        (canvas["id"],),
    )
    version_row = await version_result.fetchone()
    return new_state, version_row["version"] if version_row else canvas["version"]


async def _redo_canvas(
    conn: Any, *, project_id: str, workspace_id: str, created_by: str
) -> tuple[dict[str, Any], int]:
    canvas_result = await conn.execute(
        "SELECT id,state,version FROM ag_design_canvas WHERE project_id=%s AND workspace_id=%s",
        (project_id, workspace_id),
    )
    canvas = await canvas_result.fetchone()
    if not canvas:
        raise HTTPException(status_code=404, detail="Canvas not found")
    future_result = await conn.execute(
        """SELECT id,state FROM ag_design_canvas_history
           WHERE canvas_id=%s AND kind='future'
           ORDER BY created_at DESC LIMIT 1""",
        (canvas["id"],),
    )
    future = await future_result.fetchone()
    if not future:
        raise HTTPException(status_code=400, detail="Nothing to redo")
    # Save current state to past stack
    await _save_canvas_history(
        conn,
        canvas_id=canvas["id"],
        project_id=project_id,
        workspace_id=workspace_id,
        state=dict(canvas["state"]),
        action="redo",
        kind="past",
        created_by=created_by,
    )
    new_state = dict(future["state"])
    await conn.execute(
        "UPDATE ag_design_canvas SET state=%s::jsonb, updated_at=now() WHERE id=%s",
        (json_dumps(new_state), canvas["id"]),
    )
    await conn.execute(
        "DELETE FROM ag_design_canvas_history WHERE id=%s",
        (future["id"],),
    )
    version_result = await conn.execute(
        "SELECT version FROM ag_design_canvas WHERE id=%s",
        (canvas["id"],),
    )
    version_row = await version_result.fetchone()
    return new_state, version_row["version"] if version_row else canvas["version"]


@router.get("/projects")
async def list_projects(actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:read")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,name,slug,description,canvas_width,canvas_height,status,version,created_at,updated_at FROM ag_design_project WHERE workspace_id=%s ORDER BY updated_at DESC", (actor.workspace_id,))
        data = [_project_public(row) for row in await result.fetchall()]
    # Contract《720》listDesignProjects: ListQuery -> ListResponse<DesignProjectDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None},
    }


@router.post("/projects", status_code=201)
async def create_project(body: ProjectCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:create")
    slug = body.slug or re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")[:64] or "project"
    if not _PROJECT_SLUG.fullmatch(slug):
        raise HTTPException(status_code=422, detail="project name cannot produce a safe slug")
    project_id = new_id("dsg")
    async with pool.connection() as conn:
        try:
            result = await conn.execute(
                """INSERT INTO ag_design_project(id,org_id,workspace_id,name,slug,description,canvas_width,canvas_height,created_by)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING id,name,slug,description,canvas_width,canvas_height,status,version,created_at,updated_at""",
                (project_id, actor.org_id, actor.workspace_id, body.name, slug, body.description, body.canvas_width, body.canvas_height, actor.user_id),
            )
            row = await result.fetchone()
            await conn.commit()
        except Exception as exc:
            await conn.rollback()
            if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
                raise HTTPException(status_code=409, detail="Design project slug already exists") from exc
            raise
    return row


@router.get("/projects/{project_id}")
async def get_project(project_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:read")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,name,slug,description,canvas_width,canvas_height,status,version,created_at,updated_at FROM ag_design_project WHERE id=%s AND workspace_id=%s", (project_id, actor.workspace_id))
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Design project not found")
    return row


@router.patch("/projects/{project_id}")
async def patch_project(project_id: str, body: ProjectPatch, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:write")
    updates: list[str] = []
    values: list[Any] = []
    for field in ("name", "description", "canvas_width", "canvas_height", "status"):
        value = getattr(body, field)
        if value is not None:
            updates.append(f"{field}=%s")
            values.append(value)
    if not updates:
        return await get_project(project_id, actor)
    updates.extend(["version=version+1", "updated_at=now()"])
    values.extend([project_id, actor.workspace_id])
    async with pool.connection() as conn:
        result = await conn.execute(f"UPDATE ag_design_project SET {', '.join(updates)} WHERE id=%s AND workspace_id=%s RETURNING id,name,slug,description,canvas_width,canvas_height,status,version,created_at,updated_at", values)
        row = await result.fetchone()
        await conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Design project not found")
    return row


@router.get("/projects/{project_id}/assets")
async def list_assets(project_id: str, actor: Annotated[Actor, Depends(get_actor)], limit: int = Query(default=100, ge=1, le=200)):
    _require(actor, "design:read")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,name,kind,content_type,artifact_ref,content_sha256,size_bytes,provenance,provenance_hash,parent_asset_ids,status,created_at FROM ag_design_asset WHERE project_id=%s AND workspace_id=%s ORDER BY created_at DESC LIMIT %s", (project_id, actor.workspace_id, limit))
        data = [_asset_public(row) for row in await result.fetchall()]
    # Contract《720》listDesignAssets: ListQuery -> ListResponse<DesignAssetDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None},
    }


@router.get("/artifacts")
async def get_design_artifact(actor: Annotated[Actor, Depends(get_actor)], artifact_ref: str = Query(min_length=1, max_length=240)):
    _require(actor, "design:read")
    async with pool.connection() as conn:
        row = await _design_asset_by_ref(conn, artifact_ref, actor.workspace_id)
    return _asset_public(row)


@router.get("/artifacts/download", response_class=Response)
async def download_design_artifact(actor: Annotated[Actor, Depends(get_actor)], artifact_ref: str = Query(min_length=1, max_length=240)):
    _require(actor, "design:read")
    async with pool.connection() as conn:
        row = await _design_asset_by_ref(conn, artifact_ref, actor.workspace_id)
    content = _verified_design_content(row)
    filename = re.sub(r"[^A-Za-z0-9._-]+", "-", row["name"]).strip(".-") or row["id"]
    return Response(
        content,
        media_type=row["content_type"],
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            **_download_manifest_headers(row["provenance"]),
        },
    )


@router.post("/projects/{project_id}/jobs", status_code=202)
async def create_design_job(project_id: str, body: DesignJobCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:create")
    async with pool.connection() as conn:
        project_result = await conn.execute("SELECT id,status FROM ag_design_project WHERE id=%s AND workspace_id=%s", (project_id, actor.workspace_id))
        project = await project_result.fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Design project not found")
        if project["status"] != "active":
            raise HTTPException(status_code=409, detail="Design project is archived")
        parent_claims: list[dict[str, str]] = []
        if body.parent_asset_ids:
            parents = await conn.execute("SELECT id,content_sha256,provenance_hash FROM ag_design_asset WHERE project_id=%s AND workspace_id=%s AND id = ANY(%s) AND status='ready'", (project_id, actor.workspace_id, body.parent_asset_ids))
            parent_rows = await parents.fetchall()
            if len(parent_rows) != len(body.parent_asset_ids):
                raise HTTPException(status_code=422, detail="parent asset is not in this project")
            parent_by_id = {row["id"]: row for row in parent_rows}
            parent_claims = [
                {"asset_id": parent_id, "content_sha256": parent_by_id[parent_id]["content_sha256"], "provenance_hash": parent_by_id[parent_id]["provenance_hash"]}
                for parent_id in body.parent_asset_ids
            ]
        if body.idempotency_key:
            previous = await conn.execute("SELECT id,operation,status,asset_id,created_at,completed_at FROM ag_design_job WHERE project_id=%s AND idempotency_key=%s", (project_id, body.idempotency_key))
            row = await previous.fetchone()
            if row:
                # Contract《720》createDesignJob: ... -> OperationAccepted（保留旧字段向后兼容）
                return {
                    **row,
                    "idempotency_replayed": True,
                    "operation_id": row.get("id"),
                    "status": row.get("status", "queued"),
                    "status_url": f"/api/v1/design/jobs/{row.get('id')}",
                    "submitted_at": row.get("created_at"),
                }
        prompt_hash = hashlib.sha256(body.prompt.encode()).hexdigest()
        job_id = new_id("dsgjob")
        asset_id = new_id("dsgasset")
        content = _render_design_content(body.output_format, body.prompt, body.source_refs, asset_id)
        content_sha = hashlib.sha256(content).hexdigest()
        content_type = _content_type(body.output_format)
        signing_key = await _design_signing_key(conn, org_id=actor.org_id, workspace_id=actor.workspace_id, created_by=actor.user_id)
        manifest = _build_provenance_manifest(
            workspace_id=actor.workspace_id,
            asset_id=asset_id,
            operation=body.operation,
            source_refs=body.source_refs,
            parent_claims=parent_claims,
            content_type=content_type,
            content_sha256=content_sha,
            size_bytes=len(content),
            signing_key=signing_key,
        )
        provenance_hash = manifest_hash(manifest)
        artifact_ref = f"design://artifact/{asset_id}"
        kind = "prototype" if body.operation == "prototype" else "image"
        await conn.execute("INSERT INTO ag_design_asset(id,project_id,workspace_id,name,kind,content_type,artifact_ref,content_bytes,content_sha256,size_bytes,provenance,provenance_hash,parent_asset_ids,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)", (asset_id, project_id, actor.workspace_id, f"{body.operation}-{asset_id}", kind, content_type, artifact_ref, content, content_sha, len(content), json_dumps(manifest), provenance_hash, body.parent_asset_ids, actor.user_id))
        result = await conn.execute("""INSERT INTO ag_design_job(id,project_id,workspace_id,operation,prompt_hash,source_refs,parent_asset_ids,output_format,status,asset_id,idempotency_key,created_by,completed_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'succeeded',%s,%s,%s,now())
            RETURNING id,project_id,operation,status,asset_id,created_at,completed_at""", (job_id, project_id, actor.workspace_id, body.operation, prompt_hash, body.source_refs, body.parent_asset_ids, body.output_format, asset_id, body.idempotency_key, actor.user_id))
        row = await result.fetchone()
        await conn.commit()
    # Contract《720》createDesignJob: ... -> OperationAccepted（保留旧字段向后兼容）
    return {
        **row,
        "artifact_ref": artifact_ref,
        "content_type": content_type,
        "size_bytes": len(content),
        "content_sha256": content_sha,
        "provenance_hash": provenance_hash,
        "provenance": manifest,
        "signature_status": CONTENT_CREDENTIAL_STATUS,
        "verifier_profile": CONTENT_CREDENTIAL_PROFILE,
        "standard_embedded": False,
        "idempotency_replayed": False,
        "external_provider": "pending",
        "operation_id": row.get("id"),
        "status": row.get("status", "queued"),
        "status_url": f"/api/v1/design/jobs/{row.get('id')}",
        "submitted_at": row.get("created_at"),
    }


@router.get("/jobs/{job_id}")
async def get_design_job(job_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:read")
    async with pool.connection() as conn:
        result = await conn.execute("""SELECT j.id,j.project_id,j.operation,j.prompt_hash,j.source_refs,j.parent_asset_ids,j.output_format,j.status,j.asset_id,j.error_code,j.error_message,j.idempotency_key,j.created_at,j.completed_at,a.artifact_ref,a.content_sha256,a.provenance_hash,a.provenance
          FROM ag_design_job j LEFT JOIN ag_design_asset a ON a.id=j.asset_id
          WHERE j.id=%s AND j.workspace_id=%s""", (job_id, actor.workspace_id))
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Design job not found")
    manifest = row.get("provenance") or {}
    credentials = manifest.get("content_credentials") or {}
    return {
        **row,
        "signature_status": credentials.get("signature_status"),
        "verifier_profile": credentials.get("verifier_profile"),
        "standard_embedded": credentials.get("standard_embedded"),
    }


def _generate_image_placeholder_urls(job_id: str, prompt: str, style: str, size: str, num_images: int) -> list[str]:
    seed = hashlib.sha256(f"{job_id}\0{prompt}\0{style}\0{size}".encode()).hexdigest()
    urls = []
    for i in range(num_images):
        variant_seed = hashlib.sha256(f"{seed}\0{i}".encode()).hexdigest()[:16]
        urls.append(f"mock://image/{variant_seed}/{size}/placeholder-{i+1}.png")
    return urls


@router.post("/image-jobs", status_code=202)
async def create_image_job(body: ImageJobCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:create")
    job_id = new_id("dimg")
    result_urls = _generate_image_placeholder_urls(job_id, body.prompt, body.style, body.size, body.num_images)
    metadata = {
        "provenance": {
            "generator": "workama.mock.image.v1",
            "prompt_sha256": hashlib.sha256(body.prompt.encode()).hexdigest(),
            "style": body.style,
            "size": body.size,
            "pending_external": True,
        }
    }
    async with pool.connection() as conn:
        if body.project_id:
            project_result = await conn.execute(
                "SELECT id,status FROM ag_design_project WHERE id=%s AND workspace_id=%s",
                (body.project_id, actor.workspace_id),
            )
            project = await project_result.fetchone()
            if not project:
                raise HTTPException(status_code=404, detail="Design project not found")
            if project["status"] != "active":
                raise HTTPException(status_code=409, detail="Design project is archived")
        result = await conn.execute(
            """INSERT INTO ag_design_image_job(id,workspace_id,project_id,prompt,style,size,num_images,status,result_urls,model,metadata,created_by,completed_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s,'succeeded',%s,%s,%s::jsonb,%s,now())
               RETURNING id,workspace_id,project_id,prompt,style,size,num_images,status,result_urls,model,metadata,created_at,updated_at,completed_at""",
            (job_id, actor.workspace_id, body.project_id, body.prompt, body.style, body.size, body.num_images, result_urls, "workama.mock.image.v1", json_dumps(metadata), actor.user_id),
        )
        row = await result.fetchone()
        await conn.commit()
    return {
        **row,
        "placeholder_urls": result_urls,
        "external_provider": "pending",
    }


@router.get("/image-jobs")
async def list_image_jobs(
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    _require(actor, "design:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT id,workspace_id,project_id,prompt,style,size,num_images,status,result_urls,model,metadata,created_at,updated_at,completed_at
               FROM ag_design_image_job WHERE workspace_id=%s ORDER BY created_at DESC LIMIT %s OFFSET %s""",
            (actor.workspace_id, limit, offset),
        )
        rows = await result.fetchall()
        count_result = await conn.execute(
            "SELECT COUNT(*) AS total FROM ag_design_image_job WHERE workspace_id=%s",
            (actor.workspace_id,),
        )
        total_row = await count_result.fetchone()
    return {
        "items": rows,
        "data": rows,
        "total": total_row["total"] if total_row else 0,
        "limit": limit,
        "offset": offset,
        "has_more": len(rows) == limit,
        "meta": {"request_id": None},
    }


@router.get("/image-jobs/{job_id}")
async def get_image_job(job_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT id,workspace_id,project_id,prompt,style,size,num_images,status,result_urls,model,metadata,created_at,updated_at,completed_at
               FROM ag_design_image_job WHERE id=%s AND workspace_id=%s""",
            (job_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Image job not found")
    return dict(row)


@router.delete("/image-jobs/{job_id}")
async def delete_image_job(job_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:write")
    async with pool.connection() as conn:
        result = await conn.execute(
            "UPDATE ag_design_image_job SET status='cancelled',updated_at=now() WHERE id=%s AND workspace_id=%s RETURNING id",
            (job_id, actor.workspace_id),
        )
        row = await result.fetchone()
        await conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Image job not found")
    return {"id": job_id, "status": "cancelled"}


@router.post("/image-jobs/{job_id}/edit", status_code=202)
async def edit_image_job(job_id: str, body: ImageJobEdit, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:create")
    async with pool.connection() as conn:
        prev_result = await conn.execute(
            "SELECT id,prompt,style,size,metadata FROM ag_design_image_job WHERE id=%s AND workspace_id=%s",
            (job_id, actor.workspace_id),
        )
        prev = await prev_result.fetchone()
        if not prev:
            raise HTTPException(status_code=404, detail="Image job not found")
        new_job_id = new_id("dimg")
        result_urls = _generate_image_placeholder_urls(new_job_id, body.prompt, body.style, prev["size"], 1)
        metadata = dict(prev.get("metadata") or {})
        metadata["provenance"] = metadata.get("provenance", {})
        metadata["provenance"]["parent_job_id"] = job_id
        metadata["provenance"]["operation"] = "edit"
        metadata["provenance"]["pending_external"] = True
        result = await conn.execute(
            """INSERT INTO ag_design_image_job(id,workspace_id,project_id,prompt,style,size,num_images,status,result_urls,model,metadata,created_by,completed_at)
               VALUES(%s,%s,(SELECT project_id FROM ag_design_image_job WHERE id=%s),%s,%s,%s,1,'succeeded',%s,%s,%s::jsonb,%s,now())
               RETURNING id,workspace_id,project_id,prompt,style,size,num_images,status,result_urls,model,metadata,created_at,updated_at,completed_at""",
            (new_job_id, actor.workspace_id, job_id, body.prompt, body.style, prev["size"], result_urls, "workama.mock.image.v1", json_dumps(metadata), actor.user_id),
        )
        row = await result.fetchone()
        await conn.commit()
    return {
        **row,
        "placeholder_urls": result_urls,
        "external_provider": "pending",
    }


@router.post("/image-jobs/{job_id}/variate", status_code=202)
async def variate_image_job(job_id: str, body: ImageJobVariate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:create")
    async with pool.connection() as conn:
        prev_result = await conn.execute(
            "SELECT id,prompt,style,size,metadata FROM ag_design_image_job WHERE id=%s AND workspace_id=%s",
            (job_id, actor.workspace_id),
        )
        prev = await prev_result.fetchone()
        if not prev:
            raise HTTPException(status_code=404, detail="Image job not found")
        new_job_id = new_id("dimg")
        result_urls = _generate_image_placeholder_urls(new_job_id, prev["prompt"], prev["style"], prev["size"], body.num_variations)
        metadata = dict(prev.get("metadata") or {})
        metadata["provenance"] = metadata.get("provenance", {})
        metadata["provenance"]["parent_job_id"] = job_id
        metadata["provenance"]["operation"] = "variate"
        metadata["provenance"]["pending_external"] = True
        result = await conn.execute(
            """INSERT INTO ag_design_image_job(id,workspace_id,project_id,prompt,style,size,num_images,status,result_urls,model,metadata,created_by,completed_at)
               VALUES(%s,%s,(SELECT project_id FROM ag_design_image_job WHERE id=%s),%s,%s,%s,%s,'succeeded',%s,%s,%s::jsonb,%s,now())
               RETURNING id,workspace_id,project_id,prompt,style,size,num_images,status,result_urls,model,metadata,created_at,updated_at,completed_at""",
            (new_job_id, actor.workspace_id, job_id, prev["prompt"], prev["style"], prev["size"], body.num_variations, result_urls, "workama.mock.image.v1", json_dumps(metadata), actor.user_id),
        )
        row = await result.fetchone()
        await conn.commit()
    return {
        **row,
        "placeholder_urls": result_urls,
        "external_provider": "pending",
    }


@router.post("/projects/{project_id}/canvas/sync")
async def sync_canvas(project_id: str, body: CanvasSync, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:write")
    async with pool.connection() as conn:
        project_result = await conn.execute(
            "SELECT id FROM ag_design_project WHERE id=%s AND workspace_id=%s",
            (project_id, actor.workspace_id),
        )
        if not await project_result.fetchone():
            raise HTTPException(status_code=404, detail="Design project not found")
        canvas_result = await conn.execute(
            "SELECT id,state,version FROM ag_design_canvas WHERE project_id=%s AND workspace_id=%s",
            (project_id, actor.workspace_id),
        )
        existing = await canvas_result.fetchone()
        if existing:
            await _push_canvas_history(
                conn,
                canvas_id=existing["id"],
                project_id=project_id,
                workspace_id=actor.workspace_id,
                state=dict(existing["state"]),
                action="sync",
                created_by=actor.user_id,
            )
        result = await conn.execute(
            """INSERT INTO ag_design_canvas(id,project_id,workspace_id,state,version,created_by,updated_at)
               VALUES(%s,%s,%s,%s::jsonb,1,%s,now())
               ON CONFLICT(project_id) DO UPDATE SET state=EXCLUDED.state,version=ag_design_canvas.version+1,updated_at=now()
               RETURNING id,project_id,state,version,created_at,updated_at""",
            (new_id("dcanvas"), project_id, actor.workspace_id, json_dumps(body.state), actor.user_id),
        )
        row = await result.fetchone()
        await conn.commit()
    return dict(row)


@router.get("/projects/{project_id}/canvas")
async def get_canvas(project_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT id,project_id,state,version,created_at,updated_at FROM ag_design_canvas WHERE project_id=%s AND workspace_id=%s",
            (project_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Canvas not found")
    return dict(row)


@router.post("/projects/{project_id}/canvas/layers")
async def add_layer(project_id: str, body: LayerCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:write")
    async with pool.connection() as conn:
        canvas_result = await conn.execute(
            "SELECT id,state,version FROM ag_design_canvas WHERE project_id=%s AND workspace_id=%s",
            (project_id, actor.workspace_id),
        )
        canvas = await canvas_result.fetchone()
        if not canvas:
            raise HTTPException(status_code=404, detail="Canvas not found")
        state = dict(canvas["state"])
        layers = _get_layers(state)
        if _find_layer_index(layers, body.layer_id) >= 0:
            raise HTTPException(status_code=409, detail="Layer already exists")
        new_layer = {
            "id": body.layer_id,
            "type": body.type,
            "name": body.name,
            "transform": body.transform,
            "properties": body.properties,
        }
        layers = [new_layer] + layers
        new_state = _set_layers(state, layers)
        await _push_canvas_history(
            conn,
            canvas_id=canvas["id"],
            project_id=project_id,
            workspace_id=actor.workspace_id,
            state=dict(canvas["state"]),
            action="layer_add",
            created_by=actor.user_id,
        )
        result = await conn.execute(
            "UPDATE ag_design_canvas SET state=%s::jsonb,version=version+1,updated_at=now() WHERE id=%s RETURNING id,project_id,state,version,created_at,updated_at",
            (json_dumps(new_state), canvas["id"]),
        )
        row = await result.fetchone()
        await conn.commit()
    return dict(row)


@router.patch("/projects/{project_id}/canvas/layers/{layer_id}")
async def update_layer(project_id: str, layer_id: str, body: LayerPatch, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:write")
    async with pool.connection() as conn:
        canvas_result = await conn.execute(
            "SELECT id,state,version FROM ag_design_canvas WHERE project_id=%s AND workspace_id=%s",
            (project_id, actor.workspace_id),
        )
        canvas = await canvas_result.fetchone()
        if not canvas:
            raise HTTPException(status_code=404, detail="Canvas not found")
        state = dict(canvas["state"])
        layers = _get_layers(state)
        idx = _find_layer_index(layers, layer_id)
        if idx < 0:
            raise HTTPException(status_code=404, detail="Layer not found")
        updated = dict(layers[idx])
        if body.name is not None:
            updated["name"] = body.name
        if body.transform is not None:
            updated["transform"] = body.transform
        if body.properties is not None:
            updated["properties"] = body.properties
        layers = list(layers)
        layers[idx] = updated
        new_state = _set_layers(state, layers)
        await _push_canvas_history(
            conn,
            canvas_id=canvas["id"],
            project_id=project_id,
            workspace_id=actor.workspace_id,
            state=dict(canvas["state"]),
            action="layer_update",
            created_by=actor.user_id,
        )
        result = await conn.execute(
            "UPDATE ag_design_canvas SET state=%s::jsonb,version=version+1,updated_at=now() WHERE id=%s RETURNING id,project_id,state,version,created_at,updated_at",
            (json_dumps(new_state), canvas["id"]),
        )
        row = await result.fetchone()
        await conn.commit()
    return dict(row)


@router.delete("/projects/{project_id}/canvas/layers/{layer_id}")
async def delete_layer(project_id: str, layer_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:write")
    async with pool.connection() as conn:
        canvas_result = await conn.execute(
            "SELECT id,state,version FROM ag_design_canvas WHERE project_id=%s AND workspace_id=%s",
            (project_id, actor.workspace_id),
        )
        canvas = await canvas_result.fetchone()
        if not canvas:
            raise HTTPException(status_code=404, detail="Canvas not found")
        state = dict(canvas["state"])
        layers = _get_layers(state)
        idx = _find_layer_index(layers, layer_id)
        if idx < 0:
            raise HTTPException(status_code=404, detail="Layer not found")
        layers = [l for l in layers if l.get("id") != layer_id]
        new_state = _set_layers(state, layers)
        await _push_canvas_history(
            conn,
            canvas_id=canvas["id"],
            project_id=project_id,
            workspace_id=actor.workspace_id,
            state=dict(canvas["state"]),
            action="layer_delete",
            created_by=actor.user_id,
        )
        result = await conn.execute(
            "UPDATE ag_design_canvas SET state=%s::jsonb,version=version+1,updated_at=now() WHERE id=%s RETURNING id,project_id,state,version,created_at,updated_at",
            (json_dumps(new_state), canvas["id"]),
        )
        row = await result.fetchone()
        await conn.commit()
    return dict(row)


@router.post("/projects/{project_id}/canvas/layers/reorder")
async def reorder_layers(project_id: str, body: LayerReorder, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:write")
    async with pool.connection() as conn:
        canvas_result = await conn.execute(
            "SELECT id,state,version FROM ag_design_canvas WHERE project_id=%s AND workspace_id=%s",
            (project_id, actor.workspace_id),
        )
        canvas = await canvas_result.fetchone()
        if not canvas:
            raise HTTPException(status_code=404, detail="Canvas not found")
        state = dict(canvas["state"])
        layers = _get_layers(state)
        layer_map = {layer.get("id"): layer for layer in layers}
        ordered = []
        for lid in body.layer_ids:
            if lid in layer_map:
                ordered.append(layer_map[lid])
        for layer in layers:
            if layer.get("id") not in body.layer_ids:
                ordered.append(layer)
        new_state = _set_layers(state, ordered)
        await _push_canvas_history(
            conn,
            canvas_id=canvas["id"],
            project_id=project_id,
            workspace_id=actor.workspace_id,
            state=dict(canvas["state"]),
            action="layer_reorder",
            created_by=actor.user_id,
        )
        result = await conn.execute(
            "UPDATE ag_design_canvas SET state=%s::jsonb,version=version+1,updated_at=now() WHERE id=%s RETURNING id,project_id,state,version,created_at,updated_at",
            (json_dumps(new_state), canvas["id"]),
        )
        row = await result.fetchone()
        await conn.commit()
    return dict(row)


@router.post("/projects/{project_id}/canvas/align")
async def align_layers(project_id: str, body: AlignRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:write")
    async with pool.connection() as conn:
        canvas_result = await conn.execute(
            "SELECT id,state,version FROM ag_design_canvas WHERE project_id=%s AND workspace_id=%s",
            (project_id, actor.workspace_id),
        )
        canvas = await canvas_result.fetchone()
        if not canvas:
            raise HTTPException(status_code=404, detail="Canvas not found")
        state = dict(canvas["state"])
        layers = _get_layers(state)
        selected = []
        for layer in layers:
            if layer.get("id") in body.layer_ids:
                selected.append(layer)
        if not selected:
            raise HTTPException(status_code=422, detail="No matching layers selected")
        aligned = _align_layers(selected, body.alignment)
        aligned_map = {layer["id"]: layer for layer in aligned}
        new_layers = []
        for layer in layers:
            lid = layer.get("id")
            if lid in aligned_map:
                new_layers.append(aligned_map[lid])
            else:
                new_layers.append(layer)
        new_state = _set_layers(state, new_layers)
        await _push_canvas_history(
            conn,
            canvas_id=canvas["id"],
            project_id=project_id,
            workspace_id=actor.workspace_id,
            state=dict(canvas["state"]),
            action="align",
            created_by=actor.user_id,
        )
        result = await conn.execute(
            "UPDATE ag_design_canvas SET state=%s::jsonb,version=version+1,updated_at=now() WHERE id=%s RETURNING id,project_id,state,version,created_at,updated_at",
            (json_dumps(new_state), canvas["id"]),
        )
        row = await result.fetchone()
        await conn.commit()
    return {
        "canvas": dict(row),
        "updated_layers": aligned,
    }


@router.post("/projects/{project_id}/export")
async def export_project(project_id: str, body: ExportRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:read")
    job_id = new_id("dexport")
    async with pool.connection() as conn:
        project_result = await conn.execute(
            "SELECT id,status FROM ag_design_project WHERE id=%s AND workspace_id=%s",
            (project_id, actor.workspace_id),
        )
        project = await project_result.fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Design project not found")
        canvas_result = await conn.execute(
            "SELECT state FROM ag_design_canvas WHERE project_id=%s AND workspace_id=%s",
            (project_id, actor.workspace_id),
        )
        canvas = await canvas_result.fetchone()
        state = dict(canvas["state"]) if canvas else {}
        # Create job as queued
        await conn.execute(
            """INSERT INTO ag_design_export_job(id,project_id,workspace_id,format,include_layers,status,created_by)
               VALUES(%s,%s,%s,%s,%s,'queued',%s)""",
            (job_id, project_id, actor.workspace_id, body.format, body.include_layers, actor.user_id),
        )
        # Move to running
        await conn.execute(
            "UPDATE ag_design_export_job SET status='running',updated_at=now() WHERE id=%s",
            (job_id,),
        )
        try:
            content = _render_export_content(body.format, state, body.include_layers)
            content_sha = hashlib.sha256(content).hexdigest()
            await conn.execute(
                """UPDATE ag_design_export_job
                   SET status='succeeded',result_bytes=%s,result_sha256=%s,updated_at=now(),completed_at=now()
                   WHERE id=%s""",
                (content, content_sha, job_id),
            )
        except Exception as exc:
            await conn.execute(
                "UPDATE ag_design_export_job SET status='failed',error_message=%s,updated_at=now(),completed_at=now() WHERE id=%s",
                (str(exc)[:500], job_id),
            )
        result = await conn.execute(
            "SELECT id,project_id,format,include_layers,status,result_sha256,error_message,created_at,updated_at,completed_at FROM ag_design_export_job WHERE id=%s",
            (job_id,),
        )
        row = await result.fetchone()
        await conn.commit()
    return dict(row)


@router.get("/projects/{project_id}/exports/{job_id}")
async def get_export_job(project_id: str, job_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT id,project_id,format,include_layers,status,result_sha256,error_message,created_at,updated_at,completed_at FROM ag_design_export_job WHERE id=%s AND project_id=%s AND workspace_id=%s",
            (job_id, project_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Export job not found")
    return dict(row)


@router.post("/projects/{project_id}/canvas/undo")
async def undo_canvas(project_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:write")
    async with pool.connection() as conn:
        new_state, version = await _undo_canvas(
            conn, project_id=project_id, workspace_id=actor.workspace_id, created_by=actor.user_id
        )
        await conn.commit()
    return {"state": new_state, "version": version}


@router.post("/projects/{project_id}/canvas/redo")
async def redo_canvas(project_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:write")
    async with pool.connection() as conn:
        new_state, version = await _redo_canvas(
            conn, project_id=project_id, workspace_id=actor.workspace_id, created_by=actor.user_id
        )
        await conn.commit()
    return {"state": new_state, "version": version}


# ------------------------------------------------------------------------------
# Provenance Manifest (C2PA-compatible unsigned claim)
# ------------------------------------------------------------------------------

PROVENANCE_MANIFEST_VERSION = "1.0"


class ProvenanceSourceAsset(BaseModel):
    asset_id: str = Field(min_length=1, max_length=160)
    content_sha256: str = Field(default="", max_length=128)


class ProvenanceManifestCreate(BaseModel):
    manifest_version: str = Field(default=PROVENANCE_MANIFEST_VERSION, min_length=1, max_length=32)
    generator: dict[str, Any] = Field(default_factory=dict)
    prompt_hash: str = Field(default="", max_length=128)
    source_assets: list[ProvenanceSourceAsset] = Field(default_factory=list, max_length=32)
    metadata: dict[str, Any] = Field(default_factory=dict)


def _canonical_json_for_claim(
    *,
    asset_id: str,
    generator: dict[str, Any],
    prompt_hash: str,
    source_assets: list[dict[str, Any]],
    parent_claim_hash: str | None,
    created_at: str,
) -> str:
    return json.dumps(
        {
            "asset_id": asset_id,
            "generator": generator,
            "prompt_hash": prompt_hash,
            "source_assets": source_assets,
            "parent_claim_hash": parent_claim_hash,
            "created_at": created_at,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _compute_claim_hash(
    *,
    asset_id: str,
    generator: dict[str, Any],
    prompt_hash: str,
    source_assets: list[dict[str, Any]],
    parent_claim_hash: str | None,
    created_at: str,
) -> str:
    canonical = _canonical_json_for_claim(
        asset_id=asset_id,
        generator=generator,
        prompt_hash=prompt_hash,
        source_assets=source_assets,
        parent_claim_hash=parent_claim_hash,
        created_at=created_at,
    )
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _manifest_public(row: dict[str, Any]) -> dict[str, Any]:
    public = dict(row)
    if isinstance(public.get("generator"), str):
        try:
            public["generator"] = json.loads(public["generator"])
        except (TypeError, ValueError):
            pass
    if isinstance(public.get("source_assets"), str):
        try:
            public["source_assets"] = json.loads(public["source_assets"])
        except (TypeError, ValueError):
            pass
    if isinstance(public.get("metadata"), str):
        try:
            public["metadata"] = json.loads(public["metadata"])
        except (TypeError, ValueError):
            pass
    return public


@router.post("/projects/{project_id}/assets/{asset_id}/provenance", status_code=201)
async def create_provenance_manifest(
    project_id: str,
    asset_id: str,
    body: ProvenanceManifestCreate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require(actor, "design:write")
    async with pool.connection() as conn:
        asset_result = await conn.execute(
            "SELECT id,workspace_id,project_id,content_sha256 FROM ag_design_asset WHERE id=%s AND project_id=%s AND workspace_id=%s AND status='ready'",
            (asset_id, project_id, actor.workspace_id),
        )
        asset_row = await asset_result.fetchone()
        if not asset_row:
            raise HTTPException(status_code=404, detail="Design asset not found")
        source_assets_payload: list[dict[str, Any]] = []
        parent_claim_hash: str | None = None
        if body.source_assets:
            parent_ids = [s.asset_id for s in body.source_assets]
            parents_result = await conn.execute(
                "SELECT id,content_sha256 FROM ag_design_asset WHERE id = ANY(%s) AND workspace_id=%s AND status='ready'",
                (parent_ids, actor.workspace_id),
            )
            parent_rows = {row["id"]: row for row in await parents_result.fetchall()}
            for source in body.source_assets:
                parent_row = parent_rows.get(source.asset_id)
                if not parent_row:
                    raise HTTPException(status_code=422, detail="source asset not found in workspace")
                latest_result = await conn.execute(
                    "SELECT claim_hash FROM ag_design_provenance_manifest WHERE asset_id=%s AND workspace_id=%s ORDER BY created_at DESC LIMIT 1",
                    (source.asset_id, actor.workspace_id),
                )
                latest = await latest_result.fetchone()
                parent_claim = latest["claim_hash"] if latest else None
                source_assets_payload.append(
                    {
                        "asset_id": source.asset_id,
                        "content_sha256": source.content_sha256 or parent_row["content_sha256"],
                        "claim_hash": parent_claim,
                    }
                )
            parent_claim_hash = source_assets_payload[0]["claim_hash"]
        created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        generator = body.generator or {"model": DESIGN_GENERATOR, "version": "1.0"}
        claim_hash = _compute_claim_hash(
            asset_id=asset_id,
            generator=generator,
            prompt_hash=body.prompt_hash,
            source_assets=source_assets_payload,
            parent_claim_hash=parent_claim_hash,
            created_at=created_at,
        )
        manifest_id = new_id("dprov")
        try:
            result = await conn.execute(
                """INSERT INTO ag_design_provenance_manifest(id,org_id,workspace_id,project_id,asset_id,manifest_version,generator,prompt_hash,source_assets,claim_hash,parent_claim_hash,created_by,created_at,metadata)
                   VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb)
                   RETURNING id,workspace_id,project_id,asset_id,manifest_version,generator,prompt_hash,source_assets,claim_hash,parent_claim_hash,created_by,created_at,metadata""",
                (
                    manifest_id,
                    actor.org_id,
                    actor.workspace_id,
                    project_id,
                    asset_id,
                    body.manifest_version,
                    json_dumps(generator),
                    body.prompt_hash,
                    json_dumps(source_assets_payload),
                    claim_hash,
                    parent_claim_hash,
                    actor.user_id,
                    created_at,
                    json_dumps(body.metadata),
                ),
            )
            row = await result.fetchone()
            await conn.commit()
        except Exception as exc:
            await conn.rollback()
            if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
                raise HTTPException(status_code=409, detail="Provenance manifest version already exists for this asset") from exc
            raise
    return _manifest_public(row)


@router.get("/projects/{project_id}/assets/{asset_id}/provenance")
async def get_latest_provenance_manifest(project_id: str, asset_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT m.id,m.workspace_id,m.project_id,m.asset_id,m.manifest_version,m.generator,m.prompt_hash,m.source_assets,m.claim_hash,m.parent_claim_hash,m.created_by,m.created_at,m.metadata
               FROM ag_design_provenance_manifest m
               JOIN ag_design_asset a ON a.id=m.asset_id
               WHERE m.asset_id=%s AND m.project_id=%s AND m.workspace_id=%s AND a.status='ready'
               ORDER BY m.created_at DESC LIMIT 1""",
            (asset_id, project_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Provenance manifest not found for asset")
    return _manifest_public(row)


@router.get("/projects/{project_id}/assets/{asset_id}/provenance/history")
async def list_provenance_history(project_id: str, asset_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT m.id,m.workspace_id,m.project_id,m.asset_id,m.manifest_version,m.generator,m.prompt_hash,m.source_assets,m.claim_hash,m.parent_claim_hash,m.created_by,m.created_at,m.metadata
               FROM ag_design_provenance_manifest m
               JOIN ag_design_asset a ON a.id=m.asset_id
               WHERE m.asset_id=%s AND m.project_id=%s AND m.workspace_id=%s AND a.status='ready'
               ORDER BY m.created_at ASC""",
            (asset_id, project_id, actor.workspace_id),
        )
        rows = await result.fetchall()
    return {
        "items": [_manifest_public(row) for row in rows],
        "data": [_manifest_public(row) for row in rows],
        "total": len(rows),
    }


@router.get("/provenance/{manifest_id}")
async def get_provenance_manifest(manifest_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT id,workspace_id,project_id,asset_id,manifest_version,generator,prompt_hash,source_assets,claim_hash,parent_claim_hash,created_by,created_at,metadata
               FROM ag_design_provenance_manifest WHERE id=%s AND workspace_id=%s""",
            (manifest_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Provenance manifest not found")
    return _manifest_public(row)


@router.post("/provenance/{manifest_id}/verify")
async def verify_provenance_manifest(manifest_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT id,workspace_id,project_id,asset_id,manifest_version,generator,prompt_hash,source_assets,claim_hash,parent_claim_hash,created_by,created_at,metadata
               FROM ag_design_provenance_manifest WHERE id=%s AND workspace_id=%s""",
            (manifest_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Provenance manifest not found")
    manifest = _manifest_public(row)
    generator = manifest.get("generator") or {}
    source_assets = manifest.get("source_assets") or []
    parent_claim_hash = manifest.get("parent_claim_hash")
    created_at_value = manifest.get("created_at")
    if hasattr(created_at_value, "isoformat"):
        created_at_iso = created_at_value.isoformat().replace("+00:00", "Z")
    else:
        created_at_iso = str(created_at_value)
    recomputed = _compute_claim_hash(
        asset_id=manifest["asset_id"],
        generator=generator,
        prompt_hash=manifest.get("prompt_hash", ""),
        source_assets=source_assets,
        parent_claim_hash=parent_claim_hash,
        created_at=created_at_iso,
    )
    if recomputed == manifest["claim_hash"]:
        return {"verified": True, "reason": "claim_hash matches recomputed hash", "manifest_id": manifest_id}
    return {
        "verified": False,
        "reason": "claim_hash does not match recomputed hash",
        "manifest_id": manifest_id,
        "expected": recomputed,
        "actual": manifest["claim_hash"],
    }


@router.get("/provenance")
async def list_provenance_manifests(
    actor: Annotated[Actor, Depends(get_actor)],
    workspace_id: str = Query(min_length=1, max_length=160),
    project_id: str | None = Query(default=None, max_length=160),
    asset_id: str | None = Query(default=None, max_length=160),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    _require(actor, "design:read")
    if workspace_id != actor.workspace_id:
        raise HTTPException(status_code=403, detail="Cannot list manifests outside your workspace")
    conditions = ["workspace_id=%s"]
    params: list[Any] = [actor.workspace_id]
    if project_id:
        conditions.append("project_id=%s")
        params.append(project_id)
    if asset_id:
        conditions.append("asset_id=%s")
        params.append(asset_id)
    where = " AND ".join(conditions)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""SELECT id,workspace_id,project_id,asset_id,manifest_version,generator,prompt_hash,source_assets,claim_hash,parent_claim_hash,created_by,created_at,metadata
                FROM ag_design_provenance_manifest WHERE {where}
                ORDER BY created_at DESC LIMIT %s OFFSET %s""",
            (*params, limit, offset),
        )
        rows = await result.fetchall()
        count_result = await conn.execute(
            f"SELECT COUNT(*) AS total FROM ag_design_provenance_manifest WHERE {where}",
            tuple(params),
        )
        total_row = await count_result.fetchone()
    return {
        "items": [_manifest_public(row) for row in rows],
        "data": [_manifest_public(row) for row in rows],
        "total": total_row["total"] if total_row else 0,
        "limit": limit,
        "offset": offset,
        "has_more": len(rows) == limit,
        "meta": {"request_id": None},
    }
