#!/usr/bin/env python3
"""Controlled SAML HTTP-POST binding smoke for the local Compose stack."""

from __future__ import annotations

import base64
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from signxml import XMLSigner


BASE_URL = os.getenv("WORKAMA_API_BASE_URL", "http://platform-api:8000").rstrip("/")
EVIDENCE_PATH = Path(os.getenv("EVIDENCE_PATH", "/src/quality/evidence/saml-acs-smoke.json"))


def _setting(name: str) -> str:
    value = os.getenv(name)
    if value:
        return value
    dotenv = Path("/src/.env")
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    return ""


def _fixture(email: str) -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "WorkAMA controlled IdP")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    now = datetime.now(UTC).replace(microsecond=0)
    assertion = etree.Element(
        "{urn:oasis:names:tc:SAML:2.0:assertion}Assertion",
        ID="_smoke-assertion",
        IssueInstant=now.isoformat().replace("+00:00", "Z"),
        Version="2.0",
    )
    etree.SubElement(assertion, "{urn:oasis:names:tc:SAML:2.0:assertion}Issuer").text = "urn:workama:controlled-idp"
    subject = etree.SubElement(assertion, "{urn:oasis:names:tc:SAML:2.0:assertion}Subject")
    etree.SubElement(subject, "{urn:oasis:names:tc:SAML:2.0:assertion}NameID").text = email
    conditions = etree.SubElement(
        assertion,
        "{urn:oasis:names:tc:SAML:2.0:assertion}Conditions",
        NotBefore=(now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        NotOnOrAfter=(now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
    )
    restriction = etree.SubElement(conditions, "{urn:oasis:names:tc:SAML:2.0:assertion}AudienceRestriction")
    etree.SubElement(restriction, "{urn:oasis:names:tc:SAML:2.0:assertion}Audience").text = "https://workama.example.com/saml"
    statement = etree.SubElement(assertion, "{urn:oasis:names:tc:SAML:2.0:assertion}AttributeStatement")
    attribute = etree.SubElement(statement, "{urn:oasis:names:tc:SAML:2.0:assertion}Attribute", Name="email")
    etree.SubElement(attribute, "{urn:oasis:names:tc:SAML:2.0:assertion}AttributeValue").text = email
    response = etree.Element(
        "{urn:oasis:names:tc:SAML:2.0:protocol}Response",
        ID="_smoke-response",
        IssueInstant=now.isoformat().replace("+00:00", "Z"),
        Version="2.0",
        Destination="https://console.example.com/saml/acs",
    )
    etree.SubElement(response, "{urn:oasis:names:tc:SAML:2.0:assertion}Issuer").text = "urn:workama:controlled-idp"
    status = etree.SubElement(response, "{urn:oasis:names:tc:SAML:2.0:protocol}Status")
    etree.SubElement(status, "{urn:oasis:names:tc:SAML:2.0:protocol}StatusCode", Value="urn:oasis:names:tc:SAML:2.0:status:Success")
    signed = XMLSigner(
        signature_algorithm="rsa-sha256",
        digest_algorithm="sha256",
        c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#",
    ).sign(assertion, key=key_pem, cert=cert_pem, reference_uri="#_smoke-assertion")
    response.append(signed)
    return cert_pem, base64.b64encode(etree.tostring(response)).decode()


def main() -> int:
    email = _setting("TEST_ACCOUNT_EMAIL")
    password = _setting("TEST_ACCOUNT_PASSWORD")
    if not email or not password:
        print("SKIP: TEST_ACCOUNT_EMAIL and TEST_ACCOUNT_PASSWORD are required for SAML ACS smoke.")
        return 0
    token = os.getenv("WORKAMA_TEST_TOKEN")
    evidence = {
        "verification_scope": "local-compose-controlled",
        "protocol_profile": "workama-saml-acs-v1",
        "signed_response_accepted": False,
        "replay_rejected": False,
        "certificate_not_returned": False,
        "response_id_bound": False,
        "pending_external": True,
    }
    config_id = None
    try:
        with httpx.Client(base_url=BASE_URL, timeout=15.0) as client:
            if not token:
                login = client.post("/api/v1/auth/login", json={"email": email, "password": password})
                login.raise_for_status()
                token = login.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            cert_pem, encoded_response = _fixture(email)
            config = client.post(
                "/api/v1/identity-federation",
                headers=headers,
                json={
                    "name": f"SAML ACS Smoke {datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}",
                    "provider": "saml",
                    "issuer": "urn:workama:controlled-idp",
                    "metadata_url": "https://idp.example.com/saml/metadata",
                    "certificate": cert_pem,
                    "redirect_allowlist": ["https://console.example.com/saml/acs"],
                    "mapping": {
                        "acs_url": "https://console.example.com/saml/acs",
                        "audience": "https://workama.example.com/saml",
                        "email_attribute": "email",
                    },
                },
            )
            config.raise_for_status()
            body = config.json()
            config_id = body["id"]
            evidence["certificate_not_returned"] = "certificate" not in body and "certificate_enc" not in body
            with psycopg.connect(os.getenv("DATABASE_URL", "postgresql://workama:workama_dev@postgres:5432/workama")) as db:
                db.execute("UPDATE id_federation_sso_config SET status='active',pending_reason=NULL WHERE id=%s", (config_id,))
                db.commit()
            acs = client.post(f"/api/v1/identity-federation/{config_id}/saml/acs", data={"SAMLResponse": encoded_response})
            acs.raise_for_status()
            session = acs.json()
            evidence["signed_response_accepted"] = session.get("sso", {}).get("provider") == "saml"
            replay = client.post(f"/api/v1/identity-federation/{config_id}/saml/acs", data={"SAMLResponse": encoded_response})
            evidence["replay_rejected"] = replay.status_code == 400 and "E09022" in replay.text
            evidence["response_id_bound"] = evidence["replay_rejected"]
            if not evidence["signed_response_accepted"] or not evidence["replay_rejected"]:
                raise RuntimeError("SAML ACS smoke assertions failed")
    finally:
        if config_id and token:
            try:
                httpx.delete(f"{BASE_URL}/api/v1/identity-federation/{config_id}", headers={"Authorization": f"Bearer {token}"}, timeout=15.0)
            except httpx.HTTPError:
                pass
        EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
