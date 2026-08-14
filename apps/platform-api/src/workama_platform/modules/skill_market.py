"""技能市场与 Agent 技能挂载模块 (skill_market).

v7.162: 技能包签名验证 + Agent 挂载执行落地.

提供：
- 技能市场端点 6 个（列表 / 详情 / 安装 / 已安装列表 / 卸载 / 调用日志）
- Agent 技能挂载端点 4 个（列表 / 注册 / 调用 / 注销）
- Manifest 解析（YAML/JSON，校验 required fields）
- 默认 mock 包列表（pending_external 边界）
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import inspect
import json
import logging
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from workama_platform.core import (
    Actor,
    capability_allows,
    get_actor,
    json_dumps,
    new_id,
    pool,
)

LOGGER = logging.getLogger("workama.platform-api.skill_market")

market_router = APIRouter(prefix="/api/v1/skills/market", tags=["skill-market"])
agent_skills_router = APIRouter(prefix="/api/v1/agent/skills", tags=["agent-skills"])

_SKILL_PACKAGE_STATUSES = frozenset({"draft", "published", "archived"})
_SKILL_INSTALL_STATUSES = frozenset({"installed", "error"})

# v7.164 T-M7-007 市场审核工作流状态机
REVIEW_STATES = ("draft", "submitted", "reviewing", "approved", "rejected", "published")
REVIEW_ACTIONS = ("submit", "approve", "reject", "request_changes", "publish", "start_review")
RISK_LEVELS = ("low", "medium", "high", "critical")
# 高风险不允许 auto-approve，必须人工审核
AUTO_APPROVE_BLOCKED_LEVELS = frozenset({"high", "critical"})


# ============================================================================
# Pydantic 模型
# ============================================================================


class SkillPackageCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=2000)
    manifest_url: str | None = Field(default=None, max_length=500)
    author: str | None = Field(default=None, max_length=120)
    tags: list[str] = Field(default_factory=list)
    signature: str | None = Field(default=None, max_length=2000)
    public_key: str | None = Field(default=None, max_length=2000)
    manifest: dict[str, Any] = Field(default_factory=dict)


class SkillPackageUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=2000)
    manifest_url: str | None = Field(default=None, max_length=500)
    author: str | None = Field(default=None, max_length=120)
    tags: list[str] | None = None
    status: Literal["draft", "published", "archived"] | None = None
    signature: str | None = Field(default=None, max_length=2000)
    public_key: str | None = Field(default=None, max_length=2000)
    manifest: dict[str, Any] | None = None


class SkillInstallRequest(BaseModel):
    package_id: str = Field(min_length=1)
    config: dict[str, Any] = Field(default_factory=dict)


class SkillInvokeRequest(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)


class AgentSkillRegister(BaseModel):
    skill_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=120)
    config: dict[str, Any] = Field(default_factory=dict)


# v7.164 T-M7-007 审核工作流请求模型


class ReviewSubmitRequest(BaseModel):
    notes: str = Field(default="", max_length=2000)


class ReviewActionRequest(BaseModel):
    action: Literal["approve", "reject", "request_changes"]
    review_notes: str = Field(min_length=1, max_length=2000)


class PublishRequest(BaseModel):
    notes: str = Field(default="", max_length=2000)


# ============================================================================
# 辅助函数
# ============================================================================


def _require(actor: Actor, action: str) -> None:
    required = f"skill_market:{action}"
    if capability_allows(actor.capabilities, required):
        return
    if action == "read" and actor.role in {"owner", "admin", "member", "viewer"}:
        return
    if action in ("write", "install") and actor.role in {"owner", "admin", "member"}:
        return
    raise HTTPException(status_code=403, detail=f"Missing capability: {required}")


def _require_admin(actor: Actor) -> None:
    """审核/发布端点要求 admin/owner 或显式 skill_market:admin 能力。"""
    if capability_allows(actor.capabilities, "skill_market:admin") or capability_allows(actor.capabilities, "skill_market:*"):
        return
    if actor.actor_type == "user" and actor.role in {"owner", "admin"}:
        return
    raise HTTPException(status_code=403, detail="Missing capability: skill_market:admin")


def _infer_runtime_from_manifest_url(manifest_url: str) -> str:
    """从 manifest_url 推断 runtime 类型用于风险评分。"""
    url = (manifest_url or "").strip()
    if url.startswith("mock://"):
        return "mock"
    if url.startswith("local://"):
        return "python"
    return "local_http"


def _compute_market_risk_score(package: dict[str, Any]) -> tuple[int, str]:
    """对市场包计算风险评分 (score, level)。

    公式：permissions_count * 10 + (runtime==local_http ? 20 : 0)
         + (entrypoint/handler 含 'eval'/'exec' ? 50 : 0)
    分级：low (<20) / medium (20-50) / high (>50)
    """
    manifest = package.get("manifest") or {}
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except json.JSONDecodeError:
            manifest = {}
    permissions = manifest.get("permissions") or []
    if not isinstance(permissions, list):
        permissions = []
    entrypoint = str(manifest.get("entrypoint") or manifest.get("handler") or "")
    runtime = _infer_runtime_from_manifest_url(package.get("manifest_url") or "")
    score = len(permissions) * 10
    if runtime == "local_http":
        score += 20
    if "eval" in entrypoint or "exec" in entrypoint:
        score += 50
    if score < 20:
        level = "low"
    elif score <= 50:
        level = "medium"
    else:
        level = "high"
    return score, level


def _package_review_view(row: dict[str, Any]) -> dict[str, Any]:
    """市场包审核视图（含 review_status / risk_score / risk_level）。"""
    base = _package_summary(row)
    base.update(
        {
            "review_status": row.get("review_status") or "draft",
            "risk_score": int(row.get("risk_score") or 0),
            "risk_level": row.get("risk_level") or "low",
            "content_hash": row.get("content_hash") or "",
            "license": row.get("license") or "",
            "runtime": row.get("runtime") or "python",
            "publisher_id": row.get("publisher_id") or "",
            "reviewed_by": row.get("reviewed_by") or "",
            "reviewed_at": row.get("reviewed_at"),
            "review_notes": row.get("review_notes") or "",
        }
    )
    return base


def _review_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "skill_id": row["skill_id"],
        "workspace_id": row.get("workspace_id") or "",
        "reviewer_id": row.get("reviewer_id") or "",
        "action": row["action"],
        "notes": row.get("notes") or "",
        "risk_score": row.get("risk_score"),
        "risk_level": row.get("risk_level"),
        "created_at": row.get("created_at"),
    }


async def _insert_review_record(
    conn,
    *,
    skill_id: str,
    workspace_id: str,
    reviewer_id: str | None,
    action: str,
    notes: str,
    risk_score: int | None = None,
    risk_level: str | None = None,
) -> str:
    review_id = new_id("skrev")
    await conn.execute(
        """
        INSERT INTO ag_skill_review(
            id, skill_id, workspace_id, reviewer_id, action, notes, risk_score, risk_level
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (review_id, skill_id, workspace_id, reviewer_id, action, notes, risk_score, risk_level),
    )
    return review_id


