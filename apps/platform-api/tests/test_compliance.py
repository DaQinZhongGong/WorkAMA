from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI

from workama_platform.modules.compliance import (
    CAPABILITY_RE,
    SCHEMA_STATEMENTS,
    RegionPolicyUpsert,
    generate_license_key,
    jit_grant_allows,
    license_state,
    region_allows,
    router,
)


def test_license_keys_are_one_time_format_and_never_equal_to_hash():
    raw = generate_license_key()
    assert raw.startswith("wama-lic-")
    assert raw[-4:]
    assert raw not in raw.encode().hex()


def test_license_state_expires_and_preserves_explicit_revocation():
    now = datetime.now(UTC)
    assert license_state({"status": "active", "valid_from": now - timedelta(seconds=1), "valid_until": now + timedelta(seconds=1)}, now) == "active"
    assert license_state({"status": "active", "valid_from": now - timedelta(seconds=2), "valid_until": now - timedelta(seconds=1)}, now) == "expired"
    assert license_state({"status": "revoked", "valid_from": now - timedelta(days=1)}, now) == "revoked"


def test_region_policy_is_residency_and_cross_border_aware():
    policy = {"home_region": "cn", "allowed_regions": ["cn", "sg"], "provider_regions": ["cn", "sg"], "cross_border_mode": "deny", "residency_required": True}
    assert region_allows(policy, "cn", "cn")
    assert not region_allows(policy, "sg", "sg")
    policy["residency_required"] = False
    assert not region_allows(policy, "cn", "sg")
    policy["cross_border_mode"] = "allowlist"
    assert region_allows(policy, "cn", "sg")
    assert not region_allows(policy, "cn", "us")


def test_jit_grants_are_time_scoped_and_resource_scoped():
    now = datetime.now(UTC)
    grant = {"status": "active", "starts_at": now - timedelta(seconds=1), "expires_at": now + timedelta(seconds=10), "capabilities": ["audit:read"], "resource_scope": {"resource_ids": ["audit_1"]}}
    assert jit_grant_allows(grant, "audit:read", "audit_1", now)
    assert not jit_grant_allows(grant, "audit:write", "audit_1", now)
    assert not jit_grant_allows(grant, "audit:read", "audit_2", now)
    grant["expires_at"] = now - timedelta(seconds=1)
    assert not jit_grant_allows(grant, "audit:read", "audit_1", now)


def test_compliance_models_reject_invalid_region_and_capability():
    with pytest.raises(ValueError):
        RegionPolicyUpsert(home_region="CN", allowed_regions=["CN"])
    assert CAPABILITY_RE.fullmatch("audit:read")
    assert CAPABILITY_RE.fullmatch("workspace:*")
    assert not CAPABILITY_RE.fullmatch("audit read")


def test_compliance_router_and_schema_cover_enterprise_controls():
    app = FastAPI()
    app.include_router(router)
    paths = {route.path for route in router.routes}
    assert "/api/v1/enterprise/compliance/licenses" in paths
    assert "/api/v1/enterprise/compliance/region-policy" in paths
    assert "/api/v1/enterprise/compliance/legal-holds" in paths
    assert "/api/v1/enterprise/compliance/jit-grants" in paths
    assert "/api/v1/enterprise/compliance/subprocessors/{subprocessor_id}" in paths
    assert "/api/v1/enterprise/compliance/privacy-events/{event_id}/close" in paths
    schema = "\n".join(SCHEMA_STATEMENTS)
    for table in ("bill_license", "bill_sla_policy", "sec_region_policy", "sec_jit_grant", "sec_subprocessor", "sec_privacy_event"):
        assert table in schema
