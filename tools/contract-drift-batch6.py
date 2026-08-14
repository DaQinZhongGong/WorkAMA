#!/usr/bin/env python3
"""契约漂移第六批检测脚本（v7.141）。

校验 Python ``FREE_PROVIDER_PRESETS`` / ``PROVIDER_CATALOG`` / ``PROVIDER_ALIASES``
与 Go ``ProviderCatalog`` 之间的契约一致性，覆盖 v7.134 起 preset 扩到 100、
catalog 扩到 103 的新门槛。

校验项（每项失败记一个 drift）：
  1. preset 数量门槛（>= 100）
  2. catalog 数量门槛（>= 100）
  3. Python<->Go catalog 双向一致性（key / protocol / capabilities / regions）
  4. preset<->catalog 双向覆盖（strict 模式）
  5. PROVIDER_ALIASES 解析到 PROVIDER_CATALOG
  6. preset 字段完整性
  7. 协议白名单 {openai, anthropic, gemini}
  8. 能力白名单 {chat, vision, tool_call, json_mode, embedding, reasoning,
                background, long_context, image_generation}

脚本只使用 Python 标准库，通过 AST / 正则静态解析源码，不导入业务模块、
不依赖 Docker，可直接 ``python tools/contract-drift-batch6.py`` 运行。

证据输出：``quality/evidence/contract-drift-batch6.json``
退出码：drift_count == 0 则 0，否则 1。
"""
from __future__ import annotations

import ast
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FREE_PRESETS_PATH = (
    ROOT
    / "apps"
    / "platform-api"
    / "src"
    / "workama_platform"
    / "modules"
    / "gateway"
    / "free_presets.py"
)
ROUTER_PATH = (
    ROOT
    / "apps"
    / "platform-api"
    / "src"
    / "workama_platform"
    / "modules"
    / "gateway"
    / "router.py"
)
ADAPTER_PATH = ROOT / "apps" / "gateway" / "internal" / "relay" / "adapter" / "adapter.go"
EVIDENCE_PATH = ROOT / "quality" / "evidence" / "contract-drift-batch6.json"

# v7.134+ 门槛：preset >= 100、catalog >= 100（catalog 实际 103，但门槛取 100
# 与 test_free_providers_comprehensive.py::MIN_CATALOG_ENTRIES 一致）
MIN_FREE_PRESETS = 100
MIN_CATALOG_ENTRIES = 100

ALLOWED_PROTOCOLS = {"openai", "anthropic", "gemini"}
ALLOWED_CAPABILITIES = {
    "chat",
    "vision",
    "tool_call",
    "json_mode",
    "embedding",
    "reasoning",
    "background",
    "long_context",
    "image_generation",
}
REQUIRED_PRESET_FIELDS = {
    "provider",
    "name",
    "base_url",
    "protocol",
    "signup_url",
    "free_quota",
    "free_models",
    "capabilities",
    "regions",
    "retention_mode",
    "notes",
}


# ----------------------------------------------------------------------
# Python 源码 AST 解析
# ----------------------------------------------------------------------


def _ast_dict(node: ast.AST) -> dict | None:
    """如果 node 是 ast.Dict 字面量，返回 {key: value_node} 字典；否则 None。"""
    if not isinstance(node, ast.Dict):
        return None
    out: dict = {}
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            out[key.value] = value
    return out


def _ast_list_of_str(node: ast.AST) -> list[str] | None:
    """如果 node 是 ast.List 且所有元素是字符串常量，返回 list；否则 None。"""
    if not isinstance(node, ast.List):
        return None
    out: list[str] = []
    for element in node.elts:
        if isinstance(element, ast.Constant) and isinstance(element.value, str):
            out.append(element.value)
        else:
            return None
    return out


def _ast_scalar(node: ast.AST):
    """提取标量常量值（str/int/bool/None）；非标量返回 None。"""
    if isinstance(node, ast.Constant):
        return node.value
    return None


