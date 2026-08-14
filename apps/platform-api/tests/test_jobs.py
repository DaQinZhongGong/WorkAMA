from __future__ import annotations

import pytest
from fastapi import HTTPException

from workama_platform.core import Actor
from workama_platform.modules import jobs


# --- 测试辅助：模拟 psycopg 连接池与事务 --------------------------------


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


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


class _SeqConnection:
    """按调用顺序返回预设 Result，记录所有 execute 调用。"""

    def __init__(self, results=None):
        self._results = list(results or [])
        self.calls = []

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if self._results:
            return self._results.pop(0)
        return _Result()

    def transaction(self):
        return _Transaction()

    async def commit(self):
        return None


def _actor(role: str = "admin", workspace_id: str = "wsp_test") -> Actor:
    return Actor(
        user_id="usr_test",
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email="admin@example.test",
        display_name="Admin",
        onboarding_completed=True,
    )


# --- submit_operation：正常提交 -----------------------------------------


@pytest.mark.asyncio
async def test_submit_operation_inserts_operation_and_job_with_idempotency_key():
    # Arrange: SELECT 返回空（无已有 operation），INSERT RETURNING 返回新 operation
    new_operation = {"id": "op_new", "status": "queued", "operation_type": "export"}
    conn = _SeqConnection(results=[_Result(row=None), _Result(row=new_operation), _Result()])

    # Act
    operation = await jobs.submit_operation(
        conn,
        operation_type="export",
        workspace_id="wsp_test",
        org_id="org_test",
        actor_id="usr_test",
        actor_role="admin",
        idempotency_key="idem-001",
        payload={"format": "json"},
        job_type="export_job",
    )

    # Assert: 返回新 operation，且执行了 SELECT + 2 条 INSERT
    assert operation == new_operation
    assert len(conn.calls) == 3
    select_q, select_p = conn.calls[0]
    assert "SELECT" in select_q.upper()
    assert select_p == ("wsp_test", "export", "idem-001")

    insert_op_q, insert_op_p = conn.calls[1]
    assert "INSERT INTO ops_async_operation" in insert_op_q
    assert "idem-001" in insert_op_p  # idempotency_key 出现在 INSERT 参数中

    insert_job_q, insert_job_p = conn.calls[2]
    assert "INSERT INTO ops_job" in insert_job_q


@pytest.mark.asyncio
async def test_submit_operation_returns_existing_operation_for_same_idempotency_key():
    # Arrange: 相同 idempotency_key + 相同 input_hash → 返回已有 operation
    existing = {"id": "op_existing", "status": "queued", "input_hash": "hash_known"}
    conn = _SeqConnection(results=[_Result(row=existing)])

    # Act: 使用 input_hash_override 确保 input_hash 一致
    operation = await jobs.submit_operation(
        conn,
        operation_type="export",
        workspace_id="wsp_test",
        org_id="org_test",
        actor_id="usr_test",
        actor_role="admin",
        idempotency_key="idem-001",
        payload={"format": "json"},
        job_type="export_job",
        input_hash_override="hash_known",
    )

    # Assert: 返回已有 operation，且仅执行了 SELECT（无 INSERT）
    assert operation == existing
    assert len(conn.calls) == 1


@pytest.mark.asyncio
async def test_submit_operation_raises_conflict_when_idempotency_key_used_with_different_input():
    # Arrange: 相同 idempotency_key 但 input_hash 不同 → 冲突
    existing = {"id": "op_existing", "input_hash": "hash_old"}
    conn = _SeqConnection(results=[_Result(row=existing)])

    # Act + Assert: 抛出 IdempotencyConflict
    with pytest.raises(jobs.IdempotencyConflict, match="different input"):
        await jobs.submit_operation(
            conn,
            operation_type="export",
            workspace_id="wsp_test",
            org_id="org_test",
            actor_id="usr_test",
            actor_role="admin",
            idempotency_key="idem-001",
            payload={"format": "json"},
            job_type="export_job",
            input_hash_override="hash_new",
        )


@pytest.mark.asyncio
async def test_submit_operation_idempotency_conflict_carries_error_code():
    # Arrange: 验证 IdempotencyConflict 携带 E00008 错误码
    assert jobs.IdempotencyConflict.code == "E00008"


# --- submit_operation：默认值与自定义值 ----------------------------------


@pytest.mark.asyncio
async def test_submit_operation_uses_default_max_attempts():
    # Arrange: 不传 max_attempts → 默认值 3 进入 INSERT 参数
    new_operation = {"id": "op_new", "status": "queued"}
    conn = _SeqConnection(results=[_Result(row=None), _Result(row=new_operation), _Result()])

    # Act
    await jobs.submit_operation(
        conn,
        operation_type="export",
        workspace_id="wsp_test",
        org_id="org_test",
        actor_id="usr_test",
        actor_role="admin",
        idempotency_key="idem-001",
        payload={},
        job_type="export_job",
    )

    # Assert: INSERT ops_async_operation 的倒数第 2 个参数（max_attempts）为 3
    _, insert_op_p = conn.calls[1]
    assert insert_op_p[-1] == 3  # cancellable=True, max_attempts=3 → 最后一个是 3
    assert insert_op_p[-2] is True  # cancellable 默认 True


