from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator

from workama_platform.core import Actor, capability_allows, get_actor, json_dumps, new_id, pool, require_internal
from workama_platform.modules.security.service import evaluate_prompt


router = APIRouter(prefix="/api/v1/gateway", tags=["gateway-prompts"])
internal_router = APIRouter(prefix="/internal/gateway", tags=["gateway-prompts-internal"])

_VARIABLE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_.-]{0,63})\s*}}")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,99}$")
_ROLLOUT_KEY_VARIABLE = "__wama_rollout_key"
_ROLLOUT_SCHEMA_LOCK = asyncio.Lock()
_ROLLOUT_SCHEMA_READY = False

# 允许的灰度比例枚举（业务层校验，数据库层允许 0–100）
_ROLLOUT_PERCENTS: frozenset[int] = frozenset({10, 25, 50, 100})
# 评测门禁默认阈值（mean score = passed_cases / total_cases）
_DEFAULT_EVAL_THRESHOLD = 0.7

# ============================================================================
# SCHEMA_STATEMENTS：T-M1-007 完整 Prompt Registry 迁移登记
# 与 deploy/compose/postgres/075_prompt_registry_full.sql 保持一致，
# 供 _ensure_rollout_schema 在未迁移环境上 best-effort 应用。
# ============================================================================
SCHEMA_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE sec_prompt_version ADD COLUMN IF NOT EXISTS description TEXT",
    "ALTER TABLE sec_prompt_version ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '[]'::jsonb",
    "ALTER TABLE sec_prompt_version ADD COLUMN IF NOT EXISTS template_variables JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE sec_prompt_version ADD COLUMN IF NOT EXISTS model_hint TEXT",
    "ALTER TABLE sec_prompt_version ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ",
    "ALTER TABLE sec_prompt_version ADD COLUMN IF NOT EXISTS parent_version_id TEXT",
    "ALTER TABLE sec_prompt_version DROP CONSTRAINT IF EXISTS sec_prompt_version_status_check",
    "ALTER TABLE sec_prompt_version ADD CONSTRAINT sec_prompt_version_status_check "
    "CHECK (status IN ('draft', 'published', 'archived', 'deleted'))",
    """
    CREATE TABLE IF NOT EXISTS pf_prompt_rollout (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
        prompt_id TEXT NOT NULL REFERENCES sec_prompt_version(id) ON DELETE CASCADE,
        percent INTEGER NOT NULL DEFAULT 100 CHECK (percent BETWEEN 0 AND 100),
        strategy TEXT NOT NULL DEFAULT 'stable_sha256' CHECK (strategy IN ('stable_sha256', 'all')),
        updated_by TEXT REFERENCES id_user(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(workspace_id, prompt_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pf_prompt_rollout_workspace ON pf_prompt_rollout(workspace_id, prompt_id)",
    "CREATE INDEX IF NOT EXISTS idx_sec_prompt_version_workspace_status "
    "ON sec_prompt_version(workspace_id, status, name, version DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sec_prompt_version_workspace_name "
    "ON sec_prompt_version(workspace_id, name, version DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sec_prompt_version_tags ON sec_prompt_version USING GIN (tags)",
)


async def _ensure_rollout_schema() -> None:
    """Apply the additive rollout fields when a deployment has not run migrations yet."""
    global _ROLLOUT_SCHEMA_READY
    if _ROLLOUT_SCHEMA_READY:
        return
    async with _ROLLOUT_SCHEMA_LOCK:
        if _ROLLOUT_SCHEMA_READY:
            return
        async with pool.connection() as conn:
            async with conn.transaction():
                # v7.24 rollout_percent 基线
                await conn.execute(
                    "ALTER TABLE sec_prompt_version ADD COLUMN IF NOT EXISTS rollout_percent INTEGER NOT NULL DEFAULT 0"
                )
                await conn.execute("ALTER TABLE sec_prompt_version DROP CONSTRAINT IF EXISTS sec_prompt_version_rollout_percent_check")
                await conn.execute(
                    "ALTER TABLE sec_prompt_version ADD CONSTRAINT sec_prompt_version_rollout_percent_check CHECK (rollout_percent BETWEEN 0 AND 100)"
                )
                await conn.execute(
                    "UPDATE sec_prompt_version SET rollout_percent=100 WHERE status='published' AND rollout_percent=0"
                )
                await conn.execute(
                    """
                    WITH ranked AS (
                      SELECT id, row_number() OVER (PARTITION BY workspace_id,name ORDER BY version DESC) AS rank
                      FROM sec_prompt_version WHERE status='published'
                    )
                    UPDATE sec_prompt_version p SET status='archived',rollout_percent=0
                    FROM ranked r WHERE p.id=r.id AND r.rank>1
                    """
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sec_prompt_version_rollout ON sec_prompt_version(workspace_id,name,status,rollout_percent,version DESC)"
                )
                # T-M1-007 完整 Registry 字段与 pf_prompt_rollout 表
                for statement in SCHEMA_STATEMENTS:
                    await conn.execute(statement)
        _ROLLOUT_SCHEMA_READY = True


class PromptCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    content: str = Field(min_length=1, max_length=100_000)
    description: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = Field(default=None, max_length=32)
    template_variables: dict[str, str] | None = Field(default=None, max_length=64)
    model_hint: str | None = Field(default=None, max_length=100)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not _NAME.fullmatch(value):
            raise ValueError("prompt name contains unsupported characters")
        return value

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if len(value) > 32:
            raise ValueError("too many tags")
        normalized: list[str] = []
        for tag in value:
            if not isinstance(tag, str):
                raise ValueError("tag must be string")
            trimmed = tag.strip()
            if not trimmed or len(trimmed) > 64:
                raise ValueError("tag length invalid")
            normalized.append(trimmed)
        return normalized


class PromptPatch(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=100_000)
    name: str | None = Field(default=None, min_length=2, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = Field(default=None, max_length=32)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not _NAME.fullmatch(value):
            raise ValueError("prompt name contains unsupported characters")
        return value

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if len(value) > 32:
            raise ValueError("too many tags")
        normalized: list[str] = []
        for tag in value:
            if not isinstance(tag, str):
                raise ValueError("tag must be string")
            trimmed = tag.strip()
            if not trimmed or len(trimmed) > 64:
                raise ValueError("tag length invalid")
            normalized.append(trimmed)
        return normalized


class PromptVersionCreate(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    template_variables: dict[str, str] | None = Field(default=None, max_length=64)
    model_hint: str | None = Field(default=None, max_length=100)


class PromptReleaseRequest(BaseModel):
    version_id: str | None = Field(default=None, min_length=1, max_length=100)
    rollout_percent: int = Field(default=100, ge=1, le=100)


class PromptRollbackRequest(BaseModel):
    version_id: str = Field(min_length=1, max_length=100)
    rollout_percent: int = Field(default=100, ge=1, le=100)


class PromptPublishRequest(BaseModel):
    """T-M1-007 publish 端点请求体：可选 eval_run_id 与 rollout_percent。"""
    eval_run_id: str | None = Field(default=None, min_length=1, max_length=100)
    rollout_percent: int = Field(default=100, ge=1, le=100)
    eval_threshold: float = Field(default=_DEFAULT_EVAL_THRESHOLD, ge=0.0, le=1.0)


class PromptRolloutPatch(BaseModel):
    """T-M1-007 灰度配置 PATCH 请求体。"""
    percent: int = Field(ge=0, le=100)
    strategy: str | None = Field(default=None, max_length=64)

    @field_validator("strategy")
    @classmethod
    def normalize_strategy(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in ("stable_sha256", "all"):
            raise ValueError("strategy must be 'stable_sha256' or 'all'")
        return value


class PromptResolveRequest(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=100)
    prompt_id: str = Field(min_length=1, max_length=100)
    rollout_key: str | None = Field(default=None, min_length=1, max_length=256)
    variables: dict[str, str] = Field(default_factory=dict, max_length=64)

    @field_validator("variables")
    @classmethod
    def validate_variables(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if not _VARIABLE.fullmatch("{{" + key + "}}"):
                raise ValueError(f"invalid prompt variable: {key}")
            if len(item) > 10_000:
                raise ValueError(f"prompt variable is too large: {key}")
        return value


def _require(actor: Actor, capability: str) -> None:
    if not capability_allows(actor.capabilities, capability):
        raise HTTPException(status_code=403, detail=f"Missing capability: {capability}")


def _checksum(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _view(row: dict[str, Any], *, include_content: bool = True) -> dict[str, Any]:
    result = {
        "id": row["id"],
        "name": row["name"],
        "version": row["version"],
        "checksum": row["checksum"],
        "status": row["status"],
        "created_at": row.get("created_at"),
        "published_at": row.get("published_at"),
        "eval_status": row.get("eval_status"),
        "eval_failures": row.get("eval_failures"),
        "rollout_percent": int(row.get("rollout_percent") or 0),
        "description": row.get("description"),
        "tags": row.get("tags") or [],
        "template_variables": row.get("template_variables") or {},
        "model_hint": row.get("model_hint"),
        "parent_version_id": row.get("parent_version_id"),
        "deleted_at": row.get("deleted_at"),
    }
    result["rollout_strategy"] = (
        "stable_sha256"
        if result["status"] == "published" and result["rollout_percent"] < 100
        else "all"
        if result["status"] == "published"
        else "inactive"
    )
    if include_content:
        result["content"] = row["content"]
    return result


_VERSION_COLUMNS = (
    "p.id,p.workspace_id,p.name,p.version,p.content,p.checksum,p.status,p.rollout_percent,"
    "p.created_at,p.published_at,p.description,p.tags,p.template_variables,p.model_hint,"
    "p.parent_version_id,p.deleted_at"
)


async def _get_version(conn, version_id: str, workspace_id: str, *, for_update: bool = False) -> dict[str, Any] | None:
    suffix = " FOR UPDATE OF p" if for_update else ""
    result = await conn.execute(
        f"""SELECT {_VERSION_COLUMNS},
            e.status AS eval_status,e.failures AS eval_failures, e.id AS eval_run_id,
            e.total_cases AS eval_total_cases, e.passed_cases AS eval_passed_cases
            FROM sec_prompt_version p
            LEFT JOIN LATERAL (
                SELECT id, status, failures, total_cases, passed_cases FROM sec_eval_run
                WHERE prompt_version_id=p.id ORDER BY created_at DESC LIMIT 1
            ) e ON TRUE
            WHERE p.id=%s AND p.workspace_id=%s{suffix}""",
        (version_id, workspace_id),
    )
    return await result.fetchone()


async def _get_version_by_number(conn, prompt_id: str, version: int, workspace_id: str, *, for_update: bool = False) -> dict[str, Any] | None:
    """按 prompt_id（任意版本）定位 name，再按 version 数字定位具体版本。"""
    suffix = " FOR UPDATE OF p" if for_update else ""
    result = await conn.execute(
        f"""SELECT {_VERSION_COLUMNS},
            e.status AS eval_status,e.failures AS eval_failures, e.id AS eval_run_id,
            e.total_cases AS eval_total_cases, e.passed_cases AS eval_passed_cases
            FROM sec_prompt_version p
            LEFT JOIN LATERAL (
                SELECT id, status, failures, total_cases, passed_cases FROM sec_eval_run
                WHERE prompt_version_id=p.id ORDER BY created_at DESC LIMIT 1
            ) e ON TRUE
            WHERE p.workspace_id=%s AND p.name=(
                SELECT name FROM sec_prompt_version WHERE id=%s AND workspace_id=%s
            ) AND p.version=%s{suffix}""",
        (workspace_id, prompt_id, workspace_id, version),
    )
    return await result.fetchone()


async def _publish(conn, prompt: dict[str, Any], version_id: str, rollout_percent: int = 100) -> dict[str, Any]:
    chosen = await _get_version(conn, version_id, prompt["workspace_id"], for_update=True)
    if not chosen or chosen["name"] != prompt["name"]:
        raise HTTPException(status_code=404, detail="Prompt version not found")
    if chosen["eval_status"] != "passed":
        raise HTTPException(status_code=409, detail="Prompt must pass the latest safety evaluation")
    result = await conn.execute(
        """SELECT id,version,rollout_percent FROM sec_prompt_version
        WHERE workspace_id=%s AND name=%s AND status='published'
        ORDER BY rollout_percent DESC,version DESC FOR UPDATE""",
        (prompt["workspace_id"], prompt["name"]),
    )
    published_versions = await result.fetchall()
    if rollout_percent < 100:
        baseline = next((row for row in published_versions if row["id"] != version_id), None)
        if baseline is None:
            raise HTTPException(status_code=409, detail="A partial rollout requires a published baseline version")
        await conn.execute(
            """UPDATE sec_prompt_version SET status='archived',rollout_percent=0
            WHERE workspace_id=%s AND name=%s AND status='published' AND id<>%s AND id<>%s""",
            (prompt["workspace_id"], prompt["name"], version_id, baseline["id"]),
        )
        await conn.execute(
            "UPDATE sec_prompt_version SET status='published',rollout_percent=%s,published_at=now() WHERE id=%s AND workspace_id=%s",
            (100 - rollout_percent, baseline["id"], prompt["workspace_id"]),
        )
    else:
        await conn.execute(
            """UPDATE sec_prompt_version SET status='archived',rollout_percent=0
            WHERE workspace_id=%s AND name=%s AND status='published' AND id<>%s""",
            (prompt["workspace_id"], prompt["name"], version_id),
        )
    result = await conn.execute(
        """UPDATE sec_prompt_version SET status='published',rollout_percent=%s,published_at=now()
        WHERE id=%s AND workspace_id=%s
        RETURNING id,workspace_id,name,version,content,checksum,status,rollout_percent,created_at,published_at,
                  description,tags,template_variables,model_hint,parent_version_id,deleted_at""",
        (rollout_percent, version_id, prompt["workspace_id"]),
    )
    published = await result.fetchone()
    return _view(published)


# ============================================================================
# 列表 / 搜索
# ============================================================================


@router.get("/prompts/search")
async def search_prompts(
    actor: Annotated[Actor, Depends(get_actor)],
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=100),
):
    """全文搜索 prompt：匹配 name / description / tags（ILIKE 模式，workspace 隔离）。

    自动排除 status='deleted' 的版本；每个 name 仅返回最高版本。
    """
    _require(actor, "prompt:read")
    pattern = f"%{q.strip()}%"
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT DISTINCT ON (p.name) """
            + _VERSION_COLUMNS
            + """,
                e.status AS eval_status,e.failures AS eval_failures
                FROM sec_prompt_version p
                LEFT JOIN LATERAL (
                  SELECT status,failures FROM sec_eval_run WHERE prompt_version_id=p.id
                  ORDER BY created_at DESC LIMIT 1
                ) e ON TRUE
                WHERE p.workspace_id=%s AND p.status<>'deleted'
                  AND (p.name ILIKE %s OR COALESCE(p.description,'') ILIKE %s
                       OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(p.tags) AS t WHERE t ILIKE %s))
                ORDER BY p.name, p.version DESC LIMIT %s""",
            (actor.workspace_id, pattern, pattern, pattern, limit),
        )
        rows = await result.fetchall()
    items = [_view(row, include_content=False) for row in rows]
    return {
        "items": items,
        "data": items,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(items), "query": q},
    }


