from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcessingActivity:
    classification: str
    purpose: str
    owner: str
    region: str
    retention_days: int
    deletion_behavior: str


@dataclass(frozen=True)
class ExportManifest:
    manifest: dict
    checksum: str


_TRANSITIONS = {
    "requested": {"identity_verification"},
    "identity_verification": {"scoped", "rejected"},
    "scoped": {"approved", "rejected"},
    "approved": {"executing"},
    "executing": {"verification", "partially_completed"},
    "verification": {"completed", "partially_completed"},
    "completed": set(),
    "partially_completed": set(),
    "rejected": set(),
}


def transition_allowed(current: str, target: str) -> bool:
    return target in _TRANSITIONS.get(current, set())


def infer_processing_activity(table_name: str) -> ProcessingActivity:
    name = table_name.lower()
    if any(term in name for term in ("refresh_token", "auth_token", "mfa_factor", "api_key", "gw_token")):
        return ProcessingActivity("C4", "authentication and secret protection", "security", "home", 30, "revoke_and_delete")
    if name == "id_user" or any(term in name for term in ("consent", "data_request", "billing", "transaction", "moderation_log")):
        return ProcessingActivity("C3", "account, privacy, billing or security operations", "privacy", "home", 2555, "anonymize_or_legal_retain")
    if name.startswith(("ag_", "gw_request", "gw_channel", "id_notification", "sec_prompt")):
        return ProcessingActivity("C2", "workspace product operation", "product", "home", 30, "delete_or_anonymize")
    if name.startswith("ops_"):
        return ProcessingActivity("C1", "platform reliability and delivery", "platform", "home", 30, "expire")
    if name.startswith(("id_", "gw_", "bill_", "sec_")):
        return ProcessingActivity("C2", "workspace configuration and operation", "product", "home", 90, "delete_or_anonymize")
    return ProcessingActivity("C3", "unclassified data pending privacy review", "privacy", "home", 30, "review_before_delete")


def build_export_manifest(
    request_id: str,
    user_id: str,
    resource_counts: dict[str, int],
    retained_items: list[str],
) -> ExportManifest:
    manifest = {
        "schema_version": "1",
        "request_id": request_id,
        "subject_ref": user_id,
        "resource_counts": dict(sorted(resource_counts.items())),
        "retained_items": sorted(set(retained_items)),
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return ExportManifest(manifest, hashlib.sha256(encoded).hexdigest())


def deletion_steps(scope: str) -> list[str]:
    if scope != "content":
        return ["scope_resources", "verify_absence"]
    return [
        "revoke_access",
        "delete_postgres_content",
        "delete_object_references",
        "purge_cache",
        "write_tombstone",
        "verify_absence",
    ]
