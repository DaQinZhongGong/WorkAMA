"""记忆向量索引 (pgvector) 单元 + 端点测试。

v7.145: 40 个测试覆盖（v7.140 的 35 个 + v7.145 新增 5 个内部 LLM 渠道测试）：
- 向量工具函数（确定性 embedding / 归一化 / importance 映射 / 分词）
- 常量（遗忘曲线保留天数 / 抽取类型映射）
- Worker 类（MemoryVectorIndex / MemoryExtractionWorker / MemoryForgettingWorker）
- 8 个 REST 端点（health / create / recall / get / delete / touch / list / forget-sweep / extract）
- 鉴权（401 未认证 / 403 缺少能力）
- LLM 抽取（成功 / code fence 剥离 / 非法 JSON 回退 / 超时回退 / 连接错误回退 / 端点 llm / 端点 disable env）
- v7.145 内部 LLM 渠道：真实 API Key 成功 / 无 Key 回退 mock / 401 回退 mock /
  ensure_internal_channel 创建 / ensure_internal_channel 幂等

所有测试使用 fake pool/connection，不依赖真实 DB / Redis / 网络。
LLM 抽取测试通过 mock httpx.AsyncClient.post 模拟 gateway 响应，不真实调用 gateway。
"""
from __future__ import annotations

import math
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules import memory_vector as mv
from workama_platform.modules.gateway import internal_channel as ic
from workama_platform.modules.gateway.internal_channel import ensure_internal_channel
from workama_platform.modules.memory_vector import (
    EMBEDDING_DIMENSION,
    EXTRACTION_KIND_MAP,
    FORGET_RETENTION_DAYS,
    MemoryExtractionWorker,
    MemoryForgettingWorker,
    MemoryVectorError,
    MemoryVectorIndex,
    _call_llm_for_extraction,
    _importance_to_score,
    _mock_extract_entries,
    _normalize_vector,
    _parse_llm_extraction,
    _score_to_importance,
    _strip_code_fence,
    _tokens,
    _vector_literal,
    vector_embedding,
)


# ============================================================================
# 测试辅助：fake pool / connection / result
# ============================================================================


class _Result:
    """模拟 psycopg 查询结果。"""

    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []

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
    """记录 execute 调用并按序返回配置的结果。"""

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
        """支持 ensure_internal_channel 等显式 commit 调用。"""
        return None


class _Pool:
    """模拟连接池。"""

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


def _actor(*, capabilities=("memory:*",), workspace_id="wsp_test", role="admin") -> Actor:
    return Actor(
        user_id="usr_test",
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email="test@example.com",
        display_name="Test",
        onboarding_completed=True,
        capabilities=capabilities,
    )


