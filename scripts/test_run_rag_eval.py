"""run_rag_eval.py 单元测试。

通过 monkeypatch 替换模块级 ``_http_json`` 来 mock 所有 HTTP 请求，
不依赖真实平台服务，也不启动 Docker Compose。

覆盖：
- 金标用例数据完整性（10 条 / 必填字段 / context_ids 非空）
- 登录逻辑（提取 access_token / 缺失时抛错）
- 金标集创建（请求体 / URL / token 传递）
- 金标用例添加（URL 拼接 / 字段映射）
- 评测执行（POST top_k）
- 报告解析（GET / reports 详情）
- hit_rate 阈值判断（高 hit_rate 通过 / 低 hit_rate 失败 / 键类型兼容）
- run_eval 端到端流程（成功 / 失败 / 登录失败）
- 报告导出与摘要格式化
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

import pytest

# 确保能 import scripts/run_rag_eval.py（无论从仓库根还是 scripts/ 目录运行）
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import run_rag_eval as eval_mod  # noqa: E402


# ------------------------- Fake HTTP 基础设施 -------------------------


class FakeHTTP:
    """可编程的 _http_json 替身：按 (method, url 片段) 路由返回响应。"""

    def __init__(self, routes=None):
        # routes: list of (method, url_contains, response_dict_or_exception)
        self.routes = list(routes) if routes else []
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method, url, body=None, token=None, timeout=30.0):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "body": dict(body) if isinstance(body, dict) else body,
                "token": token,
                "timeout": timeout,
            }
        )
        # 选择 URL 中匹配位置最靠后（最具体）的路由，避免
        # "/golden-sets" 误命中 "/golden-sets/{id}/evaluate" 这类前缀冲突。
        best = None
        best_idx = -1
        for rmethod, rcontains, response in self.routes:
            if rmethod != method:
                continue
            idx = url.find(rcontains)
            if idx == -1:
                continue
            if idx > best_idx:
                best_idx = idx
                best = response
        if best is None:
            raise RuntimeError(f"FakeHTTP: no route for {method} {url}")
        if isinstance(best, Exception):
            raise best
        return dict(best)


# ------------------------- 金标用例数据完整性 -------------------------


def test_golden_cases_count_is_10():
    assert len(eval_mod.GOLDEN_CASES) == 10


def test_golden_cases_have_required_fields():
    for case in eval_mod.GOLDEN_CASES:
        assert "query" in case and case["query"]
        assert "expected_answer" in case and case["expected_answer"]
        assert "expected_context_ids" in case


def test_golden_cases_expected_context_ids_non_empty():
    for case in eval_mod.GOLDEN_CASES:
        ctx = case["expected_context_ids"]
        assert isinstance(ctx, list) and len(ctx) > 0, f"{case['query']} 的 context_ids 不应为空"


def test_golden_cases_queries_unique():
    queries = [c["query"] for c in eval_mod.GOLDEN_CASES]
    assert len(set(queries)) == len(queries)


# ------------------------- 登录逻辑 -------------------------


def test_login_extracts_access_token(monkeypatch):
    fake = FakeHTTP(routes=[("POST", "/auth/login", {"access_token": "tok_123", "token_type": "bearer"})])
    monkeypatch.setattr(eval_mod, "_http_json", fake)

    token = eval_mod.login("http://api", "tester@workama.example.com", "pw")

    assert token == "tok_123"
    call = fake.calls[0]
    assert call["url"] == "http://api/api/v1/auth/login"
    assert call["body"] == {"email": "tester@workama.example.com", "password": "pw"}


def test_login_raises_without_access_token(monkeypatch):
    fake = FakeHTTP(routes=[("POST", "/auth/login", {"mfa_required": True})])
    monkeypatch.setattr(eval_mod, "_http_json", fake)

    with pytest.raises(RuntimeError, match="access_token"):
        eval_mod.login("http://api", "u", "p")


# ------------------------- 金标集创建 -------------------------


def test_create_golden_set_posts_correct_body(monkeypatch):
    fake = FakeHTTP(routes=[("POST", "/golden-sets", {"id": "rgs_1", "name": "CI 自动评测集"})])
    monkeypatch.setattr(eval_mod, "_http_json", fake)

    result = eval_mod.create_golden_set("http://api", "tok", name="CI 自动评测集", description="desc")

    assert result["id"] == "rgs_1"
    call = fake.calls[0]
    assert call["token"] == "tok"
    assert call["body"] == {"name": "CI 自动评测集", "description": "desc"}
    assert call["url"].endswith("/api/v1/knowledge/golden-sets")


# ------------------------- 金标用例添加 -------------------------


def test_add_golden_case_posts_to_correct_url(monkeypatch):
    fake = FakeHTTP(routes=[("POST", "/cases", {"id": "rgc_1"})])
    monkeypatch.setattr(eval_mod, "_http_json", fake)

    case = {"query": "Q1", "expected_answer": "A1", "expected_context_ids": ["c1"], "tags": ["t1"]}
    result = eval_mod.add_golden_case("http://api", "tok", "rgs_1", case)

    assert result["id"] == "rgc_1"
    call = fake.calls[0]
    assert call["url"] == "http://api/api/v1/knowledge/golden-sets/rgs_1/cases"
    assert call["body"]["query"] == "Q1"
    assert call["body"]["expected_context_ids"] == ["c1"]
    assert call["body"]["tags"] == ["t1"]


# ------------------------- 评测执行 -------------------------


def test_evaluate_golden_set_posts_top_k(monkeypatch):
    fake = FakeHTTP(
        routes=[
            ("POST", "/evaluate", {"id": "rgr_1", "hit_at_k": {"1": 1.0, "3": 1.0, "5": 1.0}}),
        ]
    )
    monkeypatch.setattr(eval_mod, "_http_json", fake)

    result = eval_mod.evaluate_golden_set("http://api", "tok", "rgs_1", top_k=5)

    assert result["id"] == "rgr_1"
    call = fake.calls[0]
    assert call["url"] == "http://api/api/v1/knowledge/golden-sets/rgs_1/evaluate"
    assert call["body"] == {"top_k": 5}


# ------------------------- 报告解析 -------------------------


def test_get_report_uses_get(monkeypatch):
    fake = FakeHTTP(routes=[("GET", "/reports/", {"id": "rgr_1", "by_case": []})])
    monkeypatch.setattr(eval_mod, "_http_json", fake)

    result = eval_mod.get_report("http://api", "tok", "rgr_1")

    assert result["id"] == "rgr_1"
    call = fake.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "http://api/api/v1/knowledge/reports/rgr_1"
    assert call["token"] == "tok"


# ------------------------- hit_rate 阈值判断 -------------------------


def test_extract_hit_rate_at_3_string_key():
    report = {"hit_at_k": {"1": 0.5, "3": 0.8, "5": 0.9}}
    assert eval_mod.extract_hit_rate_at_3(report) == 0.8


def test_extract_hit_rate_at_3_int_key():
    report = {"hit_at_k": {1: 0.5, 3: 0.7, 5: 0.9}}
    assert eval_mod.extract_hit_rate_at_3(report) == 0.7


def test_extract_hit_rate_at_3_missing_returns_zero():
    assert eval_mod.extract_hit_rate_at_3({}) == 0.0
    assert eval_mod.extract_hit_rate_at_3({"hit_at_k": {}}) == 0.0


def test_extract_hit_rate_at_3_invalid_value_returns_zero():
    assert eval_mod.extract_hit_rate_at_3({"hit_at_k": {"3": "not-a-number"}}) == 0.0


# ------------------------- run_eval 端到端流程 -------------------------


def _full_routes(hit_at_3: float) -> list:
    """构造一套完整路由：登录→创建金标集→添加用例→评测→报告详情。"""
    report = {
        "id": "rgr_1",
        "golden_set_id": "rgs_1",
        "status": "completed",
        "hit_at_k": {"1": hit_at_3, "3": hit_at_3, "5": hit_at_3},
        "avg_recall": 0.8,
        "avg_precision": 0.8,
        "avg_f1": 0.8,
        "total_cases": 10,
        "passed_cases": int(10 * hit_at_3),
    }
    return [
        ("POST", "/auth/login", {"access_token": "tok"}),
        ("POST", "/golden-sets", {"id": "rgs_1", "name": "CI 自动评测集"}),
        ("POST", "/cases", {"id": "rgc_x"}),
        ("POST", "/evaluate", report),
        ("GET", "/reports/", dict(report, by_case=[])),
    ]


def test_run_eval_success_returns_zero(monkeypatch, tmp_path):
    fake = FakeHTTP(routes=_full_routes(hit_at_3=0.9))
    monkeypatch.setattr(eval_mod, "_http_json", fake)
    report_path = str(tmp_path / "report.json")

    exit_code, report = eval_mod.run_eval(
        base_url="http://api", email="u", password="p", report_path=report_path
    )

    assert exit_code == 0
    assert report["id"] == "rgr_1"
    # 应当调用 1 次登录 + 1 次创建金标集 + 10 次添加用例 + 1 次评测 + 1 次报告详情 = 14
    assert len(fake.calls) == 14
    # 报告文件应当被写入
    assert os.path.exists(report_path)
    with open(report_path, encoding="utf-8") as fh:
        saved = json.load(fh)
    assert saved["id"] == "rgr_1"


def test_run_eval_low_hit_rate_returns_one(monkeypatch, tmp_path):
    fake = FakeHTTP(routes=_full_routes(hit_at_3=0.3))
    monkeypatch.setattr(eval_mod, "_http_json", fake)
    report_path = str(tmp_path / "report.json")

    exit_code, report = eval_mod.run_eval(
        base_url="http://api", email="u", password="p", report_path=report_path
    )

    assert exit_code == 1
    assert eval_mod.extract_hit_rate_at_3(report) == 0.3


def test_run_eval_login_failure_returns_one(monkeypatch, tmp_path):
    fake = FakeHTTP(routes=[("POST", "/auth/login", RuntimeError("HTTP 401"))])
    monkeypatch.setattr(eval_mod, "_http_json", fake)

    # main() 应捕获异常并返回 1
    exit_code = eval_mod.main()
    assert exit_code == 1


def test_run_eval_report_detail_failure_falls_back(monkeypatch, tmp_path):
    """报告详情 GET 失败时，应回退到评测返回的聚合报告，不中断流程。"""
    report = {
        "id": "rgr_1",
        "golden_set_id": "rgs_1",
        "status": "completed",
        "hit_at_k": {"1": 0.9, "3": 0.9, "5": 0.9},
        "avg_recall": 0.8,
        "avg_precision": 0.8,
        "avg_f1": 0.8,
        "total_cases": 10,
        "passed_cases": 9,
    }
    routes = _full_routes(0.9)
    # 把报告详情路由替换为抛错
    routes[-1] = ("GET", "/reports/", RuntimeError("HTTP 500"))
    fake = FakeHTTP(routes=routes)
    monkeypatch.setattr(eval_mod, "_http_json", fake)
    report_path = str(tmp_path / "report.json")

    exit_code, result = eval_mod.run_eval(
        base_url="http://api", email="u", password="p", report_path=report_path
    )

    # 回退到评测返回的聚合报告，hit_rate@3=0.9 仍然通过
    assert exit_code == 0
    assert result["id"] == "rgr_1"


# ------------------------- 报告导出与摘要 -------------------------


def test_save_report_writes_valid_json(tmp_path):
    report = {"id": "rgr_1", "hit_at_k": {"3": 0.8}}
    path = str(tmp_path / "out.json")

    eval_mod.save_report(report, path)

    with open(path, encoding="utf-8") as fh:
        loaded = json.load(fh)
    assert loaded == report


def test_format_summary_contains_metrics():
    report = {
        "id": "rgr_1",
        "status": "completed",
        "golden_set_id": "rgs_1",
        "hit_at_k": {"1": 0.5, "3": 0.8, "5": 0.9},
        "avg_recall": 0.7,
        "avg_precision": 0.6,
        "avg_f1": 0.65,
        "total_cases": 10,
        "passed_cases": 8,
    }
    summary = eval_mod.format_summary(report)

    assert "rgr_1" in summary
    assert "hit_rate@3" in summary
    assert "0.8000" in summary
    assert "avg_f1" in summary
    assert str(eval_mod.HIT_RATE_THRESHOLD) in summary


def test_main_returns_exit_code(monkeypatch, tmp_path):
    """main() 应返回 0/1 退出码，供 sys.exit 使用。"""
    fake = FakeHTTP(routes=_full_routes(hit_at_3=0.9))
    monkeypatch.setattr(eval_mod, "_http_json", fake)
    monkeypatch.setattr(eval_mod, "DEFAULT_REPORT_PATH", str(tmp_path / "report.json"))

    assert eval_mod.main() == 0
