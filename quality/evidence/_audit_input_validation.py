import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(r"d:\MyCode\WorkAMA")
MODULES_DIR = ROOT / "apps" / "platform-api" / "src" / "workama_platform" / "modules"
OUT_PATH = ROOT / "quality" / "evidence" / "api-input-validation-audit.json"

DECORATOR_RE = re.compile(
    r"@router\.(post|put|patch)\(\s*([^)]*?)\)\s*\n"
    r"(?:@[^\n]+\n)*"
    r"async def (\w+)\(([^)]*)\)",
    re.DOTALL,
)

CONSTRAINT_TOKENS = (
    "min_length", "max_length", "ge=", "le=", "gt=", "lt=",
    "pattern=", "regex=", "EmailStr", "HttpUrl", "constr",
    "conint", "confloat", "conlist", "condecimal", "Literal",
    "Enum", "UUID", "AnyHttpUrl", "max_items", "min_items",
)

NATIVE_TYPES = {"str", "int", "float", "bool", "dict", "list", "Any", "Actor",
                "Request", "Response", "BackgroundTasks", "Query", "Path",
                "Header", "Cookie", "Body", "Form", "File"}
SKIP_PARAMS = {"actor", "request", "response", "ws_id", "workspace_id",
               "user_id", "token", "background_tasks"}


def parse_endpoint(file_path, content, m):
    method = m.group(1).upper()
    path_arg = m.group(2).strip()
    fn_name = m.group(3)
    sig = m.group(4)
    # 去除首尾引号
    path = path_arg
    if len(path) >= 2 and path[0] in ('"', "'") and path[-1] == path[0]:
        path = path[1:-1]
    prefix_m = re.search(r'router\s*=\s*APIRouter\([^)]*prefix\s*=\s*("|\')(.*?)\1', content)
    prefix = prefix_m.group(2) if prefix_m else ""
    full_path = (prefix + path) if path else prefix

    has_body = False
    body_model = None
    bm = re.search(r"\bbody\s*:\s*(?:Annotated\s*\[\s*)?(\w+)", sig)
    if bm:
        has_body = True
        body_model = bm.group(1)
    if not body_model:
        for pm in re.finditer(r"(\w+)\s*:\s*(?:Annotated\s*\[\s*)?(\w+)", sig):
            pname, ptype = pm.group(1), pm.group(2)
            if pname in SKIP_PARAMS:
                continue
            if ptype in NATIVE_TYPES:
                continue
            body_model = ptype
            has_body = True
            break

    has_get_actor = ("get_actor" in sig) or ("Depends(get_actor)" in sig)
    has_require_capability = "require_capability(" in sig
    has_workspace_in_path = ("workspace_id" in full_path) or ("ws_id" in full_path) or ("/workspaces" in full_path)
    has_workspace_param = bool(re.search(r"\b(workspace_id|ws_id)\b", sig))
    has_actor = "actor" in sig
    has_workspace_isolation = has_workspace_in_path or has_workspace_param or has_actor

    has_field_constraints = False
    if body_model:
        model_block_m = re.search(
            r"class\s+" + re.escape(body_model) + r"\s*\(\s*BaseModel\s*\):(.*?)(?=\nclass |\nasync def |\ndef |\Z)",
            content, re.DOTALL,
        )
        if model_block_m:
            block = model_block_m.group(1)
            has_field_constraints = any(tok in block for tok in CONSTRAINT_TOKENS)

    return {
        "file": str(file_path.relative_to(ROOT)).replace("\\", "/"),
        "method": method,
        "path": full_path,
        "function": fn_name,
        "has_body_validation": has_body,
        "body_model": body_model,
        "has_field_constraints": has_field_constraints,
        "has_auth": has_get_actor,
        "has_capability_check": has_require_capability,
        "has_workspace_isolation": has_workspace_isolation,
    }


