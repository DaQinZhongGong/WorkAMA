"""privacy 模块数据主体删除流程与 compliance 法律保留 (sec_legal_hold) 集成测试。

覆盖 GDPR Art.17(3) 法律义务保留豁免：privacy.processor.process_data_request
在执行 delete 级联删除前，必须查询 sec_legal_hold；命中活跃保留则阻止删除、
将请求置为 rejected；无活跃保留（含已释放保留）则继续原删除流程。export/correct
等请求不受法律保留影响。

所有测试使用 fake pool/connection/redis，不依赖真实 DB / Redis / 网络。
"""
from __future__ import annotations

import pytest

from workama_platform.modules.privacy import processor


# ============================================================================
# 测试辅助：fake pool / connection / result / redis
# ============================================================================


class _Result:
    """模拟 psycopg 查询结果。"""

    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = list(rows) if rows is not None else []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return list(self._rows)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Conn:
    """记录 execute 调用并按序返回配置结果。

    对 sec_legal_hold 查询，按 in-memory ``holds`` 列表模拟 DB 端 WHERE 语义
    (released_at IS NULL AND status='active' AND resource_type 匹配)，从而
    忠实反映活跃/已释放保留的拦截行为。其余查询消费有序 results。
    """

    def __init__(self, results=None, holds=None):
        self.calls: list[tuple[str, tuple]] = []
        self._results = list(results) if results else []
        self._idx = 0
        self.holds = list(holds) if holds is not None else []

    def transaction(self):
        return _Transaction()

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if "sec_legal_hold" in query:
            return self._legal_hold_result(params)
        if self._idx < len(self._results):
            r = self._results[self._idx]
            self._idx += 1
            return r
        return _Result()

    async def commit(self):
        return None

    def _legal_hold_result(self, params):
        workspace_id = params[0]
        types = set(params[1]) if len(params) > 1 else set()
        for h in self.holds:
            if h.get("workspace_id") != workspace_id:
                continue
            if h.get("released_at") is not None:
                continue
            if h.get("status") != "active":
                continue
            rt = h.get("resource_type")
            if rt == "all" or rt in types:
                return _Result(row={"id": h["id"], "resource_type": rt, "basis": h["basis"]})
        return _Result(row=None)


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_args):
                return False

        return _Ctx()


class _AsyncIter:
    def __init__(self, items):
        self._items = list(items)
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._i]
        self._i += 1
        return item


class _FakeRedis:
    def __init__(self, keys=()):
        self._keys = list(keys)
        self.deleted = []

    def scan_iter(self, match="*"):
        return _AsyncIter(self._keys)

    async def delete(self, *keys):
        self.deleted.extend(keys)
        return len(keys)


# ============================================================================
# fixtures / 行数据
# ============================================================================


def _count_results(n: int = 0, count: int = 12):
    return [_Result(row={"count": n}) for _ in range(count)]


_DELETE_REQUEST = {
    "id": "dsr_del",
    "user_id": "usr_1",
    "workspace_id": "wsp_1",
    "request_type": "delete",
    "scope": "content",
    "status": "approved",
}

_EXPORT_REQUEST = {
    "id": "dsr_exp",
    "user_id": "usr_1",
    "workspace_id": "wsp_1",
    "request_type": "export",
    "scope": "content",
    "status": "approved",
}


def _hold(hold_id, *, resource_type, basis="litigation hold", released=False, workspace_id="wsp_1"):
    return {
        "id": hold_id,
        "workspace_id": workspace_id,
        "resource_type": resource_type,
        "basis": basis,
        "status": "released" if released else "active",
        "released_at": "2026-01-01T00:00:00+00:00" if released else None,
    }


def _delete_proceeds_results(artifact_keys=()):
    """delete 请求未被法律保留拦截时，conn.execute 按序消费的结果序列。"""
    return [
        _Result(row=_DELETE_REQUEST),                              # SELECT request
        _Result(),                                                  # UPDATE executing
        _Result(),                                                  # step identity_verification
        *_count_results(),                                          # _resource_counts (12)
        _Result(),                                                  # step scope_resources
        # sec_legal_hold 查询被 _Conn 拦截，不消费结果
        _Result(),                                                  # step revoke_access
        _Result(rows=[{"s3_key": k} for k in artifact_keys]),      # SELECT ag_artifact s3_key
        _Result(rows=[]),                                           # SELECT ag_attachment s3_key
        _Result(),                                                  # DELETE ag_session RETURNING
        _Result(),                                                  # DELETE id_notification RETURNING
        _Result(),                                                  # DELETE ag_memory RETURNING
        _Result(),                                                  # DELETE pf_assistant RETURNING
        _Result(),                                                  # DELETE pf_workflow RETURNING
        _Result(),                                                  # step delete_postgres_content
        _Result(),                                                  # step delete_object_references
        _Result(),                                                  # step purge_cache
        _Result(),                                                  # INSERT id_deletion_tombstone
        _Result(),                                                  # step write_tombstone
        *_count_results(),                                          # _resource_counts (after, 12)
        _Result(),                                                  # step verify_absence
        _Result(),                                                  # UPDATE completed
    ]


