from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import quote

import httpx
import jwt
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, EmailStr, Field

from workama_platform.core import (
    Actor,
    actor_payload,
    create_access_token,
    decode_token,
    decrypt_secret,
    encrypt_secret,
    get_actor,
    hash_password,
    hash_secret,
    invalidate_jwt_cache,
    json_dumps,
    new_id,
    pool,
    redis,
    settings,
    verify_password,
)
from workama_platform.modules.auth.service import (
    OAUTH_STATE_TTL_SECONDS,
    build_oauth_authorization_url,
    new_oauth_state,
    new_pkce_verifier,
    oauth_callback_uri,
    oauth_provider_config,
    oauth_state_is_valid,
    next_login_failure,
    pkce_challenge,
    verify_totp,
)

router = APIRouter(prefix="/api/v1", tags=["identity"])


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    display_name: str = Field(min_length=1, max_length=80)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenRequest(BaseModel):
    token: str = Field(min_length=20, max_length=256)


class ResetPasswordRequest(TokenRequest):
    password: str = Field(min_length=10, max_length=128)


class EmailRequest(BaseModel):
    email: EmailStr


class MfaCodeRequest(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")


class MfaChallengeRequest(MfaCodeRequest):
    ticket: str


class MfaDisableRequest(MfaCodeRequest):
    password: str


class OnboardingRequest(BaseModel):
    user_role: str = "individual"
    primary_goal: str = "chat"
    team_size: str = "1"
    data_sensitivity: str = "standard"
    preferred_model: str = "workama-chat"
    notification_preference: str = "in_app"


class PlatformKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    scopes: list[str] = ["platform:read"]
    resource_allowlist: list[str] = Field(default_factory=list, max_length=100)
    expires_at: datetime | None = None


class WorkspaceSettingsRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    settings: dict | None = None


def _refresh_token() -> str:
    return secrets.token_urlsafe(48)


def _auth_token() -> str:
    return secrets.token_urlsafe(48)


def _mfa_ticket(user_id: str, workspace_id: str, role: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": user_id,
            "ws": workspace_id,
            "role": role,
            "type": "mfa",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def _set_refresh_cookie(response: Response, refresh: str) -> None:
    response.set_cookie(
        "workama_refresh",
        refresh,
        httponly=True,
        secure=settings.workama_env.lower() == "production",
        samesite="strict",
        max_age=30 * 24 * 3600,
        path="/api/v1/auth",
    )


def _set_access_cookie(response: Response, access_token: str) -> None:
    response.set_cookie(
        "workama_access",
        access_token,
        httponly=True,
        secure=settings.workama_env.lower() == "production",
        samesite="strict",
        max_age=15 * 60,
        path="/api/v1",
    )


async def _issue_session(
    response: Response,
    user_id: str,
    workspace_id: str,
    role: str,
    *,
    family_id: str | None = None,
    parent_id: str | None = None,
    auth_strength: int = 1,
) -> dict:
    access_token = create_access_token(user_id, workspace_id, role, auth_strength=auth_strength)
    refresh = _refresh_token()
    refresh_id = new_id("rft")
    family = family_id or refresh_id
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO id_refresh_token(id, user_id, workspace_id, token_hash, family_id, parent_id, expires_at, last_used_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            """,
            (
                refresh_id,
                user_id,
                workspace_id,
                hash_secret(refresh),
                family,
                parent_id,
                datetime.now(UTC) + timedelta(days=30),
            ),
        )
        if parent_id:
            await conn.execute(
                "UPDATE id_refresh_token SET rotated_to_id = %s WHERE id = %s",
                (refresh_id, parent_id),
            )
        await conn.commit()
    _set_refresh_cookie(response, refresh)
    _set_access_cookie(response, access_token)
    return {"access_token": access_token, "token_type": "bearer"}


async def _create_one_time_token(user_id: str, token_type: str, ttl: timedelta) -> str:
    raw = _auth_token()
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE id_auth_token SET consumed_at = now() WHERE user_id = %s AND token_type = %s AND consumed_at IS NULL",
            (user_id, token_type),
        )
        await conn.execute(
            "INSERT INTO id_auth_token(id, user_id, token_type, token_hash, expires_at) VALUES (%s, %s, %s, %s, %s)",
            (new_id("atk"), user_id, token_type, hash_secret(raw), datetime.now(UTC) + ttl),
        )
        await conn.commit()
    return raw


def _token_response(accepted: bool, raw: str | None = None) -> dict:
    result = {"accepted": accepted}
    if raw and settings.auth_debug_tokens:
        result["debug_token"] = raw
    return result


def _oauth_config_or_error(provider: str):
    config = oauth_provider_config(provider, settings)
    if config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="E01006 OAuth provider is not supported")
    if not settings.auth_oauth_enabled or not config.configured:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="E01007 OAuth provider is disabled")
    return config


def _oauth_redirect_uri(provider: str) -> str:
    try:
        return oauth_callback_uri(settings.oauth_redirect_base_url, provider)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="E01012 OAuth redirect configuration is invalid",
        ) from exc


class OAuthProviderExchangeError(ValueError):
    """Provider exchange failure with no upstream response disclosure."""


async def _exchange_oauth_profile(
    config,
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, object]:
    if not config.token_url or not config.userinfo_url:
        raise OAuthProviderExchangeError("OAuth provider exchange is not configured")
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
            transport=transport,
        ) as client:
            token_response = await client.post(
                config.token_url,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": config.client_id,
                    "client_secret": config.client_secret,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
                headers={"Accept": "application/json"},
            )
            if token_response.status_code < 200 or token_response.status_code >= 300 or len(token_response.content) > 1_000_000:
                raise OAuthProviderExchangeError("OAuth provider token exchange failed")
            token_payload = token_response.json()
            access_token = token_payload.get("access_token") if isinstance(token_payload, dict) else None
            if not isinstance(access_token, str) or not access_token:
                raise OAuthProviderExchangeError("OAuth provider token response was invalid")
            profile_response = await client.get(
                config.userinfo_url,
                headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
            )
            if profile_response.status_code < 200 or profile_response.status_code >= 300 or len(profile_response.content) > 1_000_000:
                raise OAuthProviderExchangeError("OAuth provider profile request failed")
            profile = profile_response.json()
            if not isinstance(profile, dict):
                raise OAuthProviderExchangeError("OAuth provider profile response was invalid")
            if config.profile_kind == "github" and not profile.get("email"):
                emails_response = await client.get(
                    "https://api.github.com/user/emails",
                    headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
                )
                if emails_response.status_code < 200 or emails_response.status_code >= 300 or len(emails_response.content) > 1_000_000:
                    raise OAuthProviderExchangeError("OAuth provider email profile request failed")
                emails = emails_response.json()
                if isinstance(emails, list):
                    primary = next((item for item in emails if isinstance(item, dict) and item.get("primary") and item.get("verified")), None)
                    if primary:
                        profile = {**profile, "email": primary.get("email"), "email_verified": True}
    except OAuthProviderExchangeError:
        raise
    except (httpx.HTTPError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OAuthProviderExchangeError("OAuth provider exchange failed") from exc
    email = str(profile.get("email") or "").strip().lower()
    verified = profile.get("email_verified") in (True, "true", "True", 1, "1")
    if config.profile_kind == "github" and email:
        verified = True if profile.get("email_verified") is None else verified
    if not email or not verified:
        raise OAuthProviderExchangeError("OAuth provider identity is not verified")
    return {
        "email": email,
        "display_name": str(profile.get("name") or profile.get("login") or email.split("@", 1)[0])[:80],
        "subject": str(profile.get("sub") or profile.get("id") or ""),
    }


async def _complete_oauth_login(response: Response, profile: dict[str, object]) -> dict[str, object]:
    email = str(profile["email"])
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT u.id,u.email,u.display_name,u.onboarding_completed,m.workspace_id,m.org_id,m.role
            FROM id_user u JOIN id_member m ON m.user_id=u.id
            WHERE LOWER(u.email)=LOWER(%s) AND u.status='active'
            ORDER BY m.created_at ASC LIMIT 1
            """,
            (email,),
        )
        user = await result.fetchone()
    if not user:
        raise HTTPException(status_code=403, detail="E01014 OAuth identity is not provisioned")
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
    return session


@router.get("/auth/oauth/{provider}/authorize")
async def oauth_authorize(provider: str):
    """Create a one-time PKCE authorization request for an allowlisted provider."""
    config = _oauth_config_or_error(provider)
    redirect_uri = _oauth_redirect_uri(config.name)
    state = new_oauth_state()
    code_verifier = new_pkce_verifier()
    payload = {
        "provider": config.name,
        "state": state,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
        "issued_at": datetime.now(UTC).timestamp(),
    }
    stored = await redis.set(
        f"auth:oauth:state:{state}",
        json_dumps(payload),
        ex=OAUTH_STATE_TTL_SECONDS,
        nx=True,
    )
    if not stored:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="E01013 Unable to reserve OAuth state")
    try:
        authorization_url = build_oauth_authorization_url(
            config,
            state=state,
            redirect_uri=redirect_uri,
            code_challenge=pkce_challenge(code_verifier),
        )
    except ValueError as exc:
        await redis.delete(f"auth:oauth:state:{state}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="E01012 OAuth redirect configuration is invalid") from exc
    return {
        "provider": config.name,
        "authorization_url": authorization_url,
        "expires_in": OAUTH_STATE_TTL_SECONDS,
    }


@router.get("/auth/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    response: Response,
    code: str | None = Query(default=None, max_length=4096),
    state: str | None = Query(default=None, max_length=256),
    error: str | None = Query(default=None, max_length=128),
):
    """Consume the callback state, exchange the code, and issue a WorkAMA session."""
    config = _oauth_config_or_error(provider)
    if not state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="E01009 OAuth callback parameters are incomplete")
    redirect_uri = _oauth_redirect_uri(config.name)
    raw_state = await redis.getdel(f"auth:oauth:state:{state}")
    if not raw_state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="E01010 OAuth state is invalid or expired")
    try:
        state_payload = json.loads(raw_state)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="E01010 OAuth state is invalid or expired") from exc
    if not oauth_state_is_valid(state_payload, provider=config.name, redirect_uri=redirect_uri, state=state):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="E01010 OAuth state is invalid or expired")
    if error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="E01008 OAuth provider rejected authorization")
    if not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="E01009 OAuth callback parameters are incomplete")
    try:
        profile = await _exchange_oauth_profile(
            config,
            code=code,
            code_verifier=str(state_payload["code_verifier"]),
            redirect_uri=redirect_uri,
        )
        return await _complete_oauth_login(response, profile)
    except OAuthProviderExchangeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="E01011 OAuth provider exchange failed") from exc


