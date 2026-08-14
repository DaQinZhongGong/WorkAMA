-- 060_file_storage.sql
-- v7.153: P1 平台支撑模块 - 文件存储 (file_storage)
-- 文件元数据表，配合 MinIO/S3 兼容对象存储（默认 mock 实现）。
-- 由 file_storage.py 使用，提供上传/下载/复制/删除/统计接口。
-- 与既有 ag_artifact（会话工件）独立共存，本表面向通用文件管理。

CREATE TABLE IF NOT EXISTS file_metadata (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'other',  -- document/image/audio/video/archive/other
    mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes BIGINT NOT NULL DEFAULT 0,
    storage_path TEXT NOT NULL,           -- MinIO object key
    storage_bucket TEXT NOT NULL DEFAULT 'workama-files',
    sha256 TEXT,
    uploaded_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'uploaded',  -- uploading/uploaded/deleted/failed
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS file_metadata_workspace_idx
    ON file_metadata(workspace_id);
CREATE INDEX IF NOT EXISTS file_metadata_workspace_kind_idx
    ON file_metadata(workspace_id, kind)
    WHERE status <> 'deleted';
CREATE INDEX IF NOT EXISTS file_metadata_uploaded_by_idx
    ON file_metadata(uploaded_by);
CREATE INDEX IF NOT EXISTS file_metadata_created_at_idx
    ON file_metadata(created_at DESC);
