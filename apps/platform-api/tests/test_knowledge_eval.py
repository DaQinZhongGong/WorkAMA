"""knowledge_eval 模块单元测试。

覆盖范围：
- 评测集 CRUD（创建/列表/详情/归档/删除）
- 评测用例管理（创建/批量导入/列表/软删除，含 case_hash 去重）
- 评测运行（创建/列表/详情/结果/取消，含 idempotency_key）
- 标注管理（创建/列表/批量导入，含唯一约束冲突）
- 异步作业处理（process_kb_eval_job 成功/取消/失败路径）
- workspace 隔离与 capability 鉴权（403）
- 边界校验（dataset_id 不匹配 / 非法 metrics / dataset 不存在 / 用例不存在）

测试风格与 test_jobs.py 一致：内联 _Result/_SeqConnection/_Pool + monkeypatch。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException, Response

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
        "metrics": ["retrieval_recall"],
        "status": "active",
        "created_by": "usr_test",
        "created_at": "2026-07-28T10:00:00+00:00",
        "updated_at": "2026-07-28T10:00:00+00:00",
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
        "status": "pending",
        "metrics_summary": None,
        "error": None,
        "started_at": None,
        "completed_at": None,
        "created_at": "2026-07-28T10:00:00+00:00",
        "created_by": "usr_test",
    }
    base.update(overrides)
    return base


def _dataset_row(**overrides) -> dict:
    base = {
        "id": "dts_1",
        "workspace_id": "wsp_test",
        "status": "active",
        "active_generation_id": "gen_1",
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


def _result_row(**overrides) -> dict:
    base = {
        "id": "keres_1",
        "run_id": "kerun_1",
        "case_id": "kec_1",
        "question": "Q1",
        "retrieved_chunks": ["chk_a"],
        "generated_answer": "A1",
        "metrics": {"retrieval_recall": 1.0},
        "latency_ms": 10,
        "error": None,
        "created_at": "2026-07-28T10:00:00+00:00",
    }
    base.update(overrides)
    return base


# --- 评测集 CRUD -----------------------------------------------------------


@pytest.mark.asyncio
async def test_create_eval_set_inserts_with_validated_metrics(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_dataset_row()),  # _dataset SELECT
            _Result(row=_eval_set_row()),  # INSERT RETURNING
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.EvalSetCreate(name="smoke", dataset_id="dts_1", metrics=["retrieval_recall"])
    result = await ke.create_eval_set("dts_1", body, _actor())

    assert result["id"] == "kbes_1"
    assert result["metrics"] == ["retrieval_recall"]
    # 应该先 SELECT dataset（_dataset），再 INSERT eval_set
    insert_query = next(q for q, _ in conn.calls if "INSERT INTO kb_eval_set" in q)
    assert "RETURNING *" in insert_query


@pytest.mark.asyncio
async def test_create_eval_set_rejects_dataset_id_mismatch():
    body = ke.EvalSetCreate(name="smoke", dataset_id="dts_other")
    with pytest.raises(HTTPException) as exc:
        await ke.create_eval_set("dts_path", body, _actor())
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_create_eval_set_returns_404_when_dataset_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])  # _dataset 返回空
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.EvalSetCreate(name="smoke", dataset_id="dts_missing")
    with pytest.raises(HTTPException) as exc:
        await ke.create_eval_set("dts_missing", body, _actor())
    assert exc.value.status_code == 404
    assert "Dataset not found" in exc.value.detail


@pytest.mark.asyncio
async def test_create_eval_set_returns_403_without_capability():
    body = ke.EvalSetCreate(name="smoke", dataset_id="dts_1")
    actor = _actor(capabilities=("dataset:read",))
    with pytest.raises(HTTPException) as exc:
        await ke.create_eval_set("dts_1", body, actor)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_create_eval_set_returns_409_on_unique_constraint(monkeypatch):
    conn = _SeqConnection()

    async def execute_with_unique_on_insert(query, params=()):
        conn.calls.append((query, params))
        if "INSERT INTO kb_eval_set" in query:
            raise Exception("duplicate key value violates unique constraint")
        # _dataset SELECT 返回有效行，让其通过
        return _Result(row=_dataset_row())

    conn.execute = execute_with_unique_on_insert  # type: ignore[assignment]
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.EvalSetCreate(name="dup", dataset_id="dts_1")
    with pytest.raises(HTTPException) as exc:
        await ke.create_eval_set("dts_1", body, _actor())
    assert exc.value.status_code == 409
    assert "already exists" in exc.value.detail


@pytest.mark.asyncio
async def test_list_eval_sets_filters_archived_and_workspace_scoped(monkeypatch):
    rows = [_eval_set_row(name="a"), _eval_set_row(id="kbes_2", name="b", status="archived")]
    conn = _SeqConnection(
        results=[
            _Result(row=_dataset_row()),  # _dataset
            _Result(rows=rows),  # SELECT eval_sets
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.list_eval_sets("dts_1", _actor(), limit=50)

    assert len(result["items"]) == 2
    select_q = next(q for q, _ in conn.calls if "SELECT * FROM kb_eval_set" in q)
    assert "status <> 'archived'" in select_q
    assert "workspace_id=%s" in select_q


@pytest.mark.asyncio
async def test_get_eval_set_includes_case_count(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_eval_set_row(include_archived=True)),  # _eval_set
            _Result(row={"count": 5}),  # count_result
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.get_eval_set("kbes_1", _actor())

    assert result["id"] == "kbes_1"
    assert result["case_count"] == 5


@pytest.mark.asyncio
async def test_get_eval_set_returns_404_when_not_found(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await ke.get_eval_set("kbes_missing", _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_eval_set_archives_via_status_update(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_eval_set_row()),  # _eval_set
            _Result(row={"id": "kbes_1"}),  # UPDATE RETURNING
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    response = await ke.delete_eval_set("kbes_1", _actor())

    assert isinstance(response, Response)
    assert response.status_code == 204
    update_q = next(q for q, _ in conn.calls if "UPDATE kb_eval_set SET status='archived'" in q)
    assert "workspace_id=%s" in update_q


@pytest.mark.asyncio
async def test_delete_eval_set_returns_404_when_archived_missing(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_eval_set_row()),  # _eval_set 通过
            _Result(row=None),  # UPDATE RETURNING 无行
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await ke.delete_eval_set("kbes_missing", _actor())
    assert exc.value.status_code == 404


# --- 评测用例管理 ----------------------------------------------------------


@pytest.mark.asyncio
async def test_create_eval_case_inserts_new_case(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_eval_set_row()),  # _eval_set
            _Result(row=None),  # existing check 返回空
            _Result(row=_case_row()),  # INSERT RETURNING
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.EvalCaseCreate(question="Q1", expected_chunks=["chk_a"])
    result = await ke.create_eval_case("kbes_1", body, _actor())

    assert result["id"] == "kec_1"
    assert result["question"] == "Q1"
    # 应该最后调用 UPDATE 更新 eval_set 的 updated_at
    assert any("UPDATE kb_eval_set SET updated_at=now()" in q for q, _ in conn.calls)


@pytest.mark.asyncio
async def test_create_eval_case_returns_existing_when_duplicate(monkeypatch):
    existing_row = _case_row()
    conn = _SeqConnection(
        results=[
            _Result(row=_eval_set_row()),  # _eval_set
            _Result(row=existing_row),  # existing 找到
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.EvalCaseCreate(question="Q1", expected_chunks=["chk_a"])
    result = await ke.create_eval_case("kbes_1", body, _actor())

    assert result["id"] == "kec_1"
    # 不应再 INSERT
    assert not any("INSERT INTO kb_eval_case" in q for q, _ in conn.calls)


@pytest.mark.asyncio
async def test_create_eval_case_returns_404_when_eval_set_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])  # _eval_set 返回空
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.EvalCaseCreate(question="Q1")
    with pytest.raises(HTTPException) as exc:
        await ke.create_eval_case("kbes_missing", body, _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_import_eval_cases_creates_and_skips_duplicates(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_eval_set_row()),  # _eval_set
            _Result(row=None),  # 第 1 条 existing 返回空 → INSERT
            _Result(),  # INSERT (无 RETURNING)
            _Result(row=_case_row()),  # 第 2 条 existing 找到 → skipped
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.EvalCaseImport(
        items=[
            ke.EvalCaseCreate(question="Q1"),
            ke.EvalCaseCreate(question="Q1"),  # 同 hash 触发 skipped
        ]
    )
    result = await ke.import_eval_cases("kbes_1", body, _actor())

    assert result["created"] == 1
    assert result["skipped"] == 1


@pytest.mark.asyncio
async def test_list_eval_cases_returns_active_only(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_eval_set_row()),  # _eval_set
            _Result(rows=[_case_row(), _case_row(id="kec_2")]),  # SELECT cases
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.list_eval_cases("kbes_1", _actor(), limit=200)

    assert len(result["items"]) == 2
    select_q = next(q for q, _ in conn.calls if "SELECT * FROM kb_eval_case" in q)
    assert "status='active'" in select_q


@pytest.mark.asyncio
async def test_delete_eval_case_soft_deletes_active_case(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_eval_set_row()),  # _eval_set
            _Result(row={"id": "kec_1"}),  # UPDATE RETURNING 找到
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    response = await ke.delete_eval_case("kbes_1", "kec_1", _actor())

    assert response.status_code == 204
    update_q = next(q for q, _ in conn.calls if "UPDATE kb_eval_case SET status='deleted'" in q)
    assert "status='active'" in update_q


@pytest.mark.asyncio
async def test_delete_eval_case_returns_404_when_already_deleted(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_eval_set_row()),  # _eval_set
            _Result(row=None),  # UPDATE RETURNING 无行
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await ke.delete_eval_case("kbes_1", "kec_deleted", _actor())
    assert exc.value.status_code == 404


# --- 评测运行 --------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_eval_run_returns_run_and_operation(monkeypatch):
    operation = {"id": "op_1", "status": "queued", "operation_type": "kb.eval.run"}
    conn = _SeqConnection(
        results=[
            _Result(row=_eval_set_row()),  # _eval_set
            _Result(row=_dataset_row()),  # _dataset
            _Result(row={"count": 1}),  # case_count
            _Result(row=None),  # existing_result for operation
            _Result(row=_run_row()),  # INSERT kb_eval_run RETURNING
            _Result(),  # _outbox
        ]
    )

    async def fake_submit_operation(conn, **kwargs):
        return operation

    async def fake_outbox(conn, workspace_id, operation_id, payload):
        await conn.execute("-- outbox", (operation_id,))

    monkeypatch.setattr(ke, "pool", _Pool(conn))
    monkeypatch.setattr(ke, "submit_operation", fake_submit_operation)
    monkeypatch.setattr(ke, "_outbox", fake_outbox)

    body = ke.EvalRunCreate(top_k=5, candidate_k=20)
    result = await ke.create_eval_run("kbes_1", body, _actor(), idempotency_key=None)

    assert result["run"]["id"] == "kerun_1"
    assert result["operation"] == operation


@pytest.mark.asyncio
async def test_create_eval_run_returns_409_when_dataset_index_not_ready(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_eval_set_row()),  # _eval_set
            _Result(row=_dataset_row(active_generation_id=None)),  # _dataset 无索引
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.EvalRunCreate()
    with pytest.raises(HTTPException) as exc:
        await ke.create_eval_run("kbes_1", body, _actor())
    assert exc.value.status_code == 409
    assert "E03003" in exc.value.detail


@pytest.mark.asyncio
async def test_create_eval_run_returns_422_when_no_active_cases(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_eval_set_row()),  # _eval_set
            _Result(row=_dataset_row()),  # _dataset
            _Result(row={"count": 0}),  # case_count = 0
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.EvalRunCreate()
    with pytest.raises(HTTPException) as exc:
        await ke.create_eval_run("kbes_1", body, _actor())
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_create_eval_run_translates_idempotency_conflict_to_409(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_eval_set_row()),  # _eval_set
            _Result(row=_dataset_row()),  # _dataset
            _Result(row={"count": 1}),  # case_count
        ]
    )

    async def raise_conflict(conn, **kwargs):
        raise IdempotencyConflict("different input")

    monkeypatch.setattr(ke, "pool", _Pool(conn))
    monkeypatch.setattr(ke, "submit_operation", raise_conflict)

    body = ke.EvalRunCreate()
    with pytest.raises(HTTPException) as exc:
        await ke.create_eval_run("kbes_1", body, _actor())
    assert exc.value.status_code == 409
    assert "E00008" in exc.value.detail


@pytest.mark.asyncio
async def test_create_eval_run_returns_existing_run_for_same_operation(monkeypatch):
    existing_run = _run_row()
    operation = {"id": "op_1", "status": "queued"}
    conn = _SeqConnection(
        results=[
            _Result(row=_eval_set_row()),  # _eval_set
            _Result(row=_dataset_row()),  # _dataset
            _Result(row={"count": 1}),  # case_count
            _Result(row=existing_run),  # existing_result 找到
        ]
    )

    async def fake_submit_operation(conn, **kwargs):
        return operation

    monkeypatch.setattr(ke, "pool", _Pool(conn))
    monkeypatch.setattr(ke, "submit_operation", fake_submit_operation)

    body = ke.EvalRunCreate()
    result = await ke.create_eval_run("kbes_1", body, _actor(), idempotency_key="idem-1")

    assert result["run"]["id"] == "kerun_1"
    # 不应再 INSERT kb_eval_run
    assert not any("INSERT INTO kb_eval_run" in q for q, _ in conn.calls)


@pytest.mark.asyncio
async def test_list_eval_runs_filters_by_workspace_and_optional_eval_set(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(rows=[_run_row(), _run_row(id="kerun_2")]),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.list_eval_runs(_actor(), eval_set_id="kbes_1", limit=50)

    assert len(result["items"]) == 2
    select_q = next(q for q, _ in conn.calls if "SELECT * FROM kb_eval_run" in q)
    assert "eval_set_id=%s" in select_q
    assert "workspace_id=%s" in select_q


@pytest.mark.asyncio
async def test_list_eval_runs_returns_403_without_read_capability():
    actor = _actor(capabilities=("dataset:write",))
    with pytest.raises(HTTPException) as exc:
        await ke.list_eval_runs(actor)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_eval_run_returns_summary(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=_run_row())])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.get_eval_run("kerun_1", _actor())
    assert result["id"] == "kerun_1"
    assert result["operation_id"] == "op_1"


@pytest.mark.asyncio
async def test_get_eval_run_returns_404_when_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await ke.get_eval_run("kerun_missing", _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_eval_results_returns_run_and_items(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_run_row()),  # _eval_run
            _Result(rows=[_result_row(), _result_row(id="keres_2")]),  # SELECT results
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.get_eval_results("kerun_1", _actor(), limit=500)

    assert result["run"]["id"] == "kerun_1"
    assert len(result["items"]) == 2


@pytest.mark.asyncio
async def test_cancel_eval_run_returns_run_as_is_for_terminal_state(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_run_row(status="completed")),  # _eval_run 终态
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.cancel_eval_run("kerun_1", _actor())
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_cancel_eval_run_marks_cancelled_when_operation_cancels(monkeypatch):
    operation = {"id": "op_1", "status": "cancelled", "cancellable": True}
    # request_cancellation 被 mock，不调用 conn.execute。
    # 实际 execute 序列：_eval_run SELECT → UPDATE kb_eval_run → SELECT 最新 run
    conn = _SeqConnection(
        results=[
            _Result(row=_run_row(status="running")),  # _eval_run
            _Result(),  # UPDATE kb_eval_run SET cancelled
            _Result(row=_run_row(status="cancelled")),  # SELECT 返回最新 run
        ]
    )

    async def fake_request_cancellation(conn, **kwargs):
        return operation

    monkeypatch.setattr(ke, "pool", _Pool(conn))
    monkeypatch.setattr(ke, "request_cancellation", fake_request_cancellation)

    result = await ke.cancel_eval_run("kerun_1", _actor())
    assert result["status"] == "cancelled"


# --- 标注管理 --------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_annotation_inserts_and_returns_summary(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_run_row()),  # _eval_run
            _Result(row={"id": "kec_1"}),  # case 校验
            _Result(row=_annotation_row()),  # INSERT RETURNING
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.AnnotationCreate(case_id="kec_1", rating=5, feedback="great")
    result = await ke.create_annotation("kerun_1", body, _actor())

    assert result["id"] == "kean_1"
    assert result["rating"] == 5


@pytest.mark.asyncio
async def test_create_annotation_returns_404_when_case_missing(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_run_row()),  # _eval_run
            _Result(row=None),  # case 校验失败
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.AnnotationCreate(case_id="kec_missing", rating=3)
    with pytest.raises(HTTPException) as exc:
        await ke.create_annotation("kerun_1", body, _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_create_annotation_translates_unique_constraint_to_409(monkeypatch):
    conn = _SeqConnection()

    async def execute_with_unique_on_insert(query, params=()):
        conn.calls.append((query, params))
        if "INSERT INTO kb_eval_annotation" in query:
            raise Exception("duplicate key value violates unique constraint")
        # _eval_run SELECT 返回有效行
        if "SELECT * FROM kb_eval_run" in query:
            return _Result(row=_run_row())
        # case 校验 SELECT 返回有效行
        if "SELECT id FROM kb_eval_case" in query:
            return _Result(row={"id": "kec_1"})
        return _Result()

    conn.execute = execute_with_unique_on_insert  # type: ignore[assignment]
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.AnnotationCreate(case_id="kec_1", rating=5)
    with pytest.raises(HTTPException) as exc:
        await ke.create_annotation("kerun_1", body, _actor())
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_list_annotations_filters_by_run_and_workspace(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_run_row()),  # _eval_run
            _Result(rows=[_annotation_row(), _annotation_row(id="kean_2")]),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.list_annotations("kerun_1", _actor(), limit=500)

    assert len(result["items"]) == 2
    select_q = next(q for q, _ in conn.calls if "SELECT * FROM kb_eval_annotation" in q)
    assert "run_id=%s" in select_q
    assert "workspace_id=%s" in select_q


@pytest.mark.asyncio
async def test_import_annotations_skips_missing_cases_and_duplicates(monkeypatch):
    # import_annotations 序列（每条 item）：
    #   SELECT case → 若找到：SELECT existing annotation → 若无：INSERT
    #   SELECT case → 若未找到：skipped
    conn = _SeqConnection(
        results=[
            _Result(row=_run_row()),  # _eval_run
            _Result(row={"id": "kec_1"}),  # case 1 找到
            _Result(row=None),  # existing annotation 不存在 → 继续 INSERT
            _Result(),  # INSERT (no RETURNING)
            _Result(row=None),  # case 2 不存在 → skipped
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.AnnotationImport(
        items=[
            ke.AnnotationCreate(case_id="kec_1", rating=5),
            ke.AnnotationCreate(case_id="kec_missing", rating=3),
        ]
    )
    result = await ke.import_annotations("kerun_1", body, _actor())

    assert result["created"] == 1
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
async def test_process_kb_eval_job_unknown_type_raises_value_error(monkeypatch):
    job = _claimed_job()
    job = ClaimedJob(  # type: ignore[call-arg]
        job.id, job.operation_id, job.workspace_id, "kb.unknown",
        job.payload, job.attempt_count, job.max_attempts, job.lease_token,
    )
    with pytest.raises(ValueError, match="Unknown knowledge evaluation job type"):
        await ke.process_kb_eval_job(job)


@pytest.mark.asyncio
async def test_process_kb_eval_job_marks_failed_on_exception(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_run_row()),  # SELECT kb_eval_run
            _Result(),  # UPDATE running
            _Result(row=None),  # SELECT pf_dataset 不存在
            _Result(rows=[]),  # SELECT cases
            _Result(),  # commit
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(Exception):
        await ke.process_kb_eval_job(_claimed_job())

    # 应该看到将 run 标记为 failed 的 UPDATE
    fail_q = next(
        q for q, _ in conn.calls if "status=%s" in q and "completed_at=now()" in q
    )
    assert "kb_eval_run" in fail_q


@pytest.mark.asyncio
async def test_eval_not_cancelled_raises_when_operation_cancelled(monkeypatch):
    conn = _SeqConnection(
        results=[_Result(row={"status": "cancel_requested"})]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(Exception, match="cancelled"):
        await ke._eval_not_cancelled(_claimed_job())


@pytest.mark.asyncio
async def test_eval_not_cancelled_raises_when_operation_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(Exception, match="cancelled"):
        await ke._eval_not_cancelled(_claimed_job())


@pytest.mark.asyncio
async def test_eval_not_cancelled_passes_for_running_operation(monkeypatch):
    conn = _SeqConnection(results=[_Result(row={"status": "running"})])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    # 不应抛出
    await ke._eval_not_cancelled(_claimed_job())


# --- workspace 隔离与边界校验 ---------------------------------------------


@pytest.mark.asyncio
async def test_get_eval_run_workspace_isolation_404(monkeypatch):
    # 即使 run 存在但属于其他 workspace，也应该返回 404
    conn = _SeqConnection(
        results=[_Result(row=None)]  # workspace_id=%s 不匹配
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await ke.get_eval_run("kerun_1", _actor(workspace_id="wsp_other"))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_create_eval_case_archived_eval_set_returns_404(monkeypatch):
    """归档状态的 eval_set 不能再加用例。"""
    conn = _SeqConnection(results=[_Result(row=None)])  # _eval_set 排除 archived
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.EvalCaseCreate(question="Q1")
    with pytest.raises(HTTPException) as exc:
        await ke.create_eval_case("kbes_archived", body, _actor())
    assert exc.value.status_code == 404


def test_validate_metrics_rejects_unknown_metric_via_endpoint():
    """非法 metric 在路由层通过 _validate_metrics 拒绝（422）。"""
    with pytest.raises(HTTPException) as exc:
        ke._validate_metrics(["retrieval_recall", "totally_bogus"])
    assert exc.value.status_code == 422
    assert "Unsupported metrics" in exc.value.detail
