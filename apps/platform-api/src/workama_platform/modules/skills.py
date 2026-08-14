from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response, status
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from workama_platform.core import (
    Actor,
    capability_allows,
    get_actor,
    hash_secret,
    json_dumps,
    new_id,
    pool,
)


router = APIRouter(prefix="/api/v1/skills", tags=["skills"])
skill_installs_router = APIRouter(prefix="/api/v1/skill-installs", tags=["skill-installs"])

SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,48}(?::[a-z][a-z0-9._-]{0,48})?$")
SECRET_VALUE_PATTERN = re.compile(
    r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)\s*[:=]\s*[^\s,;]+"
)
# v7.164 T-M7-007 技能包格式增强
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
SPDX_LICENSE_PATTERN = re.compile(r"^[A-Za-z0-9.+-]{1,128}(?:\s+WITH\s+[A-Za-z0-9.+-]{1,64})?$")
ENTRYPOINT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*(?::[A-Za-z_][A-Za-z0-9_]*)+$")
URL_PATTERN = re.compile(r"^https?://[A-Za-z0-9._\-:/?#@!$&'()*+,;=%~]+$")
ENHANCED_RUNTIME_TYPES = ("python", "local_http", "mock")
SIGNING_STATUSES = ("unsigned", "signed", "invalid")
REVIEW_STATES = ("draft", "submitted", "reviewing", "approved", "rejected", "published")
RISK_LEVELS_V2 = ("low", "medium", "high")

RISK_LEVELS = ("low", "medium", "high", "critical")
REVIEW_STATUSES = ("pending", "needs_review", "approved", "rejected")
SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "access_token",
        "refresh_token",
        "client_secret",
        "password",
        "private_key",
        "secret",
        "token",
    }
)


MANIFEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "version", "publisher"],
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "name": {"type": "string", "pattern": IDENTIFIER_PATTERN.pattern},
        "version": {"type": "string", "pattern": SEMVER_PATTERN.pattern},
        "publisher": {"type": "string", "pattern": IDENTIFIER_PATTERN.pattern},
        "description": {"type": "string", "maxLength": 4000},
        "trigger_description": {"type": "string", "maxLength": 1000},
        "required_tools": {"type": "array", "maxItems": 32, "items": {"type": "string"}},
        "permissions": {"type": "array", "maxItems": 32, "items": {"type": "string"}},
        "files": {"type": "array", "maxItems": 256, "items": {"type": "string"}},
        "entrypoint": {"type": "string"},
        "risk_level": {"enum": list(RISK_LEVELS)},
    },
}


class SkillManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    schema_version: int = Field(
        default=1,
        ge=1,
        le=1,
        validation_alias=AliasChoices("schema_version", "schema"),
    )
    name: str = Field(min_length=2, max_length=64, pattern=IDENTIFIER_PATTERN.pattern)
    version: str = Field(
        min_length=5,
        max_length=128,
        pattern=SEMVER_PATTERN.pattern,
        validation_alias=AliasChoices("version", "semver"),
    )
    publisher: str = Field(min_length=2, max_length=64, pattern=IDENTIFIER_PATTERN.pattern)
    description: str = Field(default="", max_length=4000)
    trigger_description: str = Field(
        default="",
        max_length=1000,
        validation_alias=AliasChoices("trigger_description", "trigger"),
    )
    required_tools: list[str] = Field(
        default_factory=list,
        max_length=32,
        validation_alias=AliasChoices("required_tools", "tools"),
    )
    permissions: list[str] = Field(default_factory=list, max_length=32)
    files: list[str] = Field(
        default_factory=lambda: ["skill.yaml", "prompt.md"],
        max_length=256,
        validation_alias=AliasChoices("files", "package_files"),
    )
    entrypoint: str = Field(
        default="prompt.md",
        max_length=160,
        validation_alias=AliasChoices("entrypoint", "entry_point"),
    )
    risk_level: Literal["low", "medium", "high", "critical"] | None = None

    @field_validator("required_tools", "permissions")
    @classmethod
    def validate_capabilities(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            value = value.strip().lower()
            if not value or not CAPABILITY_PATTERN.fullmatch(value):
                raise ValueError("tools and permissions must use a capability name")
            if value not in normalized:
                normalized.append(value)
        return sorted(normalized)

    @field_validator("files")
    @classmethod
    def validate_files(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            value = validate_package_path(value)
            if value not in normalized:
                normalized.append(value)
        if "skill.yaml" not in normalized or "prompt.md" not in normalized:
            raise ValueError("skill package must contain skill.yaml and prompt.md")
        return sorted(normalized)

    @field_validator("entrypoint")
    @classmethod
    def validate_entrypoint(cls, value: str) -> str:
        return validate_package_path(value)

    @model_validator(mode="after")
    def validate_entrypoint_membership(self) -> "SkillManifest":
        if self.entrypoint not in self.files:
            raise ValueError("entrypoint must be declared in files")
        if self.entrypoint != "prompt.md" and not self.entrypoint.startswith("scripts/"):
            raise ValueError("entrypoint must be prompt.md or a scripts path")
        return self


class SkillInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    artifact_ref: str = Field(min_length=1, max_length=240)
    manifest: dict[str, Any] | None = None
    content_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN.pattern)


class SkillStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int | None = Field(default=None, ge=1)


class SkillReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    review_status: Literal["pending", "needs_review", "approved", "rejected"]
    risk_level: Literal["low", "medium", "high", "critical"] | None = None
    reason: str = Field(default="", max_length=1000)
    expected_revision: int | None = Field(default=None, ge=1)


class SkillInstallPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    grants: dict[str, Any] | None = None
    expected_version: int | None = Field(default=None, ge=1)


@dataclass(frozen=True)
class ArtifactReference:
    kind: Literal["mock", "local"]
    name: str | None = None
    version: str | None = None
    publisher: str | None = None
    artifact_id: str | None = None


@dataclass(frozen=True)
class ResolvedPackage:
    artifact_ref: str
    manifest: dict[str, Any]
    content_sha256: str
    source_kind: Literal["mock", "local"]
    artifact_id: str | None = None


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS ag_skill (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      publisher TEXT NOT NULL,
      name TEXT NOT NULL,
      semver TEXT NOT NULL,
      manifest JSONB NOT NULL,
      artifact_ref TEXT NOT NULL,
      source_kind TEXT NOT NULL CHECK (source_kind IN ('mock','local')),
      content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
      signature_status TEXT NOT NULL DEFAULT 'not_verified'
        CHECK (signature_status IN ('not_verified','verified','invalid','unsupported')),
      risk_level TEXT NOT NULL DEFAULT 'low'
        CHECK (risk_level IN ('low','medium','high','critical')),
      review_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (review_status IN ('pending','needs_review','approved','rejected')),
      status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','blocked','revoked')),
      review_reason TEXT NOT NULL DEFAULT '',
      revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id, publisher, name, semver)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ag_skill_install (
      id TEXT PRIMARY KEY,
      skill_id TEXT NOT NULL REFERENCES ag_skill(id) ON DELETE CASCADE,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      enabled BOOLEAN NOT NULL DEFAULT FALSE,
      status TEXT NOT NULL DEFAULT 'disabled'
        CHECK (status IN ('disabled','enabled','blocked')),
      grants JSONB NOT NULL DEFAULT '{}'::jsonb,
      idempotency_key_hash TEXT NOT NULL,
      input_hash TEXT NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
      version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
      installed_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id, skill_id),
      UNIQUE(workspace_id, idempotency_key_hash)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ag_skill_workspace_status ON ag_skill(workspace_id, status, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ag_skill_install_workspace_state ON ag_skill_install(workspace_id, enabled, updated_at DESC)",
    # v7.164 T-M7-007 技能包格式增强（与迁移 075_skill_format_enhance.sql 同步）
    "ALTER TABLE ag_skill ADD COLUMN IF NOT EXISTS content_hash TEXT",
    "ALTER TABLE ag_skill ADD COLUMN IF NOT EXISTS license TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE ag_skill ADD COLUMN IF NOT EXISTS permissions JSONB NOT NULL DEFAULT '[]'::jsonb",
    "ALTER TABLE ag_skill ADD COLUMN IF NOT EXISTS runtime TEXT NOT NULL DEFAULT 'python'",
    "ALTER TABLE ag_skill ADD COLUMN IF NOT EXISTS signing_status TEXT NOT NULL DEFAULT 'unsigned'",
    "ALTER TABLE ag_skill ADD COLUMN IF NOT EXISTS signing_key_fingerprint TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE ag_skill ADD COLUMN IF NOT EXISTS risk_score INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE ag_skill ADD COLUMN IF NOT EXISTS review_state TEXT NOT NULL DEFAULT 'draft'",
    """
    CREATE TABLE IF NOT EXISTS ag_skill_review (
      id TEXT PRIMARY KEY,
      skill_id TEXT NOT NULL,
      workspace_id TEXT NOT NULL,
      reviewer_id TEXT,
      action TEXT NOT NULL
        CHECK (action IN ('submit','approve','reject','request_changes','publish','start_review')),
      notes TEXT NOT NULL DEFAULT '',
      risk_score INTEGER,
      risk_level TEXT CHECK (risk_level IS NULL OR risk_level IN ('low','medium','high','critical')),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ag_skill_review_skill ON ag_skill_review(skill_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ag_skill_review_workspace ON ag_skill_review(workspace_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS ag_skill_signature (
      id TEXT PRIMARY KEY,
      skill_id TEXT NOT NULL,
      algorithm TEXT NOT NULL DEFAULT 'ed25519',
      signature_bytes TEXT NOT NULL,
      signing_key_fingerprint TEXT NOT NULL,
      signed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      verified_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ag_skill_signature_skill ON ag_skill_signature(skill_id, signed_at DESC)",
    # 技能市场（Marketplace）：发布 listing / 评分 / 版本快照
    """
    CREATE TABLE IF NOT EXISTS skill_marketplace_listing (
      skill_id TEXT PRIMARY KEY REFERENCES ag_skill(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      category TEXT NOT NULL DEFAULT '',
      tags TEXT[] NOT NULL DEFAULT '{}',
      summary TEXT NOT NULL DEFAULT '',
      listing_status TEXT NOT NULL DEFAULT 'draft'
        CHECK (listing_status IN ('draft','published','unlisted','archived')),
      published_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skill_rating (
      id TEXT PRIMARY KEY,
      skill_id TEXT NOT NULL REFERENCES ag_skill(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      user_id TEXT NOT NULL REFERENCES id_user(id),
      score INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
      review_text TEXT NOT NULL DEFAULT '',
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(skill_id, workspace_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skill_version (
      id TEXT PRIMARY KEY,
      skill_id TEXT NOT NULL REFERENCES ag_skill(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      version INTEGER NOT NULL CHECK (version > 0),
      manifest_snapshot JSONB NOT NULL,
      changelog TEXT NOT NULL DEFAULT '',
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_skill_marketplace_listing_status ON skill_marketplace_listing(listing_status, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_skill_rating_skill ON skill_rating(skill_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_skill_version_skill ON skill_version(skill_id, version DESC)",
)


async def ensure_skills_schema(conn) -> None:
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _contains_sensitive_key(value: Any, path: str = "manifest") -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).strip().lower().replace("-", "_")
            if _is_sensitive_key(key_text):
                return f"{path}.{key}"
            found = _contains_sensitive_key(item, f"{path}.{key}")
            if found:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = _contains_sensitive_key(item, f"{path}[{index}]")
            if found:
                return found
    elif isinstance(value, str) and SECRET_VALUE_PATTERN.search(value):
        return path
    return None


def _is_sensitive_key(value: str) -> bool:
    return value in SENSITIVE_KEYS or any(token in value for token in ("secret", "password", "token"))


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>"
                if _is_sensitive_key(str(key).strip().lower().replace("-", "_"))
                else redact_sensitive(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


def redact_sensitive_text(value: str) -> str:
    return SECRET_VALUE_PATTERN.sub("<redacted>", value.replace("\x00", " "))


def validate_package_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("package path must be a string")
    path = value.strip()
    if not path or path != value or "\\" in path or "\x00" in path or ":" in path:
        raise ValueError("package path is unsafe")
    if path.startswith("/") or path.startswith(".") or "//" in path:
        raise ValueError("package path is unsafe")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("package path traversal is not allowed")
    if path in {"skill.yaml", "prompt.md", "README.md"}:
        return path
    if parts[0] not in {"scripts", "resources"} or len(parts) < 2:
        raise ValueError("package files must be under scripts/ or resources/")
    if any(not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in parts[1:]):
        raise ValueError("package path contains an unsafe segment")
    return path


def _validate_sensitive_manifest(value: Any) -> None:
    found = _contains_sensitive_key(value)
    if found:
        raise ValueError(f"manifest contains a secret-like field at {found}")


def validate_manifest(value: dict[str, Any] | SkillManifest) -> dict[str, Any]:
    if isinstance(value, SkillManifest):
        manifest = value
    else:
        if not isinstance(value, dict):
            raise ValueError("skill manifest must be an object")
        _validate_sensitive_manifest(value)
        try:
            manifest = SkillManifest.model_validate(value)
        except ValidationError as exc:
            raise ValueError(f"invalid skill manifest: {exc.errors()[0]['msg']}") from exc
    normalized = manifest.model_dump()
    normalized["required_tools"] = sorted(set(normalized["required_tools"]))
    normalized["permissions"] = sorted(set(normalized["permissions"]))
    normalized["files"] = sorted(set(normalized["files"]))
    return normalized


def _risk_rank(value: str) -> int:
    return RISK_LEVELS.index(value)


def compute_risk_level(manifest: dict[str, Any] | SkillManifest) -> str:
    normalized = validate_manifest(manifest)
    risk = normalized.get("risk_level") or "low"
    for permission in normalized.get("permissions", []):
        if permission.startswith(("secret", "credential", "shell", "code")):
            candidate = "critical"
        elif permission.startswith(("network", "external", "filesystem:write", "file:write")):
            candidate = "high"
        elif permission.endswith(":write") or permission.endswith(":send"):
            candidate = "medium"
        else:
            candidate = "low"
        if _risk_rank(candidate) > _risk_rank(risk):
            risk = candidate
    return risk


def validate_artifact_reference(value: str) -> ArtifactReference:
    if not isinstance(value, str):
        raise ValueError("artifact_ref must be a string")
    raw = value.strip()
    if raw != value or not raw or "\\" in raw or "\x00" in raw or "%" in raw:
        raise ValueError("artifact_ref is unsafe")
    parsed = urlsplit(raw)
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("artifact_ref must not contain query, fragment, or credentials")
    host = parsed.netloc.lower()
    segments = parsed.path.split("/")[1:] if parsed.path.startswith("/") else []
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError("artifact_ref path traversal is not allowed")
    if parsed.scheme == "mock" and host in {"skill", "skills"}:
        if len(segments) == 3:
            publisher, name, version = segments
        elif len(segments) == 2 and host == "skills":
            publisher, name, version = "mock", segments[0], segments[1]
        else:
            raise ValueError("mock skill artifact_ref must identify publisher/name/version")
        if not IDENTIFIER_PATTERN.fullmatch(publisher) or not IDENTIFIER_PATTERN.fullmatch(name):
            raise ValueError("mock artifact_ref contains an unsafe identifier")
        if not SEMVER_PATTERN.fullmatch(version):
            raise ValueError("mock artifact_ref contains an invalid version")
        return ArtifactReference("mock", name=name, version=version, publisher=publisher)
    if parsed.scheme == "local" and host == "artifact" and len(segments) == 1 and TOKEN_PATTERN.fullmatch(segments[0]):
        return ArtifactReference("local", artifact_id=segments[0])
    raise ValueError("only mock://skill(s)/... and local://artifact/<id> references are allowed")


def _default_mock_manifest(reference: ArtifactReference) -> dict[str, Any]:
    return validate_manifest(
        {
            "schema_version": 1,
            "name": reference.name,
            "version": reference.version,
            "publisher": reference.publisher or "mock",
            "description": "Deterministic WorkAMA mock skill package",
            "trigger_description": "Use for deterministic local validation only",
            "required_tools": [],
            "permissions": [],
            "files": ["skill.yaml", "prompt.md"],
            "entrypoint": "prompt.md",
        }
    )


def skill_content_hash(artifact_ref: str, manifest: dict[str, Any]) -> str:
    return canonical_hash({"artifact_ref": artifact_ref, "manifest": validate_manifest(manifest)})


def _safe_local_object_ref(value: Any, workspace_id: str) -> str:
    if not isinstance(value, str) or not value.startswith(f"artifacts/{workspace_id}/"):
        raise HTTPException(status_code=422, detail="local artifact is outside the workspace artifact prefix")
    if "\\" in value or ".." in value.split("/") or "//" in value:
        raise HTTPException(status_code=422, detail="local artifact object path is unsafe")
    return value


async def resolve_package_reference(
    conn,
    *,
    workspace_id: str,
    artifact_ref: str,
    manifest_override: dict[str, Any] | None = None,
    expected_content_sha256: str | None = None,
) -> ResolvedPackage:
    try:
        reference = validate_artifact_reference(artifact_ref)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if reference.kind == "mock":
        try:
            manifest = validate_manifest(manifest_override) if manifest_override is not None else _default_mock_manifest(reference)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if manifest["name"] != reference.name or manifest["version"] != reference.version:
            raise HTTPException(status_code=409, detail="manifest identity does not match artifact_ref")
        if reference.publisher != "mock" and manifest["publisher"] != reference.publisher:
            raise HTTPException(status_code=409, detail="manifest publisher does not match artifact_ref")
        digest = skill_content_hash(artifact_ref, manifest)
        if expected_content_sha256 and expected_content_sha256 != digest:
            raise HTTPException(status_code=409, detail="content_sha256 does not match the mock package")
        return ResolvedPackage(artifact_ref, manifest, digest, "mock")

    result = await conn.execute(
        """
        SELECT id, workspace_id, kind, s3_key, content_sha256, preview
        FROM ag_artifact
        WHERE id=%s AND workspace_id=%s AND deleted_at IS NULL
        """,
        (reference.artifact_id, workspace_id),
    )
    artifact = await result.fetchone()
    if not artifact:
        raise HTTPException(status_code=404, detail="Local skill artifact was not found in this workspace")
    if artifact.get("kind") not in {"skill", "skill_package"}:
        raise HTTPException(status_code=422, detail="local artifact is not a skill package")
    _safe_local_object_ref(artifact.get("s3_key"), workspace_id)
    preview = artifact.get("preview") or {}
    if isinstance(preview, str):
        try:
            preview = json.loads(preview)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=422, detail="local skill artifact metadata is invalid") from exc
    try:
        manifest = validate_manifest(preview.get("manifest"))
    except (AttributeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="local skill artifact has no valid manifest") from exc
    if manifest_override is not None:
        try:
            requested_manifest = validate_manifest(manifest_override)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if requested_manifest != manifest:
            raise HTTPException(status_code=409, detail="manifest does not match local artifact metadata")
    stored_digest = str(artifact.get("content_sha256") or "").lower()
    digest = stored_digest if SHA256_PATTERN.fullmatch(stored_digest) else skill_content_hash(artifact_ref, manifest)
    if expected_content_sha256 and expected_content_sha256 != digest:
        raise HTTPException(status_code=409, detail="content_sha256 does not match the local package")
    return ResolvedPackage(artifact_ref, manifest, digest, "local", reference.artifact_id)


def _hash_idempotency_key(value: str) -> str:
    return hash_secret(value)


def _normalize_idempotency_key(value: str | None, content_sha256: str) -> tuple[str, str]:
    key = value.strip() if value else f"skill-install:{content_sha256}"
    if not key or len(key) > 128 or any(char.isspace() for char in key):
        raise HTTPException(status_code=422, detail="Idempotency-Key must be 1-128 non-space characters")
    return key, _hash_idempotency_key(key)


def check_idempotency_replay(existing_input_hash: str | None, requested_input_hash: str) -> None:
    if existing_input_hash and existing_input_hash != requested_input_hash:
        raise HTTPException(status_code=409, detail="Idempotency-Key was used for a different skill package")


def check_skill_version_content(existing_content_sha256: str, requested_content_sha256: str) -> None:
    if existing_content_sha256 != requested_content_sha256:
        raise HTTPException(status_code=409, detail="Skill version already exists with different content")


def _require(actor: Actor, action: Literal["read", "write", "review"]) -> None:
    required = f"skill:{action}"
    if capability_allows(actor.capabilities, required) or capability_allows(actor.capabilities, f"skills:{action}"):
        return
    if actor.actor_type != "user":
        raise HTTPException(status_code=403, detail=f"Missing capability: {required}")
    if action == "read" and actor.role in {"owner", "admin", "member", "viewer"}:
        return
    if action == "write" and actor.role in {"owner", "admin", "member"}:
        return
    if action == "review" and actor.role in {"owner", "admin"}:
        return
    raise HTTPException(status_code=403, detail=f"Missing capability: {required}")


def _require_install(actor: Actor, action: Literal["read", "write"]) -> None:
    required = f"skill:{action}"
    if capability_allows(actor.capabilities, required) or capability_allows(actor.capabilities, "skill:install"):
        return
    if actor.actor_type != "user":
        raise HTTPException(status_code=403, detail=f"Missing capability: skill:install")
    if action == "read" and actor.role in {"owner", "admin", "member", "viewer"}:
        return
    if action == "write" and actor.role in {"owner", "admin", "member"}:
        return
    raise HTTPException(status_code=403, detail=f"Missing capability: skill:install")


def _parse_expected_version(if_match: str | None, body_version: int | None, *, field: str) -> int | None:
    if if_match is None:
        return body_version
    value = if_match.strip()
    if value == "*":
        return None
    match = re.fullmatch(r'(?:W/)?"?(\d+)"?', value)
    if not match:
        raise HTTPException(status_code=422, detail=f"{field} version header is invalid")
    return int(match.group(1))


def _check_version(actual: int, expected: int | None, *, resource: str) -> None:
    if expected is not None and actual != expected:
        raise HTTPException(status_code=412, detail=f"{resource} version does not match")


def _skill_view(skill: dict[str, Any], installation: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = skill.get("manifest") or {}
    if isinstance(manifest, str):
        manifest = json.loads(manifest)
    result: dict[str, Any] = {
        "id": skill["id"],
        "workspace_id": skill["workspace_id"],
        "publisher": skill["publisher"],
        "name": skill["name"],
        "version": skill["semver"],
        "semver": skill["semver"],
        "manifest": redact_sensitive(manifest),
        "artifact_ref": skill["artifact_ref"],
        "source_kind": skill["source_kind"],
        "content_sha256": skill["content_sha256"],
        "signature_status": skill["signature_status"],
        "risk_level": skill["risk_level"],
        "review_status": skill["review_status"],
        "review_reason": skill.get("review_reason", ""),
        "status": skill["status"],
        "revision": skill.get("revision", 1),
        "created_at": skill.get("created_at"),
        "updated_at": skill.get("updated_at"),
    }
    if installation is not None:
        result["installation"] = {
            "id": installation["id"],
            "enabled": installation["enabled"],
            "status": installation["status"],
            "version": installation["version"],
            "created_at": installation.get("created_at"),
            "updated_at": installation.get("updated_at"),
        }
    return result


def _skill_response(view: dict[str, Any], **extras: Any) -> dict[str, Any]:
    """Build a SkillDTO response per Contract《720》.

    Single-resource operations (install/get/enable/disable/review) are specified
    to return a bare ``SkillDTO``. Source historically wrapped it as
    ``{"skill": ...}``. To fix the drift without breaking existing clients we
    expose the SkillDTO fields at the top level and retain the legacy ``skill``
    wrapper alongside any extra backward-compatible keys.
    """
    response: dict[str, Any] = {**view, "skill": view}
    response.update(extras)
    return response


def _join_view(row: dict[str, Any]) -> dict[str, Any]:
    skill = {key: row[key] for key in (
        "skill_id", "workspace_id", "publisher", "name", "semver", "manifest", "artifact_ref",
        "source_kind", "content_sha256", "signature_status", "risk_level", "review_status", "review_reason",
        "skill_status", "skill_revision", "skill_created_at", "skill_updated_at",
    ) if key in row}
    skill["id"] = skill.pop("skill_id")
    skill["status"] = skill.pop("skill_status")
    skill["revision"] = skill.pop("skill_revision")
    skill["created_at"] = skill.pop("skill_created_at")
    skill["updated_at"] = skill.pop("skill_updated_at")
    installation = {key: row[key] for key in (
        "installation_id", "enabled", "install_status", "install_version", "install_created_at", "install_updated_at",
    ) if key in row}
    installation["id"] = installation.pop("installation_id")
    installation["status"] = installation.pop("install_status")
    installation["version"] = installation.pop("install_version")
    installation["created_at"] = installation.pop("install_created_at")
    installation["updated_at"] = installation.pop("install_updated_at")
    return _skill_view(skill, installation)


async def _get_skill(conn, skill_id: str, workspace_id: str, *, for_update: bool = False) -> dict[str, Any]:
    lock = " FOR UPDATE" if for_update else ""
    result = await conn.execute(
        f"SELECT * FROM ag_skill WHERE id=%s AND workspace_id=%s AND status <> 'revoked'{lock}",
        (skill_id, workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Skill was not found in this workspace")
    return row


async def _get_installation(conn, skill_id: str, workspace_id: str, *, for_update: bool = False) -> dict[str, Any]:
    lock = " FOR UPDATE" if for_update else ""
    result = await conn.execute(
        f"SELECT * FROM ag_skill_install WHERE skill_id=%s AND workspace_id=%s{lock}",
        (skill_id, workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Skill is not installed in this workspace")
    return row


async def _emit_skill_event(conn, *, event_type: str, skill: dict[str, Any], installation: dict[str, Any] | None = None) -> None:
    payload = {
        "skill_id": skill["id"],
        "name": skill["name"],
        "publisher": skill["publisher"],
        "semver": skill["semver"],
        "content_sha256": skill["content_sha256"],
        "risk_level": skill["risk_level"],
        "review_status": skill["review_status"],
        "signature_status": skill["signature_status"],
    }
    if installation:
        payload.update({
            "installation_id": installation["id"],
            "enabled": installation["enabled"],
            "installation_status": installation["status"],
            "installation_version": installation["version"],
        })
    await conn.execute(
        "INSERT INTO ops_outbox(id,event_type,workspace_id,trace_id,payload) VALUES (%s,%s,%s,%s,%s::jsonb)",
        (new_id("out"), event_type, skill["workspace_id"], skill["id"], json_dumps(payload)),
    )


@router.get("")
async def list_skills(
    actor: Annotated[Actor, Depends(get_actor)],
    enabled: bool | None = None,
    review_status: Literal["pending", "needs_review", "approved", "rejected"] | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    _require(actor, "read")
    predicates = ["s.workspace_id=%s", "i.workspace_id=%s", "s.status <> 'revoked'"]
    params: list[Any] = [actor.workspace_id, actor.workspace_id]
    if enabled is not None:
        predicates.append("i.enabled=%s")
        params.append(enabled)
    if review_status is not None:
        predicates.append("s.review_status=%s")
        params.append(review_status)
    params.append(limit)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT s.id AS skill_id, s.workspace_id, s.publisher, s.name, s.semver, s.manifest,
              s.artifact_ref, s.source_kind, s.content_sha256, s.signature_status, s.risk_level,
              s.review_status, s.review_reason, s.status AS skill_status, s.revision AS skill_revision,
              s.created_at AS skill_created_at, s.updated_at AS skill_updated_at,
              i.id AS installation_id, i.enabled, i.status AS install_status, i.version AS install_version,
              i.created_at AS install_created_at, i.updated_at AS install_updated_at
            FROM ag_skill s JOIN ag_skill_install i ON i.skill_id=s.id AND i.workspace_id=s.workspace_id
            WHERE {' AND '.join(predicates)}
            ORDER BY s.updated_at DESC, s.id DESC LIMIT %s
            """,
            tuple(params),
        )
        rows = await result.fetchall()
    data = [_join_view(row) for row in rows]
    # Contract《720》listSkills: ListQuery -> ListResponse<SkillDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None},
    }


@router.post("/install", status_code=status.HTTP_201_CREATED)
async def install_skill(
    body: SkillInstallRequest,
    actor: Annotated[Actor, Depends(get_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            package = await resolve_package_reference(
                conn,
                workspace_id=actor.workspace_id,
                artifact_ref=body.artifact_ref,
                manifest_override=body.manifest,
                expected_content_sha256=body.content_sha256,
            )
            _, idempotency_hash = _normalize_idempotency_key(idempotency_key, package.content_sha256)
            input_hash = canonical_hash({
                "artifact_ref": package.artifact_ref,
                "manifest": package.manifest,
                "content_sha256": package.content_sha256,
            })
            existing_result = await conn.execute(
                """
                SELECT i.id AS installation_id, i.skill_id, i.workspace_id, i.enabled,
                  i.status AS install_status, i.version AS install_version,
                  i.input_hash,
                  i.created_at AS install_created_at, i.updated_at AS install_updated_at,
                  s.id AS skill_id, s.publisher, s.name, s.semver, s.manifest, s.artifact_ref,
                  s.source_kind, s.content_sha256, s.signature_status, s.risk_level,
                  s.review_status, s.review_reason, s.status AS skill_status, s.revision AS skill_revision,
                  s.created_at AS skill_created_at, s.updated_at AS skill_updated_at
                FROM ag_skill_install i JOIN ag_skill s ON s.id=i.skill_id
                WHERE i.workspace_id=%s AND i.idempotency_key_hash=%s
                """,
                (actor.workspace_id, idempotency_hash),
            )
            existing = await existing_result.fetchone()
            if existing:
                check_idempotency_replay(existing.get("input_hash"), input_hash)
                return _skill_response(_join_view(existing), deduplicated=True)

            risk_level = compute_risk_level(package.manifest)
            skill_result = await conn.execute(
                """
                INSERT INTO ag_skill(
                  id,org_id,workspace_id,publisher,name,semver,manifest,artifact_ref,source_kind,
                  content_sha256,signature_status,risk_level,review_status,status,created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,'not_verified',%s,'pending','active',%s)
                ON CONFLICT(workspace_id,publisher,name,semver) DO NOTHING RETURNING *
                """,
                (
                    new_id("skill"), actor.org_id, actor.workspace_id, package.manifest["publisher"],
                    package.manifest["name"], package.manifest["version"], json_dumps(package.manifest),
                    package.artifact_ref, package.source_kind, package.content_sha256, risk_level, actor.user_id,
                ),
            )
            skill = await skill_result.fetchone()
            if not skill:
                skill_result = await conn.execute(
                    "SELECT * FROM ag_skill WHERE workspace_id=%s AND publisher=%s AND name=%s AND semver=%s FOR UPDATE",
                    (actor.workspace_id, package.manifest["publisher"], package.manifest["name"], package.manifest["version"]),
                )
                skill = await skill_result.fetchone()
            if not skill:
                raise HTTPException(status_code=409, detail="Skill version could not be resolved")
            check_skill_version_content(skill["content_sha256"], package.content_sha256)

            install_result = await conn.execute(
                """
                INSERT INTO ag_skill_install(
                  id,skill_id,org_id,workspace_id,enabled,status,grants,idempotency_key_hash,input_hash,installed_by
                ) VALUES (%s,%s,%s,%s,FALSE,'disabled','{}'::jsonb,%s,%s,%s)
                ON CONFLICT(workspace_id,skill_id) DO NOTHING RETURNING *
                """,
                (new_id("skillinst"), skill["id"], actor.org_id, actor.workspace_id, idempotency_hash, input_hash, actor.user_id),
            )
            installation = await install_result.fetchone()
            deduplicated = False
            if not installation:
                installation = await _get_installation(conn, skill["id"], actor.workspace_id)
                deduplicated = True
            else:
                await _emit_skill_event(conn, event_type="skill.installed.v1", skill=skill, installation=installation)
    # Contract《720》installSkill: SkillInstallRequest -> SkillDTO
    return _skill_response(_skill_view(skill, installation), deduplicated=deduplicated)


@router.get("/marketplace")
async def list_marketplace_skills(
    actor: Annotated[Actor, Depends(get_actor)],
    category: str | None = Query(default=None, max_length=64),
    tag: str | None = Query(default=None, max_length=64),
    search: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=512),
) -> dict[str, Any]:
    """GET /api/v1/skills/marketplace - 市场列表（跨 workspace 可见已发布技能）。

    注意：本路由必须注册在 ``GET /{skill_id}`` 之前，否则字面路径 ``/marketplace``
    会被路径参数 ``{skill_id}`` 捕获（Starlette 按注册顺序匹配）。
    """
    _require(actor, "read")
    offset = _decode_cursor(cursor) if cursor else 0
    predicates = ["l.listing_status='published'", "s.status <> 'revoked'"]
    params: list[Any] = []
    if category:
        predicates.append("l.category=%s")
        params.append(category)
    if tag:
        predicates.append("%s = ANY(l.tags)")
        params.append(tag)
    if search:
        predicates.append("(s.name ILIKE %s OR l.summary ILIKE %s OR s.publisher ILIKE %s)")
        like = f"%{search}%"
        params.extend([like, like, like])
    params.extend([limit, offset])
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT l.skill_id, l.workspace_id, l.category, l.tags, l.summary,
                   l.listing_status, l.published_at, l.created_at, l.updated_at,
                   s.name, s.publisher, s.semver, s.risk_level, s.review_status
            FROM skill_marketplace_listing l
            JOIN ag_skill s ON s.id = l.skill_id
            WHERE {' AND '.join(predicates)}
            ORDER BY l.updated_at DESC, l.skill_id DESC
            LIMIT %s OFFSET %s
            """,
            tuple(params),
        )
        rows = await result.fetchall()
    items = [_marketplace_view(row) for row in rows]
    has_more = len(rows) == limit
    next_cursor = _encode_cursor(offset + limit) if has_more else None
    return {
        "items": items,
        "data": items,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "meta": {"request_id": None},
    }


@router.get("/{skill_id}")
async def get_skill(skill_id: str, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    _require(actor, "read")
    async with pool.connection() as conn:
        skill = await _get_skill(conn, skill_id, actor.workspace_id)
        installation = await _get_installation(conn, skill_id, actor.workspace_id)
    # Contract《720》getSkill: PathId -> SkillDTO
    return _skill_response(_skill_view(skill, installation))


async def _change_install_state(
    skill_id: str,
    *,
    actor: Actor,
    enabled: bool,
    body: SkillStateRequest | None,
    if_match: str | None,
) -> dict[str, Any]:
    _require(actor, "write")
    expected = _parse_expected_version(if_match, body.expected_version if body else None, field="installation")
    async with pool.connection() as conn:
        async with conn.transaction():
            skill = await _get_skill(conn, skill_id, actor.workspace_id, for_update=True)
            installation = await _get_installation(conn, skill_id, actor.workspace_id, for_update=True)
            _check_version(installation["version"], expected, resource="Skill installation")
            if enabled:
                if skill["review_status"] != "approved":
                    raise HTTPException(
                        status_code=409,
                        detail=f"Skill must be approved before enabling (review_status={skill['review_status']})",
                    )
                if skill["status"] != "active":
                    raise HTTPException(status_code=409, detail="Skill is blocked or revoked")
            if installation["enabled"] == enabled:
                return _skill_response(_skill_view(skill, installation), changed=False)
            target_status = "enabled" if enabled else "disabled"
            result = await conn.execute(
                """
                UPDATE ag_skill_install SET enabled=%s,status=%s,version=version+1,updated_at=now()
                WHERE id=%s AND workspace_id=%s RETURNING *
                """,
                (enabled, target_status, installation["id"], actor.workspace_id),
            )
            installation = await result.fetchone()
            await _emit_skill_event(
                conn,
                event_type="skill.enabled.v1" if enabled else "skill.disabled.v1",
                skill=skill,
                installation=installation,
            )
    # Contract《720》enableSkill/disableSkill: PathId -> SkillDTO
    return _skill_response(_skill_view(skill, installation), changed=True)


@router.post("/{skill_id}/enable")
async def enable_skill(
    skill_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    body: SkillStateRequest | None = Body(default=None),
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    return await _change_install_state(skill_id, actor=actor, enabled=True, body=body, if_match=if_match)


@router.post("/{skill_id}/disable")
async def disable_skill(
    skill_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    body: SkillStateRequest | None = Body(default=None),
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    return await _change_install_state(skill_id, actor=actor, enabled=False, body=body, if_match=if_match)


@router.post("/{skill_id}/review")
async def review_skill(
    skill_id: str,
    body: SkillReviewRequest,
    actor: Annotated[Actor, Depends(get_actor)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    _require(actor, "review")
    expected = _parse_expected_version(if_match, body.expected_revision, field="skill")
    async with pool.connection() as conn:
        async with conn.transaction():
            skill = await _get_skill(conn, skill_id, actor.workspace_id, for_update=True)
            _check_version(skill["revision"], expected, resource="Skill")
            risk_level = compute_risk_level(skill["manifest"])
            if body.risk_level and _risk_rank(body.risk_level) > _risk_rank(risk_level):
                risk_level = body.risk_level
            reason = redact_sensitive_text(body.reason).strip()
            result = await conn.execute(
                """
                UPDATE ag_skill SET review_status=%s,risk_level=%s,status=%s,review_reason=%s,
                  revision=revision+1,updated_at=now()
                WHERE id=%s AND workspace_id=%s RETURNING *
                """,
                (
                    body.review_status, risk_level, "active" if body.review_status != "rejected" else "blocked",
                    reason[:1000], skill_id, actor.workspace_id,
                ),
            )
            skill = await result.fetchone()
            install_result = await conn.execute(
                """
                UPDATE ag_skill_install SET enabled=FALSE,
                  status=CASE WHEN %s='rejected' THEN 'blocked' ELSE 'disabled' END,
                  version=version+1,updated_at=now()
                WHERE skill_id=%s AND workspace_id=%s AND %s <> 'approved' RETURNING *
                """,
                (body.review_status, skill_id, actor.workspace_id, body.review_status),
            )
            installation = await install_result.fetchone()
            await _emit_skill_event(conn, event_type="skill.reviewed.v1", skill=skill, installation=installation)
    # Contract《720》reviewSkill: SkillReviewRequest -> SkillDTO
    return _skill_response(_skill_view(skill, installation))


@skill_installs_router.get("")
async def list_skill_installs(
    actor: Annotated[Actor, Depends(get_actor)],
    enabled: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    _require_install(actor, "read")
    predicates = ["s.workspace_id=%s", "i.workspace_id=%s", "s.status <> 'revoked'"]
    params: list[Any] = [actor.workspace_id, actor.workspace_id]
    if enabled is not None:
        predicates.append("i.enabled=%s")
        params.append(enabled)
    params.append(limit)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT s.id AS skill_id, s.workspace_id, s.publisher, s.name, s.semver, s.manifest,
              s.artifact_ref, s.source_kind, s.content_sha256, s.signature_status, s.risk_level,
              s.review_status, s.review_reason, s.status AS skill_status, s.revision AS skill_revision,
              s.created_at AS skill_created_at, s.updated_at AS skill_updated_at,
              i.id AS installation_id, i.enabled, i.status AS install_status, i.version AS install_version,
              i.created_at AS install_created_at, i.updated_at AS install_updated_at
            FROM ag_skill s JOIN ag_skill_install i ON i.skill_id=s.id AND i.workspace_id=s.workspace_id
            WHERE {' AND '.join(predicates)}
            ORDER BY s.updated_at DESC, s.id DESC LIMIT %s
            """,
            tuple(params),
        )
        rows = await result.fetchall()
    data = [_join_view(row) for row in rows]
    # Contract《720》listSkillInstalls: ListQuery -> ListResponse<SkillDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None},
    }


@skill_installs_router.post("", status_code=status.HTTP_201_CREATED)
async def create_skill_install(
    body: SkillInstallRequest,
    actor: Annotated[Actor, Depends(get_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    _require_install(actor, "write")
    return await install_skill(body, actor, idempotency_key)


@skill_installs_router.patch("/{install_id}")
async def update_skill_install(
    install_id: str,
    body: SkillInstallPatch,
    actor: Annotated[Actor, Depends(get_actor)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    _require_install(actor, "write")
    expected = _parse_expected_version(if_match, body.expected_version, field="installation")
    async with pool.connection() as conn:
        async with conn.transaction():
            install_result = await conn.execute(
                """
                SELECT i.*, s.review_status, s.status AS skill_status
                FROM ag_skill_install i JOIN ag_skill s ON s.id=i.skill_id
                WHERE i.id=%s AND i.workspace_id=%s FOR UPDATE
                """,
                (install_id, actor.workspace_id),
            )
            installation = await install_result.fetchone()
            if not installation:
                raise HTTPException(status_code=404, detail="Skill installation not found")
            _check_version(installation["version"], expected, resource="Skill installation")
            if body.enabled is not None:
                if body.enabled and installation["skill_status"] != "active":
                    raise HTTPException(status_code=409, detail="Skill is not active")
                if body.enabled and installation["review_status"] != "approved":
                    raise HTTPException(status_code=409, detail="Skill must be approved before enabling")
            if all(value is None for value in (body.enabled, body.grants)):
                skill = await _get_skill(conn, installation["skill_id"], actor.workspace_id)
                return {"skill": _skill_view(skill, installation)}
            target_status = "enabled" if body.enabled else ("disabled" if body.enabled is False else installation["status"])
            target_enabled = body.enabled if body.enabled is not None else installation["enabled"]
            result = await conn.execute(
                """
                UPDATE ag_skill_install
                SET enabled=%s, status=%s, grants=COALESCE(%s::jsonb, grants),
                  version=version+1, updated_at=now()
                WHERE id=%s RETURNING *
                """,
                (
                    target_enabled,
                    target_status,
                    json_dumps(body.grants) if body.grants is not None else None,
                    install_id,
                ),
            )
            installation = await result.fetchone()
            skill = await _get_skill(conn, installation["skill_id"], actor.workspace_id)
            await _emit_skill_event(
                conn,
                event_type="skill.enabled.v1" if body.enabled is True else "skill.disabled.v1" if body.enabled is False else "skill.installation_updated.v1",
                skill=skill,
                installation=installation,
            )
    return {"skill": _skill_view(skill, installation)}


@skill_installs_router.delete("/{install_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill_install(
    install_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
) -> Response:
    _require_install(actor, "write")
    async with pool.connection() as conn:
        result = await conn.execute(
            "DELETE FROM ag_skill_install WHERE id=%s AND workspace_id=%s RETURNING id",
            (install_id, actor.workspace_id),
        )
        row = await result.fetchone()
        await conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Skill installation not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ============================================================================
# v7.164 T-M7-007 技能包格式增强
# 规范化 manifest（含 author/license/inputs_schema/outputs_schema/runtime/...）
# Ed25519 签名验证 stub、风险评分、签名验证端点
# ============================================================================


class EnhancedSkillManifest(BaseModel):
    """规范化的技能包 manifest（v7.164）。

    与既有 ``SkillManifest``（workspace 安装 manifest）共存：
    - 既有 SkillManifest 用于 ag_skill 安装记录，由 v7.14 落地。
    - 本 EnhancedSkillManifest 用于市场包发布、签名验证与审核工作流。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=63, pattern=SKILL_NAME_PATTERN.pattern)
    version: str = Field(min_length=5, max_length=128, pattern=SEMVER_PATTERN.pattern)
    description: str = Field(min_length=1, max_length=500)
    author: str = Field(min_length=1, max_length=120)
    license: str = Field(min_length=1, max_length=200, pattern=SPDX_LICENSE_PATTERN.pattern)
    entrypoint: str = Field(min_length=3, max_length=200)
    inputs_schema: dict[str, Any] = Field(default_factory=dict)
    outputs_schema: dict[str, Any] = Field(default_factory=dict)
    permissions: list[str] = Field(default_factory=list, max_length=64)
    runtime: Literal["python", "local_http", "mock"] = "python"
    tags: list[str] = Field(default_factory=list, max_length=32)
    homepage: str | None = Field(default=None, max_length=500)
    content_hash: str | None = Field(default=None, pattern=SHA256_PATTERN.pattern)

    @field_validator("entrypoint")
    @classmethod
    def validate_entrypoint_format(cls, value: str) -> str:
        if ":" not in value:
            raise ValueError("entrypoint must be a module path like 'mymod:handler'")
        if not ENTRYPOINT_PATTERN.fullmatch(value):
            raise ValueError("entrypoint must be 'module.path:callable_name'")
        return value

    @field_validator("permissions")
    @classmethod
    def validate_permissions(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            value = (value or "").strip().lower()
            if not value or not CAPABILITY_PATTERN.fullmatch(value):
                raise ValueError("permissions must use a capability name")
            if value not in normalized:
                normalized.append(value)
        return sorted(normalized)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            value = (value or "").strip().lower()
            if not value or len(value) > 64 or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", value):
                raise ValueError("tags must be lowercase identifier-like strings")
            if value not in normalized:
                normalized.append(value)
        return sorted(normalized)

    @field_validator("homepage")
    @classmethod
    def validate_homepage(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not URL_PATTERN.fullmatch(value):
            raise ValueError("homepage must be a valid http(s) URL")
        return value

    @field_validator("inputs_schema", "outputs_schema")
    @classmethod
    def validate_json_schema_dict(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("schema must be a JSON Schema object")
        if value and "type" not in value and "$ref" not in value:
            # 仅基础提示：JSON Schema 不强制 type 字段，但本规范要求显式声明
            raise ValueError("schema must declare a 'type' or '$ref' key")
        return value


class SkillSignature(BaseModel):
    """技能包签名（v7.164，Ed25519）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    algorithm: Literal["ed25519"] = "ed25519"
    signature_bytes: str = Field(min_length=1, max_length=2048)
    signing_key_fingerprint: str = Field(default="", max_length=128)
    signed_at: str | None = None


class VerifySignatureRequest(BaseModel):
    """POST /api/v1/skills/{id}/verify-signature 请求体。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    signature: str = Field(min_length=1, max_length=2048)
    public_key: str = Field(min_length=1, max_length=2048)
    signing_key_fingerprint: str | None = Field(default=None, max_length=128)


def _enhanced_manifest_content_hash(manifest: dict[str, Any]) -> str:
    """content_hash = SHA-256(name + version + entrypoint + inputs_schema + outputs_schema)。

    按 T-M7-007 规范，使用规范化的 JSON 表示 schema 字段。
    """
    parts = [
        str(manifest.get("name", "")),
        str(manifest.get("version", "")),
        str(manifest.get("entrypoint", "")),
        _canonical_json(manifest.get("inputs_schema") or {}),
        _canonical_json(manifest.get("outputs_schema") or {}),
    ]
    payload = "|".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_enhanced_manifest(
    manifest_dict: dict[str, Any],
) -> tuple[bool, list[str], str | None]:
    """校验规范化 manifest，返回 (is_valid, errors, content_hash)。

    与既有 ``validate_manifest`` 区分：本函数返回三元组，便于审核工作流聚合错误。
    """
    if not isinstance(manifest_dict, dict):
        return False, ["manifest must be an object"], None
    try:
        manifest = EnhancedSkillManifest.model_validate(manifest_dict)
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        ]
        return False, errors, None
    normalized = manifest.model_dump(exclude_none=True)
    content_hash = _enhanced_manifest_content_hash(normalized)
    normalized["content_hash"] = content_hash
    return True, [], content_hash


def compute_risk_score(manifest: dict[str, Any] | EnhancedSkillManifest) -> tuple[int, str]:
    """计算风险评分 (score, level)。

    公式：permissions_count * 10 + (runtime==local_http ? 20 : 0)
         + (entrypoint 含 'eval'/'exec' ? 50 : 0)
    分级：low (<20) / medium (20-50) / high (>50)
    """
    if isinstance(manifest, EnhancedSkillManifest):
        data = manifest.model_dump(exclude_none=True)
    elif isinstance(manifest, dict):
        data = manifest
    else:
        return 0, "low"
    permissions = data.get("permissions") or []
    runtime = data.get("runtime") or "python"
    entrypoint = str(data.get("entrypoint") or "")
    score = len(permissions) * 10
    if runtime == "local_http":
        score += 20
    if "eval" in entrypoint or "exec" in entrypoint:
        score += 50
    if score < 20:
        level = "low"
    elif score <= 50:
        level = "medium"
    else:
        level = "high"
    return score, level


def verify_skill_signature(
    manifest: dict[str, Any],
    signature: str,
    public_key: str,
) -> bool:
    """验证 Ed25519 签名（v7.164）。

    与 skill_market.verify_skill_signature 行为一致，但本函数用于 ag_skill 路径。
    空 signature/public_key 视为未签名，返回 False（调用方应识别 unsigned 状态）。
    """
    if not signature or not public_key:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature

        pub_bytes = base64.b64decode(public_key)
        pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
        sig_bytes = base64.b64decode(signature)
        canonical = json.dumps(manifest, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).digest()
        pub_key.verify(sig_bytes, digest)
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False


def _require_admin(actor: Actor) -> None:
    """签名验证端点要求 admin/owner 或显式 skill:admin 能力。"""
    if capability_allows(actor.capabilities, "skill:admin") or capability_allows(actor.capabilities, "skill:*"):
        return
    if actor.actor_type == "user" and actor.role in {"owner", "admin"}:
        return
    raise HTTPException(status_code=403, detail="Missing capability: skill:admin")


@router.post("/{skill_id}/verify-signature")
async def verify_signature_endpoint(
    skill_id: str,
    body: VerifySignatureRequest,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """POST /api/v1/skills/{id}/verify-signature (admin/owner)。

    使用调用方提供的 Ed25519 公钥对 manifest 进行验签，更新 ag_skill.signing_status
    与 signing_key_fingerprint。验签失败时 status=invalid，但不抛 4xx 以便客户端记录。
    """
    _require_admin(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            skill = await _get_skill(conn, skill_id, actor.workspace_id, for_update=True)
            manifest = skill.get("manifest") or {}
            if isinstance(manifest, str):
                manifest = json.loads(manifest)
            valid = verify_skill_signature(manifest, body.signature, body.public_key)
            fingerprint = (body.signing_key_fingerprint or "").strip()
            if not fingerprint and body.public_key:
                fingerprint = hashlib.sha256(body.public_key.encode("utf-8")).hexdigest()[:64]
            new_status = "signed" if valid else "invalid"
            await conn.execute(
                """
                UPDATE ag_skill
                SET signing_status=%s, signing_key_fingerprint=%s, updated_at=now()
                WHERE id=%s AND workspace_id=%s
                """,
                (new_status, fingerprint, skill_id, actor.workspace_id),
            )
            await conn.execute(
                """
                INSERT INTO ag_skill_signature(
                    id, skill_id, algorithm, signature_bytes,
                    signing_key_fingerprint, signed_at, verified_at
                ) VALUES (%s, %s, 'ed25519', %s, %s, %s, CASE WHEN %s THEN now() ELSE NULL END)
                """,
                (
                    new_id("sksig"),
                    skill_id,
                    body.signature,
                    fingerprint,
                    "now()" if body.signed_at is None else body.signed_at,
                    valid,
                ),
            )
            await _emit_skill_event(
                conn,
                event_type="skill.signature_verified.v1" if valid else "skill.signature_invalid.v1",
                skill={**skill, "signing_status": new_status, "signing_key_fingerprint": fingerprint},
            )
    return {
        "skill_id": skill_id,
        "signature_status": new_status,
        "valid": valid,
        "signing_key_fingerprint": fingerprint,
    }


# ============================================================================
# 技能市场（Marketplace）：发布 / 列表 / 详情 / 订阅 / 评分 / 版本
# 跨 workspace 可见已发布技能；订阅与评分写入当前 workspace
# ============================================================================


class MarketplacePublishRequest(BaseModel):
    """POST /api/v1/skills/marketplace/publish 请求体。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    skill_id: str = Field(min_length=1, max_length=160)
    category: str = Field(default="", max_length=64)
    tags: list[str] = Field(default_factory=list, max_length=32)
    summary: str = Field(default="", max_length=2000)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            value = (value or "").strip().lower()
            if not value or len(value) > 64 or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", value):
                raise ValueError("tags must be lowercase identifier-like strings")
            if value not in normalized:
                normalized.append(value)
        return sorted(normalized)


class MarketplaceRatingRequest(BaseModel):
    """POST /api/v1/skills/marketplace/{skill_id}/ratings 请求体。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    score: int = Field(ge=1, le=5)
    review_text: str = Field(default="", max_length=2000)


class SkillVersionCreateRequest(BaseModel):
    """POST /api/v1/skills/{skill_id}/versions 请求体。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    changelog: str = Field(default="", max_length=4000)
    manifest_snapshot: dict[str, Any] | None = None


def _parse_text_array(value: Any) -> list[str]:
    """解析 PostgreSQL TEXT[] 字段（psycopg 返回 list，mock 可能返回 str/list）。"""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except (TypeError, ValueError):
            pass
        return [value] if value else []
    return []


def _encode_cursor(offset: int) -> str:
    """将分页 offset 编码为不透明 cursor。"""
    return base64.urlsafe_b64encode(str(offset).encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> int:
    """将不透明 cursor 解码为分页 offset。"""
    try:
        return int(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid pagination cursor") from exc


def _row_dict(row: Any) -> dict[str, Any]:
    """将行（dict 或 psycopg Record）统一转为 dict。"""
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except (TypeError, ValueError):
        return {}


def _marketplace_view(row: dict[str, Any], rating_stats: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造市场 listing 视图。"""
    view: dict[str, Any] = {
        "skill_id": row["skill_id"],
        "workspace_id": row["workspace_id"],
        "category": row.get("category", ""),
        "tags": _parse_text_array(row.get("tags")),
        "summary": row.get("summary", ""),
        "listing_status": row.get("listing_status", "draft"),
        "published_at": row.get("published_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "name": row.get("name"),
        "publisher": row.get("publisher"),
        "semver": row.get("semver"),
        "risk_level": row.get("risk_level"),
        "review_status": row.get("review_status"),
    }
    if rating_stats is not None:
        view["rating_avg"] = float(rating_stats.get("avg_score") or 0)
        view["rating_count"] = int(rating_stats.get("rating_count") or 0)
    return view


def _rating_view(row: dict[str, Any]) -> dict[str, Any]:
    """构造评分视图。"""
    return {
        "id": row["id"],
        "skill_id": row["skill_id"],
        "workspace_id": row["workspace_id"],
        "user_id": row["user_id"],
        "score": row["score"],
        "review_text": row.get("review_text", ""),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _version_view(row: dict[str, Any]) -> dict[str, Any]:
    """构造版本视图（解析 JSONB manifest_snapshot）。"""
    snapshot = row.get("manifest_snapshot")
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            pass
    return {
        "id": row["id"],
        "skill_id": row["skill_id"],
        "workspace_id": row["workspace_id"],
        "version": row["version"],
        "manifest_snapshot": snapshot if isinstance(snapshot, dict) else {},
        "changelog": row.get("changelog", ""),
        "created_by": row.get("created_by"),
        "created_at": row.get("created_at"),
    }


async def _get_published_listing(conn, skill_id: str) -> dict[str, Any]:
    """获取已发布的市场 listing（跨 workspace 可见）。未发布或不存在 → 404。"""
    result = await conn.execute(
        """
        SELECT l.skill_id, l.workspace_id, l.category, l.tags, l.summary,
               l.listing_status, l.published_at, l.created_at, l.updated_at,
               s.name, s.publisher, s.semver, s.risk_level, s.review_status, s.status AS skill_status
        FROM skill_marketplace_listing l
        JOIN ag_skill s ON s.id = l.skill_id
        WHERE l.skill_id=%s AND l.listing_status='published' AND s.status <> 'revoked'
        """,
        (skill_id,),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Skill is not published in the marketplace")
    return row


@router.post("/marketplace/publish", status_code=status.HTTP_201_CREATED)
async def publish_skill_to_marketplace(
    body: MarketplacePublishRequest,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """POST /api/v1/skills/marketplace/publish - 发布技能到市场。

    将当前 workspace 已审核通过的技能标记为市场公开，设置 category/tags/summary，
    listing_status=published。重复发布幂等更新（保留首次 published_at）。
    """
    _require(actor, "write")
    async with pool.connection() as conn:
        # 技能必须在当前 workspace 且未被吊销
        skill_result = await conn.execute(
            "SELECT id, workspace_id, name, publisher, semver, review_status, status FROM ag_skill WHERE id=%s AND workspace_id=%s AND status <> 'revoked'",
            (body.skill_id, actor.workspace_id),
        )
        skill = await skill_result.fetchone()
        if not skill:
            raise HTTPException(status_code=404, detail="Skill was not found in this workspace")
        # 草稿/未审核技能不允许发布
        if skill["review_status"] != "approved":
            raise HTTPException(status_code=409, detail="Skill must be approved before publishing to marketplace")
        # 插入或更新 listing（幂等：重复发布更新字段，保留首次 published_at）
        listing_result = await conn.execute(
            """
            INSERT INTO skill_marketplace_listing(skill_id, workspace_id, category, tags, summary, listing_status, published_at, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, 'published', now(), now(), now())
            ON CONFLICT (skill_id) DO UPDATE SET
              category=EXCLUDED.category,
              tags=EXCLUDED.tags,
              summary=EXCLUDED.summary,
              listing_status='published',
              published_at=COALESCE(skill_marketplace_listing.published_at, now()),
              updated_at=now()
            RETURNING *
            """,
            (body.skill_id, actor.workspace_id, body.category, body.tags, body.summary),
        )
        listing = await listing_result.fetchone()
        await conn.commit()
    merged = {**_row_dict(listing), **{k: skill.get(k) for k in ("name", "publisher", "semver", "review_status") if k in skill}}
    return _marketplace_view(merged)


@router.get("/marketplace/{skill_id}")
async def get_marketplace_skill(
    skill_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """GET /api/v1/skills/marketplace/{skill_id} - 市场详情（含评分统计）。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        listing = await _get_published_listing(conn, skill_id)
        stats_result = await conn.execute(
            "SELECT COALESCE(AVG(score),0) AS avg_score, COUNT(*) AS rating_count FROM skill_rating WHERE skill_id=%s",
            (skill_id,),
        )
        stats = await stats_result.fetchone()
    return _marketplace_view(_row_dict(listing), rating_stats=_row_dict(stats))


@router.post("/marketplace/{skill_id}/subscribe", status_code=status.HTTP_201_CREATED)
async def subscribe_marketplace_skill(
    skill_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """POST /api/v1/skills/marketplace/{skill_id}/subscribe - 在当前 workspace 安装市场技能引用。

    仅允许订阅已发布技能；重复订阅幂等返回既有安装记录。
    """
    _require(actor, "write")
    async with pool.connection() as conn:
        # 跨 workspace 校验技能已发布
        listing = await _get_published_listing(conn, skill_id)
        # 在当前 workspace 安装引用（幂等：UNIQUE(workspace_id, skill_id)）
        idempotency_hash = hash_secret(f"marketplace:{skill_id}:{actor.workspace_id}")
        input_hash = canonical_hash({"skill_id": skill_id, "source": "marketplace", "workspace_id": actor.workspace_id})
        install_result = await conn.execute(
            """
            INSERT INTO ag_skill_install(id, skill_id, org_id, workspace_id, enabled, status, grants, idempotency_key_hash, input_hash, installed_by)
            VALUES (%s, %s, %s, %s, FALSE, 'disabled', '{}'::jsonb, %s, %s, %s)
            ON CONFLICT (workspace_id, skill_id) DO NOTHING RETURNING *
            """,
            (new_id("skillinst"), skill_id, actor.org_id, actor.workspace_id, idempotency_hash, input_hash, actor.user_id),
        )
        installation = await install_result.fetchone()
        deduplicated = False
        if not installation:
            existing_result = await conn.execute(
                "SELECT * FROM ag_skill_install WHERE skill_id=%s AND workspace_id=%s",
                (skill_id, actor.workspace_id),
            )
            installation = await existing_result.fetchone()
            deduplicated = True
        await conn.commit()
    listing_view = _marketplace_view(_row_dict(listing))
    return {
        "skill_id": skill_id,
        "listing": listing_view,
        "installation": {
            "id": installation["id"],
            "enabled": installation["enabled"],
            "status": installation["status"],
            "version": installation["version"],
            "workspace_id": installation["workspace_id"],
            "created_at": installation.get("created_at"),
            "updated_at": installation.get("updated_at"),
        },
        "deduplicated": deduplicated,
    }


@router.post("/marketplace/{skill_id}/ratings", status_code=status.HTTP_201_CREATED)
async def create_marketplace_rating(
    skill_id: str,
    body: MarketplaceRatingRequest,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """POST /api/v1/skills/marketplace/{skill_id}/ratings - 评分（1-5 星，幂等）。

    同 workspace+user 仅保留最新评分（UNIQUE + ON CONFLICT DO UPDATE）。
    """
    _require(actor, "write")
    async with pool.connection() as conn:
        # 跨 workspace 校验技能已发布
        await _get_published_listing(conn, skill_id)
        result = await conn.execute(
            """
            INSERT INTO skill_rating(id, skill_id, workspace_id, user_id, score, review_text)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (skill_id, workspace_id, user_id) DO UPDATE SET
              score=EXCLUDED.score, review_text=EXCLUDED.review_text, updated_at=now()
            RETURNING *
            """,
            (new_id("skrate"), skill_id, actor.workspace_id, actor.user_id, body.score, body.review_text),
        )
        rating = await result.fetchone()
        await conn.commit()
    return _rating_view(_row_dict(rating))


@router.get("/marketplace/{skill_id}/ratings")
async def list_marketplace_ratings(
    skill_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=512),
) -> dict[str, Any]:
    """GET /api/v1/skills/marketplace/{skill_id}/ratings - 评分列表（分页，跨 workspace 可见）。"""
    _require(actor, "read")
    offset = _decode_cursor(cursor) if cursor else 0
    async with pool.connection() as conn:
        # 跨 workspace 校验技能已发布
        await _get_published_listing(conn, skill_id)
        result = await conn.execute(
            "SELECT * FROM skill_rating WHERE skill_id=%s ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
            (skill_id, limit, offset),
        )
        rows = await result.fetchall()
        stats_result = await conn.execute(
            "SELECT COALESCE(AVG(score),0) AS avg_score, COUNT(*) AS rating_count FROM skill_rating WHERE skill_id=%s",
            (skill_id,),
        )
        stats = await stats_result.fetchone()
    items = [_rating_view(_row_dict(row)) for row in rows]
    has_more = len(rows) == limit
    next_cursor = _encode_cursor(offset + limit) if has_more else None
    return {
        "items": items,
        "data": items,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "rating_avg": float(stats.get("avg_score") or 0) if stats else 0.0,
        "rating_count": int(stats.get("rating_count") or 0) if stats else 0,
        "meta": {"request_id": None},
    }


@router.post("/{skill_id}/versions", status_code=status.HTTP_201_CREATED)
async def create_skill_version(
    skill_id: str,
    body: SkillVersionCreateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """POST /api/v1/skills/{skill_id}/versions - 创建新版本（版本号自增，保存 manifest 快照）。"""
    _require(actor, "write")
    async with pool.connection() as conn:
        # 技能必须在当前 workspace（workspace 隔离）
        skill_result = await conn.execute(
            "SELECT id, workspace_id, manifest FROM ag_skill WHERE id=%s AND workspace_id=%s AND status <> 'revoked'",
            (skill_id, actor.workspace_id),
        )
        skill = await skill_result.fetchone()
        if not skill:
            raise HTTPException(status_code=404, detail="Skill was not found in this workspace")
        # 版本号自增
        max_result = await conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 AS next_version FROM skill_version WHERE skill_id=%s AND workspace_id=%s",
            (skill_id, actor.workspace_id),
        )
        max_row = await max_result.fetchone()
        next_version = int(max_row["next_version"])
        manifest_snapshot = body.manifest_snapshot if body.manifest_snapshot is not None else skill.get("manifest", {})
        result = await conn.execute(
            """
            INSERT INTO skill_version(id, skill_id, workspace_id, version, manifest_snapshot, changelog, created_by)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s) RETURNING *
            """,
            (new_id("skver"), skill_id, actor.workspace_id, next_version, json_dumps(manifest_snapshot), body.changelog, actor.user_id),
        )
        version_row = await result.fetchone()
        await conn.commit()
    return _version_view(_row_dict(version_row))


@router.get("/{skill_id}/versions")
async def list_skill_versions(
    skill_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=512),
) -> dict[str, Any]:
    """GET /api/v1/skills/{skill_id}/versions - 版本列表（分页，按版本号倒序，workspace 隔离）。"""
    _require(actor, "read")
    offset = _decode_cursor(cursor) if cursor else 0
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM skill_version WHERE skill_id=%s AND workspace_id=%s ORDER BY version DESC LIMIT %s OFFSET %s",
            (skill_id, actor.workspace_id, limit, offset),
        )
        rows = await result.fetchall()
    items = [_version_view(_row_dict(row)) for row in rows]
    has_more = len(rows) == limit
    next_cursor = _encode_cursor(offset + limit) if has_more else None
    return {
        "items": items,
        "data": items,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "meta": {"request_id": None},
    }


__all__ = [
    "MANIFEST_SCHEMA",
    "SCHEMA_STATEMENTS",
    "EnhancedSkillManifest",
    "MarketplacePublishRequest",
    "MarketplaceRatingRequest",
    "SkillInstallPatch",
    "SkillInstallRequest",
    "SkillManifest",
    "SkillReviewRequest",
    "SkillSignature",
    "SkillStateRequest",
    "SkillVersionCreateRequest",
    "VerifySignatureRequest",
    "check_idempotency_replay",
    "check_skill_version_content",
    "compute_risk_level",
    "compute_risk_score",
    "ensure_skills_schema",
    "redact_sensitive",
    "redact_sensitive_text",
    "resolve_package_reference",
    "router",
    "skill_content_hash",
    "skill_installs_router",
    "validate_artifact_reference",
    "validate_enhanced_manifest",
    "validate_manifest",
    "validate_package_path",
    "verify_skill_signature",
]
