"""Sandbox Fleet 配置中心同步器。

从 platform-api ``/internal/config/export`` 按版本轮询 **DB(UI) 发布覆盖**
（``overrides`` 视图），热应用到本进程 RuntimeSettings，实现
「可视化配置 → sandbox-fleet 热下发」：控制台发布 → Redis 版本号递增 →
本轮询器拉取新快照 → 回调应用（reaper / 预热池 / 容量检查 / 新建容器即时生效）。

设计要点（与 Go 网关 internal/configsync 同语义）：
- 只应用 ``overrides``（仅 DB 来源）：未发布的键回落本地 ENV 基线，
  严格保持「DB(UI) > ENV > 代码默认」优先级；UI 删除覆盖即回落。
- version 未变化不触发回调，避免热更新抖动。
- 失败指数退避（interval → 8×interval 封顶），恢复后立即回归常规节奏；
  绝不因配置中心不可用阻断沙箱服务。
- 密钥字段不消费：sandbox 分组全部为非密钥运行参数。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

import httpx

logger = logging.getLogger("sandbox-fleet.config-sync")

DEFAULT_INTERVAL = 1.0
REQUEST_TIMEOUT = 5.0

OverrideApplier = Callable[[dict[str, Any]], list[str]]


class ConfigSyncPoller:
    """按版本轮询配置中心导出视图，把 DB 覆盖交给 applier 热应用。"""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        applier: OverrideApplier,
        interval: float = DEFAULT_INTERVAL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = base_url.rstrip("/") + "/internal/config/export"
        self.token = token
        self.applier = applier
        self.interval = max(0.5, interval)
        self._client = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        self._last_version = -1

    async def fetch_once(self) -> dict[str, Any] | None:
        """拉取一次导出视图；返回 {"version": int, "overrides": dict} 或 None。"""
        try:
            resp = await self._client.get(self.url, headers={"X-Internal-Token": self.token})
        except Exception as exc:  # noqa: BLE001 — 网络/超时一律走退避
            logger.warning("config sync fetch failed: %s", exc)
            return None
        if resp.status_code == 401:
            logger.error("config sync unauthorized: INTERNAL_TOKEN mismatch")
            return None
        if resp.status_code != 200:
            logger.warning("config sync http %d", resp.status_code)
            return None
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("config sync decode failed: %s", exc)
            return None
        overrides = data.get("overrides")
        if not isinstance(overrides, dict):
            # 兼容旧版 platform-api：无 overrides 视图时视为空集（全基线）。
            overrides = {}
        try:
            version = int(data.get("version", 0))
        except (TypeError, ValueError):
            version = 0
        return {"version": version, "overrides": overrides}

    async def run(self) -> None:
        """阻塞式轮询直到任务被取消。"""
        backoff = self.interval
        max_backoff = 8 * self.interval
        while True:
            snap = await self.fetch_once()
            if snap is None:
                await asyncio.sleep(backoff)
                if backoff < max_backoff:
                    backoff *= 2
                continue
            backoff = self.interval
            if snap["version"] != self._last_version:
                changed = self.applier(snap["overrides"])
                if changed:
                    logger.info(
                        "sandbox config hot-applied from config center (version=%s): %s",
                        snap["version"],
                        ",".join(changed),
                    )
                else:
                    logger.debug("config center snapshot applied (version=%s): no sandbox changes", snap["version"])
                self._last_version = snap["version"]
            await asyncio.sleep(self.interval)


def build_sandbox_applier(runtime_settings: Any) -> OverrideApplier:
    """构造 applier：reset-to-baseline 后应用 overrides 中 sandbox 键，返回变更键列表。"""

    def apply(overrides: dict[str, Any]) -> list[str]:
        filtered = {k: v for k, v in overrides.items() if k in runtime_settings.HOT_KEYS}
        return runtime_settings.apply_overrides(filtered)

    return apply


def start_config_sync(
    runtime_settings: Any,
    *,
    base_url: str,
    token: str,
    interval: float = DEFAULT_INTERVAL,
) -> asyncio.Task[None]:
    """创建后台同步任务（lifespan 内保存引用；关闭时 cancel 即可）。"""
    poller = ConfigSyncPoller(
        base_url=base_url,
        token=token,
        applier=build_sandbox_applier(runtime_settings),
        interval=interval,
    )
    return asyncio.create_task(poller.run(), name="sandbox-config-sync")

