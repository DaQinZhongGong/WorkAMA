from __future__ import annotations

import base64
import importlib
import inspect
import json
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, EmailStr, Field

from workama_platform.core import Actor, get_actor, new_id, pool
from workama_platform.modules.auth.router import _issue_session


router = APIRouter(prefix="/api/v1", tags=["passkeys"])

PASSKEY_CHALLENGE_TTL_SECONDS = 300
PASSKEY_TIMEOUT_MS = 60_000


def _require_user_actor(actor: Actor) -> None:
    if actor.actor_type != "user":
        raise HTTPException(status_code=403, detail="Passkey and device operations require a user session")


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS id_passkey (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
        workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
        credential_id TEXT NOT NULL UNIQUE,
        public_key BYTEA NOT NULL,
        sign_count BIGINT NOT NULL DEFAULT 0 CHECK (sign_count >= 0),
        transports TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
        aaguid TEXT,
        name TEXT NOT NULL DEFAULT 'Passkey',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_used_at TIMESTAMPTZ,
        revoked_at TIMESTAMPTZ,
        revoke_reason TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS id_passkey_challenge (
        id TEXT PRIMARY KEY,
        user_id TEXT REFERENCES id_user(id) ON DELETE CASCADE,
        workspace_id TEXT REFERENCES id_workspace(id) ON DELETE CASCADE,
        flow TEXT NOT NULL CHECK (flow IN ('registration', 'authentication')),
        challenge TEXT NOT NULL UNIQUE,
        rp_id TEXT NOT NULL,
        origin TEXT NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        consumed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_id_passkey_user_workspace ON id_passkey(user_id, workspace_id, revoked_at, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_id_passkey_challenge_expiry ON id_passkey_challenge(expires_at, consumed_at)",
    "CREATE INDEX IF NOT EXISTS idx_id_passkey_challenge_user ON id_passkey_challenge(user_id, flow, created_at DESC)",
)


async def ensure_passkey_schema(conn) -> None:
    """Apply the additive Passkey schema to an existing connection."""
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


class PasskeyConfigurationError(RuntimeError):
    """The server cannot safely perform WebAuthn verification."""


class WebAuthnVerificationError(RuntimeError):
    """The authenticator response failed cryptographic verification."""


@dataclass(frozen=True)
class PasskeyConfig:
    rp_id: str
    origin: str
    rp_name: str = "WorkAMA"
    timeout_ms: int = PASSKEY_TIMEOUT_MS


class RegistrationOptionsRequest(BaseModel):
    name: str = Field(default="Passkey", min_length=1, max_length=120)


class RegistrationCompleteRequest(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=120)
    credential: dict[str, Any]
    name: str = Field(default="Passkey", min_length=1, max_length=120)


class AuthenticationOptionsRequest(BaseModel):
    email: EmailStr | None = None


class AuthenticationCompleteRequest(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=120)
    credential: dict[str, Any]


class PasskeyRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class RevokeRequest(BaseModel):
    reason: str = Field(default="User revoked credential", max_length=500)


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    if not isinstance(value, str) or not value or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for char in value):
        raise ValueError("Invalid base64url value")
    return base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))


def _now() -> datetime:
    return datetime.now(UTC)


def challenge_is_usable(
    expires_at: datetime,
    consumed_at: datetime | None,
    now: datetime | None = None,
) -> bool:
    current = now or _now()
    return consumed_at is None and expires_at > current


def challenge_allows_credential(
    challenge: dict[str, Any],
    credential: dict[str, Any],
) -> bool:
    challenge_user = challenge.get("user_id")
    challenge_workspace = challenge.get("workspace_id")
    return (
        (not challenge_user or credential.get("user_id") == challenge_user)
        and (not challenge_workspace or credential.get("workspace_id") == challenge_workspace)
    )


def _require_usable_challenge(row: dict[str, Any], flow: str) -> None:
    if row.get("flow") != flow or not challenge_is_usable(row["expires_at"], row.get("consumed_at")):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Passkey challenge is invalid, expired, or already used")