@router.post("/auth/register", status_code=201)
async def register(body: RegisterRequest, response: Response):
    email = body.email.lower()
    user_id = new_id("usr")
    org_id = new_id("org")
    workspace_id = new_id("wsp")
    async with pool.connection() as conn:
        exists = await conn.execute("SELECT 1 FROM id_user WHERE email = %s", (email,))
        if await exists.fetchone():
            raise HTTPException(status_code=409, detail="Email is already registered")
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO id_user(id, email, password_hash, display_name, email_verified)
                VALUES (%s, %s, %s, %s, FALSE)
                """,
                (user_id, email, hash_password(body.password), body.display_name.strip()),
            )
            await conn.execute(
                "INSERT INTO id_org(id, name, owner_user_id) VALUES (%s, %s, %s)",
                (org_id, f"{body.display_name.strip()}'s organization", user_id),
            )
            await conn.execute(
                """
                INSERT INTO id_workspace(id, org_id, name, slug)
                VALUES (%s, %s, %s, %s)
                """,
                (workspace_id, org_id, "Personal workspace", "personal"),
            )
            await conn.execute(
                """
                INSERT INTO id_member(id, org_id, workspace_id, user_id, role)
                VALUES (%s, %s, %s, %s, 'owner')
                """,
                (new_id("mem"), org_id, workspace_id, user_id),
            )
            await conn.execute(
                """
                INSERT INTO bill_account(id, workspace_id, granted_balance)
                VALUES (%s, %s, 500)
                """,
                (new_id("bacc"), workspace_id),
            )
            await conn.execute(
                """
                INSERT INTO bill_credit_grant(
                    id,workspace_id,source,period_start,expires_at,initial_amount,remaining_amount,idempotency_key
                ) VALUES (
                    %s,%s,'initial',date_trunc('month',now()),date_trunc('month',now()) + interval '1 month',500,500,%s
                )
                ON CONFLICT(workspace_id,idempotency_key) DO NOTHING
                """,
                (new_id("grant"), workspace_id, f"initial:{workspace_id}"),
            )
            await conn.execute(
                """
                INSERT INTO gw_channel(id, workspace_id, name, provider, base_url, models, last_health)
                VALUES (%s, %s, 'WorkAMA Local', 'mock', 'mock://local', ARRAY['workama-chat', 'workama-embed'], 'healthy')
                """,
                (new_id("chn"), workspace_id),
            )
            await conn.execute(
                """
                INSERT INTO gw_model_price(workspace_id, model, input_per_million, output_per_million, markup_percent)
                VALUES (%s, 'workama-chat', 1, 2, 10)
                """,
                (workspace_id,),
            )
    verification_token = await _create_one_time_token(
        user_id, "email_verify", timedelta(hours=24)
    )
    return {
        **_token_response(True, verification_token),
        "verification_required": True,
        "user": {
        "id": user_id,
        "email": email,
        "display_name": body.display_name.strip(),
        "workspace_id": workspace_id,
        "org_id": org_id,
        "role": "owner",
        "onboarding_completed": False,
        },
    }


@router.post("/auth/login")
async def login(body: LoginRequest, response: Response):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT u.id, u.email, u.password_hash, u.display_name, u.onboarding_completed,
                   u.email_verified, u.failed_login_count, u.locked_until,
                   m.workspace_id, m.org_id, m.role,
                   EXISTS(
                       SELECT 1 FROM id_mfa_factor f
                       WHERE f.user_id = u.id AND f.confirmed_at IS NOT NULL AND f.disabled_at IS NULL
                   ) AS mfa_enabled
            FROM id_user u
            JOIN id_member m ON m.user_id = u.id
            WHERE u.email = %s AND u.status = 'active'
            ORDER BY m.created_at ASC LIMIT 1
            """,
            (body.email.lower(),),
        )
        row = await result.fetchone()
        if row and row["locked_until"] and row["locked_until"] > datetime.now(UTC):
            raise HTTPException(status_code=401, detail="Email or password is incorrect")
        valid_password = bool(row and verify_password(row["password_hash"], body.password))
        if row and not valid_password:
            failures, locked_until = next_login_failure(row["failed_login_count"])
            await conn.execute(
                "UPDATE id_user SET failed_login_count = %s, locked_until = %s, updated_at = now() WHERE id = %s",
                (failures, locked_until, row["id"]),
            )
            await conn.commit()
    if not row or not valid_password:
        raise HTTPException(status_code=401, detail="Email or password is incorrect")
    if not row["email_verified"]:
        raise HTTPException(status_code=403, detail="Email verification required")
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE id_user SET failed_login_count = 0, locked_until = NULL, updated_at = now() WHERE id = %s",
            (row["id"],),
        )
        await conn.commit()
    if row["mfa_enabled"]:
        return {
            "mfa_required": True,
            "mfa_ticket": _mfa_ticket(row["id"], row["workspace_id"], row["role"]),
        }
    session = await _issue_session(response, row["id"], row["workspace_id"], row["role"])
    session["user"] = {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "workspace_id": row["workspace_id"],
        "org_id": row["org_id"],
        "role": row["role"],
        "onboarding_completed": row["onboarding_completed"],
    }
    return session


