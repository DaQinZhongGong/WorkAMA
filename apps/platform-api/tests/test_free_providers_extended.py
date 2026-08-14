"""Extended tests for the 46 newly-added free provider presets."""
from __future__ import annotations

from workama_platform.modules.gateway.free_presets import (
    FREE_PROVIDER_PRESETS,
    get_free_preset,
)
from workama_platform.modules.gateway.router import PROVIDER_CATALOG


BATCH_4_KEYS = (
    "aimlapi",
    "monsterapi",
    "predibase",
    "baseten",
    "runpod",
    "anyscale",
    "modal",
    "featherless",
    "inference_net",
    "lambda",
    "fal",
    "bentocloud",
    "xai",
    "nvidia",
    "kluster",
    "hyperbolic",
    "ai21",
    "reka",
    "watsonx",
    "lightning",
    "duckduckgo",
    "gpt4free",
    "aihubmix",
    "api2d",
    "openai_hk",
    "closeai",
    "zhizengzeng",
    "ohmygpt",
    "chatanywhere",
    "ai_ls",
    "v3api",
    "gptgod",
    "minimax",
    "baichuan",
    "metaso",
    "ppio",
    "ollama",
    "oneapi",
    "newapi",
    "llamacpp",
    "vllm",
    "xinference",
    "localai",
    "lmdeploy",
    "lmstudio",
    "gpt_link",
)

REQUIRED_FIELDS = {
    "provider",
    "name",
    "base_url",
    "protocol",
    "signup_url",
    "free_quota",
    "free_models",
    "capabilities",
    "regions",
    "retention_mode",
    "notes",
}

VALID_REGIONS = {"cn", "global", "self_hosted", "us", "eu", "asia", "sg"}

VALID_CAPABILITIES = {
    "chat",
    "vision",
    "tool_call",
    "json_mode",
    "embedding",
    "reasoning",
    "long_context",
    "image_generation",
    "background",
}


def test_batch_4_has_46_providers():
    assert len(BATCH_4_KEYS) == 46


def test_all_batch_4_providers_in_free_presets():
    for key in BATCH_4_KEYS:
        assert key in FREE_PROVIDER_PRESETS, f"missing batch-4 preset: {key}"


def test_batch_4_providers_have_required_fields():
    for key in BATCH_4_KEYS:
        preset = FREE_PROVIDER_PRESETS[key]
        missing = REQUIRED_FIELDS - set(preset.keys())
        assert not missing, f"preset {key!r} missing fields: {missing}"


def test_batch_4_provider_field_matches_key():
    for key in BATCH_4_KEYS:
        preset = FREE_PROVIDER_PRESETS[key]
        assert preset["provider"] == key


def test_batch_4_protocol_is_openai():
    for key in BATCH_4_KEYS:
        preset = FREE_PROVIDER_PRESETS[key]
        assert preset["protocol"] == "openai"


def test_batch_4_regions_are_valid():
    for key in BATCH_4_KEYS:
        preset = FREE_PROVIDER_PRESETS[key]
        for region in preset["regions"]:
            assert region in VALID_REGIONS, f"preset {key!r} invalid region: {region!r}"


def test_batch_4_capabilities_are_valid():
    for key in BATCH_4_KEYS:
        preset = FREE_PROVIDER_PRESETS[key]
        for cap in preset["capabilities"]:
            assert cap in VALID_CAPABILITIES, f"preset {key!r} invalid capability: {cap!r}"


def test_batch_4_free_models_non_empty():
    for key in BATCH_4_KEYS:
        preset = FREE_PROVIDER_PRESETS[key]
        assert preset["free_models"], f"preset {key!r} has empty free_models"


def test_batch_4_retention_mode_is_provider_retained():
    for key in BATCH_4_KEYS:
        preset = FREE_PROVIDER_PRESETS[key]
        assert preset["retention_mode"] == "provider_retained"


def test_batch_4_providers_in_catalog():
    for key in BATCH_4_KEYS:
        assert key in PROVIDER_CATALOG, f"batch-4 provider {key!r} not in PROVIDER_CATALOG"


def test_batch_4_catalog_protocol_is_openai():
    for key in BATCH_4_KEYS:
        entry = PROVIDER_CATALOG[key]
        assert entry["protocol"] == "openai"


def test_batch_4_catalog_has_required_fields():
    required = {"protocol", "capabilities", "regions", "retention_mode"}
    for key in BATCH_4_KEYS:
        entry = PROVIDER_CATALOG[key]
        missing = required - set(entry.keys())
        assert not missing, f"catalog entry {key!r} missing fields: {missing}"


def test_batch_4_get_free_preset_returns_preset():
    for key in BATCH_4_KEYS:
        preset = get_free_preset(key)
        assert preset is not None, f"get_free_preset returned None for {key!r}"
        assert preset["provider"] == key


def test_batch_4_self_hosted_use_ephemeral_retention():
    self_hosted_keys = (
        "gpt4free", "oneapi", "newapi", "llamacpp",
        "vllm", "xinference", "localai", "lmdeploy", "lmstudio",
    )
    for key in self_hosted_keys:
        entry = PROVIDER_CATALOG[key]
        assert entry["retention_mode"] == "ephemeral_retention"
        assert "self_hosted" in entry["regions"]


def test_batch_4_cn_providers_have_cn_region():
    cn_keys = (
        "aihubmix", "api2d", "openai_hk", "closeai", "zhizengzeng",
        "ohmygpt", "chatanywhere", "v3api", "gptgod", "minimax",
        "baichuan", "metaso", "ppio", "gpt_link",
    )
    for key in cn_keys:
        preset = FREE_PROVIDER_PRESETS[key]
        assert "cn" in preset["regions"]


def test_batch_4_global_providers_have_global_region():
    global_keys = (
        "aimlapi", "monsterapi", "predibase", "baseten", "runpod",
        "anyscale", "modal", "featherless", "inference_net", "lambda",
        "fal", "bentocloud", "nvidia", "kluster", "hyperbolic",
        "ai21", "reka", "watsonx", "lightning", "duckduckgo", "ai_ls",
    )
    for key in global_keys:
        preset = FREE_PROVIDER_PRESETS[key]
        assert "global" in preset["regions"]


def test_total_free_presets_is_100():
    """v7.134 起 FREE_PROVIDER_PRESETS = 100。"""
    assert len(FREE_PROVIDER_PRESETS) == 100
