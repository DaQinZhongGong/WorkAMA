"""T-M6-008 AMA-Work 浏览器自动化测试。

策略：通过 monkeypatch ``_resolve_async_playwright`` 注入假的 Playwright，
不依赖真实浏览器即可验证：
- 操作指令分发
- 批次执行（含 navigate 隐式前置步骤）
- 隔离模式校验
- 会话生命周期管理
- API 端点契约
- 错误处理（playwright 缺失 / 会话不存在 / 跨工作空间访问）
"""
from __future__ import annotations

import asyncio
import base64
from typing import Any

import pytest
from fastapi import HTTPException

from workama_platform.core import Actor
from workama_platform.modules import browser_automation as ba


def _actor(workspace_id: str = "wsp_owner", role: str = "owner") -> Actor:
    return Actor(
        user_id="usr_owner",
        workspace_id=workspace_id,
        org_id="org_1",
        role=role,
        email="owner@example.test",
        display_name="Owner",
        onboarding_completed=True,
        capabilities=() if role == "viewer" else ("*",),
    )

# ---------------------------------------------------------------------------
# Fake Playwright 实现：模拟 Page / Browser / Context / Playwright
# ---------------------------------------------------------------------------


class _FakePage:
    """模拟 playwright.Page；记录所有调用以便断言。"""

    def __init__(self, browser):
        self._browser = browser
        self.calls = []
        self._goto_url = None

    async def goto(self, url, **kwargs):
        self.calls.append(("goto", {"url": url, **kwargs}))
        self._goto_url = url
        if self._browser._goto_exc:
            raise self._browser._goto_exc

    async def click(self, selector, **kwargs):
        self.calls.append(("click", {"selector": selector, **kwargs}))
        if self._browser._click_exc:
            raise self._browser._click_exc

    async def fill(self, selector, value, **kwargs):
        self.calls.append(("fill", {"selector": selector, "value": value, **kwargs}))

    async def screenshot(self, **kwargs):
        self.calls.append(("screenshot", kwargs))
        if self._browser._screenshot_exc:
            raise self._browser._screenshot_exc
        return self._browser._screenshot_bytes

    async def inner_text(self, selector, **kwargs):
        self.calls.append(("inner_text", {"selector": selector, **kwargs}))
        return self._browser._inner_text

    async def get_attribute(self, selector, attr):
        self.calls.append(("get_attribute", {"selector": selector, "attr": attr}))
        return self._browser._attribute_value

    async def wait_for_selector(self, selector, **kwargs):
        self.calls.append(("wait_for_selector", {"selector": selector, **kwargs}))

    async def evaluate(self, expression):
        self.calls.append(("evaluate", {"expression": expression}))
        return self._browser._eval_result

    async def close(self):
        self.calls.append(("close", {}))

    @property
    def mouse(self):
        return _FakeMouse(self)

class _FakeMouse:
    def __init__(self, page):
        self._page = page

    async def wheel(self, x, y):
        self._page.calls.append(("wheel", {"x": x, "y": y}))


class _FakeContext:
    def __init__(self, browser):
        self._browser = browser
        self.closed = False
        self.pages = []

    async def new_page(self):
        page = _FakePage(self._browser)
        self.pages.append(page)
        self._browser._last_page = page
        return page

    async def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self):
        self._contexts = []
        self.closed = False
        self.launch_opts = {}
        self._goto_exc = None
        self._click_exc = None
        self._screenshot_exc = None
        self._screenshot_bytes = b"\x89PNG\r\n\x1a\n"
        self._inner_text = "Example Domain"
        self._attribute_value = "attr-value"
        self._eval_result = {"ok": True}
        self._last_page = None

    async def new_context(self, **kwargs):
        ctx = _FakeContext(self)
        self._contexts.append(ctx)
        return ctx

    async def close(self):
        self.closed = True


