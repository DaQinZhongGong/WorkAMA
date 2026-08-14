from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import HTTPException

from workama_platform.core import Actor
from workama_platform.modules import search


# --- 测试辅助：模拟 psycopg 连接池与事务 --------------------------------


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


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


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


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


def _stub_submit(captured=None, return_value=None):
    """构造一个替换 submit_operation 的协程桩。"""
    return_value = return_value or {"id": "op_test", "status": "queued"}

    async def _stub(conn, **kwargs):
        if captured is not None:
            captured.update(kwargs)
        return return_value

    return _stub


# --- rebuild_search_projection：投影逻辑 ---------------------------------


class _ProjectionConnection:
    """记录 execute 调用，按 resource_type 返回配置的源数据行。

    通过 ``from {table} `` 前缀匹配区分源表，避免 artifact SQL 中
    JOIN ag_session 被误判为 session 源。
    """

    TABLE_BY_TYPE = {
        "session": "ag_session",
        "artifact": "ag_artifact",
        "gateway_channel": "gw_channel",
        "gateway_token": "gw_token",
        "member": "id_member",
    }

    def __init__(self, items_by_type):
        self._items = items_by_type
        self.calls = []

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if query.lstrip().upper().startswith("SELECT"):
            lower = query.lower()
            for resource_type, table in self.TABLE_BY_TYPE.items():
                if f"from {table} " in lower:
                    return _Result(rows=self._items.get(resource_type, []))
            return _Result(rows=[])
        # INSERT / UPDATE（含 tombstone）被忽略
        return _Result()


def _sample_item(resource_type: str) -> dict:
    return {
        "resource_id": f"{resource_type}_1",
        "owner_id": "usr_owner",
        "title": f"{resource_type} title",
        "summary": "summary",
        "tags": ["t"],
        "status": "active",
        "source_version": 1700000000,
        "updated_at": datetime(2026, 7, 22),
    }


@pytest.mark.parametrize(
    "resource_type",
    ["session", "artifact", "gateway_channel", "gateway_token", "member"],
)
@pytest.mark.asyncio
async def test_rebuild_search_projection_processes_each_resource_type(resource_type):
    # Arrange: 仅提供该 resource_type 的源行
    conn = _ProjectionConnection({resource_type: [_sample_item(resource_type)]})

    # Act
    counts = await search.rebuild_search_projection(conn, "wsp_test", [resource_type])

    # Assert: 计数正确，且执行了 INSERT 与 tombstone UPDATE
    assert counts == {resource_type: 1}
    insert_calls = [c for c in conn.calls if c[0].lstrip().upper().startswith("INSERT")]
    tombstone_calls = [c for c in conn.calls if "TOMBSTONE=TRUE" in c[0].upper()]
    assert len(insert_calls) == 1
    assert len(tombstone_calls) == 1


@pytest.mark.asyncio
async def test_rebuild_search_projection_assigns_visibility_per_resource_type():
    # Arrange: 5 种类型各 1 条，验证 session/artifact 为 private，其余为 workspace
    items = {rt: [_sample_item(rt)] for rt in ("session", "artifact", "gateway_channel", "gateway_token", "member")}
    conn = _ProjectionConnection(items)

    # Act
    await search.rebuild_search_projection(conn, "wsp_test")

    # Assert: 检查每个 INSERT 的 visibility 参数（params[5]）
    inserts = [(q, p) for (q, p) in conn.calls if q.lstrip().upper().startswith("INSERT")]
    visibility_by_type = {params[2]: params[5] for _, params in inserts}
    assert visibility_by_type["session"] == "private"
    assert visibility_by_type["artifact"] == "private"
    assert visibility_by_type["gateway_channel"] == "workspace"
    assert visibility_by_type["gateway_token"] == "workspace"
    assert visibility_by_type["member"] == "workspace"


