"""浏览器自动化执行器 - 基于 Playwright 的生产隔离浏览器自动化。

T-M6-008: AMA-Work 浏览器自动化执行环境。

设计要点：
- 隔离模式：``container`` / ``incognito`` / ``ephemeral``，默认 ``incognito``。
- Playwright 在方法内 lazy import，避免未安装时模块加载失败。
- 会话生命周期：``active`` → ``idle`` → ``closed``，超时自动回收。
- 资源限制：全局并发会话上限，超过返回 429。
- 安全第一：浏览器实例在隔离上下文中运行；截图/抽取内容统一标记 untrusted。
"""
from __future__ import annotations

import asyncio
import base64
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from workama_platform.core import Actor, capability_allows, get_actor, new_id

router = APIRouter(prefix="/api/v1/work/browser", tags=["ama-work-browser"])

# 隔离模式与状态枚举（用元组保留字面量提示）
ISOLATION_MODES: tuple[str, ...] = ("container", "incognito", "ephemeral")
SESSION_STATES: tuple[str, ...] = ("active", "idle", "closed")

# 默认会话超时 5 分钟；最大并发会话数 20
DEFAULT_SESSION_TIMEOUT_SECONDS: float = 300.0
MAX_CONCURRENT_SESSIONS: int = 20

# 抽取文本最大长度，避免单次响应过大
MAX_EXTRACT_TEXT_CHARS: int = 50_000


class BrowserAction(BaseModel):
    """浏览器操作指令。

    支持的 action 类型：
    - ``navigate``: 导航到 ``value`` 指定的 URL
    - ``click``: 点击 ``selector`` 选中的元素
    - ``type``: 在 ``selector`` 选中的输入框中输入 ``value`` 文本
    - ``screenshot``: 截图；``options.full_page`` 控制是否整页
    - ``extract``: 提取元素文本或属性；``options.attribute`` 指定属性名
    - ``wait``: 等待元素出现（``selector``）或固定时长（``value`` 秒）
    - ``scroll``: 滚动到 ``options.x`` / ``options.y`` 像素位置
    - ``evaluate``: 执行 ``value`` 中的 JavaScript 表达式
    """

    action: str = Field(..., description="navigate/click/type/screenshot/extract/wait/scroll/evaluate")
    selector: str | None = None
    value: str | None = None
    timeout: float = 30.0
    options: dict = Field(default_factory=dict)


class BrowserActionBatch(BaseModel):
    """浏览器操作批次。"""

    actions: list[BrowserAction]
    return_extracted: bool = True


@dataclass
class BrowserActionResult:
    """浏览器操作结果。"""

    action: str
    success: bool
    data: Any = None
    error: str | None = None
    elapsed_ms: int = 0


@dataclass
class BrowserSession:
    """浏览器会话状态。"""

    session_id: str
    workspace_id: str
    created_by: str
    isolation_mode: str
    headless: bool
    state: str = "active"
    last_activity: float = field(default_factory=time.monotonic)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    closed_at: datetime | None = None
    executor: BrowserAutomationExecutor | None = None
    last_screenshot: str | None = None
    last_actions: list[BrowserActionResult] = field(default_factory=list)


def _resolve_async_playwright():
    """延迟导入 playwright.async_api.async_playwright。

    单独函数便于测试 monkeypatch；未安装时抛 RuntimeError。
    """
    try:
        from playwright.async_api import async_playwright  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - 由测试覆盖错误分支
        raise RuntimeError(
            "playwright is not installed; install with `pip install playwright` "
            "and `playwright install chromium`"
        ) from exc
    return async_playwright