class _FakePlaywright:
    def __init__(self):
        self._browser = _FakeBrowser()
        self.stopped = False

    @property
    def chromium(self):
        return self

    async def launch(self, **kwargs):
        self._browser.launch_opts = kwargs
        return self._browser

    async def stop(self):
        self.stopped = True


def _install_fake_playwright(monkeypatch, *, browser=None):
    """替换 _resolve_async_playwright，返回 _FakePlaywright 工厂。"""
    fake_pw = _FakePlaywright()
    if browser is not None:
        fake_pw._browser = browser

    class _CtxMgr:
        async def start(self):
            return fake_pw

    def _async_playwright():
        return _CtxMgr()

    monkeypatch.setattr(ba, "_resolve_async_playwright", lambda: _async_playwright)
    return fake_pw._browser

# ---------------------------------------------------------------------------
# 测试 1：BrowserAction / BrowserActionBatch 模型校验
# ---------------------------------------------------------------------------


def test_browser_action_accepts_all_supported_action_types():
    for action_type in ("navigate", "click", "type", "screenshot", "extract", "wait", "scroll", "evaluate"):
        action = ba.BrowserAction(action=action_type, selector="#x", value="v", options={"k": 1})
        assert action.action == action_type
        assert action.selector == "#x"
        assert action.value == "v"
        assert action.options == {"k": 1}
        assert action.timeout == 30.0


def test_browser_action_batch_defaults_and_round_trip():
    batch = ba.BrowserActionBatch(actions=[ba.BrowserAction(action="click", selector="#btn")])
    assert batch.return_extracted is True
    assert len(batch.actions) == 1


def test_browser_action_result_dataclass_round_trip():
    result = ba.BrowserActionResult(action="click", success=True, data={"x": 1}, elapsed_ms=12)
    assert result.action == "click"
    assert result.success is True
    assert result.data == {"x": 1}
    assert result.error is None
    assert result.elapsed_ms == 12

# ---------------------------------------------------------------------------
# 测试 2：BrowserAutomationExecutor 隔离模式校验
# ---------------------------------------------------------------------------


def test_executor_rejects_unsupported_isolation_mode():
    with pytest.raises(ValueError):
        ba.BrowserAutomationExecutor(isolation_mode="bogus")


def test_executor_accepts_all_supported_isolation_modes():
    for mode in ba.ISOLATION_MODES:
        exe = ba.BrowserAutomationExecutor(isolation_mode=mode)
        assert exe.isolation_mode == mode
        assert exe.headless is True


@pytest.mark.asyncio
async def test_executor_start_uses_incognito_context_when_isolation_is_incognito(monkeypatch):
    browser = _install_fake_playwright(monkeypatch)
    exe = ba.BrowserAutomationExecutor(isolation_mode="incognito", headless=False)
    await exe.start()
    try:
        assert browser.launch_opts["headless"] is False
        # incognito 模式直接 new_context()，不传 user_data_dir
        assert len(browser._contexts) == 1
    finally:
        await exe.close()
    assert browser.closed is True


@pytest.mark.asyncio
async def test_executor_start_uses_temp_user_data_dir_for_ephemeral(monkeypatch, tmp_path):
    browser = _install_fake_playwright(monkeypatch)
    exe = ba.BrowserAutomationExecutor(isolation_mode="ephemeral")
    await exe.start()
    try:
        assert len(browser._contexts) == 1
    finally:
        await exe.close()


@pytest.mark.asyncio
async def test_executor_start_container_mode_attaches_no_sandbox_args(monkeypatch):
    browser = _install_fake_playwright(monkeypatch)
    exe = ba.BrowserAutomationExecutor(isolation_mode="container")
    await exe.start()
    try:
        assert "--no-sandbox" in browser.launch_opts["args"]
        assert "--disable-dev-shm-usage" in browser.launch_opts["args"]
    finally:
        await exe.close()