def _extract_top_level_dict(tree: ast.Module, name: str) -> dict | None:
    """从模块顶层找 ``name = {...}`` 或 ``name: T = {...}`` 赋值。

    同时支持普通赋值（``ast.Assign``）与带类型注解的赋值
    （``ast.AnnAssign``），后者用于 ``FREE_PROVIDER_PRESETS: dict = {...}``。
    """
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return _ast_dict(node.value)
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if (
                isinstance(target, ast.Name)
                and target.id == name
                and node.value is not None
            ):
                return _ast_dict(node.value)
    return None


def load_free_presets(path: Path) -> dict[str, dict]:
    """解析 free_presets.py 中的 FREE_PROVIDER_PRESETS 字典字面量。

    返回 {preset_key: {field: value}}，其中 list 字段保持为 list。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    raw = _extract_top_level_dict(tree, "FREE_PROVIDER_PRESETS")
    if raw is None:
        raise RuntimeError("FREE_PROVIDER_PRESETS not found or not a dict literal")
    presets: dict[str, dict] = {}
    for key, value_node in raw.items():
        preset_raw = _ast_dict(value_node)
        if preset_raw is None:
            raise RuntimeError(f"preset {key!r} is not a dict literal")
        preset: dict = {}
        for field, field_value in preset_raw.items():
            if field in ("free_models", "capabilities", "regions"):
                preset[field] = _ast_list_of_str(field_value)
            else:
                preset[field] = _ast_scalar(field_value)
        presets[key] = preset
    return presets


def load_router_catalog_and_aliases(path: Path) -> tuple[dict[str, dict], dict[str, str]]:
    """解析 router.py 中的 PROVIDER_CATALOG 与 PROVIDER_ALIASES。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    catalog_raw = _extract_top_level_dict(tree, "PROVIDER_CATALOG")
    if catalog_raw is None:
        raise RuntimeError("PROVIDER_CATALOG not found or not a dict literal")
    catalog: dict[str, dict] = {}
    for key, value_node in catalog_raw.items():
        entry_raw = _ast_dict(value_node)
        if entry_raw is None:
            raise RuntimeError(f"catalog entry {key!r} is not a dict literal")
        entry: dict = {}
        for field, field_value in entry_raw.items():
            if field in ("capabilities", "regions"):
                entry[field] = _ast_list_of_str(field_value)
            else:
                entry[field] = _ast_scalar(field_value)
        catalog[key] = entry

    aliases_raw = _extract_top_level_dict(tree, "PROVIDER_ALIASES")
    if aliases_raw is None:
        raise RuntimeError("PROVIDER_ALIASES not found or not a dict literal")
    aliases: dict[str, str] = {}
    for alias, canonical in aliases_raw.items():
        value = _ast_scalar(canonical)
        if isinstance(value, str):
            aliases[alias] = value
    return catalog, aliases


# ----------------------------------------------------------------------
# Go 源码正则解析
# ----------------------------------------------------------------------

# 捕获 ProviderCatalog = map[string]providerEntry{ ... } 整个 map 字面量主体。
# 利用 map 闭合括号独占一行（``\n}``）这一格式约定，避免与条目内部的 ``}`` 冲突。
GO_CATALOG_PATTERN = re.compile(
    r"var\s+ProviderCatalog\s*=\s*map\[string\]providerEntry\{(.*?)\n\}",
    re.DOTALL,
)

# 捕获单条目：`"key": {"protocol", []string{...}, []string{...}, "retention", "version"}`
# version 字段可选（保留兼容性，当前所有条目均含 version）。
GO_ENTRY_PATTERN = re.compile(
    r'"(?P<key>[^"]+)"\s*:\s*\{\s*'
    r'"(?P<protocol>[^"]+)"\s*,\s*'
    r"\[\]string\{(?P<caps>[^}]*)\}\s*,\s*"
    r"\[\]string\{(?P<regions>[^}]*)\}\s*,\s*"
    r'"(?P<retention>[^"]+)"'
    r'(?:\s*,\s*"(?P<version>[^"]*)")?'
    r"\s*\}",
)

GO_STRING_PATTERN = re.compile(r'"([^"]+)"')


def _parse_go_string_list(raw: str) -> list[str]:
    return [m.group(1) for m in GO_STRING_PATTERN.finditer(raw)]


