"""Main CLI module — argparse-based command dispatcher for ``workama`` v2.

Usage::

    python -m apps.cli.workama_cli login --email ... --password ... --url ...
    python -m apps.cli.workama_cli whoami
    python -m apps.cli.workama_cli workspaces list
    python -m apps.cli.workama_cli free-providers list --json

Configuration is stored at ``~/.workama/credentials`` (JSON). Environment
variables ``WORKAMA_API_URL``, ``WORKAMA_API_TOKEN`` and
``WORKAMA_WORKSPACE_ID`` override the config file.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Iterable, Sequence

from . import __version__
from .client import (
    ApiError,
    NetworkError,
    NotLoggedInError,
    WorkamaClient,
)
from .config import Config, ConfigError


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CliError(Exception):
    """Raised for expected CLI-level errors (exit code 1)."""


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _print_json(data: Any) -> None:
    json.dump(data, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _print_table(rows: Sequence[dict[str, Any]], columns: Sequence[str], *, headers: Sequence[str] | None = None) -> None:
    """Print a simple human-readable table without third-party deps."""
    if not rows:
        print("(no rows)")
        return
    headers = list(headers) if headers else list(columns)
    widths = [len(h) for h in headers]
    str_rows: list[list[str]] = []
    for row in rows:
        cells = []
        for col in columns:
            value = row.get(col, "")
            if value is None:
                value = ""
            cells.append(str(value))
        str_rows.append(cells)
        for i, cell in enumerate(cells):
            widths[i] = max(widths[i], len(cell))
    # header
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for cells in str_rows:
        print("  ".join(cells[i].ljust(widths[i]) for i in range(len(headers))))


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    """Normalize list-style API responses into a list of dicts."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("items", "data", "results", "workspaces", "assistants", "workflows", "knowledge_bases", "devices", "plans", "tools", "providers", "events"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        # If the dict itself looks like a single resource, wrap it.
        if any(k in payload for k in ("id", "name")):
            return [payload]
    return []


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


def _build_client(args: argparse.Namespace, config: Config, *, require_token: bool = True) -> WorkamaClient:
    base_url = getattr(args, "api_url", None) or config.base_url
    token = getattr(args, "api_token", None) or config.token
    workspace_id = getattr(args, "workspace_id", None) or config.workspace_id
    if require_token and not token:
        raise CliError("Not logged in; run `workama login` first.")
    return WorkamaClient(base_url, token=token, workspace_id=workspace_id, timeout=getattr(args, "timeout", 30.0))


def _load_config(args: argparse.Namespace) -> Config:
    config_dir = getattr(args, "config_dir", None)
    return Config(config_dir=config_dir)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def cmd_version(args: argparse.Namespace, config: Config) -> int:
    data = {"name": "workama", "version": __version__}
    if args.json:
        _print_json(data)
    else:
        print(f"workama {__version__}")
    return 0


def cmd_login(args: argparse.Namespace, config: Config) -> int:
    base_url = args.api_url or config.base_url
    client = WorkamaClient(base_url, timeout=args.timeout)
    try:
        result = client.login(args.email, args.password)
    finally:
        client.close()
    token = result.get("access_token") if isinstance(result, dict) else None
    if not token:
        raise CliError("Login response did not contain access_token")
    workspace_id = None
    user = result.get("user") if isinstance(result, dict) else None
    if isinstance(user, dict) and user.get("workspace_id"):
        workspace_id = str(user["workspace_id"])
    config.save(base_url=base_url, token=token, workspace_id=workspace_id)
    output = {
        "base_url": base_url,
        "user": user or {},
        "token_type": result.get("token_type", "bearer") if isinstance(result, dict) else "bearer",
        "workspace_id": workspace_id,
    }
    if args.json:
        # Never print the raw token.
        output["token"] = "<saved>"
        _print_json(output)
    else:
        email = (user or {}).get("email", args.email)
        print(f"Logged in as {email}.")
        print(f"Token saved to {config.path}")
        if workspace_id:
            print(f"Active workspace: {workspace_id}")
    return 0


def cmd_whoami(args: argparse.Namespace, config: Config) -> int:
    client = _build_client(args, config)
    try:
        user = client.me()
    finally:
        client.close()
    if args.json:
        _print_json(user)
    else:
        if not isinstance(user, dict):
            print(user)
            return 0
        print(f"id:       {user.get('id', '')}")
        print(f"email:    {user.get('email', '')}")
        print(f"name:     {user.get('display_name') or user.get('name', '')}")
        print(f"workspace:{user.get('workspace_id', '')}")
    return 0


