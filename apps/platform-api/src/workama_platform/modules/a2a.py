from __future__ import annotations

import hashlib
import hmac
import json
import re
import base64
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from workama_platform.core import Actor, capability_allows, decrypt_secret, encrypt_secret, get_actor, hash_secret, json_dumps, new_id, pool
from workama_platform.modules.security.service import validate_outbound_url

router = APIRouter(prefix="/api/v1", tags=["a2a"])
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_SAFE_REF = re.compile(r"^(?:mock|local)://[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}$")
_DIGEST_SIGNATURE_RE = re.compile(r"^[a-f0-9]{64}$")
_HEX_SIGNATURE_RE = re.compile(r"^[a-f0-9]{128}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9._~-]{16,160}$")
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9._~-]{1,64}$")
_PUBLIC_KEY_ALGORITHM = "Ed25519"


def _require(actor: Actor, capability: str) -> None:
    aliases = {"a2a:read": ("external_app:read", "marketplace:read"), "a2a:write": ("external_app:*", "marketplace:*")}
    if capability_allows(actor.capabilities, capability) or any(capability_allows(actor.capabilities, item) for item in aliases.get(capability, ())):
        return
    raise HTTPException(status_code=403, detail=f"Missing capability: {capability}")


class AgentCardCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    agent_id: str = Field(min_length=2, max_length=64, pattern=_ID_RE.pattern)
    endpoint: str
    version: str = Field(min_length=1, max_length=64)
    capabilities: list[str] = Field(default_factory=list, max_length=64)
    skills: list[str] = Field(default_factory=list, max_length=64)
    authentication: Literal["none", "delegated", "oauth"] = "delegated"
    metadata: dict[str, Any] = Field(default_factory=dict)
    public_key_id: str = Field(default="default", min_length=1, max_length=64, pattern=_KEY_ID_RE.pattern)
    public_key_algorithm: Literal["Ed25519"] = _PUBLIC_KEY_ALGORITHM
    public_key: str | None = Field(default=None, min_length=43, max_length=128)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        if value.startswith("mock://") or value.startswith("local://"):
            return value
        result = validate_outbound_url(value.strip())
        if not result.allowed:
            raise ValueError(f"endpoint is unsafe: {result.reason}")
        return value.strip()

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode()) > 16_000:
            raise ValueError("metadata is too large")
        blocked = {"token", "secret", "api_key", "authorization", "password", "private_key", "public_key"}
        if any(str(key).lower() in blocked for key in value):
            raise ValueError("metadata contains a sensitive field")
        return value

    @field_validator("public_key")
    @classmethod
    def validate_public_key(cls, value: str | None) -> str | None:
        if value is not None:
            _decode_public_key(value)
        return value


class AgentCardPatch(BaseModel):
    endpoint: str | None = None
    version: str | None = Field(default=None, max_length=64)
    capabilities: list[str] | None = Field(default=None, max_length=64)
    skills: list[str] | None = Field(default=None, max_length=64)
    status: Literal["active", "disabled", "revoked"] | None = None
    public_key_id: str = Field(default="default", min_length=1, max_length=64, pattern=_KEY_ID_RE.pattern)
    public_key_algorithm: Literal["Ed25519"] = _PUBLIC_KEY_ALGORITHM
    public_key: str | None = Field(default=None, min_length=43, max_length=128)

    @field_validator("public_key")
    @classmethod
    def validate_public_key(cls, value: str | None) -> str | None:
        if value is not None:
            _decode_public_key(value)
        return value


class A2ATaskCreate(BaseModel):
    card_id: str = Field(min_length=1, max_length=128)
    operation: str = Field(min_length=2, max_length=120)
    message: str = Field(min_length=1, max_length=20_000)
    artifact_refs: list[str] = Field(default_factory=list, max_length=32)
    idempotency_key: str = Field(min_length=1, max_length=160)
    signature_key_id: str = Field(default="default", min_length=1, max_length=64, pattern=_KEY_ID_RE.pattern)
    signature: str | None = Field(default=None, min_length=64, max_length=128)
    nonce: str | None = Field(default=None, min_length=16, max_length=160)
    signed_at: datetime | None = None

    @field_validator("artifact_refs")
    @classmethod
    def validate_refs(cls, value: list[str]) -> list[str]:
        normalized = sorted(set(value))
        if any(not _SAFE_REF.fullmatch(ref) for ref in normalized):
            raise ValueError("artifact_refs must be controlled mock:// or local:// references")
        return normalized

    @field_validator("nonce")
    @classmethod
    def validate_nonce(cls, value: str | None) -> str | None:
        if value is not None and not _NONCE_RE.fullmatch(value):
            raise ValueError("nonce contains unsupported characters")
        return value

    @field_validator("signature")
    @classmethod
    def validate_signature(cls, value: str | None) -> str | None:
        if value is not None and not (_DIGEST_SIGNATURE_RE.fullmatch(value) or _HEX_SIGNATURE_RE.fullmatch(value) or _decode_signature(value)):
            raise ValueError("signature must be a 64-byte Ed25519 signature or a 64-character digest")
        return value


