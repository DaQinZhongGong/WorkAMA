from __future__ import annotations

import json
import uuid
from typing import Any, Iterator
from urllib.parse import quote, urlencode

from .transport import ApiError, HttpClient, TransportError, WebSocketConnection, endpoint, parse_sse


class ClientError(RuntimeError):
    pass


def bearer(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


class GatewayClient:
    def __init__(self, base_url: str, api_key: str | None, timeout: float = 120.0):
        self.base_url = base_url
        self.api_key = api_key
        self.http = HttpClient(timeout=timeout)
        self.timeout = timeout

    def payload(
        self,
        prompt: str,
        *,
        model: str,
        stream: bool,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": stream}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        return payload

    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.http.request(
            "POST",
            endpoint(self.base_url, "/v1/chat/completions"),
            headers=bearer(self.api_key),
            payload=payload,
            timeout=self.timeout,
        )
        if not isinstance(response, dict):
            raise ClientError("Gateway returned a non-JSON response")
        return response

    def stream_chat(self, payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
        response = self.http.stream(
            "POST",
            endpoint(self.base_url, "/v1/chat/completions"),
            headers=bearer(self.api_key),
            payload=payload,
            timeout=self.timeout,
        )
        yield from parse_sse(response)


class PlatformClient:
    def __init__(self, base_url: str, access_token: str | None, timeout: float = 120.0):
        self.base_url = base_url
        self.access_token = access_token
        self.http = HttpClient(timeout=timeout)
        self.timeout = timeout

    def login(self, email: str, password: str, *, mfa_code: str | None = None) -> dict[str, Any]:
        result = self.http.request(
            "POST",
            endpoint(self.base_url, "/api/v1/auth/login"),
            payload={"email": email, "password": password},
            timeout=self.timeout,
        )
        if not isinstance(result, dict):
            raise ClientError("Platform API returned an invalid login response")
        if result.get("mfa_required"):
            ticket = result.get("mfa_ticket")
            if not isinstance(ticket, str) or not ticket:
                raise ClientError("Platform API returned an invalid MFA ticket")
            if mfa_code is None:
                raise ClientError("MFA is required; provide --mfa-code")
            if len(mfa_code) != 6 or not mfa_code.isascii() or not mfa_code.isdigit():
                raise ClientError("MFA code must be exactly 6 digits")
            result = self.http.request(
                "POST",
                endpoint(self.base_url, "/api/v1/auth/mfa/challenge"),
                payload={"ticket": ticket, "code": mfa_code},
                timeout=self.timeout,
            )
            if not isinstance(result, dict):
                raise ClientError("Platform API returned an invalid MFA response")
        return result

    def create_session(
        self,
        *,
        title: str,
        model: str,
        max_steps: int = 50,
        max_credits: float = 500.0,
        max_duration_seconds: int = 3600,
    ) -> dict[str, Any]:
        if not self.access_token:
            raise ClientError("An access token is required to create an Agent session")
        result = self.http.request(
            "POST",
            endpoint(self.base_url, "/api/v1/sessions"),
            headers=bearer(self.access_token),
            payload={
                "title": title,
                "model": model,
                "agent_kind": "ama_chat",
                "model_config": {"temperature": 0.7},
                "max_steps": max_steps,
                "max_credits": max_credits,
                "max_duration_seconds": max_duration_seconds,
            },
            timeout=self.timeout,
        )
        if not isinstance(result, dict) or not result.get("id"):
            raise ClientError("Platform API returned an invalid session response")
        return result

    def logout(self) -> dict[str, Any]:
        if not self.access_token:
            raise ClientError("An access token is required to log out")
        result = self.http.request(
            "POST",
            endpoint(self.base_url, "/api/v1/auth/logout"),
            headers=bearer(self.access_token),
            timeout=self.timeout,
        )
        if not isinstance(result, dict):
            return {}
        return result

    def me(self) -> dict[str, Any]:
        if not self.access_token:
            raise ClientError("An access token is required to inspect the current user")
        result = self.http.request(
            "GET",
            endpoint(self.base_url, "/api/v1/auth/me"),
            headers=bearer(self.access_token),
            timeout=self.timeout,
        )
        if not isinstance(result, dict):
            raise ClientError("Platform API returned an invalid user response")
        return result

    def list_sessions(self) -> dict[str, Any]:
        if not self.access_token:
            raise ClientError("An access token is required to list Agent sessions")
        result = self.http.request(
            "GET",
            endpoint(self.base_url, "/api/v1/sessions"),
            headers=bearer(self.access_token),
            timeout=self.timeout,
        )
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise ClientError("Platform API returned an invalid session list")
        return result

    def get_session(self, session_id: str) -> dict[str, Any]:
        if not self.access_token:
            raise ClientError("An access token is required to inspect an Agent session")
        result = self.http.request(
            "GET",
            endpoint(self.base_url, f"/api/v1/sessions/{quote(session_id, safe='')}"),
            headers=bearer(self.access_token),
            timeout=self.timeout,
        )
        if not isinstance(result, dict):
            raise ClientError("Platform API returned an invalid session response")
        return result

    def list_events(self, session_id: str, *, after: int = 0) -> dict[str, Any]:
        if not self.access_token:
            raise ClientError("An access token is required to list session events")
        result = self.http.request(
            "GET",
            endpoint(self.base_url, f"/api/v1/sessions/{quote(session_id, safe='')}/events?after={after}"),
            headers=bearer(self.access_token),
            timeout=self.timeout,
        )
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise ClientError("Platform API returned an invalid event list")
        return result

    def create_code_task(
        self,
        *,
        title: str,
        prompt: str,
        branch: str = "workama/task",
        repository_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not self.access_token:
            raise ClientError("An access token is required to create a code task")
        payload: dict[str, Any] = {"title": title, "prompt": prompt, "branch": branch}
        if repository_id:
            payload["repository_id"] = repository_id
        if session_id:
            payload["session_id"] = session_id
        result = self.http.request(
            "POST",
            endpoint(self.base_url, "/api/v1/code/tasks"),
            headers=bearer(self.access_token),
            payload=payload,
            timeout=self.timeout,
        )
        if not isinstance(result, dict) or not result.get("id"):
            raise ClientError("Platform API returned an invalid code task")
        return result

    def get_code_task(self, task_id: str) -> dict[str, Any]:
        if not self.access_token:
            raise ClientError("An access token is required to inspect a code task")
        result = self.http.request(
            "GET",
            endpoint(self.base_url, f"/api/v1/code/tasks/{quote(task_id, safe='')}"),
            headers=bearer(self.access_token),
            timeout=self.timeout,
        )
        if not isinstance(result, dict):
            raise ClientError("Platform API returned an invalid code task response")
        return result

    def get_code_repository(self, repository_id: str) -> dict[str, Any]:
        if not self.access_token:
            raise ClientError("An access token is required to inspect a code repository")
        result = self.http.request(
            "GET",
            endpoint(self.base_url, f"/api/v1/code/repositories/{quote(repository_id, safe='')}"),
            headers=bearer(self.access_token),
            timeout=self.timeout,
        )
        if not isinstance(result, dict):
            raise ClientError("Platform API returned an invalid code repository response")
        return result

    def list_code_tasks(self, *, status: str | None = None, limit: int = 100) -> dict[str, Any]:
        if not self.access_token:
            raise ClientError("An access token is required to list code tasks")
        query: dict[str, str] = {"limit": str(limit)}
        if status:
            query["status"] = status
        url = endpoint(self.base_url, f"/api/v1/code/tasks?{urlencode(query)}")
        result = self.http.request("GET", url, headers=bearer(self.access_token), timeout=self.timeout)
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise ClientError("Platform API returned an invalid code task list")
        return result

    def list_code_events(self, task_id: str, *, after: int = 0, limit: int = 500) -> dict[str, Any]:
        if not self.access_token:
            raise ClientError("An access token is required to list code task events")
        query = urlencode({"after": str(after), "limit": str(limit)})
        url = endpoint(self.base_url, f"/api/v1/code/tasks/{quote(task_id, safe='')}/events?{query}")
        result = self.http.request("GET", url, headers=bearer(self.access_token), timeout=self.timeout)
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise ClientError("Platform API returned an invalid code event list")
        return result

    def switch_workspace(self, workspace_id: str) -> dict[str, Any]:
        if not self.access_token:
            raise ClientError("An access token is required to switch workspace")
        result = self.http.request(
            "POST",
            endpoint(self.base_url, f"/api/v1/workspaces/{quote(workspace_id, safe='')}/switch"),
            headers=bearer(self.access_token),
            timeout=self.timeout,
        )
        if not isinstance(result, dict):
            raise ClientError("Platform API returned an invalid workspace switch response")
        return result

    def list_session_artifacts(self, session_id: str) -> dict[str, Any]:
        if not self.access_token:
            raise ClientError("An access token is required to list session artifacts")
        result = self.http.request(
            "GET",
            endpoint(self.base_url, f"/api/v1/sessions/{quote(session_id, safe='')}/artifacts"),
            headers=bearer(self.access_token),
            timeout=self.timeout,
        )
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise ClientError("Platform API returned an invalid artifact list")
        return result

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        if not self.access_token:
            raise ClientError("An access token is required to inspect an artifact")
        result = self.http.request(
            "GET",
            endpoint(self.base_url, f"/api/v1/artifacts/{quote(artifact_id, safe='')}"),
            headers=bearer(self.access_token),
            timeout=self.timeout,
        )
        if not isinstance(result, dict):
            raise ClientError("Platform API returned an invalid artifact response")
        return result

    def download_artifact(self, artifact_id: str) -> bytes:
        if not self.access_token:
            raise ClientError("An access token is required to download an artifact")
        url = endpoint(self.base_url, f"/api/v1/artifacts/{quote(artifact_id, safe='')}/download")
        return self.http.download("GET", url, headers=bearer(self.access_token), timeout=self.timeout)

    def create_ws_ticket(self, session_id: str) -> dict[str, Any]:
        if not self.access_token:
            raise ClientError("An access token is required to create an Agent WebSocket ticket")
        result = self.http.request(
            "POST",
            endpoint(self.base_url, f"/api/v1/sessions/{quote(session_id, safe='')}/ws-tickets"),
            headers=bearer(self.access_token),
            timeout=self.timeout,
        )
        if not isinstance(result, dict) or not result.get("ticket"):
            raise ClientError("Platform API returned an invalid WebSocket ticket")
        return result


class AgentClient:
    def __init__(
        self,
        platform: PlatformClient,
        ws_base_url: str,
        *,
        timeout: float = 3600.0,
    ):
        self.platform = platform
        self.ws_base_url = ws_base_url
        self.timeout = timeout

    def ws_url(self, session_id: str, ticket: str, after: int = 0) -> str:
        base = self.ws_base_url.rstrip("/")
        if base.startswith("http://"):
            base = "ws://" + base[7:]
        elif base.startswith("https://"):
            base = "wss://" + base[8:]
        return f"{base}/ws/sessions/{quote(session_id, safe='')}?ticket={quote(ticket)}&after={after}"

    def run(
        self,
        prompt: str,
        *,
        model: str,
        session_id: str | None = None,
        title: str | None = None,
        on_event=None,
    ) -> dict[str, Any]:
        session: dict[str, Any]
        if session_id:
            session = self.platform.get_session(session_id)
            after = int(session.get("last_seq") or 0)
        else:
            session = self.platform.create_session(title=title or prompt[:120], model=model)
            session_id = str(session["id"])
            after = int(session.get("last_seq") or 0)
        ticket = self.platform.create_ws_ticket(session_id)
        url = self.ws_url(session_id, str(ticket["ticket"]), after)
        events: list[dict[str, Any]] = []
        deltas: list[str] = []
        connection = WebSocketConnection.connect(
            url,
            timeout=self.timeout,
        )
        try:
            for _ in range(2):
                raw = connection.receive_text()
                try:
                    initial = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(initial, dict) and on_event:
                    on_event(initial)
            request_id = f"cli-{uuid.uuid4().hex}"
            connection.send_json({"type": "message.create", "content": prompt, "request_id": request_id})
            while True:
                raw = connection.receive_text()
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                events.append(event)
                if on_event:
                    on_event(event)
                event_type = event.get("type")
                payload = event.get("payload") or {}
                if event_type == "agent.message.delta":
                    delta = payload.get("delta")
                    if isinstance(delta, str):
                        deltas.append(delta)
                if event_type == "error":
                    raise ClientError(str(payload.get("message") or event.get("message") or "Agent returned an error"))
                if event_type == "agent.message.completed":
                    content = payload.get("content")
                    if isinstance(content, str) and not deltas:
                        deltas.append(content)
                    break
                if event_type == "session.status" and payload.get("to") in {"cancelled", "failed"}:
                    break
                seq = event.get("seq")
                if isinstance(seq, int):
                    connection.send_json({"type": "event.ack", "seq": seq})
        finally:
            connection.close()
        return {"session_id": session_id, "text": "".join(deltas), "events": events}
