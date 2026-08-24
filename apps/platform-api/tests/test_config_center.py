"""配置中心（config_center）单元测试。

v7.180: 覆盖：
- 类型/枚举/范围/必填/邮箱/URL 校验
- 编解码（int/bool/list）往返
- 解析优先级：DB(UI) > ENV > 代码默认
- 启动期覆盖 settings 单例（UI 优先级最高，热生效基础）
- 密钥加解密落库、API 掩码、KEEP 哨兵
- 发布/审计/回滚全链路（fake-pool，不依赖真实 DB/Redis）
- 连接探测 host:port 解析与 TCP 探针（monkeypatch socket）

所有测试使用 fake pool/connection/redis，可在本地或容器内无外设运行。
"""
from __future__ import annotations

import socket
import time
import json
import datetime

import pytest

from workama_platform.modules import config_center as cc
from workama_platform.modules.config_center import (
    ConfigItem,
    ConfigRollback,
    ConfigUpdate,
    _coerce_in,
    _coerce_out,
    _parse_host_port,
    _tcp_probe,
    validate_value,
)


# ---------------------------------------------------------------------------
# fake infra
# ---------------------------------------------------------------------------


class _AIter:
    def __init__(self, rows):
        self._it = iter(rows)

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return list(self._rows)

    def __aiter__(self):
        return _AIter(self._rows)


class _Ctx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


class _ConnCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_a):
        return False


class _FakeConn:
    def __init__(self, db):
        self._db = db

    def transaction(self):
        return _Ctx()

    async def commit(self):
        return None

    async def execute(self, query, params=()):
        q = query.strip()
        if q.startswith("CREATE TABLE IF NOT EXISTS config_settings"):
            self._db.setdefault("config_settings", [])
            return _FakeResult([])
        if q.startswith("CREATE TABLE IF NOT EXISTS config_history"):
            self._db.setdefault("config_history", [])
            return _FakeResult([])
        if q.startswith("CREATE TABLE IF NOT EXISTS config_revision"):
            self._db.setdefault("config_revision", [])
            return _FakeResult([])
        if q.startswith("CREATE INDEX"):
            return _FakeResult([])
        if "FROM config_settings" in q and q.startswith("SELECT key, group_key, value"):
            rows = list(self._db.get("config_settings", []))
            if "WHERE key = ANY" in q:
                # 防御：key 列表必须是 str（防止把 ConfigField 对象当 key 传入）
                assert all(isinstance(k, str) for k in params[0]), f"非 str key: {params[0]!r}"
                keys = set(params[0])
                rows = [r for r in rows if r["key"] in keys]
            return _FakeResult([dict(r) for r in rows])
        if "FROM config_settings" in q and q.startswith("SELECT key, value, is_encrypted"):
            # _snapshot_now 的全量快照读取（回滚依赖此分支）
            return _FakeResult([dict(r) for r in self._db.get("config_settings", [])])
        if "FROM config_revision" in q and "snapshot_json" in q:
            for r in self._db.get("config_revision", []):
                if r["revision"] == params[0]:
                    snap = r["snapshot_json"]
                    if isinstance(snap, str):
                        snap = json.loads(snap)
                    return _FakeResult([{"snapshot_json": snap}])
            return _FakeResult([])
        if "MAX(revision)" in q:
            revs = [r["revision"] for r in self._db.get("config_revision", [])]
            return _FakeResult([{"m": max(revs) if revs else 0}])
        if "FROM config_history" in q:
            rows = list(self._db.get("config_history", []))
            if "WHERE key =" in q:
                rows = [r for r in rows if r["key"] == params[0]]
            rows.sort(key=lambda r: r.get("changed_at") or datetime.datetime.min, reverse=True)
            return _FakeResult([dict(r) for r in rows])
        if "FROM config_revision" in q and "changed_by" in q:
            return _FakeResult([dict(r) for r in self._db.get("config_revision", [])])
        if "INSERT INTO config_settings" in q:
            key, group_key, value, vtype, is_secret, is_encrypted, updated_by = params
            tbl = self._db.setdefault("config_settings", [])
            for r in tbl:
                if r["key"] == key:
                    r.update(value=value, value_type=vtype, is_secret=is_secret,
                             is_encrypted=is_encrypted, updated_by=updated_by)
                    break
            else:
                tbl.append({"key": key, "group_key": group_key, "value": value,
                            "value_type": vtype, "is_secret": is_secret,
                            "is_encrypted": is_encrypted, "updated_by": updated_by,
                            "updated_at": None})
            return _FakeResult([])
        if q.startswith("DELETE FROM config_settings"):
            key = params[0]
            self._db["config_settings"] = [
                r for r in self._db.get("config_settings", []) if r["key"] != key
            ]
            return _FakeResult([])
        if "INSERT INTO config_history" in q:
            rid, rev, k, gk, ov, nv, oenc, nenc, cb = params
            self._db.setdefault("config_history", []).append({
                "id": rid, "revision": rev, "key": k, "group_key": gk,
                "old_value": ov, "new_value": nv, "old_is_encrypted": oenc,
                "new_is_encrypted": nenc, "changed_by": cb, "changed_at": datetime.datetime.now(),
            })
            return _FakeResult([])
        if "INSERT INTO config_revision" in q:
            rid, rev, snap, changed_by, note = params
            self._db.setdefault("config_revision", []).append({
                "id": rid, "revision": rev, "snapshot_json": snap,
                "changed_by": changed_by, "note": note, "changed_at": None,
            })
            return _FakeResult([])
        return _FakeResult([])

    async def executemany(self, query, params_seq):
        if "INSERT INTO config_history" in query:
            for p in params_seq:
                self._db.setdefault("config_history", []).append({
                    "id": p[0], "revision": p[1], "key": p[2], "group_key": p[3],
                    "old_value": p[4], "new_value": p[5], "old_is_encrypted": p[6],
                    "new_is_encrypted": p[7], "changed_by": p[8], "changed_at": datetime.datetime.now(),
                })
        return _FakeResult([])