class A2ATaskUpdate(BaseModel):
    status: Literal["queued", "working", "completed", "failed", "cancelled"]
    result_summary: str = Field(default="", max_length=2_000)
    artifact_refs: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("artifact_refs")
    @classmethod
    def validate_refs(cls, value: list[str]) -> list[str]:
        normalized = sorted(set(value))
        if any(not _SAFE_REF.fullmatch(ref) for ref in normalized):
            raise ValueError("artifact_refs must be controlled mock:// or local:// references")
        return normalized


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS pf_a2a_agent_card (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      name TEXT NOT NULL,
      agent_id TEXT NOT NULL,
      endpoint TEXT NOT NULL,
      version TEXT NOT NULL,
      capabilities TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
      skills TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
      authentication TEXT NOT NULL DEFAULT 'delegated' CHECK (authentication IN ('none','delegated','oauth')),
      metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
      status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','revoked')),
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id,agent_id,version)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pf_a2a_agent_card_workspace ON pf_a2a_agent_card(workspace_id,status,updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS pf_a2a_agent_key (
      id TEXT PRIMARY KEY,
      card_id TEXT NOT NULL REFERENCES pf_a2a_agent_card(id) ON DELETE CASCADE,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      key_id TEXT NOT NULL,
      algorithm TEXT NOT NULL CHECK (algorithm IN ('Ed25519')),
      public_key_enc TEXT NOT NULL,
      public_key_fingerprint TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
      valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
      valid_until TIMESTAMPTZ,
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(card_id,key_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pf_a2a_agent_key_workspace ON pf_a2a_agent_key(workspace_id,card_id,status)",
    """
    CREATE TABLE IF NOT EXISTS pf_a2a_task (
      id TEXT PRIMARY KEY,
      card_id TEXT NOT NULL REFERENCES pf_a2a_agent_card(id) ON DELETE CASCADE,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      operation TEXT NOT NULL,
      message_hash TEXT NOT NULL,
      artifact_refs TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
      status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','working','completed','failed','cancelled')),
      result_summary TEXT NOT NULL DEFAULT '',
      idempotency_key TEXT NOT NULL,
      delegated_credential_hash TEXT,
      signature_key_id TEXT NOT NULL DEFAULT 'default',
      signature TEXT,
      nonce TEXT,
      signed_at TIMESTAMPTZ,
      signature_verified BOOLEAN NOT NULL DEFAULT FALSE,
      signature_mode TEXT NOT NULL DEFAULT 'digest' CHECK (signature_mode IN ('digest','ed25519')),
      execution_mode TEXT NOT NULL DEFAULT 'pending_external',
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(card_id,idempotency_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pf_a2a_task_workspace_time ON pf_a2a_task(workspace_id,created_at DESC)",
)


async def ensure_a2a_schema(conn) -> None:
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)
    await conn.execute("ALTER TABLE pf_a2a_task ADD COLUMN IF NOT EXISTS signature TEXT")
    await conn.execute("ALTER TABLE pf_a2a_task ADD COLUMN IF NOT EXISTS nonce TEXT")
    await conn.execute("ALTER TABLE pf_a2a_task ADD COLUMN IF NOT EXISTS signed_at TIMESTAMPTZ")
    await conn.execute("ALTER TABLE pf_a2a_task ADD COLUMN IF NOT EXISTS signature_verified BOOLEAN NOT NULL DEFAULT FALSE")
    await conn.execute("ALTER TABLE pf_a2a_task ADD COLUMN IF NOT EXISTS signature_key_id TEXT NOT NULL DEFAULT 'default'")
    await conn.execute("ALTER TABLE pf_a2a_task ADD COLUMN IF NOT EXISTS signature_mode TEXT NOT NULL DEFAULT 'digest'")
    await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pf_a2a_task_card_nonce ON pf_a2a_task(card_id,nonce) WHERE nonce IS NOT NULL")
    await conn.execute("CREATE TABLE IF NOT EXISTS pf_a2a_agent_key (id TEXT PRIMARY KEY, card_id TEXT NOT NULL REFERENCES pf_a2a_agent_card(id) ON DELETE CASCADE, org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE, workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE, key_id TEXT NOT NULL, algorithm TEXT NOT NULL CHECK (algorithm IN ('Ed25519')), public_key_enc TEXT NOT NULL, public_key_fingerprint TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')), valid_from TIMESTAMPTZ NOT NULL DEFAULT now(), valid_until TIMESTAMPTZ, created_by TEXT NOT NULL REFERENCES id_user(id), created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(card_id,key_id))")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_pf_a2a_agent_key_workspace ON pf_a2a_agent_key(workspace_id,card_id,status)")


def task_signature(message_hash: str, nonce: str) -> str:
    """Controlled digest retained for mock/local cards without a registered key."""
    return hashlib.sha256(f"{message_hash}:{nonce}".encode()).hexdigest()


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _decode_base64(value: str) -> bytes:
    try:
        normalized = value.strip().replace("-", "+").replace("_", "/")
        return base64.b64decode(normalized + "=" * (-len(normalized) % 4), validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("value is not valid base64") from exc


def _decode_public_key(value: str) -> bytes:
    decoded = _decode_base64(value)
    if len(decoded) != 32:
        raise ValueError("Ed25519 public key must be exactly 32 bytes")
    try:
        Ed25519PublicKey.from_public_bytes(decoded)
    except ValueError as exc:
        raise ValueError("Ed25519 public key is invalid") from exc
    return decoded


def public_key_fingerprint(public_key: str | bytes) -> str:
    raw = _decode_public_key(public_key) if isinstance(public_key, str) else bytes(public_key)
    if len(raw) != 32:
        raise ValueError("Ed25519 public key must be exactly 32 bytes")
    return hashlib.sha256(raw).hexdigest()


def public_key_signature_payload(*, workspace_id: str, card_id: str, key_id: str, message_hash: str, nonce: str, signed_at: datetime) -> bytes:
    normalized_time = _utc(signed_at)
    if normalized_time is None:
        raise ValueError("signed_at is required")
    return json_dumps({
        "version": 1,
        "workspace_id": workspace_id,
        "card_id": card_id,
        "key_id": key_id,
        "message_hash": message_hash,
        "nonce": nonce,
        "signed_at_epoch": int(normalized_time.timestamp()),
    }).encode()


def _decode_signature(value: str) -> bytes:
    if _HEX_SIGNATURE_RE.fullmatch(value):
        return bytes.fromhex(value)
    decoded = _decode_base64(value)
    if len(decoded) != 64:
        raise ValueError("Ed25519 signature must be exactly 64 bytes")
    return decoded


def _trusted_key_view(row: Any) -> dict[str, Any]:
    return {
        "key_id": row["key_id"],
        "algorithm": row["algorithm"],
        "fingerprint": row["public_key_fingerprint"],
        "status": row["status"],
        "valid_from": row.get("valid_from") if hasattr(row, "get") else None,
        "valid_until": row.get("valid_until") if hasattr(row, "get") else None,
    }


def _signature_state(*, endpoint: str, message_hash: str, signature: str | None, nonce: str | None, signed_at: datetime | None, workspace_id: str | None = None, card_id: str | None = None, key_id: str = "default", trusted_key: Any | None = None) -> tuple[bool, str]:
    controlled = endpoint.startswith(("mock://", "local://"))
    if not signature or not nonce or signed_at is None:
        raise HTTPException(status_code=401, detail="A2A signature, nonce and signed_at are required")
    normalized_time = _utc(signed_at)
    if normalized_time is None or abs(datetime.now(UTC) - normalized_time) > timedelta(minutes=5):
        raise HTTPException(status_code=401, detail="A2A signature is outside the allowed time window")
    if trusted_key:
        if trusted_key["algorithm"] != _PUBLIC_KEY_ALGORITHM or workspace_id is None or card_id is None:
            raise HTTPException(status_code=401, detail="A2A trusted public key is invalid")
        encrypted_key = trusted_key.get("public_key_enc") if hasattr(trusted_key, "get") else trusted_key["public_key_enc"]
        fingerprint = trusted_key.get("public_key_fingerprint") if hasattr(trusted_key, "get") else trusted_key["public_key_fingerprint"]
        try:
            encoded_key = decrypt_secret(encrypted_key)
            raw_key = _decode_public_key(encoded_key or "")
            if not hmac.compare_digest(public_key_fingerprint(raw_key), fingerprint):
                raise ValueError("public key fingerprint mismatch")
            Ed25519PublicKey.from_public_bytes(raw_key).verify(
                _decode_signature(signature),
                public_key_signature_payload(
                    workspace_id=workspace_id,
                    card_id=card_id,
                    key_id=key_id,
                    message_hash=message_hash,
                    nonce=nonce,
                    signed_at=normalized_time,
                ),
            )
        except (InvalidSignature, TypeError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=401, detail="A2A public-key signature verification failed") from exc
        except Exception as exc:
            raise HTTPException(status_code=401, detail="A2A trusted public key is unavailable") from exc
        return (controlled, "verified_public_key")
    if not controlled:
        raise HTTPException(status_code=401, detail="A2A endpoint has no trusted public key")
    expected = task_signature(message_hash, nonce)
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="A2A signature verification failed")
    return (controlled, "verified_controlled")


def _card_view(row: Any) -> dict[str, Any]:
    result = dict(row)
    result.pop("metadata", None)
    result.setdefault("trusted_keys", [])
    return result


async def _list_trusted_keys(conn: Any, card_id: str, workspace_id: str) -> list[dict[str, Any]]:
    result = await conn.execute(
        "SELECT key_id,algorithm,public_key_fingerprint,status,valid_from,valid_until FROM pf_a2a_agent_key WHERE card_id=%s AND workspace_id=%s ORDER BY key_id",
        (card_id, workspace_id),
    )
    return [_trusted_key_view(row) for row in await result.fetchall()]


async def _get_trusted_key(conn: Any, card_id: str, workspace_id: str, key_id: str) -> Any | None:
    result = await conn.execute(
        "SELECT key_id,algorithm,public_key_enc,public_key_fingerprint,status,valid_from,valid_until FROM pf_a2a_agent_key WHERE card_id=%s AND workspace_id=%s AND key_id=%s AND status='active' AND valid_from <= now() AND (valid_until IS NULL OR valid_until > now())",
        (card_id, workspace_id, key_id),
    )
    return await result.fetchone()


async def _upsert_trusted_key(conn: Any, *, card_id: str, org_id: str, workspace_id: str, key_id: str, algorithm: str, public_key: str, created_by: str) -> None:
    raw = _decode_public_key(public_key)
    if algorithm != _PUBLIC_KEY_ALGORITHM:
        raise HTTPException(status_code=422, detail="Only Ed25519 Agent Card keys are supported")
    await conn.execute(
        """
        INSERT INTO pf_a2a_agent_key(id,card_id,org_id,workspace_id,key_id,algorithm,public_key_enc,public_key_fingerprint,created_by)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(card_id,key_id) DO UPDATE SET algorithm=EXCLUDED.algorithm,public_key_enc=EXCLUDED.public_key_enc,public_key_fingerprint=EXCLUDED.public_key_fingerprint,status='active',valid_from=now(),valid_until=NULL,updated_at=now()
        """,
        (new_id("a2akey"), card_id, org_id, workspace_id, key_id, algorithm, encrypt_secret(base64.urlsafe_b64encode(raw).decode().rstrip("=")), public_key_fingerprint(raw), created_by),
    )


@router.get("/a2a/agent-cards")
async def list_agent_cards(actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    _require(actor, "a2a:read")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,name,agent_id,endpoint,version,capabilities,skills,authentication,status,created_by,created_at,updated_at FROM pf_a2a_agent_card WHERE workspace_id=%s ORDER BY updated_at DESC", (actor.workspace_id,))
        rows = await result.fetchall()
        items = []
        for row in rows:
            item = _card_view(row)
            item["trusted_keys"] = await _list_trusted_keys(conn, row["id"], actor.workspace_id)
            items.append(item)
    # Contract 720 listAgentCards ListQuery -> ListResponse<AgentCardDTO>
    # keep items field for backward compatibility
    return {
        "items": items,
        "data": items,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(items)},
    }


@router.get("/a2a/public/agent-cards/{card_id}")
async def get_public_agent_card(card_id: str) -> dict[str, Any]:
    """Expose only the discovery fields needed for external trust negotiation."""
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT id,name,agent_id,endpoint,version,capabilities,skills,authentication,status,workspace_id
               FROM pf_a2a_agent_card WHERE id=%s AND status='active'""",
            (card_id,),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Agent card not found")
        keys = await _list_trusted_keys(conn, row["id"], row["workspace_id"])
    return {
        "protocol": "a2a",
        "schema_version": "1",
        "name": row["name"],
        "agent_id": row["agent_id"],
        "endpoint": row["endpoint"],
        "version": row["version"],
        "capabilities": row["capabilities"],
        "skills": row["skills"],
        "authentication": row["authentication"],
        "trusted_keys": keys,
        "trust_status": "trusted" if keys else "untrusted_external",
    }


@router.post("/a2a/agent-cards", status_code=201)
async def create_agent_card(body: AgentCardCreate, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    _require(actor, "a2a:write")
    async with pool.connection() as conn:
        card_id = new_id("a2acard")
        result = await conn.execute("INSERT INTO pf_a2a_agent_card(id,org_id,workspace_id,name,agent_id,endpoint,version,capabilities,skills,authentication,metadata,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s) RETURNING id,name,agent_id,endpoint,version,capabilities,skills,authentication,status,created_by,created_at,updated_at", (card_id, actor.org_id, actor.workspace_id, body.name, body.agent_id, body.endpoint.strip(), body.version, body.capabilities, body.skills, body.authentication, json_dumps(body.metadata), actor.user_id))
        row = await result.fetchone()
        if body.public_key:
            await _upsert_trusted_key(conn, card_id=card_id, org_id=actor.org_id, workspace_id=actor.workspace_id, key_id=body.public_key_id, algorithm=body.public_key_algorithm, public_key=body.public_key, created_by=actor.user_id)
        row = dict(row)
        row["trusted_keys"] = await _list_trusted_keys(conn, card_id, actor.workspace_id)
        await conn.commit()
    return _card_view(row)


@router.patch("/a2a/agent-cards/{card_id}")
async def patch_agent_card(card_id: str, body: AgentCardPatch, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    _require(actor, "a2a:write")
    fields: list[str] = []
    params: list[Any] = []
    for field in ("endpoint", "version", "capabilities", "skills", "status"):
        value = getattr(body, field)
        if value is not None:
            if field == "endpoint":
                result = validate_outbound_url(value) if not value.startswith(("mock://", "local://")) else None
                if result is not None and not result.allowed:
                    raise HTTPException(status_code=422, detail="endpoint is unsafe")
            fields.append(f"{field}=%s")
            params.append(value)
    key_update = "public_key" in body.model_fields_set
    if "public_key_id" in body.model_fields_set and not key_update:
        raise HTTPException(status_code=422, detail="public_key is required when changing public_key_id")
    if not fields and not key_update:
        raise HTTPException(status_code=422, detail="at least one field is required")
    params.extend([card_id, actor.workspace_id])
    async with pool.connection() as conn:
        if fields:
            result = await conn.execute(f"UPDATE pf_a2a_agent_card SET {', '.join(fields)},updated_at=now() WHERE id=%s AND workspace_id=%s RETURNING id,name,agent_id,endpoint,version,capabilities,skills,authentication,status,created_by,created_at,updated_at", tuple(params))
        else:
            result = await conn.execute("SELECT id,name,agent_id,endpoint,version,capabilities,skills,authentication,status,created_by,created_at,updated_at FROM pf_a2a_agent_card WHERE id=%s AND workspace_id=%s", (card_id, actor.workspace_id))
        row = await result.fetchone()
        if row and key_update:
            await _upsert_trusted_key(conn, card_id=card_id, org_id=actor.org_id, workspace_id=actor.workspace_id, key_id=body.public_key_id, algorithm=body.public_key_algorithm, public_key=body.public_key or "", created_by=actor.user_id)
        if row:
            row = dict(row)
            row["trusted_keys"] = await _list_trusted_keys(conn, card_id, actor.workspace_id)
        await conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Agent card not found")
    return _card_view(row)


@router.post("/a2a/tasks", status_code=202)
async def create_a2a_task(body: A2ATaskCreate, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    _require(actor, "a2a:write")
    message_hash = hashlib.sha256(json_dumps({"operation": body.operation, "message": body.message, "artifact_refs": body.artifact_refs}).encode()).hexdigest()
    async with pool.connection() as conn:
        card_result = await conn.execute("SELECT id,status,endpoint FROM pf_a2a_agent_card WHERE id=%s AND workspace_id=%s", (body.card_id, actor.workspace_id))
        card = await card_result.fetchone()
        if not card:
            raise HTTPException(status_code=404, detail="Agent card not found")
        if card["status"] != "active":
            raise HTTPException(status_code=409, detail="Agent card is not active")
        existing_result = await conn.execute("SELECT id,card_id,operation,message_hash,artifact_refs,status,result_summary,idempotency_key,signature_key_id,signature,nonce,signed_at,signature_verified,signature_mode,execution_mode,created_at,updated_at FROM pf_a2a_task WHERE card_id=%s AND idempotency_key=%s", (body.card_id, body.idempotency_key))
        existing = await existing_result.fetchone()
        if existing:
            if existing["message_hash"] != message_hash:
                raise HTTPException(status_code=409, detail="Idempotency key was used with a different message")
            if existing["nonce"] != body.nonce or existing["signature"] != body.signature or existing.get("signature_key_id", "default") != body.signature_key_id:
                raise HTTPException(status_code=409, detail="Idempotency key was used with a different signed request")
            trust_status = "verified_public_key" if existing.get("signature_mode") == "ed25519" else ("verified_controlled" if existing["signature_verified"] else "untrusted_external")
            return {**existing, "trust_status": trust_status, "idempotency_replayed": True}
        nonce_result = await conn.execute("SELECT id FROM pf_a2a_task WHERE card_id=%s AND nonce=%s AND idempotency_key<>%s", (body.card_id, body.nonce, body.idempotency_key))
        if await nonce_result.fetchone():
            raise HTTPException(status_code=409, detail="A2A nonce has already been used for this Agent Card")
        trusted_key = await _get_trusted_key(conn, body.card_id, actor.workspace_id, body.signature_key_id)
        _, trust_status = _signature_state(endpoint=card["endpoint"], message_hash=message_hash, signature=body.signature, nonce=body.nonce, signed_at=body.signed_at, workspace_id=actor.workspace_id, card_id=body.card_id, key_id=body.signature_key_id, trusted_key=trusted_key)
        signature_mode = "ed25519" if trust_status == "verified_public_key" else "digest"
        result = await conn.execute("INSERT INTO pf_a2a_task(id,card_id,org_id,workspace_id,operation,message_hash,artifact_refs,idempotency_key,delegated_credential_hash,signature_key_id,signature,nonce,signed_at,signature_verified,signature_mode,execution_mode,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending_external',%s) RETURNING id,card_id,operation,message_hash,artifact_refs,status,result_summary,idempotency_key,signature_key_id,signature,nonce,signed_at,signature_verified,signature_mode,execution_mode,created_at,updated_at", (new_id("a2atask"), body.card_id, actor.org_id, actor.workspace_id, body.operation, message_hash, body.artifact_refs, body.idempotency_key, hash_secret("delegated:" + actor.user_id), body.signature_key_id, body.signature, body.nonce, _utc(body.signed_at), trust_status in {"verified_controlled", "verified_public_key"}, signature_mode, actor.user_id))
        row = await result.fetchone(); await conn.commit()
    return {**row, "external_execution": "pending", "trust_status": trust_status, "idempotency_replayed": False}


@router.get("/a2a/tasks/{task_id}")
async def get_a2a_task(task_id: str, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    _require(actor, "a2a:read")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,card_id,operation,message_hash,artifact_refs,status,result_summary,idempotency_key,signature_key_id,signature,nonce,signed_at,signature_verified,signature_mode,execution_mode,created_at,updated_at FROM pf_a2a_task WHERE id=%s AND workspace_id=%s", (task_id, actor.workspace_id))
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="A2A task not found")
    return row


@router.post("/a2a/tasks/{task_id}/updates")
async def update_a2a_task(task_id: str, body: A2ATaskUpdate, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    _require(actor, "a2a:write")
    async with pool.connection() as conn:
        result = await conn.execute("UPDATE pf_a2a_task SET status=%s,result_summary=%s,artifact_refs=%s,updated_at=now() WHERE id=%s AND workspace_id=%s RETURNING id,card_id,operation,message_hash,artifact_refs,status,result_summary,idempotency_key,signature_key_id,signature,nonce,signed_at,signature_verified,signature_mode,execution_mode,created_at,updated_at", (body.status, body.result_summary, body.artifact_refs, task_id, actor.workspace_id))
        row = await result.fetchone(); await conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="A2A task not found")
    return {**row, "execution_mode": "local_update_only"}
