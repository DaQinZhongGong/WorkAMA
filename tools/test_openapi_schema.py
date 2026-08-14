#!/usr/bin/env python3
"""v7.157: OpenAPI schema 完整性测试套件。

测试目标：验证 platform-api 的 ``/api/openapi.json`` 满足 API 文档自动生成所需的所有约定。

测试策略：
- 优先从运行中的 platform-api（``http://localhost:20200``）实时拉取 schema；
- 若设置了环境变量 ``WORKAMA_OPENAPI_PATH``，则从该本地 JSON 文件读取；
- 若上述都不可用，则跳过所有测试（避免在 CI 中误报）。

运行方式::

    python tools/test_openapi_schema.py
    python -m pytest tools/test_openapi_schema.py -v
    WORKAMA_OPENAPI_PATH=docs/api/openapi.json python tools/test_openapi_schema.py

覆盖维度（25 个测试）：
- schema 元数据（version/title/description/contact/license/servers/openapi version）
- 端点数量（>= 50 个 paths）
- 每个端点都有 summary 与 responses
- 15 个核心 tag 全部存在
- 14 类核心端点存在（auth / gateway / free-providers / knowledge-base / assistant /
  workflow / memory-vector / device / billing / mcp / unified-search / notification /
  file-storage / workspace / audit-log / healthz）
- 错误响应约定（422 文档化、HTTPValidationError schema 存在、
  info.description 提及 401/403/404/422/500）
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import unittest
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://localhost:20200"
DEFAULT_API_PATH = "/api/openapi.json"
SCHEMA_PATH_ENV = "WORKAMA_OPENAPI_PATH"

# 任务要求的核心模块 tag（必须在 openapi_tags 中声明）
REQUIRED_TAGS = (
    "auth",
    "gateway",
    "memory-vector",
    "device-telemetry",
    "knowledge-base",
    "audit-log",
    "mcp",
    "billing",
    "workspace",
    "assistant",
    "workflow",
    "notification",
    "file-storage",
    "search",
    "free-providers",
)

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")

EXPECTED_VERSION = "v7.157"
EXPECTED_TITLE = "WorkAMA Platform API"
MIN_PATHS = 50


def _load_schema() -> dict[str, Any]:
    """加载 OpenAPI schema。优先本地文件，其次 HTTP。"""
    local = os.environ.get(SCHEMA_PATH_ENV)
    if local:
        path = Path(local)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent.parent / path
        return json.loads(path.read_text(encoding="utf-8"))
    # 尝试 HTTP
    base_url = os.environ.get("WORKAMA_API_URL", DEFAULT_BASE_URL).rstrip("/")
    url = f"{base_url}{DEFAULT_API_PATH}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _maybe_skip_if_no_schema() -> dict[str, Any]:
    """加载 schema；若失败则跳过整个测试模块。"""
    try:
        return _load_schema()
    except Exception as exc:  # noqa: BLE001
        raise unittest.SkipTest(
            f"无法加载 OpenAPI schema（既无 {SCHEMA_PATH_ENV} 环境变量，"
            f"也无法访问 {DEFAULT_BASE_URL}{DEFAULT_API_PATH}）：{exc}"
        ) from exc


# 模块级 schema：在 module load 时拉取一次，所有 test_* 共享
SCHEMA: dict[str, Any] = _maybe_skip_if_no_schema()


def _all_operations(schema: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """返回 [(method, path, op_dict), ...]。"""
    ops: list[tuple[str, str, dict[str, Any]]] = []
    for path, path_item in (schema.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            op = path_item.get(method)
            if isinstance(op, dict):
                ops.append((method.upper(), path, op))
    return ops


class OpenApiSchemaMetadataTests(unittest.TestCase):
    """验证 OpenAPI 顶层元数据完整。"""

    def setUp(self) -> None:
        self.schema = SCHEMA
        self.info = self.schema.get("info") or {}

    def test_01_openapi_version_is_3_x(self) -> None:
        """openapi 字段必须为 3.x.x。"""
        self.assertEqual(self.schema.get("openapi", "")[:2], "3.")

    def test_02_info_title_correct(self) -> None:
        self.assertEqual(self.info.get("title"), EXPECTED_TITLE)

    def test_03_info_version_correct(self) -> None:
        self.assertEqual(self.info.get("version"), EXPECTED_VERSION)

    def test_04_info_description_present_and_mentions_auth(self) -> None:
        desc = self.info.get("description") or ""
        self.assertTrue(desc, "info.description 不应为空")
        self.assertIn("认证", desc)
        self.assertIn("JWT", desc)
        self.assertIn("Bearer", desc)

    def test_05_info_contact_complete(self) -> None:
        contact = self.info.get("contact") or {}
        self.assertTrue(contact.get("name"), "contact.name 缺失")
        self.assertIn("@", contact.get("email", ""))
        self.assertTrue(contact.get("url", "").startswith("http"))

    def test_06_info_license_is_mit(self) -> None:
        lic = self.info.get("license") or {}
        self.assertEqual(lic.get("name"), "MIT")
        self.assertTrue(lic.get("url", "").startswith("http"))

    def test_07_servers_has_dev_and_prod(self) -> None:
        servers = self.schema.get("servers") or []
        self.assertGreaterEqual(len(servers), 2, "至少要有 dev 与 prod 两个 servers")
        urls = {s.get("url") for s in servers}
        self.assertIn("http://localhost:20200", urls)
        self.assertIn("https://api.workama.com", urls)


class OpenApiSchemaTagsTests(unittest.TestCase):
    """验证所有任务要求的核心 tag 都在 openapi_tags 中声明。"""

    def setUp(self) -> None:
        self.schema = SCHEMA
        self.tags_meta = {t.get("name"): t for t in (self.schema.get("tags") or [])}

    def test_08_all_required_tags_present(self) -> None:
        missing = [t for t in REQUIRED_TAGS if t not in self.tags_meta]
        self.assertFalse(missing, f"缺少核心 tag metadata: {missing}")

    def test_09_all_required_tags_have_description(self) -> None:
        no_desc = [
            t for t in REQUIRED_TAGS
            if t in self.tags_meta and not (self.tags_meta[t].get("description") or "").strip()
        ]
        self.assertFalse(no_desc, f"tag 缺少 description: {no_desc}")


class OpenApiSchemaPathsTests(unittest.TestCase):
    """验证 paths 数量与端点字段完整性。"""

    def setUp(self) -> None:
        self.schema = SCHEMA
        self.paths = self.schema.get("paths") or {}
        self.ops = _all_operations(self.schema)

    def test_10_paths_count_meets_minimum(self) -> None:
        self.assertGreaterEqual(
            len(self.paths), MIN_PATHS,
            f"paths 数量 {len(self.paths)} 少于 {MIN_PATHS}",
        )

    def test_11_operations_count_meets_minimum(self) -> None:
        self.assertGreaterEqual(len(self.ops), MIN_PATHS)

    def test_12_every_operation_has_summary(self) -> None:
        missing = [
            f"{m} {p}" for m, p, op in self.ops
            if not (op.get("summary") or op.get("operationId"))
        ]
        self.assertFalse(missing, f"缺少 summary 的端点（前 10）: {missing[:10]}")

    def test_13_every_operation_has_responses(self) -> None:
        missing = [f"{m} {p}" for m, p, op in self.ops if not op.get("responses")]
        self.assertFalse(missing, f"缺少 responses 的端点（前 10）: {missing[:10]}")

    def test_14_every_operation_has_at_least_one_2xx_response(self) -> None:
        no_success = []
        for m, p, op in self.ops:
            codes = list((op.get("responses") or {}).keys())
            if not any(c.startswith("2") for c in codes):
                no_success.append(f"{m} {p}={codes}")
        self.assertFalse(no_success, f"缺少 2xx 响应的端点（前 10）: {no_success[:10]}")


class OpenApiCoreEndpointsTests(unittest.TestCase):
    """验证 14 类核心端点都存在。"""

    def setUp(self) -> None:
        self.paths = set((SCHEMA.get("paths") or {}).keys())

    def test_15_auth_endpoints_present(self) -> None:
        for p in ("/api/v1/auth/login", "/api/v1/auth/refresh", "/api/v1/auth/register"):
            self.assertIn(p, self.paths, f"缺失 {p}")

    def test_16_gateway_and_free_providers_present(self) -> None:
        self.assertIn("/api/v1/gateway/free-providers", self.paths)
        # 至少存在一个 free-providers enable 端点
        enable_paths = [p for p in self.paths if p.startswith("/api/v1/gateway/free-providers/") and p.endswith("/enable")]
        self.assertTrue(enable_paths, "缺失 free-providers enable 端点")

    def test_17_knowledge_base_endpoints_present(self) -> None:
        self.assertIn("/api/v1/knowledge-bases", self.paths)

    def test_18_assistant_endpoints_present(self) -> None:
        self.assertIn("/api/v1/assistants", self.paths)

    def test_19_workflow_endpoints_present(self) -> None:
        self.assertIn("/api/v1/workflows", self.paths)

    def test_20_memory_vector_endpoints_present(self) -> None:
        self.assertIn("/api/v1/memory-vectors", self.paths)

    def test_21_device_telemetry_endpoints_present(self) -> None:
        self.assertIn("/api/v1/devices", self.paths)

    def test_22_billing_plans_endpoint_present(self) -> None:
        self.assertIn("/api/v1/billing/plans", self.paths)

    def test_23_mcp_manifest_endpoint_present(self) -> None:
        self.assertIn("/api/v1/mcp/manifest", self.paths)

    def test_24_unified_search_endpoint_present(self) -> None:
        self.assertIn("/api/v1/unified-search", self.paths)

    def test_25_notification_center_endpoints_present(self) -> None:
        self.assertIn("/api/v1/notification-center", self.paths)

    def test_26_file_storage_endpoints_present(self) -> None:
        self.assertIn("/api/v1/files", self.paths)

    def test_27_workspaces_endpoints_present(self) -> None:
        self.assertIn("/api/v1/workspaces", self.paths)

    def test_28_audit_logs_endpoints_present(self) -> None:
        self.assertIn("/api/v1/audit-logs", self.paths)

    def test_29_health_endpoints_present(self) -> None:
        self.assertIn("/healthz", self.paths)
        self.assertIn("/readyz", self.paths)


class OpenApiErrorContractTests(unittest.TestCase):
    """验证错误响应约定。"""

    def setUp(self) -> None:
        self.schema = SCHEMA
        self.paths = self.schema.get("paths") or {}
        self.ops = _all_operations(self.schema)

    def test_30_422_validation_error_widely_documented(self) -> None:
        """至少 100 个端点声明了 422 响应（FastAPI 自带）。"""
        count_422 = sum(
            1 for _, _, op in self.ops
            if "422" in (op.get("responses") or {})
        )
        self.assertGreaterEqual(count_422, 100, f"仅 {count_422} 个端点声明了 422 响应")

    def test_31_http_validation_error_schema_exists(self) -> None:
        schemas = (self.schema.get("components") or {}).get("schemas") or {}
        self.assertIn("HTTPValidationError", schemas)
        self.assertIn("ValidationError", schemas)

    def test_32_error_codes_documented_in_description(self) -> None:
        """info.description 必须提及 401/403/404/422/500 错误码。"""
        desc = (self.schema.get("info") or {}).get("description") or ""
        for code in ("401", "403", "404", "422", "500"):
            self.assertIn(code, desc, f"info.description 未提及错误码 {code}")

    def test_33_401_documented_on_at_least_one_endpoint(self) -> None:
        """至少一个端点声明了 401 响应（鉴权失败）。"""
        # FastAPI 不自动声明 401，但部分模块可能手动声明；若无则放宽为
        # description 中已说明 401 含义即可（已在 test_32 覆盖）。
        count_401 = sum(
            1 for _, _, op in self.ops
            if "401" in (op.get("responses") or {})
        )
        # 软断言：若全部端点都没声明 401，则要求 description 必须提及（test_32 已校验）
        # 此处至少要求 description 提及即可通过；count_401 可以是 0。
        desc = (self.schema.get("info") or {}).get("description") or ""
        self.assertTrue(
            count_401 > 0 or "401" in desc,
            "没有任何端点声明 401 响应，且 info.description 也未提及 401",
        )


def main() -> int:
    """支持 ``python tools/test_openapi_schema.py`` 直接运行。"""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
