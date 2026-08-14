#!/usr/bin/env python3
"""Static and evidence checks for the implemented open-platform contracts.

The gate deliberately checks the implementation-shaped routes documented in
api/openapi.yaml.  The 720 registry contains several future/planned aliases
whose paths differ from the live vertical slice; those aliases are not used as
runtime evidence here.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# Keep direct `python tools/<script>.py` execution equivalent to module execution.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.contract_registry_check import parse_openapi, parse_source_routes


OPENAPI_OPERATIONS = {
    ("GET", "/api/v1/oauth/clients"): "listOAuthClients",
    ("POST", "/api/v1/oauth/clients"): "createOAuthClient",
    ("GET", "/api/v1/oauth/clients/{}"): "getOAuthClient",
    ("PATCH", "/api/v1/oauth/clients/{}"): "updateOAuthClient",
    ("DELETE", "/api/v1/oauth/clients/{}"): "deleteOAuthClient",
    ("GET", "/api/v1/oauth/authorize"): "authorizeOAuthClient",
    ("POST", "/api/v1/oauth/token"): "exchangeOAuthToken",
    ("POST", "/api/v1/oauth/revocations"): "revokeOAuthToken",
    ("GET", "/api/v1/webhooks"): "listWebhooks",
    ("POST", "/api/v1/webhooks"): "createWebhook",
    ("GET", "/api/v1/webhooks/{}"): "getWebhook",
    ("PATCH", "/api/v1/webhooks/{}"): "updateWebhook",
    ("DELETE", "/api/v1/webhooks/{}"): "deleteWebhook",
    ("POST", "/api/v1/webhooks/{}/tests"): "testWebhook",
    ("GET", "/api/v1/webhooks/{}/deliveries"): "listWebhookDeliveries",
    ("GET", "/api/v1/a2a/agent-cards"): "listA2AAgentCards",
    ("POST", "/api/v1/a2a/agent-cards"): "createA2AAgentCard",
    ("PATCH", "/api/v1/a2a/agent-cards/{}"): "updateA2AAgentCard",
    ("GET", "/api/v1/a2a/public/agent-cards/{}"): "getPublicA2AAgentCard",
    ("POST", "/api/v1/a2a/tasks"): "createA2ATask",
    ("GET", "/api/v1/a2a/tasks/{}"): "getA2ATask",
    ("POST", "/api/v1/a2a/tasks/{}/updates"): "updateA2ATask",
    ("GET", "/api/v1/external-apps"): "listExternalApps",
    ("POST", "/api/v1/external-apps"): "createExternalApp",
    ("GET", "/api/v1/external-apps/{}"): "getExternalApp",
    ("PATCH", "/api/v1/external-apps/{}"): "updateExternalApp",
    ("GET", "/api/v1/external-apps/{}/invocations"): "listExternalAppInvocations",
    ("POST", "/api/v1/external-apps/{}/invocations"): "invokeExternalApp",
    ("GET", "/api/v1/marketplace/templates"): "listMarketplaceTemplates",
    ("POST", "/api/v1/marketplace/templates"): "createMarketplaceTemplate",
    ("POST", "/api/v1/marketplace/templates/{}/reviews"): "reviewMarketplaceTemplate",
    ("POST", "/api/v1/marketplace/templates/{}/publish"): "publishMarketplaceTemplate",
    ("POST", "/api/v1/marketplace/templates/{}/copies"): "copyMarketplaceTemplate",
}

SOURCE_ROUTES = set(OPENAPI_OPERATIONS)

SCHEMA_REQUIRED_FIELDS = {
    "OAuthClientCreate": {"name", "redirect_uris"},
    "OAuthAuthorizationResponse": {"code", "redirect_uri", "expires_in", "provider_execution"},
    "OAuthTokenRequest": {"grant_type", "client_id", "client_secret"},
    "OAuthTokenResponse": {"token_type", "access_token", "refresh_token", "expires_in", "scope"},
    "OAuthRevokeRequest": {"client_id", "client_secret", "token"},
    "WebhookCreate": {"url", "events"},
    "WebhookTestRequest": set(),
    "AgentCardCreate": {"name", "agent_id", "endpoint", "version"},
    "A2ATaskCreate": {"card_id", "operation", "message", "idempotency_key"},
    "A2ATaskUpdate": {"status"},
    "ExternalAppCreate": {"name", "provider", "endpoint"},
    "ExternalInvocationCreate": {"operation", "idempotency_key"},
    "TemplateCreate": {"name", "display_name", "template_type", "version", "artifact_ref"},
    "TemplateReview": {"review_status", "reason"},
    "TemplateCopy": {"idempotency_key"},
}

COMMON_EVIDENCE_FIELDS = {
    "evidence_schema_version",
    "verification_scope",
    "protocol_profile",
    "verification_target",
    "verified_boundary",
    "pending_boundary",
    "staging_gate",
    "public_protocol_verified",
    "signature_mutual_trust_verified",
}
EVIDENCE_FILES = ("open-platform-smoke.json", "a2a-smoke.json", "external-apps-smoke.json")
SECRET_PREFIXES = ("wama_secret_", "wama_at_", "wama_rt_", "wama_code_", "whsec_")


def _schema_block(text: str, name: str) -> str | None:
    match = re.search(rf"^    {re.escape(name)}:\s*$", text, flags=re.MULTILINE)
    if not match:
        return None
    tail = text[match.end() :]
    next_schema = re.search(r"^    [A-Za-z][A-Za-z0-9_]*:\s*$", tail, flags=re.MULTILINE)
    return tail[: next_schema.start()] if next_schema else tail


def _schema_fields(block: str) -> set[str]:
    return set(re.findall(r"^        ([A-Za-z][A-Za-z0-9_]*):", block, flags=re.MULTILINE))


def _required_fields(block: str) -> set[str]:
    match = re.search(r"^      required:\s*\[([^]]*)\]", block, flags=re.MULTILINE)
    if not match:
        return set()
    return {item.strip() for item in match.group(1).split(",") if item.strip()}


def _finding(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **extra}


def _audit_openapi(root: Path) -> list[dict[str, Any]]:
    path = root / "api" / "openapi.yaml"
    text = path.read_text(encoding="utf-8")
    _, operations = parse_openapi(path)
    actual = {(item.method, item.path): item.operation_id for item in operations}
    findings: list[dict[str, Any]] = []
    for key, operation_id in OPENAPI_OPERATIONS.items():
        if actual.get(key) != operation_id:
            findings.append(_finding("openapi.operation_missing", f"OpenAPI must register {operation_id}: {key[0]} {key[1]}", operation_id=operation_id))
    openapi_ids = [item.operation_id for item in operations]
    duplicates = sorted({item for item in openapi_ids if openapi_ids.count(item) > 1})
    for operation_id in duplicates:
        findings.append(_finding("openapi.operation_duplicate", f"OpenAPI operationId is duplicated: {operation_id}", operation_id=operation_id))
    for name, required in SCHEMA_REQUIRED_FIELDS.items():
        block = _schema_block(text, name)
        if block is None:
            findings.append(_finding("openapi.schema_missing", f"OpenAPI schema is missing: {name}", schema=name))
            continue
        fields = _schema_fields(block)
        if not required.issubset(fields | _required_fields(block)):
            missing = sorted(required - fields - _required_fields(block))
            findings.append(_finding("openapi.schema_field_missing", f"{name} is missing fields: {', '.join(missing)}", schema=name, fields=missing))
    return findings


def _audit_source_routes(root: Path) -> list[dict[str, Any]]:
    source = root / "apps" / "platform-api" / "src"
    routes = {(item.method, item.path) for item in parse_source_routes(source)}
    return [
        _finding("source.route_missing", f"Implementation route is missing from the expected open-platform surface: {method} {path}", method=method, path=path)
        for method, path in sorted(SOURCE_ROUTES - routes)
    ]


def _audit_asyncapi(root: Path) -> list[dict[str, Any]]:
    text = (root / "api" / "asyncapi.yaml").read_text(encoding="utf-8")
    required_fragments = (
        "webhookDeliveryRequests:",
        "address: webhook.delivery.requested.v1",
        "publishWebhookDeliveryRequested:",
        "webhookDeliveryRequestedV1:",
        "event_type, idempotency_key, payload_hash, delivery_mode",
        "event_type: { const: webhook.delivery.requested.v1 }",
    )
    return [
        _finding("asyncapi.registration_missing", f"AsyncAPI is missing required registration fragment: {fragment}", fragment=fragment)
        for fragment in required_fragments
        if fragment not in text
    ]


def _audit_smokes_and_docs(root: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    smoke_files = {
        "open-platform-smoke.ps1": ("pkce_exchange_succeeded", "controlled_delivery_delivered", "external_delivery_queued", "provider_exchange"),
        "a2a-smoke.ps1": ("public_card_trusted", "task_pending_external", "local_update_only"),
        "external-apps-smoke.ps1": ("external_provider_pending", "external_http_without_staging_credential_blocked", "public_protocol_verified", "EvidencePath"),
    }
    for filename, markers in smoke_files.items():
        path = root / "tools" / filename
        text = path.read_text(encoding="utf-8")
        for marker in ("pending_external" if filename != "open-platform-smoke.ps1" else "pending_boundary", "$false", *markers):
            if marker not in text:
                findings.append(_finding("smoke.marker_missing", f"{filename} is missing required gate marker: {marker}", file=filename, marker=marker))
    doc = root / "docs" / "open-platform-developer.md"
    if not doc.exists():
        findings.append(_finding("docs.developer_guide_missing", "docs/open-platform-developer.md is required"))
    else:
        text = doc.read_text(encoding="utf-8")
        for marker in ("pending_external", "provider_execution", "x-workama-signature", "Ed25519", "idempotency-key", "workama-open-platform-rest-v1"):
            if marker not in text:
                findings.append(_finding("docs.developer_guide_marker_missing", f"developer guide is missing: {marker}", marker=marker))
    matrix = next(root.glob("WorkAMA-Docs/925-*.md"), None)
    if matrix is None:
        findings.append(_finding("docs.matrix_missing", "925 compatibility matrix is missing"))
    else:
        text = matrix.read_text(encoding="utf-8")
        for marker in ("pending_external", "public_protocol_verified=false", "open-platform-smoke.ps1", "a2a-smoke.ps1"):
            if marker not in text:
                findings.append(_finding("docs.matrix_marker_missing", f"925 is missing: {marker}", marker=marker))
    return findings


def _contains_secret(value: Any) -> bool:
    if isinstance(value, str):
        return any(prefix in value for prefix in SECRET_PREFIXES)
    if isinstance(value, dict):
        return any(_contains_secret(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return False


def _audit_evidence(evidence_dir: Path, *, required: bool) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for filename in EVIDENCE_FILES:
        path = evidence_dir / filename
        if not path.exists():
            if required:
                findings.append(_finding("evidence.missing", f"Evidence file is missing: {path}", file=filename))
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            findings.append(_finding("evidence.invalid_json", f"Evidence is not valid JSON: {path}: {exc}", file=filename))
            continue
        missing = sorted(COMMON_EVIDENCE_FIELDS - set(data))
        if missing:
            findings.append(_finding("evidence.common_field_missing", f"{filename} is missing common evidence fields: {', '.join(missing)}", file=filename, fields=missing))
        if data.get("public_protocol_verified") is not False or data.get("signature_mutual_trust_verified") is not False:
            findings.append(_finding("evidence.external_boundary_promoted", f"{filename} must keep public protocol and signature mutual trust false", file=filename))
        if not data.get("pending_boundary"):
            findings.append(_finding("evidence.pending_boundary_missing", f"{filename} must declare pending_boundary", file=filename))
        if _contains_secret(data):
            findings.append(_finding("evidence.secret_detected", f"{filename} contains a raw token-like secret", file=filename))
    return findings


def audit(root: Path, *, evidence_dir: Path | None = None, require_evidence: bool = False) -> dict[str, Any]:
    root = root.resolve()
    findings = []
    findings.extend(_audit_openapi(root))
    findings.extend(_audit_source_routes(root))
    findings.extend(_audit_asyncapi(root))
    findings.extend(_audit_smokes_and_docs(root))
    if evidence_dir is not None:
        findings.extend(_audit_evidence(evidence_dir.resolve(), required=require_evidence))
    return {
        "ok": not findings,
        "root": str(root),
        "operation_count": len(OPENAPI_OPERATIONS),
        "evidence_required": require_evidence,
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check WorkAMA open-platform OpenAPI/AsyncAPI/docs/smoke contracts")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--require-evidence", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    report = audit(args.root, evidence_dir=args.evidence_dir, require_evidence=args.require_evidence)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    print(encoded)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(encoded + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