def cmd_workspaces_list(args: argparse.Namespace, config: Config) -> int:
    client = _build_client(args, config)
    try:
        payload = client.list_workspaces()
    finally:
        client.close()
    rows = _extract_items(payload)
    if args.json:
        _print_json(payload if isinstance(payload, (dict, list)) else {"items": rows})
        return 0
    _print_table(rows, ("id", "name", "slug", "status"))
    return 0


def cmd_workspaces_create(args: argparse.Namespace, config: Config) -> int:
    client = _build_client(args, config)
    try:
        payload = client.create_workspace(args.name, slug=args.slug)
    finally:
        client.close()
    if args.json:
        _print_json(payload)
    else:
        ws = payload if isinstance(payload, dict) else {}
        print(f"Created workspace {ws.get('id', '?')} ({ws.get('name', args.name)})")
    return 0


def cmd_assistants_list(args: argparse.Namespace, config: Config) -> int:
    client = _build_client(args, config)
    try:
        payload = client.list_assistants()
    finally:
        client.close()
    rows = _extract_items(payload)
    if args.json:
        _print_json(payload if isinstance(payload, (dict, list)) else {"items": rows})
        return 0
    _print_table(rows, ("id", "name", "model", "status"))
    return 0


def cmd_assistants_create(args: argparse.Namespace, config: Config) -> int:
    client = _build_client(args, config)
    try:
        payload = client.create_assistant(args.name, model=args.model, system_prompt=args.system_prompt)
    finally:
        client.close()
    if args.json:
        _print_json(payload)
    else:
        asst = payload if isinstance(payload, dict) else {}
        print(f"Created assistant {asst.get('id', '?')} ({asst.get('name', args.name)})")
    return 0


def cmd_assistants_run(args: argparse.Namespace, config: Config) -> int:
    client = _build_client(args, config)
    try:
        payload = client.run_assistant(args.assistant_id, message=args.message)
    finally:
        client.close()
    if args.json:
        _print_json(payload)
    else:
        if isinstance(payload, dict):
            print(payload.get("output") or payload.get("content") or payload.get("text") or json.dumps(payload, ensure_ascii=False))
        else:
            print(payload)
    return 0


def cmd_workflows_list(args: argparse.Namespace, config: Config) -> int:
    client = _build_client(args, config)
    try:
        payload = client.list_workflows()
    finally:
        client.close()
    rows = _extract_items(payload)
    if args.json:
        _print_json(payload if isinstance(payload, (dict, list)) else {"items": rows})
        return 0
    _print_table(rows, ("id", "name", "status", "version"))
    return 0


def cmd_workflows_run(args: argparse.Namespace, config: Config) -> int:
    input_data: dict[str, Any] = {}
    if args.input:
        try:
            input_data = json.loads(args.input)
        except json.JSONDecodeError as exc:
            raise CliError(f"--input must be a JSON object: {exc}") from exc
    client = _build_client(args, config)
    try:
        payload = client.run_workflow(args.workflow_id, input_data=input_data)
    finally:
        client.close()
    if args.json:
        _print_json(payload)
    else:
        run = payload if isinstance(payload, dict) else {}
        print(f"Workflow run {run.get('id', '?')} status={run.get('status', '?')}")
    return 0


def cmd_knowledge_bases_list(args: argparse.Namespace, config: Config) -> int:
    client = _build_client(args, config)
    try:
        payload = client.list_knowledge_bases()
    finally:
        client.close()
    rows = _extract_items(payload)
    if args.json:
        _print_json(payload if isinstance(payload, (dict, list)) else {"items": rows})
        return 0
    _print_table(rows, ("id", "name", "status", "document_count"))
    return 0


def cmd_knowledge_bases_create(args: argparse.Namespace, config: Config) -> int:
    client = _build_client(args, config)
    try:
        payload = client.create_knowledge_base(args.name, description=args.description)
    finally:
        client.close()
    if args.json:
        _print_json(payload)
    else:
        kb = payload if isinstance(payload, dict) else {}
        print(f"Created knowledge base {kb.get('id', '?')} ({kb.get('name', args.name)})")
    return 0