class _FakePool:
    def __init__(self):
        self._db = {}

    def connection(self):
        return _ConnCtx(_FakeConn(self._db))


class _FakeRedis:
    def __init__(self):
        self._d = {}

    async def get(self, k):
        return self._d.get(k)

    async def set(self, k, v):
        self._d[k] = v

    async def incr(self, k):
        self._d[k] = int(self._d.get(k, 0)) + 1
        return self._d[k]

    def ping(self):
        return True


class _FakeActor:
    role = "owner"
    user_id = "u_test"
    workspace_id = "ws_test"


@pytest.fixture
def env():
    pool = _FakePool()
    redis = _FakeRedis()
    # 用轻量命名空间承载 settings，避免污染真实模块
    settings = type("S", (), {})()
    settings.rate_limit_default_per_min = 60
    settings.smtp_host = ""
    settings.trusted_origins = ["http://localhost:20204"]
    settings.workama_env = "development"
    settings.encryption_key = "QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI="

    orig_pool = cc.pool
    orig_redis = cc.redis
    orig_settings = cc.settings
    cc.pool = pool
    cc.redis = redis
    cc.settings = settings
    cc._LOCAL.update(version=-1, ts=0.0, snapshot={})
    yield {"pool": pool, "redis": redis, "settings": settings}
    cc.pool = orig_pool
    cc.redis = orig_redis
    cc.settings = orig_settings
    cc._LOCAL.update(version=-1, ts=0.0, snapshot={})