@pytest.mark.asyncio
async def test_rebuild_search_projection_marks_tombstones_for_all_selected_types():
    # Arrange: 全部 7 种类型（v7.249 起含知识库/文档），但源表为空 → 仅写 tombstone
    conn = _ProjectionConnection({})

    # Act
    counts = await search.rebuild_search_projection(conn, "wsp_test")

    # Assert: 计数均为 0，但对每个类型都执行了 tombstone UPDATE
    assert counts == {
        "session": 0, "artifact": 0, "gateway_channel": 0, "gateway_token": 0, "member": 0,
        "knowledge_base": 0, "knowledge_document": 0,
    }
    tombstone_calls = [c for c in conn.calls if "TOMBSTONE=TRUE" in c[0].upper()]
    assert len(tombstone_calls) == 7


@pytest.mark.asyncio
async def test_rebuild_search_projection_respects_resource_types_filter():
    # Arrange: 仅重建 session 与 artifact
    conn = _ProjectionConnection(
        {"session": [_sample_item("session")], "artifact": [_sample_item("artifact")]}
    )

    # Act
    counts = await search.rebuild_search_projection(conn, "wsp_test", ["session", "artifact"])

    # Assert: 未选中的 resource_type 不出现在计数中
    assert counts == {"session": 1, "artifact": 1}
    assert "gateway_channel" not in counts


# --- GET /api/v1/search -------------------------------------------------


class _SearchConnection:
    """模拟 search 模块的查询连接，区分全文检索与状态聚合查询。"""

    def __init__(self, rows=None, status_row=None):
        self._rows = rows if rows is not None else []
        self._status_row = status_row or {}
        self.calls = []

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if "count(*) FILTER" in query:
            return _Result(row=self._status_row)
        return _Result(rows=self._rows)

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_global_search_returns_query_and_items(monkeypatch):
    # Arrange: 数据库返回 1 条匹配项
    rows = [{"resource_type": "session", "resource_id": "ses_1", "title": "Demo"}]
    conn = _SearchConnection(rows=rows)
    monkeypatch.setattr(search, "pool", _Pool(conn))

    # Act
    result = await search.global_search(_actor(), q="demo")

    # Assert: 返回结构包含 query 与 items
    assert result["query"] == "demo"
    assert result["partial"] is False
    assert result["items"] == rows


@pytest.mark.asyncio
async def test_global_search_forwards_query_and_workspace_filters(monkeypatch):
    # Arrange: 捕获传给 execute 的参数
    conn = _SearchConnection(rows=[])
    monkeypatch.setattr(search, "pool", _Pool(conn))

    # Act
    await search.global_search(
        _actor(workspace_id="wsp_owner"),
        q="hello",
        resource_type="session",
        updated_after=datetime(2026, 1, 1),
        limit=10,
    )

    # Assert: 第一个 execute 是 count 查询（v7.260 分页），参数顺序为
    # (workspace_id, user_id, user_id, role, resource_type, resource_type,
    #  updated_after, updated_after, q, q)
    _, params = conn.calls[0]
    assert params[0] == "wsp_owner"
    assert params[4] == "session"  # resource_type
    assert params[5] == "session"
    assert params[6] == datetime(2026, 1, 1)
    assert params[8] == "hello"
    # 第二个 execute 是 page slice，(…, limit, offset)，LIMIT 在倒数第二
    _, params2 = conn.calls[1]
    assert params2[-2] == 10


@pytest.mark.asyncio
async def test_global_search_isolates_workspaces_via_acl_filter(monkeypatch):
    # Arrange: actor 属于 wsp_a，验证查询参数中 workspace_id 必为 wsp_a，
    # 因此 wsp_b 的数据因 WHERE workspace_id=%s 而不可见。
    conn = _SearchConnection(rows=[])
    monkeypatch.setattr(search, "pool", _Pool(conn))

    # Act
    await search.global_search(_actor(workspace_id="wsp_a"), q="secret")

    # Assert: 第一个 execute 是 count 查询（v7.260 分页），workspace_id 在参数首位
    query, params = conn.calls[0]
    assert "workspace_id=%s" in query
    assert params[0] == "wsp_a"


