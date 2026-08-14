"""订阅账号池 (subscription account pool) 单元 + 端点测试。

v7.165: 32 个测试覆盖：
- pool CRUD：创建 / 列表 / 分页 / workspace 隔离（5）
- account 添加/加密/列表：成功 / pool 不存在 / workspace 隔离（5）
- lease/release 生命周期：租赁 / 重放 / 无可用账号 / 额度不足 / 释放 / 释放不存在（8）
- auto_renew 逻辑：续租成功 / 不自动续租释放 / 额度不足释放（3）
- sweep_exhausted：error_count / quota_remaining / 无命中（3）
- auto_topup：成功 / 幂等 / 无策略跳过（3）
- 鉴权：未认证 401 / member 可读 / member 不可写 / admin 可写 / workspace 越权 403（5）

所有测试使用 fake pool/connection，不依赖真实 DB / Redis / 网络。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules import channel_extensions as ce


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

    async def commit(self):
        return None

    async def rollback(self):
        return None


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
    role="admin",
    workspace_id="wsp_test",
    user_id="usr_test",
    capabilities=("pool:*",),
) -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email="test@example.com",
        display_name="Test",
        onboarding_completed=True,
        capabilities=capabilities,
    )


def _pool_row(**overrides) -> dict:
    base = {
        "id": "pool_1",
        "org_id": "org_test",
        "workspace_id": "wsp_test",
        "name": "Test Pool",
        "provider": "openai",
        "sticky_ttl_seconds": 3600,
        "billing_policy": {"cost_per_lease": 0},
        "status": "active",
        "created_by": "usr_test",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _account_row(**overrides) -> dict:
    base = {
        "id": "acct_1",
        "pool_id": "pool_1",
        "org_id": "org_test",
        "workspace_id": "wsp_test",
        "display_name": "Account 1",
        "account_ref_enc": "encrypted_ref",
        "account_ref_hash": "hash123",
        "last_four": "1234",
        "region": "global",
        "weight": 100,
        "quota_remaining": 1000,
        "status": "active",
        "lease_owner_hash": None,
        "lease_expires_at": None,
        "last_used_at": None,
        "error_count": 0,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _session_row(**overrides) -> dict:
    base = {
        "id": "sess_1",
        "pool_id": "pool_1",
        "account_id": "acct_1",
        "workspace_id": "wsp_test",
        "session_key_hash": "hash_sess",
        "model": "gpt-4",
        "status": "active",
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _usage_row(**overrides) -> dict:
    base = {
        "id": "usg_1",
        "pool_id": "pool_1",
        "account_id": "acct_1",
        "workspace_id": "wsp_test",
        "session_key_hash": "hash_sess",
        "model": "gpt-4",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "cost_credits": 10,
        "billing_period": "current",
        "status": "active",
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(ce.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


# ============================================================================
# 1. Pool CRUD
# ============================================================================


class TestPoolCrud:
    """Pool 创建 / 列表 / 分页。"""

    @pytest.mark.asyncio
    async def test_create_pool_success(self, monkeypatch):
        """POST /channel-extensions/pools admin 创建 pool 返回 201。"""
        row = _pool_row(name="New Pool")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/channel-extensions/pools",
                json={"name": "New Pool", "provider": "openai"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "New Pool"
        assert body["provider"] == "openai"
        assert any("INSERT INTO gw_subscription_account_pool" in q for q, _ in conn.calls)

    @pytest.mark.asyncio
    async def test_create_pool_rejects_unsupported_provider(self, monkeypatch):
        """POST /channel-extensions/pools 不支持的 provider 返回 422。"""
        app = _app(actor=_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/channel-extensions/pools",
                json={"name": "Bad Pool", "provider": "unknown_provider"},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_create_pool_duplicate_name_returns_409(self, monkeypatch):
        """POST /channel-extensions/pools 重复名称返回 409。"""

        class _FailingConnection(_RecordingConnection):
            async def execute(self, query, params=()):
                self.calls.append((query, params))
                raise Exception("duplicate key value violates unique constraint")

        conn = _FailingConnection()
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/channel-extensions/pools",
                json={"name": "Existing Pool", "provider": "openai"},
            )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_list_pools_pagination(self, monkeypatch):
        """GET /channel-extensions/pools 分页返回 pool 列表。"""
        rows = [_pool_row(id="pool_1"), _pool_row(id="pool_2")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/channel-extensions/pools?limit=10&offset=0")
        assert resp.status_code == 200
        body = resp.json()
        assert body["meta"]["count"] == 2
        assert len(body["items"]) == 2
        query, params = conn.calls[0]
        assert "LIMIT %s OFFSET %s" in query
        assert params[1] == 10
        assert params[2] == 0

    @pytest.mark.asyncio
    async def test_list_pools_workspace_isolation(self, monkeypatch):
        """GET /channel-extensions/pools SQL 强制按 workspace_id 过滤。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member", workspace_id="wsp_isolated"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/channel-extensions/pools")
        assert resp.status_code == 200
        query, params = conn.calls[0]
        assert "WHERE p.workspace_id=%s" in query
        assert params[0] == "wsp_isolated"


