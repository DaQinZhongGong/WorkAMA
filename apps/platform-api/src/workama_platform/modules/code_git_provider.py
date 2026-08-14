"""AMA-Code 真实 Git provider 抽象层。

实现租户隔离的多 Git 托管平台（GitHub/GitLab/Gitea/Bitbucket）仓库、Issue、
PR 与事件流管理。每个工作区可独立配置 provider 与凭据，凭据使用
``workama_platform.core.encrypt_secret`` 加密存储；运行时按需解密并调用对应
平台 REST API。

本模块与既有 ``code.py`` 共存：
- ``code.py`` 处理本地任务编排（``code_repository``/``code_task``/``code_event``）
- 本模块处理真实 Git provider 集成（``code_repo``/``code_issue``/``code_pr``/
  ``code_repo_event``），暴露在 ``/api/v1/code/repos`` 路径下
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from workama_platform.core import (
    Actor,
    capability_allows,
    decrypt_secret,
    encrypt_secret,
    get_actor,
    new_id,
    pool,
)


router = APIRouter(prefix="/api/v1/code", tags=["code-git-provider"])


class GitProviderType:
    """支持的 Git provider 类型常量。"""

    GITHUB = "github"
    GITLAB = "gitlab"
    GITEA = "gitea"
    BITBUCKET = "bitbucket"
    MOCK = "mock"  # 仅用于开发/测试


SUPPORTED_PROVIDERS: frozenset[str] = frozenset(
    {
        GitProviderType.GITHUB,
        GitProviderType.GITLAB,
        GitProviderType.GITEA,
        GitProviderType.BITBUCKET,
        GitProviderType.MOCK,
    }
)


@dataclass
class GitRepo:
    """Git 仓库视图对象。"""

    id: str
    name: str
    full_name: str
    url: str
    default_branch: str
    workspace_id: str
    provider: str
    provider_repo_id: str | None = None

class GitRepoCreate(BaseModel):
    """创建仓库请求。"""

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    private: bool = True
    auto_init: bool = True
    provider: str = GitProviderType.GITHUB
    existing_remote_url: str | None = Field(default=None, max_length=2048)
    credential: str | None = Field(default=None, min_length=1, max_length=4096)
    namespace: str | None = Field(default=None, max_length=120)


class GitIssueCreate(BaseModel):
    """创建 Issue 请求。"""

    repo_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=400)
    body: str = Field(default="", max_length=20000)
    labels: list[str] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list)


class GitPRCreate(BaseModel):
    """创建 Pull Request 请求。"""

    repo_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=400)
    body: str = Field(default="", max_length=20000)
    head: str = Field(min_length=1, max_length=200)
    base: str = Field(min_length=1, max_length=200)
    draft: bool = False


class GitBranchCreate(BaseModel):
    """创建分支请求。"""

    branch: str = Field(min_length=1, max_length=200)
    from_ref: str = Field(default="main", min_length=1, max_length=200)

# ---------------------------------------------------------------------------
# 数据库 schema
# ---------------------------------------------------------------------------

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS code_repo (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        full_name TEXT NOT NULL,
        url TEXT NOT NULL,
        default_branch TEXT NOT NULL DEFAULT 'main',
        provider TEXT NOT NULL,
        provider_repo_id TEXT,
        namespace TEXT,
        credential_enc TEXT,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_by TEXT NOT NULL REFERENCES id_user(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(workspace_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS code_issue (
        id TEXT PRIMARY KEY,
        repo_id TEXT NOT NULL REFERENCES code_repo(id) ON DELETE CASCADE,
        workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        labels JSONB NOT NULL DEFAULT '[]'::jsonb,
        assignees JSONB NOT NULL DEFAULT '[]'::jsonb,
        provider_issue_id TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        created_by TEXT NOT NULL REFERENCES id_user(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS code_pr (
        id TEXT PRIMARY KEY,
        repo_id TEXT NOT NULL REFERENCES code_repo(id) ON DELETE CASCADE,
        workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        head TEXT NOT NULL,
        base TEXT NOT NULL,
        draft BOOLEAN NOT NULL DEFAULT FALSE,
        provider_pr_id TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        created_by TEXT NOT NULL REFERENCES id_user(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS code_repo_event (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
        repo_id TEXT REFERENCES code_repo(id) ON DELETE CASCADE,
        event_type TEXT NOT NULL,
        actor TEXT,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_code_repo_workspace_time ON code_repo(workspace_id, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_code_issue_repo_time ON code_issue(repo_id, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_code_pr_repo_time ON code_pr(repo_id, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_code_repo_event_workspace_time ON code_repo_event(workspace_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_code_repo_event_repo_time ON code_repo_event(repo_id, created_at DESC)",
)


async def ensure_code_git_provider_schema(conn) -> None:
    """应用 additive Git provider schema 到既有连接。"""
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)

# ---------------------------------------------------------------------------
# 抽象接口
# ---------------------------------------------------------------------------


class GitProvider(ABC):
    """Git provider 抽象接口。

    所有方法都按 ``workspace_id`` 进行租户隔离；具体实现负责将
    ``workspace_id`` 映射到对应平台的命名空间（例如 GitHub 组织）。
    """

    @abstractmethod
    async def create_repo(self, workspace_id: str, req: GitRepoCreate) -> GitRepo: ...

    @abstractmethod
    async def list_repos(self, workspace_id: str) -> list[GitRepo]: ...

    @abstractmethod
    async def get_repo(self, workspace_id: str, repo_id: str) -> GitRepo | None: ...

    @abstractmethod
    async def create_issue(self, workspace_id: str, req: GitIssueCreate) -> dict: ...

    @abstractmethod
    async def list_issues(self, workspace_id: str, repo_id: str) -> list[dict]: ...

    @abstractmethod
    async def create_pr(self, workspace_id: str, req: GitPRCreate) -> dict: ...

    @abstractmethod
    async def list_prs(self, workspace_id: str, repo_id: str) -> list[dict]: ...

    @abstractmethod
    async def create_branch(
        self, workspace_id: str, repo_id: str, branch: str, from_ref: str = "main"
    ) -> dict: ...

    @abstractmethod
    async def list_events(
        self, workspace_id: str, repo_id: str | None = None
    ) -> list[dict]: ...

# ---------------------------------------------------------------------------
# Mock Provider（开发/测试用）
# ---------------------------------------------------------------------------


class MockGitProvider(GitProvider):
    """Mock Git provider 用于开发测试。

    维护内存状态：仓库/Issue/PR/事件按 workspace 隔离。所有方法均为协程，
    以便与真实 provider 互换。
    """

    def __init__(self) -> None:
        self._repos: dict[str, list[GitRepo]] = {}
        self._issues: dict[str, list[dict]] = {}
        self._prs: dict[str, list[dict]] = {}
        self._events: dict[str, list[dict]] = {}
        self._repo_seq: int = 0

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).isoformat()

    async def create_repo(self, workspace_id: str, req: GitRepoCreate) -> GitRepo:
        if req.provider not in SUPPORTED_PROVIDERS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported provider: {req.provider}",
            )
        repos = self._repos.setdefault(workspace_id, [])
        if any(r.name == req.name for r in repos):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Repository '{req.name}' already exists in workspace",
            )
        self._repo_seq += 1
        repo_id = f"mock_repo_{self._repo_seq}"
        full_name = f"{workspace_id}/{req.name}"
        url = req.existing_remote_url or f"https://mock-git.example/{full_name}.git"
        repo = GitRepo(
            id=repo_id,
            name=req.name,
            full_name=full_name,
            url=url,
            default_branch="main",
            workspace_id=workspace_id,
            provider=req.provider,
            provider_repo_id=f"mock-{self._repo_seq}",
        )
        repos.append(repo)
        await self._record_event(
            workspace_id,
            repo.id,
            "repo.create",
            actor="mock",
            payload={"name": repo.name, "provider": repo.provider},
        )
        return repo

    async def list_repos(self, workspace_id: str) -> list[GitRepo]:
        return list(self._repos.get(workspace_id, []))

    async def get_repo(self, workspace_id: str, repo_id: str) -> GitRepo | None:
        for repo in self._repos.get(workspace_id, []):
            if repo.id == repo_id:
                return repo
        return None
    async def create_issue(self, workspace_id: str, req: GitIssueCreate) -> dict:
        repo = await self._require_repo(workspace_id, req.repo_id)
        issues = self._issues.setdefault(repo.id, [])
        seq = len(issues) + 1
        issue = {
            "id": f"mock_issue_{repo.id}_{seq}",
            "repo_id": repo.id,
            "workspace_id": workspace_id,
            "number": seq,
            "title": req.title,
            "body": req.body,
            "labels": list(req.labels),
            "assignees": list(req.assignees),
            "status": "open",
            "provider_issue_id": f"mock-issue-{seq}",
            "created_at": self._now_iso(),
        }
        issues.append(issue)
        await self._record_event(
            workspace_id,
            repo.id,
            "issue.open",
            actor="mock",
            payload={"issue_id": issue["id"], "title": issue["title"]},
        )
        return issue

    async def list_issues(self, workspace_id: str, repo_id: str) -> list[dict]:
        await self._require_repo(workspace_id, repo_id)
        return list(self._issues.get(repo_id, []))

    async def create_pr(self, workspace_id: str, req: GitPRCreate) -> dict:
        repo = await self._require_repo(workspace_id, req.repo_id)
        prs = self._prs.setdefault(repo.id, [])
        seq = len(prs) + 1
        pr = {
            "id": f"mock_pr_{repo.id}_{seq}",
            "repo_id": repo.id,
            "workspace_id": workspace_id,
            "number": seq,
            "title": req.title,
            "body": req.body,
            "head": req.head,
            "base": req.base,
            "draft": req.draft,
            "status": "open",
            "provider_pr_id": f"mock-pr-{seq}",
            "created_at": self._now_iso(),
        }
        prs.append(pr)
        await self._record_event(
            workspace_id,
            repo.id,
            "pull_request.open",
            actor="mock",
            payload={"pr_id": pr["id"], "title": pr["title"], "head": pr["head"]},
        )
        return pr

    async def list_prs(self, workspace_id: str, repo_id: str) -> list[dict]:
        await self._require_repo(workspace_id, repo_id)
        return list(self._prs.get(repo_id, []))
    async def create_branch(
        self, workspace_id: str, repo_id: str, branch: str, from_ref: str = "main"
    ) -> dict:
        repo = await self._require_repo(workspace_id, repo_id)
        result = {
            "repo_id": repo.id,
            "branch": branch,
            "from_ref": from_ref,
            "ref": f"refs/heads/{branch}",
            "created_at": self._now_iso(),
        }
        await self._record_event(
            workspace_id,
            repo.id,
            "branch.create",
            actor="mock",
            payload={"branch": branch, "from_ref": from_ref},
        )
        return result

    async def list_events(
        self, workspace_id: str, repo_id: str | None = None
    ) -> list[dict]:
        events = list(self._events.get(workspace_id, []))
        if repo_id:
            events = [evt for evt in events if evt.get("repo_id") == repo_id]
        return events

    async def _require_repo(self, workspace_id: str, repo_id: str) -> GitRepo:
        repo = await self.get_repo(workspace_id, repo_id)
        if repo is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Git repository not found in workspace",
            )
        return repo

    async def _record_event(
        self,
        workspace_id: str,
        repo_id: str | None,
        event_type: str,
        actor: str | None,
        payload: dict[str, Any],
    ) -> None:
        self._events.setdefault(workspace_id, []).append(
            {
                "id": f"mock_evt_{len(self._events.get(workspace_id, [])) + 1}",
                "workspace_id": workspace_id,
                "repo_id": repo_id,
                "event_type": event_type,
                "actor": actor,
                "payload": payload,
                "created_at": self._now_iso(),
            }
        )

# ---------------------------------------------------------------------------
# GitHub Provider
# ---------------------------------------------------------------------------


class GitHubGitProvider(GitProvider):
    """基于 GitHub REST API 的 provider 实现。

    使用 httpx.AsyncClient 调用 ``https://api.github.com``。每次调用按需
    构造 client，避免在长生命周期对象上持有连接状态；对错误响应统一抛出
    ``HTTPException`` 以便 FastAPI 转换。
    """

    BASE_URL = "https://api.github.com"

    def __init__(self, token: str, org: str = "") -> None:
        if not token:
            raise ValueError("GitHub token is required")
        self._token = token
        self._org = org

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @staticmethod
    def _raise_for_status(status_code: int, payload: dict | None, action: str) -> None:
        if 200 <= status_code < 300:
            return
        detail = (payload or {}).get("message") or "GitHub API error"
        raise HTTPException(
            status_code=status_code if status_code in (404, 403, 422, 409) else 502,
            detail=f"GitHub {action} failed: {detail}",
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        action: str = "request",
    ) -> dict:
        url = f"{self.BASE_URL}{path}"
        try:
            import httpx  # 延迟导入以避免测试 mock provider 时无 httpx

            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=json_body,
                    params=params,
                )
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"GitHub {action} network error: {exc}",
            ) from exc
        payload: dict
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {}
        self._raise_for_status(response.status_code, payload, action)
        return payload

    @staticmethod
    def _repo_from_payload(workspace_id: str, payload: dict) -> GitRepo:
        return GitRepo(
            id=str(payload.get("id")),
            name=payload.get("name", ""),
            full_name=payload.get("full_name", ""),
            url=payload.get("html_url") or payload.get("clone_url", ""),
            default_branch=payload.get("default_branch", "main"),
            workspace_id=workspace_id,
            provider=GitProviderType.GITHUB,
            provider_repo_id=str(payload.get("id")),
        )
    async def create_repo(self, workspace_id: str, req: GitRepoCreate) -> GitRepo:
        if req.existing_remote_url:
            owner_repo = self._parse_owner_repo(req.existing_remote_url)
            payload = await self._request(
                "GET",
                f"/repos/{owner_repo}",
                action="get_repo",
            )
            return self._repo_from_payload(workspace_id, payload)

        body = {
            "name": req.name,
            "description": req.description,
            "private": req.private,
            "auto_init": req.auto_init,
        }
        path = "/user/repos"
        if req.namespace:
            path = f"/orgs/{req.namespace}/repos"
        payload = await self._request("POST", path, json_body=body, action="create_repo")
        return self._repo_from_payload(workspace_id, payload)

    async def list_repos(self, workspace_id: str) -> list[GitRepo]:
        if self._org:
            payload = await self._request(
                "GET",
                f"/orgs/{self._org}/repos",
                params={"per_page": 100},
                action="list_repos",
            )
        else:
            payload = await self._request(
                "GET",
                "/user/repos",
                params={"per_page": 100, "affiliation": "owner"},
                action="list_repos",
            )
        if not isinstance(payload, list):
            return []
        return [self._repo_from_payload(workspace_id, item) for item in payload]

    async def get_repo(self, workspace_id: str, repo_id: str) -> GitRepo | None:
        path = self._repo_lookup_path(repo_id)
        try:
            payload = await self._request("GET", path, action="get_repo")
        except HTTPException as exc:
            if exc.status_code == 404:
                return None
            raise
        return self._repo_from_payload(workspace_id, payload)

    async def create_issue(self, workspace_id: str, req: GitIssueCreate) -> dict:
        owner_repo = self._repo_lookup_path(req.repo_id)
        body = {
            "title": req.title,
            "body": req.body,
            "labels": list(req.labels),
            "assignees": list(req.assignees),
        }
        payload = await self._request(
            "POST",
            f"/repos/{owner_repo}/issues",
            json_body=body,
            action="create_issue",
        )
        return self._issue_view(payload, workspace_id, req.repo_id)

    async def list_issues(self, workspace_id: str, repo_id: str) -> list[dict]:
        owner_repo = self._repo_lookup_path(repo_id)
        payload = await self._request(
            "GET",
            f"/repos/{owner_repo}/issues",
            params={"state": "all", "per_page": 100},
            action="list_issues",
        )
        if not isinstance(payload, list):
            return []
        return [self._issue_view(item, workspace_id, repo_id) for item in payload]
    async def create_pr(self, workspace_id: str, req: GitPRCreate) -> dict:
        owner_repo = self._repo_lookup_path(req.repo_id)
        body = {
            "title": req.title,
            "body": req.body,
            "head": req.head,
            "base": req.base,
            "draft": req.draft,
        }
        payload = await self._request(
            "POST",
            f"/repos/{owner_repo}/pulls",
            json_body=body,
            action="create_pr",
        )
        return self._pr_view(payload, workspace_id, req.repo_id)

    async def list_prs(self, workspace_id: str, repo_id: str) -> list[dict]:
        owner_repo = self._repo_lookup_path(repo_id)
        payload = await self._request(
            "GET",
            f"/repos/{owner_repo}/pulls",
            params={"state": "all", "per_page": 100},
            action="list_prs",
        )
        if not isinstance(payload, list):
            return []
        return [self._pr_view(item, workspace_id, repo_id) for item in payload]

    async def create_branch(
        self, workspace_id: str, repo_id: str, branch: str, from_ref: str = "main"
    ) -> dict:
        owner_repo = self._repo_lookup_path(repo_id)
        ref_payload = await self._request(
            "GET",
            f"/repos/{owner_repo}/git/refs/heads/{from_ref}",
            action="create_branch_ref",
        )
        sha = ref_payload.get("object", {}).get("sha")
        if not sha:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"GitHub create_branch missing sha for ref {from_ref}",
            )
        body = {"ref": f"refs/heads/{branch}", "sha": sha}
        payload = await self._request(
            "POST",
            f"/repos/{owner_repo}/git/refs",
            json_body=body,
            action="create_branch",
        )
        return {
            "repo_id": repo_id,
            "branch": branch,
            "from_ref": from_ref,
            "ref": payload.get("ref", f"refs/heads/{branch}"),
            "sha": sha,
            "created_at": datetime.now(UTC).isoformat(),
        }

    async def list_events(
        self, workspace_id: str, repo_id: str | None = None
    ) -> list[dict]:
        if not repo_id:
            return []
        owner_repo = self._repo_lookup_path(repo_id)
        payload = await self._request(
            "GET",
            f"/repos/{owner_repo}/events",
            params={"per_page": 100},
            action="list_events",
        )
        if not isinstance(payload, list):
            return []
        return [self._event_view(item, workspace_id, repo_id) for item in payload]
    @staticmethod
    def _parse_owner_repo(remote_url: str) -> str:
        """从 GitHub remote URL 解析 owner/repo。"""
        cleaned = remote_url.strip()
        if cleaned.startswith("git@"):
            _, _, path = cleaned.partition(":")
            cleaned = path
        cleaned = cleaned.split("#", 1)[0]
        if "://" in cleaned:
            cleaned = cleaned.split("://", 1)[1]
            if "/" in cleaned:
                cleaned = cleaned.split("/", 1)[1]
        cleaned = cleaned.removesuffix(".git")
        cleaned = cleaned.strip("/")
        if cleaned.count("/") != 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid GitHub remote URL: {remote_url}",
            )
        return cleaned

    @staticmethod
    def _repo_lookup_path(repo_id: str) -> str:
        """根据 repo_id 构造 GitHub ``/repos/{owner}/{repo}`` 路径。

        ``repo_id`` 既可能是数字 ID（来自 code_repo 表），也可能是
        ``owner/repo`` 形式。GitHub 的 ``/repositories/{id}`` 端点可避免需要
        owner，但 list/events 等仍需 owner/repo；因此优先解析 owner/repo。
        """
        if "/" in repo_id:
            return f"/repos/{repo_id}"
        if repo_id.isdigit():
            return f"/repositories/{repo_id}"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot resolve GitHub repo path for repo_id={repo_id}",
        )

    @staticmethod
    def _issue_view(payload: dict, workspace_id: str, repo_id: str) -> dict:
        return {
            "id": str(payload.get("id", "")),
            "repo_id": repo_id,
            "workspace_id": workspace_id,
            "number": payload.get("number"),
            "title": payload.get("title", ""),
            "body": payload.get("body") or "",
            "labels": [
                label.get("name")
                for label in payload.get("labels") or []
                if isinstance(label, dict)
            ],
            "assignees": [
                user.get("login")
                for user in payload.get("assignees") or []
                if isinstance(user, dict)
            ],
            "status": payload.get("state", "open"),
            "provider_issue_id": str(payload.get("id", "")),
            "created_at": payload.get("created_at"),
        }

    @staticmethod
    def _pr_view(payload: dict, workspace_id: str, repo_id: str) -> dict:
        return {
            "id": str(payload.get("id", "")),
            "repo_id": repo_id,
            "workspace_id": workspace_id,
            "number": payload.get("number"),
            "title": payload.get("title", ""),
            "body": payload.get("body") or "",
            "head": (payload.get("head") or {}).get("ref", ""),
            "base": (payload.get("base") or {}).get("ref", ""),
            "draft": bool(payload.get("draft", False)),
            "status": payload.get("state", "open"),
            "provider_pr_id": str(payload.get("id", "")),
            "created_at": payload.get("created_at"),
        }

    @staticmethod
    def _event_view(payload: dict, workspace_id: str, repo_id: str) -> dict:
        return {
            "id": str(payload.get("id", "")),
            "workspace_id": workspace_id,
            "repo_id": repo_id,
            "event_type": payload.get("type", "event"),
            "actor": (payload.get("actor") or {}).get("login"),
            "payload": payload.get("payload") or {},
            "created_at": payload.get("created_at"),
        }

# ---------------------------------------------------------------------------
# Provider 注册表（按工作区隔离）
# ---------------------------------------------------------------------------


class GitProviderRegistry:
    """按工作区缓存 provider 实例。

    生产环境应从数据库 ``code_repo.credential_enc`` 解密凭据并实例化对应
    provider；为避免每次请求重建 client，这里使用 LRU 风格缓存。
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], GitProvider] = {}
        self._mock: MockGitProvider | None = None

    def get_mock(self) -> MockGitProvider:
        if self._mock is None:
            self._mock = MockGitProvider()
        return self._mock

    def register(self, workspace_id: str, provider_name: str, instance: GitProvider) -> None:
        self._cache[(workspace_id, provider_name)] = instance

    def clear(self, workspace_id: str | None = None) -> None:
        if workspace_id is None:
            self._cache.clear()
            return
        for key in list(self._cache.keys()):
            if key[0] == workspace_id:
                self._cache.pop(key, None)

    def get(
        self,
        workspace_id: str,
        provider_name: str,
        *,
        credential: str | None = None,
        namespace: str | None = None,
    ) -> GitProvider:
        """获取 provider 实例。

        - ``provider_name == "mock"`` 或缺少凭据时返回 mock。
        - ``provider_name == "github"`` 且提供 ``credential`` 时返回 GitHub。
        - 其他情况回退到 mock。
        """
        if provider_name == GitProviderType.MOCK or not credential:
            return self.get_mock()
        cached = self._cache.get((workspace_id, provider_name))
        if cached is not None:
            return cached
        instance = self._build(provider_name, credential, namespace or "")
        if instance is not None:
            self._cache[(workspace_id, provider_name)] = instance
            return instance
        return self.get_mock()

    @staticmethod
    def _build(
        provider_name: str, credential: str, namespace: str
    ) -> GitProvider | None:
        if provider_name == GitProviderType.GITHUB:
            return GitHubGitProvider(token=credential, org=namespace)
        # GitLab/Gitea/Bitbucket 暂未实现真实调用，回退 mock
        return None


