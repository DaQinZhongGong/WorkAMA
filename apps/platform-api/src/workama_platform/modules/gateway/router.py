from __future__ import annotations

import asyncio
import json
import secrets
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, HttpUrl

from workama_platform.core import (
    Actor,
    cache_get,
    cache_set,
    _cache_key,
    decrypt_secret,
    encrypt_secret,
    get_actor,
    hash_secret,
    json_dumps,
    new_id,
    pool,
    redis,
    require_internal,
)
from workama_platform.modules.billing.metering import (
    MeterRequest,
    _assert_workspace_match,
    settle_meter,
)
from workama_platform.modules.billing.grants import expire_credit_grants_in_transaction
from workama_platform.modules.billing.reservations import estimate_cost
from workama_platform.modules.security.service import (
    validate_outbound_url,
    validate_resolved_outbound_url,
)
from workama_platform.modules.gateway.free_presets import (
    FREE_PROVIDER_PRESETS,
    get_free_preset,
)
from workama_platform.modules.gateway.internal_channel import (
    auto_select_best_free_provider,
    get_internal_llm_status,
)
from workama_platform.modules.gateway.llm_client import call_llm

router = APIRouter(prefix="/api/v1/gateway", tags=["gateway"])
internal_router = APIRouter(prefix="/internal/gateway", tags=["gateway-internal"])