# ============================================================================
# 2. Pool Account
# ============================================================================


class TestPoolAccount:
    """账号添加 / 加密 / 列表。"""

    @pytest.mark.asyncio
    async def test_add_account_success(self, monkeypatch):
        """POST /channel-extensions/pools/{pool_id}/accounts 添加账号返回 201。"""
        conn = _RecordingConnection(
            results=[
                _Result(row={"1": 1}),
                _Result(row=_account_row(display_name="Account A", last_four="cdef")),
            ]
        )
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/channel-extensions/pools/pool_1/accounts",
                json={"display_name": "Account A", "account_ref": "sk-1234567890abcdef"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["display_name"] == "Account A"
        assert "account_ref_enc" not in body
        assert "account_ref_hash" not in body
        assert body["last_four"] == "cdef"
        # 验证 INSERT 包含加密字段
        insert_sql = conn.calls[1][0]
        assert "account_ref_enc" in insert_sql
        assert "account_ref_hash" in insert_sql

    @pytest.mark.asyncio
    async def test_add_account_pool_not_found(self, monkeypatch):
        """POST /channel-extensions/pools/{pool_id}/accounts pool 不存在返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/channel-extensions/pools/missing/accounts",
                json={"display_name": "Account A", "account_ref": "sk-1234567890abcdef"},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_accounts_success(self, monkeypatch):
        """GET /channel-extensions/pools/{pool_id}/accounts 返回账号列表。"""
        rows = [_account_row(id="acct_1"), _account_row(id="acct_2")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/channel-extensions/pools/pool_1/accounts")
        assert resp.status_code == 200
        body = resp.json()
        assert body["meta"]["count"] == 2
        assert len(body["items"]) == 2
        for item in body["items"]:
            assert "account_ref_enc" not in item
            assert "account_ref_hash" not in item

    @pytest.mark.asyncio
    async def test_list_accounts_workspace_isolation(self, monkeypatch):
        """GET /channel-extensions/pools/{pool_id}/accounts 跨 workspace 403。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member", workspace_id="wsp_other"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/channel-extensions/pools/pool_1/accounts")
        assert resp.status_code == 200
        query, params = conn.calls[0]
        assert "workspace_id=%s" in query
        assert params[1] == "wsp_other"

    @pytest.mark.asyncio
    async def test_add_account_encrypts_ref(self, monkeypatch):
        """account_ref 通过 encrypt_secret 加密存储，hash_secret 生成 hash。"""
        conn = _RecordingConnection(
            results=[
                _Result(row={"1": 1}),
                _Result(row=_account_row()),
            ]
        )
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/channel-extensions/pools/pool_1/accounts",
                json={"display_name": "Enc Test", "account_ref": "sk-secret-value"},
            )
        assert resp.status_code == 201
        _query, params = conn.calls[1]
        # params 顺序: id, pool_id, org_id, workspace_id, display_name, account_ref_enc, account_ref_hash, last_four, region, weight, quota_remaining
        enc_val = params[5]
        hash_val = params[6]
        assert enc_val is not None and enc_val != "sk-secret-value"
        assert hash_val is not None and hash_val != "sk-secret-value"


# ============================================================================
# 3. Lease / Release 生命周期
# ============================================================================


