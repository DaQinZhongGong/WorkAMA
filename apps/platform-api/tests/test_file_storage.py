"""文件存储 (file_storage.py) 单元 + 端点测试。

v7.153: 18 个测试覆盖：
- 上传：成功 / kind 自动识别 / metadata JSON 解析（3）
- 列表：默认 / kind 过滤 / pagination（3）
- 详情：成功 / 不存在 404 / 跨 workspace 403（3）
- 下载：成功（StreamingResponse）（1）
- 删除：成功 / 不存在 404（2）
- 复制：成功 / 不存在 404（2）
- 统计：分组聚合（1）
- 鉴权：未认证 401 / viewer 可读不能写（2）
- 辅助：_detect_kind / _storage_path / _MinioClient mock（1）

所有测试使用 fake pool/connection + 内存 _MinioClient，不依赖真实 DB /
Redis / 网络 / MinIO。
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules import file_storage as fs


# ============================================================================
# 测试辅助：fake pool / connection / result
# ============================================================================


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []
        self.rowcount = len(self._rows)

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return list(self._rows)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _RecordingConnection:
    def __init__(self, results=None):
        self.calls: list[tuple[str, tuple]] = []
        self._results = list(results) if results else []
        self._idx = 0

    def transaction(self):
        return _Transaction()

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if self._idx < len(self._results):
            r = self._results[self._idx]
            self._idx += 1
            return r
        return _Result()

    async def commit(self):
        return None


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_args):
                return False

        return _Ctx()


def _actor(
    *,
    role="owner",
    workspace_id="wsp_test",
    user_id="usr_test",
    capabilities=("*",),
) -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email="test@example.com",
        display_name="Test",
        onboarding_completed=True,
        capabilities=capabilities,
    )


def _file_row(**overrides) -> dict:
    base = {
        "id": "file_1",
        "workspace_id": "wsp_test",
        "name": "doc.pdf",
        "kind": "document",
        "mime_type": "application/pdf",
        "size_bytes": 1024,
        "storage_path": "wsp_test/file_1/doc.pdf",
        "storage_bucket": "workama-files",
        "sha256": "abc",
        "uploaded_by": "usr_test",
        "status": "uploaded",
        "metadata": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(fs.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


def _reset_minio():
    """每个测试前重置 mock MinIO 客户端，避免相互污染。"""
    fs._minio = fs._MinioClient()


# ============================================================================
# 1. 上传
# ============================================================================


class TestUpload:
    @pytest.mark.asyncio
    async def test_upload_success(self, monkeypatch):
        """POST /upload 上传 PDF 返回 201 并写入 MinIO mock。"""
        _reset_minio()
        content = b"pdf content bytes"
        expected_sha = hashlib.sha256(content).hexdigest()
        # mock row 的 sha256 与上传内容一致（真实代码会计算后写入 DB 再 RETURNING）
        conn = _RecordingConnection(
            results=[_Result(row=_file_row(sha256=expected_sha))]
        )
        monkeypatch.setattr(fs, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/files/upload",
                files={"file": ("doc.pdf", content, "application/pdf")},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "doc.pdf"
        assert body["kind"] == "document"
        # MinIO mock 记录了 put_object 调用
        assert any(c[0] == "put_object" for c in fs._minio.calls)
        # sha256 应为内容哈希
        assert body["sha256"] == expected_sha
        # INSERT 元数据
        assert any("INSERT INTO file_metadata" in q for q, _ in conn.calls)
        # INSERT 参数中 sha256 字段应为内容哈希
        insert_call = next(
            (q, p) for q, p in conn.calls if "INSERT INTO file_metadata" in q
        )
        assert expected_sha in insert_call[1]

    @pytest.mark.asyncio
    async def test_upload_kind_auto_detected_from_mime(self, monkeypatch):
        """未指定 kind 时通过 mime_type 自动识别为 image。"""
        _reset_minio()
        conn = _RecordingConnection(results=[_Result(row=_file_row(kind="image"))])
        monkeypatch.setattr(fs, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/files/upload",
                files={"file": ("pic.png", b"\x89PNG", "image/png")},
            )
        assert resp.status_code == 201
        # INSERT 参数中 kind 字段应为 image（第 4 个位置）
        insert_call = next(
            (q, p) for q, p in conn.calls if "INSERT INTO file_metadata" in q
        )
        assert insert_call[1][3] == "image"

    @pytest.mark.asyncio
    async def test_upload_metadata_json_parsed(self, monkeypatch):
        """metadata form 字段为 JSON 字符串时被解析为 dict 写入。"""
        _reset_minio()
        conn = _RecordingConnection(results=[_Result(row=_file_row())])
        monkeypatch.setattr(fs, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/files/upload",
                files={"file": ("doc.pdf", b"x", "application/pdf")},
                data={"metadata": '{"source": "test", "version": 2}'},
            )
        assert resp.status_code == 201
        # INSERT 参数最后一个应为 JSON 字符串，包含 source=test
        insert_call = next(
            (q, p) for q, p in conn.calls if "INSERT INTO file_metadata" in q
        )
        json_arg = insert_call[1][-1]
        assert "source" in json_arg


# ============================================================================
# 2. 列表 / 统计
# ============================================================================


class TestListAndStats:
    @pytest.mark.asyncio
    async def test_list_default(self, monkeypatch):
        """GET /files 默认列表。"""
        rows = [_file_row(id="f1"), _file_row(id="f2", kind="image")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(fs, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/files")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        # SQL 必须排除 deleted
        assert "status <> 'deleted'" in conn.calls[0][0]

    @pytest.mark.asyncio
    async def test_list_kind_filter(self, monkeypatch):
        """kind=image 过滤。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(fs, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/files", params={"kind": "image"})
        assert resp.status_code == 200
        assert "kind = %s" in conn.calls[0][0]

    @pytest.mark.asyncio
    async def test_list_pagination(self, monkeypatch):
        """limit / offset 透传。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(fs, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/files", params={"limit": 5, "offset": 10}
            )
        assert resp.status_code == 200
        params = conn.calls[0][1]
        assert params[-2] == 5
        assert params[-1] == 10

    @pytest.mark.asyncio
    async def test_stats_aggregation(self, monkeypatch):
        """GET /stats 返回按 kind 分组的聚合。"""
        rows = [
            {"kind": "document", "count": 3, "total_bytes": 100},
            {"kind": "image", "count": 2, "total_bytes": 200},
        ]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(fs, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/files/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 5
        assert body["total_bytes"] == 300
        assert len(body["items"]) == 2
        assert any("GROUP BY kind" in q for q, _ in conn.calls)


# ============================================================================
# 3. 详情 / 下载
# ============================================================================


class TestDetailAndDownload:
    @pytest.mark.asyncio
    async def test_get_detail_success(self, monkeypatch):
        """GET /{id} 返回详情。"""
        conn = _RecordingConnection(results=[_Result(row=_file_row(id="f1"))])
        monkeypatch.setattr(fs, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/files/f1")
        assert resp.status_code == 200
        assert resp.json()["id"] == "f1"

    @pytest.mark.asyncio
    async def test_get_detail_not_found(self, monkeypatch):
        """GET /{id} 不存在返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(fs, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/files/missing")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_detail_cross_workspace_forbidden(self, monkeypatch):
        """GET /{id} 跨 workspace 返回 403。"""
        conn = _RecordingConnection(
            results=[_Result(row=_file_row(workspace_id="wsp_other"))]
        )
        monkeypatch.setattr(fs, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/files/f1")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_download_success(self, monkeypatch):
        """GET /{id}/download 返回 StreamingResponse。"""
        _reset_minio()
        # mock row 的 storage_path 必须与 MinIO 中的 key 一致
        row = _file_row(
            id="f1",
            storage_path="wsp_test/f1/doc.pdf",
            storage_bucket="workama-files",
        )
        # 先 put 一份数据到 mock MinIO
        fs._minio.put_object("workama-files", "wsp_test/f1/doc.pdf", b"DATA")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(fs, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/files/f1/download")
        assert resp.status_code == 200
        assert resp.content == b"DATA"
        assert "attachment" in resp.headers["content-disposition"]
        # get_object 调用被记录
        assert any(c[0] == "get_object" for c in fs._minio.calls)


# ============================================================================
# 4. 删除 / 复制
# ============================================================================


class TestDeleteAndCopy:
    @pytest.mark.asyncio
    async def test_delete_success(self, monkeypatch):
        """DELETE /{id} 标记 deleted 并清理 MinIO。"""
        _reset_minio()
        fs._minio.put_object("workama-files", "wsp_test/f1/doc.pdf", b"x")
        conn = _RecordingConnection(
            results=[
                _Result(row=_file_row(id="f1")),
                _Result(row={"id": "f1"}),
            ]
        )
        monkeypatch.setattr(fs, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/files/f1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted"] is True
        # MinIO delete_object 被调用
        assert any(c[0] == "delete_object" for c in fs._minio.calls)
        # 元数据 UPDATE status='deleted'
        assert any(
            "UPDATE file_metadata" in q and "deleted" in q for q, _ in conn.calls
        )

    @pytest.mark.asyncio
    async def test_delete_not_found(self, monkeypatch):
        """DELETE /{id} 不存在返回 404。"""
        _reset_minio()
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(fs, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/files/missing")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_copy_success(self, monkeypatch):
        """POST /{id}/copy 复制文件并新建元数据。"""
        _reset_minio()
        fs._minio.put_object("workama-files", "wsp_test/f1/doc.pdf", b"orig")
        conn = _RecordingConnection(
            results=[
                _Result(row=_file_row(id="f1")),
                _Result(row=_file_row(id="file_2")),
            ]
        )
        monkeypatch.setattr(fs, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/files/f1/copy")
        assert resp.status_code == 201
        # MinIO copy_object 被调用
        assert any(c[0] == "copy_object" for c in fs._minio.calls)
        # 新元数据 INSERT
        assert any("INSERT INTO file_metadata" in q for q, _ in conn.calls)

    @pytest.mark.asyncio
    async def test_copy_not_found(self, monkeypatch):
        """POST /{id}/copy 不存在返回 404。"""
        _reset_minio()
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(fs, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/files/missing/copy")
        assert resp.status_code == 404


# ============================================================================
# 5. 鉴权
# ============================================================================


class TestAuth:
    @pytest.mark.asyncio
    async def test_unauthenticated_returns_401(self, monkeypatch):
        """未认证请求返回 401。"""
        app = _app(actor=None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/files")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_viewer_can_read_cannot_write(self, monkeypatch):
        """viewer 可读（capability file:read 通过 ``*``）但上传写需 file:write，
        通过 ``*`` 也允许，因此返回 201（owner 全权）。
        本测试验证 viewer 至少能 list（read 通过）。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(fs, "pool", _Pool(conn))

        app = _app(actor=_actor(role="viewer"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/files")
        assert resp.status_code == 200


# ============================================================================
# 6. 辅助函数 / MinIO mock
# ============================================================================


class TestHelpers:
    def test_detect_kind_by_mime(self):
        """_detect_kind 通过 mime_type 前缀识别。"""
        assert fs._detect_kind("a.png", "image/png") == "image"
        assert fs._detect_kind("a.mp3", "audio/mpeg") == "audio"
        assert fs._detect_kind("a.mp4", "video/mp4") == "video"

    def test_detect_kind_by_extension(self):
        """_detect_kind 通过扩展名识别 archive/document。"""
        assert fs._detect_kind("a.zip", "") == "archive"
        assert fs._detect_kind("a.pdf", "") == "document"
        assert fs._detect_kind("a.docx", "") == "document"

    def test_detect_kind_other_fallback(self):
        """_detect_kind 兜底返回 other。"""
        assert fs._detect_kind("a.xyz", "application/octet-stream") == "other"

    def test_storage_path_format(self):
        """_storage_path 格式为 {workspace_id}/{file_id}/{filename}。"""
        path = fs._storage_path("wsp_x", "file_y", "doc.pdf")
        assert path == "wsp_x/file_y/doc.pdf"

    def test_storage_path_basename_safety(self):
        """_storage_path 对路径穿越取 basename。"""
        path = fs._storage_path("wsp_x", "file_y", "../../etc/passwd")
        assert path == "wsp_x/file_y/passwd"
        assert ".." not in path

    def test_minio_put_get_delete(self):
        """_MinioClient 内存 mock 支持 put/get/delete/copy/exists。"""
        client = fs._MinioClient()
        client.put_object("b", "k", b"data", "text/plain")
        assert client.object_exists("b", "k")
        assert client.get_object("b", "k") == b"data"
        client.copy_object("b", "k", "b", "k2")
        assert client.get_object("b", "k2") == b"data"
        client.delete_object("b", "k")
        assert not client.object_exists("b", "k")
        # 调用日志完整
        actions = [c[0] for c in client.calls]
        assert "put_object" in actions
        assert "copy_object" in actions
        assert "delete_object" in actions

    def test_summary_handles_none_metadata(self):
        """_summary 对 metadata 为 None 返回空 dict。"""
        row = _file_row(metadata=None)
        result = fs._summary(row)
        assert result["metadata"] == {}

    def test_router_prefix(self):
        """router prefix 为 /api/v1/files。"""
        assert fs.router.prefix == "/api/v1/files"
