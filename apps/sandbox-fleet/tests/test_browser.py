"""测试 netpolicy 模块与 browser HTTP 端点。

覆盖场景：
1. is_domain_allowed 精确匹配
2. is_domain_allowed 通配符匹配
3. build_egress_rules 返回 none 模式（非 browser 镜像）
4. build_egress_rules 返回 bridge 模式（browser 镜像）
5. POST /internal/sandboxes/{id}/browser 端点成功调用 agentd_call
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from workama_sandbox import main
from workama_sandbox.netpolicy import (
    DEFAULT_ALLOWED_DOMAINS,
    BROWSER_EGRESS_NETWORK,
    build_egress_rules,
    is_domain_allowed,
)


# ---------------------------------------------------------------------------
# 辅助：假的 psycopg 连接池 / 连接 / 结果
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, row=None):
        self._row = row

    async def fetchone(self):
        return self._row


class _FakeConn:
    """支持配置 fetchone 返回值的假连接。"""

    def __init__(self, row=None):
        self._row = row

    async def execute(self, *_args, **_kwargs):
        return _FakeResult(self._row)

    async def commit(self):
        pass


class _FakePool:
    """AsyncConnectionPool 的替身，yield 一个假连接。"""

    def __init__(self, row=None):
        self._conn = _FakeConn(row)

    def connection(self):
        @contextlib.asynccontextmanager
        async def _cm():
            yield self._conn

        return _cm()


# ---------------------------------------------------------------------------
# 测试 1：is_domain_allowed 精确匹配
# ---------------------------------------------------------------------------


def test_is_domain_allowed_exact_match():
    """精确匹配：白名单中的域名应放行，不在白名单的应拒绝。"""
    allowed = ["example.com", "github.com"]
    assert is_domain_allowed("example.com", allowed) is True
    assert is_domain_allowed("github.com", allowed) is True
    # 不在白名单
    assert is_domain_allowed("evil.com", allowed) is False
    assert is_domain_allowed("notexample.com", allowed) is False
    # 空白名单
    assert is_domain_allowed("example.com", []) is False
    # None 白名单一律拒绝
    assert is_domain_allowed("example.com", None) is False
    # 大小写不敏感
    assert is_domain_allowed("EXAMPLE.com", ["example.com"]) is True


# ---------------------------------------------------------------------------
# 测试 2：is_domain_allowed 通配符匹配
# ---------------------------------------------------------------------------


def test_is_domain_allowed_wildcard_match():
    """通配符 *.example.com 应匹配子域名，但不匹配裸域。"""
    allowed = ["*.wikipedia.org", "*.github.com"]
    # 子域名匹配
    assert is_domain_allowed("en.wikipedia.org", allowed) is True
    assert is_domain_allowed("api.github.com", allowed) is True
    assert is_domain_allowed("raw.githubusercontent.com", allowed) is False  # 不是 .github.com 后缀
    # 通配符不匹配裸域本身
    assert is_domain_allowed("wikipedia.org", allowed) is False
    assert is_domain_allowed("github.com", allowed) is False
    # 多级子域名也应匹配
    assert is_domain_allowed("meta.en.wikipedia.org", allowed) is True


# ---------------------------------------------------------------------------
# 测试 3：build_egress_rules 返回 none 模式（非 browser 镜像）
# ---------------------------------------------------------------------------


def test_build_egress_rules_none_mode_for_non_browser():
    """sandbox-base / sandbox-code 等非 browser 镜像应返回 network_mode='none'。"""
    result = build_egress_rules(image="sandbox-base")
    assert result == {"network_mode": "none"}

    result = build_egress_rules(image="sandbox-code")
    assert result == {"network_mode": "none"}

    # 默认 image 参数也是 none 模式
    result = build_egress_rules()
    assert result == {"network_mode": "none"}
    assert "network" not in result
    assert "allowed_domains" not in result


# ---------------------------------------------------------------------------
# 测试 4：build_egress_rules 返回 bridge 模式（browser 镜像）
# ---------------------------------------------------------------------------


def test_build_egress_rules_bridge_mode_for_browser():
    """sandbox-browser 镜像应返回自定义 bridge 网络 + 域名白名单。"""
    result = build_egress_rules(image="sandbox-browser")
    assert result["network_mode"] is None
    assert result["network"] == BROWSER_EGRESS_NETWORK
    assert result["allowed_domains"] == DEFAULT_ALLOWED_DOMAINS

    # 自定义白名单
    custom = build_egress_rules(image="sandbox-browser", allowed_domains=["foo.com", "*.bar.com"])
    assert custom["network"] == BROWSER_EGRESS_NETWORK
    assert custom["allowed_domains"] == ["foo.com", "*.bar.com"]


# ---------------------------------------------------------------------------
# 测试 5：browser HTTP 端点成功调用 agentd_call
# ---------------------------------------------------------------------------


async def test_browser_endpoint_calls_agentd(monkeypatch):
    """POST /internal/sandboxes/{id}/browser 应透传 body 调用 agentd_call('BrowserOp', body)。"""
    row = {"id": "sbx_test", "status": "active", "container_id": "c1"}
    monkeypatch.setattr(main, "pool", _FakePool(row=row))

    fake_container = MagicMock()
    fake_docker = MagicMock()
    fake_docker.containers.get = MagicMock(return_value=fake_container)
    monkeypatch.setattr(main, "docker_client", fake_docker)

    expected = {"ok": True, "action": "navigate", "url": "https://example.com"}
    monkeypatch.setattr(main, "agentd_call", AsyncMock(return_value=expected))

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://browser.test") as client:
        response = await client.post(
            "/internal/sandboxes/sbx_test/browser",
            json={"action": "navigate", "target": "https://example.com", "timeout_ms": 10000},
            headers={"x-internal-token": main.settings.internal_token},
        )

    assert response.status_code == 200
    assert response.json() == expected

    # 验证 agentd_call 以正确的参数被调用
    main.agentd_call.assert_called_once()
    call_args = main.agentd_call.call_args
    # 第一个位置参数是 container 对象
    assert call_args.args[0] is fake_container
    # 第二个参数是 RPC 方法名
    assert call_args.args[1] == "BrowserOp"
    # 第三个参数是透传的 body dict
    payload = call_args.args[2]
    assert payload["action"] == "navigate"
    assert payload["target"] == "https://example.com"
    assert payload["timeout_ms"] == 10000


# ---------------------------------------------------------------------------
# 补充测试：browser 端点对非活跃沙箱返回 409
# ---------------------------------------------------------------------------


async def test_browser_endpoint_rejects_non_active_sandbox(monkeypatch):
    """非活跃沙箱调用 browser 端点应返回 409。"""
    row = {"id": "sbx_test", "status": "sleeping", "container_id": "c1"}
    monkeypatch.setattr(main, "pool", _FakePool(row=row))
    monkeypatch.setattr(main, "docker_client", MagicMock())
    monkeypatch.setattr(main, "agentd_call", AsyncMock())

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://browser.test") as client:
        response = await client.post(
            "/internal/sandboxes/sbx_test/browser",
            json={"action": "screenshot"},
            headers={"x-internal-token": main.settings.internal_token},
        )

    assert response.status_code == 409
    main.agentd_call.assert_not_called()