def cmd_knowledge_bases_upload(args: argparse.Namespace, config: Config) -> int:
    client = _build_client(args, config)
    try:
        payload = client.upload_document(args.kb_id, args.file)
    finally:
        client.close()
    if args.json:
        _print_json(payload)
    else:
        doc = payload if isinstance(payload, dict) else {}
        print(f"Uploaded {args.file} -> document {doc.get('id', '?')} (status={doc.get('status', '?')})")
    return 0


def cmd_knowledge_bases_query(args: argparse.Namespace, config: Config) -> int:
    client = _build_client(args, config)
    try:
        payload = client.rag_query(args.kb_id, query=args.query, top_k=args.top_k)
    finally:
        client.close()
    if args.json:
        _print_json(payload)
    else:
        results = _extract_items(payload)
        if not results:
            print("(no results)")
        else:
            _print_table(results, ("document_id", "content", "score"))
    return 0


def cmd_devices_list(args: argparse.Namespace, config: Config) -> int:
    client = _build_client(args, config)
    try:
        payload = client.list_devices()
    finally:
        client.close()
    rows = _extract_items(payload)
    if args.json:
        _print_json(payload if isinstance(payload, (dict, list)) else {"items": rows})
        return 0
    _print_table(rows, ("id", "name", "type", "status", "last_seen"))
    return 0


def cmd_devices_register(args: argparse.Namespace, config: Config) -> int:
    client = _build_client(args, config)
    try:
        payload = client.register_device(args.name, device_type=args.type, metadata=None)
    finally:
        client.close()
    if args.json:
        _print_json(payload)
    else:
        dev = payload if isinstance(payload, dict) else {}
        print(f"Registered device {dev.get('id', '?')} ({dev.get('name', args.name)})")
    return 0


def cmd_billing_plans(args: argparse.Namespace, config: Config) -> int:
    client = _build_client(args, config)
    try:
        payload = client.list_billing_plans()
    finally:
        client.close()
    rows = _extract_items(payload)
    if args.json:
        _print_json(payload if isinstance(payload, (dict, list)) else {"items": rows})
        return 0
    _print_table(rows, ("id", "name", "price", "currency", "interval"))
    return 0


def cmd_billing_usage(args: argparse.Namespace, config: Config) -> int:
    client = _build_client(args, config)
    try:
        payload = client.list_billing_usage()
    finally:
        client.close()
    if args.json:
        _print_json(payload)
    else:
        rows = _extract_items(payload)
        _print_table(rows, ("id", "metric", "quantity", "period"))
    return 0


def cmd_mcp_tools(args: argparse.Namespace, config: Config) -> int:
    client = _build_client(args, config)
    try:
        payload = client.list_mcp_tools()
    finally:
        client.close()
    rows = _extract_items(payload)
    if args.json:
        _print_json(payload if isinstance(payload, (dict, list)) else {"items": rows})
        return 0
    _print_table(rows, ("id", "name", "description", "enabled"))
    return 0