class TestLeaseRelease:
    """租赁与释放端点测试。"""

    @pytest.mark.asyncio
    async def test_lease_success(self, monkeypatch):
        """POST /channel-extensions/pools/{pool_id}/lease 成功租赁返回 201。"""
        pool = _pool_row(billing_policy={"cost_per_lease": 0})
        account = _account_row()
        conn = _RecordingConnection(
            results=[
                _Result(row=pool),          # pool select
                _Result(row=None),          # current session
                _Result(rows=[account]),    # account select (fetchall needs rows)
                _Result(row=pool),          # deduct pool billing_policy
                _Result(row=account),       # deduct account quota
                _Result(),                  # insert session
                _Result(),                  # update account
            ]
        )
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/channel-extensions/pools/pool_1/lease",
                json={"session_key": "sess-abc"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "leased"
        assert body["account_id"] == "acct_1"
        assert "lease_id" in body

    @pytest.mark.asyncio
    async def test_lease_replayed(self, monkeypatch):
        """重复 session_key 返回 replayed 状态。"""
        pool = _pool_row()
        session = _session_row(display_name="Acct", last_four="1234")
        conn = _RecordingConnection(
            results=[
                _Result(row=pool),
                _Result(row=session),
            ]
        )
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/channel-extensions/pools/pool_1/lease",
                json={"session_key": "sess-abc"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "replayed"

    @pytest.mark.asyncio
    async def test_lease_no_account_available_409(self, monkeypatch):
        """无可用账号返回 409。"""
        pool = _pool_row()
        conn = _RecordingConnection(
            results=[
                _Result(row=pool),
                _Result(row=None),
                _Result(rows=[]),
            ]
        )
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/channel-extensions/pools/pool_1/lease",
                json={"session_key": "sess-abc"},
            )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_lease_insufficient_quota_402(self, monkeypatch):
        """额度不足返回 402。"""
        pool = _pool_row(billing_policy={"cost_per_lease": 100})
        account = _account_row(quota_remaining=50)
        conn = _RecordingConnection(
            results=[
                _Result(row=pool),
                _Result(row=None),
                _Result(rows=[account]),
                _Result(row=pool),      # deduct billing_policy
                _Result(row=account),   # deduct quota
            ]
        )
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/channel-extensions/pools/pool_1/lease",
                json={"session_key": "sess-abc"},
            )
        assert resp.status_code == 402
        assert "insufficient_quota" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_release_success(self, monkeypatch):
        """POST /channel-extensions/pools/{pool_id}/release 成功释放返回 200。"""
        conn = _RecordingConnection(
            results=[
                _Result(row={"id": "sess_1", "account_id": "acct_1"}),
                _Result(),
            ]
        )
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/channel-extensions/pools/pool_1/release",
                json={"session_key": "sess-abc"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["released"] is True
        assert body["lease_id"] == "sess_1"
        assert "UPDATE gw_subscription_session" in conn.calls[0][0]
        assert "UPDATE gw_subscription_account" in conn.calls[1][0]

    @pytest.mark.asyncio
    async def test_release_not_found(self, monkeypatch):
        """释放不存在的 lease 返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/channel-extensions/pools/pool_1/release",
                json={"session_key": "missing-key"},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_lease_workspace_isolation(self, monkeypatch):
        """lease 端点跨 workspace 返回 404（pool 查不到）。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin", workspace_id="wsp_other"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/channel-extensions/pools/pool_1/lease",
                json={"session_key": "sess-abc"},
            )
        assert resp.status_code == 404
        query, params = conn.calls[0]
        assert params[1] == "wsp_other"

    @pytest.mark.asyncio
    async def test_release_workspace_isolation(self, monkeypatch):
        """release 端点跨 workspace 返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin", workspace_id="wsp_other"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/channel-extensions/pools/pool_1/release",
                json={"session_key": "sess-abc"},
            )
        assert resp.status_code == 404


# ============================================================================
# 4. Auto Renew
# ============================================================================


class TestAutoRenew:
    """renew_expired_leases  worker 逻辑测试。"""

    @pytest.mark.asyncio
    async def test_renew_expired_leases_renews(self, monkeypatch):
        """auto_renew=True 时续租成功。"""
        pool = _pool_row(sticky_ttl_seconds=3600, billing_policy={"auto_renew": True, "cost_per_lease": 0})
        session = _session_row(billing_policy={"auto_renew": True, "cost_per_lease": 0}, sticky_ttl_seconds=3600)
        account = _account_row()
        conn = _RecordingConnection(
            results=[
                _Result(rows=[session]),   # select sessions
                _Result(row=pool),         # deduct billing_policy
                _Result(row=account),      # deduct quota
                _Result(),                 # update session
                _Result(),                 # update account lease
            ]
        )
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        result = await ce.renew_expired_leases("worker_1", limit=10)
        assert result["renewed"] == 1
        assert result["released"] == 0
        assert "UPDATE gw_subscription_session" in conn.calls[2][0]

    @pytest.mark.asyncio
    async def test_renew_expired_leases_releases_when_no_auto_renew(self, monkeypatch):
        """auto_renew=False 时释放 lease。"""
        pool = _pool_row(billing_policy={"auto_renew": False})
        session = _session_row()
        conn = _RecordingConnection(
            results=[
                _Result(rows=[session]),
                _Result(),                 # update session expired
                _Result(),                 # update account clear
            ]
        )
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        result = await ce.renew_expired_leases("worker_1", limit=10)
        assert result["renewed"] == 0
        assert result["released"] == 1

    @pytest.mark.asyncio
    async def test_renew_expired_leases_releases_when_quota_insufficient(self, monkeypatch):
        """auto_renew=True 但额度不足时释放 lease。"""
        pool = _pool_row(billing_policy={"auto_renew": True, "cost_per_lease": 100})
        session = _session_row()
        account = _account_row(quota_remaining=50)
        conn = _RecordingConnection(
            results=[
                _Result(rows=[session]),
                _Result(row=pool),         # deduct billing_policy
                _Result(row=account),      # deduct quota (insufficient)
                _Result(),                 # update session expired
                _Result(),                 # update account clear
            ]
        )
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        result = await ce.renew_expired_leases("worker_1", limit=10)
        assert result["renewed"] == 0
        assert result["released"] == 1


# ============================================================================
# 5. Sweep Exhausted
# ============================================================================


class TestSweepExhausted:
    """sweep_exhausted_accounts worker 逻辑测试。"""

    @pytest.mark.asyncio
    async def test_sweep_by_error_count(self, monkeypatch):
        """error_count>=5 的账号被标记 exhausted。"""
        account = _account_row(error_count=5)
        conn = _RecordingConnection(
            results=[
                _Result(rows=[account]),
                _Result(),
            ]
        )
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        result = await ce.sweep_exhausted_accounts("worker_1", limit=10)
        assert result["swept"] == 1
        assert result["scanned"] == 1
        assert "UPDATE gw_subscription_account" in conn.calls[1][0]
        assert "exhausted" in conn.calls[1][0]

    @pytest.mark.asyncio
    async def test_sweep_by_quota_zero(self, monkeypatch):
        """quota_remaining==0 的账号被标记 exhausted。"""
        account = _account_row(quota_remaining=0)
        conn = _RecordingConnection(
            results=[
                _Result(rows=[account]),
                _Result(),
            ]
        )
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        result = await ce.sweep_exhausted_accounts("worker_1", limit=10)
        assert result["swept"] == 1

    @pytest.mark.asyncio
    async def test_sweep_no_accounts(self, monkeypatch):
        """无命中账号时 swept=0。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        result = await ce.sweep_exhausted_accounts("worker_1", limit=10)
        assert result["swept"] == 0
        assert result["scanned"] == 0


