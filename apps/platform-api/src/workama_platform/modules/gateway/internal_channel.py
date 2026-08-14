"""Internal LLM channel bootstrap (v7.160).

确保 platform-api 内部 LLM 调用（如 ``memory_vector`` 的结构化记忆抽取）
有一条可用的 ``gw_channel`` 记录。渠道配置来自环境变量：

- ``WORKAMA_INTERNAL_LLM_PROVIDER``（默认 ``auto``）：免费供应商预设 key；
  设为 ``auto`` 时自动选择最佳可用免费供应商
- ``WORKAMA_INTERNAL_LLM_API_KEY``：上游供应商真实 API Key

若 API Key 未配置，函数直接返回（``memory_vector`` 会回退到确定性 mock 抽取）。
若配置了，则在 ``workspace_id='system'`` 下幂等创建一条 ``gw_channel`` 记录。

注意：``gw_channel`` 表没有 ``kind`` 列，因此幂等性以
``(workspace_id='system', provider, name=INTERNAL_CHANNEL_NAME)`` 为键。

启动钩子级别的失败（如 'system' 工作区不存在、DB 异常）只记录 warning，
不抛出 —— 内部渠道是可选的，不能阻断 platform-api 启动。
"""
from __future__ import annotations

import logging
import os

from workama_platform.core import encrypt_secret, new_id, pool
from workama_platform.modules.gateway.free_presets import (
    FREE_PROVIDER_PRESETS,
    get_free_preset,
)

LOGGER = logging.getLogger("workama.platform-api.gateway")

INTERNAL_WORKSPACE_ID = "system"
INTERNAL_CHANNEL_NAME = "WorkAMA Internal LLM Channel"
DEFAULT_INTERNAL_PROVIDER = "siliconflow"
AUTO_SELECT_PROVIDER = "auto"

# Preference order for auto-selecting a free provider.
# Providers earlier in this list are preferred (more capabilities, better
# reliability, larger free quotas).  Only providers with ``free_models``
# and a non-localhost ``base_url`` are considered candidates.
_AUTO_SELECT_PREFERENCE = [
    "siliconflow",
    "openrouter",
    "groq",
    "deepseek",
    "zhipu",
    "glm_api_chat",
    "qwen",
    "cerebras",
    "together",
    "mistral_free",
    "modelscope",
    "iflytek",
    "kimi",
    "doubao",
    "huggingface",
    "github",
    "cohere",
    "nvidia",
    "xai",
    "ollama",
]


def auto_select_best_free_provider() -> str | None:
    """Auto-select the best available free provider preset key.

    Scans ``FREE_PROVIDER_PRESETS`` in preference order and returns the
    first key that has at least one ``free_model`` and a non-localhost
    ``base_url``.  Returns ``None`` if no suitable provider is found.
    """
    for key in _AUTO_SELECT_PREFERENCE:
        preset = FREE_PROVIDER_PRESETS.get(key)
        if preset is None:
            continue
        models = preset.get("free_models") or []
        base_url = (preset.get("base_url") or "").strip()
        if models and base_url and "localhost" not in base_url:
            return key
    # Fallback: scan all presets outside the preference list
    for key, preset in FREE_PROVIDER_PRESETS.items():
        if key in _AUTO_SELECT_PREFERENCE:
            continue
        models = preset.get("free_models") or []
        base_url = (preset.get("base_url") or "").strip()
        if models and base_url and "localhost" not in base_url:
            return key
    return None


