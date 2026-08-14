"""订阅与计费模块 (billing) 单元 + 端点测试。

v7.148: 28 个测试覆盖：
- 套餐 CRUD：创建 / 列表 / 详情（3）
- 订阅：创建 / 切换 / 详情 / 取消（4）
- 用量记录：成功 / 字段校验（2）
- 用量查询：metric 过滤 / period 过滤（2）
- 用量汇总：汇总 / quota 比较 / 超额（3）
- 发票：列表 / 详情（2）
- workspace 隔离：订阅 / 用量 / 发票 跨区 403（3）
- 鉴权：未认证 401（1）
- 默认套餐：ensure_default_plans 幂等 / 创建 4 个（2）
- 边界：重复创建套餐 / 取消已取消 / check_quota 超额（3）
- 公开 plans 端点 / 订阅切换 plan 不存在（2）
- 集成：plan→subscription→usage→summary 全链路（1）

所有测试使用 fake pool/connection，不依赖真实 DB / Redis / 网络 / 支付网关。

注意：由于同目录下存在 ``billing/`` 包，``billing.py`` 被包遮蔽，无法通过
``from workama_platform.modules.billing import ...`` 导入。本测试使用
``importlib.util.spec_from_file_location`` 按文件路径直接加载。
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor


# ============================================================================
# 通过 importlib 加载被包遮蔽的 billing.py
# ============================================================================

_BILLING_PATH = (
    Path(__file__).parent.parent
    / "src"
    / "workama_platform"
    / "modules"
    / "billing.py"
)
_spec = importlib.util.spec_from_file_location(
    "workama_platform.modules.billing_module", _BILLING_PATH
)
b = importlib.util.module_from_spec(_spec)
# 必须在 exec_module 前注册到 sys.modules，否则 Pydantic 无法解析
# ``from __future__ import annotations`` 产生的字符串注解（PydanticUserError）。
sys.modules[_spec.name] = b
_spec.loader.exec_module(b)


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
    role="owner",
    workspace_id="wsp_test",
    user_id="usr_test",
    capabilities=("*",),
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


def _plan_row(**overrides) -> dict:
    base = {
        "id": "plan_1",
        "code": "pro",
        "name": "Pro",
        "description": "Pro tier",
        "price_monthly": Decimal("99.00"),
        "price_yearly": Decimal("990.00"),
        "token_quota": 100_000_000,
        "seat_quota": 20,
        "storage_quota_gb": 100,
        "api_rate_limit": 1200,
        "features": ["priority_support"],
        "status": "active",
        "metadata": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _sub_row(**overrides) -> dict:
    base = {
        "id": "sub_1",
        "workspace_id": "wsp_test",
        "plan_id": "plan_1",
        "billing_cycle": "monthly",
        "status": "active",
        "current_period_start": datetime.now(UTC) - timedelta(days=5),
        "current_period_end": datetime.now(UTC) + timedelta(days=25),
        "trial_end": None,
        "canceled_at": None,
        "metadata": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _usage_row(**overrides) -> dict:
    base = {
        "id": "use_1",
        "workspace_id": "wsp_test",
        "subscription_id": "sub_1",
        "metric": "tokens_used",
        "value": 1000,
        "period_start": datetime.now(UTC) - timedelta(days=5),
        "period_end": datetime.now(UTC) + timedelta(days=25),
        "metadata": {},
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _invoice_row(**overrides) -> dict:
    base = {
        "id": "inv_1",
        "workspace_id": "wsp_test",
        "subscription_id": "sub_1",
        "amount": Decimal("99.00"),
        "currency": "USD",
        "status": "paid",
        "period_start": datetime.now(UTC) - timedelta(days=30),
        "period_end": datetime.now(UTC),
        "paid_at": datetime.now(UTC),
        "metadata": {},
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(b.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


# ============================================================================
# 1. 套餐 CRUD
# ============================================================================


class TestPlanCrud:
    """套餐创建 / 列表 / 详情。"""

    @pytest.mark.asyncio
    async def test_create_plan_success(self, monkeypatch):
        """POST /plans admin 创建套餐返回 201。"""
        # SELECT code 冲突检查 (row=None) + INSERT RETURNING
        conn = _RecordingConnection(
            results=[_Result(row=None), _Result(row=_plan_row(code="custom"))]
        )
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/billing/plans",
                json={
                    "code": "custom",
                    "name": "Custom",
                    "price_monthly": "29.00",
                    "price_yearly": "290.00",
                    "token_quota": 5000000,
                    "seat_quota": 10,
                    "storage_quota_gb": 50,
                    "api_rate_limit": 600,
                    "features": ["email_support"],
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["code"] == "custom"
        # INSERT 语句包含 billing_plan
        assert any(
            "INSERT INTO billing_plan" in q for q, _ in conn.calls
        )

    @pytest.mark.asyncio
    async def test_list_plans_public_no_auth(self, monkeypatch):
        """GET /plans 公开返回套餐列表（无需鉴权）。"""
        rows = [_plan_row(id="plan_free", code="free"), _plan_row(id="plan_pro", code="pro")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=None)  # 不注入 actor
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/billing/plans")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert len(body["items"]) == 2
        # SQL 强制 status='active' 过滤
        assert "status = 'active'" in conn.calls[0][0]

    @pytest.mark.asyncio
    async def test_get_plan_detail(self, monkeypatch):
        """GET /plans/{plan_id} 返回套餐详情。"""
        conn = _RecordingConnection(results=[_Result(row=_plan_row(id="plan_x"))])
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/billing/plans/plan_x")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "plan_x"
        assert body["code"] == "pro"


# ============================================================================
# 2. 订阅
# ============================================================================


class TestSubscription:
    """订阅创建 / 切换 / 详情 / 取消。"""

    @pytest.mark.asyncio
    async def test_create_subscription_new(self, monkeypatch):
        """POST /subscriptions 创建新订阅返回 201, switched=False。"""
        plan = _plan_row()
        # plan SELECT + existing SELECT(None) + INSERT(无返回) + SELECT created
        conn = _RecordingConnection(
            results=[
                _Result(row=plan),
                _Result(row=None),
                _Result(),
                _Result(row=_sub_row(plan_id="plan_1", billing_cycle="monthly")),
            ]
        )
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/billing/subscriptions",
                json={"plan_id": "plan_1", "billing_cycle": "monthly"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["switched"] is False
        assert body["plan"]["id"] == "plan_1"
        assert body["status"] == "active"

    @pytest.mark.asyncio
    async def test_create_subscription_switches_existing(self, monkeypatch):
        """POST /subscriptions 已有 active 订阅时切换 plan, switched=True。"""
        plan = _plan_row(id="plan_pro")
        existing = _sub_row(id="sub_old", plan_id="plan_free")
        # plan SELECT + existing SELECT(row) + UPDATE(无返回) + SELECT updated
        conn = _RecordingConnection(
            results=[
                _Result(row=plan),
                _Result(row=existing),
                _Result(),
                _Result(row=_sub_row(id="sub_old", plan_id="plan_pro")),
            ]
        )
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/billing/subscriptions",
                json={"plan_id": "plan_pro", "billing_cycle": "yearly"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["switched"] is True
        assert body["id"] == "sub_old"
        # UPDATE 语句包含 plan_id 与 canceled_at=NULL
        update_sql = conn.calls[2][0]
        assert "UPDATE billing_subscription" in update_sql
        assert "canceled_at = NULL" in update_sql

    @pytest.mark.asyncio
    async def test_get_subscription_detail(self, monkeypatch):
        """GET /subscriptions/{id} 返回订阅详情。"""
        conn = _RecordingConnection(results=[_Result(row=_sub_row())])
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/billing/subscriptions/sub_1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "sub_1"
        assert body["plan_id"] == "plan_1"

    @pytest.mark.asyncio
    async def test_cancel_subscription_success(self, monkeypatch):
        """DELETE /subscriptions/{id} 取消订阅返回 200, status=canceled。"""
        existing = _sub_row(status="active")
        canceled = _sub_row(status="canceled")
        # _owned_subscription SELECT + UPDATE(无返回) + SELECT updated
        conn = _RecordingConnection(
            results=[_Result(row=existing), _Result(), _Result(row=canceled)]
        )
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/billing/subscriptions/sub_1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "canceled"
        assert "UPDATE billing_subscription" in conn.calls[1][0]


# ============================================================================
# 3. 用量记录
# ============================================================================


class TestUsageRecord:
    """用量记录端点测试。"""

    @pytest.mark.asyncio
    async def test_record_usage_success(self, monkeypatch):
        """POST /usage 记录用量返回 201。"""
        sub = _sub_row()
        usage = _usage_row(metric="tokens_used", value=5000)
        # sub SELECT + INSERT RETURNING
        conn = _RecordingConnection(
            results=[_Result(row=sub), _Result(row=usage)]
        )
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/billing/usage",
                json={
                    "subscription_id": "sub_1",
                    "metric": "tokens_used",
                    "value": 5000,
                    "period_start": "2026-07-01T00:00:00+00:00",
                    "period_end": "2026-07-31T00:00:00+00:00",
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["metric"] == "tokens_used"
        assert body["value"] == 5000
        assert "INSERT INTO billing_usage_record" in conn.calls[1][0]

    @pytest.mark.asyncio
    async def test_record_usage_rejects_invalid_metric(self):
        """POST /usage 非法 metric 触发 422 校验错误。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/billing/usage",
                json={
                    "subscription_id": "sub_1",
                    "metric": "invalid_metric",
                    "value": 100,
                    "period_start": "2026-07-01T00:00:00+00:00",
                    "period_end": "2026-07-31T00:00:00+00:00",
                },
            )
        assert resp.status_code == 422