def _config_from_environment() -> PasskeyConfig:
    rp_id = os.getenv("WORKAMA_PASSKEY_RP_ID", "").strip().lower()
    origin = os.getenv("WORKAMA_PASSKEY_ORIGIN", "").strip().rstrip("/")
    rp_name = os.getenv("WORKAMA_PASSKEY_RP_NAME", "WorkAMA").strip() or "WorkAMA"
    if not rp_id or not origin:
        raise PasskeyConfigurationError("Passkey is not configured: WORKAMA_PASSKEY_RP_ID and WORKAMA_PASSKEY_ORIGIN are required")
    if any(character.isspace() for character in rp_id) or "/" in rp_id or ":" in rp_id:
        raise PasskeyConfigurationError("WORKAMA_PASSKEY_RP_ID must be a host name without a scheme or path")
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise PasskeyConfigurationError("WORKAMA_PASSKEY_ORIGIN must be an absolute HTTP(S) origin without credentials, query, fragment, or path")
    try:
        timeout_ms = int(os.getenv("WORKAMA_PASSKEY_TIMEOUT_MS", str(PASSKEY_TIMEOUT_MS)))
    except ValueError as exc:
        raise PasskeyConfigurationError("WORKAMA_PASSKEY_TIMEOUT_MS must be an integer") from exc
    if not 30_000 <= timeout_ms <= 300_000:
        raise PasskeyConfigurationError("WORKAMA_PASSKEY_TIMEOUT_MS must be between 30000 and 300000")
    return PasskeyConfig(rp_id=rp_id, origin=origin, rp_name=rp_name, timeout_ms=timeout_ms)


def _load_webauthn() -> Any:
    try:
        module = importlib.import_module("webauthn")
    except ImportError as exc:
        raise PasskeyConfigurationError(
            "Passkey WebAuthn support is unavailable: install a compatible py-webauthn package"
        ) from exc
    required = ("generate_registration_options", "generate_authentication_options", "verify_registration_response", "verify_authentication_response")
    if any(not hasattr(module, name) for name in required):
        raise PasskeyConfigurationError("Installed WebAuthn library does not expose the required verification API")
    return module


def _load_structs(module: Any) -> Any:
    try:
        return importlib.import_module("webauthn.helpers.structs")
    except ImportError:
        structs = getattr(module, "helpers", None)
        if structs and hasattr(structs, "structs"):
            return structs.structs
        raise PasskeyConfigurationError("Installed WebAuthn library is missing helper structures")


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return _b64url_encode(value)
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump(mode="json"))
    if hasattr(value, "dict"):
        return _json_safe(value.dict())
    return str(value)


def _options_to_dict(module: Any, options: Any) -> dict[str, Any]:
    try:
        serializer = getattr(module, "options_to_json")
        rendered = serializer(options)
        value = json.loads(rendered) if isinstance(rendered, str) else rendered
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        value = _json_safe(options)
    if not isinstance(value, dict):
        raise PasskeyConfigurationError("WebAuthn library returned invalid options")
    return value


def _parse_credential(module: Any, payload: dict[str, Any], credential_type: str) -> Any:
    class_name = "RegistrationCredential" if credential_type == "registration" else "AuthenticationCredential"
    credential_class = getattr(module, class_name, None)
    if credential_class is None:
        try:
            structs = _load_structs(module)
            credential_class = getattr(structs, class_name)
        except (AttributeError, PasskeyConfigurationError) as exc:
            raise PasskeyConfigurationError(f"WebAuthn library cannot parse {credential_type} credentials") from exc
    try:
        if hasattr(credential_class, "model_validate"):
            return credential_class.model_validate(payload)
        if hasattr(credential_class, "parse_obj"):
            return credential_class.parse_obj(payload)
        if hasattr(credential_class, "parse_raw"):
            return credential_class.parse_raw(json.dumps(payload))
    except Exception as exc:
        raise WebAuthnVerificationError("Passkey credential payload is invalid") from exc
    raise PasskeyConfigurationError(f"WebAuthn library cannot parse {credential_type} credentials")


