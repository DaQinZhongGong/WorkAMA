from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
import httpx
from pydantic import BaseModel, Field, field_validator

from workama_platform.core import Actor, capability_allows, get_actor, hash_secret, json_dumps, new_id, pool
from workama_platform.modules.billing.metering import MeterRequest, settle_meter_in_transaction
from workama_platform.modules.security.service import validate_outbound_url, validate_resolved_outbound_url


router = APIRouter(prefix="/api/v1", tags=["external-apps", "marketplace"])

Provider = Literal["dify", "fastgpt", "ragflow"]
_TEMPLATE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_SAFE_REF = re.compile(r"^(?:mock|local)://[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}$")
_TEMPLATE_TYPES = ("assistant", "workflow", "skill")
_REVIEW_STATUSES = ("pending", "approved", "rejected")
_CONTROLLED_ENDPOINT = re.compile(
    r"^(?:mock|local)://(?:dify|fastgpt|ragflow)(?:/[A-Za-z0-9._:/@-]{0,240})?$"
)
_HTTP_TEST_MODE = "http_test"
_EXTERNAL_HTTP_MODE = "external_http"
_KNOWN_PROVIDERS = frozenset({"dify", "fastgpt", "ragflow"})
_HTTP_DEFAULT_TIMEOUT_SECONDS = 5.0
_HTTP_MAX_TIMEOUT_SECONDS = 15.0
_HTTP_DEFAULT_RETRIES = 1
_HTTP_MAX_RETRIES = 2
_HTTP_DEFAULT_BACKOFF_MS = 50
_HTTP_MAX_BACKOFF_MS = 500
_HTTP_MAX_RESPONSE_BYTES = 64_000
_HTTP_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_EXTERNAL_HTTP_LEASE_SECONDS = 30
_EXTERNAL_HTTP_LIVE_MAX_RETRIES = 2
_EXTERNAL_HTTP_LIVE_TIMEOUT_SECONDS = 10.0
_SENSITIVE_KEY_TERMS = ("secret", "password", "token", "api_key", "private_key", "authorization")


def _require(actor: Actor, capability: str) -> None:
    if not capability_allows(actor.capabilities, capability):
        raise HTTPException(status_code=403, detail=f"Missing capability: {capability}")