# --- POST /api/v1/admin/search-index-rebuilds ---------------------------


class _RebuildConnection:
    def __init__(self):
        self.calls = []

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        return _Result()

    def transaction(self):
        return _Transaction()


@pytest.mark.asyncio
async def test_rebuild_search_allows_owner_to_enqueue_operation(monkeypatch):
    # Arrange: owner 角色调用，模拟 submit_operation 返回固定 operation
    conn = _RebuildConnection()
    monkeypatch.setattr(search, "pool", _Pool(conn))
    captured = {}
    monkeypatch.setattr(search, "submit_operation", _stub_submit(captured))

    # Act
    result = await search.rebuild_search(
        search.SearchRebuildRequest(resource_types=["session"]), _actor(role="owner")
    )

    # Assert: 返回 202 契约字段，且 operation 入参带上 workspace_id 与 resource_types
    assert result == {"operation_id": "op_test", "status": "queued"}
    assert captured["workspace_id"] == "wsp_test"
    assert captured["payload"] == {"workspace_id": "wsp_test", "resource_types": ["session"]}


@pytest.mark.asyncio
async def test_rebuild_search_allows_admin_role(monkeypatch):
    # Arrange: admin 角色同样允许
    conn = _RebuildConnection()
    monkeypatch.setattr(search, "pool", _Pool(conn))
    monkeypatch.setattr(search, "submit_operation", _stub_submit())

    # Act
    result = await search.rebuild_search(
        search.SearchRebuildRequest(resource_types=[]), _actor(role="admin")
    )

    # Assert
    assert result["operation_id"] == "op_test"


@pytest.mark.asyncio
async def test_rebuild_search_rejects_non_admin(monkeypatch):
    # Arrange: member 角色无权触发重建，应在校验阶段即被拒绝
    conn = _RebuildConnection()
    monkeypatch.setattr(search, "pool", _Pool(conn))
    monkeypatch.setattr(search, "submit_operation", _stub_submit())

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await search.rebuild_search(search.SearchRebuildRequest(), _actor(role="member"))
    assert exc.value.status_code == 403
    assert exc.value.detail == "Admin role required"


# --- GET /api/v1/admin/search-index-status ------------------------------


@pytest.mark.asyncio
async def test_search_status_returns_aggregate_fields(monkeypatch):
    # Arrange: 状态查询返回 4 个聚合字段
    status_row = {
        "document_count": 7,
        "tombstone_count": 2,
        "last_indexed_at": datetime(2026, 7, 22, 10, 0, 0),
        "source_updated_at": datetime(2026, 7, 22, 9, 0, 0),
    }
    conn = _SearchConnection(status_row=status_row)
    monkeypatch.setattr(search, "pool", _Pool(conn))

    # Act
    result = await search.search_status(_actor(role="admin"))

    # Assert: 字段完整透传
    assert result == status_row


@pytest.mark.asyncio
async def test_search_status_rejects_non_admin(monkeypatch):
    # Arrange: viewer 角色无权查看索引状态
    conn = _SearchConnection()
    monkeypatch.setattr(search, "pool", _Pool(conn))

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await search.search_status(_actor(role="viewer"))
    assert exc.value.status_code == 403


# --- 路由契约 ------------------------------------------------------------


def test_search_routers_expose_global_and_admin_contracts():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in search.router.routes}
    admin_paths = {
        (route.path, tuple(sorted(route.methods or ()))) for route in search.admin_router.routes
    }
    assert ("/api/v1/search", ("GET",)) in paths
    assert ("/api/v1/admin/search-index-rebuilds", ("POST",)) in admin_paths
    assert ("/api/v1/admin/search-index-status", ("GET",)) in admin_paths
