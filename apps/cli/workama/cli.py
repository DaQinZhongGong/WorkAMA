from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from typing import Any
from urllib.parse import quote, urlsplit

from . import __version__
from .client import AgentClient, ClientError, GatewayClient, PlatformClient
from .config import ConfigError, ConfigStore, SECRET_KEYS
from .transport import ApiError, TransportError, endpoint


class CliError(RuntimeError):
    pass


SENSITIVE_KEYS = {
    "api_key",
    "access_token",
    "refresh_token",
    "authorization",
    "password",
    "secret",
    "client_secret",
    "private_key",
    "credential",
    "token",
    "cookie",
    "set_cookie",
    "ws_ticket",
    "ticket",
}


def _json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    result = dict(headers)
    if "Authorization" in result:
        result["Authorization"] = "Bearer <redacted>"
    return result


def _redact_text(value: str) -> str:
    value = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer <redacted>", value)
    return re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization|ws[_-]?ticket|ticket)\s*([:=])\s*[^\s,;]+",
        r"\1\2<redacted>",
        value,
    )


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized == "authorization" and isinstance(item, str) and item.lower().startswith("bearer "):
                redacted[key] = _redact_text(item)
            elif normalized in SENSITIVE_KEYS or normalized.endswith("_token"):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _profile_store(args: argparse.Namespace) -> ConfigStore:
    store = ConfigStore()
    if getattr(args, "profile", None):
        store.use(args.profile) if args.command == "config" and args.config_action == "use" else None
    return store


def _get_value(store: ConfigStore, args: argparse.Namespace, key: str, flag: str | None = None) -> str | None:
    value = getattr(args, flag, None) if flag else None
    return value or store.get(key, getattr(args, "profile", None))


def _chat_request(args: argparse.Namespace, store: ConfigStore) -> tuple[GatewayClient, dict[str, Any], str]:
    prompt = args.prompt
    if prompt is None:
        if not sys.stdin.isatty():
            raise CliError("chat requires a prompt when stdin is not interactive")
        try:
            prompt = input("you> ").strip()
        except EOFError as exc:
            raise CliError("chat prompt is empty") from exc
    if not prompt:
        raise CliError("chat prompt is empty")
    gateway_url = _get_value(store, args, "gateway_url", "gateway_url") or "http://localhost:20202"
    api_key = _get_value(store, args, "api_key", "api_key")
    model = _get_value(store, args, "model", "model") or "workama-chat"
    client = GatewayClient(gateway_url, api_key, timeout=args.timeout)
    payload = client.payload(
        prompt,
        model=model,
        stream=args.stream,
        system=args.system,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    )
    return client, payload, prompt


def command_chat(args: argparse.Namespace, store: ConfigStore) -> int:
    client, payload, _ = _chat_request(args, store)
    headers = _redact_headers({"Authorization": f"Bearer {client.api_key}"}) if client.api_key else {}
    request = {
        "method": "POST",
        "url": endpoint(client.base_url, "/v1/chat/completions"),
        "headers": headers,
        "body": payload,
    }
    if args.dry_run:
        _json_print({"dry_run": True, "request": request}) if args.json else _json_print(request)
        return 0
    if args.stream:
        chunks = []
        for event in client.stream_chat(payload):
            chunks.append(event)
            text = _content_delta(event)
            if text and not args.json:
                print(text, end="", flush=True)
        if not args.json:
            print()
        else:
            _json_print({"stream": True, "events": chunks, "text": "".join(_content_delta(item) for item in chunks)})
        return 0
    result = client.chat(payload)
    if args.json:
        _json_print(result)
    else:
        print(_content_message(result) or json.dumps(result, ensure_ascii=False))
    return 0


def command_chat_list(args: argparse.Namespace, store: ConfigStore) -> int:
    platform_url = _get_value(store, args, "platform_url", "platform_url") or "http://localhost:20200"
    access_token = _get_value(store, args, "access_token", "access_token")
    platform = PlatformClient(platform_url, access_token, timeout=args.timeout)
    result = _redact_sensitive(platform.list_sessions())
    if args.json:
        _json_print(result)
        return 0
    for item in result.get("items", []):
        print(f"{item.get('id')}\t{item.get('status', 'unknown')}\t{item.get('title', '')}")
    return 0


