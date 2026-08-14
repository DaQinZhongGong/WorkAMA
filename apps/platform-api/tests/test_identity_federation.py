import base64
import json
from urllib.parse import parse_qs
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, HTTPException
from lxml import etree
from signxml import XMLSigner

from workama_platform.core import Actor, encrypt_secret, hash_secret
from workama_platform.modules.identity_federation import (
    SCHEMA_STATEMENTS,
    SCIM_TOKEN_PREFIX,
    OidcTokenValidationError,
    ScimGroupCreate,
    ScimPatchOperation,
    ScimUserCreate,
    SsoConfigCreate,
    SamlValidationError,
    _exchange_oidc_code,
    _decode_saml_response_form,
    _validate_saml_response,
    _sso_view,
    _validate_oidc_id_token,
    generate_oidc_state_bundle,
    nonce_digest,
    parse_scim_filter,
    router,
    scim_router,
    state_digest,
    validate_external_url,
    validate_oidc_state_record,
    validate_redirect_uri,
    validate_saml_entity_id,
)


def _actor(*, workspace_id: str = "wsp_a", org_id: str = "org_a") -> Actor:
    return Actor(
        user_id="usr_a",
        workspace_id=workspace_id,
        org_id=org_id,
        role="owner",
        email="owner@example.com",
        display_name="Owner",
        onboarding_completed=True,
        capabilities=("*",),
    )


def test_sso_defaults_to_disabled_and_rejects_sensitive_mapping():
    body = SsoConfigCreate(
        name="Corporate IdP",
        provider="oidc",
        issuer="https://idp.example.com",
        metadata_url="https://idp.example.com/.well-known/openid-configuration",
        client_id="workama-client",
        client_secret="secret-value",
        redirect_allowlist=["https://console.example.com/sso/callback"],
    )
    assert body.provider == "oidc"
    assert body.redirect_allowlist == ["https://console.example.com/sso/callback"]
    row = {
        "id": "sso_1",
        "org_id": "org_a",
        "workspace_id": "wsp_a",
        "name": body.name,
        "provider": body.provider,
        "issuer": body.issuer,
        "metadata_url": body.metadata_url,
        "authorization_endpoint": None,
        "client_id": body.client_id,
        "client_secret_hash": hash_secret(body.client_secret),
        "client_secret_ref": None,
        "client_secret_last4": "alue",
        "certificate_hash": None,
        "certificate_ref": None,
        "certificate_last4": None,
        "redirect_allowlist": body.redirect_allowlist,
        "mapping": {},
        "status": "disabled",
        "pending_reason": None,
        "version": 1,
    }
    view = _sso_view(row)
    assert view["status"] == "disabled"
    assert view["client_secret_configured"] is True
    assert "client_secret_hash" not in view
    assert body.client_secret not in view.values()

    with pytest.raises(ValueError, match="sensitive"):
        SsoConfigCreate(
            name="Unsafe",
            provider="oidc",
            issuer="https://idp.example.com",
            redirect_allowlist=["https://console.example.com/callback"],
            mapping={"client_secret": "must-not-be-here"},
        )


def test_ssrf_and_redirect_validation_is_fail_closed():
    assert validate_external_url("https://idp.example.com/.well-known")
    assert validate_redirect_uri("https://console.example.com/callback")
    for value in (
        "http://idp.example.com",
        "https://127.0.0.1/idp",
        "https://localhost/idp",
        "https://user:password@idp.example.com",
    ):
        with pytest.raises(ValueError):
            validate_external_url(value)
    with pytest.raises(ValueError):
        validate_redirect_uri("https://console.example.com/callback?code=leak")
    assert validate_saml_entity_id("urn:example:corporate-idp") == "urn:example:corporate-idp"
    with pytest.raises(ValueError):
        validate_saml_entity_id("javascript:alert(1)")


def test_saml_provider_accepts_safe_entity_id_and_metadata_url():
    body = SsoConfigCreate(
        name="SAML Corporate IdP",
        provider="saml",
        issuer="urn:example:corporate-idp",
        metadata_url="https://idp.example.com/saml/metadata",
        redirect_allowlist=["https://console.example.com/saml/acs"],
    )
    assert body.provider == "saml"
    assert body.issuer == "urn:example:corporate-idp"
    assert body.metadata_url.startswith("https://")
    with pytest.raises(ValueError, match="PEM"):
        SsoConfigCreate(
            name="Invalid SAML Certificate",
            provider="saml",
            issuer="urn:example:corporate-idp",
            metadata_url="https://idp.example.com/saml/metadata",
            certificate="not-a-certificate",
            redirect_allowlist=["https://console.example.com/saml/acs"],
        )


