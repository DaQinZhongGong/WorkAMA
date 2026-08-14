from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from workama_platform.core import Actor, get_actor, new_id, pool
from workama_platform.modules.jobs import submit_operation

router = APIRouter(prefix="/api/v1", tags=["global-search"])
admin_router = APIRouter(prefix="/api/v1/admin", tags=["search-operations"])


class SearchRebuildRequest(BaseModel):
    # v7.250: 扩展 Literal 包含知识库/知识库文档。同步 rebuild_search_projection sources。
    resource_types: list[Literal["session", "artifact", "gateway_channel", "gateway_token", "member", "knowledge_base", "knowledge_document"]] = Field(default_factory=list)


def _require_admin(actor: Actor) -> None:
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")


async def rebuild_search_projection(conn, workspace_id: str, resource_types: list[str] | None = None) -> dict[str, int]:
    selected = set(resource_types or [
        "session", "artifact", "gateway_channel", "gateway_token", "member",
        "knowledge_base", "knowledge_document",
    ])
    counts: dict[str, int] = {}
    sources = {
        "session": """SELECT s.id resource_id, s.user_id owner_id, s.title, '' summary,
          ARRAY[s.model, s.status]::text[] tags, s.status, s.updated_at, extract(epoch from s.updated_at)::bigint source_version
          FROM ag_session s WHERE s.workspace_id = %s""",
        "artifact": """SELECT a.id resource_id, s.user_id owner_id, a.name title, a.content_type summary,
          ARRAY[a.content_type]::text[] tags, 'available' status, a.created_at updated_at, extract(epoch from a.created_at)::bigint source_version
          FROM ag_artifact a JOIN ag_session s ON s.id = a.session_id WHERE a.workspace_id = %s""",
        "gateway_channel": """SELECT c.id resource_id, NULL owner_id, c.name title, c.provider summary,
          c.models tags, c.status, c.updated_at, extract(epoch from c.updated_at)::bigint source_version
          FROM gw_channel c WHERE c.workspace_id = %s""",
        "gateway_token": """SELECT t.id resource_id, NULL owner_id, t.name title, concat('Key ending ', t.last_four) summary,
          t.model_whitelist tags, CASE WHEN t.revoked_at IS NULL THEN 'active' ELSE 'revoked' END status,
          t.created_at updated_at, extract(epoch from t.created_at)::bigint source_version FROM gw_token t WHERE t.workspace_id = %s""",
        "member": """SELECT u.id resource_id, u.id owner_id, u.display_name title, u.email summary,
          ARRAY[m.role]::text[] tags, u.status, u.updated_at, extract(epoch from u.updated_at)::bigint source_version
          FROM id_member m JOIN id_user u ON u.id = m.user_id WHERE m.workspace_id = %s""",
        # v7.249: knowledge_base / knowledge_document 加入 search projection。
        # 知识库元数据用于标题/描述命中，知识库文档用 content + title 做全文检索。
        "knowledge_base": """SELECT k.id resource_id, NULL owner_id, k.name title, COALESCE(k.description, '') summary,
          ARRAY[k.kind, k.status]::text[] tags, k.status, k.updated_at, extract(epoch from k.updated_at)::bigint source_version
          FROM knowledge_base k WHERE k.workspace_id = %s""",
        "knowledge_document": """SELECT d.id resource_id, NULL owner_id, d.title title, COALESCE(d.content, '') summary,
          ARRAY[d.source_type, d.status]::text[] tags, d.status, d.updated_at, extract(epoch from d.updated_at)::bigint source_version
          FROM knowledge_document d WHERE d.workspace_id = %s""",
    }
    for resource_type, sql in sources.items():
        if resource_type not in selected:
            continue
        rows = await conn.execute(sql, (workspace_id,))
        items = await rows.fetchall()
        for item in items:
            visibility = "private" if resource_type in {"session", "artifact"} else "workspace"
            await conn.execute(
                """INSERT INTO ops_search_document(
                  id, workspace_id, resource_type, resource_id, owner_id, visibility, acl_version,
                  title, summary, tags, status, source_version, tombstone, updated_at, search_vector
                ) VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s,FALSE,%s,
                  to_tsvector('simple', %s || ' ' || %s || ' ' || array_to_string(%s::text[], ' ')))
                ON CONFLICT(resource_type, resource_id) DO UPDATE SET owner_id=EXCLUDED.owner_id,
                  visibility=EXCLUDED.visibility,title=EXCLUDED.title,summary=EXCLUDED.summary,tags=EXCLUDED.tags,
                  status=EXCLUDED.status,source_version=EXCLUDED.source_version,tombstone=FALSE,
                  updated_at=EXCLUDED.updated_at,search_vector=EXCLUDED.search_vector,indexed_at=now()""",
                (new_id("srch"), workspace_id, resource_type, item["resource_id"], item["owner_id"], visibility,
                 item["title"], item["summary"], item["tags"], item["status"], item["source_version"], item["updated_at"],
                 item["title"], item["summary"], item["tags"]),
            )
        await conn.execute(
            f"""UPDATE ops_search_document d SET tombstone=TRUE, indexed_at=now()
              WHERE d.workspace_id=%s AND d.resource_type=%s AND NOT EXISTS
              (SELECT 1 FROM ({sql}) source WHERE source.resource_id=d.resource_id)""",
            (workspace_id, resource_type, workspace_id),
        )
        counts[resource_type] = len(items)
    return counts


