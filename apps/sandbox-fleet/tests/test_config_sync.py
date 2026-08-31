"""sandbox-fleet 配置中心热同步单元测试。

覆盖：
- RuntimeSettings：ENV 基线快照、apply_overrides 优先级语义
  （DB(UI) > ENV > 默认；删除覆盖即回落基线）、类型收敛、脏值跳过
- build_sandbox_applier：非 sandbox 键过滤
- ConfigSyncPoller.fetch_once：overrides 视图解析 / 版本归一 /
  旧版兼容（无 overrides 视图视为空集）/ 401/5xx/网络故障返回 None

全部使用 fake httpx transport，可在容器内无外设运行。
"""
from __future__ import annotations

import unittest

import httpx

from workama_sandbox.config_sync import ConfigSyncPoller, build_sandbox_applier
from workama_sandbox.main import RuntimeSettings, Settings


def _runtime(**env_overrides) -> RuntimeSettings:
    base = Settings(
        platform_api_url="http://platform-api:8000",
        **env_overrides,
    )
    return RuntimeSettings(base)


class RuntimeSettingsBaselineTests(unittest.TestCase):
    def test_baseline_captures_env_values(self):
        rt = _runtime(sandbox_prewarm_size=7, sandbox_max_total=123)
        self.assertEqual(rt.sandbox_prewarm_size, 7)
        self.assertEqual(rt.sandbox_max_total, 123)
        # 启动期基础设施属性保持 ENV 注入值（重启生效边界）
        self.assertEqual(rt.platform_api_url, "http://platform-api:8000")

    def test_apply_overrides_then_delete_falls_back_to_baseline(self):
        rt = _runtime()
        changed = rt.apply_overrides({"sandbox_idle_seconds": 120, "sandbox_ttl_seconds": 999})
        self.assertEqual(changed, ["sandbox_idle_seconds", "sandbox_ttl_seconds"])
        self.assertEqual(rt.sandbox_idle_seconds, 120)
        # 删除覆盖（键从 overrides 消失）→ 回落 ENV 基线，不残留上一轮值
        changed2 = rt.apply_overrides({"sandbox_ttl_seconds": 888})
        self.assertIn("sandbox_idle_seconds", changed2)  # 回落也是变更
        self.assertEqual(rt.sandbox_idle_seconds, rt._baseline["sandbox_idle_seconds"])
        self.assertEqual(rt.sandbox_ttl_seconds, 888)

    def test_empty_overrides_resets_everything_to_baseline(self):
        rt = _runtime()
        rt.apply_overrides({"sandbox_max_total": 999, "sandbox_memory": "8g"})
        changed = rt.apply_overrides({})
        self.assertEqual(sorted(changed), ["sandbox_max_total", "sandbox_memory"])
        self.assertEqual(rt.sandbox_max_total, rt._baseline["sandbox_max_total"])
        self.assertEqual(rt.sandbox_memory, rt._baseline["sandbox_memory"])

    def test_type_coercion_int_bool_str_and_dirty_skip(self):
        rt = _runtime()
        changed = rt.apply_overrides({
            "sandbox_prewarm_size": "5",        # str → int 收敛
            "sandbox_require_gvisor": "false",  # str → bool 收敛
            "sandbox_nano_cpus": 3_000_000_000, # int 直通
            "sandbox_memory": 8192,             # 非 str → str 收敛
        })
        self.assertEqual(rt.sandbox_prewarm_size, 5)
        self.assertFalse(rt.sandbox_require_gvisor)
        self.assertEqual(rt.sandbox_nano_cpus, 3_000_000_000)
        self.assertEqual(rt.sandbox_memory, "8192")
        # 脏值：int 键给不可收敛字符串 → 跳过并保持当前值
        changed2 = rt.apply_overrides({"sandbox_ttl_seconds": "not-a-number"})
        self.assertNotIn("sandbox_ttl_seconds", changed2)
        self.assertEqual(rt.sandbox_ttl_seconds, rt._baseline["sandbox_ttl_seconds"])

    def test_non_hot_keys_are_ignored(self):
        rt = _runtime()
        before = dict(rt._data)
        changed = rt.apply_overrides({
            "database_url": "postgresql://evil:evil@evil:5432/evil",
            "internal_token": "attacker-token",
            "llm_staging_enabled": True,
            "unknown_key": "x",
        })
        self.assertEqual(changed, [])
        self.assertEqual(dict(rt._data), before)
        self.assertNotEqual(rt.internal_token, "attacker-token")


class ApplierFilterTests(unittest.TestCase):
    def test_build_sandbox_applier_filters_foreign_keys(self):
        rt = _runtime()
        apply = build_sandbox_applier(rt)
        changed = apply({"sandbox_max_per_workspace": 33, "smtp_host": "smtp.evil.com"})
        self.assertEqual(changed, ["sandbox_max_per_workspace"])
        self.assertEqual(rt.sandbox_max_per_workspace, 33)


def _transport(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=2.0)


class PollerFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_parses_overrides_and_version(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/internal/config/export")
            self.assertEqual(request.headers.get("X-Internal-Token"), "tok-1")
            return httpx.Response(200, json={
                "version": 42,
                "values": {"sandbox_max_total": 88, "rate_limit_default_per_min": 60},
                "secrets": {"jwt_secret": "cipher"},
                "overrides": {"sandbox_max_total": 88},
            })

        poller = ConfigSyncPoller(base_url="http://platform-api:8000/", token="tok-1",
                                  applier=lambda ov: [], client=_transport(handler))
        snap = await poller.fetch_once()
        assert snap is not None
        self.assertEqual(snap["version"], 42)
        self.assertEqual(snap["overrides"], {"sandbox_max_total": 88})

    async def test_fetch_legacy_export_without_overrides_is_empty_set(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"version": 7, "values": {}, "secrets": {}})

        poller = ConfigSyncPoller(base_url="http://platform-api:8000", token="t",
                                  applier=lambda ov: [], client=_transport(handler))
        snap = await poller.fetch_once()
        assert snap is not None
        self.assertEqual(snap["version"], 7)
        self.assertEqual(snap["overrides"], {})

    async def test_fetch_failures_return_none(self):
        for status in (401, 500, 503):
            poller = ConfigSyncPoller(base_url="http://platform-api:8000", token="bad",
                                      applier=lambda ov: [],
                                      client=_transport(lambda req, s=status: httpx.Response(s)))
            self.assertIsNone(await poller.fetch_once(), f"http {status} 应返回 None")

    async def test_fetch_network_error_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        poller = ConfigSyncPoller(base_url="http://platform-api:8000", token="t",
                                  applier=lambda ov: [], client=_transport(handler))
        self.assertIsNone(await poller.fetch_once())

    async def test_version_string_normalized_to_int(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"version": "9", "values": {},
                                             "secrets": {}, "overrides": {}})

        poller = ConfigSyncPoller(base_url="http://platform-api:8000", token="t",
                                  applier=lambda ov: [], client=_transport(handler))
        snap = await poller.fetch_once()
        assert snap is not None
        self.assertEqual(snap["version"], 9)


if __name__ == "__main__":
    unittest.main()
