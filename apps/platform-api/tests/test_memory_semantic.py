"""记忆语义层治理深化单元测试（v7.170）。

覆盖：
- 语义聚类：空 workspace / 单条记忆 / 多条归一簇 / threshold 过滤 / cluster_summary 生成 / LLM 失败 fallback / 聚类结果列表分页
- 重要性调整：成功 / 校验 1-5 边界 / 0 和 6 拒绝 / 写 governance_log / 不存在 404 / 跨 workspace 403
- 衰减报告：空报告 / 单条报告字段完整 / decay_score 范围 0-1 / 预计保留天数计算 / 采样点数量 / merged 记忆排除
- 检索重排序：空候选 / 单候选 / 多候选加权排序 / 分数计算 / query 为空拒绝
- 统计仪表盘：空统计 / 按 importance 分布 / 按 status 分布 / 7 天趋势 / 治理操作计数
- 显式合并：成功 / source reference_count 累加到 target / source merged_into 正确 / source status=merged / 写 governance_log / 不存在 404 / 跨 workspace 403 / 已 merged 拒绝
- 辅助函数：_cluster_view / _decay_report_view / _stats_view / _merge_view / _compute_decay_score / _compute_decay_curve / _predicted_retention_days / _importance_factor / _recency_factor / _reference_factor
- SCHEMA_STATEMENTS 与 ensure_memory_semantic_schema

所有测试使用 fake pool/connection，不依赖真实 DB / 网络。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules import memory_vector as mv
from workama_platform.modules.memory_vector import (
    SCHEMA_STATEMENTS,
    ClusterRequest,
    ImportanceAdjustRequest,
    MergeRequest,
    RerankRequest,
    _cluster_view,
    _compute_decay_curve,
    _compute_decay_score,
    _decay_report_view,
    _generate_cluster_summary,
    _importance_factor,
    _importance_to_score,
    _merge_view,
    _predicted_retention_days,
    _recency_factor,
    _reference_factor,
    _stats_view,
    ensure_memory_semantic_schema,
    vector_embedding,
)


# ============================================================================
# 测试辅助：fake pool / connection / result
# ============================================================================


class _Result:
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
    """构造 memory_vector 行 mock。"""
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
        "reference_count": 0,
        "embedding": vector_embedding("hello world"),
        "cluster_id": None,
        "merged_into": None,
        "decay_score": None,
        "status": "active",
    }
    base.update(overrides)
    return base


def _cluster_row(**overrides) -> dict:
    """构造 memory_cluster 行 mock。"""
    base = {
        "id": "mc_1",
        "workspace_id": "wsp_test",
        "cluster_label": "cluster_1",
        "centroid_text": "hello world",
        "member_count": 3,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
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
# 1. 语义聚类
# ============================================================================


class TestClusterEndpoint:
    """POST /cluster 语义聚类端点测试。"""

    @pytest.mark.asyncio
    async def test_cluster_empty_workspace(self, monkeypatch):
        """空 workspace 返回 0 个聚类。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/cluster",
                json={"threshold": 0.85},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["cluster_count"] == 0
        assert body["clusters"] == []

    @pytest.mark.asyncio
    async def test_cluster_single_memory(self, monkeypatch):
        """单条记忆归入一个聚类。"""
        row = _row(id="mv_1", content="hello world")
        conn = _RecordingConnection(results=[_Result(rows=[row])])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/cluster",
                json={"threshold": 0.85},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["cluster_count"] == 1
        assert body["clusters"][0]["member_count"] == 1
        assert body["clusters"][0]["members"][0]["vector_id"] == "mv_1"

    @pytest.mark.asyncio
    async def test_cluster_multiple_same_cluster(self, monkeypatch):
        """多条相似记忆归入同一簇。"""
        emb = vector_embedding("hello world")
        rows = [
            _row(id="mv_a", content="hello world", embedding=emb),
            _row(id="mv_b", content="hello world", embedding=emb),
        ]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/cluster",
                json={"threshold": 0.85},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["cluster_count"] == 1
        assert body["clusters"][0]["member_count"] == 2

    @pytest.mark.asyncio
    async def test_cluster_threshold_filter(self, monkeypatch):
        """高 threshold 时不相似记忆分入不同簇。"""
        rows = [
            _row(id="mv_a", content="apple pie", embedding=vector_embedding("apple pie")),
            _row(id="mv_b", content="zebra crossing", embedding=vector_embedding("zebra crossing")),
        ]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/cluster",
                json={"threshold": 0.99},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["cluster_count"] == 2

    @pytest.mark.asyncio
    async def test_cluster_summary_fallback(self, monkeypatch):
        """_call_llm 默认返回 "[]"，cluster_summary fallback 到最短成员 content[:100]。"""
        row = _row(id="mv_1", content="python programming tips")
        conn = _RecordingConnection(results=[_Result(rows=[row])])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/cluster",
                json={"threshold": 0.85},
            )
        assert resp.status_code == 200
        body = resp.json()
        # fallback 取最短成员 content 前 100 字符
        assert body["clusters"][0]["cluster_summary"] == "python programming tips"

    @pytest.mark.asyncio
    async def test_cluster_summary_llm_success(self, monkeypatch):
        """LLM 返回有效摘要时使用 LLM 结果。"""
        async def fake_llm(workspace_id, messages, **kwargs):
            return "Python coding cluster"

        monkeypatch.setattr(mv, "_call_llm", fake_llm)
        row = _row(id="mv_1", content="python programming tips")
        conn = _RecordingConnection(results=[_Result(rows=[row])])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/cluster",
                json={"threshold": 0.85},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["clusters"][0]["cluster_summary"] == "Python coding cluster"

    @pytest.mark.asyncio
    async def test_cluster_unauthorized(self, monkeypatch):
        """缺少 memory:write 能力返回 403。"""
        app = _app(actor=_actor(capabilities=("memory:read",)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/cluster",
                json={"threshold": 0.85},
            )
        assert resp.status_code == 403


class TestListClustersEndpoint:
    """GET /clusters 聚类列表端点测试。"""

    @pytest.mark.asyncio
    async def test_list_clusters_pagination(self, monkeypatch):
        """聚类结果列表分页，按 member_count 倒序。"""
        rows = [
            _cluster_row(id="mc_1", member_count=5),
            _cluster_row(id="mc_2", member_count=2),
        ]
        conn = _RecordingConnection(results=[
            _Result(rows=rows),
            _Result(row={"cnt": 2}),
        ])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/memory-vectors/clusters?limit=10&offset=0"
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert body["total"] == 2
        # 按 member_count 倒序
        assert body["items"][0]["member_count"] >= body["items"][1]["member_count"]

    @pytest.mark.asyncio
    async def test_list_clusters_empty(self, monkeypatch):
        """空聚类列表。"""
        conn = _RecordingConnection(results=[
            _Result(rows=[]),
            _Result(row={"cnt": 0}),
        ])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memory-vectors/clusters")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0


# ============================================================================
# 2. 重要性调整
# ============================================================================


class TestImportanceAdjustEndpoint:
    """POST /{vector_id}/importance 端点测试。"""

    @pytest.mark.asyncio
    async def test_adjust_importance_success(self, monkeypatch):
        """成功调整重要性。"""
        row = _row(id="mv_1", importance=3)
        conn = _RecordingConnection(results=[_Result(row=row), _Result(), _Result()])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/mv_1/importance",
                json={"importance": 5, "reason": "critical memory"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["old_importance"] == 3
        assert body["new_importance"] == 5
        assert body["reason"] == "critical memory"

    @pytest.mark.asyncio
    async def test_adjust_importance_boundary_1(self, monkeypatch):
        """importance=1 边界值通过。"""
        row = _row(id="mv_1", importance=3)
        conn = _RecordingConnection(results=[_Result(row=row), _Result(), _Result()])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/mv_1/importance",
                json={"importance": 1, "reason": "trivial"},
            )
        assert resp.status_code == 200
        assert resp.json()["new_importance"] == 1

    @pytest.mark.asyncio
    async def test_adjust_importance_boundary_5(self, monkeypatch):
        """importance=5 边界值通过。"""
        row = _row(id="mv_1", importance=3)
        conn = _RecordingConnection(results=[_Result(row=row), _Result(), _Result()])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/mv_1/importance",
                json={"importance": 5, "reason": "critical"},
            )
        assert resp.status_code == 200
        assert resp.json()["new_importance"] == 5

    @pytest.mark.asyncio
    async def test_adjust_importance_reject_0(self, monkeypatch):
        """importance=0 被 Pydantic 拒绝（422）。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/mv_1/importance",
                json={"importance": 0, "reason": "invalid"},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_adjust_importance_reject_6(self, monkeypatch):
        """importance=6 被 Pydantic 拒绝（422）。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/mv_1/importance",
                json={"importance": 6, "reason": "invalid"},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_adjust_importance_writes_governance_log(self, monkeypatch):
        """调整重要性时写 governance_log。"""
        row = _row(id="mv_1", importance=3)
        conn = _RecordingConnection(results=[_Result(row=row), _Result(), _Result()])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/mv_1/importance",
                json={"importance": 4, "reason": "test"},
            )
        assert resp.status_code == 200
        # 验证 governance_log INSERT 被执行，action='importance_adjust' 在 params 中
        gov_calls = [
            (q, p) for q, p in conn.calls
            if "memory_vector_governance_log" in q
        ]
        assert len(gov_calls) >= 1
        assert any("importance_adjust" in str(p) for _, p in gov_calls)

    @pytest.mark.asyncio
    async def test_adjust_importance_not_found_404(self, monkeypatch):
        """不存在的 vector_id 返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/mv_missing/importance",
                json={"importance": 4, "reason": "test"},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_adjust_importance_cross_workspace_403(self, monkeypatch):
        """跨 workspace 的 vector 返回 403。"""
        row = _row(id="mv_1", workspace_id="other_ws")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/mv_1/importance",
                json={"importance": 4, "reason": "test"},
            )
        assert resp.status_code == 403


# ============================================================================
# 3. 衰减报告
# ============================================================================


class TestDecayReportEndpoint:
    """GET /decay-report 衰减报告端点测试。"""

    @pytest.mark.asyncio
    async def test_decay_report_empty(self, monkeypatch):
        """空 workspace 衰减报告。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memory-vectors/decay-report")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["items"] == []

    @pytest.mark.asyncio
    async def test_decay_report_single_item_fields(self, monkeypatch):
        """单条记忆报告字段完整。"""
        now = datetime.now(UTC)
        row = _row(
            id="mv_1",
            content="hello world",
            importance=4,
            reference_count=3,
            last_referenced_at=now,
            created_at=now,
        )
        conn = _RecordingConnection(results=[_Result(rows=[row])])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memory-vectors/decay-report")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["vector_id"] == "mv_1"
        assert item["content"] == "hello world"
        assert item["importance"] == 4
        assert item["reference_count"] == 3
        assert "last_referenced_at" in item
        assert "created_at" in item
        assert "decay_score" in item
        assert "predicted_retention_days" in item
        assert "decay_curve" in item
        assert isinstance(item["decay_curve"], list)

    @pytest.mark.asyncio
    async def test_decay_report_excludes_merged(self, monkeypatch):
        """衰减报告 SQL 排除已合并记忆。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memory-vectors/decay-report")
        assert resp.status_code == 200
        # 验证 SQL 包含 status = 'active' 和 merged_into IS NULL
        select_calls = [q for q, _ in conn.calls if "SELECT" in q.upper()]
        assert any("status = 'active'" in q for q in select_calls)
        assert any("merged_into IS NULL" in q for q in select_calls)

    @pytest.mark.asyncio
    async def test_decay_report_unauthorized(self, monkeypatch):
        """缺少 memory:read 能力返回 403。"""
        app = _app(actor=_actor(capabilities=("memory:write",)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memory-vectors/decay-report")
        assert resp.status_code == 403


# ============================================================================
# 4. 检索重排序
# ============================================================================


class TestRerankEndpoint:
    """POST /rerank 检索重排序端点测试。"""

    @pytest.mark.asyncio
    async def test_rerank_empty_candidates_rejected(self, monkeypatch):
        """空候选列表被 Pydantic 拒绝（422）。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/rerank",
                json={"query": "hello", "candidate_vector_ids": []},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_rerank_single_candidate(self, monkeypatch):
        """单候选重排序返回 1 个结果。"""
        now = datetime.now(UTC)
        row = _row(id="mv_1", content="hello world", importance=5, last_referenced_at=now)
        conn = _RecordingConnection(results=[_Result(rows=[row])])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/rerank",
                json={"query": "hello world", "candidate_vector_ids": ["mv_1"]},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["ranked"]) == 1
        assert body["ranked"][0]["vector_id"] == "mv_1"
        assert body["ranked"][0]["score"] > 0

    @pytest.mark.asyncio
    async def test_rerank_multiple_weighted(self, monkeypatch):
        """多候选项按加权分数降序排列。"""
        now = datetime.now(UTC)
        # 候选 1：高 importance + 内容匹配 → 高分
        row_high = _row(
            id="mv_high", content="hello world", importance=5,
            last_referenced_at=now, embedding=vector_embedding("hello world"),
        )
        # 候选 2：低 importance → 低分
        row_low = _row(
            id="mv_low", content="hello world", importance=1,
            last_referenced_at=now, embedding=vector_embedding("hello world"),
        )
        conn = _RecordingConnection(results=[_Result(rows=[row_high, row_low])])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/rerank",
                json={"query": "hello world", "candidate_vector_ids": ["mv_high", "mv_low"]},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["ranked"]) == 2
        # 高 importance 排在前面
        assert body["ranked"][0]["vector_id"] == "mv_high"
        assert body["ranked"][0]["score"] > body["ranked"][1]["score"]

    @pytest.mark.asyncio
    async def test_rerank_score_calculation(self, monkeypatch):
        """验证分数计算公式：0.5*cos + 0.3*imp + 0.2*recency。"""
        now = datetime.now(UTC)
        # content 与 query 完全一致 → cosine_sim ≈ 1.0
        # importance=5 → imp_norm=1.0
        # last_referenced_at=now → recency ≈ 1.0
        # score ≈ 0.5*1.0 + 0.3*1.0 + 0.2*1.0 = 1.0
        row = _row(
            id="mv_1", content="hello world", importance=5,
            last_referenced_at=now, embedding=vector_embedding("hello world"),
        )
        conn = _RecordingConnection(results=[_Result(rows=[row])])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/rerank",
                json={"query": "hello world", "candidate_vector_ids": ["mv_1"]},
            )
        assert resp.status_code == 200
        score = resp.json()["ranked"][0]["score"]
        # 允许微小时间差
        assert score == pytest.approx(1.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_rerank_empty_query_rejected(self, monkeypatch):
        """空 query 被 Pydantic min_length=1 拒绝（422）。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/rerank",
                json={"query": "", "candidate_vector_ids": ["mv_1"]},
            )
        assert resp.status_code == 422


# ============================================================================
# 5. 统计仪表盘
# ============================================================================


class TestStatsEndpoint:
    """GET /stats 统计仪表盘端点测试。"""

    @pytest.mark.asyncio
    async def test_stats_empty(self, monkeypatch):
        """空 workspace 统计。"""
        conn = _RecordingConnection(results=[
            _Result(rows=[]),
            _Result(rows=[]),
            _Result(rows=[]),
        ])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memory-vectors/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_memories"] == 0
        assert body["by_importance"] == {}
        assert body["by_status"] == {}
        assert body["avg_reference_count"] == 0.0
        assert body["recent_7d_trend"] == []
        assert body["governance_action_counts"] == {}

    @pytest.mark.asyncio
    async def test_stats_by_importance(self, monkeypatch):
        """按 importance 分布统计正确。"""
        mem_rows = [
            {"importance": 3, "status": "active", "reference_count": 1},
            {"importance": 3, "status": "active", "reference_count": 2},
            {"importance": 5, "status": "active", "reference_count": 5},
        ]
        conn = _RecordingConnection(results=[
            _Result(rows=mem_rows),
            _Result(rows=[]),
            _Result(rows=[]),
        ])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memory-vectors/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["by_importance"] == {"3": 2, "5": 1}
        assert body["total_memories"] == 3

    @pytest.mark.asyncio
    async def test_stats_by_status(self, monkeypatch):
        """按 status 分布统计正确。"""
        mem_rows = [
            {"importance": 3, "status": "active", "reference_count": 1},
            {"importance": 3, "status": "merged", "reference_count": 0},
        ]
        conn = _RecordingConnection(results=[
            _Result(rows=mem_rows),
            _Result(rows=[]),
            _Result(rows=[]),
        ])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memory-vectors/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["by_status"] == {"active": 1, "merged": 1}

    @pytest.mark.asyncio
    async def test_stats_7d_trend(self, monkeypatch):
        """7 天写入趋势正确返回。"""
        today = datetime.now(UTC).date().isoformat()
        trend_rows = [
            {"date": today, "cnt": 3},
        ]
        conn = _RecordingConnection(results=[
            _Result(rows=[]),
            _Result(rows=trend_rows),
            _Result(rows=[]),
        ])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memory-vectors/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["recent_7d_trend"]) == 1
        assert body["recent_7d_trend"][0]["date"] == today
        assert body["recent_7d_trend"][0]["count"] == 3

    @pytest.mark.asyncio
    async def test_stats_governance_counts(self, monkeypatch):
        """治理操作计数正确返回。"""
        gov_rows = [
            {"action": "deduplicate", "cnt": 5},
            {"action": "importance_adjust", "cnt": 3},
            {"action": "explicit_merge", "cnt": 2},
        ]
        conn = _RecordingConnection(results=[
            _Result(rows=[]),
            _Result(rows=[]),
            _Result(rows=gov_rows),
        ])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memory-vectors/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["governance_action_counts"]["deduplicate"] == 5
        assert body["governance_action_counts"]["importance_adjust"] == 3
        assert body["governance_action_counts"]["explicit_merge"] == 2

    @pytest.mark.asyncio
    async def test_stats_avg_reference_count(self, monkeypatch):
        """平均引用次数计算正确。"""
        mem_rows = [
            {"importance": 3, "status": "active", "reference_count": 4},
            {"importance": 3, "status": "active", "reference_count": 6},
        ]
        conn = _RecordingConnection(results=[
            _Result(rows=mem_rows),
            _Result(rows=[]),
            _Result(rows=[]),
        ])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memory-vectors/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["avg_reference_count"] == 5.0


# ============================================================================
# 6. 显式合并
# ============================================================================


class TestMergeEndpoint:
    """POST /merge 显式合并端点测试。"""

    @pytest.mark.asyncio
    async def test_merge_success(self, monkeypatch):
        """成功合并 source 到 target。"""
        target = _row(id="mv_target", reference_count=2)
        source = _row(id="mv_source", reference_count=3)
        conn = _RecordingConnection(results=[
            _Result(row=target),
            _Result(row=source),
        ])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/merge",
                json={
                    "source_vector_ids": ["mv_source"],
                    "target_vector_id": "mv_target",
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["target_vector_id"] == "mv_target"
        assert body["merged_count"] == 1
        assert "mv_source" in body["merged_source_ids"]

    @pytest.mark.asyncio
    async def test_merge_source_ref_count_accumulated(self, monkeypatch):
        """source 的 reference_count 累加到 target。"""
        target = _row(id="mv_target", reference_count=2)
        source = _row(id="mv_source", reference_count=3)
        conn = _RecordingConnection(results=[
            _Result(row=target),
            _Result(row=source),
        ])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/merge",
                json={
                    "source_vector_ids": ["mv_source"],
                    "target_vector_id": "mv_target",
                },
            )
        assert resp.status_code == 200
        # 验证 UPDATE target reference_count + 3 被调用
        update_calls = [
            (q, p) for q, p in conn.calls
            if "UPDATE memory_vector" in q and "reference_count = reference_count +" in q
        ]
        assert len(update_calls) == 1
        assert update_calls[0][1][0] == 3  # total_ref = 3

    @pytest.mark.asyncio
    async def test_merge_source_merged_into_correct(self, monkeypatch):
        """source 的 merged_into 正确设置为 target_id。"""
        target = _row(id="mv_target")
        source = _row(id="mv_source")
        conn = _RecordingConnection(results=[
            _Result(row=target),
            _Result(row=source),
        ])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/merge",
                json={
                    "source_vector_ids": ["mv_source"],
                    "target_vector_id": "mv_target",
                },
            )
        assert resp.status_code == 200
        # 验证 UPDATE source SET merged_into = target_id
        merge_updates = [
            (q, p) for q, p in conn.calls
            if "UPDATE memory_vector" in q and "merged_into" in q
        ]
        assert len(merge_updates) == 1
        assert merge_updates[0][1][0] == "mv_target"  # merged_into = target_id

    @pytest.mark.asyncio
    async def test_merge_source_status_merged(self, monkeypatch):
        """source 的 status 被标记为 'merged'。"""
        target = _row(id="mv_target")
        source = _row(id="mv_source")
        conn = _RecordingConnection(results=[
            _Result(row=target),
            _Result(row=source),
        ])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/merge",
                json={
                    "source_vector_ids": ["mv_source"],
                    "target_vector_id": "mv_target",
                },
            )
        assert resp.status_code == 200
        # 验证 UPDATE source SET status = 'merged'
        assert any(
            "UPDATE memory_vector" in q and "status = 'merged'" in q
            for q, _ in conn.calls
        )

    @pytest.mark.asyncio
    async def test_merge_writes_governance_log(self, monkeypatch):
        """合并时写 governance_log（action=explicit_merge）。"""
        target = _row(id="mv_target")
        source = _row(id="mv_source")
        conn = _RecordingConnection(results=[
            _Result(row=target),
            _Result(row=source),
        ])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/merge",
                json={
                    "source_vector_ids": ["mv_source"],
                    "target_vector_id": "mv_target",
                },
            )
        assert resp.status_code == 200
        # 验证 governance_log INSERT with action='explicit_merge'（在 params 中）
        gov_calls = [
            (q, p) for q, p in conn.calls
            if "memory_vector_governance_log" in q
        ]
        assert len(gov_calls) >= 1
        assert any("explicit_merge" in str(p) for _, p in gov_calls)

    @pytest.mark.asyncio
    async def test_merge_target_not_found_404(self, monkeypatch):
        """target 不存在返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/merge",
                json={
                    "source_vector_ids": ["mv_source"],
                    "target_vector_id": "mv_missing",
                },
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_merge_source_not_found_404(self, monkeypatch):
        """source 不存在返回 404。"""
        target = _row(id="mv_target")
        conn = _RecordingConnection(results=[
            _Result(row=target),
            _Result(row=None),
        ])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/merge",
                json={
                    "source_vector_ids": ["mv_missing"],
                    "target_vector_id": "mv_target",
                },
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_merge_cross_workspace_403(self, monkeypatch):
        """跨 workspace 的 target 返回 403。"""
        target = _row(id="mv_target", workspace_id="other_ws")
        conn = _RecordingConnection(results=[_Result(row=target)])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/merge",
                json={
                    "source_vector_ids": ["mv_source"],
                    "target_vector_id": "mv_target",
                },
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_merge_already_merged_source_rejected(self, monkeypatch):
        """已 merged 的 source 被拒绝（400）。"""
        target = _row(id="mv_target")
        source = _row(id="mv_source", status="merged", merged_into="mv_other")
        conn = _RecordingConnection(results=[
            _Result(row=target),
            _Result(row=source),
        ])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/merge",
                json={
                    "source_vector_ids": ["mv_source"],
                    "target_vector_id": "mv_target",
                },
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_merge_already_merged_target_rejected(self, monkeypatch):
        """已 merged 的 target 被拒绝（400）。"""
        target = _row(id="mv_target", status="merged", merged_into="mv_other")
        conn = _RecordingConnection(results=[_Result(row=target)])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/merge",
                json={
                    "source_vector_ids": ["mv_source"],
                    "target_vector_id": "mv_target",
                },
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_merge_unauthorized(self, monkeypatch):
        """缺少 memory:write 能力返回 403。"""
        app = _app(actor=_actor(capabilities=("memory:read",)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/merge",
                json={
                    "source_vector_ids": ["mv_source"],
                    "target_vector_id": "mv_target",
                },
            )
        assert resp.status_code == 403


# ============================================================================
# 7. 辅助函数
# ============================================================================


class TestHelperFunctions:
    """辅助函数单元测试。"""

    def test_cluster_view(self):
        """_cluster_view 正确转换 cluster 行。"""
        row = _cluster_row(id="mc_1", member_count=5)
        view = _cluster_view(row)
        assert view["cluster_id"] == "mc_1"
        assert view["workspace_id"] == "wsp_test"
        assert view["cluster_label"] == "cluster_1"
        assert view["centroid_text"] == "hello world"
        assert view["member_count"] == 5
        assert "created_at" in view
        assert "updated_at" in view

    def test_decay_report_view(self):
        """_decay_report_view 正确转换记忆行为衰减报告项。"""
        now = datetime.now(UTC)
        row = _row(
            id="mv_1",
            content="hello",
            importance=5,
            reference_count=3,
            last_referenced_at=now,
            created_at=now,
        )
        view = _decay_report_view(row, now)
        assert view["vector_id"] == "mv_1"
        assert view["content"] == "hello"
        assert view["importance"] == 5
        assert view["reference_count"] == 3
        assert 0.0 <= view["decay_score"] <= 1.0
        assert view["predicted_retention_days"] >= 0
        assert len(view["decay_curve"]) == 30

    def test_stats_view(self):
        """_stats_view 正确聚合统计。"""
        rows = [
            {"importance": 3, "status": "active", "reference_count": 1},
            {"importance": 5, "status": "active", "reference_count": 4},
        ]
        gov_rows = [{"action": "deduplicate", "cnt": 2}]
        trend_rows = [{"date": "2026-01-01", "cnt": 5}]
        view = _stats_view("wsp_test", rows, gov_rows, trend_rows)
        assert view["workspace_id"] == "wsp_test"
        assert view["total_memories"] == 2
        assert view["by_importance"] == {"3": 1, "5": 1}
        assert view["by_status"] == {"active": 2}
        assert view["avg_reference_count"] == 2.5
        assert view["recent_7d_trend"] == [{"date": "2026-01-01", "count": 5}]
        assert view["governance_action_counts"] == {"deduplicate": 2}

    def test_merge_view(self):
        """_merge_view 正确构造合并结果。"""
        view = _merge_view("mv_target", ["mv_a", "mv_b"])
        assert view["target_vector_id"] == "mv_target"
        assert view["merged_count"] == 2
        assert view["merged_source_ids"] == ["mv_a", "mv_b"]

    def test_compute_decay_score_range(self):
        """_compute_decay_score 结果始终在 0-1 之间。"""
        now = datetime.now(UTC)
        # 高 importance + 最近引用 + 高引用计数
        score_high = _compute_decay_score(5, now, 10, now)
        assert 0.0 <= score_high <= 1.0
        # 低 importance + 远期引用 + 0 引用计数
        old = now - timedelta(days=100)
        score_low = _compute_decay_score(1, old, 0, now)
        assert 0.0 <= score_low <= 1.0
        assert score_high > score_low

    def test_compute_decay_curve_sample_count(self):
        """_compute_decay_curve 返回 30 个采样点。"""
        now = datetime.now(UTC)
        curve = _compute_decay_curve(3, now, 2, days=30, now=now)
        assert len(curve) == 30
        for i, point in enumerate(curve):
            assert point["day"] == i
            assert "date" in point
            assert 0.0 <= point["decay_score"] <= 1.0

    def test_predicted_retention_days(self):
        """_predicted_retention_days 正确计算。"""
        # importance=5, decay_score=1.0 → 365 天
        assert _predicted_retention_days(5, 1.0) == 365
        # importance=3, decay_score=0.5 → 15 天
        assert _predicted_retention_days(3, 0.5) == 15
        # importance=1, decay_score=0.0 → 0 天
        assert _predicted_retention_days(1, 0.0) == 0

    def test_importance_factor(self):
        """_importance_factor 1-5 → 0.2-1.0。"""
        assert _importance_factor(1) == pytest.approx(0.2)
        assert _importance_factor(3) == pytest.approx(0.6)
        assert _importance_factor(5) == pytest.approx(1.0)
        # 越界钳制
        assert _importance_factor(0) == pytest.approx(0.2)
        assert _importance_factor(99) == pytest.approx(1.0)

    def test_recency_factor(self):
        """_recency_factor 0-1，越近越接近 1。"""
        now = datetime.now(UTC)
        # 刚刚引用 → 1.0
        assert _recency_factor(now, now) == pytest.approx(1.0, abs=0.001)
        # 30 天前 → 0.0
        old = now - timedelta(days=30)
        assert _recency_factor(old, now) == pytest.approx(0.0, abs=0.001)
        # 15 天前 → 0.5
        mid = now - timedelta(days=15)
        assert _recency_factor(mid, now) == pytest.approx(0.5, abs=0.01)
        # 无时间 → 0.0
        assert _recency_factor(None, now) == 0.0

    def test_reference_factor(self):
        """_reference_factor = 1.0 + min(rc, 10) * 0.05。"""
        assert _reference_factor(0) == 1.0
        assert _reference_factor(1) == 1.05
        assert _reference_factor(10) == 1.5
        # 超过 10 钳制
        assert _reference_factor(100) == 1.5
        # 负数钳制
        assert _reference_factor(-5) == 1.0


# ============================================================================
# 8. _generate_cluster_summary
# ============================================================================


class TestGenerateClusterSummary:
    """_generate_cluster_summary 辅助函数测试。"""

    @pytest.mark.asyncio
    async def test_generate_cluster_summary_empty_members(self):
        """空成员列表返回空字符串。"""
        result = await _generate_cluster_summary("wsp_test", [])
        assert result == ""

    @pytest.mark.asyncio
    async def test_generate_cluster_summary_llm_fallback(self, monkeypatch):
        """_call_llm 返回 "[]" 时 fallback 到最短成员 content[:100]。"""
        # 默认 _call_llm 返回 "[]"
        members = [
            {"content": "short"},
            {"content": "a much longer content string that exceeds limits"},
        ]
        result = await _generate_cluster_summary("wsp_test", members)
        # fallback 取最短成员 content 前 100 字符
        assert result == "short"

    @pytest.mark.asyncio
    async def test_generate_cluster_summary_llm_success(self, monkeypatch):
        """LLM 返回有效摘要时使用 LLM 结果。"""
        async def fake_llm(workspace_id, messages, **kwargs):
            return "Cluster about Python"

        monkeypatch.setattr(mv, "_call_llm", fake_llm)
        members = [{"content": "python coding"}, {"content": "python tips"}]
        result = await _generate_cluster_summary("wsp_test", members)
        assert result == "Cluster about Python"

    @pytest.mark.asyncio
    async def test_generate_cluster_summary_llm_exception(self, monkeypatch):
        """LLM 异常时 fallback 到最短成员 content[:100]。"""
        async def failing_llm(workspace_id, messages, **kwargs):
            raise RuntimeError("LLM unavailable")

        monkeypatch.setattr(mv, "_call_llm", failing_llm)
        members = [{"content": "abc"}, {"content": "longer content"}]
        result = await _generate_cluster_summary("wsp_test", members)
        assert result == "abc"

    @pytest.mark.asyncio
    async def test_generate_cluster_summary_truncates_to_100(self, monkeypatch):
        """fallback 截取 content 前 100 字符。"""
        long_content = "x" * 200
        members = [{"content": long_content}]
        result = await _generate_cluster_summary("wsp_test", members)
        assert len(result) == 100
        assert result == "x" * 100


# ============================================================================
# 9. SCHEMA_STATEMENTS 与 ensure_memory_semantic_schema
# ============================================================================


class TestSchemaStatements:
    """SCHEMA_STATEMENTS 内容与执行测试。"""

    def test_schema_contains_cluster_tables(self):
        """SCHEMA_STATEMENTS 包含 memory_cluster 和 memory_cluster_member 表。"""
        joined = "\n".join(SCHEMA_STATEMENTS)
        assert "CREATE TABLE IF NOT EXISTS memory_cluster" in joined
        assert "CREATE TABLE IF NOT EXISTS memory_cluster_member" in joined
        assert "UNIQUE(cluster_id, vector_id)" in joined

    def test_schema_contains_memory_vector_columns(self):
        """SCHEMA_STATEMENTS 包含 memory_vector 新列与约束扩展。"""
        joined = "\n".join(SCHEMA_STATEMENTS)
        assert "ALTER TABLE memory_vector ADD COLUMN IF NOT EXISTS cluster_id" in joined
        assert "ALTER TABLE memory_vector ADD COLUMN IF NOT EXISTS merged_into" in joined
        assert "ALTER TABLE memory_vector ADD COLUMN IF NOT EXISTS decay_score" in joined
        assert "ALTER TABLE memory_vector ADD COLUMN IF NOT EXISTS status" in joined
        # governance_log action 约束扩展
        assert "importance_adjust" in joined
        assert "explicit_merge" in joined

    @pytest.mark.asyncio
    async def test_ensure_memory_semantic_schema_executes_all(self):
        """ensure_memory_semantic_schema 对每条 SCHEMA_STATEMENTS 调用 conn.execute。"""
        executed: list[str] = []

        class _Conn:
            async def execute(self, query, params=()):
                executed.append(query)

        conn = _Conn()
        await ensure_memory_semantic_schema(conn)
        assert len(executed) == len(SCHEMA_STATEMENTS)