# 进程级单例
registry = GitProviderRegistry()

# ---------------------------------------------------------------------------
# 数据库视图与租户隔离辅助
# ---------------------------------------------------------------------------


def repo_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "name": row["name"],
        "full_name": row["full_name"],
        "url": row["url"],
        "default_branch": row["default_branch"],
        "provider": row["provider"],
        "provider_repo_id": row.get("provider_repo_id"),
        "namespace": row.get("namespace"),
        "created_by": row["created_by"],
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def issue_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "repo_id": row["repo_id"],
        "workspace_id": row["workspace_id"],
        "title": row["title"],
        "body": row["body"],
        "labels": row.get("labels") or [],
        "assignees": row.get("assignees") or [],
        "provider_issue_id": row.get("provider_issue_id"),
        "status": row["status"],
        "created_by": row["created_by"],
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def pr_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "repo_id": row["repo_id"],
        "workspace_id": row["workspace_id"],
        "title": row["title"],
        "body": row["body"],
        "head": row["head"],
        "base": row["base"],
        "draft": row.get("draft", False),
        "provider_pr_id": row.get("provider_pr_id"),
        "status": row["status"],
        "created_by": row["created_by"],
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def event_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "repo_id": row.get("repo_id"),
        "event_type": row["event_type"],
        "actor": row.get("actor"),
        "payload": row.get("payload") or {},
        "created_at": row.get("created_at"),
    }


