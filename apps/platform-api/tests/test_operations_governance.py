from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from workama_platform.modules.operations import (
    EVENT_CATALOG,
    content_hash,
    evaluate_flag,
    resolve_config,
    stable_bucket,
    validate_config_value,
    validate_event_properties,
    validate_flag,
)


def test_event_catalog_is_the_frozen_43_event_set():
    assert len(EVENT_CATALOG) == 43
    assert len(set(EVENT_CATALOG)) == 43
    assert all(name == name.lower() and name.replace("_", "").isalnum() for name in EVENT_CATALOG)
    assert {"signup_completed", "session_created", "dynamic_config_changed"} <= set(EVENT_CATALOG)


def test_stable_bucket_is_repeatable_and_bounded():
    first = stable_bucket("new_console", 3, "usr_123", "salt")
    assert first == stable_bucket("new_console", 3, "usr_123", "salt")
    assert 0 <= first <= 9999
    assert first != stable_bucket("new_console", 3, "usr_456", "salt")


def test_flag_validation_enforces_type_lifecycle_requirements():
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="runbook"):
        validate_flag({"flag_type": "ops", "runbook": "", "expires_at": None}, now=now)
    with pytest.raises(ValueError, match="metrics"):
        validate_flag({"flag_type": "experiment", "metrics": {}, "ends_at": now + timedelta(days=1)}, now=now)
    with pytest.raises(ValueError, match="end date"):
        validate_flag({"flag_type": "experiment", "metrics": {"primary": "activation"}, "ends_at": None}, now=now)


def test_flag_evaluation_uses_targeting_rollout_and_safe_value():
    flag = {
        "key": "new_console", "version": 2, "status": "enabled", "default_value": False,
        "safe_value": False, "salt": "stable", "starts_at": None, "ends_at": None,
        "targeting": {"workspace_ids": ["wsp_allow"], "percentage": 0},
    }
    assert evaluate_flag(flag, "usr_1", "wsp_allow")["value"] is True
    assert evaluate_flag(flag, "usr_1", "wsp_other")["value"] is False
    expired = {**flag, "ends_at": datetime.now(UTC) - timedelta(seconds=1)}
    assert evaluate_flag(expired, "usr_1", "wsp_allow") == {"value": False, "reason": "expired", "version": 2}


def test_config_schema_and_event_lint_reject_sensitive_content():
    schema = {"type": "object", "required": ["threshold"], "properties": {"threshold": {"type": "integer", "minimum": 1, "maximum": 100}}}
    assert validate_config_value(schema, {"threshold": 25}) == []
    assert validate_config_value(schema, {"threshold": 0})
    assert validate_config_value(schema, {"api_key": "sk-secret", "threshold": 25})
    with pytest.raises(ValueError, match="sensitive"):
        validate_event_properties({"prompt": "private text"}, allowed={"prompt"})
    with pytest.raises(ValueError, match="not allowed"):
        validate_event_properties({"unknown": 1}, allowed={"source"})


def test_content_hash_is_canonical():
    assert content_hash({"b": 2, "a": 1}) == content_hash({"a": 1, "b": 2})


def test_config_resolution_skips_future_expired_and_disabled_versions():
    now = datetime.now(UTC)
    versions = [
        {"version": 4, "status": "enabled", "effective_at": now + timedelta(hours=1), "expires_at": None},
        {"version": 3, "status": "disabled", "effective_at": None, "expires_at": None},
        {"version": 2, "status": "enabled", "effective_at": None, "expires_at": now - timedelta(seconds=1)},
        {"version": 1, "status": "enabled", "effective_at": None, "expires_at": None, "config_value": {"active": True}},
    ]
    assert resolve_config(versions, now=now)["version"] == 1