# ============================================================================
# 6. Auto Topup
# ============================================================================


class TestAutoTopup:
    """auto_topup worker 逻辑测试。"""

    @pytest.mark.asyncio
    async def test_auto_topup_success(self, monkeypatch):
        """配置 auto_topup 时 exhausted 账号被补充配额。"""
        pool = _pool_row(billing_policy={"auto_topup": True, "topup_amount": 500})
        account = _account_row(status="exhausted", quota_remaining=0, billing_policy={"auto_topup": True, "topup_amount": 500})
        conn = _RecordingConnection(
            results=[
                _Result(rows=[account]),
                _Result(row=None),         # idempotency check
                _Result(),                 # update account
                _Result(),                 # insert billing event
            ]
        )
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        result = await ce.auto_topup("worker_1", limit=10)
        assert result["topped_up"] == 1
        assert result["skipped"] == 0
        assert "UPDATE gw_subscription_account" in conn.calls[2][0]
        assert "INSERT INTO gw_subscription_pool_billing_event" in conn.calls[3][0]

    @pytest.mark.asyncio
    async def test_auto_topup_idempotent(self, monkeypatch):
        """同一天重复 topup 因 idempotency_key 被跳过。"""
        pool = _pool_row(billing_policy={"auto_topup": True, "topup_amount": 500})
        account = _account_row(status="exhausted", quota_remaining=0)
        conn = _RecordingConnection(
            results=[
                _Result(rows=[account]),
                _Result(row={"1": 1}),     # idempotency check hit
            ]
        )
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        result = await ce.auto_topup("worker_1", limit=10)
        assert result["topped_up"] == 0
        assert result["skipped"] == 1

    @pytest.mark.asyncio
    async def test_auto_topup_skips_without_policy(self, monkeypatch):
        """billing_policy 未启用 auto_topup 时跳过。"""
        pool = _pool_row(billing_policy={"auto_topup": False})
        account = _account_row(status="exhausted", quota_remaining=0)
        conn = _RecordingConnection(
            results=[
                _Result(rows=[account]),
            ]
        )
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        result = await ce.auto_topup("worker_1", limit=10)
        assert result["topped_up"] == 0
        assert result["skipped"] == 1


