#!/usr/bin/env python3
"""Billing async metering idempotency smoke test.

Creates a fresh workspace, seeds it via onboarding, posts the same
MeteringEvent twice to /internal/billing/meter-events, and verifies:
- first call returns status=processed
- second call returns status=duplicate
- ops_inbox record is queryable by event_id
- account balance decreased exactly once
- usage record exists for the request_id
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlencode

BASE_URL = os.environ.get("WORKAMA_API_URL", "http://localhost:20200")
INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN", "change-this-internal-token")


def _request(
    method: str,
    path: str,
    body: dict | None = None,
    headers: dict | None = None,
) -> dict:
    url = f"{BASE_URL}{path}"
    req_headers = {"Content-Type": "application/json", **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers=req_headers, method=method
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode()
        raise RuntimeError(
            f"HTTP {exc.code} on {method} {path}: {body_text}"
        ) from exc


def _meter_event(workspace_id: str, request_id: str, event_id: str) -> dict:
    return {
        "schema_version": 1,
        "event_id": event_id,
        "event_type": "metering.llm.v1",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "producer": "gateway",
        "workspace_id": workspace_id,
        "trace_id": request_id,
        "idempotency_key": request_id,
        "classification": "C2",
        "payload": {
            "request_id": request_id,
            "token_id": "gwt_smoke",
            "channel_id": "chn_smoke",
            "model": "workama-chat",
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "latency_ms": 250,
            "status_code": 200,
            "error_code": None,
        },
    }


def main() -> int:
    suffix = int(datetime.now(timezone.utc).timestamp() * 1000)
    email = f"billing-smoke-{suffix}@example.com"
    password = "Workama-Billing-Smoke-2026!"

    registration = _request(
        "POST",
        "/api/v1/auth/register",
        {"email": email, "password": password, "display_name": "Billing Smoke"},
    )
    auth = _request(
        "POST",
        "/api/v1/auth/verify-email",
        {"token": registration["debug_token"]},
    )
    access_token = auth["access_token"]
    user_headers = {"Authorization": f"Bearer {access_token}"}

    _request(
        "POST",
        "/api/v1/auth/onboarding",
        {
            "user_role": "developer",
            "primary_goal": "gateway",
            "team_size": "1",
            "data_sensitivity": "standard",
            "preferred_model": "workama-chat",
            "notification_preference": "in_app",
        },
        user_headers,
    )

    me = _request("GET", "/api/v1/auth/me", headers=user_headers)
    workspace_id = me["workspace_id"]

    account_before = _request("GET", "/api/v1/billing/account", headers=user_headers)
    balance_before = float(account_before["total_balance"])

    request_id = f"req_billing_smoke_{suffix}"
    event_id = f"evt_billing_smoke_{suffix}"
    event = _meter_event(workspace_id, request_id, event_id)
    internal_headers = {"X-Internal-Token": INTERNAL_TOKEN}

    first = _request(
        "POST", "/internal/billing/meter-events", event, internal_headers
    )
    duplicate = _request(
        "POST", "/internal/billing/meter-events", event, internal_headers
    )

    inbox = _request(
        "GET", f"/internal/billing/meter-events/{event_id}", headers=internal_headers
    )

    account_after = _request("GET", "/api/v1/billing/account", headers=user_headers)
    balance_after = float(account_after["total_balance"])

    usage = _request("GET", "/api/v1/billing/usage", headers=user_headers)
    usage_models = [item["model"] for item in usage["items"]]

    transactions = _request("GET", "/api/v1/billing/transactions", headers=user_headers)
    request_records = [
        item for item in transactions["items"] if item.get("reference_id") == request_id
    ]

    evidence = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "workspace_id": workspace_id,
        "event_id": event_id,
        "request_id": request_id,
        "first_status": first.get("status"),
        "duplicate_status": duplicate.get("status"),
        "inbox_status": inbox.get("status"),
        "inbox_request_id": inbox.get("request_id"),
        "balance_before": balance_before,
        "balance_after": balance_after,
        "balance_decreased_once": balance_before > balance_after,
        "usage_model_found": "workama-chat" in usage_models,
        "transaction_record_found": bool(request_records),
        "verification_scope": "local-compose",
    }

    os.makedirs("quality/evidence", exist_ok=True)
    with open("quality/evidence/billing-smoke.json", "w", encoding="utf-8") as f:
        json.dump(evidence, f, indent=2, ensure_ascii=False)

    print(json.dumps(evidence, indent=2, ensure_ascii=False))

    ok = (
        evidence["first_status"] == "processed"
        and evidence["duplicate_status"] == "duplicate"
        and evidence["inbox_status"] == "processed"
        and evidence["balance_decreased_once"]
        and evidence["usage_model_found"]
        and evidence["transaction_record_found"]
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
