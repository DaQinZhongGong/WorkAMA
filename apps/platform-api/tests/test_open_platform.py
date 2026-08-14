from datetime import UTC, datetime, timedelta

import httpx
import pytest

from workama_platform.modules import open_platform


def test_redirect_and_webhook_url_validation_is_strict():
    assert open_platform.validate_redirect_uri("https://app.example.com/callback") == "https://app.example.com/callback"
    assert open_platform.validate_redirect_uri("http://localhost:3000/callback") == "http://localhost:3000/callback"
    for value in ("http://evil.example/callback", "https://127.0.0.1/callback", "https://app.example.com/callback#fragment", "file:///etc/passwd"):
        with pytest.raises(ValueError):
            open_platform.validate_redirect_uri(value)
    with pytest.raises(ValueError):
        open_platform.WebhookCreate(url="http://localhost:8000/hook", events=["artifact.created"])
    assert open_platform.WebhookCreate(url="mock://webhook/controlled", events=["artifact.created"]).url == "mock://webhook/controlled"
    assert open_platform.WebhookCreate(url="local://webhook/controlled", events=["artifact.created"]).url == "local://webhook/controlled"
    with pytest.raises(ValueError):
        open_platform.WebhookCreate(url="mock://other/controlled", events=["artifact.created"])
    with pytest.raises(ValueError):
        open_platform.WebhookCreate(url="mock://webhook/../private", events=["artifact.created"])


def test_pkce_and_token_helpers_are_deterministic_without_leaking_values():
    verifier = "a" * 64
    challenge = open_platform._pkce_challenge(verifier)
    assert len(challenge) == 43
    assert challenge == open_platform._pkce_challenge(verifier)
    token = open_platform._token("wama_at_")
    assert token.startswith("wama_at_")
    assert open_platform._last4(token) == token[-4:]
    signature = open_platform.webhook_signature("secret", '{"ok":true}', 1700000000)
    assert signature.startswith("t=1700000000,v1=")
    assert "secret" not in signature


def test_oauth_models_enforce_pkce_scope_and_redirect_rules():
    client = open_platform.OAuthClientCreate(
        name="Console",
        redirect_uris=["https://app.example.com/callback"],
        scopes=["openid", "profile", "openid"],
    )
    assert client.scopes == ["openid", "profile"]
    assert client.grant_types == ["authorization_code", "refresh_token"]
    query = open_platform.OAuthAuthorizeQuery(
        client_id="wama_client_abcdefghijklmnop",
        redirect_uri=client.redirect_uris[0],
        code_challenge="b" * 43,
        scope="profile openid",
        state="state-1",
    )
    assert query.scope == "openid profile"
    with pytest.raises(ValueError):
        open_platform.OAuthTokenRequest(grant_type="authorization_code", client_id=query.client_id, client_secret="x")


def test_webhook_events_and_delivery_input_are_allowlisted_and_bounded():
    body = open_platform.WebhookCreate(url="https://hooks.example.com/workama", events=["artifact.created", "*"])
    assert body.events == ["*", "artifact.created"]
    with pytest.raises(ValueError):
        open_platform.WebhookCreate(url="https://hooks.example.com/workama", events=["user.deleted"])
    test = open_platform.WebhookTestRequest(event_type="artifact.created", payload={"artifact_id": "art_1"})
    assert test.event_type == "artifact.created"
    # P1 expanded catalog from 720 §11
    expanded = open_platform.WebhookCreate(
        url="https://hooks.example.com/workama",
        events=["session.created", "approval.requested", "billing.balance.low", "member.created"],
    )
    assert "session.created" in expanded.events
    assert "approval.requested" in expanded.events


def test_webhook_event_catalog_matches_design_registry():
    expected = {
        "*",
        "artifact.created",
        "assistant.created",
        "automation.run.updated",
        "dataset.updated",
        "workflow.run.updated",
        "session.created",
        "session.completed",
        "session.failed",
        "approval.requested",
        "approval.decided",
        "dataset.indexed",
        "dataset.failed",
        "app.published",
        "app.unpublished",
        "workflow.completed",
        "workflow.failed",
        "billing.balance.low",
        "billing.subscription.changed",
        "quota.blocked",
        "member.created",
        "member.removed",
        "security.policy.changed",
        "data_request.completed",
    }
    assert open_platform.WEBHOOK_EVENTS == expected