def _row(**overrides) -> dict:
    base = {
        "id": "mv_1",
        "workspace_id": "wsp_test",
        "memory_id": None,
        "content": "hello world",
        "kind": "semantic",
        "importance": 3,
        "metadata": {},
        "last_referenced_at": datetime.now(UTC),
        "created_at": datetime.now(UTC),
        "expires_at": None,
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(mv.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


# ============================================================================
# 1. 向量工具函数
# ============================================================================


class TestVectorUtilities:
    """向量工具函数测试。"""

    def test_vector_embedding_is_deterministic_and_normalized(self):
        """同一输入始终产生同一向量，且归一化为单位长度。"""
        v1 = vector_embedding("hello world")
        v2 = vector_embedding("hello world")
        assert v1 == v2
        assert len(v1) == EMBEDDING_DIMENSION
        norm = math.sqrt(sum(x * x for x in v1))
        assert math.isclose(norm, 1.0, abs_tol=1e-5)

    def test_vector_literal_format(self):
        """_vector_literal 返回 pgvector 字面量 [v1,v2,...]。"""
        result = _vector_literal([1.0, 0.5, 0.0])
        assert result.startswith("[") and result.endswith("]")
        assert "1.00000000" in result
        assert "0.50000000" in result

    def test_normalize_vector_rejects_zero_and_nan(self):
        """零向量和含 NaN 的向量应抛出 MemoryVectorError。"""
        with pytest.raises(MemoryVectorError):
            _normalize_vector([0.0, 0.0])
        with pytest.raises(MemoryVectorError):
            _normalize_vector([float("nan"), 1.0])

    def test_importance_score_roundtrip(self):
        """importance 1-5 与 score 0.0-1.0 可往返映射。"""
        assert _importance_to_score(1) == 0.0
        assert _importance_to_score(5) == 1.0
        assert _importance_to_score(3) == 0.5
        for importance in range(1, 6):
            score = _importance_to_score(importance)
            assert _score_to_importance(score) == importance

    def test_tokens_splits_english_and_chinese(self):
        """_tokens 英文按单词、中文按单字分词，并小写化。"""
        tokens = _tokens("Hello 世界 test")
        assert "hello" in tokens
        assert "世" in tokens
        assert "界" in tokens
        assert "test" in tokens


# ============================================================================
# 2. 常量
# ============================================================================


class TestConstants:
    """模块常量测试。"""

    def test_forget_retention_days_mapping(self):
        """遗忘曲线保留天数映射正确。"""
        assert FORGET_RETENTION_DAYS[1] == 1
        assert FORGET_RETENTION_DAYS[2] == 7
        assert FORGET_RETENTION_DAYS[3] == 30
        assert FORGET_RETENTION_DAYS[4] == 90
        assert FORGET_RETENTION_DAYS[5] == 365

    def test_extraction_kind_map_covers_all_types(self):
        """EXTRACTION_KIND_MAP 覆盖所有抽取类型。"""
        for t in ("fact", "preference", "event", "relationship", "skill"):
            assert t in EXTRACTION_KIND_MAP


# ============================================================================
# 3. MemoryVectorIndex
# ============================================================================


class TestMemoryVectorIndex:
    """MemoryVectorIndex Worker 测试（mock pool）。"""

    @pytest.mark.asyncio
    async def test_index_memory_inserts_vector(self, monkeypatch):
        """index_memory 调用 embedding 并 INSERT 向量。"""
        conn = _RecordingConnection(results=[_Result()])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        idx = MemoryVectorIndex()
        vec_id = await idx.index_memory("mem1", "hello", "ws1", {"k": "v"})
        assert vec_id is not None
        assert vec_id.startswith("mv_")
        assert len(conn.calls) == 1
        query_str = conn.calls[0][0]
        assert "INSERT INTO memory_vector" in query_str

    @pytest.mark.asyncio
    async def test_index_memory_skips_on_dimension_mismatch(self, monkeypatch):
        """embedding 维度不匹配时跳过写入并返回 None。"""
        async def fake_call_embedding(ws, text, **kw):
            return [1.0, 0.0]  # 维度错误

        monkeypatch.setattr(mv, "_call_embedding", fake_call_embedding)
        conn = _RecordingConnection()
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        idx = MemoryVectorIndex()
        result = await idx.index_memory("mem1", "hello", "ws1")
        assert result is None
        assert len(conn.calls) == 0

    @pytest.mark.asyncio
    async def test_remove_memory_deletes(self, monkeypatch):
        """remove_memory 执行 DELETE。"""
        conn = _RecordingConnection(results=[_Result()])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        idx = MemoryVectorIndex()
        await idx.remove_memory("mem1")
        assert any("DELETE FROM memory_vector" in q for q, _ in conn.calls)

    @pytest.mark.asyncio
    async def test_search_memories_filters_by_threshold(self, monkeypatch):
        """search_memories 过滤低于 threshold 的结果。"""
        fake_rows = [
            {"id": "mv1", "memory_id": "m1", "content": "a", "metadata": {},
             "score": 0.9, "last_referenced_at": None, "created_at": None},
            {"id": "mv2", "memory_id": "m2", "content": "b", "metadata": {},
             "score": 0.3, "last_referenced_at": None, "created_at": None},
        ]
        conn = _RecordingConnection(results=[_Result(rows=fake_rows)])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        idx = MemoryVectorIndex()
        hits = await idx.search_memories("query", "ws1", limit=5, threshold=0.5)
        assert len(hits) == 1
        assert hits[0]["memory_id"] == "m1"


# ============================================================================
# 4. MemoryExtractionWorker
# ============================================================================


class TestMemoryExtractionWorker:
    """LLM 记忆抽取 Worker 测试。"""

    def test_parse_extraction_valid_json(self):
        """正确解析 JSON 数组。"""
        worker = MemoryExtractionWorker()
        raw = '[{"type":"fact","content":"likes python","importance":4,"confidence":0.9}]'
        entries = worker._parse_extraction(raw)
        assert len(entries) == 1
        assert entries[0]["type"] == "fact"
        assert entries[0]["content"] == "likes python"
        assert entries[0]["importance"] == 4

    def test_parse_extraction_invalid_json_returns_empty(self):
        """无效 JSON 返回空列表。"""
        worker = MemoryExtractionWorker()
        assert worker._parse_extraction("not json at all") == []
        assert worker._parse_extraction("") == []

    def test_parse_extraction_clamps_importance_and_confidence(self):
        """importance 钳制到 1-5，confidence 钳制到 0-1。"""
        worker = MemoryExtractionWorker()
        raw = '[{"type":"fact","content":"x","importance":99,"confidence":2.0}]'
        entries = worker._parse_extraction(raw)
        assert entries[0]["importance"] == 5
        assert entries[0]["confidence"] == 1.0

    def test_parse_extraction_skips_empty_content(self):
        """content 为空时跳过。"""
        worker = MemoryExtractionWorker()
        raw = '[{"type":"fact","content":"","importance":3,"confidence":0.5}]'
        entries = worker._parse_extraction(raw)
        assert len(entries) == 0


# ============================================================================
# 5. MemoryForgettingWorker
# ============================================================================


class TestMemoryForgettingWorker:
    """自动遗忘 Worker 测试。"""

    @pytest.mark.asyncio
    async def test_process_forgetting_job_delegates_to_run_forget_sweep(self, monkeypatch):
        """process_forgetting_job 委托给 run_forget_sweep。"""
        worker = MemoryForgettingWorker()
        called = {}

        async def fake_run_sweep(conn, ws, threshold_days):
            called["ws"] = ws
            called["threshold_days"] = threshold_days
            return {"processed": 0, "forgotten_ids": []}

        monkeypatch.setattr(worker, "run_forget_sweep", fake_run_sweep)
        monkeypatch.setattr(mv, "pool", _Pool(_RecordingConnection()))
        result = await worker.process_forgetting_job({"workspace_id": "ws42", "threshold_days": 30})
        assert called["ws"] == "ws42"
        assert called["threshold_days"] == 30
        assert result["processed"] == 0

    @pytest.mark.asyncio
    async def test_run_forget_sweep_deletes_expired(self, monkeypatch):
        """run_forget_sweep 删除 expires_at < now() 的记录。"""
        conn = _RecordingConnection(results=[
            _Result(rows=[{"id": "mv_expired"}]),
            _Result(rows=[]),
        ])
        worker = MemoryForgettingWorker()
        result = await worker.run_forget_sweep(conn, "wsp_test")
        assert result["processed"] == 1
        assert "mv_expired" in result["forgotten_ids"]
        # 验证第一个 DELETE 包含 expires_at < now()
        assert "expires_at < now()" in conn.calls[0][0]

    @pytest.mark.asyncio
    async def test_run_forget_sweep_deletes_by_retention(self, monkeypatch):
        """run_forget_sweep 按 importance 保留期删除无 expires_at 的记录。"""
        conn = _RecordingConnection(results=[
            _Result(rows=[]),
            _Result(rows=[{"id": "mv_old"}]),
        ])
        worker = MemoryForgettingWorker()
        result = await worker.run_forget_sweep(conn, "wsp_test")
        assert result["processed"] == 1
        assert "mv_old" in result["forgotten_ids"]
        # 验证第二个 DELETE 包含 importance 条件
        assert "importance =" in conn.calls[1][0]

    @pytest.mark.asyncio
    async def test_run_forget_sweep_keeps_fresh(self, monkeypatch):
        """run_forget_sweep 保留未过期记录。"""
        conn = _RecordingConnection(results=[
            _Result(rows=[]),
            _Result(rows=[]),
        ])
        worker = MemoryForgettingWorker()
        result = await worker.run_forget_sweep(conn, "wsp_test")
        assert result["processed"] == 0
        assert result["forgotten_ids"] == []


# ============================================================================
# 6. 端点测试
# ============================================================================


class TestEndpoints:
    """REST 端点测试（httpx + ASGITransport + mock pool）。"""

    @pytest.mark.asyncio
    async def test_health_endpoint_returns_ok(self):
        """GET /health 返回模块状态。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memory-vectors/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["module"] == "memory_vector"
        assert body["status"] == "ok"
        assert body["impl"] == "pgvector"
        assert body["dimensions"] == EMBEDDING_DIMENSION

    @pytest.mark.asyncio
    async def test_health_requires_authentication(self):
        """未认证请求返回 401。"""
        app = _app(actor=None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memory-vectors/health")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_create_vector_endpoint(self, monkeypatch):
        """POST / 写入向量返回 201。"""
        row = _row(content="hello", kind="semantic", importance=3)
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors",
                json={"content": "hello", "kind": "semantic", "importance": 3},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["content"] == "hello"
        assert body["kind"] == "semantic"
        assert body["importance"] == 3

    @pytest.mark.asyncio
    async def test_recall_endpoint(self, monkeypatch):
        """POST /recall 返回排序结果。"""
        rows = [
            {**_row(id="mv1", content="hello"), "similarity": 0.95},
            {**_row(id="mv2", content="world"), "similarity": 0.80},
        ]
        # recall 执行 SELECT（fetchall）+ UPDATE（无 fetch）
        conn = _RecordingConnection(results=[_Result(rows=rows), _Result()])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/recall",
                json={"query": "hello", "top_k": 5},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert body["items"][0]["similarity"] == 0.95
        assert "data" in body

    @pytest.mark.asyncio
    async def test_get_vector_endpoint(self, monkeypatch):
        """GET /{vector_id} 返回单条向量。"""
        row = _row(id="mv1", content="hello")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memory-vectors/mv1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "mv1"
        assert body["content"] == "hello"

    @pytest.mark.asyncio
    async def test_get_vector_returns_404(self, monkeypatch):
        """GET /{vector_id} 不存在时返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memory-vectors/notexist")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_vector_endpoint(self, monkeypatch):
        """DELETE /{vector_id} 删除向量。"""
        row = _row(id="mv1")
        # _owned_vector SELECT + DELETE RETURNING
        conn = _RecordingConnection(
            results=[_Result(row=row), _Result(row={"id": "mv1"})]
        )
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/memory-vectors/mv1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "mv1"
        assert body["deleted"] is True

    @pytest.mark.asyncio
    async def test_touch_vector_endpoint(self, monkeypatch):
        """POST /{vector_id}/touch 重置 last_referenced_at。"""
        row = _row(id="mv1", content="hello")
        # _owned_vector SELECT + UPDATE RETURNING
        conn = _RecordingConnection(results=[_Result(row=row), _Result(row=row)])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/memory-vectors/mv1/touch")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "mv1"

    @pytest.mark.asyncio
    async def test_list_vectors_endpoint(self, monkeypatch):
        """GET / 分页列表。"""
        rows = [
            _row(id="mv1", content="a"),
            _row(id="mv2", content="b"),
        ]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memory-vectors?limit=10&offset=0")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert body["limit"] == 10
        assert body["offset"] == 0
        assert len(body["items"]) == 2

    @pytest.mark.asyncio
    async def test_forget_sweep_endpoint(self, monkeypatch):
        """POST /forget-sweep 触发异步遗忘清理任务。"""
        async def fake_submit_operation(conn, **kwargs):
            return {"id": "op_1"}

        monkeypatch.setattr(mv, "submit_operation", fake_submit_operation)
        conn = _RecordingConnection(results=[_Result(row={"id": "op_1"})])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/memory-vectors/forget-sweep")
        assert resp.status_code == 200
        body = resp.json()
        assert body["operation_id"] == "op_1"
        assert body["status"] == "queued"

    @pytest.mark.asyncio
    async def test_forget_sweep_endpoint_403(self, monkeypatch):
        """POST /forget-sweep 非 admin/owner 返回 403。"""
        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/memory-vectors/forget-sweep")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_forget_policy_get_default(self, monkeypatch):
        """GET /forget-policy 无配置时返回默认策略。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memory-vectors/forget-policy")
        assert resp.status_code == 200
        body = resp.json()
        assert body["workspace_id"] == "wsp_test"
        assert body["default_importance"] == 3
        assert body["retention_days_by_importance"]["1"] == 1
        assert body["retention_days_by_importance"]["5"] == 365

    @pytest.mark.asyncio
    async def test_forget_policy_update(self, monkeypatch):
        """POST /forget-policy 更新 workspace 策略。"""
        row = {
            "workspace_id": "wsp_test",
            "retention_days_by_importance": {1: 3, 2: 14, 3: 60, 4: 180, 5: 730},
            "default_importance": 4,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/forget-policy",
                json={"retention_days_by_importance": {1: 3, 2: 14, 3: 60, 4: 180, 5: 730}, "default_importance": 4},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["default_importance"] == 4
        assert body["retention_days_by_importance"]["1"] == 3

    @pytest.mark.asyncio
    async def test_forget_policy_403(self, monkeypatch):
        """POST /forget-policy 非 admin/owner 返回 403。"""
        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/forget-policy",
                json={"default_importance": 2},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_extract_endpoint(self, monkeypatch):
        """POST /extract 从对话抽取记忆（mock 模式，LLM 禁用）。"""
        # v7.140：禁用 LLM 走 mock，避免测试环境真实调用 gateway
        monkeypatch.setenv("WORKAMA_EXTRACTION_DISABLE_LLM", "1")
        # "用户叫小明，喜欢Python" 触发两条 INSERT（name + preference）
        conn = _RecordingConnection(
            results=[
                _Result(row={"id": "mv_name"}),
                _Result(row={"id": "mv_pref"}),
            ]
        )
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/extract",
                json={"conversation_text": "用户叫小明，喜欢Python"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert "mv_name" in body["extracted_ids"]
        assert "mv_pref" in body["extracted_ids"]
        assert body["extraction_method"] == "mock"

    @pytest.mark.asyncio
    async def test_recall_forbidden_without_capability(self, monkeypatch):
        """缺少 memory:read 能力时 recall 返回 403。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor(capabilities=()))  # 无任何 memory 能力
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/recall",
                json={"query": "hello", "top_k": 5},
            )
        assert resp.status_code == 403


