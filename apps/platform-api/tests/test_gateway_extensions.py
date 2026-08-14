from workama_platform.modules.gateway.router import PROVIDER_CATALOG, PROVIDER_ALIASES


def test_second_batch_provider_catalog_is_protocol_explicit():
    for provider in ("ollama", "vllm", "azure", "bedrock", "minimax", "qianfan", "hunyuan", "mistral", "xai"):
        assert provider in PROVIDER_CATALOG
        assert PROVIDER_CATALOG[provider]["protocol"] == "openai"
        assert PROVIDER_CATALOG[provider]["capabilities"]
        assert PROVIDER_CATALOG[provider]["regions"]


def test_import_aliases_keep_external_catalog_names_stable():
    assert PROVIDER_ALIASES["azure-openai"] == "azure"
    assert PROVIDER_ALIASES["amazon-bedrock"] == "bedrock"
    assert PROVIDER_ALIASES["self-hosted"] == "vllm"


# ----------------------------------------------------------------------
# 免费 / 公益大模型供应商批次（15 个新供应商）
# ----------------------------------------------------------------------


FREE_PROVIDER_KEYS = (
    "siliconflow",
    "openrouter",
    "groq",
    "together",
    "cerebras",
    "cloudflare",
    "huggingface",
    "modelscope",
    "iflytek",
    "dmxapi",
    "n1n",
    "github",
    "perplexity",
    "cohere",
    "mistral_free",
)


# 第二批免费 / 公益供应商（开源模型推理 / 聚合 / 国内免费层）
FREE_PROVIDER_KEYS_BATCH_2 = (
    "deepinfra",
    "fireworks",
    "novita",
    "lepton",
    "replicate",
    "stepfun",
    "lingyi",
)

# 第三批免费 / 公益供应商（开源模型推理 / 国内免费层）
FREE_PROVIDER_KEYS_BATCH_3 = (
    "sambanova",
    "chutes",
    "nebius",
    "openbayes",
)


def test_free_provider_batch_is_registered_in_catalog():
    """15 个新免费供应商已全部注册到 PROVIDER_CATALOG，且字段结构完整。"""
    for provider in FREE_PROVIDER_KEYS:
        assert provider in PROVIDER_CATALOG, f"missing free provider: {provider}"
        profile = PROVIDER_CATALOG[provider]
        assert profile["protocol"] == "openai"
        assert profile["capabilities"], f"{provider} 缺少 capabilities"
        assert profile["regions"], f"{provider} 缺少 regions"
        assert profile["retention_mode"] == "provider_retained"


def test_free_provider_batch_2_is_registered_in_catalog():
    """第二批 7 个免费供应商已全部注册到 PROVIDER_CATALOG，且字段结构完整。"""
    for provider in FREE_PROVIDER_KEYS_BATCH_2:
        assert provider in PROVIDER_CATALOG, f"missing free provider: {provider}"
        profile = PROVIDER_CATALOG[provider]
        assert profile["protocol"] == "openai"
        assert profile["capabilities"], f"{provider} 缺少 capabilities"
        assert profile["regions"], f"{provider} 缺少 regions"
        assert profile["retention_mode"] == "provider_retained"


def test_free_provider_batch_3_is_registered_in_catalog():
    """第三批 4 个免费供应商已全部注册到 PROVIDER_CATALOG，且字段结构完整。"""
    for provider in FREE_PROVIDER_KEYS_BATCH_3:
        assert provider in PROVIDER_CATALOG, f"missing free provider: {provider}"
        profile = PROVIDER_CATALOG[provider]
        assert profile["protocol"] == "openai"
        assert profile["capabilities"], f"{provider} 缺少 capabilities"
        assert profile["regions"], f"{provider} 缺少 regions"
        assert profile["retention_mode"] == "provider_retained"


def test_provider_catalog_total_count_includes_free_batch():
    """catalog 总数 = 17 原有 + 15 第一批 + 11 第二/三批 + 42 第三批 + 18 第四批 = 103。

    与 Go 端 ``apps/gateway/internal/relay/adapter/adapter.go`` 中
    ``TestProviderCatalog_Count`` 断言的 103 保持一致。
    """
    assert len(PROVIDER_CATALOG) == 103


def test_free_provider_aliases_resolve_to_canonical_keys():
    """免费供应商的常见别名都能归一化到标准 provider key。"""
    expected = {
        "硅基流动": "siliconflow",
        "silicon": "siliconflow",
        "open-router": "openrouter",
        "groq-cloud": "groq",
        "together-ai": "together",
        "workers-ai": "cloudflare",
        "hf": "huggingface",
        "hugging-face": "huggingface",
        "魔搭": "modelscope",
        "model-scope": "modelscope",
        "星火": "iflytek",
        "spark": "iflytek",
        "讯飞": "iflytek",
        "github-models": "github",
        "perplexity-ai": "perplexity",
        "cohere-ai": "cohere",
        # 第二批别名
        "deep-infra": "deepinfra",
        "fireworks-ai": "fireworks",
        "novita-ai": "novita",
        "lepton-ai": "lepton",
        "阶跃": "stepfun",
        "阶跃星辰": "stepfun",
        "零一": "lingyi",
        "01-ai": "lingyi",
        "01.ai": "lingyi",
        "yi": "lingyi",
        # 第三批别名
        "samba-nova": "sambanova",
        "chutes-ai": "chutes",
        "nebius-ai": "nebius",
        "贝式计算": "openbayes",
        "open-bayes": "openbayes",
    }
    for alias, expected_provider in expected.items():
        assert PROVIDER_ALIASES.get(alias) == expected_provider, (
            f"alias {alias!r} should map to {expected_provider!r}"
        )
