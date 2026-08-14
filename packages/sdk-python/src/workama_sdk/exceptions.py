"""WorkAMA SDK 异常定义。

所有异常均继承自 :class:`WorkAMAError`，便于调用方统一捕获。
异常会携带 HTTP 状态码与原始响应体，便于排错。
"""

from __future__ import annotations

from typing import Any, Optional


class WorkAMAError(Exception):
    """所有 SDK 异常的基类。

    :param message: 错误描述
    :param status_code: HTTP 状态码（网络层错误可能为 None）
    :param body: 服务端返回的原始响应体（已解析为 dict 或原始字符串）
    """

    def __init__(
        self,
        message: str = "",
        *,
        status_code: Optional[int] = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志输出
        if self.status_code is not None:
            return f"[{self.status_code}] {self.message}"
        return self.message


class AuthenticationError(WorkAMAError):
    """鉴权失败（HTTP 401），如 token 过期或 API Key 无效。"""


class ForbiddenError(WorkAMAError):
    """权限不足（HTTP 403），如缺少所需 scope 或跨工作空间访问被拒。"""


class NotFoundError(WorkAMAError):
    """资源不存在（HTTP 404）。"""


class RateLimitError(WorkAMAError):
    """触发限流（HTTP 429）。"""


__all__ = [
    "WorkAMAError",
    "AuthenticationError",
    "ForbiddenError",
    "NotFoundError",
    "RateLimitError",
]
