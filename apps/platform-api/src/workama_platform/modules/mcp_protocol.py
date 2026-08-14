from __future__ import annotations

import json
import hashlib
import asyncio
import math
import os
import re
import secrets
import shlex
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from opentelemetry import trace
from workama_observability import mcp_attributes, set_span_attributes

from workama_platform.modules.mcp import (
    MCP_PROTOCOL_VERSIONS,
    validate_endpoint_url,
    validate_stdio_command,
)


JSONRPC_VERSION = "2.0"
TRACER = trace.get_tracer("platform-api.mcp")

# 导出列表
__all__ = [
    "McpSessionRegistry",
    "McpServerHealthMonitor",
    "McpCapabilityCache",
    "McpLoadBalancer",
    "McpGracefulShutdown",
    "McpOperationTracker",
    "reset_mcp_enhancements",
    "reset_session_registry",
    "reset_session_registry_async",
    "bridge_jsonrpc",
    "MCPClient",
    "McpClientError",
    "McpTimeoutError",
    "McpServerError",
]

SUPPORTED_METHODS = frozenset(
    {
        "initialize",
        "tools/list",
        "resources/list",
        "prompts/list",
        "roots/list",
        "tools/call",
        "sampling/createMessage",
        "elicitation/create",
    }
)
SUPPORTED_NOTIFICATIONS = frozenset(
    {
        "notifications/initialized",
        "notifications/cancelled",
        "notifications/progress",
        "$/cancelRequest",
    }
)
SUPPORTED_PENDING_METHODS = frozenset({"sampling/createMessage", "elicitation/create"})
MCP_PENDING_ERROR = -32004
MCP_ROOTS_ERROR = -32005
MCP_SESSION_ERROR = -32006
MCP_CANCELLED_ERROR = -32800
MCP_MAX_MESSAGE_BYTES = 1_048_576
MCP_TRANSPORT_TIMEOUT_SECONDS = 30.0
_SENSITIVE_KEY = re.compile(
    r"^(?:auth[_-]?ref|credential(?:[_-]?(?:enc|ref))?|api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|secret|private[_-]?key|token)$",
    re.IGNORECASE,
)
_SENSITIVE_RESPONSE_KEY = re.compile(
    r"^(?:auth[_-]?ref|credential(?:[_-]?(?:enc|ref))?|api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|secret|private[_-]?key|token|mcp[_-]?session[_-]?id|session[_-]?id)$",
    re.IGNORECASE,
)
_SENSITIVE_VALUE = re.compile(
    r"(?i)(?:\b(?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]+|(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|authorization)\s*[:=]\s*[^\s,;]+"
)
_SESSION_KEYS = (
    "mcp-session-id",
    "Mcp-Session-Id",
    "mcp_session_id",
    "session_id",
)


