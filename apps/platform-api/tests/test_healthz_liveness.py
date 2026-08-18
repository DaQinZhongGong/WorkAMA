"""Regression guard: /healthz must stay a pure liveness endpoint (zero deps).

Background (see deploy/perf/baseline-report.md §10.10 / §10.11): a worst-case
tail spike (~427-514ms) was observed on /healthz under cross-container load.
Investigation proved it is a CLIENT-SIDE (Python GIL in the urllib generator) /
Docker-network artifact, NOT a server defect — because /healthz does no DB/Redis/
business work (it returns a static JSON). The liveness/readiness split is already
in place: /healthz = pure liveness, /readyz = DB SELECT 1 + redis.ping.

This test locks the invariant so a future refactor cannot accidentally make
/healthz touch a dependency (which would (a) defeat the liveness split and
(b) reintroduce a real server-side tail on the probe path).
"""

from __future__ import annotations

import json

import pytest

from workama_platform import main


class _DependencyBomb:
    """Any attribute access / call raises — proves healthz never reaches a dep."""

    def __getattr__(self, item: str):
        raise AssertionError(f"/healthz touched a dependency via '{item}'")

    async def connection(self):
        raise AssertionError("/healthz opened a DB connection")

    def ping(self):
        raise AssertionError("/healthz pinged redis")


@pytest.mark.asyncio
async def test_healthz_returns_ok(monkeypatch):
    resp = await main.healthz()
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body.get("status") == "ok"
    assert body.get("service") == "platform-api"
    # 禁止缓存，避免探针拿到过期状态
    assert resp.headers.get("Cache-Control") == "no-cache"


@pytest.mark.asyncio
async def test_healthz_touches_no_dependencies(monkeypatch):
    """用会爆炸的假对象替换 main 模块的 pool / redis；healthz 若触碰即失败。"""
    monkeypatch.setattr(main, "pool", _DependencyBomb())
    monkeypatch.setattr(main, "redis", _DependencyBomb())
    # 若 healthz 内部引用了 pool/redis，_DependencyBomb 会抛 AssertionError
    resp = await main.healthz()
    assert resp.status_code == 200
