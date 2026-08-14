import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import HTTPException

import workama_platform.modules.auth.router as auth_router

from workama_platform.modules.auth.service import (
    OAuthProviderConfig,
    build_oauth_authorization_url,
    new_oauth_state,
    new_pkce_verifier,
    oauth_callback_uri,
    oauth_provider_config,
    oauth_state_is_valid,
    pkce_challenge,
)


class _SingleUseRedis:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def getdel(self, key):
        self.calls += 1
        payload, self.payload = self.payload, None
        return payload


def test_pkce_s256_matches_rfc7636_vector_and_verifier_shape():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

    generated = new_pkce_verifier()
    assert 43 <= len(generated) <= 128
    assert pkce_challenge(generated)


def test_oauth_provider_allowlist_and_missing_credentials_are_explicit():
    settings = SimpleNamespace(
        github_oauth_client_id="",
        github_oauth_client_secret="",
        github_oauth_authorization_url="https://github.com/login/oauth/authorize",
        google_oauth_client_id="google-client",
        google_oauth_client_secret="google-secret",
        google_oauth_authorization_url="https://accounts.google.com/o/oauth2/v2/auth",
    )
    github = oauth_provider_config("GitHub", settings)
    google = oauth_provider_config("google", settings)
    assert github is not None and not github.configured
    assert google is not None and google.configured
    assert oauth_provider_config("microsoft", settings) is None


def test_authorization_url_contains_pkce_and_fixed_callback_without_secret():
    config = OAuthProviderConfig(
        name="github",
        client_id="public-client",
        client_secret="do-not-leak",
        authorization_url="https://github.example/authorize",
        scopes=("read:user", "user:email"),
    )
    state = new_oauth_state()
    redirect_uri = oauth_callback_uri("https://console.example", "github")
    url = build_oauth_authorization_url(
        config,
        state=state,
        redirect_uri=redirect_uri,
        code_challenge=pkce_challenge(new_pkce_verifier()),
    )
    query = parse_qs(urlsplit(url).query)
    assert query["client_id"] == ["public-client"]
    assert query["redirect_uri"] == [redirect_uri]
    assert query["state"] == [state]
    assert query["code_challenge_method"] == ["S256"]
    assert "do-not-leak" not in url


def test_oauth_state_is_single_flow_bound_and_expires():
    now = datetime.now(UTC)
    payload = {
        "provider": "google",
        "state": new_oauth_state(),
        "redirect_uri": oauth_callback_uri("https://console.example", "google"),
        "code_verifier": new_pkce_verifier(),
        "issued_at": now.timestamp(),
    }
    assert oauth_state_is_valid(payload, provider="google", redirect_uri=payload["redirect_uri"], now=now)
    assert oauth_state_is_valid(payload, provider="google", redirect_uri=payload["redirect_uri"], state=payload["state"], now=now)
    assert not oauth_state_is_valid(payload, provider="google", redirect_uri=payload["redirect_uri"], state=new_oauth_state(), now=now)
    assert not oauth_state_is_valid(payload, provider="github", redirect_uri=payload["redirect_uri"], now=now)
    assert not oauth_state_is_valid(payload, provider="google", redirect_uri="https://evil.example/callback", now=now)
    assert not oauth_state_is_valid(
        {**payload, "issued_at": (now - timedelta(seconds=601)).timestamp()},
        provider="google",
        redirect_uri=payload["redirect_uri"],
        now=now,
    )


def test_oauth_routes_fail_closed_when_disabled(monkeypatch):
    monkeypatch.setattr(auth_router, "settings", SimpleNamespace(auth_oauth_enabled=False))
    with pytest.raises(HTTPException) as disabled:
        auth_router._oauth_config_or_error("github")
    assert disabled.value.status_code == 503
    assert disabled.value.detail == "E01007 OAuth provider is disabled"

    with pytest.raises(HTTPException) as unsupported:
        auth_router._oauth_config_or_error("microsoft")
    assert unsupported.value.status_code == 404
    assert unsupported.value.detail == "E01006 OAuth provider is not supported"