def _saml_fixture():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "WorkAMA test IdP")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(private_key, hashes.SHA256())
    )
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
    now = datetime.now(UTC).replace(microsecond=0)
    assertion = etree.Element(
        "{urn:oasis:names:tc:SAML:2.0:assertion}Assertion",
        ID="_assertion-1",
        IssueInstant=now.isoformat().replace("+00:00", "Z"),
        Version="2.0",
    )
    etree.SubElement(assertion, "{urn:oasis:names:tc:SAML:2.0:assertion}Issuer").text = "urn:example:corporate-idp"
    subject = etree.SubElement(assertion, "{urn:oasis:names:tc:SAML:2.0:assertion}Subject")
    etree.SubElement(subject, "{urn:oasis:names:tc:SAML:2.0:assertion}NameID").text = "provider-user-1"
    conditions = etree.SubElement(
        assertion,
        "{urn:oasis:names:tc:SAML:2.0:assertion}Conditions",
        NotBefore=(now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        NotOnOrAfter=(now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
    )
    audience_restriction = etree.SubElement(conditions, "{urn:oasis:names:tc:SAML:2.0:assertion}AudienceRestriction")
    etree.SubElement(audience_restriction, "{urn:oasis:names:tc:SAML:2.0:assertion}Audience").text = "https://workama.example.com/saml"
    statement = etree.SubElement(assertion, "{urn:oasis:names:tc:SAML:2.0:assertion}AttributeStatement")
    attribute = etree.SubElement(statement, "{urn:oasis:names:tc:SAML:2.0:assertion}Attribute", Name="email")
    etree.SubElement(attribute, "{urn:oasis:names:tc:SAML:2.0:assertion}AttributeValue").text = "owner@example.com"
    response = etree.Element(
        "{urn:oasis:names:tc:SAML:2.0:protocol}Response",
        ID="_response-1",
        IssueInstant=now.isoformat().replace("+00:00", "Z"),
        Version="2.0",
        Destination="https://console.example.com/saml/acs",
    )
    etree.SubElement(response, "{urn:oasis:names:tc:SAML:2.0:assertion}Issuer").text = "urn:example:corporate-idp"
    status = etree.SubElement(response, "{urn:oasis:names:tc:SAML:2.0:protocol}Status")
    etree.SubElement(status, "{urn:oasis:names:tc:SAML:2.0:protocol}StatusCode", Value="urn:oasis:names:tc:SAML:2.0:status:Success")
    signed_assertion = XMLSigner(
        signature_algorithm="rsa-sha256",
        digest_algorithm="sha256",
        c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#",
    ).sign(
        assertion,
        key=private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
        cert=certificate_pem,
        reference_uri="#_assertion-1",
    )
    response.append(signed_assertion)
    config = {
        "issuer": "urn:example:corporate-idp",
        "certificate_enc": encrypt_secret(certificate_pem),
        "mapping": {
            "acs_url": "https://console.example.com/saml/acs",
            "audience": "https://workama.example.com/saml",
            "email_attribute": "email",
        },
    }
    return etree.tostring(response), config, now


def test_saml_response_requires_pinned_certificate_and_validates_signed_claims():
    payload, config, now = _saml_fixture()
    claims = _validate_saml_response(payload, config=config, now=now)
    assert claims["sub"] == "provider-user-1"
    assert claims["response_id"] == "_response-1"
    assert claims["email"] == "owner@example.com"
    assert _decode_saml_response_form(base64.b64encode(payload).decode()) == payload
    with pytest.raises(SamlValidationError):
        _decode_saml_response_form("not-base64")
    with pytest.raises(SamlValidationError):
        _validate_saml_response(payload.replace(b"owner@example.com", b"attacker@example.com"), config=config, now=now)
    with pytest.raises(SamlValidationError):
        _validate_saml_response(payload, config={**config, "certificate_enc": encrypt_secret("not-a-certificate")}, now=now)


def test_oidc_state_is_hashed_one_time_and_expires():
    bundle = generate_oidc_state_bundle()
    assert bundle["state"] not in bundle["state_hash"]
    assert bundle["nonce"] not in bundle["nonce_hash"]
    now = datetime(2026, 7, 16, 8, 0, tzinfo=UTC)
    record = {"state_hash": state_digest(bundle["state"]), "consumed_at": None, "expires_at": now + timedelta(minutes=5)}
    assert validate_oidc_state_record(record, bundle["state"], now=now)
    record["consumed_at"] = now
    assert not validate_oidc_state_record(record, bundle["state"], now=now)
    record["consumed_at"] = None
    assert not validate_oidc_state_record(record, bundle["state"], now=now + timedelta(minutes=6))
    assert nonce_digest(bundle["nonce"]) == bundle["nonce_hash"]


def test_scim_token_is_one_time_display_and_protocol_models_accept_camel_case():
    raw = SCIM_TOKEN_PREFIX + "one-time-random-value"
    assert raw.startswith(SCIM_TOKEN_PREFIX)
    assert raw not in hash_secret(raw)
    user = ScimUserCreate.model_validate({"externalId": "ext-1", "userName": "alice@example.com", "displayName": "Alice", "active": True})
    group = ScimGroupCreate.model_validate({"externalId": "group-1", "displayName": "Engineering", "members": [{"value": "ext-1"}]})
    assert user.external_id == "ext-1"
    assert user.user_name == "alice@example.com"
    assert group.external_id == "group-1"
    assert group.members[0].value == "ext-1"
    assert ScimPatchOperation.model_validate({"op": "Replace", "path": "active", "value": False}).op == "replace"


def test_scim_filters_are_allowlisted_and_tenant_neutral():
    assert parse_scim_filter('userName eq "alice@example.com"') == ("userName", "eq", "alice@example.com")
    assert parse_scim_filter("active eq false") == ("active", "eq", "false")
    with pytest.raises(HTTPException) as exc:
        parse_scim_filter("userName eq 'alice' OR 1=1")
    assert exc.value.status_code == 400


def test_router_paths_and_schema_are_additive_and_repeatable():
    app = FastAPI()
    app.include_router(router)
    app.include_router(scim_router)
    schema = app.openapi()
    assert "/api/v1/identity-federation" in schema["paths"]
    assert "/api/v1/identity-federation/scim-tokens" in schema["paths"]
    assert "/scim/v2.0/{workspace_id}/Users" in schema["paths"]
    assert "/scim/v2.0/{workspace_id}/Groups" in schema["paths"]
    joined = "\n".join(SCHEMA_STATEMENTS)
    for table in (
        "id_federation_sso_config",
        "id_federation_oidc_state",
        "id_federation_scim_token",
        "id_federation_scim_user",
        "id_federation_scim_group",
        "id_federation_scim_group_member",
        "id_federation_saml_replay",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in joined
    assert "client_secret_hash" in joined
    assert "client_secret_enc" in joined
    assert "certificate_enc" in joined
    assert "code_verifier_enc" in joined
    assert "token_hash" in joined


def test_actor_fixture_is_explicitly_tenant_bound():
    actor = _actor()
    assert actor.org_id == "org_a"
    assert actor.workspace_id == "wsp_a"
    assert actor.workspace_id != "wsp_b"


def _oidc_fixture():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "test-key", "alg": "RS256", "use": "sig"})
    nonce = "oidc-nonce-value"
    verifier = "A" * 64
    now = int(datetime.now(UTC).timestamp())
    token = jwt.encode(
        {
            "iss": "https://idp.example.com",
            "sub": "provider-user-1",
            "aud": "workama-client",
            "iat": now,
            "exp": now + 300,
            "nonce": nonce,
            "email": "owner@example.com",
            "email_verified": True,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    config = {
        "issuer": "https://idp.example.com",
        "metadata_url": "https://idp.example.com/.well-known/openid-configuration",
        "client_id": "workama-client",
        "client_secret_enc": encrypt_secret("client-secret"),
    }
    record = {
        "nonce_hash": nonce_digest(nonce),
        "code_verifier_enc": encrypt_secret(verifier),
        "redirect_uri": "https://console.example.com/oidc/callback",
    }
    return config, record, token, {"keys": [public_jwk]}


@pytest.mark.asyncio
async def test_oidc_exchange_uses_discovery_pkce_and_validates_jwks():
    config, record, token, jwks = _oidc_fixture()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": config["issuer"],
                    "token_endpoint": "https://idp.example.com/oauth/token",
                    "jwks_uri": "https://idp.example.com/.well-known/jwks.json",
                },
            )
        if request.method == "POST" and request.url.path.endswith("/oauth/token"):
            form = parse_qs(request.content.decode())
            assert form["code"] == ["provider-code"]
            assert form["client_id"] == ["workama-client"]
            assert form["client_secret"] == ["client-secret"]
            assert len(form["code_verifier"][0]) == 64
            return httpx.Response(200, json={"access_token": "opaque", "id_token": token})
        if request.method == "GET" and request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json=jwks)
        return httpx.Response(404)

    claims = await _exchange_oidc_code(
        config,
        record,
        "provider-code",
        transport=httpx.MockTransport(handler),
    )
    assert claims["sub"] == "provider-user-1"
    assert claims["email"] == "owner@example.com"


def test_oidc_id_token_rejects_nonce_mismatch():
    config, record, token, jwks = _oidc_fixture()
    with pytest.raises(OidcTokenValidationError):
        _validate_oidc_id_token(
            token,
            jwks=jwks,
            issuer=config["issuer"],
            client_id=config["client_id"],
            nonce_hash=nonce_digest("different-nonce"),
        )