class BrowserAutomationExecutor:
    """浏览器自动化执行器（生产隔离）。

    用法：
        async with BrowserAutomationExecutor(isolation_mode="incognito") as exe:
            results = await exe.execute_batch(url, batch)

    或显式启动/关闭：
        exe = BrowserAutomationExecutor()
        await exe.start()
        try:
            results = await exe.execute_batch(url, batch)
        finally:
            await exe.close()
    """

    def __init__(self, *, isolation_mode: str = "incognito", headless: bool = True):
        if isolation_mode not in ISOLATION_MODES:
            raise ValueError(f"unsupported isolation_mode: {isolation_mode}")
        self.isolation_mode = isolation_mode  # container/incognito/ephemeral
        self.headless = headless
        self._browser = None
        self._context = None
        self._playwright = None
        self._tmp_user_data_dir: str | None = None

    async def __aenter__(self) -> "BrowserAutomationExecutor":
        await self.start()
        return self

    async def __aexit__(self, *_args) -> None:
        await self.close()

    async def start(self) -> None:
        """启动隔离浏览器环境。"""
        async_playwright = _resolve_async_playwright()
        self._playwright = await async_playwright().start()
        launch_opts: dict[str, Any] = {"headless": self.headless}
        if self.isolation_mode == "container":
            # 容器隔离：交给 sandbox-fleet 编排；此处仍以 chromium 启动并
            # 附加 --no-sandbox / --disable-dev-shm-usage 保证容器内可运行。
            launch_opts["args"] = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
        self._browser = await self._playwright.chromium.launch(**launch_opts)
        if self.isolation_mode == "incognito":
            self._context = await self._browser.new_context()
        else:  # ephemeral / container
            self._tmp_user_data_dir = tempfile.mkdtemp(prefix="workama-browser-")
            self._context = await self._browser.new_context(
                user_data_dir=self._tmp_user_data_dir,
            )

    async def close(self) -> None:
        """关闭并清理浏览器资源；多次调用幂等。"""
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        if self._tmp_user_data_dir and os.path.isdir(self._tmp_user_data_dir):
            shutil.rmtree(self._tmp_user_data_dir, ignore_errors=True)
            self._tmp_user_data_dir = None

    async def execute_batch(self, url: str, batch: BrowserActionBatch) -> list[BrowserActionResult]:
        """执行浏览器操作批次。

        流程：启动浏览器（如未启动）→ 新建页面 → 导航到 url → 依次执行 actions → 收集结果。
        任意步骤失败抛 RuntimeError；调用方负责将异常映射为 HTTP 错误。
        """
        if self._context is None:
            raise RuntimeError("BrowserAutomationExecutor is not started; call start() first")
        page = await self._context.new_page()
        results: list[BrowserActionResult] = []
        try:
            # 隐式插入 navigate 步骤，保证批次执行前页面已就绪
            results.append(await self._execute_action(page, BrowserAction(action="navigate", value=url)))
            for action in batch.actions:
                result = await self._execute_action(page, action)
                results.append(result)
                # 默认遇到失败立即停止；options.continue_on_error=True 时继续
                if not result.success and not action.options.get("continue_on_error", False):
                    break
        finally:
            try:
                await page.close()
            except Exception:
                pass
        return results

    async def _execute_action(self, page, action: BrowserAction) -> BrowserActionResult:
        """执行单个浏览器操作。"""
        start = time.monotonic()
        try:
            data: Any = None
            if action.action == "navigate":
                target = action.value or ""
                if not target:
                    raise ValueError("navigate action requires a value (URL)")
                await page.goto(target, timeout=action.timeout * 1000)
                data = {"url": target}
            elif action.action == "click":
                if not action.selector:
                    raise ValueError("click action requires a selector")
                await page.click(action.selector, timeout=action.timeout * 1000)
            elif action.action == "type":
                if not action.selector:
                    raise ValueError("type action requires a selector")
                await page.fill(action.selector, action.value or "", timeout=action.timeout * 1000)
            elif action.action == "screenshot":
                png = await page.screenshot(full_page=action.options.get("full_page", False))
                data = base64.b64encode(png).decode("ascii")
            elif action.action == "extract":
                if not action.selector:
                    raise ValueError("extract action requires a selector")
                attr = action.options.get("attribute")
                if attr:
                    data = await page.get_attribute(action.selector, attr)
                else:
                    text = await page.inner_text(action.selector)
                    data = text[:MAX_EXTRACT_TEXT_CHARS] if isinstance(text, str) else text
            elif action.action == "wait":
                if action.selector:
                    await page.wait_for_selector(action.selector, timeout=action.timeout * 1000)
                else:
                    await asyncio.sleep(float(action.value or 0))
            elif action.action == "scroll":
                x = int(action.options.get("x", 0))
                y = int(action.options.get("y", 0))
                await page.mouse.wheel(x, y)
                data = {"x": x, "y": y}
            elif action.action == "evaluate":
                if not action.value:
                    raise ValueError("evaluate action requires a value (JS expression)")
                data = await page.evaluate(action.value)
            else:
                raise ValueError(f"unsupported browser action: {action.action}")
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return BrowserActionResult(action=action.action, success=True, data=data, elapsed_ms=elapsed_ms)
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return BrowserActionResult(
                action=action.action, success=False, error=str(exc), elapsed_ms=elapsed_ms
            )