@pytest.mark.asyncio
async def test_controlled_delivery_is_worker_executed_and_signed():
    delivery = {
        "url": "mock://webhook/controlled",
        "event_type": "artifact.created",
        "payload": {"artifact_id": "art_1"},
        "idempotency_key": "delivery-1",
        "secret_hash": "hashed-secret",
    }
    result = await open_platform.deliver_webhook_attempt(delivery)
    assert result["success"] is True
    assert result["response_code"] == 204
    assert result["signature"].startswith("t=")
    assert "hashed-secret" not in result["signature"]


@pytest.mark.asyncio
async def test_public_delivery_rechecks_dns_and_retries_429_without_leaking_body(monkeypatch):
    calls = []

    async def resolve(_url):
        return type("Validation", (), {"allowed": True, "reason": None})()

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429 if len(calls) == 1 else 204, content=b"ok", request=request)

    monkeypatch.setattr(open_platform, "validate_resolved_outbound_url", resolve)
    delivery = {
        "url": "https://hooks.example.com/workama",
        "event_type": "artifact.created",
        "payload": {"artifact_id": "art_1"},
        "idempotency_key": "delivery-2",
        "secret_hash": "hashed-secret",
    }
    transport = httpx.MockTransport(handler)
    first = await open_platform.deliver_webhook_attempt(delivery, transport=transport)
    second = await open_platform.deliver_webhook_attempt(delivery, transport=transport)
    assert first["retryable"] is True
    assert first["error_code"] == "webhook_http_429"
    assert second["success"] is True
    assert len(calls) == 2
    assert calls[0].headers["x-workama-signature"].startswith("t=")
    assert calls[0].content == open_platform.webhook_raw_body(delivery["event_type"], delivery["payload"])
    assert "hashed-secret" not in calls[0].content.decode()


@pytest.mark.asyncio
async def test_public_delivery_410_disables_and_response_size_is_bounded(monkeypatch):
    async def resolve(_url):
        return type("Validation", (), {"allowed": True, "reason": None})()

    monkeypatch.setattr(open_platform, "validate_resolved_outbound_url", resolve)
    delivery = {
        "url": "https://hooks.example.com/workama",
        "event_type": "artifact.created",
        "payload": {},
        "idempotency_key": "delivery-3",
        "secret_hash": "hashed-secret",
    }
    gone = await open_platform.deliver_webhook_attempt(
        delivery,
        transport=httpx.MockTransport(lambda request: httpx.Response(410, request=request)),
    )
    assert gone["disable"] is True

    too_large = await open_platform.deliver_webhook_attempt(
        delivery,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * (open_platform.WEBHOOK_MAX_RESPONSE_BYTES + 1), request=request)),
    )
    assert too_large["error_code"] == "response_too_large"
    assert too_large["retryable"] is False


def test_schema_and_router_cover_oauth_webhook_contract():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in open_platform.router.routes}
    assert ("/api/v1/oauth/clients", ("GET",)) in paths
    assert ("/api/v1/oauth/clients", ("POST",)) in paths
    assert ("/api/v1/oauth/authorize", ("GET",)) in paths
    assert ("/api/v1/oauth/token", ("POST",)) in paths
    assert ("/api/v1/webhooks/{webhook_id}/tests", ("POST",)) in paths
    assert ("/api/v1/webhooks/{webhook_id}/deliveries", ("GET",)) in paths


@pytest.mark.asyncio
async def test_schema_is_additive_and_hash_only():
    statements = []

    class Connection:
        async def execute(self, statement):
            statements.append(statement)

    await open_platform.ensure_open_platform_schema(Connection())
    schema = "\n".join(statements)
    for table in ("pf_oauth_client", "pf_oauth_code", "pf_oauth_token", "pf_webhook", "pf_webhook_delivery"):
        assert table in schema
    for field in ("client_secret_hash", "access_token_hash", "refresh_token_hash", "secret_hash", "payload_hash"):
        assert field in schema
    for field in ("delivery_mode", "payload", "signature", "response_summary", "claimed_at", "delivered_at"):
        assert field in schema
    assert "UNIQUE(webhook_id,idempotency_key)" in schema
