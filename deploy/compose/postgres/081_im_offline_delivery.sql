-- v7.180: P3 IM 通道增强（第二阶段）
--   1) 离线消息队列真正持久化：为 im_offline_message 补齐 message_id / acked_at，
--      并建立 (message_id, recipient_id) 唯一索引，使入队天然幂等。
--   2) 每成员投递游标 im_delivery_cursor：替代 im_conv_message.delivered_at 这一
--      "全局单标志"（群聊场景下投递给任意一人即被标记为已投递，其余成员会漏收）。
--   3) 群主转让 / 成员角色变更的审计表。
--   4) im_conv_message 增加 retracted_at / edited_at，使撤回与编辑状态可结构化查询，
--      不再只依赖 content = '__retracted__' 这一哨兵值。
--
-- 与 messaging.py 模块 SCHEMA_STATEMENTS 保持幂等一致；全部使用
-- CREATE TABLE / CREATE INDEX / ADD COLUMN 的 IF NOT EXISTS 形式，可重复执行。

-- ============================================================================
-- 1. 离线消息队列：补齐来源消息外键列与 ack 时间，保证入队幂等
-- ============================================================================
ALTER TABLE im_offline_message ADD COLUMN IF NOT EXISTS message_id TEXT;
ALTER TABLE im_offline_message ADD COLUMN IF NOT EXISTS acked_at TIMESTAMPTZ;

-- 同一条源消息对同一收件人最多入队一次；message_id 为 NULL 的历史行不受约束
CREATE UNIQUE INDEX IF NOT EXISTS uq_im_offline_msg_message_recipient
    ON im_offline_message(message_id, recipient_id)
    WHERE message_id IS NOT NULL;

-- 未投递队列扫描（WS 重连时的 backfill 查询）
CREATE INDEX IF NOT EXISTS idx_im_offline_msg_pending
    ON im_offline_message(recipient_id, created_at ASC)
    WHERE delivered_at IS NULL;

-- ============================================================================
-- 2. 每成员投递游标：记录某会话中该成员已确认收到的最后一条消息
-- ============================================================================
CREATE TABLE IF NOT EXISTS im_delivery_cursor (
    workspace_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    last_delivered_message_id TEXT,
    last_delivered_at TIMESTAMPTZ,
    last_acked_message_id TEXT,
    last_acked_at TIMESTAMPTZ,
    pending_count INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (conversation_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_im_delivery_cursor_user
    ON im_delivery_cursor(workspace_id, user_id);

-- ============================================================================
-- 3. 群主转让审计
-- ============================================================================
CREATE TABLE IF NOT EXISTS im_group_ownership_transfer (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    from_user_id TEXT NOT NULL,
    to_user_id TEXT NOT NULL,
    performed_by TEXT NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_im_group_transfer_group
    ON im_group_ownership_transfer(group_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_im_group_transfer_workspace
    ON im_group_ownership_transfer(workspace_id, created_at DESC);

-- ============================================================================
-- 4. 群成员角色变更审计
-- ============================================================================
CREATE TABLE IF NOT EXISTS im_group_role_change (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    target_user_id TEXT NOT NULL,
    old_role TEXT NOT NULL,
    new_role TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_im_group_role_change_group
    ON im_group_role_change(group_id, changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_im_group_role_change_workspace
    ON im_group_role_change(workspace_id, changed_at DESC);

-- ============================================================================
-- 5. 消息撤回 / 编辑的结构化状态列
-- ============================================================================
ALTER TABLE im_conv_message ADD COLUMN IF NOT EXISTS retracted_at TIMESTAMPTZ;
ALTER TABLE im_conv_message ADD COLUMN IF NOT EXISTS edited_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_im_conv_message_retracted
    ON im_conv_message(conversation_id, retracted_at)
    WHERE retracted_at IS NOT NULL;
