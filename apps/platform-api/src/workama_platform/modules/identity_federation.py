from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import json
import re
import secrets
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlencode, urlsplit
from xml.etree import ElementTree as ET

import httpx
import jwt
from lxml import etree
from cryptography import x509
from cryptography.fernet import InvalidToken
from signxml import DigestAlgorithm, SignatureConfiguration, SignatureMethod, XMLVerifier
from signxml.exceptions import InvalidSignature
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from fastapi import Form
from fastapi.responses import RedirectResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from workama_platform.core import (
    Actor,
    capability_allows,
    create_access_token,
    decrypt_secret,
    encrypt_secret,
    get_actor,
    hash_password,
    hash_secret,
    json_dumps,
    new_id,
    pool,
)
from workama_platform.modules.security.service import validate_outbound_url


router = APIRouter(prefix="/api/v1/identity-federation", tags=["identity-federation"])
scim_router = APIRouter(prefix="/scim/v2.0", tags=["scim"])

OIDC = "oidc"
SAML = "saml"
PROVIDERS = (OIDC, SAML)
SSO_STATUSES = ("disabled", "pending", "active", "degraded", "deleted")
SCIM_TOKEN_PREFIX = "scim-wama-"
MAX_REDIRECTS = 16
MAX_MAPPING_BYTES = 16_000
MAX_SAML_RESPONSE_BYTES = 1_000_000
MAX_SAML_FORM_BYTES = 1_400_000
SAML_ASSERTION_NS = "urn:oasis:names:tc:SAML:2.0:assertion"
SAML_PROTOCOL_NS = "urn:oasis:names:tc:SAML:2.0:protocol"
XMLDSIG_NS = "http://www.w3.org/2000/09/xmldsig#"
SCIM_SCHEMAS = {
    "user": "urn:ietf:params:scim:schemas:core:2.0:User",
    "group": "urn:ietf:params:scim:schemas:core:2.0:Group",
    "list": "urn:ietf:params:scim:api:messages:2.0:ListResponse",
    "patch": "urn:ietf:params:scim:api:messages:2.0:PatchOp",
}


class SsoConfigCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    provider: Literal["oidc", "saml"]
    issuer: str | None = Field(default=None, max_length=2048)
    metadata_url: str | None = Field(default=None, max_length=2048)
    authorization_endpoint: str | None = Field(default=None, max_length=2048)
    token_endpoint: str | None = Field(default=None, max_length=2048)
    jwks_uri: str | None = Field(default=None, max_length=2048)
    client_id: str | None = Field(default=None, max_length=256)
    client_secret: str | None = Field(default=None, min_length=1, max_length=4096)
    client_secret_ref: str | None = Field(default=None, max_length=256)
    certificate: str | None = Field(default=None, min_length=1, max_length=16_384)
    certificate_ref: str | None = Field(default=None, max_length=256)
    redirect_allowlist: list[str] = Field(
        default_factory=list,
        max_length=MAX_REDIRECTS,
        validation_alias=AliasChoices("redirect_allowlist", "redirect_uris"),
    )
    mapping: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return normalize_text(value, "name", 120)

    @field_validator("certificate")
    @classmethod
    def validate_certificate(cls, value: str | None) -> str | None:
        return normalize_saml_certificate(value) if value is not None else None

    @model_validator(mode="after")
    def validate_provider_shape(self) -> "SsoConfigCreate":
        validate_sso_values(
            provider=self.provider,
            issuer=self.issuer,
            metadata_url=self.metadata_url,
            authorization_endpoint=self.authorization_endpoint,
            token_endpoint=self.token_endpoint,
            jwks_uri=self.jwks_uri,
            redirect_allowlist=self.redirect_allowlist,
            mapping=self.mapping,
        )
        return self


class SsoConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=120)
    issuer: str | None = Field(default=None, max_length=2048)
    metadata_url: str | None = Field(default=None, max_length=2048)
    authorization_endpoint: str | None = Field(default=None, max_length=2048)
    token_endpoint: str | None = Field(default=None, max_length=2048)
    jwks_uri: str | None = Field(default=None, max_length=2048)
    client_id: str | None = Field(default=None, max_length=256)
    client_secret: str | None = Field(default=None, min_length=1, max_length=4096)
    client_secret_ref: str | None = Field(default=None, max_length=256)
    certificate: str | None = Field(default=None, min_length=1, max_length=16_384)
    certificate_ref: str | None = Field(default=None, max_length=256)
    redirect_allowlist: Annotated[list[str] | None, Field(max_length=MAX_REDIRECTS, validation_alias=AliasChoices("redirect_allowlist", "redirect_uris"))] = None
    mapping: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        return normalize_text(value, "name", 120) if value is not None else None

    @field_validator("certificate")
    @classmethod
    def validate_certificate(cls, value: str | None) -> str | None:
        return normalize_saml_certificate(value) if value is not None else None


