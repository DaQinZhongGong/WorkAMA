"""RAG 评测集增强 v2 单元测试（T-M3-003-P2）。

覆盖范围：
- 评测结果对比端点（成功/不同 eval_set/404/403/workspace 隔离）
- 4 项指标计算的边界与组合
- 异步作业处理（process_kb_eval_job 的成功/失败/取消路径）
- 标注回流的单条与批量导入
- RRF 融合算法边界
- 聚合统计的数值稳定性

测试风格与 test_knowledge_eval.py 一致：内联 _Result/_SeqConnection/_Pool + monkeypatch。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from workama_platform.core import Actor
from workama_platform.modules import knowledge_eval as ke
from workama_platform.modules.jobs import ClaimedJob


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


def _actor(role="admin", capabilities=("dataset:write", "dataset:read"), workspace_id="wsp_test") -> Actor:
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


def _eval_set_row(**overrides) -> dict:
    base = {
        "id": "kbes_1",
        "dataset_id": "dts_1",
        "org_id": "org_test",
        "workspace_id": "wsp_test",
        "name": "smoke",
        "description": "",
        "metrics": ["retrieval_recall", "retrieval_precision", "answer_relevance", "context_precision"],
        "status": "active",
        "created_by": "usr_test",
        "created_at": "2026-07-28T10:00:00+00:00",
        "updated_at": "2026-07-28T10:00:00+00:00",
    }
    base.update(overrides)
    return base


def _run_row(**overrides) -> dict:
    base = {
        "id": "kerun_1",
        "eval_set_id": "kbes_1",
        "dataset_id": "dts_1",
        "generation_id": "gen_1",
        "org_id": "org_test",
        "workspace_id": "wsp_test",
        "operation_id": "op_1",
        "config": {"top_k": 5},
        "status": "completed",
        "metrics_summary": {
            "retrieval_recall": {"mean": 0.8, "median": 0.85, "p90": 1.0, "count": 10},
            "retrieval_precision": {"mean": 0.6, "median": 0.65, "p90": 0.9, "count": 10},
            "answer_relevance": {"mean": 0.75, "median": 0.8, "p90": 0.95, "count": 10},
            "context_precision": {"mean": 0.7, "median": 0.75, "p90": 0.92, "count": 10},
            "total_cases": 10,
            "error_cases": 0,
        },
        "error": None,
        "started_at": "2026-07-28T10:00:00+00:00",
        "completed_at": "2026-07-28T10:01:00+00:00",
        "created_at": "2026-07-28T10:00:00+00:00",
        "created_by": "usr_test",
    }
    base.update(overrides)
    return base


def _case_row(**overrides) -> dict:
    base = {
        "id": "kec_1",
        "eval_set_id": "kbes_1",
        "dataset_id": "dts_1",
        "org_id": "org_test",
        "workspace_id": "wsp_test",
        "question": "Q1",
        "expected_answer": "A1",
        "expected_chunks": ["chk_a"],
        "tags": [],
        "metadata": {},
        "case_hash": "hash_1",
        "status": "active",
        "created_by": "usr_test",
        "created_at": "2026-07-28T10:00:00+00:00",
    }
    base.update(overrides)
    return base


def _annotation_row(**overrides) -> dict:
    base = {
        "id": "kean_1",
        "run_id": "kerun_1",
        "case_id": "kec_1",
        "org_id": "org_test",
        "workspace_id": "wsp_test",
        "rating": 5,
        "feedback": "",
        "corrected_answer": "",
        "corrected_chunks": [],
        "labels": [],
        "created_by": "usr_test",
        "created_at": "2026-07-28T10:00:00+00:00",
    }
    base.update(overrides)
    return base


# --- 评测结果对比 ----------------------------------------------------------


@pytest.mark.asyncio
async def test_compare_eval_runs_returns_delta_and_pct(monkeypatch):
    baseline = _run_row(id="kerun_b")
    candidate = _run_row(
        id="kerun_c",
        metrics_summary={
            "retrieval_recall": {"mean": 0.9, "median": 0.95, "p90": 1.0, "count": 10},
            "retrieval_precision": {"mean": 0.5, "median": 0.55, "p90": 0.8, "count": 10},
            "answer_relevance": {"mean": 0.75, "median": 0.8, "p90": 0.95, "count": 10},
            "context_precision": {"mean": 0.7, "median": 0.75, "p90": 0.92, "count": 10},
            "total_cases": 10,
            "error_cases": 0,
        },
    )
    conn = _SeqConnection(
        results=[
            _Result(row=baseline),
            _Result(row=candidate),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.EvalRunCompareRequest(baseline_run_id="kerun_b", candidate_run_id="kerun_c")
    result = await ke.compare_eval_runs(body, _actor())

    assert result["baseline"]["id"] == "kerun_b"
    assert result["candidate"]["id"] == "kerun_c"
    comp = result["comparison"]
    assert comp["retrieval_recall"]["delta"] == pytest.approx(0.1)
    assert comp["retrieval_recall"]["delta_pct"] == pytest.approx(12.5)
    assert comp["retrieval_precision"]["delta"] == pytest.approx(-0.1)
    assert comp["retrieval_precision"]["delta_pct"] == pytest.approx(-16.6667, rel=1e-3)


@pytest.mark.asyncio
async def test_compare_eval_runs_rejects_different_eval_sets(monkeypatch):
    baseline = _run_row(id="kerun_b", eval_set_id="kbes_a")
    candidate = _run_row(id="kerun_c", eval_set_id="kbes_b")
    conn = _SeqConnection(
        results=[
            _Result(row=baseline),
            _Result(row=candidate),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.EvalRunCompareRequest(baseline_run_id="kerun_b", candidate_run_id="kerun_c")
    with pytest.raises(HTTPException) as exc:
        await ke.compare_eval_runs(body, _actor())
    assert exc.value.status_code == 422
    assert "same evaluation set" in exc.value.detail


@pytest.mark.asyncio
async def test_compare_eval_runs_returns_404_when_baseline_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.EvalRunCompareRequest(baseline_run_id="kerun_missing", candidate_run_id="kerun_c")
    with pytest.raises(HTTPException) as exc:
        await ke.compare_eval_runs(body, _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_compare_eval_runs_returns_403_without_capability():
    body = ke.EvalRunCompareRequest(baseline_run_id="kerun_b", candidate_run_id="kerun_c")
    actor = _actor(capabilities=("dataset:write",))
    with pytest.raises(HTTPException) as exc:
        await ke.compare_eval_runs(body, actor)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_compare_eval_runs_handles_missing_metrics_gracefully(monkeypatch):
    baseline = _run_row(id="kerun_b", metrics_summary={})
    candidate = _run_row(id="kerun_c", metrics_summary={})
    conn = _SeqConnection(results=[_Result(row=baseline), _Result(row=candidate)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.EvalRunCompareRequest(baseline_run_id="kerun_b", candidate_run_id="kerun_c")
    result = await ke.compare_eval_runs(body, _actor())
    assert result["comparison"] == {}


@pytest.mark.asyncio
async def test_compare_eval_runs_workspace_isolation_404(monkeypatch):
    baseline = _run_row(id="kerun_b", workspace_id="wsp_other")
    conn = _SeqConnection(results=[_Result(row=baseline)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.EvalRunCompareRequest(baseline_run_id="kerun_b", candidate_run_id="kerun_c")
    actor = _actor(workspace_id="wsp_test")
    with pytest.raises(HTTPException) as exc:
        await ke.compare_eval_runs(body, actor)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_compare_eval_runs_zero_baseline_avoids_division_error(monkeypatch):
    baseline = _run_row(
        id="kerun_b",
        metrics_summary={
            "retrieval_recall": {"mean": 0.0, "count": 1},
            "total_cases": 1,
        },
    )
    candidate = _run_row(
        id="kerun_c",
        metrics_summary={
            "retrieval_recall": {"mean": 0.1, "count": 1},
            "total_cases": 1,
        },
    )
    conn = _SeqConnection(results=[_Result(row=baseline), _Result(row=candidate)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.EvalRunCompareRequest(baseline_run_id="kerun_b", candidate_run_id="kerun_c")
    result = await ke.compare_eval_runs(body, _actor())
    comp = result["comparison"]["retrieval_recall"]
    assert comp["delta"] == pytest.approx(0.1)
    assert comp["delta_pct"] is None


# --- 指标计算边界 ----------------------------------------------------------


def test_retrieval_recall_with_duplicates_in_expected():
    assert ke.retrieval_recall(["chk_a"], ["chk_a", "chk_a"]) == 1.0


def test_retrieval_precision_with_duplicates_in_retrieved():
    assert ke.retrieval_precision(["chk_a", "chk_a", "chk_b"], ["chk_a"]) == pytest.approx(1 / 2)


def test_context_precision_with_more_expected_than_retrieved():
    score = ke.context_precision(["chk_a"], ["chk_a", "chk_b"])
    assert score == pytest.approx(1.0)


def test_context_precision_all_irrelevant():
    assert ke.context_precision(["chk_x", "chk_y"], ["chk_a"]) == 0.0


def test_answer_relevance_with_punctuation_only():
    assert ke.answer_relevance("hello", "!?,.") == 1.0


def test_answer_relevance_with_numbers():
    score = ke.answer_relevance("price is 42 dollars", "42 dollars")
    assert score > 0.0


def test_aggregate_metric_with_negative_values():
    result = ke.aggregate_metric([-0.5, 0.0, 0.5])
    assert result["mean"] == pytest.approx(0.0)
    assert result["median"] == pytest.approx(0.0)
    assert result["count"] == 3


def test_aggregate_metric_with_all_same_values():
    result = ke.aggregate_metric([0.7, 0.7, 0.7, 0.7])
    assert result["mean"] == pytest.approx(0.7)
    assert result["median"] == pytest.approx(0.7)
    assert result["p90"] == pytest.approx(0.7)


# --- RRF 融合算法 ----------------------------------------------------------


def test_fuse_rows_empty_inputs():
    assert ke._fuse_rows([], [], rrf_k=60, top_k=5) == []


def test_fuse_rows_keyword_only():
    keyword = [{"id": "a", "content": "A"}, {"id": "b", "content": "B"}]
    result = ke._fuse_rows(keyword, [], rrf_k=60, top_k=5)
    assert [r["id"] for r in result] == ["a", "b"]


def test_fuse_rows_deduplicates_across_sources():
    keyword = [{"id": "a", "content": "A"}]
    vector = [{"id": "a", "content": "A"}]
    result = ke._fuse_rows(keyword, vector, rrf_k=60, top_k=5)
    assert len(result) == 1
    assert result[0]["id"] == "a"


def test_fuse_rows_respects_top_k():
    keyword = [{"id": str(i)} for i in range(100)]
    result = ke._fuse_rows(keyword, [], rrf_k=60, top_k=5)
    assert len(result) == 5


# --- 综合指标计算 ----------------------------------------------------------


def test_compute_case_metrics_excludes_unsupported_metrics():
    metrics = ke.compute_case_metrics(
        retrieved_ids=["chk_a"],
        expected_ids=["chk_a"],
        generated_answer="hello",
        expected_answer="hello",
        metrics=["retrieval_recall", "unsupported_metric"],
    )
    assert "retrieval_recall" in metrics
    assert "unsupported_metric" not in metrics


def test_compute_case_metrics_with_empty_lists():
    metrics = ke.compute_case_metrics(
        retrieved_ids=[],
        expected_ids=[],
        generated_answer="",
        expected_answer="",
        metrics=["retrieval_recall", "retrieval_precision", "context_precision", "answer_relevance"],
    )
    assert metrics["retrieval_recall"] == 1.0
    assert metrics["retrieval_precision"] == 0.0
    assert metrics["context_precision"] == 0.0
    assert metrics["answer_relevance"] == 1.0


# --- 标注回流 --------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_annotation_with_corrected_chunks_and_labels(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_run_row()),
            _Result(row={"id": "kec_1"}),
            _Result(row=_annotation_row(corrected_chunks=["chk_c"], labels=["good"])),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.AnnotationCreate(
        case_id="kec_1", rating=4, feedback="ok", corrected_answer="B2", corrected_chunks=["chk_c"], labels=["good"]
    )
    result = await ke.create_annotation("kerun_1", body, _actor())
    assert result["corrected_chunks"] == ["chk_c"]
    assert result["labels"] == ["good"]


@pytest.mark.asyncio
async def test_import_annotations_all_duplicates_returns_zero_created(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_run_row()),
            _Result(row={"id": "kec_1"}),
            _Result(row={"id": "kean_existing"}),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.AnnotationImport(items=[ke.AnnotationCreate(case_id="kec_1", rating=3)])
    result = await ke.import_annotations("kerun_1", body, _actor())
    assert result["created"] == 0
    assert result["skipped"] == 1


# --- 异步作业处理 ----------------------------------------------------------


def _claimed_job(payload=None) -> ClaimedJob:
    return ClaimedJob(
        id="job_1",
        operation_id="op_1",
        workspace_id="wsp_test",
        job_type="kb.eval.run",
        payload=payload or {
            "run_id": "kerun_1",
            "eval_set_id": "kbes_1",
            "dataset_id": "dts_1",
            "generation_id": "gen_1",
            "config": {"top_k": 5, "candidate_k": 20, "rrf_k": 60, "score_threshold": 0.0},
            "metrics": ["retrieval_recall"],
            "actor_id": "usr_test",
        },
        attempt_count=1,
        max_attempts=2,
        lease_token="lease_1",
    )


@pytest.mark.asyncio
async def test_process_kb_eval_job_success_path_with_all_metrics(monkeypatch):
    run = _run_row(status="pending")
    dataset = {"id": "dts_1", "workspace_id": "wsp_test", "active_generation_id": "gen_1"}
    cases = [
        _case_row(id="kec_1", question="Q1", expected_chunks=["chk_a"], expected_answer="A1"),
        _case_row(id="kec_2", question="Q2", expected_chunks=["chk_b"], expected_answer="A2"),
    ]

    async def fake_retrieve_rows(dataset, workspace_id, question, candidate_k):
        return [{"id": "chk_a", "content": "A1"}], [{"id": "chk_b", "content": "A2"}]

    monkeypatch.setattr("workama_platform.modules.knowledge._retrieve_rows", fake_retrieve_rows)

    conn = _SeqConnection(
        results=[
            _Result(row=run),
            _Result(),
            _Result(row=dataset),
            _Result(rows=cases),
            _Result(row={"status": "running"}),  # _eval_not_cancelled case 1
            _Result(),
            _Result(),
            _Result(row={"status": "running"}),  # _eval_not_cancelled case 2
            _Result(),
            _Result(),
            _Result(),
            _Result(),
            _Result(),
            _Result(),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    job = _claimed_job(
        payload={
            "run_id": "kerun_1",
            "eval_set_id": "kbes_1",
            "dataset_id": "dts_1",
            "generation_id": "gen_1",
            "config": {"top_k": 5, "candidate_k": 20, "rrf_k": 60, "score_threshold": 0.0},
            "metrics": ["retrieval_recall", "retrieval_precision", "answer_relevance", "context_precision"],
            "actor_id": "usr_test",
        }
    )
    result = await ke.process_kb_eval_job(job)
    assert result["run_id"] == "kerun_1"
    metrics = result["metrics"]
    assert "retrieval_recall" in metrics
    assert metrics["retrieval_recall"]["count"] == 2


@pytest.mark.asyncio
async def test_process_kb_eval_job_cancellation_during_scoring(monkeypatch):
    run = _run_row(status="pending")
    dataset = {"id": "dts_1", "workspace_id": "wsp_test", "active_generation_id": "gen_1"}
    cases = [_case_row(id="kec_1", question="Q1")]

    async def fake_retrieve_rows(dataset, workspace_id, question, candidate_k):
        return [{"id": "chk_a", "content": "A1"}], []

    monkeypatch.setattr("workama_platform.modules.knowledge._retrieve_rows", fake_retrieve_rows)

    conn = _SeqConnection(
        results=[
            _Result(row=run),
            _Result(),
            _Result(row=dataset),
            _Result(rows=cases),
            _Result(),
            _Result(row={"status": "cancel_requested"}),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    job = _claimed_job()
    with pytest.raises(Exception, match="cancelled"):
        await ke.process_kb_eval_job(job)

    fail_q = next(
        (q for q, _ in conn.calls if "kb_eval_run" in q and "cancelled" in q),
        None,
    )
    assert fail_q is not None


# --- 边界校验 ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_annotations_returns_empty_when_none(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_run_row()),
            _Result(rows=[]),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.list_annotations("kerun_1", _actor(), limit=500)
    assert result["items"] == []


@pytest.mark.asyncio
async def test_get_eval_results_returns_empty_items(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_run_row()),
            _Result(rows=[]),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.get_eval_results("kerun_1", _actor(), limit=500)
    assert result["items"] == []


@pytest.mark.parametrize("values,p,expected", [
    ([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.9, 9.1),
    ([0.1, 0.2, 0.3], 0.5, 0.2),
    ([42.0], 0.5, 42.0),
])
def test_percentile_parametrized(values, p, expected):
    assert ke._percentile(values, p) == pytest.approx(expected)
