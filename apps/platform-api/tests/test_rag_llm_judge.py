"""RAG 评测集 LLM-as-judge 单元测试。

覆盖范围：
- LLM judge 函数（成功 / JSON 解析 / code fence 剥离 / 超时回退 / 5xx 回退）
- _call_llm_judge 边界（score 越界钳制 / 缺失 score 键）
- EvalRunCreate 模型 use_llm_judge 字段
- _process_run 启用与禁用 LLM judge 路径
- POST /eval-runs/{run_id}/llm-judge 端点（成功 / 404 / 403 / workspace 隔离 / 幂等冲突）
- _process_llm_judge 异步作业（更新指标 / 聚合摘要 / 取消检查 / 保留已有指标 / 空结果 / 单条失败继续）

测试风格与 test_rag_evaluation_v2.py 一致：内联 _Result/_SeqConnection/_Pool + monkeypatch。
"""
from __future__ import annotations

import json
import pytest
from fastapi import HTTPException

from workama_platform.core import Actor
from workama_platform.modules import knowledge_eval as ke
from workama_platform.modules.jobs import ClaimedJob, IdempotencyConflict


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


def _result_row(**overrides) -> dict:
    base = {
        "id": "keres_1",
        "run_id": "kerun_1",
        "case_id": "kec_1",
        "workspace_id": "wsp_test",
        "question": "Q1",
        "retrieved_chunks": ["chk_a"],
        "generated_answer": "A1 content",
        "metrics": {"retrieval_recall": 1.0},
        "latency_ms": 100,
        "error": None,
        "created_at": "2026-07-28T10:00:00+00:00",
    }
    base.update(overrides)
    return base


def _mock_llm_resp(content: str) -> dict:
    return {"content": content, "tokens_used": 10, "model": "gpt-4o-mini", "method": "llm"}


# --- _call_llm_judge 与底层函数 ------------------------------------------


@pytest.mark.asyncio
async def test_judge_faithfulness_success(monkeypatch):
    async def fake_call_llm(*args, **kwargs):
        return _mock_llm_resp('{"score": 0.85, "reason": "supported"}')

    monkeypatch.setattr(ke.llm_client, "call_llm", fake_call_llm)
    score = await ke._judge_faithfulness("q", "a", ["ctx"], _actor())
    assert score == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_judge_faithfulness_strips_code_fence(monkeypatch):
    async def fake_call_llm(*args, **kwargs):
        return _mock_llm_resp('```json\n{"score": 0.92, "reason": "ok"}\n```')

    monkeypatch.setattr(ke.llm_client, "call_llm", fake_call_llm)
    score = await ke._judge_faithfulness("q", "a", ["ctx"], _actor())
    assert score == pytest.approx(0.92)


@pytest.mark.asyncio
async def test_judge_faithfulness_fallback_on_invalid_json(monkeypatch):
    async def fake_call_llm(*args, **kwargs):
        return _mock_llm_resp("not json at all")

    monkeypatch.setattr(ke.llm_client, "call_llm", fake_call_llm)
    score = await ke._judge_faithfulness("q", "a", ["ctx"], _actor())
    assert score == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_judge_faithfulness_fallback_on_mock_response(monkeypatch):
    async def fake_call_llm(*args, **kwargs):
        return _mock_llm_resp("[mock-llm] system=eval | model=gpt-4o-mini | user=q")

    monkeypatch.setattr(ke.llm_client, "call_llm", fake_call_llm)
    score = await ke._judge_faithfulness("q", "a", ["ctx"], _actor())
    assert score == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_judge_answer_relevance_llm_success(monkeypatch):
    async def fake_call_llm(*args, **kwargs):
        return _mock_llm_resp('{"score": 0.75, "reason": "relevant"}')

    monkeypatch.setattr(ke.llm_client, "call_llm", fake_call_llm)
    score = await ke._judge_answer_relevance_llm("q", "a", _actor())
    assert score == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_judge_answer_relevance_llm_fallback_uses_deterministic(monkeypatch):
    async def fake_call_llm(*args, **kwargs):
        return _mock_llm_resp("bad json")

    monkeypatch.setattr(ke.llm_client, "call_llm", fake_call_llm)
    score = await ke._judge_answer_relevance_llm("hello world", "hello", _actor())
    expected_fallback = ke.answer_relevance("hello", "hello world")
    assert score == pytest.approx(expected_fallback)


