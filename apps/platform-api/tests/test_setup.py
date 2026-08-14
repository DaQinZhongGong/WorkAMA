from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from workama_platform.modules import setup


# --- 测试辅助：模拟 psycopg 连接池与事务 --------------------------------


class _Result:
    def __init__(self, row=None):
        self._row = row

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return []


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _BootstrapConnection:
    """记录 execute 调用；按查询内容区分 advisory lock / exists / insert。"""

    def __init__(self, *, exists: bool = False):
        self._exists = exists
        self.calls = []

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if "EXISTS" in query.upper():
            return _Result(row={"exists": self._exists})
        return _Result()

    def transaction(self):
        return _Transaction()


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


def _valid_body(
    *,
    email: str = "owner@example.com",
    password: str = "secure-password-10",
    display_name: str = "Owner",
    organization_name: str = "Acme",
    workspace_name: str = "Acme WS",
) -> setup.BootstrapRequest:
    return setup.BootstrapRequest(
        email=email,
        password=password,
        display_name=display_name,
        organization_name=organization_name,
        workspace_name=workspace_name,
    )


# --- GET /api/v1/setup/status -------------------------------------------


@pytest.mark.asyncio
async def test_setup_status_reports_uninitialized_when_no_users(monkeypatch):
    # Arrange: id_user 表为空
    monkeypatch.setattr(setup, "pool", _Pool(_BootstrapConnection(exists=False)))

    # Act
    result = await setup.setup_status()

    # Assert
    assert result == {
        "initialized": False,
        "setup_token_required": True,
        "external_backup_configured": False,
    }


@pytest.mark.asyncio
async def test_setup_status_reports_initialized_when_users_exist(monkeypatch):
    # Arrange: id_user 表已有用户
    monkeypatch.setattr(setup, "pool", _Pool(_BootstrapConnection(exists=True)))

    # Act
    result = await setup.setup_status()

    # Assert
    assert result["initialized"] is True
    assert result["setup_token_required"] is True
    assert result["external_backup_configured"] is False


# --- POST /api/v1/setup/bootstrap ---------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_rejects_invalid_setup_token(monkeypatch):
    # Arrange: 配置真实 token，但请求传入错误 token
    monkeypatch.setattr(setup.settings, "setup_token", "real-token")

    # Act + Assert: token 不匹配 → 403
    with pytest.raises(HTTPException) as exc:
        await setup.bootstrap(_valid_body(), x_setup_token="wrong-token")
    assert exc.value.status_code == 403
    assert exc.value.detail == "Invalid setup token"


@pytest.mark.asyncio
async def test_bootstrap_rejects_when_setup_token_not_configured(monkeypatch):
    # Arrange: setup_token 为空 → 任何请求都应被拒
    monkeypatch.setattr(setup.settings, "setup_token", "")

    # Act + Assert
    with pytest.raises(HTTPException) as exc:
        await setup.bootstrap(_valid_body(), x_setup_token="anything")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_bootstrap_creates_owner_and_workspace_when_uninitialized(monkeypatch):
    # Arrange: 未初始化 + 有效 token
    monkeypatch.setattr(setup.settings, "setup_token", "valid-token")
    conn = _BootstrapConnection(exists=False)
    monkeypatch.setattr(setup, "pool", _Pool(conn))

    # Act
    result = await setup.bootstrap(
        _valid_body(email="Owner@Example.com", display_name="  Owner Name  "),
        x_setup_token="valid-token",
    )

    # Assert: 返回 owner 身份与已初始化标志
    assert result["initialized"] is True
    assert result["email"] == "owner@example.com"  # 小写化
    assert result["role"] == "owner"
    assert result["workspace_id"].startswith("wsp_")

    # 断言关键 INSERT 均被执行：user / org / workspace / member / bill_account / grant / channel
    joined = "\n".join(q for q, _ in conn.calls)
    assert "INSERT INTO id_user" in joined
    assert "INSERT INTO id_org" in joined
    assert "INSERT INTO id_workspace" in joined
    assert "INSERT INTO id_member" in joined
    assert "INSERT INTO bill_account" in joined
    assert "INSERT INTO bill_credit_grant" in joined
    assert "INSERT INTO gw_channel" in joined


@pytest.mark.asyncio
async def test_bootstrap_normalizes_email_and_display_name(monkeypatch):
    # Arrange: 验证 email 小写化、display_name 去首尾空格
    monkeypatch.setattr(setup.settings, "setup_token", "valid-token")
    conn = _BootstrapConnection(exists=False)
    monkeypatch.setattr(setup, "pool", _Pool(conn))

    # Act
    await setup.bootstrap(
        _valid_body(email="OWNER@Example.com", display_name="  Alice  "),
        x_setup_token="valid-token",
    )

    # Assert: id_user 插入参数中 email 已小写、display_name 已去首尾空格
    # INSERT INTO id_user(id,email,password_hash,display_name,email_verified) VALUES (%s,%s,%s,%s,TRUE)
    # params: (user_id, email, password_hash, display_name)
    user_insert = next((q, p) for q, p in conn.calls if q.startswith("INSERT INTO id_user"))
    _, params = user_insert
    assert params[1] == "owner@example.com"
    assert params[3] == "Alice"


@pytest.mark.asyncio
async def test_bootstrap_conflicts_when_already_initialized(monkeypatch):
    # Arrange: 已初始化 → EXISTS 返回 True
    monkeypatch.setattr(setup.settings, "setup_token", "valid-token")
    conn = _BootstrapConnection(exists=True)
    monkeypatch.setattr(setup, "pool", _Pool(conn))

    # Act + Assert: 幂等保护触发 409
    with pytest.raises(HTTPException) as exc:
        await setup.bootstrap(_valid_body(), x_setup_token="valid-token")
    assert exc.value.status_code == 409
    assert exc.value.detail == "WorkAMA is already initialized"


def test_bootstrap_rejects_short_password_via_pydantic():
    # Arrange: 密码长度 < 10 → pydantic 校验失败（min_length=10）
    # Act + Assert
    with pytest.raises(ValidationError):
        setup.BootstrapRequest(
            email="owner@example.com",
            password="short",
            display_name="Owner",
            organization_name="Acme",
            workspace_name="Acme WS",
        )


@pytest.mark.asyncio
async def test_bootstrap_uses_advisory_lock_then_exists_check_for_idempotency(monkeypatch):
    # Arrange: 验证幂等性保护顺序——先获取 advisory lock，再 EXISTS 检查
    monkeypatch.setattr(setup.settings, "setup_token", "valid-token")
    conn = _BootstrapConnection(exists=False)
    monkeypatch.setattr(setup, "pool", _Pool(conn))

    # Act
    await setup.bootstrap(_valid_body(), x_setup_token="valid-token")

    # Assert: pg_advisory_xact_lock 先于 EXISTS 执行
    lock_index = next(i for i, (q, _) in enumerate(conn.calls) if "pg_advisory_xact_lock" in q)
    exists_index = next(i for i, (q, _) in enumerate(conn.calls) if "EXISTS" in q.upper())
    assert lock_index < exists_index
    assert "pg_advisory_xact_lock(913006)" in conn.calls[lock_index][0]


# --- 路由契约 ------------------------------------------------------------


def test_setup_router_exposes_status_and_bootstrap_contracts():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in setup.router.routes}
    assert ("/api/v1/setup/status", ("GET",)) in paths
    assert ("/api/v1/setup/bootstrap", ("POST",)) in paths
