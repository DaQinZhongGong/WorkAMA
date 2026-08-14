"""工作区深度完善模块 (workspace) 单元 + 端点测试。

v7.150: 30 个测试覆盖：
- 工作区 CRUD：创建 / 列表 / 详情 / 更新 / 删除（5）
- 成员管理：添加 / 列表 / 更新角色 / 移除 / 不能移除 owner（5）
- 邀请：创建 / 列表 / 接受 / 撤销 / 过期（5）
- 权限矩阵：默认矩阵 / 更新 / check_permission 辅助 / 越权拒绝（4）
- 多租户隔离：跨工作区访问 403（4）
- 鉴权：未认证 401（1）
- 角色：owner/admin/member/viewer/guest 各自能力（6）

所有测试使用 fake `_Result`/`_RecordingConnection`/`_Pool`，不依赖真实 DB / Redis / 网络。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules import workspace as ws
from workama_platform.modules.workspace import (
    DEFAULT_PERMISSION_MATRIX,
    PERMISSIONS,
    ROLES,
    check_permission,
    check_permission_for_role,
)


# ============================================================================
# 测试辅助：fake pool / connection / result
# ============================================================================


class _Result:
    """模拟 psycopg 查询结果。"""

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
    """记录 execute 调用并按序返回配置的结果。"""

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


class _Pool:
    """模拟连接池。"""

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
    role: str = "owner",
    capabilities=("workspace:*",),
    workspace_id: str = "wsp_test",
    user_id: str = "usr_owner",
    org_id: str = "org_test",
) -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id=org_id,
        role=role,
        email="owner@example.com",
        display_name="Owner",
        onboarding_completed=True,
        capabilities=capabilities,
    )


def _ws_row(**overrides) -> dict:
    base = {
        "id": "wsp_test",
        "org_id": "org_test",
        "name": "Test Workspace",
        "slug": "test",
        "description": None,
        "plan": "free",
        "status": "active",
        "settings": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _member_row(**overrides) -> dict:
    base = {
        "id": "mem_1",
        "workspace_id": "wsp_test",
        "user_id": "usr_owner",
        "role": "owner",
        "status": "active",
        "joined_at": datetime.now(UTC),
        "metadata": {},
    }
    base.update(overrides)
    return base


def _invite_row(**overrides) -> dict:
    base = {
        "id": "inv_1",
        "workspace_id": "wsp_test",
        "email": "invite@example.com",
        "role": "member",
        "token": "tok_abc",
        "invited_by": "usr_owner",
        "status": "pending",
        "expires_at": datetime.now(UTC) + timedelta(days=7),
        "accepted_at": None,
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(ws.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# ============================================================================
# 1. 工作区 CRUD（5）
# ============================================================================


class TestWorkspaceCRUD:
    """工作区 CRUD 端点测试。"""

    @pytest.mark.asyncio
    async def test_create_workspace_success(self, monkeypatch):
        """POST / 创建工作区返回 201，actor 自动成为 owner。"""
        ws_row = _ws_row(id="wsp_new", slug="new-ws", name="New WS")
        # 1. slug 查重 (fetchone None) → 2. INSERT workspace_v2 →
        # 3. INSERT workspace_member (owner) → 4-48. _seed_default_permissions (45 INSERTs) →
        # 49. SELECT workspace_v2
        conn = _RecordingConnection(
            results=[
                _Result(row=None),  # slug 查重
                _Result(),  # INSERT workspace_v2
                _Result(),  # INSERT workspace_member
                *[_Result() for _ in range(45)],  # seed permissions
                _Result(row=ws_row),  # SELECT workspace_v2
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.post(
                "/api/v1/workspaces",
                json={"name": "New WS", "slug": "new-ws"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "New WS"
        assert body["slug"] == "new-ws"
        # 验证 INSERT workspace_v2 与 INSERT workspace_member 被调用
        assert any("INSERT INTO workspace_v2" in q for q, _ in conn.calls)
        assert any("INSERT INTO workspace_member" in q for q, _ in conn.calls)

    @pytest.mark.asyncio
    async def test_list_workspaces(self, monkeypatch):
        """GET / 列出当前用户所在的工作区。"""
        rows = [
            _ws_row(id="wsp_a", slug="a"),
            _ws_row(id="wsp_b", slug="b"),
        ]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.get("/api/v1/workspaces")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert len(body["items"]) == 2
        # 验证 SQL 按 user_id 过滤
        query, params = conn.calls[0]
        assert "m.user_id = %s" in query
        assert params[0] == "usr_owner"

    @pytest.mark.asyncio
    async def test_get_workspace_detail(self, monkeypatch):
        """GET /{ws_id} 返回工作区详情。"""
        ws_row = _ws_row()
        member_row = _member_row()
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # _require_member: SELECT workspace_v2
                _Result(row=member_row),  # _require_member: SELECT workspace_member
                _Result(row=ws_row),  # endpoint: SELECT workspace_v2
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.get("/api/v1/workspaces/wsp_test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "wsp_test"
        assert body["name"] == "Test Workspace"

    @pytest.mark.asyncio
    async def test_update_workspace(self, monkeypatch):
        """PATCH /{ws_id} 更新 name/description/settings。"""
        ws_row = _ws_row()
        member_row = _member_row(role="admin")
        updated_row = _ws_row(name="Renamed", description="Updated")
        # _require_workspace_permission(workspace.write):
        #   _require_member: SELECT workspace_v2 + SELECT workspace_member
        #   _load_permission_matrix: SELECT workspace_role_permission
        # endpoint: UPDATE workspace_v2 RETURNING
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # SELECT workspace_v2
                _Result(row=member_row),  # SELECT workspace_member
                _Result(rows=[]),  # SELECT workspace_role_permission (空→用默认矩阵)
                _Result(row=updated_row),  # UPDATE RETURNING
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with _client(app) as client:
            resp = await client.patch(
                "/api/v1/workspaces/wsp_test",
                json={"name": "Renamed", "description": "Updated"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Renamed"
        assert body["description"] == "Updated"
        # 验证 UPDATE SQL
        assert any("UPDATE workspace_v2" in q for q, _ in conn.calls)

    @pytest.mark.asyncio
    async def test_delete_workspace_owner_only(self, monkeypatch):
        """DELETE /{ws_id} owner 删除工作区返回 204。"""
        ws_row = _ws_row()
        member_row = _member_row(role="owner")
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # SELECT workspace_v2
                _Result(row=member_row),  # SELECT workspace_member
                _Result(rows=[]),  # SELECT workspace_role_permission
                # DELETE 4 张表，均无需返回
                _Result(),
                _Result(),
                _Result(),
                _Result(),
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="owner"))
        async with _client(app) as client:
            resp = await client.delete("/api/v1/workspaces/wsp_test")
        assert resp.status_code == 204
        # 验证 4 个 DELETE 语句
        delete_calls = [q for q, _ in conn.calls if q.strip().startswith("DELETE FROM")]
        assert len(delete_calls) == 4


# ============================================================================
# 2. 成员管理（5）
# ============================================================================


class TestMemberManagement:
    """成员管理端点测试。"""

    @pytest.mark.asyncio
    async def test_add_member(self, monkeypatch):
        """POST /{ws_id}/members 添加成员返回 201。"""
        ws_row = _ws_row()
        member_row = _member_row(role="owner")
        new_member = _member_row(
            id="mem_new", user_id="usr_new", role="member"
        )
        # _require_workspace_permission(member.invite):
        #   _require_member: SELECT workspace_v2 + SELECT workspace_member
        #   _load_permission_matrix: SELECT workspace_role_permission
        # existing check: SELECT workspace_member (None)
        # INSERT workspace_member
        # SELECT workspace_member (return new row)
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # SELECT workspace_v2
                _Result(row=member_row),  # SELECT workspace_member (actor)
                _Result(rows=[]),  # SELECT workspace_role_permission
                _Result(row=None),  # existing member check
                _Result(),  # INSERT workspace_member
                _Result(row=new_member),  # SELECT workspace_member
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="owner"))
        async with _client(app) as client:
            resp = await client.post(
                "/api/v1/workspaces/wsp_test/members",
                json={"user_id": "usr_new", "role": "member"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["user_id"] == "usr_new"
        assert body["role"] == "member"

    @pytest.mark.asyncio
    async def test_list_members(self, monkeypatch):
        """GET /{ws_id}/members 列出工作区成员。"""
        ws_row = _ws_row()
        member_row = _member_row()
        members = [
            _member_row(id="mem_1", user_id="usr_a", role="owner"),
            _member_row(id="mem_2", user_id="usr_b", role="member"),
        ]
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # _require_member: SELECT workspace_v2
                _Result(row=member_row),  # _require_member: SELECT workspace_member
                _Result(rows=members),  # endpoint: SELECT workspace_member
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.get("/api/v1/workspaces/wsp_test/members")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert len(body["items"]) == 2

    @pytest.mark.asyncio
    async def test_update_member_role(self, monkeypatch):
        """PATCH /{ws_id}/members/{user_id} 更新成员角色。"""
        ws_row = _ws_row()
        actor_member = _member_row(role="owner")
        target_member = _member_row(
            id="mem_target", user_id="usr_target", role="member"
        )
        updated_member = _member_row(
            id="mem_target", user_id="usr_target", role="admin"
        )
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # SELECT workspace_v2
                _Result(row=actor_member),  # SELECT workspace_member (actor)
                _Result(rows=[]),  # SELECT workspace_role_permission
                _Result(row=target_member),  # SELECT target member FOR UPDATE
                _Result(),  # UPDATE workspace_member
                _Result(row=updated_member),  # SELECT updated member
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="owner"))
        async with _client(app) as client:
            resp = await client.patch(
                "/api/v1/workspaces/wsp_test/members/usr_target",
                json={"role": "admin"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["role"] == "admin"

    @pytest.mark.asyncio
    async def test_remove_member(self, monkeypatch):
        """DELETE /{ws_id}/members/{user_id} 移除非 owner 成员返回 204。"""
        ws_row = _ws_row()
        actor_member = _member_row(role="owner")
        target_member = _member_row(
            id="mem_target", user_id="usr_target", role="member"
        )
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # SELECT workspace_v2
                _Result(row=actor_member),  # SELECT workspace_member (actor)
                _Result(rows=[]),  # SELECT workspace_role_permission
                _Result(row=target_member),  # SELECT target member FOR UPDATE
                _Result(),  # DELETE workspace_member
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="owner"))
        async with _client(app) as client:
            resp = await client.delete(
                "/api/v1/workspaces/wsp_test/members/usr_target"
            )
        assert resp.status_code == 204
        assert any("DELETE FROM workspace_member" in q for q, _ in conn.calls)

    @pytest.mark.asyncio
    async def test_cannot_remove_owner(self, monkeypatch):
        """DELETE /{ws_id}/members/{user_id} 移除 owner 返回 400。"""
        ws_row = _ws_row()
        actor_member = _member_row(role="owner")
        target_member = _member_row(
            id="mem_target", user_id="usr_target", role="owner"
        )
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # SELECT workspace_v2
                _Result(row=actor_member),  # SELECT workspace_member (actor)
                _Result(rows=[]),  # SELECT workspace_role_permission
                _Result(row=target_member),  # SELECT target member FOR UPDATE
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="owner"))
        async with _client(app) as client:
            resp = await client.delete(
                "/api/v1/workspaces/wsp_test/members/usr_target"
            )
        assert resp.status_code == 400
        assert "owner" in resp.json()["detail"].lower()


# ============================================================================
# 3. 邀请（5）
# ============================================================================


class TestInvites:
    """邀请管理端点测试。"""

    @pytest.mark.asyncio
    async def test_create_invite(self, monkeypatch):
        """POST /{ws_id}/invites 创建邀请返回 201。"""
        ws_row = _ws_row()
        member_row = _member_row(role="admin")
        invite_row = _invite_row(
            id="inv_new", email="new@example.com", role="member"
        )
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # SELECT workspace_v2
                _Result(row=member_row),  # SELECT workspace_member
                _Result(rows=[]),  # SELECT workspace_role_permission
                _Result(row=None),  # existing pending invite check
                _Result(),  # INSERT workspace_invite
                _Result(row=invite_row),  # SELECT workspace_invite
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with _client(app) as client:
            resp = await client.post(
                "/api/v1/workspaces/wsp_test/invites",
                json={"email": "new@example.com", "role": "member"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["email"] == "new@example.com"
        assert body["role"] == "member"
        assert "token" in body

    @pytest.mark.asyncio
    async def test_list_invites(self, monkeypatch):
        """GET /{ws_id}/invites 列出工作区邀请。"""
        ws_row = _ws_row()
        member_row = _member_row(role="admin")
        invites = [
            _invite_row(id="inv_1", email="a@example.com"),
            _invite_row(id="inv_2", email="b@example.com"),
        ]
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # SELECT workspace_v2
                _Result(row=member_row),  # SELECT workspace_member
                _Result(rows=[]),  # SELECT workspace_role_permission
                _Result(),  # UPDATE expired invites
                _Result(rows=invites),  # SELECT workspace_invite
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with _client(app) as client:
            resp = await client.get("/api/v1/workspaces/wsp_test/invites")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2

    @pytest.mark.asyncio
    async def test_accept_invite(self, monkeypatch):
        """POST /invites/{token}/accept 接受邀请返回 200。"""
        invite_row = _invite_row(
            token="tok_valid",
            status="pending",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        ws_row = _ws_row()
        conn = _RecordingConnection(
            results=[
                _Result(row=invite_row),  # SELECT workspace_invite FOR UPDATE
                _Result(row=None),  # existing member check
                _Result(),  # INSERT workspace_member
                _Result(),  # UPDATE workspace_invite (accepted)
                _Result(row=ws_row),  # SELECT workspace_v2
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member", user_id="usr_invitee"))
        async with _client(app) as client:
            resp = await client.post("/api/v1/workspaces/invites/tok_valid/accept")
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] is True
        assert body["role"] == "member"
        assert body["workspace_id"] == "wsp_test"

    @pytest.mark.asyncio
    async def test_revoke_invite(self, monkeypatch):
        """DELETE /invites/{invite_id} 撤销邀请返回 204。"""
        invite_row = _invite_row(id="inv_1", status="pending")
        ws_row = _ws_row()
        member_row = _member_row(role="admin")
        conn = _RecordingConnection(
            results=[
                _Result(row=invite_row),  # SELECT workspace_invite FOR UPDATE
                _Result(row=ws_row),  # SELECT workspace_v2
                _Result(row=member_row),  # SELECT workspace_member
                _Result(rows=[]),  # SELECT workspace_role_permission
                _Result(),  # UPDATE workspace_invite (revoked)
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with _client(app) as client:
            resp = await client.delete("/api/v1/workspaces/invites/inv_1")
        assert resp.status_code == 204
        # 验证 UPDATE status='revoked'
        revoke_calls = [
            q for q, _ in conn.calls
            if "UPDATE workspace_invite" in q and "revoked" in q
        ]
        assert len(revoke_calls) == 1

    @pytest.mark.asyncio
    async def test_accept_expired_invite_returns_410(self, monkeypatch):
        """POST /invites/{token}/accept 过期邀请返回 410。"""
        invite_row = _invite_row(
            token="tok_expired",
            status="pending",
            expires_at=datetime.now(UTC) - timedelta(days=1),  # 已过期
        )
        conn = _RecordingConnection(
            results=[
                _Result(row=invite_row),  # SELECT workspace_invite FOR UPDATE
                _Result(),  # UPDATE workspace_invite (expired)
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member", user_id="usr_invitee"))
        async with _client(app) as client:
            resp = await client.post("/api/v1/workspaces/invites/tok_expired/accept")
        assert resp.status_code == 410
        assert "expired" in resp.json()["detail"].lower()


# ============================================================================
# 4. 权限矩阵（4）
# ============================================================================


class TestPermissionMatrix:
    """权限矩阵测试。"""

    @pytest.mark.asyncio
    async def test_get_default_permission_matrix(self, monkeypatch):
        """GET /{ws_id}/permissions 返回默认权限矩阵（DB 无行时回退默认）。"""
        ws_row = _ws_row()
        member_row = _member_row(role="owner")
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # _require_member: SELECT workspace_v2
                _Result(row=member_row),  # _require_member: SELECT workspace_member
                _Result(rows=[]),  # _load_permission_matrix: SELECT (空)
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.get("/api/v1/workspaces/wsp_test/permissions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["workspace_id"] == "wsp_test"
        matrix = body["matrix"]
        # owner 全部权限为 True
        assert all(matrix["owner"][p] for p in PERMISSIONS)
        # guest 不能 settings.read
        assert matrix["guest"]["settings.read"] is False
        # member 不能 member.invite
        assert matrix["member"]["member.invite"] is False

    @pytest.mark.asyncio
    async def test_update_permission_matrix(self, monkeypatch):
        """PUT /{ws_id}/permissions 更新权限矩阵（owner）。"""
        ws_row = _ws_row()
        member_row = _member_row(role="owner")
        custom_matrix = {
            "owner": {p: True for p in PERMISSIONS},
            "admin": {p: True for p in PERMISSIONS},
            "member": {p: False for p in PERMISSIONS},
            "viewer": {p: False for p in PERMISSIONS},
            "guest": {p: False for p in PERMISSIONS},
        }
        # _load_permission_matrix 的 SELECT 返回与 custom_matrix 一致的行
        matrix_rows = [
            {"role": role, "permission": perm, "granted": custom_matrix[role][perm]}
            for role in ROLES
            for perm in PERMISSIONS
        ]
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # _require_member: SELECT workspace_v2
                _Result(row=member_row),  # _require_member: SELECT workspace_member
                _Result(),  # DELETE workspace_role_permission
                *[_Result() for _ in range(45)],  # INSERT 45 rows
                _Result(rows=matrix_rows),  # _load_permission_matrix: SELECT
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="owner"))
        async with _client(app) as client:
            resp = await client.put(
                "/api/v1/workspaces/wsp_test/permissions",
                json={"matrix": custom_matrix},
            )
        assert resp.status_code == 200
        body = resp.json()
        # admin 应全部为 True（按 custom_matrix）
        assert all(body["matrix"]["admin"][p] for p in PERMISSIONS)
        # member 应全部为 False
        assert not any(body["matrix"]["member"][p] for p in PERMISSIONS)
        # 验证 DELETE + INSERT 调用
        assert any("DELETE FROM workspace_role_permission" in q for q, _ in conn.calls)
        insert_calls = [q for q, _ in conn.calls if "INSERT INTO workspace_role_permission" in q]
        assert len(insert_calls) == 45

    def test_check_permission_helper_uses_default_matrix(self):
        """check_permission(actor, permission) 基于默认矩阵检查。"""
        owner = _actor(role="owner")
        member = _actor(role="member")
        viewer = _actor(role="viewer")
        guest = _actor(role="guest")

        # owner 拥有所有权限
        for perm in PERMISSIONS:
            assert check_permission(owner, perm) is True
        # member 有 workspace.read 但没有 member.invite
        assert check_permission(member, "workspace.read") is True
        assert check_permission(member, "member.invite") is False
        # viewer 有 settings.read 但没有 workspace.write
        assert check_permission(viewer, "settings.read") is True
        assert check_permission(viewer, "workspace.write") is False
        # guest 有 workspace.read 但没有 settings.read
        assert check_permission(guest, "workspace.read") is True
        assert check_permission(guest, "settings.read") is False

    def test_check_permission_for_role_with_custom_matrix(self):
        """check_permission_for_role 接受自定义矩阵覆盖默认。"""
        custom = {
            "owner": {p: True for p in PERMISSIONS},
            "member": {p: False for p in PERMISSIONS},
        }
        # 默认矩阵下 member 有 workspace.read
        assert check_permission_for_role("member", "workspace.read") is True
        # 自定义矩阵下 member 没有 workspace.read
        assert (
            check_permission_for_role("member", "workspace.read", custom) is False
        )
        # 未知角色返回 False
        assert check_permission_for_role("unknown", "workspace.read") is False
        # 未知权限返回 False
        assert check_permission_for_role("owner", "unknown.perm") is False

    @pytest.mark.asyncio
    async def test_member_cannot_invite_returns_403(self, monkeypatch):
        """member 角色调用 POST /{ws_id}/members 返回 403（缺 member.invite）。"""
        ws_row = _ws_row()
        member_row = _member_row(role="member")  # actor 是 member
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # SELECT workspace_v2
                _Result(row=member_row),  # SELECT workspace_member
                _Result(rows=[]),  # SELECT workspace_role_permission (空→默认矩阵)
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with _client(app) as client:
            resp = await client.post(
                "/api/v1/workspaces/wsp_test/members",
                json={"user_id": "usr_new", "role": "member"},
            )
        assert resp.status_code == 403
        assert "member.invite" in resp.json()["detail"]


# ============================================================================
# 5. 多租户隔离（4）
# ============================================================================


class TestMultiTenantIsolation:
    """多租户隔离测试：跨工作区访问返回 403。"""

    @pytest.mark.asyncio
    async def test_get_workspace_cross_workspace_403(self, monkeypatch):
        """GET /{ws_id} actor 不是该工作区成员返回 403。"""
        ws_row = _ws_row(id="wsp_other")
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # SELECT workspace_v2 (存在)
                _Result(row=None),  # SELECT workspace_member (actor 非成员)
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test"))
        async with _client(app) as client:
            resp = await client.get("/api/v1/workspaces/wsp_other")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_list_members_cross_workspace_403(self, monkeypatch):
        """GET /{ws_id}/members actor 不是该工作区成员返回 403。"""
        ws_row = _ws_row(id="wsp_other")
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # SELECT workspace_v2 (存在)
                _Result(row=None),  # SELECT workspace_member (非成员)
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.get("/api/v1/workspaces/wsp_other/members")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_add_member_cross_workspace_403(self, monkeypatch):
        """POST /{ws_id}/members actor 不是该工作区成员返回 403。"""
        ws_row = _ws_row(id="wsp_other")
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # SELECT workspace_v2
                _Result(row=None),  # SELECT workspace_member (非成员)
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.post(
                "/api/v1/workspaces/wsp_other/members",
                json={"user_id": "usr_new", "role": "member"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_get_workspace_returns_404_when_not_exist(self, monkeypatch):
        """GET /{ws_id} 工作区不存在返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.get("/api/v1/workspaces/wsp_missing")
        assert resp.status_code == 404