@pytest.mark.asyncio
async def test_call_llm_judge_clamps_score_above_one(monkeypatch):
    async def fake_call_llm(*args, **kwargs):
        return _mock_llm_resp('{"score": 1.5}')

    monkeypatch.setattr(ke.llm_client, "call_llm", fake_call_llm)
    score = await ke._call_llm_judge([{"role": "user", "content": "x"}], _actor(), fallback_value=0.0)
    assert score == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_call_llm_judge_clamps_score_below_zero(monkeypatch):
    async def fake_call_llm(*args, **kwargs):
        return _mock_llm_resp('{"score": -0.3}')

    monkeypatch.setattr(ke.llm_client, "call_llm", fake_call_llm)
    score = await ke._call_llm_judge([{"role": "user", "content": "x"}], _actor(), fallback_value=0.5)
    assert score == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_call_llm_judge_fallback_when_score_key_missing(monkeypatch):
    async def fake_call_llm(*args, **kwargs):
        return _mock_llm_resp('{"reason": "missing score"}')

    monkeypatch.setattr(ke.llm_client, "call_llm", fake_call_llm)
    score = await ke._call_llm_judge([{"role": "user", "content": "x"}], _actor(), fallback_value=0.42)
    assert score == pytest.approx(0.42)


# --- 模型与配置 -----------------------------------------------------------


def test_eval_run_create_default_use_llm_judge_false():
    body = ke.EvalRunCreate()
    assert body.use_llm_judge is False


def test_eval_run_create_allows_use_llm_judge_true():
    body = ke.EvalRunCreate(use_llm_judge=True)
    assert body.use_llm_judge is True


# --- _process_run 集成路径 ------------------------------------------------


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
            "config": {"top_k": 5, "candidate_k": 20, "rrf_k": 60, "score_threshold": 0.0, "use_llm_judge": True},
            "metrics": ["retrieval_recall", "answer_relevance"],
            "actor_id": "usr_test",
            "org_id": "org_test",
        },
        attempt_count=1,
        max_attempts=2,
        lease_token="lease_1",
    )