PROVIDER_CATALOG = {
    "openai": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "embedding", "reasoning", "background"], "regions": ["global", "us", "eu"], "retention_mode": "provider_retained"},
    "anthropic": {"protocol": "anthropic", "capabilities": ["chat", "vision", "tool_call", "reasoning"], "regions": ["global", "us", "eu"], "retention_mode": "provider_retained"},
    "gemini": {"protocol": "gemini", "capabilities": ["chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"], "regions": ["global", "us", "eu", "asia"], "retention_mode": "provider_retained"},
    "deepseek": {"protocol": "openai", "capabilities": ["chat", "tool_call", "json_mode", "reasoning"], "regions": ["cn", "global"], "retention_mode": "provider_retained"},
    "qwen": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"], "regions": ["cn", "sg"], "retention_mode": "provider_retained"},
    "doubao": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "embedding"], "regions": ["cn"], "retention_mode": "provider_retained"},
    "kimi": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "reasoning"], "regions": ["cn"], "retention_mode": "provider_retained"},
    "zhipu": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "embedding"], "regions": ["cn"], "retention_mode": "provider_retained"},
    "ollama": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "embedding"], "regions": ["self_hosted"], "retention_mode": "ephemeral_retention"},
    "vllm": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "embedding"], "regions": ["self_hosted"], "retention_mode": "ephemeral_retention"},
    "azure": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"], "regions": ["global", "us", "eu"], "retention_mode": "provider_retained"},
    "bedrock": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "reasoning"], "regions": ["global", "us", "eu", "asia"], "retention_mode": "provider_retained"},
    "minimax": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "reasoning"], "regions": ["cn", "global"], "retention_mode": "provider_retained"},
    "qianfan": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "embedding"], "regions": ["cn"], "retention_mode": "provider_retained"},
    "hunyuan": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["cn"], "retention_mode": "provider_retained"},
    "mistral": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"], "regions": ["global", "eu"], "retention_mode": "provider_retained"},
    "xai": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "reasoning"], "regions": ["global", "us"], "retention_mode": "provider_retained"},
    # --- 免费 / 公益大模型供应商（OpenAI 兼容协议） -------------------------
    "siliconflow": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"], "regions": ["cn", "global"], "retention_mode": "provider_retained"},
    "openrouter": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "reasoning"], "regions": ["global", "us", "eu"], "retention_mode": "provider_retained"},
    "groq": {"protocol": "openai", "capabilities": ["chat", "tool_call", "json_mode", "reasoning"], "regions": ["global", "us"], "retention_mode": "provider_retained"},
    "together": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"], "regions": ["global", "us"], "retention_mode": "provider_retained"},
    "cerebras": {"protocol": "openai", "capabilities": ["chat", "tool_call", "json_mode", "reasoning"], "regions": ["global", "us"], "retention_mode": "provider_retained"},
    "cloudflare": {"protocol": "openai", "capabilities": ["chat", "tool_call", "embedding"], "regions": ["global"], "retention_mode": "provider_retained"},
    "huggingface": {"protocol": "openai", "capabilities": ["chat", "embedding"], "regions": ["global", "eu"], "retention_mode": "provider_retained"},
    "modelscope": {"protocol": "openai", "capabilities": ["chat", "vision", "embedding"], "regions": ["cn"], "retention_mode": "provider_retained"},
    "iflytek": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call"], "regions": ["cn"], "retention_mode": "provider_retained"},
    "dmxapi": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"], "regions": ["cn", "global"], "retention_mode": "provider_retained"},
    "n1n": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "reasoning"], "regions": ["global", "us"], "retention_mode": "provider_retained"},
    "github": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "reasoning"], "regions": ["global", "us"], "retention_mode": "provider_retained"},
    "perplexity": {"protocol": "openai", "capabilities": ["chat", "tool_call", "json_mode"], "regions": ["global", "us"], "retention_mode": "provider_retained"},
    "cohere": {"protocol": "openai", "capabilities": ["chat", "tool_call", "json_mode", "embedding", "reasoning"], "regions": ["global", "us"], "retention_mode": "provider_retained"},
    "mistral_free": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"], "regions": ["global", "eu"], "retention_mode": "provider_retained"},
    # --- 第二批免费 / 公益供应商（开源模型推理 / 聚合 / 国内免费层） -----------
    "deepinfra": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "embedding"], "regions": ["global", "us"], "retention_mode": "provider_retained"},
    "fireworks": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"], "regions": ["global", "us"], "retention_mode": "provider_retained"},
    "novita": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "embedding"], "regions": ["global", "us"], "retention_mode": "provider_retained"},
    "lepton": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["global", "us"], "retention_mode": "provider_retained"},
    "replicate": {"protocol": "openai", "capabilities": ["chat", "vision"], "regions": ["global", "us"], "retention_mode": "provider_retained"},
    "stepfun": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call"], "regions": ["cn"], "retention_mode": "provider_retained"},
    "lingyi": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["cn", "global"], "retention_mode": "provider_retained"},
    "sambanova": {"protocol": "openai", "capabilities": ["chat", "tool_call", "json_mode"], "regions": ["global", "us"], "retention_mode": "provider_retained"},
    "chutes": {"protocol": "openai", "capabilities": ["chat", "tool_call", "json_mode", "reasoning"], "regions": ["global", "us"], "retention_mode": "provider_retained"},
    "nebius": {"protocol": "openai", "capabilities": ["chat", "tool_call", "json_mode", "embedding"], "regions": ["global", "eu"], "retention_mode": "provider_retained"},
    "openbayes": {"protocol": "openai", "capabilities": ["chat", "tool_call"], "regions": ["cn"], "retention_mode": "provider_retained"},
    # --- 第三批免费 / 公益供应商（聚合 / 自部署 / 国内国际免费层） -----------
    "aimlapi": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["global"], "retention_mode": "provider_retained"},
    "monsterapi": {"protocol": "openai", "capabilities": ["chat", "tool_call", "json_mode"], "regions": ["global"], "retention_mode": "provider_retained"},
    "predibase": {"protocol": "openai", "capabilities": ["chat", "tool_call", "json_mode"], "regions": ["global"], "retention_mode": "provider_retained"},
    "baseten": {"protocol": "openai", "capabilities": ["chat", "tool_call"], "regions": ["global"], "retention_mode": "provider_retained"},
    "runpod": {"protocol": "openai", "capabilities": ["chat"], "regions": ["global"], "retention_mode": "provider_retained"},
    "anyscale": {"protocol": "openai", "capabilities": ["chat", "tool_call", "json_mode"], "regions": ["global"], "retention_mode": "provider_retained"},
    "modal": {"protocol": "openai", "capabilities": ["chat"], "regions": ["global"], "retention_mode": "provider_retained"},
    "featherless": {"protocol": "openai", "capabilities": ["chat", "tool_call"], "regions": ["global"], "retention_mode": "provider_retained"},
    "inference_net": {"protocol": "openai", "capabilities": ["chat", "tool_call"], "regions": ["global"], "retention_mode": "provider_retained"},
    "lambda": {"protocol": "openai", "capabilities": ["chat", "tool_call", "json_mode"], "regions": ["global"], "retention_mode": "provider_retained"},
    "fal": {"protocol": "openai", "capabilities": ["chat", "vision"], "regions": ["global"], "retention_mode": "provider_retained"},
    "bentocloud": {"protocol": "openai", "capabilities": ["chat"], "regions": ["global"], "retention_mode": "provider_retained"},
    "nvidia": {"protocol": "openai", "capabilities": ["chat", "tool_call", "json_mode", "vision"], "regions": ["global"], "retention_mode": "provider_retained"},
    "kluster": {"protocol": "openai", "capabilities": ["chat", "reasoning", "tool_call"], "regions": ["global"], "retention_mode": "provider_retained"},
    "hyperbolic": {"protocol": "openai", "capabilities": ["chat", "reasoning"], "regions": ["global"], "retention_mode": "provider_retained"},
    "ai21": {"protocol": "openai", "capabilities": ["chat", "tool_call"], "regions": ["global"], "retention_mode": "provider_retained"},
    "reka": {"protocol": "openai", "capabilities": ["chat", "vision", "reasoning"], "regions": ["global"], "retention_mode": "provider_retained"},
    "watsonx": {"protocol": "openai", "capabilities": ["chat", "tool_call"], "regions": ["global"], "retention_mode": "provider_retained"},
    "lightning": {"protocol": "openai", "capabilities": ["chat"], "regions": ["global"], "retention_mode": "provider_retained"},
    "duckduckgo": {"protocol": "openai", "capabilities": ["chat", "reasoning"], "regions": ["global"], "retention_mode": "provider_retained"},
    "gpt4free": {"protocol": "openai", "capabilities": ["chat"], "regions": ["self_hosted"], "retention_mode": "ephemeral_retention"},
    "aihubmix": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["cn"], "retention_mode": "provider_retained"},
    "api2d": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["cn"], "retention_mode": "provider_retained"},
    "openai_hk": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["cn"], "retention_mode": "provider_retained"},
    "closeai": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["cn"], "retention_mode": "provider_retained"},
    "zhizengzeng": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["cn"], "retention_mode": "provider_retained"},
    "ohmygpt": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call"], "regions": ["cn"], "retention_mode": "provider_retained"},
    "chatanywhere": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call"], "regions": ["cn"], "retention_mode": "provider_retained"},
    "ai_ls": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["global"], "retention_mode": "provider_retained"},
    "v3api": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["cn"], "retention_mode": "provider_retained"},
    "gptgod": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["cn"], "retention_mode": "provider_retained"},
    "baichuan": {"protocol": "openai", "capabilities": ["chat", "tool_call", "json_mode"], "regions": ["cn"], "retention_mode": "provider_retained"},
    "metaso": {"protocol": "openai", "capabilities": ["chat", "tool_call"], "regions": ["cn"], "retention_mode": "provider_retained"},
    "ppio": {"protocol": "openai", "capabilities": ["chat", "reasoning", "tool_call"], "regions": ["cn"], "retention_mode": "provider_retained"},
    "oneapi": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["self_hosted"], "retention_mode": "ephemeral_retention"},
    "newapi": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["self_hosted"], "retention_mode": "ephemeral_retention"},
    "llamacpp": {"protocol": "openai", "capabilities": ["chat", "tool_call"], "regions": ["self_hosted"], "retention_mode": "ephemeral_retention"},
    "xinference": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["self_hosted"], "retention_mode": "ephemeral_retention"},
    "localai": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["self_hosted"], "retention_mode": "ephemeral_retention"},
    "lmdeploy": {"protocol": "openai", "capabilities": ["chat", "tool_call"], "regions": ["self_hosted"], "retention_mode": "ephemeral_retention"},
    "lmstudio": {"protocol": "openai", "capabilities": ["chat", "tool_call", "json_mode"], "regions": ["self_hosted"], "retention_mode": "ephemeral_retention"},
    "gpt_link": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["cn"], "retention_mode": "provider_retained"},
    # --- 第四批免费 / 公益供应商（国内中转聚合 / 海外开源 / 自部署网关） ---------
    "4sapi": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["cn", "global"], "retention_mode": "provider_retained"},
    "147api": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["cn", "global"], "retention_mode": "provider_retained"},
    "poloapi": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["cn", "global"], "retention_mode": "provider_retained"},
    "aigcbest": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["cn", "global"], "retention_mode": "provider_retained"},
    "deepbricks": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["global", "us"], "retention_mode": "provider_retained"},
    "vegal": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["cn", "global"], "retention_mode": "provider_retained"},
    "siliconflow_global": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "embedding"], "regions": ["global", "us", "sg"], "retention_mode": "provider_retained"},
    "openrouter_free": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "reasoning"], "regions": ["global", "us", "eu"], "retention_mode": "provider_retained"},
    "poe": {"protocol": "openai", "capabilities": ["chat", "vision"], "regions": ["global", "us"], "retention_mode": "provider_retained"},
    "glm_api_chat": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "embedding"], "regions": ["cn"], "retention_mode": "provider_retained"},
    "qwenpg": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "embedding", "reasoning"], "regions": ["cn", "sg"], "retention_mode": "provider_retained"},
    "fireworks_serverless": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "reasoning"], "regions": ["global", "us"], "retention_mode": "provider_retained"},
    "perplexity_online": {"protocol": "openai", "capabilities": ["chat", "tool_call", "json_mode"], "regions": ["global", "us"], "retention_mode": "provider_retained"},
    "openai_forward": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["self_hosted"], "retention_mode": "provider_retained"},
    "glhf": {"protocol": "openai", "capabilities": ["chat", "tool_call", "json_mode"], "regions": ["global", "us"], "retention_mode": "provider_retained"},
    "tokenflux": {"protocol": "openai", "capabilities": ["chat", "tool_call", "json_mode"], "regions": ["global"], "retention_mode": "provider_retained"},
    "llama_api": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode", "reasoning"], "regions": ["global", "us"], "retention_mode": "provider_retained"},
    "openai_compat_proxy": {"protocol": "openai", "capabilities": ["chat", "vision", "tool_call", "json_mode"], "regions": ["self_hosted"], "retention_mode": "provider_retained"},
}
PROVIDER_ALIASES = {
    "openai-compatible": "openai",
    "google": "gemini",
    "dashscope": "qwen",
    "volcengine": "doubao",
    "moonshot": "kimi",
    "glm": "zhipu",
    "azure-openai": "azure",
    "azure_openai": "azure",
    "amazon-bedrock": "bedrock",
    "self-hosted": "vllm",
    # --- 免费 / 公益供应商别名 --------------------------------------------
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
    # --- 第二批免费 / 公益供应商别名 ----------------------------------------
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
    # --- 第三批免费 / 公益供应商别名 ----------------------------------------
    "samba-nova": "sambanova",
    "chutes-ai": "chutes",
    "nebius-ai": "nebius",
    "贝式计算": "openbayes",
    "open-bayes": "openbayes",
    # --- 第四批免费 / 公益供应商别名 ----------------------------------------
    "星链": "4sapi",
    "4s-api": "4sapi",
    "4s": "4sapi",
    "147-api": "147api",
    "polo-api": "poloapi",
    "polo": "poloapi",
    "aigc-best": "aigcbest",
    "deep-bricks": "deepbricks",
    "vegal-ai": "vegal",
    "siliconflow-global": "siliconflow_global",
    "openrouter-free": "openrouter_free",
    "poe-api": "poe",
    "chatglm": "glm_api_chat",
    "qwen-pg": "qwenpg",
    "fireworks-serverless": "fireworks_serverless",
    "perplexity-online": "perplexity_online",
    "openai-forward": "openai_forward",
    "glhf-chat": "glhf",
    "token-flux": "tokenflux",
    "llama-com": "llama_api",
    "meta-llama": "llama_api",
    "openai-compat": "openai_compat_proxy",
}


def _provider_name(value: str) -> str:
    normalized = value.strip().lower()
    return PROVIDER_ALIASES.get(normalized, normalized)


class ChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: str = "openai-compatible"
    base_url: str
    api_key: str | None = None
    models: list[str] = []
    weight: int = Field(default=100, ge=1, le=1000)
    status: str = "enabled"


class ChannelImportRequest(BaseModel):
    source: Literal["one-api", "new-api"]
    channels: list[dict] = Field(default_factory=list, max_length=200)
    dry_run: bool = True