@pytest.mark.asyncio
async def test_executor_close_is_idempotent(monkeypatch):
    _install_fake_playwright(monkeypatch)
    exe = ba.BrowserAutomationExecutor()
    await exe.start()
    await exe.close()
    await exe.close()  # 第二次调用不应抛异常


@pytest.mark.asyncio
async def test_executor_execute_batch_raises_when_not_started():
    exe = ba.BrowserAutomationExecutor()
    with pytest.raises(RuntimeError, match="not started"):
        await exe.execute_batch("https://example.com", ba.BrowserActionBatch(actions=[]))

# ---------------------------------------------------------------------------
# 测试 3：execute_batch 单个 action 分发
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_batch_navigates_then_runs_actions_in_order(monkeypatch):
    browser = _install_fake_playwright(monkeypatch)
    exe = ba.BrowserAutomationExecutor()
    await exe.start()
    try:
        batch = ba.BrowserActionBatch(actions=[
            ba.BrowserAction(action="click", selector="#btn"),
            ba.BrowserAction(action="type", selector="#input", value="hello"),
        ])
        results = await exe.execute_batch("https://example.com", batch)
    finally:
        await exe.close()

    # 第一个结果是隐式的 navigate
    assert results[0].action == "navigate"
    assert results[0].success is True
    assert results[0].data == {"url": "https://example.com"}
    assert results[1].action == "click"
    assert results[1].success is True
    assert results[2].action == "type"
    assert results[2].success is True

    page = browser._last_page
    assert page.calls[0][0] == "goto"
    assert page.calls[0][1]["url"] == "https://example.com"
    assert page.calls[1][0] == "click"
    assert page.calls[2][0] == "fill"


@pytest.mark.asyncio
async def test_execute_batch_screenshot_returns_base64_png(monkeypatch):
    browser = _install_fake_playwright(monkeypatch)
    browser._screenshot_bytes = b"\x89PNG\r\n\x1a\nfake-bytes"
    exe = ba.BrowserAutomationExecutor()
    await exe.start()
    try:
        batch = ba.BrowserActionBatch(actions=[
            ba.BrowserAction(action="screenshot", options={"full_page": True}),
        ])
        results = await exe.execute_batch("https://example.com", batch)
    finally:
        await exe.close()
    screenshot_result = next(r for r in results if r.action == "screenshot")
    assert screenshot_result.success is True
    expected_b64 = base64.b64encode(browser._screenshot_bytes).decode("ascii")
    assert screenshot_result.data == expected_b64

@pytest.mark.asyncio
async def test_execute_batch_extract_returns_inner_text_or_attribute(monkeypatch):
    browser = _install_fake_playwright(monkeypatch)
    browser._inner_text = "Hello, world!"
    browser._attribute_value = "link-attr"
    exe = ba.BrowserAutomationExecutor()
    await exe.start()
    try:
        batch = ba.BrowserActionBatch(actions=[
            ba.BrowserAction(action="extract", selector="#title"),
            ba.BrowserAction(action="extract", selector="a", options={"attribute": "href"}),
        ])
        results = await exe.execute_batch("https://example.com", batch)
    finally:
        await exe.close()
    extract_results = [r for r in results if r.action == "extract"]
    assert extract_results[0].success is True
    assert extract_results[0].data == "Hello, world!"
    assert extract_results[1].success is True
    assert extract_results[1].data == "link-attr"


@pytest.mark.asyncio
async def test_execute_batch_evaluate_returns_js_result(monkeypatch):
    browser = _install_fake_playwright(monkeypatch)
    browser._eval_result = {"answer": 42}
    exe = ba.BrowserAutomationExecutor()
    await exe.start()
    try:
        batch = ba.BrowserActionBatch(actions=[
            ba.BrowserAction(action="evaluate", value="document.title"),
        ])
        results = await exe.execute_batch("https://example.com", batch)
    finally:
        await exe.close()
    eval_result = next(r for r in results if r.action == "evaluate")
    assert eval_result.success is True
    assert eval_result.data == {"answer": 42}


