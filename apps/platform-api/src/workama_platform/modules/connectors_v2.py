from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from workama_platform.core import Actor, capability_allows, get_actor, json_dumps, new_id, pool


router = APIRouter(prefix="/api/v1/connectors/v2", tags=["connectors-v2"])

ProviderV2 = Literal["google_drive", "notion"]
ConnectorV2Status = Literal["active", "pending", "disabled", "error"]


class ConnectorV2Create(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=120)
    provider: ProviderV2
    auth_config: dict[str, Any] = Field(default_factory=dict)
    sync_root: str | None = Field(default=None, max_length=512)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("name is required")
        return value


class ConnectorV2Patch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=2, max_length=120)
    auth_config: dict[str, Any] | None = None
    sync_root: str | None = Field(default=None, max_length=512)
    status: ConnectorV2Status | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        return " ".join(value.split()) if value is not None else None


class ConnectorAdapter(ABC):
    """Abstract base for enterprise knowledge connector adapters.

    Implementations must not emit real external HTTP in dry-run mode.
    """

    @abstractmethod
    async def authenticate(self, config: dict[str, Any]) -> dict[str, Any]:
        """Validate auth_config and return normalized credentials summary."""

    @abstractmethod
    async def discover(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        """Return a list of source items (documents / pages / files) available for sync."""

    @abstractmethod
    async def incremental_sync(
        self,
        config: dict[str, Any],
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str]:
        """Return changed items since cursor and the next cursor."""

    @abstractmethod
    async def map_acl(self, config: dict[str, Any], source_item: dict[str, Any]) -> dict[str, Any]:
        """Map external ACL to WorkAMA connector ACL format."""

    @abstractmethod
    async def propagate_deletion(self, config: dict[str, Any], source_id: str) -> dict[str, Any]:
        """Propagate a deletion event and return tombstone metadata."""


class GoogleDriveAdapter(ConnectorAdapter):
    async def authenticate(self, config: dict[str, Any]) -> dict[str, Any]:
        client_email = config.get("client_email", "")
        if not client_email or "@" not in client_email:
            raise ValueError("Google Drive adapter requires a valid client_email in auth_config")
        return {"adapter": "google_drive", "client_email": client_email, "authenticated": True}

    async def discover(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        root = config.get("sync_root", "root")
        return [
            {"source_id": f"gdrive:{root}:file_1", "title": "Mock File 1", "mime_type": "application/pdf", "updated_at": datetime.now(UTC).isoformat()},
            {"source_id": f"gdrive:{root}:file_2", "title": "Mock File 2", "mime_type": "text/plain", "updated_at": datetime.now(UTC).isoformat()},
        ]

    async def incremental_sync(
        self,
        config: dict[str, Any],
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str]:
        items = await self.discover(config)
        next_cursor = hashlib.sha256(json.dumps({"items": [i["source_id"] for i in items]}, sort_keys=True).encode()).hexdigest()
        if cursor == next_cursor:
            return [], next_cursor
        return items, next_cursor

    async def map_acl(self, config: dict[str, Any], source_item: dict[str, Any]) -> dict[str, Any]:
        return {
            "source_id": source_item.get("source_id"),
            "allow_users": ["owner@example.com"],
            "allow_groups": [],
            "allow_roles": ["owner", "admin", "member"],
        }

    async def propagate_deletion(self, config: dict[str, Any], source_id: str) -> dict[str, Any]:
        return {"source_id": source_id, "status": "tombstone", "propagated_at": datetime.now(UTC).isoformat()}


class NotionAdapter(ConnectorAdapter):
    async def authenticate(self, config: dict[str, Any]) -> dict[str, Any]:
        token = config.get("integration_token", "")
        if not token or len(token) < 10:
            raise ValueError("Notion adapter requires a valid integration_token in auth_config")
        return {"adapter": "notion", "workspace_name": "Mock Workspace", "authenticated": True}

    async def discover(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        root = config.get("sync_root", "root")
        return [
            {"source_id": f"notion:{root}:page_1", "title": "Mock Page 1", "page_type": "page", "updated_at": datetime.now(UTC).isoformat()},
            {"source_id": f"notion:{root}:page_2", "title": "Mock Page 2", "page_type": "database", "updated_at": datetime.now(UTC).isoformat()},
        ]

    async def incremental_sync(
        self,
        config: dict[str, Any],
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str]:
        items = await self.discover(config)
        next_cursor = hashlib.sha256(json.dumps({"items": [i["source_id"] for i in items]}, sort_keys=True).encode()).hexdigest()
        if cursor == next_cursor:
            return [], next_cursor
        return items, next_cursor

    async def map_acl(self, config: dict[str, Any], source_item: dict[str, Any]) -> dict[str, Any]:
        return {
            "source_id": source_item.get("source_id"),
            "allow_users": [],
            "allow_groups": ["engineering"],
            "allow_roles": ["owner", "admin", "member"],
        }

    async def propagate_deletion(self, config: dict[str, Any], source_id: str) -> dict[str, Any]:
        return {"source_id": source_id, "status": "tombstone", "propagated_at": datetime.now(UTC).isoformat()}


_ADAPTERS: dict[ProviderV2, ConnectorAdapter] = {
    "google_drive": GoogleDriveAdapter(),
    "notion": NotionAdapter(),
}


def _get_adapter(provider: ProviderV2) -> ConnectorAdapter:
    adapter = _ADAPTERS.get(provider)
    if not adapter:
        raise HTTPException(status_code=422, detail=f"Provider {provider} is not supported")
    return adapter


def _require(actor: Actor, action: Literal["read", "write"]) -> None:
    required = f"connector:{action}"
    if capability_allows(actor.capabilities, required):
        return
    if actor.actor_type != "user":
        raise HTTPException(status_code=403, detail=f"Missing capability: {required}")
    if action == "read" and actor.role in {"owner", "admin", "member", "viewer"}:
        return
    if action == "write" and actor.role in {"owner", "admin", "member"}:
        return
    raise HTTPException(status_code=403, detail=f"Missing capability: {required}")


def _connector_v2_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "name": row["name"],
        "provider": row["provider"],
        "status": row["status"],
        "auth_configured": bool(row.get("auth_config")),
        "sync_root": row.get("sync_root"),
        "last_cursor": row.get("last_cursor"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _sync_run_view(row: dict[str, Any]) -> dict[str, Any]:
    """将 connector_sync_run 行序列化为对外 JSON 视图。"""
    return {
        "id": row["id"],
        "connector_id": row["connector_id"],
        "workspace_id": row["workspace_id"],
        "status": row["status"],
        "items_synced": row.get("items_synced", 0),
        "acl_mappings_count": row.get("acl_mappings_count", 0),
        "duration_ms": row.get("duration_ms"),
        "error": row.get("error"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "created_at": row.get("created_at"),
    }


def _acl_mapping_view(row: dict[str, Any]) -> dict[str, Any]:
    """将 connector_acl_mapping 行序列化为对外 JSON 视图。"""
    return {
        "id": row["id"],
        "connector_id": row["connector_id"],
        "workspace_id": row["workspace_id"],
        "source_entity": row["source_entity"],
        "source_permission": row.get("source_permission"),
        "workama_resource_type": row["workama_resource_type"],
        "workama_resource_id": row["workama_resource_id"],
        "workama_permission": row.get("workama_permission"),
        "mapping_status": row["mapping_status"],
        "applied_by": row.get("applied_by"),
        "applied_at": row.get("applied_at"),
        "reject_reason": row.get("reject_reason"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _sync_cursor_view(row: dict[str, Any]) -> dict[str, Any]:
    """将 connector_sync_cursor 行序列化为对外 JSON 视图。"""
    return {
        "connector_id": row["connector_id"],
        "workspace_id": row["workspace_id"],
        "last_synced_at": row.get("last_synced_at"),
        "next_page_token": row.get("next_page_token"),
        "total_synced": row.get("total_synced", 0),
        "updated_at": row.get("updated_at"),
    }


def _acl_audit_view(row: dict[str, Any]) -> dict[str, Any]:
    """将 connector_acl_audit 行序列化为对外 JSON 视图。"""
    details = row.get("details")
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except (TypeError, ValueError):
            pass
    return {
        "id": row["id"],
        "connector_id": row["connector_id"],
        "workspace_id": row["workspace_id"],
        "mapping_id": row.get("mapping_id"),
        "action": row["action"],
        "actor_id": row["actor_id"],
        "details": details,
        "created_at": row.get("created_at"),
    }


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS connector_config_v2 (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      name TEXT NOT NULL,
      provider TEXT NOT NULL CHECK (provider IN ('google_drive','notion')),
      status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('active','pending','disabled','error')),
      auth_config JSONB NOT NULL DEFAULT '{}'::jsonb,
      sync_root TEXT,
      last_cursor TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id, name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_connector_config_v2_workspace ON connector_config_v2(workspace_id, status, updated_at DESC)",
    # 同步运行历史：每次 adapter.incremental_sync 调用落库一条记录
    """
    CREATE TABLE IF NOT EXISTS connector_sync_run (
      id TEXT PRIMARY KEY,
      connector_id TEXT NOT NULL REFERENCES connector_config_v2(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','failed')),
      items_synced INTEGER NOT NULL DEFAULT 0 CHECK (items_synced >= 0),
      acl_mappings_count INTEGER NOT NULL DEFAULT 0 CHECK (acl_mappings_count >= 0),
      duration_ms INTEGER,
      error TEXT,
      started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      finished_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_connector_sync_run_connector_time ON connector_sync_run(connector_id, created_at DESC)",
    # ACL 映射：每个源条目映射到 WorkAMA 资源 + 权限
    """
    CREATE TABLE IF NOT EXISTS connector_acl_mapping (
      id TEXT PRIMARY KEY,
      connector_id TEXT NOT NULL REFERENCES connector_config_v2(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      source_entity TEXT NOT NULL,
      source_permission TEXT,
      workama_resource_type TEXT NOT NULL,
      workama_resource_id TEXT NOT NULL,
      workama_permission TEXT,
      mapping_status TEXT NOT NULL DEFAULT 'pending' CHECK (mapping_status IN ('pending','applied','rejected')),
      applied_by TEXT,
      applied_at TIMESTAMPTZ,
      reject_reason TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(connector_id, source_entity, workama_resource_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_connector_acl_mapping_connector_status ON connector_acl_mapping(connector_id, mapping_status)",
    # 同步游标：每个 connector 一条，记录增量同步状态
    """
    CREATE TABLE IF NOT EXISTS connector_sync_cursor (
      connector_id TEXT PRIMARY KEY REFERENCES connector_config_v2(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      last_synced_at TIMESTAMPTZ,
      next_page_token TEXT,
      total_synced INTEGER NOT NULL DEFAULT 0 CHECK (total_synced >= 0),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # ACL 审计：所有 ACL 相关操作（mapped/applied/rejected/reset_cursor）的审计追踪
    """
    CREATE TABLE IF NOT EXISTS connector_acl_audit (
      id TEXT PRIMARY KEY,
      connector_id TEXT NOT NULL REFERENCES connector_config_v2(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      mapping_id TEXT,
      action TEXT NOT NULL CHECK (action IN ('mapped','applied','rejected','reset_cursor')),
      actor_id TEXT NOT NULL,
      details JSONB NOT NULL DEFAULT '{}'::jsonb,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_connector_acl_audit_connector_time ON connector_acl_audit(connector_id, created_at DESC)",
)


async def ensure_connectors_v2_schema(conn) -> None:
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_connector_v2(
    body: ConnectorV2Create,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    _require(actor, "write")
    adapter = _get_adapter(body.provider)
    auth_summary = await adapter.authenticate(body.auth_config)
    connector_id = new_id("connv2")
    async with pool.connection() as conn:
        async with conn.transaction():
            duplicate = await conn.execute(
                "SELECT 1 FROM connector_config_v2 WHERE workspace_id=%s AND name=%s",
                (actor.workspace_id, body.name),
            )
            if await duplicate.fetchone():
                raise HTTPException(status_code=409, detail="Connector name already exists in this workspace")
            result = await conn.execute(
                """
                INSERT INTO connector_config_v2(id,workspace_id,name,provider,status,auth_config,sync_root)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s) RETURNING *
                """,
                (connector_id, actor.workspace_id, body.name, body.provider, "active" if auth_summary["authenticated"] else "pending", json_dumps(body.auth_config), body.sync_root),
            )
            row = await result.fetchone()
    view = _connector_v2_view(row)
    return {"connector": view, **view, "auth_summary": auth_summary}


@router.get("")
async def list_connectors_v2(
    actor: Annotated[Actor, Depends(get_actor)],
    provider: ProviderV2 | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    _require(actor, "read")
    predicates = ["workspace_id=%s"]
    params: list[Any] = [actor.workspace_id]
    if provider is not None:
        predicates.append("provider=%s")
        params.append(provider)
    params.append(limit)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"SELECT * FROM connector_config_v2 WHERE {' AND '.join(predicates)} ORDER BY updated_at DESC LIMIT %s",
            tuple(params),
        )
        rows = await result.fetchall()
    data = [_connector_v2_view(row) for row in rows]
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.get("/{connector_id}")
async def get_connector_v2(
    connector_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    _require(actor, "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM connector_config_v2 WHERE id=%s AND workspace_id=%s",
            (connector_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Connector not found")
    view = _connector_v2_view(row)
    return {"connector": view, **view}


@router.patch("/{connector_id}")
async def patch_connector_v2(
    connector_id: str,
    body: ConnectorV2Patch,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    _require(actor, "write")
    updates: list[str] = []
    values: list[Any] = []
    if body.name is not None:
        updates.append("name=%s")
        values.append(body.name)
    if body.auth_config is not None:
        updates.append("auth_config=%s::jsonb")
        values.append(json_dumps(body.auth_config))
    if body.sync_root is not None:
        updates.append("sync_root=%s")
        values.append(body.sync_root)
    if body.status is not None:
        updates.append("status=%s")
        values.append(body.status)
    if not updates:
        return await get_connector_v2(connector_id, actor)
    updates.append("updated_at=now()")
    values.extend([connector_id, actor.workspace_id])
    async with pool.connection() as conn:
        result = await conn.execute(
            f"UPDATE connector_config_v2 SET {', '.join(updates)} WHERE id=%s AND workspace_id=%s RETURNING *",
            values,
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Connector not found")
    view = _connector_v2_view(row)
    return {"connector": view, **view}


@router.delete("/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connector_v2(
    connector_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
) -> Response:
    _require(actor, "write")
    async with pool.connection() as conn:
        result = await conn.execute(
            "DELETE FROM connector_config_v2 WHERE id=%s AND workspace_id=%s RETURNING id",
            (connector_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Connector not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{connector_id}/sync")
async def sync_connector_v2(
    connector_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    _require(actor, "write")
    started_at = datetime.now(UTC)
    operation_id = new_id("op")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM connector_config_v2 WHERE id=%s AND workspace_id=%s",
            (connector_id, actor.workspace_id),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Connector not found")
        if row["status"] != "active":
            raise HTTPException(status_code=409, detail="Connector is not active")
        adapter = _get_adapter(row["provider"])
        auth_config = dict(row.get("auth_config") or {})
        if row.get("sync_root"):
            auth_config.setdefault("sync_root", row["sync_root"])
        # 读取当前同步游标
        cursor_result = await conn.execute(
            "SELECT next_page_token, total_synced FROM connector_sync_cursor WHERE connector_id=%s AND workspace_id=%s",
            (connector_id, actor.workspace_id),
        )
        cursor_row = await cursor_result.fetchone()
        prev_cursor = row.get("last_cursor")
        if cursor_row and cursor_row.get("next_page_token"):
            prev_cursor = cursor_row["next_page_token"]
        # 执行增量同步
        try:
            items, next_cursor = await adapter.incremental_sync(auth_config, prev_cursor)
        except Exception as exc:
            finished_at = datetime.now(UTC)
            duration_ms = int((finished_at - started_at).total_seconds() * 1000)
            try:
                await conn.execute(
                    """
                    INSERT INTO connector_sync_run(id, connector_id, workspace_id, status, items_synced, acl_mappings_count, duration_ms, error, started_at, finished_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (operation_id, connector_id, actor.workspace_id, "failed", 0, 0, duration_ms, str(exc)[:500], started_at, finished_at),
                )
            except Exception:
                pass
            raise HTTPException(status_code=500, detail=f"Sync failed: {exc}") from exc
        finished_at = datetime.now(UTC)
        duration_ms = int((finished_at - started_at).total_seconds() * 1000)
        # 落库 sync_run
        run_result = await conn.execute(
            """
            INSERT INTO connector_sync_run(id, connector_id, workspace_id, status, items_synced, acl_mappings_count, duration_ms, error, started_at, finished_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
            """,
            (operation_id, connector_id, actor.workspace_id, "completed", len(items), 0, duration_ms, None, started_at, finished_at),
        )
        run_row = await run_result.fetchone()
        # 对每个 item 计算 ACL 映射并落库
        acl_mappings_count = 0
        for item in items:
            try:
                acl = await adapter.map_acl(auth_config, item)
            except Exception:
                continue
            source_entity = acl.get("source_id") or item.get("source_id")
            if not source_entity:
                continue
            workama_resource_id = f"resource:{source_entity}"
            mapping_id = new_id("aclmap")
            source_permission = ",".join(sorted(set((acl.get("allow_users") or []) + (acl.get("allow_groups") or []))))
            workama_permission = ",".join(sorted(set(acl.get("allow_roles") or [])))
            try:
                mapping_result = await conn.execute(
                    """
                    INSERT INTO connector_acl_mapping(id, connector_id, workspace_id, source_entity, source_permission, workama_resource_type, workama_resource_id, workama_permission, mapping_status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
                    """,
                    (mapping_id, connector_id, actor.workspace_id, source_entity, source_permission or None, "document", workama_resource_id, workama_permission or None, "pending"),
                )
                mapping_row = await mapping_result.fetchone()
            except Exception:
                # UNIQUE 冲突等：跳过该映射
                continue
            if not mapping_row:
                continue
            acl_mappings_count += 1
            # 写 ACL 审计
            audit_id = new_id("aclaud")
            try:
                await conn.execute(
                    """
                    INSERT INTO connector_acl_audit(id, connector_id, workspace_id, mapping_id, action, actor_id, details)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (audit_id, connector_id, actor.workspace_id, mapping_id, "mapped", actor.user_id, json_dumps({"source_entity": source_entity, "workama_resource_id": workama_resource_id})),
                )
            except Exception:
                pass
        # 更新 sync_run 的 acl_mappings_count
        if acl_mappings_count > 0:
            await conn.execute(
                "UPDATE connector_sync_run SET acl_mappings_count=%s WHERE id=%s",
                (acl_mappings_count, operation_id),
            )
        # UPSERT 同步游标
        total_synced = (cursor_row["total_synced"] if cursor_row else 0) + len(items)
        await conn.execute(
            """
            INSERT INTO connector_sync_cursor(connector_id, workspace_id, last_synced_at, next_page_token, total_synced, updated_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (connector_id) DO UPDATE SET
              last_synced_at=EXCLUDED.last_synced_at,
              next_page_token=EXCLUDED.next_page_token,
              total_synced=EXCLUDED.total_synced,
              updated_at=now()
            """,
            (connector_id, actor.workspace_id, finished_at, next_cursor, total_synced),
        )
        # 更新 connector_config_v2 last_cursor
        await conn.execute(
            "UPDATE connector_config_v2 SET last_cursor=%s, updated_at=now() WHERE id=%s AND workspace_id=%s",
            (next_cursor, connector_id, actor.workspace_id),
        )
    sync_run_view = _sync_run_view(run_row) if run_row else None
    if sync_run_view is not None:
        sync_run_view["acl_mappings_count"] = acl_mappings_count
    return {
        "operation_id": operation_id,
        "sync_run_id": operation_id,
        "connector_id": connector_id,
        "status": "accepted",
        "items_synced": len(items),
        "acl_mappings_count": acl_mappings_count,
        "next_cursor": next_cursor,
        "submitted_at": finished_at.isoformat(),
        "sync_run": sync_run_view,
    }


@router.post("/{connector_id}/dry-run")
async def dry_run_connector_v2(
    connector_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    _require(actor, "write")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM connector_config_v2 WHERE id=%s AND workspace_id=%s",
            (connector_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Connector not found")
    adapter = _get_adapter(row["provider"])
    auth_config = dict(row.get("auth_config") or {})
    auth_summary = await adapter.authenticate(auth_config)
    discovered = await adapter.discover(auth_config)
    mapped_acls = []
    for item in discovered:
        mapped_acls.append(await adapter.map_acl(auth_config, item))
    sample_tombstone = await adapter.propagate_deletion(auth_config, "sample:deleted:id")
    return {
        "connector_id": connector_id,
        "dry_run": True,
        "auth_summary": auth_summary,
        "discovered_count": len(discovered),
        "discovered": discovered,
        "acl_mappings": mapped_acls,
        "deletion_propagation": sample_tombstone,
    }


# ============================================================================
# ACL 增量同步落库相关 Pydantic 模型与端点
# ============================================================================


class ACLMappingRejectRequest(BaseModel):
    """拒绝 ACL 映射的请求体。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1, max_length=1_000)


class SyncCursorResetRequest(BaseModel):
    """重置同步游标的请求体。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str | None = Field(default=None, max_length=1_000)


async def _assert_connector_in_workspace(conn, connector_id: str, workspace_id: str) -> dict[str, Any]:
    """校验 connector 属于指定 workspace，返回 connector 行；否则 404。"""
    result = await conn.execute(
        "SELECT id, workspace_id, name, provider, status FROM connector_config_v2 WHERE id=%s AND workspace_id=%s",
        (connector_id, workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Connector not found")
    return row


# 1. 同步历史列表
@router.get("/{connector_id}/sync-history")
async def list_sync_history(
    connector_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=160),
) -> dict[str, Any]:
    """列出 connector 的同步运行历史（按 created_at 倒序，分页）。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        await _assert_connector_in_workspace(conn, connector_id, actor.workspace_id)
        predicates = ["connector_id=%s", "workspace_id=%s"]
        params: list[Any] = [connector_id, actor.workspace_id]
        if cursor:
            predicates.append("created_at < %s")
            params.append(cursor)
        result = await conn.execute(
            f"SELECT * FROM connector_sync_run WHERE {' AND '.join(predicates)} ORDER BY created_at DESC LIMIT %s",
            (*params, limit),
        )
        rows = await result.fetchall()
    items = [_sync_run_view(row) for row in rows]
    has_more = len(items) == limit
    next_cursor = items[-1]["created_at"] if has_more and items else None
    return {
        "items": items,
        "data": items,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "meta": {"request_id": None, "count": len(items)},
    }


# 2. 单次同步详情
@router.get("/{connector_id}/sync-history/{sync_id}")
async def get_sync_history_detail(
    connector_id: str,
    sync_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """获取单次同步详情，含完整 ACL 映射列表。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        await _assert_connector_in_workspace(conn, connector_id, actor.workspace_id)
        # 仅按 id+connector_id 查询，便于事后做跨 workspace 防护
        result = await conn.execute(
            "SELECT * FROM connector_sync_run WHERE id=%s AND connector_id=%s",
            (sync_id, connector_id),
        )
        run_row = await result.fetchone()
        if not run_row:
            raise HTTPException(status_code=404, detail="Sync run not found")
        # 跨 workspace 防护：sync_run 应属于 actor 的 workspace
        if run_row.get("workspace_id") != actor.workspace_id:
            raise HTTPException(status_code=403, detail="Cannot access sync run outside your workspace")
        mapping_result = await conn.execute(
            "SELECT * FROM connector_acl_mapping WHERE connector_id=%s AND workspace_id=%s ORDER BY created_at ASC",
            (connector_id, actor.workspace_id),
        )
        mapping_rows = await mapping_result.fetchall()
        audit_result = await conn.execute(
            "SELECT * FROM connector_acl_audit WHERE connector_id=%s AND workspace_id=%s ORDER BY created_at DESC",
            (connector_id, actor.workspace_id),
        )
        audit_rows = await audit_result.fetchall()
    return {
        "sync_run": _sync_run_view(run_row),
        "items": _sync_run_view(run_row),  # 兼容旧字段
        "acl_mappings": [_acl_mapping_view(r) for r in mapping_rows],
        "audit_logs": [_acl_audit_view(r) for r in audit_rows],
    }


# 3. ACL 映射列表
@router.get("/{connector_id}/acl-mappings")
async def list_acl_mappings(
    connector_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    source_entity: str | None = Query(default=None, max_length=320),
    permission: str | None = Query(default=None, max_length=320),
    mapping_status: Literal["pending", "applied", "rejected"] | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=160),
) -> dict[str, Any]:
    """列出 connector 的 ACL 映射（支持 source_entity / permission / status 过滤）。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        await _assert_connector_in_workspace(conn, connector_id, actor.workspace_id)
        predicates = ["connector_id=%s", "workspace_id=%s"]
        params: list[Any] = [connector_id, actor.workspace_id]
        if source_entity:
            predicates.append("source_entity=%s")
            params.append(source_entity)
        if permission:
            predicates.append("(workama_permission ILIKE %s OR source_permission ILIKE %s)")
            params.extend([f"%{permission}%", f"%{permission}%"])
        if mapping_status:
            predicates.append("mapping_status=%s")
            params.append(mapping_status)
        if cursor:
            predicates.append("created_at < %s")
            params.append(cursor)
        result = await conn.execute(
            f"SELECT * FROM connector_acl_mapping WHERE {' AND '.join(predicates)} ORDER BY created_at DESC LIMIT %s",
            (*params, limit),
        )
        rows = await result.fetchall()
    items = [_acl_mapping_view(row) for row in rows]
    has_more = len(items) == limit
    next_cursor = items[-1]["created_at"] if has_more and items else None
    return {
        "items": items,
        "data": items,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "meta": {"request_id": None, "count": len(items)},
    }


# 4. 应用 ACL 映射（pending -> applied）
@router.post("/{connector_id}/acl-mappings/{mapping_id}/apply")
async def apply_acl_mapping(
    connector_id: str,
    mapping_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """将 ACL 映射应用到 WorkAMA 资源（status: pending -> applied）。"""
    _require(actor, "write")
    async with pool.connection() as conn:
        await _assert_connector_in_workspace(conn, connector_id, actor.workspace_id)
        # 仅按 id+connector_id 查询，便于事后做跨 workspace 防护
        result = await conn.execute(
            "SELECT * FROM connector_acl_mapping WHERE id=%s AND connector_id=%s",
            (mapping_id, connector_id),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="ACL mapping not found")
        if row.get("workspace_id") != actor.workspace_id:
            raise HTTPException(status_code=403, detail="Cannot apply ACL mapping outside your workspace")
        if row["mapping_status"] != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"ACL mapping is not pending (current={row['mapping_status']})",
            )
        update_result = await conn.execute(
            """
            UPDATE connector_acl_mapping
            SET mapping_status='applied', applied_by=%s, applied_at=now(), updated_at=now()
            WHERE id=%s AND connector_id=%s AND workspace_id=%s AND mapping_status='pending'
            RETURNING *
            """,
            (actor.user_id, mapping_id, connector_id, actor.workspace_id),
        )
        updated = await update_result.fetchone()
        if not updated:
            raise HTTPException(status_code=409, detail="ACL mapping state changed concurrently")
        # 写 ACL 审计
        audit_id = new_id("aclaud")
        await conn.execute(
            """
            INSERT INTO connector_acl_audit(id, connector_id, workspace_id, mapping_id, action, actor_id, details)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                audit_id,
                connector_id,
                actor.workspace_id,
                mapping_id,
                "applied",
                actor.user_id,
                json_dumps({"workama_resource_id": updated["workama_resource_id"], "applied_by": actor.user_id}),
            ),
        )
    return {"mapping": _acl_mapping_view(updated), "applied": True}


# 5. 拒绝 ACL 映射（pending -> rejected）
@router.post("/{connector_id}/acl-mappings/{mapping_id}/reject")
async def reject_acl_mapping(
    connector_id: str,
    mapping_id: str,
    body: ACLMappingRejectRequest,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """拒绝 ACL 映射（status: pending -> rejected，需 reason）。"""
    _require(actor, "write")
    async with pool.connection() as conn:
        await _assert_connector_in_workspace(conn, connector_id, actor.workspace_id)
        # 仅按 id+connector_id 查询，便于事后做跨 workspace 防护
        result = await conn.execute(
            "SELECT * FROM connector_acl_mapping WHERE id=%s AND connector_id=%s",
            (mapping_id, connector_id),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="ACL mapping not found")
        if row.get("workspace_id") != actor.workspace_id:
            raise HTTPException(status_code=403, detail="Cannot reject ACL mapping outside your workspace")
        if row["mapping_status"] != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"ACL mapping is not pending (current={row['mapping_status']})",
            )
        update_result = await conn.execute(
            """
            UPDATE connector_acl_mapping
            SET mapping_status='rejected', reject_reason=%s, updated_at=now()
            WHERE id=%s AND connector_id=%s AND workspace_id=%s AND mapping_status='pending'
            RETURNING *
            """,
            (body.reason, mapping_id, connector_id, actor.workspace_id),
        )
        updated = await update_result.fetchone()
        if not updated:
            raise HTTPException(status_code=409, detail="ACL mapping state changed concurrently")
        # 写 ACL 审计
        audit_id = new_id("aclaud")
        await conn.execute(
            """
            INSERT INTO connector_acl_audit(id, connector_id, workspace_id, mapping_id, action, actor_id, details)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                audit_id,
                connector_id,
                actor.workspace_id,
                mapping_id,
                "rejected",
                actor.user_id,
                json_dumps({"reason": body.reason, "rejected_by": actor.user_id}),
            ),
        )
    return {"mapping": _acl_mapping_view(updated), "rejected": True}


# 6. 获取同步游标
@router.get("/{connector_id}/sync-cursor")
async def get_sync_cursor(
    connector_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """获取 connector 当前的同步游标。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        await _assert_connector_in_workspace(conn, connector_id, actor.workspace_id)
        result = await conn.execute(
            "SELECT * FROM connector_sync_cursor WHERE connector_id=%s AND workspace_id=%s",
            (connector_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Sync cursor not found")
    return {"cursor": _sync_cursor_view(row), **_sync_cursor_view(row)}


# 7. 重置同步游标
@router.post("/{connector_id}/sync-cursor/reset")
async def reset_sync_cursor(
    connector_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    body: SyncCursorResetRequest | None = None,
) -> dict[str, Any]:
    """重置同步游标（清空 next_page_token，total_synced=0，下次全量同步）。"""
    _require(actor, "write")
    async with pool.connection() as conn:
        await _assert_connector_in_workspace(conn, connector_id, actor.workspace_id)
        result = await conn.execute(
            "SELECT * FROM connector_sync_cursor WHERE connector_id=%s AND workspace_id=%s",
            (connector_id, actor.workspace_id),
        )
        cursor_row = await result.fetchone()
        if not cursor_row:
            # 自动创建一条空游标
            upsert_result = await conn.execute(
                """
                INSERT INTO connector_sync_cursor(connector_id, workspace_id, last_synced_at, next_page_token, total_synced, updated_at)
                VALUES (%s, %s, NULL, NULL, 0, now())
                RETURNING *
                """,
                (connector_id, actor.workspace_id),
            )
            updated = await upsert_result.fetchone()
        else:
            upsert_result = await conn.execute(
                """
                UPDATE connector_sync_cursor
                SET next_page_token=NULL, total_synced=0, updated_at=now()
                WHERE connector_id=%s AND workspace_id=%s
                RETURNING *
                """,
                (connector_id, actor.workspace_id),
            )
            updated = await upsert_result.fetchone()
        # 写 ACL 审计
        audit_id = new_id("aclaud")
        reason = body.reason if body and body.reason else None
        await conn.execute(
            """
            INSERT INTO connector_acl_audit(id, connector_id, workspace_id, mapping_id, action, actor_id, details)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                audit_id,
                connector_id,
                actor.workspace_id,
                None,
                "reset_cursor",
                actor.user_id,
                json_dumps({"reason": reason}),
            ),
        )
    cursor_view = _sync_cursor_view(updated) if updated else None
    if cursor_view:
        cursor_view["next_page_token"] = None
        cursor_view["total_synced"] = 0
    return {"cursor": cursor_view, "reset": True}


__all__ = [
    "ACLMappingRejectRequest",
    "ConnectorAdapter",
    "ConnectorV2Create",
    "ConnectorV2Patch",
    "GoogleDriveAdapter",
    "NotionAdapter",
    "SyncCursorResetRequest",
    "ensure_connectors_v2_schema",
    "router",
    "SCHEMA_STATEMENTS",
]
