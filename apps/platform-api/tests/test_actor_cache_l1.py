"""Unit tests for the actor-path process-local L1 cache (core.get_actor).

get_actor resolves an ``Actor`` from a JWT on the hot path. With L1 wired in
(core.py), a warmed L1 must short-circuit both the L2 redis lookup AND the
per-request DB query, which is the dominant residual contributor to P99. These
tests drive get_actor directly with fakes (no real JWT, no DB, no redis) to
prove the L1 semantics in isolation; the live cross-container run proves them
end-to-end.
"""

from __future__ import annotations

import json

import pytest
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials

from workama_platform import core


def _make_request() -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/assistants",
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 80),
    }
    return Request(scope)


class _Result:
    def __init__(self, row):
        self.row = row

    async def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, row):
        self.row = row
        self.execute_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *a, **k):
        self.execute_calls += 1
        return _Result(self.row)


class _Pool:
    def __init__(self, row):
        self.conn = _Conn(row)

    def connection(self):
        return self.conn


_ROW = {
    "user_id": "usr_x",
    "email": "x@y.com",
    "display_name": "X",
    "onboarding_completed": True,
    "workspace_id": "ws_x",
    "org_id": "org_x",
    "role": "owner",
}


@pytest.mark.asyncio
async def test_actor_l1_short_circuits_db(monkeypatch):
    """首次走 DB 填充 L1；第二次命中 L1，必须不再查询 DB。"""
    monkeypatch.setattr(core, "_ACTOR_CACHE_ENABLED", True)
    core._ACTOR_LOCAL.clear()

    async def _fake_decode(token, expected_type="access"):
        return {"sub": "usr_x", "ws": "ws_x", "auth_strength": 2}

    monkeypatch.setattr(core, "decode_token_cached", _fake_decode)
    set_calls = {"n": 0}

    async def _fake_set(k, v, t):
        set_calls["n"] += 1

    async def _fake_get(k):
        return None  # L2 miss -> force DB

    monkeypatch.setattr(core, "cache_set", _fake_set)
    monkeypatch.setattr(core, "cache_get", _fake_get)
    fp = _Pool(_ROW)
    monkeypatch.setattr(core, "pool", fp)

    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="fake.jwt.token")
    req = _make_request()
    a1 = await core.get_actor(req, creds, None)
    a2 = await core.get_actor(req, creds, None)

    assert fp.conn.execute_calls == 1  # 第二次请求由 L1 命中，无 DB 查询
    assert a1 == a2
    assert set_calls["n"] >= 1  # 首次请求填充了 L1（+ L2）


@pytest.mark.asyncio
async def test_actor_l1_refilled_from_l2_hit(monkeypatch):
    """L2 命中时回填 L1，且首次即可跳过 DB；二次 L1 直接命中。"""
    monkeypatch.setattr(core, "_ACTOR_CACHE_ENABLED", True)
    core._ACTOR_LOCAL.clear()

    async def _fake_decode(token, expected_type="access"):
        return {"sub": "usr_x", "ws": "ws_x", "auth_strength": 2}

    monkeypatch.setattr(core, "decode_token_cached", _fake_decode)
    l2_json = json.dumps({
        "user_id": "usr_x",
        "workspace_id": "ws_x",
        "org_id": "org_x",
        "role": "owner",
        "email": "x@y.com",
        "display_name": "X",
        "onboarding_completed": True,
        "capabilities": ["*"],
        "auth_strength": 2,
    })

    async def _fake_get(k):
        return l2_json

    async def _fake_set(k, v, t):
        pass

    monkeypatch.setattr(core, "cache_get", _fake_get)
    monkeypatch.setattr(core, "cache_set", _fake_set)
    fp = _Pool(_ROW)
    monkeypatch.setattr(core, "pool", fp)

    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="fake.jwt.token")
    req = _make_request()
    a1 = await core.get_actor(req, creds, None)
    a2 = await core.get_actor(req, creds, None)

    assert fp.conn.execute_calls == 0  # L2 命中 -> 全程无 DB
    assert a1 == a2
    # 第二次请求应直接命中 L1，不再次回源 L2（L1 已被 L2 命中回填）
    assert core._ACTOR_LOCAL.get(core._actor_cache_key("fake.jwt.token")) is not None