class WebAuthnAdapter:
    """Small compatibility boundary around py-webauthn.

    The API is intentionally strict: a missing or incompatible verifier raises a
    configuration error; it never falls back to comparing a client-provided token.
    """

    def __init__(self, module: Any | None = None):
        self.module = module or _load_webauthn()

    def registration_options(
        self,
        config: PasskeyConfig,
        *,
        user_id: bytes,
        user_name: str,
        display_name: str,
        challenge: bytes,
        exclude_credentials: list[bytes],
    ) -> dict[str, Any]:
        structs = _load_structs(self.module)
        try:
            uv = structs.UserVerificationRequirement.REQUIRED
            descriptors = [structs.PublicKeyCredentialDescriptor(id=item) for item in exclude_credentials]
            parameters = inspect.signature(self.module.generate_registration_options).parameters
            if "rp" in parameters:
                rp = structs.PublicKeyCredentialRpEntity(id=config.rp_id, name=config.rp_name)
                user = structs.PublicKeyCredentialUserEntity(id=user_id, name=user_name, display_name=display_name)
                options = self.module.generate_registration_options(
                    rp=rp,
                    user=user,
                    challenge=challenge,
                    timeout=config.timeout_ms,
                    user_verification=uv,
                    exclude_credentials=descriptors,
                )
            else:
                selection_type = getattr(structs, "AuthenticatorSelectionCriteria", None)
                if selection_type is None:
                    raise PasskeyConfigurationError("Installed WebAuthn library is missing authenticator selection support")
                selection = selection_type(user_verification=uv)
                options = self.module.generate_registration_options(
                    rp_id=config.rp_id,
                    rp_name=config.rp_name,
                    user_name=user_name,
                    user_id=user_id,
                    user_display_name=display_name,
                    challenge=challenge,
                    timeout=config.timeout_ms,
                    authenticator_selection=selection,
                    exclude_credentials=descriptors,
                )
        except TypeError as exc:
            raise PasskeyConfigurationError("Installed WebAuthn library has an incompatible registration API") from exc
        except PasskeyConfigurationError:
            raise
        except Exception as exc:
            raise PasskeyConfigurationError("Unable to create Passkey registration options") from exc
        return _options_to_dict(self.module, options)

    def authentication_options(
        self,
        config: PasskeyConfig,
        *,
        challenge: bytes,
        allow_credentials: list[bytes],
    ) -> dict[str, Any]:
        structs = _load_structs(self.module)
        try:
            uv = structs.UserVerificationRequirement.REQUIRED
            descriptors = [structs.PublicKeyCredentialDescriptor(id=item) for item in allow_credentials]
            options = self.module.generate_authentication_options(
                rp_id=config.rp_id,
                challenge=challenge,
                timeout=config.timeout_ms,
                user_verification=uv,
                allow_credentials=descriptors,
            )
        except TypeError as exc:
            raise PasskeyConfigurationError("Installed WebAuthn library has an incompatible authentication API") from exc
        except Exception as exc:
            raise PasskeyConfigurationError("Unable to create Passkey authentication options") from exc
        return _options_to_dict(self.module, options)

    def verify_registration(
        self,
        config: PasskeyConfig,
        *,
        challenge: bytes,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        credential = _parse_credential(self.module, payload, "registration")
        try:
            verified = self.module.verify_registration_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=config.rp_id,
                expected_origin=config.origin,
                require_user_verification=True,
            )
        except TypeError as exc:
            raise PasskeyConfigurationError("Installed WebAuthn library has an incompatible registration verifier") from exc
        except Exception as exc:
            raise WebAuthnVerificationError("Passkey registration signature or origin verification failed") from exc
        return _verified_registration(verified, payload)

    def verify_authentication(
        self,
        config: PasskeyConfig,
        *,
        challenge: bytes,
        payload: dict[str, Any],
        public_key: bytes,
        sign_count: int,
    ) -> int:
        credential = _parse_credential(self.module, payload, "authentication")
        try:
            verified = self.module.verify_authentication_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=config.rp_id,
                expected_origin=config.origin,
                credential_public_key=public_key,
                credential_current_sign_count=sign_count,
                require_user_verification=True,
            )
        except TypeError as exc:
            raise PasskeyConfigurationError("Installed WebAuthn library has an incompatible authentication verifier") from exc
        except Exception as exc:
            raise WebAuthnVerificationError("Passkey assertion signature or origin verification failed") from exc
        next_count = getattr(verified, "new_sign_count", getattr(verified, "sign_count", None))
        if next_count is None:
            raise PasskeyConfigurationError("WebAuthn verifier did not return a sign count")
        return int(next_count)


