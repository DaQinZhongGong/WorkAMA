"""技能市场（Marketplace）单元 + 端点测试。

覆盖：
- 发布：成功 / draft skill 拒绝 / 重复发布幂等 / 技能不存在 404 / 未授权 401 / 无 write 权限 403 (6)
- 市场列表：空列表 / 分页 cursor / category 过滤 / tag 过滤 / search 关键词 /
  跨 workspace 可见 published / 不显示 draft (7)
- 市场详情：成功 / 不存在 404 / 跨 workspace 可见 / 评分统计 (4)
- 订阅：成功 / 订阅 draft 拒绝 / 重复订阅幂等 / 跨 workspace 订阅 (4)
- 评分：创建 / 更新幂等 / score 边界（1 和 5 通过，0 和 6 拒绝）/ 列表分页 / 评分统计 (6)
- 版本：创建 / 版本号自增 / 列表倒序 / changelog / workspace 隔离 / 不存在 404 (6)
- 辅助函数：_marketplace_view / _rating_view / _version_view / _parse_text_array /
  cursor 编解码 (6)
- Schema / 路由 (3)

所有测试使用 fake pool/connection，不依赖真实 DB / Redis / 网络。
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from workama_platform.core import Actor, get_actor
from workama_platform.modules import skills


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


class _SeqConnection:
    def __init__(self, results=None):
        self._results = list(results or [])
        self.calls: list[tuple[str, tuple]] = []
        self._idx = 0

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if self._idx < len(self._results):
            r = self._results[self._idx]
            self._idx += 1
            return r
        return _Result()

    async def commit(self):
        return None

    async def rollback(self):
        return None


class _Pool:
    def __init__(self, connection):
        self._connection = connection

    def connection(self):
        connection = self._connection

        class _Context:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *_args):
                return False

        return _Context()


def _actor(
    *,
    capabilities=("skill:*",),
    workspace_id="wsp_current",
    user_id="usr_owner",
    role="admin",
) -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email="owner@example.test",
        display_name="Owner",
        onboarding_completed=True,
        capabilities=capabilities,
    )


_NOW = datetime.now(UTC)


def _skill_row(**overrides) -> dict:
    base = {
        "id": "skill_1",
        "workspace_id": "wsp_current",
        "name": "research-helper",
        "publisher": "workama",
        "semver": "1.2.3",
        "review_status": "approved",
        "status": "active",
        "manifest": {"name": "research-helper", "version": "1.2.3"},
    }
    base.update(overrides)
    return base


def _listing_row(**overrides) -> dict:
    base = {
        "skill_id": "skill_1",
        "workspace_id": "wsp_publisher",
        "category": "research",
        "tags": ["ai", "search"],
        "summary": "A research helper skill",
        "listing_status": "published",
        "published_at": _NOW,
        "created_at": _NOW,
        "updated_at": _NOW,
        "name": "research-helper",
        "publisher": "workama",
        "semver": "1.2.3",
        "risk_level": "low",
        "review_status": "approved",
        "skill_status": "active",
    }
    base.update(overrides)
    return base


def _rating_row(**overrides) -> dict:
    base = {
        "id": "skrate_1",
        "skill_id": "skill_1",
        "workspace_id": "wsp_current",
        "user_id": "usr_owner",
        "score": 5,
        "review_text": "Great skill",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return base


def _version_row(**overrides) -> dict:
    base = {
        "id": "skver_1",
        "skill_id": "skill_1",
        "workspace_id": "wsp_current",
        "version": 1,
        "manifest_snapshot": {"name": "research-helper", "version": "1.2.3"},
        "changelog": "initial release",
        "created_by": "usr_owner",
        "created_at": _NOW,
    }
    base.update(overrides)
    return base


def _install_row(**overrides) -> dict:
    base = {
        "id": "skillinst_1",
        "skill_id": "skill_1",
        "workspace_id": "wsp_current",
        "enabled": False,
        "status": "disabled",
        "version": 1,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return base


def _app(actor=None):
    app = FastAPI()
    app.include_router(skills.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


# ============================================================================
# 1. 发布技能到市场
# ============================================================================


@pytest.mark.asyncio
async def test_publish_skill_to_marketplace_success(monkeypatch):
    skill = _skill_row()
    listing = {
        "skill_id": "skill_1",
        "workspace_id": "wsp_current",
        "category": "research",
        "tags": ["ai", "search"],
        "summary": "A research helper",
        "listing_status": "published",
        "published_at": _NOW,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    conn = _SeqConnection(results=[_Result(row=skill), _Result(row=listing)])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    result = await skills.publish_skill_to_marketplace(
        skills.MarketplacePublishRequest(
            skill_id="skill_1",
            category="research",
            tags=["ai", "search"],
            summary="A research helper",
        ),
        _actor(),
    )
    assert result["skill_id"] == "skill_1"
    assert result["listing_status"] == "published"
    assert result["category"] == "research"
    assert result["name"] == "research-helper"
    assert result["publisher"] == "workama"
    # INSERT 应使用 ON CONFLICT 幂等 upsert
    insert_query, _ = conn.calls[1]
    assert "ON CONFLICT" in insert_query
    assert "listing_status='published'" in insert_query


@pytest.mark.asyncio
async def test_publish_skill_draft_rejected_409(monkeypatch):
    # 技能 review_status=pending（草稿/未审核）→ 拒绝发布
    skill = _skill_row(review_status="pending")
    conn = _SeqConnection(results=[_Result(row=skill)])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await skills.publish_skill_to_marketplace(
            skills.MarketplacePublishRequest(skill_id="skill_1"),
            _actor(),
        )
    assert exc.value.status_code == 409
    assert "approved" in exc.value.detail


@pytest.mark.asyncio
async def test_publish_skill_republish_is_idempotent(monkeypatch):
    """重复发布：ON CONFLICT DO UPDATE，保留首次 published_at，更新字段。"""
    skill = _skill_row()
    listing = {
        "skill_id": "skill_1",
        "workspace_id": "wsp_current",
        "category": "analytics",
        "tags": ["data"],
        "summary": "Updated summary",
        "listing_status": "published",
        "published_at": _NOW,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    conn = _SeqConnection(results=[_Result(row=skill), _Result(row=listing)])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    result = await skills.publish_skill_to_marketplace(
        skills.MarketplacePublishRequest(
            skill_id="skill_1",
            category="analytics",
            tags=["data"],
            summary="Updated summary",
        ),
        _actor(),
    )
    # 重复发布应成功（幂等），返回 published
    assert result["listing_status"] == "published"
    assert result["category"] == "analytics"
    insert_query, _ = conn.calls[1]
    # 保留首次 published_at
    assert "COALESCE(skill_marketplace_listing.published_at" in insert_query


@pytest.mark.asyncio
async def test_publish_skill_not_found_404(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await skills.publish_skill_to_marketplace(
            skills.MarketplacePublishRequest(skill_id="skill_missing"),
            _actor(),
        )
    assert exc.value.status_code == 404
    # 查询应带 actor.workspace_id（workspace 隔离）
    query, params = conn.calls[0]
    assert "wsp_current" in params


@pytest.mark.asyncio
async def test_publish_skill_missing_write_capability_403(monkeypatch):
    conn = _SeqConnection()
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await skills.publish_skill_to_marketplace(
            skills.MarketplacePublishRequest(skill_id="skill_1"),
            _actor(capabilities=(), role="viewer"),
        )
    assert exc.value.status_code == 403
    # 鉴权在 DB 访问前发生
    assert conn.calls == []


@pytest.mark.asyncio
async def test_publish_skill_unauthenticated_401():
    app = _app(actor=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/skills/marketplace/publish",
            json={"skill_id": "skill_1"},
        )
    assert resp.status_code == 401


# ============================================================================
# 2. 市场列表
# ============================================================================


@pytest.mark.asyncio
async def test_list_marketplace_empty(monkeypatch):
    conn = _SeqConnection(results=[_Result(rows=[])])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    result = await skills.list_marketplace_skills(
        _actor(), category=None, tag=None, search=None, limit=50, cursor=None
    )
    assert result["items"] == []
    assert result["has_more"] is False
    assert result["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_marketplace_pagination(monkeypatch):
    row = _listing_row()
    conn = _SeqConnection(results=[_Result(rows=[row])])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    result = await skills.list_marketplace_skills(
        _actor(), category=None, tag=None, search=None, limit=1, cursor=None
    )
    assert len(result["items"]) == 1
    assert result["has_more"] is True
    assert result["next_cursor"] is not None
    # cursor 解码后应为下一页 offset
    assert skills._decode_cursor(result["next_cursor"]) == 1
    # 查询应包含 LIMIT/OFFSET
    query, params = conn.calls[0]
    assert "LIMIT %s OFFSET %s" in query
    assert 1 in params and 0 in params


@pytest.mark.asyncio
async def test_list_marketplace_category_filter(monkeypatch):
    conn = _SeqConnection(results=[_Result(rows=[_listing_row()])])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    await skills.list_marketplace_skills(
        _actor(), category="research", tag=None, search=None, limit=50, cursor=None
    )
    query, params = conn.calls[0]
    assert "l.category=%s" in query
    assert "research" in params


@pytest.mark.asyncio
async def test_list_marketplace_tag_filter(monkeypatch):
    conn = _SeqConnection(results=[_Result(rows=[_listing_row()])])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    await skills.list_marketplace_skills(
        _actor(), category=None, tag="ai", search=None, limit=50, cursor=None
    )
    query, params = conn.calls[0]
    assert "= ANY(l.tags)" in query
    assert "ai" in params


@pytest.mark.asyncio
async def test_list_marketplace_search_keyword(monkeypatch):
    conn = _SeqConnection(results=[_Result(rows=[_listing_row()])])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    await skills.list_marketplace_skills(
        _actor(), category=None, tag=None, search="research", limit=50, cursor=None
    )
    query, params = conn.calls[0]
    assert "ILIKE" in query
    assert any("research" in str(p) for p in params)


@pytest.mark.asyncio
async def test_list_marketplace_cross_workspace_visible(monkeypatch):
    """listing 来自 wsp_publisher，actor 在 wsp_current，跨 workspace 可见。"""
    row = _listing_row(workspace_id="wsp_publisher")
    conn = _SeqConnection(results=[_Result(rows=[row])])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    result = await skills.list_marketplace_skills(
        _actor(workspace_id="wsp_current"), category=None, tag=None, search=None, limit=50, cursor=None
    )
    assert len(result["items"]) == 1
    query, params = conn.calls[0]
    # 查询不应按 actor.workspace_id 过滤 listing（跨 workspace 可见）
    assert "l.workspace_id=%s" not in query
    assert "s.workspace_id=%s" not in query
    assert "wsp_current" not in params


@pytest.mark.asyncio
async def test_list_marketplace_excludes_draft(monkeypatch):
    conn = _SeqConnection(results=[_Result(rows=[])])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    await skills.list_marketplace_skills(
        _actor(), category=None, tag=None, search=None, limit=50, cursor=None
    )
    query, _ = conn.calls[0]
    # 始终只查询 published 状态
    assert "listing_status='published'" in query


# ============================================================================
# 3. 市场详情
# ============================================================================


@pytest.mark.asyncio
async def test_get_marketplace_skill_success(monkeypatch):
    listing = _listing_row()
    stats = {"avg_score": 4.5, "rating_count": 10}
    conn = _SeqConnection(results=[_Result(row=listing), _Result(row=stats)])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    result = await skills.get_marketplace_skill("skill_1", _actor())
    assert result["skill_id"] == "skill_1"
    assert result["listing_status"] == "published"
    assert result["rating_avg"] == 4.5
    assert result["rating_count"] == 10


@pytest.mark.asyncio
async def test_get_marketplace_skill_not_found_404(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await skills.get_marketplace_skill("skill_missing", _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_marketplace_skill_cross_workspace_visible(monkeypatch):
    """详情跨 workspace 可见：listing 在 wsp_publisher，actor 在 wsp_other 仍可查。"""
    listing = _listing_row(workspace_id="wsp_publisher")
    stats = {"avg_score": 0, "rating_count": 0}
    conn = _SeqConnection(results=[_Result(row=listing), _Result(row=stats)])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    result = await skills.get_marketplace_skill("skill_1", _actor(workspace_id="wsp_other"))
    assert result["skill_id"] == "skill_1"
    # 查询不应按 actor.workspace_id 过滤
    query, params = conn.calls[0]
    assert "wsp_other" not in params


@pytest.mark.asyncio
async def test_get_marketplace_skill_rating_stats_zero_when_no_ratings(monkeypatch):
    listing = _listing_row()
    stats = {"avg_score": 0, "rating_count": 0}
    conn = _SeqConnection(results=[_Result(row=listing), _Result(row=stats)])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    result = await skills.get_marketplace_skill("skill_1", _actor())
    assert result["rating_avg"] == 0.0
    assert result["rating_count"] == 0


# ============================================================================
# 4. 订阅市场技能
# ============================================================================


@pytest.mark.asyncio
async def test_subscribe_marketplace_skill_success(monkeypatch):
    listing = _listing_row()
    install = _install_row()
    conn = _SeqConnection(results=[_Result(row=listing), _Result(row=install)])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    result = await skills.subscribe_marketplace_skill("skill_1", _actor())
    assert result["skill_id"] == "skill_1"
    assert result["deduplicated"] is False
    assert result["installation"]["id"] == "skillinst_1"
    assert result["installation"]["workspace_id"] == "wsp_current"
    assert result["listing"]["listing_status"] == "published"
    # 安装写入当前 workspace
    insert_query, insert_params = conn.calls[1]
    assert "INSERT INTO ag_skill_install" in insert_query
    assert "wsp_current" in insert_params


@pytest.mark.asyncio
async def test_subscribe_marketplace_skill_draft_rejected_404(monkeypatch):
    """订阅未发布（draft）技能 → listing 查不到 → 404。"""
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await skills.subscribe_marketplace_skill("skill_1", _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_subscribe_marketplace_skill_idempotent(monkeypatch):
    """重复订阅：INSERT ON CONFLICT DO NOTHING 返回 None，回查既有安装，deduplicated=True。"""
    listing = _listing_row()
    existing_install = _install_row()
    conn = _SeqConnection(
        results=[
            _Result(row=listing),  # listing
            _Result(row=None),  # INSERT 冲突返回 None
            _Result(row=existing_install),  # 回查既有安装
        ]
    )
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    result = await skills.subscribe_marketplace_skill("skill_1", _actor())
    assert result["deduplicated"] is True
    assert result["installation"]["id"] == "skillinst_1"
    insert_query, _ = conn.calls[1]
    assert "ON CONFLICT (workspace_id, skill_id) DO NOTHING" in insert_query


@pytest.mark.asyncio
async def test_subscribe_marketplace_skill_cross_workspace(monkeypatch):
    """跨 workspace 订阅：listing 来自 wsp_publisher，安装写入 actor 的 wsp_current。"""
    listing = _listing_row(workspace_id="wsp_publisher")
    install = _install_row()
    conn = _SeqConnection(results=[_Result(row=listing), _Result(row=install)])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    result = await skills.subscribe_marketplace_skill("skill_1", _actor(workspace_id="wsp_current"))
    assert result["installation"]["workspace_id"] == "wsp_current"
    assert result["listing"]["workspace_id"] == "wsp_publisher"


# ============================================================================
# 5. 评分
# ============================================================================


@pytest.mark.asyncio
async def test_create_marketplace_rating_success(monkeypatch):
    listing = _listing_row()
    rating = _rating_row(score=5)
    conn = _SeqConnection(results=[_Result(row=listing), _Result(row=rating)])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    result = await skills.create_marketplace_rating(
        "skill_1",
        skills.MarketplaceRatingRequest(score=5, review_text="Great skill"),
        _actor(),
    )
    assert result["score"] == 5
    assert result["review_text"] == "Great skill"
    assert result["workspace_id"] == "wsp_current"
    assert result["user_id"] == "usr_owner"


@pytest.mark.asyncio
async def test_create_marketplace_rating_idempotent_update(monkeypatch):
    """同 workspace+user 重复评分：ON CONFLICT DO UPDATE 仅保留最新。"""
    listing = _listing_row()
    rating = _rating_row(score=3, review_text="updated")
    conn = _SeqConnection(results=[_Result(row=listing), _Result(row=rating)])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    result = await skills.create_marketplace_rating(
        "skill_1",
        skills.MarketplaceRatingRequest(score=3, review_text="updated"),
        _actor(),
    )
    assert result["score"] == 3
    insert_query, _ = conn.calls[1]
    assert "ON CONFLICT (skill_id, workspace_id, user_id) DO UPDATE" in insert_query


@pytest.mark.asyncio
async def test_create_marketplace_rating_score_boundaries_valid():
    """score=1 和 score=5 应通过 Pydantic 校验。"""
    r1 = skills.MarketplaceRatingRequest(score=1)
    assert r1.score == 1
    r5 = skills.MarketplaceRatingRequest(score=5)
    assert r5.score == 5


@pytest.mark.asyncio
async def test_create_marketplace_rating_score_zero_rejected():
    """score=0 应被 Pydantic 拒绝。"""
    with pytest.raises(ValidationError):
        skills.MarketplaceRatingRequest(score=0)


@pytest.mark.asyncio
async def test_create_marketplace_rating_score_six_rejected():
    """score=6 应被 Pydantic 拒绝。"""
    with pytest.raises(ValidationError):
        skills.MarketplaceRatingRequest(score=6)


@pytest.mark.asyncio
async def test_create_marketplace_rating_not_published_404(monkeypatch):
    """对未发布技能评分 → listing 查不到 → 404。"""
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await skills.create_marketplace_rating(
            "skill_1",
            skills.MarketplaceRatingRequest(score=4),
            _actor(),
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_list_marketplace_ratings_pagination(monkeypatch):
    listing = _listing_row()
    ratings = [_rating_row(id="skrate_1"), _rating_row(id="skrate_2")]
    stats = {"avg_score": 4.0, "rating_count": 2}
    conn = _SeqConnection(
        results=[
            _Result(row=listing),  # listing 校验
            _Result(rows=ratings),  # 评分列表
            _Result(row=stats),  # 评分统计
        ]
    )
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    result = await skills.list_marketplace_ratings("skill_1", _actor(), limit=50, cursor=None)
    assert len(result["items"]) == 2
    assert result["rating_avg"] == 4.0
    assert result["rating_count"] == 2
    query, _ = conn.calls[1]
    assert "ORDER BY created_at DESC" in query


@pytest.mark.asyncio
async def test_list_marketplace_ratings_stats(monkeypatch):
    """评分列表附带 avg/count 统计。"""
    listing = _listing_row()
    stats = {"avg_score": 3.5, "rating_count": 4}
    conn = _SeqConnection(
        results=[_Result(row=listing), _Result(rows=[]), _Result(row=stats)]
    )
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    result = await skills.list_marketplace_ratings("skill_1", _actor(), limit=50, cursor=None)
    assert result["rating_avg"] == 3.5
    assert result["rating_count"] == 4


# ============================================================================
# 6. 版本
# ============================================================================


@pytest.mark.asyncio
async def test_create_skill_version_success(monkeypatch):
    skill = _skill_row()
    version_row = _version_row(version=1, changelog="initial release")
    conn = _SeqConnection(
        results=[
            _Result(row=skill),  # SELECT skill
            _Result(row={"next_version": 1}),  # MAX(version)
            _Result(row=version_row),  # INSERT RETURNING
        ]
    )
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    result = await skills.create_skill_version(
        "skill_1",
        skills.SkillVersionCreateRequest(changelog="initial release"),
        _actor(),
    )
    assert result["version"] == 1
    assert result["changelog"] == "initial release"
    assert result["workspace_id"] == "wsp_current"
    # 版本号自增查询应带 workspace 隔离
    max_query, max_params = conn.calls[1]
    assert "COALESCE(MAX(version),0)+1" in max_query
    assert "wsp_current" in max_params


@pytest.mark.asyncio
async def test_create_skill_version_autoincrement(monkeypatch):
    """版本号自增：已有 max=2 → 新版本=3。"""
    skill = _skill_row()
    version_row = _version_row(version=3)
    conn = _SeqConnection(
        results=[
            _Result(row=skill),
            _Result(row={"next_version": 3}),
            _Result(row=version_row),
        ]
    )
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    result = await skills.create_skill_version(
        "skill_1",
        skills.SkillVersionCreateRequest(changelog="v3"),
        _actor(),
    )
    assert result["version"] == 3
    # INSERT 应使用自增后的版本号
    insert_query, insert_params = conn.calls[2]
    assert 3 in insert_params


@pytest.mark.asyncio
async def test_list_skill_versions_descending(monkeypatch):
    rows = [
        _version_row(id="skver_3", version=3),
        _version_row(id="skver_2", version=2),
        _version_row(id="skver_1", version=1),
    ]
    conn = _SeqConnection(results=[_Result(rows=rows)])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    result = await skills.list_skill_versions("skill_1", _actor(), limit=50, cursor=None)
    versions = [item["version"] for item in result["items"]]
    assert versions == [3, 2, 1]
    query, params = conn.calls[0]
    assert "ORDER BY version DESC" in query
    # workspace 隔离
    assert "wsp_current" in params


@pytest.mark.asyncio
async def test_create_skill_version_changelog_persisted(monkeypatch):
    skill = _skill_row()
    version_row = _version_row(changelog="bug fixes and perf improvements")
    conn = _SeqConnection(
        results=[
            _Result(row=skill),
            _Result(row={"next_version": 1}),
            _Result(row=version_row),
        ]
    )
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    result = await skills.create_skill_version(
        "skill_1",
        skills.SkillVersionCreateRequest(changelog="bug fixes and perf improvements"),
        _actor(),
    )
    assert result["changelog"] == "bug fixes and perf improvements"
    insert_query, insert_params = conn.calls[2]
    assert "bug fixes and perf improvements" in insert_params


@pytest.mark.asyncio
async def test_create_skill_version_workspace_isolation_404(monkeypatch):
    """技能不在当前 workspace → 404。"""
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await skills.create_skill_version(
            "skill_1",
            skills.SkillVersionCreateRequest(),
            _actor(workspace_id="wsp_other"),
        )
    assert exc.value.status_code == 404
    query, params = conn.calls[0]
    assert "wsp_other" in params


@pytest.mark.asyncio
async def test_list_skill_versions_empty(monkeypatch):
    conn = _SeqConnection(results=[_Result(rows=[])])
    monkeypatch.setattr(skills, "pool", _Pool(conn))

    result = await skills.list_skill_versions("skill_1", _actor(), limit=50, cursor=None)
    assert result["items"] == []
    assert result["has_more"] is False


# ============================================================================
# 7. 辅助函数
# ============================================================================


def test_marketplace_view_with_rating_stats():
    row = _listing_row()
    view = skills._marketplace_view(row, rating_stats={"avg_score": 4.2, "rating_count": 7})
    assert view["skill_id"] == "skill_1"
    assert view["listing_status"] == "published"
    assert view["tags"] == ["ai", "search"]
    assert view["rating_avg"] == 4.2
    assert view["rating_count"] == 7


def test_marketplace_view_tags_parsed_from_string():
    """tags 以 JSON 字符串形式传入时应解析为 list。"""
    row = _listing_row(tags='["python","ml"]')
    view = skills._marketplace_view(row)
    assert view["tags"] == ["python", "ml"]


def test_rating_view_shape():
    view = skills._rating_view(_rating_row())
    assert view["id"] == "skrate_1"
    assert view["score"] == 5
    assert view["review_text"] == "Great skill"


def test_version_view_parses_jsonb_string():
    """manifest_snapshot 以 JSON 字符串传入时应解析为 dict。"""
    import json

    row = _version_row(manifest_snapshot=json.dumps({"name": "x", "version": "1.0.0"}))
    view = skills._version_view(row)
    assert view["manifest_snapshot"] == {"name": "x", "version": "1.0.0"}
    assert view["version"] == 1


def test_parse_text_array_variants():
    assert skills._parse_text_array(None) == []
    assert skills._parse_text_array(["a", "b"]) == ["a", "b"]
    assert skills._parse_text_array('["x","y"]') == ["x", "y"]
    assert skills._parse_text_array("plain") == ["plain"]
    assert skills._parse_text_array("") == []


def test_cursor_encode_decode_roundtrip():
    for offset in (0, 1, 50, 200):
        cursor = skills._encode_cursor(offset)
        assert skills._decode_cursor(cursor) == offset


def test_cursor_decode_invalid_raises_422():
    with pytest.raises(HTTPException) as exc:
        skills._decode_cursor("!!!not-base64!!!")
    assert exc.value.status_code == 422


# ============================================================================
# 8. Schema / 路由
# ============================================================================


@pytest.mark.asyncio
async def test_schema_includes_marketplace_tables():
    statements = []

    class Connection:
        async def execute(self, statement):
            statements.append(statement)

    await skills.ensure_skills_schema(Connection())
    schema = "\n".join(statements)
    assert "skill_marketplace_listing" in schema
    assert "skill_rating" in schema
    assert "skill_version" in schema
    assert "listing_status" in schema
    assert "manifest_snapshot" in schema
    assert "UNIQUE(skill_id, workspace_id, user_id)" in schema
    assert "CHECK (score BETWEEN 1 AND 5)" in schema
    assert "idx_skill_version_skill" in schema


def test_skills_router_exposes_marketplace_endpoints():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in skills.router.routes}
    assert ("/api/v1/skills/marketplace/publish", ("POST",)) in paths
    assert ("/api/v1/skills/marketplace", ("GET",)) in paths
    assert ("/api/v1/skills/marketplace/{skill_id}", ("GET",)) in paths
    assert ("/api/v1/skills/marketplace/{skill_id}/subscribe", ("POST",)) in paths
    assert ("/api/v1/skills/marketplace/{skill_id}/ratings", ("POST",)) in paths
    assert ("/api/v1/skills/marketplace/{skill_id}/ratings", ("GET",)) in paths
    assert ("/api/v1/skills/{skill_id}/versions", ("POST",)) in paths
    assert ("/api/v1/skills/{skill_id}/versions", ("GET",)) in paths


def test_existing_skill_endpoints_still_registered():
    """回归：现有 7 个端点仍注册（未被破坏）。"""
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in skills.router.routes}
    assert ("/api/v1/skills", ("GET",)) in paths
    assert ("/api/v1/skills/install", ("POST",)) in paths
    assert ("/api/v1/skills/{skill_id}", ("GET",)) in paths
    assert ("/api/v1/skills/{skill_id}/enable", ("POST",)) in paths
    assert ("/api/v1/skills/{skill_id}/disable", ("POST",)) in paths
    assert ("/api/v1/skills/{skill_id}/review", ("POST",)) in paths
    assert ("/api/v1/skills/{skill_id}/verify-signature", ("POST",)) in paths