def _clamp_int(value, default: int, lo: int, hi: int) -> int:
    """安全解析分页参数。

    FastAPI HTTP 路径会先把 Query 默认值解析为 int；但直接函数调用（测试/内部复用）
    时 Query 默认值对象会原样传入，int() 会抛 TypeError。统一在此收敛。
    """
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


@router.get("/search")
async def global_search(
    actor: Annotated[Actor, Depends(get_actor)], q: str = Query(min_length=1, max_length=200),
    resource_type: str | None = None, updated_after: datetime | None = None, limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10000, description="v7.260: 分页偏移，0 表示首页"),
):
    # v7.260: 加 offset 分页参数，返回 total + items + has_more 字段方便前端实现「Load more」或页码。
    offset = _clamp_int(offset, 0, 0, 10000)
    limit = _clamp_int(limit, 20, 1, 100)
    async with pool.connection() as conn:
        # 1) 先 count total hits（忽略 limit/offset），用相同 where 条件
        count_result = await conn.execute(
            """SELECT count(*) AS total FROM ops_search_document
            WHERE workspace_id=%s AND tombstone=FALSE
              AND (visibility='workspace' OR owner_id=%s OR %s=ANY(acl_user_ids) OR %s=ANY(acl_roles))
              AND (%s::text IS NULL OR resource_type=%s) AND (%s::timestamptz IS NULL OR updated_at >= %s)
              AND (search_vector @@ websearch_to_tsquery('simple', %s) OR title ILIKE '%%' || %s || '%%')""",
            (actor.workspace_id, actor.user_id, actor.user_id, actor.role, resource_type, resource_type,
             updated_after, updated_after, q, q),
        )
        total_row = await count_result.fetchone()
        total = int(total_row["total"]) if total_row else 0
        # 2) page slice
        result = await conn.execute(
            """SELECT resource_type, resource_id, title, summary, tags, status, visibility, updated_at,
              ts_rank(search_vector, websearch_to_tsquery('simple', %s)) AS rank
            FROM ops_search_document
            WHERE workspace_id=%s AND tombstone=FALSE
              AND (visibility='workspace' OR owner_id=%s OR %s=ANY(acl_user_ids) OR %s=ANY(acl_roles))
              AND (%s::text IS NULL OR resource_type=%s) AND (%s::timestamptz IS NULL OR updated_at >= %s)
              AND (search_vector @@ websearch_to_tsquery('simple', %s) OR title ILIKE '%%' || %s || '%%')
            ORDER BY rank DESC, updated_at DESC LIMIT %s OFFSET %s""",
            (q, actor.workspace_id, actor.user_id, actor.user_id, actor.role, resource_type, resource_type,
             updated_after, updated_after, q, q, limit, offset),
        )
        items = await result.fetchall()
    return {
        "query": q,
        "partial": False,
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(items) < total,
    }