def _has_call(calls, needle):
    """needle 出现在某次调用的 SQL 文本或参数字符串表示中即为命中。"""
    return any(needle in q or needle in str(p) for q, p in calls)


def _params_for(calls, needle):
    return next(p for q, p in calls if needle in q)


@pytest.fixture
def stub_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(processor, "redis", fake)
    return fake


# ============================================================================
# 1. delete 命中 resource_type='all' 的活跃保留 → rejected，不执行删除
# ============================================================================


@pytest.mark.asyncio
async def test_delete_blocked_by_global_legal_hold(monkeypatch, stub_redis):
    deleted_objects = []

    async def _fake_delete_object(bucket, key):
        deleted_objects.append((bucket, key))

    monkeypatch.setattr(processor, "delete_object", _fake_delete_object)

    results = [
        _Result(row=_DELETE_REQUEST),
        _Result(),
        _Result(),
        *_count_results(),
        _Result(),
        # sec_legal_hold 查询返回活跃 'all' 保留
        _Result(),  # UPDATE rejected
        _Result(),  # step legal_hold_check
    ]
    conn = _Conn(results=results, holds=[_hold("hold_all", resource_type="all", basis="pending litigation")])
    monkeypatch.setattr(processor, "pool", _Pool(conn))

    ok = await processor.process_data_request("dsr_del")

    assert ok is True
    assert _has_call(conn.calls, "sec_legal_hold")
    assert _has_call(conn.calls, "status = 'rejected'")
    assert not _has_call(conn.calls, "DELETE FROM")
    assert not _has_call(conn.calls, "id_deletion_tombstone")
    assert _has_call(conn.calls, "legal_hold_check")
    # rejected 更新携带拦截原因
    rejected_params = _params_for(conn.calls, "status = 'rejected'")
    assert "blocked by legal hold: hold_all" in rejected_params[0]
    assert "pending litigation" in rejected_params[0]
    # 未触发对象存储删除
    assert deleted_objects == []


# ============================================================================
# 2. delete 命中 resource_type='session' 的活跃保留 → rejected
# ============================================================================


@pytest.mark.asyncio
async def test_delete_blocked_by_session_legal_hold(monkeypatch, stub_redis):
    deleted_objects = []

    async def _fake_delete_object(bucket, key):
        deleted_objects.append((bucket, key))

    monkeypatch.setattr(processor, "delete_object", _fake_delete_object)

    results = [
        _Result(row=_DELETE_REQUEST),
        _Result(),
        _Result(),
        *_count_results(),
        _Result(),
        _Result(),  # UPDATE rejected
        _Result(),  # step legal_hold_check
    ]
    conn = _Conn(results=results, holds=[_hold("hold_sess", resource_type="session", basis="subpoena")])
    monkeypatch.setattr(processor, "pool", _Pool(conn))

    ok = await processor.process_data_request("dsr_del")

    assert ok is True
    assert _has_call(conn.calls, "status = 'rejected'")
    assert not _has_call(conn.calls, "DELETE FROM ag_session")
    assert not _has_call(conn.calls, "id_deletion_tombstone")
    rejected_params = _params_for(conn.calls, "status = 'rejected'")
    assert "blocked by legal hold: hold_sess" in rejected_params[0]
    assert "subpoena" in rejected_params[0]
    assert deleted_objects == []


# ============================================================================
# 3. delete 无活跃保留 → 正常删除流程（DELETE 被调用、写 tombstone、completed）
# ============================================================================


@pytest.mark.asyncio
async def test_delete_proceeds_when_no_active_hold(monkeypatch, stub_redis):
    deleted_objects = []

    async def _fake_delete_object(bucket, key):
        deleted_objects.append((bucket, key))

    monkeypatch.setattr(processor, "delete_object", _fake_delete_object)

    conn = _Conn(results=_delete_proceeds_results(artifact_keys=("art/k1", "art/k2")), holds=[])
    monkeypatch.setattr(processor, "pool", _Pool(conn))

    ok = await processor.process_data_request("dsr_del")

    assert ok is True
    assert _has_call(conn.calls, "DELETE FROM ag_session")
    assert _has_call(conn.calls, "DELETE FROM id_notification")
    assert _has_call(conn.calls, "DELETE FROM ag_memory")
    assert _has_call(conn.calls, "DELETE FROM pf_assistant")
    assert _has_call(conn.calls, "DELETE FROM pf_workflow")
    assert _has_call(conn.calls, "id_deletion_tombstone")
    assert _has_call(conn.calls, "status = 'completed'")
    assert not _has_call(conn.calls, "status = 'rejected'")
    # 对象存储删除按 artifact key 触发
    assert ("workama-artifacts", "art/k1") in deleted_objects
    assert ("workama-artifacts", "art/k2") in deleted_objects