class JsonRpcError(Exception):
    def __init__(
        self,
        code: int,
        message: str,
        *,
        request_id: Any = None,
        data: Any = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id
        self.data = data

    def with_id(self, request_id: Any) -> "JsonRpcError":
        return JsonRpcError(
            self.code,
            self.message,
            request_id=request_id,
            data=self.data,
        )

    def response(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = _sanitize(self.data)
        return {"jsonrpc": JSONRPC_VERSION, "id": self.request_id, "error": error}


def jsonrpc_error_response(
    request_id: Any,
    code: int,
    message: str,
    data: Any = None,
) -> dict[str, Any]:
    return JsonRpcError(code, message, request_id=request_id, data=data).response()


def _sanitize(value: Any) -> Any:
    """Strip credential-bearing fields from untrusted server responses."""
    if isinstance(value, dict):
        return {
            str(key): _sanitize(child)
            for key, child in value.items()
            if not _SENSITIVE_KEY.fullmatch(str(key))
        }
    if isinstance(value, list):
        return [_sanitize(child) for child in value]
    if isinstance(value, tuple):
        return [_sanitize(child) for child in value]
    if isinstance(value, str):
        return _SENSITIVE_VALUE.sub("<redacted>", value)
    return value


def _sanitize_untrusted(value: Any) -> Any:
    """Sanitize a remote payload, including opaque remote session handles."""
    if isinstance(value, dict):
        return {
            str(key): _sanitize_untrusted(child)
            for key, child in value.items()
            if not _SENSITIVE_RESPONSE_KEY.fullmatch(str(key))
        }
    if isinstance(value, list):
        return [_sanitize_untrusted(child) for child in value]
    if isinstance(value, tuple):
        return [_sanitize_untrusted(child) for child in value]
    if isinstance(value, str):
        return _SENSITIVE_VALUE.sub("<redacted>", value)
    return value


def _valid_id(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return bool(value) and len(value) <= 256
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class RpcRequest:
    request_id: Any
    method: str
    params: dict[str, Any]
    is_notification: bool = False


@dataclass
class _McpSession:
    server_key: str
    transport: str
    protocol_version: str
    expires_at: float
    cancelled_ids: set[str]
    progress: dict[str, dict[str, bool]]
    runtime: Any = None
    last_heartbeat: float = 0.0
    active_operations: dict[str, float] = None  # type: ignore
    operation_lock: asyncio.Lock = None  # type: ignore

    def __post_init__(self):
        if self.active_operations is None:
            object.__setattr__(self, 'active_operations', {})
        if self.operation_lock is None:
            object.__setattr__(self, 'operation_lock', asyncio.Lock())


class McpSessionRegistry:
    """Small process-local session registry for the synchronous bridge.

    The registry deliberately stores only a hash of server identity and
    progress metadata. It never stores endpoint, command, auth reference, or
    a remote server-provided session token.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = 300.0,
        clock: Any = None,
        heartbeat_interval: float = 30.0,
    ) -> None:
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.heartbeat_interval = max(1.0, float(heartbeat_interval))
        self.clock = clock or time.monotonic
        self._sessions: dict[str, _McpSession] = {}

    def _purge(self) -> None:
        now = float(self.clock())
        for session_id, session in tuple(self._sessions.items()):
            if session.expires_at <= now:
                _dispose_runtime(session.runtime)
                self._sessions.pop(session_id, None)

    def create(self, *, server_key: str, transport: str, protocol_version: str, runtime: Any = None) -> str:
        self._purge()
        session_id = secrets.token_urlsafe(24)
        while session_id in self._sessions:
            session_id = secrets.token_urlsafe(24)
        now = float(self.clock())
        self._sessions[session_id] = _McpSession(
            server_key=server_key,
            transport=transport,
            protocol_version=protocol_version,
            expires_at=now + self.ttl_seconds,
            cancelled_ids=set(),
            progress={},
            runtime=runtime,
            last_heartbeat=now,
        )
        return session_id

    def get(self, session_id: str) -> _McpSession | None:
        self._purge()
        return self._sessions.get(session_id)

    def heartbeat(self, session_id: str) -> bool:
        """Update session heartbeat and extend expiration."""
        session = self.get(session_id)
        if session is None:
            return False
        now = float(self.clock())
        session.last_heartbeat = now
        session.expires_at = now + self.ttl_seconds
        return True

    def clear(self) -> None:
        for session in self._sessions.values():
            _dispose_runtime(session.runtime)
        self._sessions.clear()

    async def close(self) -> None:
        runtimes = [session.runtime for session in self._sessions.values() if session.runtime is not None]
        self._sessions.clear()
        await asyncio.gather(*(_shutdown_runtime(runtime) for runtime in runtimes), return_exceptions=True)


_SESSION_REGISTRY = McpSessionRegistry()


def reset_session_registry() -> None:
    """Clear bridge sessions between tests or controlled worker restarts."""

    _SESSION_REGISTRY.clear()


async def reset_session_registry_async() -> None:
    """Await process cleanup for async lifecycle and test hooks."""

    await _SESSION_REGISTRY.close()


class McpServerHealthMonitor:
    """Monitor MCP server health with periodic checks."""

    def __init__(
        self,
        *,
        check_interval: float = 60.0,
        timeout: float = 10.0,
        max_failures: int = 3,
        clock: Any = None,
    ) -> None:
        self.check_interval = max(1.0, float(check_interval))
        self.timeout = max(1.0, float(timeout))
        self.max_failures = max(1, int(max_failures))
        self.clock = clock or time.monotonic
        self._server_health: dict[str, dict[str, Any]] = {}

    def record_check(self, server_id: str, *, healthy: bool, error: str | None = None) -> None:
        """Record a health check result for a server."""
        now = float(self.clock())
        if server_id not in self._server_health:
            self._server_health[server_id] = {
                "last_check": now,
                "consecutive_failures": 0,
                "last_error": None,
                "healthy": True,
            }
        health = self._server_health[server_id]
        health["last_check"] = now
        if healthy:
            health["consecutive_failures"] = 0
            health["last_error"] = None
            health["healthy"] = True
        else:
            health["consecutive_failures"] += 1
            health["last_error"] = error
            if health["consecutive_failures"] >= self.max_failures:
                health["healthy"] = False

    def is_healthy(self, server_id: str) -> bool:
        """Check if a server is considered healthy."""
        health = self._server_health.get(server_id)
        if health is None:
            return True  # Unknown servers are assumed healthy
        return health["healthy"]

    def get_health_status(self, server_id: str) -> dict[str, Any]:
        """Get detailed health status for a server."""
        health = self._server_health.get(server_id)
        if health is None:
            return {"healthy": True, "last_check": None, "consecutive_failures": 0}
        return {
            "healthy": health["healthy"],
            "last_check": health["last_check"],
            "consecutive_failures": health["consecutive_failures"],
            "last_error": health["last_error"],
        }

    def clear(self) -> None:
        """Clear all health records."""
        self._server_health.clear()


class McpCapabilityCache:
    """Cache MCP server capabilities with TTL."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 300.0,
        clock: Any = None,
    ) -> None:
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.clock = clock or time.monotonic
        self._cache: dict[str, dict[str, Any]] = {}

    def get(self, server_id: str) -> dict[str, Any] | None:
        """Get cached capabilities if not expired."""
        entry = self._cache.get(server_id)
        if entry is None:
            return None
        now = float(self.clock())
        if now > entry["expires_at"]:
            self._cache.pop(server_id, None)
            return None
        return entry["capabilities"]

    def set(self, server_id: str, capabilities: dict[str, Any]) -> None:
        """Cache capabilities with TTL."""
        now = float(self.clock())
        self._cache[server_id] = {
            "capabilities": capabilities,
            "expires_at": now + self.ttl_seconds,
            "cached_at": now,
        }

    def invalidate(self, server_id: str) -> None:
        """Invalidate cached capabilities for a server."""
        self._cache.pop(server_id, None)

    def clear(self) -> None:
        """Clear all cached capabilities."""
        self._cache.clear()


class McpLoadBalancer:
    """Round-robin load balancer for multiple MCP server instances."""

    def __init__(self) -> None:
        self._server_pools: dict[str, list[str]] = {}
        self._current_index: dict[str, int] = {}

    def register_instance(self, pool_id: str, server_id: str) -> None:
        """Register a server instance in a pool."""
        if pool_id not in self._server_pools:
            self._server_pools[pool_id] = []
            self._current_index[pool_id] = 0
        if server_id not in self._server_pools[pool_id]:
            self._server_pools[pool_id].append(server_id)

    def unregister_instance(self, pool_id: str, server_id: str) -> None:
        """Unregister a server instance from a pool."""
        if pool_id in self._server_pools:
            try:
                self._server_pools[pool_id].remove(server_id)
            except ValueError:
                pass
            if not self._server_pools[pool_id]:
                self._server_pools.pop(pool_id, None)
                self._current_index.pop(pool_id, None)

    def select_server(self, pool_id: str) -> str | None:
        """Select next server using round-robin."""
        if pool_id not in self._server_pools or not self._server_pools[pool_id]:
            return None
        servers = self._server_pools[pool_id]
        index = self._current_index.get(pool_id, 0)
        server = servers[index % len(servers)]
        self._current_index[pool_id] = (index + 1) % len(servers)
        return server

    def get_pool_size(self, pool_id: str) -> int:
        """Get number of servers in a pool."""
        return len(self._server_pools.get(pool_id, []))

    def clear(self) -> None:
        """Clear all pools."""
        self._server_pools.clear()
        self._current_index.clear()


class McpGracefulShutdown:
    """Manage graceful shutdown with session draining."""

    def __init__(
        self,
        *,
        drain_timeout: float = 30.0,
        clock: Any = None,
    ) -> None:
        self.drain_timeout = max(1.0, float(drain_timeout))
        self.clock = clock or time.monotonic
        self._shutting_down = False
        self._shutdown_started_at: float | None = None
        self._active_sessions: set[str] = set()

    def start_shutdown(self) -> None:
        """Initiate graceful shutdown."""
        if not self._shutting_down:
            self._shutting_down = True
            self._shutdown_started_at = float(self.clock())

    def is_shutting_down(self) -> bool:
        """Check if shutdown is in progress."""
        return self._shutting_down

    def register_session(self, session_id: str) -> None:
        """Register an active session."""
        self._active_sessions.add(session_id)

    def unregister_session(self, session_id: str) -> None:
        """Unregister a session."""
        self._active_sessions.discard(session_id)

    def get_active_session_count(self) -> int:
        """Get number of active sessions."""
        return len(self._active_sessions)

    def can_accept_new_session(self) -> bool:
        """Check if new sessions can be accepted."""
        return not self._shutting_down

    async def wait_for_drain(self) -> bool:
        """Wait for all sessions to complete or timeout."""
        if not self._shutting_down or self._shutdown_started_at is None:
            return True
        start = self._shutdown_started_at
        while float(self.clock()) - start < self.drain_timeout:
            if not self._active_sessions:
                return True
            await asyncio.sleep(0.1)
        return len(self._active_sessions) == 0

    def reset(self) -> None:
        """Reset shutdown state (for testing)."""
        self._shutting_down = False
        self._shutdown_started_at = None
        self._active_sessions.clear()


class McpOperationTracker:
    """Track active operations for progress reporting and cancellation."""

    def __init__(self, *, clock: Any = None) -> None:
        self.clock = clock or time.monotonic
        self._operations: dict[str, dict[str, Any]] = {}

    def start_operation(
        self,
        operation_id: str,
        *,
        session_id: str,
        method: str,
        total: float | None = None,
    ) -> None:
        """Start tracking an operation."""
        now = float(self.clock())
        self._operations[operation_id] = {
            "session_id": session_id,
            "method": method,
            "started_at": now,
            "progress": 0.0,
            "total": total,
            "completed": False,
            "cancelled": False,
        }

    def update_progress(self, operation_id: str, progress: float) -> bool:
        """Update operation progress."""
        op = self._operations.get(operation_id)
        if op is None or op["completed"] or op["cancelled"]:
            return False
        op["progress"] = float(progress)
        return True

    def complete_operation(self, operation_id: str) -> None:
        """Mark operation as completed."""
        op = self._operations.get(operation_id)
        if op is not None:
            op["completed"] = True

    def cancel_operation(self, operation_id: str) -> bool:
        """Mark operation as cancelled."""
        op = self._operations.get(operation_id)
        if op is None or op["completed"]:
            return False
        op["cancelled"] = True
        return True

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        """Get operation status."""
        return self._operations.get(operation_id)

    def get_session_operations(self, session_id: str) -> list[dict[str, Any]]:
        """Get all operations for a session."""
        return [
            {"operation_id": op_id, **op_data}
            for op_id, op_data in self._operations.items()
            if op_data["session_id"] == session_id
        ]

    def clear(self) -> None:
        """Clear all operations."""
        self._operations.clear()


_SERVER_HEALTH_MONITOR = McpServerHealthMonitor()
_CAPABILITY_CACHE = McpCapabilityCache()
_LOAD_BALANCER = McpLoadBalancer()
_GRACEFUL_SHUTDOWN = McpGracefulShutdown()
_OPERATION_TRACKER = McpOperationTracker()


def reset_mcp_enhancements() -> None:
    """Reset all enhancement state between tests."""
    _SERVER_HEALTH_MONITOR.clear()
    _CAPABILITY_CACHE.clear()
    _LOAD_BALANCER.clear()
    _GRACEFUL_SHUTDOWN.reset()
    _OPERATION_TRACKER.clear()


def validate_jsonrpc_request(message: Any) -> RpcRequest:
    if not isinstance(message, dict):
        raise JsonRpcError(-32600, "Invalid Request")
    if message.get("jsonrpc") != JSONRPC_VERSION:
        raise JsonRpcError(-32600, "Invalid Request")
    method = message.get("method")
    if not isinstance(method, str) or not method.strip():
        request_id = message.get("id") if _valid_id(message.get("id")) else None
        raise JsonRpcError(-32600, "Invalid Request", request_id=request_id)
    is_notification = "id" not in message
    if is_notification and method not in SUPPORTED_NOTIFICATIONS:
        raise JsonRpcError(-32600, "Invalid Request")
    request_id = message.get("id")
    if not _valid_id(request_id):
        raise JsonRpcError(-32600, "Invalid Request")
    params = message.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise JsonRpcError(-32602, "Invalid params", request_id=request_id)
    return RpcRequest(
        request_id=request_id,
        method=method,
        params=params,
        is_notification=is_notification,
    )


def _validate_method_params(request: RpcRequest, *, server: dict[str, Any] | None = None) -> None:
    if request.method in SUPPORTED_NOTIFICATIONS:
        _validate_notification_params(request)
        return
    if request.method not in SUPPORTED_METHODS:
        raise JsonRpcError(-32601, "Method not found", request_id=request.request_id)

    params = request.params
    if "roots" in params:
        _validate_root_list(
            params.get("roots"),
            server=server or {},
            request_id=request.request_id,
            error_code=-32602,
        )
    if request.method in SUPPORTED_PENDING_METHODS:
        return
    if request.method == "initialize":
        protocol_version = params.get("protocolVersion")
        if protocol_version not in MCP_PROTOCOL_VERSIONS:
            raise JsonRpcError(
                -32602,
                "Unsupported or missing protocolVersion",
                request_id=request.request_id,
            )
        if not isinstance(params.get("capabilities", {}), dict):
            raise JsonRpcError(-32602, "capabilities must be an object", request_id=request.request_id)
        if not isinstance(params.get("clientInfo", {}), dict):
            raise JsonRpcError(-32602, "clientInfo must be an object", request_id=request.request_id)
        return

    if request.method in {"tools/list", "resources/list", "prompts/list", "roots/list"}:
        cursor = params.get("cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise JsonRpcError(-32602, "cursor must be a string", request_id=request.request_id)
        return

    tool_name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise JsonRpcError(-32602, "tools/call requires a tool name", request_id=request.request_id)
    if not isinstance(arguments, dict):
        raise JsonRpcError(-32602, "tools/call arguments must be an object", request_id=request.request_id)


def _valid_progress_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _validate_notification_params(request: RpcRequest) -> None:
    params = request.params
    if request.method == "notifications/initialized":
        return
    if request.method in {"notifications/cancelled", "$/cancelRequest"}:
        cancelled_id = params.get("requestId", params.get("request_id"))
        if not _valid_id(cancelled_id) or cancelled_id is None:
            raise JsonRpcError(
                -32602,
                "cancelled notification requires a requestId",
                request_id=request.request_id,
            )
        reason = params.get("reason")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 256):
            raise JsonRpcError(-32602, "cancelled notification reason is invalid", request_id=request.request_id)
        return

    token = params.get("progressToken", params.get("progress_token"))
    progress = params.get("progress")
    total = params.get("total")
    if not _valid_id(token) or token is None:
        raise JsonRpcError(
            -32602,
            "progress notification requires a progressToken",
            request_id=request.request_id,
        )
    if not _valid_progress_number(progress) or float(progress) < 0:
        raise JsonRpcError(-32602, "progress notification progress is invalid", request_id=request.request_id)
    if total is not None and (not _valid_progress_number(total) or float(total) < 0):
        raise JsonRpcError(-32602, "progress notification total is invalid", request_id=request.request_id)


def _root_virtual_path(parsed: Any) -> str:
    host = str(parsed.netloc or "").lower()
    if host and host not in {"workspace", "workspaces", "workama"}:
        raise ValueError("MCP file root host is not controlled")
    path = unquote(str(parsed.path or ""))
    if host:
        path = f"/{host}{path}"
    if "\\" in path or "\x00" in path:
        raise ValueError("MCP root URI contains an unsafe path")
    parts = [part for part in path.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("MCP root URI must not contain traversal")
    return "/" + "/".join(parts)


def _root_is_under_workspace(path: str, server: dict[str, Any]) -> bool:
    workspace_id = str(server.get("workspace_id") or "").strip()
    if workspace_id and ("/" in workspace_id or "\\" in workspace_id or ".." in workspace_id):
        return False
    if workspace_id:
        prefixes = (
            f"/workspace/{workspace_id}",
            f"/workspaces/{workspace_id}",
            f"/workama/workspaces/{workspace_id}",
        )
    else:
        prefixes = ("/workspace", "/workspaces", "/workama/workspaces")
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def _validate_root_uri(value: Any, *, server: dict[str, Any]) -> str:
    if not isinstance(value, str):
        raise ValueError("MCP root URI must be a string")
    root = value.strip()
    if not root or len(root) > 2048 or any(character in root for character in "\x00\r\n"):
        raise ValueError("MCP root URI is invalid")
    decoded = unquote(root)
    if ".." in decoded.split("/") or "\\" in decoded:
        raise ValueError("MCP root URI must not contain traversal")
    try:
        parsed = urlsplit(decoded)
    except ValueError as exc:
        raise ValueError("MCP root URI is invalid") from exc
    if parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None:
        raise ValueError("MCP root URI must not contain query, fragment, or credentials")
    if parsed.scheme == "mock":
        if not re.fullmatch(
            r"mock://[A-Za-z0-9][A-Za-z0-9._-]{0,120}(?:/[A-Za-z0-9._~!$&'()*+,;=@%/-]{0,180})?",
            decoded,
        ):
            raise ValueError("MCP mock root URI is not controlled")
        if any(part in {".", ".."} for part in decoded.split("/")):
            raise ValueError("MCP root URI must not contain traversal")
        return decoded
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("MCP file root URI has an invalid authority") from exc
    if parsed.scheme != "file" or port is not None:
        raise ValueError("MCP root URI must use controlled file:// or mock:// scheme")
    path = _root_virtual_path(parsed)
    if not _root_is_under_workspace(path, server):
        raise ValueError("MCP file root must stay inside the workspace root")
    return decoded


def _validate_root_list(
    value: Any,
    *,
    server: dict[str, Any],
    request_id: Any,
    error_code: int,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 50:
        raise JsonRpcError(error_code, "MCP roots must be a list of at most 50 URIs", request_id=request_id)
    normalized: list[str] = []
    for root in value:
        try:
            clean = _validate_root_uri(root, server=server)
        except ValueError as exc:
            raise JsonRpcError(error_code, str(exc), request_id=request_id) from exc
        if clean not in normalized:
            normalized.append(clean)
    return normalized


def _server_roots(server: dict[str, Any], *, request_id: Any) -> list[str]:
    value = server.get("roots", [])
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise JsonRpcError(MCP_ROOTS_ERROR, "MCP server roots are invalid", request_id=request_id) from exc
    return _validate_root_list(
        value,
        server=server,
        request_id=request_id,
        error_code=MCP_ROOTS_ERROR,
    )


def _server_key(server: dict[str, Any]) -> str:
    identity = {
        "id": str(server.get("id") or ""),
        "workspace_id": str(server.get("workspace_id") or ""),
        "org_id": str(server.get("org_id") or ""),
        "transport": str(server.get("transport") or ""),
        "protocol_version": str(server.get("protocol_version") or ""),
        "target": str(server.get("endpoint_or_command") or ""),
    }
    return hashlib.sha256(
        json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _id_key(value: Any) -> str:
    return f"{type(value).__name__}:{value!r}"


def _dispose_runtime(runtime: Any) -> None:
    """Best-effort cleanup for a process kept by an in-memory MCP session."""

    process = runtime
    if process is None or getattr(process, "returncode", None) is not None:
        return
    try:
        asyncio.get_running_loop().create_task(_shutdown_runtime(process))
    except (OSError, RuntimeError):
        pass


async def _shutdown_runtime(process: Any) -> None:
    try:
        if getattr(process, "returncode", None) is None:
            process.terminate()
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except (OSError, asyncio.TimeoutError):
        try:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except (OSError, asyncio.TimeoutError):
            pass
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(process, stream_name, None)
        if stream is not None and hasattr(stream, "close"):
            try:
                stream.close()
            except (OSError, RuntimeError, AttributeError):
                pass


def _extract_session_id(
    message: Any,
    request: RpcRequest,
    explicit_session_id: str | None,
) -> tuple[RpcRequest, str | None]:
    candidates: list[Any] = []
    if isinstance(message, dict):
        for key in _SESSION_KEYS:
            if key in message:
                candidates.append(message[key])
    for key in _SESSION_KEYS:
        if key in request.params:
            candidates.append(request.params[key])
    if explicit_session_id is not None:
        candidates.append(explicit_session_id)
    non_null = [value for value in candidates if value is not None]
    if any(not isinstance(value, str) or not value or len(value) > 256 for value in non_null):
        raise JsonRpcError(-32602, "MCP session id is invalid", request_id=request.request_id)
    if len(set(non_null)) > 1:
        raise JsonRpcError(-32602, "MCP session ids do not match", request_id=request.request_id)
    session_id = non_null[0] if non_null else None
    clean_params = {key: value for key, value in request.params.items() if key not in _SESSION_KEYS}
    return (
        RpcRequest(
            request_id=request.request_id,
            method=request.method,
            params=clean_params,
            is_notification=request.is_notification,
        ),
        session_id,
    )


def _session_for_request(
    server: dict[str, Any],
    request: RpcRequest,
    session_id: str | None,
) -> _McpSession | None:
    transport = str(server.get("transport") or "")
    target = str(server.get("endpoint_or_command") or "")
    # Both real and controlled stdio/SSE transports have a session lifecycle.
    # Requiring the local opaque handle prevents an uninitialized request from
    # creating a new process or network stream on every call.
    required = transport in {"stdio", "sse"} or bool(server.get("require_session"))
    if session_id is None:
        if required:
            raise JsonRpcError(
                MCP_SESSION_ERROR,
                "MCP session id is required after initialize",
                request_id=request.request_id,
            )
        return None
    session = _SESSION_REGISTRY.get(session_id)
    if session is None or not secrets.compare_digest(session.server_key, _server_key(server)):
        raise JsonRpcError(
            MCP_SESSION_ERROR,
            "MCP session is unknown or not valid for this server",
            request_id=request.request_id,
        )
    return session


def _success(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": _sanitize(result)}


def _mock_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "echo",
            "description": "Return the supplied text.",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
        {
            "name": "sum",
            "description": "Add a list of numbers.",
            "inputSchema": {
                "type": "object",
                "properties": {"values": {"type": "array", "items": {"type": "number"}}},
                "required": ["values"],
            },
        },
    ]


def _mock_scenario(server: dict[str, Any]) -> str:
    target = str(server.get("endpoint_or_command") or "")
    if not re.fullmatch(r"mock://[A-Za-z0-9][A-Za-z0-9._/?=&%\-]{0,200}", target):
        raise JsonRpcError(MCP_PENDING_ERROR, "MCP mock target is not controlled")
    suffix = target.removeprefix("mock://").strip("/")
    parts = suffix.split("/") if suffix else []
    transport = str(server.get("transport") or "")
    if transport in {"stdio", "sse"}:
        if not parts or parts[0] != transport:
            raise JsonRpcError(
                MCP_PENDING_ERROR,
                "MCP mock target does not match the configured transport",
            )
        parts = parts[1:]
    if any(part in {".", ".."} for part in parts):
        raise JsonRpcError(MCP_PENDING_ERROR, "MCP mock target is not controlled")
    scenario = "-".join(parts) or "default"
    return re.sub(r"[^A-Za-z0-9._-]", "-", scenario)[:80] or "default"


def _mock_call(server: dict[str, Any], request: RpcRequest) -> dict[str, Any]:
    scenario = _mock_scenario(server)
    if scenario in {"error", "unavailable"}:
        raise JsonRpcError(-32001, "Mock MCP server is unavailable", request_id=request.request_id)

    if request.method == "initialize":
        return _success(
            request.request_id,
            {
                "protocolVersion": request.params["protocolVersion"],
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"listChanged": False},
                    "prompts": {"listChanged": False},
                    "roots": {"listChanged": False},
                },
                "serverInfo": {"name": f"workama-mock-{scenario}", "version": "1.0.0"},
            },
        )
    if request.method == "tools/list":
        return _success(request.request_id, {"tools": _mock_tools()})
    if request.method == "resources/list":
        return _success(
            request.request_id,
            {
                "resources": [
                    {
                        "uri": f"mock://{scenario}/README.md",
                        "name": "README",
                        "description": "Deterministic mock resource",
                        "mimeType": "text/plain",
                    }
                ]
            },
        )
    if request.method == "prompts/list":
        return _success(
            request.request_id,
            {
                "prompts": [
                    {
                        "name": "greeting",
                        "description": "A deterministic greeting prompt",
                        "arguments": [],
                    }
                ]
            },
        )
    if request.method == "roots/list":
        return _success(
            request.request_id,
            {"roots": _server_roots(server, request_id=request.request_id)},
        )

    name = request.params["name"]
    arguments = request.params.get("arguments", {})
    if name == "echo":
        text = arguments.get("text")
        if not isinstance(text, str) or len(text) > 8192:
            raise JsonRpcError(-32602, "echo requires string argument text", request_id=request.request_id)
        result = {"content": [{"type": "text", "text": text}], "isError": False}
    elif name == "sum":
        values = arguments.get("values")
        if not isinstance(values, list) or any(
            not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value))
            for value in values
        ):
            raise JsonRpcError(-32602, "sum requires numeric argument values", request_id=request.request_id)
        result = {
            "content": [{"type": "text", "text": str(sum(values))}],
            "isError": False,
        }
    else:
        raise JsonRpcError(-32602, "Unknown mock tool", request_id=request.request_id)
    return _success(request.request_id, result)


def _parse_http_response(response: Any) -> Any:
    headers = getattr(response, "headers", {}) or {}
    try:
        content_length = int(headers.get("content-length", "0"))
    except (TypeError, ValueError):
        content_length = 0
    content = getattr(response, "content", None)
    if content_length > MCP_MAX_MESSAGE_BYTES or (isinstance(content, (bytes, bytearray, str)) and len(content) > MCP_MAX_MESSAGE_BYTES):
        raise ValueError("MCP HTTP response exceeded the size limit")
    content_type = str(headers.get("content-type", "")).lower()
    if "text/event-stream" in content_type:
        for line in str(getattr(response, "text", "")).splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
                if data and data != "[DONE]":
                    return json.loads(data)
        raise ValueError("MCP streamable HTTP response contained no JSON event")
    return response.json()


def _validate_http_response(request: RpcRequest, response: Any) -> dict[str, Any]:
    if not isinstance(response, dict) or response.get("jsonrpc") != JSONRPC_VERSION:
        raise JsonRpcError(-32000, "MCP server returned an invalid JSON-RPC response", request_id=request.request_id)
    if _id_key(response.get("id")) != _id_key(request.request_id):
        raise JsonRpcError(-32000, "MCP server returned a mismatched JSON-RPC id", request_id=request.request_id)
    has_result = "result" in response
    has_error = "error" in response
    if has_result == has_error:
        raise JsonRpcError(-32000, "MCP server returned an invalid JSON-RPC envelope", request_id=request.request_id)
    if has_error:
        error = _sanitize_untrusted(response["error"])
        if (
            not isinstance(error, dict)
            or not isinstance(error.get("code"), int)
            or isinstance(error.get("code"), bool)
        ):
            raise JsonRpcError(-32000, "MCP server returned an invalid JSON-RPC error", request_id=request.request_id)
        return {"jsonrpc": JSONRPC_VERSION, "id": request.request_id, "error": error}
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request.request_id,
        "result": _sanitize_untrusted(response["result"]),
    }


