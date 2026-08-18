"""进程内 L1 TTL 缓存（尾延迟收口）。

背景：platform-api 的 actor / list 热点路径已用 Redis 做 L2 读穿透缓存，但每次请求仍需
2 次**跨容器** Redis GET（actor + list），单次 RTT 约 1-5ms，是 P99 未达 <30ms 的残余主因。
本模块提供线程安全的进程内 TTL 缓存，作为 L1 置于 Redis 之前：热读命中 L1 即返回（亚毫秒、
无需序列化、无网络），仅在 L1 未命中才回源 Redis / DB。

设计要点：
- 纯内存 dict + threading.Lock，同步接口（无 I/O），可在 async 路径内直接调用。
- TTL 过期在 get/set 时惰性判定；maxsize LRU 淘汰防止内存无界增长。
- best-effort：调用方自行 try/except，缓存异常不影响主路径正确性。
- 每个 worker 进程持独立实例（Granian 多进程模型），故 L1 为 per-worker；列表写后需同时失效
  本地 L1 与 Redis L2（跨 worker 的 L1 陈旧由 L1 TTL 兜底，与既有 Redis TTL 取舍一致）。
"""

from __future__ import annotations

import threading
import time
from typing import Any


class LocalTTLCache:
    """线程安全、TTL + 可选 LRU 淘汰的进程内缓存。值可为任意可 pickle/可持有的对象。"""

    __slots__ = ("_ttl", "_maxsize", "_store", "_lock")

    def __init__(self, ttl: float = 60.0, maxsize: int = 1024) -> None:
        self._ttl = float(ttl)
        self._maxsize = int(maxsize)
        # key -> (expire_at: float, value: Any)；dict 保序（py3.7+）以支持 LRU。
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            expire_at, value = item
            if expire_at < time.monotonic():
                self._store.pop(key, None)
                return None
            # LRU 触碰：移到末尾
            self._store.pop(key)
            self._store[key] = item
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if key not in self._store and len(self._store) >= self._maxsize:
                # 淘汰最旧（dict 首项是 LRU 末端）
                try:
                    self._store.pop(next(iter(self._store)))
                except StopIteration:
                    pass
            self._store[key] = (time.monotonic() + self._ttl, value)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        return len(self._store)
