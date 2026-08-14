"""金标集 + 聚合报告 + 基线对比 + 导出 单元测试（T-M3-003 扩展）。

覆盖范围：
- 金标集 CRUD：创建 / 列表 / 详情 / dataset 过滤 / 分页 / workspace 隔离 / 不存在 404 / 鉴权 403
- 金标用例：添加 / expected_context_ids 存储 / tags 存储 / 不存在 404 / 鉴权 403
- 金标评测：执行成功 / report 生成 / hit_at_k 计算 / avg_recall / avg_precision / avg_f1 /
  total_cases / passed_cases / 状态 completed / 检索 mock 返回 / 空金标集拒绝 / 基线报告 /
  空 expected_context hit 为 false / baseline_report_id 记录
- 报告列表：空列表 / 分页 / 倒序 / workspace 隔离 / 不存在 404
- 报告详情：聚合指标完整 / by_case 明细 / 无基线 diff 为空 / 有基线计算差值 / 不存在 404
- 导出：json 格式 / csv 格式 / csv 含表头和明细行 / 非法格式 422 / 不存在 404
- 辅助函数：_golden_set_view / _golden_case_view / _report_view / _report_case_view /
  _f1_from_pr / _mock_retrieve_contexts
- Schema：SCHEMA_STATEMENTS 含 4 张表 / ensure_knowledge_eval_schema 执行全部语句

测试风格与 test_knowledge_eval.py 一致：内联 _Result/_SeqConnection/_Pool + monkeypatch。
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException, Response

from workama_platform.core import Actor
from workama_platform.modules import knowledge_eval as ke


# --- mock 基础设施 ---------------------------------------------------------


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []
        self.rowcount = len(self._rows)

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return list(self._rows)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _SeqConnection:
    """按调用顺序返回预设 Result，记录所有 execute 调用。"""

    def __init__(self, results=None):
        self._results = list(results) if results else []
        self.calls: list[tuple[str, tuple]] = []
        self._idx = 0

    def transaction(self):
        return _Transaction()

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if self._idx < len(self._results):
            r = self._results[self._idx]
            self._idx += 1
            return r
        return _Result()

    async def commit(self):
        return None


class _Pool:
    def __init__(self, connection):
        self._connection = connection

    def connection(self):
        connection = self._connection

        class _Context:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *_args):
                return False

        return _Context()


def _actor(
    role="admin",
    capabilities=("dataset:write", "dataset:read"),
    workspace_id="wsp_test",
) -> Actor:
    return Actor(
        user_id="usr_test",
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email="admin@example.test",
        display_name="Admin",
        onboarding_completed=True,
        capabilities=capabilities,
    )


def _golden_set_row(**overrides) -> dict:
    base = {
        "id": "rgs_1",
        "workspace_id": "wsp_test",
        "name": "golden-smoke",
        "description": "",
        "dataset_id": None,
        "created_by": "usr_test",
        "created_at": "2026-07-28T10:00:00+00:00",
        "updated_at": "2026-07-28T10:00:00+00:00",
    }
    base.update(overrides)
    return base


def _golden_case_row(**overrides) -> dict:
    base = {
        "id": "rgc_1",
        "golden_set_id": "rgs_1",
        "workspace_id": "wsp_test",
        "query": "Q1",
        "expected_answer": "",
        "expected_context_ids": ["chk_a"],
        "tags": ["smoke"],
        "created_at": "2026-07-28T10:00:00+00:00",
    }
    base.update(overrides)
    return base


def _report_row(**overrides) -> dict:
    base = {
        "id": "rgr_1",
        "workspace_id": "wsp_test",
        "golden_set_id": "rgs_1",
        "eval_run_id": None,
        "status": "completed",
        "hit_at_k": {"1": 0.5, "3": 0.5, "5": 0.5},
        "avg_recall": 0.5,
        "avg_precision": 0.5,
        "avg_f1": 0.5,
        "avg_faithfulness": 1.0,
        "avg_answer_relevance": 1.0,
        "total_cases": 2,
        "passed_cases": 1,
        "baseline_report_id": None,
        "summary": {},
        "created_at": "2026-07-28T10:00:00+00:00",
        "completed_at": "2026-07-28T10:00:00+00:00",
    }
    base.update(overrides)
    return base


def _report_case_row(**overrides) -> dict:
    base = {
        "id": "rgrc_1",
        "report_id": "rgr_1",
        "case_id": "rgc_1",
        "query": "Q1",
        "expected_answer": "",
        "actual_answer": "chk_a",
        "retrieved_context_ids": ["chk_a"],
        "expected_context_ids": ["chk_a"],
        "hit": True,
        "recall": 1.0,
        "precision": 1.0,
        "f1": 1.0,
        "faithfulness": 1.0,
        "answer_relevance": 1.0,
        "created_at": "2026-07-28T10:00:00+00:00",
    }
    base.update(overrides)
    return base


def _make_fake_retrieve(mapping: dict[str, list[str]]):
    """构造按 query 映射返回检索结果的 mock。"""
    async def fake_retrieve(query, workspace_id, top_k=5):
        return list(mapping.get(query, []))
    return fake_retrieve


# ============================================================================
# 金标集 CRUD
# ============================================================================


@pytest.mark.asyncio
async def test_create_golden_set_inserts_and_returns_view(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=_golden_set_row(dataset_id="dts_1"))])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.GoldenSetCreate(name="golden-smoke", dataset_id="dts_1")
    result = await ke.create_golden_set(body, _actor())

    assert result["id"] == "rgs_1"
    assert result["name"] == "golden-smoke"
    assert result["dataset_id"] == "dts_1"
    insert_q = next(q for q, _ in conn.calls if "INSERT INTO rag_golden_set" in q)
    assert "RETURNING *" in insert_q


@pytest.mark.asyncio
async def test_create_golden_set_with_optional_dataset_id_none(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=_golden_set_row(dataset_id=None))])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.GoldenSetCreate(name="no-dataset")
    result = await ke.create_golden_set(body, _actor())

    assert result["dataset_id"] is None


@pytest.mark.asyncio
async def test_create_golden_set_returns_403_without_capability():
    body = ke.GoldenSetCreate(name="x")
    actor = _actor(capabilities=("dataset:read",))
    with pytest.raises(HTTPException) as exc:
        await ke.create_golden_set(body, actor)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_list_golden_sets_filters_by_workspace(monkeypatch):
    conn = _SeqConnection(
        results=[_Result(rows=[_golden_set_row(), _golden_set_row(id="rgs_2")])]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.list_golden_sets(_actor(), limit=50)

    assert len(result["items"]) == 2
    select_q = next(q for q, _ in conn.calls if "SELECT * FROM rag_golden_set" in q)
    assert "workspace_id=%s" in select_q


@pytest.mark.asyncio
async def test_list_golden_sets_filters_by_dataset_id(monkeypatch):
    conn = _SeqConnection(results=[_Result(rows=[_golden_set_row(dataset_id="dts_1")])])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.list_golden_sets(_actor(), dataset_id="dts_1", limit=50)

    assert len(result["items"]) == 1
    select_q = next(q for q, _ in conn.calls if "SELECT * FROM rag_golden_set" in q)
    assert "dataset_id=%s" in select_q


@pytest.mark.asyncio
async def test_list_golden_sets_paginates_with_offset(monkeypatch):
    conn = _SeqConnection(results=[_Result(rows=[])] )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    await ke.list_golden_sets(_actor(), limit=10, offset=20)

    select_q = next(q for q, _ in conn.calls if "SELECT * FROM rag_golden_set" in q)
    assert "LIMIT %s OFFSET %s" in select_q


@pytest.mark.asyncio
async def test_get_golden_set_includes_cases_and_count(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),  # _golden_set
            _Result(rows=[_golden_case_row(), _golden_case_row(id="rgc_2")]),  # cases
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.get_golden_set("rgs_1", _actor())

    assert result["id"] == "rgs_1"
    assert result["case_count"] == 2
    assert len(result["cases"]) == 2


@pytest.mark.asyncio
async def test_get_golden_set_returns_404_when_not_found(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await ke.get_golden_set("rgs_missing", _actor())
    assert exc.value.status_code == 404
    assert "Golden set not found" in exc.value.detail


@pytest.mark.asyncio
async def test_get_golden_set_workspace_isolation_404(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await ke.get_golden_set("rgs_1", _actor(workspace_id="wsp_other"))
    assert exc.value.status_code == 404


# ============================================================================
# 金标用例管理
# ============================================================================


@pytest.mark.asyncio
async def test_create_golden_case_inserts_and_returns_view(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),  # _golden_set
            _Result(row=_golden_case_row()),  # INSERT RETURNING
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.GoldenCaseCreate(query="Q1", expected_context_ids=["chk_a"], tags=["smoke"])
    result = await ke.create_golden_case("rgs_1", body, _actor())

    assert result["id"] == "rgc_1"
    assert result["query"] == "Q1"
    # 应该更新 golden_set 的 updated_at
    assert any("UPDATE rag_golden_set SET updated_at=now()" in q for q, _ in conn.calls)


@pytest.mark.asyncio
async def test_create_golden_case_stores_expected_context_ids(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),
            _Result(row=_golden_case_row(expected_context_ids=["chk_a", "chk_b"])),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.GoldenCaseCreate(query="Q1", expected_context_ids=["chk_a", "chk_b"])
    result = await ke.create_golden_case("rgs_1", body, _actor())

    assert result["expected_context_ids"] == ["chk_a", "chk_b"]
    # 验证 INSERT 参数中包含 expected_context_ids
    insert_call = next((q, p) for q, p in conn.calls if "INSERT INTO rag_golden_case" in q)
    assert ["chk_a", "chk_b"] in insert_call[1]


@pytest.mark.asyncio
async def test_create_golden_case_stores_tags(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),
            _Result(row=_golden_case_row(tags=["prod", "regression"])),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.GoldenCaseCreate(query="Q1", tags=["prod", "regression"])
    result = await ke.create_golden_case("rgs_1", body, _actor())

    assert result["tags"] == ["prod", "regression"]


@pytest.mark.asyncio
async def test_create_golden_case_returns_404_when_set_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])  # _golden_set 404
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.GoldenCaseCreate(query="Q1")
    with pytest.raises(HTTPException) as exc:
        await ke.create_golden_case("rgs_missing", body, _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_create_golden_case_returns_403_without_capability():
    body = ke.GoldenCaseCreate(query="Q1")
    actor = _actor(capabilities=("dataset:read",))
    with pytest.raises(HTTPException) as exc:
        await ke.create_golden_case("rgs_1", body, actor)
    assert exc.value.status_code == 403


# ============================================================================
# 金标评测
# ============================================================================


@pytest.mark.asyncio
async def test_evaluate_golden_set_success_returns_completed_report(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),  # _golden_set
            _Result(rows=[_golden_case_row()]),  # cases
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))
    monkeypatch.setattr(ke, "_mock_retrieve_contexts", _make_fake_retrieve({"Q1": ["chk_a"]}))

    body = ke.GoldenEvalRequest()
    result = await ke.evaluate_golden_set("rgs_1", body, _actor())

    assert result["status"] == "completed"
    assert result["golden_set_id"] == "rgs_1"
    assert result["total_cases"] == 1


@pytest.mark.asyncio
async def test_evaluate_golden_set_rejects_empty_cases(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),  # _golden_set
            _Result(rows=[]),  # 空用例
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.GoldenEvalRequest()
    with pytest.raises(HTTPException) as exc:
        await ke.evaluate_golden_set("rgs_1", body, _actor())
    assert exc.value.status_code == 422
    assert "no cases" in exc.value.detail


@pytest.mark.asyncio
async def test_evaluate_golden_set_returns_404_when_set_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.GoldenEvalRequest()
    with pytest.raises(HTTPException) as exc:
        await ke.evaluate_golden_set("rgs_missing", body, _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_evaluate_golden_set_computes_hit_at_k(monkeypatch):
    # 2 个用例：Q1 命中（chk_a 在 top1），Q2 未命中
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),
            _Result(rows=[
                _golden_case_row(id="rgc_1", query="Q1", expected_context_ids=["chk_a"]),
                _golden_case_row(id="rgc_2", query="Q2", expected_context_ids=["chk_b"]),
            ]),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))
    monkeypatch.setattr(
        ke, "_mock_retrieve_contexts",
        _make_fake_retrieve({"Q1": ["chk_a", "x", "y"], "Q2": ["x", "y", "z"]}),
    )

    body = ke.GoldenEvalRequest()
    result = await ke.evaluate_golden_set("rgs_1", body, _actor())

    # hit_at_k: Q1 命中所有 k，Q2 全未命中 → 0.5
    assert result["hit_at_k"]["1"] == 0.5
    assert result["hit_at_k"]["3"] == 0.5
    assert result["hit_at_k"]["5"] == 0.5
    # 验证 UPDATE 语句写入的 hit_at_k JSON
    update_call = next(
        (q, p) for q, p in conn.calls
        if "UPDATE rag_eval_report" in q and "status='completed'" in q
    )
    stored_hit = json.loads(update_call[1][0])
    assert stored_hit["1"] == 0.5


@pytest.mark.asyncio
async def test_evaluate_golden_set_computes_avg_recall(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),
            _Result(rows=[
                _golden_case_row(id="rgc_1", query="Q1", expected_context_ids=["chk_a"]),
                _golden_case_row(id="rgc_2", query="Q2", expected_context_ids=["chk_b"]),
            ]),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))
    monkeypatch.setattr(
        ke, "_mock_retrieve_contexts",
        _make_fake_retrieve({"Q1": ["chk_a"], "Q2": ["x", "y"]}),
    )

    body = ke.GoldenEvalRequest()
    result = await ke.evaluate_golden_set("rgs_1", body, _actor())

    # Q1 recall=1.0, Q2 recall=0.0 → avg=0.5
    assert result["avg_recall"] == 0.5


@pytest.mark.asyncio
async def test_evaluate_golden_set_computes_avg_precision(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),
            _Result(rows=[
                _golden_case_row(id="rgc_1", query="Q1", expected_context_ids=["chk_a"]),
                _golden_case_row(id="rgc_2", query="Q2", expected_context_ids=["chk_b"]),
            ]),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))
    monkeypatch.setattr(
        ke, "_mock_retrieve_contexts",
        _make_fake_retrieve({"Q1": ["chk_a"], "Q2": ["x", "y"]}),
    )

    body = ke.GoldenEvalRequest()
    result = await ke.evaluate_golden_set("rgs_1", body, _actor())

    # Q1 precision=1.0 (1/1), Q2 precision=0.0 → avg=0.5
    assert result["avg_precision"] == 0.5


@pytest.mark.asyncio
async def test_evaluate_golden_set_computes_avg_f1(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),
            _Result(rows=[
                _golden_case_row(id="rgc_1", query="Q1", expected_context_ids=["chk_a"]),
                _golden_case_row(id="rgc_2", query="Q2", expected_context_ids=["chk_b"]),
            ]),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))
    monkeypatch.setattr(
        ke, "_mock_retrieve_contexts",
        _make_fake_retrieve({"Q1": ["chk_a"], "Q2": ["x", "y"]}),
    )

    body = ke.GoldenEvalRequest()
    result = await ke.evaluate_golden_set("rgs_1", body, _actor())

    # Q1 f1=1.0, Q2 f1=0.0 → avg=0.5
    assert result["avg_f1"] == 0.5


@pytest.mark.asyncio
async def test_evaluate_golden_set_total_and_passed_cases(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),
            _Result(rows=[
                _golden_case_row(id="rgc_1", query="Q1", expected_context_ids=["chk_a"]),
                _golden_case_row(id="rgc_2", query="Q2", expected_context_ids=["chk_b"]),
            ]),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))
    monkeypatch.setattr(
        ke, "_mock_retrieve_contexts",
        _make_fake_retrieve({"Q1": ["chk_a"], "Q2": ["x", "y"]}),
    )

    body = ke.GoldenEvalRequest()
    result = await ke.evaluate_golden_set("rgs_1", body, _actor())

    assert result["total_cases"] == 2
    assert result["passed_cases"] == 1  # 仅 Q1 hit@5


@pytest.mark.asyncio
async def test_evaluate_golden_set_uses_mock_retrieve(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),
            _Result(rows=[_golden_case_row(query="Q1")]),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    call_log: list[str] = []

    async def recording_retrieve(query, workspace_id, top_k=5):
        call_log.append(query)
        return ["chk_a"]

    monkeypatch.setattr(ke, "_mock_retrieve_contexts", recording_retrieve)

    body = ke.GoldenEvalRequest()
    await ke.evaluate_golden_set("rgs_1", body, _actor())

    assert call_log == ["Q1"]


@pytest.mark.asyncio
async def test_evaluate_golden_set_with_baseline_report(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),  # _golden_set
            _Result(rows=[_golden_case_row()]),  # cases
            _Result(row=_report_row(id="rgr_base")),  # baseline 校验
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))
    monkeypatch.setattr(ke, "_mock_retrieve_contexts", _make_fake_retrieve({"Q1": ["chk_a"]}))

    body = ke.GoldenEvalRequest(baseline_report_id="rgr_base")
    result = await ke.evaluate_golden_set("rgs_1", body, _actor())

    assert result["baseline_report_id"] == "rgr_base"
    # INSERT 报告时应传入 baseline_report_id
    insert_call = next((q, p) for q, p in conn.calls if "INSERT INTO rag_eval_report" in q)
    assert "rgr_base" in insert_call[1]


@pytest.mark.asyncio
async def test_evaluate_golden_set_baseline_not_found_404(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),  # _golden_set
            _Result(rows=[_golden_case_row()]),  # cases
            _Result(row=None),  # baseline 不存在
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.GoldenEvalRequest(baseline_report_id="rgr_missing")
    with pytest.raises(HTTPException) as exc:
        await ke.evaluate_golden_set("rgs_1", body, _actor())
    assert exc.value.status_code == 404
    assert "Baseline report not found" in exc.value.detail


@pytest.mark.asyncio
async def test_evaluate_golden_set_empty_expected_context_hit_false(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),
            _Result(rows=[
                _golden_case_row(id="rgc_1", query="Q1", expected_context_ids=[]),
            ]),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))
    monkeypatch.setattr(ke, "_mock_retrieve_contexts", _make_fake_retrieve({"Q1": ["chk_a"]}))

    body = ke.GoldenEvalRequest()
    result = await ke.evaluate_golden_set("rgs_1", body, _actor())

    # 空 expected_context_ids → hit 全为 false → passed=0
    assert result["passed_cases"] == 0
    assert result["hit_at_k"]["5"] == 0.0


@pytest.mark.asyncio
async def test_evaluate_golden_set_returns_403_without_capability():
    body = ke.GoldenEvalRequest()
    actor = _actor(capabilities=("dataset:read",))
    with pytest.raises(HTTPException) as exc:
        await ke.evaluate_golden_set("rgs_1", body, actor)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_evaluate_golden_set_records_eval_set_id(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),
            _Result(rows=[_golden_case_row()]),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))
    monkeypatch.setattr(ke, "_mock_retrieve_contexts", _make_fake_retrieve({"Q1": ["chk_a"]}))

    body = ke.GoldenEvalRequest(eval_set_id="kbes_1")
    result = await ke.evaluate_golden_set("rgs_1", body, _actor())

    assert result["eval_run_id"] == "kbes_1"


# ============================================================================
# 报告列表
# ============================================================================


@pytest.mark.asyncio
async def test_list_golden_reports_empty(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),  # _golden_set
            _Result(rows=[]),  # 空报告列表
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.list_golden_reports("rgs_1", _actor())

    assert result["items"] == []


@pytest.mark.asyncio
async def test_list_golden_reports_paginates(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),
            _Result(rows=[_report_row()]),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    await ke.list_golden_reports("rgs_1", _actor(), limit=10, offset=5)

    select_q = next(q for q, _ in conn.calls if "SELECT * FROM rag_eval_report" in q)
    assert "LIMIT %s OFFSET %s" in select_q


@pytest.mark.asyncio
async def test_list_golden_reports_ordered_desc(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),
            _Result(rows=[_report_row()]),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    await ke.list_golden_reports("rgs_1", _actor())

    select_q = next(q for q, _ in conn.calls if "SELECT * FROM rag_eval_report" in q)
    assert "ORDER BY created_at DESC" in select_q


@pytest.mark.asyncio
async def test_list_golden_reports_workspace_isolation(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),
            _Result(rows=[]),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    await ke.list_golden_reports("rgs_1", _actor(workspace_id="wsp_other"))

    select_q = next(q for q, _ in conn.calls if "SELECT * FROM rag_eval_report" in q)
    assert "workspace_id=%s" in select_q


@pytest.mark.asyncio
async def test_list_golden_reports_returns_404_when_set_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await ke.list_golden_reports("rgs_missing", _actor())
    assert exc.value.status_code == 404


# ============================================================================
# 报告详情
# ============================================================================


@pytest.mark.asyncio
async def test_get_report_returns_aggregates(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_report_row()),  # _golden_report
            _Result(rows=[_report_case_row()]),  # cases
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.get_report("rgr_1", _actor())

    assert result["id"] == "rgr_1"
    assert result["avg_recall"] == 0.5
    assert result["avg_precision"] == 0.5
    assert result["avg_f1"] == 0.5
    assert result["avg_faithfulness"] == 1.0
    assert result["avg_answer_relevance"] == 1.0
    assert result["total_cases"] == 2
    assert result["passed_cases"] == 1
    assert result["hit_at_k"] == {"1": 0.5, "3": 0.5, "5": 0.5}


@pytest.mark.asyncio
async def test_get_report_returns_by_case_detail(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_report_row()),
            _Result(rows=[_report_case_row(), _report_case_row(id="rgrc_2", case_id="rgc_2")]),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.get_report("rgr_1", _actor())

    assert len(result["by_case"]) == 2
    assert result["by_case"][0]["case_id"] == "rgc_1"
    assert result["by_case"][0]["retrieved_context_ids"] == ["chk_a"]
    assert result["by_case"][0]["hit"] is True


@pytest.mark.asyncio
async def test_get_report_baseline_diff_empty_without_baseline(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_report_row(baseline_report_id=None)),
            _Result(rows=[]),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.get_report("rgr_1", _actor())

    assert result["baseline_diff"] == {}


@pytest.mark.asyncio
async def test_get_report_baseline_diff_with_baseline(monkeypatch):
    current = _report_row(
        id="rgr_cur",
        baseline_report_id="rgr_base",
        avg_recall=0.8,
        avg_precision=0.7,
        avg_f1=0.75,
        avg_faithfulness=1.0,
        avg_answer_relevance=0.9,
        hit_at_k={"1": 0.6, "3": 0.8, "5": 0.9},
        total_cases=2,
        passed_cases=2,
    )
    baseline = _report_row(
        id="rgr_base",
        avg_recall=0.5,
        avg_precision=0.5,
        avg_f1=0.5,
        avg_faithfulness=1.0,
        avg_answer_relevance=1.0,
        hit_at_k={"1": 0.5, "3": 0.5, "5": 0.5},
        total_cases=2,
        passed_cases=1,
    )
    conn = _SeqConnection(
        results=[
            _Result(row=current),  # _golden_report
            _Result(rows=[_report_case_row()]),  # cases
            _Result(row=baseline),  # baseline SELECT
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.get_report("rgr_cur", _actor())

    diff = result["baseline_diff"]
    assert diff["avg_recall"] == round(0.8 - 0.5, 6)
    assert diff["avg_precision"] == round(0.7 - 0.5, 6)
    assert diff["avg_f1"] == round(0.75 - 0.5, 6)
    assert diff["hit_at_1"] == round(0.6 - 0.5, 6)
    assert diff["hit_at_5"] == round(0.9 - 0.5, 6)
    assert diff["total_cases"] == 0
    assert diff["passed_cases"] == 1


@pytest.mark.asyncio
async def test_get_report_returns_404_when_not_found(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await ke.get_report("rgr_missing", _actor())
    assert exc.value.status_code == 404
    assert "Evaluation report not found" in exc.value.detail


# ============================================================================
# 导出
# ============================================================================


@pytest.mark.asyncio
async def test_export_report_json_format(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_report_row()),
            _Result(rows=[_report_case_row()]),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    response = await ke.export_report("rgr_1", _actor(), format="json")

    assert isinstance(response, Response)
    assert response.media_type == "application/json"
    payload = json.loads(response.body.decode())
    assert payload["id"] == "rgr_1"
    assert len(payload["by_case"]) == 1
    assert "attachment" in response.headers.get("content-disposition", "")


@pytest.mark.asyncio
async def test_export_report_csv_format(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_report_row()),
            _Result(rows=[_report_case_row()]),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    response = await ke.export_report("rgr_1", _actor(), format="csv")

    assert response.media_type == "text/csv"
    assert "attachment" in response.headers.get("content-disposition", "")
    content = response.body.decode()
    assert "case_id" in content


@pytest.mark.asyncio
async def test_export_report_csv_has_header_and_rows(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_report_row()),
            _Result(rows=[
                _report_case_row(case_id="rgc_1", query="Q1"),
                _report_case_row(id="rgrc_2", case_id="rgc_2", query="Q2"),
            ]),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    response = await ke.export_report("rgr_1", _actor(), format="csv")
    content = response.body.decode()
    lines = content.strip().splitlines()

    # 表头 + 2 行明细
    assert len(lines) == 3
    assert lines[0].startswith("case_id,query,expected_answer,actual_answer")
    assert "rgc_1" in lines[1]
    assert "rgc_2" in lines[2]


@pytest.mark.asyncio
async def test_export_report_rejects_invalid_format(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_report_row()),
            _Result(rows=[]),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await ke.export_report("rgr_1", _actor(), format="xml")
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_export_report_returns_404_when_not_found(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await ke.export_report("rgr_missing", _actor(), format="json")
    assert exc.value.status_code == 404


# ============================================================================
# 辅助函数视图
# ============================================================================


def test_golden_set_view_fields():
    view = ke._golden_set_view(_golden_set_row(dataset_id="dts_1"))
    assert view["id"] == "rgs_1"
    assert view["workspace_id"] == "wsp_test"
    assert view["name"] == "golden-smoke"
    assert view["dataset_id"] == "dts_1"
    assert view["created_by"] == "usr_test"
    assert "created_at" in view
    assert "updated_at" in view


def test_golden_case_view_fields():
    row = _golden_case_row(expected_context_ids=["a", "b"], tags=["t1"])
    view = ke._golden_case_view(row)
    assert view["id"] == "rgc_1"
    assert view["golden_set_id"] == "rgs_1"
    assert view["query"] == "Q1"
    assert view["expected_answer"] == ""
    assert view["expected_context_ids"] == ["a", "b"]
    assert view["tags"] == ["t1"]


def test_report_view_fields():
    view = ke._report_view(_report_row())
    assert view["id"] == "rgr_1"
    assert view["status"] == "completed"
    assert view["hit_at_k"] == {"1": 0.5, "3": 0.5, "5": 0.5}
    assert view["avg_recall"] == 0.5
    assert view["total_cases"] == 2
    assert view["passed_cases"] == 1
    assert view["eval_run_id"] is None
    assert view["baseline_report_id"] is None


def test_report_view_handles_null_optionals():
    row = _report_row()
    row["eval_run_id"] = None
    row["baseline_report_id"] = None
    row["hit_at_k"] = None
    row["summary"] = None
    row["completed_at"] = None
    view = ke._report_view(row)
    assert view["eval_run_id"] is None
    assert view["hit_at_k"] == {}
    assert view["summary"] == {}
    assert view["completed_at"] is None


def test_report_case_view_fields():
    row = _report_case_row(
        retrieved_context_ids=["chk_a", "chk_b"],
        expected_context_ids=["chk_a"],
        hit=True,
        recall=1.0,
        precision=0.5,
        f1=0.6666666666666666,
    )
    view = ke._report_case_view(row)
    assert view["case_id"] == "rgc_1"
    assert view["retrieved_context_ids"] == ["chk_a", "chk_b"]
    assert view["expected_context_ids"] == ["chk_a"]
    assert view["hit"] is True
    assert view["recall"] == 1.0
    assert view["f1"] == 0.6666666666666666


def test_f1_from_pr_returns_zero_when_sum_zero():
    assert ke._f1_from_pr(0.0, 0.0) == 0.0


def test_f1_from_pr_computes_harmonic_mean():
    # recall=1.0, precision=0.5 → f1 = 2*1*0.5/1.5 = 0.666...
    f1 = ke._f1_from_pr(1.0, 0.5)
    assert abs(f1 - (2 * 1.0 * 0.5 / 1.5)) < 1e-9


@pytest.mark.asyncio
async def test_mock_retrieve_contexts_is_deterministic():
    a = await ke._mock_retrieve_contexts("Q1", "wsp_test", top_k=5)
    b = await ke._mock_retrieve_contexts("Q1", "wsp_test", top_k=5)
    assert a == b
    assert len(a) > 0


@pytest.mark.asyncio
async def test_mock_retrieve_contexts_respects_top_k():
    result = await ke._mock_retrieve_contexts("Q1", "wsp_test", top_k=2)
    assert len(result) <= 2


# ============================================================================
# Schema
# ============================================================================


def test_schema_statements_contains_golden_tables():
    statements = " ".join(ke.SCHEMA_STATEMENTS)
    assert "CREATE TABLE IF NOT EXISTS rag_golden_set" in statements
    assert "CREATE TABLE IF NOT EXISTS rag_golden_case" in statements
    assert "CREATE TABLE IF NOT EXISTS rag_eval_report" in statements
    assert "CREATE TABLE IF NOT EXISTS rag_eval_report_case" in statements


def test_schema_statements_has_workspace_index():
    statements = " ".join(ke.SCHEMA_STATEMENTS)
    assert "idx_rag_golden_set_workspace" in statements
    assert "idx_rag_eval_report_workspace" in statements
    assert "idx_rag_golden_case_set" in statements
    assert "idx_rag_eval_report_case_report" in statements


@pytest.mark.asyncio
async def test_ensure_knowledge_eval_schema_executes_all_statements(monkeypatch):
    executed: list[str] = []

    class _SchemaConn:
        async def execute(self, query, params=()):
            executed.append(query)

    await ke.ensure_knowledge_eval_schema(_SchemaConn())
    assert len(executed) == len(ke.SCHEMA_STATEMENTS)