def get_internal_llm_status() -> dict:
    """Return the current internal LLM client status (for health endpoint).

    Returns a dict with:
    - provider: the resolved provider key (or None if not configured)
    - method: "llm" if API key is set, "mock" otherwise
    - auto_selected: True if provider was auto-selected
    - gateway_url: the gateway URL being used
    - available_channels: count of free provider presets with models
    """
    raw_provider = os.getenv("WORKAMA_INTERNAL_LLM_PROVIDER", "").strip().lower()
    api_key = os.getenv("WORKAMA_INTERNAL_LLM_API_KEY", "").strip()
    gateway_url = os.getenv("WORKAMA_GATEWAY_URL", "http://gateway:8080").rstrip("/")

    auto_selected = False
    if raw_provider == AUTO_SELECT_PROVIDER or not raw_provider:
        resolved = auto_select_best_free_provider()
        auto_selected = True
    else:
        resolved = raw_provider

    available_channels = sum(
        1
        for p in FREE_PROVIDER_PRESETS.values()
        if (p.get("free_models") or []) and (p.get("base_url") or "").strip()
    )

    return {
        "provider": resolved,
        "method": "llm" if api_key else "mock",
        "auto_selected": auto_selected,
        "gateway_url": gateway_url,
        "api_key_configured": bool(api_key),
        "available_channels": available_channels,
        "total_free_presets": len(FREE_PROVIDER_PRESETS),
    }


async def ensure_internal_channel() -> None:
    """Ensure a system-owned internal LLM channel exists.

    读取 ``WORKAMA_INTERNAL_LLM_PROVIDER``（默认 ``auto``）和
    ``WORKAMA_INTERNAL_LLM_API_KEY``。API Key 未配置时直接返回。
    当 provider 设为 ``auto`` 或未设置时，自动选择最佳可用免费供应商。
    否则在 ``workspace_id='system'`` 下幂等 upsert 一条 ``gw_channel``。

    任何异常（含 'system' 工作区不存在导致的 FK 违反）只记 warning，
    不抛出，确保启动不被可选功能阻断。
    """
    raw_provider = os.getenv("WORKAMA_INTERNAL_LLM_PROVIDER", "").strip().lower()
    api_key = os.getenv("WORKAMA_INTERNAL_LLM_API_KEY", "").strip()
    if not api_key:
        LOGGER.warning(
            "WORKAMA_INTERNAL_LLM_API_KEY not set; internal LLM channel not "
            "created (memory_vector extraction will fall back to mock)."
        )
        return

    # Auto-select best free provider when WORKAMA_INTERNAL_LLM_PROVIDER
    # is set to "auto" or left empty.
    if raw_provider == AUTO_SELECT_PROVIDER or not raw_provider:
        provider = auto_select_best_free_provider() or DEFAULT_INTERNAL_PROVIDER
        LOGGER.info(
            "Auto-selected internal LLM provider: %s", provider,
        )
    else:
        provider = raw_provider

    preset = get_free_preset(provider)
    if preset is None:
        LOGGER.warning(
            "WORKAMA_INTERNAL_LLM_PROVIDER=%s is not a known free preset; "
            "internal LLM channel not created.",
            provider,
        )
        return
    base_url = preset["base_url"].rstrip("/")
    models = list(preset.get("free_models", []))

    try:
        async with pool.connection() as conn:
            existing = await conn.execute(
                """
                SELECT id FROM gw_channel
                WHERE workspace_id = %s AND provider = %s AND name = %s
                LIMIT 1
                """,
                (INTERNAL_WORKSPACE_ID, provider, INTERNAL_CHANNEL_NAME),
            )
            if await existing.fetchone():
                return
            await conn.execute(
                """
                INSERT INTO gw_channel(
                    id, workspace_id, name, provider, base_url, credential_enc,
                    models, weight, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'enabled')
                """,
                (
                    new_id("chn"),
                    INTERNAL_WORKSPACE_ID,
                    INTERNAL_CHANNEL_NAME,
                    provider,
                    base_url,
                    encrypt_secret(api_key),
                    models,
                    100,
                ),
            )
            await conn.commit()
    except Exception as exc:  # noqa: BLE001 — 启动钩子不能因可选渠道失败而中断
        LOGGER.warning(
            "ensure_internal_channel failed (provider=%s): %s", provider, exc
        )
        return
    LOGGER.info(
        "ensure_internal_channel: ensured internal channel for provider=%s "
        "(base_url=%s).",
        provider,
        base_url,
    )