def test_validate_value_ok_and_errors():
    f = cc._SCHEMA_BY_KEY["rate_limit_default_per_min"]
    validate_value(f, 123)
    validate_value(f, "123")
    with pytest.raises(ValueError):
        validate_value(f, 0)  # < min 1
    with pytest.raises(ValueError):
        validate_value(f, "abc")  # not int
    ef = cc._SCHEMA_BY_KEY["workama_env"]
    validate_value(ef, "production")
    with pytest.raises(ValueError):
        validate_value(ef, "nope")
    em = cc._SCHEMA_BY_KEY["smtp_from"]
    validate_value(em, "a@b.com")
    with pytest.raises(ValueError):
        validate_value(em, "not-an-email")
    urlf = cc._SCHEMA_BY_KEY["gateway_url"]
    validate_value(urlf, "http://gateway:8080")
    with pytest.raises(ValueError):
        validate_value(urlf, "gateway:8080")
    # KEEP 哨兵对必填项放行（视为不修改）
    with pytest.raises(ValueError):
        validate_value(cc._SCHEMA_BY_KEY["database_url"], None)
    validate_value(cc._SCHEMA_BY_KEY["database_url"], cc.KEEP_SENTINEL)


def test_coerce_roundtrip():
    f = cc._SCHEMA_BY_KEY["rate_limit_default_per_min"]
    assert _coerce_in(f, 5) == "5"
    assert _coerce_out(f, "5") == 5
    bf = cc._SCHEMA_BY_KEY["smtp_mock"]
    assert _coerce_in(bf, True) == "true"
    assert _coerce_out(bf, "true") is True
    lf = cc._SCHEMA_BY_KEY["trusted_origins"]
    assert _coerce_in(lf, ["a", "b"]) == "a,b"
    assert _coerce_out(lf, "a,b") == ["a", "b"]
    assert _coerce_out(lf, "") == []


def test_parse_host_port():
    f = cc._SCHEMA_BY_KEY["gateway_url"]
    assert _parse_host_port(f, "http://gateway:8080") == ("gateway", 8080)
    mf = cc._SCHEMA_BY_KEY["minio_endpoint"]
    assert _parse_host_port(mf, "minio:9000") == ("minio", 9000)
    sf = cc._SCHEMA_BY_KEY["redis_sentinels"]
    assert _parse_host_port(sf, "10.0.0.1:26379,10.0.0.2:26379") == ("10.0.0.1", 26379)
    assert _parse_host_port(f, "http://no-port") == ("no-port", 0)


def test_tcp_probe(monkeypatch):
    import contextlib

    @contextlib.contextmanager
    def ok_cm(*_a, **_k):
        yield None

    def fail_cm(*_a, **_k):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(socket, "create_connection", ok_cm)
    assert _tcp_probe("h", 80)[0] is True
    monkeypatch.setattr(socket, "create_connection", fail_cm)
    ok2, detail = _tcp_probe("h", 80)
    assert ok2 is False and "无法连接" in detail
    assert _tcp_probe("h", 0)[0] is False


async def test_resolution_priority_db_over_env(env):
    # DB 行优先
    env["pool"]._db["config_settings"] = [{
        "key": "rate_limit_default_per_min", "group_key": "auth",
        "value": "123", "value_type": "int", "is_secret": False, "is_encrypted": False,
    }]
    eff = await cc.get_effective_config(force=True)
    assert eff["rate_limit_default_per_min"]["value"] == 123
    assert eff["rate_limit_default_per_min"]["source"] == "db"

    # 无 DB 行时回退 ENV
    env["pool"]._db["config_settings"] = []
    env["settings"].rate_limit_default_per_min = 77
    eff = await cc.get_effective_config(force=True)
    assert eff["rate_limit_default_per_min"]["value"] == 77
    assert eff["rate_limit_default_per_min"]["source"] == "env"

    # 无 DB 无 ENV 回退默认
    env["settings"].rate_limit_default_per_min = cc._SCHEMA_BY_KEY[
        "rate_limit_default_per_min"].default
    eff = await cc.get_effective_config(force=True)
    assert eff["rate_limit_default_per_min"]["source"] == "default"


async def test_apply_overrides_to_settings(env):
    env["pool"]._db["config_settings"] = [{
        "key": "rate_limit_default_per_min", "group_key": "auth",
        "value": "222", "value_type": "int", "is_secret": False, "is_encrypted": False,
    }, {
        "key": "trusted_origins", "group_key": "auth",
        "value": "a.example,b.example", "value_type": "list",
        "is_secret": False, "is_encrypted": False,
    }]
    await cc.load_and_apply_config_overrides()
    assert env["settings"].rate_limit_default_per_min == 222
    assert env["settings"].trusted_origins == ["a.example", "b.example"]


