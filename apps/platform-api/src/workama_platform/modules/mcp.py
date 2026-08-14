from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import secrets
import socket
from datetime import UTC, datetime
from typing import Annotated, Any, Callable, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, model_validator

from workama_platform.core import Actor, capability_allows, get_actor, json_dumps, new_id, pool, redis
from workama_platform.modules.auth.service import new_oauth_state, new_pkce_verifier, pkce_challenge


router = APIRouter(prefix="/api/v1/mcp-servers", tags=["mcp"])

MCP_TRANSPORTS = frozenset({"stdio", "sse", "streamable_http"})
MCP_PROTOCOL_VERSIONS = frozenset({"2024-11-05", "2025-03-26", "2025-06-18"})
MCP_SERVER_STATUSES = frozenset(
    {"draft", "validating", "enabled", "degraded", "circuit_open", "half_open", "disabled", "deleted"}
)
MCP_OAUTH_SCOPES = frozenset({"mcp:tools", "mcp:resources", "mcp:prompts"})
MCP_MANAGEMENT_ROLES = frozenset({"owner", "admin"})
MCP_READ_ROLES = frozenset({"owner", "admin", "member", "viewer"})
MCP_CAPABILITY_KINDS = ("tools", "resources", "prompts")

# These terms describe the actual operation rather than a server-provided
# annotation. The list is intentionally conservative: an over-classified item
# can be reviewed, while an under-classified item could bypass approval.
_CRITICAL_TERMS = (
    "credential",
    "password",
    "secret",
    "private_key",
    "private-key",
    "api_key",
    "api-key",
    "payment",
    "billing",
    "admin",
    "sudo",
)
_HIGH_TERMS = (
    "execute",
    "exec",
    "shell",
    "terminal",
    "command",
    "write",
    "delete",
    "remove",
    "send",
    "publish",
    "upload",
    "network",
    "http",
    "browser",
    "root",
    "filesystem",
    "file",
)
_RISK_FIELDS = frozenset(
    {
        "risk",
        "risk_level",
        "riskLevel",
        "sensitive",
        "destructive",
        "destructiveHint",
        "readOnly",
        "read_only",
        "readOnlyHint",
        "openWorldHint",
    }
)
_NAME_RE = re.compile(r"^[^\x00\r\n]{1,128}$")
_SECRET_IN_COMMAND_RE = re.compile(
    r"(?:--?(?:api[-_]?key|token|secret|password)|(?:api[-_]?key|token|secret|password)\s*=)",
    re.IGNORECASE,
)
_PRIVATE_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".intranet",
    ".home.arpa",
)
_METADATA_IPS = frozenset({"100.100.100.200", "169.254.169.254"})


class McpServerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    transport: Literal["stdio", "sse", "streamable_http"]
    endpoint_or_command: str | None = Field(default=None, min_length=1, max_length=2048)
    endpoint: str | None = Field(default=None, min_length=1, max_length=2048)
    command: str | None = Field(default=None, min_length=1, max_length=2048)
    auth_type: Literal["none", "oauth", "bearer"] = "none"
    auth_ref: str | None = Field(default=None, min_length=1, max_length=256)
    protocol_version: str = Field(default="2025-06-18", min_length=1, max_length=40)
    approval_policy: Literal["explicit", "workspace", "always"] = "explicit"
    roots: list[str] = Field(default_factory=list, max_length=50)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    server_identity: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def target_alias(self) -> "McpServerCreate":
        target = self.endpoint_or_command or self.endpoint or self.command
        if not target:
            raise ValueError("endpoint_or_command is required")
        object.__setattr__(self, "endpoint_or_command", target)
        return self


class McpServerPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    endpoint_or_command: str | None = Field(default=None, min_length=1, max_length=2048)
    endpoint: str | None = Field(default=None, min_length=1, max_length=2048)
    command: str | None = Field(default=None, min_length=1, max_length=2048)
    auth_type: Literal["none", "oauth", "bearer"] | None = None
    auth_ref: str | None = Field(default=None, min_length=1, max_length=256)
    protocol_version: str | None = Field(default=None, min_length=1, max_length=40)
    approval_policy: Literal["explicit", "workspace", "always"] | None = None
    roots: list[str] | None = Field(default=None, max_length=50)
    capabilities: dict[str, Any] | None = None

    def target(self) -> str | None:
        return self.endpoint_or_command or self.endpoint or self.command


class McpDiscoveryRequest(BaseModel):
    protocol_version: str = Field(min_length=1, max_length=40)
    server_identity: dict[str, Any] = Field(default_factory=dict)
    server_capabilities: dict[str, Any] = Field(default_factory=dict)
    tools: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    resources: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    prompts: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    roots: list[str] = Field(default_factory=list, max_length=50)