class OidcAuthorizationStart(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    redirect_uri: str = Field(min_length=1, max_length=2048)
    scope: str = Field(default="openid profile email", min_length=1, max_length=256)


class ScimTokenCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    workspace_id: str | None = Field(default=None, min_length=1, max_length=128)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def normalize_expiry(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ScimMember(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)

    value: str = Field(min_length=1, max_length=256)
    display: str | None = Field(default=None, max_length=256)
    type: str | None = Field(default=None, max_length=32)


class ScimUserCreate(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)

    external_id: Annotated[str | None, Field(max_length=256, validation_alias=AliasChoices("externalId", "external_id"))] = None
    user_name: Annotated[str, Field(min_length=1, max_length=320, validation_alias=AliasChoices("userName", "user_name"))]
    display_name: Annotated[str | None, Field(max_length=256, validation_alias=AliasChoices("displayName", "display_name"))] = None
    active: bool = True
    emails: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize_values(self) -> "ScimUserCreate":
        self.user_name = normalize_scim_value(self.user_name, "userName", 320)
        if self.external_id is not None:
            self.external_id = normalize_scim_value(self.external_id, "externalId", 256)
        if self.display_name is not None:
            self.display_name = normalize_scim_value(self.display_name, "displayName", 256)
        if not self.display_name:
            self.display_name = self.user_name
        return self


class ScimPatchOperation(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)

    op: Literal["add", "replace", "remove"]
    path: str | None = Field(default=None, max_length=256)
    value: Any = None

    @field_validator("op", mode="before")
    @classmethod
    def normalize_operation(cls, value: str) -> str:
        return str(value).strip().lower()


class ScimPatchRequest(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    operations: Annotated[list[ScimPatchOperation], Field(validation_alias=AliasChoices("Operations", "operations"))] = Field(default_factory=list)
    active: bool | None = None
    user_name: Annotated[str | None, Field(validation_alias=AliasChoices("userName", "user_name"))] = None
    display_name: Annotated[str | None, Field(validation_alias=AliasChoices("displayName", "display_name"))] = None
    external_id: Annotated[str | None, Field(validation_alias=AliasChoices("externalId", "external_id"))] = None
    members: list[ScimMember] | None = None


class ScimGroupCreate(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)

    external_id: Annotated[str | None, Field(max_length=256, validation_alias=AliasChoices("externalId", "external_id"))] = None
    display_name: Annotated[str, Field(min_length=1, max_length=256, validation_alias=AliasChoices("displayName", "display_name"))]
    members: list[ScimMember] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize_values(self) -> "ScimGroupCreate":
        self.display_name = normalize_scim_value(self.display_name, "displayName", 256)
        if self.external_id is not None:
            self.external_id = normalize_scim_value(self.external_id, "externalId", 256)
        return self


class ScimGroupPatchRequest(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    operations: Annotated[list[ScimPatchOperation], Field(validation_alias=AliasChoices("Operations", "operations"))] = Field(default_factory=list)
    display_name: Annotated[str | None, Field(validation_alias=AliasChoices("displayName", "display_name"))] = None
    external_id: Annotated[str | None, Field(validation_alias=AliasChoices("externalId", "external_id"))] = None
    members: list[ScimMember] | None = None


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS id_federation_sso_config (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      provider TEXT NOT NULL CHECK (provider IN ('oidc','saml')),
      name TEXT NOT NULL,
      issuer TEXT,
      metadata_url TEXT,
      authorization_endpoint TEXT,
      token_endpoint TEXT,
      jwks_uri TEXT,
      client_id TEXT,
      client_secret_hash TEXT,
      client_secret_enc TEXT,
      client_secret_ref TEXT,
      client_secret_last4 TEXT,
      certificate_hash TEXT,
      certificate_enc TEXT,
      certificate_ref TEXT,
      certificate_last4 TEXT,
      redirect_allowlist TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
      mapping JSONB NOT NULL DEFAULT '{}'::jsonb,
      status TEXT NOT NULL DEFAULT 'disabled'
        CHECK (status IN ('disabled','pending','active','degraded','deleted')),
      pending_reason TEXT,
      version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
      last_tested_at TIMESTAMPTZ,
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      deleted_at TIMESTAMPTZ,
      UNIQUE(workspace_id, name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_id_federation_sso_workspace_status ON id_federation_sso_config(workspace_id, status, updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS id_federation_oidc_state (
      id TEXT PRIMARY KEY,
      config_id TEXT NOT NULL REFERENCES id_federation_sso_config(id) ON DELETE CASCADE,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      state_hash TEXT NOT NULL UNIQUE,
      nonce_hash TEXT NOT NULL,
      code_verifier_hash TEXT NOT NULL,
      code_verifier_enc TEXT,
      redirect_uri TEXT NOT NULL,
      expires_at TIMESTAMPTZ NOT NULL,
      consumed_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_id_federation_oidc_state_expiry ON id_federation_oidc_state(workspace_id, expires_at, consumed_at)",
    """
    CREATE TABLE IF NOT EXISTS id_federation_scim_token (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      token_hash TEXT NOT NULL UNIQUE,
      last_four TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked','expired')),
      expires_at TIMESTAMPTZ,
      last_used_at TIMESTAMPTZ,
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      revoked_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_id_federation_scim_token_workspace ON id_federation_scim_token(workspace_id, status, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS id_federation_scim_user (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      external_id TEXT NOT NULL,
      user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
      user_name TEXT NOT NULL,
      display_name TEXT NOT NULL,
      active BOOLEAN NOT NULL DEFAULT TRUE,
      version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id, external_id),
      UNIQUE(workspace_id, user_id),
      UNIQUE(workspace_id, user_name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_id_federation_scim_user_workspace_active ON id_federation_scim_user(workspace_id, active, updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS id_federation_scim_group (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      external_id TEXT NOT NULL,
      display_name TEXT NOT NULL,
      active BOOLEAN NOT NULL DEFAULT TRUE,
      version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id, external_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_id_federation_scim_group_workspace ON id_federation_scim_group(workspace_id, active, updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS id_federation_scim_group_member (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      group_id TEXT NOT NULL REFERENCES id_federation_scim_group(id) ON DELETE CASCADE,
      scim_user_id TEXT REFERENCES id_federation_scim_user(id) ON DELETE SET NULL,
      external_member_id TEXT NOT NULL,
      display TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(group_id, external_member_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_id_federation_scim_group_member_workspace ON id_federation_scim_group_member(workspace_id, group_id)",
    "ALTER TABLE id_federation_sso_config ADD COLUMN IF NOT EXISTS token_endpoint TEXT",
    "ALTER TABLE id_federation_sso_config ADD COLUMN IF NOT EXISTS jwks_uri TEXT",
    "ALTER TABLE id_federation_sso_config ADD COLUMN IF NOT EXISTS client_secret_enc TEXT",
    "ALTER TABLE id_federation_sso_config ADD COLUMN IF NOT EXISTS certificate_enc TEXT",
    "ALTER TABLE id_federation_oidc_state ADD COLUMN IF NOT EXISTS code_verifier_enc TEXT",
    """
    CREATE TABLE IF NOT EXISTS id_federation_saml_replay (
      id TEXT PRIMARY KEY,
      config_id TEXT NOT NULL REFERENCES id_federation_sso_config(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      response_id TEXT NOT NULL,
      expires_at TIMESTAMPTZ NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(config_id, response_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_id_federation_saml_replay_expiry ON id_federation_saml_replay(config_id, expires_at)",
    "ALTER TABLE id_federation_sso_config DROP CONSTRAINT IF EXISTS id_federation_sso_config_workspace_id_name_key",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_id_federation_sso_workspace_name_active ON id_federation_sso_config(workspace_id, name) WHERE status <> 'deleted'",
    """
    CREATE TABLE IF NOT EXISTS id_federation_sso_login_session (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      provider_id TEXT NOT NULL REFERENCES id_federation_sso_config(id) ON DELETE CASCADE,
      user_id TEXT REFERENCES id_user(id) ON DELETE SET NULL,
      token TEXT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      expires_at TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_id_federation_sso_login_session_provider_token ON id_federation_sso_login_session(provider_id, token)",
    "ALTER TABLE id_federation_sso_login_session ADD COLUMN IF NOT EXISTS consumed_at TIMESTAMPTZ",
    """
    CREATE TABLE IF NOT EXISTS id_federation_scim_sync_run (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      provider_id TEXT NOT NULL REFERENCES id_federation_sso_config(id) ON DELETE CASCADE,
      status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','failed')),
      created_users INTEGER NOT NULL DEFAULT 0,
      updated_users INTEGER NOT NULL DEFAULT 0,
      deactivated_users INTEGER NOT NULL DEFAULT 0,
      total_users INTEGER NOT NULL DEFAULT 0,
      error_message TEXT,
      started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      completed_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_id_federation_scim_sync_run_workspace_provider ON id_federation_scim_sync_run(workspace_id, provider_id, started_at DESC)",
)


async def ensure_identity_federation_schema(conn) -> None:
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


def normalize_text(value: str, field_name: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or any(ord(char) < 32 for char in normalized):
        raise ValueError(f"{field_name} is invalid")
    return normalized


def normalize_saml_certificate(value: str) -> str:
    normalized = value.strip()
    if len(normalized) > 16_384 or "\x00" in normalized:
        raise ValueError("certificate is invalid")
    if not normalized.startswith("-----BEGIN CERTIFICATE-----") or not normalized.endswith("-----END CERTIFICATE-----"):
        raise ValueError("certificate must be PEM encoded")
    try:
        x509.load_pem_x509_certificate(normalized.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("certificate is invalid") from exc
    return normalized


def normalize_scim_value(value: str, field_name: str, maximum: int) -> str:
    try:
        return normalize_text(value, field_name, maximum)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} is invalid") from exc


def _contains_sensitive_key(value: Any, path: str = "") -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).strip().lower().replace("-", "_")
            current = f"{path}.{key_text}" if path else key_text
            if key_text in {
                "secret",
                "client_secret",
                "certificate",
                "private_key",
                "password",
                "token",
                "access_token",
                "refresh_token",
                "authorization",
            }:
                return current
            found = _contains_sensitive_key(item, current)
            if found:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = _contains_sensitive_key(item, f"{path}[{index}]")
            if found:
                return found
    return None


def validate_external_url(value: str, *, field_name: str = "url") -> str:
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} is invalid") from exc
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{field_name} must be an HTTPS URL without credentials")
    if parsed.fragment or any(char.isspace() for char in value):
        raise ValueError(f"{field_name} must not contain whitespace or a fragment")
    validation = validate_outbound_url(value)
    if not validation.allowed:
        raise ValueError(f"{field_name} is not an allowed public endpoint")
    return value


def validate_redirect_uri(value: str) -> str:
    normalized = validate_external_url(value, field_name="redirect_uri")
    parsed = urlsplit(normalized)
    if parsed.query:
        raise ValueError("redirect_uri must not contain a query string")
    return normalized


def validate_saml_entity_id(value: str) -> str:
    normalized = normalize_text(value, "entity_id", 2048)
    if normalized.startswith(("http://", "https://")):
        return validate_external_url(normalized, field_name="entity_id")
    if not re.fullmatch(r"urn:[^\s]{1,1980}", normalized, re.IGNORECASE):
        raise ValueError("entity_id must be a safe URN or HTTPS URL")
    return normalized


def validate_sso_values(
    *,
    provider: str,
    issuer: str | None,
    metadata_url: str | None,
    authorization_endpoint: str | None,
    token_endpoint: str | None = None,
    jwks_uri: str | None = None,
    redirect_allowlist: list[str],
    mapping: dict[str, Any],
) -> None:
    if provider not in PROVIDERS:
        raise ValueError("provider must be oidc or saml")
    if provider == OIDC and not issuer:
        raise ValueError("issuer is required for OIDC")
    if provider == SAML and not metadata_url and not issuer:
        raise ValueError("metadata_url or issuer is required for SAML")
    if issuer:
        if provider == SAML:
            validate_saml_entity_id(issuer)
        else:
            validate_external_url(issuer, field_name="issuer")
    if metadata_url:
        validate_external_url(metadata_url, field_name="metadata_url")
    if authorization_endpoint:
        validate_external_url(authorization_endpoint, field_name="authorization_endpoint")
    if token_endpoint:
        validate_external_url(token_endpoint, field_name="token_endpoint")
    if jwks_uri:
        validate_external_url(jwks_uri, field_name="jwks_uri")
    if not redirect_allowlist:
        raise ValueError("at least one redirect URI is required")
    for redirect_uri in redirect_allowlist:
        validate_redirect_uri(redirect_uri)
    if not isinstance(mapping, dict):
        raise ValueError("mapping must be an object")
    if _contains_sensitive_key(mapping):
        raise ValueError("mapping contains a sensitive field")
    try:
        if len(json.dumps(mapping, ensure_ascii=False)) > MAX_MAPPING_BYTES:
            raise ValueError("mapping is too large")
    except (TypeError, ValueError) as exc:
        raise ValueError("mapping must be JSON serializable") from exc


def _last_four(value: str | None) -> str | None:
    return value[-4:] if value else None


def _hash_value(value: str) -> str:
    return hash_secret(value)


def _pkce_challenge(verifier: str) -> str:
    return __import__("base64").urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def state_digest(value: str) -> str:
    return _hash_value(value)


def nonce_digest(value: str) -> str:
    return _hash_value(value)


def generate_oidc_state_bundle() -> dict[str, str]:
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(48)
    return {
        "state": state,
        "nonce": nonce,
        "code_verifier": code_verifier,
        "state_hash": state_digest(state),
        "nonce_hash": nonce_digest(nonce),
        "code_verifier_hash": _hash_value(code_verifier),
    }


def validate_oidc_state_record(
    record: dict[str, Any],
    state: str,
    *,
    now: datetime | None = None,
) -> bool:
    current = now or datetime.now(UTC)
    expires_at = record.get("expires_at")
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return (
        not record.get("consumed_at")
        and expires_at > current
        and hmac.compare_digest(str(record.get("state_hash", "")), state_digest(state))
    )


class OidcExchangeError(ValueError):
    """A provider exchange failed without exposing upstream response data."""


class OidcTokenValidationError(ValueError):
    """An ID token failed local issuer, key, claim, or nonce validation."""


async def _fetch_oidc_json(
    url: str,
    *,
    method: str = "GET",
    data: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    try:
        target = validate_external_url(url, field_name="oidc_endpoint")
    except ValueError as exc:
        raise OidcExchangeError("OIDC endpoint is not allowed") from exc
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
            transport=transport,
        ) as client:
            response = await client.request(method, target, data=data)
    except httpx.HTTPError as exc:
        raise OidcExchangeError("OIDC endpoint request failed") from exc
    if response.status_code < 200 or response.status_code >= 300 or len(response.content) > 1_000_000:
        raise OidcExchangeError("OIDC endpoint returned an unusable response")
    try:
        payload = response.json()
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OidcExchangeError("OIDC endpoint returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise OidcExchangeError("OIDC endpoint returned an invalid object")
    return payload


def _oidc_jwk_for_token(token: str, jwks: dict[str, Any]) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise OidcTokenValidationError("OIDC ID token header is invalid") from exc
    if header.get("alg") != "RS256" or not header.get("kid"):
        raise OidcTokenValidationError("OIDC ID token algorithm or key id is not allowed")
    keys = jwks.get("keys")
    if not isinstance(keys, list):
        raise OidcTokenValidationError("OIDC JWKS is invalid")
    for key in keys:
        if isinstance(key, dict) and key.get("kid") == header["kid"] and key.get("kty") == "RSA":
            return key
    raise OidcTokenValidationError("OIDC signing key was not found")


def _validate_oidc_id_token(
    token: str,
    *,
    jwks: dict[str, Any],
    issuer: str,
    client_id: str,
    nonce_hash: str,
) -> dict[str, Any]:
    if not token or len(token) > 32_768:
        raise OidcTokenValidationError("OIDC ID token is invalid")
    key = _oidc_jwk_for_token(token, jwks)
    try:
        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))
        claims = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=issuer,
            options={"require": ["exp", "iat", "iss", "sub", "aud", "nonce"]},
            leeway=5,
        )
    except (TypeError, ValueError, jwt.PyJWTError) as exc:
        raise OidcTokenValidationError("OIDC ID token signature or claims are invalid") from exc
    if not hmac.compare_digest(nonce_digest(str(claims.get("nonce", ""))), nonce_hash):
        raise OidcTokenValidationError("OIDC ID token nonce is invalid")
    return claims


async def _exchange_oidc_code(
    config: dict[str, Any],
    state_record: dict[str, Any],
    code: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    client_id = str(config.get("client_id") or "")
    issuer = str(config.get("issuer") or "")
    if not client_id or not issuer:
        raise OidcExchangeError("OIDC client configuration is incomplete")
    metadata: dict[str, Any] = {}
    if config.get("metadata_url") and (not config.get("token_endpoint") or not config.get("jwks_uri")):
        metadata = await _fetch_oidc_json(str(config["metadata_url"]), transport=transport)
        discovered_issuer = metadata.get("issuer")
        if discovered_issuer and not hmac.compare_digest(str(discovered_issuer), issuer):
            raise OidcExchangeError("OIDC discovery issuer does not match configuration")
    token_endpoint = str(config.get("token_endpoint") or metadata.get("token_endpoint") or "")
    jwks_uri = str(config.get("jwks_uri") or metadata.get("jwks_uri") or "")
    if not token_endpoint or not jwks_uri:
        raise OidcExchangeError("OIDC token and JWKS endpoints are not configured")
    try:
        verifier = decrypt_secret(state_record.get("code_verifier_enc"))
    except (InvalidToken, TypeError, ValueError) as exc:
        raise OidcExchangeError("OIDC PKCE verifier is unavailable") from exc
    if not verifier:
        raise OidcExchangeError("OIDC PKCE verifier is unavailable")
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": str(state_record.get("redirect_uri") or ""),
        "code_verifier": verifier,
    }
    try:
        client_secret = decrypt_secret(config.get("client_secret_enc"))
    except (InvalidToken, TypeError, ValueError) as exc:
        raise OidcExchangeError("OIDC client secret is unavailable") from exc
    if client_secret:
        form["client_secret"] = client_secret
    token_payload = await _fetch_oidc_json(token_endpoint, method="POST", data=form, transport=transport)
    id_token = token_payload.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        raise OidcExchangeError("OIDC token response did not contain an ID token")
    jwks = await _fetch_oidc_json(jwks_uri, transport=transport)
    try:
        return _validate_oidc_id_token(
            id_token,
            jwks=jwks,
            issuer=issuer,
            client_id=client_id,
            nonce_hash=str(state_record.get("nonce_hash") or ""),
        )
    except OidcTokenValidationError:
        raise
    except (TypeError, ValueError) as exc:
        raise OidcTokenValidationError("OIDC ID token claims are invalid") from exc


class SamlValidationError(ValueError):
    """Raised when a SAML response fails the configured trust boundary."""


def _saml_time(value: str | None, field_name: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise SamlValidationError(f"SAML {field_name} is invalid") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _saml_values(assertion: etree._Element) -> dict[str, str]:
    values: dict[str, str] = {}
    for attribute in assertion.findall(f".//{{{SAML_ASSERTION_NS}}}Attribute"):
        name = (attribute.get("Name") or attribute.get("FriendlyName") or "").strip()
        if not name:
            continue
        value = attribute.find(f"{{{SAML_ASSERTION_NS}}}AttributeValue")
        text = (value.text or "").strip() if value is not None else ""
        if text:
            values[name] = text
    return values


def _saml_verify_signed_node(node: etree._Element, certificate: str) -> etree._Element:
    if node.get("ID") is None:
        raise SamlValidationError("SAML signed node is missing an ID")
    try:
        verified = XMLVerifier().verify(
            node,
            x509_cert=certificate,
            expect_config=SignatureConfiguration(
                require_x509=True,
                location=".//",
                expect_references=1,
                signature_methods=frozenset({SignatureMethod.RSA_SHA256}),
                digest_algorithms=frozenset({DigestAlgorithm.SHA256}),
            ),
            id_attribute="ID",
        )
    except (InvalidSignature, ValueError, TypeError, etree.XMLSyntaxError) as exc:
        raise SamlValidationError("SAML signature validation failed") from exc
    signed_xml = verified.signed_xml
    if signed_xml is None or signed_xml.get("ID") != node.get("ID"):
        raise SamlValidationError("SAML signature reference is invalid")
    return signed_xml


def _validate_saml_response(
    payload: str | bytes,
    *,
    config: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if not payload or len(payload) > MAX_SAML_RESPONSE_BYTES:
        raise SamlValidationError("SAML response is too large or empty")
    try:
        certificate = decrypt_secret(config.get("certificate_enc"))
    except (InvalidToken, TypeError, ValueError) as exc:
        raise SamlValidationError("SAML signing certificate is unavailable") from exc
    if not certificate:
        raise SamlValidationError("SAML signing certificate is not configured")
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
        root = etree.fromstring(payload, parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise SamlValidationError("SAML response XML is invalid") from exc
    if root.tag != f"{{{SAML_PROTOCOL_NS}}}Response":
        raise SamlValidationError("SAML response root is invalid")
    response_id = (root.get("ID") or "").strip()
    if not response_id or len(response_id) > 256 or any(ord(char) < 33 for char in response_id):
        raise SamlValidationError("SAML response ID is invalid")
    assertions = root.findall(f".//{{{SAML_ASSERTION_NS}}}Assertion")
    if len(assertions) != 1:
        raise SamlValidationError("SAML response must contain exactly one assertion")
    assertion = assertions[0]
    response_signature = root.find(f"{{{XMLDSIG_NS}}}Signature")
    assertion_signature = assertion.find(f"{{{XMLDSIG_NS}}}Signature")
    signed_nodes = []
    if response_signature is not None:
        signed_nodes.append(root)
    if assertion_signature is not None:
        signed_nodes.append(assertion)
    if not signed_nodes:
        raise SamlValidationError("SAML response is unsigned")
    verified_nodes = [_saml_verify_signed_node(node, certificate) for node in signed_nodes]
    verified_assertion = next((node for node in verified_nodes if node.tag == f"{{{SAML_ASSERTION_NS}}}Assertion"), None)
    if verified_assertion is None:
        verified_assertion = assertion

    status_code = root.find(f".//{{{SAML_PROTOCOL_NS}}}StatusCode")
    if status_code is None or status_code.get("Value") != "urn:oasis:names:tc:SAML:2.0:status:Success":
        raise SamlValidationError("SAML response status is not success")
    expected_issuer = str(config.get("issuer") or "")
    assertion_issuer = (verified_assertion.findtext(f"{{{SAML_ASSERTION_NS}}}Issuer") or "").strip()
    if expected_issuer and assertion_issuer != expected_issuer:
        raise SamlValidationError("SAML issuer is not trusted")

    mapping = config.get("mapping") or {}
    expected_destination = str(mapping.get("acs_url") or "")
    destination = root.get("Destination")
    if expected_destination and destination != expected_destination:
        raise SamlValidationError("SAML destination is not trusted")
    expected_audience = str(mapping.get("audience") or mapping.get("sp_entity_id") or "")
    if expected_audience:
        audiences = {
            (item.text or "").strip()
            for item in verified_assertion.findall(f".//{{{SAML_ASSERTION_NS}}}Audience")
            if item.text
        }
        if expected_audience not in audiences:
            raise SamlValidationError("SAML audience is not trusted")

    current = now.astimezone(UTC) if now and now.tzinfo else (now.replace(tzinfo=UTC) if now else datetime.now(UTC))
    conditions = verified_assertion.find(f"{{{SAML_ASSERTION_NS}}}Conditions")
    if conditions is not None:
        not_before = _saml_time(conditions.get("NotBefore"), "NotBefore")
        not_on_or_after = _saml_time(conditions.get("NotOnOrAfter"), "NotOnOrAfter")
        if not_before and not_before > current + timedelta(seconds=5):
            raise SamlValidationError("SAML assertion is not active yet")
        if not_on_or_after and not_on_or_after <= current - timedelta(seconds=5):
            raise SamlValidationError("SAML assertion has expired")

    name_id = verified_assertion.find(f".//{{{SAML_ASSERTION_NS}}}NameID")
    subject = (name_id.text or "").strip() if name_id is not None else ""
    if not subject:
        raise SamlValidationError("SAML subject is missing")
    attributes = _saml_values(verified_assertion)
    email_attribute = str(mapping.get("email_attribute") or mapping.get("email_claim") or "email")
    email = attributes.get(email_attribute, "").strip().lower()
    if not email:
        raise SamlValidationError("SAML email attribute is missing")
    verified_attribute = str(mapping.get("email_verified_attribute") or "email_verified")
    if attributes.get(verified_attribute, "true").lower() in {"false", "0", "no"}:
        raise SamlValidationError("SAML email is not verified")
    return {
        "response_id": response_id,
        "sub": subject,
        "iss": assertion_issuer or expected_issuer,
        "email": email,
        "email_verified": True,
        "attributes": attributes,
    }


def _decode_saml_response_form(value: str) -> bytes:
    if not value or len(value) > MAX_SAML_FORM_BYTES:
        raise SamlValidationError("SAML form response is too large or empty")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise SamlValidationError("SAML form response is not valid base64") from exc
    if not decoded or len(decoded) > MAX_SAML_RESPONSE_BYTES:
        raise SamlValidationError("SAML decoded response is too large or empty")
    return decoded


def _safe_host(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return urlsplit(value).hostname
    except ValueError:
        return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if not isinstance(value, str) else [value]


def _sso_view(row: dict[str, Any]) -> dict[str, Any]:
    issuer = row.get("issuer")
    metadata_url = row.get("metadata_url")
    endpoint = row.get("authorization_endpoint")
    return {
        "id": row.get("id"),
        "org_id": row.get("org_id"),
        "workspace_id": row.get("workspace_id"),
        "name": row.get("name"),
        "provider": row.get("provider"),
        "issuer": issuer,
        "issuer_host": _safe_host(issuer),
        "metadata_url": metadata_url,
        "metadata_host": _safe_host(metadata_url),
        "authorization_endpoint": endpoint,
        "token_endpoint": row.get("token_endpoint"),
        "token_endpoint_host": _safe_host(row.get("token_endpoint")),
        "jwks_uri": row.get("jwks_uri"),
        "jwks_host": _safe_host(row.get("jwks_uri")),
        "client_id": row.get("client_id"),
        "client_secret_configured": bool(row.get("client_secret_hash") or row.get("client_secret_ref")),
        "client_secret_last4": row.get("client_secret_last4"),
        "client_secret_ref_configured": bool(row.get("client_secret_ref")),
        "certificate_configured": bool(row.get("certificate_hash") or row.get("certificate_enc") or row.get("certificate_ref")),
        "certificate_validation_ready": bool(row.get("certificate_enc")),
        "certificate_last4": row.get("certificate_last4"),
        "certificate_ref_configured": bool(row.get("certificate_ref")),
        "redirect_allowlist": _as_list(row.get("redirect_allowlist")),
        "mapping": row.get("mapping") or {},
        "status": row.get("status"),
        "pending_reason": row.get("pending_reason"),
        "version": row.get("version", 1),
        "last_tested_at": row.get("last_tested_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _token_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "org_id": row.get("org_id"),
        "workspace_id": row.get("workspace_id"),
        "last_four": row.get("last_four"),
        "status": row.get("status"),
        "expires_at": row.get("expires_at"),
        "last_used_at": row.get("last_used_at"),
        "created_at": row.get("created_at"),
        "revoked_at": row.get("revoked_at"),
    }


def _user_view(row: dict[str, Any], workspace_id: str) -> dict[str, Any]:
    resource_id = str(row["id"])
    version = int(row.get("version", 1))
    return {
        "schemas": [SCIM_SCHEMAS["user"]],
        "id": resource_id,
        "externalId": row.get("external_id"),
        "userName": row.get("user_name"),
        "displayName": row.get("display_name"),
        "active": bool(row.get("active")),
        "meta": {
            "resourceType": "User",
            "location": f"/scim/v2.0/{workspace_id}/Users/{resource_id}",
            "version": f'W/"{version}"',
            "lastModified": row.get("updated_at"),
        },
    }


def _group_view(row: dict[str, Any], members: list[dict[str, Any]], workspace_id: str) -> dict[str, Any]:
    resource_id = str(row["id"])
    version = int(row.get("version", 1))
    return {
        "schemas": [SCIM_SCHEMAS["group"]],
        "id": resource_id,
        "externalId": row.get("external_id"),
        "displayName": row.get("display_name"),
        "members": [
            {
                "value": item.get("scim_user_id") or item.get("external_member_id"),
                "display": item.get("display"),
                "type": "User",
            }
            for item in members
        ],
        "meta": {
            "resourceType": "Group",
            "location": f"/scim/v2.0/{workspace_id}/Groups/{resource_id}",
            "version": f'W/"{version}"',
            "lastModified": row.get("updated_at"),
        },
    }


def _require_capability(actor: Actor, action: Literal["read", "write"]) -> None:
    required = f"identity_federation:{action}"
    aliases = (f"sso:{action}", f"scim:{action}")
    if capability_allows(actor.capabilities, required) or any(capability_allows(actor.capabilities, alias) for alias in aliases):
        return
    raise HTTPException(status_code=403, detail=f"Missing capability: {required}")


async def _workspace_for_actor(conn, actor: Actor, workspace_id: str | None = None) -> dict[str, Any]:
    requested = workspace_id or actor.workspace_id
    if requested != actor.workspace_id:
        raise HTTPException(status_code=404, detail="Workspace not found")
    result = await conn.execute(
        "SELECT id, org_id, name FROM id_workspace WHERE id=%s AND org_id=%s",
        (requested, actor.org_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return row


async def _get_sso(conn, config_id: str, workspace_id: str, *, include_deleted: bool = False) -> dict[str, Any]:
    predicate = "" if include_deleted else " AND status <> 'deleted'"
    result = await conn.execute(
        f"SELECT * FROM id_federation_sso_config WHERE id=%s AND workspace_id=%s{predicate}",
        (config_id, workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="SSO configuration not found")
    return row


async def _emit_outbox(conn, event_type: str, workspace_id: str, payload: dict[str, Any], trace_id: str) -> None:
    await conn.execute(
        "INSERT INTO ops_outbox(id,event_type,workspace_id,trace_id,payload) VALUES (%s,%s,%s,%s,%s::jsonb)",
        (new_id("out"), event_type, workspace_id, trace_id, json_dumps(payload)),
    )


def _sso_values(body: SsoConfigCreate) -> dict[str, Any]:
    validate_sso_values(
        provider=body.provider,
        issuer=body.issuer,
        metadata_url=body.metadata_url,
        authorization_endpoint=body.authorization_endpoint,
        token_endpoint=body.token_endpoint,
        jwks_uri=body.jwks_uri,
        redirect_allowlist=body.redirect_allowlist,
        mapping=body.mapping,
    )
    return body.model_dump()


def _config_secret_values(body: SsoConfigCreate | SsoConfigPatch) -> dict[str, Any]:
    fields = body.model_dump(exclude_unset=True)
    return {
        "client_secret_hash": hash_secret(fields["client_secret"]) if fields.get("client_secret") else None,
        "client_secret_enc": encrypt_secret(fields.get("client_secret")),
        "client_secret_ref": fields.get("client_secret_ref"),
        "client_secret_last4": _last_four(fields.get("client_secret")),
        "certificate_hash": hash_secret(fields["certificate"]) if fields.get("certificate") else None,
        "certificate_enc": encrypt_secret(fields.get("certificate")),
        "certificate_ref": fields.get("certificate_ref"),
        "certificate_last4": _last_four(fields.get("certificate")),
    }


def _normalize_patch_values(current: dict[str, Any], body: SsoConfigPatch) -> dict[str, Any]:
    values = {
        "provider": current["provider"],
        "issuer": current.get("issuer"),
        "metadata_url": current.get("metadata_url"),
        "authorization_endpoint": current.get("authorization_endpoint"),
        "token_endpoint": current.get("token_endpoint"),
        "jwks_uri": current.get("jwks_uri"),
        "redirect_allowlist": _as_list(current.get("redirect_allowlist")),
        "mapping": current.get("mapping") or {},
    }
    changes = body.model_dump(exclude_unset=True)
    for field in ("issuer", "metadata_url", "authorization_endpoint", "token_endpoint", "jwks_uri", "redirect_allowlist", "mapping"):
        if field in changes:
            values[field] = changes[field]
    if "name" in changes:
        values["name"] = normalize_text(changes["name"], "name", 120)
    else:
        values["name"] = current["name"]
    validate_sso_values(**{key: values[key] for key in ("provider", "issuer", "metadata_url", "authorization_endpoint", "token_endpoint", "jwks_uri", "redirect_allowlist", "mapping")})
    return values


@router.get("")
@router.get("/sso-configs")
async def list_sso_configs(
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=50, ge=1, le=200),
):
    _require_capability(actor, "read")
    async with pool.connection() as conn:
        await _workspace_for_actor(conn, actor)
        result = await conn.execute(
            "SELECT * FROM id_federation_sso_config WHERE workspace_id=%s AND status <> 'deleted' ORDER BY updated_at DESC, id DESC LIMIT %s",
            (actor.workspace_id, limit),
        )
        return {"items": [_sso_view(row) for row in await result.fetchall()]}


@router.post("", status_code=status.HTTP_201_CREATED)
@router.post("/sso-configs", status_code=status.HTTP_201_CREATED)
async def create_sso_config(body: SsoConfigCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require_capability(actor, "write")
    values = _sso_values(body)
    secret_values = _config_secret_values(body)
    async with pool.connection() as conn:
        async with conn.transaction():
            await _workspace_for_actor(conn, actor)
            duplicate = await conn.execute(
                "SELECT 1 FROM id_federation_sso_config WHERE workspace_id=%s AND name=%s AND status <> 'deleted'",
                (actor.workspace_id, values["name"]),
            )
            if await duplicate.fetchone():
                raise HTTPException(status_code=409, detail="SSO configuration name already exists")
            result = await conn.execute(
                """
                INSERT INTO id_federation_sso_config(
                  id,org_id,workspace_id,provider,name,issuer,metadata_url,authorization_endpoint,token_endpoint,jwks_uri,client_id,
                  client_secret_hash,client_secret_enc,client_secret_ref,client_secret_last4,certificate_hash,certificate_enc,certificate_ref,
                  certificate_last4,redirect_allowlist,mapping,status,created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'disabled',%s)
                RETURNING *
                """,
                (
                    new_id("sso"), actor.org_id, actor.workspace_id, values["provider"], values["name"], values["issuer"],
                    values["metadata_url"], values["authorization_endpoint"], values.get("token_endpoint"), values.get("jwks_uri"), values.get("client_id"),
                    secret_values["client_secret_hash"], secret_values["client_secret_enc"], secret_values["client_secret_ref"], secret_values["client_secret_last4"],
                    secret_values["certificate_hash"], secret_values["certificate_enc"], secret_values["certificate_ref"], secret_values["certificate_last4"],
                    values["redirect_allowlist"], json_dumps(values["mapping"]), actor.user_id,
                ),
            )
            row = await result.fetchone()
            await _emit_outbox(conn, "federation.sso.updated", actor.workspace_id, {
                "config_id": row["id"], "provider": row["provider"], "status": row["status"], "version": row["version"],
            }, row["id"])
    return _sso_view(row)


@router.get("/scim-tokens")
async def list_scim_tokens(actor: Annotated[Actor, Depends(get_actor)], limit: int = Query(default=50, ge=1, le=200)):
    _require_capability(actor, "read")
    async with pool.connection() as conn:
        await _workspace_for_actor(conn, actor)
        result = await conn.execute(
            "SELECT * FROM id_federation_scim_token WHERE workspace_id=%s ORDER BY created_at DESC LIMIT %s",
            (actor.workspace_id, limit),
        )
        return {"items": [_token_view(row) for row in await result.fetchall()]}


@router.get("/sso-configs/{config_id}/saml/metadata")
@router.get("/{config_id}/saml/metadata")
async def get_saml_metadata(config_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require_capability(actor, "read")
    async with pool.connection() as conn:
        row = await _get_sso(conn, config_id, actor.workspace_id)
    if row["provider"] != SAML:
        raise HTTPException(status_code=409, detail="SSO configuration is not SAML")
    return {
        "protocol": SAML,
        "config_id": row["id"],
        "entity_id": row.get("issuer"),
        "metadata_url_host": _safe_host(row.get("metadata_url")),
        "acs": {
            "path": f"/api/v1/identity-federation/sso-configs/{row['id']}/saml/acs",
            "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
        },
        "status": row["status"],
        "certificate_configured": bool(row.get("certificate_hash") or row.get("certificate_enc") or row.get("certificate_ref")),
        "certificate_validation_ready": bool(row.get("certificate_enc")),
    }


@router.get("/sso-configs/{config_id}/saml/acs")
@router.get("/{config_id}/saml/acs")
async def get_saml_acs_metadata(config_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    metadata = await get_saml_metadata(config_id, actor)
    return {
        "protocol": SAML,
        "config_id": config_id,
        "acs": metadata["acs"],
        "status": metadata["status"],
        "verification": "certificate_configured" if metadata["certificate_validation_ready"] else "pending/not_configured",
        "upstream_signature_validation": metadata["certificate_validation_ready"],
    }


async def _active_saml_config(config_id: str) -> dict[str, Any]:
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM id_federation_sso_config WHERE id=%s AND status <> 'deleted'",
            (config_id,),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="SAML SSO configuration not found")
    if row["provider"] != SAML:
        raise HTTPException(status_code=409, detail="SSO configuration is not SAML")
    if row["status"] != "active":
        raise HTTPException(status_code=503, detail="E09019 SAML provider is pending/not_configured")
    return row


@router.post("/sso-configs/{config_id}/saml/acs")
@router.post("/{config_id}/saml/acs")
async def saml_acs(
    config_id: str,
    response: Response,
    saml_response: Annotated[str, Form(alias="SAMLResponse", min_length=1, max_length=MAX_SAML_RESPONSE_BYTES)] ,
    relay_state: Annotated[str | None, Form(alias="RelayState", max_length=2048)] = None,
):
    config = await _active_saml_config(config_id)
    try:
        claims = _validate_saml_response(_decode_saml_response_form(saml_response), config=config)
    except SamlValidationError as exc:
        raise HTTPException(status_code=401, detail="E09020 SAML response validation failed") from exc
    mapping = config.get("mapping") or {}
    if relay_state and mapping.get("relay_state") and not hmac.compare_digest(relay_state, str(mapping["relay_state"])):
        raise HTTPException(status_code=400, detail="E09021 SAML relay state is invalid")
    email = str(claims["email"])
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                SELECT u.id,u.email,u.display_name,u.onboarding_completed,m.workspace_id,m.org_id,m.role
                FROM id_user u JOIN id_member m ON m.user_id=u.id
                WHERE LOWER(u.email)=LOWER(%s) AND u.status='active' AND m.workspace_id=%s AND m.org_id=%s
                ORDER BY m.created_at ASC LIMIT 1
                """,
                (email, config["workspace_id"], config["org_id"]),
            )
            user = await result.fetchone()
            if not user:
                raise HTTPException(status_code=403, detail="E09023 SAML identity is not provisioned or verified")
            replay = await conn.execute(
                """
                INSERT INTO id_federation_saml_replay(id,config_id,workspace_id,response_id,expires_at)
                VALUES (%s,%s,%s,%s,now()+interval '5 minutes')
                ON CONFLICT (config_id,response_id) DO NOTHING
                RETURNING id
                """,
                (new_id("sreplay"), config["id"], config["workspace_id"], claims["response_id"]),
            )
            if not await replay.fetchone():
                raise HTTPException(status_code=400, detail="E09022 SAML response was already consumed")
    from workama_platform.modules.auth.router import _issue_session

    session = await _issue_session(response, user["id"], user["workspace_id"], user["role"], auth_strength=2)
    session["user"] = {
        "id": user["id"],
        "email": user["email"],
        "display_name": user["display_name"],
        "workspace_id": user["workspace_id"],
        "org_id": user["org_id"],
        "role": user["role"],
        "onboarding_completed": user["onboarding_completed"],
    }
    session["sso"] = {"provider": SAML, "subject": claims["sub"], "issuer": claims["iss"], "auth_strength": 2}
    return session


@router.get("/sso-configs/{config_id}")
@router.get("/{config_id}")
async def get_sso_config(config_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require_capability(actor, "read")
    async with pool.connection() as conn:
        row = await _get_sso(conn, config_id, actor.workspace_id)
    return _sso_view(row)


@router.patch("/sso-configs/{config_id}")
@router.patch("/{config_id}")
async def update_sso_config(config_id: str, body: SsoConfigPatch, actor: Annotated[Actor, Depends(get_actor)]):
    _require_capability(actor, "write")
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="At least one SSO configuration field is required")
    async with pool.connection() as conn:
        async with conn.transaction():
            current = await _get_sso(conn, config_id, actor.workspace_id, include_deleted=False)
            values = _normalize_patch_values(current, body)
            assignments = [
                "name=%s", "issuer=%s", "metadata_url=%s", "authorization_endpoint=%s", "token_endpoint=%s", "jwks_uri=%s",
                "redirect_allowlist=%s", "mapping=%s::jsonb", "status='disabled'", "pending_reason=NULL",
                "version=version+1", "updated_at=now()",
            ]
            params: list[Any] = [
                values["name"], values["issuer"], values["metadata_url"], values["authorization_endpoint"], values["token_endpoint"], values["jwks_uri"],
                values["redirect_allowlist"], json_dumps(values["mapping"]),
            ]
            secret_values = _config_secret_values(body)
            if "client_secret" in changes or "client_secret_ref" in changes:
                assignments.extend(["client_secret_hash=%s", "client_secret_enc=%s", "client_secret_ref=%s", "client_secret_last4=%s"])
                params.extend([secret_values["client_secret_hash"], secret_values["client_secret_enc"], secret_values["client_secret_ref"], secret_values["client_secret_last4"]])
            if "certificate" in changes or "certificate_ref" in changes:
                assignments.extend(["certificate_hash=%s", "certificate_enc=%s", "certificate_ref=%s", "certificate_last4=%s"])
                params.extend([secret_values["certificate_hash"], secret_values["certificate_enc"], secret_values["certificate_ref"], secret_values["certificate_last4"]])
            params.extend([config_id, actor.workspace_id])
            result = await conn.execute(
                f"UPDATE id_federation_sso_config SET {','.join(assignments)} WHERE id=%s AND workspace_id=%s AND status <> 'deleted' RETURNING *",
                tuple(params),
            )
            row = await result.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="SSO configuration not found")
            await _emit_outbox(conn, "federation.sso.updated", actor.workspace_id, {
                "config_id": row["id"], "provider": row["provider"], "status": row["status"], "version": row["version"],
            }, row["id"])
    return _sso_view(row)


@router.post("/sso-configs/{config_id}/enable", status_code=status.HTTP_202_ACCEPTED)
@router.post("/{config_id}/enable", status_code=status.HTTP_202_ACCEPTED)
async def enable_sso_config(config_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require_capability(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await _get_sso(conn, config_id, actor.workspace_id)
            validate_sso_values(
                provider=row["provider"], issuer=row.get("issuer"), metadata_url=row.get("metadata_url"),
                authorization_endpoint=row.get("authorization_endpoint"), token_endpoint=row.get("token_endpoint"),
                jwks_uri=row.get("jwks_uri"), redirect_allowlist=_as_list(row.get("redirect_allowlist")),
                mapping=row.get("mapping") or {},
            )
            result = await conn.execute(
                "UPDATE id_federation_sso_config SET status='pending',pending_reason='not_configured',version=version+1,updated_at=now() WHERE id=%s AND workspace_id=%s AND status <> 'deleted' RETURNING *",
                (config_id, actor.workspace_id),
            )
            row = await result.fetchone()
            await _emit_outbox(conn, "federation.sso.updated", actor.workspace_id, {
                "config_id": row["id"], "provider": row["provider"], "status": row["status"], "version": row["version"],
            }, row["id"])
    result = _sso_view(row)
    result["verification"] = "pending/not_configured"
    result["upstream_signature_validation"] = False
    return result


@router.post("/sso-configs/{config_id}/disable")
@router.post("/{config_id}/disable")
async def disable_sso_config(config_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require_capability(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                "UPDATE id_federation_sso_config SET status='disabled',pending_reason=NULL,version=version+1,updated_at=now() WHERE id=%s AND workspace_id=%s AND status <> 'deleted' RETURNING *",
                (config_id, actor.workspace_id),
            )
            row = await result.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="SSO configuration not found")
            await _emit_outbox(conn, "federation.sso.updated", actor.workspace_id, {
                "config_id": row["id"], "provider": row["provider"], "status": row["status"], "version": row["version"],
            }, row["id"])
    return _sso_view(row)


@router.delete("/sso-configs/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
@router.delete("/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sso_config(config_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require_capability(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                "UPDATE id_federation_sso_config SET status='deleted',deleted_at=now(),updated_at=now(),version=version+1 WHERE id=%s AND workspace_id=%s AND status <> 'deleted' RETURNING id,provider,version",
                (config_id, actor.workspace_id),
            )
            row = await result.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="SSO configuration not found")
            await _emit_outbox(conn, "federation.sso.updated", actor.workspace_id, {
                "config_id": row["id"], "provider": row["provider"], "status": "deleted", "version": row["version"],
            }, row["id"])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _active_oidc_config(config_id: str, workspace_id: str) -> dict[str, Any]:
    async with pool.connection() as conn:
        row = await _get_sso(conn, config_id, workspace_id)
    if row["provider"] != OIDC:
        raise HTTPException(status_code=409, detail="SSO configuration is not OIDC")
    if row["status"] != "active":
        raise HTTPException(status_code=503, detail="E09009 OIDC provider is pending/not_configured")
    return row


@router.post("/sso-configs/{config_id}/oidc/authorization-start", status_code=status.HTTP_202_ACCEPTED)
@router.post("/{config_id}/oidc/authorization-start", status_code=status.HTTP_202_ACCEPTED)
async def start_oidc_authorization(config_id: str, body: OidcAuthorizationStart, actor: Annotated[Actor, Depends(get_actor)]):
    _require_capability(actor, "write")
    row = await _active_oidc_config(config_id, actor.workspace_id)
    redirect_uri = validate_redirect_uri(body.redirect_uri)
    if redirect_uri not in _as_list(row.get("redirect_allowlist")):
        raise HTTPException(status_code=422, detail="redirect_uri is not on the configured allowlist")
    bundle = generate_oidc_state_bundle()
    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    endpoint = row.get("authorization_endpoint") or f"{str(row['issuer']).rstrip('/')}/authorize"
    query = urlencode({
        "response_type": "code", "client_id": row.get("client_id") or "", "redirect_uri": redirect_uri,
        "scope": body.scope, "state": bundle["state"], "nonce": bundle["nonce"],
        "code_challenge": _pkce_challenge(bundle["code_verifier"]), "code_challenge_method": "S256",
    })
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO id_federation_oidc_state(id,config_id,org_id,workspace_id,state_hash,nonce_hash,code_verifier_hash,code_verifier_enc,redirect_uri,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (new_id("oidc"), row["id"], row["org_id"], row["workspace_id"], bundle["state_hash"], bundle["nonce_hash"], bundle["code_verifier_hash"], encrypt_secret(bundle["code_verifier"]), redirect_uri, expires_at),
            )
            await _emit_outbox(conn, "authorization.pending", actor.workspace_id, {
                "config_id": row["id"], "provider": OIDC, "status": "pending", "expires_at": expires_at.isoformat(),
            }, row["id"])
    return {
        "status": "pending",
        "authorization_url": f"{endpoint}?{query}",
        "state": bundle["state"],
        "expires_in": 300,
        "provider_exchange": "configured" if row.get("metadata_url") or row.get("token_endpoint") else "not_configured",
    }


@router.get("/sso-configs/{config_id}/oidc/callback")
@router.get("/{config_id}/oidc/callback")
async def oidc_callback(
    config_id: str,
    response: Response,
    state: str | None = Query(default=None, max_length=256),
    code: str | None = Query(default=None, max_length=4096),
    error: str | None = Query(default=None, max_length=128),
):
    if not state:
        raise HTTPException(status_code=400, detail="E09010 OIDC state is required")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                "UPDATE id_federation_oidc_state SET consumed_at=now() WHERE config_id=%s AND state_hash=%s AND consumed_at IS NULL AND expires_at > now() RETURNING *",
                (config_id, state_digest(state)),
            )
            record = await result.fetchone()
            if not record:
                raise HTTPException(status_code=400, detail="E09010 OIDC state is invalid, expired, or replayed")
            config = await _get_sso(conn, config_id, record["workspace_id"])
            if not config or config["provider"] != OIDC:
                raise HTTPException(status_code=400, detail="E09010 OIDC state is invalid, expired, or replayed")
            if config["status"] != "active":
                raise HTTPException(status_code=503, detail="E09009 OIDC provider is pending/not_configured")
            if error:
                raise HTTPException(status_code=400, detail="E09010 OIDC provider rejected authorization")
            if not code:
                raise HTTPException(status_code=400, detail="E09010 OIDC callback code is missing")
    try:
        claims = await _exchange_oidc_code(config, record, code)
    except OidcTokenValidationError as exc:
        raise HTTPException(status_code=401, detail="E09012 OIDC ID token validation failed") from exc
    except OidcExchangeError as exc:
        raise HTTPException(status_code=502, detail="E09011 OIDC provider exchange failed") from exc

    mapping = config.get("mapping") or {}
    email_claim = str(mapping.get("email_claim") or "email")
    verified_claim = str(mapping.get("email_verified_claim") or "email_verified")
    email = str(claims.get(email_claim) or "").strip().lower()
    email_verified = claims.get(verified_claim) in (True, "true", "True", 1, "1")
    if not email or not email_verified:
        raise HTTPException(status_code=403, detail="E09013 OIDC identity is not provisioned or verified")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT u.id,u.email,u.display_name,u.onboarding_completed,m.workspace_id,m.org_id,m.role
            FROM id_user u JOIN id_member m ON m.user_id=u.id
            WHERE LOWER(u.email)=LOWER(%s) AND u.status='active' AND m.workspace_id=%s AND m.org_id=%s
            ORDER BY m.created_at ASC LIMIT 1
            """,
            (email, config["workspace_id"], config["org_id"]),
        )
        user = await result.fetchone()
    if not user:
        raise HTTPException(status_code=403, detail="E09013 OIDC identity is not provisioned or verified")
    from workama_platform.modules.auth.router import _issue_session

    session = await _issue_session(response, user["id"], user["workspace_id"], user["role"], auth_strength=2)
    session["user"] = {
        "id": user["id"],
        "email": user["email"],
        "display_name": user["display_name"],
        "workspace_id": user["workspace_id"],
        "org_id": user["org_id"],
        "role": user["role"],
        "onboarding_completed": user["onboarding_completed"],
    }
    session["sso"] = {"provider": OIDC, "subject": str(claims["sub"]), "issuer": config["issuer"], "auth_strength": 2}
    return session


def _new_scim_token() -> tuple[str, str]:
    raw = SCIM_TOKEN_PREFIX + secrets.token_urlsafe(40)
    return raw, hash_secret(raw)


async def _scim_token_workspace(conn, actor: Actor, requested: str | None) -> None:
    await _workspace_for_actor(conn, actor, requested)


@router.post("/scim-tokens", status_code=status.HTTP_201_CREATED)
async def create_scim_token(body: ScimTokenCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require_capability(actor, "write")
    if body.expires_at and body.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=422, detail="expires_at must be in the future")
    raw, digest = _new_scim_token()
    workspace_id = body.workspace_id or actor.workspace_id
    async with pool.connection() as conn:
        async with conn.transaction():
            await _scim_token_workspace(conn, actor, workspace_id)
            result = await conn.execute(
                "INSERT INTO id_federation_scim_token(id,org_id,workspace_id,token_hash,last_four,expires_at,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (new_id("sct"), actor.org_id, workspace_id, digest, raw[-4:], body.expires_at, actor.user_id),
            )
            row = await result.fetchone()
    return {**_token_view(row), "token": raw, "token_display": "one_time"}


@router.post("/scim-tokens/{token_id}/rotate", status_code=status.HTTP_201_CREATED)
async def rotate_scim_token(token_id: str, actor: Annotated[Actor, Depends(get_actor)], body: ScimTokenCreate | None = None):
    _require_capability(actor, "write")
    expires_at = body.expires_at if body else None
    if expires_at and expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=422, detail="expires_at must be in the future")
    raw, digest = _new_scim_token()
    async with pool.connection() as conn:
        async with conn.transaction():
            old_result = await conn.execute(
                "SELECT * FROM id_federation_scim_token WHERE id=%s AND workspace_id=%s AND status='active' FOR UPDATE",
                (token_id, actor.workspace_id),
            )
            old = await old_result.fetchone()
            if not old:
                raise HTTPException(status_code=404, detail="SCIM token not found")
            await conn.execute("UPDATE id_federation_scim_token SET status='revoked',revoked_at=now() WHERE id=%s", (token_id,))
            result = await conn.execute(
                "INSERT INTO id_federation_scim_token(id,org_id,workspace_id,token_hash,last_four,expires_at,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (new_id("sct"), actor.org_id, actor.workspace_id, digest, raw[-4:], expires_at, actor.user_id),
            )
            row = await result.fetchone()
    return {**_token_view(row), "token": raw, "token_display": "one_time", "rotated_from": token_id}


@router.delete("/scim-tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_scim_token(token_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require_capability(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                "UPDATE id_federation_scim_token SET status='revoked',revoked_at=now() WHERE id=%s AND workspace_id=%s AND status='active' RETURNING id",
                (token_id, actor.workspace_id),
            )
            if not await result.fetchone():
                raise HTTPException(status_code=404, detail="SCIM token not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _authenticate_scim_token(conn, workspace_id: str, authorization: str | None) -> dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="SCIM bearer token is required")
    raw = authorization[7:].strip()
    if not raw.startswith(SCIM_TOKEN_PREFIX) or len(raw) < len(SCIM_TOKEN_PREFIX) + 16:
        raise HTTPException(status_code=401, detail="SCIM bearer token is invalid")
    result = await conn.execute(
        "SELECT * FROM id_federation_scim_token WHERE workspace_id=%s AND token_hash=%s AND status='active' AND (expires_at IS NULL OR expires_at > now())",
        (workspace_id, hash_secret(raw)),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="SCIM bearer token is invalid or expired")
    await conn.execute("UPDATE id_federation_scim_token SET last_used_at=now() WHERE id=%s", (row["id"],))
    return row


def _scim_error(detail: str, code: int = 400) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


def parse_scim_filter(value: str | None) -> tuple[str, str, str] | None:
    if not value:
        return None
    match = re.fullmatch(r'\s*(userName|externalId|displayName|active)\s+(eq|co)\s+("(?:[^"\\]|\\.)*"|true|false)\s*', value, re.IGNORECASE)
    if not match:
        raise _scim_error("Only simple SCIM eq/co filters are supported", 400)
    field, operator, raw = match.groups()
    if raw.lower() in {"true", "false"}:
        parsed = raw.lower()
    else:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _scim_error("SCIM filter value is invalid", 400) from exc
    if operator.lower() == "co" and field.lower() == "active":
        raise _scim_error("active does not support co", 400)
    return field, operator.lower(), parsed


def _filter_sql(filter_value: tuple[str, str, str] | None, *, alias: str = "u") -> tuple[str, list[Any]]:
    if not filter_value:
        return "", []
    field, operator, value = filter_value
    columns = {"username": "user_name", "externalid": "external_id", "displayname": "display_name", "active": "active"}
    column = columns[field.lower()]
    if operator == "co":
        return f" AND {alias}.{column} ILIKE %s", [f"%{value}%"]
    if column == "active":
        return f" AND {alias}.{column}=%s", [value == "true"]
    return f" AND lower({alias}.{column})=lower(%s)", [value]


def _group_filter_sql(filter_value: tuple[str, str, str] | None) -> tuple[str, list[Any]]:
    if not filter_value:
        return "", []
    field, operator, value = filter_value
    columns = {"externalid": "external_id", "displayname": "display_name"}
    column = columns.get(field.lower())
    if not column:
        raise _scim_error("SCIM Group filter field is not supported", 400)
    if operator == "co":
        return f" AND g.{column} ILIKE %s", [f"%{value}%"]
    return f" AND lower(g.{column})=lower(%s)", [value]


async def _get_scim_user(conn, workspace_id: str, resource_id: str) -> dict[str, Any]:
    result = await conn.execute(
        "SELECT * FROM id_federation_scim_user WHERE id=%s AND workspace_id=%s",
        (resource_id, workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise _scim_error("SCIM User not found", 404)
    return row


async def _get_scim_group(conn, workspace_id: str, resource_id: str) -> dict[str, Any]:
    result = await conn.execute(
        "SELECT * FROM id_federation_scim_group WHERE id=%s AND workspace_id=%s",
        (resource_id, workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise _scim_error("SCIM Group not found", 404)
    return row


async def _group_members(conn, workspace_id: str, group_id: str) -> list[dict[str, Any]]:
    result = await conn.execute(
        "SELECT scim_user_id,external_member_id,display FROM id_federation_scim_group_member WHERE group_id=%s AND workspace_id=%s ORDER BY created_at,id",
        (group_id, workspace_id),
    )
    return await result.fetchall()


async def _resolve_member(conn, workspace_id: str, member: ScimMember) -> tuple[str | None, str, str | None]:
    value = normalize_scim_value(member.value, "member.value", 256)
    result = await conn.execute(
        "SELECT id,external_id,user_name FROM id_federation_scim_user WHERE workspace_id=%s AND (id=%s OR external_id=%s) LIMIT 1",
        (workspace_id, value, value),
    )
    user = await result.fetchone()
    return (user["id"], value, member.display or (user["user_name"] if user else None)) if user else (None, value, member.display)


async def _replace_group_members(conn, group: dict[str, Any], members: list[ScimMember]) -> None:
    await conn.execute("DELETE FROM id_federation_scim_group_member WHERE group_id=%s AND workspace_id=%s", (group["id"], group["workspace_id"]))
    for member in members:
        user_id, external_member_id, display = await _resolve_member(conn, group["workspace_id"], member)
        await conn.execute(
            "INSERT INTO id_federation_scim_group_member(id,org_id,workspace_id,group_id,scim_user_id,external_member_id,display) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (group_id,external_member_id) DO UPDATE SET scim_user_id=EXCLUDED.scim_user_id,display=EXCLUDED.display",
            (new_id("scgm"), group["org_id"], group["workspace_id"], group["id"], user_id, external_member_id, display),
        )


async def _emit_user_event(conn, workspace_id: str, event_type: str, row: dict[str, Any]) -> None:
    payload = {"resource_id": row["id"], "external_id": row.get("external_id"), "active": bool(row.get("active")), "version": row.get("version", 1)}
    await _emit_outbox(conn, event_type, workspace_id, payload, row["id"])


async def _emit_group_event(conn, workspace_id: str, row: dict[str, Any]) -> None:
    members = await _group_members(conn, workspace_id, row["id"])
    await _emit_outbox(conn, "scim.group.upserted", workspace_id, {
        "resource_id": row["id"], "external_id": row.get("external_id"), "member_count": len(members), "version": row.get("version", 1),
    }, row["id"])


@scim_router.get("/{workspace_id}/Users")
async def list_scim_users(
    workspace_id: str,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    filter: str | None = Query(default=None, max_length=512),
    start_index: int = Query(default=1, alias="startIndex", ge=1, le=1_000_000),
    count: int = Query(default=100, ge=1, le=200),
):
    parsed_filter = parse_scim_filter(filter)
    where, filter_params = _filter_sql(parsed_filter)
    async with pool.connection() as conn:
        async with conn.transaction():
            await _authenticate_scim_token(conn, workspace_id, authorization)
            total_result = await conn.execute(f"SELECT count(*) AS total FROM id_federation_scim_user u WHERE u.workspace_id=%s{where}", tuple([workspace_id, *filter_params]))
            total = int((await total_result.fetchone())["total"])
            result = await conn.execute(
                f"SELECT u.* FROM id_federation_scim_user u WHERE u.workspace_id=%s{where} ORDER BY u.created_at,u.id LIMIT %s OFFSET %s",
                tuple([workspace_id, *filter_params, count, start_index - 1]),
            )
            rows = await result.fetchall()
    return {
        "schemas": [SCIM_SCHEMAS["list"]], "totalResults": total, "startIndex": start_index,
        "itemsPerPage": len(rows), "Resources": [_user_view(row, workspace_id) for row in rows],
    }


@scim_router.get("/{workspace_id}/Users/{resource_id}")
async def get_scim_user(workspace_id: str, resource_id: str, authorization: Annotated[str | None, Header(alias="Authorization")] = None):
    async with pool.connection() as conn:
        async with conn.transaction():
            await _authenticate_scim_token(conn, workspace_id, authorization)
            row = await _get_scim_user(conn, workspace_id, resource_id)
    return _user_view(row, workspace_id)


def _user_patch_values(current: dict[str, Any], body: ScimPatchRequest) -> dict[str, Any]:
    values = {"external_id": current["external_id"], "user_name": current["user_name"], "display_name": current["display_name"], "active": bool(current["active"])}
    operations = list(body.operations)
    if body.active is not None:
        values["active"] = body.active
    if body.user_name is not None:
        values["user_name"] = normalize_scim_value(body.user_name, "userName", 320)
    if body.display_name is not None:
        values["display_name"] = normalize_scim_value(body.display_name, "displayName", 256)
    if body.external_id is not None:
        values["external_id"] = normalize_scim_value(body.external_id, "externalId", 256)
    for operation in operations:
        path = (operation.path or "").split(":")[-1].lower()
        if path == "" and isinstance(operation.value, dict):
            nested = ScimUserCreate.model_validate(operation.value)
            values.update({"external_id": nested.external_id or values["external_id"], "user_name": nested.user_name, "display_name": nested.display_name or values["display_name"], "active": nested.active})
            continue
        if path not in {"active", "username", "displayname", "externalid"}:
            raise _scim_error("Unsupported SCIM User PATCH path", 400)
        if operation.op == "remove" and path in {"displayname", "externalid"}:
            raise _scim_error("SCIM User identity fields cannot be removed", 400)
        if path == "active":
            if operation.op == "remove":
                values["active"] = False
            elif not isinstance(operation.value, bool):
                raise _scim_error("active must be boolean", 400)
            else:
                values["active"] = operation.value
        elif path == "username":
            values["user_name"] = normalize_scim_value(str(operation.value), "userName", 320)
        elif path == "displayname":
            values["display_name"] = normalize_scim_value(str(operation.value), "displayName", 256)
        elif path == "externalid":
            values["external_id"] = normalize_scim_value(str(operation.value), "externalId", 256)
    return values


async def _upsert_scim_user(conn, workspace_id: str, org_id: str, body: ScimUserCreate, resource_id: str | None = None) -> tuple[dict[str, Any], bool]:
    external_id = body.external_id or f"generated:{hashlib.sha256(body.user_name.encode()).hexdigest()[:32]}"
    existing_result = await conn.execute(
        "SELECT * FROM id_federation_scim_user WHERE workspace_id=%s AND (external_id=%s OR id=%s) LIMIT 1 FOR UPDATE",
        (workspace_id, external_id, resource_id or ""),
    )
    existing = await existing_result.fetchone()
    if existing:
        if existing["user_name"].lower() != body.user_name.lower() and resource_id is None:
            raise _scim_error("SCIM externalId already belongs to another user", 409)
        result = await conn.execute(
            "UPDATE id_federation_scim_user SET external_id=%s,user_name=%s,display_name=%s,active=%s,version=version+1,updated_at=now() WHERE id=%s AND workspace_id=%s RETURNING *",
            (external_id, body.user_name, body.display_name or body.user_name, body.active, existing["id"], workspace_id),
        )
        row = await result.fetchone()
        await conn.execute("UPDATE id_user SET display_name=%s,status=%s,updated_at=now() WHERE id=%s", (row["display_name"], "active" if row["active"] else "inactive", row["user_id"]))
        await conn.execute("UPDATE id_federation_scim_group_member SET scim_user_id=%s WHERE workspace_id=%s AND external_member_id=%s", (row["id"], workspace_id, external_id))
        return row, True

    duplicate_name = await conn.execute(
        "SELECT id FROM id_federation_scim_user WHERE workspace_id=%s AND lower(user_name)=lower(%s)",
        (workspace_id, body.user_name),
    )
    if await duplicate_name.fetchone():
        raise _scim_error("SCIM userName already exists", 409)
    email_candidate = body.user_name.lower() if "@" in body.user_name and " " not in body.user_name else None
    email = email_candidate
    if email:
        email_result = await conn.execute("SELECT id FROM id_user WHERE email=%s", (email,))
        email_row = await email_result.fetchone()
        if email_row:
            member_result = await conn.execute("SELECT 1 FROM id_member WHERE user_id=%s AND workspace_id=%s", (email_row["id"], workspace_id))
            if await member_result.fetchone():
                user_id = email_row["id"]
            else:
                email = None
    if not email:
        suffix = hashlib.sha256(f"{org_id}:{workspace_id}:{external_id}".encode()).hexdigest()[:32]
        email = f"scim+{suffix}@invalid.workama.local"
    if "user_id" not in locals():
        user_id = new_id("usr")
        await conn.execute(
            "INSERT INTO id_user(id,email,password_hash,display_name,status,email_verified) VALUES (%s,%s,%s,%s,%s,TRUE)",
            (user_id, email, hash_password(secrets.token_urlsafe(32)), body.display_name or body.user_name, "active" if body.active else "inactive"),
        )
        await conn.execute(
            "INSERT INTO id_member(id,org_id,workspace_id,user_id,role) VALUES (%s,%s,%s,%s,'member')",
            (new_id("mem"), org_id, workspace_id, user_id),
        )
    resource_id = new_id("scu")
    result = await conn.execute(
        "INSERT INTO id_federation_scim_user(id,org_id,workspace_id,external_id,user_id,user_name,display_name,active) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (resource_id, org_id, workspace_id, external_id, user_id, body.user_name, body.display_name or body.user_name, body.active),
    )
    row = await result.fetchone()
    await conn.execute("UPDATE id_federation_scim_group_member SET scim_user_id=%s WHERE workspace_id=%s AND external_member_id=%s", (row["id"], workspace_id, external_id))
    return row, False


@scim_router.post("/{workspace_id}/Users", status_code=status.HTTP_201_CREATED)
async def create_scim_user(workspace_id: str, body: ScimUserCreate, authorization: Annotated[str | None, Header(alias="Authorization")] = None):
    if not body.external_id:
        raise _scim_error("externalId is required for SCIM User provisioning", 400)
    async with pool.connection() as conn:
        async with conn.transaction():
            token = await _authenticate_scim_token(conn, workspace_id, authorization)
            row, replayed = await _upsert_scim_user(conn, workspace_id, token["org_id"], body)
            await _emit_user_event(conn, workspace_id, "scim.user.upserted", row)
    result = _user_view(row, workspace_id)
    if replayed:
        result["idempotent_replay"] = True
    return result


@scim_router.patch("/{workspace_id}/Users/{resource_id}")
async def patch_scim_user(workspace_id: str, resource_id: str, body: ScimPatchRequest, authorization: Annotated[str | None, Header(alias="Authorization")] = None):
    async with pool.connection() as conn:
        async with conn.transaction():
            await _authenticate_scim_token(conn, workspace_id, authorization)
            current = await _get_scim_user(conn, workspace_id, resource_id)
            values = _user_patch_values(current, body)
            duplicate = await conn.execute("SELECT id FROM id_federation_scim_user WHERE workspace_id=%s AND id<>%s AND (external_id=%s OR lower(user_name)=lower(%s))", (workspace_id, resource_id, values["external_id"], values["user_name"]))
            if await duplicate.fetchone():
                raise _scim_error("SCIM User identity already exists", 409)
            result = await conn.execute(
                "UPDATE id_federation_scim_user SET external_id=%s,user_name=%s,display_name=%s,active=%s,version=version+1,updated_at=now() WHERE id=%s AND workspace_id=%s RETURNING *",
                (values["external_id"], values["user_name"], values["display_name"], values["active"], resource_id, workspace_id),
            )
            row = await result.fetchone()
            await conn.execute("UPDATE id_user SET display_name=%s,status=%s,updated_at=now() WHERE id=%s", (row["display_name"], "active" if row["active"] else "inactive", row["user_id"]))
            event = "scim.user.upserted" if row["active"] else "scim.user.deprovisioned"
            await _emit_user_event(conn, workspace_id, event, row)
    return _user_view(row, workspace_id)


@scim_router.delete("/{workspace_id}/Users/{resource_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scim_user(workspace_id: str, resource_id: str, authorization: Annotated[str | None, Header(alias="Authorization")] = None):
    async with pool.connection() as conn:
        async with conn.transaction():
            await _authenticate_scim_token(conn, workspace_id, authorization)
            current = await _get_scim_user(conn, workspace_id, resource_id)
            result = await conn.execute("UPDATE id_federation_scim_user SET active=FALSE,version=version+1,updated_at=now() WHERE id=%s AND workspace_id=%s RETURNING *", (resource_id, workspace_id))
            row = await result.fetchone()
            await conn.execute("UPDATE id_user SET status='inactive',updated_at=now() WHERE id=%s", (row["user_id"],))
            await _emit_user_event(conn, workspace_id, "scim.user.deprovisioned", row)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@scim_router.get("/{workspace_id}/Groups")
async def list_scim_groups(
    workspace_id: str,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    filter: str | None = Query(default=None, max_length=512),
    start_index: int = Query(default=1, alias="startIndex", ge=1, le=1_000_000),
    count: int = Query(default=100, ge=1, le=200),
):
    parsed_filter = parse_scim_filter(filter)
    where, filter_params = _group_filter_sql(parsed_filter)
    async with pool.connection() as conn:
        async with conn.transaction():
            await _authenticate_scim_token(conn, workspace_id, authorization)
            total_result = await conn.execute(f"SELECT count(*) AS total FROM id_federation_scim_group g WHERE g.workspace_id=%s{where}", tuple([workspace_id, *filter_params]))
            total = int((await total_result.fetchone())["total"])
            result = await conn.execute(f"SELECT g.* FROM id_federation_scim_group g WHERE g.workspace_id=%s{where} ORDER BY g.created_at,g.id LIMIT %s OFFSET %s", tuple([workspace_id, *filter_params, count, start_index - 1]))
            groups = await result.fetchall()
            resources = []
            for group in groups:
                resources.append(_group_view(group, await _group_members(conn, workspace_id, group["id"]), workspace_id))
    return {"schemas": [SCIM_SCHEMAS["list"]], "totalResults": total, "startIndex": start_index, "itemsPerPage": len(resources), "Resources": resources}


@scim_router.get("/{workspace_id}/Groups/{resource_id}")
async def get_scim_group(workspace_id: str, resource_id: str, authorization: Annotated[str | None, Header(alias="Authorization")] = None):
    async with pool.connection() as conn:
        async with conn.transaction():
            await _authenticate_scim_token(conn, workspace_id, authorization)
            row = await _get_scim_group(conn, workspace_id, resource_id)
            members = await _group_members(conn, workspace_id, row["id"])
    return _group_view(row, members, workspace_id)


def _group_patch_values(current: dict[str, Any], body: ScimGroupPatchRequest) -> tuple[dict[str, Any], list[ScimMember] | None]:
    values = {"external_id": current["external_id"], "display_name": current["display_name"]}
    members: list[ScimMember] | None = None
    if body.external_id is not None:
        values["external_id"] = normalize_scim_value(body.external_id, "externalId", 256)
    if body.display_name is not None:
        values["display_name"] = normalize_scim_value(body.display_name, "displayName", 256)
    if body.members is not None:
        members = body.members
    for operation in body.operations:
        path = (operation.path or "").split(":")[-1].lower()
        if path in {"displayname", "externalid"}:
            if operation.op == "remove":
                raise _scim_error("SCIM Group identity fields cannot be removed", 400)
            values["display_name" if path == "displayname" else "external_id"] = normalize_scim_value(str(operation.value), "group field", 256)
        elif path == "members":
            if operation.op == "remove":
                members = []
            elif not isinstance(operation.value, list):
                raise _scim_error("members must be an array", 400)
            else:
                parsed = [ScimMember.model_validate(item) for item in operation.value]
                if operation.op == "add":
                    members = (members or []) + parsed
                else:
                    members = parsed
        else:
            raise _scim_error("Unsupported SCIM Group PATCH path", 400)
    return values, members


@scim_router.post("/{workspace_id}/Groups", status_code=status.HTTP_201_CREATED)
async def create_scim_group(workspace_id: str, body: ScimGroupCreate, authorization: Annotated[str | None, Header(alias="Authorization")] = None):
    external_id = body.external_id or f"generated:{hashlib.sha256(body.display_name.encode()).hexdigest()[:32]}"
    async with pool.connection() as conn:
        async with conn.transaction():
            token = await _authenticate_scim_token(conn, workspace_id, authorization)
            existing_result = await conn.execute("SELECT * FROM id_federation_scim_group WHERE workspace_id=%s AND external_id=%s FOR UPDATE", (workspace_id, external_id))
            existing = await existing_result.fetchone()
            if existing:
                result = await conn.execute("UPDATE id_federation_scim_group SET display_name=%s,active=TRUE,version=version+1,updated_at=now() WHERE id=%s AND workspace_id=%s RETURNING *", (body.display_name, existing["id"], workspace_id))
                row = await result.fetchone()
                replayed = True
            else:
                result = await conn.execute("INSERT INTO id_federation_scim_group(id,org_id,workspace_id,external_id,display_name) VALUES (%s,%s,%s,%s,%s) RETURNING *", (new_id("scg"), token["org_id"], workspace_id, external_id, body.display_name))
                row = await result.fetchone()
                replayed = False
            await _replace_group_members(conn, row, body.members)
            await _emit_group_event(conn, workspace_id, row)
            members = await _group_members(conn, workspace_id, row["id"])
    result = _group_view(row, members, workspace_id)
    if replayed:
        result["idempotent_replay"] = True
    return result


@scim_router.patch("/{workspace_id}/Groups/{resource_id}")
async def patch_scim_group(workspace_id: str, resource_id: str, body: ScimGroupPatchRequest, authorization: Annotated[str | None, Header(alias="Authorization")] = None):
    async with pool.connection() as conn:
        async with conn.transaction():
            await _authenticate_scim_token(conn, workspace_id, authorization)
            current = await _get_scim_group(conn, workspace_id, resource_id)
            values, members = _group_patch_values(current, body)
            duplicate = await conn.execute("SELECT id FROM id_federation_scim_group WHERE workspace_id=%s AND id<>%s AND external_id=%s", (workspace_id, resource_id, values["external_id"]))
            if await duplicate.fetchone():
                raise _scim_error("SCIM Group externalId already exists", 409)
            result = await conn.execute("UPDATE id_federation_scim_group SET external_id=%s,display_name=%s,version=version+1,updated_at=now() WHERE id=%s AND workspace_id=%s RETURNING *", (values["external_id"], values["display_name"], resource_id, workspace_id))
            row = await result.fetchone()
            if members is not None:
                await _replace_group_members(conn, row, members)
            await _emit_group_event(conn, workspace_id, row)
            members_view = await _group_members(conn, workspace_id, row["id"])
    return _group_view(row, members_view, workspace_id)


@scim_router.delete("/{workspace_id}/Groups/{resource_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scim_group(workspace_id: str, resource_id: str, authorization: Annotated[str | None, Header(alias="Authorization")] = None):
    async with pool.connection() as conn:
        async with conn.transaction():
            await _authenticate_scim_token(conn, workspace_id, authorization)
            await _get_scim_group(conn, workspace_id, resource_id)
            result = await conn.execute("UPDATE id_federation_scim_group SET active=FALSE,version=version+1,updated_at=now() WHERE id=%s AND workspace_id=%s RETURNING *", (resource_id, workspace_id))
            row = await result.fetchone()
            await _emit_group_event(conn, workspace_id, row)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# =============================================================================
# M9 SSO/SCIM 增强：SAML/OIDC 登录流程 + SCIM 同步执行
# 使用标准库 urllib + xml.etree（不引入第三方 SAML/OIDC 库）
# =============================================================================


class SsoLoginResult(BaseModel):
    """SSO 登录结果响应模型。"""
    model_config = ConfigDict(extra="allow")

    access_token: str
    token_type: str = "bearer"
    user: dict[str, Any]
    sso: dict[str, Any]


class SsoTestResult(BaseModel):
    """SSO 连接测试结果模型。"""
    model_config = ConfigDict(extra="allow")

    status: Literal["ok", "failed"]
    provider: str
    error: str | None = None


class ScimSyncReport(BaseModel):
    """SCIM 同步执行报告模型。"""
    model_config = ConfigDict(extra="allow")

    run_id: str
    status: Literal["running", "completed", "failed"]
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    total: int = 0


class ScimSyncHistoryItem(BaseModel):
    """SCIM 同步历史记录模型。"""
    model_config = ConfigDict(extra="allow")

    id: str
    workspace_id: str
    provider_id: str
    status: str
    created_users: int = 0
    updated_users: int = 0
    deactivated_users: int = 0
    total_users: int = 0
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


def _http_get_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """用标准库 urllib 发起 GET 请求，返回 JSON 字典。

    v7.172 修复：调用前先经 ``validate_outbound_url`` 做 SSRF 校验，
    阻断内网/loopback/元数据地址等不安全出站请求。
    """
    validation = validate_outbound_url(url)
    if not validation.allowed:
        raise ValueError(f"Outbound URL is not allowed: {validation.reason}")
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read()
    return json.loads(body.decode("utf-8"))


def _http_post_json(url: str, data: dict[str, str], headers: dict[str, str] | None = None) -> dict[str, Any]:
    """用标准库 urllib 发起 POST 请求（表单编码），返回 JSON 字典。

    v7.172 修复：与 ``_http_get_json`` 一致，出站前 SSRF 校验。
    """
    validation = validate_outbound_url(url)
    if not validation.allowed:
        raise ValueError(f"Outbound URL is not allowed: {validation.reason}")
    body = urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        response_body = resp.read()
    return json.loads(response_body.decode("utf-8"))


def _http_get_raw(url: str, headers: dict[str, str] | None = None) -> bytes:
    """用标准库 urllib 发起 GET 请求，返回原始字节（用于连接可达性测试）。

    v7.172 修复：与 ``_http_get_json`` 一致，出站前 SSRF 校验。
    """
    validation = validate_outbound_url(url)
    if not validation.allowed:
        raise ValueError(f"Outbound URL is not allowed: {validation.reason}")
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read()


def _parse_saml_response_basic(saml_response_b64: str) -> dict[str, Any]:
    """解析 SAML Response（base64 解码，用 xml.etree 提取 NameID + Attributes）。

    注意：此函数仅实现基本解析流程，不验证 XML 签名。
    生产环境应加签名验证（参见 _validate_saml_response）。
    """
    if not saml_response_b64:
        raise ValueError("SAML Response is empty")
    try:
        decoded = base64.b64decode(saml_response_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("SAML Response is not valid base64") from exc
    if not decoded:
        raise ValueError("SAML Response is empty after decode")
    try:
        root = ET.fromstring(decoded)
    except ET.ParseError as exc:
        raise ValueError("SAML Response XML is invalid") from exc

    ns = {"samlp": SAML_PROTOCOL_NS, "saml": SAML_ASSERTION_NS}
    response_id = root.get("ID", "")

    assertion = root.find(".//saml:Assertion", ns)
    if assertion is None:
        raise ValueError("SAML Response missing Assertion")

    name_id_elem = assertion.find(".//saml:NameID", ns)
    name_id = (name_id_elem.text or "").strip() if name_id_elem is not None and name_id_elem.text else ""
    if not name_id:
        raise ValueError("SAML Response missing NameID")

    attributes: dict[str, str] = {}
    for attr in assertion.findall(".//saml:Attribute", ns):
        name = attr.get("Name") or attr.get("FriendlyName") or ""
        if not name:
            continue
        value_elem = attr.find("saml:AttributeValue", ns)
        value = (value_elem.text or "").strip() if value_elem is not None and value_elem.text else ""
        if value:
            attributes[name] = value

    return {
        "response_id": response_id,
        "name_id": name_id,
        "attributes": attributes,
    }


def _fetch_oidc_userinfo(access_token: str, userinfo_endpoint: str) -> dict[str, Any]:
    """用 access_token 通过 urllib GET userinfo 端点获取用户信息。"""
    headers = {"Authorization": f"Bearer {access_token}"}
    return _http_get_json(userinfo_endpoint, headers=headers)


def _exchange_oidc_code_basic(code: str, config: dict[str, Any]) -> dict[str, Any]:
    """用授权码通过 urllib POST token 端点换取 access_token（基本流程）。"""
    issuer = str(config.get("issuer") or "").rstrip("/")
    token_endpoint = str(config.get("token_endpoint") or f"{issuer}/token")
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": str(config.get("redirect_uri") or ""),
        "client_id": str(config.get("client_id") or ""),
        "client_secret": str(config.get("client_secret") or ""),
    }
    return _http_post_json(token_endpoint, data)


async def _get_provider_config(provider_id: str) -> dict[str, Any]:
    """获取 provider 配置（不要求认证，用于登录流程端点）。"""
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM id_federation_sso_config WHERE id=%s AND status <> 'deleted'",
            (provider_id,),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="SSO provider not found")
    return row


async def _create_or_update_user_from_sso(
    conn,
    email: str,
    display_name: str,
    provider_id: str,
    workspace_id: str,
    org_id: str,
) -> tuple[dict[str, Any], bool]:
    """根据 SSO 返回的 email 创建或更新本地用户，返回 (user_row, created)。"""
    result = await conn.execute(
        """
        SELECT u.id,u.email,u.display_name,u.onboarding_completed,m.workspace_id,m.org_id,m.role
        FROM id_user u JOIN id_member m ON m.user_id=u.id
        WHERE LOWER(u.email)=LOWER(%s) AND u.status='active' AND m.workspace_id=%s AND m.org_id=%s
        ORDER BY m.created_at ASC LIMIT 1
        """,
        (email, workspace_id, org_id),
    )
    user = await result.fetchone()
    if user:
        if display_name and display_name != user["display_name"]:
            await conn.execute(
                "UPDATE id_user SET display_name=%s,updated_at=now() WHERE id=%s",
                (display_name, user["id"]),
            )
        return dict(user), False

    user_id = new_id("usr")
    await conn.execute(
        "INSERT INTO id_user(id,email,password_hash,display_name,status,email_verified) VALUES (%s,%s,%s,%s,%s,TRUE)",
        (user_id, email, hash_password(secrets.token_urlsafe(32)), display_name or email, "active"),
    )
    await conn.execute(
        "INSERT INTO id_member(id,org_id,workspace_id,user_id,role) VALUES (%s,%s,%s,%s,'member')",
        (new_id("mem"), org_id, workspace_id, user_id),
    )
    return {
        "id": user_id,
        "email": email,
        "display_name": display_name or email,
        "onboarding_completed": False,
        "workspace_id": workspace_id,
        "org_id": org_id,
        "role": "member",
    }, True


def _test_oidc_connection(config: dict[str, Any]) -> dict[str, Any]:
    """验证 OIDC provider 配置：尝试 GET {issuer}/.well-known/openid-configuration。"""
    issuer = str(config.get("issuer") or "").rstrip("/")
    if not issuer:
        return {"status": "failed", "provider": OIDC, "error": "issuer is not configured"}
    discovery_url = f"{issuer}/.well-known/openid-configuration"
    try:
        metadata = _http_get_json(discovery_url)
        return {
            "status": "ok",
            "provider": OIDC,
            "issuer": metadata.get("issuer", issuer),
            "authorization_endpoint": metadata.get("authorization_endpoint"),
            "token_endpoint": metadata.get("token_endpoint"),
            "userinfo_endpoint": metadata.get("userinfo_endpoint"),
            "jwks_uri": metadata.get("jwks_uri"),
        }
    except Exception as exc:
        return {"status": "failed", "provider": OIDC, "error": str(exc)}


def _test_saml_connection(config: dict[str, Any]) -> dict[str, Any]:
    """验证 SAML provider 配置：验证证书格式 + endpoint 可达性。"""
    certificate_enc = config.get("certificate_enc")
    certificate_configured = False
    if certificate_enc:
        try:
            cert = decrypt_secret(certificate_enc)
            if cert:
                normalize_saml_certificate(cert)
                certificate_configured = True
        except Exception as exc:
            return {"status": "failed", "provider": SAML, "error": f"certificate is invalid: {exc}"}
    elif config.get("certificate_ref"):
        certificate_configured = True

    metadata_url = config.get("metadata_url")
    endpoint_reachable = False
    endpoint_error: str | None = None
    if metadata_url:
        try:
            _http_get_raw(metadata_url)
            endpoint_reachable = True
        except Exception as exc:
            endpoint_error = str(exc)

    mapping = config.get("mapping") or {}
    sso_url = mapping.get("sso_url") or str(config.get("issuer") or "")
    status_val = "ok" if certificate_configured and (endpoint_reachable or not metadata_url) else "failed"
    return {
        "status": status_val,
        "provider": SAML,
        "issuer": config.get("issuer"),
        "sso_url": sso_url,
        "certificate_configured": certificate_configured,
        "endpoint_reachable": endpoint_reachable,
        "endpoint_error": endpoint_error,
    }


async def _sync_scim_users(config: dict[str, Any], workspace_id: str, org_id: str) -> dict[str, Any]:
    """从 IdP 拉取 SCIM 用户列表，与本地比对，创建/更新/禁用差异用户。

    使用 urllib GET {scim_endpoint}/Users（Bearer SCIM token）。
    """
    mapping = config.get("mapping") or {}
    scim_endpoint = str(mapping.get("scim_endpoint") or str(config.get("issuer") or "")).rstrip("/")
    scim_token = str(mapping.get("scim_bearer") or "")
    users_url = f"{scim_endpoint}/Users"

    remote_data = _http_get_json(users_url, headers={"Authorization": f"Bearer {scim_token}"})
    remote_users = remote_data.get("Resources", []) if isinstance(remote_data, dict) else []

    created = 0
    updated = 0
    deactivated = 0
    remote_external_ids: set[str] = set()

    async with pool.connection() as conn:
        async with conn.transaction():
            for remote_user in remote_users:
                external_id = str(remote_user.get("externalId") or remote_user.get("id") or "")
                if not external_id:
                    continue
                user_name = str(remote_user.get("userName") or external_id)
                display_name = str(remote_user.get("displayName") or user_name)
                active = bool(remote_user.get("active", True))
                remote_external_ids.add(external_id)

                result = await conn.execute(
                    "SELECT * FROM id_federation_scim_user WHERE workspace_id=%s AND external_id=%s FOR UPDATE",
                    (workspace_id, external_id),
                )
                local = await result.fetchone()
                if local is None:
                    # 创建缺失用户
                    user_id = new_id("usr")
                    email = user_name if "@" in user_name and " " not in user_name else f"scim+{hashlib.sha256(external_id.encode()).hexdigest()[:16]}@invalid.workama.local"
                    await conn.execute(
                        "INSERT INTO id_user(id,email,password_hash,display_name,status,email_verified) VALUES (%s,%s,%s,%s,%s,TRUE)",
                        (user_id, email, hash_password(secrets.token_urlsafe(32)), display_name, "active" if active else "inactive"),
                    )
                    await conn.execute(
                        "INSERT INTO id_member(id,org_id,workspace_id,user_id,role) VALUES (%s,%s,%s,%s,'member')",
                        (new_id("mem"), org_id, workspace_id, user_id),
                    )
                    await conn.execute(
                        "INSERT INTO id_federation_scim_user(id,org_id,workspace_id,external_id,user_id,user_name,display_name,active) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (new_id("scu"), org_id, workspace_id, external_id, user_id, user_name, display_name, active),
                    )
                    created += 1
                else:
                    # 更新变更用户
                    if local["display_name"] != display_name or bool(local["active"]) != active or local["user_name"] != user_name:
                        await conn.execute(
                            "UPDATE id_federation_scim_user SET display_name=%s,active=%s,user_name=%s,version=version+1,updated_at=now() WHERE id=%s",
                            (display_name, active, user_name, local["id"]),
                        )
                        await conn.execute(
                            "UPDATE id_user SET display_name=%s,status=%s,updated_at=now() WHERE id=%s",
                            (display_name, "active" if active else "inactive", local["user_id"]),
                        )
                        updated += 1

            # 禁用 IdP 中不存在的用户
            local_result = await conn.execute(
                "SELECT id,external_id FROM id_federation_scim_user WHERE workspace_id=%s AND active=TRUE",
                (workspace_id,),
            )
            local_users = await local_result.fetchall()
            for local_user in local_users:
                if local_user["external_id"] not in remote_external_ids:
                    await conn.execute(
                        "UPDATE id_federation_scim_user SET active=FALSE,version=version+1,updated_at=now() WHERE id=%s",
                        (local_user["id"],),
                    )
                    deactivated += 1

    return {"created": created, "updated": updated, "deactivated": deactivated, "total": len(remote_users)}


def _sync_run_view(row: dict[str, Any]) -> dict[str, Any]:
    """格式化 scim_sync_run 行为响应字典。"""
    return {
        "id": row.get("id"),
        "workspace_id": row.get("workspace_id"),
        "provider_id": row.get("provider_id"),
        "status": row.get("status"),
        "created_users": int(row.get("created_users", 0)),
        "updated_users": int(row.get("updated_users", 0)),
        "deactivated_users": int(row.get("deactivated_users", 0)),
        "total_users": int(row.get("total_users", 0)),
        "error_message": row.get("error_message"),
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
    }


@router.get("/providers/{provider_id}/authorize")
async def sso_authorize(
    provider_id: str,
    redirect_uri: str | None = Query(default=None, max_length=2048),
):
    """OIDC/SAML 登录跳转。

    OIDC：生成授权 URL 并 302 重定向到 IdP。
    SAML：生成 AuthnRequest，返回 HTML 表单自动提交。
    """
    config = await _get_provider_config(provider_id)

    # v7.171 修复：校验 redirect_uri 在 redirect_allowlist 内（前缀匹配），防止开放重定向。
    # OIDC 与 SAML 分支共用该校验；redirect_uri 为空时跳过（SAML 分支允许无 redirect_uri）。
    if redirect_uri:
        allowlist = _as_list(config.get("redirect_allowlist"))
        if not any(redirect_uri.startswith(prefix) for prefix in allowlist if prefix):
            raise HTTPException(status_code=400, detail="redirect_uri not allowed")

    if config["provider"] == OIDC:
        issuer = str(config.get("issuer") or "").rstrip("/")
        client_id = str(config.get("client_id") or "")
        if not issuer or not client_id:
            raise HTTPException(status_code=503, detail="OIDC provider is not configured")
        state = secrets.token_urlsafe(32)
        authorize_endpoint = str(config.get("authorization_endpoint") or f"{issuer}/authorize")
        # 存登录 session
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO id_federation_sso_login_session(id,workspace_id,provider_id,token,expires_at) VALUES (%s,%s,%s,%s,%s)",
                (new_id("ssos"), config["workspace_id"], config["id"], state, datetime.now(UTC) + timedelta(minutes=10)),
            )
            await conn.commit()
        params = urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri or "",
            "scope": "openid profile email",
            "state": state,
        })
        return RedirectResponse(url=f"{authorize_endpoint}?{params}", status_code=302)

    # SAML：生成 AuthnRequest HTML 表单
    mapping = config.get("mapping") or {}
    sso_url = str(mapping.get("sso_url") or config.get("issuer") or "")
    if not sso_url:
        raise HTTPException(status_code=503, detail="SAML SSO URL is not configured")
    request_id = "_" + secrets.token_urlsafe(16)
    issue_instant = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    entity_id = str(config.get("issuer") or "")
    acs_url = redirect_uri or ""
    # v7.171 修复：对 sso_url/acs_url/entity_id 做 HTML 转义，防止配置值注入 HTML/XML。
    sso_url_esc = html.escape(sso_url, quote=True)
    acs_url_esc = html.escape(acs_url, quote=True)
    entity_id_esc = html.escape(entity_id, quote=True)
    authn_request = (
        f'<samlp:AuthnRequest xmlns:samlp="{SAML_PROTOCOL_NS}" '
        f'xmlns:saml="{SAML_ASSERTION_NS}" '
        f'ID="{request_id}" Version="2.0" IssueInstant="{issue_instant}" '
        f'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f'AssertionConsumerServiceURL="{acs_url_esc}">'
        f'<saml:Issuer>{entity_id_esc}</saml:Issuer>'
        f'</samlp:AuthnRequest>'
    )
    encoded = base64.b64encode(authn_request.encode()).decode()
    state = secrets.token_urlsafe(32)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO id_federation_sso_login_session(id,workspace_id,provider_id,token,expires_at) VALUES (%s,%s,%s,%s,%s)",
            (new_id("ssos"), config["workspace_id"], config["id"], state, datetime.now(UTC) + timedelta(minutes=10)),
        )
        await conn.commit()
    html_body = (
        f'<!DOCTYPE html><html><head><title>Redirecting to IdP</title></head>'
        f'<body onload="document.forms[0].submit()">'
        f'<form method="POST" action="{sso_url_esc}">'
        f'<input type="hidden" name="SAMLRequest" value="{encoded}"/>'
        f'<input type="hidden" name="RelayState" value="{state}"/>'
        f'<noscript><input type="submit" value="Continue"/></noscript>'
        f'</form></body></html>'
    )
    return Response(content=html_body, media_type="text/html")


@router.post("/providers/{provider_id}/acs")
async def sso_acs(
    provider_id: str,
    saml_response: Annotated[str, Form(alias="SAMLResponse", min_length=1, max_length=MAX_SAML_RESPONSE_BYTES)],
    relay_state: Annotated[str | None, Form(alias="RelayState", max_length=2048)] = None,
):
    """SAML Assertion Consumer Service。

    接收 SAML Response，验证 XML 签名后提取用户信息，
    创建/更新本地用户，签发平台 token。

    v7.172 修复：原先调用 ``_parse_saml_response_basic`` 不验证签名，
    可被伪造 SAML Response 冒充任意用户登录。现改为调用
    ``_validate_saml_response``（复用既有验签路径，与
    ``POST /sso-configs/{config_id}/saml/acs`` 一致），强制校验
    Response/Assertion 的 XMLDSIG 签名 + 证书 + issuer/audience/destination
    + NotBefore/NotOnOrAfter 时效。
    """
    config = await _get_provider_config(provider_id)
    if config["provider"] != SAML:
        raise HTTPException(status_code=409, detail="Provider is not SAML")
    try:
        parsed = _validate_saml_response(
            _decode_saml_response_form(saml_response), config=config
        )
    except SamlValidationError as exc:
        raise HTTPException(
            status_code=401, detail=f"SAML response validation failed: {exc}"
        ) from exc

    mapping = config.get("mapping") or {}
    email = parsed.get("email", "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="SAML response missing email attribute")

    display_name = parsed["attributes"].get("displayName") or parsed["attributes"].get("name") or email

    async with pool.connection() as conn:
        async with conn.transaction():
            # v7.171 修复：防重放检查——INSERT id_federation_saml_replay，
            # 若 (config_id, response_id) 已存在（UniqueViolation/ON CONFLICT DO NOTHING
            # 返回 0 行）则抛 409，阻止同一 SAML Response 被重复消费签发 token。
            replay = await conn.execute(
                """
                INSERT INTO id_federation_saml_replay(id,config_id,workspace_id,response_id,expires_at)
                VALUES (%s,%s,%s,%s,now()+interval '5 minutes')
                ON CONFLICT (config_id,response_id) DO NOTHING
                RETURNING id
                """,
                (new_id("sreplay"), config["id"], config["workspace_id"], parsed["response_id"]),
            )
            if not await replay.fetchone():
                raise HTTPException(status_code=409, detail="SAML response replay detected")
            user, created = await _create_or_update_user_from_sso(
                conn, email, display_name, provider_id, config["workspace_id"], config["org_id"],
            )
            if relay_state:
                await conn.execute(
                    "UPDATE id_federation_sso_login_session SET user_id=%s WHERE token=%s AND provider_id=%s",
                    (user["id"], relay_state, provider_id),
                )

    token = create_access_token(user["id"], user["workspace_id"], user["role"], auth_strength=2)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user["id"],
            "email": user["email"],
            "display_name": user["display_name"],
            "workspace_id": user["workspace_id"],
            "org_id": user["org_id"],
            "role": user["role"],
        },
        "sso": {"provider": SAML, "subject": parsed["sub"], "created": created},
    }


@router.get("/providers/{provider_id}/callback")
async def sso_oidc_callback(
    provider_id: str,
    code: str | None = Query(default=None, max_length=4096),
    state: str | None = Query(default=None, max_length=256),
    error: str | None = Query(default=None, max_length=128),
):
    """OIDC Callback。

    接收 code，换 access_token，获取 userinfo，
    创建/更新本地用户，签发平台 token。
    """
    config = await _get_provider_config(provider_id)
    if config["provider"] != OIDC:
        raise HTTPException(status_code=409, detail="Provider is not OIDC")
    if error:
        raise HTTPException(status_code=400, detail=f"OIDC provider error: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="OIDC callback code is missing")
    if not state:
        raise HTTPException(status_code=400, detail="OIDC state is missing")

    # 验证 session
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM id_federation_sso_login_session WHERE token=%s AND provider_id=%s AND expires_at > now() AND consumed_at IS NULL",
            (state, provider_id),
        )
        session = await result.fetchone()
        if not session:
            raise HTTPException(status_code=400, detail="OIDC state is invalid or expired")

    # 换 access_token
    try:
        token_data = _exchange_oidc_code_basic(code, config)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="OIDC token exchange failed") from exc
    access_token = str(token_data.get("access_token") or "")
    if not access_token:
        raise HTTPException(status_code=502, detail="OIDC token response missing access_token")

    # 获取 userinfo
    issuer = str(config.get("issuer") or "").rstrip("/")
    userinfo_endpoint = str(config.get("userinfo_endpoint") or f"{issuer}/userinfo")
    try:
        userinfo = _fetch_oidc_userinfo(access_token, userinfo_endpoint)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="OIDC userinfo fetch failed") from exc

    mapping = config.get("mapping") or {}
    email_claim = str(mapping.get("email_claim") or "email")
    email = str(userinfo.get(email_claim) or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="OIDC userinfo missing email")

    display_name = str(userinfo.get("name") or userinfo.get("preferred_username") or email)

    async with pool.connection() as conn:
        async with conn.transaction():
            user, created = await _create_or_update_user_from_sso(
                conn, email, display_name, provider_id, config["workspace_id"], config["org_id"],
            )
            await conn.execute(
                "UPDATE id_federation_sso_login_session SET user_id=%s, consumed_at=now() WHERE token=%s AND provider_id=%s",
                (user["id"], state, provider_id),
            )

    token = create_access_token(user["id"], user["workspace_id"], user["role"], auth_strength=2)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user["id"],
            "email": user["email"],
            "display_name": user["display_name"],
            "workspace_id": user["workspace_id"],
            "org_id": user["org_id"],
            "role": user["role"],
        },
        "sso": {"provider": OIDC, "subject": str(userinfo.get("sub", "")), "created": created},
    }


@router.post("/providers/{provider_id}/test")
async def sso_test_connection(provider_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    """SSO 连接测试。

    验证 provider 配置是否正确，返回测试结果，不实际登录。
    OIDC：尝试 GET {issuer}/.well-known/openid-configuration 验证可达。
    SAML：验证证书格式和 endpoint 可达性。
    """
    _require_capability(actor, "read")
    async with pool.connection() as conn:
        row = await _get_sso(conn, provider_id, actor.workspace_id)
    if row["provider"] == OIDC:
        return _test_oidc_connection(row)
    return _test_saml_connection(row)


@router.post("/providers/{provider_id}/sync")
async def scim_sync(provider_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    """SCIM 同步执行。

    从 IdP 拉取用户列表，与本地用户比对，
    创建/更新/禁用差异用户，返回同步报告。
    """
    _require_capability(actor, "write")
    async with pool.connection() as conn:
        config = await _get_sso(conn, provider_id, actor.workspace_id)
        run_id = new_id("sync")
        await conn.execute(
            "INSERT INTO id_federation_scim_sync_run(id,workspace_id,provider_id,status,started_at) VALUES (%s,%s,%s,'running',now())",
            (run_id, actor.workspace_id, provider_id),
        )
        await conn.commit()

    try:
        report = await _sync_scim_users(config, actor.workspace_id, actor.org_id)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE id_federation_scim_sync_run SET status='completed',created_users=%s,updated_users=%s,deactivated_users=%s,total_users=%s,completed_at=now() WHERE id=%s",
                (report["created"], report["updated"], report["deactivated"], report["total"], run_id),
            )
            await conn.commit()
        return {"run_id": run_id, "status": "completed", **report}
    except Exception as exc:
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE id_federation_scim_sync_run SET status='failed',error_message=%s,completed_at=now() WHERE id=%s",
                (str(exc), run_id),
            )
            await conn.commit()
        raise HTTPException(status_code=502, detail="SCIM sync failed") from exc


@router.get("/providers/{provider_id}/sync-history")
async def scim_sync_history(
    provider_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
):
    """SCIM 同步历史（分页查询同步执行记录）。"""
    _require_capability(actor, "read")
    async with pool.connection() as conn:
        await _workspace_for_actor(conn, actor)
        result = await conn.execute(
            "SELECT * FROM id_federation_scim_sync_run WHERE workspace_id=%s AND provider_id=%s ORDER BY started_at DESC LIMIT %s OFFSET %s",
            (actor.workspace_id, provider_id, limit, offset),
        )
        rows = await result.fetchall()
        total_result = await conn.execute(
            "SELECT count(*) AS total FROM id_federation_scim_sync_run WHERE workspace_id=%s AND provider_id=%s",
            (actor.workspace_id, provider_id),
        )
        total = int((await total_result.fetchone())["total"])
    return {
        "items": [_sync_run_view(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


__all__ = [
    "SCHEMA_STATEMENTS",
    "SCIM_TOKEN_PREFIX",
    "SsoConfigCreate",
    "SsoConfigPatch",
    "SamlValidationError",
    "OidcAuthorizationStart",
    "ScimTokenCreate",
    "ScimUserCreate",
    "ScimGroupCreate",
    "ScimPatchRequest",
    "ScimGroupPatchRequest",
    "SsoLoginResult",
    "SsoTestResult",
    "ScimSyncReport",
    "ScimSyncHistoryItem",
    "ensure_identity_federation_schema",
    "generate_oidc_state_bundle",
    "nonce_digest",
    "parse_scim_filter",
    "router",
    "scim_router",
    "state_digest",
    "validate_external_url",
    "validate_oidc_state_record",
    "validate_redirect_uri",
    "validate_saml_entity_id",
    "normalize_saml_certificate",
    "validate_sso_values",
    "_parse_saml_response_basic",
    "_fetch_oidc_userinfo",
    "_exchange_oidc_code_basic",
    "_create_or_update_user_from_sso",
    "_sync_scim_users",
    "_test_oidc_connection",
    "_test_saml_connection",
    "_http_get_json",
    "_http_post_json",
    "_http_get_raw",
    "_get_provider_config",
    "_sync_run_view",
    "sso_authorize",
    "sso_acs",
    "sso_oidc_callback",
    "sso_test_connection",
    "scim_sync",
    "scim_sync_history",
]