async def test_put_get_rollback_flow(env):
    actor = _FakeActor()
    # 1) 更新限流 + 设置一个密钥
    body = ConfigUpdate(items=[
        ConfigItem(key="rate_limit_default_per_min", value=199),
        ConfigItem(key="smtp_password", value="super-secret"),
        ConfigItem(key="smtp_mock", value=False),
    ], note="initial")
    res = await cc.put_values(body, actor)
    assert res["revision"] == 1
    assert res["version"] == 1
    # 密钥不应以明文出现在返回值中
    assert res["values"]["smtp_password"]["value"] == cc.KEEP_SENTINEL
    assert res["values"]["smtp_password"]["secret_set"] is True
    # 热生效：settings 单例被覆盖
    assert env["settings"].rate_limit_default_per_min == 199
    assert env["settings"].smtp_mock is False

    # 2) 再改一次
    await cc.put_values(
        ConfigUpdate(items=[ConfigItem(key="rate_limit_default_per_min", value=250)]),
        actor,
    )

    # 3) 历史与 revision 记录
    hist = await cc.get_history(key="rate_limit_default_per_min", actor=_FakeActor())
    assert hist["items"][0]["revision"] == 2
    revs = await cc.get_revisions(actor=_FakeActor())
    assert len(revs["items"]) == 2

    # 4) 回滚到 rev 1（限流应回到 199）
    rb = await cc.rollback(ConfigRollback(revision=1, note="rollback-test"), actor)
    assert rb["revision"] == 3
    assert rb["values"]["rate_limit_default_per_min"]["value"] == 199
    # 回滚后 settings 也更新
    assert env["settings"].rate_limit_default_per_min == 199

    # 5) 密钥回滚后仍加密落库，不被明文泄露
    hist2 = await cc.get_history(key="smtp_password", actor=_FakeActor())
    assert hist2["items"][0]["new_value"] == cc.KEEP_SENTINEL


async def test_keep_sentinel_keeps_secret(env):
    actor = _FakeActor()
    await cc.put_values(
        ConfigUpdate(items=[ConfigItem(key="smtp_password", value="first-secret")]), actor
    )
    # 后续更新不传密钥明文，用哨兵保持
    await cc.put_values(
        ConfigUpdate(items=[ConfigItem(key="smtp_password", value=cc.KEEP_SENTINEL),
                            ConfigItem(key="rate_limit_default_per_min", value=5)]), actor
    )
    # 密钥仍为第一次设置的值（保持），未被哨兵覆盖
    eff = await cc.get_effective_config(force=True)
    assert eff["smtp_password"]["secret_set"] is True
    # 校验落库明文确为 first-secret（通过解密）
    rows = env["pool"]._db["config_settings"]
    smtp_row = next(r for r in rows if r["key"] == "smtp_password")
    from workama_platform.core import decrypt_secret
    assert decrypt_secret(smtp_row["value"]) == "first-secret"


async def test_delete_override_restores_fallback(env):
    """删除 UI 覆盖：键回落 ENV/默认，settings 同步恢复，历史记录删除事件。"""
    actor = _FakeActor()
    await cc.put_values(
        ConfigUpdate(items=[ConfigItem(key="rate_limit_default_per_min", value=300)]), actor
    )
    assert env["settings"].rate_limit_default_per_min == 300
    res = await cc.delete_value("rate_limit_default_per_min", actor)
    assert res["deleted"] is True
    assert res["source"] in {"env", "default"}
    # settings 恢复到 ENV 值（fixture 里为 60）
    assert env["settings"].rate_limit_default_per_min == 60
    # 历史含一条 new_value 为 NULL 的删除事件
    hist = await cc.get_history(key="rate_limit_default_per_min", actor=actor)
    deletes = [h for h in hist["items"] if h["new_value"] is None]
    assert len(deletes) >= 1
    # 幂等：再删一次返回 deleted=False
    res2 = await cc.delete_value("rate_limit_default_per_min", actor)
    assert res2["deleted"] is False


