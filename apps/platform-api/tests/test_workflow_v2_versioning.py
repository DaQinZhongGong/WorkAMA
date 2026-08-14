"""工作流 v2 版本快照 / 回滚 / 对比 + M5 节点执行器补全测试。

覆盖：
- 快照创建：成功 / workflow 不存在 404 / 跨 workspace 403 / 缺权限 403 /
  未认证 401 / version 自增（6）
- 快照列表：分页 / version DESC 排序 / workspace 隔离（3）
- 快照详情：存在 / 不存在 404 / 跨 workspace 403（3）
- 回滚：成功（先快照当前 → 恢复目标 → 新建快照）/ 目标版本不存在 404 /
  跨 workspace 403（3）
- 对比：节点增删改 / 边增删改 / metadata 变更 / 相同版本无差异 /
  from_version < to_version（5）
- transform：字段映射 / 嵌套路径 / 缺失字段保留原值 / 模板插值（4）
- branch：条件命中 / 默认分支 / 条件不命中走默认 / 复合条件（4）
- webhook：SSRF 拒绝 / 超时 / 成功 / 失败 continue_on_error（4）
- delay：成功 / 超过 300 秒拒绝（2）
- parallel：所有分支成功 / 部分失败不影响其他（2）

全部使用 fake ``_Result`` / ``_RecordingConnection`` / ``_Pool`` mock，
不依赖真实 DB / Redis / 网络。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules import workflow as wf


# ============================================================================
# 测试辅助：fake pool / connection / result
# ============================================================================


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return list(self._rows)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _RecordingConnection:
    def __init__(self, results=None):
        self.calls: list[tuple[str, tuple]] = []
        self._results = list(results) if results else []
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


def _actor(
    *,
    capabilities=("workflow:*",),
    workspace_id="wsp_test",
    user_id="usr_test",
) -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_test",
        role="admin",
        email="test@example.com",
        display_name="Test",
        onboarding_completed=True,
        capabilities=capabilities,
    )


def _workflow_row(**overrides) -> dict:
    base = {
        "id": "wf_1",
        "workspace_id": "wsp_test",
        "name": "Test Workflow",
        "description": "A test workflow",
        "nodes": [
            {"id": "n1", "type": "output", "name": "out", "config": {"fields": ["answer"]}},
        ],
        "edges": [],
        "status": "published",
        "version": 1,
        "metadata": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _snapshot_row(**overrides) -> dict:
    base = {
        "id": "wfvs_1",
        "workspace_id": "wsp_test",
        "workflow_id": "wf_1",
        "version": 1,
        "snapshot": {
            "name": "Test Workflow",
            "description": "A test workflow",
            "nodes": [
                {"id": "n1", "type": "output", "name": "out", "config": {"fields": ["answer"]}},
            ],
            "edges": [],
            "status": "published",
            "version": 1,
            "metadata": {},
        },
        "changelog": None,
        "created_by": "usr_test",
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(wf.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# ============================================================================
# 1. 快照创建
# ============================================================================


class TestSnapshotCreate:
    @pytest.mark.asyncio
    async def test_create_snapshot_success(self, monkeypatch):
        """POST /snapshots 创建快照成功返回 201。"""
        snapshot = _snapshot_row(changelog="initial snapshot")
        conn = _RecordingConnection(
            results=[
                _Result(row=_workflow_row()),  # _owned_workflow
                _Result(row={"next_version": 1}),  # max(version)
                _Result(row=snapshot),  # INSERT RETURNING
            ]
        )
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.post(
                "/api/v1/workflows/wf_1/snapshots",
                json={"changelog": "initial snapshot"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["workflow_id"] == "wf_1"
        assert body["version"] == 1
        assert body["changelog"] == "initial snapshot"
        assert body["snapshot"]["name"] == "Test Workflow"
        # 验证 INSERT 语句包含 changelog 参数
        insert_calls = [q for q, _ in conn.calls if "INSERT INTO workflow_v2_version_snapshot" in q]
        assert insert_calls, "snapshot INSERT should be executed"

    @pytest.mark.asyncio
    async def test_create_snapshot_workflow_not_found_404(self, monkeypatch):
        """workflow 不存在时 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.post(
                "/api/v1/workflows/wf_missing/snapshots", json={}
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_create_snapshot_other_workspace_403(self, monkeypatch):
        """跨 workspace 创建快照 403。"""
        other = _workflow_row(workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=other)])
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.post(
                "/api/v1/workflows/wf_1/snapshots", json={}
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_create_snapshot_no_write_capability_403(self, monkeypatch):
        """只有 read 能力的 actor 不能创建快照（403）。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor(capabilities=("workflow:read",)))
        async with _client(app) as client:
            resp = await client.post(
                "/api/v1/workflows/wf_1/snapshots", json={}
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_create_snapshot_unauthenticated_401(self):
        """未认证请求 401。"""
        app = _app(actor=None)
        async with _client(app) as client:
            resp = await client.post(
                "/api/v1/workflows/wf_1/snapshots", json={}
            )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_create_snapshot_version_auto_increment(self, monkeypatch):
        """version 基于已有最大 version 自增。"""
        snapshot = _snapshot_row(version=3)
        conn = _RecordingConnection(
            results=[
                _Result(row=_workflow_row()),  # _owned_workflow
                _Result(row={"next_version": 3}),  # max(version)+1 = 3
                _Result(row=snapshot),  # INSERT RETURNING
            ]
        )
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.post(
                "/api/v1/workflows/wf_1/snapshots", json={}
            )
        assert resp.status_code == 201
        assert resp.json()["version"] == 3


# ============================================================================
# 2. 快照列表
# ============================================================================


class TestSnapshotList:
    @pytest.mark.asyncio
    async def test_list_snapshots_pagination(self, monkeypatch):
        """GET /snapshots 分页返回。"""
        rows = [_snapshot_row(version=i) for i in range(1, 4)]
        conn = _RecordingConnection(
            results=[
                _Result(row=_workflow_row()),  # _owned_workflow
                _Result(rows=rows),  # SELECT snapshots
            ]
        )
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.get(
                "/api/v1/workflows/wf_1/snapshots?limit=10&offset=0"
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 3
        assert body["limit"] == 10
        assert body["offset"] == 0

    @pytest.mark.asyncio
    async def test_list_snapshots_version_desc_order(self, monkeypatch):
        """列表按 version DESC 排序。"""
        rows = [_snapshot_row(version=3), _snapshot_row(version=2), _snapshot_row(version=1)]
        conn = _RecordingConnection(
            results=[_Result(row=_workflow_row()), _Result(rows=rows)]
        )
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.get("/api/v1/workflows/wf_1/snapshots")
        assert resp.status_code == 200
        versions = [item["version"] for item in resp.json()["items"]]
        assert versions == [3, 2, 1]

    @pytest.mark.asyncio
    async def test_list_snapshots_workspace_isolation(self, monkeypatch):
        """跨 workspace 查询快照列表 403。"""
        other = _workflow_row(workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=other)])
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.get("/api/v1/workflows/wf_1/snapshots")
        assert resp.status_code == 403


# ============================================================================
# 3. 快照详情
# ============================================================================


class TestSnapshotDetail:
    @pytest.mark.asyncio
    async def test_get_snapshot_detail_success(self, monkeypatch):
        """GET /snapshots/{version} 详情。"""
        snapshot = _snapshot_row(version=2)
        conn = _RecordingConnection(
            results=[
                _Result(row=_workflow_row()),  # _owned_workflow
                _Result(row=snapshot),  # SELECT snapshot
            ]
        )
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.get("/api/v1/workflows/wf_1/snapshots/2")
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == 2
        assert body["snapshot"]["name"] == "Test Workflow"

    @pytest.mark.asyncio
    async def test_get_snapshot_not_found_404(self, monkeypatch):
        """快照版本不存在 404。"""
        conn = _RecordingConnection(
            results=[
                _Result(row=_workflow_row()),  # _owned_workflow
                _Result(row=None),  # SELECT snapshot -> None
            ]
        )
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.get("/api/v1/workflows/wf_1/snapshots/99")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_snapshot_other_workspace_403(self, monkeypatch):
        """跨 workspace 查询快照 403。"""
        other = _workflow_row(workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=other)])
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.get("/api/v1/workflows/wf_1/snapshots/1")
        assert resp.status_code == 403


# ============================================================================
# 4. 回滚
# ============================================================================


class TestRollback:
    @pytest.mark.asyncio
    async def test_rollback_success(self, monkeypatch):
        """POST /rollback 成功：先快照当前 → 恢复目标 → 新建快照。"""
        current = _workflow_row(
            name="UpdatedName",
            version=2,
            nodes=[
                {"id": "n1", "type": "output", "config": {"fields": ["new"]}},
            ],
        )
        target_snapshot = _snapshot_row(
            version=1,
            snapshot={
                "name": "OriginalName",
                "description": "original",
                "nodes": [
                    {"id": "n1", "type": "output", "config": {"fields": ["old"]}},
                ],
                "edges": [],
                "status": "draft",
                "version": 1,
                "metadata": {"key": "v1"},
            },
        )
        restored = _workflow_row(name="OriginalName", version=3, status="draft", metadata={"key": "v1"})
        new_snapshot = _snapshot_row(version=3, changelog="rollback to v1")
        conn = _RecordingConnection(
            results=[
                _Result(row=current),  # _owned_workflow (current)
                _Result(row=current),  # _owned_workflow (inside _owned_snapshot)
                _Result(row=target_snapshot),  # SELECT target snapshot
                _Result(row={"next_version": 2}),  # max(version) for pre-rollback
                _Result(),  # INSERT pre-rollback (no RETURNING)
                _Result(row=restored),  # UPDATE workflow RETURNING *
                _Result(row=new_snapshot),  # INSERT post-rollback RETURNING *
            ]
        )
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.post(
                "/api/v1/workflows/wf_1/rollback",
                json={"version": 1},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["rolled_back_to_version"] == 1
        assert body["previous_version"] == 2
        assert body["new_snapshot_version"] == 3
        assert body["workflow"]["name"] == "OriginalName"

    @pytest.mark.asyncio
    async def test_rollback_target_not_found_404(self, monkeypatch):
        """目标版本不存在 404。"""
        conn = _RecordingConnection(
            results=[
                _Result(row=_workflow_row()),  # _owned_workflow (current)
                _Result(row=_workflow_row()),  # _owned_workflow (inside _owned_snapshot)
                _Result(row=None),  # SELECT target snapshot -> None
            ]
        )
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.post(
                "/api/v1/workflows/wf_1/rollback",
                json={"version": 99},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_rollback_other_workspace_403(self, monkeypatch):
        """跨 workspace 回滚 403。"""
        other = _workflow_row(workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=other)])
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.post(
                "/api/v1/workflows/wf_1/rollback",
                json={"version": 1},
            )
        assert resp.status_code == 403


# ============================================================================
# 5. 版本对比
# ============================================================================


class TestCompare:
    @pytest.mark.asyncio
    async def test_compare_same_version_no_diff(self, monkeypatch):
        """相同版本对比无差异。"""
        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.get(
                "/api/v1/workflows/wf_1/compare?from_version=1&to_version=1"
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["diff"]["has_changes"] is False

    @pytest.mark.asyncio
    async def test_compare_node_changes(self, monkeypatch):
        """对比节点增删改。"""
        from_snap = {
            "name": "Wf",
            "description": None,
            "nodes": [
                {"id": "n1", "type": "output", "config": {"fields": ["a"]}},
                {"id": "n2", "type": "llm_call", "config": {}},
            ],
            "edges": [],
            "status": "draft",
            "version": 1,
            "metadata": {},
        }
        to_snap = {
            "name": "Wf",
            "description": None,
            "nodes": [
                {"id": "n1", "type": "output", "config": {"fields": ["b"]}},  # changed
                {"id": "n3", "type": "tool_call", "config": {}},  # added (n2 removed)
            ],
            "edges": [],
            "status": "draft",
            "version": 2,
            "metadata": {},
        }
        conn = _RecordingConnection(
            results=[
                _Result(row=_workflow_row()),  # _owned_workflow (from)
                _Result(row=_snapshot_row(version=1, snapshot=from_snap)),
                _Result(row=_workflow_row()),  # _owned_workflow (to)
                _Result(row=_snapshot_row(version=2, snapshot=to_snap)),
            ]
        )
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.get(
                "/api/v1/workflows/wf_1/compare?from_version=1&to_version=2"
            )
        assert resp.status_code == 200
        diff = resp.json()["diff"]
        assert diff["has_changes"] is True
        added_ids = [n["id"] for n in diff["nodes"]["added"]]
        removed_ids = [n["id"] for n in diff["nodes"]["removed"]]
        changed_ids = [n["id"] for n in diff["nodes"]["changed"]]
        assert "n3" in added_ids
        assert "n2" in removed_ids
        assert "n1" in changed_ids

    @pytest.mark.asyncio
    async def test_compare_edge_changes(self, monkeypatch):
        """对比边增删改。"""
        from_snap = {
            "name": "Wf", "description": None, "nodes": [
                {"id": "n1", "type": "start", "config": {}},
                {"id": "n2", "type": "end", "config": {}},
                {"id": "n3", "type": "output", "config": {}},
            ],
            "edges": [
                {"source": "n1", "target": "n2"},
                {"source": "n1", "target": "n3"},
            ],
            "status": "draft", "version": 1, "metadata": {},
        }
        to_snap = {
            "name": "Wf", "description": None, "nodes": [
                {"id": "n1", "type": "start", "config": {}},
                {"id": "n2", "type": "end", "config": {}},
                {"id": "n3", "type": "output", "config": {}},
            ],
            "edges": [
                {"source": "n1", "target": "n3"},
                {"source": "n2", "target": "n3"},  # added
            ],
            "status": "draft", "version": 2, "metadata": {},
        }
        conn = _RecordingConnection(
            results=[
                _Result(row=_workflow_row()),
                _Result(row=_snapshot_row(version=1, snapshot=from_snap)),
                _Result(row=_workflow_row()),
                _Result(row=_snapshot_row(version=2, snapshot=to_snap)),
            ]
        )
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.get(
                "/api/v1/workflows/wf_1/compare?from_version=1&to_version=2"
            )
        assert resp.status_code == 200
        diff = resp.json()["diff"]
        assert diff["has_changes"] is True
        added_keys = [e["key"] for e in diff["edges"]["added"]]
        removed_keys = [e["key"] for e in diff["edges"]["removed"]]
        assert "n2->n3" in added_keys
        assert "n1->n2" in removed_keys

    @pytest.mark.asyncio
    async def test_compare_metadata_changes(self, monkeypatch):
        """对比 metadata 变更。"""
        from_snap = {
            "name": "Wf", "description": None, "nodes": [],
            "edges": [], "status": "draft", "version": 1,
            "metadata": {"env": "dev", "owner": "alice"},
        }
        to_snap = {
            "name": "Wf", "description": None, "nodes": [],
            "edges": [], "status": "draft", "version": 2,
            "metadata": {"env": "prod", "owner": "alice"},  # env changed
        }
        conn = _RecordingConnection(
            results=[
                _Result(row=_workflow_row()),
                _Result(row=_snapshot_row(version=1, snapshot=from_snap)),
                _Result(row=_workflow_row()),
                _Result(row=_snapshot_row(version=2, snapshot=to_snap)),
            ]
        )
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.get(
                "/api/v1/workflows/wf_1/compare?from_version=1&to_version=2"
            )
        assert resp.status_code == 200
        diff = resp.json()["diff"]
        assert diff["has_changes"] is True
        assert "env" in diff["metadata_changed"]
        assert diff["metadata_changed"]["env"]["from"] == "dev"
        assert diff["metadata_changed"]["env"]["to"] == "prod"
        assert "owner" not in diff["metadata_changed"]

    @pytest.mark.asyncio
    async def test_compare_from_lt_to(self, monkeypatch):
        """from_version < to_version 正常对比。"""
        from_snap = {
            "name": "OldName", "description": None, "nodes": [],
            "edges": [], "status": "draft", "version": 1, "metadata": {},
        }
        to_snap = {
            "name": "NewName", "description": "updated", "nodes": [],
            "edges": [], "status": "published", "version": 2, "metadata": {},
        }
        conn = _RecordingConnection(
            results=[
                _Result(row=_workflow_row()),
                _Result(row=_snapshot_row(version=1, snapshot=from_snap)),
                _Result(row=_workflow_row()),
                _Result(row=_snapshot_row(version=2, snapshot=to_snap)),
            ]
        )
        monkeypatch.setattr(wf, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.get(
                "/api/v1/workflows/wf_1/compare?from_version=1&to_version=2"
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["from_version"] == 1
        assert body["to_version"] == 2
        diff = body["diff"]
        assert diff["fields_changed"]["name"]["from"] == "OldName"
        assert diff["fields_changed"]["name"]["to"] == "NewName"
        assert diff["fields_changed"]["status"]["from"] == "draft"
        assert diff["fields_changed"]["status"]["to"] == "published"


# ============================================================================
# 6. transform 节点执行器
# ============================================================================


class TestTransformNode:
    def test_transform_field_mapping(self):
        """字段映射：{{input.name}} 提取。"""
        node = {"id": "t1", "type": "transform", "config": {
            "mapping": {"greeting": "{{input.name}}"}
        }}
        inputs = {"input": {"name": "Alice"}}
        output = wf._execute_transform(node, inputs)
        assert output["transformed"]["greeting"] == "Alice"
        assert output["method"] == "transform"

    def test_transform_nested_path(self):
        """嵌套路径：{{input.user.address.city}} 提取。"""
        node = {"id": "t1", "type": "transform", "config": {
            "mapping": {"city": "{{input.user.address.city}}"}
        }}
        inputs = {"input": {"user": {"address": {"city": "NYC"}}}}
        output = wf._execute_transform(node, inputs)
        assert output["transformed"]["city"] == "NYC"

    def test_transform_missing_field_preserves_original(self):
        """缺失字段保留原字符串。"""
        node = {"id": "t1", "type": "transform", "config": {
            "mapping": {"out": "{{input.missing}}"}
        }}
        inputs = {"input": {}}
        output = wf._execute_transform(node, inputs)
        # 不可解析时保留原始模板字符串
        assert output["transformed"]["out"] == "{{input.missing}}"

    def test_transform_template_interpolation(self):
        """模板插值：混合字符串。"""
        node = {"id": "t1", "type": "transform", "config": {
            "mapping": {"msg": "Hello {{input.name}}, score={{input.score}}"}
        }}
        inputs = {"input": {"name": "Alice", "score": 95}}
        output = wf._execute_transform(node, inputs)
        assert output["transformed"]["msg"] == "Hello Alice, score=95"


# ============================================================================
# 7. branch 节点执行器
# ============================================================================


class TestBranchNode:
    def test_branch_condition_hit(self):
        """条件命中：score > 0.8 → node_a。"""
        node = {"id": "b1", "type": "branch", "config": {
            "cases": [
                {"when": "input.score > 0.8", "next": "node_a"},
                {"when": "*", "next": "node_b"},
            ]
        }}
        output, branch = wf._execute_branch(node, {"input": {"score": 0.9}})
        assert branch == "node_a"
        assert output["matched"] == "input.score > 0.8"

    def test_branch_default_branch(self):
        """默认分支：'*' 直接命中。"""
        node = {"id": "b1", "type": "branch", "config": {
            "cases": [{"when": "*", "next": "node_default"}]
        }}
        output, branch = wf._execute_branch(node, {"input": {}})
        assert branch == "node_default"
        assert output["matched"] == "*"

    def test_branch_no_match_goes_default(self):
        """条件不命中走默认分支。"""
        node = {"id": "b1", "type": "branch", "config": {
            "cases": [
                {"when": "input.score > 0.8", "next": "node_a"},
                {"when": "*", "next": "node_b"},
            ]
        }}
        output, branch = wf._execute_branch(node, {"input": {"score": 0.5}})
        assert branch == "node_b"

    def test_branch_compound_condition(self):
        """复合条件 and/or。"""
        node = {"id": "b1", "type": "branch", "config": {
            "cases": [
                {"when": "input.score > 0.8 and input.level == 2", "next": "high"},
                {"when": "*", "next": "low"},
            ]
        }}
        # 命中复合条件
        _, branch = wf._execute_branch(node, {"input": {"score": 0.9, "level": 2}})
        assert branch == "high"
        # 不命中（level 不匹配）
        _, branch = wf._execute_branch(node, {"input": {"score": 0.9, "level": 1}})
        assert branch == "low"


# ============================================================================
# 8. webhook 节点执行器
# ============================================================================


class _MockHTTPResponse:
    """模拟 httpx.Response。"""

    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json_data = json_data
        self._text = text if text else (
            "" if json_data is None else __import__("json").dumps(json_data)
        )
        self.headers = headers or (
            {"content-type": "application/json"} if json_data is not None
            else {"content-type": "text/plain"}
        )

    def json(self):
        if self._json_data is None:
            raise ValueError("no json body")
        return self._json_data

    @property
    def text(self):
        return self._text


class TestWebhookNode:
    @pytest.mark.asyncio
    async def test_webhook_ssrf_rejected(self, monkeypatch):
        """SSRF 防护：非白名单主机被拒绝。"""
        monkeypatch.setattr(wf, "_http_allowed_hosts", lambda: {"allowed.example.com"})
        node = {"id": "wh1", "type": "webhook", "config": {
            "url": "http://evil.com/hook",
            "method": "POST",
        }}
        output = await wf._execute_webhook(
            node, {}, workspace_id="wsp_test", actor=_actor()
        )
        assert "error" in output
        assert "forbidden" in output["error"]
        assert output["method"] == "mock"

    @pytest.mark.asyncio
    async def test_webhook_timeout(self, monkeypatch):
        """超时返回 error。"""
        monkeypatch.setattr(wf, "_http_allowed_hosts", lambda: {"allowed.example.com"})

        def _raise_timeout(**kwargs):
            raise httpx.TimeoutException("timed out")

        monkeypatch.setattr(httpx, "request", _raise_timeout)
        node = {"id": "wh1", "type": "webhook", "config": {
            "url": "http://allowed.example.com/hook",
            "method": "POST",
            "timeout": 0.1,
        }}
        output = await wf._execute_webhook(
            node, {}, workspace_id="wsp_test", actor=_actor()
        )
        assert "error" in output
        assert "timeout" in output["error"]
        assert output["continue_on_error"] is True

    @pytest.mark.asyncio
    async def test_webhook_success(self, monkeypatch):
        """成功调用 webhook。"""
        monkeypatch.setattr(wf, "_http_allowed_hosts", lambda: {"allowed.example.com"})
        mock_resp = _MockHTTPResponse(
            status_code=200, json_data={"ok": True}
        )
        monkeypatch.setattr(httpx, "request", lambda **kwargs: mock_resp)
        node = {"id": "wh1", "type": "webhook", "config": {
            "url": "http://allowed.example.com/hook",
            "method": "POST",
            "body": {"event": "test"},
        }}
        output = await wf._execute_webhook(
            node, {}, workspace_id="wsp_test", actor=_actor()
        )
        assert output["status_code"] == 200
        assert output["body"] == {"ok": True}
        assert output["method"] == "http"

    @pytest.mark.asyncio
    async def test_webhook_failure_continue_on_error(self, monkeypatch):
        """失败时 continue_on_error 标记保留。"""
        monkeypatch.setattr(wf, "_http_allowed_hosts", lambda: {"allowed.example.com"})

        def _raise_connect(**kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "request", _raise_connect)
        node = {"id": "wh1", "type": "webhook", "config": {
            "url": "http://allowed.example.com/hook",
            "method": "POST",
            "continue_on_error": False,
        }}
        output = await wf._execute_webhook(
            node, {}, workspace_id="wsp_test", actor=_actor()
        )
        assert "error" in output
        assert "connection_error" in output["error"]
        assert output["continue_on_error"] is False


# ============================================================================
# 9. delay 节点执行器
# ============================================================================


class TestDelayNode:
    @pytest.mark.asyncio
    async def test_delay_success(self):
        """delay 0 秒立即返回成功。"""
        node = {"id": "d1", "type": "delay", "config": {"seconds": 0}}
        output = await wf._execute_delay(node, {})
        assert output["waited_seconds"] == 0.0
        assert output["method"] == "delay"

    @pytest.mark.asyncio
    async def test_delay_exceeds_max_rejected(self):
        """超过 300 秒被拒绝。"""
        node = {"id": "d1", "type": "delay", "config": {"seconds": 301}}
        output = await wf._execute_delay(node, {})
        assert "error" in output
        assert "300" in output["error"]
        assert output["method"] == "mock"


# ============================================================================
# 10. parallel 节点执行器
# ============================================================================


class TestParallelNode:
    @pytest.mark.asyncio
    async def test_parallel_all_branches_success(self):
        """所有分支成功执行。"""
        workflow_nodes = [
            {"id": "n1", "type": "output", "config": {"fields": ["q"]}},
            {"id": "n2", "type": "output", "config": {"fields": ["q"]}},
        ]
        node = {"id": "p1", "type": "parallel", "config": {
            "branches": ["n1", "n2"]
        }}
        output = await wf._execute_parallel(
            node, {"q": "hello"},
            workspace_id="wsp_test",
            actor=_actor(),
            workflow_nodes=workflow_nodes,
        )
        assert output["method"] == "parallel"
        assert "n1" in output["parallel_results"]
        assert "n2" in output["parallel_results"]
        # output 节点会透传 q 字段
        assert output["parallel_results"]["n1"]["q"] == "hello"
        assert output["parallel_results"]["n2"]["q"] == "hello"

    @pytest.mark.asyncio
    async def test_parallel_partial_failure_doesnt_affect_others(self):
        """部分分支失败不影响其他分支。"""
        workflow_nodes = [
            {"id": "n1", "type": "output", "config": {"fields": ["q"]}},
            # n2 不在 workflow_nodes 中 → 分支失败
        ]
        node = {"id": "p1", "type": "parallel", "config": {
            "branches": ["n1", "n2_missing"]
        }}
        output = await wf._execute_parallel(
            node, {"q": "hello"},
            workspace_id="wsp_test",
            actor=_actor(),
            workflow_nodes=workflow_nodes,
        )
        assert output["method"] == "parallel"
        # n1 成功
        assert "n1" in output["parallel_results"]
        assert output["parallel_results"]["n1"]["q"] == "hello"
        # n2_missing 失败但记录 error
        assert "n2_missing" in output["parallel_results"]
        assert "error" in output["parallel_results"]["n2_missing"]
