-- 056_workspace_v2.sql
-- v7.150: 工作区深度完善（多租户隔离 / 成员管理 / 权限矩阵）
-- 4 张新表：workspace_v2 / workspace_member / workspace_invite / workspace_role_permission
-- 与既有 id_workspace / id_member / id_invitation 独立共存，互不污染。

CREATE TABLE IF NOT EXISTS workspace_v2 (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    description TEXT,
    plan TEXT NOT NULL DEFAULT 'free',
    status TEXT NOT NULL DEFAULT 'active',
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(org_id, slug)
);

CREATE TABLE IF NOT EXISTS workspace_member (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',  -- owner/admin/member/viewer/guest
    status TEXT NOT NULL DEFAULT 'active',  -- active/invited/suspended
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE(workspace_id, user_id)
);

CREATE TABLE IF NOT EXISTS workspace_invite (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    email TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    token TEXT NOT NULL UNIQUE,
    invited_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending/accepted/revoked/expired
    expires_at TIMESTAMPTZ NOT NULL,
    accepted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workspace_role_permission (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    role TEXT NOT NULL,
    permission TEXT NOT NULL,  -- workspace.read/workspace.write/workspace.delete
                               -- /member.invite/member.remove
                               -- /billing.read/billing.write
                               -- /settings.read/settings.write
    granted BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, role, permission)
);

CREATE INDEX IF NOT EXISTS ws_v2_org_idx ON workspace_v2(org_id);
CREATE INDEX IF NOT EXISTS ws_member_workspace_idx ON workspace_member(workspace_id);
CREATE INDEX IF NOT EXISTS ws_member_user_idx ON workspace_member(user_id);
CREATE INDEX IF NOT EXISTS ws_invite_workspace_idx ON workspace_invite(workspace_id);
CREATE INDEX IF NOT EXISTS ws_invite_token_idx ON workspace_invite(token);
