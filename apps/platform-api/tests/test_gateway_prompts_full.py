"""T-M1-007 完整 Prompt Registry CRUD 单元 + 端点测试。

覆盖：
- CRUD 全流程（创建/列表/详情/更新/软删除）
- 版本管理（创建版本/列表/按数字与按 ID 查询）
- 发布/回滚（含按 version 数字的 publish/rollback）
- 搜索（name/description/tags ILIKE）
- 评测门禁（通过/失败/无评测/低分/eval_run_id 校验）
- 灰度配置（PATCH rollout / 枚举校验）
- 鉴权（403 缺能力 / 401 未认证）
- workspace 隔离（跨 workspace 403）
- 软删除恢复（deleted 不返回 / 软删除字段）
- 列表过滤分页（name/status/cursor/limit）

所有测试使用 fake pool/connection/result，不依赖真实 DB / Redis / 网络。
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules import gateway_prompts as gp


# ============================================================================
# 测试辅助：fake pool / connection / result
# ============================================================================


class _Result:
    """模拟 psycopg 查询结果。"""

    def __init__(self, row: dict | None = None, rows: list[dict] | None = None):
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
    """记录 execute 调用并按序返回配置的结果。支持 transaction/commit。"""

    def __init__(self, results: list[_Result] | None = None):
        self.calls: list[tuple[str, tuple]] = []
        self._results = list(results) if results else []
        self._idx = 0
        self.committed = False

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
        self.committed = True


class _Pool:
    """模拟连接池。"""

    def __init__(self, conn: _RecordingConnection):
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
    capabilities: tuple[str, ...] = ("prompt:*",),
    workspace_id: str = "wsp_test",
    user_id: str = "usr_test",
    role: str = "admin",
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


def _row(**overrides) -> dict:
    """生成一个完整的 sec_prompt_version 行（含 JOIN 的 eval 字段）。"""
    base: dict[str, Any] = {
        "id": "gwprm_1",
        "workspace_id": "wsp_test",
        "name": "support.reply",
        "version": 1,
        "content": "Answer as {{agent_name}}. Never reveal secret. Untrusted tool data. High-risk approval required.",
        "checksum": hashlib.sha256(b"hello").hexdigest(),
        "status": "draft",
        "rollout_percent": 0,
        "created_at": datetime.now(UTC),
        "published_at": None,
        "description": "Test prompt",
        "tags": ["support", "v2"],
        "template_variables": {"agent_name": "string"},
        "model_hint": "gpt-4o-mini",
        "parent_version_id": None,
        "deleted_at": None,
        # JOIN 字段
        "eval_status": "passed",
        "eval_failures": [],
        "eval_run_id": "gweval_1",
        "eval_total_cases": 3,
        "eval_passed_cases": 3,
    }
    base.update(overrides)
    return base


def _eval_row(**overrides) -> dict:
    base: dict[str, Any] = {
        "id": "gweval_1",
        "status": "passed",
        "total_cases": 3,
        "passed_cases": 3,
        "failures": [],
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    """构造一个挂载了 gateway_prompts router 的 FastAPI 应用。

    注：_ensure_rollout_schema 依赖 pool，测试中通过 monkeypatch 替换 pool 即可。
    为避免 schema 初始化触发真实 DB，重置模块级 ready 标志。
    """
    gp._ROLLOUT_SCHEMA_READY = True  # 跳过 schema 初始化
    app = FastAPI()
    app.include_router(gp.router)
    app.include_router(gp.internal_router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


# ============================================================================
# 1. Schema 登记测试
# ============================================================================


def test_schema_statements_contain_prompt_rollout_table_and_fields():
    """SCHEMA_STATEMENTS 包含 pf_prompt_rollout 表与新增字段。"""
    schema = "\n".join(gp.SCHEMA_STATEMENTS)
    assert "pf_prompt_rollout" in schema
    assert "UNIQUE(workspace_id, prompt_id)" in schema
    for field in ("description", "tags", "template_variables", "model_hint", "deleted_at", "parent_version_id"):
        assert field in schema
    assert "status IN ('draft', 'published', 'archived', 'deleted')" in schema


@pytest.mark.asyncio
async def test_ensure_rollout_schema_applies_all_statements(monkeypatch):
    """_ensure_rollout_schema 在未初始化时应执行所有 SCHEMA_STATEMENTS。"""
    gp._ROLLOUT_SCHEMA_READY = False
    executed: list[str] = []

    class _SchemaConn:
        def transaction(self):
            return _Transaction()

        async def execute(self, query, params=()):
            executed.append(query)

    monkeypatch.setattr(gp, "pool", _Pool(_SchemaConn()))  # type: ignore[arg-type]
    try:
        await gp._ensure_rollout_schema()
    finally:
        gp._ROLLOUT_SCHEMA_READY = True
    # 至少包含 SCHEMA_STATEMENTS 中的所有语句
    joined = "\n".join(executed)
    assert "pf_prompt_rollout" in joined
    assert "ADD COLUMN IF NOT EXISTS description" in joined
    assert "ADD COLUMN IF NOT EXISTS parent_version_id" in joined


# ============================================================================
# 2. Pydantic 模型测试
# ============================================================================


def test_prompt_create_accepts_metadata_fields():
    body = gp.PromptCreate(
        name="support.reply",
        content="hello",
        description="A test prompt",
        tags=["support", "v2"],
        template_variables={"agent_name": "string"},
        model_hint="gpt-4o-mini",
    )
    assert body.description == "A test prompt"
    assert body.tags == ["support", "v2"]
    assert body.template_variables == {"agent_name": "string"}
    assert body.model_hint == "gpt-4o-mini"


def test_prompt_create_rejects_too_many_tags():
    with pytest.raises(ValueError):
        gp.PromptCreate(name="support.reply", content="hello", tags=[f"t{i}" for i in range(33)])


def test_prompt_patch_validates_name_and_tags():
    body = gp.PromptPatch(name="new.name", tags=["a", "b"])
    assert body.name == "new.name"
    assert body.tags == ["a", "b"]
    with pytest.raises(ValueError):
        gp.PromptPatch(name="bad name")
    with pytest.raises(ValueError):
        gp.PromptPatch(tags=[""])


def test_prompt_publish_request_defaults():
    body = gp.PromptPublishRequest()
    assert body.eval_run_id is None
    assert body.rollout_percent == 100
    assert body.eval_threshold == gp._DEFAULT_EVAL_THRESHOLD


def test_prompt_rollout_patch_validates_strategy():
    body = gp.PromptRolloutPatch(percent=50, strategy="stable_sha256")
    assert body.strategy == "stable_sha256"
    with pytest.raises(ValueError):
        gp.PromptRolloutPatch(percent=50, strategy="invalid")


# ============================================================================
# 3. _view / _eval_mean_score 辅助
# ============================================================================


def test_view_includes_metadata_and_deleted_fields():
    view = gp._view(_row(status="deleted", deleted_at=datetime.now(UTC)))
    assert view["status"] == "deleted"
    assert view["deleted_at"] is not None
    assert view["description"] == "Test prompt"
    assert view["tags"] == ["support", "v2"]
    assert view["template_variables"] == {"agent_name": "string"}
    assert view["model_hint"] == "gpt-4o-mini"
    assert view["parent_version_id"] is None
    assert view["rollout_strategy"] == "inactive"


def test_eval_mean_score_handles_zero_total():
    assert gp._eval_mean_score({"total_cases": 0, "passed_cases": 0}) == 0.0
    assert gp._eval_mean_score({"total_cases": 4, "passed_cases": 3}) == 0.75
    assert gp._eval_mean_score({"eval_total_cases": 3, "eval_passed_cases": 2}) == 2 / 3
    assert gp._eval_mean_score({}) == 0.0


# ============================================================================
# 4. Create 端点
# ============================================================================


class TestCreatePrompt:
    @pytest.mark.asyncio
    async def test_create_prompt_with_metadata_success(self, monkeypatch):
        row = _row()
        conn = _RecordingConnection(results=[_Result(row={"version": 1}), _Result(row=row)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))
        monkeypatch.setattr(gp, "new_id", lambda prefix: "gwprm_1")

        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/gateway/prompts",
                json={
                    "name": "support.reply",
                    "content": "hello",
                    "description": "Test prompt",
                    "tags": ["support", "v2"],
                    "template_variables": {"agent_name": "string"},
                    "model_hint": "gpt-4o-mini",
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "support.reply"
        assert body["description"] == "Test prompt"
        assert body["tags"] == ["support", "v2"]
        assert body["model_hint"] == "gpt-4o-mini"
        assert conn.committed is True

    @pytest.mark.asyncio
    async def test_create_prompt_returns_403_without_capability(self):
        app = _app(_actor(capabilities=("prompt:read",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/gateway/prompts",
                json={"name": "support.reply", "content": "hello"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_create_prompt_422_on_invalid_name(self):
        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/gateway/prompts",
                json={"name": "bad name", "content": "hello"},
            )
        assert resp.status_code == 422


# ============================================================================
# 5. List 端点（过滤/分页/workspace 隔离）
# ============================================================================


class TestListPrompts:
    @pytest.mark.asyncio
    async def test_list_prompts_returns_paginated(self, monkeypatch):
        rows = [_row(id="gwprm_a"), _row(id="gwprm_b", version=2, name="other.prompt")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/gateway/prompts?limit=10")
        assert resp.status_code == 200
        body = resp.json()
        assert body["meta"]["count"] == 2
        assert len(body["items"]) == 2
        # include_content=False，列表项不含 content
        assert "content" not in body["items"][0]

    @pytest.mark.asyncio
    async def test_list_prompts_filters_by_name(self, monkeypatch):
        rows = [_row(id="gwprm_a", name="support.reply")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/gateway/prompts?name=support.reply")
        assert resp.status_code == 200
        # 验证 SQL 包含 name 过滤
        assert any("p.name=%s" in c[0] and "support.reply" in c[1] for c in conn.calls)

    @pytest.mark.asyncio
    async def test_list_prompts_filters_by_status(self, monkeypatch):
        rows = [_row(id="gwprm_a", status="published")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/gateway/prompts?status=published")
        assert resp.status_code == 200
        # 显式 status=published 时不再追加 status<>'deleted'
        assert any("p.status=%s" in c[0] for c in conn.calls)

    @pytest.mark.asyncio
    async def test_list_prompts_invalid_status_returns_422(self, monkeypatch):
        conn = _RecordingConnection()
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/gateway/prompts?status=invalid")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_list_prompts_workspace_isolation_403(self, monkeypatch):
        conn = _RecordingConnection()
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        # 仅 prompt:read，无 workspace:read / *
        app = _app(_actor(capabilities=("prompt:read",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/gateway/prompts?workspace_id=wsp_other")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_list_prompts_cursor_pagination(self, monkeypatch):
        rows = [_row(id="gwprm_a")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/gateway/prompts?cursor=support.reply|1&limit=5")
        assert resp.status_code == 200
        # SQL 包含 cursor 谓词
        assert any("(p.name, p.version) >" in c[0] for c in conn.calls)


# ============================================================================
# 6. Get 详情
# ============================================================================


class TestGetPrompt:
    @pytest.mark.asyncio
    async def test_get_prompt_returns_versions(self, monkeypatch):
        row = _row(version=1)
        version_rows = [_row(version=1), _row(id="gwprm_2", version=2)]
        conn = _RecordingConnection(results=[_Result(row=row), _Result(rows=version_rows)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/gateway/prompts/gwprm_1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["prompt"]["id"] == "gwprm_1"
        assert body["current_version"] == 1
        assert len(body["versions"]) == 2

    @pytest.mark.asyncio
    async def test_get_prompt_404_on_deleted(self, monkeypatch):
        row = _row(status="deleted")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/gateway/prompts/gwprm_1")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_prompt_404_on_missing(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/gateway/prompts/gwprm_missing")
        assert resp.status_code == 404


# ============================================================================
# 7. Patch 更新
# ============================================================================


class TestPatchPrompt:
    @pytest.mark.asyncio
    async def test_patch_prompt_updates_metadata(self, monkeypatch):
        original = _row(status="draft")
        updated = _row(status="draft", description="Updated", tags=["new"])
        conn = _RecordingConnection(results=[_Result(row=original), _Result(row=updated)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(
                "/api/v1/gateway/prompts/gwprm_1",
                json={"description": "Updated", "tags": ["new"]},
            )
        assert resp.status_code == 200
        assert resp.json()["description"] == "Updated"
        assert resp.json()["tags"] == ["new"]
        assert conn.committed is True

    @pytest.mark.asyncio
    async def test_patch_prompt_rejects_published(self, monkeypatch):
        row = _row(status="published")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(
                "/api/v1/gateway/prompts/gwprm_1",
                json={"description": "Updated"},
            )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_patch_prompt_name_conflict_409(self, monkeypatch):
        original = _row(status="draft")
        # conflict check 返回 1 行表示冲突
        conn = _RecordingConnection(results=[_Result(row=original), _Result(row={"1": 1})])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(
                "/api/v1/gateway/prompts/gwprm_1",
                json={"name": "taken.name"},
            )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_patch_prompt_404_on_deleted(self, monkeypatch):
        row = _row(status="deleted")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(
                "/api/v1/gateway/prompts/gwprm_1",
                json={"description": "Updated"},
            )
        assert resp.status_code == 404


# ============================================================================
# 8. Delete 软删除
# ============================================================================


class TestDeletePrompt:
    @pytest.mark.asyncio
    async def test_delete_prompt_soft_deletes(self, monkeypatch):
        row = _row(status="draft")
        conn = _RecordingConnection(results=[_Result(row=row), _Result(row={"id": "gwprm_1"})])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor(capabilities=("prompt:delete",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete("/api/v1/gateway/prompts/gwprm_1")
        assert resp.status_code == 204
        # 验证 SQL 设置 status='deleted'
        assert any("status='deleted'" in c[0] and "deleted_at=now()" in c[0] for c in conn.calls)
        assert conn.committed is True

    @pytest.mark.asyncio
    async def test_delete_published_returns_409(self, monkeypatch):
        row = _row(status="published")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor(capabilities=("prompt:delete",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete("/api/v1/gateway/prompts/gwprm_1")
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_delete_prompt_403_without_capability(self, monkeypatch):
        app = _app(_actor(capabilities=("prompt:read",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete("/api/v1/gateway/prompts/gwprm_1")
        assert resp.status_code == 403


# ============================================================================
# 9. Versions
# ============================================================================


class TestVersions:
    @pytest.mark.asyncio
    async def test_create_version_inherits_parent_id(self, monkeypatch):
        base = _row(version=1)
        new_row = _row(id="gwprm_2", version=2, parent_version_id="gwprm_1", model_hint="gpt-4")
        conn = _RecordingConnection(results=[
            _Result(row=base),  # _get_version
            _Result(row={"version": 2}),  # max(version)+1
            _Result(row=new_row),  # INSERT RETURNING
        ])
        monkeypatch.setattr(gp, "pool", _Pool(conn))
        monkeypatch.setattr(gp, "new_id", lambda prefix: "gwprm_2")

        app = _app(_actor(capabilities=("prompt:write",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/gateway/prompts/gwprm_1/versions",
                json={"content": "new content", "template_variables": {"x": "string"}, "model_hint": "gpt-4"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["version"] == 2
        assert body["parent_version_id"] == "gwprm_1"
        assert body["model_hint"] == "gpt-4"

    @pytest.mark.asyncio
    async def test_list_versions(self, monkeypatch):
        base = _row(version=1)
        version_rows = [_row(version=2), _row(version=1)]
        conn = _RecordingConnection(results=[_Result(row=base), _Result(rows=version_rows)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/gateway/prompts/gwprm_1/versions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["meta"]["count"] == 2

    @pytest.mark.asyncio
    async def test_get_version_by_number(self, monkeypatch):
        base = _row(version=1)
        target = _row(version=2)
        conn = _RecordingConnection(results=[_Result(row=base), _Result(row=target)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/gateway/prompts/gwprm_1/versions/2")
        assert resp.status_code == 200
        assert resp.json()["version"] == 2

    @pytest.mark.asyncio
    async def test_get_version_by_id_string(self, monkeypatch):
        base = _row(version=1)
        target = _row(id="gwprm_2", version=2)
        conn = _RecordingConnection(results=[_Result(row=base), _Result(row=target)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/gateway/prompts/gwprm_1/versions/gwprm_2")
        assert resp.status_code == 200
        assert resp.json()["id"] == "gwprm_2"


# ============================================================================
# 10. Publish + 评测门禁
# ============================================================================


class TestPublishPromptVersion:
    @pytest.mark.asyncio
    async def test_publish_version_eval_gate_failed_no_eval(self, monkeypatch):
        target = _row(version=1, eval_status=None, eval_run_id=None, eval_total_cases=None, eval_passed_cases=None)
        conn = _RecordingConnection(results=[_Result(row=target)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor(capabilities=("prompt:release",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/v1/gateway/prompts/gwprm_1/versions/1/publish")
        assert resp.status_code == 422
        body = resp.json()
        detail = body["detail"]
        assert isinstance(detail, dict)
        assert detail["code"] == "eval_gate_failed"

    @pytest.mark.asyncio
    async def test_publish_version_eval_gate_failed_low_score(self, monkeypatch):
        # eval_status=passed 但 passed_cases/total_cases = 1/3 = 0.33 < 0.7
        target = _row(version=1, eval_status="passed", eval_total_cases=3, eval_passed_cases=1)
        conn = _RecordingConnection(results=[_Result(row=target)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor(capabilities=("prompt:release",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/v1/gateway/prompts/gwprm_1/versions/1/publish")
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["code"] == "eval_gate_failed"
        assert detail["mean_score"] < detail["threshold"]

    @pytest.mark.asyncio
    async def test_publish_version_success_with_eval_run_id(self, monkeypatch):
        target = _row(version=1, eval_status="passed", eval_total_cases=3, eval_passed_cases=3)
        eval_row = _eval_row(id="gweval_1", status="passed", total_cases=3, passed_cases=3)
        published = _row(version=1, status="published", rollout_percent=100)
        # 顺序: _get_version_by_number, _fetch_eval_run, UPDATE archived, UPDATE published RETURNING, INSERT rollout
        conn = _RecordingConnection(results=[
            _Result(row=target),
            _Result(row=eval_row),
            _Result(),
            _Result(row=published),
            _Result(),
        ])
        monkeypatch.setattr(gp, "pool", _Pool(conn))
        monkeypatch.setattr(gp, "new_id", lambda prefix: "pfrl_1")

        app = _app(_actor(capabilities=("prompt:release",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/gateway/prompts/gwprm_1/versions/1/publish",
                json={"eval_run_id": "gweval_1", "rollout_percent": 100},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "published"
        assert body["rollout_percent"] == 100
        assert body["rollout_strategy"] == "all"

    @pytest.mark.asyncio
    async def test_publish_version_eval_run_id_not_belonging_returns_422(self, monkeypatch):
        target = _row(version=1, eval_status="passed", eval_total_cases=3, eval_passed_cases=3)
        # _fetch_eval_run 返回 None
        conn = _RecordingConnection(results=[_Result(row=target), _Result(row=None)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor(capabilities=("prompt:release",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/gateway/prompts/gwprm_1/versions/1/publish",
                json={"eval_run_id": "gweval_other"},
            )
        assert resp.status_code == 422
        assert "eval_run_id does not belong" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_publish_version_invalid_rollout_percent_returns_422(self, monkeypatch):
        app = _app(_actor(capabilities=("prompt:release",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/gateway/prompts/gwprm_1/versions/1/publish",
                json={"rollout_percent": 30},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_publish_version_partial_rollout_50(self, monkeypatch):
        target = _row(version=2, eval_status="passed", eval_total_cases=3, eval_passed_cases=3)
        published = _row(version=2, status="published", rollout_percent=50)
        conn = _RecordingConnection(results=[
            _Result(row=target),  # _get_version_by_number
            _Result(),  # UPDATE archived
            _Result(row=published),  # UPDATE published RETURNING
            _Result(),  # INSERT rollout
        ])
        monkeypatch.setattr(gp, "pool", _Pool(conn))
        monkeypatch.setattr(gp, "new_id", lambda prefix: "pfrl_1")

        app = _app(_actor(capabilities=("prompt:release",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/gateway/prompts/gwprm_1/versions/2/publish",
                json={"rollout_percent": 50},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["rollout_percent"] == 50
        assert body["rollout_strategy"] == "stable_sha256"

    @pytest.mark.asyncio
    async def test_publish_version_404_on_missing(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor(capabilities=("prompt:release",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/v1/gateway/prompts/gwprm_missing/versions/1/publish")
        assert resp.status_code == 404


# ============================================================================
# 11. Rollback
# ============================================================================


class TestRollbackPromptVersion:
    @pytest.mark.asyncio
    async def test_rollback_creates_new_version(self, monkeypatch):
        target = _row(version=1, status="published")
        new_row = _row(id="gwprm_2", version=2, parent_version_id="gwprm_1")
        published = _row(id="gwprm_2", version=2, status="published", rollout_percent=100, parent_version_id="gwprm_1")
        # 顺序: _get_version_by_number, max(version)+1, INSERT new, UPDATE archived, UPDATE published RETURNING
        conn = _RecordingConnection(results=[
            _Result(row=target),
            _Result(row={"version": 2}),
            _Result(row=new_row),
            _Result(),
            _Result(row=published),
        ])
        monkeypatch.setattr(gp, "pool", _Pool(conn))
        monkeypatch.setattr(gp, "new_id", lambda prefix: "gwprm_2")

        app = _app(_actor(capabilities=("prompt:release",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/v1/gateway/prompts/gwprm_1/versions/1/rollback")
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == 2
        assert body["status"] == "published"
        assert body["rollback_from_version"] == 1
        assert body["rollback_to_version"] == 2
        assert body["parent_version_id"] == "gwprm_1"

    @pytest.mark.asyncio
    async def test_rollback_404_on_missing_version(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor(capabilities=("prompt:release",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/v1/gateway/prompts/gwprm_missing/versions/99/rollback")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_rollback_invalid_version_returns_422(self, monkeypatch):
        app = _app(_actor(capabilities=("prompt:release",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/v1/gateway/prompts/gwprm_1/versions/0/rollback")
        assert resp.status_code == 422


# ============================================================================
# 12. Eval-status
# ============================================================================


class TestEvalStatus:
    @pytest.mark.asyncio
    async def test_eval_status_returns_latest(self, monkeypatch):
        target = _row(version=1)
        eval_runs = [_eval_row(id="gweval_2", status="passed", total_cases=3, passed_cases=3)]
        conn = _RecordingConnection(results=[_Result(row=target), _Result(rows=eval_runs)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/gateway/prompts/gwprm_1/versions/1/eval-status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["eval_run_id"] == "gweval_2"
        assert body["eval_status"] == "passed"
        assert body["mean_score"] == 1.0
        assert body["gate_passed"] is True
        assert body["gate_threshold"] == gp._DEFAULT_EVAL_THRESHOLD

    @pytest.mark.asyncio
    async def test_eval_status_no_runs(self, monkeypatch):
        target = _row(version=1)
        conn = _RecordingConnection(results=[_Result(row=target), _Result(rows=[])])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/gateway/prompts/gwprm_1/versions/1/eval-status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["eval_run_id"] is None
        assert body["gate_passed"] is False
        assert body["mean_score"] == 0.0


# ============================================================================
# 13. Rollout patch
# ============================================================================


class TestPatchRollout:
    @pytest.mark.asyncio
    async def test_patch_rollout_success(self, monkeypatch):
        row = _row(status="published")
        rollout_row = {"id": "pfrl_1", "percent": 50, "strategy": "stable_sha256", "updated_at": datetime.now(UTC)}
        # 顺序: _get_version, INSERT rollout RETURNING, UPDATE sec_prompt_version
        conn = _RecordingConnection(results=[_Result(row=row), _Result(row=rollout_row), _Result()])
        monkeypatch.setattr(gp, "pool", _Pool(conn))
        monkeypatch.setattr(gp, "new_id", lambda prefix: "pfrl_1")

        app = _app(_actor(capabilities=("prompt:release",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(
                "/api/v1/gateway/prompts/gwprm_1/rollout",
                json={"percent": 50},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["percent"] == 50
        assert body["strategy"] == "stable_sha256"

    @pytest.mark.asyncio
    async def test_patch_rollout_invalid_percent_returns_422(self, monkeypatch):
        app = _app(_actor(capabilities=("prompt:release",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(
                "/api/v1/gateway/prompts/gwprm_1/rollout",
                json={"percent": 30},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_patch_rollout_404_on_deleted(self, monkeypatch):
        row = _row(status="deleted")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor(capabilities=("prompt:release",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(
                "/api/v1/gateway/prompts/gwprm_1/rollout",
                json={"percent": 50},
            )
        assert resp.status_code == 404


# ============================================================================
# 14. Search
# ============================================================================


class TestSearchPrompts:
    @pytest.mark.asyncio
    async def test_search_prompts_matches_name(self, monkeypatch):
        rows = [_row(name="support.reply")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/gateway/prompts/search?q=support")
        assert resp.status_code == 200
        body = resp.json()
        assert body["meta"]["count"] == 1
        assert body["meta"]["query"] == "support"
        # 验证 ILIKE
        assert any("ILIKE" in c[0] for c in conn.calls)

    @pytest.mark.asyncio
    async def test_search_prompts_empty_query_returns_422(self, monkeypatch):
        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/gateway/prompts/search?q=")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_search_prompts_excludes_deleted(self, monkeypatch):
        rows = []
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/gateway/prompts/search?q=anything")
        assert resp.status_code == 200
        # SQL 包含 status<>'deleted'
        assert any("p.status<>'deleted'" in c[0] for c in conn.calls)


# ============================================================================
# 15. 鉴权 / 路由覆盖
# ============================================================================


class TestAuthAndRoutes:
    @pytest.mark.asyncio
    async def test_unauthorized_401_no_actor(self):
        # 不提供 actor override → get_actor 会因缺少 Authorization 头返回 401
        app = _app(actor=None)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/gateway/prompts")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_publish_requires_release_capability(self, monkeypatch):
        app = _app(_actor(capabilities=("prompt:write",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/v1/gateway/prompts/gwprm_1/versions/1/publish")
        # 缺 prompt:release，应在 422 之前 403
        assert resp.status_code == 403

    def test_routes_cover_full_crud(self):
        paths = {(route.path, tuple(sorted(route.methods or ()))) for route in gp.router.routes}
        # 完整 CRUD
        assert ("/api/v1/gateway/prompts", ("GET",)) in paths
        assert ("/api/v1/gateway/prompts", ("POST",)) in paths
        assert ("/api/v1/gateway/prompts/{prompt_id}", ("GET",)) in paths
        assert ("/api/v1/gateway/prompts/{prompt_id}", ("PATCH",)) in paths
        assert ("/api/v1/gateway/prompts/{prompt_id}", ("DELETE",)) in paths
        # 搜索
        assert ("/api/v1/gateway/prompts/search", ("GET",)) in paths
        # 版本管理
        assert ("/api/v1/gateway/prompts/{prompt_id}/versions", ("GET",)) in paths
        assert ("/api/v1/gateway/prompts/{prompt_id}/versions", ("POST",)) in paths
        assert ("/api/v1/gateway/prompts/{prompt_id}/versions/{version_id}", ("GET",)) in paths
        # T-M1-007 新增
        assert ("/api/v1/gateway/prompts/{prompt_id}/versions/{version}/publish", ("POST",)) in paths
        assert ("/api/v1/gateway/prompts/{prompt_id}/versions/{version}/rollback", ("POST",)) in paths
        assert ("/api/v1/gateway/prompts/{prompt_id}/versions/{version}/eval-status", ("GET",)) in paths
        assert ("/api/v1/gateway/prompts/{prompt_id}/rollout", ("PATCH",)) in paths
        # 旧版兼容
        assert ("/api/v1/gateway/prompts/{prompt_id}/releases", ("POST",)) in paths
        assert ("/api/v1/gateway/prompts/{prompt_id}/rollbacks", ("POST",)) in paths


# ============================================================================
# 16. 软删除恢复 / 跨 workspace 隔离
# ============================================================================


class TestSoftDeleteAndIsolation:
    @pytest.mark.asyncio
    async def test_deleted_prompt_excluded_from_versions_list(self, monkeypatch):
        base = _row(version=1)
        # 版本列表只返回未删除的
        version_rows = [_row(version=1)]
        conn = _RecordingConnection(results=[_Result(row=base), _Result(rows=version_rows)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/gateway/prompts/gwprm_1/versions")
        assert resp.status_code == 200
        # SQL 应排除 deleted
        assert any("p.status<>'deleted'" in c[0] for c in conn.calls)

    @pytest.mark.asyncio
    async def test_get_version_404_on_deleted(self, monkeypatch):
        base = _row(version=1)
        target = _row(version=2, status="deleted")
        conn = _RecordingConnection(results=[_Result(row=base), _Result(row=target)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/gateway/prompts/gwprm_1/versions/2")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_workspace_isolation_in_get_prompt(self, monkeypatch):
        # _get_version 返回 None（因 workspace 不匹配）
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor(workspace_id="wsp_a"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/gateway/prompts/gwprm_1")
        assert resp.status_code == 404
        # 验证 SQL 使用了 actor 的 workspace_id
        assert any("wsp_a" in c[1] for c in conn.calls)

    @pytest.mark.asyncio
    async def test_delete_already_deleted_returns_404(self, monkeypatch):
        row = _row(status="deleted")
        # UPDATE ... WHERE status<>'deleted' 返回 None
        conn = _RecordingConnection(results=[_Result(row=row), _Result(row=None)])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor(capabilities=("prompt:delete",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete("/api/v1/gateway/prompts/gwprm_1")
        assert resp.status_code == 404


# ============================================================================
# 17. 旧版 releases/rollbacks 向后兼容
# ============================================================================


class TestLegacyEndpoints:
    @pytest.mark.asyncio
    async def test_release_endpoint_still_works(self, monkeypatch):
        base = _row(version=1, eval_status="passed")
        published = _row(version=1, status="published", rollout_percent=100)
        # _publish 内部: _get_version(for_update), SELECT published, UPDATE archived, UPDATE published RETURNING
        conn = _RecordingConnection(results=[
            _Result(row=base),  # _get_version in release_prompt
            _Result(row=base),  # _publish -> _get_version(for_update)
            _Result(rows=[]),  # SELECT published versions
            _Result(),  # UPDATE archived (rollout=100 path)
            _Result(row=published),  # UPDATE published RETURNING
        ])
        monkeypatch.setattr(gp, "pool", _Pool(conn))

        app = _app(_actor(capabilities=("prompt:release",)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/gateway/prompts/gwprm_1/releases",
                json={"rollout_percent": 100},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "published"

    @pytest.mark.asyncio
    async def test_resolve_internal_endpoint_route_exists(self):
        internal_paths = {(route.path, tuple(sorted(route.methods or ()))) for route in gp.internal_router.routes}
        assert ("/internal/gateway/prompts/resolve", ("POST",)) in internal_paths