def _package_summary(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "version": row["version"],
        "description": row.get("description") or "",
        "manifest_url": row.get("manifest_url") or "",
        "author": row.get("author") or "",
        "tags": list(row.get("tags") or []),
        "downloads": int(row.get("downloads") or 0),
        "rating": float(row.get("rating") or 0.0),
        "status": row["status"],
        "signature": row.get("signature") or "",
        "public_key": row.get("public_key") or "",
        "public_key_hash": row.get("public_key_hash") or "",
        "verified_at": row.get("verified_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _install_summary(row: dict) -> dict:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "package_id": row["package_id"],
        "installed_version": row["installed_version"],
        "config": row.get("config") or {},
        "status": row["status"],
        "installed_at": row.get("installed_at"),
        "updated_at": row.get("updated_at"),
    }


def _log_summary(row: dict) -> dict:
    return {
        "id": row["id"],
        "install_id": row["install_id"],
        "input": row.get("input") or {},
        "output": row.get("output") or {},
        "tokens_used": int(row.get("tokens_used") or 0),
        "duration_ms": int(row.get("duration_ms") or 0),
        "created_at": row.get("created_at"),
    }


# ============================================================================
# 签名验证
# ============================================================================


def verify_skill_signature(manifest: dict[str, Any], signature: str, public_key: str) -> bool:
    """验证技能包签名。

    对 manifest JSON（canonical 排序后）计算 SHA-256，再用 Ed25519 验证 signature。
    空 signature / public_key 视为未签名，返回 False（mock:// / local:// 应在调用方跳过）。
    """
    if not signature or not public_key:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature

        pub_bytes = base64.b64decode(public_key)
        pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
        sig_bytes = base64.b64decode(signature)
        canonical = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).digest()
        pub_key.verify(sig_bytes, digest)
        return True
    except InvalidSignature:
        return False
    except Exception:
        LOGGER.exception("Signature verification error")
        return False


# ============================================================================
# Manifest 解析
# ============================================================================


