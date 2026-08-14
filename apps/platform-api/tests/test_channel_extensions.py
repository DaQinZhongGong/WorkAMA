from datetime import UTC, datetime, timedelta

from fastapi import FastAPI

from workama_platform.modules.channel_extensions import (
    SCHEMA_STATEMENTS,
    choose_sticky_account,
    miniapp_manifest,
    normalize_im_content,
    public_router,
    router,
)


def test_sticky_account_selection_is_deterministic_and_respects_quota():
    accounts = [
        {"id": "a1", "status": "active", "weight": 100, "quota_remaining": 10},
        {"id": "a2", "status": "active", "weight": 100, "quota_remaining": 10},
        {"id": "a3", "status": "exhausted", "weight": 100, "quota_remaining": 0},
    ]
    first = choose_sticky_account(accounts, "session-1")
    second = choose_sticky_account(accounts, "session-1")
    assert first and first["id"] == second["id"]
    assert first["id"] in {"a1", "a2"}
    assert choose_sticky_account([{**accounts[0], "quota_remaining": 0, "status": "paused"}], "session-1") is None


def test_im_content_has_channel_context_and_miniapp_manifest_is_explicit():
    assert normalize_im_content("feishu", "hello") == "[feishu] hello"
    assert normalize_im_content("feishu", "[feishu] hello") == "[feishu] hello"
    manifest = miniapp_manifest()
    assert manifest["client"] == "react-miniapp-adapter"
    assert manifest["credential_storage"] == "memory_only"
    assert manifest["provider_exchange"] == "pending_external"


def test_channel_extension_routes_and_schema_are_registered():
    app = FastAPI()
    app.include_router(router)
    app.include_router(public_router)
    paths = {route.path for route in router.routes} | {route.path for route in public_router.routes}
    assert "/api/v1/gateway/account-pools" in paths
    assert "/api/v1/gateway/account-pools/{pool_id}/leases" in paths
    assert "/api/v1/im/channels/{channel_id}/events" in paths
    assert "/api/v1/miniapp/bootstrap" in paths
    assert "/api/v1/miniapp/sessions/{session_id}/messages" in paths
    assert "/api/v1/public/miniapp/manifest" in paths
    schema = "\n".join(SCHEMA_STATEMENTS)
    for table in ("gw_subscription_account_pool", "gw_subscription_account", "gw_subscription_session", "im_channel", "im_message", "miniapp_session", "miniapp_message", "miniapp_subscription"):
        assert table in schema


def test_channel_binding_routes_cover_contract_crud_endpoints():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in router.routes}
    assert ("/api/v1/channel-bindings", ("GET",)) in paths
    assert ("/api/v1/channel-bindings", ("POST",)) in paths
    assert ("/api/v1/channel-bindings/{binding_id}", ("GET",)) in paths
    assert ("/api/v1/channel-bindings/{binding_id}", ("PATCH",)) in paths
    assert ("/api/v1/channel-bindings/{binding_id}", ("DELETE",)) in paths