@admin_router.post("/search-index-rebuilds", status_code=202)
async def rebuild_search(body: SearchRebuildRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    payload = {"workspace_id": actor.workspace_id, "resource_types": body.resource_types}
    async with pool.connection() as conn:
        async with conn.transaction():
            operation = await submit_operation(
                conn, operation_type="search.index_rebuild", workspace_id=actor.workspace_id, org_id=actor.org_id,
                actor_id=actor.user_id, actor_role=actor.role, idempotency_key=f"search-rebuild-{new_id('req')}",
                payload=payload, job_type="search.index_rebuild", max_attempts=3,
            )
    return {"operation_id": operation["id"], "status": operation["status"]}


@admin_router.get("/search-index-status")
async def search_status(actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT count(*) FILTER (WHERE tombstone=FALSE) document_count,
              count(*) FILTER (WHERE tombstone=TRUE) tombstone_count, max(indexed_at) last_indexed_at,
              max(updated_at) source_updated_at FROM ops_search_document WHERE workspace_id=%s""",
            (actor.workspace_id,),
        )
        return await result.fetchone()


# ============================================================================
# v7.153: 统一搜索（unified_router）
# ============================================================================
# 直接对多张业务表（assistant / workflow / knowledge_base / file_metadata /
# notification）做 ILIKE 查询并聚合返回，不依赖 ops_search_document 投影，
# 适合数据量小或需即时索引的场景。与既有 ``router`` 的全局搜索（基于
# ``ops_search_document`` 投影 + ACL 过滤）独立共存，前缀为
# ``/api/v1/unified-search``，不会遮蔽 ``GET /api/v1/search``。

unified_router = APIRouter(prefix="/api/v1/unified-search", tags=["unified-search"])

# 资源类型 → (表名, 主键列, 标题列, 副标题列, workspace 隔离列, 额外过滤)
# 副标题列为 None 时使用空字符串。额外过滤用于排除已归档/已删除记录。
_UNIFIED_SOURCES: dict[str, dict[str, str | None]] = {
    "assistant": {
        "table": "assistant",
        "id_col": "id",
        "title_col": "name",
        "subtitle_col": "description",
        "ws_col": "workspace_id",
        "extra_filter": "status <> 'archived'",
    },
    "workflow": {
        "table": "workflow",
        "id_col": "id",
        "title_col": "name",
        "subtitle_col": "description",
        "ws_col": "workspace_id",
        "extra_filter": "status <> 'archived'",
    },
    "knowledge_base": {
        "table": "knowledge_base",
        "id_col": "id",
        "title_col": "name",
        "subtitle_col": "description",
        "ws_col": "workspace_id",
        "extra_filter": None,
    },
    "file": {
        "table": "file_metadata",
        "id_col": "id",
        "title_col": "name",
        "subtitle_col": "mime_type",
        "ws_col": "workspace_id",
        "extra_filter": "status <> 'deleted'",
    },
    "notification": {
        "table": "notification",
        "id_col": "id",
        "title_col": "title",
        "subtitle_col": "body",
        "ws_col": "workspace_id",
        "extra_filter": None,
    },
}


class SearchRequest(BaseModel):
    """统一搜索请求体（POST /unified-search）。"""

    q: str = Field(min_length=1, max_length=200, description="搜索关键字")
    resource_types: list[Literal[
        "assistant", "workflow", "knowledge_base", "file", "notification"
    ]] = Field(default_factory=list, description="限定资源类型，为空表示全部")
    limit: int = Field(default=20, ge=1, le=100, description="每类最多返回数")


class SearchResult(BaseModel):
    resource_type: str
    resource_id: str
    title: str
    subtitle: str | None = None
    workspace_id: str


class SearchResponse(BaseModel):
    query: str
    total: int
    items: list[SearchResult]
    partial: bool = False
    searched_types: list[str]


def _build_unified_sql(
    resource_type: str, source: dict[str, str | None]
) -> tuple[str, str]:
    """构造单类型 ILIKE 查询的 SQL 与参数占位符模板。

    返回 (sql_template, ws_placeholder)，调用方按 (q%, q%, workspace_id) 顺序填参。
    """
    table = source["table"]
    id_col = source["id_col"]
    title_col = source["title_col"]
    subtitle_col = source["subtitle_col"]
    ws_col = source["ws_col"]
    extra = source["extra_filter"]
    sub_expr = subtitle_col if subtitle_col else "''"
    clauses = [
        f"{ws_col} = %s",
        f"({title_col} ILIKE %s OR {sub_expr} ILIKE %s)",
    ]
    if extra:
        clauses.append(extra)
    where = " AND ".join(clauses)
    sql = (
        f"SELECT '{resource_type}' AS resource_type, {id_col} AS resource_id, "
        f"{title_col} AS title, {sub_expr} AS subtitle, {ws_col} AS workspace_id "
        f"FROM {table} WHERE {where} "
        f"ORDER BY {title_col} LIMIT %s"
    )
    return sql, where


@unified_router.get("")
async def unified_search(
    actor: Annotated[Actor, Depends(get_actor)],
    q: str = Query(min_length=1, max_length=200, description="搜索关键字"),
    resource_type: str | None = Query(
        default=None, description="限定单个资源类型；为空则搜索全部"
    ),
    limit: int = Query(default=20, ge=1, le=100, description="每类最多返回数"),
):
    """统一搜索（GET）：跨多张业务表 ILIKE 聚合。

    - 不依赖 ``ops_search_document`` 投影，直接查事实表
    - 严格按 ``workspace_id`` 隔离
    - ``resource_type`` 限定单类型；为空则全部
    """
    pattern = f"%{q}%"
    selected_types: list[str]
    if resource_type:
        if resource_type not in _UNIFIED_SOURCES:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported resource_type: {resource_type}",
            )
        selected_types = [resource_type]
    else:
        selected_types = list(_UNIFIED_SOURCES.keys())

    items: list[dict] = []
    async with pool.connection() as conn:
        for rtype in selected_types:
            source = _UNIFIED_SOURCES[rtype]
            sql, _ = _build_unified_sql(rtype, source)
            result = await conn.execute(
                sql, (pattern, pattern, actor.workspace_id, limit)
            )
            rows = await result.fetchall()
            for row in rows:
                items.append(
                    {
                        "resource_type": row["resource_type"],
                        "resource_id": row["resource_id"],
                        "title": row["title"],
                        "subtitle": row.get("subtitle"),
                        "workspace_id": row["workspace_id"],
                    }
                )
    return {
        "query": q,
        "total": len(items),
        "items": items,
        "partial": False,
        "searched_types": selected_types,
    }


@unified_router.post("", status_code=status.HTTP_200_OK)
async def unified_search_post(
    body: SearchRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """统一搜索（POST）：支持多类型批量搜索。

    与 GET 版本的区别：``resource_types`` 接受数组，可一次限定多个类型。
    """
    pattern = f"%{body.q}%"
    if body.resource_types:
        for rtype in body.resource_types:
            if rtype not in _UNIFIED_SOURCES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported resource_type: {rtype}",
                )
        selected_types = list(body.resource_types)
    else:
        selected_types = list(_UNIFIED_SOURCES.keys())

    items: list[dict] = []
    async with pool.connection() as conn:
        for rtype in selected_types:
            source = _UNIFIED_SOURCES[rtype]
            sql, _ = _build_unified_sql(rtype, source)
            result = await conn.execute(
                sql, (pattern, pattern, actor.workspace_id, body.limit)
            )
            rows = await result.fetchall()
            for row in rows:
                items.append(
                    {
                        "resource_type": row["resource_type"],
                        "resource_id": row["resource_id"],
                        "title": row["title"],
                        "subtitle": row.get("subtitle"),
                        "workspace_id": row["workspace_id"],
                    }
                )
    return {
        "query": body.q,
        "total": len(items),
        "items": items,
        "partial": False,
        "searched_types": selected_types,
        "limit": body.limit,
    }


@unified_router.get("/types")
async def list_resource_types(
    actor: Annotated[Actor, Depends(get_actor)],
):
    """返回统一搜索支持的全部资源类型及说明。"""
    _ = actor  # 鉴权占位
    return {
        "items": [
            {"type": rtype, "table": src["table"]}
            for rtype, src in _UNIFIED_SOURCES.items()
        ],
        "count": len(_UNIFIED_SOURCES),
    }
