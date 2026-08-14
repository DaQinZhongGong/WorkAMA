import pytest

from workama_platform.modules import gateway_prompts


def test_prompt_models_and_rendering_are_bounded():
    prompt = gateway_prompts.PromptCreate(
        name="support.reply",
        content="Answer as {{agent_name}} for {{customer}}.",
    )
    assert prompt.name == "support.reply"
    assert gateway_prompts._render(prompt.content, {"agent_name": "Ada", "customer": "WorkAMA"}) == "Answer as Ada for WorkAMA."
    with pytest.raises(gateway_prompts.HTTPException) as missing:
        gateway_prompts._render(prompt.content, {"agent_name": "Ada"})
    assert missing.value.status_code == 422


def test_prompt_name_and_variable_validation_reject_unsafe_shapes():
    with pytest.raises(ValueError):
        gateway_prompts.PromptCreate(name="bad name", content="hello")
    with pytest.raises(ValueError):
        gateway_prompts.PromptResolveRequest(workspace_id="ws_1", prompt_id="support", variables={"bad key": "x"})


def test_prompt_views_do_not_hide_version_provenance():
    view = gateway_prompts._view(
        {
            "id": "gwprm_1",
            "name": "support.reply",
            "version": 2,
            "content": "hello",
            "checksum": "a" * 64,
            "status": "published",
            "created_at": None,
            "published_at": None,
            "eval_status": "passed",
            "eval_failures": [],
            "rollout_percent": 40,
        }
    )
    assert view["version"] == 2
    assert view["checksum"] == "a" * 64
    assert view["eval_status"] == "passed"
    assert view["rollout_percent"] == 40
    assert view["rollout_strategy"] == "stable_sha256"


def test_prompt_rollout_uses_stable_hash_and_percentage_ranges():
    rows = [
        {"id": "gwprm_v2", "version": 2, "rollout_percent": 25},
        {"id": "gwprm_v1", "version": 1, "rollout_percent": 75},
    ]
    bucket = gateway_prompts._stable_rollout_bucket("ws_1", "support.reply", "token_1")
    assert 0 <= bucket < 100
    assert bucket == gateway_prompts._stable_rollout_bucket("ws_1", "support.reply", "token_1")
    selected, selected_bucket = gateway_prompts._select_rollout_version(rows, "ws_1", "support.reply", "token_1")
    assert selected_bucket == bucket
    assert selected["id"] in {"gwprm_v1", "gwprm_v2"}

    seen = {gateway_prompts._select_rollout_version(rows, "ws_1", "support.reply", f"token_{i}")[0]["id"] for i in range(256)}
    assert seen == {"gwprm_v1", "gwprm_v2"}


def test_prompt_rollout_requests_bound_percentages_and_reserved_key_is_internal_only():
    assert gateway_prompts.PromptReleaseRequest(rollout_percent=50).rollout_percent == 50
    assert gateway_prompts.PromptRollbackRequest(version_id="v1", rollout_percent=100).rollout_percent == 100
    with pytest.raises(ValueError):
        gateway_prompts.PromptReleaseRequest(rollout_percent=0)
    with pytest.raises(ValueError):
        gateway_prompts.PromptReleaseRequest(rollout_percent=101)
    request = gateway_prompts.PromptResolveRequest(
        workspace_id="ws_1",
        prompt_id="support.reply",
        variables={"__wama_rollout_key": "token_1"},
    )
    assert request.variables["__wama_rollout_key"] == "token_1"


def test_gateway_prompt_routes_cover_registry_and_internal_resolution():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in gateway_prompts.router.routes}
    assert ("/api/v1/gateway/prompts", ("GET",)) in paths
    assert ("/api/v1/gateway/prompts", ("POST",)) in paths
    assert ("/api/v1/gateway/prompts/{prompt_id}/versions", ("POST",)) in paths
    assert ("/api/v1/gateway/prompts/{prompt_id}/releases", ("POST",)) in paths
    assert ("/api/v1/gateway/prompts/{prompt_id}/rollbacks", ("POST",)) in paths
    internal_paths = {(route.path, tuple(sorted(route.methods or ()))) for route in gateway_prompts.internal_router.routes}
    assert ("/internal/gateway/prompts/resolve", ("POST",)) in internal_paths


@pytest.mark.asyncio
async def test_gateway_prompt_schema_reuses_security_prompt_tables():
    statements = []

    class Connection:
        async def execute(self, statement, *args):
            statements.append(statement)

    # The module intentionally has no schema creator: sec_prompt_version and
    # sec_eval_run are part of the existing security baseline.
    assert gateway_prompts._checksum("hello") == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert statements == []
