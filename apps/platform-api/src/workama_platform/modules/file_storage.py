"""平台支撑模块 - 文件存储 (file_storage)。

v7.153: P1 平台支撑模块（通知/文件/搜索）。

提供：
- 7 个 REST 端点（上传 / 列表 / 详情 / 下载 / 删除 / 复制 / 统计）
- ``_MinioClient`` 封装：默认 mock（内存），代码结构支持替换为真实 minio SDK
- ``create_notification`` 之外的辅助函数 ``_minio`` 单例

MinIO 调用说明：默认使用内存 mock（不真实写入 MinIO，但记录 storage_path）。
将 ``_MinioClient`` 各方法替换为真实 ``minio.Minio`` 调用即可接入对象存储，
端点与数据模型无需改动。

设计文档：910-进度追踪与任务清单.md「P1 平台支撑模块（通知/文件/搜索）」
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from workama_platform.core import (
    Actor,
    capability_allows,
    get_actor,
    json_dumps,
    new_id,
    pool,
)

router = APIRouter(prefix="/api/v1/files", tags=["file-storage"])

FileKind = Literal["document", "image", "audio", "video", "archive", "other"]
FileStatus = Literal["uploading", "uploaded", "deleted", "failed"]

DEFAULT_BUCKET = "workama-files"
_VALID_KINDS: frozenset[str] = frozenset(
    {"document", "image", "audio", "video", "archive", "other"}
)
_ARCHIVE_EXT = {".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".xz"}
_DOC_EXT = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".txt", ".md", ".csv", ".json", ".xml",
}


class FileUploadRequest(BaseModel):
    """上传元数据（JSON 描述，实际上传走 multipart/form-data）。"""

    kind: FileKind | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FileMetadata(BaseModel):
    id: str
    workspace_id: str
    name: str
    kind: str
    mime_type: str
    size_bytes: int
    storage_path: str
    storage_bucket: str
    sha256: str | None = None
    uploaded_by: str
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


# FileResponse 作为 FileMetadata 的别名导出
FileResponse = FileMetadata


# ============================================================================
# MinIO 客户端封装（mock 实现，结构支持真实 minio SDK）
# ============================================================================


class _MinioClient:
    """MinIO/S3 兼容客户端封装。

    默认为内存 mock 实现（不真实写入 MinIO，但记录 storage_path 与调用日志）。
    替换 ``put_object``/``get_object``/``delete_object``/``copy_object`` 为真实
    ``minio.Minio`` 调用即可接入对象存储，端点与数据模型无需改动。
    """

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], bytes] = {}
        self.calls: list[tuple[str, tuple]] = []

    def put_object(
        self, bucket: str, key: str, data: bytes, content_type: str | None = None
    ) -> None:
        self.calls.append(("put_object", (bucket, key, len(data), content_type)))
        self._store[(bucket, key)] = data

    def get_object(self, bucket: str, key: str) -> bytes:
        self.calls.append(("get_object", (bucket, key)))
        return self._store.get((bucket, key), b"")

    def delete_object(self, bucket: str, key: str) -> None:
        self.calls.append(("delete_object", (bucket, key)))
        self._store.pop((bucket, key), None)

    def copy_object(
        self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str
    ) -> None:
        self.calls.append(
            ("copy_object", (src_bucket, src_key, dst_bucket, dst_key))
        )
        data = self._store.get((src_bucket, src_key), b"")
        self._store[(dst_bucket, dst_key)] = data

    def object_exists(self, bucket: str, key: str) -> bool:
        return (bucket, key) in self._store


_minio = _MinioClient()


def _require(actor: Actor, action: str) -> None:
    if not capability_allows(actor.capabilities, f"file:{action}"):
        raise HTTPException(
            status_code=403, detail=f"Missing capability: file:{action}"
        )


def _detect_kind(filename: str, mime_type: str) -> FileKind:
    ext = os.path.splitext(filename)[1].lower()
    if mime_type:
        if mime_type.startswith("image/"):
            return "image"
        if mime_type.startswith("audio/"):
            return "audio"
        if mime_type.startswith("video/"):
            return "video"
    if ext in _ARCHIVE_EXT:
        return "archive"
    if ext in _DOC_EXT:
        return "document"
    return "other"


def _storage_path(workspace_id: str, file_id: str, filename: str) -> str:
    """生成 MinIO object key：{workspace_id}/{file_id}/{filename}。"""
    safe = os.path.basename(filename) or "file"
    return f"{workspace_id}/{file_id}/{safe}"


def _summary(row: dict) -> dict:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "name": row["name"],
        "kind": row["kind"],
        "mime_type": row["mime_type"],
        "size_bytes": int(row["size_bytes"]),
        "storage_path": row["storage_path"],
        "storage_bucket": row["storage_bucket"],
        "sha256": row.get("sha256"),
        "uploaded_by": row["uploaded_by"],
        "status": row["status"],
        "metadata": row.get("metadata") or {},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def _owned_file(conn: Any, file_id: str, actor: Actor) -> dict:
    result = await conn.execute(
        "SELECT * FROM file_metadata WHERE id = %s", (file_id,)
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="File not found")
    if row["workspace_id"] != actor.workspace_id:
        raise HTTPException(
            status_code=403, detail="File belongs to another workspace"
        )
    return row


# ============================================================================
# Router 端点
# ============================================================================
#
# 路由声明顺序：具体路径（/upload, /stats）必须在参数化路径 /{id} 之前声明，
# 否则 FastAPI 会将 "upload"/"stats" 当作 file_id 参数匹配。


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_file(
    actor: Annotated[Actor, Depends(get_actor)],
    file: UploadFile = File(...),
    kind: str | None = Form(default=None),
    metadata: str | None = Form(default=None),
):
    """上传文件：写入 MinIO（mock）+ 记录元数据。

    - ``kind`` 为空时按扩展名 / mime_type 自动识别
    - ``metadata`` 为 JSON 字符串（可选）
    - 返回新建文件元数据
    """
    _require(actor, "write")
    import json as _json

    data = await file.read()
    size = len(data)
    mime_type = file.content_type or "application/octet-stream"
    filename = file.filename or "file"
    if kind in _VALID_KINDS:
        file_kind: FileKind = kind  # type: ignore[assignment]
    else:
        file_kind = _detect_kind(filename, mime_type)
    file_id = new_id("file")
    bucket = DEFAULT_BUCKET
    key = _storage_path(actor.workspace_id, file_id, filename)
    # 写入 MinIO（mock，记录 storage_path）
    _minio.put_object(bucket, key, data, mime_type)
    sha256 = hashlib.sha256(data).hexdigest()
    meta: dict[str, Any] = {}
    if metadata:
        try:
            decoded = _json.loads(metadata)
            if isinstance(decoded, dict):
                meta = decoded
        except (ValueError, TypeError):
            meta = {}
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                INSERT INTO file_metadata(
                    id, workspace_id, name, kind, mime_type, size_bytes,
                    storage_path, storage_bucket, sha256, uploaded_by,
                    status, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        'uploaded', %s::jsonb)
                RETURNING *
                """,
                (
                    file_id,
                    actor.workspace_id,
                    filename,
                    file_kind,
                    mime_type,
                    size,
                    key,
                    bucket,
                    sha256,
                    actor.user_id,
                    json_dumps(meta),
                ),
            )
            row = await result.fetchone()
    return _summary(row)