class FreeProviderEnableRequest(BaseModel):
    """一键启用免费供应商的请求体。

    - ``api_key`` 必填：免费供应商的 API Key
    - ``name`` 选填：自定义渠道名称，缺省时使用预设名称
    - ``models`` 选填：自定义模型列表，缺省时使用预设免费模型清单
    - ``base_url`` 选填：自定义基础 URL（如 Cloudflare 需替换 ``{account_id}``）
    - ``enabled`` 选填：是否启用渠道，缺省为 True
    """

    api_key: str = Field(min_length=1, max_length=500)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    models: list[str] | None = None
    base_url: str | None = None
    enabled: bool = True


class LlmTestRequest(BaseModel):
    """Request body for the LLM test endpoint."""

    model: str = Field(default="gpt-4o-mini", max_length=120)
    message: str = Field(default="hello", max_length=500)


class GatewayTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    rpm_limit: int = Field(default=60, ge=1, le=100000)
    tpm_limit: int = Field(default=100000, ge=1)
    model_whitelist: list[str] = []
    pinned_channel_id: str | None = None
    group_id: str | None = None


class TokenGroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    rpm_limit: int = Field(default=600, ge=1, le=1000000)
    tpm_limit: int = Field(default=1000000, ge=1)
    model_whitelist: list[str] = []
    pinned_channel_id: str | None = None
    fallback_chain: dict[str, list[str]] = Field(default_factory=dict)
    model_mapping_override: dict[str, dict[str, str]] = Field(default_factory=dict)
    status: Literal["enabled", "disabled"] = "enabled"


class ModelMappingCreate(BaseModel):
    model: str = Field(min_length=1, max_length=120)
    channel_id: str = Field(min_length=1, max_length=40)
    upstream_model: str = Field(min_length=1, max_length=120)


class PricingUpdate(BaseModel):
    model: str
    input_per_million: Decimal = Field(ge=0)
    output_per_million: Decimal = Field(ge=0)
    markup_percent: Decimal = Field(default=10, ge=0, le=1000)


class ResolveRequest(BaseModel):
    api_key: str | None = None
    workspace_id: str | None = None
    model: str


class RateLimitRequest(BaseModel):
    actor_key: str = Field(min_length=1, max_length=160)
    rpm_limit: int = Field(ge=1, le=1000000)
    tpm_limit: int = Field(ge=1)
    estimated_tokens: int = Field(default=1, ge=1)


class RateLimitScope(BaseModel):
    actor_key: str = Field(min_length=1, max_length=160)
    rpm_limit: int = Field(ge=1, le=1000000)
    tpm_limit: int = Field(ge=1)


class RateLimitBatchRequest(BaseModel):
    scopes: list[RateLimitScope] = Field(min_length=1, max_length=2)
    estimated_tokens: int = Field(default=1, ge=1)


class ReservationRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=160)
    workspace_id: str = Field(min_length=1, max_length=40)
    model: str = Field(min_length=1, max_length=120)
    estimated_tokens: int = Field(ge=1, le=2_000_000)


class ReservationReleaseRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=160)


def _require_admin(actor: Actor) -> None:
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")


def _validate_channel_url(provider: str, base_url: str) -> None:
    if provider == "mock" and base_url == "mock://local":
        return
    if _provider_name(provider) not in PROVIDER_CATALOG:
        raise HTTPException(status_code=422, detail="Unsupported provider; select one of the first eight provider adapters")
    result = validate_outbound_url(base_url)
    if not result.allowed:
        raise HTTPException(status_code=422, detail=f"Unsafe channel URL: {result.reason}")


async def _validate_pinned_channel(conn, workspace_id: str, channel_id: str | None):
    if not channel_id:
        return
    result = await conn.execute(
        """
        SELECT 1 FROM gw_channel
        WHERE id = %s AND workspace_id = %s AND status = 'enabled'
        """,
        (channel_id, workspace_id),
    )
    if not await result.fetchone():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Pinned channel must be enabled and belong to this workspace",
        )


async def _validate_token_group(conn, workspace_id: str, group_id: str | None):
    if not group_id:
        return
    result = await conn.execute(
        """
        SELECT 1 FROM gw_token_group
        WHERE id = %s AND workspace_id = %s AND status = 'enabled'
        """,
        (group_id, workspace_id),
    )
    if not await result.fetchone():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Token group must be enabled and belong to this workspace",
        )


def _normalize_model_name(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 120:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name} must be a non-empty model name up to 120 characters",
        )
    return normalized


def _normalize_group_routing(
    body: TokenGroupRequest,
) -> tuple[dict[str, dict[str, str]], dict[str, list[str]]]:
    if len(body.model_mapping_override) > 50 or len(body.fallback_chain) > 50:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A token group supports at most 50 routing rules of each type",
        )
    overrides: dict[str, dict[str, str]] = {}
    for raw_model, raw_channels in body.model_mapping_override.items():
        model = _normalize_model_name(raw_model, "Model mapping override key")
        if model in overrides or not raw_channels:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Each model mapping override needs one or more unique channel mappings",
            )
        channels: dict[str, str] = {}
        for raw_channel_id, raw_upstream_model in raw_channels.items():
            channel_id = raw_channel_id.strip()
            upstream_model = _normalize_model_name(
                raw_upstream_model, "Model mapping override upstream model"
            )
            if not channel_id or len(channel_id) > 40 or channel_id in channels:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Model mapping override channel ids must be unique non-empty ids",
                )
            channels[channel_id] = upstream_model
        overrides[model] = channels

    fallback_chain: dict[str, list[str]] = {}
    for raw_model, raw_fallbacks in body.fallback_chain.items():
        model = _normalize_model_name(raw_model, "Fallback chain primary model")
        if model in fallback_chain or not raw_fallbacks or len(raw_fallbacks) > 5:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Each fallback chain needs one to five unique fallback models",
            )
        fallbacks: list[str] = []
        for raw_fallback in raw_fallbacks:
            fallback = _normalize_model_name(raw_fallback, "Fallback model")
            if fallback == model or fallback in fallbacks:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Fallback models cannot repeat or refer to the primary model",
                )
            fallbacks.append(fallback)
        fallback_chain[model] = fallbacks
    return overrides, fallback_chain


async def _validate_group_mapping_override_channels(
    conn, workspace_id: str, overrides: dict[str, dict[str, str]]
):
    channel_ids = {channel_id for channels in overrides.values() for channel_id in channels}
    if not channel_ids:
        return
    result = await conn.execute(
        """
        SELECT id FROM gw_channel
        WHERE workspace_id = %s AND status = 'enabled' AND id = ANY(%s)
        """,
        (workspace_id, list(channel_ids)),
    )
    valid_ids = {row["id"] for row in await result.fetchall()}
    if valid_ids != channel_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Model mapping override channels must be enabled and belong to this workspace",
        )


async def _clear_channel_references(conn, workspace_id: str, channel_id: str):
    await conn.execute(
        """
        UPDATE gw_token SET pinned_channel_id = NULL
        WHERE workspace_id = %s AND pinned_channel_id = %s
        """,
        (workspace_id, channel_id),
    )
    await conn.execute(
        """
        UPDATE gw_token_group
        SET pinned_channel_id = CASE
                WHEN pinned_channel_id = %s THEN NULL ELSE pinned_channel_id
            END,
            model_mapping_override = COALESCE(
                (
                    SELECT jsonb_object_agg(item.key, item.value - %s)
                    FROM jsonb_each(gw_token_group.model_mapping_override) AS item
                    WHERE jsonb_typeof(item.value) = 'object'
                      AND item.value - %s <> '{}'::jsonb
                ),
                '{}'::jsonb
            ),
            updated_at = now()
        WHERE workspace_id = %s
          AND (
              pinned_channel_id = %s
              OR EXISTS (
                  SELECT 1
                  FROM jsonb_each(gw_token_group.model_mapping_override) AS item
                  WHERE jsonb_typeof(item.value) = 'object' AND item.value ? %s
              )
          )
        """,
        (channel_id, channel_id, channel_id, workspace_id, channel_id, channel_id),
    )


