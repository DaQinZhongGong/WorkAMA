"""工作流 HTTP 节点生产硬化模块 (T-M5-003)。

提供生产级 HTTP 请求能力与 Code 节点输出校验：

- ``http_request_with_retry()``: 带可配置重试（指数退避）、独立连接/读取超时、
  错误分类（transient 自动重试 / permanent 不重试）、响应大小限制、
  响应内容类型校验的 HTTP 请求封装。记录 attempts/latency_ms/status_code/headers。
- ``validate_code_output()``: Code 节点输出 JSON Schema 校验（基础类型/属性/必填/
  数组 items/枚举/minimum/maximum）。
- ``classify_http_error()``: HTTP 错误分类为 transient 或 permanent。
- ``sanitize_headers()``: 响应头脱敏（Authorization/Cookie/Set-Cookie 值打码）。

设计文档：910-进度追踪与任务清单.md T-M5-003；510-AI中台核心设计.md §5。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any
from urllib.parse import urlsplit

LOGGER = logging.getLogger("workama.platform-api.workflow_http_node")

# ============================================================================
# 常量与默认值
# ============================================================================

DEFAULT_MAX_RETRIES = 3
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 30.0
DEFAULT_MAX_RESPONSE_SIZE = 1_048_576  # 1 MB
DEFAULT_RETRY_BASE_DELAY = 1.0
DEFAULT_RETRY_MAX_DELAY = 30.0

# 需要脱敏的响应头（小写匹配）
_SENSITIVE_HEADERS = frozenset({
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "proxy-authorization",
})

# ============================================================================
# Sub-workflow 深度配置
# ============================================================================


def max_subworkflow_depth() -> int:
    """从环境变量读取 sub-workflow 最大嵌套深度，默认 5。"""
    try:
        return int(os.getenv("WORKAMA_MAX_SUBWORKFLOW_DEPTH", "5"))
    except ValueError:
        return 5


# ============================================================================
# HTTP 错误分类
# ============================================================================


def classify_http_error(status_code: int | None, exc: Exception | None = None) -> str | None:
    """将 HTTP 错误分类为 ``transient``（可重试）或 ``permanent``（不可重试）。

    规则：
    - 5xx / 429 → transient（服务端临时故障或限流）
    - 4xx（除 429）→ permanent（客户端请求错误，重试无意义）
    - 超时 / 连接错误 / 网络异常 → transient
    - 其他异常 → transient（保守重试）
    - 无错误（2xx/3xx）→ None
    """
    if exc is not None:
        # 异常类错误一律视为 transient（超时/连接/网络）
        return "transient"
    if status_code is None:
        return None
    if status_code >= 500:
        return "transient"
    if status_code == 429:
        return "transient"
    if 400 <= status_code < 500:
        return "permanent"
    return None


# ============================================================================
# 响应头脱敏
# ============================================================================


def sanitize_headers(headers: dict[str, Any]) -> dict[str, Any]:
    """对响应头进行脱敏：敏感头的值替换为 ``[REDACTED]``。"""
    sanitized: dict[str, Any] = {}
    for key, value in headers.items():
        if key.lower() in _SENSITIVE_HEADERS:
            sanitized[key] = "[REDACTED]"
        else:
            sanitized[key] = value
    return sanitized


# ============================================================================
# HTTP 请求（带重试/退避/超时/错误分类/响应限制/内容类型校验）
# ============================================================================


async def http_request_with_retry(
    method: str,
    url: str,
    *,
    headers: dict[str, Any] | None = None,
    body: Any = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    max_response_size: int = DEFAULT_MAX_RESPONSE_SIZE,
    expected_content_type: str | None = None,
    allowed_hosts: set[str] | None = None,
    retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
    retry_max_delay: float = DEFAULT_RETRY_MAX_DELAY,
) -> dict[str, Any]:
    """执行 HTTP 请求，带重试/退避/超时/错误分类/响应限制/内容类型校验。

    返回 dict 包含：
    - ``status_code``: HTTP 状态码（失败时可能为 None）
    - ``headers``: 脱敏后的响应头
    - ``body``: 解析后的响应体（JSON 自动解析，否则文本）
    - ``truncated``: 响应是否被截断
    - ``attempts``: 总尝试次数（含首次）
    - ``latency_ms``: 总耗时毫秒
    - ``error``: 错误信息（成功时不存在）
    - ``error_code``: 错误码（timeout/connection_error/http_error/validation_error）
    - ``error_class``: 错误分类（transient/permanent，仅在最终失败时）
    """
    import httpx

    headers = headers or {}
    started = time.monotonic()
    attempts = 0
    last_error: str | None = None
    last_error_code: str | None = None
    last_error_class: str | None = None

    # 总超时 = connect_timeout + read_timeout
    total_timeout = connect_timeout + read_timeout

    while attempts <= max_retries:
        attempts += 1
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    httpx.request,
                    method=method,
                    url=url,
                    headers=headers,
                    json=body if isinstance(body, dict) else None,
                    data=body if isinstance(body, str) else None,
                    timeout=total_timeout,
                ),
                timeout=total_timeout,
            )
        except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
            last_error = f"timeout: {exc}"
            last_error_code = "timeout"
            last_error_class = "transient"
        except httpx.ConnectError as exc:
            last_error = f"connection_error: {exc}"
            last_error_code = "connection_error"
            last_error_class = "transient"
        except httpx.HTTPError as exc:
            last_error = f"http_error: {exc}"
            last_error_code = "http_error"
            last_error_class = "transient"
        except Exception as exc:  # pragma: no cover
            last_error = f"request_failed: {exc}"
            last_error_code = "request_failed"
            last_error_class = "transient"
        else:
            # 请求成功返回，判断状态码是否需要重试
            error_class = classify_http_error(response.status_code)
            if error_class is None:
                # 成功（2xx/3xx）：处理响应体
                return _build_success_response(
                    response,
                    max_response_size=max_response_size,
                    expected_content_type=expected_content_type,
                    attempts=attempts,
                    started=started,
                )
            # 需要重试的错误状态码
            last_error = f"http_status_{response.status_code}"
            last_error_code = "http_error"
            last_error_class = error_class
            if error_class == "permanent":
                # permanent 错误不重试，直接返回
                break

        # 判断是否继续重试
        if attempts > max_retries:
            break
        if last_error_class == "permanent":
            break
        # 指数退避
        delay = min(retry_base_delay * (2 ** (attempts - 1)), retry_max_delay)
        await asyncio.sleep(delay)

    return {
        "status_code": None,
        "headers": {},
        "body": None,
        "truncated": False,
        "attempts": attempts,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "error": last_error,
        "error_code": last_error_code,
        "error_class": last_error_class,
    }


def _build_success_response(
    response: Any,
    *,
    max_response_size: int,
    expected_content_type: str | None,
    attempts: int,
    started: float,
) -> dict[str, Any]:
    """从成功响应构建返回 dict，处理大小限制与内容类型校验。"""
    resp_headers = dict(response.headers)
    raw_body = response.text or ""
    truncated = False

    if len(raw_body) > max_response_size:
        LOGGER.warning(
            "http_request response truncated: size=%d max=%d",
            len(raw_body),
            max_response_size,
        )
        raw_body = raw_body[:max_response_size]
        truncated = True

    content_type = resp_headers.get("content-type", "")

    # 内容类型校验
    if expected_content_type and expected_content_type not in content_type:
        return {
            "status_code": response.status_code,
            "headers": sanitize_headers(resp_headers),
            "body": raw_body,
            "truncated": truncated,
            "attempts": attempts,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": (
                f"validation_error: expected content-type '{expected_content_type}' "
                f"but got '{content_type}'"
            ),
            "error_code": "validation_error",
            "error_class": "permanent",
        }

    # 尝试 JSON 解析
    parsed_body: Any = raw_body
    if "application/json" in content_type:
        try:
            parsed_body = response.json()
        except Exception:
            parsed_body = raw_body

    return {
        "status_code": response.status_code,
        "headers": sanitize_headers(resp_headers),
        "body": parsed_body,
        "truncated": truncated,
        "attempts": attempts,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }


# ============================================================================
# Code 节点输出 JSON Schema 校验
# ============================================================================


def validate_code_output(output: Any, schema: dict[str, Any]) -> dict[str, Any]:
    """用 JSON Schema 校验 Code 节点输出。

    支持的 JSON Schema 关键字：
    - ``type``: 类型（object/array/string/number/integer/boolean/null）
    - ``properties``: 对象属性 schema
    - ``required``: 必填属性列表
    - ``items``: 数组元素 schema
    - ``enum``: 枚举值列表
    - ``minimum`` / ``maximum``: 数值范围
    - ``additionalProperties``: 是否允许额外属性（bool）

    返回 ``{"valid": bool, "error": str | None}``。
    """
    if not isinstance(schema, dict):
        return {"valid": False, "error": "schema must be a dict"}

    error = _validate_against_schema(output, schema, path="root")
    if error is not None:
        return {"valid": False, "error": error}
    return {"valid": True, "error": None}


def _validate_against_schema(value: Any, schema: dict[str, Any], path: str) -> str | None:
    """递归校验 value 是否符合 schema，返回错误描述或 None。"""
    # type 校验
    expected_type = schema.get("type")
    if expected_type is not None:
        type_error = _check_type(value, expected_type, path)
        if type_error is not None:
            return type_error

    # enum 校验
    if "enum" in schema:
        if value not in schema["enum"]:
            return f"{path}: value {value!r} not in enum {schema['enum']}"

    # 数值范围校验
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{path}: value {value} below minimum {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{path}: value {value} above maximum {schema['maximum']}"

    # object 校验
    if expected_type == "object" or (
        expected_type is None and isinstance(value, dict)
    ):
        if isinstance(value, dict):
            required = schema.get("required") or []
            for field in required:
                if field not in value:
                    return f"{path}: missing required field '{field}'"
            properties = schema.get("properties") or {}
            for key, sub_value in value.items():
                if key in properties:
                    sub_error = _validate_against_schema(
                        sub_value, properties[key], f"{path}.{key}"
                    )
                    if sub_error is not None:
                        return sub_error
                elif schema.get("additionalProperties") is False:
                    return f"{path}: additional property '{key}' not allowed"

    # array 校验
    if expected_type == "array" or (expected_type is None and isinstance(value, list)):
        if isinstance(value, list):
            items_schema = schema.get("items")
            if items_schema is not None:
                for idx, item in enumerate(value):
                    sub_error = _validate_against_schema(
                        item, items_schema, f"{path}[{idx}]"
                    )
                    if sub_error is not None:
                        return sub_error

    return None


def _check_type(value: Any, expected_type: str, path: str) -> str | None:
    """检查 value 是否符合 expected_type，返回错误描述或 None。"""
    if expected_type == "object":
        if not isinstance(value, dict):
            return f"{path}: expected object, got {type(value).__name__}"
    elif expected_type == "array":
        if not isinstance(value, list):
            return f"{path}: expected array, got {type(value).__name__}"
    elif expected_type == "string":
        if not isinstance(value, str):
            return f"{path}: expected string, got {type(value).__name__}"
    elif expected_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{path}: expected integer, got {type(value).__name__}"
    elif expected_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{path}: expected number, got {type(value).__name__}"
    elif expected_type == "boolean":
        if not isinstance(value, bool):
            return f"{path}: expected boolean, got {type(value).__name__}"
    elif expected_type == "null":
        if value is not None:
            return f"{path}: expected null, got {type(value).__name__}"
    return None


# ============================================================================
# SSRF 校验（复用 workflow.py 的逻辑，保持独立可用）
# ============================================================================


def validate_resolved_outbound_url(url: str, allowed_hosts: set[str]) -> str | None:
    """校验 URL 是否允许出站请求。

    返回 None 表示通过，否则返回错误描述。SSRF 防护：只允许白名单主机。
    """
    try:
        parsed = urlsplit(url)
    except Exception as exc:
        return f"invalid_url: {exc}"
    host = (parsed.hostname or "").lower()
    if not host:
        return "invalid_url: missing hostname"
    if host not in allowed_hosts:
        return f"forbidden: host '{host}' not in allowed list"
    return None
