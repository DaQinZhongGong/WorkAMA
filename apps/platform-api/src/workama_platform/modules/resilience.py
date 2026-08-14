"""Resilience primitives for the WorkAMA platform API.

提供多可用区高可用所需的弹性能力：

* ``db_with_retry`` —— DB 操作重试装饰器（指数退避 + jitter），仅重试连接类错误。
* ``GracefulShutdown`` —— 跟踪进行中请求，支持优雅关闭等待（drain）。
* ``GET /api/v1/system/deepz`` —— 深度健康检查端点（Liveness 不变，新增 Readiness 深度版）。
* ``make_redis_client`` —— 根据配置创建 Redis 客户端，支持哨兵模式（best-effort）。

设计约束：
* 不修改 ``main.py`` / ``core.py``；路由注册由主 Agent 接线。
* 不引入第三方依赖，仅使用 asyncio + 标准库 + 已有 redis/psycopg。
* 现有 ``/healthz`` 与 ``/readyz`` 端点保持不变，本模块新增 ``/api/v1/system/deepz``。
"""
from __future__ import annotations

import asyncio
import random

import psycopg.errors
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout
from redis.asyncio import Redis
from redis.asyncio.sentinel import Sentinel

from workama_platform.core import pool, redis


# ==============================================================================
# 1. DB 连接池重试
# ==============================================================================

# 可重试的连接类异常（数据修改类错误如 IntegrityError/ProgrammingError 不在此列，
# 因此不会被重试，直接向上抛出）。
_RETRYABLE_EXC: tuple[type[BaseException], ...] = (
    psycopg.errors.OperationalError,
    psycopg.errors.InterfaceError,
    PoolTimeout,
    ConnectionError,
)


async def db_with_retry(
    coro_factory,
    *,
    max_retries: int = 3,
    base_delay: float = 0.1,
    max_delay: float = 2.0,
):
    """执行 DB 操作，遇连接类异常自动重试（指数退避 + jitter）。

    ``coro_factory`` 是一个无参 async callable，每次调用返回一个新的 coroutine
    （每次重试都会重新获取连接，避免复用已失效的连接）。

    重试异常：
        * ``psycopg.errors.OperationalError`` / ``psycopg.errors.InterfaceError``
        * ``psycopg_pool.PoolTimeout``
        * ``ConnectionError``

    退避公式：``delay = min(base_delay * 2 ** attempt + random.random() * 0.1, max_delay)``

    非连接类错误（如 ``IntegrityError`` / ``ProgrammingError``）立即抛出，不重试。
    最后一次失败抛出原始异常。
    """
    last_exc: BaseException | None = None
    for attempt in range(max_retries):
        try:
            return await coro_factory()
        except _RETRYABLE_EXC as exc:
            last_exc = exc
            if attempt == max_retries - 1:
                raise
            delay = min(base_delay * (2 ** attempt) + random.random() * 0.1, max_delay)
            await asyncio.sleep(delay)
    # 理论不可达：循环要么 return，要么在最后一次 raise。
    if last_exc is not None:  # pragma: no cover - defensive
        raise last_exc
    raise RuntimeError("db_with_retry exhausted retries without an exception")  # pragma: no cover


# ==============================================================================
# 2. 优雅关闭管理器
# ==============================================================================