@router.get("/channels")
async def list_channels(actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, name, provider, base_url, models, weight, status, last_health,
                   (credential_enc IS NOT NULL) AS has_credential, created_at, updated_at
            FROM gw_channel WHERE workspace_id = %s ORDER BY created_at DESC
            """,
            (actor.workspace_id,),
        )
        # Contract《720》listChannels: ListQuery -> ListResponse<ChannelDTO>
        # 保留 items 字段向后兼容
        data = await result.fetchall()
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.get("/providers")
async def list_providers(actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    # Contract《720》listProviders: ListQuery -> ListResponse<ProviderDTO>
    # 保留 items 字段向后兼容
    data = [{"id": provider, **profile} for provider, profile in PROVIDER_CATALOG.items()]
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


class ProviderHealthCheckRequest(BaseModel):
    """Supplier health check request body."""

    provider_keys: list[str] | None = None


def _provider_health_cache_key(workspace_id: str) -> str:
    return f"gw:provider_health:{workspace_id}"


def _health_result(channel, status, latency_ms, error_message):
    base_url = (channel.get("base_url") or "").rstrip("/")
    return {
        "channel_id": channel.get("id"),
        "name": channel.get("name"),
        "provider": channel.get("provider"),
        "base_url": base_url,
        "status": status,
        "latency_ms": latency_ms,
        "last_checked": datetime.now(UTC).isoformat(),
        "error_message": error_message,
    }

async def _probe_provider_health(channel: dict[str, Any]) -> dict[str, Any]:
    base_url = (channel.get("base_url") or "").rstrip("/")
    if not base_url or base_url.startswith(("mock://", "local://")):
        return _health_result(channel, "healthy", 0, None)

    loop = asyncio.get_event_loop()
    started = loop.time()
    health_url = f"{base_url}/v1/models"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            response = await client.get(health_url)
        latency_ms = int((loop.time() - started) * 1000)
        ok = 200 <= response.status_code < 300
        return _health_result(
            channel,
            "healthy" if ok else "unhealthy",
            latency_ms,
            None if ok else f"HTTP {response.status_code}",
        )
    except httpx.TimeoutException:
        latency_ms = int((loop.time() - started) * 1000)
        return _health_result(channel, "unhealthy", latency_ms, "timeout")
    except httpx.HTTPError as exc:
        latency_ms = int((loop.time() - started) * 1000)
        return _health_result(channel, "unhealthy", latency_ms, str(exc) or "http_error")
    except Exception as exc:
        latency_ms = int((loop.time() - started) * 1000)
        return _health_result(channel, "unknown", latency_ms, str(exc) or "unknown_error")


async def _probe_channels_concurrently(channels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not channels:
        return []
    return await asyncio.gather(*[_probe_provider_health(channel) for channel in channels])

@router.post("/providers/health-check")
async def health_check_providers(
    body: ProviderHealthCheckRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require_admin(actor)
    async with pool.connection() as conn:
        if body.provider_keys:
            result = await conn.execute(
                """SELECT id, name, provider, base_url, status FROM gw_channel
                   WHERE workspace_id = %s AND provider = ANY(%s)
                   ORDER BY provider, name""",
                (actor.workspace_id, list(body.provider_keys)),
            )
        else:
            result = await conn.execute(
                """SELECT id, name, provider, base_url, status FROM gw_channel
                   WHERE workspace_id = %s ORDER BY provider, name""",
                (actor.workspace_id,),
            )
        channels = [dict(row) for row in await result.fetchall()]

    unknown_providers: list[dict[str, Any]] = []
    if body.provider_keys:
        configured = {ch["provider"] for ch in channels}
        for key in body.provider_keys:
            normalized = _provider_name(key)
            if normalized not in configured:
                unknown_providers.append(_health_result(
                    {"id": None, "name": None, "provider": normalized, "base_url": None},
                    "unknown", 0, "no channel configured for this provider",
                ))

    probed = await _probe_channels_concurrently(channels)
    results = unknown_providers + probed
    healthy = sum(1 for r in results if r["status"] == "healthy")
    unhealthy = sum(1 for r in results if r["status"] == "unhealthy")
    unknown = sum(1 for r in results if r["status"] == "unknown")
    payload = {
        "results": results,
        "checked_at": datetime.now(UTC).isoformat(),
        "total": len(results),
        "healthy": healthy,
        "unhealthy": unhealthy,
        "unknown": unknown,
        "cached": True,
    }
    try:
        await redis.set(
            _provider_health_cache_key(actor.workspace_id),
            json.dumps(payload, ensure_ascii=False),
            ex=60,
        )
    except Exception:
        pass
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO id_enterprise_audit_event(
                id, org_id, actor_user_id, action, resource_type, resource_id, reason, details
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
            (
                new_id("eau"),
                actor.org_id,
                actor.user_id,
                "gateway.provider.health_check",
                "gateway_provider",
                actor.workspace_id,
                "",
                json_dumps({"total": len(results), "healthy": healthy, "unhealthy": unhealthy, "unknown": unknown}),
            ),
        )
        await conn.commit()
    return payload


@router.get("/providers/health")
async def get_provider_health(actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    try:
        cached = await redis.get(_provider_health_cache_key(actor.workspace_id))
    except Exception:
        cached = None
    if cached:
        try:
            payload = json.loads(cached)
            payload["cached"] = True
            return payload
        except (TypeError, ValueError):
            pass
    return {
        "results": [],
        "checked_at": None,
        "total": 0,
        "healthy": 0,
        "unhealthy": 0,
        "unknown": 0,
        "cached": False,
    }


@router.post("/channels", status_code=201)
async def create_channel(
    body: ChannelCreate, actor: Annotated[Actor, Depends(get_actor)]
):
    _require_admin(actor)
    _validate_channel_url(body.provider, body.base_url)
    channel_id = new_id("chn")
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO gw_channel(
                id, workspace_id, name, provider, base_url, credential_enc, models,
                weight, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                channel_id,
                actor.workspace_id,
                body.name,
                _provider_name(body.provider),
                body.base_url.rstrip("/"),
                encrypt_secret(body.api_key),
                body.models,
                body.weight,
                body.status,
            ),
        )
        await conn.commit()
    return {"id": channel_id, **body.model_dump(exclude={"api_key"})}


@router.post("/channels/import")
async def import_channels(body: ChannelImportRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    candidates: list[dict] = []
    errors: list[dict] = []
    for index, item in enumerate(body.channels):
        provider = _provider_name(str(item.get("provider") or item.get("type") or "openai-compatible"))
        base_url = str(item.get("base_url") or item.get("baseURL") or item.get("url") or "").strip()
        api_key = str(item.get("api_key") or item.get("key") or item.get("token") or "").strip()
        name = str(item.get("name") or item.get("channel_name") or f"Imported {index + 1}").strip()
        models = item.get("models") or item.get("model") or []
        if isinstance(models, str):
            models = [model.strip() for model in models.split(",") if model.strip()]
        try:
            if not name or len(name) > 100:
                raise ValueError("channel name is required and must be at most 100 characters")
            if not base_url:
                raise ValueError("base_url is required")
            _validate_channel_url(provider, base_url)
            candidates.append({"index": index, "name": name, "provider": provider, "base_url": base_url.rstrip("/"), "models": models[:100], "has_credential": bool(api_key), "api_key": api_key})
        except (HTTPException, ValueError) as exc:
            errors.append({"index": index, "error": exc.detail if isinstance(exc, HTTPException) else str(exc)})
    if body.dry_run or errors:
        return {"source": body.source, "dry_run": body.dry_run, "created": [], "candidates": [{key: value for key, value in item.items() if key != "api_key"} for item in candidates], "errors": errors}
    created: list[dict] = []
    async with pool.connection() as conn:
        for item in candidates:
            channel_id = new_id("chn")
            await conn.execute(
                "INSERT INTO gw_channel(id,workspace_id,name,provider,base_url,credential_enc,models,weight,status) VALUES (%s,%s,%s,%s,%s,%s,%s,100,'disabled')",
                (channel_id, actor.workspace_id, item["name"], item["provider"], item["base_url"], encrypt_secret(item["api_key"]), item["models"]),
            )
            created.append({"id": channel_id, "name": item["name"], "provider": item["provider"], "status": "disabled", "has_credential": bool(item["api_key"])})
        await conn.commit()
    return {"source": body.source, "dry_run": False, "created": created, "candidates": [], "errors": []}


@router.patch("/channels/{channel_id}")
async def update_channel(
    channel_id: str,
    body: ChannelCreate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require_admin(actor)
    _validate_channel_url(body.provider, body.base_url)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            UPDATE gw_channel SET name = %s, provider = %s, base_url = %s,
                credential_enc = COALESCE(%s, credential_enc), models = %s,
                weight = %s, status = %s, updated_at = now()
            WHERE id = %s AND workspace_id = %s RETURNING id
            """,
            (
                body.name,
                _provider_name(body.provider),
                body.base_url.rstrip("/"),
                encrypt_secret(body.api_key),
                body.models,
                body.weight,
                body.status,
                channel_id,
                actor.workspace_id,
            ),
        )
        if not await result.fetchone():
            raise HTTPException(status_code=404, detail="Channel not found")
        if body.status != "enabled":
            await _clear_channel_references(conn, actor.workspace_id, channel_id)
        await conn.commit()
    return {"id": channel_id, **body.model_dump(exclude={"api_key"})}