@router.post("/auth/refresh")
async def refresh(
    response: Response,
    workama_refresh: Annotated[str | None, Cookie()] = None,
):
    if not workama_refresh:
        raise HTTPException(status_code=401, detail="Refresh token required")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT r.id, r.user_id, r.family_id, r.revoked_at, r.expires_at,
                   r.workspace_id, m.role, r.last_used_at
            FROM id_refresh_token r
            JOIN id_member m ON m.user_id = r.user_id AND m.workspace_id = r.workspace_id
            WHERE r.token_hash = %s
            FOR UPDATE
            """,
            (hash_secret(workama_refresh),),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="Refresh token invalid")
        if row["revoked_at"] is not None:
            await conn.execute(
                "UPDATE id_refresh_token SET revoked_at = COALESCE(revoked_at, now()) WHERE family_id = %s",
                (row["family_id"],),
            )
            await conn.commit()
            response.delete_cookie("workama_refresh", path="/api/v1/auth")
            response.delete_cookie("workama_access", path="/api/v1")
            raise HTTPException(status_code=401, detail="Refresh token reuse detected")
        if row["expires_at"] <= datetime.now(UTC):
            raise HTTPException(status_code=401, detail="Refresh token invalid")
        if row["last_used_at"] and row["last_used_at"] <= datetime.now(UTC) - timedelta(days=7):
            await conn.execute("UPDATE id_refresh_token SET revoked_at = now() WHERE id = %s", (row["id"],))
            await conn.commit()
            response.delete_cookie("workama_refresh", path="/api/v1/auth")
            response.delete_cookie("workama_access", path="/api/v1")
            raise HTTPException(status_code=401, detail="Refresh token idle lifetime exceeded")
        await conn.execute(
            "UPDATE id_refresh_token SET revoked_at = now(), last_used_at = now() WHERE id = %s", (row["id"],)
        )
        await conn.commit()
    return await _issue_session(
        response,
        row["user_id"],
        row["workspace_id"],
        row["role"],
        family_id=row["family_id"],
        parent_id=row["id"],
    )


@router.post("/auth/logout", status_code=204)
async def logout(
    response: Response,
    workama_refresh: Annotated[str | None, Cookie()] = None,
    workama_access: Annotated[str | None, Cookie()] = None,
    authorization: Annotated[str | None, Header()] = None,
):
    # 性能优化：登出时失效 JWT 验签缓存，避免缓存窗口内旧 token 仍被放行
    access_token = None
    if authorization and authorization.lower().startswith("bearer "):
        access_token = authorization.split(" ", 1)[1].strip()
    elif workama_access:
        access_token = workama_access
    if access_token:
        await invalidate_jwt_cache(access_token)
    if workama_refresh:
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE id_refresh_token SET revoked_at = now() WHERE token_hash = %s",
                (hash_secret(workama_refresh),),
            )
            await conn.commit()
    response.delete_cookie("workama_refresh", path="/api/v1/auth")
    response.delete_cookie("workama_access", path="/api/v1")


@router.get("/auth/me")
async def me(actor: Annotated[Actor, Depends(get_actor)]):
    return actor_payload(actor)


@router.get("/me/capabilities")
async def effective_capabilities(actor: Annotated[Actor, Depends(get_actor)]):
    return {"actor_type": actor.actor_type, "role": actor.role, "capabilities": list(actor.capabilities), "auth_strength": actor.auth_strength}


@router.post("/auth/onboarding")
async def onboarding(
    body: OnboardingRequest, actor: Annotated[Actor, Depends(get_actor)]
):
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE id_user SET profile = %s::jsonb, onboarding_completed = TRUE, updated_at = now() WHERE id = %s",
            (body.model_dump_json(), actor.user_id),
        )
        await conn.commit()
    return {"completed": True}


@router.post("/auth/verify-email")
async def verify_email(body: TokenRequest, response: Response):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT t.id, t.user_id, u.email, u.display_name, u.onboarding_completed,
                   m.workspace_id, m.org_id, m.role
            FROM id_auth_token t JOIN id_user u ON u.id = t.user_id
            JOIN id_member m ON m.user_id = u.id
            WHERE t.token_hash = %s AND t.token_type = 'email_verify'
              AND t.consumed_at IS NULL AND t.expires_at > now()
            ORDER BY m.created_at ASC LIMIT 1
            """,
            (hash_secret(body.token),),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="Verification token invalid or expired")
        async with conn.transaction():
            await conn.execute("UPDATE id_auth_token SET consumed_at = now() WHERE id = %s", (row["id"],))
            await conn.execute("UPDATE id_user SET email_verified = TRUE, updated_at = now() WHERE id = %s", (row["user_id"],))
    session = await _issue_session(response, row["user_id"], row["workspace_id"], row["role"])
    session["user"] = {
        "id": row["user_id"], "email": row["email"], "display_name": row["display_name"],
        "workspace_id": row["workspace_id"], "org_id": row["org_id"], "role": row["role"],
        "onboarding_completed": row["onboarding_completed"],
    }
    return session


