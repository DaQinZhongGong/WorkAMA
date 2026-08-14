from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Mapping
from urllib.parse import urlencode


OAUTH_STATE_TTL_SECONDS = 600


@dataclass(frozen=True)
class OAuthProviderConfig:
    name: str
    client_id: str
    client_secret: str
    authorization_url: str
    scopes: tuple[str, ...]
    token_url: str = ""
    userinfo_url: str = ""
    profile_kind: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.authorization_url)


def oauth_provider_config(provider: str, settings: object) -> OAuthProviderConfig | None:
    """Return the allowlisted social provider configuration without exposing secrets."""
    normalized = provider.strip().lower()
    if normalized == "github":
        return OAuthProviderConfig(
            name=normalized,
            client_id=str(getattr(settings, "github_oauth_client_id", "")),
            client_secret=str(getattr(settings, "github_oauth_client_secret", "")),
            authorization_url=str(
                getattr(settings, "github_oauth_authorization_url", "https://github.com/login/oauth/authorize")
            ),
            scopes=("read:user", "user:email"),
            token_url="https://github.com/login/oauth/access_token",
            userinfo_url="https://api.github.com/user",
            profile_kind="github",
        )
    if normalized == "google":
        return OAuthProviderConfig(
            name=normalized,
            client_id=str(getattr(settings, "google_oauth_client_id", "")),
            client_secret=str(getattr(settings, "google_oauth_client_secret", "")),
            authorization_url=str(
                getattr(settings, "google_oauth_authorization_url", "https://accounts.google.com/o/oauth2/v2/auth")
            ),
            scopes=("openid", "email", "profile"),
            token_url="https://oauth2.googleapis.com/token",
            userinfo_url="https://openidconnect.googleapis.com/v1/userinfo",
            profile_kind="oidc",
        )
    return None


def new_oauth_state() -> str:
    return secrets.token_urlsafe(32)


def new_pkce_verifier() -> str:
    # RFC 7636 requires a 43-128 character high-entropy verifier.
    return secrets.token_urlsafe(64)


def pkce_challenge(verifier: str) -> str:
    if not 43 <= len(verifier) <= 128:
        raise ValueError("PKCE verifier must be between 43 and 128 characters")
    if any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~" for char in verifier):
        raise ValueError("PKCE verifier contains unsupported characters")
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")


def oauth_callback_uri(base_url: str, provider: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if not normalized.startswith(("http://", "https://")) or "#" in normalized:
        raise ValueError("OAuth redirect base URL must be an absolute HTTP(S) URL without a fragment")
    return f"{normalized}/api/v1/auth/oauth/{provider.strip().lower()}/callback"


def build_oauth_authorization_url(
    config: OAuthProviderConfig,
    *,
    state: str,
    redirect_uri: str,
    code_challenge: str,
) -> str:
    if not config.configured:
        raise ValueError("OAuth provider is not configured")
    query_params = {
        'client_id': config.client_id,
        'response_type': 'code',
        'redirect_uri': redirect_uri,
        'scope': ' '.join(config.scopes),
        'state': state,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256',
    }
    return f"{config.authorization_url}?{urlencode(query_params)}"


def oauth_state_is_valid(
    payload: Mapping[str, object] | None,
    *,
    provider: str,
    redirect_uri: str,
    state: str | None = None,
    now: datetime | None = None,
) -> bool:
    if not payload:
        return False
    if not hmac.compare_digest(str(payload.get("provider", "")), provider.strip().lower()):
        return False
    if not hmac.compare_digest(str(payload.get("redirect_uri", "")), redirect_uri):
        return False
    payload_state = str(payload.get("state", ""))
    if state is not None and not hmac.compare_digest(payload_state, state):
        return False
    verifier = str(payload.get("code_verifier", ""))
    if len(payload_state) < 20 or not verifier:
        return False
    try:
        issued_at = float(payload["issued_at"])
    except (KeyError, TypeError, ValueError):
        return False
    age = (now or datetime.now(UTC)).timestamp() - issued_at
    return -60 <= age <= OAUTH_STATE_TTL_SECONDS


def auth_token_is_usable(
    expires_at: datetime, consumed_at: datetime | None, now: datetime | None = None
) -> bool:
    current = now or datetime.now(UTC)
    return consumed_at is None and expires_at > current


def next_login_failure(
    current_failures: int, now: datetime | None = None
) -> tuple[int, datetime | None]:
    current = now or datetime.now(UTC)
    failures = current_failures + 1
    return failures, current + timedelta(minutes=15) if failures >= 5 else None


def _counter(at: datetime, period: int = 30) -> int:
    return int(at.timestamp()) // period


def _totp_for_counter(secret: str, counter: int, digits: int = 6) -> str:
    normalized = secret.strip().replace(" ", "").upper()
    padding = "=" * ((8 - len(normalized) % 8) % 8)
    key = base64.b32decode(normalized + padding, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10**digits)).zfill(digits)


def totp_code(secret: str, at: datetime | None = None) -> str:
    current = at or datetime.now(UTC)
    return _totp_for_counter(secret, _counter(current))


def verify_totp(secret: str, code: str, at: datetime | None = None) -> bool:
    if len(code) != 6 or not code.isdigit():
        return False
    current = at or datetime.now(UTC)
    counter = _counter(current)
    return any(
        hmac.compare_digest(_totp_for_counter(secret, counter + offset), code)
        for offset in (-1, 0, 1)
    )