@router.delete("/channels/{channel_id}", status_code=204)
async def delete_channel(
    channel_id: str, actor: Annotated[Actor, Depends(get_actor)]
):
    _require_admin(actor)
    async with pool.connection() as conn:
        deleted = await conn.execute(
            """
            DELETE FROM gw_channel
            WHERE id = %s AND workspace_id = %s AND provider <> 'mock'
            RETURNING id
            """,
            (channel_id, actor.workspace_id),
        )
        if await deleted.fetchone():
            await _clear_channel_references(conn, actor.workspace_id, channel_id)
        await conn.commit()


@router.post("/channels/{channel_id}/health")
async def test_channel(
    channel_id: str, actor: Annotated[Actor, Depends(get_actor)]
):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT provider, base_url, credential_enc FROM gw_channel WHERE id = %s AND workspace_id = %s",
            (channel_id, actor.workspace_id),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Channel not found")
    healthy = row["provider"] == "mock"
    detail = "Local mock provider is ready"
    if not healthy:
        url_check = await validate_resolved_outbound_url(row["base_url"])
        if not url_check.allowed:
            raise HTTPException(status_code=422, detail=f"Unsafe channel URL: {url_check.reason}")
        try:
            headers = {}
            api_key = decrypt_secret(row["credential_enc"])
            provider = _provider_name(row["provider"])
            endpoint = row["base_url"].rstrip("/") + "/models"
            if provider == "anthropic" and api_key:
                headers["x-api-key"] = api_key
                headers["anthropic-version"] = "2023-06-01"
            elif provider == "gemini" and api_key:
                endpoint += f"?key={api_key}"
            elif api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.get(endpoint, headers=headers)
                healthy = response.is_success
                detail = f"Provider returned HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            detail = str(exc)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE gw_channel SET last_health = %s, updated_at = now() WHERE id = %s",
            ("healthy" if healthy else "unhealthy", channel_id),
        )
        await conn.commit()
    if not healthy:
        raise HTTPException(status_code=502, detail=detail)
    return {"healthy": True, "detail": detail}


# ----------------------------------------------------------------------
# 免费 / 公益大模型供应商预设目录与一键启用
# ----------------------------------------------------------------------


@router.get("/free-providers")
async def list_free_providers():
    """公开端点：返回所有免费 / 公益供应商预设目录（无需认证）。

    响应结构：
    ```
    {
        "items": [
            {
                "provider": "siliconflow",
                "name": "硅基流动 SiliconFlow（免费层）",
                "base_url": "https://api.siliconflow.cn/v1",
                "protocol": "openai",
                "signup_url": "...",
                "free_quota": "...",
                "free_models": [...],
                "capabilities": [...],
                "regions": [...],
                "retention_mode": "provider_retained",
                "notes": "...",
            },
            ...
        ],
        "total": 31
    }
    ```
    """
    cache_key = _cache_key("_global_", "free_providers", "static")
    cached = await cache_get(cache_key)
    if cached:
        return json.loads(cached)
    items = [
        # 将预设 key 作为 ``provider`` 字段返回（即启用端点 URL 中的
        # provider_key）；preset 自身的 ``provider`` 字段表示该预设对应的
        # PROVIDER_CATALOG 适配器（如 gemini_free -> gemini），保留在 ``catalog_provider``
        # 字段中以便客户端识别底层协议适配。
        {**preset, "provider": key, "catalog_provider": preset["provider"]}
        for key, preset in FREE_PROVIDER_PRESETS.items()
    ]
    # Contract《720》listFreeProviders: ListQuery -> ListResponse<FreeProviderPresetDTO>
    # 保留 items 与 total 字段向后兼容
    response = {
        "items": items,
        "total": len(items),
        "data": items,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(items)},
    }
    await cache_set(cache_key, json.dumps(response))
    return response


@router.get("/llm-status")
async def llm_status():
    """Return the current internal LLM client status.

    This endpoint does not require authentication — it returns operational
    metadata about the internal LLM pipeline (configured provider, method
    being used: mock/llm, available channels) for health monitoring.
    """
    return get_internal_llm_status()