def load_go_catalog(path: Path) -> dict[str, dict]:
    """从 adapter.go 正则解析 ProviderCatalog map。

    不硬编码条目数量，逐条动态提取。返回
    {key: {protocol, capabilities, regions, retention_mode, version}}。
    """
    text = path.read_text(encoding="utf-8")
    catalog_match = GO_CATALOG_PATTERN.search(text)
    if not catalog_match:
        raise RuntimeError("ProviderCatalog map literal not found in adapter.go")
    body = catalog_match.group(1)
    catalog: dict[str, dict] = {}
    for entry_match in GO_ENTRY_PATTERN.finditer(body):
        key = entry_match.group("key")
        catalog[key] = {
            "protocol": entry_match.group("protocol"),
            "capabilities": _parse_go_string_list(entry_match.group("caps")),
            "regions": _parse_go_string_list(entry_match.group("regions")),
            "retention_mode": entry_match.group("retention"),
            "version": entry_match.group("version") or "",
        }
    return catalog


# ----------------------------------------------------------------------
# 漂移检测
# ----------------------------------------------------------------------


def check_drifts(
    free_presets: dict[str, dict],
    py_catalog: dict[str, dict],
    py_aliases: dict[str, str],
    go_catalog: dict[str, dict],
) -> list[dict]:
    drifts: list[dict] = []

    # 1. Preset 数量门槛
    if len(free_presets) < MIN_FREE_PRESETS:
        drifts.append(
            {
                "category": "preset.count",
                "details": (
                    f"FREE_PROVIDER_PRESETS count {len(free_presets)} "
                    f"< threshold {MIN_FREE_PRESETS}"
                ),
            }
        )

    # 2. Catalog 数量门槛
    if len(py_catalog) < MIN_CATALOG_ENTRIES:
        drifts.append(
            {
                "category": "catalog.count",
                "details": (
                    f"PROVIDER_CATALOG count {len(py_catalog)} "
                    f"< threshold {MIN_CATALOG_ENTRIES}"
                ),
            }
        )

    # 3. Python <-> Go catalog 双向一致性
    py_keys = set(py_catalog)
    go_keys = set(go_catalog)
    for key in sorted(py_keys - go_keys):
        drifts.append(
            {
                "category": "catalog.py_go.missing_in_go",
                "details": (
                    f"provider {key!r} in Python PROVIDER_CATALOG but missing "
                    f"in Go ProviderCatalog"
                ),
            }
        )
    for key in sorted(go_keys - py_keys):
        drifts.append(
            {
                "category": "catalog.py_go.missing_in_py",
                "details": (
                    f"provider {key!r} in Go ProviderCatalog but missing "
                    f"in Python PROVIDER_CATALOG"
                ),
            }
        )
    for key in sorted(py_keys & go_keys):
        py_entry = py_catalog[key]
        go_entry = go_catalog[key]
        if py_entry.get("protocol") != go_entry.get("protocol"):
            drifts.append(
                {
                    "category": "catalog.py_go.protocol_mismatch",
                    "details": (
                        f"provider {key!r}: python protocol="
                        f"{py_entry.get('protocol')!r} vs go protocol="
                        f"{go_entry.get('protocol')!r}"
                    ),
                }
            )
        if py_entry.get("capabilities") != go_entry.get("capabilities"):
            drifts.append(
                {
                    "category": "catalog.py_go.capabilities_mismatch",
                    "details": (
                        f"provider {key!r}: python capabilities="
                        f"{py_entry.get('capabilities')} vs go capabilities="
                        f"{go_entry.get('capabilities')}"
                    ),
                }
            )
        if py_entry.get("regions") != go_entry.get("regions"):
            drifts.append(
                {
                    "category": "catalog.py_go.regions_mismatch",
                    "details": (
                        f"provider {key!r}: python regions="
                        f"{py_entry.get('regions')} vs go regions="
                        f"{go_entry.get('regions')}"
                    ),
                }
            )

    # 4. Preset <-> Catalog 双向覆盖（strict 模式）
    # 4a. 每个 preset 的 provider 字段必须在 PROVIDER_CATALOG 中
    for key, preset in free_presets.items():
        provider = preset.get("provider")
        if not isinstance(provider, str):
            drifts.append(
                {
                    "category": "preset.catalog_coverage",
                    "details": (
                        f"preset {key!r} missing or non-string 'provider' field"
                    ),
                }
            )
            continue
        if provider not in py_catalog:
            drifts.append(
                {
                    "category": "preset.catalog_coverage",
                    "details": (
                        f"preset {key!r} provider={provider!r} not in "
                        f"PROVIDER_CATALOG"
                    ),
                }
            )
    # 4b. catalog key 与 preset key 同名时，preset 必须可被 enable
    # （即 preset['provider'] 在 catalog 中）
    for cat_key in py_catalog:
        if cat_key in free_presets:
            provider = free_presets[cat_key].get("provider")
            if not isinstance(provider, str) or provider not in py_catalog:
                drifts.append(
                    {
                        "category": "catalog.preset_coverage",
                        "details": (
                            f"catalog key {cat_key!r} has same-named preset but "
                            f"preset provider={provider!r} not in catalog"
                        ),
                    }
                )

    # 5. 别名解析：每个 PROVIDER_ALIASES 的 value 必须在 PROVIDER_CATALOG 中
    for alias, canonical in py_aliases.items():
        if canonical not in py_catalog:
            drifts.append(
                {
                    "category": "alias.unresolved",
                    "details": (
                        f"alias {alias!r} -> {canonical!r} not in PROVIDER_CATALOG"
                    ),
                }
            )

    # 6. 字段完整性
    for key, preset in free_presets.items():
        missing = REQUIRED_PRESET_FIELDS - set(preset.keys())
        if missing:
            drifts.append(
                {
                    "category": "preset.field.integrity",
                    "details": (
                        f"preset {key!r} missing required fields: {sorted(missing)}"
                    ),
                }
            )

    # 7. 协议白名单
    for key, preset in free_presets.items():
        protocol = preset.get("protocol")
        if protocol not in ALLOWED_PROTOCOLS:
            drifts.append(
                {
                    "category": "preset.protocol.invalid",
                    "details": (
                        f"preset {key!r} protocol={protocol!r} not in "
                        f"{sorted(ALLOWED_PROTOCOLS)}"
                    ),
                }
            )

    # 8. 能力白名单
    for key, preset in free_presets.items():
        capabilities = preset.get("capabilities") or []
        for capability in capabilities:
            if capability not in ALLOWED_CAPABILITIES:
                drifts.append(
                    {
                        "category": "preset.capability.invalid",
                        "details": (
                            f"preset {key!r} invalid capability {capability!r} "
                            f"not in {sorted(ALLOWED_CAPABILITIES)}"
                        ),
                    }
                )

    return drifts