def _safe_json(value: dict[str, Any], *, max_bytes: int = 200_000) -> dict[str, Any]:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > max_bytes:
        raise ValueError("manifest/config is too large")

    def walk(item: Any, path: str = "manifest") -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = str(key).lower().replace("-", "_")
                if any(term in normalized for term in ("secret", "password", "token", "api_key", "private_key")):
                    raise ValueError(f"secret-like field at {path}.{key} is not allowed")
                walk(child, f"{path}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                walk(child, f"{path}[{index}]")
        elif isinstance(item, str):
            if _SAFE_REF.fullmatch(item) or item.startswith("https://") or item.startswith("http://"):
                if not _SAFE_REF.fullmatch(item):
                    raise ValueError(f"external URL at {path} is not allowed in local manifest")
            if "bearer " in item.lower() or "authorization:" in item.lower():
                raise ValueError(f"credential-like value at {path} is not allowed")

    walk(value)
    return value


class ExternalAppCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    provider: Provider
    endpoint: str = Field(min_length=1, max_length=2048)
    credential: str | None = Field(default=None, min_length=1, max_length=4096)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        value = value.strip()
        if _CONTROLLED_ENDPOINT.fullmatch(value):
            return value
        result = validate_outbound_url(value)
        if not result.allowed:
            raise ValueError(f"endpoint is unsafe: {result.reason}")
        return value

    @field_validator("config")
    @classmethod
    def validate_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        value = _safe_json(value, max_bytes=32_000)
        _http_execution_config(value)
        return value


class ExternalAppPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    endpoint: str | None = Field(default=None, max_length=2048)
    config: dict[str, Any] | None = None
    enabled: bool | None = None
    status: Literal["active", "disabled", "revoked"] | None = None

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if _CONTROLLED_ENDPOINT.fullmatch(value):
            return value
        result = validate_outbound_url(value)
        if not result.allowed:
            raise ValueError(f"endpoint is unsafe: {result.reason}")
        return value

    @field_validator("config")
    @classmethod
    def validate_config(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        value = _safe_json(value, max_bytes=32_000)
        _http_execution_config(value)
        return value


class ExternalInvocationCreate(BaseModel):
    operation: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _safe_json(value, max_bytes=100_000)


class TemplateCreate(BaseModel):
    name: str = Field(min_length=2, max_length=64, pattern=_TEMPLATE_NAME.pattern)
    display_name: str = Field(min_length=2, max_length=160)
    template_type: Literal["assistant", "workflow", "skill"]
    version: str = Field(min_length=1, max_length=64)
    manifest: dict[str, Any] = Field(default_factory=dict)
    artifact_ref: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=2_000)

    @field_validator("artifact_ref")
    @classmethod
    def validate_artifact_ref(cls, value: str) -> str:
        value = value.strip()
        if not _SAFE_REF.fullmatch(value):
            raise ValueError("artifact_ref must be a controlled mock:// or local:// reference")
        return value

    @field_validator("manifest")
    @classmethod
    def validate_manifest(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _safe_json(value)


class TemplateReview(BaseModel):
    review_status: Literal["approved", "rejected"]
    reason: str = Field(min_length=1, max_length=1_000)


class TemplateCopy(BaseModel):
    target_workspace_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=160)


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS pf_external_app (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      name TEXT NOT NULL,
      provider TEXT NOT NULL CHECK (provider IN ('dify','fastgpt','ragflow')),
      endpoint TEXT NOT NULL,
      credential_hash TEXT,
      credential_last4 TEXT,
      config JSONB NOT NULL DEFAULT '{}'::jsonb,
      status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','revoked')),
      enabled BOOLEAN NOT NULL DEFAULT TRUE,
      version INTEGER NOT NULL DEFAULT 1,
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id,name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pf_external_app_workspace ON pf_external_app(workspace_id,status,updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS pf_external_app_invocation (
      id TEXT PRIMARY KEY,
      app_id TEXT NOT NULL REFERENCES pf_external_app(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      operation TEXT NOT NULL,
      idempotency_key TEXT NOT NULL,
      input_hash TEXT NOT NULL,
      payload JSONB NOT NULL DEFAULT '{}'::jsonb,
      status TEXT NOT NULL DEFAULT 'pending_external' CHECK (status IN ('queued','running','succeeded','failed','pending_external')),
      execution_mode TEXT NOT NULL DEFAULT 'external_pending' CHECK (execution_mode IN ('controlled_mock','http_test','external_http','external_pending')),
      result JSONB NOT NULL DEFAULT '{}'::jsonb,
      error_code TEXT,
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      completed_at TIMESTAMPTZ,
      next_attempt_at TIMESTAMPTZ,
      claimed_at TIMESTAMPTZ,
      lease_owner TEXT,
      lease_expires_at TIMESTAMPTZ,
      UNIQUE(app_id,idempotency_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pf_external_app_invocation_workspace ON pf_external_app_invocation(workspace_id,created_at DESC)",
    "ALTER TABLE pf_external_app_invocation ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE pf_external_app_invocation ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE pf_external_app_invocation ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ",
    "ALTER TABLE pf_external_app_invocation ADD COLUMN IF NOT EXISTS response_code INTEGER",
    "ALTER TABLE pf_external_app_invocation ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE pf_external_app_invocation ADD COLUMN IF NOT EXISTS execution_mode TEXT NOT NULL DEFAULT 'external_pending'",
    "ALTER TABLE pf_external_app_invocation ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ",
    "ALTER TABLE pf_external_app_invocation ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ",
    "ALTER TABLE pf_external_app_invocation ADD COLUMN IF NOT EXISTS lease_owner TEXT",
    "ALTER TABLE pf_external_app_invocation ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ",
    "ALTER TABLE pf_external_app_invocation DROP CONSTRAINT IF EXISTS pf_external_app_invocation_execution_mode_check",
    "ALTER TABLE pf_external_app_invocation ADD CONSTRAINT pf_external_app_invocation_execution_mode_check CHECK (execution_mode IN ('controlled_mock','http_test','external_http','external_pending'))",
    """UPDATE pf_external_app_invocation i
       SET execution_mode = CASE
         WHEN a.endpoint ~ '^(mock|local)://(dify|fastgpt|ragflow)(/|$)' THEN 'controlled_mock'
         WHEN a.endpoint ~ '^https?://' AND a.config->>'execution_mode' IN ('http_test','external_http') THEN a.config->>'execution_mode'
         ELSE 'external_pending'
       END
       FROM pf_external_app a
       WHERE a.id=i.app_id AND i.execution_mode='external_pending'""",
    "CREATE INDEX IF NOT EXISTS idx_pf_external_app_invocation_delivery ON pf_external_app_invocation(execution_mode,status,next_attempt_at,lease_expires_at,created_at)",
    """
    CREATE TABLE IF NOT EXISTS pf_marketplace_template (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      name TEXT NOT NULL,
      display_name TEXT NOT NULL,
      template_type TEXT NOT NULL CHECK (template_type IN ('assistant','workflow','skill')),
      version TEXT NOT NULL,
      description TEXT NOT NULL DEFAULT '',
      manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
      artifact_ref TEXT NOT NULL,
      review_status TEXT NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending','approved','rejected')),
      visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private','public')),
      status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','archived')),
      created_by TEXT NOT NULL REFERENCES id_user(id),
      reviewed_by TEXT REFERENCES id_user(id),
      reviewed_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id,name,version)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pf_marketplace_template_public ON pf_marketplace_template(status,review_status,template_type,updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS pf_marketplace_copy (
      id TEXT PRIMARY KEY,
      template_id TEXT NOT NULL REFERENCES pf_marketplace_template(id) ON DELETE CASCADE,
      source_workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      target_workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      idempotency_key TEXT NOT NULL,
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(template_id,target_workspace_id,idempotency_key)
    )
    """,
)


async def ensure_external_apps_schema(conn) -> None:
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


def _app_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"], "name": row["name"], "provider": row["provider"], "endpoint": _mask_endpoint(row["endpoint"]),
        "credential_configured": bool(row.get("credential_hash")), "credential_last4": row.get("credential_last4"),
        "config": row["config"], "status": row["status"], "enabled": row["enabled"], "version": row["version"],
        "execution_mode": _execution_mode(row["endpoint"], row.get("config")),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _mask_endpoint(endpoint: str) -> str:
    from urllib.parse import urlsplit
    parsed = urlsplit(endpoint)
    return f"{parsed.scheme}://{parsed.hostname}{parsed.path or '/'}"


def _http_execution_config(config: dict[str, Any] | None) -> dict[str, Any]:
    config = config or {}
    mode = config.get("execution_mode")
    if mode is None:
        return {
            "enabled": False,
            "timeout_seconds": _HTTP_DEFAULT_TIMEOUT_SECONDS,
            "max_retries": _HTTP_DEFAULT_RETRIES,
            "backoff_ms": _HTTP_DEFAULT_BACKOFF_MS,
        }
    if mode not in {_HTTP_TEST_MODE, _EXTERNAL_HTTP_MODE}:
        raise ValueError("config.execution_mode must be http_test or external_http when provided")
    forbidden = {"headers", "request_headers", "authorization", "credential", "credentials"}
    if forbidden.intersection(config):
        raise ValueError("http_test does not accept credential or custom header fields")
    timeout = config.get("timeout_seconds", _HTTP_DEFAULT_TIMEOUT_SECONDS)
    retries = config.get("max_retries", _HTTP_DEFAULT_RETRIES)
    backoff = config.get("backoff_ms", _HTTP_DEFAULT_BACKOFF_MS)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.05 <= float(timeout) <= _HTTP_MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be between 0.05 and {_HTTP_MAX_TIMEOUT_SECONDS:g}")
    if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= _HTTP_MAX_RETRIES:
        raise ValueError(f"max_retries must be between 0 and {_HTTP_MAX_RETRIES}")
    if isinstance(backoff, bool) or not isinstance(backoff, int) or not 0 <= backoff <= _HTTP_MAX_BACKOFF_MS:
        raise ValueError(f"backoff_ms must be between 0 and {_HTTP_MAX_BACKOFF_MS}")
    return {
        "enabled": True,
        "execution_mode": mode,
        "timeout_seconds": float(timeout),
        "max_retries": retries,
        "backoff_ms": backoff,
    }


def _execution_mode(endpoint: str, config: dict[str, Any] | None = None) -> str:
    if _CONTROLLED_ENDPOINT.fullmatch(endpoint):
        return "controlled_mock"
    if endpoint.startswith(("http://", "https://")):
        execution_config = _http_execution_config(config)
        if execution_config.get("enabled"):
            return str(execution_config["execution_mode"])
    return "external_pending"


def external_http_block_reason(app: dict[str, Any]) -> str | None:
    """Return a fail-closed reason before a real provider invocation is queued."""
    if app.get("provider") not in _KNOWN_PROVIDERS:
        return "unknown_provider"
    if _execution_mode(str(app.get("endpoint") or ""), app.get("config")) != _EXTERNAL_HTTP_MODE:
        return "external_http_not_enabled"
    if not app.get("credential_hash"):
        return "staging_credential_required"
    return None


def external_http_retry_delay(attempt: int, config: dict[str, Any] | None) -> float:
    """Return a bounded queue delay after a retryable provider response."""
    execution_config = _http_execution_config(config)
    base = execution_config["backoff_ms"] / 1000
    return min(_HTTP_MAX_BACKOFF_MS / 1000, base * (2 ** max(0, attempt - 1)))


def _controlled_execution(
    provider: str,
    operation: str,
    payload: dict[str, Any],
    input_hash: str,
) -> dict[str, Any]:
    """Return a deterministic local result without contacting the configured endpoint."""
    message = payload.get("message")
    if not isinstance(message, str):
        message = payload.get("query") if isinstance(payload.get("query"), str) else "completed"
    output_text = f"mock:{provider}:{operation}:{message[:256]}"
    return {
        "provider": provider,
        "operation": operation,
        "execution": "controlled_mock",
        "input_hash": input_hash,
        "output_text": output_text,
        "provider_request_sent": False,
    }


def _estimate_tokens(value: Any) -> int:
    encoded = json_dumps(value).encode()
    return max(1, (len(encoded) + 3) // 4)


def _redact_provider_value(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(term in normalized for term in _SENSITIVE_KEY_TERMS):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = _redact_provider_value(child, depth + 1)
        return redacted
    if isinstance(value, list):
        return [_redact_provider_value(child, depth + 1) for child in value[:256]]
    if isinstance(value, str) and "bearer " in value.lower():
        return "[REDACTED]"
    return value


def _provider_response_body(response: httpx.Response) -> Any:
    raw = response.content[:_HTTP_MAX_RESPONSE_BYTES]
    truncated = len(response.content) > _HTTP_MAX_RESPONSE_BYTES
    return _provider_response_body_bytes(raw, truncated=truncated)


def _provider_response_body_bytes(raw: bytes, *, truncated: bool = False) -> Any:
    text = raw.decode("utf-8", errors="replace")
    try:
        value: Any = json.loads(text)
    except json.JSONDecodeError:
        value = text
    value = _redact_provider_value(value)
    if truncated:
        return {"body": value, "truncated": True}
    return value


async def _audit_external_app_call(
    workspace_id: str,
    actor_id: str | None,
    invocation_id: str | None,
    app_id: str | None,
    action: str,
    endpoint: str,
    request_payload: dict[str, Any],
    response_summary: dict[str, Any],
    status_code: int | None,
    error_code: str | None,
    attempt: int,
) -> None:
    """Write a redacted audit log entry for an external app call."""
    try:
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO pf_external_app_audit_log(
                    id, invocation_id, app_id, workspace_id, actor_id, action, endpoint,
                    request_payload, response_summary, status_code, error_code, attempt
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                """,
                (
                    new_id("exau"),
                    invocation_id,
                    app_id,
                    workspace_id,
                    actor_id,
                    action,
                    endpoint,
                    json_dumps(_redact_provider_value(request_payload)),
                    json_dumps(_redact_provider_value(response_summary)),
                    status_code,
                    error_code,
                    attempt,
                ),
            )
            await conn.commit()
    except Exception:
        pass


def _external_http_failure(
    *,
    attempts: int,
    response_code: int | None,
    error_code: str,
    provider_request_sent: bool,
) -> dict[str, Any]:
    return {
        "success": False,
        "attempts": attempts,
        "response_code": response_code,
        "error_code": error_code,
        "retryable": bool(response_code == 429 or response_code is not None and 500 <= response_code <= 599),
        "result": {
            "execution": _EXTERNAL_HTTP_MODE,
            "provider_request_sent": provider_request_sent,
            "attempts": attempts,
            "http_status": response_code,
            "error_code": error_code,
        },
    }


async def external_http_execution(
    provider: str,
    endpoint: str,
    operation: str,
    payload: dict[str, Any],
    input_hash: str,
    config: dict[str, Any] | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    workspace_id: str | None = None,
    actor_id: str | None = None,
    invocation_id: str | None = None,
) -> dict[str, Any]:
    """Execute external HTTP delivery with bounded retries and audit logging.

    Credentials are intentionally not accepted here.  The worker receives only
    the credential hash as a queue gate, so this function cannot accidentally
    put a provider secret into a request or persisted result.
    """
    if provider not in _KNOWN_PROVIDERS:
        return _external_http_failure(
            attempts=0,
            response_code=None,
            error_code="unknown_provider",
            provider_request_sent=False,
        )
    try:
        execution_config = _http_execution_config(config)
    except ValueError:
        return _external_http_failure(
            attempts=0,
            response_code=None,
            error_code="invalid_execution_config",
            provider_request_sent=False,
        )
    if execution_config.get("execution_mode") != _EXTERNAL_HTTP_MODE:
        return _external_http_failure(
            attempts=0,
            response_code=None,
            error_code="external_http_not_enabled",
            provider_request_sent=False,
        )
    try:
        validation = await validate_resolved_outbound_url(endpoint)
    except Exception:
        validation = None
    if validation is None or not validation.allowed:
        return _external_http_failure(
            attempts=0,
            response_code=None,
            error_code="unsafe_endpoint",
            provider_request_sent=False,
        )

    request_body = {
        "provider": provider,
        "operation": operation,
        "payload": payload,
        "request_hash": input_hash,
    }
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "user-agent": "WorkAMA-ExternalApp/1",
        "x-workama-execution-mode": _EXTERNAL_HTTP_MODE,
        "idempotency-key": input_hash,
    }
    timeout_seconds = min(_EXTERNAL_HTTP_LIVE_TIMEOUT_SECONDS, execution_config["timeout_seconds"])
    timeout = httpx.Timeout(timeout_seconds)
    max_attempts = _EXTERNAL_HTTP_LIVE_MAX_RETRIES + 1
    attempts = 0
    last_response_code: int | None = None
    last_error_code = "provider_network_error"
    provider_request_sent = False
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=transport) as client:
        while attempts < max_attempts:
            attempts += 1
            try:
                async with client.stream("POST", endpoint, json=request_body, headers=headers) as response:
                    provider_request_sent = True
                    last_response_code = response.status_code
                    if not 200 <= response.status_code < 300:
                        last_error_code = f"provider_http_{response.status_code}"
                        if response.status_code not in _HTTP_RETRYABLE_STATUS_CODES:
                            break
                        continue
                    declared_size = response.headers.get("content-length")
                    if declared_size and declared_size.isdigit() and int(declared_size) > _HTTP_MAX_RESPONSE_BYTES:
                        last_error_code = "response_too_large"
                        break
                    chunks: list[bytes] = []
                    response_bytes = 0
                    async for chunk in response.aiter_bytes():
                        response_bytes += len(chunk)
                        if response_bytes > _HTTP_MAX_RESPONSE_BYTES:
                            last_error_code = "response_too_large"
                            break
                        chunks.append(chunk)
                    if last_error_code == "response_too_large":
                        break
                    result_body = _provider_response_body_bytes(b"".join(chunks))
                    outcome = {
                        "success": True,
                        "attempts": attempts,
                        "response_code": response.status_code,
                        "error_code": None,
                        "retryable": False,
                        "result": {
                            "provider": provider,
                            "operation": operation,
                            "execution": _EXTERNAL_HTTP_MODE,
                            "provider_request_sent": True,
                            "attempts": attempts,
                            "http_status": response.status_code,
                            "response": result_body,
                        },
                    }
                    if workspace_id:
                        await _audit_external_app_call(
                            workspace_id=workspace_id,
                            actor_id=actor_id,
                            invocation_id=invocation_id,
                            app_id=None,
                            action=operation,
                            endpoint=endpoint,
                            request_payload=request_body,
                            response_summary={"success": True, "status_code": response.status_code, "attempts": attempts},
                            status_code=response.status_code,
                            error_code=None,
                            attempt=attempts,
                        )
                    return outcome
            except httpx.TimeoutException:
                last_error_code = "provider_timeout"
            except httpx.RequestError:
                last_error_code = "provider_network_error"
            except httpx.HTTPError:
                last_error_code = "provider_protocol_error"
            if attempts < max_attempts:
                delay = min(_HTTP_MAX_BACKOFF_MS / 1000, (execution_config["backoff_ms"] / 1000) * (2 ** max(0, attempts - 1)))
                await asyncio.sleep(delay)
    failure = _external_http_failure(
        attempts=attempts,
        response_code=last_response_code,
        error_code=last_error_code,
        provider_request_sent=provider_request_sent,
    )
    if workspace_id:
        await _audit_external_app_call(
            workspace_id=workspace_id,
            actor_id=actor_id,
            invocation_id=invocation_id,
            app_id=None,
            action=operation,
            endpoint=endpoint,
            request_payload=request_body,
            response_summary={"success": False, "status_code": last_response_code, "error_code": last_error_code, "attempts": attempts},
            status_code=last_response_code,
            error_code=last_error_code,
            attempt=attempts,
        )
    return failure




async def _http_test_execution(
    provider: str,
    endpoint: str,
    operation: str,
    payload: dict[str, Any],
    input_hash: str,
    config: dict[str, Any] | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Execute the explicitly opted-in HTTP test contract with bounded retries."""
    validation = validate_outbound_url(endpoint)
    if not validation.allowed:
        return {
            "success": False,
            "attempts": 0,
            "response_code": None,
            "error_code": "unsafe_endpoint",
            "result": {"execution": _HTTP_TEST_MODE, "provider_request_sent": False},
        }
    execution_config = _http_execution_config(config)
    max_attempts = execution_config["max_retries"] + 1
    request_body = {
        "provider": provider,
        "operation": operation,
        "payload": payload,
        "request_hash": input_hash,
    }
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "x-workama-execution-mode": _HTTP_TEST_MODE,
        "idempotency-key": input_hash,
    }
    attempts = 0
    last_response_code: int | None = None
    last_error_code = "provider_network_error"
    timeout = httpx.Timeout(execution_config["timeout_seconds"])
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=transport) as client:
        while attempts < max_attempts:
            attempts += 1
            try:
                response = await client.post(endpoint, json=request_body, headers=headers)
                last_response_code = response.status_code
                if 200 <= response.status_code < 300:
                    return {
                        "success": True,
                        "attempts": attempts,
                        "response_code": response.status_code,
                        "error_code": None,
                        "result": {
                            "provider": provider,
                            "operation": operation,
                            "execution": _HTTP_TEST_MODE,
                            "provider_request_sent": True,
                            "attempts": attempts,
                            "http_status": response.status_code,
                            "response": _provider_response_body(response),
                        },
                    }
                last_error_code = f"provider_http_{response.status_code}"
                if response.status_code not in _HTTP_RETRYABLE_STATUS_CODES:
                    break
            except httpx.TimeoutException:
                last_error_code = "provider_timeout"
            except httpx.RequestError:
                last_error_code = "provider_network_error"
            except httpx.HTTPError:
                last_error_code = "provider_protocol_error"
            if attempts < max_attempts and execution_config["backoff_ms"]:
                await asyncio.sleep(execution_config["backoff_ms"] / 1000)
    return {
        "success": False,
        "attempts": attempts,
        "response_code": last_response_code,
        "error_code": last_error_code,
        "result": {
            "provider": provider,
            "operation": operation,
            "execution": _HTTP_TEST_MODE,
            "provider_request_sent": attempts > 0,
            "attempts": attempts,
            "http_status": last_response_code,
            "error_code": last_error_code,
        },
    }


def _template_view(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in {"org_id", "workspace_id"}}


_INVOCATION_FIELDS = "id,app_id,operation,idempotency_key,input_hash,status,execution_mode,result,error_code,attempt,max_attempts,next_attempt_at,last_attempt_at,response_code,claimed_at,lease_expires_at,created_at,completed_at"


@router.get("/external-apps")
async def list_external_apps(actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "external_app:read")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,name,provider,endpoint,credential_hash,credential_last4,config,status,enabled,version,created_at,updated_at FROM pf_external_app WHERE workspace_id=%s ORDER BY updated_at DESC", (actor.workspace_id,))
        data = [_app_view(row) for row in await result.fetchall()]
    # Contract 720 listExternalApps ListQuery -> ListResponse<ExternalAppDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("/external-apps", status_code=201)
async def create_external_app(body: ExternalAppCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "external_app:create")
    app_id = new_id("extapp")
    async with pool.connection() as conn:
        try:
            result = await conn.execute("""INSERT INTO pf_external_app(id,org_id,workspace_id,name,provider,endpoint,credential_hash,credential_last4,config,enabled,created_by)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
                RETURNING id,name,provider,endpoint,credential_hash,credential_last4,config,status,enabled,version,created_at,updated_at""", (app_id, actor.org_id, actor.workspace_id, body.name, body.provider, body.endpoint, hash_secret(body.credential) if body.credential else None, body.credential[-4:] if body.credential else None, json_dumps(body.config), body.enabled, actor.user_id))
            row = await result.fetchone(); await conn.commit()
        except Exception as exc:
            await conn.rollback()
            if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
                raise HTTPException(status_code=409, detail="External app name already exists") from exc
            raise
    return _app_view(row)


@router.get("/external-apps/{app_id}")
async def get_external_app(app_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "external_app:read")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,name,provider,endpoint,credential_hash,credential_last4,config,status,enabled,version,created_at,updated_at FROM pf_external_app WHERE id=%s AND workspace_id=%s", (app_id, actor.workspace_id))
        row = await result.fetchone()
    if not row: raise HTTPException(status_code=404, detail="External app not found")
    return _app_view(row)


@router.patch("/external-apps/{app_id}")
async def patch_external_app(app_id: str, body: ExternalAppPatch, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "external_app:write")
    updates: list[str] = []; values: list[Any] = []
    for field in ("name", "endpoint", "config", "enabled", "status"):
        value = getattr(body, field)
        if value is not None:
            updates.append(f"{field}=%s" + ("::jsonb" if field == "config" else "")); values.append(json_dumps(value) if field == "config" else value)
    if not updates: return await get_external_app(app_id, actor)
    updates.extend(["version=version+1", "updated_at=now()"]); values.extend([app_id, actor.workspace_id])
    async with pool.connection() as conn:
        result = await conn.execute(f"UPDATE pf_external_app SET {', '.join(updates)} WHERE id=%s AND workspace_id=%s RETURNING id,name,provider,endpoint,credential_hash,credential_last4,config,status,enabled,version,created_at,updated_at", values)
        row = await result.fetchone(); await conn.commit()
    if not row: raise HTTPException(status_code=404, detail="External app not found")
    return _app_view(row)


@router.post("/external-apps/{app_id}/invocations", status_code=202)
async def invoke_external_app(app_id: str, body: ExternalInvocationCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "external_app:invoke")
    input_hash = hashlib.sha256(json_dumps({"operation": body.operation, "payload": body.payload}).encode()).hexdigest()
    replayed = False
    should_execute = False
    execution_mode = "external_pending"
    async with pool.connection() as conn:
        async with conn.transaction():
            app_result = await conn.execute(
                "SELECT id,provider,endpoint,config,credential_hash,status,enabled FROM pf_external_app WHERE id=%s AND workspace_id=%s",
                (app_id, actor.workspace_id),
            )
            app = await app_result.fetchone()
            if not app:
                raise HTTPException(status_code=404, detail="External app not found")
            if app["status"] != "active" or not app["enabled"]:
                raise HTTPException(status_code=409, detail="External app is not enabled")
            execution_mode = _execution_mode(app["endpoint"], app.get("config"))
            execution_config = _http_execution_config(app.get("config"))
            external_block_reason = (
                external_http_block_reason(app)
                if execution_mode == _EXTERNAL_HTTP_MODE
                else None
            )
            existing_result = await conn.execute(
                f"SELECT {_INVOCATION_FIELDS} FROM pf_external_app_invocation WHERE app_id=%s AND idempotency_key=%s FOR UPDATE",
                (app_id, body.idempotency_key),
            )
            existing = await existing_result.fetchone()
            if existing:
                if existing["input_hash"] != input_hash:
                    raise HTTPException(status_code=409, detail="Idempotency key was already used with a different request")
                row = existing
                execution_mode = existing.get("execution_mode") or execution_mode
                replayed = True
                if existing["status"] == "running":
                    last_attempt_at = existing.get("last_attempt_at")
                    stale_after = max(30.0, execution_config["timeout_seconds"] * 2)
                    is_stale = not last_attempt_at or (datetime.now(UTC) - last_attempt_at).total_seconds() >= stale_after
                    if is_stale and existing.get("attempt", 0) < existing.get("max_attempts", 1):
                        reclaimed = await conn.execute(
                            """UPDATE pf_external_app_invocation
                            SET last_attempt_at=now()
                            WHERE id=%s
                            RETURNING id,app_id,operation,idempotency_key,input_hash,status,result,error_code,attempt,max_attempts,last_attempt_at,response_code,created_at,completed_at""",
                            (existing["id"],),
                        )
                        row = await reclaimed.fetchone()
                        should_execute = execution_mode in {"controlled_mock", _HTTP_TEST_MODE}
            else:
                result = await conn.execute(
                    f"""INSERT INTO pf_external_app_invocation(
                      id,app_id,workspace_id,operation,idempotency_key,input_hash,payload,status,execution_mode,result,error_code,attempt,max_attempts,next_attempt_at,last_attempt_at,created_by
                    )
                    VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb,%s,0,%s,CASE WHEN %s THEN now() ELSE NULL END,CASE WHEN %s IN ('controlled_mock','http_test') THEN now() ELSE NULL END,%s)
                    ON CONFLICT(app_id,idempotency_key) DO NOTHING
                    RETURNING {_INVOCATION_FIELDS}""",
                    (new_id("extinv"), app_id, actor.workspace_id, body.operation, body.idempotency_key, input_hash,
                     json_dumps(body.payload),
                     "running" if execution_mode in {"controlled_mock", _HTTP_TEST_MODE} else (
                         "queued" if execution_mode == _EXTERNAL_HTTP_MODE and external_block_reason is None else "pending_external"
                     ),
                     execution_mode,
                     json_dumps(
                         {"execution": _EXTERNAL_HTTP_MODE, "provider_request_sent": False, "blocked": True}
                         if external_block_reason
                         else {}
                     ),
                     external_block_reason,
                     execution_config["max_retries"] + 1 if execution_mode in {_HTTP_TEST_MODE, _EXTERNAL_HTTP_MODE} else 1,
                     execution_mode == _EXTERNAL_HTTP_MODE and external_block_reason is None,
                     execution_mode, actor.user_id),
                )
                row = await result.fetchone()
                if row is not None:
                    should_execute = execution_mode in {"controlled_mock", _HTTP_TEST_MODE}
                if row is None:
                    replay_result = await conn.execute(
                        f"SELECT {_INVOCATION_FIELDS} FROM pf_external_app_invocation WHERE app_id=%s AND idempotency_key=%s FOR UPDATE",
                        (app_id, body.idempotency_key),
                    )
                    row = await replay_result.fetchone()
                    if not row:
                        raise HTTPException(status_code=409, detail="Unable to create idempotent invocation")
                    if row["input_hash"] != input_hash:
                        raise HTTPException(status_code=409, detail="Idempotency key was already used with a different request")
                    replayed = True
                    execution_mode = row.get("execution_mode") or execution_mode
                    if row["status"] == "running":
                        should_execute = execution_mode in {"controlled_mock", _HTTP_TEST_MODE}
    if should_execute:
        if execution_mode == "controlled_mock":
            controlled = _controlled_execution(app["provider"], body.operation, body.payload, input_hash)
            execution = {
                "success": True,
                "attempts": 1,
                "response_code": 200,
                "error_code": None,
                "result": controlled,
            }
        else:
            try:
                execution = await _http_test_execution(
                    app["provider"], app["endpoint"], body.operation, body.payload, input_hash, app.get("config")
                )
            except Exception:
                execution = {
                    "success": False,
                    "attempts": 0,
                    "response_code": None,
                    "error_code": "provider_internal_error",
                    "result": {"execution": _HTTP_TEST_MODE, "provider_request_sent": False, "error_code": "provider_internal_error"},
                }
        async with conn.transaction():
            if execution["success"]:
                result_value = execution["result"]
                await settle_meter_in_transaction(
                    conn,
                    MeterRequest(
                        request_id=row["id"],
                        workspace_id=actor.workspace_id,
                        model=f"external-app:{app['provider']}",
                        prompt_tokens=_estimate_tokens(body.payload),
                        completion_tokens=_estimate_tokens(result_value),
                        status_code=execution["response_code"] or 200,
                    ),
                )
                completed = await conn.execute(
                    f"""UPDATE pf_external_app_invocation
                    SET status='succeeded', result=%s::jsonb, error_code=NULL, attempt=%s, response_code=%s, last_attempt_at=now(), completed_at=now()
                    WHERE id=%s
                    RETURNING {_INVOCATION_FIELDS}""",
                    (json_dumps(result_value), execution["attempts"], execution["response_code"], row["id"]),
                )
            else:
                failed = await conn.execute(
                    f"""UPDATE pf_external_app_invocation
                    SET status='failed', result=%s::jsonb, error_code=%s, attempt=%s, response_code=%s, last_attempt_at=now(), completed_at=now()
                    WHERE id=%s
                    RETURNING {_INVOCATION_FIELDS}""",
                    (json_dumps(execution["result"]), execution["error_code"], execution["attempts"], execution["response_code"], row["id"]),
                )
            row = await (completed if execution["success"] else failed).fetchone()
    if execution_mode == "controlled_mock" and row["status"] == "succeeded":
        external_execution = "completed"
    elif execution_mode == _HTTP_TEST_MODE and row["status"] == "succeeded":
        external_execution = "completed"
    elif execution_mode == _EXTERNAL_HTTP_MODE and row["status"] == "queued":
        external_execution = "queued"
    elif execution_mode == _HTTP_TEST_MODE and row["status"] == "failed":
        external_execution = "failed"
    elif execution_mode == _EXTERNAL_HTTP_MODE and row["status"] == "failed":
        external_execution = "failed"
    elif row["status"] == "running":
        external_execution = "in_progress"
    elif execution_mode == _EXTERNAL_HTTP_MODE and row["status"] == "pending_external" and row.get("error_code"):
        external_execution = "blocked"
    else:
        external_execution = "pending"
    return {**row, "external_execution": external_execution, "execution_mode": execution_mode, "idempotency_replayed": replayed}


@router.get("/external-apps/{app_id}/invocations")
async def list_external_invocations(app_id: str, actor: Annotated[Actor, Depends(get_actor)], limit: int = Query(default=50, ge=1, le=100)):
    _require(actor, "external_app:read")
    async with pool.connection() as conn:
        result = await conn.execute(f"SELECT {_INVOCATION_FIELDS} FROM pf_external_app_invocation WHERE app_id=%s AND workspace_id=%s ORDER BY created_at DESC LIMIT %s", (app_id, actor.workspace_id, limit))
        data = await result.fetchall()
    # Contract 720 listExternalAppInvocations ListQuery -> ListResponse<ExternalAppInvocationDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.delete("/external-apps/{app_id}", status_code=204)
async def delete_external_app(app_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "external_app:delete")
    async with pool.connection() as conn:
        result = await conn.execute(
            "UPDATE pf_external_app SET status='revoked',enabled=FALSE,version=version+1,updated_at=now() WHERE id=%s AND workspace_id=%s",
            (app_id, actor.workspace_id),
        )
        await conn.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="External app not found")
    return Response(status_code=204)


@router.post("/external-apps/{app_id}/tests")
async def test_external_app(app_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "external_app:write")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT id,provider,endpoint,config,credential_hash,status,enabled FROM pf_external_app WHERE id=%s AND workspace_id=%s",
            (app_id, actor.workspace_id),
        )
        app = await result.fetchone()
    if not app:
        raise HTTPException(status_code=404, detail="External app not found")
    if app["status"] != "active":
        raise HTTPException(status_code=409, detail="External app is not active")
    execution_mode = _execution_mode(app["endpoint"], app.get("config"))
    input_hash = hashlib.sha256(json_dumps({"operation": "test", "payload": {}}).encode()).hexdigest()
    if execution_mode == "controlled_mock":
        result_value = _controlled_execution(app["provider"], "test", {}, input_hash)
        return {"app_id": app_id, "execution_mode": execution_mode, "success": True, "result": result_value}
    if execution_mode == _HTTP_TEST_MODE:
        try:
            execution = await _http_test_execution(app["provider"], app["endpoint"], "test", {}, input_hash, app.get("config"))
        except Exception:
            execution = {
                "success": False,
                "response_code": None,
                "error_code": "provider_internal_error",
                "result": {"execution": _HTTP_TEST_MODE, "provider_request_sent": False, "error_code": "provider_internal_error"},
            }
        return {"app_id": app_id, "execution_mode": execution_mode, **execution}
    return {"app_id": app_id, "execution_mode": execution_mode, "success": True, "result": {"execution": execution_mode, "provider_request_sent": False}}




@router.get("/external-apps/{app_id}/health")
async def get_external_app_health(app_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "external_app:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT id,name,provider,endpoint,config,status,enabled FROM pf_external_app WHERE id=%s AND workspace_id=%s",
            (app_id, actor.workspace_id),
        )
        app = await result.fetchone()
    if not app:
        raise HTTPException(status_code=404, detail="External app not found")
    execution_mode = _execution_mode(app["endpoint"], app.get("config"))
    if execution_mode == "controlled_mock":
        return {"app_id": app_id, "status": "healthy", "execution_mode": execution_mode, "reachable": True}
    if execution_mode != _EXTERNAL_HTTP_MODE:
        return {"app_id": app_id, "status": "unknown", "execution_mode": execution_mode, "reachable": None}
    try:
        validation = await validate_resolved_outbound_url(app["endpoint"])
        reachable = validation is not None and validation.allowed
    except Exception:
        reachable = False
    return {
        "app_id": app_id,
        "status": "healthy" if reachable else "unhealthy",
        "execution_mode": execution_mode,
        "reachable": reachable,
        "endpoint": _mask_endpoint(app["endpoint"]),
    }

@router.get("/marketplace/templates")
async def list_templates(actor: Annotated[Actor, Depends(get_actor)], template_type: str | None = Query(default=None)):
    _require(actor, "marketplace:read")
    params: list[Any] = []
    where = "(visibility='public' AND status='published') OR workspace_id=%s"
    params.append(actor.workspace_id)
    if template_type:
        where = f"({where}) AND template_type=%s"; params.append(template_type)
    async with pool.connection() as conn:
        result = await conn.execute(f"SELECT id,name,display_name,template_type,version,description,manifest,artifact_ref,review_status,visibility,status,created_by,reviewed_at,created_at,updated_at FROM pf_marketplace_template WHERE {where} ORDER BY updated_at DESC", tuple(params))
        data = [_template_view(row) for row in await result.fetchall()]
    # Contract 720 listMarketplaceTemplates ListQuery -> ListResponse<MarketplaceTemplateDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("/marketplace/templates", status_code=201)
async def create_template(body: TemplateCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "marketplace:create")
    async with pool.connection() as conn:
        result = await conn.execute("""INSERT INTO pf_marketplace_template(id,org_id,workspace_id,name,display_name,template_type,version,description,manifest,artifact_ref,created_by)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            RETURNING id,name,display_name,template_type,version,description,manifest,artifact_ref,review_status,visibility,status,created_by,reviewed_at,created_at,updated_at""", (new_id("tpl"), actor.org_id, actor.workspace_id, body.name, body.display_name, body.template_type, body.version, body.description, json_dumps(body.manifest), body.artifact_ref, actor.user_id))
        row = await result.fetchone(); await conn.commit()
    return _template_view(row)


@router.post("/marketplace/templates/{template_id}/reviews")
async def review_template(template_id: str, body: TemplateReview, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "marketplace:review")
    async with pool.connection() as conn:
        result = await conn.execute("UPDATE pf_marketplace_template SET review_status=%s,reviewed_by=%s,reviewed_at=now(),updated_at=now() WHERE id=%s AND (workspace_id=%s OR visibility='public') RETURNING id,review_status,reviewed_at", (body.review_status, actor.user_id, template_id, actor.workspace_id))
        row = await result.fetchone(); await conn.commit()
    if not row: raise HTTPException(status_code=404, detail="Template not found")
    return row


@router.post("/marketplace/templates/{template_id}/publish")
async def publish_template(template_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "marketplace:publish")
    async with pool.connection() as conn:
        result = await conn.execute("UPDATE pf_marketplace_template SET visibility='public',status='published',updated_at=now() WHERE id=%s AND workspace_id=%s AND review_status='approved' RETURNING id,status,visibility,review_status,updated_at", (template_id, actor.workspace_id))
        row = await result.fetchone(); await conn.commit()
    if not row: raise HTTPException(status_code=409, detail="Template must be approved in this workspace before publishing")
    return row


@router.post("/marketplace/templates/{template_id}/copies", status_code=201)
async def copy_template(template_id: str, body: TemplateCopy, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "marketplace:copy")
    target = body.target_workspace_id or actor.workspace_id
    if target != actor.workspace_id and actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Only workspace administrators can copy across workspaces")
    async with pool.connection() as conn:
        template = await conn.execute("SELECT id,workspace_id,status,review_status FROM pf_marketplace_template WHERE id=%s AND ((visibility='public' AND status='published') OR workspace_id=%s)", (template_id, actor.workspace_id)); row = await template.fetchone()
        if not row: raise HTTPException(status_code=404, detail="Template not found")
        result = await conn.execute("""INSERT INTO pf_marketplace_copy(id,template_id,source_workspace_id,target_workspace_id,idempotency_key,created_by)
          VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(template_id,target_workspace_id,idempotency_key) DO UPDATE SET id=pf_marketplace_copy.id RETURNING id,template_id,source_workspace_id,target_workspace_id,idempotency_key,created_at""", (new_id("tplcopy"), template_id, row["workspace_id"], target, body.idempotency_key, actor.user_id))
        copied = await result.fetchone(); await conn.commit()
    return {**copied, "copy_mode": "metadata_only", "execution": "pending_template_materialization"}


class TemplatePatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=2, max_length=160)
    description: str | None = Field(default=None, max_length=2_000)
    manifest: dict[str, Any] | None = None
    artifact_ref: str | None = Field(default=None, min_length=1, max_length=256)
    status: Literal["draft", "published", "archived"] | None = None

    @field_validator("artifact_ref")
    @classmethod
    def validate_artifact_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not _SAFE_REF.fullmatch(value):
            raise ValueError("artifact_ref must be a controlled mock:// or local:// reference")
        return value

    @field_validator("manifest")
    @classmethod
    def validate_manifest(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return _safe_json(value)


class TemplateInstall(BaseModel):
    target_workspace_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=160)


@router.get("/marketplace/templates/{template_id}")
async def get_marketplace_template(template_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "marketplace:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT id,name,display_name,template_type,version,description,manifest,artifact_ref,
                      review_status,visibility,status,created_by,reviewed_at,created_at,updated_at
               FROM pf_marketplace_template
               WHERE id=%s AND ((visibility='public' AND status='published') OR workspace_id=%s)""",
            (template_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Template not found")
    return _template_view(row)


@router.patch("/marketplace/templates/{template_id}")
async def patch_marketplace_template(template_id: str, body: TemplatePatch, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "marketplace:write")
    updates: list[str] = []
    values: list[Any] = []
    if body.display_name is not None:
        updates.append("display_name=%s"); values.append(body.display_name)
    if body.description is not None:
        updates.append("description=%s"); values.append(body.description)
    if body.manifest is not None:
        updates.append("manifest=%s::jsonb"); values.append(json_dumps(body.manifest))
    if body.artifact_ref is not None:
        updates.append("artifact_ref=%s"); values.append(body.artifact_ref)
    if body.status is not None:
        updates.append("status=%s"); values.append(body.status)
    if not updates:
        return await get_marketplace_template(template_id, actor)
    updates.append("updated_at=now()")
    values.extend([template_id, actor.workspace_id])
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""UPDATE pf_marketplace_template SET {', '.join(updates)}
                WHERE id=%s AND workspace_id=%s
                RETURNING id,name,display_name,template_type,version,description,manifest,artifact_ref,
                          review_status,visibility,status,created_by,reviewed_at,created_at,updated_at""",
            values,
        )
        row = await result.fetchone(); await conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Template not found")
    return _template_view(row)


@router.post("/marketplace/templates/{template_id}/releases")
async def release_marketplace_template(template_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "marketplace:publish")
    async with pool.connection() as conn:
        result = await conn.execute(
            """UPDATE pf_marketplace_template
               SET visibility='public',status='published',version=version+1,updated_at=now()
               WHERE id=%s AND workspace_id=%s AND review_status='approved'
               RETURNING id,name,display_name,template_type,version,status,visibility,review_status,updated_at""",
            (template_id, actor.workspace_id),
        )
        row = await result.fetchone(); await conn.commit()
    if not row:
        raise HTTPException(status_code=409, detail="Template must be approved in this workspace before release")
    return {**_template_view(row), "released": True}


@router.post("/marketplace/templates/{template_id}/unpublish")
async def unpublish_marketplace_template(template_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "marketplace:publish")
    async with pool.connection() as conn:
        result = await conn.execute(
            """UPDATE pf_marketplace_template
               SET visibility='private',status='draft',version=version+1,updated_at=now()
               WHERE id=%s AND workspace_id=%s
               RETURNING id,name,display_name,template_type,version,status,visibility,review_status,updated_at""",
            (template_id, actor.workspace_id),
        )
        row = await result.fetchone(); await conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Template not found")
    return {**_template_view(row), "unpublished": True}


@router.post("/marketplace/templates/{template_id}/installs", status_code=201)
async def install_marketplace_template(template_id: str, body: TemplateInstall, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "marketplace:copy")
    target = body.target_workspace_id or actor.workspace_id
    if target != actor.workspace_id and actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Only workspace administrators can install across workspaces")
    async with pool.connection() as conn:
        template = await conn.execute(
            "SELECT id,workspace_id,status,visibility FROM pf_marketplace_template WHERE id=%s AND ((visibility='public' AND status='published') OR workspace_id=%s)",
            (template_id, actor.workspace_id),
        )
        row = await template.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Template not found")
        result = await conn.execute(
            """INSERT INTO pf_marketplace_copy(id,template_id,source_workspace_id,target_workspace_id,idempotency_key,created_by)
               VALUES(%s,%s,%s,%s,%s,%s)
               ON CONFLICT(template_id,target_workspace_id,idempotency_key) DO UPDATE SET id=pf_marketplace_copy.id
               RETURNING id,template_id,source_workspace_id,target_workspace_id,idempotency_key,created_at""",
            (new_id("tplcopy"), template_id, row["workspace_id"], target, body.idempotency_key, actor.user_id),
        )
        copied = await result.fetchone(); await conn.commit()
    return {**copied, "copy_mode": "metadata_only", "execution": "pending_template_materialization"}


@router.get("/marketplace/skills")
async def list_marketplace_skills(actor: Annotated[Actor, Depends(get_actor)], limit: int = Query(default=50, ge=1, le=200)):
    _require(actor, "marketplace:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT id,publisher,name,semver,manifest,artifact_ref,source_kind,content_sha256,
                      signature_status,risk_level,review_status,status,revision,created_at,updated_at
               FROM ag_skill
               WHERE (workspace_id=%s OR review_status='approved') AND status='active'
               ORDER BY updated_at DESC LIMIT %s""",
            (actor.workspace_id, limit),
        )
        data = [{
            "id": row["id"], "publisher": row["publisher"], "name": row["name"], "version": row["semver"],
            "manifest": row["manifest"], "artifact_ref": row["artifact_ref"], "source_kind": row["source_kind"],
            "content_sha256": row["content_sha256"], "signature_status": row["signature_status"],
            "risk_level": row["risk_level"], "review_status": row["review_status"], "status": row["status"],
            "revision": row["revision"], "created_at": row["created_at"], "updated_at": row["updated_at"],
        } for row in await result.fetchall()]
    # Contract 720 listMarketplaceSkills ListQuery -> ListResponse<MarketplaceSkillDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.get("/marketplace/skills/{skill_id}")
async def get_marketplace_skill(skill_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "marketplace:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT id,publisher,name,semver,manifest,artifact_ref,source_kind,content_sha256,
                      signature_status,risk_level,review_status,status,revision,created_at,updated_at
               FROM ag_skill
               WHERE id=%s AND (workspace_id=%s OR review_status='approved') AND status='active'""",
            (skill_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Skill not found")
    return {
        "id": row["id"], "publisher": row["publisher"], "name": row["name"], "version": row["semver"],
        "manifest": row["manifest"], "artifact_ref": row["artifact_ref"], "source_kind": row["source_kind"],
        "content_sha256": row["content_sha256"], "signature_status": row["signature_status"],
        "risk_level": row["risk_level"], "review_status": row["review_status"], "status": row["status"],
        "revision": row["revision"], "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


@router.post("/marketplace/skills/{skill_id}/releases")
async def release_marketplace_skill(skill_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "marketplace:publish")
    async with pool.connection() as conn:
        result = await conn.execute(
            """UPDATE ag_skill
               SET review_status='approved',status='active',revision=revision+1,updated_at=now()
               WHERE id=%s AND workspace_id=%s
               RETURNING id,publisher,name,semver,review_status,status,revision,updated_at""",
            (skill_id, actor.workspace_id),
        )
        row = await result.fetchone(); await conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Skill not found")
    return {"skill_id": row["id"], "name": row["name"], "version": row["semver"], "review_status": row["review_status"], "status": row["status"], "revision": row["revision"], "released": True}


@router.post("/marketplace/skills/{skill_id}/unpublish")
async def unpublish_marketplace_skill(skill_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "marketplace:publish")
    async with pool.connection() as conn:
        result = await conn.execute(
            """UPDATE ag_skill
               SET status='revoked',revision=revision+1,updated_at=now()
               WHERE id=%s AND workspace_id=%s
               RETURNING id,publisher,name,semver,review_status,status,revision,updated_at""",
            (skill_id, actor.workspace_id),
        )
        row = await result.fetchone(); await conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Skill not found")
    return {"skill_id": row["id"], "name": row["name"], "version": row["semver"], "status": row["status"], "revision": row["revision"], "unpublished": True}
