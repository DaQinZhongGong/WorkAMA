#!/usr/bin/env python3
"""真实 LLM 渠道接入端到端验证（v7.159）。

对 ``gateway.llm_client.call_llm`` + ``memory_vector`` / ``assistant`` 的真实 LLM
路径做端到端验证：

- 配置一个真实免费 LLM 渠道（SiliconFlow，从 ``free_presets.py`` 取 base_url）
- 不硬编码 API Key：从 ``WORKAMA_INTERNAL_LLM_API_KEY`` 读
- 已配置 Key 时：
    * 登录测试账号 → 创建工作区（如不存在）→ 调用 memory-vector extract
      验证 ``extraction_method='llm'`` → 创建 assistant → 调用 run 验证真实 LLM
- 未配置 Key 时：跳过真实验证，只验证 mock 路径（``extraction_method='mock'``）
- 输出 evidence 到 ``quality/evidence/llm-e2e-verify.json``

Usage:
    # mock 路径验证（无 key）
    python tools/llm-e2e-verify.py

    # 真实 LLM 验证
    $env:WORKAMA_INTERNAL_LLM_API_KEY='sk-xxx'; python tools/llm-e2e-verify.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------- #
# 路径与常量
# --------------------------------------------------------------------------- #
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _PROJECT_ROOT / "apps" / "platform-api" / "src"
sys.path.insert(0, str(_SRC_DIR))

from workama_platform.modules.gateway.free_presets import get_free_preset  # noqa: E402

API_BASE_URL = os.getenv("WORKAMA_API_BASE_URL", "http://localhost:20200").rstrip("/")
TEST_EMAIL = os.getenv("TEST_ACCOUNT_EMAIL", "tester@workama.example.com")
TEST_PASSWORD = os.getenv("TEST_ACCOUNT_PASSWORD", "WorkAMA-Test-2026!")
DEFAULT_PROVIDER = os.getenv("WORKAMA_INTERNAL_LLM_PROVIDER", "siliconflow").strip()
DEFAULT_MODEL = os.getenv(
    "WORKAMA_INTERNAL_LLM_MODEL", "Qwen/Qwen2.5-7B-Instruct"
).strip()
EVIDENCE_PATH = Path(
    os.environ.get("EVIDENCE_PATH", "quality/evidence/llm-e2e-verify.json")
)
if not EVIDENCE_PATH.is_absolute():
    EVIDENCE_PATH = _PROJECT_ROOT / EVIDENCE_PATH

_TIMEOUT = 30.0


# --------------------------------------------------------------------------- #
# HTTP 工具
# --------------------------------------------------------------------------- #
def _request(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    headers: dict | None = None,
    timeout: float = _TIMEOUT,
) -> tuple[int, dict | str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def _get(url: str, token: str | None = None) -> tuple[int, dict | str]:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    return _request("GET", url, headers=headers)


def _post(url: str, body: dict, token: str | None = None) -> tuple[int, dict | str]:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    return _request("POST", url, body=body, headers=headers)


# --------------------------------------------------------------------------- #
# 流程
# --------------------------------------------------------------------------- #
def _login() -> tuple[str | None, dict]:
    """登录测试账号，返回 (token, evidence_dict)。"""
    status, body = _post(
        f"{API_BASE_URL}/api/v1/auth/login",
        {"email": TEST_EMAIL, "password": TEST_PASSWORD},
    )
    ev = {"status": status, "ok": status == 200}
    if status == 200 and isinstance(body, dict):
        token = body.get("access_token") or body.get("token")
        ev["token_present"] = bool(token)
        return token, ev
    ev["error"] = str(body)[:200] if body else "no body"
    return None, ev


def _ensure_workspace(token: str) -> tuple[str | None, dict]:
    """获取或创建测试工作区，返回 (workspace_id, evidence_dict)。"""
    # 先列出现有工作区，取第一个
    status, body = _get(f"{API_BASE_URL}/api/v1/workspaces?limit=5", token)
    ev = {"list_status": status}
    if status == 200 and isinstance(body, dict):
        items = body.get("items") or body.get("data") or []
        if items and isinstance(items[0], dict):
            ws_id = items[0].get("id")
            ev["workspace_id"] = ws_id
            ev["reused"] = True
            return ws_id, ev
    # 没有工作区，尝试创建
    create_status, create_body = _post(
        f"{API_BASE_URL}/api/v1/workspaces",
        {"name": "llm-e2e-verify", "slug": "llm-e2e-verify"},
        token,
    )
    ev["create_status"] = create_status
    if create_status in (200, 201) and isinstance(create_body, dict):
        ws_id = create_body.get("id")
        ev["workspace_id"] = ws_id
        ev["reused"] = False
        return ws_id, ev
    ev["error"] = str(create_body)[:200] if create_body else "no body"
    return None, ev


def _verify_memory_vector_extract(
    token: str, expect_llm: bool
) -> tuple[bool, dict]:
    """调用 memory-vector extract 端点验证 LLM 抽取路径。

    - expect_llm=True：期望 ``extraction_method='llm'``（真实 LLM 调用）
    - expect_llm=False：期望 ``extraction_method='mock'``（mock 回退路径）
    """
    # 用一段含 "用户叫" + "喜欢" 的文本，mock 路径也能产出 2 条
    payload = {"conversation_text": "用户叫张三，喜欢喝咖啡"}
    status, body = _post(
        f"{API_BASE_URL}/api/v1/memory-vectors/extract", payload, token
    )
    ev = {
        "status": status,
        "request_payload": payload,
    }
    if status != 200 or not isinstance(body, dict):
        ev["ok"] = False
        ev["error"] = str(body)[:200] if body else "no body"
        return False, ev
    method = body.get("extraction_method")
    ev["extraction_method"] = method
    ev["count"] = body.get("count", 0)
    ev["extracted_ids_count"] = len(body.get("extracted_ids") or [])
    if expect_llm:
        ok = method == "llm"
    else:
        ok = method == "mock"
    ev["ok"] = ok
    ev["expected"] = "llm" if expect_llm else "mock"
    return ok, ev


def _verify_assistant_run(token: str, expect_llm: bool) -> tuple[bool, dict]:
    """创建一个临时助手并运行，验证 LLM 调用路径。

    - expect_llm=True：期望 metadata.method='llm'
    - expect_llm=False：期望 metadata.method='mock'
    """
    # 1. 创建助手
    create_status, create_body = _post(
        f"{API_BASE_URL}/api/v1/assistants",
        {
            "name": "llm-e2e-verify-temp",
            "system_prompt": "You are a helpful assistant. Answer briefly.",
            "model": DEFAULT_MODEL,
            "temperature": 0.3,
            "max_tokens": 256,
            "memory_enabled": False,
        },
        token,
    )
    ev = {"create_status": create_status}
    if create_status not in (200, 201) or not isinstance(create_body, dict):
        ev["ok"] = False
        ev["error"] = str(create_body)[:200] if create_body else "create failed"
        return False, ev
    assistant_id = create_body.get("id")
    ev["assistant_id"] = assistant_id
    if not assistant_id:
        ev["ok"] = False
        ev["error"] = "missing id in create response"
        return False, ev

    # 2. 运行助手
    run_status, run_body = _post(
        f"{API_BASE_URL}/api/v1/assistants/{assistant_id}/run",
        {"user_message": "Say 'pong' if you hear me."},
        token,
    )
    ev["run_status"] = run_status
    if run_status != 200 or not isinstance(run_body, dict):
        ev["ok"] = False
        ev["error"] = str(run_body)[:200] if run_body else "run failed"
        return False, ev

    method = (run_body.get("metadata") or {}).get("method")
    ev["method"] = method
    ev["tokens_used"] = run_body.get("tokens_used")
    ev["assistant_message_preview"] = (run_body.get("assistant_message") or "")[:120]
    if expect_llm:
        ok = method == "llm"
    else:
        ok = method == "mock"
    ev["ok"] = ok
    ev["expected"] = "llm" if expect_llm else "mock"
    return ok, ev


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def main() -> int:
    api_key = os.getenv("WORKAMA_INTERNAL_LLM_API_KEY", "").strip()
    has_key = bool(api_key)
    preset = get_free_preset(DEFAULT_PROVIDER)
    base_url = preset.get("base_url") if preset else None
    free_models = preset.get("free_models") if preset else []

    evidence: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "v7.159",
        "api_base_url": API_BASE_URL,
        "internal_provider": DEFAULT_PROVIDER,
        "internal_base_url": base_url,
        "internal_model": DEFAULT_MODEL,
        "free_models": list(free_models) if free_models else [],
        "api_key_configured": has_key,
        "mode": "real_llm" if has_key else "mock_only",
        "login": None,
        "workspace": None,
        "memory_vector_extract": None,
        "assistant_run": None,
        "summary": {
            "total_checks": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
        },
    }

    summary = evidence["summary"]

    # 1. 登录
    token, login_ev = _login()
    evidence["login"] = login_ev
    if not token:
        summary["failed"] += 1
        evidence["summary"]["error"] = "login failed; subsequent checks skipped"
        _write_evidence(evidence)
        print(f"[FAIL] login: status={login_ev.get('status')}")
        return 1
    summary["total_checks"] += 1
    summary["passed"] += 1
    print(f"[OK] login: status={login_ev.get('status')}")

    # 2. 工作区
    ws_id, ws_ev = _ensure_workspace(token)
    evidence["workspace"] = ws_ev
    summary["total_checks"] += 1
    if not ws_id:
        summary["failed"] += 1
        evidence["summary"]["error"] = "no workspace available"
        _write_evidence(evidence)
        print(f"[FAIL] workspace: {ws_ev}")
        return 1
    summary["passed"] += 1
    print(f"[OK] workspace: id={ws_id} reused={ws_ev.get('reused')}")

    # 3. memory_vector extract
    mv_ok, mv_ev = _verify_memory_vector_extract(token, expect_llm=has_key)
    evidence["memory_vector_extract"] = mv_ev
    summary["total_checks"] += 1
    if mv_ok:
        summary["passed"] += 1
        print(
            f"[OK] memory_vector extract: method={mv_ev.get('extraction_method')} "
            f"(expected {mv_ev.get('expected')})"
        )
    else:
        summary["failed"] += 1
        print(
            f"[FAIL] memory_vector extract: method={mv_ev.get('extraction_method')} "
            f"(expected {mv_ev.get('expected')})"
        )

    # 4. assistant run
    ast_ok, ast_ev = _verify_assistant_run(token, expect_llm=has_key)
    evidence["assistant_run"] = ast_ev
    summary["total_checks"] += 1
    if ast_ok:
        summary["passed"] += 1
        print(
            f"[OK] assistant run: method={ast_ev.get('method')} "
            f"(expected {ast_ev.get('expected')})"
        )
    else:
        summary["failed"] += 1
        print(
            f"[FAIL] assistant run: method={ast_ev.get('method')} "
            f"(expected {ast_ev.get('expected')})"
        )

    _write_evidence(evidence)
    exit_code = 0 if summary["failed"] == 0 else 1
    print(
        f"\nSummary: {summary['passed']}/{summary['total_checks']} passed, "
        f"{summary['failed']} failed (mode={evidence['mode']})"
    )
    print(f"Evidence: {EVIDENCE_PATH}")
    return exit_code


def _write_evidence(evidence: dict) -> None:
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8"
    )


if __name__ == "__main__":
    sys.exit(main())
