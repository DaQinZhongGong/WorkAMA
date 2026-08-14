from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from workama_platform.core import Actor, capability_allows, get_actor, hash_secret, json_dumps, new_id, pool


router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])

Provider = Literal["mock", "local"]
AuthMode = Literal["none", "oauth", "service_account"]
ConnectorStatus = Literal["active", "disabled", "pending", "revoked"]
SyncMode = Literal["full", "incremental"]

IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}$")
SECRET_VALUE_PATTERN = re.compile(
    r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|private[_-]?key)\s*[:=]\s*[^\s,;]+"
)
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
        "credential",
        "credentials",
    }
)
ALLOWED_CREDENTIAL_KEYS = frozenset(
    {
        "client_id",
        "client_secret",
        "access_token",
        "refresh_token",
        "service_account_id",
        "service_account_email",
        "service_account_key",
        "credential_ref",
    }
)
PRINCIPAL_TYPES = ("user", "group", "role")


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _json_object(value: Any, *, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} must contain valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _safe_principal(value: str, *, field: str) -> str:
    value = value.strip()
    if not value or len(value) > 256 or any(ord(char) < 32 for char in value):
        raise ValueError(f"{field} is invalid")
    if "\\" in value or ".." in value or "://" in value or value.startswith("/"):
        raise ValueError(f"{field} contains an unsafe path or URL")
    return value


def _safe_source_id(value: str) -> str:
    value = value.strip()
    if not SOURCE_ID_PATTERN.fullmatch(value) or "\\" in value or ".." in value or "://" in value:
        raise ValueError("source_id is invalid")
    return value


def _is_sensitive_key(value: str) -> bool:
    normalized = value.strip().lower().replace("-", "_")
    return normalized in SENSITIVE_KEYS or any(token in normalized for token in ("secret", "password", "token"))