@router.post("/auth/resend-verification")
async def resend_verification(body: EmailRequest):
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT id, email_verified FROM id_user WHERE email = %s AND status = 'active'",
            (body.email.lower(),),
        )
        user = await result.fetchone()
    raw = None
    if user and not user["email_verified"]:
        raw = await _create_one_time_token(user["id"], "email_verify", timedelta(hours=24))
    return _token_response(True, raw)


@router.post("/auth/forgot-password")
async def forgot_password(body: EmailRequest):
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT id FROM id_user WHERE email = %s AND status = 'active'",
            (body.email.lower(),),
        )
        user = await result.fetchone()
    raw = None
    if user:
        raw = await _create_one_time_token(user["id"], "password_reset", timedelta(hours=1))
    return _token_response(True, raw)


@router.post("/auth/reset-password")
async def reset_password(body: ResetPasswordRequest):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, user_id FROM id_auth_token
            WHERE token_hash = %s AND token_type = 'password_reset'
              AND consumed_at IS NULL AND expires_at > now()
            FOR UPDATE
            """,
            (hash_secret(body.token),),
        )
        token = await result.fetchone()
        if not token:
            raise HTTPException(status_code=400, detail="Reset token invalid or expired")
        async with conn.transaction():
            await conn.execute("UPDATE id_auth_token SET consumed_at = now() WHERE id = %s", (token["id"],))
            await conn.execute(
                "UPDATE id_user SET password_hash = %s, failed_login_count = 0, locked_until = NULL, updated_at = now() WHERE id = %s",
                (hash_password(body.password), token["user_id"]),
            )
            await conn.execute(
                "UPDATE id_refresh_token SET revoked_at = COALESCE(revoked_at, now()) WHERE user_id = %s",
                (token["user_id"],),
            )
    return {"reset": True}


@router.get("/auth/security")
async def auth_security(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT confirmed_at, disabled_at FROM id_mfa_factor
            WHERE user_id = %s AND factor_type = 'totp'
            """,
            (actor.user_id,),
        )
        factor = await result.fetchone()
        sessions = await conn.execute(
            "SELECT count(*) AS count FROM id_refresh_token WHERE user_id = %s AND revoked_at IS NULL AND expires_at > now()",
            (actor.user_id,),
        )
        active_sessions = (await sessions.fetchone())["count"]
    return {
        "mfa_enabled": bool(factor and factor["confirmed_at"] and not factor["disabled_at"]),
        "active_sessions": active_sessions,
    }


