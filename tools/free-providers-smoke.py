#!/usr/bin/env python3
"""Free providers catalog smoke test for the local WorkAMA platform-api.

Calls ``GET /api/v1/gateway/free-providers`` on a running platform-api instance
and verifies:
- HTTP 200
- the returned catalog contains at least ``MIN_PROVIDERS`` entries
- every entry exposes ``provider`` / ``name`` / ``free_quota`` fields

Writes an evidence JSON to ``quality/evidence/free-providers-smoke.json`` and
prints each provider's provider/name/free_quota. Exits 0 on success, 1 on
failure.

Usage:
    python tools/free-providers-smoke.py
    # override base URL / threshold via env:
    WORKAMA_API_URL=http://localhost:20200 MIN_PROVIDERS=100 python tools/free-providers-smoke.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


API_URL = os.environ.get("WORKAMA_API_URL", "http://localhost:20200").rstrip("/")
ENDPOINT = f"{API_URL}/api/v1/gateway/free-providers"
EVIDENCE_PATH = Path(
    os.environ.get("EVIDENCE_PATH", "quality/evidence/free-providers-smoke.json")
)

# Minimum number of free providers expected in the catalog.
# v7.134 起 FREE_PROVIDER_PRESETS=100 且 PROVIDER_CATALOG=103。
# 可通过环境变量覆盖。
MIN_PROVIDERS = int(os.environ.get("MIN_PROVIDERS", "100"))


def _fetch_catalog() -> tuple[int, dict | str]:
    req = urllib.request.Request(ENDPOINT, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
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
    except urllib.error.URLError as exc:
        # 连接失败（服务未启动等）
        return -1, str(exc.reason)


def main() -> int:
    evidence: dict = {
        "ok": False,
        "total_providers": 0,
        "providers": [],
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": ENDPOINT,
        "min_required": MIN_PROVIDERS,
        "http_status": None,
        "error": None,
    }

    status, payload = _fetch_catalog()
    evidence["http_status"] = status

    if status != 200:
        evidence["error"] = f"GET {ENDPOINT} returned status {status}: {payload}"
        _write_evidence(evidence)
        print(json.dumps(evidence, indent=2, ensure_ascii=False))
        return 1

    if not isinstance(payload, dict):
        evidence["error"] = f"unexpected response type: {type(payload).__name__}"
        _write_evidence(evidence)
        print(json.dumps(evidence, indent=2, ensure_ascii=False))
        return 1

    # 端点契约：返回 items 列表（同时提供 data 别名）与 total 计数
    items = payload.get("items")
    if not isinstance(items, list):
        # 兼容：若返回的是 providers 字段也接受
        items = payload.get("providers")
        if not isinstance(items, list):
            evidence["error"] = (
                "response missing 'items'/'providers' list field; "
                f"got keys: {list(payload.keys())}"
            )
            _write_evidence(evidence)
            print(json.dumps(evidence, indent=2, ensure_ascii=False))
            return 1

    providers_summary: list[dict] = []
    for item in items:
        key = item.get("provider") or item.get("key")
        providers_summary.append(
            {
                "key": key,
                "name": item.get("name"),
                "free_quota": item.get("free_quota"),
            }
        )

    evidence["total_providers"] = len(items)
    evidence["providers"] = providers_summary

    if len(items) < MIN_PROVIDERS:
        evidence["error"] = (
            f"expected >= {MIN_PROVIDERS} free providers, got {len(items)}"
        )
        _write_evidence(evidence)
        print(json.dumps(evidence, indent=2, ensure_ascii=False))
        return 1

    # 校验每个条目都有关键字段
    for entry in providers_summary:
        if not entry["key"] or not entry["name"] or entry["free_quota"] is None:
            evidence["error"] = (
                f"provider entry missing required fields: {entry}"
            )
            _write_evidence(evidence)
            print(json.dumps(evidence, indent=2, ensure_ascii=False))
            return 1

    evidence["ok"] = True
    _write_evidence(evidence)

    # 打印每个供应商的 provider/name/free_quota
    print(f"OK: {len(items)} free providers (>= {MIN_PROVIDERS} required)")
    for entry in providers_summary:
        print(f"  - {entry['key']}: {entry['name']} | quota: {entry['free_quota']}")
    print(f"evidence -> {EVIDENCE_PATH}")
    return 0


def _write_evidence(evidence: dict) -> None:
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    sys.exit(main())
