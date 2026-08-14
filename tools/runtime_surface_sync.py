#!/usr/bin/env python3
"""Generate and verify the committed implementation surface snapshot.

The numbered design registries describe the frozen contract baseline. This
snapshot records the routes, tables, and OpenAPI operations that are actually
present in the checked-in runtime source, so implementation growth and design
registry drift remain separately auditable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.contract_registry_check import parse_go_routes, parse_openapi, parse_source_routes, parse_source_tables


SCHEMA_VERSION = 1
DEFAULT_OUTPUT = Path("api/runtime-surface.json")


def build_surface(root: Path) -> dict:
    source_root = root / "apps" / "platform-api" / "src"
    routes = parse_source_routes(source_root)
    gateway_root = root / "apps" / "gateway"
    if gateway_root.exists():
        routes.extend(parse_go_routes(gateway_root, root))
    routes.sort(key=lambda item: (item.method, item.path, item.file, item.line, item.handler))

    tables = parse_source_tables(source_root)
    tables.sort(key=lambda item: (item.name, item.file, item.line))

    _, openapi_operations = parse_openapi(root / "api" / "openapi.yaml")
    openapi_operations.sort(key=lambda item: (item.method, item.path, item.operation_id, item.line))

    def _norm(path: str) -> str:
        return path.replace("\\", "/")

    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "route_scope": "platform-api FastAPI /api/v1 routes and explicit gateway net/http routes",
            "routes": [
                {
                    "method": item.method,
                    "path": item.path,
                    "file": _norm(item.file),
                    "line": item.line,
                    "handler": item.handler,
                }
                for item in routes
            ],
            "tables": [
                {"name": item.name, "file": _norm(item.file), "line": item.line}
                for item in tables
            ],
        },
        "openapi": {
            "operations": [
                {
                    "operation_id": item.operation_id,
                    "method": item.method,
                    "path": item.path,
                    "line": item.line,
                }
                for item in openapi_operations
            ],
        },
    }


def encode(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate or check api/runtime-surface.json")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--write", action="store_true", help="write the generated snapshot")
    parser.add_argument("--check", action="store_true", help="fail when the committed snapshot is stale or missing")
    args = parser.parse_args()
    if args.write and args.check:
        parser.error("--write and --check are mutually exclusive")
    if not args.write and not args.check:
        parser.error("one of --write or --check is required")

    root = args.root.resolve()
    output = args.output if args.output.is_absolute() else root / args.output
    expected = build_surface(root)
    if args.write:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encode(expected), encoding="utf-8")
        print(f"wrote {output}")
        return 0

    if not output.exists():
        print(f"runtime surface snapshot is missing: {output}")
        return 1
    try:
        actual = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"runtime surface snapshot is invalid: {output}: {exc}")
        return 1
    if actual != expected:
        print(f"runtime surface snapshot is stale: {output}")
        return 1
    print(f"runtime surface is current: {len(expected['source']['routes'])} routes, {len(expected['source']['tables'])} table references, {len(expected['openapi']['operations'])} OpenAPI operations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
