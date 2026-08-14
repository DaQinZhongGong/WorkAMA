-- v7.179: P3 IM 通道增强——离线消息存储 + 群组管理 + 消息撤回/编辑审计
-- 与 messaging.py 模块 SCHEMA_STATEMENTS 中的语句保持幂等一致。
-- 注意：本文件由 postgres 容器在初始化时自动执行；应用层 ensure_messaging_schema
-- 也会在启动时幂等执行同名 CREATE TABLE IF NOT EXISTS / ALTER TABLE ADD COLUMN IF NOT EXISTS。

-- ============================================================================
-- 1. 离线消息存储：用户离线时收到的消息持久化，上线后通过 API 拉取
-- ============================================================================
CREATE TABLE IF NOT EXISTS im_offline_message (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_im_offline_msg_recipient_created
    ON im_offline_message(recipient_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_im_offline_msg_conv_recipient
    ON im_offline_message(conversation_id, recipient_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_im_offline_msg_workspace
    ON im_offline_message(workspace_id, created_at DESC);

-- ============================================================================
-- 2. 群组（独立于 im_conversation 的 group 类型，提供更丰富的群治理能力）
-- ============================================================================
CREATE TABLE IF NOT EXISTS im_group (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    announcement TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_im_group_workspace
    ON im_group(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_im_group_owner
    ON im_group(owner_id);

-- ============================================================================
-- 3. 群成员：role ∈ (owner, admin, member)，UNIQUE(group_id, user_id)
-- ============================================================================
CREATE TABLE IF NOT EXISTS im_group_member (
    group_id TEXT NOT NULL REFERENCES im_group(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'admin', 'member')),
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_im_group_member_user
    ON im_group_member(user_id);

-- ============================================================================
-- 4. 消息撤回/编辑审计日志：action ∈ (retract, edit)
-- ============================================================================
CREATE TABLE IF NOT EXISTS im_message_edit_log (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    edited_by TEXT NOT NULL,
    old_payload JSONB,
    new_payload JSONB,
    action TEXT NOT NULL CHECK (action IN ('retract', 'edit')),
    edited_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_im_msg_edit_log_message
    ON im_message_edit_log(message_id, edited_at DESC);
CREATE INDEX IF NOT EXISTS idx_im_msg_edit_log_workspace
    ON im_message_edit_log(workspace_id, edited_at DESC);