@router.get("/stats")
async def file_stats(
    actor: Annotated[Actor, Depends(get_actor)],
):
    """文件统计：按 kind 分组计数和大小。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT kind, count(*) AS count,
                   COALESCE(sum(size_bytes), 0) AS total_bytes
            FROM file_metadata
            WHERE workspace_id = %s AND status <> 'deleted'
            GROUP BY kind ORDER BY kind
            """,
            (actor.workspace_id,),
        )
        rows = await result.fetchall()
    items = [
        {
            "kind": r["kind"],
            "count": int(r["count"]),
            "total_bytes": int(r["total_bytes"]),
        }
        for r in rows
    ]
    total_count = sum(i["count"] for i in items)
    total_bytes = sum(i["total_bytes"] for i in items)
    return {
        "items": items,
        "total_count": total_count,
        "total_bytes": total_bytes,
    }


@router.get("")
async def list_files(
    actor: Annotated[Actor, Depends(get_actor)],
    kind: FileKind | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """列表：workspace 隔离，支持 kind 过滤（排除已删除）。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        clause = "workspace_id = %s AND status <> 'deleted'"
        params: list[object] = [actor.workspace_id]
        if kind:
            clause += " AND kind = %s"
            params.append(kind)
        params.append(limit)
        params.append(offset)
        result = await conn.execute(
            f"""
            SELECT * FROM file_metadata
            WHERE {clause}
            ORDER BY created_at DESC LIMIT %s OFFSET %s
            """,
            tuple(params),
        )
        rows = await result.fetchall()
    items = [_summary(row) for row in rows]
    return {
        "items": items,
        "data": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
    }


@router.get("/{file_id}")
async def get_file(
    file_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """文件详情。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        row = await _owned_file(conn, file_id, actor)
    return _summary(row)


@router.get("/{file_id}/download")
async def download_file(
    file_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """下载文件：从 MinIO（mock）读取，返回 StreamingResponse。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        row = await _owned_file(conn, file_id, actor)
    data = _minio.get_object(row["storage_bucket"], row["storage_path"])

    def _iter():
        yield data

    headers = {
        "Content-Disposition": f'attachment; filename="{row["name"]}"'
    }
    return StreamingResponse(_iter(), media_type=row["mime_type"], headers=headers)


@router.delete("/{file_id}", status_code=status.HTTP_200_OK)
async def delete_file(
    file_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """删除文件：MinIO（mock）+ 元数据（标记 deleted）。"""
    _require(actor, "delete")
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await _owned_file(conn, file_id, actor)
            _minio.delete_object(row["storage_bucket"], row["storage_path"])
            result = await conn.execute(
                """
                UPDATE file_metadata
                SET status = 'deleted', updated_at = now()
                WHERE id = %s AND workspace_id = %s
                RETURNING id
                """,
                (file_id, actor.workspace_id),
            )
            deleted = await result.fetchone()
    return {"id": deleted["id"], "deleted": True}


@router.post("/{file_id}/copy", status_code=status.HTTP_201_CREATED)
async def copy_file(
    file_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """复制文件：MinIO（mock）复制 + 新建元数据记录。"""
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await _owned_file(conn, file_id, actor)
            new_fid = new_id("file")
            new_key = _storage_path(actor.workspace_id, new_fid, row["name"])
            _minio.copy_object(
                row["storage_bucket"], row["storage_path"],
                row["storage_bucket"], new_key,
            )
            result = await conn.execute(
                """
                INSERT INTO file_metadata(
                    id, workspace_id, name, kind, mime_type, size_bytes,
                    storage_path, storage_bucket, sha256, uploaded_by,
                    status, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        'uploaded', %s::jsonb)
                RETURNING *
                """,
                (
                    new_fid,
                    actor.workspace_id,
                    row["name"],
                    row["kind"],
                    row["mime_type"],
                    int(row["size_bytes"]),
                    new_key,
                    row["storage_bucket"],
                    row.get("sha256"),
                    actor.user_id,
                    json_dumps(row.get("metadata") or {}),
                ),
            )
            new_row = await result.fetchone()
    return _summary(new_row)