def main() -> int:
    drifts: list[dict] = []
    free_presets: dict[str, dict] = {}
    py_catalog: dict[str, dict] = {}
    py_aliases: dict[str, str] = {}
    go_catalog: dict[str, dict] = {}

    try:
        free_presets = load_free_presets(FREE_PRESETS_PATH)
    except (OSError, SyntaxError, RuntimeError) as exc:
        drifts.append({"category": "load.free_presets", "details": str(exc)})

    try:
        py_catalog, py_aliases = load_router_catalog_and_aliases(ROUTER_PATH)
    except (OSError, SyntaxError, RuntimeError) as exc:
        drifts.append({"category": "load.router", "details": str(exc)})

    try:
        go_catalog = load_go_catalog(ADAPTER_PATH)
    except (OSError, RuntimeError) as exc:
        drifts.append({"category": "load.adapter", "details": str(exc)})

    drifts.extend(check_drifts(free_presets, py_catalog, py_aliases, go_catalog))

    report = {
        "ok": not drifts,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "drifts": drifts,
        "summary": {
            "preset_count": len(free_presets),
            "catalog_count": len(py_catalog),
            "go_catalog_count": len(go_catalog),
            "drift_count": len(drifts),
        },
    }

    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not drifts else 1


if __name__ == "__main__":
    sys.exit(main())
