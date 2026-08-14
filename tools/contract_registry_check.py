#!/usr/bin/env python3
"""Check implementation surfaces against the frozen API/data registries.

The design registry is intentionally larger than the currently enabled vertical
slice.  Therefore the default check is a non-breaking audit: it fails on
duplicate/invalid registry entries, while reporting source-only routes/tables
and OpenAPI drift as warnings.  ``--strict-source`` and ``--strict-openapi``
turn those warnings into release blockers when a phase is ready to seal.

This module uses only the Python standard library so it can run before service
dependencies are installed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path


EXPECTED_OPERATION_COUNT = 855
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
# v7.151+ 引入 v2 router（assistant_v2_router / workflow_v2_router /
# workspace_v2_router / billing_v2_router）挂在既有 router 之前，FastAPI 采用
# first-match 优先匹配 v2。旧 router 仍声明相同路径，属于设计意图，非缺陷，
# 因此在 source.route_duplicate 检查中显式豁免以下 16 条重复声明。
INTENTIONAL_V2_DUPLICATES = frozenset({
    "GET /api/v1/assistants",
    "GET /api/v1/assistants/{}",
    "GET /api/v1/assistants/{}/runs",
    "GET /api/v1/billing/invoices",
    "GET /api/v1/billing/invoices/{}",
    "GET /api/v1/billing/plans",
    "GET /api/v1/billing/usage",
    "GET /api/v1/workflows",
    "GET /api/v1/workflows/{}",
    "GET /api/v1/workflows/{}/runs",
    "GET /api/v1/workspaces",
    "GET /api/v1/workspaces/{}",
    "PATCH /api/v1/workflows/{}",
    "POST /api/v1/assistants",
    "POST /api/v1/workflows",
    "POST /api/v1/workspaces",
})
TABLE_PREFIXES = ("id", "gw", "pf", "ag", "bill", "sec", "ops")
TABLE_PATTERN = re.compile(r"\b(?:id|gw|pf|ag|bill|sec|ops)_[a-z0-9_]+\b")
OPERATION_PATTERN = re.compile(
    r"^\|\s*([A-Za-z][A-Za-z0-9]+)\s*\|\s*`(GET|POST|PUT|PATCH|DELETE)\s+([^`]+)`\s*\|",
    re.MULTILINE,
)
ROUTER_PATTERN = re.compile(
    r"^\s*(\w+)\s*=\s*APIRouter\(\s*prefix\s*=\s*['\"]([^'\"]*)",
    re.MULTILINE,
)
ROUTE_PATTERN = re.compile(
    r"^\s*@(?P<router>\w+)\.(?P<method>get|post|put|patch|delete)"
    r"\(\s*['\"](?P<path>[^'\"]*)['\"]",
    re.MULTILINE,
)
CREATE_TABLE_PATTERN = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:[\"']?[A-Za-z_][A-Za-z0-9_]*[\"']?\.)?"
    r"[\"']?([a-z][a-z0-9_]*)[\"']?",
    re.IGNORECASE,
)
GO_ROUTE_PATTERN = re.compile(
    r"mux\.HandleFunc\(\s*['\"](?P<method>GET|POST|PUT|PATCH|DELETE)\s+"
    r"(?P<path>/[^'\"]+)['\"]",
    re.MULTILINE,
)
OPENAPI_PATH_PATTERN = re.compile(r"^  (?P<path>/[^:#]+):\s*$")
OPENAPI_METHOD_PATTERN = re.compile(r"^    (?P<method>get|post|put|patch|delete):\s*$")
OPENAPI_OPERATION_PATTERN = re.compile(r"^\s+operationId:\s*(?P<operation>[A-Za-z][A-Za-z0-9_-]*)\s*$")
OPENAPI_SERVER_PATTERN = re.compile(r"^\s*-?\s*url:\s*(?:https?://[^/]+)?(?P<base>/[^\s]+)\s*$")


@dataclass(frozen=True)
class Operation:
    operation_id: str
    method: str
    path: str
    line: int


@dataclass(frozen=True)
class SourceRoute:
    method: str
    path: str
    file: str
    line: int
    handler: str


@dataclass(frozen=True)
class SourceTable:
    name: str
    file: str
    line: int


def normalize_path(path: str, *, base: str = "") -> str:
    """Normalize path parameters while preserving method/path semantics."""

    value = path.strip()
    if base:
        normalized_base = base.rstrip("/") or "/"
        if value != normalized_base and not value.startswith(f"{normalized_base}/"):
            value = f"{normalized_base}/{value.lstrip('/')}"
    value = re.sub(r"/+", "/", value)
    if not value.startswith("/"):
        value = f"/{value}"
    value = re.sub(r"\{[^}/]+\}", "{}", value)
    if len(value) > 1:
        value = value.rstrip("/")
    return value


def parse_operations(path: Path) -> list[Operation]:
    text = path.read_text(encoding="utf-8")
    operations: list[Operation] = []
    for match in OPERATION_PATTERN.finditer(text):
        operations.append(
            Operation(
                operation_id=match.group(1),
                method=match.group(2),
                path=normalize_path(match.group(3)),
                line=text[: match.start()].count("\n") + 1,
            )
        )
    return operations


def parse_source_routes(source_root: Path) -> list[SourceRoute]:
    routes: list[SourceRoute] = []
    for path in sorted(source_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            continue
        prefixes = {match.group(1): match.group(2) for match in ROUTER_PATTERN.finditer(text)}
        for match in ROUTE_PATTERN.finditer(text):
            prefix = prefixes.get(match.group("router"), "")
            if not prefix.startswith("/api/v1"):
                continue
            route_path = normalize_path(match.group("path"), base=prefix)
            function = re.search(r"^\s*(?:async\s+)?def\s+(\w+)", text[match.end() :], re.MULTILINE)
            routes.append(
                SourceRoute(
                    method=match.group("method").upper(),
                    path=route_path,
                    file=str(path.relative_to(source_root.parent.parent.parent))
                    if len(path.parents) >= 3
                    else str(path),
                    line=text[: match.start()].count("\n") + 1,
                    handler=function.group(1) if function else "<unknown>",
                )
            )
    return routes


def parse_go_routes(source_root: Path, workspace: Path) -> list[SourceRoute]:
    """Read explicit net/http method patterns from the gateway mux."""

    routes: list[SourceRoute] = []
    for path in sorted(path for path in source_root.rglob("*.go") if path.is_file()):
        text = path.read_text(encoding="utf-8")
        for match in GO_ROUTE_PATTERN.finditer(text):
            routes.append(
                SourceRoute(
                    method=match.group("method"),
                    path=normalize_path(match.group("path")),
                    file=str(path.relative_to(workspace)),
                    line=text[: match.start()].count("\n") + 1,
                    handler="mux.HandleFunc",
                )
            )
    return routes


def parse_source_tables(source_root: Path) -> list[SourceTable]:
    tables: list[SourceTable] = []
    scan_roots = [source_root]
    workspace = source_root.parent.parent.parent
    for name in ("api", "deploy"):
        candidate = workspace / name
        if candidate.exists():
            scan_roots.append(candidate)
    paths: set[Path] = set()
    for root in scan_roots:
        paths.update(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in {".py", ".sql", ".go"})
    for path in sorted(paths):
        text = path.read_text(encoding="utf-8")
        for match in CREATE_TABLE_PATTERN.finditer(text):
            name = match.group(1).lower()
            if not any(name.startswith(f"{prefix}_") for prefix in TABLE_PREFIXES):
                continue
            try:
                display_path = str(path.relative_to(workspace))
            except ValueError:
                display_path = str(path)
            tables.append(SourceTable(name=name, file=display_path, line=text[: match.start()].count("\n") + 1))
    return tables


def parse_table_registry(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return set(TABLE_PATTERN.findall(text))


def parse_openapi(path: Path) -> tuple[str, list[Operation]]:
    """Parse the small, stable OpenAPI shape without requiring PyYAML."""

    lines = path.read_text(encoding="utf-8").splitlines()
    base = ""
    current_path: str | None = None
    current_method: str | None = None
    operations: list[Operation] = []
    for index, line in enumerate(lines):
        server = OPENAPI_SERVER_PATTERN.match(line)
        if server and not base:
            base = normalize_path(server.group("base"))
        path_match = OPENAPI_PATH_PATTERN.match(line)
        if path_match:
            current_path = path_match.group("path").strip()
            current_method = None
            continue
        method_match = OPENAPI_METHOD_PATTERN.match(line)
        if method_match and current_path:
            current_method = method_match.group("method").upper()
            continue
        operation_match = OPENAPI_OPERATION_PATTERN.match(line)
        if operation_match and current_path and current_method:
            operations.append(
                Operation(
                    operation_id=operation_match.group("operation"),
                    method=current_method,
                    path=normalize_path(current_path, base=base),
                    line=index + 1,
                )
            )
    return base, operations


def parse_runtime_surface(path: Path) -> dict:
    """Load the generated runtime inventory with a small schema guard."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("runtime surface schema_version must be 1")
    source = payload.get("source")
    openapi = payload.get("openapi")
    if not isinstance(source, dict) or not isinstance(openapi, dict):
        raise ValueError("runtime surface source/openapi sections are required")
    if not isinstance(source.get("routes"), list) or not isinstance(source.get("tables"), list):
        raise ValueError("runtime surface source routes/tables must be arrays")
    if not isinstance(openapi.get("operations"), list):
        raise ValueError("runtime surface OpenAPI operations must be an array")
    return payload


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