class McpServerTestRequest(BaseModel):
    resolve_dns: bool = False


class McpAuthorizationStart(BaseModel):
    scopes: list[str] = Field(default_factory=list, max_length=30)


def ensure_mcp_schema_statements() -> tuple[str, ...]:
    """Return additive SQL for the MCP registry.

    Keeping this separate from ``main`` lets the migration runner and tests
    apply the same schema contract without changing the application bootstrap.
    """

    return (
        """
        CREATE TABLE IF NOT EXISTS ag_mcp_server (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
            workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            transport TEXT NOT NULL CHECK (transport IN ('stdio','sse','streamable_http')),
            endpoint_or_command TEXT NOT NULL,
            auth_type TEXT NOT NULL DEFAULT 'none' CHECK (auth_type IN ('none','oauth','bearer')),
            auth_ref TEXT,
            protocol_version TEXT NOT NULL,
            server_identity JSONB NOT NULL DEFAULT '{}'::jsonb,
            capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
            schema_hash TEXT NOT NULL DEFAULT '',
            roots JSONB NOT NULL DEFAULT '[]'::jsonb,
            approval_policy TEXT NOT NULL DEFAULT 'explicit'
                CHECK (approval_policy IN ('explicit','workspace','always')),
            risk_policy JSONB NOT NULL DEFAULT '{"source":"workama","sensitive_default":"approval"}'::jsonb,
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft','validating','enabled','degraded','circuit_open','half_open','disabled','deleted')),
            last_test JSONB NOT NULL DEFAULT '{}'::jsonb,
            last_tested_at TIMESTAMPTZ,
            version INTEGER NOT NULL DEFAULT 1,
            created_by TEXT NOT NULL REFERENCES id_user(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_ag_mcp_server_workspace_name_active ON ag_mcp_server(workspace_id, name) WHERE status <> 'deleted'",
        "CREATE INDEX IF NOT EXISTS idx_ag_mcp_server_workspace_status ON ag_mcp_server(workspace_id, status, updated_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_ag_mcp_server_org_status ON ag_mcp_server(org_id, status, updated_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_ag_mcp_server_schema_hash ON ag_mcp_server(workspace_id, schema_hash)",
    )


async def ensure_mcp_schema(conn) -> None:
    """Apply the MCP registry schema to an existing connection.

    This function is intentionally not imported by ``main.py``. Deployment
    wiring remains the caller's decision while the migration and module share
    one idempotent definition.
    """

    for statement in ensure_mcp_schema_statements():
        await conn.execute(statement)


def _json_object(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _text_for_risk(kind: str, item: dict[str, Any]) -> str:
    relevant: dict[str, Any] = {}
    if kind == "tools":
        relevant = {
            "name": item.get("name"),
            "description": item.get("description"),
            "input_schema": item.get("input_schema", item.get("inputSchema")),
        }
    elif kind == "resources":
        relevant = {"name": item.get("name"), "uri": item.get("uri"), "description": item.get("description")}
    else:
        relevant = {"name": item.get("name"), "description": item.get("description")}
    return json.dumps(relevant, ensure_ascii=True, sort_keys=True, default=str).lower()


def classify_capability_risk(kind: str, item: dict[str, Any]) -> Literal["low", "medium", "high", "critical"]:
    """Derive risk from the requested operation, never from server annotations."""

    if kind not in MCP_CAPABILITY_KINDS:
        raise ValueError(f"Unsupported MCP capability kind: {kind}")
    text = _text_for_risk(kind, item)
    if any(term in text for term in _CRITICAL_TERMS):
        return "critical"
    if any(term in text for term in _HIGH_TERMS):
        return "high"
    if kind == "tools" and item.get("input_schema", item.get("inputSchema")):
        return "medium"
    return "low"


def _clean_description(value: Any) -> str:
    return str(value or "").strip()[:4000]


def _clean_name(value: Any, kind: str) -> str:
    name = str(value or "").strip()
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"{kind} capability name is invalid")
    return name


def _clean_schema(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("MCP input schema must be an object")
    # A server can provide arbitrary annotations in a schema. They are data,
    # not policy, but stripping policy-like keys prevents accidental trust.
    return {str(key): child for key, child in value.items() if str(key) not in _RISK_FIELDS}


def _drop_policy_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _drop_policy_fields(child)
            for key, child in value.items()
            if str(key) not in _RISK_FIELDS
        }
    if isinstance(value, list):
        return [_drop_policy_fields(child) for child in value]
    return value


def _normalize_tool(item: dict[str, Any]) -> dict[str, Any]:
    name = _clean_name(item.get("name"), "tool")
    normalized = {
        "name": name,
        "description": _clean_description(item.get("description")),
        "input_schema": _clean_schema(item.get("input_schema", item.get("inputSchema"))),
    }
    normalized.update(
        {
            "platform_risk": classify_capability_risk("tools", normalized),
            "risk_source": "workama_policy",
            "untrusted_source": True,
            "server_risk_ignored": True,
        }
    )
    return normalized


def _normalize_resource(item: dict[str, Any]) -> dict[str, Any]:
    uri = str(item.get("uri") or "").strip()
    if not uri or len(uri) > 2048 or "\x00" in uri:
        raise ValueError("resource URI is invalid")
    normalized = {
        "name": _clean_name(item.get("name") or uri, "resource"),
        "uri": uri,
        "description": _clean_description(item.get("description")),
        "mime_type": str(item.get("mime_type", item.get("mimeType", "")) or "")[:200],
    }
    normalized.update(
        {
            "platform_risk": classify_capability_risk("resources", normalized),
            "risk_source": "workama_policy",
            "untrusted_source": True,
            "server_risk_ignored": True,
        }
    )
    return normalized


def _normalize_prompt(item: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "name": _clean_name(item.get("name"), "prompt"),
        "description": _clean_description(item.get("description")),
        "arguments": item.get("arguments") if isinstance(item.get("arguments"), list) else [],
    }
    normalized.update(
        {
            "platform_risk": classify_capability_risk("prompts", normalized),
            "risk_source": "workama_policy",
            "untrusted_source": True,
            "server_risk_ignored": True,
        }
    )
    return normalized


def normalize_capability_snapshot(
    *,
    tools: list[dict[str, Any]] | None = None,
    resources: list[dict[str, Any]] | None = None,
    prompts: list[dict[str, Any]] | None = None,
    server_capabilities: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """Normalize an untrusted MCP discovery response and hash its policy view."""

    values = {"tools": tools or [], "resources": resources or [], "prompts": prompts or []}
    if any(not isinstance(items, list) for items in values.values()):
        raise ValueError("MCP capability collections must be lists")
    if any(not isinstance(item, dict) for items in values.values() for item in items):
        raise ValueError("MCP capability entries must be objects")
    if server_capabilities is not None and not isinstance(server_capabilities, dict):
        raise ValueError("MCP server capabilities must be an object")
    snapshot = {
        "tools": [_normalize_tool(item) for item in values["tools"]],
        "resources": [_normalize_resource(item) for item in values["resources"]],
        "prompts": [_normalize_prompt(item) for item in values["prompts"]],
        "server_capabilities": _drop_policy_fields(dict(server_capabilities or {})),
        "untrusted_source": True,
        "risk_policy": "workama_policy_v1",
    }
    return snapshot, _canonical_hash(snapshot)


def _forbidden_ip(value: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        str(value) in _METADATA_IPS
        or value.is_private
        or value.is_loopback
        or value.is_link_local
        or value.is_reserved
        or value.is_multicast
        or value.is_unspecified
        or not value.is_global
    )


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        # Decimal IPv4 literals (for example 2130706433) are accepted by some
        # HTTP clients and must not bypass the private-address check.
        if host.isdecimal():
            try:
                numeric = int(host, 10)
                if 0 <= numeric <= 0xFFFFFFFF:
                    return ipaddress.IPv4Address(numeric)
            except ValueError:
                pass
        # inet_aton also accepts legacy hexadecimal, octal and shortened
        # dotted IPv4 forms that some URL clients still interpret as IPs.
        try:
            return ipaddress.IPv4Address(socket.inet_aton(host))
        except (OSError, ValueError):
            pass
        return None


def validate_host_not_private(
    host: str,
    *,
    port: int = 443,
    resolve: bool = False,
    resolver: Callable[[str, int], list[str]] | None = None,
) -> str:
    """Validate a remote host and optionally re-check all resolved addresses."""

    normalized = host.rstrip(".").strip().lower()
    if not normalized:
        raise ValueError("MCP endpoint host is required")
    try:
        ascii_host = normalized.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("MCP endpoint host is not a valid hostname") from exc
    if ascii_host == "localhost" or ascii_host.endswith(_PRIVATE_HOST_SUFFIXES):
        raise ValueError("MCP endpoint cannot target a local hostname")
    literal = _parse_ip_literal(ascii_host)
    if literal is not None and _forbidden_ip(literal):
        raise ValueError("MCP endpoint cannot target a private or reserved address")
    if resolve and literal is None:
        lookup = resolver or _resolve_host
        try:
            addresses = lookup(ascii_host, port)
        except OSError as exc:
            raise ValueError("MCP endpoint host could not be resolved safely") from exc
        if not addresses:
            raise ValueError("MCP endpoint host has no resolved address")
        for address in addresses:
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError as exc:
                raise ValueError("MCP endpoint resolver returned an invalid address") from exc
            if _forbidden_ip(parsed):
                raise ValueError("MCP endpoint resolves to a private or reserved address")
    return ascii_host


def _resolve_host(host: str, port: int) -> list[str]:
    return sorted({str(item[4][0]) for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})


def validate_endpoint_url(
    value: str,
    *,
    resolve: bool = False,
    resolver: Callable[[str, int], list[str]] | None = None,
) -> str:
    """Validate an SSE/Streamable HTTP URL against common SSRF targets."""

    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError("MCP endpoint URL has an invalid port") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("MCP endpoint must be an absolute HTTP(S) URL")
    if any(character in value for character in "\x00\r\n"):
        raise ValueError("MCP endpoint must not contain control characters")
    if port == 0:
        raise ValueError("MCP endpoint port must be between 1 and 65535")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("MCP endpoint must not contain user credentials")
    if parsed.fragment:
        raise ValueError("MCP endpoint must not contain a fragment")
    host = parsed.hostname
    if not host:
        raise ValueError("MCP endpoint host is required")
    validate_host_not_private(
        host,
        port=port or (443 if parsed.scheme == "https" else 80),
        resolve=resolve,
        resolver=resolver,
    )
    return parsed.geturl()


def validate_stdio_command(value: str) -> str:
    command = value.strip()
    if not command or "\x00" in command or "\r" in command or "\n" in command:
        raise ValueError("MCP stdio command is invalid")
    if len(command) > 2048:
        raise ValueError("MCP stdio command is too long")
    if _SECRET_IN_COMMAND_RE.search(command):
        raise ValueError("MCP stdio command must not contain inline credentials")
    return command


def validate_transport_target(transport: str, value: str, *, resolve: bool = False) -> str:
    if transport not in MCP_TRANSPORTS:
        raise ValueError("Unsupported MCP transport")
    if value.strip().startswith("mock://"):
        if transport != "streamable_http":
            raise ValueError("mock:// targets require streamable_http transport")
        if not re.fullmatch(r"mock://[A-Za-z0-9][A-Za-z0-9._/?=&%\-]{0,200}", value.strip()):
            raise ValueError("MCP mock target is invalid")
        return value.strip()
    if transport == "stdio":
        return validate_stdio_command(value)
    return validate_endpoint_url(value, resolve=resolve)


def _validate_protocol(protocol_version: str) -> str:
    if protocol_version not in MCP_PROTOCOL_VERSIONS:
        raise ValueError(f"Unsupported MCP protocol version: {protocol_version}")
    return protocol_version


def _validate_auth_ref(auth_type: str, auth_ref: str | None) -> str | None:
    if auth_type == "none" and auth_ref:
        raise ValueError("auth_ref is not allowed when auth_type is none")
    if auth_ref and _SECRET_IN_COMMAND_RE.search(auth_ref):
        raise ValueError("auth_ref must reference managed credentials, not contain a secret")
    return auth_ref


def _normalize_roots(roots: list[str]) -> list[str]:
    normalized: list[str] = []
    for root in roots:
        value = str(root).strip()
        if not value or len(value) > 2048 or "\x00" in value:
            raise ValueError("MCP root URI is invalid")
        if value not in normalized:
            normalized.append(value)
    return normalized


def oauth_metadata_placeholder(server_id: str | None = None) -> dict[str, Any]:
    """Return explicit OAuth capability metadata until an IdP is configured.

    The authorization envelope is functional and PKCE-protected, while the
    external provider token exchange remains deliberately pending.
    """

    metadata = {
        "issuer": None,
        "authorization_endpoint": None,
        "token_endpoint": None,
        "registration_endpoint": None,
        "scopes_supported": sorted(MCP_OAUTH_SCOPES),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "configured": False,
        "status": "pending_external_configuration",
        "server_id": server_id,
        "credential_upload_supported": False,
        "authorization_state_supported": True,
        "callback_endpoint": "/api/v1/mcp-servers/oauth/callback",
    }
    return metadata


def _etag(version: int) -> str:
    return f'W/"{version}"'


def _assert_if_match(if_match: str | None, version: int) -> None:
    if if_match is None:
        return
    if if_match.strip() not in {"*", str(version), _etag(version), f'"{version}"'}:
        raise HTTPException(status_code=412, detail="MCP server version does not match If-Match")


def can_manage_mcp(actor: Actor) -> bool:
    return actor.role in MCP_MANAGEMENT_ROLES or capability_allows(actor.capabilities, "mcp_server:write")


def can_read_mcp(actor: Actor) -> bool:
    return actor.role in MCP_READ_ROLES or capability_allows(actor.capabilities, "mcp_server:read")


def _require_read(actor: Actor) -> None:
    if not can_read_mcp(actor):
        raise HTTPException(status_code=403, detail="Missing capability: mcp_server:read")


def _require_manage(actor: Actor) -> None:
    if not can_manage_mcp(actor):
        raise HTTPException(status_code=403, detail="Owner or admin role required for MCP server management")


def _target_for_patch(row: dict[str, Any], body: McpServerPatch) -> str:
    return body.target() or str(row["endpoint_or_command"])


def _server_summary(row: dict[str, Any]) -> dict[str, Any]:
    capabilities = _json_object(row.get("capabilities"), {})
    roots = _json_object(row.get("roots"), [])
    return {
        "id": row["id"],
        "org_id": row["org_id"],
        "workspace_id": row["workspace_id"],
        "name": row["name"],
        "transport": row["transport"],
        "endpoint_or_command": row["endpoint_or_command"],
        "auth": {"type": row.get("auth_type", "none"), "configured": bool(row.get("auth_ref"))},
        "protocol_version": row["protocol_version"],
        "server_identity": _json_object(row.get("server_identity"), {}),
        "capabilities": capabilities,
        "schema_hash": row.get("schema_hash", ""),
        "roots": roots,
        "approval_policy": row.get("approval_policy", "explicit"),
        "risk_policy": _json_object(row.get("risk_policy"), {"source": "workama"}),
        "status": row["status"],
        "last_test": _json_object(row.get("last_test"), {}),
        "last_tested_at": row.get("last_tested_at"),
        "version": row.get("version", 1),
        "created_by": row.get("created_by"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


async def _get_server(conn, server_id: str, workspace_id: str, *, for_update: bool = False) -> dict[str, Any]:
    lock = " FOR UPDATE" if for_update else ""
    result = await conn.execute(
        f"SELECT * FROM ag_mcp_server WHERE id=%s AND workspace_id=%s AND status <> 'deleted'{lock}",
        (server_id, workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return row


def _http_validation_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


@router.get("/oauth/metadata")
async def mcp_oauth_metadata() -> dict[str, Any]:
    return oauth_metadata_placeholder()


@router.get("")
async def list_mcp_servers(
    actor: Annotated[Actor, Depends(get_actor)],
    status_filter: str | None = Query(default=None, alias="status"),
    query: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    _require_read(actor)
    # Handler unit tests and internal callers can invoke this function without
    # FastAPI resolving Query defaults first.
    if not isinstance(status_filter, str):
        status_filter = None
    if not isinstance(query, str):
        query = None
    if not isinstance(limit, int):
        limit = 50
    if status_filter is not None and status_filter not in MCP_SERVER_STATUSES:
        raise HTTPException(status_code=422, detail="Unsupported MCP server status")
    clauses = ["workspace_id=%s", "status <> 'deleted'"]
    params: list[Any] = [actor.workspace_id]
    if status_filter:
        clauses.append("status=%s")
        params.append(status_filter)
    if query:
        clauses.append("name ILIKE %s")
        params.append(f"%{query.strip()}%")
    params.append(limit)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"SELECT * FROM ag_mcp_server WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT %s",
            tuple(params),
        )
        rows = await result.fetchall()
    # Contract《720》listMCPServers: ListQuery -> ListResponse<MCPServerDTO>
    # 保留 items 与 count 字段向后兼容
    data = [_server_summary(row) for row in rows]
    return {
        "items": data,
        "count": len(data),
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_mcp_server(body: McpServerCreate, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    _require_manage(actor)
    try:
        protocol = _validate_protocol(body.protocol_version)
        target = validate_transport_target(body.transport, body.endpoint_or_command or "", resolve=body.transport != "stdio")
        auth_ref = _validate_auth_ref(body.auth_type, body.auth_ref)
        roots = _normalize_roots(body.roots)
        capabilities, schema_hash = normalize_capability_snapshot(
            tools=body.capabilities.get("tools", []),
            resources=body.capabilities.get("resources", []),
            prompts=body.capabilities.get("prompts", []),
            server_capabilities=body.capabilities.get("server_capabilities", {}),
        )
    except ValueError as exc:
        raise _http_validation_error(exc) from exc
    server_id = new_id("mcp")
    async with pool.connection() as conn:
        async with conn.transaction():
            duplicate = await conn.execute(
                "SELECT 1 FROM ag_mcp_server WHERE workspace_id=%s AND name=%s AND status <> 'deleted'",
                (actor.workspace_id, body.name.strip()),
            )
            if await duplicate.fetchone():
                raise HTTPException(status_code=409, detail="MCP server name already exists in this workspace")
            result = await conn.execute(
                """
                INSERT INTO ag_mcp_server(
                    id,org_id,workspace_id,name,transport,endpoint_or_command,auth_type,auth_ref,
                    protocol_version,server_identity,capabilities,schema_hash,roots,approval_policy,
                    risk_policy,status,created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s::jsonb,%s,
                          '{"source":"workama","sensitive_default":"approval","server_risk":"ignored"}'::jsonb,
                          'draft',%s)
                RETURNING *
                """,
                (
                    server_id,
                    actor.org_id,
                    actor.workspace_id,
                    body.name.strip(),
                    body.transport,
                    target,
                    body.auth_type,
                    auth_ref,
                    protocol,
                    json_dumps(body.server_identity),
                    json_dumps(capabilities),
                    schema_hash,
                    json_dumps(roots),
                    body.approval_policy,
                    actor.user_id,
                ),
            )
            row = await result.fetchone()
    return _server_summary(row)


@router.get("/{server_id}")
async def get_mcp_server(server_id: str, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    _require_read(actor)
    async with pool.connection() as conn:
        row = await _get_server(conn, server_id, actor.workspace_id)
    return _server_summary(row)


@router.patch("/{server_id}")
async def update_mcp_server(
    server_id: str,
    body: McpServerPatch,
    actor: Annotated[Actor, Depends(get_actor)],
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    _require_manage(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await _get_server(conn, server_id, actor.workspace_id, for_update=True)
            _assert_if_match(if_match, int(row.get("version", 1)))
            try:
                values: dict[str, Any] = {}
                if body.name is not None:
                    values["name"] = body.name.strip()
                if body.target() is not None:
                    values["endpoint_or_command"] = body.target()
                if body.auth_type is not None:
                    values["auth_type"] = body.auth_type
                if body.auth_ref is not None or body.auth_type == "none":
                    values["auth_ref"] = body.auth_ref
                if body.protocol_version is not None:
                    values["protocol_version"] = body.protocol_version
                if body.approval_policy is not None:
                    values["approval_policy"] = body.approval_policy
                if body.roots is not None:
                    values["roots"] = _normalize_roots(body.roots)
                if body.capabilities is not None:
                    capabilities, schema_hash = normalize_capability_snapshot(
                        tools=body.capabilities.get("tools", []),
                        resources=body.capabilities.get("resources", []),
                        prompts=body.capabilities.get("prompts", []),
                        server_capabilities=body.capabilities.get("server_capabilities", {}),
                    )
                    values["capabilities"] = capabilities
                    values["schema_hash"] = schema_hash
                if "protocol_version" in values:
                    _validate_protocol(values["protocol_version"])
                if "endpoint_or_command" in values:
                    values["endpoint_or_command"] = validate_transport_target(
                        row["transport"], values["endpoint_or_command"], resolve=row["transport"] != "stdio"
                    )
                auth_type = values.get("auth_type", row["auth_type"])
                _validate_auth_ref(auth_type, values.get("auth_ref", row.get("auth_ref")))
            except ValueError as exc:
                raise _http_validation_error(exc) from exc
            if "name" in values:
                duplicate = await conn.execute(
                    "SELECT 1 FROM ag_mcp_server WHERE workspace_id=%s AND name=%s AND id<>%s AND status <> 'deleted'",
                    (actor.workspace_id, values["name"], server_id),
                )
                if await duplicate.fetchone():
                    raise HTTPException(status_code=409, detail="MCP server name already exists in this workspace")
            if not values:
                response.headers["ETag"] = _etag(int(row.get("version", 1)))
                return _server_summary(row)
            assignments: list[str] = []
            params: list[Any] = []
            for key, value in values.items():
                assignments.append(f"{key}=%s" + ("::jsonb" if key in {"roots", "capabilities"} else ""))
                params.append(json_dumps(value) if key in {"roots", "capabilities"} else value)
            assignments.extend(["status='draft'", "version=version+1", "updated_at=now()"])
            params.extend([server_id, actor.workspace_id])
            result = await conn.execute(
                f"UPDATE ag_mcp_server SET {', '.join(assignments)} WHERE id=%s AND workspace_id=%s RETURNING *",
                tuple(params),
            )
            updated = await result.fetchone()
    response.headers["ETag"] = _etag(int(updated["version"]))
    return _server_summary(updated)


@router.post("/{server_id}/start")
async def start_mcp_server(server_id: str, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    _require_manage(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await _get_server(conn, server_id, actor.workspace_id, for_update=True)
            try:
                _validate_protocol(row["protocol_version"])
                validate_transport_target(row["transport"], row["endpoint_or_command"], resolve=row["transport"] != "stdio")
            except ValueError as exc:
                raise _http_validation_error(exc) from exc
            result = await conn.execute(
                "UPDATE ag_mcp_server SET status='enabled',version=version+1,updated_at=now() WHERE id=%s AND workspace_id=%s RETURNING *",
                (server_id, actor.workspace_id),
            )
            updated = await result.fetchone()
    return _server_summary(updated)


@router.post("/{server_id}/stop")
async def stop_mcp_server(server_id: str, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    _require_manage(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            "UPDATE ag_mcp_server SET status='disabled',version=version+1,updated_at=now() WHERE id=%s AND workspace_id=%s AND status <> 'deleted' RETURNING *",
            (server_id, actor.workspace_id),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="MCP server not found")
        await conn.commit()
    return _server_summary(row)


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp_server(
    server_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    _require_manage(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await _get_server(conn, server_id, actor.workspace_id, for_update=True)
            _assert_if_match(if_match, int(row.get("version", 1)))
            result = await conn.execute(
                "UPDATE ag_mcp_server SET status='deleted',version=version+1,updated_at=now() WHERE id=%s AND workspace_id=%s RETURNING id",
                (server_id, actor.workspace_id),
            )
            if not await result.fetchone():
                raise HTTPException(status_code=404, detail="MCP server not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{server_id}/tests")
async def test_mcp_server(
    server_id: str,
    body: McpServerTestRequest,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    _require_manage(actor)
    checked_at = datetime.now(UTC)
    try:
        async with pool.connection() as conn:
            row = await _get_server(conn, server_id, actor.workspace_id)
            validate_transport_target(row["transport"], row["endpoint_or_command"], resolve=body.resolve_dns)
            result = await conn.execute(
                "UPDATE ag_mcp_server SET last_test=%s::jsonb,last_tested_at=now(),updated_at=now() WHERE id=%s AND workspace_id=%s RETURNING *",
                (
                    json_dumps(
                        {
                            "status": "passed",
                            "transport_validated": True,
                            "network_connection": False,
                            "checked_at": checked_at.isoformat(),
                            "untrusted_source": True,
                        }
                    ),
                    server_id,
                    actor.workspace_id,
                ),
            )
            updated = await result.fetchone()
            await conn.commit()
    except ValueError as exc:
        raise _http_validation_error(exc) from exc
    return _server_summary(updated)


@router.post("/{server_id}/discoveries")
async def discover_mcp_server_capabilities(
    server_id: str,
    body: McpDiscoveryRequest,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    _require_manage(actor)
    try:
        protocol = _validate_protocol(body.protocol_version)
        roots = _normalize_roots(body.roots)
        capabilities, schema_hash = normalize_capability_snapshot(
            tools=body.tools,
            resources=body.resources,
            prompts=body.prompts,
            server_capabilities=body.server_capabilities,
        )
    except ValueError as exc:
        raise _http_validation_error(exc) from exc
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await _get_server(conn, server_id, actor.workspace_id, for_update=True)
            result = await conn.execute(
                """
                UPDATE ag_mcp_server SET protocol_version=%s,server_identity=%s::jsonb,
                    capabilities=%s::jsonb,schema_hash=%s,roots=%s::jsonb,
                    version=version+1,updated_at=now() WHERE id=%s AND workspace_id=%s RETURNING *
                """,
                (
                    protocol,
                    json_dumps(body.server_identity),
                    json_dumps(capabilities),
                    schema_hash,
                    json_dumps(roots),
                    server_id,
                    actor.workspace_id,
                ),
            )
            updated = await result.fetchone()
    return {
        "server": _server_summary(updated),
        "capability_snapshot": capabilities,
        "schema_hash": schema_hash,
        "approval_invalidated": int(updated["version"]) > int(row["version"]),
    }


@router.get("/{server_id}/capabilities")
async def get_mcp_server_capabilities(server_id: str, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    _require_read(actor)
    async with pool.connection() as conn:
        row = await _get_server(conn, server_id, actor.workspace_id)
    return {
        "server_id": row["id"],
        "workspace_id": row["workspace_id"],
        "schema_hash": row.get("schema_hash", ""),
        "capabilities": _json_object(row.get("capabilities"), {}),
        "roots": _json_object(row.get("roots"), []),
        "untrusted_source": True,
        "risk_policy": "workama_policy_v1",
    }


@router.post("/{server_id}/rpc")
async def mcp_server_rpc(
    server_id: str,
    request: Request,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """Bridge the small, synchronous JSON-RPC MCP surface for one tenant server."""

    _require_read(actor)
    try:
        message = await request.json()
    except (ValueError, json.JSONDecodeError):
        from workama_platform.modules.mcp_protocol import jsonrpc_error_response

        return jsonrpc_error_response(None, -32700, "Parse error")
    async with pool.connection() as conn:
        row = await _get_server(conn, server_id, actor.workspace_id)
    from workama_platform.modules.mcp_protocol import bridge_jsonrpc

    return await bridge_jsonrpc(row, message)


@router.get("/{server_id}/catalog")
async def list_mcp_server_catalog(server_id: str, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    return await get_mcp_server_capabilities(server_id, actor)


@router.get("/{server_id}/oauth/metadata")
async def get_mcp_server_oauth_metadata(server_id: str, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    _require_read(actor)
    async with pool.connection() as conn:
        row = await _get_server(conn, server_id, actor.workspace_id)
    return {**oauth_metadata_placeholder(server_id), "auth_type": row["auth_type"]}


@router.post("/{server_id}/authorizations")
async def start_mcp_server_authorization(
    server_id: str,
    body: McpAuthorizationStart,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    _require_manage(actor)
    async with pool.connection() as conn:
        row = await _get_server(conn, server_id, actor.workspace_id)
    if row["auth_type"] != "oauth":
        raise HTTPException(status_code=409, detail="MCP server is not configured for OAuth")
    scopes = sorted(set(body.scopes))
    unsupported_scopes = sorted(set(scopes) - MCP_OAUTH_SCOPES)
    if unsupported_scopes:
        raise HTTPException(status_code=422, detail=f"Unsupported MCP OAuth scopes: {unsupported_scopes}")
    state = new_oauth_state()
    code_verifier = new_pkce_verifier()
    code_challenge = pkce_challenge(code_verifier)
    payload = {
        "server_id": server_id,
        "workspace_id": actor.workspace_id,
        "org_id": actor.org_id,
        "actor_id": actor.user_id,
        "state": state,
        "code_verifier": code_verifier,
        "code_challenge": code_challenge,
        "scopes": scopes,
        "issued_at": datetime.now(UTC).timestamp(),
    }
    stored = await redis.set(
        f"mcp:oauth:state:{state}",
        json_dumps(payload),
        ex=600,
        nx=True,
    )
    if not stored:
        raise HTTPException(status_code=503, detail="Unable to reserve MCP OAuth state")
    return {
        "status": "pending_external",
        "server_id": server_id,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "scopes": scopes,
        "expires_in": 600,
        "provider_execution": "pending_external_exchange",
        "callback_endpoint": "/api/v1/mcp-servers/oauth/callback",
        "metadata": {**oauth_metadata_placeholder(server_id), "state_reserved": True},
        "authorization_url": None,
        "credential_upload_supported": False,
    }


@router.get("/oauth/callback")
async def complete_mcp_server_authorization(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    if not state:
        raise HTTPException(status_code=400, detail="MCP OAuth callback state is required")
    raw_state = await redis.getdel(f"mcp:oauth:state:{state}")
    if not raw_state:
        raise HTTPException(status_code=400, detail="MCP OAuth state is invalid or expired")
    try:
        state_payload = json.loads(raw_state)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="MCP OAuth state is invalid or expired") from exc
    if not isinstance(state_payload, dict) or not secrets.compare_digest(str(state_payload.get("state", "")), state):
        raise HTTPException(status_code=400, detail="MCP OAuth state is invalid or expired")
    server_id = str(state_payload.get("server_id", "")) or None
    if error:
        return {
            "status": "rejected",
            "configured": False,
            "server_id": server_id,
            "provider_execution": "rejected_external",
            "code_received": False,
            "state_received": True,
            "error_received": True,
            "credential_persisted": False,
        }
    if not code:
        raise HTTPException(status_code=400, detail="MCP OAuth callback code is required")
    return {
        "status": "pending_external_exchange",
        "configured": False,
        "server_id": server_id,
        "provider_execution": "pending_external_exchange",
        "code_received": True,
        "state_received": True,
        "error_received": False,
        "credential_persisted": False,
    }
