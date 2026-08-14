#!/usr/bin/env python3
"""Comprehensive end-to-end smoke test for WorkAMA platform (20200-20299 port range).

Tests:
  1. Service health on all ports
  2. Authentication flow (login → token → /auth/me)
  3. Module endpoint reachability (one request per module)
  4. CRUD flow (workspace create/list/delete, knowledge-base create/upload/query/delete)

Writes JSON evidence to ``quality/evidence/smoke-test.json`` and prints a
pass/fail summary. Exits 0 when all checks pass, 1 on any failure.

Usage:
    python tools/smoke-test.py
    # override base URL or credentials via env:
    WORKAMA_API_URL=http://localhost:20200 python tools/smoke-test.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

# ── Configuration ──────────────────────────────────────────────────

API_URL = os.environ.get("WORKAMA_API_URL", "http://localhost:20200").rstrip("/")
GATEWAY_URL = os.environ.get("WORKAMA_GATEWAY_URL", "http://localhost:20202").rstrip("/")
AGENT_URL = os.environ.get("WORKAMA_AGENT_URL", "http://localhost:20201").rstrip("/")
SANDBOX_URL = os.environ.get("WORKAMA_SANDBOX_URL", "http://localhost:20203").rstrip("/")
WEB_URL = os.environ.get("WORKAMA_WEB_URL", "http://localhost:20204").rstrip("/")
MINIO_URL = os.environ.get("WORKAMA_MINIO_URL", "http://localhost:20221").rstrip("/")
GRAFANA_URL = os.environ.get("WORKAMA_GRAFANA_URL", "http://localhost:20230").rstrip("/")
PROMETHEUS_URL = os.environ.get("WORKAMA_PROMETHEUS_URL", "http://localhost:20231").rstrip("/")

TEST_EMAIL = os.environ.get("WORKAMA_TEST_EMAIL", "tester@workama.example.com")
TEST_PASSWORD = os.environ.get("WORKAMA_TEST_PASSWORD", "WorkAMA-Test-2026!")

TIMEOUT = 10.0
EVIDENCE_PATH = Path(
    os.environ.get("EVIDENCE_PATH", "quality/evidence/smoke-test.json")
)

# ── Result tracking ────────────────────────────────────────────────

class Result:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.passed = 0
        self.failed = 0

    def record(self, name: str, ok: bool, detail: str = "", *, status: int | None = None, body: Any = None) -> None:
        entry: dict[str, Any] = {
            "name": name,
            "ok": ok,
            "detail": detail,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if status is not None:
            entry["status"] = status
        if body is not None:
            entry["body"] = body
        self.items.append(entry)
        if ok:
            self.passed += 1
        else:
            self.failed += 1

    def dump(self) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total": self.passed + self.failed,
            "passed": self.passed,
            "failed": self.failed,
            "items": self.items,
        }


results = Result()
client = httpx.Client(timeout=TIMEOUT, follow_redirects=True)

# ── Helpers ────────────────────────────────────────────────────────

def health_check(name: str, url: str, *, expected_status: int = 200) -> None:
    try:
        resp = client.get(url)
        ok = resp.status_code == expected_status
        results.record(name, ok, f"HTTP {resp.status_code}", status=resp.status_code)
    except httpx.ConnectError as exc:
        results.record(name, False, f"Connection error: {exc}")
    except httpx.TimeoutException:
        results.record(name, False, "Timeout")
    except Exception as exc:
        results.record(name, False, f"Error: {exc}")


def api_get(name: str, path: str, *, token: str | None = None, ok_statuses: set[int] | None = None) -> httpx.Response | None:
    url = f"{API_URL}{path}"
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = client.get(url, headers=headers)
        acceptable = ok_statuses or {200, 201}
        is_ok = resp.status_code in acceptable
        results.record(name, is_ok, f"HTTP {resp.status_code}", status=resp.status_code)
        return resp
    except httpx.ConnectError as exc:
        results.record(name, False, f"Connection error: {exc}")
    except httpx.TimeoutException:
        results.record(name, False, "Timeout")
    except Exception as exc:
        results.record(name, False, f"Error: {exc}")
    return None


def api_post(name: str, path: str, body: Any = None, *, token: str | None = None, ok_statuses: set[int] | None = None) -> httpx.Response | None:
    url = f"{API_URL}{path}"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = client.post(url, json=body, headers=headers)
        acceptable = ok_statuses or {200, 201}
        is_ok = resp.status_code in acceptable
        results.record(name, is_ok, f"HTTP {resp.status_code}", status=resp.status_code)
        return resp
    except httpx.ConnectError as exc:
        results.record(name, False, f"Connection error: {exc}")
    except httpx.TimeoutException:
        results.record(name, False, "Timeout")
    except Exception as exc:
        results.record(name, False, f"Error: {exc}")
    return None


def api_delete(name: str, path: str, *, token: str | None = None, ok_statuses: set[int] | None = None) -> httpx.Response | None:
    url = f"{API_URL}{path}"
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = client.request("DELETE", url, headers=headers)
        acceptable = ok_statuses or {200, 204}
        is_ok = resp.status_code in acceptable
        results.record(name, is_ok, f"HTTP {resp.status_code}", status=resp.status_code)
        return resp
    except httpx.ConnectError as exc:
        results.record(name, False, f"Connection error: {exc}")
    except httpx.TimeoutException:
        results.record(name, False, "Timeout")
    except Exception as exc:
        results.record(name, False, f"Error: {exc}")
    return None


# ── 1. Service health checks ──────────────────────────────────────

def test_service_health() -> None:
    print("\n── 1. Service health checks ──")
    health_check("Platform API /healthz", f"{API_URL}/healthz")
    health_check("Platform API /readyz", f"{API_URL}/readyz")
    health_check("Gateway /healthz", f"{GATEWAY_URL}/healthz")
    health_check("Agent Server /healthz", f"{AGENT_URL}/healthz")
    health_check("Sandbox Fleet /healthz", f"{SANDBOX_URL}/healthz")
    health_check("Web Frontend /", f"{WEB_URL}/")
    health_check("MinIO Console /", f"{MINIO_URL}/")
    health_check("Grafana /", f"{GRAFANA_URL}/")
    health_check("Prometheus /-/ready", f"{PROMETHEUS_URL}/-/ready")


# ── 2. Authentication flow ────────────────────────────────────────

def test_auth_flow() -> str | None:
    print("\n── 2. Authentication flow ──")
    token: str | None = None

    # Login
    resp = api_post(
        "Auth login",
        "/api/v1/auth/login",
        {"email": TEST_EMAIL, "password": TEST_PASSWORD},
    )
    if resp and resp.status_code in (200, 201):
        try:
            data = resp.json()
            token = data.get("access_token")
            if token:
                results.record("Auth token returned", True, f"token length={len(token)}")
            else:
                results.record("Auth token returned", False, "No access_token in response")
        except json.JSONDecodeError:
            results.record("Auth token returned", False, "Response is not valid JSON")
    else:
        results.record("Auth token returned", False, "Login request failed")

    # /auth/me
    if token:
        me_resp = api_get("Auth /auth/me", "/api/v1/auth/me", token=token)
        if me_resp and me_resp.status_code == 200:
            try:
                me_data = me_resp.json()
                email_ok = me_data.get("email") == TEST_EMAIL
                results.record("Auth /auth/me returns user info", email_ok, f"email={me_data.get('email')}")
            except json.JSONDecodeError:
                results.record("Auth /auth/me returns user info", False, "Invalid JSON")
        else:
            results.record("Auth /auth/me returns user info", False, "Request failed")
    else:
        results.record("Auth /auth/me returns user info", False, "Skipped: no token")

    return token


# ── 3. Module endpoint checks ─────────────────────────────────────

def test_module_endpoints(token: str | None) -> None:
    print("\n── 3. Module endpoint checks ──")
    endpoints = [
        ("Free providers", "/api/v1/gateway/free-providers"),
        ("Billing plans", "/api/v1/billing/plans"),
        ("MCP manifest", "/api/v1/mcp/manifest"),
        ("Knowledge bases", "/api/v1/knowledge-bases"),
        ("Workspaces", "/api/v1/workspaces"),
        ("Assistants", "/api/v1/assistants"),
        ("Workflows", "/api/v1/workflows"),
        ("Notifications", "/api/v1/notification-center"),
        ("Files stats", "/api/v1/files/stats"),
        ("Unified search types", "/api/v1/unified-search/types"),
        ("Audit logs", "/api/v1/audit-logs"),
        ("Memory vectors health", "/api/v1/memory-vectors/health"),
        ("Devices", "/api/v1/devices"),
    ]
    for name, path in endpoints:
        api_get(name, path, token=token, ok_statuses={200, 401, 403})

    # LLM status — may not exist on all deployments
    llm_resp = api_get("LLM status", "/api/v1/gateway/llm-status", token=token, ok_statuses={200, 401, 403, 404})


# ── 4. CRUD flow ──────────────────────────────────────────────────

def test_crud_flow(token: str | None) -> None:
    print("\n── 4. CRUD flow ──")
    if not token:
        results.record("CRUD: Workspace create", False, "Skipped: no token")
        results.record("CRUD: Workspace list", False, "Skipped: no token")
        results.record("CRUD: Workspace delete", False, "Skipped: no token")
        results.record("CRUD: Knowledge base create", False, "Skipped: no token")
        results.record("CRUD: Knowledge base upload", False, "Skipped: no token")
        results.record("CRUD: Knowledge base query", False, "Skipped: no token")
        results.record("CRUD: Knowledge base delete", False, "Skipped: no token")
        return

    # Get current user org_id for workspace creation
    org_id: str | None = None
    if token:
        me_resp = client.get(f"{API_URL}/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        if me_resp.status_code == 200:
            org_id = me_resp.json().get("org_id")

    # Workspace CRUD (workspace v2 requires name + slug + org_id)
    ws_id: str | None = None
    ts = int(time.time())
    ws_body: dict[str, Any] = {"name": f"smoke-test-{ts}", "slug": f"smoke-test-{ts}"}
    if org_id:
        ws_body["org_id"] = org_id
    create_resp = api_post("CRUD: Workspace create", "/api/v1/workspaces", ws_body, token=token)
    if create_resp and create_resp.status_code in (200, 201):
        try:
            ws_data = create_resp.json()
            ws_id = ws_data.get("id")
        except json.JSONDecodeError:
            pass

    # List workspaces
    api_get("CRUD: Workspace list", "/api/v1/workspaces", token=token)

    # Delete workspace
    if ws_id:
        api_delete("CRUD: Workspace delete", f"/api/v1/workspaces/{ws_id}", token=token)
    else:
        results.record("CRUD: Workspace delete", False, "Skipped: no workspace ID from create")

    # Knowledge base CRUD (requires name + kind)
    kb_id: str | None = None
    kb_create = api_post("CRUD: Knowledge base create", "/api/v1/knowledge-bases", {"name": f"smoke-kb-{ts}", "kind": "general"}, token=token)
    if kb_create and kb_create.status_code in (200, 201):
        try:
            kb_data = kb_create.json()
            kb_id = kb_data.get("id")
        except json.JSONDecodeError:
            pass

    # Upload document (title + content as JSON body)
    if kb_id:
        upload_url = f"{API_URL}/api/v1/knowledge-bases/{kb_id}/documents"
        try:
            upload_resp = client.post(
                upload_url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"title": "smoke-test.txt", "content": "Smoke test document content for knowledge base", "source_type": "manual"},
            )
            upload_ok = upload_resp.status_code in (200, 201, 202)
            results.record("CRUD: Knowledge base upload", upload_ok, f"HTTP {upload_resp.status_code}", status=upload_resp.status_code)
        except Exception as exc:
            results.record("CRUD: Knowledge base upload", False, f"Error: {exc}")
    else:
        results.record("CRUD: Knowledge base upload", False, "Skipped: no knowledge base ID")

    # Query (RAG query endpoint: POST /{kb_id}/rag/query)
    if kb_id:
        api_post("CRUD: Knowledge base query", f"/api/v1/knowledge-bases/{kb_id}/rag/query", {"query": "smoke test", "top_k": 3}, token=token)
    else:
        results.record("CRUD: Knowledge base query", False, "Skipped: no knowledge base ID")

    # Delete
    if kb_id:
        api_delete("CRUD: Knowledge base delete", f"/api/v1/knowledge-bases/{kb_id}", token=token)
    else:
        results.record("CRUD: Knowledge base delete", False, "Skipped: no knowledge base ID")


# ── Main ───────────────────────────────────────────────────────────

def main() -> int:
    print("WorkAMA Smoke Test — 20200-20299 port range")
    print(f"API: {API_URL}")
    print(f"Evidence: {EVIDENCE_PATH}")

    test_service_health()
    token = test_auth_flow()
    test_module_endpoints(token)
    test_crud_flow(token)

    # Write evidence
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    evidence = results.dump()
    EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")

    # Summary
    print("\n═══════════════════════════════════════")
    print(f"  Total:  {evidence['total']}")
    print(f"  Passed: {evidence['passed']}")
    print(f"  Failed: {evidence['failed']}")
    print("═══════════════════════════════════════")

    if evidence["failed"] > 0:
        print("\nFailed checks:")
        for item in evidence["items"]:
            if not item["ok"]:
                print(f"  ✗ {item['name']}: {item['detail']}")
    else:
        print("\nAll checks passed!")

    return 1 if evidence["failed"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