class BrowserSessionManager:
    """浏览器会话管理器。

    - 跟踪会话状态：``active`` / ``idle`` / ``closed``
    - 超时自动清理（默认 5 分钟无活动）
    - 资源限制：最大并发会话数
    - 工作空间隔离：会话只能被所属 workspace 访问
    """

    def __init__(
        self,
        *,
        max_concurrent_sessions: int = MAX_CONCURRENT_SESSIONS,
        session_timeout_seconds: float = DEFAULT_SESSION_TIMEOUT_SECONDS,
    ):
        self._sessions: dict[str, BrowserSession] = {}
        self.max_concurrent_sessions = max_concurrent_sessions
        self.session_timeout_seconds = session_timeout_seconds
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.state == "active")

    @property
    def total_count(self) -> int:
        return len(self._sessions)

    def get(self, session_id: str) -> BrowserSession | None:
        return self._sessions.get(session_id)

    def require(self, session_id: str, *, workspace_id: str) -> BrowserSession:
        """按工作空间范围查找会话；不存在或跨工作空间访问返回 404。"""
        session = self._sessions.get(session_id)
        if not session or session.workspace_id != workspace_id:
            raise HTTPException(status_code=404, detail="Browser session not found")
        if session.state == "closed":
            raise HTTPException(status_code=410, detail="Browser session is closed")
        return session

    async def create_session(
        self,
        *,
        workspace_id: str,
        created_by: str,
        isolation_mode: str = "incognito",
        headless: bool = True,
    ) -> BrowserSession:
        async with self._lock:
            self._evict_expired_locked()
            if self.active_count >= self.max_concurrent_sessions:
                raise HTTPException(status_code=429, detail="Too many concurrent browser sessions")
            executor = BrowserAutomationExecutor(isolation_mode=isolation_mode, headless=headless)
            session = BrowserSession(
                session_id=new_id("wbs"),
                workspace_id=workspace_id,
                created_by=created_by,
                isolation_mode=isolation_mode,
                headless=headless,
                executor=executor,
            )
            self._sessions[session.session_id] = session
            return session

    async def touch(self, session_id: str) -> None:
        """刷新会话最近活动时间；idle 状态自动转回 active。"""
        session = self._sessions.get(session_id)
        if session:
            session.last_activity = time.monotonic()
            if session.state == "idle":
                session.state = "active"

    async def mark_idle(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session and session.state == "active":
            session.state = "idle"

    async def close_session(self, session_id: str) -> bool:
        """关闭会话并释放浏览器资源；幂等。返回是否真的关闭了一个会话。"""
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            if not session:
                return False
            if session.executor is not None:
                try:
                    await session.executor.close()
                except Exception:
                    pass
            session.executor = None
            session.state = "closed"
            session.closed_at = datetime.now(UTC)
            return True

    def _evict_expired_locked(self) -> int:
        """清理超时会话；返回被清理的数量。必须在持有 _lock 时调用。"""
        now = time.monotonic()
        expired = [
            sid for sid, s in self._sessions.items()
            if s.state != "closed" and (now - s.last_activity) > self.session_timeout_seconds
        ]
        for sid in expired:
            session = self._sessions.pop(sid, None)
            if session:
                session.state = "closed"
                session.closed_at = datetime.now(UTC)
        return len(expired)

    async def evict_expired(self) -> int:
        """主动清理超时会话；同步路径返回被清理数量。"""
        async with self._lock:
            return self._evict_expired_locked()


# 全局单例；通过 get_session_manager() 暴露便于测试 monkeypatch
_session_manager = BrowserSessionManager()


def get_session_manager() -> BrowserSessionManager:
    return _session_manager


# ---- 请求/响应模型 ----


class SessionCreate(BaseModel):
    """创建浏览器会话请求。"""

    isolation_mode: str = Field(default="incognito")
    headless: bool = True


class SessionCreateResponse(BaseModel):
    session_id: str
    isolation_mode: str
    headless: bool
    state: str
    created_at: datetime


class SessionActionRequest(BaseModel):
    """在已有会话上执行浏览器操作批次。"""

    url: str
    actions: list[BrowserAction]
    return_extracted: bool = True


# ---- 权限校验 ----


def _require_work_write(actor: Actor) -> None:
    if actor.actor_type != "user":
        raise HTTPException(status_code=403, detail="User authentication is required")
    if capability_allows(actor.capabilities, "work:write"):
        return
    if actor.role in {"owner", "admin", "member"}:
        return
    raise HTTPException(status_code=403, detail="Missing capability: work:write")


def _require_work_read(actor: Actor) -> None:
    if actor.actor_type != "user":
        raise HTTPException(status_code=403, detail="User authentication is required")
    if capability_allows(actor.capabilities, "work:read"):
        return
    if actor.role in {"owner", "admin", "member", "viewer"}:
        return
    raise HTTPException(status_code=403, detail="Missing capability: work:read")


# ---- API 端点 ----


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def create_browser_session(
    body: SessionCreate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """创建浏览器会话；隔离模式可选 container/incognito/ephemeral。"""
    _require_work_write(actor)
    if body.isolation_mode not in ISOLATION_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"isolation_mode must be one of {ISOLATION_MODES}",
        )
    manager = get_session_manager()
    session = await manager.create_session(
        workspace_id=actor.workspace_id,
        created_by=actor.user_id,
        isolation_mode=body.isolation_mode,
        headless=body.headless,
    )
    return {
        "session_id": session.session_id,
        "isolation_mode": session.isolation_mode,
        "headless": session.headless,
        "state": session.state,
        "created_at": session.created_at,
    }


@router.post("/sessions/{session_id}/actions")
async def execute_browser_actions(
    session_id: str,
    body: SessionActionRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """在指定会话上执行浏览器操作批次。"""
    _require_work_write(actor)
    manager = get_session_manager()
    session = manager.require(session_id, workspace_id=actor.workspace_id)
    if session.executor is None:
        raise HTTPException(status_code=410, detail="Browser session executor is closed")
    await manager.touch(session_id)
    batch = BrowserActionBatch(actions=body.actions, return_extracted=body.return_extracted)
    try:
        results = await session.executor.execute_batch(body.url, batch)
    except RuntimeError as exc:
        # 启动失败 / playwright 缺失等运行时错误映射为 503
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # 缓存最新 screenshot，便于 GET /screenshot 端点返回
    for result in results:
        if result.action == "screenshot" and result.success and isinstance(result.data, str):
            session.last_screenshot = result.data
    session.last_actions = results
    extracted = (
        [r.data for r in results if r.action == "extract" and r.success]
        if body.return_extracted
        else []
    )
    return {
        "session_id": session_id,
        "results": [r.__dict__ for r in results],
        "extracted": extracted,
        # 浏览器自动抓取的内容统一标记为 untrusted，调用方不得作为可信证据
        "untrusted": True,
    }


@router.get("/sessions/{session_id}/screenshot")
async def get_browser_screenshot(
    session_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """获取会话最近一次截图（base64 PNG）。"""
    _require_work_read(actor)
    manager = get_session_manager()
    session = manager.require(session_id, workspace_id=actor.workspace_id)
    if not session.last_screenshot:
        raise HTTPException(status_code=404, detail="No screenshot available for this session")
    return {"session_id": session_id, "screenshot": session.last_screenshot, "untrusted": True}


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def close_browser_session(
    session_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """关闭浏览器会话；幂等。"""
    _require_work_write(actor)
    manager = get_session_manager()
    # 跨工作空间访问返回 404，避免泄漏存在性
    session = manager.get(session_id)
    if session and session.workspace_id != actor.workspace_id:
        raise HTTPException(status_code=404, detail="Browser session not found")
    await manager.close_session(session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