def audit(
    root: Path,
    *,
    expected_operation_count: int = EXPECTED_OPERATION_COUNT,
    strict_source: bool = False,
    strict_openapi: bool = False,
    strict_runtime: bool = False,
) -> dict:
    docs = root / "WorkAMA-Docs"
    registry = parse_operations(docs / "720-实施级API操作与消息契约注册表.md")
    registry_by_key = {(item.method, item.path): item for item in registry}
    platform_source_root = root / "apps" / "platform-api" / "src"
    source_routes = parse_source_routes(platform_source_root)
    gateway_root = root / "apps" / "gateway"
    if gateway_root.exists():
        source_routes.extend(parse_go_routes(gateway_root, root))
    source_tables = parse_source_tables(root / "apps" / "platform-api" / "src")
    documented_tables = parse_table_registry(docs / "610-数据库物理模型与数据生命周期设计.md")
    _, openapi_operations = parse_openapi(root / "api" / "openapi.yaml")
    runtime_surface_path = root / "api" / "runtime-surface.json"
    runtime_surface: dict | None = None

    findings: list[dict] = []

    def add(code: str, severity: str, message: str, **extra: object) -> None:
        findings.append({"code": code, "severity": severity, "message": message, **extra})

    if runtime_surface_path.exists():
        try:
            runtime_surface = parse_runtime_surface(runtime_surface_path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            add("runtime.surface_invalid", "error", f"runtime surface snapshot is invalid: {exc}", path="api/runtime-surface.json")
    elif strict_runtime:
        add("runtime.surface_missing", "error", "runtime surface snapshot is required in strict runtime mode", path="api/runtime-surface.json")

    runtime_route_keys: set[tuple[str, str]] = set()
    runtime_table_names: set[str] = set()
    runtime_openapi_keys: set[tuple[str, str, str]] = set()

    operation_ids = [item.operation_id for item in registry]
    operation_keys = [(item.method, item.path) for item in registry]
    duplicate_ids = _duplicates(operation_ids)
    duplicate_keys = _duplicates([f"{method} {path}" for method, path in operation_keys])
    if len(registry) != expected_operation_count:
        add("registry.count", "error", f"expected {expected_operation_count} operations, found {len(registry)}")
    for operation_id in duplicate_ids:
        add("registry.duplicate_operation_id", "error", f"operationId is duplicated: {operation_id}", operation_id=operation_id)
    for key in duplicate_keys:
        add("registry.duplicate_method_path", "error", f"method/path is duplicated: {key}", key=key)

    source_keys = {(item.method, item.path) for item in source_routes}
    unregistered_routes = sorted(source_keys - set(registry_by_key))
    route_severity = "error" if strict_source else "warning"
    for method, path in unregistered_routes:
        add("source.route_unregistered", route_severity, f"implemented route is not in 720: {method} {path}", method=method, path=path)
    for key in _duplicates([f"{item.method} {item.path}" for item in source_routes]):
        if key in INTENTIONAL_V2_DUPLICATES:
            continue
        add("source.route_duplicate", "error", f"implemented route is declared more than once: {key}", key=key)

    documented_openapi_ids = set(operation_ids)
    openapi_id_duplicates = _duplicates([item.operation_id for item in openapi_operations])
    for operation_id in openapi_id_duplicates:
        add("openapi.duplicate_operation_id", "error", f"OpenAPI operationId is duplicated: {operation_id}", operation_id=operation_id)
    for item in openapi_operations:
        if item.operation_id not in documented_openapi_ids:
            add(
                "openapi.operation_unregistered",
                "error" if strict_openapi else "warning",
                f"OpenAPI operationId is not in 720: {item.operation_id}",
                operation_id=item.operation_id,
                line=item.line,
            )
        if (item.method, item.path) not in registry_by_key:
            add(
                "openapi.path_unregistered",
                "error" if strict_openapi else "warning",
                f"OpenAPI method/path is not in 720: {item.method} {item.path}",
                method=item.method,
                path=item.path,
                line=item.line,
            )

    documented_table_names = sorted(documented_tables)
    source_table_names = sorted({item.name for item in source_tables})
    table_severity = "error" if strict_source else "warning"
    for name in sorted(set(source_table_names) - documented_tables):
        references = [asdict(item) for item in source_tables if item.name == name]
        add("source.table_unregistered", table_severity, f"source table is not in 610: {name}", table=name, references=references)

    if runtime_surface:
        runtime_route_keys = {
            (str(item.get("method", "")).upper(), normalize_path(str(item.get("path", ""))))
            for item in runtime_surface["source"]["routes"]
            if isinstance(item, dict)
        }
        runtime_table_names = {
            str(item.get("name", "")).lower()
            for item in runtime_surface["source"]["tables"]
            if isinstance(item, dict)
        }
        runtime_openapi_keys = {
            (
                str(item.get("operation_id", "")),
                str(item.get("method", "")).upper(),
                normalize_path(str(item.get("path", ""))),
            )
            for item in runtime_surface["openapi"]["operations"]
            if isinstance(item, dict)
        }
        for method, path in sorted(source_keys - runtime_route_keys):
            add("runtime.route_missing", "error", f"runtime surface is missing implemented route: {method} {path}", method=method, path=path)
        for method, path in sorted(runtime_route_keys - source_keys):
            add("runtime.route_stale", "error", f"runtime surface contains a route no longer in source: {method} {path}", method=method, path=path)
        for name in sorted(set(source_table_names) - runtime_table_names):
            add("runtime.table_missing", "error", f"runtime surface is missing implemented table: {name}", table=name)
        for name in sorted(runtime_table_names - set(source_table_names)):
            add("runtime.table_stale", "error", f"runtime surface contains a table no longer in source: {name}", table=name)

        actual_openapi_keys = {(item.operation_id, item.method, item.path) for item in openapi_operations}
        for operation_id, method, path in sorted(actual_openapi_keys - runtime_openapi_keys):
            add("runtime.openapi_missing", "error", f"runtime surface is missing OpenAPI operation: {operation_id} {method} {path}", operation_id=operation_id, method=method, path=path)
        for operation_id, method, path in sorted(runtime_openapi_keys - actual_openapi_keys):
            add("runtime.openapi_stale", "error", f"runtime surface contains an OpenAPI operation no longer in source spec: {operation_id} {method} {path}", operation_id=operation_id, method=method, path=path)

    errors = [item for item in findings if item["severity"] == "error"]
    warnings = [item for item in findings if item["severity"] == "warning"]
    return {
        "ok": not errors,
        "strict_source": strict_source,
        "strict_openapi": strict_openapi,
        "strict_runtime": strict_runtime,
        "registry": {
            "operation_count": len(registry),
            "expected_operation_count": expected_operation_count,
            "operation_id_count": len(set(operation_ids)),
            "method_path_count": len(set(operation_keys)),
        },
        "implementation": {
            "route_scope": "platform-api FastAPI /api/v1 routes and explicit gateway net/http routes",
            "route_count": len(source_routes),
            "route_unique_count": len(source_keys),
            "unregistered_routes": [f"{method} {path}" for method, path in unregistered_routes],
            "source_table_count": len(source_table_names),
            "documented_table_count": len(documented_table_names),
            "unregistered_tables": sorted(set(source_table_names) - documented_tables),
        },
        "openapi": {
            "operation_count": len(openapi_operations),
            "unregistered_operation_ids": sorted({item.operation_id for item in openapi_operations} - documented_openapi_ids),
            "unregistered_method_paths": sorted(
                f"{item.method} {item.path}" for item in openapi_operations if (item.method, item.path) not in registry_by_key
            ),
        },
        "runtime_surface": {
            "path": "api/runtime-surface.json",
            "present": runtime_surface is not None,
            "route_count": len(runtime_route_keys),
            "table_count": len(runtime_table_names),
            "openapi_operation_count": len(runtime_openapi_keys),
        },
        "findings": findings,
        "error_count": len(errors),
        "warning_count": len(warnings),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit implementation routes/tables against WorkAMA contract registries")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", type=Path)
    parser.add_argument("--strict-source", action="store_true", help="fail on source routes/tables not registered in 720/610")
    parser.add_argument("--strict-openapi", action="store_true", help="fail on OpenAPI operations/paths not registered in 720")
    parser.add_argument("--strict-runtime", action="store_true", help="require and validate the generated runtime surface snapshot")
    args = parser.parse_args()
    report = audit(
        args.root.resolve(),
        strict_source=args.strict_source,
        strict_openapi=args.strict_openapi,
        strict_runtime=args.strict_runtime,
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    print(encoded)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(encoded + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
