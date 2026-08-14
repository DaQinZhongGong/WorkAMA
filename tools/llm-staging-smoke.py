#!/usr/bin/env python3
"""LLM staging / mock fallback smoke test for the local WorkAMA gateway.

Usage:
    # mock/local fallback (no credentials configured)
    python tools/llm-staging-smoke.py

    # real provider staging
    LLM_STAGING_PROVIDER=openai \
    LLM_STAGING_API_KEY=sk-... \
    LLM_STAGING_MODEL=gpt-4o-mini \
    python tools/llm-staging-smoke.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


API_BASE_URL = os.getenv("WORKAMA_API_BASE_URL", "http://localhost:20200").rstrip("/")
GATEWAY_URL = os.getenv("WORKAMA_GATEWAY_URL", "http://localhost:20202").rstrip("/")
EVIDENCE_PATH = Path(os.getenv("EVIDENCE_PATH", "quality/evidence/llm-staging-smoke.json"))


def _setting(name: str) -> str:
    value = os.getenv(name)
    if value:
        return value
    dotenv = Path(".env")
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"')
    return ""


def _post_json(url: str, payload: dict, headers: dict | None = None, timeout: float = 30.0) -> tuple[int, dict | str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body


def _shape_ok(response: dict) -> bool:
    choices = response.get("choices") if isinstance(response, dict) else None
    return isinstance(choices, list) and len(choices) > 0


def main() -> int:
    staging_enabled = bool(_setting("LLM_STAGING_PROVIDER") and _setting("LLM_STAGING_API_KEY"))
    staging_skipped = not staging_enabled

    evidence = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "gateway_url": GATEWAY_URL,
        "api_base_url": API_BASE_URL,
        "staging_provider": _setting("LLM_STAGING_PROVIDER") or None,
        "staging_base_url": _setting("LLM_STAGING_BASE_URL") or None,
        "staging_model": _setting("LLM_STAGING_MODEL") or None,
        "staging_enabled": staging_enabled,
        "staging_skipped": staging_skipped,
        "login_ok": False,
        "chat_status": None,
        "response_shape_ok": False,
        "fallback_mock": False,
        "error": None,
    }

    try:
        token = os.getenv("WORKAMA_TEST_TOKEN")
        if not token:
            email = _setting("TEST_ACCOUNT_EMAIL")
            password = _setting("TEST_ACCOUNT_PASSWORD")
            if not email or not password:
                raise RuntimeError("TEST_ACCOUNT_EMAIL and TEST_ACCOUNT_PASSWORD are required")
            status, login = _post_json(f"{API_BASE_URL}/api/v1/auth/login", {"email": email, "password": password})
            if status != 200 or not isinstance(login, dict):
                raise RuntimeError(f"login failed: {status} {login}")
            token = login.get("access_token")
            if not token:
                raise RuntimeError("login response missing access_token")
        evidence["login_ok"] = True

        # Gateway chat completions require a Gateway token (sk-wama-*), not a JWT.
        api_key = os.getenv("WORKAMA_TEST_API_KEY")
        if not api_key:
            status, key_resp = _post_json(
                f"{API_BASE_URL}/api/v1/gateway/tokens",
                {
                    "name": "llm-staging-smoke",
                    "rpm_limit": 60,
                    "tpm_limit": 100000,
                    "model_whitelist": ["workama-chat"],
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            if status != 201 or not isinstance(key_resp, dict) or not key_resp.get("key"):
                raise RuntimeError(f"gateway token creation failed: {status} {key_resp}")
            api_key = key_resp["key"]

        headers = {"Authorization": f"Bearer {api_key}"}
        payload = {
            "model": "workama-chat",
            "messages": [{"role": "user", "content": "Say 'WorkAMA staging smoke ok' briefly."}],
            "stream": False,
            "max_tokens": 64,
        }
        status, response = _post_json(f"{GATEWAY_URL}/v1/chat/completions", payload, headers=headers)
        evidence["chat_status"] = status

        if status != 200:
            raise RuntimeError(f"gateway chat failed: {status} {response}")

        evidence["response_shape_ok"] = isinstance(response, dict) and _shape_ok(response)
        if evidence["response_shape_ok"]:
            content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
            evidence["fallback_mock"] = "local verification model" in str(content)
        else:
            raise RuntimeError(f"unexpected response shape: {response}")
    except Exception as exc:  # noqa: BLE001
        evidence["error"] = str(exc)

    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))

    if evidence["error"]:
        return 1
    if staging_enabled and evidence["fallback_mock"]:
        print("WARNING: staging was enabled but response looks like the mock/local fallback", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
