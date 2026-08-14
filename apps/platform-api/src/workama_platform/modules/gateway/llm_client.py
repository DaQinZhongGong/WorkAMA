"""统一 LLM 客户端（v7.159）。

提供 ``call_llm`` 函数作为 ``memory_vector`` / ``assistant`` / ``workflow`` 模块的
公共 LLM 调用入口，集中处理：

- 网关地址与鉴权（``WORKAMA_GATEWAY_URL`` + ``WORKAMA_INTERNAL_LLM_API_KEY``）
- 失败回退到确定性 mock 响应（不抛错，保证调用方稳定）
- 响应归一化为 ``{"content", "tokens_used", "model", "method"}`` 结构

设计原则：
- 单一入口：所有内部 LLM 调用都走这里，方便后续替换底层实现
- 容错优先：API Key 未配置 / 网络错误 / 4xx/5xx / 超时 → 都返回 mock 响应
- 可测试：纯函数式调用，依赖通过环境变量注入，便于 mock httpx 测试
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

LOGGER = logging.getLogger("workama.platform-api.gateway.llm_client")

DEFAULT_GATEWAY_URL = "http://gateway:8080"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT = 30.0


def _mock_response(messages: list[dict], model: str) -> dict:
    """生成确定性 mock 响应（不依赖外部服务）。

    根据消息内容生成稳定的 mock 内容，便于调用方验证流程。
    """
    user_messages = [
        str(m.get("content") or "")
        for m in messages
        if m.get("role") == "user"
    ]
    system_messages = [
        str(m.get("content") or "")
        for m in messages
        if m.get("role") == "system"
    ]
    user_text = user_messages[0] if user_messages else ""
    system_text = system_messages[0] if system_messages else ""
    content_parts = ["[mock-llm]"]
    if system_text:
        content_parts.append(f"system={system_text[:80]}")
    content_parts.append(f"model={model}")
    content_parts.append(f"user={user_text[:200]}")
    content = " | ".join(content_parts)
    return {
        "content": content,
        "tokens_used": max(1, len(content) // 4),
        "model": model,
        "method": "mock",
    }


async def call_llm(
    messages: list[dict],
    model: str,
    workspace_id: str,
    actor: Any,
    **kwargs: Any,
) -> dict:
    """调用 gateway LLM，返回归一化响应 dict。

    Args:
        messages: OpenAI 消息列表，``[{"role": "system"|"user", "content": str}, ...]``
        model: 模型名（如 ``gpt-4o-mini``）
        workspace_id: 工作区 id，用于 gateway 鉴权头
        actor: 调用方 Actor，用于日志和鉴权头
        **kwargs: 可选参数：
            - temperature: float，默认 0.2
            - max_tokens: int，默认 2048
            - timeout: float，默认 30.0
            - disabled: bool，强制走 mock（不读环境变量）

    Returns:
        ``{"content": str, "tokens_used": int, "model": str, "method": "llm"|"mock"}``

    容错策略：
    - ``disabled=True`` → 直接走 mock
    - ``WORKAMA_INTERNAL_LLM_API_KEY`` 未配置 → 走 mock（log warning）
    - 网络超时/连接错误/HTTP 错误 → 走 mock
    - 4xx/5xx 响应 → 走 mock
    - 响应体非 JSON / 缺 choices → 走 mock
    - 内容为空 → 走 mock
    """
    disabled = bool(kwargs.get("disabled"))
    if disabled:
        return _mock_response(messages, model)

    api_key = os.getenv("WORKAMA_INTERNAL_LLM_API_KEY", "").strip()
    if not api_key:
        LOGGER.warning(
            "WORKAMA_INTERNAL_LLM_API_KEY not set; returning mock LLM response."
        )
        return _mock_response(messages, model)

    gateway_url = os.getenv("WORKAMA_GATEWAY_URL", DEFAULT_GATEWAY_URL).rstrip("/")
    timeout = float(kwargs.get("timeout") or DEFAULT_TIMEOUT)
    temperature = float(kwargs.get("temperature") or 0.2)
    max_tokens = int(kwargs.get("max_tokens") or 2048)
    url = f"{gateway_url}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Workspace-Id": workspace_id,
        "X-Actor-Id": getattr(actor, "user_id", "") or "",
    }
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=payload)
    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as exc:
        LOGGER.warning(
            "llm_client call_llm gateway request failed: %s; using mock.", exc
        )
        return _mock_response(messages, model)

    if response.status_code >= 400:
        LOGGER.warning(
            "llm_client call_llm gateway returned status=%s; using mock.",
            response.status_code,
        )
        return _mock_response(messages, model)

    try:
        body = response.json()
    except (ValueError, TypeError):
        LOGGER.warning(
            "llm_client call_llm gateway returned non-JSON body; using mock."
        )
        return _mock_response(messages, model)

    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices:
        return _mock_response(messages, model)
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = str(message.get("content") or "")
    if not content:
        return _mock_response(messages, model)

    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    try:
        tokens_used = int(usage.get("total_tokens") or (len(content) // 4))
    except (TypeError, ValueError):
        tokens_used = max(1, len(content) // 4)

    return {
        "content": content,
        "tokens_used": tokens_used,
        "model": model,
        "method": "llm",
    }
