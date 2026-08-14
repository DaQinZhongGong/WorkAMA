from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from workama_platform.modules import passkeys


def test_challenge_is_single_use_and_expiry_is_enforced():
    now = datetime.now(UTC)
    assert passkeys.challenge_is_usable(now + timedelta(seconds=30), None, now)
    assert not passkeys.challenge_is_usable(now + timedelta(seconds=30), now, now)
    assert not passkeys.challenge_is_usable(now - timedelta(seconds=1), None, now)

    with pytest.raises(Exception) as exc:
        passkeys._require_usable_challenge(
            {"flow": "authentication", "expires_at": now + timedelta(seconds=30), "consumed_at": now},
            "authentication",
        )
    assert getattr(exc.value, "status_code", None) == 400


class _ConsumeResult:
    def __init__(self, rows):
        self.rows = iter(rows)

    async def fetchone(self):
        return next(self.rows)


class _ConsumeConnection:
    def __init__(self):
        self.calls = []
        self.results = [_ConsumeResult([{"id": "pck_1"}]), _ConsumeResult([None])]

    async def execute(self, query, params):
        self.calls.append((query, params))
        return self.results.pop(0)


@pytest.mark.asyncio
async def test_challenge_consume_is_atomic_and_replay_returns_false():
    conn = _ConsumeConnection()
    assert await passkeys._consume_challenge(conn, "pck_1") is True
    assert await passkeys._consume_challenge(conn, "pck_1") is False
    assert all("consumed_at" in query and "expires_at > now()" in query for query, _ in conn.calls)


def test_challenge_cannot_be_used_by_another_user_or_workspace():
    challenge = {"user_id": "usr_a", "workspace_id": "wsp_a"}
    assert passkeys.challenge_allows_credential(challenge, {"user_id": "usr_a", "workspace_id": "wsp_a"})
    assert not passkeys.challenge_allows_credential(challenge, {"user_id": "usr_b", "workspace_id": "wsp_a"})
    assert not passkeys.challenge_allows_credential(challenge, {"user_id": "usr_a", "workspace_id": "wsp_b"})
    assert passkeys.challenge_allows_credential({"user_id": None, "workspace_id": None}, {"user_id": "usr_b", "workspace_id": "wsp_b"})


def test_public_views_never_return_public_keys_or_refresh_token_material():
    passkey = passkeys.passkey_view(
        {
            "id": "pky_1",
            "name": "Laptop",
            "credential_id": "AQID",
            "public_key": b"private-public-key-material",
            "sign_count": 3,
            "transports": ["internal"],
            "aaguid": "aaguid",
            "created_at": None,
            "last_used_at": None,
            "revoked_at": None,
        }
    )
    session = passkeys.device_session_view(
        {
            "id": "rft_1",
            "token_hash": "hash-that-must-not-leak",
            "family_id": "family",
            "created_at": None,
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
            "revoked_at": None,
        }
    )
    assert "public_key" not in passkey
    assert "token_hash" not in session
    assert "family_id" not in session
    assert passkey["credential_id"] == "AQID"
    assert session["status"] == "active"


def test_missing_webauthn_library_is_an_explicit_configuration_error(monkeypatch):
    def missing(_name):
        raise ImportError("not installed")

    monkeypatch.setattr(passkeys.importlib, "import_module", missing)
    with pytest.raises(passkeys.PasskeyConfigurationError, match="unavailable"):
        passkeys._load_webauthn()


class _Credential:
    @classmethod
    def model_validate(cls, value):
        return value


def test_adapter_does_not_turn_verification_errors_into_success():
    def reject(**_kwargs):
        raise ValueError("bad signature")

    fake = SimpleNamespace(
        AuthenticationCredential=_Credential,
        verify_authentication_response=reject,
    )
    adapter = passkeys.WebAuthnAdapter(fake)
    config = passkeys.PasskeyConfig(rp_id="example.test", origin="https://example.test")
    with pytest.raises(passkeys.WebAuthnVerificationError, match="verification failed"):
        adapter.verify_authentication(
            config,
            challenge=b"challenge",
            payload={"id": "AQID", "response": {}},
            public_key=b"public-key",
            sign_count=0,
        )


def test_adapter_passes_challenge_origin_rp_and_counter_to_real_verifier():
    seen = {}

    def accept(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(new_sign_count=7)

    fake = SimpleNamespace(
        AuthenticationCredential=_Credential,
        verify_authentication_response=accept,
    )
    adapter = passkeys.WebAuthnAdapter(fake)
    config = passkeys.PasskeyConfig(rp_id="example.test", origin="https://example.test")
    assert adapter.verify_authentication(
        config,
        challenge=b"challenge",
        payload={"id": "AQID", "response": {}},
        public_key=b"public-key",
        sign_count=4,
    ) == 7
    assert seen["expected_challenge"] == b"challenge"
    assert seen["expected_rp_id"] == "example.test"
    assert seen["expected_origin"] == "https://example.test"
    assert seen["credential_public_key"] == b"public-key"
    assert seen["credential_current_sign_count"] == 4
    assert seen["require_user_verification"] is True


def test_passkey_configuration_requires_explicit_origin_and_rp_id(monkeypatch):
    monkeypatch.delenv("WORKAMA_PASSKEY_RP_ID", raising=False)
    monkeypatch.delenv("WORKAMA_PASSKEY_ORIGIN", raising=False)
    with pytest.raises(passkeys.PasskeyConfigurationError, match="required"):
        passkeys._config_from_environment()

    monkeypatch.setenv("WORKAMA_PASSKEY_RP_ID", "example.test")
    monkeypatch.setenv("WORKAMA_PASSKEY_ORIGIN", "https://user:password@example.test")
    with pytest.raises(passkeys.PasskeyConfigurationError, match="origin"):
        passkeys._config_from_environment()


def test_router_exposes_registration_authentication_and_device_center_routes():
    routes = {(route.path, tuple(sorted(route.methods or ()))) for route in passkeys.router.routes}
    assert ("/api/v1/passkeys/registration/options", ("POST",)) in routes
    assert ("/api/v1/passkeys/registration/complete", ("POST",)) in routes
    assert ("/api/v1/passkeys/authentication/options", ("POST",)) in routes
    assert ("/api/v1/passkeys/authentication/complete", ("POST",)) in routes
    assert ("/api/v1/passkeys/{passkey_id}", ("PATCH",)) in routes
    assert ("/api/v1/passkeys/{passkey_id}/revoke", ("POST",)) in routes
    assert ("/api/v1/devices/sessions", ("GET",)) in routes
    assert ("/api/v1/devices/sessions/{session_id}/revoke", ("POST",)) in routes


@pytest.mark.asyncio
async def test_ensure_passkey_schema_executes_all_additive_statements():
    statements = []

    class Connection:
        async def execute(self, statement):
            statements.append(statement)

    await passkeys.ensure_passkey_schema(Connection())
    assert len(statements) == len(passkeys.SCHEMA_STATEMENTS)
    assert any("id_passkey_challenge" in statement for statement in statements)
