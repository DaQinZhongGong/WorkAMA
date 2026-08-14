"""测试 sandbox-fleet 的镜像路由逻辑。

覆盖场景：
1. container_options(image="sandbox-code") 选用 settings.sandbox_code_image
2. container_options(image="sandbox-browser") 选用 settings.sandbox_browser_image
3. container_options(image="sandbox-base") 选用 settings.sandbox_image
4. container_options(image="") 默认选用 settings.sandbox_image
5. sandbox-code 的 egress 是 network_mode="none"（沿用 netpolicy.build_egress_rules）

对齐《520-Agent引擎与运行时设计》§4.2 三镜像设计。
"""

from __future__ import annotations

import pytest

from workama_sandbox import main
from workama_sandbox.main import container_options, settings


@pytest.fixture
def _fake_runtime_available(monkeypatch):
    """让 container_options 跳过 gVisor / Firecracker runtime 检查。

    container_options 会调用 runtime_available() 判断运行时是否就绪，
    测试环境下没有真实 Docker daemon，故 mock 为 (True, ["runsc"])。
    """
    monkeypatch.setattr(main, "runtime_available", lambda: (True, ["runsc"]))


def _opts(image: str) -> dict:
    """构造 container_options 调用并返回结果。"""
    return container_options("sbx_test", "vol_test", image=image)


# ---------------------------------------------------------------------------
# 测试 1：sandbox-code 镜像路由
# ---------------------------------------------------------------------------


def test_sandbox_code_image_selected(_fake_runtime_available):
    """container_options(image='sandbox-code') 应选用 sandbox_code_image。"""
    opts = _opts("sandbox-code")
    assert opts["image"] == settings.sandbox_code_image
    assert opts["image"] == "workama-sandbox-code:local"


# ---------------------------------------------------------------------------
# 测试 2：sandbox-browser 镜像路由
# ---------------------------------------------------------------------------


def test_sandbox_browser_image_selected(_fake_runtime_available):
    """container_options(image='sandbox-browser') 应选用 sandbox_browser_image。"""
    opts = _opts("sandbox-browser")
    assert opts["image"] == settings.sandbox_browser_image
    assert opts["image"] == "workama-sandbox-browser:local"


# ---------------------------------------------------------------------------
# 测试 3：sandbox-base 镜像路由
# ---------------------------------------------------------------------------


def test_sandbox_base_image_selected(_fake_runtime_available):
    """container_options(image='sandbox-base') 应选用 sandbox_image。"""
    opts = _opts("sandbox-base")
    assert opts["image"] == settings.sandbox_image
    assert opts["image"] == "workama-sandbox-agentd:local"


# ---------------------------------------------------------------------------
# 测试 4：image='' 默认走 sandbox-base
# ---------------------------------------------------------------------------


def test_empty_image_defaults_to_sandbox_base(_fake_runtime_available):
    """image='' 等同 sandbox-base，选用 sandbox_image。"""
    opts = _opts("")
    assert opts["image"] == settings.sandbox_image


# ---------------------------------------------------------------------------
# 测试 5：sandbox-code 的 egress 是 network_mode="none"
# ---------------------------------------------------------------------------


def test_sandbox_code_egress_is_network_none(_fake_runtime_available):
    """sandbox-code 的 egress 必须是 network_mode='none'，禁止出网。"""
    opts = _opts("sandbox-code")
    assert opts["network_mode"] == "none"
    # 确保没有走 bridge 网络
    assert "network" not in opts


def test_sandbox_base_egress_is_network_none(_fake_runtime_available):
    """sandbox-base 的 egress 也应是 network_mode='none'。"""
    opts = _opts("sandbox-base")
    assert opts["network_mode"] == "none"
    assert "network" not in opts


def test_sandbox_browser_egress_uses_bridge_network(_fake_runtime_available):
    """sandbox-browser 应走自定义 bridge 网络，不走 network_mode='none'。"""
    opts = _opts("sandbox-browser")
    assert opts.get("network") == "workama-browser-egress"
    assert "network_mode" not in opts