@router.post("/auth/mfa/setup")
async def mfa_setup(actor: Annotated[Actor, Depends(get_actor)]):
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Owner or Admin role required")
    secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    factor_id = new_id("mfa")
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO id_mfa_factor(id, user_id, factor_type, secret_enc, confirmed_at, disabled_at)
            VALUES (%s, %s, 'totp', %s, NULL, NULL)
            ON CONFLICT(user_id, factor_type) DO UPDATE SET
              secret_enc = EXCLUDED.secret_enc, confirmed_at = NULL, disabled_at = NULL, created_at = now()
            """,
            (factor_id, actor.user_id, encrypt_secret(secret)),
        )
        await conn.commit()
    label = quote(f"WorkAMA:{actor.email}")
    issuer = quote("WorkAMA")
    return {
        "secret": secret,
        "otpauth_uri": f"otpauth://totp/{label}?secret={secret}&issuer={issuer}&algorithm=SHA1&digits=6&period=30",
    }


@router.post("/auth/mfa/confirm")
async def mfa_confirm(
    body: MfaCodeRequest, actor: Annotated[Actor, Depends(get_actor)]
):
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT id, secret_enc FROM id_mfa_factor WHERE user_id = %s AND factor_type = 'totp' AND disabled_at IS NULL",
            (actor.user_id,),
        )
        factor = await result.fetchone()
        secret = decrypt_secret(factor["secret_enc"]) if factor else None
        if not factor or not secret or not verify_totp(secret, body.code):
            raise HTTPException(status_code=400, detail="Verification code is invalid")
        await conn.execute(
            "UPDATE id_mfa_factor SET confirmed_at = now() WHERE id = %s",
            (factor["id"],),
        )
        await conn.commit()
    return {"mfa_enabled": True}


@router.post("/auth/mfa/challenge")
async def mfa_challenge(body: MfaChallengeRequest, response: Response):
    payload = decode_token(body.ticket, expected_type="mfa")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT f.secret_enc, m.workspace_id, m.role
            FROM id_mfa_factor f JOIN id_member m ON m.user_id = f.user_id
            WHERE f.user_id = %s AND m.workspace_id = %s AND f.factor_type = 'totp'
              AND f.confirmed_at IS NOT NULL AND f.disabled_at IS NULL
            """,
            (payload["sub"], payload["ws"]),
        )
        factor = await result.fetchone()
    secret = decrypt_secret(factor["secret_enc"]) if factor else None
    if not factor or not secret or not verify_totp(secret, body.code):
        raise HTTPException(status_code=401, detail="Verification code is invalid")
    return await _issue_session(
        response, payload["sub"], factor["workspace_id"], factor["role"], auth_strength=2
    )