# ============================================================================
# 4. 用量查询
# ============================================================================


class TestUsageQuery:
    """用量查询过滤测试。"""

    @pytest.mark.asyncio
    async def test_list_usage_metric_filter(self, monkeypatch):
        """GET /usage?metric=tokens_used SQL 含 metric 过滤。"""
        rows = [_usage_row(metric="tokens_used")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/billing/usage?metric=tokens_used")
        assert resp.status_code == 200
        query, params = conn.calls[0]
        assert "metric = %s" in query
        assert "tokens_used" in params
        assert resp.json()["count"] == 1

    @pytest.mark.asyncio
    async def test_list_usage_period_filter(self, monkeypatch):
        """GET /usage?start=...&end=... SQL 含 period 过滤。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/billing/usage?start=2026-07-01T00:00:00Z&end=2026-07-31T00:00:00Z"
            )
        assert resp.status_code == 200
        query, params = conn.calls[0]
        assert "period_start >= %s" in query
        assert "period_end <= %s" in query
        # workspace 隔离参数首位
        assert params[0] == "wsp_test"


# ============================================================================
# 5. 用量汇总
# ============================================================================


class TestUsageSummary:
    """用量汇总测试。"""

    @pytest.mark.asyncio
    async def test_usage_summary_aggregates_metrics(self, monkeypatch):
        """GET /usage/summary 返回当前周期各 metric 总和。"""
        sub = _sub_row()
        plan = _plan_row(token_quota=1_000_000, seat_quota=10)
        usage_rows = [
            {"metric": "tokens_used", "total": 500_000},
            {"metric": "api_calls", "total": 200},
        ]
        # sub SELECT + plan SELECT + usage GROUP BY
        conn = _RecordingConnection(
            results=[
                _Result(row=sub),
                _Result(row=plan),
                _Result(rows=usage_rows),
            ]
        )
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/billing/usage/summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_active_subscription"] is True
        metrics = {m["metric"]: m for m in body["metrics"]}
        assert metrics["tokens_used"]["used"] == 500_000
        assert metrics["tokens_used"]["quota"] == 1_000_000
        assert metrics["tokens_used"]["exceeded"] is False
        assert metrics["api_calls"]["used"] == 200
        assert metrics["seats_used"]["used"] == 0  # 无用量的 metric 也出现

    @pytest.mark.asyncio
    async def test_usage_summary_quota_comparison_not_exceeded(self, monkeypatch):
        """GET /usage/summary used < quota 时 exceeded=False。"""
        sub = _sub_row()
        plan = _plan_row(token_quota=1_000_000)
        usage_rows = [{"metric": "tokens_used", "total": 100_000}]
        conn = _RecordingConnection(
            results=[
                _Result(row=sub),
                _Result(row=plan),
                _Result(rows=usage_rows),
            ]
        )
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/billing/usage/summary")
        body = resp.json()
        tokens = next(m for m in body["metrics"] if m["metric"] == "tokens_used")
        assert tokens["used"] == 100_000
        assert tokens["quota"] == 1_000_000
        assert tokens["exceeded"] is False

    @pytest.mark.asyncio
    async def test_usage_summary_exceeded_when_used_over_quota(self, monkeypatch):
        """GET /usage/summary used > quota 时 exceeded=True。"""
        sub = _sub_row()
        plan = _plan_row(token_quota=100_000)
        usage_rows = [{"metric": "tokens_used", "total": 500_000}]
        conn = _RecordingConnection(
            results=[
                _Result(row=sub),
                _Result(row=plan),
                _Result(rows=usage_rows),
            ]
        )
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/billing/usage/summary")
        body = resp.json()
        tokens = next(m for m in body["metrics"] if m["metric"] == "tokens_used")
        assert tokens["used"] == 500_000
        assert tokens["quota"] == 100_000
        assert tokens["exceeded"] is True


# ============================================================================
# 6. 发票
# ============================================================================


class TestInvoice:
    """发票列表 / 详情测试。"""

    @pytest.mark.asyncio
    async def test_list_invoices(self, monkeypatch):
        """GET /invoices 返回发票列表。"""
        rows = [_invoice_row(id="inv_1"), _invoice_row(id="inv_2")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/billing/invoices")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert body["items"][0]["id"] == "inv_1"
        # workspace 隔离
        assert "workspace_id = %s" in conn.calls[0][0]
        assert conn.calls[0][1][0] == "wsp_test"

    @pytest.mark.asyncio
    async def test_get_invoice_detail(self, monkeypatch):
        """GET /invoices/{id} 返回发票详情。"""
        conn = _RecordingConnection(results=[_Result(row=_invoice_row())])
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/billing/invoices/inv_1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "inv_1"
        assert float(body["amount"]) == 99.0  # Decimal 经 JSON 序列化为 float


# ============================================================================
# 7. workspace 隔离
# ============================================================================


class TestWorkspaceIsolation:
    """跨 workspace 访问返回 403。"""

    @pytest.mark.asyncio
    async def test_get_subscription_cross_workspace_403(self, monkeypatch):
        """GET /subscriptions/{id} 订阅属于其他 workspace 返回 403。"""
        row = _sub_row(workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/billing/subscriptions/sub_1")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_record_usage_cross_workspace_403(self, monkeypatch):
        """POST /usage subscription 属于其他 workspace 返回 403。"""
        sub = _sub_row(workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=sub)])
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/billing/usage",
                json={
                    "subscription_id": "sub_1",
                    "metric": "tokens_used",
                    "value": 100,
                    "period_start": "2026-07-01T00:00:00+00:00",
                    "period_end": "2026-07-31T00:00:00+00:00",
                },
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_get_invoice_cross_workspace_403(self, monkeypatch):
        """GET /invoices/{id} 发票属于其他 workspace 返回 403。"""
        row = _invoice_row(workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/billing/invoices/inv_1")
        assert resp.status_code == 403


# ============================================================================
# 8. 鉴权
# ============================================================================


class TestAuth:
    """鉴权测试。"""

    @pytest.mark.asyncio
    async def test_list_subscriptions_requires_authentication(self):
        """未认证请求 GET /subscriptions 返回 401。"""
        app = _app(actor=None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/billing/subscriptions")
        assert resp.status_code == 401


# ============================================================================
# 9. 默认套餐
# ============================================================================


class TestEnsureDefaultPlans:
    """ensure_default_plans 启动钩子测试。"""

    @pytest.mark.asyncio
    async def test_ensure_default_plans_creates_four(self, monkeypatch):
        """ensure_default_plans 对 4 个默认套餐各执行一次 upsert。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(b, "pool", _Pool(conn))

        count = await b.ensure_default_plans()
        assert count == 4
        # 4 次 INSERT ... ON CONFLICT
        inserts = [q for q, _ in conn.calls if "INSERT INTO billing_plan" in q]
        assert len(inserts) == 4
        # 验证 4 个 code
        codes = [params[1] for _, params in conn.calls if len(params) >= 2 and params[1] in {"free", "starter", "pro", "enterprise"}]
        assert set(codes) == {"free", "starter", "pro", "enterprise"}

    @pytest.mark.asyncio
    async def test_ensure_default_plans_idempotent(self, monkeypatch):
        """ensure_default_plans 重复调用仍执行 4 次 upsert（ON CONFLICT 幂等）。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(b, "pool", _Pool(conn))

        first = await b.ensure_default_plans()
        second = await b.ensure_default_plans()
        assert first == 4
        assert second == 4
        # 两次调用共 8 次 execute（每次 4 个 upsert）
        assert len(conn.calls) == 8
        # 所有 SQL 都含 ON CONFLICT (code) DO UPDATE
        assert all("ON CONFLICT (code) DO UPDATE" in q for q, _ in conn.calls)


# ============================================================================
# 10. 边界
# ============================================================================


class TestEdgeCases:
    """边界场景测试。"""

    @pytest.mark.asyncio
    async def test_create_plan_duplicate_code_409(self, monkeypatch):
        """POST /plans 重复 code 返回 409。"""
        # SELECT 冲突检查返回已存在行
        conn = _RecordingConnection(results=[_Result(row=_plan_row(code="pro"))])
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/billing/plans",
                json={"code": "pro", "name": "Pro Dup"},
            )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_cancel_already_canceled_subscription_409(self, monkeypatch):
        """DELETE /subscriptions/{id} 取消已取消订阅返回 409。"""
        existing = _sub_row(status="canceled")
        conn = _RecordingConnection(results=[_Result(row=existing)])
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/billing/subscriptions/sub_1")
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_check_quota_exceeded(self, monkeypatch):
        """check_quota 用量 + value 超过 quota 时 exceeded=True。"""
        # check_quota 的 SQL 做 JOIN，返回 sub 字段 + plan quota 字段
        # quota=1_000_000, used=900_000, value=200_000 → projected=1_100_000 > quota
        sub = _sub_row(token_quota=1_000_000, seat_quota=20, storage_quota_gb=100, api_rate_limit=1200)
        # sub SELECT + usage SUM
        conn = _RecordingConnection(
            results=[
                _Result(row=sub),
                _Result(row={"used": 900_000}),
            ]
        )
        monkeypatch.setattr(b, "pool", _Pool(conn))

        result = await b.check_quota("wsp_test", "tokens_used", 200_000)
        assert result["exceeded"] is True
        assert result["used"] == 900_000
        assert result["quota"] == 1_000_000
        assert result["projected_total"] == 1_100_000
        assert result["reason"] == "ok"

    @pytest.mark.asyncio
    async def test_create_subscription_plan_not_found_404(self, monkeypatch):
        """POST /subscriptions plan 不存在返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/billing/subscriptions",
                json={"plan_id": "plan_missing", "billing_cycle": "monthly"},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_create_plan_forbidden_for_member(self, monkeypatch):
        """POST /plans 非 admin/owner 返回 403。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/billing/plans",
                json={"code": "x", "name": "X"},
            )
        assert resp.status_code == 403
        assert len(conn.calls) == 0  # 不应执行任何 SQL

    @pytest.mark.asyncio
    async def test_create_subscription_forbidden_for_admin(self, monkeypatch):
        """POST /subscriptions 非 owner（admin）返回 403（订阅需要 owner）。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/billing/subscriptions",
                json={"plan_id": "plan_1", "billing_cycle": "monthly"},
            )
        assert resp.status_code == 403