def _verified_registration(verified: Any, payload: dict[str, Any]) -> dict[str, Any]:
    credential_id = getattr(verified, "credential_id", None)
    public_key = getattr(verified, "credential_public_key", None)
    if credential_id is None:
        credential_id = _b64url_decode(str(payload.get("rawId") or payload.get("id") or ""))
    if public_key is None:
        raise PasskeyConfigurationError("WebAuthn verifier did not return a credential public key")
    try:
        credential_id_bytes = bytes(credential_id)
        public_key_bytes = bytes(public_key)
    except (TypeError, ValueError) as exc:
        raise PasskeyConfigurationError("WebAuthn verifier returned invalid credential material") from exc
    if not credential_id_bytes or not public_key_bytes:
        raise PasskeyConfigurationError("WebAuthn verifier returned empty credential material")
    transports = payload.get("response", {}).get("transports", []) if isinstance(payload.get("response"), dict) else []
    if not isinstance(transports, list):
        transports = []
    return {
        "credential_id": _b64url_encode(credential_id_bytes),
        "public_key": public_key_bytes,
        "sign_count": max(0, int(getattr(verified, "sign_count", 0) or 0)),
        "aaguid": str(getattr(verified, "aaguid", "") or "") or None,
        "transports": [str(item) for item in transports if isinstance(item, str)][:8],
    }