@pytest.mark.asyncio
async def test_submit_operation_passes_custom_max_attempts():
    # Arrange: 传 max_attempts=5 → INSERT 参数中为 5
    new_operation = {"id": "op_new", "status": "queued"}
    conn = _SeqConnection(results=[_Result(row=None), _Result(row=new_operation), _Result()])

    # Act
    await jobs.submit_operation(
        conn,
        operation_type="export",
        workspace_id="wsp_test",
        org_id="org_test",
        actor_id="usr_test",
        actor_role="admin",
        idempotency_key="idem-001",
        payload={},
        job_type="export_job",
        max_attempts=5,
    )

    # Assert: INSERT ops_async_operation 最后一个参数为 5
    _, insert_op_p = conn.calls[1]
    assert insert_op_p[-1] == 5


@pytest.mark.asyncio
async def test_submit_operation_uses_default_queue_and_priority():
    # Arrange: 不传 queue / priority → 默认 "platform" / 100
    new_operation = {"id": "op_new", "status": "queued"}
    conn = _SeqConnection(results=[_Result(row=None), _Result(row=new_operation), _Result()])

    # Act
    await jobs.submit_operation(
        conn,
        operation_type="export",
        workspace_id="wsp_test",
        org_id="org_test",
        actor_id="usr_test",
        actor_role="admin",
        idempotency_key="idem-001",
        payload={},
        job_type="export_job",
    )

    # Assert: INSERT ops_job 参数中 queue="platform", priority=100
    # 参数顺序: (job_id, operation_id, workspace_id, job_type, payload_json, input_hash, queue, priority, max_attempts, cancellable)
    _, insert_job_p = conn.calls[2]
    assert insert_job_p[6] == "platform"
    assert insert_job_p[7] == 100


@pytest.mark.asyncio
async def test_submit_operation_uses_input_hash_override():
    # Arrange: 提供 input_hash_override → 跳过 canonical_hash 计算
    new_operation = {"id": "op_new", "status": "queued"}
    conn = _SeqConnection(results=[_Result(row=None), _Result(row=new_operation), _Result()])

    # Act
    await jobs.submit_operation(
        conn,
        operation_type="export",
        workspace_id="wsp_test",
        org_id="org_test",
        actor_id="usr_test",
        actor_role="admin",
        idempotency_key="idem-001",
        payload={"format": "json"},
        job_type="export_job",
        input_hash_override="custom_hash_value",
    )

    # Assert: INSERT ops_async_operation 参数中 input_hash 为 override 值
    _, insert_op_p = conn.calls[1]
    assert "custom_hash_value" in insert_op_p


@pytest.mark.asyncio
async def test_submit_operation_requires_operation_type():
    # Arrange: 缺少必填参数 operation_type
    conn = _SeqConnection()

    # Act + Assert: Python keyword-only 参数缺失 → TypeError
    with pytest.raises(TypeError):
        await jobs.submit_operation(
            conn,
            workspace_id="wsp_test",
            org_id="org_test",
            actor_id="usr_test",
            actor_role="admin",
            idempotency_key="idem-001",
            payload={},
            job_type="export_job",
        )


# --- GET /api/v1/operations/{operation_id} ------------------------------


@pytest.mark.asyncio
async def test_get_operation_returns_row_for_owner_workspace(monkeypatch):
    # Arrange: 数据库返回 operation 行
    row = {"id": "op_1", "status": "queued", "workspace_id": "wsp_test"}
    conn = _SeqConnection(results=[_Result(row=row)])
    monkeypatch.setattr(jobs, "pool", _Pool(conn))

    # Act
    result = await jobs.get_operation("op_1", _actor())

    # Assert: 返回数据库行
    assert result == row