def _require(actor: Actor, action: str) -> None:
    if not capability_allows(actor.capabilities, f"code:{action}"):
        raise HTTPException(status_code=403, detail=f"Missing capability: code:{action}")


async def _owned_repo(conn, repo_id: str, actor: Actor) -> dict[str, Any]:
    """获取属于当前工作区的仓库行，否则 404。"""
    result = await conn.execute(
        """
        SELECT id, workspace_id, name, full_name, url, default_branch,
               provider, provider_repo_id, namespace, credential_enc,
               metadata, created_by, created_at, updated_at
        FROM code_repo
        WHERE id = %s AND workspace_id = %s
        """,
        (repo_id, actor.workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Git repository not found in workspace")
    return row

async def _resolve_provider(
    conn, actor: Actor, repo_row: dict[str, Any] | None = None
) -> tuple[GitProvider, str | None]:
    """根据工作区/仓库配置解析 provider 实例。

    返回 (provider, namespace)。如果仓库未配置凭据，回退到 mock。
    """
    workspace_id = actor.workspace_id
    provider_name = (repo_row or {}).get("provider") if repo_row else GitProviderType.MOCK
    credential_enc = (repo_row or {}).get("credential_enc") if repo_row else None
    namespace = (repo_row or {}).get("namespace") if repo_row else None

    if not provider_name:
        provider_name = GitProviderType.MOCK
    if provider_name not in SUPPORTED_PROVIDERS:
        provider_name = GitProviderType.MOCK

    credential = decrypt_secret(credential_enc) if credential_enc else None
    provider = registry.get(
        workspace_id,
        provider_name,
        credential=credential,
        namespace=namespace,
    )
    return provider, namespace


async def _record_event(
    conn,
    workspace_id: str,
    repo_id: str | None,
    event_type: str,
    actor: str | None,
    payload: dict[str, Any],
) -> None:
    """将 Git provider 事件写入 code_repo_event。"""
    await conn.execute(
        """
        INSERT INTO code_repo_event(id, workspace_id, repo_id, event_type, actor, payload)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            new_id("crevt"),
            workspace_id,
            repo_id,
            event_type,
            actor,
            json.dumps(payload, ensure_ascii=False),
        ),
    )

# ---------------------------------------------------------------------------
# API 端点
# ---------------------------------------------------------------------------


@router.post("/repos", status_code=status.HTTP_201_CREATED)
async def create_repo_endpoint(
    body: GitRepoCreate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """创建（或注册）Git 仓库。"""
    _require(actor, "write")
    if body.provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported provider: {body.provider}",
        )
    provider = registry.get(
        actor.workspace_id,
        body.provider,
        credential=body.credential,
        namespace=body.namespace,
    )
    repo = await provider.create_repo(actor.workspace_id, body)
    credential_enc = encrypt_secret(body.credential) if body.credential else None
    repo_id = new_id("cgrep")
    async with pool.connection() as conn:
        try:
            await conn.execute(
                """
                INSERT INTO code_repo(
                    id, workspace_id, name, full_name, url, default_branch,
                    provider, provider_repo_id, namespace, credential_enc, metadata, created_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    repo_id,
                    actor.workspace_id,
                    repo.name,
                    repo.full_name,
                    repo.url,
                    repo.default_branch,
                    repo.provider,
                    repo.provider_repo_id,
                    body.namespace,
                    credential_enc,
                    json.dumps(
                        {"description": body.description, "private": body.private},
                        ensure_ascii=False,
                    ),
                    actor.user_id,
                ),
            )
            await _record_event(
                conn,
                actor.workspace_id,
                repo_id,
                "repo.create",
                actor.user_id,
                {"name": repo.name, "provider": repo.provider, "url": repo.url},
            )
            await conn.commit()
            row = await _owned_repo(conn, repo_id, actor)
        except Exception:
            await conn.rollback()
            raise
    return repo_view(row)


@router.get("/repos")
async def list_repos_endpoint(
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=100, ge=1, le=200),
):
    """列出当前工作区的 Git 仓库。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, workspace_id, name, full_name, url, default_branch,
                   provider, provider_repo_id, namespace, created_by, created_at, updated_at
            FROM code_repo
            WHERE workspace_id = %s
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (actor.workspace_id, limit),
        )
        rows = await result.fetchall()
    return {"items": [repo_view(row) for row in rows]}


@router.get("/repos/{repo_id}")
async def get_repo_endpoint(
    repo_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """获取单个 Git 仓库。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        row = await _owned_repo(conn, repo_id, actor)
    return repo_view(row)

@router.post("/repos/{repo_id}/issues", status_code=status.HTTP_201_CREATED)
async def create_issue_endpoint(
    repo_id: str,
    body: GitIssueCreate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """在仓库下创建 Issue。"""
    _require(actor, "write")
    if body.repo_id != repo_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="repo_id in body must match path",
        )
    async with pool.connection() as conn:
        repo_row = await _owned_repo(conn, repo_id, actor)
        provider, _ = await _resolve_provider(conn, actor, repo_row)
        issue = await provider.create_issue(actor.workspace_id, body)
        issue_id = new_id("ciss")
        try:
            await conn.execute(
                """
                INSERT INTO code_issue(
                    id, repo_id, workspace_id, title, body, labels, assignees,
                    provider_issue_id, status, created_by
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                """,
                (
                    issue_id,
                    repo_id,
                    actor.workspace_id,
                    issue.get("title", body.title),
                    issue.get("body", body.body),
                    json.dumps(
                        issue.get("labels", list(body.labels)), ensure_ascii=False
                    ),
                    json.dumps(
                        issue.get("assignees", list(body.assignees)), ensure_ascii=False
                    ),
                    issue.get("provider_issue_id"),
                    issue.get("status", "open"),
                    actor.user_id,
                ),
            )
            await _record_event(
                conn,
                actor.workspace_id,
                repo_id,
                "issue.open",
                actor.user_id,
                {"issue_id": issue_id, "title": issue.get("title", body.title)},
            )
            await conn.commit()
            result = await conn.execute(
                """
                SELECT id, repo_id, workspace_id, title, body, labels, assignees,
                       provider_issue_id, status, created_by, created_at, updated_at
                FROM code_issue
                WHERE id = %s AND workspace_id = %s
                """,
                (issue_id, actor.workspace_id),
            )
            row = await result.fetchone()
        except Exception:
            await conn.rollback()
            raise
    return issue_view(row)


@router.get("/repos/{repo_id}/issues")
async def list_issues_endpoint(
    repo_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=100, ge=1, le=200),
):
    """列出仓库下的 Issue。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        await _owned_repo(conn, repo_id, actor)
        result = await conn.execute(
            """
            SELECT id, repo_id, workspace_id, title, body, labels, assignees,
                   provider_issue_id, status, created_by, created_at, updated_at
            FROM code_issue
            WHERE repo_id = %s AND workspace_id = %s
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (repo_id, actor.workspace_id, limit),
        )
        rows = await result.fetchall()
    return {"items": [issue_view(row) for row in rows]}