def main():
    endpoints = []
    files_scanned = 0
    for py_file in sorted(MODULES_DIR.rglob("*.py")):
        if py_file.name == "__init__.py":
            continue
        files_scanned += 1
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in DECORATOR_RE.finditer(content):
            try:
                endpoints.append(parse_endpoint(py_file, content, m))
            except Exception:
                continue

    total = len(endpoints)
    with_validation = sum(1 for e in endpoints if e["has_body_validation"])
    with_field_constraints = sum(1 for e in endpoints if e["has_field_constraints"])
    with_auth = sum(1 for e in endpoints if e["has_auth"])
    with_capability = sum(1 for e in endpoints if e["has_capability_check"])
    with_workspace_iso = sum(1 for e in endpoints if e["has_workspace_isolation"])

    issues = []
    for e in endpoints:
        if e["has_body_validation"] and not e["has_field_constraints"]:
            issues.append({
                "type": "body_without_field_constraints",
                "endpoint": e["method"] + " " + e["path"],
                "function": e["function"],
                "file": e["file"],
                "body_model": e["body_model"],
                "note": "请求体使用 Pydantic 模型但未发现 Field 长度/格式约束",
            })
        if not e["has_auth"]:
            issues.append({
                "type": "missing_auth_dependency",
                "endpoint": e["method"] + " " + e["path"],
                "function": e["function"],
                "file": e["file"],
                "note": "未在签名中发现 Depends(get_actor) 鉴权依赖（可能为公开/回调端点）",
            })
        if not e["has_workspace_isolation"]:
            issues.append({
                "type": "missing_workspace_isolation",
                "endpoint": e["method"] + " " + e["path"],
                "function": e["function"],
                "file": e["file"],
                "note": "未发现 workspace_id 隔离痕迹（可能为公开/全局/跨工作区端点）",
            })
        if not e["has_capability_check"]:
            issues.append({
                "type": "missing_capability_check",
                "endpoint": e["method"] + " " + e["path"],
                "function": e["function"],
                "file": e["file"],
                "note": "签名中未发现 require_capability(...)（部分模块在函数体内调用 _require_capability）",
            })

    without_validation_summary = [
        {
            "endpoint": e["method"] + " " + e["path"],
            "function": e["function"],
            "file": e["file"],
            "reason": "no pydantic body parameter",
        }
        for e in endpoints if not e["has_body_validation"]
    ]

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scan_target": "apps/platform-api/src/workama_platform/modules/*.py",
        "files_scanned": files_scanned,
        "total_endpoints": total,
        "with_validation": with_validation,
        "without_validation": without_validation_summary[:80],
        "metrics": {
            "with_body_validation": with_validation,
            "with_field_constraints_on_body": with_field_constraints,
            "with_auth_get_actor": with_auth,
            "with_capability_check": with_capability,
            "with_workspace_isolation": with_workspace_iso,
        },
        "validation_coverage": {
            "body_validation": "{} / {} ({}%)".format(with_validation, total, with_validation*100//total if total else 0),
            "field_constraints": "{} / {} ({}%)".format(with_field_constraints, total, with_field_constraints*100//total if total else 0),
            "auth_get_actor": "{} / {} ({}%)".format(with_auth, total, with_auth*100//total if total else 0),
            "capability_check": "{} / {} ({}%)".format(with_capability, total, with_capability*100//total if total else 0),
            "workspace_isolation": "{} / {} ({}%)".format(with_workspace_iso, total, with_workspace_iso*100//total if total else 0),
        },
        "issues_count": len(issues),
        "issues_by_type": {
            "body_without_field_constraints": sum(1 for i in issues if i["type"] == "body_without_field_constraints"),
            "missing_auth_dependency": sum(1 for i in issues if i["type"] == "missing_auth_dependency"),
            "missing_workspace_isolation": sum(1 for i in issues if i["type"] == "missing_workspace_isolation"),
            "missing_capability_check": sum(1 for i in issues if i["type"] == "missing_capability_check"),
        },
        "issues": issues[:200],
        "all_endpoints": endpoints,
    }
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("written:", OUT_PATH)
    print(json.dumps(result["metrics"], indent=2))
    print(json.dumps(result["validation_coverage"], indent=2))
    print(json.dumps(result["issues_by_type"], indent=2))


if __name__ == "__main__":
    main()