def _validate_manifest_safety(value: Any, path: str = "manifest") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            normalized = key_text.strip().lower().replace("-", "_")
            if _is_sensitive_key(normalized):
                raise ValueError(f"manifest contains a secret-like field at {path}.{key_text}")
            if normalized in {"url", "endpoint", "path", "file_path", "command", "headers", "auth"}:
                raise ValueError(f"manifest contains an unsupported external field at {path}.{key_text}")
            _validate_manifest_safety(item, f"{path}.{key_text}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_manifest_safety(item, f"{path}[{index}]")
        return
    if isinstance(value, str) and not path.endswith((".content", ".content_ref")):
        if SECRET_VALUE_PATTERN.search(value) or re.search(r"(?i)\b(?:https?|ftp|file|data)://", value):
            raise ValueError(f"manifest contains an unsafe URL or secret at {path}")
        if "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value):
            raise ValueError(f"manifest contains an unsafe path at {path}")


class AccessControl(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    allow_users: list[str] = Field(default_factory=list, validation_alias="users")
    allow_groups: list[str] = Field(default_factory=list, validation_alias="groups")
    allow_roles: list[str] = Field(default_factory=list, validation_alias="roles")

    @field_validator("allow_users", "allow_groups", "allow_roles")
    @classmethod
    def validate_principals(cls, values: list[str], info) -> list[str]:
        normalized: list[str] = []
        field = info.field_name
        for value in values:
            item = _safe_principal(value, field=field)
            if item not in normalized:
                normalized.append(item)
        return sorted(normalized)

    def normalized(self) -> dict[str, list[str]]:
        return {
            "allow_users": sorted(set(self.allow_users)),
            "allow_groups": sorted(set(self.allow_groups)),
            "allow_roles": sorted(set(self.allow_roles)),
        }


class SourceItem(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    source_id: str = Field(min_length=1, max_length=256)
    source_version: str | int = Field(default="1")
    title: str = Field(default="Untitled source", min_length=1, max_length=500)
    content: str | None = Field(default=None, max_length=1_000_000)
    content_ref: str | None = Field(default=None, max_length=240)
    etag: str | None = Field(default=None, max_length=256)
    updated_at: datetime | None = None
    acl: AccessControl | None = None
    deleted: bool = False
    revoked: bool = False

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        return _safe_source_id(value)

    @field_validator("source_version", "etag")
    @classmethod
    def normalize_version_fields(cls, value: str | int | None) -> str | None:
        if value is None:
            return None
        value = str(value).strip()
        if not value or len(value) > 256 or any(ord(char) < 32 for char in value):
            raise ValueError("source version or etag is invalid")
        if "\\" in value or ".." in value or "://" in value:
            raise ValueError("source version or etag contains an unsafe value")
        return value

    @field_validator("content_ref")
    @classmethod
    def validate_content_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        validate_controlled_reference(value)
        return value

    @model_validator(mode="after")
    def validate_source_payload(self) -> "SourceItem":
        if self.content is None and self.content_ref is None and not (self.deleted or self.revoked):
            raise ValueError("active source item requires content or content_ref")
        if self.deleted and self.revoked:
            raise ValueError("source item cannot be both deleted and revoked")
        return self

    def normalized(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", by_alias=False)
        data["source_version"] = str(data["source_version"])
        data["acl"] = self.acl.normalized() if self.acl else None
        return data


class ConnectorManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: int = Field(default=1, ge=1, le=1)
    description: str = Field(default="", max_length=4_000)
    cursor_field: Literal["updated_at", "etag"] = "updated_at"
    documents: list[SourceItem] = Field(default_factory=list, max_length=1_000)


class ConnectorCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=120)
    provider: Provider
    auth_mode: AuthMode = "none"
    endpoint: str | None = Field(default=None, max_length=240)
    manifest: dict[str, Any] = Field(default_factory=dict)
    credentials: dict[str, str] | None = None
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("name is required")
        return value

    @field_validator("manifest")
    @classmethod
    def validate_manifest_field(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_manifest(value)

    @field_validator("credentials")
    @classmethod
    def validate_credentials_field(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return normalize_credentials(value)

    @model_validator(mode="after")
    def validate_definition(self) -> "ConnectorCreate":
        endpoint = self.endpoint or default_endpoint(self.provider, self.name)
        validate_endpoint(endpoint, self.provider)
        normalize_credentials(self.credentials, auth_mode=self.auth_mode)
        if self.provider == "local" and not endpoint:
            raise ValueError("local connector endpoint is required")
        return self


class ConnectorPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=2, max_length=120)
    endpoint: str | None = Field(default=None, max_length=240)
    manifest: dict[str, Any] | None = None
    credentials: dict[str, str] | None = None
    enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        return " ".join(value.split()) if value is not None else None

    @field_validator("manifest")
    @classmethod
    def validate_manifest_field(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return validate_manifest(value) if value is not None else None

    @field_validator("credentials")
    @classmethod
    def validate_credentials_field(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return normalize_credentials(value)


class ConnectorSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: SyncMode = "incremental"


class IdentityMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    external_type: Literal["user", "group", "role"]
    external_id: str = Field(min_length=1, max_length=256)
    principal_type: Literal["user", "group", "role"]
    principal_id: str = Field(min_length=1, max_length=256)
    enabled: bool = True

    @field_validator("external_id", "principal_id")
    @classmethod
    def validate_mapping_ids(cls, value: str, info) -> str:
        return _safe_principal(value, field=info.field_name)


def validate_manifest(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("manifest must be an object")
    _validate_manifest_safety(value)
    try:
        manifest = ConnectorManifest.model_validate(value)
    except ValidationError as exc:
        raise ValueError(f"invalid connector manifest: {exc.errors()[0]['msg']}") from exc
    normalized = manifest.model_dump(mode="json")
    normalized["documents"] = [SourceItem.model_validate(item).normalized() for item in manifest.documents]
    return normalized


def validate_controlled_reference(value: str) -> str:
    if not isinstance(value, str) or value != value.strip() or "\\" in value or "\x00" in value:
        raise ValueError("controlled reference is unsafe")
    parsed = urlsplit(value)
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("controlled reference must not contain query, fragment, or credentials")
    segments = parsed.path.split("/")[1:] if parsed.path.startswith("/") else []
    if parsed.scheme != "local" or parsed.netloc not in {"artifact", "document"} or len(segments) != 1:
        raise ValueError("only local://artifact/<id> or local://document/<id> is allowed")
    if not TOKEN_PATTERN.fullmatch(segments[0]):
        raise ValueError("controlled reference contains an unsafe identifier")
    return value


def default_endpoint(provider: Provider, name: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip(".-")[:64] or "connector"
    return f"mock://connector/{slug}" if provider == "mock" else f"local://artifact/{slug}"


def validate_endpoint(value: str, provider: Provider) -> str:
    if not isinstance(value, str) or value != value.strip() or not value or len(value) > 240:
        raise ValueError("connector endpoint is required and must be normalized")
    if "\\" in value or "\x00" in value:
        raise ValueError("connector endpoint contains an unsafe path")
    parsed = urlsplit(value)
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("connector endpoint must not contain query, fragment, or credentials")
    segments = parsed.path.split("/")[1:] if parsed.path.startswith("/") else []
    if provider == "mock":
        if parsed.scheme != "mock" or parsed.netloc not in {"connector", "source"} or len(segments) != 1:
            raise ValueError("only controlled mock://connector/<id> endpoints are allowed")
    elif provider == "local":
        validate_controlled_reference(value)
    else:  # pragma: no cover - Provider is a closed Literal
        raise ValueError("unsupported connector provider")
    if not TOKEN_PATTERN.fullmatch(segments[0]):
        raise ValueError("connector endpoint identifier is unsafe")
    return value


def normalize_credentials(
    value: dict[str, str] | None,
    *,
    auth_mode: AuthMode | None = None,
) -> dict[str, str]:
    credentials = dict(value or {})
    for key, raw in credentials.items():
        if key not in ALLOWED_CREDENTIAL_KEYS:
            raise ValueError(f"unsupported credential field: {key}")
        if not isinstance(raw, str) or not raw or len(raw) > 4_096 or any(ord(char) < 32 for char in raw):
            raise ValueError(f"credential field {key} is invalid")
        if key == "credential_ref" and not TOKEN_PATTERN.fullmatch(raw):
            raise ValueError("credential_ref must be a safe reference identifier")
    if auth_mode == "none" and credentials:
        raise ValueError("credentials are not allowed when auth_mode is none")
    return credentials


def credential_hash(value: dict[str, str] | None) -> str | None:
    credentials = normalize_credentials(value)
    return hash_secret(canonical_hash(credentials)) if credentials else None


def _connector_status(auth_mode: AuthMode, enabled: bool) -> ConnectorStatus:
    if not enabled:
        return "disabled"
    return "active" if auth_mode == "none" else "pending"


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat()
    return str(value)


def _manifest_from_connector(connector: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(connector.get("manifest"))


def _default_acl() -> dict[str, list[str]]:
    return {"allow_users": [], "allow_groups": [], "allow_roles": ["owner", "admin", "member", "viewer"]}


def mock_source_items(connector: dict[str, Any]) -> list[dict[str, Any]]:
    manifest = _manifest_from_connector(connector)
    configured = manifest.get("documents") or []
    if configured:
        return [SourceItem.model_validate(item).normalized() for item in configured]
    name = str(connector.get("name") or "mock connector")
    connector_id = str(connector.get("id") or canonical_hash(name)[:12])
    timestamp = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
    return [
        SourceItem(
            source_id=f"mock:{connector_id}:overview",
            source_version="1",
            title=f"{name} overview",
            content=f"Deterministic WorkAMA mock document for {name}.",
            etag=canonical_hash({"connector": connector_id, "document": "overview"}),
            updated_at=timestamp,
            acl=AccessControl(users=[], groups=[], roles=["owner", "admin", "member", "viewer"]),
        ).normalized(),
        SourceItem(
            source_id=f"mock:{connector_id}:runbook",
            source_version="1",
            title=f"{name} runbook",
            content=f"Controlled mock sync source for connector {connector_id}.",
            etag=canonical_hash({"connector": connector_id, "document": "runbook"}),
            updated_at=timestamp,
            acl=AccessControl(users=[], groups=[], roles=["owner", "admin", "member", "viewer"]),
        ).normalized(),
    ]


def local_source_items(connector: dict[str, Any]) -> list[dict[str, Any]]:
    manifest = _manifest_from_connector(connector)
    configured = manifest.get("documents") or []
    if configured:
        return [SourceItem.model_validate(item).normalized() for item in configured]
    endpoint = validate_controlled_reference(str(connector.get("endpoint_ref") or ""))
    identifier = endpoint.rsplit("/", 1)[-1]
    return [
        SourceItem(
            source_id=f"local:{identifier}",
            source_version="1",
            title=f"Controlled local reference {identifier}",
            content_ref=endpoint,
            etag=canonical_hash({"endpoint": endpoint}),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
            acl=AccessControl(users=[], groups=[], roles=["owner", "admin", "member", "viewer"]),
        ).normalized()
    ]


def source_items_for_connector(connector: dict[str, Any]) -> list[dict[str, Any]]:
    if connector.get("provider") == "mock":
        return mock_source_items(connector)
    if connector.get("provider") == "local":
        return local_source_items(connector)
    raise HTTPException(status_code=422, detail="Connector provider is not supported")


def _source_item_cursor(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_version": str(item.get("source_version") or ""),
        "etag": item.get("etag"),
        "updated_at": _iso(item.get("updated_at")),
        "status": "revoked" if item.get("revoked") else "tombstone" if item.get("deleted") else "active",
    }


def source_cursor(items: list[dict[str, Any]]) -> dict[str, Any]:
    source_map = {str(item["source_id"]): _source_item_cursor(item) for item in items}
    timestamps = [value["updated_at"] for value in source_map.values() if value.get("updated_at")]
    return {
        "updated_at": max(timestamps) if timestamps else None,
        "etag": canonical_hash(source_map),
        "sources": source_map,
    }


def incremental_source_items(items: list[dict[str, Any]], cursor: dict[str, Any] | None) -> list[dict[str, Any]]:
    previous = _as_dict(cursor)
    previous_sources = _as_dict(previous.get("sources"))
    if not previous_sources:
        return items
    changed: list[dict[str, Any]] = []
    for item in items:
        source_id = str(item["source_id"])
        if _source_item_cursor(item) != _as_dict(previous_sources.get(source_id)):
            changed.append(item)
    return changed


def snapshot_for_sync(connector: dict[str, Any], mode: SyncMode) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items = source_items_for_connector(connector)
    complete_cursor = source_cursor(items)
    if mode == "full":
        return items, complete_cursor
    return incremental_source_items(items, _as_dict(connector.get("source_cursor"))), complete_cursor


def document_visible(
    document: dict[str, Any],
    *,
    user_id: str | None,
    roles: list[str] | tuple[str, ...] | None = None,
    groups: list[str] | tuple[str, ...] | None = None,
    workspace_id: str | None = None,
) -> bool:
    if workspace_id is not None and document.get("workspace_id") != workspace_id:
        return False
    if document.get("status") != "active":
        return False
    acl = _as_dict(document.get("acl"))
    if not acl:
        return False
    users = set(acl.get("allow_users") or acl.get("users") or [])
    groups_allowed = set(acl.get("allow_groups") or acl.get("groups") or [])
    roles_allowed = set(acl.get("allow_roles") or acl.get("roles") or [])
    if user_id and user_id in users:
        return True
    if groups_allowed.intersection(groups or ()):
        return True
    if roles_allowed.intersection(roles or ()):
        return True
    return False


def visible_documents(
    documents: list[dict[str, Any]],
    *,
    user_id: str | None,
    roles: list[str] | tuple[str, ...] | None = None,
    groups: list[str] | tuple[str, ...] | None = None,
    workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    return [
        document
        for document in documents
        if document_visible(document, user_id=user_id, roles=roles, groups=groups, workspace_id=workspace_id)
    ]


def _redact_text(value: str | None) -> str | None:
    if value is None:
        return None
    return SECRET_VALUE_PATTERN.sub("<redacted>", value)


def _require(actor: Actor, action: Literal["read", "write"]) -> None:
    required = f"connector:{action}"
    if capability_allows(actor.capabilities, required):
        return
    if actor.actor_type != "user":
        raise HTTPException(status_code=403, detail=f"Missing capability: {required}")
    if action == "read" and actor.role in {"owner", "admin", "member", "viewer"}:
        return
    if action == "write" and actor.role in {"owner", "admin", "member"}:
        return
    raise HTTPException(status_code=403, detail=f"Missing capability: {required}")


def _normalize_idempotency_key(value: str | None, default: str) -> tuple[str, str]:
    key = value.strip() if value else default
    if not key or len(key) > 128 or any(char.isspace() for char in key):
        raise HTTPException(status_code=422, detail="Idempotency-Key must be 1-128 non-space characters")
    return key, hash_secret(key)


def _check_idempotency(existing_input_hash: str | None, input_hash: str) -> None:
    if existing_input_hash and existing_input_hash != input_hash:
        raise HTTPException(status_code=409, detail="Idempotency-Key was used for different connector input")


def _connector_view(row: dict[str, Any]) -> dict[str, Any]:
    manifest = _as_dict(row.get("manifest"))
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "name": row["name"],
        "provider": row["provider"],
        "auth_mode": row["auth_mode"],
        "endpoint": row.get("endpoint_ref"),
        "manifest": manifest,
        "credential_configured": bool(row.get("credential_hash") or row.get("credential_ref")),
        "status": row["status"],
        "enabled": row.get("enabled", row["status"] == "active"),
        "source_cursor": _as_dict(row.get("source_cursor")),
        "last_sync_at": row.get("last_sync_at"),
        "version": row.get("version", 1),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _run_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "connector_id": row["connector_id"],
        "workspace_id": row["workspace_id"],
        "mode": row["mode"],
        "idempotency_key": row.get("idempotency_key", "<hidden>"),
        "status": row["status"],
        "execution_status": row.get("execution_status"),
        "executed": row.get("executed", False),
        "source_cursor_before": _as_dict(row.get("source_cursor_before")),
        "source_cursor_after": _as_dict(row.get("source_cursor_after")),
        "documents_seen": row.get("documents_seen", 0),
        "documents_upserted": row.get("documents_upserted", 0),
        "documents_tombstoned": row.get("documents_tombstoned", 0),
        "documents_revoked": row.get("documents_revoked", 0),
        "error_code": row.get("error_code"),
        "error_message": _redact_text(row.get("error_message")),
        "created_at": row.get("created_at"),
        "completed_at": row.get("completed_at"),
    }


def _document_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "connector_id": row["connector_id"],
        "workspace_id": row["workspace_id"],
        "source_id": row["source_id"],
        "source_version": row["source_version"],
        "etag": row.get("source_etag"),
        "source_updated_at": row.get("source_updated_at"),
        "title": row["title"],
        "content": row.get("content"),
        "content_ref": row.get("content_ref"),
        "content_sha256": row.get("content_sha256"),
        "acl": _as_dict(row.get("acl")),
        "status": row["status"],
        "version": row.get("version", 1),
        "updated_at": row.get("updated_at"),
    }


def _mapping_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "connector_id": row["connector_id"],
        "workspace_id": row["workspace_id"],
        "external_type": row["external_type"],
        "external_id": row["external_id"],
        "principal_type": row["principal_type"],
        "principal_id": row["principal_id"],
        "enabled": row["enabled"],
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


async def ensure_connectors_schema(conn) -> None:
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


async def _outbox(conn, event_type: str, workspace_id: str, trace_id: str, payload: dict[str, Any]) -> None:
    await conn.execute(
        "INSERT INTO ops_outbox(id,event_type,workspace_id,trace_id,payload) VALUES (%s,%s,%s,%s,%s::jsonb)",
        (new_id("out"), event_type, workspace_id, trace_id, json_dumps(payload)),
    )


async def _get_connector(conn, connector_id: str, workspace_id: str, *, for_update: bool = False) -> dict[str, Any]:
    lock = " FOR UPDATE" if for_update else ""
    result = await conn.execute(
        f"SELECT * FROM pf_connector WHERE id=%s AND workspace_id=%s AND status <> 'revoked'{lock}",
        (connector_id, workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Connector was not found in this workspace")
    return row


def _source_item_status(item: dict[str, Any]) -> str:
    if item.get("revoked"):
        return "revoked"
    if item.get("deleted"):
        return "tombstone"
    if item.get("acl") is not None:
        acl = _source_item_acl(item)
        if not any(acl.values()):
            return "revoked"
    return "active"


def _source_item_acl(item: dict[str, Any]) -> dict[str, list[str]]:
    acl = item.get("acl")
    if acl is None:
        return {"allow_users": [], "allow_groups": [], "allow_roles": []}
    try:
        return AccessControl.model_validate(acl).normalized()
    except ValidationError:
        return {"allow_users": [], "allow_groups": [], "allow_roles": []}


async def _upsert_document_projection(conn, connector: dict[str, Any], run_id: str, item: dict[str, Any]) -> str:
    status_value = _source_item_status(item)
    acl = _source_item_acl(item)
    content = None if status_value != "active" else item.get("content")
    content_ref = None if status_value != "active" else item.get("content_ref")
    content_hash = canonical_hash(content if content is not None else content_ref or "")
    result = await conn.execute(
        """
        INSERT INTO pf_connector_document(
          id,org_id,workspace_id,connector_id,source_id,source_version,source_etag,source_updated_at,
          title,content,content_ref,content_sha256,acl,status,last_run_id
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
        ON CONFLICT(connector_id,source_id) DO UPDATE SET
          source_version=EXCLUDED.source_version,source_etag=EXCLUDED.source_etag,
          source_updated_at=EXCLUDED.source_updated_at,title=EXCLUDED.title,content=EXCLUDED.content,
          content_ref=EXCLUDED.content_ref,content_sha256=EXCLUDED.content_sha256,acl=EXCLUDED.acl,
          status=EXCLUDED.status,last_run_id=EXCLUDED.last_run_id,version=pf_connector_document.version+1,updated_at=now()
        RETURNING id
        """,
        (
            new_id("cndoc"), connector["org_id"], connector["workspace_id"], connector["id"],
            item["source_id"], str(item.get("source_version") or "1"), item.get("etag"), item.get("updated_at"),
            item.get("title") or "Untitled source", content, content_ref, content_hash, json_dumps(acl), status_value, run_id,
        ),
    )
    document = await result.fetchone()
    document_id = document["id"] if document else new_id("cndoc")
    await conn.execute(
        "DELETE FROM pf_connector_document_acl WHERE document_id=%s AND workspace_id=%s",
        (document_id, connector["workspace_id"]),
    )
    for principal_type, values in (
        ("user", acl["allow_users"]),
        ("group", acl["allow_groups"]),
        ("role", acl["allow_roles"]),
    ):
        for principal_id in values:
            await conn.execute(
                """
                INSERT INTO pf_connector_document_acl(
                  id,org_id,workspace_id,connector_id,document_id,principal_type,principal_id,effect
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,'allow')
                ON CONFLICT(document_id,principal_type,principal_id) DO UPDATE SET revoked_at=NULL,effect='allow'
                """,
                (
                    new_id("cacl"), connector["org_id"], connector["workspace_id"], connector["id"],
                    document_id, principal_type, principal_id,
                ),
            )
    return status_value


async def _tombstone_missing_documents(
    conn,
    connector: dict[str, Any],
    run_id: str,
    seen_source_ids: set[str],
) -> int:
    result = await conn.execute(
        """
        SELECT id,source_id FROM pf_connector_document
        WHERE connector_id=%s AND workspace_id=%s AND status='active'
        """,
        (connector["id"], connector["workspace_id"]),
    )
    rows = await result.fetchall()
    missing = [row for row in rows if row["source_id"] not in seen_source_ids]
    for row in missing:
        await conn.execute(
            """
            UPDATE pf_connector_document SET status='tombstone',content=NULL,content_ref=NULL,
              version=version+1, last_run_id=%s, updated_at=now()
            WHERE id=%s AND connector_id=%s AND workspace_id=%s AND status='active'
            """,
            (run_id, row["id"], connector["id"], connector["workspace_id"]),
        )
        await conn.execute(
            "UPDATE pf_connector_document_acl SET revoked_at=now() WHERE document_id=%s AND workspace_id=%s AND revoked_at IS NULL",
            (row["id"], connector["workspace_id"]),
        )
        await _outbox(
            conn,
            "connector.document.revoked.v1",
            connector["workspace_id"],
            run_id,
            {
                "connector_id": connector["id"],
                "run_id": run_id,
                "source_id": row["source_id"],
                "reason": "source_missing_from_full_snapshot",
            },
        )
    return len(missing)


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS pf_connector (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      name TEXT NOT NULL,
      provider TEXT NOT NULL CHECK (provider IN ('mock','local')),
      auth_mode TEXT NOT NULL CHECK (auth_mode IN ('none','oauth','service_account')),
      endpoint_ref TEXT NOT NULL,
      manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
      credential_ref TEXT,
      credential_hash TEXT,
      status TEXT NOT NULL DEFAULT 'disabled' CHECK (status IN ('active','disabled','pending','revoked')),
      enabled BOOLEAN NOT NULL DEFAULT FALSE,
      source_cursor JSONB NOT NULL DEFAULT '{}'::jsonb,
      last_sync_at TIMESTAMPTZ,
      version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id,name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pf_connector_run (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      connector_id TEXT NOT NULL REFERENCES pf_connector(id) ON DELETE CASCADE,
      mode TEXT NOT NULL CHECK (mode IN ('full','incremental')),
      idempotency_key TEXT NOT NULL,
      idempotency_key_hash TEXT NOT NULL,
      input_hash TEXT NOT NULL,
      source_cursor_before JSONB NOT NULL DEFAULT '{}'::jsonb,
      source_cursor_after JSONB NOT NULL DEFAULT '{}'::jsonb,
      status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','succeeded','failed','unsupported','cancelled')),
      execution_status TEXT NOT NULL DEFAULT 'pending' CHECK (execution_status IN ('pending','executed','unsupported')),
      executed BOOLEAN NOT NULL DEFAULT FALSE,
      documents_seen INTEGER NOT NULL DEFAULT 0,
      documents_upserted INTEGER NOT NULL DEFAULT 0,
      documents_tombstoned INTEGER NOT NULL DEFAULT 0,
      documents_revoked INTEGER NOT NULL DEFAULT 0,
      error_code TEXT,
      error_message TEXT,
      created_by TEXT REFERENCES id_user(id) ON DELETE SET NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      completed_at TIMESTAMPTZ,
      UNIQUE(connector_id,idempotency_key_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pf_connector_document (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      connector_id TEXT NOT NULL REFERENCES pf_connector(id) ON DELETE CASCADE,
      source_id TEXT NOT NULL,
      source_version TEXT NOT NULL,
      source_etag TEXT,
      source_updated_at TIMESTAMPTZ,
      title TEXT NOT NULL,
      content TEXT,
      content_ref TEXT,
      content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
      acl JSONB NOT NULL DEFAULT '{}'::jsonb,
      status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','tombstone','revoked')),
      last_run_id TEXT REFERENCES pf_connector_run(id) ON DELETE SET NULL,
      version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(connector_id,source_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pf_connector_document_acl (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      connector_id TEXT NOT NULL REFERENCES pf_connector(id) ON DELETE CASCADE,
      document_id TEXT NOT NULL REFERENCES pf_connector_document(id) ON DELETE CASCADE,
      principal_type TEXT NOT NULL CHECK (principal_type IN ('user','group','role')),
      principal_id TEXT NOT NULL,
      effect TEXT NOT NULL DEFAULT 'allow' CHECK (effect IN ('allow','deny')),
      revoked_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(document_id,principal_type,principal_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pf_connector_identity_mapping (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      connector_id TEXT NOT NULL REFERENCES pf_connector(id) ON DELETE CASCADE,
      external_type TEXT NOT NULL CHECK (external_type IN ('user','group','role')),
      external_id TEXT NOT NULL,
      principal_type TEXT NOT NULL CHECK (principal_type IN ('user','group','role')),
      principal_id TEXT NOT NULL,
      enabled BOOLEAN NOT NULL DEFAULT TRUE,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(connector_id,external_type,external_id,principal_type,principal_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pf_connector_workspace_status ON pf_connector(workspace_id,status,updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_pf_connector_run_workspace_time ON pf_connector_run(workspace_id,connector_id,created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_pf_connector_document_visible ON pf_connector_document(workspace_id,connector_id,status,updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_pf_connector_acl_principal ON pf_connector_document_acl(workspace_id,principal_type,principal_id)",
    "CREATE INDEX IF NOT EXISTS idx_pf_connector_mapping_user ON pf_connector_identity_mapping(workspace_id,connector_id,principal_type,principal_id,enabled)",
)


@router.get("")
async def list_connectors(
    actor: Annotated[Actor, Depends(get_actor)],
    enabled: bool | None = None,
    provider: Provider | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    _require(actor, "read")
    predicates = ["workspace_id=%s", "status <> 'revoked'"]
    params: list[Any] = [actor.workspace_id]
    if enabled is not None:
        predicates.append("enabled=%s")
        params.append(enabled)
    if provider is not None:
        predicates.append("provider=%s")
        params.append(provider)
    params.append(limit)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"SELECT * FROM pf_connector WHERE {' AND '.join(predicates)} ORDER BY updated_at DESC,id DESC LIMIT %s",
            tuple(params),
        )
        rows = await result.fetchall()
    data = [_connector_view(row) for row in rows]
    # Contract《720》listConnectors: ListQuery -> ListResponse<ConnectorDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None},
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_connector(
    body: ConnectorCreate,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    _require(actor, "write")
    endpoint = validate_endpoint(body.endpoint or default_endpoint(body.provider, body.name), body.provider)
    manifest = validate_manifest(body.manifest)
    credentials = normalize_credentials(body.credentials, auth_mode=body.auth_mode)
    credential_ref = credentials.get("credential_ref")
    state = _connector_status(body.auth_mode, body.enabled)
    async with pool.connection() as conn:
        async with conn.transaction():
            duplicate = await conn.execute(
                "SELECT 1 FROM pf_connector WHERE workspace_id=%s AND name=%s AND status <> 'revoked'",
                (actor.workspace_id, body.name),
            )
            if await duplicate.fetchone():
                raise HTTPException(status_code=409, detail="Connector name already exists in this workspace")
            result = await conn.execute(
                """
                INSERT INTO pf_connector(
                  id,org_id,workspace_id,name,provider,auth_mode,endpoint_ref,manifest,credential_ref,
                  credential_hash,status,enabled,created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s) RETURNING *
                """,
                (
                    new_id("conn"), actor.org_id, actor.workspace_id, body.name, body.provider, body.auth_mode,
                    endpoint, json_dumps(manifest), credential_ref, credential_hash(credentials), state, body.enabled, actor.user_id,
                ),
            )
            row = await result.fetchone()
            await _outbox(
                conn,
                "connector.created.v1",
                actor.workspace_id,
                row["id"],
                {
                    "connector_id": row["id"],
                    "provider": row["provider"],
                    "auth_mode": row["auth_mode"],
                    "status": row["status"],
                    "credential_configured": bool(credentials),
                },
            )
    view = _connector_view(row)
    # Contract《720》createConnector: ConnectorCreate -> ConnectorDTO（顶层 DTO，保留旧包装向后兼容）
    return {"connector": view, **view}


@router.get("/{connector_id}")
async def get_connector(connector_id: str, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    _require(actor, "read")
    async with pool.connection() as conn:
        row = await _get_connector(conn, connector_id, actor.workspace_id)
    view = _connector_view(row)
    # Contract《720》getConnector: PathId -> ConnectorDTO（顶层 DTO，保留旧包装向后兼容）
    return {"connector": view, **view}


@router.patch("/{connector_id}")
async def update_connector(
    connector_id: str,
    body: ConnectorPatch,
    actor: Annotated[Actor, Depends(get_actor)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    _require(actor, "write")
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="At least one connector field is required")
    async with pool.connection() as conn:
        async with conn.transaction():
            current = await _get_connector(conn, connector_id, actor.workspace_id, for_update=True)
            if if_match and if_match.strip() not in {"*", str(current.get("version", 1)), f'W/"{current.get("version", 1)}"', f'"{current.get("version", 1)}"'}:
                raise HTTPException(status_code=412, detail="Connector version does not match If-Match")
            name = changes.get("name", current["name"])
            endpoint = changes.get("endpoint", current["endpoint_ref"])
            manifest = validate_manifest(changes.get("manifest", _manifest_from_connector(current)))
            credentials = changes.get("credentials") if "credentials" in changes else None
            if "credentials" not in changes:
                configured = bool(current.get("credential_hash") or current.get("credential_ref"))
                credentials_for_hash = None
                next_credential_hash = current.get("credential_hash")
                next_credential_ref = current.get("credential_ref")
            else:
                credentials = normalize_credentials(credentials, auth_mode=current["auth_mode"])
                credentials_for_hash = credentials
                next_credential_hash = credential_hash(credentials)
                next_credential_ref = credentials.get("credential_ref")
                configured = bool(credentials)
            validate_endpoint(endpoint, current["provider"])
            enabled = changes.get("enabled", current["enabled"])
            next_status = _connector_status(current["auth_mode"], enabled)
            result = await conn.execute(
                """
                UPDATE pf_connector SET name=%s,endpoint_ref=%s,manifest=%s::jsonb,credential_ref=%s,
                  credential_hash=%s,status=%s,enabled=%s,version=version+1,updated_at=now()
                WHERE id=%s AND workspace_id=%s RETURNING *
                """,
                (
                    name, endpoint, json_dumps(manifest), next_credential_ref, next_credential_hash,
                    next_status, enabled, connector_id, actor.workspace_id,
                ),
            )
            row = await result.fetchone()
            await _outbox(
                conn,
                "connector.updated.v1",
                actor.workspace_id,
                connector_id,
                {"connector_id": connector_id, "status": row["status"], "enabled": row["enabled"], "credential_configured": configured},
            )
    view = _connector_view(row)
    # Contract《720》updateConnector: ConnectorPatch -> ConnectorDTO（顶层 DTO，保留旧包装向后兼容）
    return {"connector": view, **view}


async def _set_enabled(connector_id: str, actor: Actor, enabled: bool) -> dict[str, Any]:
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            current = await _get_connector(conn, connector_id, actor.workspace_id, for_update=True)
            next_status = _connector_status(current["auth_mode"], enabled)
            result = await conn.execute(
                "UPDATE pf_connector SET enabled=%s,status=%s,version=version+1,updated_at=now() WHERE id=%s AND workspace_id=%s RETURNING *",
                (enabled, next_status, connector_id, actor.workspace_id),
            )
            row = await result.fetchone()
            await _outbox(
                conn,
                "connector.enabled.v1" if enabled else "connector.disabled.v1",
                actor.workspace_id,
                connector_id,
                {"connector_id": connector_id, "enabled": enabled, "status": next_status},
            )
    view = _connector_view(row)
    # Contract《720》enable/disableGatewayChannel: ... -> ConnectorDTO（顶层 DTO，保留旧包装向后兼容）
    return {"connector": view, **view}


@router.post("/{connector_id}/enable")
async def enable_connector(connector_id: str, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    return await _set_enabled(connector_id, actor, True)


@router.post("/{connector_id}/disable")
async def disable_connector(connector_id: str, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    return await _set_enabled(connector_id, actor, False)


@router.delete("/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_connector(connector_id: str, actor: Annotated[Actor, Depends(get_actor)]) -> Response:
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                "UPDATE pf_connector SET status='revoked',enabled=FALSE,version=version+1,updated_at=now() WHERE id=%s AND workspace_id=%s AND status <> 'revoked' RETURNING id",
                (connector_id, actor.workspace_id),
            )
            if not await result.fetchone():
                raise HTTPException(status_code=404, detail="Connector was not found in this workspace")
            await conn.execute(
                "UPDATE pf_connector_document SET status='revoked',version=version+1,updated_at=now() WHERE connector_id=%s AND workspace_id=%s AND status <> 'revoked'",
                (connector_id, actor.workspace_id),
            )
            await conn.execute(
                "UPDATE pf_connector_document_acl SET revoked_at=now() WHERE connector_id=%s AND workspace_id=%s AND revoked_at IS NULL",
                (connector_id, actor.workspace_id),
            )
            await _outbox(conn, "connector.revoked.v1", actor.workspace_id, connector_id, {"connector_id": connector_id})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{connector_id}/sync", status_code=status.HTTP_202_ACCEPTED)
async def sync_connector(
    connector_id: str,
    body: ConnectorSyncRequest,
    actor: Annotated[Actor, Depends(get_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            connector = await _get_connector(conn, connector_id, actor.workspace_id, for_update=True)
            if connector["status"] != "active":
                raise HTTPException(status_code=409, detail=f"Connector is not ready for sync (status={connector['status']})")
            before = _as_dict(connector.get("source_cursor"))
            default_key = f"connector-sync:{connector_id}:{body.mode}:{canonical_hash(before)}"
            raw_key, key_hash = _normalize_idempotency_key(idempotency_key, default_key)
            # The cursor is server state and advances after a successful run;
            # including it would turn a legitimate replay into an idempotency conflict.
            input_hash = canonical_hash({"connector_id": connector_id, "mode": body.mode})
            existing_result = await conn.execute(
                "SELECT * FROM pf_connector_run WHERE connector_id=%s AND workspace_id=%s AND idempotency_key_hash=%s",
                (connector_id, actor.workspace_id, key_hash),
            )
            existing = await existing_result.fetchone()
            if existing:
                _check_idempotency(existing.get("input_hash"), input_hash)
                # Contract《720》syncConnector: ... -> OperationAccepted（保留旧字段向后兼容）
                _existing_view = _run_view(existing)
                return {
                    "run": _existing_view,
                    "deduplicated": True,
                    "operation_id": _existing_view.get("id"),
                    "status": _existing_view.get("status", "queued"),
                    "status_url": f"/api/v1/connectors/{connector_id}/sync-runs/{_existing_view.get('id')}",
                    "submitted_at": _existing_view.get("created_at"),
                }
            run_id = new_id("cnrun")
            result = await conn.execute(
                """
                INSERT INTO pf_connector_run(
                  id,org_id,workspace_id,connector_id,mode,idempotency_key,idempotency_key_hash,input_hash,source_cursor_before,created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s) RETURNING *
                """,
                (run_id, actor.org_id, actor.workspace_id, connector_id, body.mode, raw_key, key_hash, input_hash, json_dumps(before), actor.user_id),
            )
            run = await result.fetchone()
            await _outbox(conn, "connector.sync.started.v1", actor.workspace_id, run_id, {"connector_id": connector_id, "run_id": run_id, "mode": body.mode})
            if connector["auth_mode"] != "none":
                result = await conn.execute(
                    """
                    UPDATE pf_connector_run SET status='unsupported',execution_status='unsupported',executed=FALSE,
                      error_code='connector_auth_pending',error_message=%s,completed_at=now()
                    WHERE id=%s AND workspace_id=%s RETURNING *
                    """,
                    (f"{connector['auth_mode']} authentication is recorded but external sync is not enabled", run_id, actor.workspace_id),
                )
                run = await result.fetchone()
                await _outbox(conn, "connector.sync.completed.v1", actor.workspace_id, run_id, {"connector_id": connector_id, "run_id": run_id, "status": "unsupported", "executed": False, "error_code": "connector_auth_pending"})
                # Contract《720》syncConnector: ... -> OperationAccepted（保留旧字段向后兼容）
                _unsupported_view = _run_view(run)
                return {
                    "run": _unsupported_view,
                    "deduplicated": False,
                    "operation_id": _unsupported_view.get("id"),
                    "status": _unsupported_view.get("status", "queued"),
                    "status_url": f"/api/v1/connectors/{connector_id}/sync-runs/{_unsupported_view.get('id')}",
                    "submitted_at": _unsupported_view.get("created_at"),
                }
            items, after = snapshot_for_sync(connector, body.mode)
            upserted = tombstoned = revoked = 0
            for item in items:
                item_status = await _upsert_document_projection(conn, connector, run_id, item)
                if item_status == "tombstone":
                    tombstoned += 1
                elif item_status == "revoked":
                    revoked += 1
                    reason = "source_revoked" if item.get("revoked") else "source_deleted" if item.get("deleted") else "acl_revoked"
                    await _outbox(conn, "connector.document.revoked.v1", actor.workspace_id, run_id, {"connector_id": connector_id, "run_id": run_id, "source_id": item["source_id"], "reason": reason})
                else:
                    upserted += 1
            if body.mode == "full":
                tombstoned += await _tombstone_missing_documents(conn, connector, run_id, {str(item["source_id"]) for item in items})
            result = await conn.execute(
                """
                UPDATE pf_connector_run SET status='succeeded',execution_status='executed',executed=TRUE,
                  source_cursor_after=%s::jsonb,documents_seen=%s,documents_upserted=%s,documents_tombstoned=%s,
                  documents_revoked=%s,completed_at=now() WHERE id=%s AND workspace_id=%s RETURNING *
                """,
                (json_dumps(after), len(items), upserted, tombstoned, revoked, run_id, actor.workspace_id),
            )
            run = await result.fetchone()
            await conn.execute(
                "UPDATE pf_connector SET source_cursor=%s::jsonb,last_sync_at=now(),version=version+1,updated_at=now() WHERE id=%s AND workspace_id=%s",
                (json_dumps(after), connector_id, actor.workspace_id),
            )
            await _outbox(conn, "connector.sync.completed.v1", actor.workspace_id, run_id, {"connector_id": connector_id, "run_id": run_id, "status": "succeeded", "executed": True, "documents_seen": len(items), "documents_upserted": upserted, "documents_tombstoned": tombstoned, "documents_revoked": revoked})
    # Contract《720》syncConnector: ... -> OperationAccepted（保留旧字段向后兼容）
    _succeeded_view = _run_view(run)
    return {
        "run": _succeeded_view,
        "deduplicated": False,
        "operation_id": _succeeded_view.get("id"),
        "status": _succeeded_view.get("status", "queued"),
        "status_url": f"/api/v1/connectors/{connector_id}/sync-runs/{_succeeded_view.get('id')}",
        "submitted_at": _succeeded_view.get("created_at"),
    }


@router.get("/{connector_id}/sync-runs")
async def list_sync_runs(
    connector_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    _require(actor, "read")
    async with pool.connection() as conn:
        await _get_connector(conn, connector_id, actor.workspace_id)
        result = await conn.execute(
            "SELECT * FROM pf_connector_run WHERE connector_id=%s AND workspace_id=%s ORDER BY created_at DESC,id DESC LIMIT %s",
            (connector_id, actor.workspace_id, limit),
        )
        rows = await result.fetchall()
    data = [_run_view(row) for row in rows]
    # Contract《720》listConnectorSyncRuns: ListQuery -> ListResponse<ConnectorRunDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None},
    }


@router.get("/{connector_id}/sync-runs/{run_id}")
async def get_sync_run(connector_id: str, run_id: str, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    _require(actor, "read")
    async with pool.connection() as conn:
        await _get_connector(conn, connector_id, actor.workspace_id)
        result = await conn.execute(
            "SELECT * FROM pf_connector_run WHERE id=%s AND connector_id=%s AND workspace_id=%s",
            (run_id, connector_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Connector sync run was not found")
    view = _run_view(row)
    # Contract《720》getConnectorSyncRun: PathId -> ConnectorRunDTO（顶层 DTO，保留旧包装向后兼容）
    return {"run": view, **view}


async def _actor_groups(conn, connector_id: str, workspace_id: str, user_id: str) -> list[str]:
    result = await conn.execute(
        """
        SELECT external_id FROM pf_connector_identity_mapping
        WHERE connector_id=%s AND workspace_id=%s AND external_type='group'
          AND principal_type='user' AND principal_id=%s AND enabled=TRUE
        """,
        (connector_id, workspace_id, user_id),
    )
    return [str(row["external_id"]) for row in await result.fetchall()]


@router.get("/{connector_id}/documents")
async def list_connector_documents(
    connector_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    visible_only: bool = True,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    _require(actor, "read")
    if not visible_only:
        _require(actor, "write")
    async with pool.connection() as conn:
        await _get_connector(conn, connector_id, actor.workspace_id)
        result = await conn.execute(
            "SELECT * FROM pf_connector_document WHERE connector_id=%s AND workspace_id=%s ORDER BY updated_at DESC,id DESC LIMIT %s",
            (connector_id, actor.workspace_id, limit),
        )
        rows = await result.fetchall()
        groups = await _actor_groups(conn, connector_id, actor.workspace_id, actor.user_id) if visible_only else []
    if visible_only:
        rows = visible_documents(rows, user_id=actor.user_id, roles=(actor.role,), groups=groups)
    data = [_document_view(row) for row in rows]
    # Contract《720》listConnectorDocuments: ListQuery -> ListResponse<ConnectorDocumentDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None},
    }


@router.get("/{connector_id}/documents/{document_id}")
async def get_connector_document(connector_id: str, document_id: str, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    _require(actor, "read")
    async with pool.connection() as conn:
        await _get_connector(conn, connector_id, actor.workspace_id)
        result = await conn.execute(
            "SELECT * FROM pf_connector_document WHERE id=%s AND connector_id=%s AND workspace_id=%s",
            (document_id, connector_id, actor.workspace_id),
        )
        row = await result.fetchone()
        groups = await _actor_groups(conn, connector_id, actor.workspace_id, actor.user_id)
    if not row or not document_visible(row, user_id=actor.user_id, roles=(actor.role,), groups=groups):
        raise HTTPException(status_code=404, detail="Connector document was not found")
    view = _document_view(row)
    # Contract《720》getConnectorDocument: PathId -> ConnectorDocumentDTO（顶层 DTO，保留旧包装向后兼容）
    return {"document": view, **view}


@router.post("/{connector_id}/identity-mappings", status_code=status.HTTP_201_CREATED)
async def upsert_identity_mapping(
    connector_id: str,
    body: IdentityMappingRequest,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            connector = await _get_connector(conn, connector_id, actor.workspace_id)
            result = await conn.execute(
                """
                INSERT INTO pf_connector_identity_mapping(
                  id,org_id,workspace_id,connector_id,external_type,external_id,principal_type,principal_id,enabled
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(connector_id,external_type,external_id,principal_type,principal_id)
                DO UPDATE SET enabled=EXCLUDED.enabled,updated_at=now() RETURNING *
                """,
                (
                    new_id("cmap"), connector["org_id"], actor.workspace_id, connector_id, body.external_type,
                    body.external_id, body.principal_type, body.principal_id, body.enabled,
                ),
            )
            row = await result.fetchone()
            await _outbox(conn, "connector.identity_mapping.updated.v1", actor.workspace_id, connector_id, {"connector_id": connector_id, "mapping_id": row["id"], "enabled": row["enabled"]})
    view = _mapping_view(row)
    # Contract《720》upsertIdentityMapping: ... -> IdentityMappingDTO（顶层 DTO，保留旧包装向后兼容）
    return {"mapping": view, **view}


@router.get("/{connector_id}/identity-mappings")
async def list_identity_mappings(connector_id: str, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    _require(actor, "read")
    async with pool.connection() as conn:
        await _get_connector(conn, connector_id, actor.workspace_id)
        result = await conn.execute(
            "SELECT * FROM pf_connector_identity_mapping WHERE connector_id=%s AND workspace_id=%s ORDER BY created_at,id",
            (connector_id, actor.workspace_id),
        )
        rows = await result.fetchall()
    data = [_mapping_view(row) for row in rows]
    # Contract《720》listConnectorIdentityMappings: ListQuery -> ListResponse<IdentityMappingDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None},
    }


__all__ = [
    "AccessControl",
    "ConnectorCreate",
    "ConnectorManifest",
    "ConnectorPatch",
    "ConnectorSyncRequest",
    "IdentityMappingRequest",
    "SCHEMA_STATEMENTS",
    "SourceItem",
    "canonical_hash",
    "credential_hash",
    "document_visible",
    "ensure_connectors_schema",
    "incremental_source_items",
    "local_source_items",
    "mock_source_items",
    "router",
    "snapshot_for_sync",
    "source_cursor",
    "source_items_for_connector",
    "validate_controlled_reference",
    "validate_endpoint",
    "validate_manifest",
    "visible_documents",
]