async def _streamable_http_call(
    server: dict[str, Any],
    request: RpcRequest,
    *,
    http_client: Any = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    target = str(server.get("endpoint_or_command") or "")
    try:
        target = validate_endpoint_url(target, resolve=True)
    except ValueError as exc:
        raise JsonRpcError(-32002, "MCP endpoint rejected by SSRF policy", request_id=request.request_id) from exc

    auth_type = str(server.get("auth_type") or "none")
    if auth_type == "oauth":
        raise JsonRpcError(
            -32003,
            "MCP OAuth authorization is pending",
            request_id=request.request_id,
            data={"oauth_pending": True},
        )
    if auth_type == "bearer":
        raise JsonRpcError(
            -32003,
            "Managed MCP bearer credentials are not available in this bridge",
            request_id=request.request_id,
        )

    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": str(server.get("protocol_version") or "2025-06-18"),
    }
    if session_id is not None:
        headers["MCP-Session-Id"] = session_id
    payload = {
        "jsonrpc": JSONRPC_VERSION,
        "id": request.request_id,
        "method": request.method,
        "params": request.params,
    }
    try:
        if http_client is not None:
            response = await http_client.post(target, json=payload, headers=headers)
        else:
            async with httpx.AsyncClient(
                timeout=30,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    target,
                    json=payload,
                    headers=headers,
                )
    except httpx.HTTPError as exc:
        raise JsonRpcError(-32000, "MCP HTTP transport failed", request_id=request.request_id) from exc

    status_code = int(getattr(response, "status_code", 0))
    if bool(getattr(response, "is_redirect", False)) or 300 <= status_code < 400:
        raise JsonRpcError(-32002, "MCP HTTP redirects are not allowed", request_id=request.request_id)
    if status_code < 200 or status_code >= 300:
        raise JsonRpcError(-32000, "MCP server returned an HTTP error", request_id=request.request_id)
    try:
        payload = _parse_http_response(response)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise JsonRpcError(-32000, "MCP server returned invalid JSON", request_id=request.request_id) from exc
    return _validate_http_response(request, payload)