@router.post("/llm-test")
async def llm_test(
    body: LlmTestRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """Send a simple test message through the LLM pipeline.

    Used for debugging and verifying the LLM integration end-to-end.
    Returns the response content, method (mock/llm), and timing info.
    """
    import time

    started = time.monotonic()
    result = await call_llm(
        messages=[{"role": "user", "content": body.message}],
        model=body.model,
        workspace_id=actor.workspace_id,
        actor=actor,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "content": result["content"],
        "tokens_used": result["tokens_used"],
        "model": result["model"],
        "method": result["method"],
        "elapsed_ms": elapsed_ms,
        "request_message": body.message,
        "request_model": body.model,
    }


@router.post("/free-providers/{provider_key}/enable", status_code=201)
async def enable_free_provider(
    provider_key: str,
    body: FreeProviderEnableRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """一键启用某个免费供应商（管理员权限）。

    根据预设自动创建 ``gw_channel`` 记录：
    - ``provider``、``base_url``、``free_models`` 等取自预设（可被请求体覆盖）
    - ``api_key`` 加密入库
    - 同一 workspace 下若已存在同名 + 同 provider + 同 base_url 的渠道，
      视为幂等启用：返回已存在的渠道信息而不重复创建
    """
    _require_admin(actor)
    preset = get_free_preset(provider_key)
    if preset is None:
        raise HTTPException(
            status_code=404,
            detail="Unknown free provider; GET /api/v1/gateway/free-providers for the catalog",
        )

    channel_name = (body.name or preset["name"]).strip()
    base_url = (body.base_url or preset["base_url"]).rstrip("/")
    models = body.models if body.models is not None else list(preset["free_models"])
    provider = preset["provider"]
    channel_status = "enabled" if body.enabled else "disabled"

    # 复用既有 URL 校验逻辑（同时确认 provider 在 catalog 中）
    _validate_channel_url(provider, base_url)

    async with pool.connection() as conn:
        # 幂等性：同一 workspace + 同名 + 同 provider + 同 base_url 视为重复启用
        existing_result = await conn.execute(
            """
            SELECT id, name, provider, base_url, models, weight, status, created_at, updated_at
            FROM gw_channel
            WHERE workspace_id = %s AND name = %s AND provider = %s AND base_url = %s
            LIMIT 1
            """,
            (actor.workspace_id, channel_name, provider, base_url),
        )
        existing = await existing_result.fetchone()
        if existing is not None:
            await conn.commit()
            return {
                "id": existing["id"],
                "provider": existing["provider"],
                "name": existing["name"],
                "base_url": existing["base_url"],
                "models": existing["models"],
                "weight": existing["weight"],
                "status": existing["status"],
                "created_at": existing["created_at"],
                "updated_at": existing["updated_at"],
                "preset": preset,
                "idempotent": True,
            }

        channel_id = new_id("chn")
        await conn.execute(
            """
            INSERT INTO gw_channel(
                id, workspace_id, name, provider, base_url, credential_enc, models,
                weight, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                channel_id,
                actor.workspace_id,
                channel_name,
                provider,
                base_url,
                encrypt_secret(body.api_key),
                models,
                100,
                channel_status,
            ),
        )
        await conn.commit()
    return {
        "id": channel_id,
        "provider": provider,
        "name": channel_name,
        "base_url": base_url,
        "models": models,
        "weight": 100,
        "status": channel_status,
        "preset": preset,
        "idempotent": False,
    }


@router.get("/model-mappings")
async def list_model_mappings(actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT m.id, m.model, m.channel_id, c.name AS channel_name,
                   m.upstream_model, c.status AS channel_status, m.updated_at
            FROM gw_model_mapping m
            JOIN gw_channel c ON c.id = m.channel_id AND c.workspace_id = m.workspace_id
            WHERE m.workspace_id = %s
            ORDER BY m.model, c.name
            """,
            (actor.workspace_id,),
        )
        # Contract《720》listModelMappings: ListQuery -> ListResponse<ModelMappingDTO>
        # 保留 items 字段向后兼容
        data = await result.fetchall()
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("/model-mappings", status_code=201)
async def create_model_mapping(
    body: ModelMappingCreate, actor: Annotated[Actor, Depends(get_actor)]
):
    _require_admin(actor)
    async with pool.connection() as conn:
        channel_result = await conn.execute(
            "SELECT id FROM gw_channel WHERE id = %s AND workspace_id = %s",
            (body.channel_id, actor.workspace_id),
        )
        if not await channel_result.fetchone():
            raise HTTPException(status_code=404, detail="Channel not found")
        mapping_id = new_id("gmm")
        result = await conn.execute(
            """
            INSERT INTO gw_model_mapping(id, workspace_id, model, channel_id, upstream_model)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(workspace_id, model, channel_id) DO UPDATE SET
                upstream_model = EXCLUDED.upstream_model, updated_at = now()
            RETURNING id
            """,
            (
                mapping_id,
                actor.workspace_id,
                body.model.strip(),
                body.channel_id,
                body.upstream_model.strip(),
            ),
        )
        row = await result.fetchone()
        await conn.commit()
    return {"id": row["id"], **body.model_dump()}


@router.delete("/model-mappings/{mapping_id}", status_code=204)
async def delete_model_mapping(
    mapping_id: str, actor: Annotated[Actor, Depends(get_actor)]
):
    _require_admin(actor)
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM gw_model_mapping WHERE id = %s AND workspace_id = %s",
            (mapping_id, actor.workspace_id),
        )
        await conn.commit()


@router.get("/token-groups")
async def list_token_groups(actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT g.id, g.name, g.rpm_limit, g.tpm_limit, g.model_whitelist,
                   g.pinned_channel_id, g.fallback_chain, g.model_mapping_override,
                   c.name AS pinned_channel_name, g.status,
                   COUNT(t.id) FILTER (WHERE t.revoked_at IS NULL) AS active_token_count,
                   g.created_at, g.updated_at
            FROM gw_token_group g
            LEFT JOIN gw_channel c
                ON c.id = g.pinned_channel_id AND c.workspace_id = g.workspace_id
            LEFT JOIN gw_token t
                ON t.group_id = g.id AND t.workspace_id = g.workspace_id
            WHERE g.workspace_id = %s
            GROUP BY g.id, c.name
            ORDER BY g.created_at DESC
            """,
            (actor.workspace_id,),
        )
        # Contract《720》listTokenGroups: ListQuery -> ListResponse<TokenGroupDTO>
        # 保留 items 字段向后兼容
        data = await result.fetchall()
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.get("/token-groups/{group_id}")
async def get_token_group(
    group_id: str, actor: Annotated[Actor, Depends(get_actor)]
):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, name, rpm_limit, tpm_limit, model_whitelist,
                   pinned_channel_id, fallback_chain, model_mapping_override,
                   status, created_at, updated_at
            FROM gw_token_group WHERE id = %s AND workspace_id = %s
            """,
            (group_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Token group not found")
    return row


@router.post("/token-groups", status_code=201)
async def create_token_group(
    body: TokenGroupRequest, actor: Annotated[Actor, Depends(get_actor)]
):
    _require_admin(actor)
    group_id = new_id("gtg")
    async with pool.connection() as conn:
        overrides, fallback_chain = _normalize_group_routing(body)
        await _validate_pinned_channel(conn, actor.workspace_id, body.pinned_channel_id)
        await _validate_group_mapping_override_channels(
            conn, actor.workspace_id, overrides
        )
        duplicate = await conn.execute(
            "SELECT 1 FROM gw_token_group WHERE workspace_id = %s AND name = %s",
            (actor.workspace_id, body.name.strip()),
        )
        if await duplicate.fetchone():
            raise HTTPException(status_code=409, detail="Token group name already exists")
        await conn.execute(
            """
            INSERT INTO gw_token_group(
                id, workspace_id, name, rpm_limit, tpm_limit, model_whitelist,
                pinned_channel_id, fallback_chain, model_mapping_override, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
            """,
            (
                group_id,
                actor.workspace_id,
                body.name.strip(),
                body.rpm_limit,
                body.tpm_limit,
                body.model_whitelist,
                body.pinned_channel_id,
                json_dumps(fallback_chain),
                json_dumps(overrides),
                body.status,
            ),
        )
        await conn.commit()
    return {
        "id": group_id,
        **body.model_dump(),
        "fallback_chain": fallback_chain,
        "model_mapping_override": overrides,
    }


@router.patch("/token-groups/{group_id}")
async def update_token_group(
    group_id: str,
    body: TokenGroupRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require_admin(actor)
    async with pool.connection() as conn:
        overrides, fallback_chain = _normalize_group_routing(body)
        await _validate_pinned_channel(conn, actor.workspace_id, body.pinned_channel_id)
        await _validate_group_mapping_override_channels(
            conn, actor.workspace_id, overrides
        )
        duplicate = await conn.execute(
            """
            SELECT 1 FROM gw_token_group
            WHERE workspace_id = %s AND name = %s AND id <> %s
            """,
            (actor.workspace_id, body.name.strip(), group_id),
        )
        if await duplicate.fetchone():
            raise HTTPException(status_code=409, detail="Token group name already exists")
        result = await conn.execute(
            """
            UPDATE gw_token_group SET name = %s, rpm_limit = %s, tpm_limit = %s,
                model_whitelist = %s, pinned_channel_id = %s, fallback_chain = %s::jsonb,
                model_mapping_override = %s::jsonb, status = %s,
                updated_at = now()
            WHERE id = %s AND workspace_id = %s RETURNING id
            """,
            (
                body.name.strip(),
                body.rpm_limit,
                body.tpm_limit,
                body.model_whitelist,
                body.pinned_channel_id,
                json_dumps(fallback_chain),
                json_dumps(overrides),
                body.status,
                group_id,
                actor.workspace_id,
            ),
        )
        if not await result.fetchone():
            raise HTTPException(status_code=404, detail="Token group not found")
        await conn.commit()
    return {
        "id": group_id,
        **body.model_dump(),
        "fallback_chain": fallback_chain,
        "model_mapping_override": overrides,
    }


@router.delete("/token-groups/{group_id}", status_code=204)
async def delete_token_group(
    group_id: str, actor: Annotated[Actor, Depends(get_actor)]
):
    _require_admin(actor)
    async with pool.connection() as conn:
        exists = await conn.execute(
            "SELECT 1 FROM gw_token_group WHERE id = %s AND workspace_id = %s",
            (group_id, actor.workspace_id),
        )
        if not await exists.fetchone():
            raise HTTPException(status_code=404, detail="Token group not found")
        await conn.execute(
            "UPDATE gw_token SET group_id = NULL WHERE workspace_id = %s AND group_id = %s",
            (actor.workspace_id, group_id),
        )
        await conn.execute(
            "DELETE FROM gw_token_group WHERE id = %s AND workspace_id = %s",
            (group_id, actor.workspace_id),
        )
        await conn.commit()


@router.get("/tokens")
async def list_tokens(actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT t.id, t.name, t.last_four, t.rpm_limit, t.tpm_limit,
                   t.model_whitelist, t.pinned_channel_id, c.name AS pinned_channel_name,
                   t.group_id, g.name AS group_name,
                   gc.name AS group_pinned_channel_name,
                   t.expires_at, t.revoked_at, t.created_at
            FROM gw_token t
            LEFT JOIN gw_channel c ON c.id = t.pinned_channel_id AND c.workspace_id = t.workspace_id
            LEFT JOIN gw_token_group g ON g.id = t.group_id AND g.workspace_id = t.workspace_id
            LEFT JOIN gw_channel gc
                ON gc.id = g.pinned_channel_id AND gc.workspace_id = g.workspace_id
            WHERE t.workspace_id = %s ORDER BY t.created_at DESC
            """,
            (actor.workspace_id,),
        )
        # Contract《720》listGatewayTokens: ListQuery -> ListResponse<GatewayTokenDTO>
        # 保留 items 字段向后兼容
        data = await result.fetchall()
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("/tokens", status_code=201)
async def create_token(
    body: GatewayTokenCreate, actor: Annotated[Actor, Depends(get_actor)]
):
    _require_admin(actor)
    raw = "sk-wama-" + secrets.token_urlsafe(36)[:43]
    token_id = new_id("gwt")
    async with pool.connection() as conn:
        await _validate_pinned_channel(conn, actor.workspace_id, body.pinned_channel_id)
        await _validate_token_group(conn, actor.workspace_id, body.group_id)
        await conn.execute(
            """
            INSERT INTO gw_token(
                id, workspace_id, name, key_hash, last_four, rpm_limit, tpm_limit,
                model_whitelist, pinned_channel_id, group_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                token_id,
                actor.workspace_id,
                body.name,
                hash_secret(raw),
                raw[-4:],
                body.rpm_limit,
                body.tpm_limit,
                body.model_whitelist,
                body.pinned_channel_id,
                body.group_id,
            ),
        )
        await conn.commit()
    return {
        "id": token_id,
        "name": body.name,
        "key": raw,
        "last_four": raw[-4:],
        "pinned_channel_id": body.pinned_channel_id,
        "group_id": body.group_id,
    }


@router.delete("/tokens/{token_id}", status_code=204)
async def revoke_token(
    token_id: str, actor: Annotated[Actor, Depends(get_actor)]
):
    _require_admin(actor)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE gw_token SET revoked_at = now() WHERE id = %s AND workspace_id = %s",
            (token_id, actor.workspace_id),
        )
        await conn.commit()


@router.get("/pricing")
async def list_pricing(actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT model, input_per_million, output_per_million, markup_percent, updated_at
            FROM gw_model_price WHERE workspace_id = %s ORDER BY model
            """,
            (actor.workspace_id,),
        )
        # Contract《720》listModelPricing: ListQuery -> ListResponse<ModelPriceDTO>
        # 保留 items 字段向后兼容
        data = await result.fetchall()
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.put("/pricing/{model}")
async def update_pricing(
    model: str, body: PricingUpdate, actor: Annotated[Actor, Depends(get_actor)]
):
    if actor.role != "owner":
        raise HTTPException(status_code=403, detail="Owner role required")
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO gw_model_price(workspace_id, model, input_per_million, output_per_million, markup_percent)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(workspace_id, model) DO UPDATE SET
                input_per_million = EXCLUDED.input_per_million,
                output_per_million = EXCLUDED.output_per_million,
                markup_percent = EXCLUDED.markup_percent,
                updated_at = now()
            """,
            (
                actor.workspace_id,
                model,
                body.input_per_million,
                body.output_per_million,
                body.markup_percent,
            ),
        )
        await conn.commit()
    return {"model": model, **body.model_dump(exclude={"model"})}


@router.get("/logs")
async def list_logs(
    actor: Annotated[Actor, Depends(get_actor)], limit: int = 50
):
    _require_admin(actor)
    limit = min(max(limit, 1), 100)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT request_id, token_id, channel_id, model, prompt_tokens,
                   completion_tokens, total_tokens, cost_credits, latency_ms,
                   status_code, error_code, created_at
            FROM gw_request_log WHERE workspace_id = %s
            ORDER BY created_at DESC LIMIT %s
            """,
            (actor.workspace_id, limit),
        )
        # Contract《720》listGatewayRequestLogs: ListQuery -> ListResponse<RequestLogDTO>
        # 保留 items 字段向后兼容
        data = await result.fetchall()
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.get("/usage")
async def usage(actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        totals_result = await conn.execute(
            """
            SELECT COUNT(*) AS requests, COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(cost_credits), 0) AS cost_credits,
                   COALESCE(AVG(latency_ms), 0)::int AS avg_latency_ms
            FROM gw_request_log WHERE workspace_id = %s
            """,
            (actor.workspace_id,),
        )
        daily_result = await conn.execute(
            """
            SELECT created_at::date AS date, COUNT(*) AS requests,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(cost_credits), 0) AS cost_credits
            FROM gw_request_log
            WHERE workspace_id = %s AND created_at >= now() - interval '7 days'
            GROUP BY created_at::date ORDER BY date
            """,
            (actor.workspace_id,),
        )
        return {"totals": await totals_result.fetchone(), "daily": await daily_result.fetchall()}


def _model_is_allowed(token, model: str) -> bool:
    return not (
        (token["model_whitelist"] and model not in token["model_whitelist"])
        or (
            token["group_model_whitelist"]
            and model not in token["group_model_whitelist"]
        )
    )


async def _resolve_model_candidates(
    conn,
    workspace_id: str,
    model: str,
    pinned_channel_id: str | None,
    model_mapping_override: dict[str, dict[str, str]],
) -> list[dict]:
    overrides = model_mapping_override.get(model, {})
    if overrides:
        result = await conn.execute(
            """
            SELECT c.id, c.provider, c.base_url, c.credential_enc, c.weight,
                   COALESCE(c.id = %s, FALSE) AS pinned
            FROM gw_channel c
            WHERE c.workspace_id = %s AND c.status = 'enabled' AND c.id = ANY(%s)
            ORDER BY CASE WHEN c.id = %s THEN 0 ELSE 1 END,
                     CASE WHEN c.provider = 'mock' THEN 1 ELSE 0 END,
                     -ln(GREATEST(random(), 0.000001)) / GREATEST(c.weight, 1), c.id
            """,
            (pinned_channel_id, workspace_id, list(overrides), pinned_channel_id),
        )
        channels = await result.fetchall()
        return [
            {
                "id": channel["id"],
                "provider": channel["provider"],
                "base_url": channel["base_url"],
                "api_key": decrypt_secret(channel["credential_enc"]),
                "weight": channel["weight"],
                "upstream_model": overrides[channel["id"]],
                "pinned": channel["pinned"],
            }
            for channel in channels
        ]
    result = await conn.execute(
        """
        WITH candidates AS (
            SELECT c.id, c.provider, c.base_url, c.credential_enc, c.models, c.weight,
                   COALESCE(m.upstream_model, %s) AS upstream_model,
                   COALESCE(c.id = %s, FALSE) AS pinned
            FROM gw_channel c
            LEFT JOIN gw_model_mapping m
                ON m.channel_id = c.id
                AND m.workspace_id = c.workspace_id
                AND m.model = %s
            WHERE c.workspace_id = %s AND c.status = 'enabled'
              AND (m.id IS NOT NULL OR cardinality(c.models) = 0 OR %s = ANY(c.models))
        )
        SELECT id, provider, base_url, credential_enc, weight, upstream_model, pinned
        FROM candidates
        ORDER BY CASE WHEN pinned THEN 0 ELSE 1 END,
                 CASE WHEN provider = 'mock' THEN 1 ELSE 0 END,
                 -ln(GREATEST(random(), 0.000001)) / GREATEST(weight, 1), id
        """,
        (model, pinned_channel_id, model, workspace_id, model),
    )
    channels = await result.fetchall()
    return [
        {
            "id": channel["id"],
            "provider": channel["provider"],
            "base_url": channel["base_url"],
            "api_key": decrypt_secret(channel["credential_enc"]),
            "weight": channel["weight"],
            "upstream_model": channel["upstream_model"],
            "pinned": channel["pinned"],
        }
        for channel in channels
    ]


@internal_router.post("/resolve", dependencies=[Depends(require_internal)])
async def resolve_route(body: ResolveRequest):
    if not body.api_key and not body.workspace_id:
        raise HTTPException(status_code=401, detail="API key or workspace context required")
    token = None
    workspace_id = body.workspace_id
    async with pool.connection() as conn:
        if body.api_key:
            result = await conn.execute(
                """
                SELECT t.id, t.workspace_id, t.rpm_limit, t.tpm_limit,
                       t.model_whitelist, t.pinned_channel_id, t.group_id,
                       g.rpm_limit AS group_rpm_limit,
                       g.tpm_limit AS group_tpm_limit,
                       g.model_whitelist AS group_model_whitelist,
                       g.pinned_channel_id AS group_pinned_channel_id,
                       g.fallback_chain AS group_fallback_chain,
                       g.model_mapping_override AS group_model_mapping_override
                FROM gw_token t
                LEFT JOIN gw_token_group g
                    ON g.id = t.group_id AND g.workspace_id = t.workspace_id
                WHERE t.key_hash = %s AND t.revoked_at IS NULL
                  AND (t.expires_at IS NULL OR t.expires_at > now())
                  AND (t.group_id IS NULL OR g.status = 'enabled')
                """,
                (hash_secret(body.api_key),),
            )
            token = await result.fetchone()
            if not token:
                raise HTTPException(status_code=401, detail="E01001")
            workspace_id = token["workspace_id"]
            if not _model_is_allowed(token, body.model):
                raise HTTPException(status_code=403, detail="E01002")
        balance_result = await conn.execute(
            "SELECT granted_balance + purchased_balance - frozen_balance AS balance FROM bill_account WHERE workspace_id = %s",
            (workspace_id,),
        )
        balance = await balance_result.fetchone()
        if not balance or balance["balance"] <= 0:
            raise HTTPException(status_code=402, detail="E01004")
        pinned_channel_id = (
            token["pinned_channel_id"] or token["group_pinned_channel_id"]
            if token
            else None
        )
        overrides = token["group_model_mapping_override"] or {} if token else {}
        channels = await _resolve_model_candidates(
            conn, workspace_id, body.model, pinned_channel_id, overrides
        )
        fallbacks = []
        if token:
            fallback_chain = token["group_fallback_chain"] or {}
            for fallback_model in fallback_chain.get(body.model, []):
                if not _model_is_allowed(token, fallback_model):
                    continue
                fallback_channels = await _resolve_model_candidates(
                    conn,
                    workspace_id,
                    fallback_model,
                    pinned_channel_id,
                    overrides,
                )
                if fallback_channels:
                    fallbacks.append(
                        {"model": fallback_model, "channels": fallback_channels}
                    )
    if not channels:
        raise HTTPException(status_code=404, detail="E01006")
    return {
        "workspace_id": workspace_id,
        "token_id": token["id"] if token else None,
        "group_id": token["group_id"] if token else None,
        "rpm_limit": token["rpm_limit"] if token else 1000,
        "tpm_limit": token["tpm_limit"] if token else 1000000,
        "group_rpm_limit": token["group_rpm_limit"] if token and token["group_id"] else 0,
        "group_tpm_limit": token["group_tpm_limit"] if token and token["group_id"] else 0,
        "channel": channels[0],
        "channels": channels,
        "fallbacks": fallbacks,
    }


RATE_LIMIT_BATCH_SCRIPT = """
local estimated = tonumber(ARGV[1])
local maxRPM = 0
local maxTPM = 0
local retryAfter = 1
for scope = 1, #KEYS / 2 do
    local keyIndex = (scope - 1) * 2 + 1
    local argIndex = (scope - 1) * 2 + 2
    local rpmLimit = tonumber(ARGV[argIndex])
    local tpmLimit = tonumber(ARGV[argIndex + 1])
    local rpm = tonumber(redis.call('GET', KEYS[keyIndex]) or '0')
    local tpm = tonumber(redis.call('GET', KEYS[keyIndex + 1]) or '0')
    local ttl = math.max(redis.call('TTL', KEYS[keyIndex]), redis.call('TTL', KEYS[keyIndex + 1]))
    if ttl > retryAfter then retryAfter = ttl end
    if rpm + 1 > rpmLimit or tpm + estimated > tpmLimit then
        return {0, rpm, tpm, math.max(retryAfter, 1)}
    end
end
for scope = 1, #KEYS / 2 do
    local keyIndex = (scope - 1) * 2 + 1
    local rpm = redis.call('INCR', KEYS[keyIndex])
    if rpm == 1 then redis.call('EXPIRE', KEYS[keyIndex], 60) end
    local tpm = redis.call('INCRBY', KEYS[keyIndex + 1], estimated)
    if tpm == estimated then redis.call('EXPIRE', KEYS[keyIndex + 1], 60) end
    if rpm > maxRPM then maxRPM = rpm end
    if tpm > maxTPM then maxTPM = tpm end
end
return {1, maxRPM, maxTPM, 1}
"""


async def _check_rate_limit_scopes(
    scopes: list[RateLimitScope], estimated_tokens: int
) -> dict:
    keys: list[str] = []
    args: list[int] = [estimated_tokens]
    for scope in scopes:
        prefix = f"gw:rate:{scope.actor_key}"
        keys.extend([f"{prefix}:rpm", f"{prefix}:tpm"])
        args.extend([scope.rpm_limit, scope.tpm_limit])
    result = await redis.eval(
        RATE_LIMIT_BATCH_SCRIPT,
        len(keys),
        *keys,
        *args,
    )
    return {
        "allowed": bool(result[0]),
        "rpm_used": int(result[1]),
        "tpm_used": int(result[2]),
        "retry_after": max(int(result[3]), 1),
    }


@internal_router.post("/rate-limit", dependencies=[Depends(require_internal)])
async def check_rate_limit(body: RateLimitRequest):
    return await _check_rate_limit_scopes(
        [
            RateLimitScope(
                actor_key=body.actor_key,
                rpm_limit=body.rpm_limit,
                tpm_limit=body.tpm_limit,
            )
        ],
        body.estimated_tokens,
    )


@internal_router.post("/rate-limit/batch", dependencies=[Depends(require_internal)])
async def check_rate_limit_batch(body: RateLimitBatchRequest):
    return await _check_rate_limit_scopes(body.scopes, body.estimated_tokens)


@internal_router.post("/meter", dependencies=[Depends(require_internal)])
async def record_meter(body: MeterRequest):
    return await settle_meter(body)


@internal_router.post("/reserve", dependencies=[Depends(require_internal)])
async def reserve_budget(body: ReservationRequest):
    async with pool.connection() as conn:
        async with conn.transaction():
            await expire_credit_grants_in_transaction(conn, body.workspace_id)
            existing_result = await conn.execute(
                "SELECT id, workspace_id, estimated_cost, status FROM bill_reservation WHERE request_id = %s FOR UPDATE",
                (body.request_id,),
            )
            existing = await existing_result.fetchone()
            if existing:
                _assert_workspace_match(existing, body.workspace_id, "Reservation")
                return {"duplicate": True, "reservation_id": existing["id"], "estimated_cost": existing["estimated_cost"], "status": existing["status"]}
            price_result = await conn.execute(
                "SELECT input_per_million, output_per_million, markup_percent FROM gw_model_price WHERE workspace_id = %s AND model = %s",
                (body.workspace_id, body.model),
            )
            price = await price_result.fetchone() or {
                "input_per_million": Decimal("1"), "output_per_million": Decimal("2"), "markup_percent": Decimal("10")
            }
            estimated_cost = estimate_cost(0, body.estimated_tokens, price)
            account_result = await conn.execute(
                "SELECT id, granted_balance, purchased_balance, frozen_balance FROM bill_account WHERE workspace_id = %s FOR UPDATE",
                (body.workspace_id,),
            )
            account = await account_result.fetchone()
            if not account:
                raise HTTPException(status_code=404, detail="Billing account missing")
            available = account["granted_balance"] + account["purchased_balance"] - account["frozen_balance"]
            if available < estimated_cost:
                raise HTTPException(status_code=402, detail="E01004")
            await conn.execute(
                "UPDATE bill_account SET frozen_balance = frozen_balance + %s, version = version + 1, updated_at = now() WHERE id = %s",
                (estimated_cost, account["id"]),
            )
            reservation_id = new_id("res")
            await conn.execute(
                "INSERT INTO bill_reservation(id, workspace_id, request_id, model, estimated_cost, status) VALUES (%s, %s, %s, %s, %s, 'frozen')",
                (reservation_id, body.workspace_id, body.request_id, body.model, estimated_cost),
            )
            return {"duplicate": False, "reservation_id": reservation_id, "estimated_cost": estimated_cost, "status": "frozen"}


@internal_router.post("/release", dependencies=[Depends(require_internal)])
async def release_budget(body: ReservationReleaseRequest):
    async with pool.connection() as conn:
        async with conn.transaction():
            reference_result = await conn.execute(
                "SELECT workspace_id FROM bill_reservation WHERE request_id = %s",
                (body.request_id,),
            )
            reference = await reference_result.fetchone()
            if not reference:
                return {"duplicate": False, "status": "missing"}
            account_result = await conn.execute(
                "SELECT id FROM bill_account WHERE workspace_id = %s FOR UPDATE",
                (reference["workspace_id"],),
            )
            account = await account_result.fetchone()
            if not account:
                raise HTTPException(status_code=404, detail="Billing account missing")
            result = await conn.execute(
                "SELECT id, workspace_id, estimated_cost, status FROM bill_reservation WHERE request_id = %s FOR UPDATE",
                (body.request_id,),
            )
            reservation = await result.fetchone()
            if not reservation or reservation["status"] != "frozen":
                return {"duplicate": bool(reservation), "status": reservation["status"] if reservation else "missing"}
            released = await conn.execute(
                """
                UPDATE bill_account
                SET frozen_balance = frozen_balance - %s, version = version + 1, updated_at = now()
                WHERE id = %s AND frozen_balance >= %s
                RETURNING id
                """,
                (reservation["estimated_cost"], account["id"], reservation["estimated_cost"]),
            )
            if not await released.fetchone():
                raise HTTPException(status_code=409, detail="Reservation state is invalid")
            await conn.execute(
                "UPDATE bill_reservation SET status = 'released', settled_at = now() WHERE id = %s",
                (reservation["id"],),
            )
            return {"duplicate": False, "status": "released"}