@pytest.mark.asyncio
async def test_process_run_with_use_llm_judge_enabled(monkeypatch):
    run = _run_row(status="pending")
    dataset = {"id": "dts_1", "workspace_id": "wsp_test", "active_generation_id": "gen_1"}
    cases = [
        {"id": "kec_1", "question": "Q1", "expected_chunks": ["chk_a"], "expected_answer": "A1"},
    ]

    async def fake_retrieve_rows(dataset, workspace_id, question, candidate_k):
        return [{"id": "chk_a", "content": "A1"}], []

    monkeypatch.setattr("workama_platform.modules.knowledge._retrieve_rows", fake_retrieve_rows)

    llm_calls = []

    async def fake_call_llm(*args, **kwargs):
        llm_calls.append(args)
        return _mock_llm_resp('{"score": 0.88, "reason": "ok"}')

    monkeypatch.setattr(ke.llm_client, "call_llm", fake_call_llm)

    conn = _SeqConnection(
        results=[
            _Result(row=run),
            _Result(),
            _Result(row=dataset),
            _Result(rows=cases),
            _Result(row={"status": "running"}),
            _Result(),
            _Result(),
            _Result(),
            _Result(),
            _Result(),
            _Result(),
            _Result(),
            _Result(),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    job = _claimed_job()
    result = await ke.process_kb_eval_job(job)
    assert result["run_id"] == "kerun_1"
    metrics = result["metrics"]
    assert "faithfulness" in metrics
    assert metrics["faithfulness"]["mean"] == pytest.approx(0.88)
    assert metrics["answer_relevance"]["mean"] == pytest.approx(0.88)
    assert len(llm_calls) == 2  # faithfulness + answer_relevance


@pytest.mark.asyncio
async def test_process_run_without_use_llm_judge_skips_llm(monkeypatch):
    run = _run_row(status="pending")
    dataset = {"id": "dts_1", "workspace_id": "wsp_test", "active_generation_id": "gen_1"}
    cases = [
        {"id": "kec_1", "question": "Q1", "expected_chunks": ["chk_a"], "expected_answer": "A1"},
    ]

    async def fake_retrieve_rows(dataset, workspace_id, question, candidate_k):
        return [{"id": "chk_a", "content": "A1"}], []

    monkeypatch.setattr("workama_platform.modules.knowledge._retrieve_rows", fake_retrieve_rows)

    llm_calls = []

    async def fake_call_llm(*args, **kwargs):
        llm_calls.append(args)
        return _mock_llm_resp('{"score": 0.99}')

    monkeypatch.setattr(ke.llm_client, "call_llm", fake_call_llm)

    conn = _SeqConnection(
        results=[
            _Result(row=run),
            _Result(),
            _Result(row=dataset),
            _Result(rows=cases),
            _Result(row={"status": "running"}),
            _Result(),
            _Result(),
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
            "config": {"top_k": 5, "candidate_k": 20, "rrf_k": 60, "score_threshold": 0.0, "use_llm_judge": False},
            "metrics": ["retrieval_recall"],
            "actor_id": "usr_test",
            "org_id": "org_test",
        }
    )
    result = await ke.process_kb_eval_job(job)
    assert result["run_id"] == "kerun_1"
    assert "faithfulness" not in result["metrics"]
    assert len(llm_calls) == 0


# --- 端点测试 --------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_llm_judge_endpoint_returns_operation_id(monkeypatch):
    async def fake_submit_operation(*args, **kwargs):
        return {"id": "op_new"}

    monkeypatch.setattr(ke, "submit_operation", fake_submit_operation)
    conn = _SeqConnection(results=[_Result(row=_run_row())])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.batch_llm_judge("kerun_1", _actor())
    assert result["operation_id"] == "op_new"


@pytest.mark.asyncio
async def test_batch_llm_judge_endpoint_404_when_run_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await ke.batch_llm_judge("kerun_missing", _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_batch_llm_judge_endpoint_403_without_capability():
    actor = _actor(capabilities=("dataset:read",))
    with pytest.raises(HTTPException) as exc:
        await ke.batch_llm_judge("kerun_1", actor)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_batch_llm_judge_workspace_isolation_404(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    actor = _actor(workspace_id="wsp_other")
    with pytest.raises(HTTPException) as exc:
        await ke.batch_llm_judge("kerun_1", actor)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_batch_llm_judge_idempotency_conflict(monkeypatch):
    async def fake_submit_operation(*args, **kwargs):
        raise IdempotencyConflict("dup")

    monkeypatch.setattr(ke, "submit_operation", fake_submit_operation)
    conn = _SeqConnection(results=[_Result(row=_run_row())])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await ke.batch_llm_judge("kerun_1", _actor())
    assert exc.value.status_code == 409


# --- _process_llm_judge 异步作业 ------------------------------------------


def _llm_judge_claimed_job(payload=None) -> ClaimedJob:
    return ClaimedJob(
        id="job_2",
        operation_id="op_2",
        workspace_id="wsp_test",
        job_type="kb.eval.llm_judge",
        payload=payload or {
            "run_id": "kerun_1",
            "eval_set_id": "kbes_1",
            "dataset_id": "dts_1",
            "actor_id": "usr_test",
            "org_id": "org_test",
        },
        attempt_count=1,
        max_attempts=2,
        lease_token="lease_2",
    )


@pytest.mark.asyncio
async def test_process_llm_judge_updates_result_metrics(monkeypatch):
    run = _run_row(status="completed")
    results = [
        _result_row(id="keres_1", metrics={"retrieval_recall": 1.0}),
    ]

    async def fake_call_llm(*args, **kwargs):
        return _mock_llm_resp('{"score": 0.77, "reason": "ok"}')

    monkeypatch.setattr(ke.llm_client, "call_llm", fake_call_llm)

    conn = _SeqConnection(
        results=[
            _Result(row=run),
            _Result(rows=results),
            _Result(row={"status": "running"}),
            _Result(),
            _Result(row={"id": "job_2"}),
            _Result(),
            _Result(rows=[{"metrics": {"retrieval_recall": 1.0, "faithfulness": 0.77, "answer_relevance": 0.77}}]),
            _Result(),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    job = _llm_judge_claimed_job()
    result = await ke._process_llm_judge(job)
    assert result["updated"] == 1
    assert result["errors"] == 0

    update_q = next((q for q, _ in conn.calls if "UPDATE kb_eval_result" in q), None)
    assert update_q is not None


@pytest.mark.asyncio
async def test_process_llm_judge_aggregates_run_summary(monkeypatch):
    run = _run_row(status="completed")
    results = [
        _result_row(id="keres_1", metrics={"retrieval_recall": 1.0}),
        _result_row(id="keres_2", metrics={"retrieval_recall": 0.5}),
    ]

    async def fake_call_llm(*args, **kwargs):
        return _mock_llm_resp('{"score": 0.9}')

    monkeypatch.setattr(ke.llm_client, "call_llm", fake_call_llm)

    conn = _SeqConnection(
        results=[
            _Result(row=run),
            _Result(rows=results),
            _Result(row={"status": "running"}),
            _Result(),
            _Result(row={"id": "job_2"}),
            _Result(),
            _Result(row={"status": "running"}),
            _Result(),
            _Result(row={"id": "job_2"}),
            _Result(),
            _Result(rows=[
                {"metrics": {"retrieval_recall": 1.0, "faithfulness": 0.9, "answer_relevance": 0.9}},
                {"metrics": {"retrieval_recall": 0.5, "faithfulness": 0.9, "answer_relevance": 0.9}},
            ]),
            _Result(),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    job = _llm_judge_claimed_job()
    result = await ke._process_llm_judge(job)
    assert result["updated"] == 2

    update_run_q = next((q for q, _ in conn.calls if "UPDATE kb_eval_run" in q and "metrics_summary" in q), None)
    assert update_run_q is not None


@pytest.mark.asyncio
async def test_process_llm_judge_handles_cancellation(monkeypatch):
    run = _run_row(status="completed")
    results = [
        _result_row(id="keres_1"),
    ]

    conn = _SeqConnection(
        results=[
            _Result(row=run),
            _Result(rows=results),
            _Result(row={"status": "cancel_requested"}),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    job = _llm_judge_claimed_job()
    with pytest.raises(Exception, match="cancelled"):
        await ke._process_llm_judge(job)


@pytest.mark.asyncio
async def test_process_llm_judge_preserves_existing_metrics(monkeypatch):
    run = _run_row(status="completed")
    results = [
        _result_row(id="keres_1", metrics={"retrieval_recall": 0.75, "context_precision": 0.6}),
    ]

    async def fake_call_llm(*args, **kwargs):
        return _mock_llm_resp('{"score": 0.55}')

    monkeypatch.setattr(ke.llm_client, "call_llm", fake_call_llm)

    conn = _SeqConnection(
        results=[
            _Result(row=run),
            _Result(rows=results),
            _Result(row={"status": "running"}),
            _Result(),
            _Result(row={"id": "job_2"}),
            _Result(),
            _Result(rows=[{"metrics": {"retrieval_recall": 0.75, "context_precision": 0.6, "faithfulness": 0.55, "answer_relevance": 0.55}}]),
            _Result(),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    job = _llm_judge_claimed_job()
    await ke._process_llm_judge(job)

    update_q, params = next(((q, p) for q, p in conn.calls if "UPDATE kb_eval_result" in q), (None, None))
    assert update_q is not None
    dumped = params[0]
    assert "retrieval_recall" in dumped
    assert "context_precision" in dumped


@pytest.mark.asyncio
async def test_process_llm_judge_skips_empty_results(monkeypatch):
    run = _run_row(status="completed")
    results = []

    conn = _SeqConnection(
        results=[
            _Result(row=run),
            _Result(rows=results),
            _Result(rows=[]),
            _Result(),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    job = _llm_judge_claimed_job()
    result = await ke._process_llm_judge(job)
    assert result["updated"] == 0
    assert result["errors"] == 0


@pytest.mark.asyncio
async def test_process_llm_judge_graceful_on_db_error(monkeypatch):
    run = _run_row(status="completed")
    results = [
        _result_row(id="keres_1", metrics={"retrieval_recall": 1.0}),
        _result_row(id="keres_2", metrics={"retrieval_recall": 0.5}),
    ]

    async def fake_call_llm(*args, **kwargs):
        return _mock_llm_resp('{"score": 0.8}')

    monkeypatch.setattr(ke.llm_client, "call_llm", fake_call_llm)

    class _FailingConnection(_SeqConnection):
        async def execute(self, query, params=()):
            self.calls.append((query, params))
            if "UPDATE kb_eval_result" in query and self._idx == 3:
                self._idx += 1
                raise RuntimeError("DB error")
            if self._idx < len(self._results):
                r = self._results[self._idx]
                self._idx += 1
                return r
            return _Result()

    conn = _FailingConnection(
        results=[
            _Result(row=run),                           # 0: SELECT kb_eval_run
            _Result(rows=results),                      # 1: SELECT kb_eval_result
            _Result(row={"status": "running"}),         # 2: _eval_not_cancelled #1
            _Result(),                                  # 3: UPDATE kb_eval_result #1 → RuntimeError (consumed by raise)
            _Result(row={"status": "running"}),         # 4: _eval_not_cancelled #2
            _Result(),                                  # 5: UPDATE kb_eval_result #2
            _Result(row={"id": "job_2"}),               # 6: heartbeat #2 (RETURNING)
            _Result(),                                  # 7: heartbeat #2 (UPDATE ops_async_operation)
            _Result(rows=[
                {"metrics": {"retrieval_recall": 1.0, "faithfulness": 0.8, "answer_relevance": 0.8}},
                {"metrics": {"retrieval_recall": 0.5, "faithfulness": 0.8, "answer_relevance": 0.8}},
            ]),                                         # 8: SELECT metrics (re-aggregation)
            _Result(),                                  # 9: UPDATE kb_eval_run
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    job = _llm_judge_claimed_job()
    result = await ke._process_llm_judge(job)
    assert result["updated"] == 1
    assert result["errors"] == 1


@pytest.mark.asyncio
async def test_process_llm_judge_workspace_isolation_404(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    job = _llm_judge_claimed_job(payload={"run_id": "kerun_1", "org_id": "org_test"})
    with pytest.raises(ValueError, match="not found"):
        await ke._process_llm_judge(job)