@router.post("/auth/mfa/disable")
async def mfa_disable(
    body: MfaDisableRequest, actor: Annotated[Actor, Depends(get_actor)]
):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT u.password_hash, f.id, f.secret_enc
            FROM id_user u JOIN id_mfa_factor f ON f.user_id = u.id
            WHERE u.id = %s AND f.factor_type = 'totp'
              AND f.confirmed_at IS NOT NULL AND f.disabled_at IS NULL
            """,
            (actor.user_id,),
        )
        factor = await result.fetchone()
        secret = decrypt_secret(factor["secret_enc"]) if factor else None
        if (
            not factor
            or not secret
            or not verify_password(factor["password_hash"], body.password)
            or not verify_totp(secret, body.code)
        ):
            raise HTTPException(status_code=400, detail="Password or verification code is invalid")
        await conn.execute(
            "UPDATE id_mfa_factor SET disabled_at = now() WHERE id = %s",
            (factor["id"],),
        )
        await conn.commit()
    return {"mfa_enabled": False}


@router.get("/members")
async def list_members(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT u.id, u.email, u.display_name, m.role, m.created_at
            FROM id_member m JOIN id_user u ON u.id = m.user_id
            WHERE m.workspace_id = %s ORDER BY m.created_at
            """,
            (actor.workspace_id,),
        )
        return {"items": await result.fetchall()}


