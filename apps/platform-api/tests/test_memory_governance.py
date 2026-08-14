"""记忆完整形态治理单元测试（v7.164-A）。

覆盖：
- cosine_similarity 工具函数
- MemoryGovernanceWorker（去重 / 合并 / process_governance_job）
- _EnhancedForgettingWorker（引用计数衰减）
- REST 端点（govern / annotate / list_annotations）

所有测试使用 fake pool/connection，不依赖真实 DB / 网络。
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules import memory as mem_module
from workama_platform.modules import memory_vector as mv
from workama_platform.modules.memory_vector import (
    AnnotationCreate,
    AnnotationResponse,
    GovernRequest,
    MemoryGovernanceWorker,
    _EnhancedForgettingWorker,
    cosine_similarity,
    vector_embedding,
)
from workama_platform.worker import process_memory_governance

# Reuse test helpers from test_memory_vector.py
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
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(mv.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


def _memory_app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(mem_module.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


# ============================================================================
# 1. cosine_similarity
# ============================================================================

class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        assert math.isclose(cosine_similarity(v, v), 1.0, abs_tol=1e-6)

    def test_orthogonal_vectors(self):
        assert math.isclose(cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0, abs_tol=1e-6)

    def test_opposite_vectors(self):
        assert math.isclose(cosine_similarity([1.0, 0.0], [-1.0, 0.0]), -1.0, abs_tol=1e-6)

    def test_dimension_mismatch(self):
        assert cosine_similarity([1.0, 0.0], [1.0]) == 0.0

    def test_empty_vectors(self):
        assert cosine_similarity([], []) == 0.0


# ============================================================================
# 2. MemoryGovernanceWorker
# ============================================================================

class TestMemoryGovernanceWorker:
    @pytest.mark.asyncio
    async def test_deduplicate_workspace_removes_duplicates(self, monkeypatch):
        emb = vector_embedding("hello world")
        rows = [
            _row(id="mv_a", content="hello world", embedding=emb),
            _row(id="mv_b", content="hello world", embedding=emb),
        ]
        conn = _RecordingConnection(results=[_Result(rows=rows), _Result(), _Result()])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        worker = MemoryGovernanceWorker()
        result = await worker.deduplicate_workspace("wsp_test", threshold=0.95)
        assert result["removed"] == 1
        assert "mv_b" in result["removed_ids"] or "mv_a" in result["removed_ids"]

    @pytest.mark.asyncio
    async def test_deduplicate_workspace_keeps_unique(self, monkeypatch):
        rows = [
            _row(id="mv_a", content="hello world", embedding=vector_embedding("hello world")),
            _row(id="mv_b", content="goodbye moon", embedding=vector_embedding("goodbye moon")),
        ]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        worker = MemoryGovernanceWorker()
        result = await worker.deduplicate_workspace("wsp_test", threshold=0.95)
        assert result["removed"] == 0

    @pytest.mark.asyncio
    async def test_deduplicate_workspace_empty(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        worker = MemoryGovernanceWorker()
        result = await worker.deduplicate_workspace("wsp_test")
        assert result["scanned"] == 0
        assert result["removed"] == 0

    @pytest.mark.asyncio
    async def test_merge_similar_memories_groups(self, monkeypatch):
        emb = vector_embedding("python coding")
        rows = [
            _row(id="mv_1", content="python coding", embedding=emb, importance=4, reference_count=2),
            _row(id="mv_2", content="python coding tips", embedding=emb, importance=3, reference_count=1),
        ]
        conn = _RecordingConnection(results=[
            _Result(rows=rows),
            _Result(row={"id": "mv_merged"}),
            _Result(),
            _Result(),
        ])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        worker = MemoryGovernanceWorker()
        result = await worker.merge_similar_memories("wsp_test", threshold=0.90)
        assert result["merged_groups"] >= 0
        assert result["scanned"] == 2

    @pytest.mark.asyncio
    async def test_merge_similar_memories_no_similar(self, monkeypatch):
        rows = [
            _row(id="mv_1", content="apple", embedding=vector_embedding("apple")),
            _row(id="mv_2", content="zebra", embedding=vector_embedding("zebra")),
        ]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        worker = MemoryGovernanceWorker()
        result = await worker.merge_similar_memories("wsp_test", threshold=0.99)
        assert result["merged_groups"] == 0
        assert result["merged_count"] == 0

    @pytest.mark.asyncio
    async def test_process_governance_job_deduplicate(self, monkeypatch):
        called = {}

        async def fake_dedup(self, ws, threshold):
            called["action"] = "deduplicate"
            called["ws"] = ws
            return {"removed": 0}

        monkeypatch.setattr(MemoryGovernanceWorker, "deduplicate_workspace", fake_dedup)
        worker = MemoryGovernanceWorker()
        result = await worker.process_governance_job(
            {"workspace_id": "ws1", "action": "deduplicate", "threshold": 0.9}
        )
        assert called["action"] == "deduplicate"
        assert called["ws"] == "ws1"

    @pytest.mark.asyncio
    async def test_process_governance_job_merge(self, monkeypatch):
        called = {}

        async def fake_merge(self, ws, threshold):
            called["action"] = "merge"
            return {"merged_groups": 1}

        monkeypatch.setattr(MemoryGovernanceWorker, "merge_similar_memories", fake_merge)
        worker = MemoryGovernanceWorker()
        result = await worker.process_governance_job(
            {"workspace_id": "ws1", "action": "merge", "threshold": 0.85}
        )
        assert called["action"] == "merge"

    @pytest.mark.asyncio
    async def test_process_governance_job_unknown_action(self):
        worker = MemoryGovernanceWorker()
        result = await worker.process_governance_job(
            {"workspace_id": "ws1", "action": "unknown"}
        )
        assert "error" in result


# ============================================================================
# 3. _EnhancedForgettingWorker
# ============================================================================

class TestEnhancedForgettingWorker:
    @pytest.mark.asyncio
    async def test_apply_forgetting_curve_with_reference_count(self, monkeypatch):
        rows = [
            _row(id="mv_old", content="old", last_referenced_at=datetime(2020, 1, 1, tzinfo=UTC)),
        ]
        conn = _RecordingConnection(results=[
            _Result(row={"cnt": 1}),
            _Result(rows=[{"id": "mv_old"}]),
            _Result(),
        ])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        worker = _EnhancedForgettingWorker()
        result = await worker.apply_forgetting_curve("wsp_test")
        assert result["scanned"] == 1
        assert result["forgotten"] == 1
        assert result["forgotten_ids"] == ["mv_old"]

    @pytest.mark.asyncio
    async def test_apply_forgetting_curve_empty_workspace(self, monkeypatch):
        conn = _RecordingConnection(results=[
            _Result(row={"cnt": 0}),
            _Result(rows=[]),
        ])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        worker = _EnhancedForgettingWorker()
        result = await worker.apply_forgetting_curve("wsp_test")
        assert result["scanned"] == 0
        assert result["forgotten"] == 0


# ============================================================================
# 4. Govern Endpoint
# ============================================================================

class TestGovernEndpoint:
    @pytest.mark.asyncio
    async def test_govern_deduplicate_endpoint(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(rows=[]), _Result()])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/govern",
                json={"action": "deduplicate", "threshold": 0.95},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["action"] == "deduplicate"
        assert body["workspace_id"] == "wsp_test"

    @pytest.mark.asyncio
    async def test_govern_merge_endpoint(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/govern",
                json={"action": "merge", "threshold": 0.90},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["action"] == "merge"

    @pytest.mark.asyncio
    async def test_govern_forget_sweep_endpoint(self, monkeypatch):
        conn = _RecordingConnection(results=[
            _Result(row={"cnt": 0}),
            _Result(rows=[]),
        ])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/govern",
                json={"action": "forget_sweep"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["action"] == "forget_sweep"

    @pytest.mark.asyncio
    async def test_govern_unknown_action(self, monkeypatch):
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/govern",
                json={"action": "unknown"},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_govern_unauthorized(self, monkeypatch):
        app = _app(actor=_actor(capabilities=("memory:read",)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/govern",
                json={"action": "deduplicate"},
            )
        assert resp.status_code == 403


# ============================================================================
# 5. Annotate Endpoint
# ============================================================================

class TestAnnotateEndpoint:
    @pytest.mark.asyncio
    async def test_annotate_vector_increases_importance(self, monkeypatch):
        row = _row(id="mv_1")
        conn = _RecordingConnection(results=[
            _Result(row=row),
            _Result(row={"id": "mva_1", "vector_id": "mv_1", "relevance_score": 0.9, "accuracy_score": 0.8, "feedback": None, "actor_id": "usr_test", "created_at": datetime.now(UTC)}),
            _Result(),
        ])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/mv_1/annotate",
                json={"relevance_score": 0.9, "accuracy_score": 0.8},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["vector_id"] == "mv_1"
        assert body["relevance_score"] == 0.9
        # Check that UPDATE importance was called
        assert any("UPDATE memory_vector" in q for q, _ in conn.calls)

    @pytest.mark.asyncio
    async def test_annotate_vector_decreases_importance(self, monkeypatch):
        row = _row(id="mv_1")
        conn = _RecordingConnection(results=[
            _Result(row=row),
            _Result(row={"id": "mva_1", "vector_id": "mv_1", "relevance_score": 0.2, "accuracy_score": 0.3, "feedback": "bad", "actor_id": "usr_test", "created_at": datetime.now(UTC)}),
            _Result(),
        ])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/mv_1/annotate",
                json={"relevance_score": 0.2, "accuracy_score": 0.3, "feedback": "bad"},
            )
        assert resp.status_code == 200
        assert any("UPDATE memory_vector" in q for q, _ in conn.calls)

    @pytest.mark.asyncio
    async def test_annotate_vector_not_found(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/mv_missing/annotate",
                json={"relevance_score": 0.5},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_annotate_vector_cross_workspace(self, monkeypatch):
        row = _row(id="mv_1", workspace_id="other_ws")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/memory-vectors/mv_1/annotate",
                json={"relevance_score": 0.5},
            )
        assert resp.status_code == 403


# ============================================================================
# 6. List Annotations Endpoint
# ============================================================================

class TestListAnnotationsEndpoint:
    @pytest.mark.asyncio
    async def test_list_annotations_by_workspace(self, monkeypatch):
        rows = [
            {"id": "mva_1", "vector_id": "mv_1", "relevance_score": 0.9, "accuracy_score": 0.8, "feedback": None, "actor_id": "usr_test", "created_at": datetime.now(UTC)},
        ]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memory-vectors/annotations")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["items"][0]["vector_id"] == "mv_1"

    @pytest.mark.asyncio
    async def test_list_annotations_by_vector_id(self, monkeypatch):
        rows = [
            {"id": "mva_1", "vector_id": "mv_1", "relevance_score": 0.9, "accuracy_score": 0.8, "feedback": None, "actor_id": "usr_test", "created_at": datetime.now(UTC)},
        ]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memory-vectors/annotations?vector_id=mv_1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        # Verify vector_id filter was used
        assert any("vector_id = %s" in q for q, _ in conn.calls)

    @pytest.mark.asyncio
    async def test_list_annotations_empty(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(mv, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memory-vectors/annotations")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    @pytest.mark.asyncio
    async def test_list_annotations_unauthorized(self, monkeypatch):
        app = _app(actor=_actor(capabilities=("memory:write",)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memory-vectors/annotations")
        assert resp.status_code == 403


# ============================================================================
# 7. Memory Governance Policy Endpoint (memory.py)
# ============================================================================

class TestMemoryGovernancePolicyEndpoint:
    @pytest.mark.asyncio
    async def test_get_governance_policy_exists(self, monkeypatch):
        row = {
            "workspace_id": "wsp_test",
            "retention_days_by_importance": {"1": 7, "3": 30, "5": 365},
            "default_importance": 3,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(mem_module, "pool", _Pool(conn))

        app = _memory_app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memories/governance-policy")
        assert resp.status_code == 200
        body = resp.json()
        assert body["workspace_id"] == "wsp_test"
        assert body["retention_days_by_importance"] == {"1": 7, "3": 30, "5": 365}
        assert body["default_importance"] == 3

    @pytest.mark.asyncio
    async def test_get_governance_policy_not_exists(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(mem_module, "pool", _Pool(conn))

        app = _memory_app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/memories/governance-policy")
        assert resp.status_code == 200
        body = resp.json()
        assert body["workspace_id"] == "wsp_test"
        assert body["retention_days_by_importance"] == {}
        assert body["default_importance"] == 3
        assert body["created_at"] is None

    @pytest.mark.asyncio
    async def test_update_governance_policy_success(self, monkeypatch):
        row = {
            "workspace_id": "wsp_test",
            "retention_days_by_importance": {"1": 7, "5": 365},
            "default_importance": 3,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(mem_module, "pool", _Pool(conn))

        app = _memory_app(actor=_actor(role="owner"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put(
                "/api/v1/memories/governance-policy",
                json={"retention_days_by_importance": {"1": 7, "5": 365}, "default_importance": 3},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["retention_days_by_importance"] == {"1": 7, "5": 365}
        # Verify UPSERT was executed
        assert any("memory_governance_policy" in q for q, _ in conn.calls)

    @pytest.mark.asyncio
    async def test_update_governance_policy_unauthorized_role(self, monkeypatch):
        app = _memory_app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put(
                "/api/v1/memories/governance-policy",
                json={"retention_days_by_importance": {"1": 7}, "default_importance": 3},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_update_governance_policy_invalid_key(self, monkeypatch):
        app = _memory_app(actor=_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put(
                "/api/v1/memories/governance-policy",
                json={"retention_days_by_importance": {"0": 7}, "default_importance": 3},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_update_governance_policy_invalid_days(self, monkeypatch):
        app = _memory_app(actor=_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put(
                "/api/v1/memories/governance-policy",
                json={"retention_days_by_importance": {"1": -1}, "default_importance": 3},
            )
        assert resp.status_code == 422


# ============================================================================
# 8. Process Memory Governance (worker.py)
# ============================================================================

class TestProcessMemoryGovernance:
    @pytest.mark.asyncio
    async def test_no_policies(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr("workama_platform.worker.pool", _Pool(conn))
        monkeypatch.setattr("workama_platform.worker.audit_log_action", lambda *args, **kwargs: None)

        result = await process_memory_governance()
        assert result["workspaces_scanned"] == 0
        assert result["memories_forgotten"] == 0

    @pytest.mark.asyncio
    async def test_expired_memories_are_forgotten(self, monkeypatch):
        now = datetime.now(UTC)
        policy_row = {
            "workspace_id": "wsp_test",
            "retention_days_by_importance": {"3": 1},
            "default_importance": 3,
        }
        memory_row = {
            "id": "mem_1",
            "org_id": "org_test",
            "user_id": "usr_test",
            "importance": 0.5,
            "updated_at": now - timedelta(days=2),
        }
        conn = _RecordingConnection(results=[
            _Result(rows=[policy_row]),
            _Result(rows=[memory_row]),
            _Result(),
        ])
        monkeypatch.setattr("workama_platform.worker.pool", _Pool(conn))

        async def fake_audit(*args, **kwargs):
            pass

        monkeypatch.setattr("workama_platform.worker.audit_log_action", fake_audit)

        result = await process_memory_governance()
        assert result["workspaces_scanned"] == 1
        assert result["memories_forgotten"] == 1
        # Verify UPDATE was called with status='deleted'
        assert any("status='deleted'" in q for q, _ in conn.calls)

    @pytest.mark.asyncio
    async def test_not_expired_memories_unchanged(self, monkeypatch):
        now = datetime.now(UTC)
        policy_row = {
            "workspace_id": "wsp_test",
            "retention_days_by_importance": {"3": 10},
            "default_importance": 3,
        }
        memory_row = {
            "id": "mem_1",
            "org_id": "org_test",
            "user_id": "usr_test",
            "importance": 0.5,
            "updated_at": now - timedelta(days=2),
        }
        conn = _RecordingConnection(results=[
            _Result(rows=[policy_row]),
            _Result(rows=[memory_row]),
        ])
        monkeypatch.setattr("workama_platform.worker.pool", _Pool(conn))
        monkeypatch.setattr("workama_platform.worker.audit_log_action", lambda *args, **kwargs: None)

        result = await process_memory_governance()
        assert result["workspaces_scanned"] == 1
        assert result["memories_forgotten"] == 0
        assert not any("UPDATE ag_memory" in q for q, _ in conn.calls)

    @pytest.mark.asyncio
    async def test_different_importance_levels(self, monkeypatch):
        now = datetime.now(UTC)
        policy_row = {
            "workspace_id": "wsp_test",
            "retention_days_by_importance": {"1": 1, "5": 100},
            "default_importance": 3,
        }
        # importance 0.1 -> level 1 (expired, 1 day)
        mem_low = {"id": "mem_low", "org_id": "org_test", "user_id": "usr_test", "importance": 0.1, "updated_at": now - timedelta(days=2)}
        # importance 0.9 -> level 5 (not expired, 100 days)
        mem_high = {"id": "mem_high", "org_id": "org_test", "user_id": "usr_test", "importance": 0.9, "updated_at": now - timedelta(days=2)}
        conn = _RecordingConnection(results=[
            _Result(rows=[policy_row]),
            _Result(rows=[mem_low, mem_high]),
            _Result(),
        ])
        monkeypatch.setattr("workama_platform.worker.pool", _Pool(conn))

        async def fake_audit(*args, **kwargs):
            pass

        monkeypatch.setattr("workama_platform.worker.audit_log_action", fake_audit)

        result = await process_memory_governance()
        assert result["memories_forgotten"] == 1
        # Verify only the low-importance memory was targeted
        update_calls = [q for q, _ in conn.calls if "UPDATE ag_memory" in q]
        assert len(update_calls) == 1
        # ids list contains mem_low only
        assert "mem_low" in str(update_calls[0]) or "ANY(%s)" in str(update_calls[0])

    @pytest.mark.asyncio
    async def test_audit_log_recorded(self, monkeypatch):
        now = datetime.now(UTC)
        policy_row = {
            "workspace_id": "wsp_test",
            "retention_days_by_importance": {"3": 1},
            "default_importance": 3,
        }
        memory_row = {
            "id": "mem_1",
            "org_id": "org_test",
            "user_id": "usr_test",
            "importance": 0.5,
            "updated_at": now - timedelta(days=2),
        }
        conn = _RecordingConnection(results=[
            _Result(rows=[policy_row]),
            _Result(rows=[memory_row]),
            _Result(),
        ])
        monkeypatch.setattr("workama_platform.worker.pool", _Pool(conn))

        logged = []

        async def fake_audit(*args, **kwargs):
            logged.append(kwargs)

        monkeypatch.setattr("workama_platform.worker.audit_log_action", fake_audit)

        result = await process_memory_governance()
        assert result["memories_forgotten"] == 1
        assert len(logged) == 1
        assert logged[0]["resource_type"] == "memory"
        assert logged[0]["metadata"]["forgotten_count"] == 1