@pytest.mark.asyncio
async def test_oauth_callback_consumes_state_once_before_exchange(monkeypatch):
    now = datetime.now(UTC)
    state = new_oauth_state()
    redirect_uri = oauth_callback_uri("https://console.example", "github")
    state_payload = {
        "provider": "github",
        "state": state,
        "redirect_uri": redirect_uri,
        "code_verifier": new_pkce_verifier(),
        "issued_at": now.timestamp(),
    }
    redis = _SingleUseRedis(json.dumps(state_payload))
    monkeypatch.setattr(
        auth_router,
        "settings",
        SimpleNamespace(
            auth_oauth_enabled=True,
            oauth_redirect_base_url="https://console.example",
            github_oauth_client_id="client",
            github_oauth_client_secret="secret",
            github_oauth_authorization_url="https://github.example/authorize",
        ),
    )
    monkeypatch.setattr(auth_router, "redis", redis)

    async def exchange(_config, **kwargs):
        assert kwargs["code"] == "provider-code"
        assert kwargs["code_verifier"] == state_payload["code_verifier"]
        return {"email": "owner@example.com", "display_name": "Owner", "subject": "github-user"}

    async def complete(_response, profile):
        return {"access_token": "issued-token", "user": profile}

    monkeypatch.setattr(auth_router, "_exchange_oauth_profile", exchange)
    monkeypatch.setattr(auth_router, "_complete_oauth_login", complete)
    result = await auth_router.oauth_callback("github", response=auth_router.Response(), code="provider-code", state=state, error=None)
    assert result["access_token"] == "issued-token"
    assert result["user"]["email"] == "owner@example.com"

    with pytest.raises(HTTPException) as replay:
        await auth_router.oauth_callback("github", response=auth_router.Response(), code="provider-code", state=state, error=None)
    assert replay.value.status_code == 400
    assert replay.value.detail == "E01010 OAuth state is invalid or expired"
    assert redis.calls == 2

    denied_state = new_oauth_state()
    denied_payload = {**state_payload, "state": denied_state}
    denied_redis = _SingleUseRedis(json.dumps(denied_payload))
    monkeypatch.setattr(auth_router, "redis", denied_redis)
    with pytest.raises(HTTPException) as denied:
        await auth_router.oauth_callback("github", response=auth_router.Response(), code=None, state=denied_state, error="access_denied")
    assert denied.value.status_code == 400
    assert denied.value.detail == "E01008 OAuth provider rejected authorization"
    with pytest.raises(HTTPException) as denied_replay:
        await auth_router.oauth_callback("github", response=auth_router.Response(), code="provider-code", state=denied_state, error=None)
    assert denied_replay.value.detail == "E01010 OAuth state is invalid or expired"


@pytest.mark.parametrize("base_url", ["console.example", "https://console.example/#fragment", "javascript:alert(1)"])
def test_oauth_callback_uri_rejects_non_absolute_or_fragment_urls(base_url):
    with pytest.raises(ValueError):
        oauth_callback_uri(base_url, "github")


@pytest.mark.asyncio
async def test_oauth_profile_exchange_uses_pkce_and_verified_profile():
    config = OAuthProviderConfig(
        name="google",
        client_id="client",
        client_secret="secret",
        authorization_url="https://idp.example/authorize",
        scopes=("openid", "email"),
        token_url="https://idp.example/token",
        userinfo_url="https://idp.example/userinfo",
        profile_kind="oidc",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            form = parse_qs(request.content.decode())
            assert form["code_verifier"] == ["verifier-value"]
            assert form["client_secret"] == ["secret"]
            return httpx.Response(200, json={"access_token": "access-token"})
        assert request.headers["authorization"] == "Bearer access-token"
        return httpx.Response(200, json={"sub": "subject", "email": "owner@example.com", "email_verified": True, "name": "Owner"})

    profile = await auth_router._exchange_oauth_profile(
        config,
        code="provider-code",
        code_verifier="verifier-value",
        redirect_uri="https://console.example/callback",
        transport=httpx.MockTransport(handler),
    )
    assert profile == {"email": "owner@example.com", "display_name": "Owner", "subject": "subject"}