def cmd_mcp_invoke(args: argparse.Namespace, config: Config) -> int:
    arguments: dict[str, Any] = {}
    if args.arguments:
        try:
            arguments = json.loads(args.arguments)
        except json.JSONDecodeError as exc:
            raise CliError(f"--arguments must be a JSON object: {exc}") from exc
    client = _build_client(args, config)
    try:
        payload = client.invoke_mcp_tool(args.tool_id, arguments=arguments)
    finally:
        client.close()
    if args.json:
        _print_json(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_free_providers_list(args: argparse.Namespace, config: Config) -> int:
    client = _build_client(args, config)
    try:
        payload = client.list_free_providers()
    finally:
        client.close()
    rows = _extract_items(payload)
    if args.json:
        _print_json(payload if isinstance(payload, (dict, list)) else {"items": rows})
        return 0
    _print_table(rows, ("key", "name", "enabled", "category"))
    return 0


def cmd_free_providers_enable(args: argparse.Namespace, config: Config) -> int:
    client = _build_client(args, config)
    try:
        payload = client.enable_free_provider(args.provider_key)
    finally:
        client.close()
    if args.json:
        _print_json(payload)
    else:
        print(f"Enabled free provider {args.provider_key}")
    return 0


def cmd_audit_logs_list(args: argparse.Namespace, config: Config) -> int:
    client = _build_client(args, config)
    try:
        payload = client.list_audit_logs(limit=args.limit, offset=args.offset)
    finally:
        client.close()
    rows = _extract_items(payload)
    if args.json:
        _print_json(payload if isinstance(payload, (dict, list)) else {"items": rows})
        return 0
    _print_table(rows, ("id", "action", "actor_id", "created_at"))
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _add_global_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Output raw JSON instead of human-readable tables")
    parser.add_argument("--api-url", help="Override the platform API base URL")
    parser.add_argument("--api-token", help="Override the access token")
    parser.add_argument("--workspace-id", help="Override the active workspace id")
    parser.add_argument("--config-dir", help="Override the config directory (default: ~/.workama)")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workama",
        description="WorkAMA CLI v2 — manage workspaces, assistants, workflows, knowledge bases, devices, billing, MCP and more.",
    )
    parser.add_argument("--version", action="version", version=f"workama {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # version
    p_version = subparsers.add_parser("version", help="Show CLI version")
    _add_global_options(p_version)
    p_version.set_defaults(handler=cmd_version)

    # login
    p_login = subparsers.add_parser("login", help="Log in and save credentials")
    p_login.add_argument("--email", required=True)
    p_login.add_argument("--password", required=True)
    p_login.add_argument("--url", dest="api_url", help="Platform API base URL")
    _add_global_options(p_login)
    p_login.set_defaults(handler=cmd_login)

    # whoami
    p_whoami = subparsers.add_parser("whoami", help="Show the currently logged-in user")
    _add_global_options(p_whoami)
    p_whoami.set_defaults(handler=cmd_whoami)

    # workspaces
    p_workspaces = subparsers.add_parser("workspaces", help="Manage workspaces")
    ws_sub = p_workspaces.add_subparsers(dest="subcommand", required=True)
    p_ws_list = ws_sub.add_parser("list", help="List workspaces")
    _add_global_options(p_ws_list)
    p_ws_list.set_defaults(handler=cmd_workspaces_list)
    p_ws_create = ws_sub.add_parser("create", help="Create a workspace")
    p_ws_create.add_argument("name")
    p_ws_create.add_argument("--slug")
    _add_global_options(p_ws_create)
    p_ws_create.set_defaults(handler=cmd_workspaces_create)

    # assistants
    p_assistants = subparsers.add_parser("assistants", help="Manage assistants")
    asst_sub = p_assistants.add_subparsers(dest="subcommand", required=True)
    p_asst_list = asst_sub.add_parser("list", help="List assistants")
    _add_global_options(p_asst_list)
    p_asst_list.set_defaults(handler=cmd_assistants_list)
    p_asst_create = asst_sub.add_parser("create", help="Create an assistant")
    p_asst_create.add_argument("name")
    p_asst_create.add_argument("--model")
    p_asst_create.add_argument("--system-prompt", dest="system_prompt")
    _add_global_options(p_asst_create)
    p_asst_create.set_defaults(handler=cmd_assistants_create)
    p_asst_run = asst_sub.add_parser("run", help="Run an assistant with a message")
    p_asst_run.add_argument("assistant_id")
    p_asst_run.add_argument("--message", required=True)
    _add_global_options(p_asst_run)
    p_asst_run.set_defaults(handler=cmd_assistants_run)

    # workflows
    p_workflows = subparsers.add_parser("workflows", help="Manage workflows")
    wf_sub = p_workflows.add_subparsers(dest="subcommand", required=True)
    p_wf_list = wf_sub.add_parser("list", help="List workflows")
    _add_global_options(p_wf_list)
    p_wf_list.set_defaults(handler=cmd_workflows_list)
    p_wf_run = wf_sub.add_parser("run", help="Run a workflow")
    p_wf_run.add_argument("workflow_id")
    p_wf_run.add_argument("--input", help="JSON object passed as workflow input")
    _add_global_options(p_wf_run)
    p_wf_run.set_defaults(handler=cmd_workflows_run)

    # knowledge-bases
    p_kb = subparsers.add_parser("knowledge-bases", help="Manage knowledge bases")
    kb_sub = p_kb.add_subparsers(dest="subcommand", required=True)
    p_kb_list = kb_sub.add_parser("list", help="List knowledge bases")
    _add_global_options(p_kb_list)
    p_kb_list.set_defaults(handler=cmd_knowledge_bases_list)
    p_kb_create = kb_sub.add_parser("create", help="Create a knowledge base")
    p_kb_create.add_argument("name")
    p_kb_create.add_argument("--description")
    _add_global_options(p_kb_create)
    p_kb_create.set_defaults(handler=cmd_knowledge_bases_create)
    p_kb_upload = kb_sub.add_parser("upload", help="Upload a document to a knowledge base")
    p_kb_upload.add_argument("kb_id")
    p_kb_upload.add_argument("file", help="Path to the file to upload")
    _add_global_options(p_kb_upload)
    p_kb_upload.set_defaults(handler=cmd_knowledge_bases_upload)
    p_kb_query = kb_sub.add_parser("query", help="Run a RAG query against a knowledge base")
    p_kb_query.add_argument("kb_id")
    p_kb_query.add_argument("--query", required=True)
    p_kb_query.add_argument("--top-k", type=int, default=5, dest="top_k")
    _add_global_options(p_kb_query)
    p_kb_query.set_defaults(handler=cmd_knowledge_bases_query)

    # devices
    p_devices = subparsers.add_parser("devices", help="Manage devices")
    dev_sub = p_devices.add_subparsers(dest="subcommand", required=True)
    p_dev_list = dev_sub.add_parser("list", help="List devices")
    _add_global_options(p_dev_list)
    p_dev_list.set_defaults(handler=cmd_devices_list)
    p_dev_register = dev_sub.add_parser("register", help="Register a device")
    p_dev_register.add_argument("name")
    p_dev_register.add_argument("--type", default="desktop", dest="type")
    _add_global_options(p_dev_register)
    p_dev_register.set_defaults(handler=cmd_devices_register)

    # billing
    p_billing = subparsers.add_parser("billing", help="View billing plans and usage")
    bill_sub = p_billing.add_subparsers(dest="subcommand", required=True)
    p_bill_plans = bill_sub.add_parser("plans", help="List billing plans")
    _add_global_options(p_bill_plans)
    p_bill_plans.set_defaults(handler=cmd_billing_plans)
    p_bill_usage = bill_sub.add_parser("usage", help="List billing usage records")
    _add_global_options(p_bill_usage)
    p_bill_usage.set_defaults(handler=cmd_billing_usage)

    # mcp
    p_mcp = subparsers.add_parser("mcp", help="Inspect and invoke MCP tools")
    mcp_sub = p_mcp.add_subparsers(dest="subcommand", required=True)
    p_mcp_tools = mcp_sub.add_parser("tools", help="List MCP tools")
    _add_global_options(p_mcp_tools)
    p_mcp_tools.set_defaults(handler=cmd_mcp_tools)
    p_mcp_invoke = mcp_sub.add_parser("invoke", help="Invoke an MCP tool")
    p_mcp_invoke.add_argument("tool_id")
    p_mcp_invoke.add_argument("--arguments", help="JSON object of tool arguments")
    _add_global_options(p_mcp_invoke)
    p_mcp_invoke.set_defaults(handler=cmd_mcp_invoke)

    # free-providers
    p_fp = subparsers.add_parser("free-providers", help="List and enable free LLM providers")
    fp_sub = p_fp.add_subparsers(dest="subcommand", required=True)
    p_fp_list = fp_sub.add_parser("list", help="List free providers")
    _add_global_options(p_fp_list)
    p_fp_list.set_defaults(handler=cmd_free_providers_list)
    p_fp_enable = fp_sub.add_parser("enable", help="Enable a free provider")
    p_fp_enable.add_argument("provider_key")
    _add_global_options(p_fp_enable)
    p_fp_enable.set_defaults(handler=cmd_free_providers_enable)

    # audit-logs
    p_audit = subparsers.add_parser("audit-logs", help="View audit logs")
    p_audit_list = p_audit.add_subparsers(dest="subcommand", required=True).add_parser("list", help="List audit logs")
    p_audit_list.add_argument("--limit", type=int, default=50)
    p_audit_list.add_argument("--offset", type=int, default=0)
    _add_global_options(p_audit_list)
    p_audit_list.set_defaults(handler=cmd_audit_logs_list)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = _load_config(args)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return 1
    try:
        return handler(args, config)
    except CliError as exc:
        print(f"workama: {exc}", file=sys.stderr)
        return 1
    except NotLoggedInError as exc:
        print(f"workama: {exc}", file=sys.stderr)
        return 1
    except ApiError as exc:
        print(f"workama: API error: {exc}", file=sys.stderr)
        return 1
    except NetworkError as exc:
        print(f"workama: network error: {exc}", file=sys.stderr)
        return 1
    except ConfigError as exc:
        print(f"workama: config error: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"workama: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
