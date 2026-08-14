#!/usr/bin/env python3
"""v7.157: OpenAPI schema 导出与文档生成工具。

从运行中的 platform-api 拉取 OpenAPI schema 并生成：
1. ``docs/api/openapi.json`` —— 美化格式（4 空格缩进）的 JSON schema
2. ``docs/api/openapi.yaml`` —— YAML 格式 schema（pyyaml 已安装则使用，否则跳过）
3. ``docs/api/README.md`` —— 自动按 tag 分组的端点目录

使用方式::

    # 前置：platform-api 容器已启动
    python tools/export-openapi.py
    python tools/export-openapi.py --base-url http://localhost:20200
    python tools/export-openapi.py --base-url http://localhost:20200 --api-path /api/openapi.json

退出码：0=全部成功；1=拉取失败；2=部分导出失败。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "http://localhost:20200"
DEFAULT_API_PATHS = ("/api/openapi.json", "/openapi.json")
OUTPUT_DIR = PROJECT_ROOT / "docs" / "api"
JSON_PATH = OUTPUT_DIR / "openapi.json"
YAML_PATH = OUTPUT_DIR / "openapi.yaml"
README_PATH = OUTPUT_DIR / "README.md"

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")


def fetch_schema(base_url: str, api_path: str | None) -> dict[str, Any]:
    """从 platform-api 拉取 OpenAPI schema。

    优先使用 ``api_path``；若未指定，依次尝试 ``/api/openapi.json`` 与 ``/openapi.json``。
    """
    base = base_url.rstrip("/")
    candidates: list[str]
    if api_path:
        candidates = [api_path if api_path.startswith("/") else f"/{api_path}"]
    else:
        candidates = list(DEFAULT_API_PATHS)
    last_error: Exception | None = None
    for path in candidates:
        url = f"{base}{path}"
        try:
            print(f"[export-openapi] GET {url}")
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status != 200:
                    last_error = RuntimeError(f"{url} -> HTTP {resp.status}")
                    continue
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            last_error = exc
            continue
    raise RuntimeError(
        f"无法从 platform-api 拉取 OpenAPI schema（尝试过 {candidates}）；最后错误: {last_error}"
    )


def write_json(schema: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(
        json.dumps(schema, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"[export-openapi] 已写入 {JSON_PATH} ({JSON_PATH.stat().st_size} bytes)")


def write_yaml(schema: dict[str, Any]) -> bool:
    """导出 YAML；pyyaml 未安装时跳过并返回 False。"""
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        print("[export-openapi] 未安装 pyyaml，跳过 YAML 导出（json 版本仍可用）")
        return False
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    yaml_text = yaml.safe_dump(
        schema,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        indent=2,
    )
    YAML_PATH.write_text(yaml_text, encoding="utf-8")
    print(f"[export-openapi] 已写入 {YAML_PATH} ({YAML_PATH.stat().st_size} bytes)")
    return True


def _op_summary(op: dict[str, Any]) -> str:
    return op.get("summary") or op.get("operationId") or "(未命名)"


def _op_tags(op: dict[str, Any]) -> list[str]:
    tags = op.get("tags") or []
    return list(tags) if tags else ["untagged"]


def _format_parameters(op: dict[str, Any]) -> str:
    """格式化参数列表为 Markdown 表格。"""
    params = op.get("parameters") or []
    body_ref = ""
    req_body = op.get("requestBody")
    if req_body:
        content = req_body.get("content", {})
        app_json = content.get("application/json")
        if app_json and app_json.get("schema"):
            body_ref = _schema_brief(app_json["schema"])
    if not params and not body_ref:
        return "无"
    lines = []
    if params:
        lines.append("| 名称 | 位置 | 必填 | 类型 | 描述 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for p in params:
            name = p.get("name", "?")
            loc = p.get("in", "?")
            required = "是" if p.get("required") else "否"
            schema = p.get("schema", {}) or {}
            ptype = schema.get("type") or schema.get("$ref", "?").split("/")[-1] or "?"
            desc = (p.get("description") or "").replace("\n", " ").strip()
            lines.append(f"| `{name}` | {loc} | {required} | {ptype} | {desc} |")
    if body_ref:
        lines.append(f"- 请求体: {body_ref}")
    return "\n".join(lines)


def _schema_brief(schema: dict[str, Any]) -> str:
    if not schema:
        return "(无 schema)"
    if "$ref" in schema:
        ref = schema["$ref"].split("/")[-1]
        return f"`{ref}`"
    ptype = schema.get("type", "?")
    return f"`{ptype}`"


def _format_responses(op: dict[str, Any]) -> str:
    responses = op.get("responses") or {}
    if not responses:
        return "无"
    lines = ["| 状态码 | 描述 |", "| --- | --- |"]
    for code in sorted(responses.keys()):
        resp = responses[code] or {}
        desc = (resp.get("description") or "").replace("\n", " ").strip() or "-"
        lines.append(f"| `{code}` | {desc} |")
    return "\n".join(lines)


def write_readme(schema: dict[str, Any]) -> None:
    """按 tag 分组生成端点目录 README.md。"""
    info = schema.get("info", {}) or {}
    title = info.get("title", "OpenAPI")
    version = info.get("version", "")
    description = info.get("description", "") or ""
    paths = schema.get("paths", {}) or {}
    tags_meta = {t.get("name"): (t.get("description") or "") for t in (schema.get("tags") or [])}

    # 按 tag 聚合端点
    by_tag: dict[str, list[tuple[str, str, dict[str, Any]]]] = defaultdict(list)
    total_ops = 0
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            total_ops += 1
            for tag in _op_tags(op):
                by_tag[tag].append((method.upper(), path, op))

    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"> 版本：`{version}` ｜ 端点总数：**{total_ops}** ｜ 路径总数：**{len(paths)}**")
    lines.append("")
    lines.append("本文档由 `tools/export-openapi.py` 从 `/api/openapi.json` 自动生成，请勿手工编辑。")
    lines.append("")
    lines.append("## 目录")
    lines.append("")
    lines.append(f"- [API 概览](#api-概览)")
    lines.append(f"- [认证机制](#认证机制)")
    lines.append(f"- [模块端点清单](#模块端点清单)")
    # tag 索引
    sorted_tags = sorted(by_tag.keys())
    for tag in sorted_tags:
        anchor = _anchor(tag)
        lines.append(f"  - [`{tag}`](#{anchor})")
    lines.append("")
    lines.append("## API 概览")
    lines.append("")
    if description:
        lines.append(description.strip())
        lines.append("")
    servers = schema.get("servers") or []
    if servers:
        lines.append("### Servers")
        lines.append("")
        lines.append("| URL | 描述 |")
        lines.append("| --- | --- |")
        for s in servers:
            lines.append(f"| `{s.get('url', '')}` | {s.get('description', '')} |")
        lines.append("")
    contact = info.get("contact") or {}
    license_info = info.get("license") or {}
    if contact or license_info:
        lines.append("### 联系与许可证")
        lines.append("")
        if contact:
            parts = []
            if contact.get("name"):
                parts.append(f"联系人：{contact['name']}")
            if contact.get("email"):
                parts.append(f"邮箱：<{contact['email']}>")
            if contact.get("url"):
                parts.append(f"URL：{contact['url']}")
            lines.append("- " + " ｜ ".join(parts))
        if license_info:
            lines.append(f"- 许可证：{license_info.get('name', '?')} ({license_info.get('url', '')})")
        lines.append("")
    lines.append("## 认证机制")
    lines.append("")
    security_schemes = (schema.get("components") or {}).get("securitySchemes") or {}
    if security_schemes:
        lines.append("支持的认证方案：")
        lines.append("")
        for name, scheme in security_schemes.items():
            stype = scheme.get("type", "?")
            desc = scheme.get("description", "")
            lines.append(f"- **{name}**（{stype}）：{desc}")
        lines.append("")
    else:
        lines.append("OpenAPI schema 未声明 securitySchemes；实际使用 JWT Bearer Token，"
                     "请在 `Authorization` 头携带 `Bearer <access_token>`，"
                     "跨工作区操作请额外携带 `X-Workspace-Id` 头。")
        lines.append("")
    lines.append("## 模块端点清单")
    lines.append("")
    lines.append(f"共 **{len(sorted_tags)}** 个 tag 分组，**{total_ops}** 个端点。")
    lines.append("")
    for tag in sorted_tags:
        ops = by_tag[tag]
        anchor = _anchor(tag)
        lines.append(f"### `{tag}`")
        lines.append("")
        meta_desc = tags_meta.get(tag, "")
        if meta_desc:
            lines.append(f"> {meta_desc}")
            lines.append("")
        lines.append(f"共 **{len(ops)}** 个端点。")
        lines.append("")
        # 端点速查表
        lines.append("| 方法 | 路径 | 摘要 |")
        lines.append("| --- | --- | --- |")
        for method, path, op in ops:
            summary = _op_summary(op).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| `{method}` | `{path}` | {summary} |")
        lines.append("")
        # 端点详情
        for method, path, op in ops:
            lines.append(f"#### `{method} {path}`")
            lines.append("")
            summary = _op_summary(op)
            lines.append(f"- **摘要**：{summary}")
            op_id = op.get("operationId")
            if op_id:
                lines.append(f"- **operationId**：`{op_id}`")
            desc = op.get("description")
            if desc:
                lines.append(f"- **描述**：{desc.strip()}")
            lines.append(f"- **参数**：")
            lines.append("")
            lines.append(_format_parameters(op))
            lines.append("")
            lines.append(f"- **响应**：")
            lines.append("")
            lines.append(_format_responses(op))
            lines.append("")
        lines.append("---")
        lines.append("")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    README_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"[export-openapi] 已写入 {README_PATH} ({README_PATH.stat().st_size} bytes)")


def _anchor(text: str) -> str:
    """生成 GitHub Flavored Markdown 锚点。"""
    return text.lower().replace(" ", "-").replace(".", "").replace("/", "").replace("_", "-")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从 platform-api 拉取 OpenAPI schema 并导出 JSON/YAML/README.md"
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"platform-api 基础 URL（默认 {DEFAULT_BASE_URL}）",
    )
    parser.add_argument(
        "--api-path",
        default=None,
        help="OpenAPI 端点路径（默认依次尝试 /api/openapi.json 与 /openapi.json）",
    )
    parser.add_argument(
        "--no-yaml",
        action="store_true",
        help="跳过 YAML 导出",
    )
    parser.add_argument(
        "--no-readme",
        action="store_true",
        help="跳过 README.md 生成",
    )
    args = parser.parse_args()

    try:
        schema = fetch_schema(args.base_url, args.api_path)
    except Exception as exc:  # noqa: BLE001
        print(f"[export-openapi] ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        f"[export-openapi] schema 拉取成功：title={schema.get('info', {}).get('title')!r} "
        f"version={schema.get('info', {}).get('version')!r} "
        f"paths={len(schema.get('paths', {}))}"
    )

    failures = 0
    try:
        write_json(schema)
    except Exception as exc:  # noqa: BLE001
        print(f"[export-openapi] JSON 导出失败：{exc}", file=sys.stderr)
        failures += 1

    if not args.no_yaml:
        try:
            write_yaml(schema)
        except Exception as exc:  # noqa: BLE001
            print(f"[export-openapi] YAML 导出失败：{exc}", file=sys.stderr)
            failures += 1

    if not args.no_readme:
        try:
            write_readme(schema)
        except Exception as exc:  # noqa: BLE001
            print(f"[export-openapi] README 生成失败：{exc}", file=sys.stderr)
            failures += 1

    if failures:
        return 2
    print("[export-openapi] 全部导出完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