@pytest.mark.asyncio
async def test_get_operation_returns_404_when_not_found(monkeypatch):
    # Arrange: 数据库返回空
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(jobs, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await jobs.get_operation("op_missing", _actor())
    assert exc.value.status_code == 404
    assert exc.value.detail == "Operation not found"


@pytest.mark.asyncio
async def test_get_operation_isolates_workspace_via_acl(monkeypatch):
    # Arrange: 验证查询参数中 workspace_id 与 actor 一致
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(jobs, "pool", _Pool(conn))

    # Act
    with pytest.raises(HTTPException) as exc:
        await jobs.get_operation("op_1", _actor(workspace_id="wsp_a"))
    assert exc.value.status_code == 404

    # Assert: SQL 含 workspace_id 过滤，参数为 actor 的 workspace_id
    query, params = conn.calls[0]
    assert "workspace_id = %s" in query
    assert params[1] == "wsp_a"


# --- POST /api/v1/operations/{operation_id}/cancellations ---------------


@pytest.mark.asyncio
async def test_cancel_operation_returns_404_when_not_found(monkeypatch):
    # Arrange: request_cancellation 返回 None（operation 不存在）
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(jobs, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await jobs.cancel_operation("op_missing", jobs.ReasonRequest(reason="user request"), _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_cancel_operation_returns_cancelled_status(monkeypatch):
    # Arrange: operation 处于 queued 状态 → 直接取消
    operation = {
        "id": "op_1", "status": "queued", "cancellable": True,
        "actor_id": "usr_test", "org_id": "org_test", "actor_role": "admin",
    }
    # 调用序列: SELECT op, UPDATE op, UPDATE job, SELECT workflow_run(None), SELECT work_execution(None)
    conn = _SeqConnection(results=[
        _Result(row=operation), _Result(), _Result(), _Result(row=None), _Result(row=None),
    ])
    monkeypatch.setattr(jobs, "pool", _Pool(conn))

    # Act
    result = await jobs.cancel_operation("op_1", jobs.ReasonRequest(reason="user request"), _actor())

    # Assert: 返回 cancelled 状态
    assert result == {"id": "op_1", "status": "cancelled"}


@pytest.mark.asyncio
async def test_cancel_operation_returns_cancel_requested_for_running(monkeypatch):
    # Arrange: operation 处于 running 状态 → cancel_requested
    operation = {
        "id": "op_1", "status": "running", "cancellable": True,
        "actor_id": "usr_test", "org_id": "org_test", "actor_role": "admin",
    }
    # running → cancel_requested: SELECT op, UPDATE op, UPDATE job（无 workflow/work 取消）
    conn = _SeqConnection(results=[_Result(row=operation), _Result(), _Result()])
    monkeypatch.setattr(jobs, "pool", _Pool(conn))

    # Act
    result = await jobs.cancel_operation("op_1", jobs.ReasonRequest(reason="user request"), _actor())

    # Assert: 返回 cancel_requested 状态
    assert result == {"id": "op_1", "status": "cancel_requested"}


# --- GET /api/v1/admin/operations ---------------------------------------


@pytest.mark.asyncio
async def test_list_operations_rejects_non_admin():
    # Arrange: member 角色无权访问
    # Act + Assert: 在 DB 调用前即被拒绝
    with pytest.raises(HTTPException) as exc:
        await jobs.list_operations(_actor(role="member"), limit=50)
    assert exc.value.status_code == 403
    assert exc.value.detail == "Admin role required"


@pytest.mark.asyncio
async def test_list_operations_returns_items_for_workspace(monkeypatch):
    # Arrange: admin 角色查询，返回 2 条 operation
    rows = [{"id": "op_1"}, {"id": "op_2"}]
    conn = _SeqConnection(results=[_Result(rows=rows)])
    monkeypatch.setattr(jobs, "pool", _Pool(conn))

    # Act
    result = await jobs.list_operations(_actor(role="admin"), limit=50)

    # Assert: 返回 items 列表，且 workspace_id 进入查询参数
    assert result["items"] == rows
    _, params = conn.calls[0]
    assert params[0] == "wsp_test"


# --- GET /api/v1/admin/jobs ---------------------------------------------


@pytest.mark.asyncio
async def test_list_jobs_rejects_non_admin():
    # Arrange: viewer 角色无权访问
    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await jobs.list_jobs(_actor(role="viewer"), limit=50)
    assert exc.value.status_code == 403


# --- GET /api/v1/admin/jobs/{job_id} ------------------------------------


@pytest.mark.asyncio
async def test_get_job_returns_404_when_not_found(monkeypatch):
    # Arrange: 数据库返回空
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(jobs, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await jobs.get_job("job_missing", _actor(role="admin"))
    assert exc.value.status_code == 404
    assert exc.value.detail == "Job not found"


# --- GET /api/v1/admin/jobs/{job_id}/runs -------------------------------


@pytest.mark.asyncio
async def test_list_job_runs_returns_404_when_job_not_found(monkeypatch):
    # Arrange: 第一条 SELECT（检查 job 归属）返回空
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(jobs, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await jobs.list_job_runs("job_missing", _actor(role="admin"))
    assert exc.value.status_code == 404
    assert exc.value.detail == "Job not found"


# --- GET /api/v1/admin/dead-letters -------------------------------------


@pytest.mark.asyncio
async def test_list_dead_letters_rejects_non_admin():
    # Arrange: member 角色无权访问
    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await jobs.list_dead_letters(_actor(role="member"), limit=50)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_dead_letter_returns_404_when_not_found(monkeypatch):
    # Arrange: 数据库返回空
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(jobs, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await jobs.get_dead_letter("dlq_missing", _actor(role="admin"))
    assert exc.value.status_code == 404
    assert exc.value.detail == "Dead letter not found"


# --- POST /api/v1/admin/dead-letters/{id}/replays -----------------------


@pytest.mark.asyncio
async def test_replay_dead_letter_returns_404_when_not_found(monkeypatch):
    # Arrange: 数据库返回空
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(jobs, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await jobs.replay_dead_letter("dlq_missing", jobs.ReasonRequest(reason="retry needed"), _actor(role="admin"))
    assert exc.value.status_code == 404
    assert exc.value.detail == "Dead letter not found"


# --- POST /api/v1/admin/jobs/{job_id}/cancellations ---------------------


@pytest.mark.asyncio
async def test_cancel_job_returns_404_when_job_not_found(monkeypatch):
    # Arrange: SELECT job 返回空
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(jobs, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await jobs.cancel_job("job_missing", jobs.ReasonRequest(reason="cancel needed"), _actor(role="admin"))
    assert exc.value.status_code == 404
    assert exc.value.detail == "Job not found"


# --- POST /api/v1/admin/jobs/{job_id}/retries ---------------------------


@pytest.mark.asyncio
async def test_retry_job_returns_404_when_job_not_found(monkeypatch):
    # Arrange: SELECT job 返回空
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(jobs, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await jobs.retry_job("job_missing", jobs.ReasonRequest(reason="retry needed"), _actor(role="admin"))
    assert exc.value.status_code == 404
    assert exc.value.detail == "Job not found"


@pytest.mark.asyncio
async def test_retry_job_returns_409_when_job_not_failed_or_cancelled(monkeypatch):
    # Arrange: job 状态为 succeeded → 不允许重试
    job = {"id": "job_1", "status": "succeeded", "workspace_id": "wsp_test", "operation_id": "op_1"}
    conn = _SeqConnection(results=[_Result(row=job)])
    monkeypatch.setattr(jobs, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await jobs.retry_job("job_1", jobs.ReasonRequest(reason="retry needed"), _actor(role="admin"))
    assert exc.value.status_code == 409
    assert exc.value.detail == "Only failed or cancelled jobs can be retried"


@pytest.mark.asyncio
async def test_retry_job_succeeds_for_failed_job(monkeypatch):
    # Arrange: job 状态为 failed → 允许重试，创建新 job
    source_job = {
        "id": "job_1", "status": "failed", "workspace_id": "wsp_test", "operation_id": "op_1",
        "job_type": "export", "schema_version": 1, "queue": "platform", "priority": 100,
        "payload": {"k": "v"}, "payload_hash": "hash", "max_attempts": 3,
        "timeout_seconds": 300, "heartbeat_seconds": 15, "cancellable": True,
    }
    # 调用序列: SELECT job FOR UPDATE, INSERT new job, UPDATE op, UPDATE dlq
    conn = _SeqConnection(results=[_Result(row=source_job), _Result(), _Result(), _Result()])
    monkeypatch.setattr(jobs, "pool", _Pool(conn))

    # Act
    result = await jobs.retry_job("job_1", jobs.ReasonRequest(reason="retry needed"), _actor(role="admin"))

    # Assert: 返回新 job_id 和 queued 状态
    assert result["operation_id"] == "op_1"
    assert result["status"] == "queued"
    assert result["id"].startswith("job_")


# --- 路由契约 ------------------------------------------------------------


def test_operation_router_exposes_get_and_cancel_contracts():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in jobs.operation_router.routes}
    assert ("/api/v1/operations/{operation_id}", ("GET",)) in paths
    assert ("/api/v1/operations/{operation_id}/cancellations", ("POST",)) in paths


def test_admin_router_exposes_jobs_and_dead_letter_contracts():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in jobs.admin_router.routes}
    assert ("/api/v1/admin/operations", ("GET",)) in paths
    assert ("/api/v1/admin/jobs", ("GET",)) in paths
    assert ("/api/v1/admin/jobs/{job_id}", ("GET",)) in paths
    assert ("/api/v1/admin/jobs/{job_id}/runs", ("GET",)) in paths
    assert ("/api/v1/admin/jobs/{job_id}/retries", ("POST",)) in paths
    assert ("/api/v1/admin/jobs/{job_id}/cancellations", ("POST",)) in paths
    assert ("/api/v1/admin/dead-letters", ("GET",)) in paths
    assert ("/api/v1/admin/dead-letters/{dead_letter_id}", ("GET",)) in paths
    assert ("/api/v1/admin/dead-letters/{dead_letter_id}/replays", ("POST",)) in paths