def credential_id_from_payload(payload: dict[str, Any]) -> str:
    raw_id = payload.get("rawId") or payload.get("id")
    try:
        decoded = _b64url_decode(str(raw_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Passkey credential id is invalid") from exc
    if not decoded:
        raise HTTPException(status_code=400, detail="Passkey credential id is empty")
    return _b64url_encode(decoded)


def passkey_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "credential_id": row["credential_id"],
        "transports": list(row.get("transports") or []),
        "aaguid": row.get("aaguid"),
        "sign_count": int(row.get("sign_count") or 0),
        "created_at": row.get("created_at"),
        "last_used_at": row.get("last_used_at"),
        "revoked_at": row.get("revoked_at"),
    }


def device_session_view(row: dict[str, Any]) -> dict[str, Any]:
    revoked_at = row.get("revoked_at")
    expires_at = row.get("expires_at")
    state = "revoked" if revoked_at else "expired" if expires_at and expires_at <= _now() else "active"
    return {
        "session_id": row["id"],
        "status": state,
        "created_at": row.get("created_at"),
        "expires_at": expires_at,
        "revoked_at": revoked_at,
    }


def _public_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PasskeyConfigurationError):
        return HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc))
    if isinstance(exc, WebAuthnVerificationError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Passkey response is invalid")


async def _challenge(conn, challenge_id: str, flow: str, *, for_update: bool = False) -> dict[str, Any]:
    lock = " FOR UPDATE" if for_update else ""
    result = await conn.execute(
        f"""
        SELECT id, user_id, workspace_id, flow, challenge, rp_id, origin,
               expires_at, consumed_at, created_at
        FROM id_passkey_challenge
        WHERE id = %s AND flow = %s{lock}
        """,
        (challenge_id, flow),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=400, detail="Passkey challenge is invalid")
    _require_usable_challenge(row, flow)
    return row


async def _consume_challenge(conn, challenge_id: str) -> bool:
    result = await conn.execute(
        """
        UPDATE id_passkey_challenge
        SET consumed_at = now()
        WHERE id = %s AND consumed_at IS NULL AND expires_at > now()
        RETURNING id
        """,
        (challenge_id,),
    )
    return bool(await result.fetchone())


def _config_and_adapter() -> tuple[PasskeyConfig, WebAuthnAdapter]:
    try:
        return _config_from_environment(), WebAuthnAdapter()
    except (PasskeyConfigurationError, ImportError) as exc:
        raise _public_error(exc)


@router.get("/passkeys")
async def list_passkeys(actor: Actor = Depends(get_actor)):
    _require_user_actor(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, name, credential_id, transports, aaguid, sign_count,
                   created_at, last_used_at, revoked_at
            FROM id_passkey
            WHERE user_id = %s AND workspace_id = %s
            ORDER BY created_at DESC
            """,
            (actor.user_id, actor.workspace_id),
        )
        rows = await result.fetchall()
    return {"items": [passkey_view(row) for row in rows]}


@router.post("/passkeys/registration/options")
async def registration_options(body: RegistrationOptionsRequest, actor: Actor = Depends(get_actor)):
    _require_user_actor(actor)
    config, adapter = _config_and_adapter()
    challenge = secrets.token_bytes(32)
    challenge_id = new_id("pck")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT credential_id FROM id_passkey WHERE user_id = %s AND workspace_id = %s AND revoked_at IS NULL",
            (actor.user_id, actor.workspace_id),
        )
        existing = [str(row["credential_id"]) for row in await result.fetchall()]
        try:
            exclude = [_b64url_decode(value) for value in existing]
        except ValueError as exc:
            raise HTTPException(status_code=500, detail="Stored Passkey credential id is invalid") from exc
        try:
            options = adapter.registration_options(
                config,
                user_id=actor.user_id.encode("utf-8"),
                user_name=actor.email,
                display_name=actor.display_name,
                challenge=challenge,
                exclude_credentials=exclude,
            )
        except Exception as exc:
            raise _public_error(exc)
        await conn.execute(
            """
            INSERT INTO id_passkey_challenge(
                id, user_id, workspace_id, flow, challenge, rp_id, origin, expires_at
            ) VALUES (%s, %s, %s, 'registration', %s, %s, %s, %s)
            """,
            (challenge_id, actor.user_id, actor.workspace_id, _b64url_encode(challenge), config.rp_id, config.origin, _now() + timedelta(seconds=PASSKEY_CHALLENGE_TTL_SECONDS)),
        )
        await conn.commit()
    return {"challenge_id": challenge_id, "expires_in": PASSKEY_CHALLENGE_TTL_SECONDS, "options": options}


@router.post("/passkeys/registration/complete", status_code=status.HTTP_201_CREATED)
async def registration_complete(body: RegistrationCompleteRequest, actor: Actor = Depends(get_actor)):
    _require_user_actor(actor)
    config, adapter = _config_and_adapter()
    async with pool.connection() as conn:
        challenge = await _challenge(conn, body.challenge_id, "registration")
        if challenge.get("user_id") != actor.user_id or challenge.get("workspace_id") != actor.workspace_id:
            raise HTTPException(status_code=404, detail="Passkey challenge not found")
        if challenge["rp_id"] != config.rp_id or challenge["origin"] != config.origin:
            raise HTTPException(status_code=400, detail="Passkey challenge configuration changed")
        try:
            verified = adapter.verify_registration(config, challenge=_b64url_decode(challenge["challenge"]), payload=body.credential)
        except Exception as exc:
            raise _public_error(exc)
        duplicate = await conn.execute(
            "SELECT id FROM id_passkey WHERE credential_id = %s",
            (verified["credential_id"],),
        )
        if await duplicate.fetchone():
            raise HTTPException(status_code=409, detail="Passkey credential is already registered")
        if not await _consume_challenge(conn, body.challenge_id):
            raise HTTPException(status_code=400, detail="Passkey challenge is invalid, expired, or already used")
        await conn.execute(
            """
            INSERT INTO id_passkey(
                id, user_id, workspace_id, credential_id, public_key, sign_count,
                transports, aaguid, name
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (new_id("pky"), actor.user_id, actor.workspace_id, verified["credential_id"], verified["public_key"], verified["sign_count"], verified["transports"], verified["aaguid"], body.name.strip()),
        )
        await conn.commit()
        result = await conn.execute(
            """
            SELECT id, name, credential_id, transports, aaguid, sign_count,
                   created_at, last_used_at, revoked_at
            FROM id_passkey WHERE credential_id = %s AND user_id = %s AND workspace_id = %s
            """,
            (verified["credential_id"], actor.user_id, actor.workspace_id),
        )
        row = await result.fetchone()
    return passkey_view(row)


@router.post("/passkeys/authentication/options")
async def authentication_options(body: AuthenticationOptionsRequest):
    config, adapter = _config_and_adapter()
    challenge = secrets.token_bytes(32)
    challenge_id = new_id("pck")
    user_id: str | None = None
    allow_credentials: list[bytes] = []
    async with pool.connection() as conn:
        if body.email:
            result = await conn.execute(
                "SELECT id FROM id_user WHERE email = %s AND status = 'active'",
                (str(body.email).lower(),),
            )
            user = await result.fetchone()
            if user:
                user_id = str(user["id"])
                result = await conn.execute(
                    "SELECT credential_id FROM id_passkey WHERE user_id = %s AND revoked_at IS NULL",
                    (user_id,),
                )
                for row in await result.fetchall():
                    try:
                        allow_credentials.append(_b64url_decode(str(row["credential_id"])))
                    except ValueError:
                        continue
        try:
            options = adapter.authentication_options(config, challenge=challenge, allow_credentials=allow_credentials)
        except Exception as exc:
            raise _public_error(exc)
        await conn.execute(
            """
            INSERT INTO id_passkey_challenge(
                id, user_id, flow, challenge, rp_id, origin, expires_at
            ) VALUES (%s, %s, 'authentication', %s, %s, %s, %s)
            """,
            (challenge_id, user_id, _b64url_encode(challenge), config.rp_id, config.origin, _now() + timedelta(seconds=PASSKEY_CHALLENGE_TTL_SECONDS)),
        )
        await conn.commit()
    return {"challenge_id": challenge_id, "expires_in": PASSKEY_CHALLENGE_TTL_SECONDS, "options": options}


@router.post("/passkeys/authentication/complete")
async def authentication_complete(body: AuthenticationCompleteRequest, response: Response):
    config, adapter = _config_and_adapter()
    credential_id = credential_id_from_payload(body.credential)
    async with pool.connection() as conn:
        challenge = await _challenge(conn, body.challenge_id, "authentication")
        if challenge["rp_id"] != config.rp_id or challenge["origin"] != config.origin:
            raise HTTPException(status_code=400, detail="Passkey challenge configuration changed")
        result = await conn.execute(
            """
            SELECT id, user_id, workspace_id, credential_id, public_key, sign_count,
                   revoked_at
            FROM id_passkey
            WHERE credential_id = %s AND revoked_at IS NULL
              AND (%s IS NULL OR user_id = %s)
            """,
            (credential_id, challenge.get("user_id"), challenge.get("user_id")),
        )
        passkey = await result.fetchone()
        if not passkey:
            raise HTTPException(status_code=401, detail="Passkey assertion is not recognized")
        try:
            next_sign_count = adapter.verify_authentication(
                config,
                challenge=_b64url_decode(challenge["challenge"]),
                payload=body.credential,
                public_key=bytes(passkey["public_key"]),
                sign_count=int(passkey["sign_count"] or 0),
            )
        except Exception as exc:
            raise _public_error(exc)
        if not await _consume_challenge(conn, body.challenge_id):
            raise HTTPException(status_code=400, detail="Passkey challenge is invalid, expired, or already used")
        await conn.execute(
            "UPDATE id_passkey SET sign_count = %s, last_used_at = now() WHERE id = %s AND revoked_at IS NULL",
            (max(int(passkey["sign_count"] or 0), next_sign_count), passkey["id"]),
        )
        user_result = await conn.execute(
            """
            SELECT u.id AS user_id, u.email, u.display_name, u.onboarding_completed,
                   m.workspace_id, m.org_id, m.role
            FROM id_user u JOIN id_member m ON m.user_id = u.id
            WHERE u.id = %s AND m.workspace_id = %s AND u.status = 'active'
            ORDER BY m.created_at ASC LIMIT 1
            """,
            (passkey["user_id"], passkey["workspace_id"]),
        )
        user = await user_result.fetchone()
    if not user:
        raise HTTPException(status_code=401, detail="Passkey owner is unavailable")
    session = await _issue_session(response, user["user_id"], user["workspace_id"], user["role"], auth_strength=2)
    session["auth_method"] = "passkey"
    session["user"] = {
        "id": user["user_id"],
        "email": user["email"],
        "display_name": user["display_name"],
        "workspace_id": user["workspace_id"],
        "org_id": user["org_id"],
        "role": user["role"],
        "onboarding_completed": user["onboarding_completed"],
    }
    return session


@router.patch("/passkeys/{passkey_id}")
async def rename_passkey(passkey_id: str, body: PasskeyRenameRequest, actor: Actor = Depends(get_actor)):
    _require_user_actor(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            UPDATE id_passkey SET name = %s
            WHERE id = %s AND user_id = %s AND workspace_id = %s AND revoked_at IS NULL
            RETURNING id, name, credential_id, transports, aaguid, sign_count, created_at, last_used_at, revoked_at
            """,
            (body.name.strip(), passkey_id, actor.user_id, actor.workspace_id),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Passkey not found")
        await conn.commit()
    return passkey_view(row)


@router.post("/passkeys/{passkey_id}/revoke")
async def revoke_passkey(passkey_id: str, body: RevokeRequest, actor: Actor = Depends(get_actor)):
    _require_user_actor(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            UPDATE id_passkey
            SET revoked_at = COALESCE(revoked_at, now()), revoke_reason = %s
            WHERE id = %s AND user_id = %s AND workspace_id = %s
            RETURNING id, name, credential_id, transports, aaguid, sign_count, created_at, last_used_at, revoked_at
            """,
            (body.reason, passkey_id, actor.user_id, actor.workspace_id),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Passkey not found")
        await conn.commit()
    return passkey_view(row)


@router.get("/devices/sessions")
async def list_device_sessions(actor: Actor = Depends(get_actor)):
    _require_user_actor(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT r.id, r.expires_at, r.revoked_at, r.created_at
            FROM id_refresh_token r
            JOIN id_member m ON m.user_id = r.user_id AND m.workspace_id = %s
            WHERE r.user_id = %s
            ORDER BY r.created_at DESC
            """,
            (actor.workspace_id, actor.user_id),
        )
        rows = await result.fetchall()
    return {"items": [device_session_view(row) for row in rows]}


@router.post("/devices/sessions/{session_id}/revoke")
async def revoke_device_session(session_id: str, body: RevokeRequest, actor: Actor = Depends(get_actor)):
    _require_user_actor(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            UPDATE id_refresh_token r
            SET revoked_at = COALESCE(r.revoked_at, now())
            WHERE r.id = %s AND r.user_id = %s
              AND EXISTS (
                  SELECT 1 FROM id_member m
                  WHERE m.user_id = r.user_id AND m.workspace_id = %s
              )
            RETURNING r.id, r.expires_at, r.revoked_at, r.created_at
            """,
            (session_id, actor.user_id, actor.workspace_id),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Device session not found")
        await conn.commit()
    return device_session_view(row)