def _transport_timeout() -> float:
    try:
        value = float(os.getenv("WORKAMA_MCP_TRANSPORT_TIMEOUT_SECONDS", str(MCP_TRANSPORT_TIMEOUT_SECONDS)))
    except ValueError:
        value = MCP_TRANSPORT_TIMEOUT_SECONDS
    return min(120.0, max(1.0, value))


def _stdio_argv(target: str) -> list[str]:
    try:
        command = validate_stdio_command(target)
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise JsonRpcError(
            MCP_PENDING_ERROR,
            "MCP stdio command is rejected; no subprocess was started",
            data={"transport": "stdio", "execution": "rejected", "subprocess_started": False},
        ) from exc
    if not argv:
        raise JsonRpcError(
            MCP_PENDING_ERROR,
            "MCP stdio command is rejected; no subprocess was started",
            data={"transport": "stdio", "execution": "rejected", "subprocess_started": False},
        )
    return argv


def _stdio_execution_allowed(argv: list[str]) -> bool:
    enabled = os.getenv("WORKAMA_MCP_STDIO_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
    allowed = {
        os.path.basename(item.strip())
        for item in os.getenv("WORKAMA_MCP_STDIO_ALLOWED_COMMANDS", "").split(",")
        if item.strip()
    }
    return enabled and os.path.basename(argv[0]) in allowed


async def _start_stdio_process(server: dict[str, Any]) -> Any:
    argv = _stdio_argv(str(server.get("endpoint_or_command") or ""))
    if not _stdio_execution_allowed(argv):
        raise JsonRpcError(
            MCP_PENDING_ERROR,
            "MCP stdio execution is disabled by the server policy",
            data={"transport": "stdio", "execution": "disabled", "subprocess_started": False},
        )
    # Do not inherit the API process environment. In particular, credentials
    # and database URLs must never become subprocess environment variables.
    env = {
        "PATH": os.getenv("PATH", ""),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "WORKAMA_MCP_TRANSPORT": "stdio",
    }
    try:
        return await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            ),
            timeout=min(10.0, _transport_timeout()),
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise JsonRpcError(
            -32000,
            "MCP stdio subprocess could not be started",
            data={"transport": "stdio", "execution": "failed", "subprocess_started": False},
        ) from exc


