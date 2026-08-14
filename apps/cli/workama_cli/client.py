"""HTTP client for the WorkAMA platform API.

Uses ``httpx`` (already a project dependency). All resource methods return the
parsed JSON response body. On HTTP errors an :class:`ApiError` is raised; on
network errors a :class:`NetworkError` is raised.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import httpx


class ApiError(RuntimeError):
    """Raised when the platform API returns an HTTP error status."""

    def __init__(self, status_code: int, message: str, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.body = body


class NetworkError(RuntimeError):
    """Raised when the request cannot reach the platform API."""


class NotLoggedInError(RuntimeError):
    """Raised when a command requires a token but none is configured."""


def _build_headers(token, workspace_id, extra=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if workspace_id:
        headers["X-Workspace-Id"] = workspace_id
    if extra:
        headers.update(extra)
    return headers


def _parse_response(resp):
    if resp.status_code == 204 or not resp.content:
        return {}
    content_type = resp.headers.get("Content-Type", "")
    if "json" in content_type.lower() or resp.text.lstrip().startswith(("{", "[")):
        try:
            return resp.json()
        except ValueError:
            return resp.text
    return resp.text


class WorkamaClient:
    """Thin sync wrapper around the WorkAMA platform API."""

    def __init__(
        self,
        base_url,
        *,
        token=None,
        workspace_id=None,
        timeout=30.0,
        client=None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.workspace_id = workspace_id
        self.timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)

    # -- internal ----------------------------------------------------------
    def _request(
        self,
        method,
        path,
        *,
        json_body=None,
        files=None,
        data=None,
        params=None,
        extra_headers=None,
        require_token=True,
    ):
        if require_token and not self.token:
            raise NotLoggedInError("Not logged in; run `workama login` first.")
        url = self.base_url + path
        headers = _build_headers(self.token, self.workspace_id, extra_headers)
        if json_body is not None and files is None:
            headers.setdefault("Content-Type", "application/json")
        try:
            resp = self._client.request(
                method.upper(),
                url,
                json=json_body if json_body is not None else None,
                files=dict(files) if files else None,
                data=dict(data) if data else None,
                params=dict(params) if params else None,
                headers=headers,
            )
        except httpx.RequestError as exc:
            raise NetworkError("Network error talking to " + url + ": " + str(exc)) from exc
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            if isinstance(body, dict):
                detail = body.get("detail") or body.get("error") or body.get("message") or body
            else:
                detail = body
            raise ApiError(resp.status_code, "HTTP " + str(resp.status_code) + ": " + str(detail), body)
        return _parse_response(resp)

    def close(self):
        self._client.close()

    # -- auth --------------------------------------------------------------
    def login(self, email, password):
        return self._request("POST", "/api/v1/auth/login", json_body={"email": email, "password": password}, require_token=False)

    def me(self):
        return self._request("GET", "/api/v1/auth/me")

    # -- workspaces --------------------------------------------------------
    def list_workspaces(self):
        return self._request("GET", "/api/v1/workspaces")

    def create_workspace(self, name, *, slug=None):
        payload = {"name": name}
        if slug:
            payload["slug"] = slug
        return self._request("POST", "/api/v1/workspaces", json_body=payload)

    # -- assistants --------------------------------------------------------
    def list_assistants(self):
        return self._request("GET", "/api/v1/assistants")

    def create_assistant(self, name, *, model=None, system_prompt=None):
        payload = {"name": name}
        if model:
            payload["model"] = model
        if system_prompt:
            payload["system_prompt"] = system_prompt
        return self._request("POST", "/api/v1/assistants", json_body=payload)

    def run_assistant(self, assistant_id, *, message):
        return self._request("POST", "/api/v1/assistants/" + assistant_id + "/run", json_body={"message": message})

    # -- workflows ---------------------------------------------------------
    def list_workflows(self):
        return self._request("GET", "/api/v1/workflows")

    def run_workflow(self, workflow_id, *, input_data=None):
        payload = {"input": input_data or {}}
        return self._request("POST", "/api/v1/workflows/" + workflow_id + "/runs", json_body=payload)

    # -- knowledge bases ---------------------------------------------------
    def list_knowledge_bases(self):
        return self._request("GET", "/api/v1/knowledge-bases")

    def create_knowledge_base(self, name, *, description=None):
        payload = {"name": name}
        if description:
            payload["description"] = description
        return self._request("POST", "/api/v1/knowledge-bases", json_body=payload)

    def upload_document(self, kb_id, file_path):
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError("File not found: " + file_path)
        with path.open("rb") as handle:
            files = {"file": (path.name, handle.read(), "application/octet-stream")}
        return self._request("POST", "/api/v1/knowledge-bases/" + kb_id + "/documents", files=files)

    def rag_query(self, kb_id, *, query, top_k=5):
        return self._request("POST", "/api/v1/knowledge-bases/" + kb_id + "/rag/query", json_body={"query": query, "top_k": top_k})

    # -- devices -----------------------------------------------------------
    def list_devices(self):
        return self._request("GET", "/api/v1/devices")

    def register_device(self, name, *, device_type="desktop", metadata=None):
        payload = {"name": name, "type": device_type}
        if metadata:
            payload["metadata"] = metadata
        return self._request("POST", "/api/v1/devices/register", json_body=payload)

    # -- billing -----------------------------------------------------------
    def list_billing_plans(self):
        return self._request("GET", "/api/v1/billing/plans")

    def list_billing_usage(self):
        return self._request("GET", "/api/v1/billing/usage")

    # -- mcp ---------------------------------------------------------------
    def list_mcp_tools(self):
        return self._request("GET", "/api/v1/mcp/tools")

    def invoke_mcp_tool(self, tool_id, *, arguments=None):
        return self._request("POST", "/api/v1/mcp/tools/" + tool_id + "/invoke", json_body={"arguments": arguments or {}})

    # -- free providers ----------------------------------------------------
    def list_free_providers(self):
        return self._request("GET", "/api/v1/gateway/free-providers")

    def enable_free_provider(self, provider_key):
        return self._request("POST", "/api/v1/gateway/free-providers/" + provider_key + "/enable")

    # -- audit logs --------------------------------------------------------
    def list_audit_logs(self, *, limit=50, offset=0):
        return self._request("GET", "/api/v1/audit-logs", params={"limit": limit, "offset": offset})