@router.post("/repos/{repo_id}/prs", status_code=status.HTTP_201_CREATED)
async def create_pr_endpoint(
    repo_id: str,
    body: GitPRCreate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """创建 Pull Request。"""
    _require(actor, "write")
    if body.repo_id != repo_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="repo_id in body must match path",
        )
    async with pool.connection() as conn:
        repo_row = await _owned_repo(conn, repo_id, actor)
        provider, _ = await _resolve_provider(conn, actor, repo_row)
        pr = await provider.create_pr(actor.workspace_id, body)
        pr_id = new_id("cpr")
        try:
            await conn.execute(
                """
                INSERT INTO code_pr(
                    id, repo_id, workspace_id, title, body, head, base, draft,
                    provider_pr_id, status, created_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    pr_id,
                    repo_id,
                    actor.workspace_id,
                    pr.get("title", body.title),
                    pr.get("body", body.body),
                    pr.get("head", body.head),
                    pr.get("base", body.base),
                    bool(pr.get("draft", body.draft)),
                    pr.get("provider_pr_id"),
                    pr.get("status", "open"),
                    actor.user_id,
                ),
            )
            await _record_event(
                conn,
                actor.workspace_id,
                repo_id,
                "pull_request.open",
                actor.user_id,
                {
                    "pr_id": pr_id,
                    "title": pr.get("title", body.title),
                    "head": pr.get("head", body.head),
                },
            )
            await conn.commit()
            result = await conn.execute(
                """
                SELECT id, repo_id, workspace_id, title, body, head, base, draft,
                       provider_pr_id, status, created_by, created_at, updated_at
                FROM code_pr
                WHERE id = %s AND workspace_id = %s
                """,
                (pr_id, actor.workspace_id),
            )
            row = await result.fetchone()
        except Exception:
            await conn.rollback()
            raise
    return pr_view(row)


@router.get("/repos/{repo_id}/prs")
async def list_prs_endpoint(
    repo_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=100, ge=1, le=200),
):
    """列出仓库下的 PR。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        await _owned_repo(conn, repo_id, actor)
        result = await conn.execute(
            """
            SELECT id, repo_id, workspace_id, title, body, head, base, draft,
                   provider_pr_id, status, created_by, created_at, updated_at
            FROM code_pr
            WHERE repo_id = %s AND workspace_id = %s
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (repo_id, actor.workspace_id, limit),
        )
        rows = await result.fetchall()
    return {"items": [pr_view(row) for row in rows]}

@router.post("/repos/{repo_id}/branches", status_code=status.HTTP_201_CREATED)
async def create_branch_endpoint(
    repo_id: str,
    body: GitBranchCreate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """在仓库下创建分支。"""
    _require(actor, "write")
    async with pool.connection() as conn:
        repo_row = await _owned_repo(conn, repo_id, actor)
        provider, _ = await _resolve_provider(conn, actor, repo_row)
        result_payload = await provider.create_branch(
            actor.workspace_id, repo_id, body.branch, body.from_ref
        )
        await _record_event(
            conn,
            actor.workspace_id,
            repo_id,
            "branch.create",
            actor.user_id,
            {"branch": body.branch, "from_ref": body.from_ref},
        )
        await conn.commit()
    return result_payload


@router.get("/repos/{repo_id}/events")
async def list_events_endpoint(
    repo_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=100, ge=1, le=500),
):
    """列出仓库事件流。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        await _owned_repo(conn, repo_id, actor)
        result = await conn.execute(
            """
            SELECT id, workspace_id, repo_id, event_type, actor, payload, created_at
            FROM code_repo_event
            WHERE repo_id = %s AND workspace_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (repo_id, actor.workspace_id, limit),
        )
        rows = await result.fetchall()
    return {"items": [event_view(row) for row in rows]}


@router.get("/events")
async def list_workspace_events_endpoint(
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=100, ge=1, le=500),
):
    """列出工作区级别的事件流。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, workspace_id, repo_id, event_type, actor, payload, created_at
            FROM code_repo_event
            WHERE workspace_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (actor.workspace_id, limit),
        )
        rows = await result.fetchall()
    return {"items": [event_view(row) for row in rows]}