@router.get("/prompts")
async def list_prompts(
    actor: Annotated[Actor, Depends(get_actor)],
    name: str | None = Query(default=None, max_length=100),
    status_filter: str | None = Query(default=None, alias="status", max_length=20),
    workspace_id: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=100),
):
    """列表分页：支持 name/status 过滤、cursor 分页、ListResponse<Prompt> 格式。

    - workspace_id 参数仅 owner/admin 可跨工作区查询（capability 校验）。
    - 默认排除 status='deleted'，除非显式 status=deleted。
    - cursor 为最后一条 (name, version) 的 base64 编码（此处简化为 name|version 字符串）。
    """
    _require(actor, "prompt:read")
    target_workspace = actor.workspace_id
    if workspace_id and workspace_id != actor.workspace_id:
        # 跨 workspace 查询需要 owner/admin 显式能力
        if not (capability_allows(actor.capabilities, "*") or capability_allows(actor.capabilities, "workspace:read")):
            raise HTTPException(status_code=403, detail="Cannot query other workspaces")
        target_workspace = workspace_id

    # 校验 status 过滤值
    valid_statuses = {"draft", "published", "archived", "deleted"}
    if status_filter and status_filter not in valid_statuses:
        raise HTTPException(status_code=422, detail=f"Invalid status: {status_filter}")

    clauses = ["p.workspace_id=%s", "p.status<>'deleted'"]
    params: list[Any] = [target_workspace]
    if name:
        clauses.append("p.name=%s")
        params.append(name)
    if status_filter:
        # 显式 status 覆盖默认排除 deleted 的逻辑
        clauses = [c for c in clauses if c != "p.status<>'deleted'"]
        clauses.append("p.status=%s")
        params.append(status_filter)
    if cursor:
        try:
            cur_name, cur_version = cursor.split("|", 1)
            clauses.append("(p.name, p.version) > (%s, %s)")
            params.extend([cur_name, int(cur_version)])
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="Invalid cursor format")

    params.append(limit + 1)
    where = " AND ".join(clauses)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""SELECT {_VERSION_COLUMNS},
                e.status AS eval_status,e.failures AS eval_failures
                FROM sec_prompt_version p
                LEFT JOIN LATERAL (
                  SELECT status,failures FROM sec_eval_run WHERE prompt_version_id=p.id
                  ORDER BY created_at DESC LIMIT 1
                ) e ON TRUE
                WHERE {where}
                ORDER BY p.name, p.version DESC LIMIT %s""",
            tuple(params),
        )
        rows = await result.fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_view(row, include_content=False) for row in rows]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = f"{last['name']}|{last['version']}"
    return {
        "items": items,
        "data": items,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "meta": {"request_id": None, "count": len(items), "limit": limit},
    }


@router.post("/prompts", status_code=201)
async def create_prompt(body: PromptCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "prompt:create")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT COALESCE(max(version),0)+1 AS version FROM sec_prompt_version WHERE workspace_id=%s AND name=%s",
            (actor.workspace_id, body.name),
        )
        version = (await result.fetchone())["version"]
        prompt_id = new_id("gwprm")
        result = await conn.execute(
            """INSERT INTO sec_prompt_version(id,workspace_id,name,version,content,checksum,created_by,
                description,tags,template_variables,model_hint)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
            RETURNING id,workspace_id,name,version,content,checksum,status,rollout_percent,created_at,published_at,
                      description,tags,template_variables,model_hint,parent_version_id,deleted_at""",
            (
                prompt_id, actor.workspace_id, body.name, version, body.content, _checksum(body.content), actor.user_id,
                body.description,
                json_dumps(body.tags or []),
                json_dumps(body.template_variables or {}),
                body.model_hint,
            ),
        )
        row = await result.fetchone()
        await conn.commit()
    return _view(row)


@router.get("/prompts/{prompt_id}")
async def get_prompt(prompt_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    """详情：返回当前版本及所有版本列表。"""
    _require(actor, "prompt:read")
    async with pool.connection() as conn:
        row = await _get_version(conn, prompt_id, actor.workspace_id)
        if not row or row["status"] == "deleted":
            raise HTTPException(status_code=404, detail="Prompt not found")
        # 列出所有版本
        result = await conn.execute(
            f"""SELECT {_VERSION_COLUMNS},
                e.status AS eval_status,e.failures AS eval_failures
                FROM sec_prompt_version p
                LEFT JOIN LATERAL (
                  SELECT status,failures FROM sec_eval_run WHERE prompt_version_id=p.id
                  ORDER BY created_at DESC LIMIT 1
                ) e ON TRUE
                WHERE p.workspace_id=%s AND p.name=%s AND p.status<>'deleted'
                ORDER BY p.version DESC""",
            (actor.workspace_id, row["name"]),
        )
        version_rows = await result.fetchall()
    return {
        "prompt": _view(row),
        "versions": [_view(v) for v in version_rows],
        "current_version": row["version"],
    }


@router.patch("/prompts/{prompt_id}")
async def update_prompt(prompt_id: str, body: PromptPatch, actor: Annotated[Actor, Depends(get_actor)]):
    """更新元数据：name/description/tags/content（仅 draft/archived 版本可改）。"""
    _require(actor, "prompt:write")
    async with pool.connection() as conn:
        row = await _get_version(conn, prompt_id, actor.workspace_id, for_update=True)
        if not row or row["status"] == "deleted":
            raise HTTPException(status_code=404, detail="Prompt not found")
        if row["status"] == "published":
            raise HTTPException(status_code=409, detail="Published prompt versions are immutable")
        sets: list[str] = []
        params: list[Any] = []
        if body.content is not None:
            sets.append("content=%s")
            sets.append("checksum=%s")
            params.extend([body.content, _checksum(body.content)])
        if body.name is not None:
            # 重命名需要校验新 name 不与同 workspace 其他 prompt 冲突
            conflict_result = await conn.execute(
                "SELECT 1 FROM sec_prompt_version WHERE workspace_id=%s AND name=%s AND id<>%s LIMIT 1",
                (actor.workspace_id, body.name, prompt_id),
            )
            if await conflict_result.fetchone():
                raise HTTPException(status_code=409, detail="Prompt name already in use")
            sets.append("name=%s")
            params.append(body.name)
        if body.description is not None:
            sets.append("description=%s")
            params.append(body.description)
        if body.tags is not None:
            sets.append("tags=%s::jsonb")
            params.append(json_dumps(body.tags))
        if not sets:
            return _view(row)
        params.append(prompt_id)
        params.append(actor.workspace_id)
        result = await conn.execute(
            f"""UPDATE sec_prompt_version SET {', '.join(sets)}
            WHERE id=%s AND workspace_id=%s
            RETURNING id,workspace_id,name,version,content,checksum,status,rollout_percent,created_at,published_at,
                      description,tags,template_variables,model_hint,parent_version_id,deleted_at""",
            tuple(params),
        )
        row = await result.fetchone()
        await conn.commit()
    return _view(row)


@router.delete("/prompts/{prompt_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_prompt(prompt_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    """软删除：设置 status='deleted' 并记录 deleted_at。

    保留旧版 archived 行为：当 prompt 处于 published 状态时拒绝删除。
    """
    _require(actor, "prompt:delete")
    async with pool.connection() as conn:
        row = await _get_version(conn, prompt_id, actor.workspace_id, for_update=True)
        if not row:
            raise HTTPException(status_code=404, detail="Prompt not found")
        if row["status"] == "published":
            raise HTTPException(status_code=409, detail="Published prompts cannot be deleted; archive first")
        result = await conn.execute(
            """UPDATE sec_prompt_version SET status='deleted',deleted_at=now(),rollout_percent=0
            WHERE id=%s AND workspace_id=%s AND status<>'deleted' RETURNING id""",
            (prompt_id, actor.workspace_id),
        )
        if not await result.fetchone():
            raise HTTPException(status_code=404, detail="Prompt already deleted or not found")
        await conn.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/prompts/{prompt_id}/versions")
async def list_prompt_versions(prompt_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "prompt:read")
    async with pool.connection() as conn:
        base = await _get_version(conn, prompt_id, actor.workspace_id)
        if not base or base["status"] == "deleted":
            raise HTTPException(status_code=404, detail="Prompt not found")
        result = await conn.execute(
            f"""SELECT {_VERSION_COLUMNS},
                e.status AS eval_status,e.failures AS eval_failures
                FROM sec_prompt_version p
                LEFT JOIN LATERAL (
                  SELECT status,failures FROM sec_eval_run WHERE prompt_version_id=p.id
                  ORDER BY created_at DESC LIMIT 1
                ) e ON TRUE
                WHERE p.workspace_id=%s AND p.name=%s AND p.status<>'deleted'
                ORDER BY p.version DESC""",
            (actor.workspace_id, base["name"]),
        )
        data = [_view(row) for row in await result.fetchall()]
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("/prompts/{prompt_id}/versions", status_code=201)
async def create_prompt_version(prompt_id: str, body: PromptVersionCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "prompt:write")
    async with pool.connection() as conn:
        base = await _get_version(conn, prompt_id, actor.workspace_id)
        if not base or base["status"] == "deleted":
            raise HTTPException(status_code=404, detail="Prompt not found")
        result = await conn.execute(
            "SELECT COALESCE(max(version),0)+1 AS version FROM sec_prompt_version WHERE workspace_id=%s AND name=%s",
            (actor.workspace_id, base["name"]),
        )
        version = (await result.fetchone())["version"]
        result = await conn.execute(
            """INSERT INTO sec_prompt_version(id,workspace_id,name,version,content,checksum,created_by,
                template_variables,model_hint,parent_version_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            RETURNING id,workspace_id,name,version,content,checksum,status,rollout_percent,created_at,published_at,
                      description,tags,template_variables,model_hint,parent_version_id,deleted_at""",
            (
                new_id("gwprm"), actor.workspace_id, base["name"], version, body.content, _checksum(body.content),
                actor.user_id, json_dumps(body.template_variables or {}), body.model_hint, prompt_id,
            ),
        )
        row = await result.fetchone()
        await conn.commit()
    return _view(row)


@router.post("/prompts/{prompt_id}/evaluate")
async def evaluate_prompt_version(prompt_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "prompt:write")
    async with pool.connection() as conn:
        row = await _get_version(conn, prompt_id, actor.workspace_id)
        if not row or row["status"] == "deleted":
            raise HTTPException(status_code=404, detail="Prompt version not found")
        evaluation = evaluate_prompt(row["content"])
        result = await conn.execute(
            """INSERT INTO sec_eval_run(id,workspace_id,prompt_version_id,status,total_cases,passed_cases,failures)
            VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb)
            RETURNING id,status,total_cases,passed_cases,failures,created_at""",
            (
                new_id("gweval"), actor.workspace_id, prompt_id,
                "passed" if evaluation.passed else "failed", evaluation.total_cases,
                evaluation.total_cases - len(evaluation.failures), json_dumps(evaluation.failures),
            ),
        )
        evaluation_row = await result.fetchone()
        await conn.commit()
    return evaluation_row


@router.get("/prompts/{prompt_id}/versions/{version_id}")
async def get_prompt_version(prompt_id: str, version_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    """获取特定版本。

    支持两种 version_id 形式：
    - 数字（如 "1"）：按 version 数字查询（T-M1-007 要求）
    - 字符串 ID（如 "gwprm_xxx"）：按主键查询（向后兼容）
    """
    _require(actor, "prompt:read")
    async with pool.connection() as conn:
        base = await _get_version(conn, prompt_id, actor.workspace_id)
        if not base or base["status"] == "deleted":
            raise HTTPException(status_code=404, detail="Prompt not found")
        # 智能：纯数字按 version 查，否则按 ID 查
        if version_id.isdigit():
            row = await _get_version_by_number(conn, prompt_id, int(version_id), actor.workspace_id)
        else:
            row = await _get_version(conn, version_id, actor.workspace_id)
    if not row or row["name"] != base["name"] or row["status"] == "deleted":
        raise HTTPException(status_code=404, detail="Prompt version not found")
    return _view(row)


# ============================================================================
# T-M1-007 新增：按 version 数字的 publish / rollback / eval-status
# ============================================================================


async def _fetch_eval_run(conn, eval_run_id: str, version_id: str, workspace_id: str) -> dict[str, Any] | None:
    result = await conn.execute(
        """SELECT id,status,total_cases,passed_cases,failures FROM sec_eval_run
           WHERE id=%s AND workspace_id=%s AND prompt_version_id=%s""",
        (eval_run_id, workspace_id, version_id),
    )
    return await result.fetchone()


def _eval_mean_score(row: dict[str, Any]) -> float:
    total = int(row.get("eval_total_cases") or row.get("total_cases") or 0)
    passed = int(row.get("eval_passed_cases") or row.get("passed_cases") or 0)
    if total <= 0:
        return 0.0
    return passed / total


@router.post("/prompts/{prompt_id}/versions/{version}/publish")
async def publish_prompt_version(
    prompt_id: str,
    version: int,
    actor: Annotated[Actor, Depends(get_actor)],
    body: PromptPublishRequest | None = None,
):
    """发布指定版本：设置 status='published'，更新当前激活版本。

    评测门禁：
    - 若传入 eval_run_id，校验该 run 属于该版本且 mean score >= eval_threshold（默认 0.7）。
    - 若未传入 eval_run_id，使用最新一次 eval run。
    - 评测未通过返回 422，错误码 `eval_gate_failed`。
    """
    _require(actor, "prompt:release")
    if body is None:
        body = PromptPublishRequest()
    if version <= 0:
        raise HTTPException(status_code=422, detail="version must be positive")
    if body.rollout_percent not in _ROLLOUT_PERCENTS:
        raise HTTPException(status_code=422, detail=f"rollout_percent must be one of {sorted(_ROLLOUT_PERCENTS)}")

    async with pool.connection() as conn:
        async with conn.transaction():
            target = await _get_version_by_number(conn, prompt_id, version, actor.workspace_id, for_update=True)
            if not target or target["status"] == "deleted":
                raise HTTPException(status_code=404, detail="Prompt version not found")

            # 评测门禁
            eval_row: dict[str, Any] | None
            if body.eval_run_id:
                eval_row = await _fetch_eval_run(conn, body.eval_run_id, target["id"], actor.workspace_id)
                if not eval_row:
                    raise HTTPException(status_code=422, detail="eval_run_id does not belong to this version")
            else:
                # 使用 _get_version 已 JOIN 的最新 eval run
                eval_row = {
                    "id": target.get("eval_run_id"),
                    "status": target.get("eval_status"),
                    "total_cases": target.get("eval_total_cases"),
                    "passed_cases": target.get("eval_passed_cases"),
                } if target.get("eval_status") else None

            if not eval_row or eval_row.get("status") != "passed":
                raise HTTPException(
                    status_code=422,
                    detail={"code": "eval_gate_failed", "message": "Prompt must pass evaluation before publish",
                            "eval_status": (eval_row or {}).get("status")},
                )
            mean_score = _eval_mean_score(eval_row)
            if mean_score < body.eval_threshold:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "eval_gate_failed",
                            "message": f"Eval mean score {mean_score:.3f} below threshold {body.eval_threshold:.3f}",
                            "mean_score": mean_score, "threshold": body.eval_threshold},
                )

            # 归档同 name 的其他 published 版本
            await conn.execute(
                """UPDATE sec_prompt_version SET status='archived',rollout_percent=0
                WHERE workspace_id=%s AND name=%s AND status='published' AND id<>%s""",
                (actor.workspace_id, target["name"], target["id"]),
            )
            result = await conn.execute(
                """UPDATE sec_prompt_version SET status='published',rollout_percent=%s,published_at=now()
                WHERE id=%s AND workspace_id=%s
                RETURNING id,workspace_id,name,version,content,checksum,status,rollout_percent,created_at,published_at,
                          description,tags,template_variables,model_hint,parent_version_id,deleted_at""",
                (body.rollout_percent, target["id"], actor.workspace_id),
            )
            published = await result.fetchone()

            # 同步 pf_prompt_rollout 配置
            await conn.execute(
                """INSERT INTO pf_prompt_rollout(id,workspace_id,prompt_id,percent,strategy,updated_by)
                VALUES(%s,%s,%s,%s,%s,%s)
                ON CONFLICT (workspace_id,prompt_id) DO UPDATE SET
                    percent=EXCLUDED.percent, strategy=EXCLUDED.strategy,
                    updated_by=EXCLUDED.updated_by, updated_at=now()""",
                (new_id("pfrl"), actor.workspace_id, target["id"], body.rollout_percent,
                 "stable_sha256" if body.rollout_percent < 100 else "all", actor.user_id),
            )
    return _view(published)


@router.post("/prompts/{prompt_id}/versions/{version}/rollback")
async def rollback_prompt_version(
    prompt_id: str,
    version: int,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """回滚到指定版本：创建新版本继承目标版本内容，并发布。

    新版本 parent_version_id 指向被回滚的版本。
    """
    _require(actor, "prompt:release")
    if version <= 0:
        raise HTTPException(status_code=422, detail="version must be positive")
    async with pool.connection() as conn:
        async with conn.transaction():
            target = await _get_version_by_number(conn, prompt_id, version, actor.workspace_id, for_update=True)
            if not target or target["status"] == "deleted":
                raise HTTPException(status_code=404, detail="Prompt version not found")
            # 计算新版本号
            result = await conn.execute(
                "SELECT COALESCE(max(version),0)+1 AS version FROM sec_prompt_version WHERE workspace_id=%s AND name=%s",
                (actor.workspace_id, target["name"]),
            )
            new_version = (await result.fetchone())["version"]
            new_id_str = new_id("gwprm")
            result = await conn.execute(
                """INSERT INTO sec_prompt_version(id,workspace_id,name,version,content,checksum,created_by,
                    template_variables,model_hint,parent_version_id)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
                RETURNING id,workspace_id,name,version,content,checksum,status,rollout_percent,created_at,published_at,
                          description,tags,template_variables,model_hint,parent_version_id,deleted_at""",
                (
                    new_id_str, actor.workspace_id, target["name"], new_version,
                    target["content"], target["checksum"], actor.user_id,
                    json_dumps(target.get("template_variables") or {}), target.get("model_hint"), target["id"],
                ),
            )
            new_row = await result.fetchone()

            # 立即发布新版本（回滚目标）
            await conn.execute(
                """UPDATE sec_prompt_version SET status='archived',rollout_percent=0
                WHERE workspace_id=%s AND name=%s AND status='published' AND id<>%s""",
                (actor.workspace_id, target["name"], new_id_str),
            )
            result = await conn.execute(
                """UPDATE sec_prompt_version SET status='published',rollout_percent=100,published_at=now()
                WHERE id=%s AND workspace_id=%s
                RETURNING id,workspace_id,name,version,content,checksum,status,rollout_percent,created_at,published_at,
                          description,tags,template_variables,model_hint,parent_version_id,deleted_at""",
                (new_id_str, actor.workspace_id),
            )
            published = await result.fetchone()
    return {**_view(published), "rollback_from_version": version, "rollback_to_version": new_version}


@router.get("/prompts/{prompt_id}/versions/{version}/eval-status")
async def get_prompt_version_eval_status(
    prompt_id: str,
    version: int,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """返回该版本的评测关联状态：最新 eval run / mean score / 是否通过门禁。"""
    _require(actor, "prompt:read")
    if version <= 0:
        raise HTTPException(status_code=422, detail="version must be positive")
    async with pool.connection() as conn:
        target = await _get_version_by_number(conn, prompt_id, version, actor.workspace_id)
        if not target or target["status"] == "deleted":
            raise HTTPException(status_code=404, detail="Prompt version not found")
        result = await conn.execute(
            """SELECT id,status,total_cases,passed_cases,failures,created_at FROM sec_eval_run
               WHERE workspace_id=%s AND prompt_version_id=%s ORDER BY created_at DESC LIMIT 10""",
            (actor.workspace_id, target["id"]),
        )
        eval_runs = await result.fetchall()
    latest = eval_runs[0] if eval_runs else None
    mean_score = _eval_mean_score(latest) if latest else 0.0
    return {
        "prompt_id": prompt_id,
        "version": version,
        "version_id": target["id"],
        "eval_run_id": latest["id"] if latest else None,
        "eval_status": latest["status"] if latest else None,
        "total_cases": latest["total_cases"] if latest else 0,
        "passed_cases": latest["passed_cases"] if latest else 0,
        "mean_score": mean_score,
        "gate_threshold": _DEFAULT_EVAL_THRESHOLD,
        "gate_passed": bool(latest and latest["status"] == "passed" and mean_score >= _DEFAULT_EVAL_THRESHOLD),
        "history": [
            {
                "id": r["id"], "status": r["status"], "total_cases": r["total_cases"],
                "passed_cases": r["passed_cases"], "created_at": r["created_at"],
            }
            for r in eval_runs
        ],
    }


# ============================================================================
# T-M1-007 灰度发布配置：PATCH /prompts/{prompt_id}/rollout
# ============================================================================


@router.patch("/prompts/{prompt_id}/rollout")
async def patch_prompt_rollout(
    prompt_id: str,
    body: PromptRolloutPatch,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """动态调整灰度比例（10/25/50/100）。

    - 校验 prompt 存在且非 deleted。
    - 写入 pf_prompt_rollout 表（upsert）。
    - 同步更新 sec_prompt_version.rollout_percent（若当前为 published）。
    """
    _require(actor, "prompt:release")
    if body.percent not in _ROLLOUT_PERCENTS:
        raise HTTPException(status_code=422, detail=f"percent must be one of {sorted(_ROLLOUT_PERCENTS)}")
    strategy = body.strategy or ("stable_sha256" if body.percent < 100 else "all")
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await _get_version(conn, prompt_id, actor.workspace_id, for_update=True)
            if not row or row["status"] == "deleted":
                raise HTTPException(status_code=404, detail="Prompt not found")
            await conn.execute(
                """INSERT INTO pf_prompt_rollout(id,workspace_id,prompt_id,percent,strategy,updated_by)
                VALUES(%s,%s,%s,%s,%s,%s)
                ON CONFLICT (workspace_id,prompt_id) DO UPDATE SET
                    percent=EXCLUDED.percent, strategy=EXCLUDED.strategy,
                    updated_by=EXCLUDED.updated_by, updated_at=now()
                RETURNING id,percent,strategy,updated_at""",
                (new_id("pfrl"), actor.workspace_id, prompt_id, body.percent, strategy, actor.user_id),
            )
            # 同步到 sec_prompt_version（仅 published 版本）
            if row["status"] == "published":
                await conn.execute(
                    "UPDATE sec_prompt_version SET rollout_percent=%s WHERE id=%s AND workspace_id=%s",
                    (body.percent, prompt_id, actor.workspace_id),
                )
    return {"prompt_id": prompt_id, "percent": body.percent, "strategy": strategy}


# ============================================================================
# 旧版 releases / rollbacks 端点（保留向后兼容）
# ============================================================================


@router.post("/prompts/{prompt_id}/releases")
async def release_prompt(prompt_id: str, body: PromptReleaseRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "prompt:release")
    async with pool.connection() as conn:
        base = await _get_version(conn, prompt_id, actor.workspace_id, for_update=True)
        if not base:
            raise HTTPException(status_code=404, detail="Prompt not found")
        async with conn.transaction():
            result = await _publish(conn, base, body.version_id or prompt_id, body.rollout_percent)
    return result


@router.post("/prompts/{prompt_id}/rollbacks")
async def rollback_prompt(prompt_id: str, body: PromptRollbackRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "prompt:release")
    async with pool.connection() as conn:
        base = await _get_version(conn, prompt_id, actor.workspace_id, for_update=True)
        if not base:
            raise HTTPException(status_code=404, detail="Prompt not found")
        async with conn.transaction():
            result = await _publish(conn, base, body.version_id, body.rollout_percent)
    return {**result, "rollback": True}


def _render(content: str, variables: dict[str, str]) -> str:
    names = {match.group(1) for match in _VARIABLE.finditer(content)}
    missing = sorted(name for name in names if name not in variables)
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing prompt variables: {', '.join(missing)}")
    return _VARIABLE.sub(lambda match: variables[match.group(1)], content)


def _stable_rollout_bucket(workspace_id: str, prompt_name: str, rollout_key: str | None) -> int:
    subject = (rollout_key or workspace_id).strip() or workspace_id
    digest = hashlib.sha256(f"{workspace_id}\x00{prompt_name}\x00{subject}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 100


def _select_rollout_version(
    rows: list[dict[str, Any]], workspace_id: str, prompt_name: str, rollout_key: str | None
) -> tuple[dict[str, Any], int]:
    if not rows:
        raise HTTPException(status_code=404, detail="Published prompt not found")
    ordered = sorted(
        rows,
        key=lambda row: (int(row.get("rollout_percent") or 0), int(row.get("version") or 0)),
        reverse=True,
    )
    total = sum(int(row.get("rollout_percent") or 0) for row in ordered)
    if total != 100 or any(int(row.get("rollout_percent") or 0) <= 0 for row in ordered):
        raise HTTPException(status_code=409, detail="Prompt rollout configuration is invalid")
    bucket = _stable_rollout_bucket(workspace_id, prompt_name, rollout_key)
    offset = 0
    for row in ordered:
        percent = int(row["rollout_percent"])
        if bucket < offset + percent:
            return row, bucket
        offset += percent
    # The total check above makes this unreachable, but keeping the failure closed
    # protects against malformed database rows during a concurrent rollout.
    raise HTTPException(status_code=409, detail="Prompt rollout configuration is invalid")


@internal_router.post("/prompts/resolve", dependencies=[Depends(require_internal)])
async def resolve_prompt(body: PromptResolveRequest):
    variables = dict(body.variables)
    rollout_key = body.rollout_key or variables.pop(_ROLLOUT_KEY_VARIABLE, None) or body.workspace_id
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT id,workspace_id,name,version,content,checksum,status,rollout_percent
            FROM sec_prompt_version WHERE workspace_id=%s AND status='published'
            AND (id=%s OR name=%s) ORDER BY version DESC""",
            (body.workspace_id, body.prompt_id, body.prompt_id),
        )
        rows = await result.fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail="Published prompt not found")
    row, bucket = _select_rollout_version(rows, body.workspace_id, rows[0]["name"], rollout_key)
    return {
        "id": row["id"],
        "name": row["name"],
        "version": row["version"],
        "checksum": row["checksum"],
        "content": _render(row["content"], variables),
        "rollout_bucket": bucket,
        "rollout_percent": row["rollout_percent"],
        "rollout_strategy": "stable_sha256",
    }


router.dependencies.append(Depends(_ensure_rollout_schema))
internal_router.dependencies.append(Depends(_ensure_rollout_schema))