@router.get("/workspace")
async def get_workspace(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT id, org_id, name, slug, settings, created_at FROM id_workspace WHERE id = %s",
            (actor.workspace_id,),
        )
        return await result.fetchone()


@router.patch("/workspace")
async def update_workspace(
    body: WorkspaceSettingsRequest, actor: Annotated[Actor, Depends(get_actor)]
):
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")
    async with pool.connection() as conn:
        if body.name:
            await conn.execute(
                "UPDATE id_workspace SET name = %s WHERE id = %s",
                (body.name, actor.workspace_id),
            )
        if body.settings is not None:
            await conn.execute(
                "UPDATE id_workspace SET settings = %s::jsonb WHERE id = %s",
                (json_dumps(body.settings), actor.workspace_id),
            )
        await conn.commit()
    return await get_workspace(actor)


@router.get("/api-keys")
async def list_platform_keys(actor: Annotated[Actor, Depends(get_actor)]):
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, name, last_four, scopes, resource_allowlist, expires_at, last_used_at, revoked_at, created_at,
                   CASE WHEN revoked_at IS NOT NULL THEN 'revoked' WHEN expires_at<=now() THEN 'expired' ELSE 'active' END status
            FROM id_api_key WHERE workspace_id = %s ORDER BY created_at DESC
            """,
            (actor.workspace_id,),
        )
        return {"items": await result.fetchall()}


@router.post("/api-keys", status_code=201)
async def create_platform_key(
    body: PlatformKeyRequest, actor: Annotated[Actor, Depends(get_actor)]
):
    if actor.actor_type != "user" or actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")
    allowed = {"platform:read", "platform:write", "platform:*", "billing:read", "gateway_channel:read", "gateway_channel:write", "session:read", "session:write", "operation:read"}
    if not body.scopes or any(scope not in allowed for scope in body.scopes):
        raise HTTPException(status_code=400, detail="One or more API key scopes are not allowed")
    if body.expires_at and body.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=400, detail="API key expiry must be in the future")
    raw = "sk-wama-" + secrets.token_urlsafe(36)[:43]
    key_id = new_id("pak")
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO id_api_key(id, workspace_id, actor_user_id, name, key_hash, last_four, scopes, resource_allowlist, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                key_id,
                actor.workspace_id,
                actor.user_id,
                body.name,
                hash_secret(raw),
                raw[-4:],
                body.scopes,
                body.resource_allowlist,
                body.expires_at,
            ),
        )
        await conn.commit()
    return {"id": key_id, "name": body.name, "key": raw, "last_four": raw[-4:], "scopes": body.scopes, "resource_allowlist": body.resource_allowlist, "expires_at": body.expires_at}


@router.delete("/api-keys/{key_id}", status_code=204)
async def revoke_platform_key(
    key_id: str, actor: Annotated[Actor, Depends(get_actor)]
):
    if actor.actor_type != "user" or actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")
    async with pool.connection() as conn:
        result = await conn.execute(
            "UPDATE id_api_key SET revoked_at = now() WHERE id = %s AND workspace_id = %s",
            (key_id, actor.workspace_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="API key not found")
        await conn.commit()
