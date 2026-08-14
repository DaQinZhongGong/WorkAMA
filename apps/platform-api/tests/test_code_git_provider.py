"""AMA-Code Git provider 单元测试。

覆盖：
- MockGitProvider 全流程（仓库/Issue/PR/分支/事件 CRUD）
- 租户隔离：不同 workspace 之间互不可见
- GitHubGitProvider 的纯逻辑方法（URL 解析、payload 视图、错误转换）
- GitProviderRegistry 缓存与回退
- 数据库 schema 语句包含必要字段与租户键
- API 路由路径与 capability 校验
- repo_view/issue_view/pr_view/event_view 不泄漏 credential_enc
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from workama_platform.core import Actor
from workama_platform.modules import code_git_provider as cgp
from workama_platform.modules.code_git_provider import (
    GitHubGitProvider,
    GitIssueCreate,
    GitPRCreate,
    GitProvider,
    GitProviderRegistry,
    GitProviderType,
    GitRepo,
    GitRepoCreate,
    MockGitProvider,
    SUPPORTED_PROVIDERS,
)


def actor(workspace_id: str, *, role: str = "owner", capabilities=()) -> Actor:
    return Actor(
        user_id="usr_test",
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email="owner@example.test",
        display_name="Owner",
        onboarding_completed=True,
        capabilities=capabilities or ("code:*",),
    )

# ---------------------------------------------------------------------------
# MockGitProvider 全流程
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_create_repo_returns_workspace_scoped_repo():
    provider = MockGitProvider()
    repo = await provider.create_repo(
        "wsp_a", GitRepoCreate(name="demo", provider="mock")
    )
    assert repo.name == "demo"
    assert repo.workspace_id == "wsp_a"
    assert repo.full_name == "wsp_a/demo"
    assert repo.default_branch == "main"
    assert repo.provider == "mock"
    assert repo.provider_repo_id is not None


@pytest.mark.asyncio
async def test_mock_create_repo_rejects_duplicate_name_in_workspace():
    provider = MockGitProvider()
    await provider.create_repo("wsp_a", GitRepoCreate(name="demo", provider="mock"))
    with pytest.raises(HTTPException) as exc:
        await provider.create_repo("wsp_a", GitRepoCreate(name="demo", provider="mock"))
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_mock_create_repo_rejects_unsupported_provider():
    provider = MockGitProvider()
    with pytest.raises(HTTPException) as exc:
        await provider.create_repo(
            "wsp_a", GitRepoCreate(name="demo", provider="unsupported")
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_mock_list_repos_returns_only_workspace_repos():
    provider = MockGitProvider()
    await provider.create_repo("wsp_a", GitRepoCreate(name="a", provider="mock"))
    await provider.create_repo("wsp_b", GitRepoCreate(name="b", provider="mock"))
    repos_a = await provider.list_repos("wsp_a")
    repos_b = await provider.list_repos("wsp_b")
    assert [r.name for r in repos_a] == ["a"]
    assert [r.name for r in repos_b] == ["b"]


@pytest.mark.asyncio
async def test_mock_get_repo_returns_none_for_other_workspace():
    provider = MockGitProvider()
    repo = await provider.create_repo("wsp_a", GitRepoCreate(name="a", provider="mock"))
    assert await provider.get_repo("wsp_a", repo.id) is repo
    assert await provider.get_repo("wsp_b", repo.id) is None


@pytest.mark.asyncio
async def test_mock_create_issue_requires_existing_repo():
    provider = MockGitProvider()
    with pytest.raises(HTTPException) as exc:
        await provider.create_issue(
            "wsp_a", GitIssueCreate(repo_id="missing", title="t")
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_mock_create_issue_returns_issue_with_provider_id():
    provider = MockGitProvider()
    repo = await provider.create_repo("wsp_a", GitRepoCreate(name="demo", provider="mock"))
    issue = await provider.create_issue(
        "wsp_a",
        GitIssueCreate(
            repo_id=repo.id,
            title="Bug",
            body="description",
            labels=["bug"],
            assignees=["alice"],
        ),
    )
    assert issue["title"] == "Bug"
    assert issue["repo_id"] == repo.id
    assert issue["labels"] == ["bug"]
    assert issue["assignees"] == ["alice"]
    assert issue["provider_issue_id"].startswith("mock-issue-")
    assert issue["status"] == "open"


@pytest.mark.asyncio
async def test_mock_list_issues_returns_only_repo_issues():
    provider = MockGitProvider()
    repo_a = await provider.create_repo("wsp_a", GitRepoCreate(name="a", provider="mock"))
    repo_b = await provider.create_repo("wsp_a", GitRepoCreate(name="b", provider="mock"))
    await provider.create_issue("wsp_a", GitIssueCreate(repo_id=repo_a.id, title="i1"))
    await provider.create_issue("wsp_a", GitIssueCreate(repo_id=repo_b.id, title="i2"))
    issues_a = await provider.list_issues("wsp_a", repo_a.id)
    issues_b = await provider.list_issues("wsp_a", repo_b.id)
    assert [i["title"] for i in issues_a] == ["i1"]
    assert [i["title"] for i in issues_b] == ["i2"]

@pytest.mark.asyncio
async def test_mock_create_pr_returns_pr_with_head_base():
    provider = MockGitProvider()
    repo = await provider.create_repo("wsp_a", GitRepoCreate(name="demo", provider="mock"))
    pr = await provider.create_pr(
        "wsp_a",
        GitPRCreate(
            repo_id=repo.id, title="Feature", body="body", head="feature", base="main"
        ),
    )
    assert pr["title"] == "Feature"
    assert pr["head"] == "feature"
    assert pr["base"] == "main"
    assert pr["draft"] is False
    assert pr["provider_pr_id"].startswith("mock-pr-")
    assert pr["status"] == "open"


@pytest.mark.asyncio
async def test_mock_create_pr_with_draft_flag():
    provider = MockGitProvider()
    repo = await provider.create_repo("wsp_a", GitRepoCreate(name="demo", provider="mock"))
    pr = await provider.create_pr(
        "wsp_a",
        GitPRCreate(repo_id=repo.id, title="Draft", head="h", base="main", draft=True),
    )
    assert pr["draft"] is True


@pytest.mark.asyncio
async def test_mock_list_prs_returns_only_repo_prs():
    provider = MockGitProvider()
    repo_a = await provider.create_repo("wsp_a", GitRepoCreate(name="a", provider="mock"))
    repo_b = await provider.create_repo("wsp_a", GitRepoCreate(name="b", provider="mock"))
    await provider.create_pr("wsp_a", GitPRCreate(repo_id=repo_a.id, title="p1", head="h", base="main"))
    await provider.create_pr("wsp_a", GitPRCreate(repo_id=repo_b.id, title="p2", head="h", base="main"))
    prs_a = await provider.list_prs("wsp_a", repo_a.id)
    prs_b = await provider.list_prs("wsp_a", repo_b.id)
    assert [p["title"] for p in prs_a] == ["p1"]
    assert [p["title"] for p in prs_b] == ["p2"]


@pytest.mark.asyncio
async def test_mock_create_branch_returns_ref():
    provider = MockGitProvider()
    repo = await provider.create_repo("wsp_a", GitRepoCreate(name="demo", provider="mock"))
    result = await provider.create_branch("wsp_a", repo.id, "feature", "main")
    assert result["branch"] == "feature"
    assert result["from_ref"] == "main"
    assert result["ref"] == "refs/heads/feature"
    assert result["repo_id"] == repo.id


@pytest.mark.asyncio
async def test_mock_create_branch_requires_existing_repo():
    provider = MockGitProvider()
    with pytest.raises(HTTPException) as exc:
        await provider.create_branch("wsp_a", "missing", "feature", "main")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_mock_list_events_records_all_actions():
    provider = MockGitProvider()
    repo = await provider.create_repo("wsp_a", GitRepoCreate(name="demo", provider="mock"))
    await provider.create_issue("wsp_a", GitIssueCreate(repo_id=repo.id, title="i"))
    await provider.create_pr(
        "wsp_a", GitPRCreate(repo_id=repo.id, title="p", head="h", base="main")
    )
    await provider.create_branch("wsp_a", repo.id, "feature", "main")
    events = await provider.list_events("wsp_a")
    event_types = [e["event_type"] for e in events]
    assert "repo.create" in event_types
    assert "issue.open" in event_types
    assert "pull_request.open" in event_types
    assert "branch.create" in event_types


@pytest.mark.asyncio
async def test_mock_list_events_filters_by_repo():
    provider = MockGitProvider()
    repo_a = await provider.create_repo("wsp_a", GitRepoCreate(name="a", provider="mock"))
    repo_b = await provider.create_repo("wsp_a", GitRepoCreate(name="b", provider="mock"))
    await provider.create_issue("wsp_a", GitIssueCreate(repo_id=repo_a.id, title="i1"))
    await provider.create_issue("wsp_a", GitIssueCreate(repo_id=repo_b.id, title="i2"))
    events_a = await provider.list_events("wsp_a", repo_a.id)
    events_b = await provider.list_events("wsp_a", repo_b.id)
    assert all(e["repo_id"] == repo_a.id for e in events_a)
    assert all(e["repo_id"] == repo_b.id for e in events_b)
    assert any(e["event_type"] == "issue.open" for e in events_a)
    assert any(e["event_type"] == "issue.open" for e in events_b)

# ---------------------------------------------------------------------------
# 租户隔离验证
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_isolation_workspace_a_repo_not_in_b():
    provider = MockGitProvider()
    repo = await provider.create_repo("wsp_a", GitRepoCreate(name="shared", provider="mock"))
    # workspace B 不能看到 workspace A 的仓库
    assert await provider.get_repo("wsp_b", repo.id) is None
    # workspace B 不能在 workspace A 的仓库下创建 Issue
    with pytest.raises(HTTPException) as exc:
        await provider.create_issue(
            "wsp_b", GitIssueCreate(repo_id=repo.id, title="x")
        )
    assert exc.value.status_code == 404
    # workspace B 列表不包含 A 的仓库
    repos_b = await provider.list_repos("wsp_b")
    assert repos_b == []


@pytest.mark.asyncio
async def test_tenant_isolation_events_are_workspace_scoped():
    provider = MockGitProvider()
    await provider.create_repo("wsp_a", GitRepoCreate(name="a", provider="mock"))
    await provider.create_repo("wsp_b", GitRepoCreate(name="b", provider="mock"))
    events_a = await provider.list_events("wsp_a")
    events_b = await provider.list_events("wsp_b")
    assert all(e["workspace_id"] == "wsp_a" for e in events_a)
    assert all(e["workspace_id"] == "wsp_b" for e in events_b)
    assert len(events_a) >= 1
    assert len(events_b) >= 1


# ---------------------------------------------------------------------------
# GitHubGitProvider 纯逻辑测试（不发起网络请求）
# ---------------------------------------------------------------------------


class TestGitHubGitProviderPure:
    def test_parse_owner_repo_https_url(self):
        assert (
            GitHubGitProvider._parse_owner_repo("https://github.com/owner/repo.git")
            == "owner/repo"
        )

    def test_parse_owner_repo_https_url_no_git_suffix(self):
        assert (
            GitHubGitProvider._parse_owner_repo("https://github.com/owner/repo")
            == "owner/repo"
        )

    def test_parse_owner_repo_ssh_url(self):
        assert (
            GitHubGitProvider._parse_owner_repo("git@github.com:owner/repo.git")
            == "owner/repo"
        )

    def test_parse_owner_repo_rejects_invalid_url(self):
        with pytest.raises(HTTPException) as exc:
            GitHubGitProvider._parse_owner_repo("not-a-url")
        assert exc.value.status_code == 400

    def test_parse_owner_repo_rejects_too_many_segments(self):
        with pytest.raises(HTTPException) as exc:
            GitHubGitProvider._parse_owner_repo("https://github.com/a/b/c")
        assert exc.value.status_code == 400

    def test_repo_lookup_path_owner_repo(self):
        assert (
            GitHubGitProvider._repo_lookup_path("owner/repo")
            == "/repos/owner/repo"
        )

    def test_repo_lookup_path_numeric_id(self):
        assert (
            GitHubGitProvider._repo_lookup_path("12345")
            == "/repositories/12345"
        )

    def test_repo_lookup_path_rejects_mock_id(self):
        with pytest.raises(HTTPException) as exc:
            GitHubGitProvider._repo_lookup_path("mock_repo_1")
        assert exc.value.status_code == 400

    def test_repo_from_payload_maps_required_fields(self):
        repo = GitHubGitProvider._repo_from_payload(
            "wsp_a",
            {
                "id": 1234,
                "name": "demo",
                "full_name": "owner/demo",
                "html_url": "https://github.com/owner/demo",
                "default_branch": "develop",
            },
        )
        assert repo.id == "1234"
        assert repo.name == "demo"
        assert repo.full_name == "owner/demo"
        assert repo.url == "https://github.com/owner/demo"
        assert repo.default_branch == "develop"
        assert repo.workspace_id == "wsp_a"
        assert repo.provider == GitProviderType.GITHUB
        assert repo.provider_repo_id == "1234"

    def test_issue_view_maps_github_payload(self):
        view = GitHubGitProvider._issue_view(
            {
                "id": 42,
                "number": 7,
                "title": "Bug",
                "body": "desc",
                "labels": [{"name": "bug"}, {"name": "p0"}],
                "assignees": [{"login": "alice"}, {"login": "bob"}],
                "state": "open",
                "created_at": "2024-01-01T00:00:00Z",
            },
            "wsp_a",
            "repo-1",
        )
        assert view["id"] == "42"
        assert view["number"] == 7
        assert view["title"] == "Bug"
        assert view["labels"] == ["bug", "p0"]
        assert view["assignees"] == ["alice", "bob"]
        assert view["status"] == "open"
        assert view["repo_id"] == "repo-1"
        assert view["workspace_id"] == "wsp_a"

    def test_pr_view_maps_github_payload(self):
        view = GitHubGitProvider._pr_view(
            {
                "id": 99,
                "number": 3,
                "title": "PR",
                "body": "body",
                "head": {"ref": "feature"},
                "base": {"ref": "main"},
                "draft": True,
                "state": "open",
                "created_at": "2024-01-01T00:00:00Z",
            },
            "wsp_a",
            "repo-1",
        )
        assert view["head"] == "feature"
        assert view["base"] == "main"
        assert view["draft"] is True
        assert view["provider_pr_id"] == "99"

    def test_event_view_maps_github_payload(self):
        view = GitHubGitProvider._event_view(
            {
                "id": 1,
                "type": "PushEvent",
                "actor": {"login": "alice"},
                "payload": {"ref": "refs/heads/main"},
                "created_at": "2024-01-01T00:00:00Z",
            },
            "wsp_a",
            "repo-1",
        )
        assert view["event_type"] == "PushEvent"
        assert view["actor"] == "alice"
        assert view["payload"] == {"ref": "refs/heads/main"}

    def test_raise_for_status_passes_on_2xx(self):
        GitHubGitProvider._raise_for_status(200, {}, "test")
        GitHubGitProvider._raise_for_status(204, None, "test")

    def test_raise_for_status_raises_on_4xx(self):
        with pytest.raises(HTTPException) as exc:
            GitHubGitProvider._raise_for_status(404, {"message": "not found"}, "get")
        assert exc.value.status_code == 404

    def test_raise_for_status_raises_502_on_5xx(self):
        with pytest.raises(HTTPException) as exc:
            GitHubGitProvider._raise_for_status(500, {"message": "boom"}, "get")
        assert exc.value.status_code == 502

    def test_constructor_requires_token(self):
        with pytest.raises(ValueError):
            GitHubGitProvider(token="")

# ---------------------------------------------------------------------------
# GitProviderRegistry
# ---------------------------------------------------------------------------


def test_registry_returns_mock_for_mock_provider():
    r = GitProviderRegistry()
    p = r.get("wsp_a", GitProviderType.MOCK)
    assert isinstance(p, MockGitProvider)


def test_registry_returns_mock_when_credential_missing():
    r = GitProviderRegistry()
    p = r.get("wsp_a", GitProviderType.GITHUB)  # no credential
    assert isinstance(p, MockGitProvider)


def test_registry_returns_github_when_credential_provided():
    r = GitProviderRegistry()
    p = r.get("wsp_a", GitProviderType.GITHUB, credential="ghp_xxx")
    assert isinstance(p, GitHubGitProvider)


def test_registry_caches_provider_per_workspace():
    r = GitProviderRegistry()
    p1 = r.get("wsp_a", GitProviderType.GITHUB, credential="ghp_xxx")
    p2 = r.get("wsp_a", GitProviderType.GITHUB, credential="ghp_yyy")
    assert p1 is p2  # cached


def test_registry_clear_removes_specific_workspace():
    r = GitProviderRegistry()
    p_a = r.get("wsp_a", GitProviderType.GITHUB, credential="ghp_xxx")
    p_b = r.get("wsp_b", GitProviderType.GITHUB, credential="ghp_yyy")
    r.clear("wsp_a")
    p_a_new = r.get("wsp_a", GitProviderType.GITHUB, credential="ghp_zzz")
    p_b_new = r.get("wsp_b", GitProviderType.GITHUB, credential="ghp_yyy")
    assert p_a_new is not p_a  # was rebuilt
    assert p_b_new is p_b      # untouched


def test_registry_clear_all_removes_everything():
    r = GitProviderRegistry()
    r.get("wsp_a", GitProviderType.GITHUB, credential="ghp_xxx")
    r.get("wsp_b", GitProviderType.GITHUB, credential="ghp_yyy")
    r.clear()
    # After clear, accessing should rebuild (mock since no cred provided)
    assert isinstance(r.get("wsp_a", GitProviderType.GITHUB), MockGitProvider)


def test_registry_register_overrides_cache():
    r = GitProviderRegistry()
    custom = MockGitProvider()
    r.register("wsp_a", GitProviderType.GITHUB, custom)
    assert r.get("wsp_a", GitProviderType.GITHUB, credential="ghp_xxx") is custom


def test_registry_falls_back_to_mock_for_unsupported_provider():
    r = GitProviderRegistry()
    # GitLab is in SUPPORTED_PROVIDERS but _build returns None -> mock fallback
    p = r.get("wsp_a", GitProviderType.GITLAB, credential="glp_xxx")
    assert isinstance(p, MockGitProvider)


# ---------------------------------------------------------------------------
# Schema 语句验证
# ---------------------------------------------------------------------------


def test_schema_has_required_tables_and_tenant_keys():
    schema = "\n".join(cgp.SCHEMA_STATEMENTS)
    assert "CREATE TABLE IF NOT EXISTS code_repo" in schema
    assert "CREATE TABLE IF NOT EXISTS code_issue" in schema
    assert "CREATE TABLE IF NOT EXISTS code_pr" in schema
    assert "CREATE TABLE IF NOT EXISTS code_repo_event" in schema
    # 租户隔离：每个 CREATE TABLE 语句都应包含 workspace_id 字段。
    # 由于 SCHEMA_STATEMENTS 中 CREATE TABLE 与 CREATE INDEX 混排，
    # 这里按语句遍历，仅检查 CREATE TABLE 语句。
    for statement in cgp.SCHEMA_STATEMENTS:
        if "CREATE TABLE" not in statement:
            continue
        assert "workspace_id" in statement, (
            f"语句缺少 workspace_id 字段：{statement[:80]}"
        )


def test_schema_has_credential_enc_for_encrypted_storage():
    schema = "\n".join(cgp.SCHEMA_STATEMENTS)
    assert "credential_enc TEXT" in schema


def test_schema_has_unique_constraint_per_workspace():
    schema = "\n".join(cgp.SCHEMA_STATEMENTS)
    assert "UNIQUE(workspace_id, name)" in schema


def test_schema_has_indexes_for_query_performance():
    schema = "\n".join(cgp.SCHEMA_STATEMENTS)
    assert "idx_code_repo_workspace_time" in schema
    assert "idx_code_issue_repo_time" in schema
    assert "idx_code_pr_repo_time" in schema
    assert "idx_code_repo_event_workspace_time" in schema
    assert "idx_code_repo_event_repo_time" in schema


def test_supported_providers_contains_all_known_types():
    assert GitProviderType.GITHUB in SUPPORTED_PROVIDERS
    assert GitProviderType.GITLAB in SUPPORTED_PROVIDERS
    assert GitProviderType.GITEA in SUPPORTED_PROVIDERS
    assert GitProviderType.BITBUCKET in SUPPORTED_PROVIDERS
    assert GitProviderType.MOCK in SUPPORTED_PROVIDERS


def test_git_provider_is_abstract():
    # 不能直接实例化抽象类
    with pytest.raises(TypeError):
        GitProvider()

# ---------------------------------------------------------------------------
# 视图函数（防止凭据泄漏）
# ---------------------------------------------------------------------------


def test_repo_view_excludes_credential_enc():
    view = cgp.repo_view(
        {
            "id": "repo_1",
            "workspace_id": "wsp_1",
            "name": "demo",
            "full_name": "owner/demo",
            "url": "https://github.com/owner/demo",
            "default_branch": "main",
            "provider": "github",
            "provider_repo_id": "123",
            "namespace": "owner",
            "credential_enc": "encrypted-secret",
            "created_by": "usr_1",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
        }
    )
    assert "credential_enc" not in view
    assert "credential" not in view
    assert view["id"] == "repo_1"
    assert view["provider"] == "github"
    assert view["namespace"] == "owner"


def test_issue_view_returns_safe_fields():
    view = cgp.issue_view(
        {
            "id": "iss_1",
            "repo_id": "repo_1",
            "workspace_id": "wsp_1",
            "title": "Bug",
            "body": "desc",
            "labels": ["bug"],
            "assignees": ["alice"],
            "provider_issue_id": "42",
            "status": "open",
            "created_by": "usr_1",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
        }
    )
    assert view["title"] == "Bug"
    assert view["labels"] == ["bug"]
    assert view["assignees"] == ["alice"]
    assert view["status"] == "open"


def test_pr_view_returns_safe_fields():
    view = cgp.pr_view(
        {
            "id": "pr_1",
            "repo_id": "repo_1",
            "workspace_id": "wsp_1",
            "title": "PR",
            "body": "desc",
            "head": "feature",
            "base": "main",
            "draft": True,
            "provider_pr_id": "99",
            "status": "open",
            "created_by": "usr_1",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
        }
    )
    assert view["head"] == "feature"
    assert view["base"] == "main"
    assert view["draft"] is True


def test_event_view_returns_safe_payload():
    view = cgp.event_view(
        {
            "id": "evt_1",
            "workspace_id": "wsp_1",
            "repo_id": "repo_1",
            "event_type": "push",
            "actor": "alice",
            "payload": {"ref": "refs/heads/main"},
            "created_at": "2024-01-01T00:00:00Z",
        }
    )
    assert view["event_type"] == "push"
    assert view["actor"] == "alice"
    assert view["payload"] == {"ref": "refs/heads/main"}


# ---------------------------------------------------------------------------
# API 路由
# ---------------------------------------------------------------------------


def test_router_exposes_all_required_endpoints():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in cgp.router.routes}
    # 任务规格要求的端点
    assert ("/api/v1/code/repos", ("GET",)) in paths
    assert ("/api/v1/code/repos", ("POST",)) in paths
    assert ("/api/v1/code/repos/{repo_id}", ("GET",)) in paths
    assert ("/api/v1/code/repos/{repo_id}/issues", ("GET",)) in paths
    assert ("/api/v1/code/repos/{repo_id}/issues", ("POST",)) in paths
    assert ("/api/v1/code/repos/{repo_id}/prs", ("GET",)) in paths
    assert ("/api/v1/code/repos/{repo_id}/prs", ("POST",)) in paths
    assert ("/api/v1/code/repos/{repo_id}/branches", ("POST",)) in paths
    assert ("/api/v1/code/repos/{repo_id}/events", ("GET",)) in paths
    # 额外的工作区级事件端点
    assert ("/api/v1/code/events", ("GET",)) in paths


def test_router_prefix_is_under_code_namespace():
    assert cgp.router.prefix == "/api/v1/code"


def test_require_raises_when_capability_missing():
    viewer = actor("wsp_a", role="viewer", capabilities=("code:read",))
    cgp._require(viewer, "read")
    with pytest.raises(HTTPException) as exc:
        cgp._require(viewer, "write")
    assert exc.value.status_code == 403


def test_require_accepts_wildcard_capability():
    owner = actor("wsp_a", role="owner", capabilities=("code:*",))
    cgp._require(owner, "read")
    cgp._require(owner, "write")


# ---------------------------------------------------------------------------
# 数据库租户隔离辅助（mock 连接）
# ---------------------------------------------------------------------------


class _EmptyResult:
    async def fetchone(self):
        return None


class _RecordingConn:
    def __init__(self):
        self.calls = []

    async def execute(self, query, params=None):
        self.calls.append((query, params))
        return _EmptyResult()


@pytest.mark.asyncio
async def test_owned_repo_returns_404_when_repo_not_in_workspace():
    conn = _RecordingConn()
    with pytest.raises(HTTPException) as exc:
        await cgp._owned_repo(conn, "repo_other", actor("wsp_current"))
    assert exc.value.status_code == 404
    # 验证查询使用了 workspace_id 过滤
    query, params = conn.calls[0]
    assert "workspace_id = %s" in query
    assert params == ("repo_other", "wsp_current")


@pytest.mark.asyncio
async def test_record_event_inserts_with_correct_payload():
    conn = _RecordingConn()
    await cgp._record_event(
        conn,
        "wsp_a",
        "repo_1",
        "push",
        "alice",
        {"ref": "refs/heads/main"},
    )
    query, params = conn.calls[0]
    assert "INSERT INTO code_repo_event" in query
    assert params[1] == "wsp_a"  # workspace_id
    assert params[2] == "repo_1"  # repo_id
    assert params[3] == "push"    # event_type
    assert params[4] == "alice"   # actor
    assert "refs/heads/main" in params[5]  # payload JSON


# ---------------------------------------------------------------------------
# 加密凭据存储（encrypt_secret / decrypt_secret 集成）
# ---------------------------------------------------------------------------


def test_encrypt_secret_round_trip_for_provider_credentials():
    """验证 encrypt_secret/decrypt_secret 可正确往返 Git provider PAT。"""
    from workama_platform.core import decrypt_secret, encrypt_secret

    pat = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    encrypted = encrypt_secret(pat)
    assert encrypted != pat
    assert decrypt_secret(encrypted) == pat


def test_encrypt_secret_returns_none_for_empty_input():
    from workama_platform.core import encrypt_secret

    assert encrypt_secret(None) is None
    assert encrypt_secret("") is None