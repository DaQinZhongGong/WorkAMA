from pathlib import Path
import json
import tempfile
import unittest

from tools import contract_registry_check
from tools.runtime_surface_sync import build_surface


class ContractRegistryTests(unittest.TestCase):
    def _root(self) -> Path:
        directory = Path(tempfile.mkdtemp())
        (directory / "WorkAMA-Docs").mkdir()
        (directory / "apps/platform-api/src").mkdir(parents=True)
        (directory / "api").mkdir()
        return directory

    def test_normalize_path_replaces_parameter_names_and_prefixes(self):
        self.assertEqual(
            contract_registry_check.normalize_path("/sessions/{session_id}/events", base="/api/v1"),
            "/api/v1/sessions/{}/events",
        )
        self.assertEqual(contract_registry_check.normalize_path("/health/"), "/health")

    def test_audit_reports_source_drift_without_blocking_default_mode(self):
        root = self._root()
        (root / "WorkAMA-Docs/720-实施级API操作与消息契约注册表.md").write_text(
            "| listThings | `GET /api/v1/things` | P0 |\n"
            "| createThing | `POST /api/v1/things` | P0 |\n",
            encoding="utf-8",
        )
        (root / "WorkAMA-Docs/610-数据库物理模型与数据生命周期设计.md").write_text(
            "`id_user`、`pf_dataset`。\n", encoding="utf-8"
        )
        (root / "apps/platform-api/src/module.py").write_text(
            "from fastapi import APIRouter\n"
            "router = APIRouter(prefix='/api/v1')\n"
            "@router.get('/things')\n"
            "async def list_things():\n"
            "    return {}\n"
            "@router.delete('/unregistered/{thing_id}')\n"
            "async def delete_thing():\n"
            "    return {}\n"
            "SQL = 'CREATE TABLE IF NOT EXISTS id_user (id text)'\n"
            "SQL2 = 'CREATE TABLE IF NOT EXISTS pf_runtime (id text)'\n",
            encoding="utf-8",
        )
        (root / "api/openapi.yaml").write_text(
            "servers:\n  - url: http://localhost:20200/api/v1\n"
            "paths:\n  /things:\n    get:\n      operationId: listThings\n",
            encoding="utf-8",
        )

        report = contract_registry_check.audit(root, expected_operation_count=2)

        self.assertTrue(report["ok"], report["findings"])
        self.assertEqual(report["registry"]["operation_count"], 2)
        self.assertIn("DELETE /api/v1/unregistered/{}", report["implementation"]["unregistered_routes"])
        self.assertEqual(report["implementation"]["unregistered_tables"], ["pf_runtime"])
        self.assertGreater(report["warning_count"], 0)

    def test_strict_modes_turn_drift_into_blocking_findings(self):
        root = self._root()
        (root / "WorkAMA-Docs/720-实施级API操作与消息契约注册表.md").write_text(
            "| listThings | `GET /api/v1/things` | P0 |\n", encoding="utf-8"
        )
        (root / "WorkAMA-Docs/610-数据库物理模型与数据生命周期设计.md").write_text(
            "`id_user`。\n", encoding="utf-8"
        )
        (root / "apps/platform-api/src/module.py").write_text(
            "from fastapi import APIRouter\n"
            "router = APIRouter(prefix='/api/v1')\n"
            "@router.post('/unregistered')\n"
            "async def create_thing():\n"
            "    return {}\n"
            "SQL = 'CREATE TABLE IF NOT EXISTS pf_runtime (id text)'\n",
            encoding="utf-8",
        )
        (root / "api/openapi.yaml").write_text(
            "paths:\n  /things:\n    get:\n      operationId: unknownThings\n", encoding="utf-8"
        )

        report = contract_registry_check.audit(
            root, expected_operation_count=1, strict_source=True, strict_openapi=True
        )

        self.assertFalse(report["ok"])
        self.assertIn("source.route_unregistered", {item["code"] for item in report["findings"]})
        self.assertIn("source.table_unregistered", {item["code"] for item in report["findings"]})
        self.assertIn("openapi.operation_unregistered", {item["code"] for item in report["findings"]})

    def test_registry_duplicates_are_always_blocking(self):
        root = self._root()
        (root / "WorkAMA-Docs/720-实施级API操作与消息契约注册表.md").write_text(
            "| listThings | `GET /api/v1/things` | P0 |\n"
            "| listThingsAgain | `GET /api/v1/things` | P0 |\n",
            encoding="utf-8",
        )
        (root / "WorkAMA-Docs/610-数据库物理模型与数据生命周期设计.md").write_text("`id_user`。\n", encoding="utf-8")
        (root / "apps/platform-api/src/module.py").write_text("", encoding="utf-8")
        (root / "api/openapi.yaml").write_text("paths:\n", encoding="utf-8")

        report = contract_registry_check.audit(root, expected_operation_count=2)

        self.assertFalse(report["ok"])
        self.assertIn("registry.duplicate_method_path", {item["code"] for item in report["findings"]})

    def test_runtime_surface_snapshot_is_checked_independently_from_design_registry(self):
        root = self._root()
        (root / "WorkAMA-Docs/720-实施级API操作与消息契约注册表.md").write_text(
            "| listThings | `GET /api/v1/things` | P0 |\n", encoding="utf-8"
        )
        (root / "WorkAMA-Docs/610-数据库物理模型与数据生命周期设计.md").write_text(
            "`id_user`。\n", encoding="utf-8"
        )
        (root / "apps/platform-api/src/module.py").write_text(
            "from fastapi import APIRouter\n"
            "router = APIRouter(prefix='/api/v1')\n"
            "@router.get('/things')\n"
            "async def list_things():\n"
            "    return {}\n"
            "SQL = 'CREATE TABLE IF NOT EXISTS id_user (id text)'\n",
            encoding="utf-8",
        )
        (root / "api/openapi.yaml").write_text(
            "paths:\n  /things:\n    get:\n      operationId: listThings\n", encoding="utf-8"
        )
        (root / "api/runtime-surface.json").write_text(json.dumps(build_surface(root), indent=2), encoding="utf-8")

        report = contract_registry_check.audit(root, expected_operation_count=1, strict_runtime=True)

        self.assertTrue(report["ok"], report["findings"])
        self.assertEqual(report["runtime_surface"]["route_count"], 1)
        source = (root / "apps/platform-api/src/module.py").read_text(encoding="utf-8")
        (root / "apps/platform-api/src/module.py").write_text(
            source + "@router.post('/new')\nasync def new_route():\n    return {}\n",
            encoding="utf-8",
        )
        stale = contract_registry_check.audit(root, expected_operation_count=1, strict_runtime=True)
        self.assertIn("runtime.route_missing", {item["code"] for item in stale["findings"]})


if __name__ == "__main__":
    unittest.main()
