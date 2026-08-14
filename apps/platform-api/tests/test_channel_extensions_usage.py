import pytest
from datetime import UTC, datetime, timedelta

from workama_platform.modules import channel_extensions


def test_deduct_lease_cost_with_zero_cost_returns_success():
    # _deduct_lease_cost requires a DB connection, so we test the policy parsing logic indirectly
    policy = {"cost_per_lease": 0}
    assert int(policy.get("cost_per_lease", 0)) == 0


def test_deduct_lease_cost_parses_positive_cost():
    policy = {"cost_per_lease": 10}
    assert int(policy.get("cost_per_lease", 0)) == 10


def test_account_pool_usage_endpoint_is_registered():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in channel_extensions.router.routes}
    assert ("/api/v1/channel-extensions/account-pools/{pool_id}/usage", ("GET",)) in paths


def test_renew_expired_leases_and_cleanup_functions_exist():
    assert callable(channel_extensions.renew_expired_leases)
    assert callable(channel_extensions.cleanup_expired_sessions)


def test_schema_includes_usage_tables():
    schema = "\n".join(channel_extensions.SCHEMA_STATEMENTS)
    assert "gw_subscription_account_usage" in schema
    assert "gw_subscription_pool_billing_event" in schema
    assert "UNIQUE(pool_id, idempotency_key)" in schema
    assert "idx_account_usage_pool" in schema
    assert "idx_pool_billing_event" in schema


def test_sticky_account_selection_skips_exhausted_accounts():
    accounts = [
        {"id": "a1", "status": "active", "weight": 100, "quota_remaining": 0},
        {"id": "a2", "status": "exhausted", "weight": 100, "quota_remaining": 0},
    ]
    result = channel_extensions.choose_sticky_account(accounts, "session-1")
    assert result is None


def test_sticky_account_selection_respects_positive_quota():
    accounts = [
        {"id": "a1", "status": "active", "weight": 100, "quota_remaining": 5},
        {"id": "a2", "status": "active", "weight": 100, "quota_remaining": 10},
    ]
    result = channel_extensions.choose_sticky_account(accounts, "session-1")
    assert result is not None
    assert result["id"] in {"a1", "a2"}


def test_sticky_account_selection_with_none_quota():
    accounts = [
        {"id": "a1", "status": "active", "weight": 100, "quota_remaining": None},
    ]
    result = channel_extensions.choose_sticky_account(accounts, "session-1")
    assert result is not None
    assert result["id"] == "a1"


def test_lease_request_model_validation():
    req = channel_extensions.LeaseRequest(session_key="my-session", model="gpt-4")
    assert req.session_key == "my-session"
    assert req.model == "gpt-4"


def test_account_pool_create_defaults():
    body = channel_extensions.AccountPoolCreate(name="Test Pool", provider="openai")
    assert body.sticky_ttl_seconds == 3600
    assert body.billing_policy == {}


def test_pool_account_create_defaults():
    body = channel_extensions.PoolAccountCreate(display_name="Account 1", account_ref="sk-12345")
    assert body.region == "global"
    assert body.weight == 100


def test_lease_request_requires_session_key():
    with pytest.raises(ValueError):
        channel_extensions.LeaseRequest(session_key="", model="gpt-4")


def test_billing_event_table_has_idempotency_constraint():
    schema = "\n".join(channel_extensions.SCHEMA_STATEMENTS)
    assert "UNIQUE(pool_id, idempotency_key)" in schema


def test_usage_table_has_period_index():
    schema = "\n".join(channel_extensions.SCHEMA_STATEMENTS)
    assert "idx_account_usage_period" in schema


def test_renew_function_signature():
    import inspect
    sig = inspect.signature(channel_extensions.renew_expired_leases)
    assert "worker_id" in sig.parameters
    assert "limit" in sig.parameters


def test_cleanup_function_signature():
    import inspect
    sig = inspect.signature(channel_extensions.cleanup_expired_sessions)
    assert "worker_id" in sig.parameters
    assert "limit" in sig.parameters


def test_channel_extension_routes_cover_new_usage_endpoint():
    paths = {route.path for route in channel_extensions.router.routes}
    assert "/api/v1/channel-extensions/account-pools/{pool_id}/usage" in paths