@pytest.mark.asyncio
async def test_execute_batch_scroll_invokes_mouse_wheel(monkeypatch):
    browser = _install_fake_playwright(monkeypatch)
    exe = ba.BrowserAutomationExecutor()
    await exe.start()
    try:
        batch = ba.BrowserActionBatch(actions=[
            ba.BrowserAction(action="scroll", options={"x": 100, "y": 200}),
        ])
        results = await exe.execute_batch("https://example.com", batch)
    finally:
        await exe.close()
    scroll_result = next(r for r in results if r.action == "scroll")
    assert scroll_result.success is True
    assert scroll_result.data == {"x": 100, "y": 200}
    assert ("wheel", {"x": 100, "y": 200}) in browser._last_page.calls

@pytest.mark.asyncio
async def test_execute_batch_wait_uses_selector_when_provided(monkeypatch):
    browser = _install_fake_playwright(monkeypatch)
    exe = ba.BrowserAutomationExecutor()
    await exe.start()
    try:
        batch = ba.BrowserActionBatch(actions=[
            ba.BrowserAction(action="wait", selector=".loaded", timeout=5.0),
        ])
        results = await exe.execute_batch("https://example.com", batch)
    finally:
        await exe.close()
    wait_result = next(r for r in results if r.action == "wait")
    assert wait_result.success is True
    assert ("wait_for_selector", {"selector": ".loaded", "timeout": 5000.0}) in browser._last_page.calls


@pytest.mark.asyncio
async def test_execute_batch_action_failure_stops_by_default(monkeypatch):
    browser = _install_fake_playwright(monkeypatch)
    browser._click_exc = RuntimeError("element not found")
    exe = ba.BrowserAutomationExecutor()
    await exe.start()
    try:
        batch = ba.BrowserActionBatch(actions=[
            ba.BrowserAction(action="click", selector="#missing"),
            ba.BrowserAction(action="screenshot"),  # 不应执行
        ])
        results = await exe.execute_batch("https://example.com", batch)
    finally:
        await exe.close()
    click_result = next(r for r in results if r.action == "click")
    assert click_result.success is False
    assert "element not found" in click_result.error
    # screenshot 没有被执行（默认遇到失败就 break）
    assert not any(r.action == "screenshot" for r in results)

@pytest.mark.asyncio
async def test_execute_batch_continue_on_error(monkeypatch):
    browser = _install_fake_playwright(monkeypatch)
    browser._click_exc = RuntimeError("element not found")
    exe = ba.BrowserAutomationExecutor()
    await exe.start()
    try:
        batch = ba.BrowserActionBatch(actions=[
            ba.BrowserAction(action="click", selector="#missing", options={"continue_on_error": True}),
            ba.BrowserAction(action="screenshot"),
        ])
        results = await exe.execute_batch("https://example.com", batch)
    finally:
        await exe.close()
    assert any(r.action == "click" and not r.success for r in results)
    assert any(r.action == "screenshot" and r.success for r in results)


@pytest.mark.asyncio
async def test_execute_batch_unsupported_action_returns_failure_result(monkeypatch):
    _install_fake_playwright(monkeypatch)
    exe = ba.BrowserAutomationExecutor()
    await exe.start()
    try:
        batch = ba.BrowserActionBatch(actions=[
            ba.BrowserAction(action="bogus"),
        ])
        results = await exe.execute_batch("https://example.com", batch)
    finally:
        await exe.close()
    bogus_result = next(r for r in results if r.action == "bogus")
    assert bogus_result.success is False
    assert "unsupported browser action" in bogus_result.error


@pytest.mark.asyncio
async def test_execute_batch_missing_playwright_raises_runtime_error(monkeypatch):
    """_resolve_async_playwright 抛 RuntimeError 时 start() 应原样抛出。"""
    def _raise_resolver():
        raise RuntimeError("playwright is not installed")

    monkeypatch.setattr(ba, "_resolve_async_playwright", _raise_resolver)
    exe = ba.BrowserAutomationExecutor()
    with pytest.raises(RuntimeError, match="playwright is not installed"):
        await exe.start()

