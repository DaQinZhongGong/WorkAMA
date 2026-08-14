"""轻量 OpenAI 兼容转发层（Relay）。

提供标准 OpenAI 兼容的 ``/v1/chat/completions`` 与 ``/v1/models`` 端点，
让免费供应商预设真正可被外部客户端调用。转发层实现以下最小管道：

    认证 → 解析路由 → 协议适配 → 转发 → 计量记录

- 认证：解析 ``Authorization: Bearer sk-wama-xxx``，查询 ``gw_token`` 校验
- 解析路由：复用 ``gateway.router.resolve_route``，得到渠道与已解密凭据
- 协议适配：按 ``PROVIDER_CATALOG[provider].protocol`` 适配请求
  - ``openai``    直接转发，``Authorization: Bearer {api_key}``
  - ``anthropic`` 转换为 Anthropic Messages 格式，``x-api-key`` 头
  - ``gemini``    转换为 Gemini generateContent 格式，``?key=`` 参数
- 转发：``httpx.AsyncClient`` 支持流式 (SSE) 与非流式
- 计量记录：将用量写入 ``gw_request_log``

错误统一采用 OpenAI 兼容结构::

    {"error": {"code": "E01006", "message": "...", "type": "invalid_request_error"}}
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from workama_platform.core import hash_secret, new_id, pool
from workama_platform.modules.gateway.router import (
    PROVIDER_CATALOG,
    ResolveRequest,
    _provider_name,
    resolve_route,
)

router = APIRouter(prefix="/v1", tags=["openai-compatible"])

# 上游转发超时：读 60s，连接 10s
_UPSTREAM_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# E01xxx 错误码 → HTTP 状态码映射
_ERROR_STATUS: dict[str, int] = {
    "E01001": 401,  # 无效或缺失令牌
    "E01002": 403,  # 模型未授权
    "E01004": 402,  # 余额不足
    "E01006": 404,  # 模型 / 渠道未找到
}

# ----------------------------------------------------------------------
# Go 网关 feature flag：下线 Python relay，统一由 Go 网关处理 LLM 调用
# ----------------------------------------------------------------------

# 读取环境变量 GATEWAY_GO_CHAT_COMPLETIONS_ENABLED（默认 true）。
# 启用时，/v1/chat/completions 和 /v1/models 反向代理到 Go 网关；
# 禁用时（设为 false/0/no），走 Python relay 原有逻辑作为 fallback。
GATEWAY_GO_ENABLED = os.getenv(
    "GATEWAY_GO_CHAT_COMPLETIONS_ENABLED", "true"
).lower() in ("true", "1", "yes")

# Go 网关 base URL，默认 http://gateway:8080（docker-compose 服务名）。
GATEWAY_GO_URL = os.getenv("GATEWAY_GO_URL", "http://gateway:8080")

# Go 网关反向代理超时：读 120s（LLM 推理可能较慢），连接 10s
_GATEWAY_PROXY_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------


def _openai_error(
    code: str,
    message: str,
    error_type: str = "invalid_request_error",
    status: int = 400,
) -> JSONResponse:
    """构造 OpenAI 兼容的错误响应。"""
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "type": error_type,
            }
        },
    )


def _parse_bearer(request: Request) -> str | None:
    """从 ``Authorization`` 头解析 Bearer 令牌，缺失或格式错误返回 None。"""
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return None


async def _resolve_channel(api_key: str, model: str) -> dict:
    """调用既有 ``resolve_route`` 解析模型→渠道，返回 resolve 结果。

    ``resolve_route`` 内部已通过 ``decrypt_secret`` 解密渠道凭据
    （``channel.api_key`` 即为上游明文密钥），并完成令牌校验、余额检查、
    模型白名单校验与渠道路由。
    """
    try:
        return await resolve_route(ResolveRequest(api_key=api_key, model=model))
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "model resolution failed"
        # 沿用 resolve_route 抛出的 E01xxx 错误码，未识别则用 E01000
        code = detail if detail.startswith("E010") else "E01000"
        status = _ERROR_STATUS.get(code, exc.status_code)
        raise _ResolveError(code, detail, status)


class _ResolveError(Exception):
    """解析路由失败时抛出，携带 OpenAI 兼容错误码。"""

    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ----------------------------------------------------------------------
# 协议适配：构造上游请求
# ----------------------------------------------------------------------


def _adapt_request(channel: dict, body: dict, protocol: str) -> tuple[str, dict, dict]:
    """按协议将 OpenAI 请求转换为上游格式，返回 (url, headers, payload)。

    ``channel`` 来自 ``resolve_route`` 的返回，其中 ``api_key`` 已解密。
    """
    base_url = channel["base_url"].rstrip("/")
    upstream_model = channel.get("upstream_model") or body.get("model", "")
    api_key = channel.get("api_key")

    if protocol == "anthropic":
        return _adapt_anthropic(base_url, api_key, body, upstream_model)
    if protocol == "gemini":
        return _adapt_gemini(base_url, api_key, body, upstream_model)
    return _adapt_openai(base_url, api_key, body, upstream_model)


def _adapt_openai(
    base_url: str, api_key: str | None, body: dict, upstream_model: str
) -> tuple[str, dict, dict]:
    """OpenAI 协议：直接转发，``Authorization: Bearer {api_key}``。"""
    url = f"{base_url}/chat/completions"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {**body, "model": upstream_model}
    return url, headers, payload


def _adapt_anthropic(
    base_url: str, api_key: str | None, body: dict, upstream_model: str
) -> tuple[str, dict, dict]:
    """Anthropic 协议：转换为 Messages 格式，``x-api-key`` 头。"""
    url = f"{base_url}/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    if api_key:
        headers["x-api-key"] = api_key

    system_prompt = ""
    messages: list[dict] = []
    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            system_prompt += content + "\n"
        else:
            messages.append({"role": role, "content": content})

    payload: dict[str, Any] = {
        "model": upstream_model,
        "messages": messages,
        "max_tokens": body.get("max_tokens", 1024),
        "stream": bool(body.get("stream", False)),
    }
    if system_prompt.strip():
        payload["system"] = system_prompt.strip()
    if body.get("temperature") is not None:
        payload["temperature"] = body["temperature"]
    return url, headers, payload


def _adapt_gemini(
    base_url: str, api_key: str | None, body: dict, upstream_model: str
) -> tuple[str, dict, dict]:
    """Gemini 协议：转换为 generateContent 格式，``?key={api_key}`` 参数。"""
    url = f"{base_url}/models/{upstream_model}:generateContent"
    if api_key:
        url += f"?key={api_key}"
    headers = {"Content-Type": "application/json"}

    contents: list[dict] = []
    for msg in body.get("messages", []):
        role = "user" if msg.get("role") == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg.get("content", "")}]})

    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": body.get("max_tokens", 1024),
        },
    }
    if body.get("temperature") is not None:
        payload["generationConfig"]["temperature"] = body["temperature"]
    return url, headers, payload


# ----------------------------------------------------------------------
# 响应转换：将 Anthropic / Gemini 响应转回 OpenAI 格式
# ----------------------------------------------------------------------


def _convert_anthropic_response(data: dict, model: str) -> dict:
    """将 Anthropic Messages 响应转换为 OpenAI ChatCompletion 格式。"""
    content = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            content += block.get("text", "")
    usage = data.get("usage", {})
    prompt_tokens = usage.get("input_tokens", 0)
    completion_tokens = usage.get("output_tokens", 0)
    return {
        "id": data.get("id", f"chatcmpl-{new_id('rel').split('_', 1)[-1]}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _convert_gemini_response(data: dict, model: str) -> dict:
    """将 Gemini generateContent 响应转换为 OpenAI ChatCompletion 格式。"""
    content = ""
    finish_reason = "stop"
    for candidate in data.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            content += part.get("text", "")
        if candidate.get("finishReason"):
            finish_reason = _gemini_finish_reason(candidate["finishReason"])
    usage = data.get("usageMetadata", {})
    return {
        "id": f"chatcmpl-{new_id('rel').split('_', 1)[-1]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("promptTokenCount", 0),
            "completion_tokens": usage.get("candidatesTokenCount", 0),
            "total_tokens": usage.get("totalTokenCount", 0),
        },
    }


def _gemini_finish_reason(reason: str) -> str:
    """Gemini finishReason → OpenAI finish_reason。"""
    mapping = {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "SAFETY": "content_filter",
        "RECITATION": "content_filter",
    }
    return mapping.get(reason, "stop")


def _synthesize_stream(payload: dict) -> bytes:
    """将非流式 OpenAI 响应合成为单条 SSE chunk + done 标记。"""
    chunk = {
        "id": payload.get("id", "chatcmpl-relay"),
        "object": "chat.completion.chunk",
        "created": payload.get("created", int(time.time())),
        "model": payload.get("model", ""),
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": payload["choices"][0]["message"]["content"],
                },
                "finish_reason": None,
            }
        ],
    }
    done_chunk = {
        "id": payload.get("id", "chatcmpl-relay"),
        "object": "chat.completion.chunk",
        "created": payload.get("created", int(time.time())),
        "model": payload.get("model", ""),
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    return (
        f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        f"data: {json.dumps(done_chunk, ensure_ascii=False)}\n\n"
        f"data: [DONE]\n\n"
    ).encode()


# ----------------------------------------------------------------------
# 计量记录
# ----------------------------------------------------------------------


async def _log_usage(
    request_id: str,
    workspace_id: str,
    token_id: str | None,
    channel_id: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    status_code: int,
    error_code: str | None = None,
) -> None:
    """将本次请求用量写入 ``gw_request_log``。

    轻量实现：仅记录用量日志，不触发计费结算（计费由完整网关管道负责）。
    """
    try:
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO gw_request_log(
                    request_id, workspace_id, token_id, channel_id, model,
                    prompt_tokens, completion_tokens, total_tokens, cost_credits,
                    latency_ms, status_code, error_code
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    request_id,
                    workspace_id,
                    token_id,
                    channel_id,
                    model,
                    prompt_tokens,
                    completion_tokens,
                    prompt_tokens + completion_tokens,
                    latency_ms,
                    status_code,
                    error_code,
                ),
            )
            await conn.commit()
    except Exception:
        # 用量记录失败不应影响已返回给客户端的响应
        pass


def _extract_usage(body: dict, response: dict, protocol: str) -> tuple[int, int]:
    """从请求与上游响应中提取 prompt/completion token 用量。"""
    prompt_tokens = 0
    completion_tokens = 0
    if protocol == "openai":
        usage = response.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
    elif protocol == "anthropic":
        usage = response.get("usage", {})
        prompt_tokens = usage.get("input_tokens", 0)
        completion_tokens = usage.get("output_tokens", 0)
    elif protocol == "gemini":
        usage = response.get("usageMetadata", {})
        prompt_tokens = usage.get("promptTokenCount", 0)
        completion_tokens = usage.get("candidatesTokenCount", 0)
    # 估算 prompt tokens（粗略：按消息字符数 / 4）
    if not prompt_tokens:
        prompt_tokens = sum(
            len(str(m.get("content", ""))) for m in body.get("messages", [])
        ) // 4
    return prompt_tokens, completion_tokens


# ----------------------------------------------------------------------
# Go 网关反向代理：将请求透传到 Go 网关（含流式 SSE）
# ----------------------------------------------------------------------


async def _proxy_chat_completions_to_go_gateway(
    request: Request,
) -> Response | StreamingResponse:
    """将 /v1/chat/completions 请求反向代理到 Go 网关。

    保留 method（POST）、body、Authorization 头，透传响应（含流式 SSE）。
    Go 网关不可用时返回 502 (E01050)。
    """
    target_url = f"{GATEWAY_GO_URL}/v1/chat/completions"
    headers: dict[str, str] = {}
    auth = request.headers.get("Authorization") or request.headers.get("authorization")
    if auth:
        headers["Authorization"] = auth
    content_type = request.headers.get("Content-Type") or request.headers.get("content-type")
    if content_type:
        headers["Content-Type"] = content_type

    body = await request.body()

    # 判断是否为流式请求：body 中 stream=true 时透传 SSE
    is_stream = False
    if body:
        try:
            payload = json.loads(body)
            is_stream = bool(payload.get("stream", False))
        except Exception:
            pass

    if is_stream:
        # 流式：用 StreamingResponse 透传上游 SSE
        async def _stream_generator():
            try:
                async with httpx.AsyncClient(timeout=_GATEWAY_PROXY_TIMEOUT) as client:
                    async with client.stream(
                        "POST", target_url, content=body, headers=headers
                    ) as resp:
                        async for line in resp.aiter_lines():
                            yield line.encode() + b"\n"
            except httpx.HTTPError:
                # Go 网关不可用时返回 SSE 错误并关闭流
                err = {"error": {"code": "E01050", "message": "Go gateway unavailable", "type": "api_error"}}
                yield b"data: " + json.dumps(err).encode() + b"\n\n"
                yield b"data: [DONE]\n\n"

        return StreamingResponse(_stream_generator(), media_type="text/event-stream")

    # 非流式：转发并返回响应
    try:
        async with httpx.AsyncClient(timeout=_GATEWAY_PROXY_TIMEOUT) as client:
            resp = await client.post(target_url, content=body, headers=headers)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )
    except httpx.HTTPError as exc:
        return _openai_error(
            "E01050", f"Go gateway unavailable: {exc}", "api_error", 502
        )


async def _proxy_models_to_go_gateway(request: Request) -> Response:
    """将 /v1/models 请求反向代理到 Go 网关。"""
    target_url = f"{GATEWAY_GO_URL}/v1/models"
    headers: dict[str, str] = {}
    auth = request.headers.get("Authorization") or request.headers.get("authorization")
    if auth:
        headers["Authorization"] = auth
    try:
        async with httpx.AsyncClient(timeout=_GATEWAY_PROXY_TIMEOUT) as client:
            resp = await client.get(target_url, headers=headers)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )
    except httpx.HTTPError as exc:
        return _openai_error(
            "E01050", f"Go gateway unavailable: {exc}", "api_error", 502
        )


# ----------------------------------------------------------------------
# 端点：POST /v1/chat/completions
# ----------------------------------------------------------------------


@router.post("/chat/completions")
async def chat_completions(request: Request):
    """OpenAI 兼容的对话补全端点。"""
    # Go 网关启用时，反向代理到 Go 网关；否则走 Python relay 原有逻辑（fallback）
    if GATEWAY_GO_ENABLED:
        return await _proxy_chat_completions_to_go_gateway(request)

    request_id = new_id("req")
    started_at = time.monotonic()

    # 1. 认证：解析 Bearer 令牌
    api_key = _parse_bearer(request)
    if not api_key:
        return _openai_error(
            "E01001", "Missing Authorization Bearer token", "authentication_error", 401
        )

    # 2. 解析请求体
    try:
        body = await request.json()
    except Exception:
        return _openai_error("E01007", "Invalid JSON request body", "invalid_request_error", 400)

    model = body.get("model")
    if not model:
        return _openai_error(
            "E01006", "model is required", "invalid_request_error", 404
        )

    # 3. 解析路由：模型 → 渠道（resolve_route 内部已解密渠道凭据）
    try:
        resolve_result = await _resolve_channel(api_key, model)
    except _ResolveError as exc:
        await _log_usage(
            request_id, "", None, "", model, 0, 0,
            int((time.monotonic() - started_at) * 1000), exc.status, exc.code,
        )
        return _openai_error(
            exc.code, exc.message, "invalid_request_error", exc.status
        )

    channel = resolve_result["channel"]
    workspace_id = resolve_result["workspace_id"]
    token_id = resolve_result.get("token_id")

    # 4. 协议适配
    provider = _provider_name(channel["provider"])
    profile = PROVIDER_CATALOG.get(provider, {})
    protocol = profile.get("protocol", "openai")
    upstream_url, headers, payload = _adapt_request(channel, body, protocol)

    stream = bool(body.get("stream", False))

    # 5. 转发到上游
    try:
        if stream and protocol == "openai":
            # OpenAI 流式：透传 SSE
            return await _stream_openai(
                upstream_url, headers, payload, request_id, workspace_id,
                token_id, channel, model, protocol, body, started_at,
            )
        # 非流式（或非 openai 协议的流式请求，先以非流式调用上游再合成）
        return await _forward_non_stream(
            upstream_url, headers, payload, stream, request_id, workspace_id,
            token_id, channel, model, protocol, body, started_at,
        )
    except httpx.HTTPError as exc:
        latency_ms = int((time.monotonic() - started_at) * 1000)
        await _log_usage(
            request_id, workspace_id, token_id, channel["id"], model,
            0, 0, latency_ms, 502, "E01050",
        )
        return _openai_error(
            "E01050", f"Upstream connection error: {exc}", "api_error", 502
        )


async def _forward_non_stream(
    url: str, headers: dict, payload: dict, stream_requested: bool,
    request_id: str, workspace_id: str, token_id: str | None,
    channel: dict, model: str, protocol: str, body: dict, started_at: float,
) -> JSONResponse | StreamingResponse:
    """非流式转发：调用上游并按协议转换响应。"""
    # 非 openai 协议下，若客户端请求 stream，则强制上游非流式调用以便合成
    upstream_payload = {**payload, "stream": False} if protocol != "openai" else payload

    async with httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT) as client:
        response = await client.post(url, headers=headers, json=upstream_payload)

    latency_ms = int((time.monotonic() - started_at) * 1000)
    upstream_data: dict
    if protocol == "anthropic":
        upstream_data = _convert_anthropic_response(response.json(), model)
    elif protocol == "gemini":
        upstream_data = _convert_gemini_response(response.json(), model)
    else:
        upstream_data = response.json()

    prompt_tokens, completion_tokens = _extract_usage(body, upstream_data, protocol)

    # 记录用量
    await _log_usage(
        request_id, workspace_id, token_id, channel["id"], model,
        prompt_tokens, completion_tokens, latency_ms, response.status_code, None,
    )

    if response.status_code >= 400:
        return _openai_error(
            "E01051",
            f"Upstream returned HTTP {response.status_code}",
            "api_error",
            502,
        )

    # 若客户端请求流式但上游协议不支持透传，合成单条 SSE 流
    if stream_requested:
        return StreamingResponse(
            iter([_synthesize_stream(upstream_data)]),
            media_type="text/event-stream",
        )

    return JSONResponse(content=upstream_data)


async def _stream_openai(
    url: str, headers: dict, payload: dict,
    request_id: str, workspace_id: str, token_id: str | None,
    channel: dict, model: str, protocol: str, body: dict, started_at: float,
) -> StreamingResponse:
    """OpenAI 协议流式转发：透传上游 SSE。"""

    async def _generator():
        prompt_tokens = 0
        completion_tokens = 0
        status_code = 200
        error_code: str | None = None
        try:
            async with httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT) as client:
                async with client.stream(
                    "POST", url, headers=headers, json=payload
                ) as response:
                    status_code = response.status_code
                    async for line in response.aiter_lines():
                        # 透传每一行 SSE
                        yield line.encode() + b"\n"
                        # 尝试从 usage chunk 中提取 token 用量
                        if line.startswith("data: ") and "usage" in line:
                            try:
                                chunk = json.loads(line[6:])
                                usage = chunk.get("usage", {})
                                prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                                completion_tokens = usage.get("completion_tokens", completion_tokens)
                            except Exception:
                                pass
        except httpx.HTTPError:
            error_code = "E01050"
            status_code = 502
        finally:
            latency_ms = int((time.monotonic() - started_at) * 1000)
            await _log_usage(
                request_id, workspace_id, token_id, channel["id"], model,
                prompt_tokens, completion_tokens, latency_ms, status_code, error_code,
            )

    return StreamingResponse(_generator(), media_type="text/event-stream")


# ----------------------------------------------------------------------
# 端点：GET /v1/models
# ----------------------------------------------------------------------


@router.get("/models")
async def list_models(request: Request):
    """返回当前令牌可用的模型列表（OpenAI 兼容格式）。"""
    # Go 网关启用时，反向代理到 Go 网关；否则走 Python relay 原有逻辑（fallback）
    if GATEWAY_GO_ENABLED:
        return await _proxy_models_to_go_gateway(request)

    api_key = _parse_bearer(request)
    if not api_key:
        return _openai_error(
            "E01001", "Missing Authorization Bearer token", "authentication_error", 401
        )

    async with pool.connection() as conn:
        # 验证令牌
        result = await conn.execute(
            """
            SELECT id, workspace_id, model_whitelist, pinned_channel_id, group_id
            FROM gw_token
            WHERE key_hash = %s AND revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at > now())
            """,
            (hash_secret(api_key),),
        )
        token = await result.fetchone()
        if not token:
            return _openai_error(
                "E01001", "Invalid API key", "authentication_error", 401
            )

        # 聚合 workspace 下已启用渠道的模型
        channels_result = await conn.execute(
            """
            SELECT DISTINCT unnest(models) AS model
            FROM gw_channel
            WHERE workspace_id = %s AND status = 'enabled'
              AND cardinality(models) > 0
            """,
            (token["workspace_id"],),
        )
        rows = await channels_result.fetchall()

    models: list[dict] = []
    whitelist = token["model_whitelist"] or []
    for row in rows:
        model_name = row["model"]
        if whitelist and model_name not in whitelist:
            continue
        models.append(
            {
                "id": model_name,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "workama",
            }
        )
    return {"object": "list", "data": models}
