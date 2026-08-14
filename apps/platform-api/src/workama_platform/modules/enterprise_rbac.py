from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import secrets
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from workama_platform.core import (
    Actor,
    capability_allows,
    get_actor,
    json_dumps,
    new_id,
    pool,
)
from workama_platform.modules.audit_exports import append_audit_chain


router = APIRouter(prefix="/api/v1/enterprise", tags=["enterprise-rbac"])

GROUP_SOURCES = frozenset({"local", "scim", "directory"})
ROLE_STATUSES = frozenset({"active", "disabled"})
BINDING_STATUSES = frozenset({"active", "disabled"})
SERVICE_POLICY_STATUSES = frozenset({"active", "disabled"})
AUTH_POLICY_STATUSES = frozenset({"active", "disabled"})
_NAME_RE = re.compile(r"^[^\x00\n\r]{1,120}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,48}:(?:[a-z][a-z0-9_.-]{0,80}|\*)$")
_OPERATION_RE = re.compile(r"^[a-z][a-z0-9_.:-]{1,127}$")
_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "client_secret",
        "credential",
        "password",
        "private_key",
        "secret",
        "token",
    }
)
_RESERVED_CAPABILITY_DOMAINS = frozenset(
    {
        "billing",
        "group",
        "license",
        "owner",
        "platform",
        "privacy",
        "region",
        "role",
        "role_binding",
        "security",
        "service_account",
        "system",
    }
)
_FORBIDDEN_CAPABILITIES = frozenset(
    {
        "org:delete",
        "org:owner_transfer",
        "workspace:delete",
    }
)


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS id_group (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      name TEXT NOT NULL,
      external_id TEXT,
      source TEXT NOT NULL DEFAULT 'local'
        CHECK (source IN ('local','scim','directory')),
      status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','disabled')),
      version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
      created_by TEXT NOT NULL REFERENCES id_user(id),
      create_idempotency_key TEXT,
      request_hash TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(org_id, name)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_id_group_idempotency ON id_group(org_id, create_idempotency_key) WHERE create_idempotency_key IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_id_group_external_id ON id_group(org_id, source, external_id) WHERE external_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_id_group_org_status ON id_group(org_id, status, updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS id_group_member (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      group_id TEXT NOT NULL REFERENCES id_group(id) ON DELETE CASCADE,
      user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
      source TEXT NOT NULL DEFAULT 'local'
        CHECK (source IN ('local','scim','directory')),
      status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','disabled')),
      create_idempotency_key TEXT,
      request_hash TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(group_id, user_id)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_id_group_member_idempotency ON id_group_member(group_id, create_idempotency_key) WHERE create_idempotency_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_id_group_member_org_user ON id_group_member(org_id, user_id, status)",
    """
    CREATE TABLE IF NOT EXISTS id_role (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      name TEXT NOT NULL,
      description TEXT NOT NULL DEFAULT '',
      capabilities TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
      system BOOLEAN NOT NULL DEFAULT FALSE,
      status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','disabled')),
      version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
      created_by TEXT NOT NULL REFERENCES id_user(id),
      create_idempotency_key TEXT,
      request_hash TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id, name)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_id_role_idempotency ON id_role(workspace_id, create_idempotency_key) WHERE create_idempotency_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_id_role_workspace_status ON id_role(workspace_id, status, updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS id_role_binding (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      role_id TEXT NOT NULL REFERENCES id_role(id) ON DELETE CASCADE,
      subject_type TEXT NOT NULL CHECK (subject_type IN ('user','group','service_account')),
      subject_id TEXT NOT NULL,
      resource_type TEXT,
      resource_id TEXT,
      conditions JSONB NOT NULL DEFAULT '{}'::jsonb,
      status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','disabled')),
      expires_at TIMESTAMPTZ,
      version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
      created_by TEXT NOT NULL REFERENCES id_user(id),
      create_idempotency_key TEXT,
      request_hash TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_id_role_binding_idempotency ON id_role_binding(workspace_id, create_idempotency_key) WHERE create_idempotency_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_id_role_binding_subject ON id_role_binding(org_id, workspace_id, subject_type, subject_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_id_role_binding_role ON id_role_binding(workspace_id, role_id, status, expires_at)",
    """
    CREATE TABLE IF NOT EXISTS id_service_account_policy (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      service_account_id TEXT NOT NULL REFERENCES id_service_account(id) ON DELETE CASCADE,
      allowed_scopes TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
      allowed_ip_cidrs TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
      status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','disabled')),
      expires_at TIMESTAMPTZ,
      version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
      created_by TEXT NOT NULL REFERENCES id_user(id),
      create_idempotency_key TEXT,
      request_hash TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id, service_account_id)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_id_service_account_policy_idempotency ON id_service_account_policy(workspace_id, create_idempotency_key) WHERE create_idempotency_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_id_service_account_policy_workspace ON id_service_account_policy(workspace_id, status, expires_at)",
    """
    CREATE TABLE IF NOT EXISTS id_auth_strength_policy (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      operation TEXT NOT NULL,
      required_auth_strength SMALLINT NOT NULL CHECK (required_auth_strength BETWEEN 1 AND 4),
      status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','disabled')),
      version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
      created_by TEXT NOT NULL REFERENCES id_user(id),
      create_idempotency_key TEXT,
      request_hash TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id, operation)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_id_auth_strength_policy_idempotency ON id_auth_strength_policy(workspace_id, create_idempotency_key) WHERE create_idempotency_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_id_auth_strength_policy_workspace ON id_auth_strength_policy(workspace_id, status, operation)",
)


async def ensure_enterprise_rbac_schema(conn) -> None:
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _normalize_name(value: str, field: str = "name") -> str:
    normalized = " ".join(value.split())
    if not normalized or not _NAME_RE.fullmatch(normalized):
        raise ValueError(f"{field} is invalid")
    return normalized


def _normalize_id(value: str, field: str) -> str:
    value = value.strip()
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"{field} is invalid")
    return value


def _normalize_operation(value: str) -> str:
    value = value.strip().lower()
    if not _OPERATION_RE.fullmatch(value):
        raise ValueError("operation is invalid")
    return value


def _normalize_capabilities(values: list[str]) -> list[str]:
    normalized = sorted({value.strip().lower() for value in values if value and value.strip()})
    if not normalized:
        raise ValueError("at least one capability is required")
    if len(normalized) > 64:
        raise ValueError("too many capabilities")
    invalid = [value for value in normalized if not _CAPABILITY_RE.fullmatch(value)]
    if invalid:
        raise ValueError("capability format is invalid")
    for capability in normalized:
        domain = capability.split(":", 1)[0]
        if capability in _FORBIDDEN_CAPABILITIES or domain in _RESERVED_CAPABILITY_DOMAINS:
            raise ValueError("capability is reserved for the platform")
    return normalized


def _normalize_scopes(values: list[str]) -> list[str]:
    normalized = sorted({value.strip() for value in values if value and value.strip()})
    if not normalized or len(normalized) > 64:
        raise ValueError("allowed_scopes must contain between 1 and 64 values")
    invalid = [value for value in normalized if not _CAPABILITY_RE.fullmatch(value)]
    if invalid:
        raise ValueError("allowed_scopes contain an invalid value")
    forbidden = {"org:delete", "org:owner_transfer", "service_account:credential"}
    if any(value in forbidden or value.split(":", 1)[0] in {"owner", "platform", "system"} for value in normalized):
        raise ValueError("high-risk service-account scopes cannot be delegated")
    return normalized


def _normalize_cidrs(values: list[str]) -> list[str]:
    if len(values) > 32:
        raise ValueError("too many allowed_ip_cidrs")
    normalized: list[str] = []
    for value in values:
        try:
            network = ipaddress.ip_network(value.strip(), strict=False)
        except ValueError as exc:
            raise ValueError("allowed_ip_cidrs contains an invalid network") from exc
        rendered = str(network)
        if rendered not in normalized:
            normalized.append(rendered)
    return sorted(normalized)


def _normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _validate_safe_json(value: dict[str, Any], field: str, max_bytes: int = 16_000) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError(f"{field} is too large")

    def walk(current: Any) -> None:
        if isinstance(current, dict):
            for key, item in current.items():
                if str(key).strip().lower() in _SENSITIVE_KEYS:
                    raise ValueError(f"{field} contains a sensitive key")
                walk(item)
        elif isinstance(current, list):
            for item in current:
                walk(item)

    walk(value)
    return value


class GroupCreate(_Model):
    name: str = Field(min_length=1, max_length=120)
    source: Literal["local", "scim", "directory"] = "local"
    external_id: str | None = Field(default=None, max_length=256)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _normalize_name(value)

    @field_validator("external_id")
    @classmethod
    def validate_external_id(cls, value: str | None) -> str | None:
        return _normalize_id(value, "external_id") if value is not None else None

    @model_validator(mode="after")
    def validate_source(self) -> "GroupCreate":
        if self.source != "local":
            raise ValueError("external group provisioning is pending")
        return self


class GroupPatch(_Model):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    status: Literal["active", "disabled"] | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        return _normalize_name(value) if value is not None else None

    @model_validator(mode="after")
    def require_change(self) -> "GroupPatch":
        if self.name is None and self.status is None:
            raise ValueError("at least one group field is required")
        return self


class GroupMemberCreate(_Model):
    user_id: str = Field(min_length=1, max_length=128)
    source: Literal["local", "scim", "directory"] = "local"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, value: str) -> str:
        return _normalize_id(value, "user_id")

    @model_validator(mode="after")
    def validate_source(self) -> "GroupMemberCreate":
        if self.source != "local":
            raise ValueError("external directory group membership is pending")
        return self


class RoleCreate(_Model):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    capabilities: list[str] = Field(min_length=1, max_length=64)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _normalize_name(value)

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: list[str]) -> list[str]:
        return _normalize_capabilities(value)


class RolePatch(_Model):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    capabilities: list[str] | None = Field(default=None, max_length=64)
    status: Literal["active", "disabled"] | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        return _normalize_name(value) if value is not None else None

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: list[str] | None) -> list[str] | None:
        return _normalize_capabilities(value) if value is not None else None

    @model_validator(mode="after")
    def require_change(self) -> "RolePatch":
        if self.name is None and self.description is None and self.capabilities is None and self.status is None:
            raise ValueError("at least one role field is required")
        return self


class RoleBindingCreate(_Model):
    role_id: str = Field(min_length=1, max_length=128)
    subject_type: Literal["user", "group", "service_account"]
    subject_id: str = Field(min_length=1, max_length=128)
    workspace_id: str | None = Field(default=None, max_length=128)
    resource_type: str | None = Field(default=None, max_length=80)
    resource_id: str | None = Field(default=None, max_length=128)
    conditions: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("role_id", "subject_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        return _normalize_id(value, "id")

    @field_validator("workspace_id")
    @classmethod
    def validate_workspace(cls, value: str | None) -> str | None:
        return _normalize_id(value, "workspace_id") if value is not None else None

    @field_validator("resource_type", "resource_id")
    @classmethod
    def validate_resource(cls, value: str | None) -> str | None:
        return value.strip() if value is not None and value.strip() else None

    @field_validator("conditions")
    @classmethod
    def validate_conditions(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_safe_json(value, "conditions", 8_000)

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime | None) -> datetime | None:
        normalized = _normalize_datetime(value)
        if normalized is not None and normalized <= datetime.now(UTC):
            raise ValueError("expires_at must be in the future")
        return normalized

    @model_validator(mode="after")
    def validate_scope(self) -> "RoleBindingCreate":
        if (self.resource_type is None) != (self.resource_id is None):
            raise ValueError("resource_type and resource_id must be provided together")
        return self


class RoleBindingPatch(_Model):
    resource_type: str | None = Field(default=None, max_length=80)
    resource_id: str | None = Field(default=None, max_length=128)
    conditions: dict[str, Any] | None = None
    expires_at: datetime | None = None
    status: Literal["active", "disabled"] | None = None

    @field_validator("resource_type", "resource_id")
    @classmethod
    def validate_resource(cls, value: str | None) -> str | None:
        return value.strip() if value is not None and value.strip() else None

    @field_validator("conditions")
    @classmethod
    def validate_conditions(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _validate_safe_json(value, "conditions", 8_000) if value is not None else None

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime | None) -> datetime | None:
        normalized = _normalize_datetime(value)
        if normalized is not None and normalized <= datetime.now(UTC):
            raise ValueError("expires_at must be in the future")
        return normalized

    @model_validator(mode="after")
    def require_change(self) -> "RoleBindingPatch":
        if self.resource_type is None and self.resource_id is None and self.conditions is None and self.expires_at is None and self.status is None:
            raise ValueError("at least one binding field is required")
        if (self.resource_type is None) != (self.resource_id is None) and self.conditions is None and self.expires_at is None and self.status is None:
            raise ValueError("resource_type and resource_id must be provided together")
        return self


class ServiceAccountPolicyCreate(_Model):
    service_account_id: str = Field(min_length=1, max_length=128)
    allowed_scopes: list[str] = Field(min_length=1, max_length=64)
    allowed_ip_cidrs: list[str] = Field(default_factory=list, max_length=32)
    expires_at: datetime | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("service_account_id")
    @classmethod
    def validate_service_account_id(cls, value: str) -> str:
        return _normalize_id(value, "service_account_id")

    @field_validator("allowed_scopes")
    @classmethod
    def validate_scopes(cls, value: list[str]) -> list[str]:
        return _normalize_scopes(value)

    @field_validator("allowed_ip_cidrs")
    @classmethod
    def validate_networks(cls, value: list[str]) -> list[str]:
        return _normalize_cidrs(value)

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime | None) -> datetime | None:
        normalized = _normalize_datetime(value)
        if normalized is not None and normalized <= datetime.now(UTC):
            raise ValueError("expires_at must be in the future")
        return normalized


class ServiceAccountPolicyPatch(_Model):
    allowed_scopes: list[str] | None = Field(default=None, max_length=64)
    allowed_ip_cidrs: list[str] | None = Field(default=None, max_length=32)
    expires_at: datetime | None = None
    status: Literal["active", "disabled"] | None = None

    @field_validator("allowed_scopes")
    @classmethod
    def validate_scopes(cls, value: list[str] | None) -> list[str] | None:
        return _normalize_scopes(value) if value is not None else None

    @field_validator("allowed_ip_cidrs")
    @classmethod
    def validate_networks(cls, value: list[str] | None) -> list[str] | None:
        return _normalize_cidrs(value) if value is not None else None

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime | None) -> datetime | None:
        normalized = _normalize_datetime(value)
        if normalized is not None and normalized <= datetime.now(UTC):
            raise ValueError("expires_at must be in the future")
        return normalized

    @model_validator(mode="after")
    def require_change(self) -> "ServiceAccountPolicyPatch":
        if self.allowed_scopes is None and self.allowed_ip_cidrs is None and self.expires_at is None and self.status is None:
            raise ValueError("at least one policy field is required")
        return self


class AuthStrengthPolicyCreate(_Model):
    operation: str = Field(min_length=2, max_length=128)
    required_auth_strength: int = Field(ge=1, le=4)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("operation")
    @classmethod
    def validate_operation(cls, value: str) -> str:
        return _normalize_operation(value)


class AuthStrengthPolicyPatch(_Model):
    required_auth_strength: int | None = Field(default=None, ge=1, le=4)
    status: Literal["active", "disabled"] | None = None

    @model_validator(mode="after")
    def require_change(self) -> "AuthStrengthPolicyPatch":
        if self.required_auth_strength is None and self.status is None:
            raise ValueError("at least one auth policy field is required")
        return self


class AuthStrengthEvaluate(_Model):
    operation: str = Field(min_length=2, max_length=128)

    @field_validator("operation")
    @classmethod
    def validate_operation(cls, value: str) -> str:
        return _normalize_operation(value)


class ServiceAccountPolicyEvaluate(_Model):
    scope: str = Field(min_length=1, max_length=128)
    source_ip: str | None = Field(default=None, max_length=64)

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: str) -> str:
        return _normalize_scopes([value])[0]

    @field_validator("source_ip")
    @classmethod
    def validate_source_ip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return str(ipaddress.ip_address(value.strip()))
        except ValueError as exc:
            raise ValueError("source_ip is invalid") from exc


def _now() -> datetime:
    return datetime.now(UTC)


def _not_found(resource: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{resource} not found")


def _same(value_a: str | None, value_b: str | None) -> bool:
    return value_a is not None and value_b is not None and secrets.compare_digest(str(value_a), str(value_b))


def _require_user(actor: Actor) -> None:
    if actor.actor_type != "user":
        raise HTTPException(status_code=403, detail="A human user actor is required")


def _require_capability(actor: Actor, required: str) -> None:
    if capability_allows(actor.capabilities, required):
        return
    aliases = {
        "group:read": ("org:read", "workspace:read", "workspace:*") ,
        "group:write": ("org:write", "workspace:*") ,
        "role:read": ("workspace:read", "workspace:*") ,
        "role:write": ("workspace:*") ,
        "role_binding:read": ("workspace:read", "workspace:*") ,
        "role_binding:write": ("workspace:*") ,
        "service_account_policy:read": ("service_account:read", "workspace:*") ,
        "service_account_policy:write": ("service_account:write", "service_account:*", "workspace:*") ,
        "auth_policy:read": ("security:read", "workspace:*") ,
        "auth_policy:write": ("security:*", "workspace:*") ,
    }.get(required, ())
    if not any(capability_allows(actor.capabilities, alias) for alias in aliases):
        raise HTTPException(status_code=403, detail=f"Missing capability: {required}")


def _require_high_assurance(actor: Actor) -> None:
    if actor.auth_strength < 2:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "AUTH_STRENGTH_REQUIRED",
                "required_auth_strength": 2,
                "step_up_required": True,
            },
        )


def _workspace_id(actor: Actor, requested: str | None) -> str:
    if requested is not None and not _same(requested, actor.workspace_id):
        raise _not_found("Workspace")
    if not actor.workspace_id or not actor.org_id:
        raise HTTPException(status_code=403, detail="Tenant context is unavailable")
    return actor.workspace_id


def _request_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _body_hash(model: BaseModel, extra: dict[str, Any] | None = None) -> str:
    payload = model.model_dump(mode="json", exclude_none=True)
    if extra:
        payload.update(extra)
    payload.pop("idempotency_key", None)
    return _request_hash(payload)


def _row(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _group_view(row: Any, *, members: list[dict[str, Any]] | None = None, replay: bool = False) -> dict[str, Any]:
    item = _row(row)
    result = {
        "id": item.get("id"),
        "org_id": item.get("org_id"),
        "name": item.get("name"),
        "external_id": item.get("external_id"),
        "source": item.get("source"),
        "status": item.get("status"),
        "version": item.get("version"),
        "created_by": item.get("created_by"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }
    if "member_count" in item:
        result["member_count"] = item["member_count"]
    if members is not None:
        result["members"] = members
    if replay:
        result["idempotency_replayed"] = True
    return result


def _member_view(row: Any, *, replay: bool = False) -> dict[str, Any]:
    item = _row(row)
    result = {
        "id": item.get("id"),
        "group_id": item.get("group_id"),
        "org_id": item.get("org_id"),
        "user_id": item.get("user_id"),
        "source": item.get("source"),
        "status": item.get("status"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }
    if replay:
        result["idempotency_replayed"] = True
    return result


def _role_view(row: Any, *, replay: bool = False) -> dict[str, Any]:
    item = _row(row)
    result = {
        "id": item.get("id"),
        "org_id": item.get("org_id"),
        "workspace_id": item.get("workspace_id"),
        "name": item.get("name"),
        "description": item.get("description"),
        "capabilities": sorted(item.get("capabilities") or []),
        "system": bool(item.get("system", False)),
        "status": item.get("status"),
        "version": item.get("version"),
        "created_by": item.get("created_by"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }
    if replay:
        result["idempotency_replayed"] = True
    return result


def _binding_view(row: Any, *, replay: bool = False) -> dict[str, Any]:
    item = _row(row)
    result = {
        "id": item.get("id"),
        "org_id": item.get("org_id"),
        "workspace_id": item.get("workspace_id"),
        "role_id": item.get("role_id"),
        "role_name": item.get("role_name"),
        "subject_type": item.get("subject_type"),
        "subject_id": item.get("subject_id"),
        "resource_type": item.get("resource_type"),
        "resource_id": item.get("resource_id"),
        "conditions": item.get("conditions") or {},
        "status": item.get("status"),
        "expires_at": item.get("expires_at"),
        "version": item.get("version"),
        "created_by": item.get("created_by"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }
    if replay:
        result["idempotency_replayed"] = True
    return result


def _service_policy_view(row: Any, *, replay: bool = False) -> dict[str, Any]:
    item = _row(row)
    result = {
        "id": item.get("id"),
        "org_id": item.get("org_id"),
        "workspace_id": item.get("workspace_id"),
        "service_account_id": item.get("service_account_id"),
        "allowed_scopes": sorted(item.get("allowed_scopes") or []),
        "allowed_ip_cidrs": sorted(item.get("allowed_ip_cidrs") or []),
        "status": item.get("status"),
        "expires_at": item.get("expires_at"),
        "version": item.get("version"),
        "created_by": item.get("created_by"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }
    if replay:
        result["idempotency_replayed"] = True
    return result


def _auth_policy_view(row: Any, *, replay: bool = False) -> dict[str, Any]:
    item = _row(row)
    result = {
        "id": item.get("id"),
        "org_id": item.get("org_id"),
        "workspace_id": item.get("workspace_id"),
        "operation": item.get("operation"),
        "required_auth_strength": item.get("required_auth_strength"),
        "status": item.get("status"),
        "version": item.get("version"),
        "created_by": item.get("created_by"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }
    if replay:
        result["idempotency_replayed"] = True
    return result


async def _audit(conn: Any, actor: Actor, action: str, resource_type: str, resource_id: str, details: dict[str, Any] | None = None) -> None:
    event_id = new_id("audit")
    await conn.execute(
        """
        INSERT INTO id_enterprise_audit_event
          (id, org_id, actor_user_id, action, resource_type, resource_id, reason, details)
        VALUES (%s, %s, %s, %s, %s, %s, '', %s::jsonb)
        """,
        (event_id, actor.org_id, actor.user_id, action, resource_type, resource_id, json_dumps(details or {})),
    )
    await append_audit_chain(
        conn,
        event_id=event_id,
        org_id=actor.org_id,
        workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
    )


async def _get_group(conn: Any, actor: Actor, group_id: str, *, for_update: bool = False) -> dict[str, Any]:
    result = await conn.execute(
        f"SELECT id, org_id, name, external_id, source, status, version, created_by, created_at, updated_at FROM id_group WHERE id=%s AND org_id=%s {'FOR UPDATE' if for_update else ''}",
        (group_id, actor.org_id),
    )
    row = await result.fetchone()
    if not row:
        raise _not_found("Group")
    return row


async def _get_role(conn: Any, actor: Actor, role_id: str, *, for_update: bool = False) -> dict[str, Any]:
    result = await conn.execute(
        f"SELECT id, org_id, workspace_id, name, description, capabilities, system, status, version, created_by, created_at, updated_at FROM id_role WHERE id=%s AND org_id=%s AND workspace_id=%s {'FOR UPDATE' if for_update else ''}",
        (role_id, actor.org_id, actor.workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise _not_found("Role")
    return row


async def _get_service_account(conn: Any, actor: Actor, service_account_id: str) -> dict[str, Any]:
    result = await conn.execute(
        "SELECT id, org_id, workspace_id, status, scopes, expires_at FROM id_service_account WHERE id=%s AND org_id=%s AND workspace_id=%s",
        (service_account_id, actor.org_id, actor.workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise _not_found("Service-account")
    if row["status"] != "active" or (row.get("expires_at") and _normalize_datetime(row["expires_at"]) <= _now()):
        raise HTTPException(status_code=409, detail="Service-account is not active")
    return row


async def _idempotent_row(conn: Any, table: str, scope_column: str, scope_value: str, key: str | None, request_hash: str) -> tuple[dict[str, Any] | None, bool]:
    if not key:
        return None, False
    result = await conn.execute(
        f"SELECT * FROM {table} WHERE {scope_column}=%s AND create_idempotency_key=%s LIMIT 1",
        (scope_value, key),
    )
    row = await result.fetchone()
    if not row:
        return None, False
    if not _same(row.get("request_hash"), request_hash):
        raise HTTPException(status_code=409, detail="Idempotency key was already used with a different request")
    return row, True


async def _insert_or_existing(conn: Any, statement: str, params: tuple[Any, ...], *, table: str, scope_column: str, scope_value: str, key: str | None, request_hash: str) -> tuple[dict[str, Any], bool]:
    result = await conn.execute(statement, params)
    row = await result.fetchone()
    if row:
        return row, False
    if key:
        existing, replay = await _idempotent_row(conn, table, scope_column, scope_value, key, request_hash)
        if existing:
            return existing, replay
    raise HTTPException(status_code=409, detail="Resource already exists")


@router.get("/groups")
async def list_groups(actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "group:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT g.id, g.org_id, g.name, g.external_id, g.source, g.status, g.version,
                   g.created_by, g.created_at, g.updated_at, COUNT(gm.id)::int AS member_count
            FROM id_group g
            LEFT JOIN id_group_member gm ON gm.group_id=g.id AND gm.status='active'
            WHERE g.org_id=%s
            GROUP BY g.id
            ORDER BY g.updated_at DESC, g.id
            """,
            (actor.org_id,),
        )
        rows = await result.fetchall()
    # Contract《720》listGroups: ListQuery -> ListResponse<GroupDTO>（保留旧字段 count 向后兼容）
    data = [_group_view(row) for row in rows]
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("/groups", status_code=201)
async def create_group(body: GroupCreate, actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "group:write")
    _require_high_assurance(actor)
    request_hash = _body_hash(body)
    async with pool.connection() as conn:
        async with conn.transaction():
            existing, replay = await _idempotent_row(conn, "id_group", "org_id", actor.org_id, body.idempotency_key, request_hash)
            if existing:
                return _group_view(existing, replay=replay)
            result = await conn.execute(
                """
                INSERT INTO id_group(id, org_id, name, external_id, source, created_by, create_idempotency_key, request_hash)
                VALUES (%s,%s,%s,%s,'local',%s,%s,%s)
                ON CONFLICT DO NOTHING
                RETURNING id, org_id, name, external_id, source, status, version, created_by, created_at, updated_at
                """,
                (new_id("grp"), actor.org_id, body.name, body.external_id, actor.user_id, body.idempotency_key, request_hash),
            )
            row = await result.fetchone()
            if not row:
                if body.idempotency_key:
                    existing, replay = await _idempotent_row(conn, "id_group", "org_id", actor.org_id, body.idempotency_key, request_hash)
                    if existing:
                        return _group_view(existing, replay=replay)
                raise HTTPException(status_code=409, detail="Group name or external_id already exists")
            await _audit(conn, actor, "group.created", "group", row["id"], {"source": "local"})
    return _group_view(row)


@router.get("/groups/{group_id}")
async def get_group(group_id: str, actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "group:read")
    async with pool.connection() as conn:
        group = await _get_group(conn, actor, group_id)
        result = await conn.execute(
            "SELECT id, group_id, org_id, user_id, source, status, created_at, updated_at FROM id_group_member WHERE group_id=%s AND org_id=%s ORDER BY created_at, id",
            (group_id, actor.org_id),
        )
        members = [_member_view(row) for row in await result.fetchall()]
    return _group_view(group, members=members)


@router.patch("/groups/{group_id}")
async def patch_group(group_id: str, body: GroupPatch, actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "group:write")
    _require_high_assurance(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            group = await _get_group(conn, actor, group_id, for_update=True)
            fields: list[str] = []
            params: list[Any] = []
            if body.name is not None:
                fields.append("name=%s")
                params.append(body.name)
            if body.status is not None:
                fields.append("status=%s")
                params.append(body.status)
            params.extend([group_id, actor.org_id])
            try:
                result = await conn.execute(
                    f"UPDATE id_group SET {', '.join(fields)}, version=version+1, updated_at=now() WHERE id=%s AND org_id=%s RETURNING id, org_id, name, external_id, source, status, version, created_by, created_at, updated_at",
                    tuple(params),
                )
            except Exception as exc:
                if "unique" in str(exc).lower():
                    raise HTTPException(status_code=409, detail="Group name already exists") from exc
                raise
            row = await result.fetchone()
            if not row:
                raise _not_found("Group")
            await _audit(conn, actor, "group.updated", "group", group_id, {"version": row["version"]})
    return _group_view(row)


@router.delete("/groups/{group_id}", status_code=204)
async def delete_group(group_id: str, actor: Actor = Depends(get_actor)) -> Response:
    _require_user(actor)
    _require_capability(actor, "group:write")
    _require_high_assurance(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            group = await _get_group(conn, actor, group_id, for_update=True)
            await conn.execute("UPDATE id_group SET status='disabled', version=version+1, updated_at=now() WHERE id=%s AND org_id=%s", (group_id, actor.org_id))
            await _audit(conn, actor, "group.disabled", "group", group_id, {"previous_status": group["status"]})
    return Response(status_code=204)


@router.post("/groups/{group_id}/members", status_code=201)
async def add_group_member(group_id: str, body: GroupMemberCreate, actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "group:write")
    _require_high_assurance(actor)
    request_hash = _body_hash(body, {"group_id": group_id})
    async with pool.connection() as conn:
        async with conn.transaction():
            group = await _get_group(conn, actor, group_id)
            existing, replay = await _idempotent_row(conn, "id_group_member", "group_id", group_id, body.idempotency_key, request_hash)
            if existing:
                return _member_view(existing, replay=replay)
            result = await conn.execute(
                """
                SELECT 1 FROM id_user u
                WHERE u.id=%s AND u.status='active' AND (
                  EXISTS (SELECT 1 FROM id_org o WHERE o.id=%s AND o.owner_user_id=u.id)
                  OR EXISTS (SELECT 1 FROM id_member m WHERE m.org_id=%s AND m.user_id=u.id)
                )
                """,
                (body.user_id, actor.org_id, actor.org_id),
            )
            if not await result.fetchone():
                raise _not_found("Organization member")
            result = await conn.execute(
                """
                INSERT INTO id_group_member(id, org_id, group_id, user_id, source, create_idempotency_key, request_hash)
                VALUES (%s,%s,%s,%s,'local',%s,%s)
                ON CONFLICT DO NOTHING
                RETURNING id, group_id, org_id, user_id, source, status, created_at, updated_at
                """,
                (new_id("grpm"), actor.org_id, group_id, body.user_id, body.idempotency_key, request_hash),
            )
            row = await result.fetchone()
            if not row:
                if body.idempotency_key:
                    existing, replay = await _idempotent_row(conn, "id_group_member", "group_id", group_id, body.idempotency_key, request_hash)
                    if existing:
                        return _member_view(existing, replay=replay)
                raise HTTPException(status_code=409, detail="Group membership already exists")
            await _audit(conn, actor, "group.member_added", "group_member", row["id"], {"group_id": group_id, "user_id": body.user_id})
    return _member_view(row)


@router.delete("/groups/{group_id}/members/{user_id}", status_code=204)
async def remove_group_member(group_id: str, user_id: str, actor: Actor = Depends(get_actor)) -> Response:
    _require_user(actor)
    _require_capability(actor, "group:write")
    _require_high_assurance(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            await _get_group(conn, actor, group_id)
            result = await conn.execute("DELETE FROM id_group_member WHERE group_id=%s AND org_id=%s AND user_id=%s RETURNING id", (group_id, actor.org_id, user_id))
            row = await result.fetchone()
            if not row:
                raise _not_found("Group membership")
            await _audit(conn, actor, "group.member_removed", "group_member", row["id"], {"group_id": group_id})
    return Response(status_code=204)


@router.get("/roles")
async def list_roles(actor: Actor = Depends(get_actor), include_disabled: bool = Query(default=False)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "role:read")
    status_filter = "" if include_disabled else " AND status='active'"
    async with pool.connection() as conn:
        result = await conn.execute(
            f"SELECT id, org_id, workspace_id, name, description, capabilities, system, status, version, created_by, created_at, updated_at FROM id_role WHERE org_id=%s AND workspace_id=%s{status_filter} ORDER BY updated_at DESC, id",
            (actor.org_id, actor.workspace_id),
        )
        rows = await result.fetchall()
    # Contract《720》listRoles: ListQuery -> ListResponse<RoleDTO>（保留旧字段 count 向后兼容）
    data = [_role_view(row) for row in rows]
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("/roles", status_code=201)
async def create_role(body: RoleCreate, actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "role:write")
    _require_high_assurance(actor)
    request_hash = _body_hash(body)
    async with pool.connection() as conn:
        async with conn.transaction():
            existing, replay = await _idempotent_row(conn, "id_role", "workspace_id", actor.workspace_id, body.idempotency_key, request_hash)
            if existing:
                return _role_view(existing, replay=replay)
            result = await conn.execute(
                """
                INSERT INTO id_role(id, org_id, workspace_id, name, description, capabilities, created_by, create_idempotency_key, request_hash)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
                RETURNING id, org_id, workspace_id, name, description, capabilities, system, status, version, created_by, created_at, updated_at
                """,
                (new_id("role"), actor.org_id, actor.workspace_id, body.name, body.description, body.capabilities, actor.user_id, body.idempotency_key, request_hash),
            )
            row = await result.fetchone()
            if not row:
                if body.idempotency_key:
                    existing, replay = await _idempotent_row(conn, "id_role", "workspace_id", actor.workspace_id, body.idempotency_key, request_hash)
                    if existing:
                        return _role_view(existing, replay=replay)
                raise HTTPException(status_code=409, detail="Role name already exists")
            await _audit(conn, actor, "role.created", "role", row["id"], {"workspace_id": actor.workspace_id, "version": row["version"]})
    return _role_view(row)


@router.get("/roles/{role_id}")
async def get_role(role_id: str, actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "role:read")
    async with pool.connection() as conn:
        row = await _get_role(conn, actor, role_id)
    return _role_view(row)


@router.patch("/roles/{role_id}")
async def patch_role(role_id: str, body: RolePatch, actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "role:write")
    _require_high_assurance(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            current = await _get_role(conn, actor, role_id, for_update=True)
            if current["system"]:
                raise HTTPException(status_code=403, detail="System roles cannot be modified")
            fields: list[str] = []
            params: list[Any] = []
            for name in ("name", "description", "capabilities", "status"):
                value = getattr(body, name)
                if value is not None:
                    fields.append(f"{name}=%s")
                    params.append(value)
            params.extend([role_id, actor.org_id, actor.workspace_id])
            try:
                result = await conn.execute(
                    f"UPDATE id_role SET {', '.join(fields)}, version=version+1, updated_at=now() WHERE id=%s AND org_id=%s AND workspace_id=%s RETURNING id, org_id, workspace_id, name, description, capabilities, system, status, version, created_by, created_at, updated_at",
                    tuple(params),
                )
            except Exception as exc:
                if "unique" in str(exc).lower():
                    raise HTTPException(status_code=409, detail="Role name already exists") from exc
                raise
            row = await result.fetchone()
            if not row:
                raise _not_found("Role")
            await _audit(conn, actor, "role.updated", "role", role_id, {"version": row["version"]})
    return _role_view(row)


@router.delete("/roles/{role_id}", status_code=204)
async def delete_role(role_id: str, actor: Actor = Depends(get_actor)) -> Response:
    _require_user(actor)
    _require_capability(actor, "role:write")
    _require_high_assurance(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            current = await _get_role(conn, actor, role_id, for_update=True)
            if current["system"]:
                raise HTTPException(status_code=403, detail="System roles cannot be deleted")
            await conn.execute("UPDATE id_role SET status='disabled', version=version+1, updated_at=now() WHERE id=%s AND org_id=%s AND workspace_id=%s", (role_id, actor.org_id, actor.workspace_id))
            await _audit(conn, actor, "role.disabled", "role", role_id, {"previous_status": current["status"]})
    return Response(status_code=204)


async def _validate_binding_subject(conn: Any, actor: Actor, subject_type: str, subject_id: str) -> None:
    if subject_type == "group":
        await _get_group(conn, actor, subject_id)
        return
    if subject_type == "service_account":
        await _get_service_account(conn, actor, subject_id)
        return
    result = await conn.execute(
        """
        SELECT 1 FROM id_user u
        WHERE u.id=%s AND u.status='active' AND (
          EXISTS (SELECT 1 FROM id_org o WHERE o.id=%s AND o.owner_user_id=u.id)
          OR EXISTS (SELECT 1 FROM id_member m WHERE m.org_id=%s AND m.workspace_id=%s AND m.user_id=u.id)
        )
        """,
        (subject_id, actor.org_id, actor.org_id, actor.workspace_id),
    )
    if not await result.fetchone():
        raise _not_found("Binding subject")


@router.get("/role-bindings")
async def list_role_bindings(actor: Actor = Depends(get_actor), include_disabled: bool = Query(default=False)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "role_binding:read")
    status_filter = "" if include_disabled else " AND b.status='active'"
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT b.id, b.org_id, b.workspace_id, b.role_id, r.name AS role_name,
                   b.subject_type, b.subject_id, b.resource_type, b.resource_id,
                   b.conditions, b.status, b.expires_at, b.version, b.created_by,
                   b.created_at, b.updated_at
            FROM id_role_binding b JOIN id_role r ON r.id=b.role_id
            WHERE b.org_id=%s AND b.workspace_id=%s{status_filter}
            ORDER BY b.updated_at DESC, b.id
            """,
            (actor.org_id, actor.workspace_id),
        )
        rows = await result.fetchall()
    # Contract《720》listRoleBindings: ListQuery -> ListResponse<RoleBindingDTO>（保留旧字段 count 向后兼容）
    data = [_binding_view(row) for row in rows]
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("/role-bindings", status_code=201)
async def create_role_binding(body: RoleBindingCreate, actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "role_binding:write")
    _require_high_assurance(actor)
    workspace_id = _workspace_id(actor, body.workspace_id)
    request_hash = _body_hash(body, {"workspace_id": workspace_id})
    async with pool.connection() as conn:
        async with conn.transaction():
            existing, replay = await _idempotent_row(conn, "id_role_binding", "workspace_id", workspace_id, body.idempotency_key, request_hash)
            if existing:
                return _binding_view(existing, replay=replay)
            role = await _get_role(conn, actor, body.role_id)
            if role["status"] != "active":
                raise HTTPException(status_code=409, detail="Role is not active")
            await _validate_binding_subject(conn, actor, body.subject_type, body.subject_id)
            result = await conn.execute(
                """
                INSERT INTO id_role_binding(
                  id, org_id, workspace_id, role_id, subject_type, subject_id,
                  resource_type, resource_id, conditions, expires_at,
                  created_by, create_idempotency_key, request_hash
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
                RETURNING id, org_id, workspace_id, role_id, subject_type, subject_id,
                          resource_type, resource_id, conditions, status, expires_at,
                          version, created_by, created_at, updated_at
                """,
                (new_id("bind"), actor.org_id, workspace_id, body.role_id, body.subject_type, body.subject_id, body.resource_type, body.resource_id, json_dumps(body.conditions), body.expires_at, actor.user_id, body.idempotency_key, request_hash),
            )
            row = await result.fetchone()
            if not row:
                if body.idempotency_key:
                    existing, replay = await _idempotent_row(conn, "id_role_binding", "workspace_id", workspace_id, body.idempotency_key, request_hash)
                    if existing:
                        return _binding_view(existing, replay=replay)
                raise HTTPException(status_code=409, detail="Role binding already exists")
            row = dict(row)
            row["role_name"] = role["name"]
            await _audit(conn, actor, "role_binding.created", "role_binding", row["id"], {"role_id": body.role_id, "subject_type": body.subject_type})
    return _binding_view(row)


@router.get("/role-bindings/{binding_id}")
async def get_role_binding(binding_id: str, actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "role_binding:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT b.id, b.org_id, b.workspace_id, b.role_id, r.name AS role_name,
                   b.subject_type, b.subject_id, b.resource_type, b.resource_id,
                   b.conditions, b.status, b.expires_at, b.version, b.created_by,
                   b.created_at, b.updated_at
            FROM id_role_binding b JOIN id_role r ON r.id=b.role_id
            WHERE b.id=%s AND b.org_id=%s AND b.workspace_id=%s
            """,
            (binding_id, actor.org_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise _not_found("Role binding")
    return _binding_view(row)


@router.patch("/role-bindings/{binding_id}")
async def patch_role_binding(binding_id: str, body: RoleBindingPatch, actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "role_binding:write")
    _require_high_assurance(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                "SELECT id, org_id, workspace_id, resource_type, resource_id FROM id_role_binding WHERE id=%s AND org_id=%s AND workspace_id=%s FOR UPDATE",
                (binding_id, actor.org_id, actor.workspace_id),
            )
            current = await result.fetchone()
            if not current:
                raise _not_found("Role binding")
            resource_type = body.resource_type if body.resource_type is not None else current["resource_type"]
            resource_id = body.resource_id if body.resource_id is not None else current["resource_id"]
            if (resource_type is None) != (resource_id is None):
                raise HTTPException(status_code=422, detail="resource_type and resource_id must be provided together")
            fields: list[str] = []
            params: list[Any] = []
            if body.resource_type is not None or body.resource_id is not None:
                fields.extend(["resource_type=%s", "resource_id=%s"])
                params.extend([resource_type, resource_id])
            if body.conditions is not None:
                fields.append("conditions=%s::jsonb")
                params.append(json_dumps(body.conditions))
            if body.expires_at is not None:
                fields.append("expires_at=%s")
                params.append(body.expires_at)
            if body.status is not None:
                fields.append("status=%s")
                params.append(body.status)
            params.extend([binding_id, actor.org_id, actor.workspace_id])
            result = await conn.execute(
                f"""
                UPDATE id_role_binding SET {', '.join(fields)}, version=version+1, updated_at=now()
                WHERE id=%s AND org_id=%s AND workspace_id=%s
                RETURNING id, org_id, workspace_id, role_id, subject_type, subject_id,
                          resource_type, resource_id, conditions, status, expires_at,
                          version, created_by, created_at, updated_at
                """,
                tuple(params),
            )
            row = await result.fetchone()
            if not row:
                raise _not_found("Role binding")
            role_result = await conn.execute("SELECT name FROM id_role WHERE id=%s AND org_id=%s AND workspace_id=%s", (row["role_id"], actor.org_id, actor.workspace_id))
            role = await role_result.fetchone()
            row = dict(row)
            row["role_name"] = role["name"] if role else None
            await _audit(conn, actor, "role_binding.updated", "role_binding", binding_id, {"version": row["version"]})
    return _binding_view(row)


@router.delete("/role-bindings/{binding_id}", status_code=204)
async def delete_role_binding(binding_id: str, actor: Actor = Depends(get_actor)) -> Response:
    _require_user(actor)
    _require_capability(actor, "role_binding:write")
    _require_high_assurance(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute("UPDATE id_role_binding SET status='disabled', version=version+1, updated_at=now() WHERE id=%s AND org_id=%s AND workspace_id=%s RETURNING id", (binding_id, actor.org_id, actor.workspace_id))
            row = await result.fetchone()
            if not row:
                raise _not_found("Role binding")
            await _audit(conn, actor, "role_binding.disabled", "role_binding", binding_id)
    return Response(status_code=204)


@router.get("/service-account-policies")
async def list_service_account_policies(actor: Actor = Depends(get_actor), include_disabled: bool = Query(default=False)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "service_account_policy:read")
    status_filter = "" if include_disabled else " AND status='active'"
    async with pool.connection() as conn:
        result = await conn.execute(
            f"SELECT id, org_id, workspace_id, service_account_id, allowed_scopes, allowed_ip_cidrs, status, expires_at, version, created_by, created_at, updated_at FROM id_service_account_policy WHERE org_id=%s AND workspace_id=%s{status_filter} ORDER BY updated_at DESC, id",
            (actor.org_id, actor.workspace_id),
        )
        rows = await result.fetchall()
    # Contract《720》listServiceAccountPolicies: ListQuery -> ListResponse<ServiceAccountPolicyDTO>（保留旧字段 count 向后兼容）
    data = [_service_policy_view(row) for row in rows]
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("/service-account-policies", status_code=201)
async def create_service_account_policy(body: ServiceAccountPolicyCreate, actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "service_account_policy:write")
    _require_high_assurance(actor)
    request_hash = _body_hash(body)
    async with pool.connection() as conn:
        async with conn.transaction():
            existing, replay = await _idempotent_row(conn, "id_service_account_policy", "workspace_id", actor.workspace_id, body.idempotency_key, request_hash)
            if existing:
                return _service_policy_view(existing, replay=replay)
            service_account = await _get_service_account(conn, actor, body.service_account_id)
            service_scopes = set(service_account.get("scopes") or [])
            if not set(body.allowed_scopes).issubset(service_scopes):
                raise HTTPException(status_code=422, detail="Policy scope must be a subset of the service-account scopes")
            result = await conn.execute(
                """
                INSERT INTO id_service_account_policy(
                  id, org_id, workspace_id, service_account_id, allowed_scopes,
                  allowed_ip_cidrs, expires_at, created_by, create_idempotency_key, request_hash
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
                RETURNING id, org_id, workspace_id, service_account_id, allowed_scopes,
                          allowed_ip_cidrs, status, expires_at, version, created_by,
                          created_at, updated_at
                """,
                (new_id("sap"), actor.org_id, actor.workspace_id, body.service_account_id, body.allowed_scopes, body.allowed_ip_cidrs, body.expires_at, actor.user_id, body.idempotency_key, request_hash),
            )
            row = await result.fetchone()
            if not row:
                if body.idempotency_key:
                    existing, replay = await _idempotent_row(conn, "id_service_account_policy", "workspace_id", actor.workspace_id, body.idempotency_key, request_hash)
                    if existing:
                        return _service_policy_view(existing, replay=replay)
                raise HTTPException(status_code=409, detail="Service-account policy already exists")
            await _audit(conn, actor, "service_account_policy.created", "service_account_policy", row["id"], {"service_account_id": body.service_account_id})
    return _service_policy_view(row)


@router.get("/service-account-policies/{policy_id}")
async def get_service_account_policy(policy_id: str, actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "service_account_policy:read")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id, org_id, workspace_id, service_account_id, allowed_scopes, allowed_ip_cidrs, status, expires_at, version, created_by, created_at, updated_at FROM id_service_account_policy WHERE id=%s AND org_id=%s AND workspace_id=%s", (policy_id, actor.org_id, actor.workspace_id))
        row = await result.fetchone()
    if not row:
        raise _not_found("Service-account policy")
    return _service_policy_view(row)


@router.patch("/service-account-policies/{policy_id}")
async def patch_service_account_policy(policy_id: str, body: ServiceAccountPolicyPatch, actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "service_account_policy:write")
    _require_high_assurance(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute("SELECT id, service_account_id, allowed_scopes FROM id_service_account_policy WHERE id=%s AND org_id=%s AND workspace_id=%s FOR UPDATE", (policy_id, actor.org_id, actor.workspace_id))
            current = await result.fetchone()
            if not current:
                raise _not_found("Service-account policy")
            if body.allowed_scopes is not None:
                service_account = await _get_service_account(conn, actor, current["service_account_id"])
                if not set(body.allowed_scopes).issubset(set(service_account.get("scopes") or [])):
                    raise HTTPException(status_code=422, detail="Policy scope must be a subset of the service-account scopes")
            fields: list[str] = []
            params: list[Any] = []
            for name in ("allowed_scopes", "allowed_ip_cidrs", "expires_at", "status"):
                value = getattr(body, name)
                if value is not None:
                    fields.append(f"{name}=%s")
                    params.append(value)
            params.extend([policy_id, actor.org_id, actor.workspace_id])
            result = await conn.execute(
                f"UPDATE id_service_account_policy SET {', '.join(fields)}, version=version+1, updated_at=now() WHERE id=%s AND org_id=%s AND workspace_id=%s RETURNING id, org_id, workspace_id, service_account_id, allowed_scopes, allowed_ip_cidrs, status, expires_at, version, created_by, created_at, updated_at",
                tuple(params),
            )
            row = await result.fetchone()
            if not row:
                raise _not_found("Service-account policy")
            await _audit(conn, actor, "service_account_policy.updated", "service_account_policy", policy_id, {"version": row["version"]})
    return _service_policy_view(row)


@router.delete("/service-account-policies/{policy_id}", status_code=204)
async def delete_service_account_policy(policy_id: str, actor: Actor = Depends(get_actor)) -> Response:
    _require_user(actor)
    _require_capability(actor, "service_account_policy:write")
    _require_high_assurance(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute("UPDATE id_service_account_policy SET status='disabled', version=version+1, updated_at=now() WHERE id=%s AND org_id=%s AND workspace_id=%s RETURNING id", (policy_id, actor.org_id, actor.workspace_id))
            row = await result.fetchone()
            if not row:
                raise _not_found("Service-account policy")
            await _audit(conn, actor, "service_account_policy.disabled", "service_account_policy", policy_id)
    return Response(status_code=204)


@router.post("/service-account-policies/{policy_id}/evaluate")
async def evaluate_service_account_policy(policy_id: str, body: ServiceAccountPolicyEvaluate, actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "service_account_policy:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT p.id, p.service_account_id, p.allowed_scopes, p.allowed_ip_cidrs,
                   p.status, p.expires_at, sa.status AS service_account_status,
                   sa.expires_at AS service_account_expires_at
            FROM id_service_account_policy p
            JOIN id_service_account sa ON sa.id=p.service_account_id
            WHERE p.id=%s AND p.org_id=%s AND p.workspace_id=%s
            """,
            (policy_id, actor.org_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise _not_found("Service-account policy")
    now = _now()
    allowed = row["status"] == "active" and row["service_account_status"] == "active" and body.scope in set(row["allowed_scopes"] or [])
    reason = "allowed"
    if row["status"] != "active" or row["service_account_status"] != "active":
        reason = "inactive"
        allowed = False
    elif row["expires_at"] and _normalize_datetime(row["expires_at"]) <= now:
        reason = "policy_expired"
        allowed = False
    elif row["service_account_expires_at"] and _normalize_datetime(row["service_account_expires_at"]) <= now:
        reason = "service_account_expired"
        allowed = False
    elif body.scope not in set(row["allowed_scopes"] or []):
        reason = "scope_not_allowed"
        allowed = False
    elif not body.source_ip:
        reason = "source_ip_required"
        allowed = False
    else:
        source_ip = ipaddress.ip_address(body.source_ip)
        networks = [ipaddress.ip_network(value) for value in row["allowed_ip_cidrs"] or []]
        if not any(source_ip in network for network in networks):
            reason = "source_ip_not_allowed"
            allowed = False
    return {"allowed": allowed, "reason": reason, "fail_closed": not allowed}


@router.get("/auth-strength-matrix")
async def list_auth_strength_policies(actor: Actor = Depends(get_actor), include_disabled: bool = Query(default=False)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "auth_policy:read")
    status_filter = "" if include_disabled else " AND status='active'"
    async with pool.connection() as conn:
        result = await conn.execute(
            f"SELECT id, org_id, workspace_id, operation, required_auth_strength, status, version, created_by, created_at, updated_at FROM id_auth_strength_policy WHERE org_id=%s AND workspace_id=%s{status_filter} ORDER BY operation, id",
            (actor.org_id, actor.workspace_id),
        )
        rows = await result.fetchall()
    # Contract《720》listAuthStrengthPolicies: ListQuery -> ListResponse<AuthStrengthPolicyDTO>（保留旧字段 count 向后兼容）
    data = [_auth_policy_view(row) for row in rows]
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("/auth-strength-matrix", status_code=201)
async def create_auth_strength_policy(body: AuthStrengthPolicyCreate, actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "auth_policy:write")
    _require_high_assurance(actor)
    request_hash = _body_hash(body)
    async with pool.connection() as conn:
        async with conn.transaction():
            existing, replay = await _idempotent_row(conn, "id_auth_strength_policy", "workspace_id", actor.workspace_id, body.idempotency_key, request_hash)
            if existing:
                return _auth_policy_view(existing, replay=replay)
            result = await conn.execute(
                """
                INSERT INTO id_auth_strength_policy(
                  id, org_id, workspace_id, operation, required_auth_strength,
                  created_by, create_idempotency_key, request_hash
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
                RETURNING id, org_id, workspace_id, operation, required_auth_strength,
                          status, version, created_by, created_at, updated_at
                """,
                (new_id("authp"), actor.org_id, actor.workspace_id, body.operation, body.required_auth_strength, actor.user_id, body.idempotency_key, request_hash),
            )
            row = await result.fetchone()
            if not row:
                if body.idempotency_key:
                    existing, replay = await _idempotent_row(conn, "id_auth_strength_policy", "workspace_id", actor.workspace_id, body.idempotency_key, request_hash)
                    if existing:
                        return _auth_policy_view(existing, replay=replay)
                raise HTTPException(status_code=409, detail="Auth-strength policy already exists")
            await _audit(conn, actor, "auth_strength_policy.created", "auth_strength_policy", row["id"], {"operation": body.operation, "required_auth_strength": body.required_auth_strength})
    return _auth_policy_view(row)


@router.get("/auth-strength-matrix/{policy_id}")
async def get_auth_strength_policy(policy_id: str, actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "auth_policy:read")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id, org_id, workspace_id, operation, required_auth_strength, status, version, created_by, created_at, updated_at FROM id_auth_strength_policy WHERE id=%s AND org_id=%s AND workspace_id=%s", (policy_id, actor.org_id, actor.workspace_id))
        row = await result.fetchone()
    if not row:
        raise _not_found("Auth-strength policy")
    return _auth_policy_view(row)


@router.patch("/auth-strength-matrix/{policy_id}")
async def patch_auth_strength_policy(policy_id: str, body: AuthStrengthPolicyPatch, actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "auth_policy:write")
    _require_high_assurance(actor)
    fields: list[str] = []
    params: list[Any] = []
    for name in ("required_auth_strength", "status"):
        value = getattr(body, name)
        if value is not None:
            fields.append(f"{name}=%s")
            params.append(value)
    params.extend([policy_id, actor.org_id, actor.workspace_id])
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                f"UPDATE id_auth_strength_policy SET {', '.join(fields)}, version=version+1, updated_at=now() WHERE id=%s AND org_id=%s AND workspace_id=%s RETURNING id, org_id, workspace_id, operation, required_auth_strength, status, version, created_by, created_at, updated_at",
                tuple(params),
            )
            row = await result.fetchone()
            if not row:
                raise _not_found("Auth-strength policy")
            await _audit(conn, actor, "auth_strength_policy.updated", "auth_strength_policy", policy_id, {"version": row["version"]})
    return _auth_policy_view(row)


@router.delete("/auth-strength-matrix/{policy_id}", status_code=204)
async def delete_auth_strength_policy(policy_id: str, actor: Actor = Depends(get_actor)) -> Response:
    _require_user(actor)
    _require_capability(actor, "auth_policy:write")
    _require_high_assurance(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute("UPDATE id_auth_strength_policy SET status='disabled', version=version+1, updated_at=now() WHERE id=%s AND org_id=%s AND workspace_id=%s RETURNING id", (policy_id, actor.org_id, actor.workspace_id))
            row = await result.fetchone()
            if not row:
                raise _not_found("Auth-strength policy")
            await _audit(conn, actor, "auth_strength_policy.disabled", "auth_strength_policy", policy_id)
    return Response(status_code=204)


@router.post("/auth-strength-matrix/evaluate")
async def evaluate_auth_strength(body: AuthStrengthEvaluate, actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "auth_policy:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT id, operation, required_auth_strength, status FROM id_auth_strength_policy WHERE org_id=%s AND workspace_id=%s AND operation=%s AND status='active' LIMIT 1",
            (actor.org_id, actor.workspace_id, body.operation),
        )
        row = await result.fetchone()
    if not row:
        return {"allowed": False, "reason": "policy_missing", "fail_closed": True, "operation": body.operation}
    required = int(row["required_auth_strength"])
    allowed = actor.auth_strength >= required
    return {
        "allowed": allowed,
        "reason": "allowed" if allowed else "auth_strength_insufficient",
        "fail_closed": not allowed,
        "operation": body.operation,
        "required_auth_strength": required,
        "actual_auth_strength": actor.auth_strength,
        "policy_id": row["id"],
    }


async def resolve_effective_capabilities(conn: Any, actor: Actor) -> tuple[str, ...]:
    """Resolve local role/group bindings for an actor without crossing the tenant boundary."""
    if actor.actor_type != "user" or not actor.org_id or not actor.workspace_id:
        return tuple()
    result = await conn.execute(
        """
        SELECT r.capabilities
        FROM id_role_binding b
        JOIN id_role r ON r.id=b.role_id
        WHERE b.org_id=%s AND b.workspace_id=%s AND r.org_id=%s AND r.workspace_id=%s
          AND r.status='active' AND b.status='active'
          AND (b.expires_at IS NULL OR b.expires_at>now())
          AND (
            (b.subject_type='user' AND b.subject_id=%s)
            OR (b.subject_type='group' AND EXISTS (
              SELECT 1 FROM id_group_member gm
              WHERE gm.group_id=b.subject_id AND gm.org_id=%s AND gm.user_id=%s AND gm.status='active'
            ))
          )
        """,
        (actor.org_id, actor.workspace_id, actor.org_id, actor.workspace_id, actor.user_id, actor.org_id, actor.user_id),
    )
    capabilities = set(actor.capabilities)
    for row in await result.fetchall():
        capabilities.update(row.get("capabilities") or [])
    return tuple(sorted(capabilities))


async def service_account_policy_allows(conn: Any, *, org_id: str, workspace_id: str, service_account_id: str, scope: str, source_ip: str | None) -> bool:
    """Fail-closed policy helper for service-account middleware integration."""
    try:
        parsed_scope = _normalize_scopes([scope])[0]
        parsed_ip = ipaddress.ip_address(source_ip) if source_ip else None
    except ValueError:
        return False
    result = await conn.execute(
        """
        SELECT p.allowed_scopes, p.allowed_ip_cidrs, p.status, p.expires_at,
               sa.status AS service_account_status, sa.expires_at AS service_account_expires_at
        FROM id_service_account_policy p
        JOIN id_service_account sa ON sa.id=p.service_account_id
        WHERE p.id IS NOT NULL AND p.org_id=%s AND p.workspace_id=%s AND p.service_account_id=%s
        LIMIT 1
        """,
        (org_id, workspace_id, service_account_id),
    )
    row = await result.fetchone()
    if not row or row["status"] != "active" or row["service_account_status"] != "active":
        return False
    now = _now()
    if row["expires_at"] and _normalize_datetime(row["expires_at"]) <= now:
        return False
    if row["service_account_expires_at"] and _normalize_datetime(row["service_account_expires_at"]) <= now:
        return False
    if parsed_scope not in set(row["allowed_scopes"] or []) or parsed_ip is None:
        return False
    return any(parsed_ip in ipaddress.ip_network(value) for value in row["allowed_ip_cidrs"] or [])
