"""RAG 评测端到端执行脚本（CI 自动化）。

仅依赖 Python 标准库 ``urllib``，与 packages/sdk-python 风格保持一致，
不引入任何第三方依赖。

流程：
1. 登录获取 access_token（POST /api/v1/auth/login）
2. 创建金标集（POST /api/v1/knowledge/golden-sets）
3. 逐条添加 10 个金标用例（POST /api/v1/knowledge/golden-sets/{id}/cases）
4. 执行评测（POST /api/v1/knowledge/golden-sets/{id}/evaluate）
5. 获取评测报告详情（GET /api/v1/knowledge/reports/{id}）
6. 导出报告到 scripts/rag-eval-report.json
7. 打印评测摘要（hit_rate@1/3/5、avg_recall、avg_precision、avg_f1）
8. 如果 hit_rate@3 < 0.8，以退出码 1 失败

退出码：0 成功，1 失败。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Mapping, Optional

# 平台 API 默认配置（可由环境变量覆盖）
DEFAULT_BASE_URL = os.environ.get("RAG_EVAL_BASE_URL", "http://localhost:20200")
DEFAULT_EMAIL = os.environ.get("RAG_EVAL_EMAIL", "tester@workama.example.com")
DEFAULT_PASSWORD = os.environ.get("RAG_EVAL_PASSWORD", "WorkAMA-Test-2026!")

# hit_rate@3 通过阈值，低于此值视为回归，CI 失败
HIT_RATE_THRESHOLD = 0.8

# 默认报告输出路径（相对于仓库根目录）
DEFAULT_REPORT_PATH = os.path.join(os.path.dirname(__file__), "rag-eval-report.json")

# 10 个预定义金标用例（query + expected_answer + expected_context_ids）
GOLDEN_CASES: list[dict[str, Any]] = [
    {"query": "如何重置密码", "expected_answer": "通过登录页的忘记密码链接", "expected_context_ids": ["doc_auth_reset"]},
    {"query": "支持哪些 LLM 供应商", "expected_answer": "OpenAI/Anthropic/Google 等", "expected_context_ids": ["doc_llm_providers"]},
    {"query": "如何创建工作流", "expected_answer": "在工作流页面拖拽节点", "expected_context_ids": ["doc_workflow_create"]},
    {"query": "知识库支持哪些格式", "expected_answer": "PDF/Word/Markdown/HTML", "expected_context_ids": ["doc_kb_formats"]},
    {"query": "如何配置 OAuth", "expected_answer": "在设置页面配置 OAuth 客户端", "expected_context_ids": ["doc_oauth_config"]},
    {"query": "沙箱支持哪些语言", "expected_answer": "Python/JavaScript/Go 等", "expected_context_ids": ["doc_sandbox_langs"]},
    {"query": "如何查看用量统计", "expected_answer": "在仪表盘查看用量", "expected_context_ids": ["doc_usage_stats"]},
    {"query": "API 限流策略", "expected_answer": "按 workspace 限流", "expected_context_ids": ["doc_api_ratelimit"]},
    {"query": "如何导入 One-API", "expected_answer": "用导入工具迁移", "expected_context_ids": ["doc_oneapi_import"]},
    {"query": "记忆向量如何检索", "expected_answer": "基于 cosine 相似度", "expected_context_ids": ["doc_memory_recall"]},
]


# ------------------------- 底层 HTTP 封装 -------------------------


def _http_json(
    method: str,
    url: str,
    body: Optional[Mapping[str, Any]] = None,
    token: Optional[str] = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """执行一次 HTTP 请求并返回解析后的 JSON 字典。

    使用标准库 ``urllib.request``，非 2xx 响应抛出 ``RuntimeError``。
    该函数为模块级单点，便于测试 monkeypatch 替换。
    """
    headers = {"Accept": "application/json", "User-Agent": "workama-rag-eval/1.0"}
    data: Optional[bytes] = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        # 捕获错误响应体，便于定位问题
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code} {exc.reason} for {method} {url}: {err_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error for {method} {url}: {exc.reason}") from exc

    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON response from {method} {url}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"expected JSON object from {method} {url}, got {type(parsed).__name__}")
    return parsed


# ------------------------- 业务步骤 -------------------------


def login(base_url: str, email: str, password: str) -> str:
    """登录并返回 access_token。"""
    resp = _http_json("POST", f"{base_url}/api/v1/auth/login", body={"email": email, "password": password})
    token = resp.get("access_token")
    if not token:
        raise RuntimeError(f"login did not return access_token: {resp}")
    return str(token)


def create_golden_set(base_url: str, token: str, name: str, description: str = "") -> dict[str, Any]:
    """创建金标集，返回完整响应（含 id）。"""
    return _http_json(
        "POST",
        f"{base_url}/api/v1/knowledge/golden-sets",
        body={"name": name, "description": description},
        token=token,
    )


def add_golden_case(base_url: str, token: str, golden_set_id: str, case: Mapping[str, Any]) -> dict[str, Any]:
    """向指定金标集添加一条金标用例。"""
    return _http_json(
        "POST",
        f"{base_url}/api/v1/knowledge/golden-sets/{golden_set_id}/cases",
        body={
            "query": case["query"],
            "expected_answer": case.get("expected_answer", ""),
            "expected_context_ids": list(case.get("expected_context_ids", [])),
            "tags": list(case.get("tags", [])),
        },
        token=token,
    )


def evaluate_golden_set(base_url: str, token: str, golden_set_id: str, top_k: int = 5) -> dict[str, Any]:
    """对金标集执行评测，返回聚合报告。"""
    return _http_json(
        "POST",
        f"{base_url}/api/v1/knowledge/golden-sets/{golden_set_id}/evaluate",
        body={"top_k": top_k},
        token=token,
    )


def get_report(base_url: str, token: str, report_id: str) -> dict[str, Any]:
    """获取评测报告详情（含 by_case 明细）。"""
    return _http_json("GET", f"{base_url}/api/v1/knowledge/reports/{report_id}", token=token)


def save_report(report: Mapping[str, Any], path: str) -> None:
    """将评测报告导出为 JSON 文件。"""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)


def extract_hit_rate_at_3(report: Mapping[str, Any]) -> float:
    """从报告中提取 hit_rate@3。

    hit_at_k 的键可能是字符串 ``"3"`` 或整数 ``3``，这里两种都兼容。
    """
    hit_at_k = report.get("hit_at_k") or {}
    value = hit_at_k.get("3", hit_at_k.get(3, 0.0))
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def format_summary(report: Mapping[str, Any]) -> str:
    """格式化评测摘要为可读字符串。"""
    hit_at_k = report.get("hit_at_k") or {}
    lines = [
        "================ RAG 评测摘要 ================",
        f"报告 ID        : {report.get('id', 'N/A')}",
        f"状态           : {report.get('status', 'N/A')}",
        f"金标集 ID      : {report.get('golden_set_id', 'N/A')}",
        f"用例总数       : {report.get('total_cases', 0)}",
        f"通过用例数     : {report.get('passed_cases', 0)}",
        f"hit_rate@1     : {hit_at_k.get('1', 0.0):.4f}",
        f"hit_rate@3     : {hit_at_k.get('3', 0.0):.4f}",
        f"hit_rate@5     : {hit_at_k.get('5', 0.0):.4f}",
        f"avg_recall     : {float(report.get('avg_recall', 0.0)):.4f}",
        f"avg_precision  : {float(report.get('avg_precision', 0.0)):.4f}",
        f"avg_f1         : {float(report.get('avg_f1', 0.0)):.4f}",
        f"hit_rate@3 阈值 : {HIT_RATE_THRESHOLD:.2f}",
        "==============================================",
    ]
    return "\n".join(lines)


def run_eval(
    base_url: str = DEFAULT_BASE_URL,
    email: str = DEFAULT_EMAIL,
    password: str = DEFAULT_PASSWORD,
    report_path: str = DEFAULT_REPORT_PATH,
    golden_set_name: str = "CI 自动评测集",
) -> tuple[int, dict[str, Any]]:
    """执行完整评测流程，返回 (退出码, 报告)。

    成功返回 (0, report)；hit_rate@3 低于阈值返回 (1, report)；
    任意步骤异常返回 (1, {})。
    """
    # 1. 登录
    token = login(base_url, email, password)

    # 2. 创建金标集
    golden_set = create_golden_set(base_url, token, name=golden_set_name, description="CI 自动化 RAG 评测金标集")
    golden_set_id = golden_set.get("id")
    if not golden_set_id:
        raise RuntimeError(f"create_golden_set did not return id: {golden_set}")

    # 3. 添加 10 个金标用例
    for case in GOLDEN_CASES:
        add_golden_case(base_url, token, golden_set_id, case)

    # 4. 执行评测
    report = evaluate_golden_set(base_url, token, golden_set_id, top_k=5)
    report_id = report.get("id")
    if not report_id:
        raise RuntimeError(f"evaluate_golden_set did not return report id: {report}")

    # 5. 获取报告详情（含 by_case 明细），失败时回退到评测返回的聚合报告
    try:
        detail = get_report(base_url, token, report_id)
        if detail:
            report = detail
    except RuntimeError:
        # 报告详情获取失败不影响主流程，使用评测返回的聚合数据
        pass

    # 6. 导出报告
    save_report(report, report_path)

    # 7. 打印摘要
    print(format_summary(report))

    # 8. hit_rate@3 阈值判断
    hit_rate_3 = extract_hit_rate_at_3(report)
    if hit_rate_3 < HIT_RATE_THRESHOLD:
        print(f"FAIL: hit_rate@3={hit_rate_3:.4f} 低于阈值 {HIT_RATE_THRESHOLD:.2f}")
        return 1, report

    print(f"PASS: hit_rate@3={hit_rate_3:.4f} 达到阈值 {HIT_RATE_THRESHOLD:.2f}")
    return 0, report


def main() -> int:
    """脚本入口，返回退出码。"""
    try:
        exit_code, _report = run_eval()
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，确保 CI 失败可见
        print(f"ERROR: RAG 评测执行失败: {exc}", file=sys.stderr)
        return 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