def command_chat_resume(args: argparse.Namespace, store: ConfigStore) -> int:
    platform_url = _get_value(store, args, "platform_url", "platform_url") or "http://localhost:20200"
    access_token = _get_value(store, args, "access_token", "access_token")
    platform = PlatformClient(platform_url, access_token, timeout=args.timeout)
    session_id = args.resume
    result = _redact_sensitive(platform.list_events(session_id, after=args.after))
    if args.json:
        _json_print(result)
        return 0
    printed = False
    for event in result.get("items", []):
        payload = event.get("payload") or {}
        event_type = event.get("type")
        if event_type == "agent.message.completed":
            content = payload.get("content")
            if isinstance(content, str):
                print(content)
                printed = True
        elif event_type == "agent.message.delta":
            delta = payload.get("delta")
            if isinstance(delta, str):
                print(delta, end="")
                printed = True
    if printed:
        print()
    return 0


def _content_delta(event: dict[str, Any]) -> str:
    choices = event.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    choice = choices[0]
    delta = choice.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("content"), str):
        return delta["content"]
    message = choice.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    return ""


def _content_message(result: dict[str, Any]) -> str:
    choices = result.get("choices") or []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def command_run(args: argparse.Namespace, store: ConfigStore) -> int:
    if not args.prompt:
        raise CliError("run requires a prompt")
    platform_url = _get_value(store, args, "platform_url", "platform_url") or "http://localhost:20200"
    ws_url = _get_value(store, args, "agent_ws_url", "agent_ws_url") or "ws://localhost:20201"
    access_token = _get_value(store, args, "access_token", "access_token")
    model = _get_value(store, args, "model", "model") or "workama-chat"
    session_id = args.session_id
    session_body = {
        "title": args.title or args.prompt[:120],
        "model": model,
        "agent_kind": "ama_chat",
        "model_config": {"temperature": 0.7},
        "max_steps": args.max_steps,
        "max_credits": args.max_credits,
        "max_duration_seconds": args.max_duration,
    }
    plan = {
        "dry_run": True,
        "session": {
            "method": "POST",
            "url": endpoint(platform_url, "/api/v1/sessions"),
            "headers": _redact_headers({"Authorization": f"Bearer {access_token}"}) if access_token else {},
            "body": session_body,
        },
        "ticket": {
            "method": "POST",
            "url": endpoint(platform_url, "/api/v1/sessions/<session-id>/ws-tickets"),
        },
        "message": {"type": "message.create", "content": args.prompt},
        "agent_ws_url": f"{ws_url.rstrip('/')}/ws/sessions/<session-id>?ticket=<one-time-ticket>&after=0",
    }
    if args.dry_run:
        _json_print(plan)
        return 0
    platform = PlatformClient(platform_url, access_token, timeout=args.timeout)
    rendered = False

    def on_event(event: dict[str, Any]) -> None:
        nonlocal rendered
        if args.json:
            return
        if event.get("type") == "agent.message.delta":
            payload = event.get("payload") or {}
            delta = payload.get("delta")
            if isinstance(delta, str):
                print(delta, end="", flush=True)
                rendered = True

    result = AgentClient(platform, ws_url, timeout=args.timeout).run(
        args.prompt,
        model=model,
        session_id=session_id,
        title=args.title,
        on_event=on_event,
    )
    if args.json:
        _json_print(result)
    elif not rendered:
        print(result.get("text", ""))
    else:
        print()
    return 0