# ============================================================================
# 6. 鉴权（1）
# ============================================================================


class TestAuth:
    """鉴权测试。"""

    @pytest.mark.asyncio
    async def test_list_workspaces_requires_authentication(self):
        """未认证请求 GET / 返回 401。"""
        app = _app(actor=None)
        async with _client(app) as client:
            resp = await client.get("/api/v1/workspaces")
        assert resp.status_code == 401


# ============================================================================
# 7. 角色能力（6）
# ============================================================================


class TestRoleCapabilities:
    """不同角色的能力边界测试。"""

    @pytest.mark.asyncio
    async def test_owner_can_delete_workspace(self, monkeypatch):
        """owner 调用 DELETE /{ws_id} 通过权限检查（workspace.delete=True）。"""
        ws_row = _ws_row()
        member_row = _member_row(role="owner")
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # SELECT workspace_v2
                _Result(row=member_row),  # SELECT workspace_member
                _Result(rows=[]),  # SELECT workspace_role_permission
                _Result(),  # DELETE role_permission
                _Result(),  # DELETE member
                _Result(),  # DELETE invite
                _Result(),  # DELETE workspace_v2
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="owner"))
        async with _client(app) as client:
            resp = await client.delete("/api/v1/workspaces/wsp_test")
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_admin_cannot_delete_workspace(self, monkeypatch):
        """admin 调用 DELETE /{ws_id} 返回 403（workspace.delete=False）。"""
        ws_row = _ws_row()
        member_row = _member_row(role="admin")
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # SELECT workspace_v2
                _Result(row=member_row),  # SELECT workspace_member
                _Result(rows=[]),  # SELECT workspace_role_permission (默认矩阵)
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with _client(app) as client:
            resp = await client.delete("/api/v1/workspaces/wsp_test")
        assert resp.status_code == 403
        assert "workspace.delete" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_member_cannot_create_invite(self, monkeypatch):
        """member 调用 POST /{ws_id}/invites 返回 403（member.invite=False）。"""
        ws_row = _ws_row()
        member_row = _member_row(role="member")
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # SELECT workspace_v2
                _Result(row=member_row),  # SELECT workspace_member
                _Result(rows=[]),  # SELECT workspace_role_permission
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with _client(app) as client:
            resp = await client.post(
                "/api/v1/workspaces/wsp_test/invites",
                json={"email": "x@example.com"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_viewer_cannot_update_workspace(self, monkeypatch):
        """viewer 调用 PATCH /{ws_id} 返回 403（workspace.write=False）。"""
        ws_row = _ws_row()
        member_row = _member_row(role="viewer")
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # SELECT workspace_v2
                _Result(row=member_row),  # SELECT workspace_member
                _Result(rows=[]),  # SELECT workspace_role_permission
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="viewer"))
        async with _client(app) as client:
            resp = await client.patch(
                "/api/v1/workspaces/wsp_test",
                json={"name": "Hacked"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_guest_can_read_workspace(self, monkeypatch):
        """guest 调用 GET /{ws_id} 返回 200（workspace.read=True）。"""
        ws_row = _ws_row()
        member_row = _member_row(role="guest")
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # _require_member: SELECT workspace_v2
                _Result(row=member_row),  # _require_member: SELECT workspace_member
                _Result(row=ws_row),  # endpoint: SELECT workspace_v2
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="guest"))
        async with _client(app) as client:
            resp = await client.get("/api/v1/workspaces/wsp_test")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_admin_can_manage_members(self, monkeypatch):
        """admin 调用 POST /{ws_id}/members 通过权限检查（member.invite=True）。"""
        ws_row = _ws_row()
        admin_member = _member_row(role="admin")
        new_member = _member_row(id="mem_new", user_id="usr_new", role="member")
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # SELECT workspace_v2
                _Result(row=admin_member),  # SELECT workspace_member
                _Result(rows=[]),  # SELECT workspace_role_permission (默认矩阵)
                _Result(row=None),  # existing member check
                _Result(),  # INSERT workspace_member
                _Result(row=new_member),  # SELECT workspace_member
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with _client(app) as client:
            resp = await client.post(
                "/api/v1/workspaces/wsp_test/members",
                json={"user_id": "usr_new", "role": "member"},
            )
        assert resp.status_code == 201