# ---------------------------------------------------------------------------
# 测试 4：BrowserSessionManager 生命周期与资源限制
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_manager_create_close_lifecycle():
    manager = ba.BrowserSessionManager(max_concurrent_sessions=5)
    session = await manager.create_session(
        workspace_id="wsp_1", created_by="usr_1", isolation_mode="incognito",
    )
    assert session.state == "active"
    assert session.workspace_id == "wsp_1"
    assert session.executor is not None
    assert manager.get(session.session_id) is session
    assert manager.active_count == 1

    closed = await manager.close_session(session.session_id)
    assert closed is True
    assert session.state == "closed"
    assert session.executor is None
    assert manager.get(session.session_id) is None
    # 再次关闭幂等
    closed_again = await manager.close_session(session.session_id)
    assert closed_again is False


@pytest.mark.asyncio
async def test_session_manager_require_returns_404_for_unknown_session():
    manager = ba.BrowserSessionManager()
    with pytest.raises(HTTPException) as exc:
        manager.require("missing", workspace_id="wsp_1")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_session_manager_require_returns_404_for_cross_workspace_access():
    manager = ba.BrowserSessionManager()
    session = await manager.create_session(
        workspace_id="wsp_1", created_by="usr_1",
    )
    with pytest.raises(HTTPException) as exc:
        manager.require(session.session_id, workspace_id="wsp_2")
    assert exc.value.status_code == 404
    await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_session_manager_enforces_max_concurrent_sessions():
    manager = ba.BrowserSessionManager(max_concurrent_sessions=2)
    s1 = await manager.create_session(workspace_id="wsp_1", created_by="u1")
    s2 = await manager.create_session(workspace_id="wsp_1", created_by="u1")
    with pytest.raises(HTTPException) as exc:
        await manager.create_session(workspace_id="wsp_1", created_by="u1")
    assert exc.value.status_code == 429
    # 关闭一个后可以新建
    await manager.close_session(s1.session_id)
    s3 = await manager.create_session(workspace_id="wsp_1", created_by="u1")
    assert s3.state == "active"
    await manager.close_session(s2.session_id)
    await manager.close_session(s3.session_id)

@pytest.mark.asyncio
async def test_session_manager_touch_transitions_idle_back_to_active():
    manager = ba.BrowserSessionManager()
    session = await manager.create_session(workspace_id="wsp_1", created_by="u1")
    await manager.mark_idle(session.session_id)
    assert session.state == "idle"
    # active_count 不再包含 idle 状态会话
    assert manager.active_count == 0
    await manager.touch(session.session_id)
    assert session.state == "active"
    assert manager.active_count == 1
    await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_session_manager_evict_expired_removes_idle_sessions():
    manager = ba.BrowserSessionManager(session_timeout_seconds=0.01)
    session = await manager.create_session(workspace_id="wsp_1", created_by="u1")
    # 等待超时
    await asyncio.sleep(0.05)
    evicted = await manager.evict_expired()
    assert evicted == 1
    assert manager.get(session.session_id) is None
    assert session.state == "closed"
    assert session.closed_at is not None


@pytest.mark.asyncio
async def test_session_manager_create_evicts_before_capacity_check():
    """create_session 内部先清理超时再检查容量。"""
    manager = ba.BrowserSessionManager(
        max_concurrent_sessions=1, session_timeout_seconds=0.01,
    )
    # 第一个会话很快超时
    s1 = await manager.create_session(workspace_id="wsp_1", created_by="u1")
    await asyncio.sleep(0.05)
    # 因为 create_session 会先 evict，所以即便 active_count 接近上限也能新建
    s2 = await manager.create_session(workspace_id="wsp_1", created_by="u1")
    assert s1.session_id != s2.session_id
    assert manager.active_count == 1
    await manager.close_session(s2.session_id)