def parse_manifest(raw: str | bytes) -> dict[str, Any]:
    """解析 YAML 或 JSON manifest，返回 dict。"""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    text = raw.strip()
    if not text:
        raise ValueError("manifest is empty")
    if text.startswith(("{", "[")):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON manifest: {exc}") from exc
    else:
        try:
            data = yaml.safe_load(text)
        except Exception as exc:
            raise ValueError(f"invalid YAML manifest: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("manifest must be an object")
    return data


def validate_manifest(data: dict[str, Any]) -> dict[str, Any]:
    """校验 manifest 必要字段，返回规范化后的 dict。"""
    required = {"name", "version", "entrypoint", "tools"}
    missing = required - set(data.keys())
    if missing:
        raise ValueError(f"manifest missing required fields: {sorted(missing)}")
    name = str(data["name"]).strip()
    version = str(data["version"]).strip()
    entrypoint = str(data["entrypoint"]).strip()
    tools = data["tools"]
    if not name or not version or not entrypoint:
        raise ValueError("manifest name/version/entrypoint must be non-empty")
    if not isinstance(tools, list):
        raise ValueError("manifest tools must be a list")
    for t in tools:
        if not isinstance(t, dict):
            raise ValueError("each tool must be an object")
        if "name" not in t:
            raise ValueError("each tool must have a name")
    return {
        "name": name,
        "version": version,
        "entrypoint": entrypoint,
        "tools": tools,
        "description": str(data.get("description", "")),
        "parameters": data.get("parameters") or {},
    }


# ============================================================================
# Mock 包列表（pending_external 边界）
# ============================================================================


def _mock_packages() -> list[dict[str, Any]]:
    """默认 mock 技能包列表，不依赖外部市场服务。"""
    return [
        {
            "id": "pkg_mock_echo",
            "name": "echo",
            "version": "1.0.0",
            "description": "Mock echo skill for testing",
            "manifest_url": "mock://skill/echo/1.0.0",
            "author": "workama",
            "tags": ["utility", "mock"],
            "downloads": 42,
            "rating": 4.5,
            "status": "published",
            "manifest": {
                "name": "echo",
                "version": "1.0.0",
                "handler": "workama_platform.modules.skill_market:_mock_echo_handler",
                "input_schema": {"type": "object", "properties": {"message": {"type": "string"}}},
                "permissions": [],
            },
            "signature": "",
            "public_key": "",
        },
        {
            "id": "pkg_mock_math",
            "name": "math",
            "version": "1.0.0",
            "description": "Mock math skill for testing",
            "manifest_url": "mock://skill/math/1.0.0",
            "author": "workama",
            "tags": ["utility", "math"],
            "downloads": 30,
            "rating": 4.0,
            "status": "published",
            "manifest": {
                "name": "math",
                "version": "1.0.0",
                "handler": "workama_platform.modules.skill_market:_mock_math_handler",
                "input_schema": {"type": "object", "properties": {"expression": {"type": "string"}}},
                "permissions": [],
            },
            "signature": "",
            "public_key": "",
        },
        {
            "id": "pkg_mock_search",
            "name": "search",
            "version": "2.1.0",
            "description": "Mock search skill for testing",
            "manifest_url": "mock://skill/search/2.1.0",
            "author": "workama",
            "tags": ["search", "external"],
            "downloads": 100,
            "rating": 4.2,
            "status": "published",
            "manifest": {
                "name": "search",
                "version": "2.1.0",
                "handler": "workama_platform.modules.skill_market:_mock_search_handler",
                "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
                "permissions": ["skill:write"],
            },
            "signature": "",
            "public_key": "",
        },
    ]


def _mock_echo_handler(arguments: dict, actor: Actor, context: dict) -> dict:
    return {"result": arguments.get("message", ""), "handler": "echo"}


def _mock_math_handler(arguments: dict, actor: Actor, context: dict) -> dict:
    return {"result": f"eval({arguments.get('expression', '')})", "handler": "math"}


def _mock_search_handler(arguments: dict, actor: Actor, context: dict) -> dict:
    return {"result": f"search({arguments.get('query', '')})", "handler": "search"}


# ============================================================================
# 输入校验
# ============================================================================


def _validate_input_schema(value: Any, schema: dict[str, Any]) -> None:
    """极简 JSON Schema 校验（仅覆盖基础类型与 required）。"""
    if not isinstance(schema, dict):
        return
    schema_type = schema.get("type")
    if schema_type == "object":
        if not isinstance(value, dict):
            raise ValueError("expected object")
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"missing required field: {key}")
        for key, prop_schema in schema.get("properties", {}).items():
            if key in value:
                _validate_input_schema(value[key], prop_schema)
    elif schema_type == "array":
        if not isinstance(value, list):
            raise ValueError("expected array")
        items_schema = schema.get("items")
        if items_schema:
            for item in value:
                _validate_input_schema(item, items_schema)
    elif schema_type == "string":
        if not isinstance(value, str):
            raise ValueError("expected string")
    elif schema_type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("expected integer")
    elif schema_type == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("expected number")
    elif schema_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError("expected boolean")


# ============================================================================
# 市场端点
# ============================================================================