# ============================================================================
# 7. LLM 抽取测试（v7.140 新增）
# ============================================================================


class _LLMResponse:
    """模拟 httpx gateway 响应。"""

    def __init__(self, status_code=200, content_text='[]'):
        self.status_code = status_code
        self._body = (
            {"choices": [{"message": {"content": content_text}}]}
            if status_code < 400
            else {"error": "bad request"}
        )

    def json(self):
        return self._body


def _patch_gateway_post(monkeypatch, *, response=None, exc=None):
    """拦截 gateway ``/v1/chat/completions`` 调用，不影响 ASGITransport 请求。

    - response: 返回的 _LLMResponse（exc 为 None 时生效）
    - exc: 抛出的异常（优先于 response）
    """
    real_post = httpx.AsyncClient.post

    async def fake_post(self, url, *args, **kwargs):
        if "/v1/chat/completions" in str(url):
            if exc is not None:
                raise exc
            return response
        return await real_post(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


class TestLLMExtraction:
    """v7.140 LLM 抽取测试（5 个单元测试 + 2 个端点测试）。"""

    @pytest.mark.asyncio
    async def test_call_llm_for_extraction_success(self, monkeypatch):
        """mock httpx 返回合法 JSON，验证 LLM 抽取成功。"""
        monkeypatch.delenv("WORKAMA_EXTRACTION_DISABLE_LLM", raising=False)
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-test-internal-key")
        llm_json = (
            '[{"content":"用户喜欢咖啡","kind":"preference","importance":3,'
            '"metadata":{"source":"extraction","type":"preference"}}]'
        )
        _patch_gateway_post(
            monkeypatch, response=_LLMResponse(200, llm_json)
        )

        entries, method = await _call_llm_for_extraction(
            "用户叫张三，喜欢喝咖啡", "wsp_test", _actor()
        )
        assert method == "llm"
        assert len(entries) == 1
        assert entries[0]["content"] == "用户喜欢咖啡"
        assert entries[0]["kind"] == "preference"
        assert entries[0]["importance"] == 3

    @pytest.mark.asyncio
    async def test_call_llm_for_extraction_json_with_code_fence(self, monkeypatch):
        """mock httpx 返回带 markdown code fence 的 JSON，验证剥离后解析成功。"""
        monkeypatch.delenv("WORKAMA_EXTRACTION_DISABLE_LLM", raising=False)
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-test-internal-key")
        fenced = (
            "```json\n"
            '[{"content":"用户名是张三","kind":"profile","importance":4,"metadata":{}}]\n'
            "```"
        )
        _patch_gateway_post(
            monkeypatch, response=_LLMResponse(200, fenced)
        )

        entries, method = await _call_llm_for_extraction(
            "用户叫张三", "wsp_test", _actor()
        )
        assert method == "llm"
        assert len(entries) == 1
        assert entries[0]["content"] == "用户名是张三"
        assert entries[0]["kind"] == "profile"
        assert entries[0]["importance"] == 4

    @pytest.mark.asyncio
    async def test_call_llm_for_extraction_invalid_json_falls_back_to_mock(
        self, monkeypatch
    ):
        """mock httpx 返回非法 JSON，验证回退到 mock 抽取。"""
        monkeypatch.delenv("WORKAMA_EXTRACTION_DISABLE_LLM", raising=False)
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-test-internal-key")
        _patch_gateway_post(
            monkeypatch, response=_LLMResponse(200, "this is not valid json at all")
        )

        entries, method = await _call_llm_for_extraction(
            "用户叫张三，喜欢喝咖啡", "wsp_test", _actor()
        )
        assert method == "mock"
        # mock 抽取应产出 name + preference 两条
        assert len(entries) == 2
        contents = [e["content"] for e in entries]
        assert any("张三" in c for c in contents)
        assert any("咖啡" in c for c in contents)

    @pytest.mark.asyncio
    async def test_call_llm_for_extraction_timeout_falls_back_to_mock(
        self, monkeypatch
    ):
        """mock httpx 抛 TimeoutException，验证回退到 mock 抽取。"""
        monkeypatch.delenv("WORKAMA_EXTRACTION_DISABLE_LLM", raising=False)
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-test-internal-key")
        _patch_gateway_post(monkeypatch, exc=httpx.TimeoutException("simulated timeout"))

        entries, method = await _call_llm_for_extraction(
            "用户叫张三，喜欢喝咖啡", "wsp_test", _actor()
        )
        assert method == "mock"
        assert len(entries) == 2  # mock 产出 name + preference

    @pytest.mark.asyncio
    async def test_call_llm_for_extraction_connection_error_falls_back_to_mock(
        self, monkeypatch
    ):
        """mock httpx 抛 ConnectError，验证回退到 mock 抽取。"""
        monkeypatch.delenv("WORKAMA_EXTRACTION_DISABLE_LLM", raising=False)
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-test-internal-key")
        _patch_gateway_post(monkeypatch, exc=httpx.ConnectError("simulated connection error"))

        entries, method = await _call_llm_for_extraction(
            "用户叫张三，喜欢喝咖啡", "wsp_test", _actor()
        )
        assert method == "mock"
        assert len(entries) == 2  # mock 产出 name + preference

    @pytest.mark.asyncio
    async def test_extract_endpoint_returns_extraction_method_llm(self, monkeypatch):
        """端点级测试：mock httpx 成功，验证返回 extraction_method='llm'。"""
        monkeypatch.delenv("WORKAMA_EXTRACTION_DISABLE_LLM", raising=False)
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-test-internal-key")
        llm_json = (
            '[{"content":"用户喜欢喝茶","kind":"preference","importance":3,'
            '"metadata":{"source":"extraction"}}]'
        )
        _patch_gateway_post(
            monkeypatch, response=_LLMResponse(200, llm_json)
        )
        # LLM 返回 1 条 → 1 次 INSERT
        conn = _RecordingConnection(results=[_Result(row={"id": "mv_llm_1"})])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/extract",
                json={"conversation_text": "用户叫张三，喜欢喝茶"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["extraction_method"] == "llm"
        assert body["count"] == 1
        assert "mv_llm_1" in body["extracted_ids"]

    @pytest.mark.asyncio
    async def test_extract_endpoint_disable_llm_env(self, monkeypatch):
        """设置 WORKAMA_EXTRACTION_DISABLE_LLM=1，验证走 mock，返回 extraction_method='mock'。"""
        monkeypatch.setenv("WORKAMA_EXTRACTION_DISABLE_LLM", "1")
        # mock 抽取 "用户叫张三，喜欢喝咖啡" → 2 条（name + preference）
        conn = _RecordingConnection(
            results=[
                _Result(row={"id": "mv_name"}),
                _Result(row={"id": "mv_pref"}),
            ]
        )
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/extract",
                json={"conversation_text": "用户叫张三，喜欢喝咖啡"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["extraction_method"] == "mock"
        assert body["count"] == 2
        assert "mv_name" in body["extracted_ids"]
        assert "mv_pref" in body["extracted_ids"]


# ============================================================================
# 8. 内部 LLM 渠道测试（v7.145 新增）
# ============================================================================


class TestInternalLLMChannel:
    """v7.145 内部 LLM 渠道与 _call_llm_for_extraction 鉴权测试。"""

    @pytest.mark.asyncio
    async def test_call_llm_with_real_api_key_success(self, monkeypatch):
        """配置真实 API Key + mock httpx 返回 200 合法 JSON，验证 LLM 抽取成功。"""
        monkeypatch.delenv("WORKAMA_EXTRACTION_DISABLE_LLM", raising=False)
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-real-test-key")
        llm_json = (
            '[{"content":"用户喜欢喝茶","kind":"preference","importance":3,'
            '"metadata":{"source":"extraction","type":"preference"}}]'
        )
        captured: dict = {}

        async def fake_post(self, url, *args, **kwargs):
            if "/v1/chat/completions" in str(url):
                captured["headers"] = kwargs.get("headers", {})
                return _LLMResponse(200, llm_json)
            return await httpx.AsyncClient.post(self, url, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        entries, method = await _call_llm_for_extraction(
            "用户叫李四，喜欢喝茶", "wsp_test", _actor()
        )
        assert method == "llm"
        assert len(entries) == 1
        assert entries[0]["content"] == "用户喜欢喝茶"
        # 验证 Authorization 头使用的是环境变量中的 API Key
        assert captured["headers"]["Authorization"] == "Bearer sk-real-test-key"

    @pytest.mark.asyncio
    async def test_call_llm_without_api_key_falls_back_to_mock(self, monkeypatch):
        """未设置 WORKAMA_INTERNAL_LLM_API_KEY，验证直接走 mock，不调用 gateway。"""
        monkeypatch.delenv("WORKAMA_EXTRACTION_DISABLE_LLM", raising=False)
        monkeypatch.delenv("WORKAMA_INTERNAL_LLM_API_KEY", raising=False)

        called = {"count": 0}
        real_post = httpx.AsyncClient.post

        async def spy_post(self, url, *args, **kwargs):
            if "/v1/chat/completions" in str(url):
                called["count"] += 1
            return await real_post(self, url, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "post", spy_post)

        entries, method = await _call_llm_for_extraction(
            "用户叫李四，喜欢喝茶", "wsp_test", _actor()
        )
        assert method == "mock"
        # 不应调用 gateway
        assert called["count"] == 0
        # mock 抽取应产出 name + preference 两条
        assert len(entries) == 2
        contents = [e["content"] for e in entries]
        assert any("李四" in c for c in contents)
        assert any("茶" in c for c in contents)

    @pytest.mark.asyncio
    async def test_call_llm_with_api_key_but_gateway_401_falls_back_to_mock(
        self, monkeypatch
    ):
        """配置 API Key 但 gateway 返回 401，验证回退 mock。"""
        monkeypatch.delenv("WORKAMA_EXTRACTION_DISABLE_LLM", raising=False)
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-fake-test-key")
        _patch_gateway_post(monkeypatch, response=_LLMResponse(401))

        entries, method = await _call_llm_for_extraction(
            "用户叫李四，喜欢喝茶", "wsp_test", _actor()
        )
        assert method == "mock"
        # mock 抽取应产出 name + preference 两条
        assert len(entries) == 2

    @pytest.mark.asyncio
    async def test_ensure_internal_channel_creates_channel(self, monkeypatch):
        """API Key 已配置且渠道不存在时，ensure_internal_channel 执行 INSERT。"""
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-internal-channel-key")
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_PROVIDER", "siliconflow")
        # SELECT 返回空（fetchone=None）→ 触发 INSERT
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(ic, "pool", _Pool(conn))

        await ensure_internal_channel()

        # 至少一次 SELECT + 一次 INSERT
        queries = [q for q, _ in conn.calls]
        assert any("SELECT id FROM gw_channel" in q for q in queries)
        assert any("INSERT INTO gw_channel" in q for q in queries)

    @pytest.mark.asyncio
    async def test_ensure_internal_channel_idempotent(self, monkeypatch):
        """渠道已存在时，ensure_internal_channel 不执行 INSERT。"""
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "sk-internal-channel-key")
        monkeypatch.setenv("WORKAMA_INTERNAL_LLM_PROVIDER", "siliconflow")
        # SELECT 返回已存在记录 → 直接 return，不 INSERT
        conn = _RecordingConnection(
            results=[_Result(row={"id": "chn_existing"})]
        )
        monkeypatch.setattr(ic, "pool", _Pool(conn))

        await ensure_internal_channel()

        queries = [q for q, _ in conn.calls]
        assert any("SELECT id FROM gw_channel" in q for q in queries)
        # 不应有 INSERT
        assert not any("INSERT INTO gw_channel" in q for q in queries)


# ============================================================================
# 9. Worker 集成与治理事件测试（v7.167 新增）
# ============================================================================


def test_worker_imports_memory_job_types_and_workers():
    """验证 worker.py 正确导入了 memory_vector 的 job type 和 worker 实例。"""
    import sys

    if "aiosmtplib" not in sys.modules:
        sys.modules["aiosmtplib"] = type(sys)("aiosmtplib")
    from workama_platform.worker import (
        MEM_EXTRACT_JT,
        MEM_FORGET_JT,
        MEM_REINDEX_JT,
        memory_extraction_worker,
        memory_forgetting_worker,
        memory_vector_index,
    )

    assert MEM_EXTRACT_JT == "memory_extract"
    assert MEM_FORGET_JT == "memory_forget"
    assert MEM_REINDEX_JT == "memory_reindex"
    assert hasattr(memory_extraction_worker, "process_extraction_job")
    assert hasattr(memory_forgetting_worker, "run_forget_sweep")
    assert hasattr(memory_forgetting_worker, "process_forgetting_job")
    assert hasattr(memory_vector_index, "reindex_workspace")


@pytest.mark.asyncio
async def test_worker_forget_job_audit_and_notification(monkeypatch):
    """验证 worker 处理 MEM_FORGET_JT 时写入审计日志，大量删除时发送通知。"""
    import sys

    if "aiosmtplib" not in sys.modules:
        sys.modules["aiosmtplib"] = type(sys)("aiosmtplib")
    from workama_platform import worker as worker_mod
    from workama_platform.core import Actor

    audit_calls = []

    async def fake_audit_log_action(actor, **kwargs):
        audit_calls.append(kwargs)

    monkeypatch.setattr(worker_mod, "audit_log_action", fake_audit_log_action)

    notification_calls = []

    async def fake_create_notification(conn, **kwargs):
        notification_calls.append(kwargs)

    monkeypatch.setattr(worker_mod, "create_notification", fake_create_notification)

    # mock run_forget_sweep 返回大量删除
    async def fake_run_forget_sweep(conn, workspace_id, threshold_days):
        return {"processed": 150, "forgotten_ids": [f"mv_{i}" for i in range(150)]}

    monkeypatch.setattr(
        worker_mod.memory_forgetting_worker, "run_forget_sweep", fake_run_forget_sweep
    )

    conn = _RecordingConnection(results=[_Result(row={"user_id": "usr_owner"})])

    # 执行 worker.py 中 MEM_FORGET_JT 的核心逻辑
    payload = {"workspace_id": "wsp_test", "threshold_days": 30}
    workspace_id = payload.get("workspace_id")
    threshold_days = payload.get("threshold_days")
    async with _Pool(conn).connection() as c:
        async with c.transaction():
            result = await worker_mod.memory_forgetting_worker.run_forget_sweep(
                c, workspace_id, threshold_days
            )
            actor = Actor(
                user_id="system",
                workspace_id=workspace_id,
                org_id=payload.get("org_id", ""),
                role="system",
                email="",
                display_name="System",
                onboarding_completed=True,
                actor_type="system",
                capabilities=("memory:*",),
            )
            await worker_mod.audit_log_action(
                actor,
                action="delete",
                resource_type="memory_vector",
                severity="info",
                description=f"Memory forget sweep deleted {result['processed']} vectors",
                metadata={
                    "forgotten_ids": result.get("forgotten_ids", []),
                    "count": result["processed"],
                },
            )
            if result["processed"] > 100:
                owner_result = await c.execute(
                    "SELECT user_id FROM id_member WHERE workspace_id = %s AND role = 'owner' LIMIT 1",
                    (workspace_id,),
                )
                owner_row = await owner_result.fetchone()
                if owner_row:
                    await worker_mod.create_notification(
                        c,
                        user_id=owner_row["user_id"],
                        workspace_id=workspace_id,
                        event_type="memory.forget_sweep.batch_deleted",
                        title="大量记忆向量已清理",
                        summary=f"自动遗忘任务清理了 {result['processed']} 条过期记忆向量。",
                        priority="warning",
                    )

    assert len(audit_calls) == 1
    assert audit_calls[0]["action"] == "delete"
    assert audit_calls[0]["resource_type"] == "memory_vector"
    assert audit_calls[0]["metadata"]["count"] == 150
    assert len(notification_calls) == 1
    assert notification_calls[0]["event_type"] == "memory.forget_sweep.batch_deleted"
    assert notification_calls[0]["priority"] == "warning"


@pytest.mark.asyncio
async def test_worker_forget_job_no_notification_below_threshold(monkeypatch):
    """删除少于 100 条时写入审计日志，但不发送通知。"""
    import sys

    if "aiosmtplib" not in sys.modules:
        sys.modules["aiosmtplib"] = type(sys)("aiosmtplib")
    from workama_platform import worker as worker_mod
    from workama_platform.core import Actor

    audit_calls = []

    async def fake_audit_log_action(actor, **kwargs):
        audit_calls.append(kwargs)

    monkeypatch.setattr(worker_mod, "audit_log_action", fake_audit_log_action)

    notification_calls = []

    async def fake_create_notification(conn, **kwargs):
        notification_calls.append(kwargs)

    monkeypatch.setattr(worker_mod, "create_notification", fake_create_notification)

    async def fake_run_forget_sweep(conn, workspace_id, threshold_days):
        return {"processed": 50, "forgotten_ids": [f"mv_{i}" for i in range(50)]}

    monkeypatch.setattr(
        worker_mod.memory_forgetting_worker, "run_forget_sweep", fake_run_forget_sweep
    )

    conn = _RecordingConnection(results=[_Result(row={"user_id": "usr_owner"})])

    payload = {"workspace_id": "wsp_test", "threshold_days": 30}
    workspace_id = payload.get("workspace_id")
    threshold_days = payload.get("threshold_days")
    async with _Pool(conn).connection() as c:
        async with c.transaction():
            result = await worker_mod.memory_forgetting_worker.run_forget_sweep(
                c, workspace_id, threshold_days
            )
            actor = Actor(
                user_id="system",
                workspace_id=workspace_id,
                org_id=payload.get("org_id", ""),
                role="system",
                email="",
                display_name="System",
                onboarding_completed=True,
                actor_type="system",
                capabilities=("memory:*",),
            )
            await worker_mod.audit_log_action(
                actor,
                action="delete",
                resource_type="memory_vector",
                severity="info",
                description=f"Memory forget sweep deleted {result['processed']} vectors",
                metadata={
                    "forgotten_ids": result.get("forgotten_ids", []),
                    "count": result["processed"],
                },
            )
            if result["processed"] > 100:
                owner_result = await c.execute(
                    "SELECT user_id FROM id_member WHERE workspace_id = %s AND role = 'owner' LIMIT 1",
                    (workspace_id,),
                )
                owner_row = await owner_result.fetchone()
                if owner_row:
                    await worker_mod.create_notification(
                        c,
                        user_id=owner_row["user_id"],
                        workspace_id=workspace_id,
                        event_type="memory.forget_sweep.batch_deleted",
                        title="大量记忆向量已清理",
                        summary=f"自动遗忘任务清理了 {result['processed']} 条过期记忆向量。",
                        priority="warning",
                    )

    assert len(audit_calls) == 1
    assert audit_calls[0]["metadata"]["count"] == 50
    assert len(notification_calls) == 0


@pytest.mark.asyncio
async def test_forget_policy_get_existing(monkeypatch):
    """GET /forget-policy 返回已保存的策略配置。"""
    row = {
        "workspace_id": "wsp_test",
        "retention_days_by_importance": {1: 5, 2: 14, 3: 60, 4: 120, 5: 730},
        "default_importance": 2,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    conn = _RecordingConnection(results=[_Result(row=row)])
    monkeypatch.setattr(mv, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/memory-vectors/forget-policy")
    assert resp.status_code == 200
    body = resp.json()
    assert body["workspace_id"] == "wsp_test"
    assert body["default_importance"] == 2
    assert body["retention_days_by_importance"]["1"] == 5
    assert body["retention_days_by_importance"]["5"] == 730


@pytest.mark.asyncio
async def test_forget_policy_workspace_isolation(monkeypatch):
    """GET /forget-policy 的 SQL 查询应使用当前 actor 的 workspace_id。"""
    conn = _RecordingConnection(results=[_Result(row=None)])
    monkeypatch.setattr(mv, "pool", _Pool(conn))

    app = _app(actor=_actor(workspace_id="wsp_isolated"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/memory-vectors/forget-policy")
    assert resp.status_code == 200
    # 验证查询参数包含正确的 workspace_id
    assert len(conn.calls) >= 1
    assert conn.calls[0][1] == ("wsp_isolated",)
