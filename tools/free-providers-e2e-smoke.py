#!/usr/bin/env python3
"""Free providers end-to-end real-probe smoke test.

对 ``FREE_PROVIDER_PRESETS`` 中的 100 个免费供应商做真实 API 拨测：

- 按区域分组（``regions`` 字段）：``cn``（国内）/ ``global``（国际）/ ``self_hosted``（自部署）
- 对每个供应商优先调用 ``GET {base_url}/models``（轻量、不消耗 token）
- 若配置了 API Key 且 ``GET /models`` 失败，回退 ``POST {base_url}/chat/completions``
- 自部署类（localhost）连不上标记 ``not_running``（不算失败）
- 未配置 Key 的供应商 ``GET /models`` 失败标记 ``needs_api_key``/``skipped``（不算失败）
- Gemini 协议走 ``/models?key=xxx`` 与 ``/models/{model}:generateContent?key=xxx``
- Cloudflare ``base_url`` 含 ``{account_id}`` 占位，需 ``PROVIDER_CLOUDFLARE_ACCOUNT_ID``

API Key 来源（不硬编码）：
- ``WORKAMA_FREE_PROVIDER_KEYS``：JSON 字符串 ``{"siliconflow":"sk-xxx","groq":"gsk_xxx"}``
- ``PROVIDER_{KEY_UPPER}_API_KEY``：如 ``PROVIDER_SILICONFLOW_API_KEY``

退出码：所有配置了 Key 的供应商都 ``reachable`` 则 0，否则 1；
未配置 Key 的供应商失败不算失败。

Usage:
    python tools/free-providers-e2e-smoke.py
    $env:WORKAMA_FREE_PROVIDER_KEYS='{"siliconflow":"sk-xxx","groq":"gsk_xxx"}'; python tools/free-providers-e2e-smoke.py
    $env:PROVIDER_SILICONFLOW_API_KEY='sk-xxx'; python tools/free-providers-e2e-smoke.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

try:
    import httpx
except ImportError:  # pragma: no cover
    print("ERROR: httpx 未安装，请先 `pip install httpx`", file=sys.stderr)
    sys.exit(2)

# --------------------------------------------------------------------------- #
# 路径与常量
# --------------------------------------------------------------------------- #
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _PROJECT_ROOT / "apps" / "platform-api" / "src"
sys.path.insert(0, str(_SRC_DIR))

from workama_platform.modules.gateway.free_presets import FREE_PROVIDER_PRESETS  # noqa: E402

EVIDENCE_PATH = Path(
    os.environ.get("EVIDENCE_PATH", "quality/evidence/free-providers-e2e-smoke.json")
)
if not EVIDENCE_PATH.is_absolute():
    EVIDENCE_PATH = _PROJECT_ROOT / EVIDENCE_PATH

TIMEOUT = float(os.environ.get("FREE_PROBE_TIMEOUT", "10"))
MAX_WORKERS = int(os.environ.get("FREE_PROBE_WORKERS", "20"))

# 12 家主流免费供应商：配置了 key 时必须真实调用 chat
MAINSTREAM_KEYS: set[str] = {
    "siliconflow", "groq", "openrouter", "together", "cerebras",
    "cloudflare", "huggingface", "modelscope", "deepseek", "qwen",
    "gemini_free", "sambanova",
}

# 自部署类 provider key（base_url 为 localhost 时按自部署探测）
SELF_HOSTED_KEYS: set[str] = {
    "ollama", "vllm", "llamacpp", "xinference", "localai",
    "lmdeploy", "lmstudio", "gpt_link", "oneapi", "newapi",
    "gpt4free", "duckduckgo",
}


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _is_localhost(url: str) -> bool:
    return any(h in url for h in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]"))


def _region_group(regions: list[str]) -> str:
    """每个供应商归入唯一区域组：self_hosted > cn > global。"""
    if "self_hosted" in regions:
        return "self_hosted"
    if "cn" in regions:
        return "cn"
    return "global"


def _load_api_keys() -> dict[str, str]:
    """从环境变量加载 API Key（不硬编码）。"""
    keys: dict[str, str] = {}
    # 1. JSON 字符串：WORKAMA_FREE_PROVIDER_KEYS
    json_env = os.environ.get("WORKAMA_FREE_PROVIDER_KEYS", "").strip()
    if json_env:
        try:
            parsed = json.loads(json_env)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if isinstance(v, str) and v:
                        keys[k] = v
        except json.JSONDecodeError as exc:
            print(f"WARN: WORKAMA_FREE_PROVIDER_KEYS 解析失败: {exc}", file=sys.stderr)
    # 2. 单独环境变量：PROVIDER_{KEY_UPPER}_API_KEY
    for k in FREE_PROVIDER_PRESETS:
        v = os.environ.get(f"PROVIDER_{k.upper()}_API_KEY", "").strip()
        if v and k not in keys:
            keys[k] = v
    return keys


def _short(text: str, limit: int = 200) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "..."


# --------------------------------------------------------------------------- #
# 协议探测
# --------------------------------------------------------------------------- #
def _probe_self_hosted(client: httpx.Client, base_url: str, key: str | None,
                       free_models: list[str]) -> tuple[str, str, int, str]:
    """自部署（localhost）：连不上标记 not_running（不算失败）。"""
    models_url = base_url.rstrip("/") + "/models"
    headers = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        r = client.get(models_url, headers=headers)
        if r.status_code == 200:
            return "reachable", "GET /models (self-hosted)", r.status_code, ""
        return "not_running", "GET /models", r.status_code, _short(r.text)
    except Exception as exc:
        return "not_running", "GET /models (connect)", -1, str(exc)


def _probe_gemini(client: httpx.Client, base_url: str, key: str | None,
                  free_models: list[str]) -> tuple[str, str, int, str]:
    """Gemini 协议：/models?key=xxx 与 /models/{model}:generateContent?key=xxx。"""
    models_url = base_url.rstrip("/") + "/models"
    # GET /models（带 key 优先）
    params = {"key": key} if key else {}
    last_status = -1
    last_detail = ""
    try:
        r = client.get(models_url, params=params)
        last_status, last_detail = r.status_code, _short(r.text)
        if r.status_code == 200:
            return "reachable", "GET /models?key", r.status_code, ""
    except Exception as exc:
        last_status, last_detail = -1, str(exc)

    # 无 key：无法继续，标记 needs_api_key
    if not key:
        return "needs_api_key", "GET /models (no key)", last_status, last_detail

    # 有 key：尝试 generateContent
    model = free_models[0] if free_models else "gemini-1.5-flash"
    chat_url = f"{base_url.rstrip('/')}/models/{model}:generateContent"
    body = {"contents": [{"parts": [{"text": "hi"}]}]}
    try:
        r = client.post(chat_url, params={"key": key}, json=body)
        if r.status_code == 200:
            return "reachable", "POST generateContent", r.status_code, ""
        return "unreachable", "POST generateContent", r.status_code, _short(r.text)
    except Exception as exc:
        return "unreachable", "POST generateContent", -1, str(exc)


def _probe_anthropic(client: httpx.Client, base_url: str, key: str | None,
                     free_models: list[str]) -> tuple[str, str, int, str]:
    """Anthropic 协议：x-api-key header。"""
    models_url = base_url.rstrip("/") + "/models"
    headers = {}
    if key:
        headers["x-api-key"] = key
    try:
        r = client.get(models_url, headers=headers)
        if r.status_code == 200:
            return "reachable", "GET /models (x-api-key)", r.status_code, ""
        if not key:
            return "needs_api_key", "GET /models (no key)", r.status_code, _short(r.text)
    except Exception as exc:
        if not key:
            return "needs_api_key", "GET /models (network)", -1, str(exc)
    # 有 key 回退 chat
    chat_url = base_url.rstrip("/") + "/messages"
    model = free_models[0] if free_models else "claude-3-haiku-20240307"
    body = {"model": model, "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
    try:
        r = client.post(chat_url, headers={"x-api-key": key}, json=body)
        if r.status_code == 200:
            return "reachable", "POST /messages", r.status_code, ""
        return "unreachable", "POST /messages", r.status_code, _short(r.text)
    except Exception as exc:
        return "unreachable", "POST /messages", -1, str(exc)


def _probe_openai(client: httpx.Client, base_url: str, key: str | None,
                  free_models: list[str]) -> tuple[str, str, int, str]:
    """OpenAI 兼容协议：先 GET /models 无 auth，401 再带 key，最后 POST /chat/completions。"""
    models_url = base_url.rstrip("/") + "/models"
    auth_needed = False
    last_status = -1
    last_detail = ""

    # 1. GET /models 不带 Authorization
    try:
        r = client.get(models_url)
        last_status, last_detail = r.status_code, _short(r.text)
        if r.status_code == 200:
            return "reachable", "GET /models (no auth)", r.status_code, ""
        auth_needed = r.status_code in (401, 403)
    except Exception as exc:
        last_status, last_detail = -1, str(exc)
        # 网络错误：无 key 则 skipped，有 key 则尝试 chat
        if not key:
            return "skipped", "GET /models (network, no key)", -1, str(exc)

    # 2. 无 key：401/403 → needs_api_key，其他 → skipped
    if not key:
        if auth_needed:
            return "needs_api_key", "GET /models (401/403, no key)", last_status, last_detail
        return "skipped", "GET /models (no key)", last_status, last_detail

    # 3. 有 key：GET /models 带 auth 重试
    if auth_needed:
        try:
            r = client.get(models_url, headers={"Authorization": f"Bearer {key}"})
            if r.status_code == 200:
                return "reachable", "GET /models (auth)", r.status_code, ""
        except Exception:
            pass  # 回退到 chat

    # 4. POST /chat/completions 带 auth
    chat_url = base_url.rstrip("/") + "/chat/completions"
    model = free_models[0] if free_models else "gpt-3.5-turbo"
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
    try:
        r = client.post(chat_url, headers={"Authorization": f"Bearer {key}"}, json=body)
        if r.status_code == 200:
            return "reachable", "POST /chat/completions", r.status_code, ""
        return "unreachable", "POST /chat/completions", r.status_code, _short(r.text)
    except Exception as exc:
        return "unreachable", "POST /chat/completions", -1, str(exc)


def probe_provider(key: str, preset: dict, api_key: str | None,
                   client: httpx.Client) -> dict:
    """探测单个供应商，返回 evidence 条目。"""
    base_url = preset.get("base_url", "")
    protocol = preset.get("protocol", "openai")
    free_models = preset.get("free_models", []) or []
    regions = preset.get("regions", []) or []

    # Cloudflare {account_id} 占位
    if "{account_id}" in base_url:
        account_id = os.environ.get("PROVIDER_CLOUDFLARE_ACCOUNT_ID", "").strip()
        if account_id:
            base_url = base_url.replace("{account_id}", account_id)
        else:
            return {
                "key": key, "name": preset.get("name"), "provider": preset.get("provider"),
                "region_group": _region_group(regions), "protocol": protocol,
                "base_url": base_url, "has_key": bool(api_key),
                "mainstream": key in MAINSTREAM_KEYS,
                "status": "needs_api_key",
                "method": "GET /models (cloudflare account_id 未配置)",
                "http_status": -1, "detail": "需配置 PROVIDER_CLOUDFLARE_ACCOUNT_ID",
                "latency_ms": 0,
            }

    start = time.perf_counter()
    if _is_localhost(base_url):
        status, method, http_status, detail = _probe_self_hosted(
            client, base_url, api_key, free_models)
    elif protocol == "gemini":
        status, method, http_status, detail = _probe_gemini(
            client, base_url, api_key, free_models)
    elif protocol == "anthropic":
        status, method, http_status, detail = _probe_anthropic(
            client, base_url, api_key, free_models)
    else:
        status, method, http_status, detail = _probe_openai(
            client, base_url, api_key, free_models)
    latency_ms = round((time.perf_counter() - start) * 1000)

    return {
        "key": key, "name": preset.get("name"), "provider": preset.get("provider"),
        "region_group": _region_group(regions), "protocol": protocol,
        "base_url": base_url, "has_key": bool(api_key),
        "mainstream": key in MAINSTREAM_KEYS,
        "status": status, "method": method, "http_status": http_status,
        "detail": detail, "latency_ms": latency_ms,
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    api_keys = _load_api_keys()
    presets = FREE_PROVIDER_PRESETS

    evidence: dict = {
        "ok": False,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "total_probed": 0,
        "reachable": 0,
        "unreachable": 0,
        "needs_api_key": 0,
        "not_running": 0,
        "skipped": 0,
        "by_region_group": {"cn": {"total": 0, "reachable": 0},
                             "global": {"total": 0, "reachable": 0},
                             "self_hosted": {"total": 0, "reachable": 0}},
        "mainstream_providers": [],
        "providers": [],
        "api_keys_configured": sorted(api_keys.keys()),
        "exit_code": 1,
    }

    # 并发探测
    results: list[dict] = []
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            future_map = {
                pool.submit(probe_provider, k, p, api_keys.get(k), client): k
                for k, p in presets.items()
            }
            for fut in as_completed(future_map):
                try:
                    results.append(fut.result())
                except Exception as exc:  # pragma: no cover
                    k = future_map[fut]
                    results.append({
                        "key": k, "status": "unreachable", "method": "probe-error",
                        "http_status": -1, "detail": str(exc), "latency_ms": 0,
                        "region_group": _region_group(presets[k].get("regions", [])),
                        "has_key": k in api_keys,
                        "mainstream": k in MAINSTREAM_KEYS,
                    })

    # 保持 preset 原始顺序
    order = {k: i for i, k in enumerate(presets.keys())}
    results.sort(key=lambda r: order.get(r["key"], 9999))

    for r in results:
        evidence["providers"].append(r)
        evidence["total_probed"] += 1
        st = r["status"]
        if st in evidence:
            evidence[st] += 1
        grp = r.get("region_group", "global")
        if grp in evidence["by_region_group"]:
            evidence["by_region_group"][grp]["total"] += 1
            if st == "reachable":
                evidence["by_region_group"][grp]["reachable"] += 1
        if r.get("mainstream"):
            evidence["mainstream_providers"].append({
                "key": r["key"], "status": st, "method": r.get("method"),
                "http_status": r.get("http_status"), "has_key": r.get("has_key"),
                "latency_ms": r.get("latency_ms"),
            })

    # 退出码：仅配置了 key 的供应商 unreachable 才算失败
    key_unreachable = [r for r in results
                       if r.get("has_key") and r["status"] == "unreachable"]
    evidence["ok"] = len(key_unreachable) == 0
    evidence["exit_code"] = 0 if evidence["ok"] else 1

    _write_evidence(evidence)
    _print_summary(evidence, key_unreachable)
    return evidence["exit_code"]


def _print_summary(evidence: dict, key_unreachable: list[dict]) -> None:
    print("=" * 72)
    print("Free Providers E2E Smoke Test")
    print("=" * 72)
    print(f"checked_at         : {evidence['checked_at']}")
    print(f"total_probed       : {evidence['total_probed']}")
    print(f"reachable          : {evidence['reachable']}")
    print(f"unreachable        : {evidence['unreachable']}")
    print(f"needs_api_key      : {evidence['needs_api_key']}")
    print(f"not_running        : {evidence['not_running']}")
    print(f"skipped            : {evidence['skipped']}")
    print(f"api_keys_configured: {evidence['api_keys_configured']}")
    print("-" * 72)
    print("by region group:")
    for grp, stats in evidence["by_region_group"].items():
        print(f"  {grp:12s}: {stats['reachable']}/{stats['total']} reachable")
    print("-" * 72)
    print("mainstream providers (12):")
    for mp in evidence["mainstream_providers"]:
        flag = "KEY" if mp.get("has_key") else "   "
        print(f"  [{flag}] {mp['key']:14s} -> {mp['status']:14s} "
              f"({mp.get('method','')}, {mp.get('http_status','')})")
    print("-" * 72)
    if key_unreachable:
        print(f"FAILED: {len(key_unreachable)} key-configured provider(s) unreachable:")
        for r in key_unreachable:
            print(f"  - {r['key']}: {r.get('method')} "
                  f"HTTP {r.get('http_status')} {r.get('detail','')}")
    else:
        print("OK: all key-configured providers reachable "
              "(no-key / self-hosted failures ignored)")
    print(f"evidence -> {EVIDENCE_PATH}")
    print("=" * 72)


def _write_evidence(evidence: dict) -> None:
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    sys.exit(main())