class GracefulShutdown:
    """跟踪进行中的请求，支持优雅关闭等待。

    * ``begin_request()`` 在请求开始时调用，正在关闭时返回 ``False`` 以拒绝新请求。
    * ``end_request()`` 在请求结束时调用，引用计数归零时唤醒 ``drain()``。
    * ``drain(timeout)`` 等待所有进行中请求完成，超时返回 ``False``。
    """

    def __init__(self) -> None:
        self._active_requests: int = 0
        self._shutting_down: bool = False
        # 当 _active_requests == 0 时 set，drain() 据此等待。
        self._zero_event: asyncio.Event = asyncio.Event()
        self._zero_event.set()

    def begin_request(self) -> bool:
        """请求开始时调用。若正在关闭返回 ``False``（拒绝新请求）。"""
        if self._shutting_down:
            return False
        self._active_requests += 1
        self._zero_event.clear()
        return True

    def end_request(self) -> None:
        """请求结束时调用。引用计数归零时唤醒等待中的 ``drain()``。"""
        if self._active_requests > 0:
            self._active_requests -= 1
        if self._active_requests == 0:
            self._zero_event.set()

    async def drain(self, timeout: float = 30.0) -> bool:
        """等待所有进行中请求完成。超时返回 ``False``。"""
        self._shutting_down = True
        if self._active_requests == 0:
            self._zero_event.set()
            return True
        try:
            await asyncio.wait_for(self._zero_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    @property
    def is_shutting_down(self) -> bool:
        """是否正在关闭（拒绝新请求）。"""
        return self._shutting_down

    @property
    def active_requests(self) -> int:
        """当前进行中的请求数（主要用于测试与可观测性）。"""
        return self._active_requests


# 模块级单例：供 deepz 端点与主 Agent 接线中间件使用。
shutdown = GracefulShutdown()


# ==============================================================================
# 3. 深度健康检查端点
# ==============================================================================

router = APIRouter(prefix="/api/v1/system", tags=["system"])


@router.get("/deepz")
async def deepz() -> JSONResponse:
    """深度健康检查（Readiness 深度版）。

    保留现有 ``/healthz``（Liveness）与 ``/readyz``（轻量 Readiness）不变，
    本端点额外聚合：DB 连通性 + Redis 连通性 + 优雅关闭状态。

    返回结构：
        ``{"status": "ready"|"unready",
           "checks": {"db": "ok"|"fail", "redis": "ok"|"skip"|"fail",
                      "shutdown": "active"|"idle"}}``

    * ``shutdown`` 为 ``active`` 时返回 503（正在关闭，不再接流量）。
    * DB 失败返回 503；Redis 失败返回 503；Redis 为 ``skip``（未配置）不影响就绪。
    """
    checks: dict[str, str] = {
        "db": "fail",
        "redis": "skip",
        "shutdown": "active" if shutdown.is_shutting_down else "idle",
    }

    # DB 连通性：执行 SELECT 1（参数化占位以保持参数化 SQL 习惯）。
    try:
        async with pool.connection() as conn:
            await conn.execute("SELECT %s", (1,))
        checks["db"] = "ok"
    except Exception:
        checks["db"] = "fail"

    # Redis 连通性：redis 为 None 时跳过（skip），不视为失败。
    if redis is not None:
        try:
            await redis.ping()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "fail"

    ready = (
        not shutdown.is_shutting_down
        and checks["db"] == "ok"
        and checks["redis"] != "fail"
    )
    status_code = 200 if ready else 503
    return JSONResponse(
        {"status": "ready" if ready else "unready", "checks": checks},
        status_code=status_code,
        headers={"Cache-Control": "no-cache"},
    )


# ==============================================================================
# 4. Redis 哨兵支持（best-effort，仅定义不接入）
# ==============================================================================


def _parse_sentinels(raw: str) -> list[tuple[str, int]]:
    """解析 ``host:port,host:port`` 形式的哨兵列表为 ``[(host, port), ...]``。"""
    parsed: list[tuple[str, int]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            host, _, port_str = item.partition(":")
            try:
                parsed.append((host.strip(), int(port_str.strip())))
            except ValueError:
                continue
        else:
            # 仅 host，使用 redis 默认端口 26379。
            parsed.append((item, 26379))
    return parsed


def make_redis_client(settings) -> Redis:
    """根据 ``settings`` 创建 Redis 客户端。

    * 若 ``settings.redis_sentinels`` 配置（逗号分隔 ``host:port`` 列表）且
      ``settings.redis_master_name`` 非空，则使用 ``redis.asyncio.sentinel.Sentinel``
      创建 master 客户端（哨兵模式，best-effort：失败回退单节点）。
    * 否则使用 ``Redis.from_url(settings.redis_url, decode_responses=True)``。

    注意：本函数只定义，不在 ``core.py`` 中替换 ``redis``，由主 Agent 决定是否接入。
    """
    sentinels_raw = getattr(settings, "redis_sentinels", "") or ""
    master_name = getattr(settings, "redis_master_name", "") or ""

    if sentinels_raw and master_name:
        sentinels = _parse_sentinels(sentinels_raw)
        if sentinels:
            try:
                sentinel = Sentinel(
                    sentinels,
                    socket_timeout=getattr(settings, "redis_socket_timeout", 0.5),
                    socket_connect_timeout=getattr(settings, "redis_socket_connect_timeout", 0.5),
                )
                return sentinel.master_for(master_name, decode_responses=True)
            except Exception:
                # best-effort：哨兵初始化失败时回退到单节点模式。
                pass

    return Redis.from_url(settings.redis_url, decode_responses=True)