# ============================================================================
# 8. 边界 / 集成测试（附加，确保 30+ 测试）
# ============================================================================


class TestEdgeCases:
    """边界与集成测试。"""

    @pytest.mark.asyncio
    async def test_create_workspace_slug_conflict_409(self, monkeypatch):
        """POST / slug 已存在返回 409。"""
        # slug 查重返回已有 row
        conn = _RecordingConnection(
            results=[_Result(row=_ws_row(id="wsp_existing", slug="taken"))]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with _client(app) as client:
            resp = await client.post(
                "/api/v1/workspaces",
                json={"name": "New", "slug": "taken"},
            )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_add_member_already_member_409(self, monkeypatch):
        """POST /{ws_id}/members 用户已是成员返回 409。"""
        ws_row = _ws_row()
        owner_member = _member_row(role="owner")
        existing_member = _member_row(id="mem_existing", user_id="usr_existing")
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # SELECT workspace_v2
                _Result(row=owner_member),  # SELECT workspace_member
                _Result(rows=[]),  # SELECT workspace_role_permission
                _Result(row=existing_member),  # existing member check (已有)
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="owner"))
        async with _client(app) as client:
            resp = await client.post(
                "/api/v1/workspaces/wsp_test/members",
                json={"user_id": "usr_existing", "role": "member"},
            )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_create_invite_owner_role_400(self, monkeypatch):
        """POST /{ws_id}/invites role=owner 返回 400。"""
        ws_row = _ws_row()
        member_row = _member_row(role="admin")
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),
                _Result(row=member_row),
                _Result(rows=[]),
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with _client(app) as client:
            resp = await client.post(
                "/api/v1/workspaces/wsp_test/invites",
                json={"email": "x@example.com", "role": "owner"},
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_update_permissions_non_owner_403(self, monkeypatch):
        """PUT /{ws_id}/permissions 非 owner 返回 403。"""
        ws_row = _ws_row()
        admin_member = _member_row(role="admin")
        conn = _RecordingConnection(
            results=[
                _Result(row=ws_row),  # SELECT workspace_v2
                _Result(row=admin_member),  # SELECT workspace_member
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with _client(app) as client:
            resp = await client.put(
                "/api/v1/workspaces/wsp_test/permissions",
                json={"matrix": DEFAULT_PERMISSION_MATRIX},
            )
        assert resp.status_code == 403

    def test_default_matrix_constants(self):
        """常量 ROLES / PERMISSIONS / DEFAULT_PERMISSION_MATRIX 形态正确。"""
        assert ROLES == ("owner", "admin", "member", "viewer", "guest")
        assert len(PERMISSIONS) == 9
        for role in ROLES:
            assert role in DEFAULT_PERMISSION_MATRIX
            for perm in PERMISSIONS:
                assert perm in DEFAULT_PERMISSION_MATRIX[role]
                assert isinstance(DEFAULT_PERMISSION_MATRIX[role][perm], bool)

    @pytest.mark.asyncio
    async def test_revoke_accepted_invite_409(self, monkeypatch):
        """DELETE /invites/{invite_id} 撤销已接受邀请返回 409。"""
        invite_row = _invite_row(id="inv_1", status="accepted")
        ws_row = _ws_row()
        member_row = _member_row(role="admin")
        conn = _RecordingConnection(
            results=[
                _Result(row=invite_row),  # SELECT workspace_invite
                _Result(row=ws_row),  # SELECT workspace_v2
                _Result(row=member_row),  # SELECT workspace_member
                _Result(rows=[]),  # SELECT workspace_role_permission
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with _client(app) as client:
            resp = await client.delete("/api/v1/workspaces/invites/inv_1")
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_accept_revoked_invite_410(self, monkeypatch):
        """POST /invites/{token}/accept 撤销的邀请返回 410。"""
        invite_row = _invite_row(token="tok_revoked", status="revoked")
        conn = _RecordingConnection(
            results=[_Result(row=invite_row)]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member", user_id="usr_invitee"))
        async with _client(app) as client:
            resp = await client.post("/api/v1/workspaces/invites/tok_revoked/accept")
        assert resp.status_code == 410

    @pytest.mark.asyncio
    async def test_get_invite_requires_membership(self, monkeypatch):
        """GET /invites/{invite_id} 非成员返回 403。"""
        invite_row = _invite_row(id="inv_1", workspace_id="wsp_test")
        ws_row = _ws_row(id="wsp_test")
        conn = _RecordingConnection(
            results=[
                _Result(row=invite_row),  # SELECT workspace_invite
                _Result(row=ws_row),  # SELECT workspace_v2 (存在)
                _Result(row=None),  # SELECT workspace_member (非成员)
            ]
        )
        monkeypatch.setattr(ws, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_other"))
        async with _client(app) as client:
            resp = await client.get("/api/v1/workspaces/invites/inv_1")
        assert resp.status_code == 403