# ============================================================================
# 11. 集成测试
# ============================================================================


class TestIntegration:
    """端到端集成测试。"""

    @pytest.mark.asyncio
    async def test_plan_subscription_usage_summary_full_flow(self, monkeypatch):
        """集成：plan → subscription → usage → summary 全链路使用同一 fake 连接。"""
        plan = _plan_row(id="plan_pro", token_quota=100_000)
        # create_subscription: plan SELECT + existing SELECT(None) + INSERT + SELECT
        sub_created = _sub_row(id="sub_new", plan_id="plan_pro")
        # record_usage: sub SELECT + INSERT RETURNING
        usage = _usage_row(id="use_new", subscription_id="sub_new", value=80_000)
        # usage/summary: sub SELECT + plan SELECT + usage GROUP BY
        usage_agg = [{"metric": "tokens_used", "total": 80_000}]

        flow_results = [
            # create_subscription: plan SELECT + existing SELECT(None) + INSERT(无返回) + SELECT
            _Result(row=plan),
            _Result(row=None),
            _Result(),
            _Result(row=sub_created),
            # record_usage: sub SELECT + INSERT RETURNING
            _Result(row=sub_created),
            _Result(row=usage),
            # usage/summary: sub SELECT + plan SELECT + usage GROUP BY
            _Result(row=sub_created),
            _Result(row=plan),
            _Result(rows=usage_agg),
        ]
        conn = _RecordingConnection(results=flow_results)
        monkeypatch.setattr(b, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            sub_resp = await client.post(
                "/api/v1/billing/subscriptions",
                json={"plan_id": "plan_pro", "billing_cycle": "monthly"},
            )
            usage_resp = await client.post(
                "/api/v1/billing/usage",
                json={
                    "subscription_id": "sub_new",
                    "metric": "tokens_used",
                    "value": 80000,
                    "period_start": "2026-07-01T00:00:00+00:00",
                    "period_end": "2026-07-31T00:00:00+00:00",
                },
            )
            summary_resp = await client.get("/api/v1/billing/usage/summary")

        assert sub_resp.status_code == 201
        assert usage_resp.status_code == 201
        assert summary_resp.status_code == 200
        assert sub_resp.json()["id"] == "sub_new"
        assert usage_resp.json()["value"] == 80_000
        summary = summary_resp.json()
        tokens = next(m for m in summary["metrics"] if m["metric"] == "tokens_used")
        assert tokens["used"] == 80_000
        assert tokens["quota"] == 100_000
        assert tokens["exceeded"] is False  # 80000 < 100000


# ============================================================================
# 12. check_quota 边界
# ============================================================================


class TestCheckQuota:
    """check_quota 辅助函数测试。"""

    @pytest.mark.asyncio
    async def test_check_quota_no_active_subscription(self, monkeypatch):
        """check_quota 无 active 订阅返回 reason=no_active_subscription, exceeded=False。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(b, "pool", _Pool(conn))

        result = await b.check_quota("wsp_test", "tokens_used", 100)
        assert result["exceeded"] is False
        assert result["reason"] == "no_active_subscription"
        assert result["quota"] is None

    @pytest.mark.asyncio
    async def test_check_quota_metric_not_capped(self, monkeypatch):
        """check_quota 未知 metric 返回 reason=metric_not_capped。"""
        sub = _sub_row()
        conn = _RecordingConnection(results=[_Result(row=sub)])
        monkeypatch.setattr(b, "pool", _Pool(conn))

        result = await b.check_quota("wsp_test", "unknown_metric", 100)
        assert result["reason"] == "metric_not_capped"
        assert result["exceeded"] is False

    @pytest.mark.asyncio
    async def test_check_quota_within_limit(self, monkeypatch):
        """check_quota used+value <= quota 时 exceeded=False。"""
        # check_quota 的 SQL 做 JOIN，返回 sub 字段 + plan quota 字段
        sub = _sub_row(token_quota=100_000_000, seat_quota=20, storage_quota_gb=100, api_rate_limit=1200)
        conn = _RecordingConnection(
            results=[_Result(row=sub), _Result(row={"used": 1_000})]
        )
        monkeypatch.setattr(b, "pool", _Pool(conn))

        result = await b.check_quota("wsp_test", "tokens_used", 1_000)
        assert result["used"] == 1_000
        assert result["projected_total"] == 2_000
        assert result["exceeded"] is False  # 2000 < 100000000