def _code_context(args: argparse.Namespace, store: ConfigStore) -> tuple[str, str | None]:
    platform_url = _get_value(store, args, "platform_url", "platform_url") or "http://localhost:20200"
    parsed = urlsplit(platform_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CliError("code API base URL must be an absolute http or https URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CliError("code API base URL must not contain credentials, query parameters, or fragments")
    access_token = _get_value(store, args, "access_token", "access_token")
    return platform_url, access_token


def _code_task_plan(args: argparse.Namespace, store: ConfigStore) -> tuple[PlatformClient, dict[str, Any]]:
    platform_url, access_token = _code_context(args, store)
    title = args.title or args.prompt[:160]
    body: dict[str, Any] = {"title": title, "prompt": args.prompt, "branch": args.branch}
    if args.repository_id:
        body["repository_id"] = args.repository_id
    if args.session_id:
        body["session_id"] = args.session_id
    plan = {
        "dry_run": True,
        "request": {
            "method": "POST",
            "url": endpoint(platform_url, "/api/v1/code/tasks"),
            "headers": _redact_headers({"Authorization": f"Bearer {access_token}"}) if access_token else {},
            "body": body,
        },
    }
    return PlatformClient(platform_url, access_token, timeout=args.timeout), _redact_sensitive(plan)


def command_code_task(args: argparse.Namespace, store: ConfigStore) -> int:
    platform, plan = _code_task_plan(args, store)
    if args.dry_run:
        _json_print(plan)
        return 0
    result = _redact_sensitive(
        platform.create_code_task(
            title=args.title or args.prompt[:160],
            prompt=args.prompt,
            branch=args.branch,
            repository_id=args.repository_id,
            session_id=args.session_id,
        )
    )
    _json_print(result) if args.json else print(f"Created code task {result['id']}: {result.get('title', '')}")
    return 0


def command_code_list(args: argparse.Namespace, store: ConfigStore) -> int:
    platform_url, access_token = _code_context(args, store)
    if args.limit < 1:
        raise CliError("code list --limit must be at least 1")
    platform = PlatformClient(platform_url, access_token, timeout=args.timeout)
    query = f"?limit={args.limit}" + (f"&status={quote(args.status, safe='')}" if args.status else "")
    request = {
        "dry_run": True,
        "request": {
            "method": "GET",
            "url": endpoint(platform_url, f"/api/v1/code/tasks{query}"),
            "headers": _redact_headers({"Authorization": f"Bearer {access_token}"}) if access_token else {},
        },
    }
    if args.dry_run:
        _json_print(request)
        return 0
    result = _redact_sensitive(platform.list_code_tasks(status=args.status, limit=args.limit))
    _json_print(result) if args.json else print("\n".join(f"{item.get('id')}\t{item.get('status')}\t{item.get('title')}" for item in result["items"]))
    return 0


def command_code_event(args: argparse.Namespace, store: ConfigStore) -> int:
    platform_url, access_token = _code_context(args, store)
    if args.after < 0 or args.limit < 1:
        raise CliError("code event requires --after >= 0 and --limit >= 1")
    platform = PlatformClient(platform_url, access_token, timeout=args.timeout)
    encoded_task_id = quote(args.task_id, safe="")
    request = {
        "dry_run": True,
        "request": {
            "method": "GET",
            "url": endpoint(platform_url, f"/api/v1/code/tasks/{encoded_task_id}/events?after={args.after}&limit={args.limit}"),
            "headers": _redact_headers({"Authorization": f"Bearer {access_token}"}) if access_token else {},
        },
    }
    if args.dry_run:
        _json_print(request)
        return 0
    result = _redact_sensitive(platform.list_code_events(args.task_id, after=args.after, limit=args.limit))
    _json_print(result) if args.json else print("\n".join(f"{item.get('seq')}\t{item.get('type')}\t{item.get('id')}" for item in result["items"]))
    return 0


def command_code_status(args: argparse.Namespace, store: ConfigStore) -> int:
    platform_url, access_token = _code_context(args, store)
    platform = PlatformClient(platform_url, access_token, timeout=args.timeout)
    if args.task_id:
        encoded_task_id = quote(args.task_id, safe="")
        request = {
            "dry_run": True,
            "request": {
                "method": "GET",
                "url": endpoint(platform_url, f"/api/v1/code/tasks/{encoded_task_id}"),
                "headers": _redact_headers({"Authorization": f"Bearer {access_token}"}) if access_token else {},
            },
        }
        if args.dry_run:
            _json_print(request)
            return 0
        task = _redact_sensitive(platform.get_code_task(args.task_id))
        repository: dict[str, Any] | None = None
        if task.get("repository_id"):
            try:
                repository = _redact_sensitive(platform.get_code_repository(task["repository_id"]))
            except (ApiError, TransportError, ClientError):
                repository = None
        recent_events = _redact_sensitive(platform.list_code_events(args.task_id, limit=5))
        if args.json:
            _json_print({"task": task, "repository": repository, "recent_events": recent_events.get("items", [])})
            return 0
        print(f"{task.get('id')}\t{task.get('status', 'unknown')}\t{task.get('title', '')}")
        if repository:
            print(f"Repository: {repository.get('name')} ({repository.get('provider')})")
        for event in recent_events.get("items", []):
            print(f"  {event.get('seq')}\t{event.get('type')}\t{event.get('id')}")
        return 0
    if args.dry_run:
        _json_print({
            "dry_run": True,
            "request": {
                "method": "GET",
                "url": endpoint(platform_url, "/api/v1/code/tasks?limit=5"),
                "headers": _redact_headers({"Authorization": f"Bearer {access_token}"}) if access_token else {},
            },
        })
        return 0
    result = _redact_sensitive(platform.list_code_tasks(limit=5))
    if args.json:
        _json_print(result)
    else:
        print("Recent code tasks:")
        for item in result.get("items", []):
            print(f"{item.get('id')}\t{item.get('status', 'unknown')}\t{item.get('title', '')}")
    return 0


def command_code_artifact(args: argparse.Namespace, store: ConfigStore) -> int:
    platform_url, access_token = _code_context(args, store)
    platform = PlatformClient(platform_url, access_token, timeout=args.timeout)
    if args.download:
        artifact_id = args.download
        encoded_artifact_id = quote(artifact_id, safe="")
        if args.dry_run:
            _json_print({
                "dry_run": True,
                "request": {
                    "method": "GET",
                    "url": endpoint(platform_url, f"/api/v1/artifacts/{encoded_artifact_id}/download"),
                    "headers": _redact_headers({"Authorization": f"Bearer {access_token}"}) if access_token else {},
                },
            })
            return 0
        content = platform.download_artifact(artifact_id)
        if args.output:
            from pathlib import Path
            Path(args.output).write_bytes(content)
            print(f"Downloaded artifact {artifact_id} to {args.output}")
        else:
            sys.stdout.buffer.write(content)
        return 0
    if not args.task_id:
        raise CliError("code artifact requires --task TASK_ID or --download ARTIFACT_ID")
    encoded_task_id = quote(args.task_id, safe="")
    if args.dry_run:
        _json_print({
            "dry_run": True,
            "request": {
                "method": "GET",
                "url": endpoint(platform_url, f"/api/v1/code/tasks/{encoded_task_id}"),
                "headers": _redact_headers({"Authorization": f"Bearer {access_token}"}) if access_token else {},
            },
        })
        return 0
    task = platform.get_code_task(args.task_id)
    session_id = task.get("session_id")
    if not session_id:
        raise CliError(f"Code task {args.task_id} has no associated session")
    result = _redact_sensitive(platform.list_session_artifacts(session_id))
    if args.json:
        _json_print(result)
    else:
        print(f"Artifacts for task {args.task_id}:")
        for item in result.get("items", []):
            print(f"{item.get('id')}\t{item.get('kind', 'file')}\t{item.get('name', '')}")
    return 0


def command_event_tail(args: argparse.Namespace, store: ConfigStore) -> int:
    platform_url = _get_value(store, args, "platform_url", "platform_url") or "http://localhost:20200"
    access_token = _get_value(store, args, "access_token", "access_token")
    if not access_token:
        raise CliError("An access token is required to tail events")
    platform = PlatformClient(platform_url, access_token, timeout=args.timeout)
    session_id = args.session
    after = args.after
    if args.dry_run:
        encoded_session_id = quote(session_id, safe="")
        _json_print({
            "dry_run": True,
            "request": {
                "method": "GET",
                "url": endpoint(platform_url, f"/api/v1/sessions/{encoded_session_id}/events?after={after}"),
                "headers": _redact_headers({"Authorization": f"Bearer {access_token}"}) if access_token else {},
            },
        })
        return 0
    import time
    while True:
        result = platform.list_events(session_id, after=after)
        items = result.get("items", [])
        for event in items:
            if args.json:
                print(json.dumps(_redact_sensitive(event), ensure_ascii=False, default=str))
            else:
                print(f"{event.get('seq')}\t{event.get('type')}\t{event.get('id')}")
            after = max(after, event.get("seq", after))
        if not args.follow:
            break
        if not items:
            time.sleep(2.0)
    return 0


def command_workspace_switch(args: argparse.Namespace, store: ConfigStore) -> int:
    platform_url = _get_value(store, args, "platform_url", "platform_url") or "http://localhost:20200"
    access_token = _get_value(store, args, "access_token", "access_token")
    if not access_token:
        raise CliError("An access token is required to switch workspace")
    platform = PlatformClient(platform_url, access_token, timeout=args.timeout)
    workspace_id = args.workspace_id
    if args.dry_run:
        _json_print({
            "dry_run": True,
            "request": {
                "method": "POST",
                "url": endpoint(platform_url, f"/api/v1/workspaces/{quote(workspace_id, safe='')}/switch"),
                "headers": _redact_headers({"Authorization": f"Bearer {access_token}"}) if access_token else {},
            },
        })
        return 0
    result = _redact_sensitive(platform.switch_workspace(workspace_id))
    if args.set_default:
        store.set("workspace_id", workspace_id, args.profile)
    if args.json:
        _json_print(result)
    else:
        workspace = result.get("workspace", {})
        print(f"Switched to workspace {workspace.get('id', workspace_id)} ({workspace.get('name', '')})")
        if args.set_default:
            print(f"Saved {workspace_id} as default workspace for profile {args.profile or store.current_profile()}.")
    return 0


def command_auth_login(args: argparse.Namespace, store: ConfigStore) -> int:
    profile = args.profile
    if args.api_key:
        store.set("api_key", args.api_key, profile)
        if args.gateway_url:
            store.set("gateway_url", args.gateway_url, profile)
        result = {"profile": profile or store.current_profile(), "api_key": "<configured>"}
        _json_print(result) if args.json else print(f"Saved gateway API key to profile {result['profile']}.")
        return 0
    if args.access_token:
        store.set("access_token", args.access_token, profile)
        if args.platform_url:
            store.set("platform_url", args.platform_url, profile)
        result = {"profile": profile or store.current_profile(), "access_token": "<configured>"}
        _json_print(result) if args.json else print(f"Saved platform access token to profile {result['profile']}.")
        return 0
    email = args.email or (input("Email: ") if sys.stdin.isatty() else None)
    password = args.password or (getpass.getpass("Password: ") if sys.stdin.isatty() else None)
    if not email or not password:
        raise CliError("auth login requires --api-key, --access-token, or email/password")
    mfa_code = args.mfa_code
    if mfa_code is None and sys.stdin.isatty():
        mfa_code = getpass.getpass("MFA code (press Enter if not enabled): ").strip() or None
    platform_url = args.platform_url or store.get("platform_url", profile) or "http://localhost:20200"
    result = PlatformClient(platform_url, None, timeout=args.timeout).login(email, password, mfa_code=mfa_code)
    access_token = result.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise CliError("Login response did not contain an access_token")
    store.set("access_token", access_token, profile)
    store.set("platform_url", platform_url, profile)
    user = result.get("user") if isinstance(result.get("user"), dict) else {}
    if user.get("workspace_id"):
        store.set("workspace_id", str(user["workspace_id"]), profile)
    output = {"profile": profile or store.current_profile(), "user": user, "token_type": result.get("token_type", "bearer")}
    _json_print(output) if args.json else print(f"Logged in as {user.get('email', email)}.")
    return 0


def command_auth_logout(args: argparse.Namespace, store: ConfigStore) -> int:
    profile = args.profile
    access_token = _get_value(store, args, "access_token", "access_token")
    platform_url = args.platform_url or store.get("platform_url", profile) or "http://localhost:20200"
    if access_token:
        PlatformClient(platform_url, access_token, timeout=args.timeout).logout()
    deleted: list[str] = []
    for key in ("access_token", "refresh_token", "api_key"):
        if store.delete(key, profile):
            deleted.append(key)
    if args.json:
        _json_print({"profile": profile or store.current_profile(), "logged_out": True, "deleted_keys": deleted})
    else:
        print(f"Logged out and cleared tokens for profile {profile or store.current_profile()}.")
    return 0


def command_auth_status(args: argparse.Namespace, store: ConfigStore) -> int:
    profile = args.profile
    access_token = _get_value(store, args, "access_token", "access_token")
    platform_url = args.platform_url or store.get("platform_url", profile) or "http://localhost:20200"
    if not access_token:
        raise CliError("Not logged in; run `workama auth login` first")
    user = _redact_sensitive(PlatformClient(platform_url, access_token, timeout=args.timeout).me())
    if args.json:
        _json_print({"profile": profile or store.current_profile(), "user": user})
    else:
        print(f"Logged in as {user.get('email', user.get('id', 'unknown'))} on {platform_url}")
    return 0


def command_config(args: argparse.Namespace, store: ConfigStore) -> int:
    profile = args.profile
    if args.config_action == "set":
        store.set(args.key, args.value, profile)
        if args.json:
            _json_print({"profile": profile or store.current_profile(), "key": args.key, "value": "<configured>" if args.key in SECRET_KEYS else args.value})
        else:
            print(f"Set {args.key} in profile {profile or store.current_profile()}.")
        return 0
    if args.config_action == "get":
        if args.key:
            value = store.get(args.key, profile)
            if value is None:
                raise CliError(f"Config key is not set: {args.key}")
            if args.json:
                _json_print({"profile": profile or store.current_profile(), "key": args.key, "value": value})
            else:
                print(value)
        else:
            _json_print(store.display_values(profile, show_secrets=args.show_secrets))
        return 0
    if args.config_action == "use":
        store.use(args.profile_name)
        print(f"Using profile {args.profile_name}.")
        return 0
    if args.config_action == "use-workspace":
        store.set("workspace_id", args.workspace_id, profile)
        if args.json:
            _json_print({"profile": profile or store.current_profile(), "workspace_id": args.workspace_id})
        else:
            print(f"Using workspace {args.workspace_id} in profile {profile or store.current_profile()}.")
        return 0
    if args.config_action == "unset":
        existed = store.delete(args.key, profile)
        if args.json:
            _json_print({"profile": profile or store.current_profile(), "key": args.key, "existed": existed})
        else:
            print(f"Removed {args.key} from profile {profile or store.current_profile()}.")
        return 0
    if args.config_action == "profiles":
        names = store.profile_names()
        _json_print(names) if args.json else print("\n".join(names))
        return 0
    raise CliError(f"Unknown config action: {args.config_action}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workama",
        description="WorkAMA CLI for gateway chat and Agent runs.",
    )
    parser.add_argument("--version", action="version", version=f"workama {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth = subparsers.add_parser("auth", help="Manage WorkAMA authentication")
    auth_subparsers = auth.add_subparsers(dest="auth_action", required=True)
    login = auth_subparsers.add_parser("login", help="Save a gateway key or log in to the Platform API")
    login.add_argument("--api-key", help="Gateway API key")
    login.add_argument("--access-token", help="Existing Platform access token")
    login.add_argument("--email")
    login.add_argument("--password")
    login.add_argument("--mfa-code", help="Six-digit TOTP code for accounts with MFA enabled")
    login.add_argument("--platform-url")
    login.add_argument("--gateway-url")
    login.add_argument("--profile", default=None)
    login.add_argument("--timeout", type=float, default=30.0)
    login.add_argument("--json", action="store_true")
    auth_logout = auth_subparsers.add_parser("logout", help="Log out and clear local tokens")
    auth_logout.add_argument("--platform-url")
    auth_logout.add_argument("--access-token", "--token", dest="access_token")
    auth_logout.add_argument("--profile", default=None)
    auth_logout.add_argument("--timeout", type=float, default=30.0)
    auth_logout.add_argument("--json", action="store_true")
    auth_status = auth_subparsers.add_parser("status", help="Show the current login status")
    auth_status.add_argument("--platform-url")
    auth_status.add_argument("--access-token", "--token", dest="access_token")
    auth_status.add_argument("--profile", default=None)
    auth_status.add_argument("--timeout", type=float, default=30.0)
    auth_status.add_argument("--json", action="store_true")

    config = subparsers.add_parser("config", help="Read and update local CLI configuration")
    config_subparsers = config.add_subparsers(dest="config_action", required=True)
    config_set = config_subparsers.add_parser("set", help="Set a profile value")
    config_set.add_argument("key")
    config_set.add_argument("value")
    config_set.add_argument("--profile", default=None)
    config_set.add_argument("--json", action="store_true")
    config_get = config_subparsers.add_parser("get", help="Get one value or all profile values")
    config_get.add_argument("key", nargs="?")
    config_get.add_argument("--profile", default=None)
    config_get.add_argument("--show-secrets", action="store_true")
    config_get.add_argument("--json", action="store_true")
    config_use = config_subparsers.add_parser("use", help="Select the current profile")
    config_use.add_argument("profile_name")
    config_use_workspace = config_subparsers.add_parser("use-workspace", help="Set the active workspace for the current profile")
    config_use_workspace.add_argument("workspace_id")
    config_use_workspace.add_argument("--profile", default=None)
    config_use_workspace.add_argument("--json", action="store_true")
    config_unset = config_subparsers.add_parser("unset", help="Remove a profile value")
    config_unset.add_argument("key")
    config_unset.add_argument("--profile", default=None)
    config_unset.add_argument("--json", action="store_true")
    config_profiles = config_subparsers.add_parser("profiles", help="List profiles")
    config_profiles.add_argument("--json", action="store_true")

    chat = subparsers.add_parser("chat", help="Call the OpenAI-compatible WorkAMA gateway")
    chat.add_argument("prompt", nargs="?")
    chat.add_argument("--gateway-url")
    chat.add_argument("--api-key")
    chat.add_argument("--model")
    chat.add_argument("--system")
    chat.add_argument("--temperature", type=float)
    chat.add_argument("--max-tokens", type=int)
    chat.add_argument("--stream", action="store_true")
    chat.add_argument("--dry-run", action="store_true", help="Print the request without sending it")
    chat.add_argument("--json", action="store_true")
    chat.add_argument("--profile", default=None)
    chat.add_argument("--timeout", type=float, default=120.0)
    chat.add_argument("--list", action="store_true", help="List recent chat sessions")
    chat.add_argument("--resume", metavar="SESSION_ID", help="Resume a chat session by session id")
    chat.add_argument("--after", type=int, default=0, help="Event seq to resume after")
    chat.add_argument("--platform-url")
    chat.add_argument("--access-token", "--token", dest="access_token")

    run = subparsers.add_parser("run", help="Run a prompt through the Agent WebSocket")
    run.add_argument("prompt")
    run.add_argument("--platform-url")
    run.add_argument("--agent-ws-url")
    run.add_argument("--access-token", "--token", dest="access_token")
    run.add_argument("--model")
    run.add_argument("--session-id")
    run.add_argument("--title")
    run.add_argument("--max-steps", type=int, default=50)
    run.add_argument("--max-credits", type=float, default=500.0)
    run.add_argument("--max-duration", type=int, default=3600)
    run.add_argument("--dry-run", action="store_true", help="Print the session and WebSocket plan")
    run.add_argument("--json", action="store_true")
    run.add_argument("--profile", default=None)
    run.add_argument("--timeout", type=float, default=3600.0)

    code = subparsers.add_parser("code", help="Create and inspect WorkAMA code tasks")
    code_subparsers = code.add_subparsers(dest="code_action", required=True)
    task = code_subparsers.add_parser("task", help="Create a code task")
    task.add_argument("prompt")
    task.add_argument("--platform-url", "--api-base-url", dest="platform_url")
    task.add_argument("--access-token", "--token", dest="access_token")
    task.add_argument("--repository-id")
    task.add_argument("--session-id")
    task.add_argument("--title")
    task.add_argument("--branch", default="workama/task")
    task.add_argument("--dry-run", action="store_true", help="Print the code task plan without sending it")
    task.add_argument("--json", action="store_true")
    task.add_argument("--profile", default=None)
    task.add_argument("--timeout", type=float, default=120.0)

    code_list = code_subparsers.add_parser("list", help="List code tasks")
    code_list.add_argument("--platform-url", "--api-base-url", dest="platform_url")
    code_list.add_argument("--access-token", "--token", dest="access_token")
    code_list.add_argument("--status", choices=("queued", "running", "paused", "succeeded", "failed", "cancelled"))
    code_list.add_argument("--limit", type=int, default=100)
    code_list.add_argument("--dry-run", action="store_true", help="Print the list request without sending it")
    code_list.add_argument("--json", action="store_true")
    code_list.add_argument("--profile", default=None)
    code_list.add_argument("--timeout", type=float, default=120.0)

    code_event = code_subparsers.add_parser("event", help="List events for a code task")
    code_event.add_argument("task_id")
    code_event.add_argument("--after", type=int, default=0)
    code_event.add_argument("--limit", type=int, default=500)
    code_event.add_argument("--platform-url", "--api-base-url", dest="platform_url")
    code_event.add_argument("--access-token", "--token", dest="access_token")
    code_event.add_argument("--dry-run", action="store_true", help="Print the event request without sending it")
    code_event.add_argument("--json", action="store_true")
    code_event.add_argument("--profile", default=None)
    code_event.add_argument("--timeout", type=float, default=120.0)

    code_status = code_subparsers.add_parser("status", help="Show the status of a code task")
    code_status.add_argument("task_id", nargs="?")
    code_status.add_argument("--platform-url", "--api-base-url", dest="platform_url")
    code_status.add_argument("--access-token", "--token", dest="access_token")
    code_status.add_argument("--dry-run", action="store_true", help="Print the status request without sending it")
    code_status.add_argument("--json", action="store_true")
    code_status.add_argument("--profile", default=None)
    code_status.add_argument("--timeout", type=float, default=120.0)

    code_artifact = code_subparsers.add_parser("artifact", help="List or download artifacts for a code task")
    code_artifact.add_argument("--task", dest="task_id", help="Code task ID whose session artifacts to list")
    code_artifact.add_argument("--download", metavar="ARTIFACT_ID", help="Artifact ID to download")
    code_artifact.add_argument("--output", help="Local file path to save the downloaded artifact")
    code_artifact.add_argument("--platform-url", "--api-base-url", dest="platform_url")
    code_artifact.add_argument("--access-token", "--token", dest="access_token")
    code_artifact.add_argument("--dry-run", action="store_true", help="Print the artifact request without sending it")
    code_artifact.add_argument("--json", action="store_true")
    code_artifact.add_argument("--profile", default=None)
    code_artifact.add_argument("--timeout", type=float, default=120.0)

    event = subparsers.add_parser("event", help="Tail Agent event streams")
    event_subparsers = event.add_subparsers(dest="event_action", required=True)
    event_tail = event_subparsers.add_parser("tail", help="Tail events for an Agent session")
    event_tail.add_argument("--session", dest="session", required=True, help="Session ID to tail")
    event_tail.add_argument("--after", type=int, default=0, help="Event seq to resume after")
    event_tail.add_argument("--follow", action="store_true", help="Keep polling for new events")
    event_tail.add_argument("--platform-url")
    event_tail.add_argument("--access-token", "--token", dest="access_token")
    event_tail.add_argument("--dry-run", action="store_true", help="Print the tail request without sending it")
    event_tail.add_argument("--json", action="store_true")
    event_tail.add_argument("--profile", default=None)
    event_tail.add_argument("--timeout", type=float, default=120.0)

    workspace = subparsers.add_parser("workspace", help="Manage the active workspace")
    workspace_subparsers = workspace.add_subparsers(dest="workspace_action", required=True)
    workspace_switch = workspace_subparsers.add_parser("switch", help="Switch to another workspace")
    workspace_switch.add_argument("workspace_id")
    workspace_switch.add_argument("--set-default", action="store_true", help="Save this workspace as the default")
    workspace_switch.add_argument("--platform-url")
    workspace_switch.add_argument("--access-token", "--token", dest="access_token")
    workspace_switch.add_argument("--dry-run", action="store_true", help="Print the switch request without sending it")
    workspace_switch.add_argument("--json", action="store_true")
    workspace_switch.add_argument("--profile", default=None)
    workspace_switch.add_argument("--timeout", type=float, default=120.0)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        store = ConfigStore()
        if args.command == "auth":
            if args.auth_action == "login":
                return command_auth_login(args, store)
            if args.auth_action == "logout":
                return command_auth_logout(args, store)
            if args.auth_action == "status":
                return command_auth_status(args, store)
        elif args.command == "config":
            return command_config(args, store)
        elif args.command == "chat":
            if args.list:
                return command_chat_list(args, store)
            if args.resume:
                return command_chat_resume(args, store)
            return command_chat(args, store)
        elif args.command == "run":
            return command_run(args, store)
        elif args.command == "code":
            if args.code_action == "task":
                return command_code_task(args, store)
            if args.code_action == "list":
                return command_code_list(args, store)
            if args.code_action == "event":
                return command_code_event(args, store)
            if args.code_action == "status":
                return command_code_status(args, store)
            if args.code_action == "artifact":
                return command_code_artifact(args, store)
        elif args.command == "event":
            if args.event_action == "tail":
                return command_event_tail(args, store)
        elif args.command == "workspace":
            if args.workspace_action == "switch":
                return command_workspace_switch(args, store)
        raise CliError("A supported command is required")
    except (CliError, ConfigError, ApiError, TransportError, ClientError) as exc:
        print(f"workama: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