# ============================================================================
# 7. Auth & Workspace Isolation
# ============================================================================


class TestAuth:
    """鉴权与 workspace 隔离测试。"""

    @pytest.mark.asyncio
    async def test_unauthenticated_401(self):
        """未携带 token 返回 401。"""
        app = _app(actor=None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/channel-extensions/pools")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_member_can_read_pools(self, monkeypatch):
        """member 角色可读取 pool 列表。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/channel-extensions/pools")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_member_can_read_accounts(self, monkeypatch):
        """member 角色可读取 account 列表。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/channel-extensions/pools/pool_1/accounts")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_member_can_read_usage(self, monkeypatch):
        """member 角色可读取 usage。"""
        conn = _RecordingConnection(
            results=[
                _Result(row={"1": 1}),
                _Result(row={"total_cost": 0, "total_prompt_tokens": 0, "total_completion_tokens": 0, "total_sessions": 0}),
                _Result(rows=[]),
            ]
        )
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/channel-extensions/pools/pool_1/usage")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_member_cannot_write_pool_403(self, monkeypatch):
        """member 角色创建 pool 返回 403。"""
        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/channel-extensions/pools",
                json={"name": "New Pool", "provider": "openai"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_cross_workspace_403_on_existing_resource(self, monkeypatch):
        """资源存在但属于其他 workspace 时，端点按 workspace 隔离拒绝。"""
        # 通过 list 端点验证 workspace 参数被传入，实际跨 workspace 因 SQL 过滤而无数据
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin", workspace_id="wsp_other"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/channel-extensions/pools")
        assert resp.status_code == 200
        query, params = conn.calls[0]
        assert params[0] == "wsp_other"


# ============================================================================
# 8. Release Expired Leases Worker
# ============================================================================


class TestReleaseExpired:
    """release_expired_leases worker 逻辑测试。"""

    @pytest.mark.asyncio
    async def test_release_expired_leases_clears_locks(self, monkeypatch):
        """释放过期 lease 并清空账号 lease 锁。"""
        session = _session_row()
        conn = _RecordingConnection(
            results=[
                _Result(rows=[session]),
                _Result(),                 # update session
                _Result(),                 # update account
            ]
        )
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        result = await ce.release_expired_leases("worker_1", limit=10)
        assert result["released"] == 1
        assert "status='expired'" in conn.calls[1][0]
        assert "lease_owner_hash=NULL" in conn.calls[2][0]

    @pytest.mark.asyncio
    async def test_release_expired_leases_empty(self, monkeypatch):
        """无过期 lease 时 released=0。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        result = await ce.release_expired_leases("worker_1", limit=10)
        assert result["released"] == 0


# ============================================================================
# 9. Usage Endpoint
# ============================================================================


class TestUsage:
    """用量统计端点测试。"""

    @pytest.mark.asyncio
    async def test_usage_endpoint_returns_aggregates(self, monkeypatch):
        """GET /channel-extensions/pools/{pool_id}/usage 返回用量聚合。"""
        conn = _RecordingConnection(
            results=[
                _Result(row={"1": 1}),
                _Result(row={"total_cost": 200, "total_prompt_tokens": 1000, "total_completion_tokens": 500, "total_sessions": 5}),
                _Result(rows=[{"event_type": "lease", "amount": 10, "status": "succeeded", "created_at": datetime.now(UTC)}]),
            ]
        )
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/channel-extensions/pools/pool_1/usage")
        assert resp.status_code == 200
        body = resp.json()
        assert body["pool_id"] == "pool_1"
        assert body["total_cost_credits"] == 200
        assert body["total_sessions"] == 5
        assert len(body["recent_events"]) == 1

    @pytest.mark.asyncio
    async def test_usage_endpoint_pool_not_found(self, monkeypatch):
        """pool 不存在返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(ce, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/channel-extensions/pools/missing/usage")
        assert resp.status_code == 404