# ============================================================================
# 4. delete 命中已释放的保留 (released_at 非 NULL) → 不拦截，正常删除
# ============================================================================


@pytest.mark.asyncio
async def test_delete_proceeds_when_hold_released(monkeypatch, stub_redis):
    deleted_objects = []

    async def _fake_delete_object(bucket, key):
        deleted_objects.append((bucket, key))

    monkeypatch.setattr(processor, "delete_object", _fake_delete_object)

    # 保留存在但已释放：released_at 非 NULL、status='released'
    conn = _Conn(
        results=_delete_proceeds_results(artifact_keys=()),
        holds=[_hold("hold_released", resource_type="all", basis="old case", released=True)],
    )
    monkeypatch.setattr(processor, "pool", _Pool(conn))

    ok = await processor.process_data_request("dsr_del")

    assert ok is True
    assert _has_call(conn.calls, "sec_legal_hold")
    # 法律保留查询 SQL 必须包含已释放保留的过滤条件
    lh_query = next(q for q, _ in conn.calls if "sec_legal_hold" in q)
    assert "released_at IS NULL" in lh_query
    assert "status = 'active'" in lh_query
    assert "resource_type = 'all'" in lh_query
    # 已释放保留不拦截 → 正常删除
    assert _has_call(conn.calls, "DELETE FROM ag_session")
    assert _has_call(conn.calls, "id_deletion_tombstone")
    assert _has_call(conn.calls, "status = 'completed'")
    assert not _has_call(conn.calls, "status = 'rejected'")


# ============================================================================
# 5. export 请求不受法律保留影响（仍正常导出，不查 sec_legal_hold）
# ============================================================================


@pytest.mark.asyncio
async def test_export_unaffected_by_legal_hold(monkeypatch, stub_redis):
    deleted_objects = []

    async def _fake_delete_object(bucket, key):
        deleted_objects.append((bucket, key))

    monkeypatch.setattr(processor, "delete_object", _fake_delete_object)

    results = [
        _Result(row=_EXPORT_REQUEST),
        _Result(),
        _Result(),
        *_count_results(),
        _Result(),  # step scope_resources
        _Result(),  # step build_manifest
        _Result(),  # step verify_manifest
        _Result(),  # UPDATE completed
    ]
    # 即便存在活跃 'all' 保留，export 也不应被拦截
    conn = _Conn(results=results, holds=[_hold("hold_all", resource_type="all")])
    monkeypatch.setattr(processor, "pool", _Pool(conn))

    ok = await processor.process_data_request("dsr_exp")

    assert ok is True
    assert not _has_call(conn.calls, "sec_legal_hold")
    assert not _has_call(conn.calls, "DELETE FROM")
    assert not _has_call(conn.calls, "status = 'rejected'")
    assert _has_call(conn.calls, "status = 'completed'")
    assert deleted_objects == []


# ============================================================================
# 6. delete 命中 resource_type='workspace' 的活跃保留 → 不拦截（workspace 不在删除资源映射内）
#    验证 resource_type 映射精度：仅 all/session/attachment/artifact/notification 拦截删除
# ============================================================================


@pytest.mark.asyncio
async def test_delete_proceeds_for_workspace_scoped_hold(monkeypatch, stub_redis):
    deleted_objects = []

    async def _fake_delete_object(bucket, key):
        deleted_objects.append((bucket, key))

    monkeypatch.setattr(processor, "delete_object", _fake_delete_object)

    # workspace 级保留只针对 workspace 资源本身，delete 路径不删除 id_workspace，
    # 故不应拦截用户内容删除
    conn = _Conn(
        results=_delete_proceeds_results(artifact_keys=()),
        holds=[_hold("hold_ws", resource_type="workspace", basis="workspace freeze")],
    )
    monkeypatch.setattr(processor, "pool", _Pool(conn))

    ok = await processor.process_data_request("dsr_del")

    assert ok is True
    assert _has_call(conn.calls, "sec_legal_hold")
    assert _has_call(conn.calls, "DELETE FROM ag_session")
    assert _has_call(conn.calls, "status = 'completed'")
    assert not _has_call(conn.calls, "status = 'rejected'")