async def test_delete_secret_masks_history(env):
    actor = _FakeActor()
    await cc.put_values(
        ConfigUpdate(items=[ConfigItem(key="smtp_password", value="top-secret")]), actor
    )
    res = await cc.delete_value("smtp_password", actor)
    assert res["deleted"] is True and res["value"] is None
    hist = await cc.get_history(key="smtp_password", actor=actor)
    latest = hist["items"][0]
    assert latest["new_value"] is None
    assert latest["old_value"] == cc.KEEP_SENTINEL  # 密钥明文永不入 API 视图


async def test_schema_catalog_complete(env):
    schema = await cc.get_schema(_FakeActor())
    keys = {f["key"] for g in schema["groups"] for f in g["fields"]}
    assert "database_url" in keys and "rate_limit_default_per_min" in keys
    assert "smtp_password" in keys
    # 每个分组都有 label
    assert all(g["label"] for g in schema["groups"])


async def test_watcher_converges_on_version_change(env):
    """跨进程热收敛：外部 bump 版本号后，watcher 在一个周期内把新值应用到本进程。"""
    import asyncio

    def seed(value: str) -> None:
        env["pool"]._db["config_settings"] = [{
            "key": "rate_limit_default_per_min", "group_key": "auth",
            "value": value, "value_type": "int", "is_secret": False, "is_encrypted": False,
        }]

    seed("555")
    task = asyncio.create_task(cc.config_watcher_loop(interval=0.01))
    try:
        await asyncio.sleep(0.05)
        assert env["settings"].rate_limit_default_per_min == 555
        # 模拟其它进程发布：改库 + bump Redis 版本号
        seed("777")
        await env["redis"].incr(cc.REDIS_VERSION_KEY)
        await asyncio.sleep(0.1)
        assert env["settings"].rate_limit_default_per_min == 777
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_export_runtime_includes_encrypted_secret_only(env):
    """内部导出：非密钥明文 + 密钥仅密文（明文永不出现，可解密回原值）。"""
    actor = _FakeActor()
    await cc.put_values(ConfigUpdate(items=[
        ConfigItem(key="llm_staging_enabled", value=True),
        ConfigItem(key="llm_staging_provider", value="openai-compatible"),
        ConfigItem(key="llm_staging_base_url", value="http://mock-llm:9101/v1"),
        ConfigItem(key="llm_staging_api_key", value="sk-test-abc123"),
        ConfigItem(key="llm_staging_model", value="mock-upstream-model"),
    ]), actor)
    out = await cc.export_runtime(actor)
    assert out["values"]["llm_staging_enabled"] is True
    assert out["values"]["llm_staging_provider"] == "openai-compatible"
    cipher = out["secrets"].get("llm_staging_api_key")
    assert cipher, "密钥字段应以密文出现在 secrets 中"
    dumped = json.dumps(out)
    assert "sk-test-abc123" not in dumped, "明文 API Key 绝不允许出现在导出视图"
    from workama_platform.core import decrypt_secret
    assert decrypt_secret(cipher) == "sk-test-abc123"
    # 未设置的密钥（如 smtp_password）不应出现
    assert "smtp_password" not in out["secrets"]


async def test_watcher_survives_redis_and_db_errors(env):
    """Redis/DB 故障时 watcher 只跳过本轮，不抛出、不退出。"""
    import asyncio

    orig_get_version = cc._redis_get_version
    task = asyncio.create_task(cc.config_watcher_loop(interval=0.01))
    try:
        await asyncio.sleep(0.03)

        async def boom(*_a, **_k):
            raise RuntimeError("redis down")

        cc._redis_get_version = boom  # type: ignore[method-assign]
        await asyncio.sleep(0.05)  # 若异常逃逸，task 会 done 并携带异常
        assert not task.done() or task.exception() is None
    finally:
        cc._redis_get_version = orig_get_version  # type: ignore[method-assign]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