async def _read_stdio_frame(process: Any) -> dict[str, Any]:
    stdout = getattr(process, "stdout", None)
    if stdout is None:
        raise JsonRpcError(-32000, "MCP stdio subprocess has no readable output")
    timeout = _transport_timeout()
    first = await asyncio.wait_for(stdout.readline(), timeout=timeout)
    if not first:
        raise JsonRpcError(-32000, "MCP stdio subprocess closed its output")
    if len(first) > MCP_MAX_MESSAGE_BYTES:
        raise JsonRpcError(-32000, "MCP stdio response exceeded the size limit")
    if first.lower().startswith(b"content-length:"):
        try:
            length = int(first.split(b":", 1)[1].strip())
        except (ValueError, IndexError) as exc:
            raise JsonRpcError(-32000, "MCP stdio response framing is invalid") from exc
        if length < 0 or length > MCP_MAX_MESSAGE_BYTES:
            raise JsonRpcError(-32000, "MCP stdio response exceeded the size limit")
        while True:
            header = await asyncio.wait_for(stdout.readline(), timeout=timeout)
            if not header:
                raise JsonRpcError(-32000, "MCP stdio response framing is incomplete")
            if header in {b"\n", b"\r\n"}:
                break
        raw = await asyncio.wait_for(stdout.readexactly(length), timeout=timeout)
    else:
        raw = first.strip()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JsonRpcError(-32000, "MCP stdio subprocess returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise JsonRpcError(-32000, "MCP stdio subprocess returned an invalid JSON-RPC envelope")
    return value


async def _stdio_call(
    server: dict[str, Any],
    request: RpcRequest,
    *,
    process: Any = None,
) -> tuple[dict[str, Any], Any]:
    process = process or await _start_stdio_process(server)
    stdin = getattr(process, "stdin", None)
    if stdin is None or getattr(process, "returncode", None) is not None:
        _dispose_runtime(process)
        raise JsonRpcError(-32000, "MCP stdio subprocess is not available", request_id=request.request_id)
    payload = {
        "jsonrpc": JSONRPC_VERSION,
        "id": request.request_id,
        "method": request.method,
        "params": request.params,
    }
    try:
        stdin.write((json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        await asyncio.wait_for(stdin.drain(), timeout=_transport_timeout())
        while True:
            response = await _read_stdio_frame(process)
            if response.get("id") is None:
                continue
            if _id_key(response.get("id")) != _id_key(request.request_id):
                raise JsonRpcError(-32000, "MCP stdio response id did not match the request", request_id=request.request_id)
            return _validate_http_response(request, response), process
    except JsonRpcError:
        _dispose_runtime(process)
        raise
    except (BrokenPipeError, ConnectionError, OSError, asyncio.TimeoutError) as exc:
        _dispose_runtime(process)
        raise JsonRpcError(-32000, "MCP stdio transport failed", request_id=request.request_id) from exc


async def _sse_events(response: Any):
    event_name = "message"
    data: list[str] = []
    total = 0
    async for line in response.aiter_lines():
        total += len(line.encode("utf-8")) + 1
        if total > MCP_MAX_MESSAGE_BYTES:
            raise JsonRpcError(-32000, "MCP SSE response exceeded the size limit")
        if not line:
            if data:
                yield event_name, "\n".join(data)
            event_name = "message"
            data = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event_name = value
        elif field == "data":
            data.append(value)
    if data:
        yield event_name, "\n".join(data)


async def _sse_call(
    server: dict[str, Any],
    request: RpcRequest,
    *,
    http_client: Any = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    target = str(server.get("endpoint_or_command") or "")
    try:
        target = validate_endpoint_url(target, resolve=True)
    except ValueError as exc:
        raise JsonRpcError(-32002, "MCP endpoint rejected by SSRF policy", request_id=request.request_id) from exc
    headers = {
        "Accept": "text/event-stream",
        "MCP-Protocol-Version": str(server.get("protocol_version") or "2025-06-18"),
    }
    if session_id is not None:
        headers["MCP-Session-Id"] = session_id
    payload = {
        "jsonrpc": JSONRPC_VERSION,
        "id": request.request_id,
        "method": request.method,
        "params": request.params,
    }
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=_transport_timeout(), follow_redirects=False, trust_env=False)
    try:
        async with client.stream("GET", target, headers=headers) as stream:
            status_code = int(getattr(stream, "status_code", 0))
            if bool(getattr(stream, "is_redirect", False)) or 300 <= status_code < 400:
                raise JsonRpcError(-32002, "MCP SSE redirects are not allowed", request_id=request.request_id)
            if status_code < 200 or status_code >= 300:
                raise JsonRpcError(-32000, "MCP SSE endpoint returned an HTTP error", request_id=request.request_id)
            async for event_name, data in _sse_events(stream):
                if event_name == "endpoint":
                    message_url = urljoin(target, data.strip())
                    try:
                        message_url = validate_endpoint_url(message_url, resolve=True)
                    except ValueError as exc:
                        raise JsonRpcError(-32002, "MCP SSE message endpoint rejected by SSRF policy", request_id=request.request_id) from exc
                    post_headers = {
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                        "MCP-Protocol-Version": headers["MCP-Protocol-Version"],
                    }
                    if session_id is not None:
                        post_headers["MCP-Session-Id"] = session_id
                    try:
                        posted = await client.post(message_url, json=payload, headers=post_headers)
                    except httpx.HTTPError as exc:
                        raise JsonRpcError(-32000, "MCP SSE message delivery failed", request_id=request.request_id) from exc
                    posted_status = int(getattr(posted, "status_code", 0))
                    if bool(getattr(posted, "is_redirect", False)) or 300 <= posted_status < 400:
                        raise JsonRpcError(-32002, "MCP SSE message redirects are not allowed", request_id=request.request_id)
                    if posted_status < 200 or posted_status >= 300:
                        raise JsonRpcError(-32000, "MCP SSE message endpoint returned an HTTP error", request_id=request.request_id)
                    try:
                        posted_payload = _parse_http_response(posted)
                    except (ValueError, TypeError, json.JSONDecodeError):
                        posted_payload = None
                    if isinstance(posted_payload, dict) and posted_payload.get("id") is not None:
                        return _validate_http_response(request, posted_payload)
                    continue
                if not data or data == "[DONE]":
                    continue
                try:
                    response = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(response, dict) and response.get("id") is not None:
                    return _validate_http_response(request, response)
        raise JsonRpcError(-32000, "MCP SSE stream ended without a matching response", request_id=request.request_id)
    except asyncio.TimeoutError as exc:
        raise JsonRpcError(-32000, "MCP SSE transport timed out", request_id=request.request_id) from exc
    except httpx.HTTPError as exc:
        raise JsonRpcError(-32000, "MCP SSE transport failed", request_id=request.request_id) from exc
    finally:
        if owns_client:
            await client.aclose()


def _pending_transport_error(server: dict[str, Any], request: RpcRequest) -> JsonRpcError:
    transport = str(server.get("transport") or "unknown")
    return JsonRpcError(
        MCP_PENDING_ERROR,
        "MCP transport is not implemented in this bridge",
        request_id=request.request_id,
        data={"transport": transport, "execution": "pending"},
    )


def _notification_response(request: RpcRequest, result: dict[str, Any]) -> dict[str, Any]:
    if request.is_notification:
        return {"jsonrpc": JSONRPC_VERSION, "id": None, "result": _sanitize(result)}
    return _success(request.request_id, result)


def _handle_notification(
    request: RpcRequest,
    session: _McpSession | None,
) -> dict[str, Any]:
    if request.method == "notifications/initialized":
        return _notification_response(
            request,
            {"notification": True, "accepted": True, "kind": "initialized"},
        )
    if request.method in {"notifications/cancelled", "$/cancelRequest"}:
        cancelled_id = request.params.get("requestId", request.params.get("request_id"))
        if session is not None:
            session.cancelled_ids.add(_id_key(cancelled_id))
        return _notification_response(
            request,
            {"notification": True, "accepted": True, "kind": "cancelled"},
        )

    token = request.params.get("progressToken", request.params.get("progress_token"))
    token_key = hashlib.sha256(_id_key(token).encode("utf-8")).hexdigest()
    if session is not None:
        session.progress[token_key] = {"has_total": request.params.get("total") is not None}
    return _notification_response(
        request,
        {"notification": True, "accepted": True, "kind": "progress", "has_total": request.params.get("total") is not None},
    )


def _attach_session(response: dict[str, Any], *, session_id: str) -> dict[str, Any]:
    result = response.get("result")
    if not isinstance(result, dict):
        raise JsonRpcError(-32000, "MCP initialize result must be an object", request_id=response.get("id"))
    # The HTTP route cannot set a response header for the protocol bridge, so
    # expose the opaque local handle in a clearly namespaced result extension.
    attached = dict(response)
    attached_result = dict(result)
    attached_result["session_id"] = session_id
    attached["result"] = attached_result
    attached["mcp-session-id"] = session_id
    return attached


async def _bridge_jsonrpc(
    server: dict[str, Any],
    message: Any,
    *,
    http_client: Any = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    try:
        request = validate_jsonrpc_request(message)
        request, request_session_id = _extract_session_id(message, request, session_id)
        _validate_method_params(request, server=server)
        _server_roots(server, request_id=request.request_id)
        if request.method == "initialize" and request_session_id is not None:
            raise JsonRpcError(
                -32602,
                "initialize must not include an MCP session id",
                request_id=request.request_id,
            )

        auth_type = str(server.get("auth_type") or "none")
        if auth_type == "oauth":
            raise JsonRpcError(
                -32003,
                "MCP OAuth authorization is pending",
                request_id=request.request_id,
                data={"oauth_pending": True},
            )
        if auth_type == "bearer":
            raise JsonRpcError(
                -32003,
                "Managed MCP bearer credentials are not available in this bridge",
                request_id=request.request_id,
            )

        transport = str(server.get("transport") or "")
        session = None
        runtime = None
        operation_id = None
        if request.method != "initialize":
            session = _session_for_request(server, request, request_session_id)
            if session is not None and request.request_id is not None:
                if _id_key(request.request_id) in session.cancelled_ids:
                    raise JsonRpcError(
                        MCP_CANCELLED_ERROR,
                        "MCP request was cancelled",
                        request_id=request.request_id,
                    )
                # Update session heartbeat
                _SESSION_REGISTRY.heartbeat(request_session_id)
                # Track operation
                operation_id = f"{request_session_id}:{request.request_id}"
                _OPERATION_TRACKER.start_operation(
                    operation_id,
                    session_id=request_session_id,
                    method=request.method,
                )
        if request.method in SUPPORTED_NOTIFICATIONS:
            return _handle_notification(request, session)

        if request.method in SUPPORTED_PENDING_METHODS:
            raise JsonRpcError(
                MCP_PENDING_ERROR,
                f"MCP {request.method} is pending in this controlled bridge",
                request_id=request.request_id,
                data={"execution": "pending", "external_request_sent": False},
            )

        target = str(server.get("endpoint_or_command") or "")
        try:
            if target.startswith("mock://"):
                if server.get("auth_type") == "oauth":
                    raise JsonRpcError(
                        -32003,
                        "MCP OAuth authorization is pending",
                        request_id=request.request_id,
                        data={"oauth_pending": True},
                    )
                if server.get("auth_type") == "bearer":
                    raise JsonRpcError(
                        -32003,
                        "Managed MCP bearer credentials are not available in this bridge",
                        request_id=request.request_id,
                    )
                response = _mock_call(server, request)
            elif transport == "streamable_http":
                response = await _streamable_http_call(
                    server,
                    request,
                    http_client=http_client,
                    session_id=request_session_id,
                )
            elif transport == "stdio":
                response, runtime = await _stdio_call(
                    server,
                    request,
                    process=session.runtime if session is not None else None,
                )
                if session is not None:
                    session.runtime = runtime
            elif transport == "sse":
                response = await _sse_call(
                    server,
                    request,
                    http_client=http_client,
                    session_id=request_session_id,
                )
            else:
                raise _pending_transport_error(server, request)

            if request.method == "initialize":
                created_session_id = _SESSION_REGISTRY.create(
                    server_key=_server_key(server),
                    transport=transport,
                    protocol_version=request.params["protocolVersion"],
                    runtime=runtime if transport == "stdio" else None,
                )
                return _attach_session(response, session_id=created_session_id)
            # Mark operation as completed on success
            if operation_id is not None:
                _OPERATION_TRACKER.complete_operation(operation_id)
            return response
        except JsonRpcError:
            # Mark operation as cancelled on protocol error
            if operation_id is not None:
                _OPERATION_TRACKER.cancel_operation(operation_id)
            raise
    except JsonRpcError as exc:
        if (
            isinstance(message, dict)
            and "id" in message
            and _valid_id(message.get("id"))
            and exc.request_id is None
        ):
            return exc.with_id(message.get("id")).response()
        return exc.response()


async def bridge_jsonrpc(
    server: dict[str, Any],
    message: Any,
    *,
    http_client: Any = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Execute the bridge while emitting the stable, redacted MCP span contract."""

    method = message.get("method", "invalid") if isinstance(message, dict) else "invalid"
    transport = str(server.get("transport") or "unknown")
    with TRACER.start_as_current_span("mcp.rpc") as span:
        set_span_attributes(
            span,
            mcp_attributes(
                server_id=str(server.get("id") or server.get("server_identity") or "unknown"),
                transport=transport,
                method=str(method),
                status="started",
            ),
        )
        response = await _bridge_jsonrpc(server, message, http_client=http_client, session_id=session_id)
        set_span_attributes(span, {"mcp.status": "error" if response.get("error") else "succeeded"})
        return response


# --------------------------------------------------------------------------- #
# Real external server client (subprocess + SSE interop)
# --------------------------------------------------------------------------- #
class McpClientError(Exception):
    """Raised when a real MCP client transport fails."""


class McpTimeoutError(McpClientError):
    """Raised when a real MCP client operation times out."""


class McpServerError(McpClientError):
    """Raised when a real MCP server returns a JSON-RPC error response."""

    def __init__(self, code, message, data=None, request_id=None):
        super().__init__(f"MCP server error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data
        self.request_id = request_id

class MCPClient:
    """Async MCP client for real external stdio/SSE servers.

    Performs the full JSON-RPC 2.0 handshake against a live subprocess or SSE
    server. Unlike :func:`bridge_jsonrpc`, this client is intended for trusted,
    explicitly-configured server-to-server interop and therefore does not apply
    the multi-tenant SSRF/exec guards that the platform bridge enforces for
    untrusted tenant configuration.
    """

    def __init__(
        self,
        *,
        transport: str,
        endpoint_or_command: str,
        protocol_version: str = "2025-06-18",
        timeout: float = 10.0,
        env: dict[str, str] | None = None,
        client_info: dict[str, str] | None = None,
    ):
        if transport not in {"stdio", "sse"}:
            raise ValueError(f"Unsupported MCP transport: {transport!r}")
        if not endpoint_or_command:
            raise ValueError("MCP endpoint_or_command is required")
        self.transport = transport
        self.endpoint_or_command = endpoint_or_command
        self.protocol_version = protocol_version
        self.timeout = float(timeout)
        self.env = dict(env) if env else None
        self.client_info = client_info or {"name": "workama-mcp-client", "version": "1.0"}

        self._process: Any = None
        self._send_lock = asyncio.Lock()

        self._http_client: Any = None
        self._sse_stream: Any = None
        self._sse_task: Any = None
        self._sse_message_url: str | None = None
        self._sse_session_id: str | None = None
        self._sse_endpoint_ready: asyncio.Event | None = None
        self._open_lock = asyncio.Lock()
        self._pending: dict[Any, asyncio.Future] = {}

        self._next_id = 1
        self._closed = False
        self._initialized = False
        self.capabilities: dict[str, Any] | None = None
        self.server_info: dict[str, Any] | None = None
    async def __aenter__(self) -> "MCPClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    async def connect(self) -> dict[str, Any]:
        """Perform the initialize handshake and notify the server."""
        result = await self.send(
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": self.client_info,
            },
        )
        if not isinstance(result, dict):
            raise McpClientError("MCP initialize result must be an object")
        self.capabilities = result.get("capabilities") or {}
        self.server_info = result.get("serverInfo") or {}
        await self.send("notifications/initialized", {}, is_notification=True)
        self._initialized = True
        return result
    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._process
        self._process = None
        if proc is not None:
            await self._terminate_process(proc)
        if self._sse_task is not None:
            self._sse_task.cancel()
            try:
                await self._sse_task
            except (asyncio.CancelledError, Exception):
                pass
            self._sse_task = None
        self._fail_all_pending(McpClientError("MCP client closed"))
        if self._sse_stream is not None:
            try:
                await self._sse_stream.aclose()
            except Exception:
                pass
            self._sse_stream = None
        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None
    async def list_tools(self) -> dict[str, Any]:
        return await self.send("tools/list")

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.send("tools/call", {"name": name, "arguments": arguments or {}})

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        is_notification: bool = False,
    ) -> dict[str, Any] | None:
        if self._closed:
            raise McpClientError("MCP client is closed")
        request_id = None if is_notification else self._next_request_id()
        payload: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
        if params is not None:
            payload["params"] = params
        if request_id is not None:
            payload["id"] = request_id
        if self.transport == "stdio":
            return await self._stdio_request(payload, request_id, is_notification)
        return await self._sse_request(payload, request_id, is_notification)

    def _next_request_id(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid
    # -- stdio transport ------------------------------------------------- #
    async def _spawn_stdio_process(self) -> Any:
        argv = shlex.split(self.endpoint_or_command, posix=True)
        if not argv:
            raise McpClientError("MCP stdio command is empty")
        spawn_env = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
        }
        if self.env:
            spawn_env.update({k: str(v) for k, v in self.env.items()})
        try:
            return await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=spawn_env,
            )
        except (OSError, FileNotFoundError) as exc:
            raise McpClientError(f"MCP stdio subprocess could not be started: {exc}") from exc

    async def _stdio_request(self, payload, request_id, is_notification):
        async with self._send_lock:
            if self._process is None:
                self._process = await self._spawn_stdio_process()
            await self._stdio_write(payload)
            if is_notification:
                return None
            return await self._stdio_read_response(request_id)

    async def _stdio_write(self, payload: dict[str, Any]) -> None:
        proc = self._process
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise McpClientError("MCP stdio subprocess is not available")
        data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(data) > MCP_MAX_MESSAGE_BYTES:
            raise McpClientError("MCP stdio request exceeded the size limit")
        try:
            proc.stdin.write(data)
            await asyncio.wait_for(proc.stdin.drain(), timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            raise McpTimeoutError("MCP stdio write timed out") from exc
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            raise McpClientError("MCP stdio write failed (subprocess may have exited)") from exc
    async def _read_stdio_frame(self, process: Any) -> dict[str, Any]:
        stdout = process.stdout
        if stdout is None:
            raise McpClientError("MCP stdio subprocess has no readable output")
        first = await stdout.readline()
        if not first:
            rc = process.returncode
            raise McpClientError(f"MCP stdio subprocess closed stdout (returncode={rc})")
        if len(first) > MCP_MAX_MESSAGE_BYTES:
            raise McpClientError("MCP stdio response exceeded the size limit")
        first = first.rstrip(b"\r\n")
        if first.lower().startswith(b"content-length:"):
            try:
                length = int(first.split(b":", 1)[1].strip())
            except ValueError as exc:
                raise McpClientError("MCP stdio content-length framing is invalid") from exc
            if length < 0 or length > MCP_MAX_MESSAGE_BYTES:
                raise McpClientError("MCP stdio response exceeded the size limit")
            while True:
                header = await stdout.readline()
                if not header:
                    raise McpClientError("MCP stdio response framing is incomplete")
                if header in (b"\n", b"\r\n"):
                    break
            raw = await stdout.readexactly(length)
        else:
            raw = first
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise McpClientError("MCP stdio subprocess returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise McpClientError("MCP stdio subprocess returned an invalid JSON-RPC envelope")
        return value
    async def _stdio_read_response(self, request_id: int) -> dict[str, Any]:
        proc = self._process
        if proc is None:
            raise McpClientError("MCP stdio subprocess is not available")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise McpTimeoutError(f"MCP stdio timed out waiting for response to id={request_id}")
            try:
                frame = await asyncio.wait_for(self._read_stdio_frame(proc), timeout=remaining)
            except asyncio.TimeoutError:
                raise McpTimeoutError(f"MCP stdio timed out waiting for response to id={request_id}")
            if frame.get("id") is None:
                continue
            if frame.get("id") != request_id:
                continue
            if "error" in frame:
                err = frame["error"] if isinstance(frame["error"], dict) else {}
                raise McpServerError(err.get("code"), err.get("message"), err.get("data"), request_id)
            if "result" not in frame:
                raise McpClientError("MCP stdio response had no result or error")
            return frame["result"]

    async def _terminate_process(self, proc: Any) -> None:
        try:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=1.0)
                    except (asyncio.TimeoutError, OSError):
                        pass
        except (OSError, ProcessLookupError):
            pass
        for name in ("stdin", "stdout", "stderr"):
            stream = getattr(proc, name, None)
            if stream is not None:
                try:
                    stream.close()
                except (OSError, RuntimeError, AttributeError):
                    pass
    # -- SSE transport --------------------------------------------------- #
    async def _sse_ensure_open(self) -> None:
        async with self._open_lock:
            if self._http_client is not None:
                return
            self._sse_endpoint_ready = asyncio.Event()
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=self.timeout),
                follow_redirects=False,
            )
            req = self._http_client.build_request(
                "GET",
                self.endpoint_or_command,
                headers={
                    "Accept": "text/event-stream",
                    "MCP-Protocol-Version": self.protocol_version,
                },
            )
            try:
                response = await self._http_client.send(req, stream=True)
            except httpx.HTTPError as exc:
                await self._http_client.aclose()
                self._http_client = None
                raise McpClientError(f"MCP SSE connect failed: {exc}") from exc
            if response.status_code < 200 or response.status_code >= 300:
                status = response.status_code
                await response.aclose()
                await self._http_client.aclose()
                self._http_client = None
                raise McpClientError(f"MCP SSE endpoint returned HTTP {status}")
            self._sse_stream = response
            self._sse_session_id = response.headers.get("mcp-session-id")
            self._sse_task = asyncio.create_task(self._sse_reader())
            try:
                await asyncio.wait_for(self._sse_endpoint_ready.wait(), timeout=self.timeout)
            except asyncio.TimeoutError:
                await self.aclose()
                raise McpTimeoutError("MCP SSE endpoint did not send an endpoint event")
            if self._sse_message_url is None:
                await self.aclose()
                raise McpClientError("MCP SSE endpoint did not provide a message URL")
    async def _sse_reader(self) -> None:
        try:
            async for event_name, data in self._sse_iter_events(self._sse_stream):
                if event_name == "endpoint":
                    self._sse_message_url = urljoin(self.endpoint_or_command, data.strip())
                    if self._sse_endpoint_ready is not None:
                        self._sse_endpoint_ready.set()
                    continue
                if event_name != "message":
                    continue
                if not data or data == "[DONE]":
                    continue
                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                rid = msg.get("id")
                if rid is None:
                    continue
                fut = self._pending.pop(rid, None)
                if fut is None or fut.done():
                    continue
                if "error" in msg:
                    err = msg["error"] if isinstance(msg["error"], dict) else {}
                    fut.set_exception(
                        McpServerError(err.get("code"), err.get("message"), err.get("data"), rid)
                    )
                elif "result" in msg:
                    fut.set_result(msg["result"])
                else:
                    fut.set_exception(McpClientError("MCP SSE message had no result or error"))
            self._fail_all_pending(McpClientError("MCP SSE stream ended without a response"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_all_pending(McpClientError(f"MCP SSE stream closed: {exc}"))
    async def _sse_iter_events(self, stream: Any):
        event_name = "message"
        data: list[str] = []
        total = 0
        async for line in stream.aiter_lines():
            total += len(line.encode("utf-8")) + 1
            if total > MCP_MAX_MESSAGE_BYTES:
                raise McpClientError("MCP SSE response exceeded the size limit")
            if not line:
                if data:
                    yield event_name, "\n".join(data)
                event_name = "message"
                data = []
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                event_name = value
            elif field == "data":
                data.append(value)
        if data:
            yield event_name, "\n".join(data)
    def _sse_post_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self.protocol_version,
        }
        if self._sse_session_id:
            headers["MCP-Session-Id"] = self._sse_session_id
        return headers

    async def _sse_request(self, payload, request_id, is_notification):
        await self._sse_ensure_open()
        if self._sse_message_url is None:
            raise McpClientError("MCP SSE message endpoint is not available")
        headers = self._sse_post_headers()
        if is_notification:
            try:
                await self._http_client.post(self._sse_message_url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                raise McpClientError(f"MCP SSE notification POST failed: {exc}") from exc
            return None
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[request_id] = fut
        try:
            resp = await self._http_client.post(self._sse_message_url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            self._pending.pop(request_id, None)
            raise McpClientError(f"MCP SSE POST failed: {exc}") from exc
        if resp.status_code < 200 or resp.status_code >= 300:
            self._pending.pop(request_id, None)
            raise McpClientError(f"MCP SSE message endpoint returned HTTP {resp.status_code}")
        try:
            return await asyncio.wait_for(fut, timeout=self.timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise McpTimeoutError(f"MCP SSE timed out waiting for response to id={request_id}")

    def _fail_all_pending(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()