# ---------------------------------------------------------------------------
# 测试 5：API 端点契约（路由形状 + 权限 + 路径）
# ---------------------------------------------------------------------------


def test_browser_router_exposes_session_action_screenshot_close_routes():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in ba.router.routes}
    assert ("/api/v1/work/browser/sessions", ("POST",)) in paths
    assert ("/api/v1/work/browser/sessions/{session_id}/actions", ("POST",)) in paths
    assert ("/api/v1/work/browser/sessions/{session_id}/screenshot", ("GET",)) in paths
    assert ("/api/v1/work/browser/sessions/{session_id}", ("DELETE",)) in paths


def test_router_prefix_is_under_work_browser_namespace():
    assert ba.router.prefix == "/api/v1/work/browser"


def test_session_create_request_validates_isolation_mode_default():
    body = ba.SessionCreate()
    assert body.isolation_mode == "incognito"
    assert body.headless is True


def test_session_action_request_requires_url_and_actions():
    body = ba.SessionActionRequest(
        url="https://example.com",
        actions=[ba.BrowserAction(action="click", selector="#btn")],
    )
    assert body.url == "https://example.com"
    assert body.return_extracted is True

# ---------------------------------------------------------------------------
# 测试 6：权限校验
# ---------------------------------------------------------------------------


def test_require_work_write_allows_owner_admin_member():
    for role in ("owner", "admin", "member"):
        ba._require_work_write(_actor(role=role))


def test_require_work_write_rejects_viewer():
    with pytest.raises(HTTPException) as exc:
        ba._require_work_write(_actor(role="viewer"))
    assert exc.value.status_code == 403


def test_require_work_read_allows_viewer():
    ba._require_work_read(_actor(role="viewer"))


def test_require_work_write_rejects_service_account_actor():
    sa_actor = Actor(
        user_id="usr_sa", workspace_id="wsp", org_id="org", role="service_account",
        email="sa@example.test", display_name="SA", onboarding_completed=True,
        actor_type="service_account",
    )
    with pytest.raises(HTTPException) as exc:
        ba._require_work_write(sa_actor)
    assert exc.value.status_code == 403


def test_require_work_write_allows_explicit_capability():
    cap_actor = Actor(
        user_id="usr_cap", workspace_id="wsp", org_id="org", role="viewer",
        email="cap@example.test", display_name="CAP", onboarding_completed=True,
        capabilities=("work:write",),
    )
    ba._require_work_write(cap_actor)

# ---------------------------------------------------------------------------
# 测试 7：API 端点集成（直接调用路由函数，替换 _session_manager）
# ---------------------------------------------------------------------------


class _StubExecutor:
    """可控制的执行器 stub；记录 execute_batch 调用并返回预设结果。"""

    def __init__(self, results):
        self._results = results
        self.closed = False
        self.execute_calls = []

    async def execute_batch(self, url, batch):
        self.execute_calls.append((url, batch))
        return list(self._results)

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_create_browser_session_endpoint_returns_201(monkeypatch):
    manager = ba.BrowserSessionManager()
    monkeypatch.setattr(ba, "get_session_manager", lambda: manager)
    body = ba.SessionCreate(isolation_mode="ephemeral", headless=False)
    result = await ba.create_browser_session(body, _actor())
    assert result["state"] == "active"
    assert result["isolation_mode"] == "ephemeral"
    assert result["headless"] is False
    assert result["session_id"].startswith("wbs_")
    await manager.close_session(result["session_id"])


@pytest.mark.asyncio
async def test_create_browser_session_rejects_invalid_isolation_mode(monkeypatch):
    manager = ba.BrowserSessionManager()
    monkeypatch.setattr(ba, "get_session_manager", lambda: manager)
    body = ba.SessionCreate(isolation_mode="bogus")
    with pytest.raises(HTTPException) as exc:
        await ba.create_browser_session(body, _actor())
    assert exc.value.status_code == 422