@market_router.get("")
async def list_market_packages(
    actor: Annotated[Actor, Depends(get_actor)],
    q: str | None = Query(default=None, description="搜索关键词"),
    tag: str | None = Query(default=None, description="标签过滤"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """技能市场列表：搜索 / 标签过滤 / 分页。"""
    _require(actor, "read")
    # 默认 mock 模式：返回内置 mock 列表，不查外部市场
    packages = _mock_packages()
    if q:
        packages = [p for p in packages if q.lower() in p["name"].lower() or q.lower() in p["description"].lower()]
    if tag:
        packages = [p for p in packages if tag.lower() in [t.lower() for t in p["tags"]]]
    total = len(packages)
    items = packages[offset:offset + limit]
    return {
        "items": items,
        "data": items,
        "count": len(items),
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@market_router.post("", status_code=status.HTTP_201_CREATED)
async def create_market_package(
    body: SkillPackageCreate,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """创建技能包（支持可选签名）。"""
    _require(actor, "write")
    package_id = new_id("pkg")
    now = datetime.now(UTC)

    manifest = body.manifest or {}
    signature = body.signature or ""
    public_key = body.public_key or ""
    public_key_hash = hashlib.sha256(public_key.encode()).hexdigest() if public_key else ""

    verified_at = None
    if signature and public_key and manifest:
        if verify_skill_signature(manifest, signature, public_key):
            verified_at = now

    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO skill_package(
                id, name, version, description, manifest_url, author, tags,
                downloads, rating, status, manifest, signature, public_key,
                public_key_hash, verified_at, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
            """,
            (
                package_id,
                body.name,
                body.version,
                body.description or "",
                body.manifest_url or "",
                body.author or "",
                body.tags or [],
                0,
                0.0,
                "draft",
                json_dumps(manifest),
                signature,
                public_key,
                public_key_hash,
                verified_at,
                now,
                now,
            ),
        )

    return {
        "package": {
            "id": package_id,
            "name": body.name,
            "version": body.version,
            "description": body.description or "",
            "manifest_url": body.manifest_url or "",
            "author": body.author or "",
            "tags": body.tags or [],
            "downloads": 0,
            "rating": 0.0,
            "status": "draft",
            "signature": signature,
            "public_key": public_key,
            "public_key_hash": public_key_hash,
            "verified_at": verified_at.isoformat() if verified_at else None,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
    }


@market_router.patch("/{package_id}")
async def update_market_package(
    package_id: str,
    body: SkillPackageUpdate,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """更新技能包（支持签名字段更新与重新验证）。"""
    _require(actor, "write")

    fields: list[str] = []
    values: list[Any] = []

    if body.description is not None:
        fields.append("description = %s")
        values.append(body.description)
    if body.manifest_url is not None:
        fields.append("manifest_url = %s")
        values.append(body.manifest_url)
    if body.author is not None:
        fields.append("author = %s")
        values.append(body.author)
    if body.tags is not None:
        fields.append("tags = %s")
        values.append(body.tags)
    if body.status is not None:
        fields.append("status = %s")
        values.append(body.status)
    if body.signature is not None:
        fields.append("signature = %s")
        values.append(body.signature)
    if body.public_key is not None:
        fields.append("public_key = %s")
        values.append(body.public_key)
        fields.append("public_key_hash = %s")
        values.append(hashlib.sha256(body.public_key.encode()).hexdigest() if body.public_key else "")
    if body.manifest is not None:
        fields.append("manifest = %s::jsonb")
        values.append(json_dumps(body.manifest))

    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM skill_package WHERE id = %s",
            (package_id,),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Package not found")

        # 若签名相关字段有变更，重新验证
        if body.signature is not None or body.public_key is not None or body.manifest is not None:
            manifest = body.manifest if body.manifest is not None else (row.get("manifest") or {})
            if isinstance(manifest, str):
                manifest = json.loads(manifest)
            signature = body.signature if body.signature is not None else (row.get("signature") or "")
            public_key = body.public_key if body.public_key is not None else (row.get("public_key") or "")
            if signature and public_key and manifest:
                if verify_skill_signature(manifest, signature, public_key):
                    fields.append("verified_at = %s")
                    values.append(datetime.now(UTC))
                else:
                    fields.append("verified_at = %s")
                    values.append(None)
            else:
                fields.append("verified_at = %s")
                values.append(None)

        fields.append("updated_at = %s")
        values.append(datetime.now(UTC))
        values.append(package_id)

        await conn.execute(
            f"UPDATE skill_package SET {', '.join(fields)} WHERE id = %s",
            tuple(values),
        )

        result = await conn.execute(
            "SELECT * FROM skill_package WHERE id = %s",
            (package_id,),
        )
        row = await result.fetchone()

    return {"package": _package_summary(row)}


@market_router.get("/installed")
async def list_installed_packages(
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """已安装技能包列表。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT * FROM skill_install
            WHERE workspace_id = %s
            ORDER BY installed_at DESC LIMIT %s OFFSET %s
            """,
            (actor.workspace_id, limit, offset),
        )
        rows = await result.fetchall()
    items = [_install_summary(row) for row in rows]
    return {"items": items, "data": items, "count": len(items), "limit": limit, "offset": offset}


@market_router.get("/{package_id}")
async def get_market_package(
    package_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """技能包详情。"""
    _require(actor, "read")
    for pkg in _mock_packages():
        if pkg["id"] == package_id:
            return {"package": pkg}
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM skill_package WHERE id = %s",
            (package_id,),
        )
        row = await result.fetchone()
        if row:
            return {"package": _package_summary(row)}
    raise HTTPException(status_code=404, detail="Package not found")


@market_router.post("/{package_id}/install", status_code=status.HTTP_201_CREATED)
async def install_market_package(
    package_id: str,
    body: SkillInstallRequest,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """安装技能包到工作区。"""
    _require(actor, "install")
    pkg = None

    # 先从 DB 查询真实包
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM skill_package WHERE id = %s",
            (package_id,),
        )
        row = await result.fetchone()
        if row:
            pkg = dict(row)

    # 回退到 mock 包
    if not pkg:
        for p in _mock_packages():
            if p["id"] == package_id:
                pkg = p
                break
    if not pkg:
        raise HTTPException(status_code=404, detail="Package not found")

    manifest_url = pkg.get("manifest_url") or ""
    manifest = pkg.get("manifest") or {}
    signature = pkg.get("signature") or ""
    public_key = pkg.get("public_key") or ""

    # 签名验证：非受控路径且提供了 public_key 时执行
    if not manifest_url.startswith(("mock://", "local://")) and public_key:
        if not verify_skill_signature(manifest, signature, public_key):
            LOGGER.warning(
                "Skill package signature verification failed: package_id=%s", package_id
            )
            raise HTTPException(status_code=400, detail="Signature verification failed")

    install_id = new_id("skinst")
    async with pool.connection() as conn:
        async with conn.transaction():
            # 幂等：已安装则返回已有记录
            result = await conn.execute(
                "SELECT * FROM skill_install WHERE workspace_id=%s AND package_id=%s",
                (actor.workspace_id, package_id),
            )
            existing = await result.fetchone()
            if existing:
                return {"install": _install_summary(existing), "deduplicated": True}

            await conn.execute(
                """
                INSERT INTO skill_install(
                    id, workspace_id, package_id, installed_version, config, status, enabled
                ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                """,
                (
                    install_id,
                    actor.workspace_id,
                    package_id,
                    pkg["version"],
                    json_dumps(body.config),
                    "installed",
                    True,
                ),
            )
    return {
        "install": {
            "id": install_id,
            "workspace_id": actor.workspace_id,
            "package_id": package_id,
            "installed_version": pkg["version"],
            "config": body.config,
            "status": "installed",
            "enabled": True,
        },
        "deduplicated": False,
    }


@market_router.delete("/{install_id}")
async def uninstall_package(
    install_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """卸载技能包。"""
    _require(actor, "install")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                "SELECT * FROM skill_install WHERE id = %s AND workspace_id = %s",
                (install_id, actor.workspace_id),
            )
            row = await result.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Install record not found")
            await conn.execute(
                "DELETE FROM skill_install WHERE id = %s",
                (install_id,),
            )
    return {"id": install_id, "deleted": True}


@market_router.get("/{install_id}/logs")
async def list_invocation_logs(
    install_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """调用日志列表。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM skill_install WHERE id = %s AND workspace_id = %s",
            (install_id, actor.workspace_id),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Install record not found")
        result = await conn.execute(
            """
            SELECT * FROM skill_invocation_log
            WHERE install_id = %s
            ORDER BY created_at DESC LIMIT %s OFFSET %s
            """,
            (install_id, limit, offset),
        )
        rows = await result.fetchall()
    items = [_log_summary(row) for row in rows]
    return {"items": items, "data": items, "count": len(items), "limit": limit, "offset": offset}


# ============================================================================
# Agent 技能挂载端点
# ============================================================================


@agent_skills_router.get("")
async def list_agent_skills(
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """获取 Agent 可用技能列表（已安装且状态正常的技能）。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT i.*, p.name as package_name, p.manifest as package_manifest
            FROM skill_install i
            JOIN skill_package p ON p.id = i.package_id
            WHERE i.workspace_id = %s AND i.status = 'installed'
            ORDER BY i.updated_at DESC LIMIT %s OFFSET %s
            """,
            (actor.workspace_id, limit, offset),
        )
        rows = await result.fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = _install_summary(row)
        item["package_name"] = row.get("package_name") or ""
        item["package_manifest"] = row.get("package_manifest") or {}
        items.append(item)
    return {"items": items, "data": items, "count": len(items), "limit": limit, "offset": offset}


@agent_skills_router.post("", status_code=status.HTTP_201_CREATED)
async def register_agent_skill(
    body: AgentSkillRegister,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """注册技能到 Agent（基于已安装记录创建运行时绑定）。"""
    _require(actor, "write")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM skill_install WHERE workspace_id = %s AND package_id = %s",
            (actor.workspace_id, body.skill_id),
        )
        install = await result.fetchone()
        if not install:
            raise HTTPException(status_code=404, detail="Skill not installed in workspace")
    return {
        "registered": True,
        "skill_id": body.skill_id,
        "name": body.name,
        "config": body.config,
        "install_id": install["id"],
    }


@agent_skills_router.post("/{skill_id}/invoke")
async def invoke_agent_skill(
    skill_id: str,
    body: SkillInvokeRequest,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """调用 Agent 挂载的技能（支持 mock 路径与真实 handler 调用）。"""
    _require(actor, "write")
    started = time.monotonic()

    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT i.*, p.manifest as package_manifest, p.manifest_url
            FROM skill_install i
            JOIN skill_package p ON p.id = i.package_id
            WHERE i.workspace_id = %s AND i.package_id = %s AND i.status = 'installed'
            """,
            (actor.workspace_id, skill_id),
        )
        install = await result.fetchone()
        if not install:
            raise HTTPException(status_code=404, detail="Skill not installed or not available")

        # 校验技能已 enable
        if not install.get("enabled", True):
            raise HTTPException(status_code=403, detail="Skill is disabled")

        manifest = install.get("package_manifest") or {}
        manifest_url = install.get("manifest_url") or ""

        # mock:// 受控路径返回确定性结果
        if manifest_url.startswith("mock://"):
            output = {
                "result": f"[mock-skill] skill={skill_id} input_keys={list(body.input.keys())}",
                "method": "mock",
            }
            duration_ms = int((time.monotonic() - started) * 1000)
            await _insert_invocation_log(conn, install, body.input, output, 0, duration_ms)
            return {
                "output": output,
                "duration_ms": duration_ms,
                "tokens_used": 0,
                "install_id": install["id"],
            }

        # 真实 handler 调用
        handler_path = manifest.get("handler")
        if not handler_path or not isinstance(handler_path, str):
            raise HTTPException(status_code=500, detail="Skill manifest has no handler")

        # Actor 能力校验
        required_caps = manifest.get("permissions") or []
        for cap in required_caps:
            if not capability_allows(actor.capabilities, cap):
                raise HTTPException(status_code=403, detail=f"Missing capability: {cap}")

        # 参数通过 input_schema 校验
        input_schema = manifest.get("input_schema")
        if input_schema:
            try:
                _validate_input_schema(body.input, input_schema)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"Input validation failed: {exc}") from exc

        # 动态解析 handler
        if ":" not in handler_path:
            raise HTTPException(status_code=500, detail="Handler format must be module.path:function_name")
        module_path, func_name = handler_path.split(":", 1)

        try:
            module = importlib.import_module(module_path)
            handler = getattr(module, func_name)
        except Exception as exc:
            LOGGER.exception("Handler resolution failed: skill_id=%s handler=%s", skill_id, handler_path)
            raise HTTPException(status_code=500, detail=f"Handler resolution failed: {exc}") from exc

        context = {
            "workspace_id": actor.workspace_id,
            "skill_id": skill_id,
            "install_id": install["id"],
        }

        try:
            if inspect.iscoroutinefunction(handler):
                output = await asyncio.wait_for(handler(body.input, actor, context), timeout=30.0)
            else:
                loop = asyncio.get_event_loop()
                output = await asyncio.wait_for(
                    loop.run_in_executor(None, handler, body.input, actor, context),
                    timeout=30.0,
                )
            if not isinstance(output, dict):
                output = {"result": output}
        except asyncio.TimeoutError:
            duration_ms = int((time.monotonic() - started) * 1000)
            LOGGER.error("Skill handler timed out: skill_id=%s duration_ms=%s", skill_id, duration_ms)
            await _insert_invocation_log(
                conn, install, body.input,
                {"error": "timeout", "handler": handler_path},
                0, duration_ms,
            )
            raise HTTPException(status_code=500, detail="Skill execution timed out")
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            LOGGER.exception("Skill handler failed: skill_id=%s", skill_id)
            await _insert_invocation_log(
                conn, install, body.input,
                {"error": str(exc), "handler": handler_path},
                0, duration_ms,
            )
            raise HTTPException(status_code=500, detail=f"Skill execution failed: {exc}") from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        await _insert_invocation_log(conn, install, body.input, output, 0, duration_ms)

    return {
        "output": output,
        "duration_ms": duration_ms,
        "tokens_used": 0,
        "install_id": install["id"],
    }


async def _insert_invocation_log(
    conn,
    install: dict[str, Any],
    input_data: dict[str, Any],
    output_data: dict[str, Any],
    tokens_used: int,
    duration_ms: int,
) -> None:
    await conn.execute(
        """
        INSERT INTO skill_invocation_log(
            id, install_id, input, output, tokens_used, duration_ms
        ) VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s)
        """,
        (
            new_id("sklog"),
            install["id"],
            json_dumps(input_data),
            json_dumps(output_data),
            tokens_used,
            duration_ms,
        ),
    )


@agent_skills_router.delete("/{skill_id}")
async def unregister_agent_skill(
    skill_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """注销 Agent 技能（移除运行时绑定，保留安装记录）。"""
    _require(actor, "write")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM skill_install WHERE workspace_id = %s AND package_id = %s",
            (actor.workspace_id, skill_id),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Skill not found in workspace")
    return {"skill_id": skill_id, "unregistered": True}


# ============================================================================
# v7.164 T-M7-007 市场审核工作流端点
# 状态机：draft → submitted → reviewing → approved/rejected → published
# ============================================================================


@market_router.post("/{skill_id}/submit")
async def submit_for_review(
    skill_id: str,
    body: ReviewSubmitRequest,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """POST /api/v1/skills/market/{skill_id}/submit — 提交审核（owner/write）。

    状态流转：draft → submitted（高风险）或 draft → approved（低/中风险 auto-approve）。
    高风险技能不允许 auto-approve，必须进入 submitted 等待人工审核。
    """
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                "SELECT * FROM skill_package WHERE id = %s FOR UPDATE",
                (skill_id,),
            )
            pkg = await result.fetchone()
            if not pkg:
                raise HTTPException(status_code=404, detail="Package not found")
            current_status = pkg.get("review_status") or "draft"
            if current_status != "draft":
                raise HTTPException(
                    status_code=409,
                    detail=f"Package must be in draft state to submit (current: {current_status})",
                )
            risk_score, risk_level = _compute_market_risk_score(pkg)
            notes = (body.notes or "").strip()
            # 高风险必须人工审核；低/中风险允许 auto-approve
            if risk_level in AUTO_APPROVE_BLOCKED_LEVELS:
                new_status = "submitted"
                auto_approved = False
                action = "submit"
            else:
                new_status = "approved"
                auto_approved = True
                action = "approve"
                if not notes:
                    notes = f"auto-approved: risk_level={risk_level}, score={risk_score}"
            await conn.execute(
                """
                UPDATE skill_package
                SET review_status=%s, risk_score=%s, risk_level=%s,
                    publisher_id=COALESCE(publisher_id, %s),
                    reviewed_at=CASE WHEN %s THEN now() ELSE reviewed_at END,
                    reviewed_by=CASE WHEN %s THEN %s ELSE reviewed_by END,
                    review_notes=%s, updated_at=now()
                WHERE id=%s
                """,
                (
                    new_status, risk_score, risk_level,
                    actor.user_id,
                    auto_approved, auto_approved, actor.user_id,
                    notes, skill_id,
                ),
            )
            await _insert_review_record(
                conn,
                skill_id=skill_id,
                workspace_id=actor.workspace_id,
                reviewer_id=actor.user_id if auto_approved else None,
                action=action,
                notes=notes,
                risk_score=risk_score,
                risk_level=risk_level,
            )
            result = await conn.execute(
                "SELECT * FROM skill_package WHERE id = %s",
                (skill_id,),
            )
            pkg = await result.fetchone()
    return {
        "package": _package_review_view(pkg),
        "auto_approved": auto_approved,
        "risk_score": risk_score,
        "risk_level": risk_level,
    }


@market_router.post("/{skill_id}/review")
async def review_market_package(
    skill_id: str,
    body: ReviewActionRequest,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """POST /api/v1/skills/market/{skill_id}/review — 审核操作（admin）。

    action: approve / reject / request_changes（必填 review_notes）。
    状态流转：
      - approve: submitted/reviewing → approved
      - reject:  submitted/reviewing → rejected
      - request_changes: submitted/reviewing → reviewing（等待作者修改）
    """
    _require_admin(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                "SELECT * FROM skill_package WHERE id = %s FOR UPDATE",
                (skill_id,),
            )
            pkg = await result.fetchone()
            if not pkg:
                raise HTTPException(status_code=404, detail="Package not found")
            current_status = pkg.get("review_status") or "draft"
            if current_status not in {"submitted", "reviewing"}:
                raise HTTPException(
                    status_code=409,
                    detail=f"Package must be in submitted/reviewing state to review (current: {current_status})",
                )
            risk_score, risk_level = _compute_market_risk_score(pkg)
            action = body.action
            if action == "approve":
                new_status = "approved"
            elif action == "reject":
                new_status = "rejected"
            else:
                new_status = "reviewing"
            await conn.execute(
                """
                UPDATE skill_package
                SET review_status=%s, risk_score=%s, risk_level=%s,
                    reviewed_by=%s, reviewed_at=now(),
                    review_notes=%s, updated_at=now()
                WHERE id=%s
                """,
                (
                    new_status, risk_score, risk_level,
                    actor.user_id, body.review_notes.strip(), skill_id,
                ),
            )
            await _insert_review_record(
                conn,
                skill_id=skill_id,
                workspace_id=actor.workspace_id,
                reviewer_id=actor.user_id,
                action=action,
                notes=body.review_notes.strip(),
                risk_score=risk_score,
                risk_level=risk_level,
            )
            result = await conn.execute(
                "SELECT * FROM skill_package WHERE id = %s",
                (skill_id,),
            )
            pkg = await result.fetchone()
    return {
        "package": _package_review_view(pkg),
        "action": action,
        "risk_score": risk_score,
        "risk_level": risk_level,
    }


@market_router.post("/{skill_id}/publish")
async def publish_market_package(
    skill_id: str,
    body: PublishRequest,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """POST /api/v1/skills/market/{skill_id}/publish — 发布（admin，仅 approved 可发布）。"""
    _require_admin(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                "SELECT * FROM skill_package WHERE id = %s FOR UPDATE",
                (skill_id,),
            )
            pkg = await result.fetchone()
            if not pkg:
                raise HTTPException(status_code=404, detail="Package not found")
            current_status = pkg.get("review_status") or "draft"
            if current_status != "approved":
                raise HTTPException(
                    status_code=409,
                    detail=f"Package must be approved before publishing (current: {current_status})",
                )
            await conn.execute(
                """
                UPDATE skill_package
                SET review_status='published', status='published',
                    reviewed_by=%s, review_notes=COALESCE(NULLIF(%s, ''), review_notes),
                    updated_at=now()
                WHERE id=%s
                """,
                (actor.user_id, (body.notes or "").strip(), skill_id),
            )
            await _insert_review_record(
                conn,
                skill_id=skill_id,
                workspace_id=actor.workspace_id,
                reviewer_id=actor.user_id,
                action="publish",
                notes=(body.notes or "").strip(),
            )
            result = await conn.execute(
                "SELECT * FROM skill_package WHERE id = %s",
                (skill_id,),
            )
            pkg = await result.fetchone()
    return {"package": _package_review_view(pkg), "published": True}


@market_router.get("/{skill_id}/review-history")
async def get_review_history(
    skill_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """GET /api/v1/skills/market/{skill_id}/review-history — 审核历史（分页）。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        # 先确认包存在
        result = await conn.execute(
            "SELECT id FROM skill_package WHERE id = %s",
            (skill_id,),
        )
        if not await result.fetchone():
            raise HTTPException(status_code=404, detail="Package not found")
        result = await conn.execute(
            """
            SELECT * FROM ag_skill_review
            WHERE skill_id = %s
            ORDER BY created_at DESC LIMIT %s OFFSET %s
            """,
            (skill_id, limit, offset),
        )
        rows = await result.fetchall()
        result = await conn.execute(
            "SELECT count(*) AS total FROM ag_skill_review WHERE skill_id = %s",
            (skill_id,),
        )
        count_row = await result.fetchone()
    items = [_review_summary(row) for row in rows]
    total = int((count_row or {}).get("total") or 0)
    return {
        "items": items,
        "data": items,
        "count": len(items),
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@market_router.get("/pending-reviews")
async def list_pending_reviews(
    actor: Annotated[Actor, Depends(get_actor)],
    status: Literal["submitted", "reviewing"] | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """GET /api/v1/skills/market/pending-reviews — 待审核列表（admin，分页+status 过滤）。"""
    _require_admin(actor)
    predicates: list[str] = ["review_status IN ('submitted', 'reviewing')"]
    params: list[Any] = []
    if status is not None:
        predicates.append("review_status = %s")
        params.append(status)
    where_clause = " AND ".join(predicates)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT * FROM skill_package
            WHERE {where_clause}
            ORDER BY updated_at ASC LIMIT %s OFFSET %s
            """,
            tuple(params + [limit, offset]),
        )
        rows = await result.fetchall()
        result = await conn.execute(
            f"SELECT count(*) AS total FROM skill_package WHERE {where_clause}",
            tuple(params),
        )
        count_row = await result.fetchone()
    items = [_package_review_view(row) for row in rows]
    total = int((count_row or {}).get("total") or 0)
    return {
        "items": items,
        "data": items,
        "count": len(items),
        "total": total,
        "limit": limit,
        "offset": offset,
    }
