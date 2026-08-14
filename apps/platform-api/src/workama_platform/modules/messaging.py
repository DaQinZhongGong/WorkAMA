"""M7 IM 通道基础模块 (messaging)。

P3 第 11 月交付项：内部用户间 IM 会话基础（REST）+ WebSocket 实时推送。

提供：
- 会话（direct/group）创建 / 列表 / 退出
- 会话消息发送 / 列表
- 已读标记
- workspace 隔离 + 成员鉴权（非成员 403）
- WebSocket 实时推送（/ws 端点：消息/typing 广播、在线状态）
- 离线消息存储与拉取（im_offline_message）
- 群组管理增强（im_group / im_group_member，role ∈ owner/admin/member）
- 消息撤回与编辑（5 分钟时间窗口，写审计到 im_message_edit_log）

表：im_conversation / im_conversation_member / im_conv_message
     im_offline_message / im_group / im_group_member / im_message_edit_log

注意：``im_conv_message`` 表名刻意区别于 ``channel_extensions`` 模块的 ``im_message``
（后者用于 Slack/WhatsApp 等外部渠道消息日志，字段为 channel_id/external_message_id/
direction 等）。本模块的 ``im_conv_message`` 用于内部用户间会话消息
（conversation_id/sender_id/content），两者领域不同、schema 不同，故独立命名避免
CREATE TABLE IF NOT EXISTS 跳过建表导致的 schema 不匹配。消息 ID 前缀用 ``icm``。
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, Field

from workama_platform.core import (
    Actor,
    decode_token_cached,
    get_actor,
    new_id,
    pool,
)


router = APIRouter(prefix="/api/v1/messaging", tags=["messaging"])


# ============================================================================
# 表定义（幂等建表）
# ============================================================================

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS im_conversation (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      type TEXT NOT NULL CHECK (type IN ('direct','group')),
      title TEXT,
      created_by TEXT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_im_conv_workspace ON im_conversation(workspace_id)",
    """
    CREATE TABLE IF NOT EXISTS im_conversation_member (
      conversation_id TEXT NOT NULL REFERENCES im_conversation(id) ON DELETE CASCADE,
      user_id TEXT NOT NULL,
      joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      last_read_at TIMESTAMPTZ,
      PRIMARY KEY (conversation_id, user_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_im_conv_member_user ON im_conversation_member(user_id)",
    """
    CREATE TABLE IF NOT EXISTS im_conv_message (
      id TEXT PRIMARY KEY,
      conversation_id TEXT NOT NULL REFERENCES im_conversation(id) ON DELETE CASCADE,
      sender_id TEXT NOT NULL,
      content TEXT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_im_conv_message_conv_created ON im_conv_message(conversation_id, created_at DESC)",
    # 离线消息队列扩展：unread_count 跟踪每个成员的未读消息数；
    # delivered_at 标记消息是否已通过 WS 投递给目标用户（NULL=未投递）。
    # ALTER TABLE ADD COLUMN IF NOT EXISTS 保证幂等，已存在列时跳过。
    "ALTER TABLE im_conversation_member ADD COLUMN IF NOT EXISTS unread_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE im_conv_message ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ",
    # 部分索引：仅索引未投递消息，加速 WS 连接时的 backfill 查询
    "CREATE INDEX IF NOT EXISTS idx_im_conv_message_undelivered ON im_conv_message(delivered_at) WHERE delivered_at IS NULL",
    # 部分索引：仅索引有未读消息的成员，加速未读计数查询
    "CREATE INDEX IF NOT EXISTS idx_im_conv_member_unread ON im_conversation_member(user_id, unread_count) WHERE unread_count > 0",
    # ====================================================================
    # P3 v7.179: 离线消息 + 群组管理 + 消息撤回/编辑审计
    # 与 deploy/compose/postgres/080_im_offline_group.sql 保持幂等一致
    # ====================================================================
    """
    CREATE TABLE IF NOT EXISTS im_offline_message (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      conversation_id TEXT NOT NULL,
      sender_id TEXT NOT NULL,
      recipient_id TEXT NOT NULL,
      payload JSONB NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      delivered_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_im_offline_msg_recipient_created ON im_offline_message(recipient_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_im_offline_msg_conv_recipient ON im_offline_message(conversation_id, recipient_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_im_offline_msg_workspace ON im_offline_message(workspace_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS im_group (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      name TEXT NOT NULL,
      owner_id TEXT NOT NULL,
      announcement TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_im_group_workspace ON im_group(workspace_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_im_group_owner ON im_group(owner_id)",
    """
    CREATE TABLE IF NOT EXISTS im_group_member (
      group_id TEXT NOT NULL REFERENCES im_group(id) ON DELETE CASCADE,
      user_id TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'admin', 'member')),
      joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      PRIMARY KEY (group_id, user_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_im_group_member_user ON im_group_member(user_id)",
    """
    CREATE TABLE IF NOT EXISTS im_message_edit_log (
      id TEXT PRIMARY KEY,
      message_id TEXT NOT NULL,
      workspace_id TEXT NOT NULL,
      edited_by TEXT NOT NULL,
      old_payload JSONB,
      new_payload JSONB,
      action TEXT NOT NULL CHECK (action IN ('retract', 'edit')),
      edited_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_im_msg_edit_log_message ON im_message_edit_log(message_id, edited_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_im_msg_edit_log_workspace ON im_message_edit_log(workspace_id, edited_at DESC)",
    # ====================================================================
    # P3 v7.180: 离线队列持久化 + 每成员投递游标 + 群治理审计
    # 与 deploy/compose/postgres/081_im_offline_delivery.sql 保持幂等一致
    # ====================================================================
    "ALTER TABLE im_offline_message ADD COLUMN IF NOT EXISTS message_id TEXT",
    "ALTER TABLE im_offline_message ADD COLUMN IF NOT EXISTS acked_at TIMESTAMPTZ",
    # 同一条源消息对同一收件人最多入队一次 → ON CONFLICT DO NOTHING 幂等入队
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_im_offline_msg_message_recipient "
    "ON im_offline_message(message_id, recipient_id) WHERE message_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_im_offline_msg_pending "
    "ON im_offline_message(recipient_id, created_at ASC) WHERE delivered_at IS NULL",
    """
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
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_im_delivery_cursor_user ON im_delivery_cursor(workspace_id, user_id)",
    """
    CREATE TABLE IF NOT EXISTS im_group_ownership_transfer (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      group_id TEXT NOT NULL,
      from_user_id TEXT NOT NULL,
      to_user_id TEXT NOT NULL,
      performed_by TEXT NOT NULL,
      reason TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_im_group_transfer_group ON im_group_ownership_transfer(group_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_im_group_transfer_workspace ON im_group_ownership_transfer(workspace_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS im_group_role_change (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      group_id TEXT NOT NULL,
      target_user_id TEXT NOT NULL,
      old_role TEXT NOT NULL,
      new_role TEXT NOT NULL,
      changed_by TEXT NOT NULL,
      changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_im_group_role_change_group ON im_group_role_change(group_id, changed_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_im_group_role_change_workspace ON im_group_role_change(workspace_id, changed_at DESC)",
    "ALTER TABLE im_conv_message ADD COLUMN IF NOT EXISTS retracted_at TIMESTAMPTZ",
    "ALTER TABLE im_conv_message ADD COLUMN IF NOT EXISTS edited_at TIMESTAMPTZ",
    "CREATE INDEX IF NOT EXISTS idx_im_conv_message_retracted "
    "ON im_conv_message(conversation_id, retracted_at) WHERE retracted_at IS NOT NULL",
)

# 撤回后消息 content 被替换为该哨兵值（保留以兼容既有客户端与历史数据）；
# 结构化状态请优先读 im_conv_message.retracted_at。
RETRACTED_CONTENT = "__retracted__"


async def ensure_messaging_schema(conn: Any) -> None:
    """应用 IM 通道基础模块的幂等建表语句。"""
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


# ============================================================================
# Pydantic 模型
# ============================================================================

ConversationType = Literal["direct", "group"]


class ConversationCreateRequest(BaseModel):
    """POST /api/v1/messaging/conversations 请求体。

    ``member_user_ids`` 为除创建者外的其他成员；创建者自动加入。
    """

    type: ConversationType
    title: str | None = Field(default=None, max_length=200)
    member_user_ids: list[str] = Field(min_length=1, max_length=100)


class MessageCreateRequest(BaseModel):
    """POST /api/v1/messaging/conversations/{id}/messages 请求体。"""

    content: str = Field(min_length=1, max_length=8000)


class AddMemberRequest(BaseModel):
    """POST /api/v1/messaging/conversations/{id}/members 请求体。"""

    user_id: str = Field(min_length=1, max_length=200)


class UpdateConversationRequest(BaseModel):
    """PATCH /api/v1/messaging/conversations/{id} 请求体。"""

    title: str = Field(min_length=1, max_length=200)


# ============================================================================
# 辅助函数
# ============================================================================


def _conversation_summary(row: dict[str, Any]) -> dict[str, Any]:
    """将会话行转为 API 响应 dict。"""
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "type": row["type"],
        "title": row.get("title"),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


def _message_summary(row: dict[str, Any]) -> dict[str, Any]:
    """将消息行转为 API 响应 dict。

    ``retracted_at`` / ``edited_at`` 为 v7.180 新增的结构化状态列，使用 ``.get``
    读取以兼容尚未执行迁移的历史行与既有单测中的精简行字典。
    """
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "sender_id": row["sender_id"],
        "content": row["content"],
        "created_at": row["created_at"],
        "retracted_at": row.get("retracted_at"),
        "edited_at": row.get("edited_at"),
    }


def _isoformat(value: Any) -> Any:
    """datetime → ISO 字符串；其他类型原样返回（用于 WS JSON 序列化）。"""
    return value.isoformat() if hasattr(value, "isoformat") else value


async def _enqueue_offline_messages(
    conn: Any,
    *,
    workspace_id: str,
    conversation_id: str,
    sender_id: str,
    message_id: str,
    content: str,
    created_at: Any,
    recipient_ids: list[str],
) -> None:
    """为离线成员将消息写入持久化离线队列 ``im_offline_message``。

    - 幂等：唯一索引 ``(message_id, recipient_id)`` + ``ON CONFLICT DO NOTHING``，
      重复调用（例如重试）不会产生重复行。
    - 同时把 ``im_delivery_cursor.pending_count`` +1，作为每成员待投递计数。
    """
    if not recipient_ids:
        return
    payload = json.dumps(
        {
            "type": "message",
            "id": message_id,
            "conversation_id": conversation_id,
            "sender_id": sender_id,
            "content": content,
            "created_at": _isoformat(created_at),
        }
    )
    for recipient_id in recipient_ids:
        await conn.execute(
            """
            INSERT INTO im_offline_message(
                id, workspace_id, conversation_id, sender_id,
                recipient_id, payload, message_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                new_id("imo"),
                workspace_id,
                conversation_id,
                sender_id,
                recipient_id,
                payload,
                message_id,
            ),
        )
        await conn.execute(
            """
            INSERT INTO im_delivery_cursor(
                workspace_id, conversation_id, user_id, pending_count
            ) VALUES (%s, %s, %s, 1)
            ON CONFLICT (conversation_id, user_id) DO UPDATE
              SET pending_count = im_delivery_cursor.pending_count + 1,
                  updated_at = now()
            """,
            (workspace_id, conversation_id, recipient_id),
        )


async def _broadcast_message_mutation(
    conn: Any,
    *,
    conversation_id: str,
    action: str,
    message_id: str,
    content: str | None,
    actor_user_id: str,
) -> None:
    """撤回/编辑后向会话在线成员广播状态变更，并同步未投递的离线副本。

    离线副本同步很关键：如果撤回时目标成员仍离线，队列里那条 payload 必须一起
    更新，否则该成员上线后仍会收到已撤回的原文。
    """
    member_result = await conn.execute(
        "SELECT user_id FROM im_conversation_member WHERE conversation_id = %s",
        (conversation_id,),
    )
    member_rows = await member_result.fetchall()
    member_ids = [r["user_id"] for r in member_rows]
    # 同步未投递的离线副本（仅改写尚未投递的行，已投递的由客户端按广播事件处理）
    await conn.execute(
        """
        UPDATE im_offline_message
        SET payload = jsonb_set(
              jsonb_set(payload, '{content}', to_jsonb(%s::text), true),
              '{mutation}', to_jsonb(%s::text), true
            )
        WHERE message_id = %s AND delivered_at IS NULL
        """,
        (content if content is not None else RETRACTED_CONTENT, action, message_id),
    )
    await manager.broadcast_to_conversation(
        conversation_id,
        member_ids,
        {
            "type": action,
            "id": message_id,
            "conversation_id": conversation_id,
            "content": content,
            "actor_id": actor_user_id,
            "at": datetime.now(UTC).isoformat(),
        },
    )


async def _get_owned_conversation(
    conn: Any, conversation_id: str, actor: Actor
) -> dict[str, Any]:
    """查询会话并校验 workspace 归属。

    - 不存在或 workspace 不匹配 → 404（不泄露跨 workspace 存在性）
    """
    result = await conn.execute(
        "SELECT * FROM im_conversation WHERE id = %s",
        (conversation_id,),
    )
    row = await result.fetchone()
    if not row or row["workspace_id"] != actor.workspace_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return row


async def _assert_member(
    conn: Any, conversation_id: str, actor: Actor
) -> None:
    """校验调用者是会话成员，否则 403。

    调用前应已通过 ``_get_owned_conversation`` 校验 workspace 归属。
    """
    result = await conn.execute(
        "SELECT 1 FROM im_conversation_member WHERE conversation_id = %s AND user_id = %s",
        (conversation_id, actor.user_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(
            status_code=403, detail="User is not a member of the conversation"
        )


# ============================================================================
# Router 端点
# ============================================================================
#
# 路由顺序：固定路径（无）均在参数化路径之前；当前所有端点均为参数化路径
# /conversations/{conversation_id}/...，无遮蔽风险。


# 1. 创建会话
@router.post("/conversations", status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: ConversationCreateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """创建会话（direct/group）。创建者自动加入；direct 类型要求总成员数为 2。"""
    # 去重 member_user_ids，并排除创建者自身（创建者自动加入，避免重复）
    unique_members: list[str] = []
    seen: set[str] = {actor.user_id}
    for member_id in body.member_user_ids:
        if member_id not in seen:
            seen.add(member_id)
            unique_members.append(member_id)
    total_members = 1 + len(unique_members)  # 创建者 + 其他成员
    if body.type == "direct" and total_members != 2:
        raise HTTPException(
            status_code=422,
            detail="direct conversation requires exactly 2 members (creator + 1 other)",
        )
    if body.type == "group" and len(unique_members) < 1:
        raise HTTPException(
            status_code=422,
            detail="group conversation requires at least 1 other member",
        )

    conversation_id = new_id("imc")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                INSERT INTO im_conversation(id, workspace_id, type, title, created_by)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    conversation_id,
                    actor.workspace_id,
                    body.type,
                    body.title,
                    actor.user_id,
                ),
            )
            conv_row = await result.fetchone()
            # 创建者自动加入
            await conn.execute(
                "INSERT INTO im_conversation_member(conversation_id, user_id) VALUES (%s, %s)",
                (conversation_id, actor.user_id),
            )
            # 其他成员加入
            for member_id in unique_members:
                await conn.execute(
                    "INSERT INTO im_conversation_member(conversation_id, user_id) VALUES (%s, %s)",
                    (conversation_id, member_id),
                )
    return _conversation_summary(conv_row)


# 2. 列出当前用户参与的会话
@router.get("/conversations")
async def list_conversations(
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """列出当前用户参与的会话（分页，仅自己参与的）。"""
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT c.* FROM im_conversation c
            JOIN im_conversation_member m ON m.conversation_id = c.id
            WHERE m.user_id = %s AND c.workspace_id = %s
            ORDER BY c.created_at DESC
            LIMIT %s OFFSET %s
            """,
            (actor.user_id, actor.workspace_id, limit, offset),
        )
        rows = await result.fetchall()
    items = [_conversation_summary(row) for row in rows]
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


# 3. 列出会话消息
@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """列出会话消息（按 created_at ASC，分页）。非成员 403。"""
    async with pool.connection() as conn:
        await _get_owned_conversation(conn, conversation_id, actor)
        await _assert_member(conn, conversation_id, actor)
        result = await conn.execute(
            """
            SELECT * FROM im_conv_message
            WHERE conversation_id = %s
            ORDER BY created_at ASC
            LIMIT %s OFFSET %s
            """,
            (conversation_id, limit, offset),
        )
        rows = await result.fetchall()
    items = [_message_summary(row) for row in rows]
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


# 4. 发送消息
@router.post(
    "/conversations/{conversation_id}/messages",
    status_code=status.HTTP_201_CREATED,
)
async def send_message(
    conversation_id: str,
    body: MessageCreateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """发送消息。非成员 403。"""
    message_id = new_id("icm")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _get_owned_conversation(conn, conversation_id, actor)
            await _assert_member(conn, conversation_id, actor)
            result = await conn.execute(
                """
                INSERT INTO im_conv_message(id, conversation_id, sender_id, content)
                VALUES (%s, %s, %s, %s)
                RETURNING *
                """,
                (message_id, conversation_id, actor.user_id, body.content),
            )
            row = await result.fetchone()
            # 查询会话所有成员 ID，用于广播实时推送（best-effort，不影响响应）
            member_result = await conn.execute(
                "SELECT user_id FROM im_conversation_member WHERE conversation_id = %s",
                (conversation_id,),
            )
            member_rows = await member_result.fetchall()
            # 离线消息队列：对离线成员（非发送者）unread_count +1，
            # 在线成员通过下面的 broadcast 即时推送，不累加 unread_count。
            offline_member_ids = [
                r["user_id"]
                for r in member_rows
                if r["user_id"] != actor.user_id
                and not manager.is_online(r["user_id"])
            ]
            if offline_member_ids:
                await conn.execute(
                    "UPDATE im_conversation_member SET unread_count = unread_count + 1 "
                    "WHERE conversation_id = %s AND user_id = ANY(%s)",
                    (conversation_id, offline_member_ids),
                )
                # 持久化离线队列：离线成员重连后按 per-member 游标补投
                await _enqueue_offline_messages(
                    conn,
                    workspace_id=actor.workspace_id,
                    conversation_id=conversation_id,
                    sender_id=actor.user_id,
                    message_id=message_id,
                    content=body.content,
                    created_at=row["created_at"],
                    recipient_ids=offline_member_ids,
                )
    summary = _message_summary(row)
    # 广播给会话所有在线成员（仅追加广播，响应结构不变）
    member_ids = [r["user_id"] for r in member_rows]
    created_at = summary["created_at"]
    await manager.broadcast_to_conversation(
        conversation_id,
        member_ids,
        {
            "type": "message",
            "id": summary["id"],
            "conversation_id": conversation_id,
            "sender_id": summary["sender_id"],
            "content": summary["content"],
            "created_at": (
                created_at.isoformat()
                if hasattr(created_at, "isoformat")
                else created_at
            ),
        },
    )
    return summary


# 5. 标记已读
@router.post("/conversations/{conversation_id}/read")
async def mark_read(
    conversation_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """标记已读（更新 last_read_at + 清零 unread_count）。非成员 403。"""
    async with pool.connection() as conn:
        async with conn.transaction():
            await _get_owned_conversation(conn, conversation_id, actor)
            await _assert_member(conn, conversation_id, actor)
            result = await conn.execute(
                """
                UPDATE im_conversation_member
                SET last_read_at = now(), unread_count = 0
                WHERE conversation_id = %s AND user_id = %s
                RETURNING conversation_id, user_id, last_read_at
                """,
                (conversation_id, actor.user_id),
            )
            row = await result.fetchone()
    return {
        "conversation_id": row["conversation_id"],
        "user_id": row["user_id"],
        "last_read_at": row["last_read_at"],
    }


# 6. 退出会话
@router.delete("/conversations/{conversation_id}")
async def leave_conversation(
    conversation_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """退出会话：删除该成员行；若会话无剩余成员则删会话（CASCADE 清理消息）。"""
    async with pool.connection() as conn:
        async with conn.transaction():
            await _get_owned_conversation(conn, conversation_id, actor)
            await _assert_member(conn, conversation_id, actor)
            await conn.execute(
                "DELETE FROM im_conversation_member WHERE conversation_id = %s AND user_id = %s",
                (conversation_id, actor.user_id),
            )
            # 检查剩余成员；无成员则删会话（CASCADE 自动清理消息与残留成员）
            result = await conn.execute(
                "SELECT 1 FROM im_conversation_member WHERE conversation_id = %s LIMIT 1",
                (conversation_id,),
            )
            remaining = await result.fetchone()
            if not remaining:
                await conn.execute(
                    "DELETE FROM im_conversation WHERE id = %s",
                    (conversation_id,),
                )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ============================================================================
# WebSocket 实时推送
# ----------------------------------------------------------------------------
# WS 端点不经过 HTTP 中间件，无法使用 Depends(get_actor)；在处理函数内手动解析
# JWT（query 参数 token），复用 core.decode_token_cached 获取 user_id/workspace_id。
# 在线状态用模块级 dict 维护（内存版）；Redis 兜底为后续扩展点。
# ============================================================================


class ConnectionManager:
    """WebSocket 连接管理器（内存版）。

    维护 ``user_id → 该用户所有在线 ws 连接集合``。一个用户可能同时开多个
    终端/标签页，故 value 用 ``set[WebSocket]``。
    """

    def __init__(self) -> None:
        self.active_connections: dict[str, set[WebSocket]] = {}

    async def connect(self, user_id: str, websocket: WebSocket) -> None:
        """接受连接并加入该用户的连接集合。"""
        await websocket.accept()
        self.active_connections.setdefault(user_id, set()).add(websocket)

    async def disconnect(self, user_id: str, websocket: WebSocket) -> None:
        """移除连接；用户无剩余连接时清理 key（幂等，重复调用安全）。"""
        conns = self.active_connections.get(user_id)
        if conns is None:
            return
        conns.discard(websocket)
        if not conns:
            self.active_connections.pop(user_id, None)

    async def send_to_user(self, user_id: str, message: dict[str, Any]) -> None:
        """向指定用户所有在线连接发消息（best-effort：单连接失败不影响其他）。"""
        for ws in list(self.active_connections.get(user_id, set())):
            try:
                await ws.send_json(message)
            except Exception:
                # 连接已断开或发送失败，忽略；disconnect 会在握手收尾时清理
                pass

    async def broadcast_to_conversation(
        self,
        conversation_id: str,
        member_ids: list[str],
        message: dict[str, Any],
    ) -> None:
        """向会话所有在线成员广播消息（非在线成员自动跳过）。"""
        for member_id in member_ids:
            await self.send_to_user(member_id, message)

    def is_online(self, user_id: str) -> bool:
        """判断用户是否至少有一个在线连接。"""
        return bool(self.active_connections.get(user_id))


# 模块级单例：随 messaging_router 一起加载，WS 端点与 REST 广播共用同一实例
manager = ConnectionManager()


# WS 心跳超时：客户端应 30s 发一次 ping，服务端 60s 无消息则关闭连接
_WS_IDLE_TIMEOUT_SECONDS = 60.0
# WS 关闭码（4000-4999 为应用自定义区间，符合 RFC 6455）
_WS_CLOSE_UNAUTHORIZED = 4401
_WS_CLOSE_IDLE_TIMEOUT = 4408


async def _ws_assert_member_or_error(
    websocket: WebSocket,
    conn: Any,
    conversation_id: str,
    user_id: str,
    workspace_id: str,
) -> bool:
    """WS 内联成员校验：校验会话 workspace 归属 + 成员身份。

    校验失败时通过 ws 发送 error 消息并返回 False；成功返回 True。
    （WS 不走 HTTP，不能用 _assert_member 抛 HTTPException 的方式）
    """
    result = await conn.execute(
        "SELECT * FROM im_conversation WHERE id = %s",
        (conversation_id,),
    )
    conv_row = await result.fetchone()
    if not conv_row or conv_row["workspace_id"] != workspace_id:
        await websocket.send_json({"type": "error", "detail": "conversation not found"})
        return False
    member_result = await conn.execute(
        "SELECT 1 FROM im_conversation_member WHERE conversation_id = %s AND user_id = %s",
        (conversation_id, user_id),
    )
    if not await member_result.fetchone():
        await websocket.send_json(
            {"type": "error", "detail": "user is not a member of the conversation"}
        )
        return False
    return True


async def _handle_ws_message(
    websocket: WebSocket,
    user_id: str,
    workspace_id: str,
    data: dict[str, Any],
) -> None:
    """处理 WS message 类型：校验成员 → INSERT → 广播给会话所有在线成员。"""
    conversation_id = data.get("conversation_id")
    content = data.get("content")
    if not conversation_id or not isinstance(content, str) or not content:
        await websocket.send_json(
            {"type": "error", "detail": "invalid message payload"}
        )
        return
    if len(content) > 8000:
        await websocket.send_json({"type": "error", "detail": "content too long"})
        return
    message_id = new_id("icm")
    async with pool.connection() as conn:
        if not await _ws_assert_member_or_error(
            websocket, conn, conversation_id, user_id, workspace_id
        ):
            return
        async with conn.transaction():
            insert_result = await conn.execute(
                """
                INSERT INTO im_conv_message(id, conversation_id, sender_id, content)
                VALUES (%s, %s, %s, %s)
                RETURNING *
                """,
                (message_id, conversation_id, user_id, content),
            )
            msg_row = await insert_result.fetchone()
        # 查询会话所有成员 ID 用于广播
        member_result = await conn.execute(
            "SELECT user_id FROM im_conversation_member WHERE conversation_id = %s",
            (conversation_id,),
        )
        member_rows = await member_result.fetchall()
        # 离线消息队列：对离线成员（非发送者）unread_count +1
        offline_member_ids = [
            r["user_id"]
            for r in member_rows
            if r["user_id"] != user_id
            and not manager.is_online(r["user_id"])
        ]
        if offline_member_ids:
            await conn.execute(
                "UPDATE im_conversation_member SET unread_count = unread_count + 1 "
                "WHERE conversation_id = %s AND user_id = ANY(%s)",
                (conversation_id, offline_member_ids),
            )
            await _enqueue_offline_messages(
                conn,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                sender_id=user_id,
                message_id=message_id,
                content=content,
                created_at=msg_row["created_at"],
                recipient_ids=offline_member_ids,
            )
    member_ids = [r["user_id"] for r in member_rows]
    created_at = msg_row["created_at"]
    await manager.broadcast_to_conversation(
        conversation_id,
        member_ids,
        {
            "type": "message",
            "id": msg_row["id"],
            "conversation_id": conversation_id,
            "sender_id": user_id,
            "content": content,
            "created_at": (
                created_at.isoformat()
                if hasattr(created_at, "isoformat")
                else created_at
            ),
        },
    )


async def _handle_ws_typing(
    websocket: WebSocket,
    user_id: str,
    workspace_id: str,
    data: dict[str, Any],
) -> None:
    """处理 WS typing 事件：校验成员 → 广播 typing 给会话其他在线成员。"""
    conversation_id = data.get("conversation_id")
    if not conversation_id:
        await websocket.send_json({"type": "error", "detail": "invalid typing payload"})
        return
    async with pool.connection() as conn:
        if not await _ws_assert_member_or_error(
            websocket, conn, conversation_id, user_id, workspace_id
        ):
            return
        member_result = await conn.execute(
            "SELECT user_id FROM im_conversation_member WHERE conversation_id = %s AND user_id <> %s",
            (conversation_id, user_id),
        )
        member_rows = await member_result.fetchall()
    other_ids = [r["user_id"] for r in member_rows]
    await manager.broadcast_to_conversation(
        conversation_id,
        other_ids,
        {"type": "typing", "conversation_id": conversation_id, "sender_id": user_id},
    )


_OFFLINE_BACKFILL_LIMIT = 500


async def _deliver_undelivered_messages(
    websocket: WebSocket,
    user_id: str,
    workspace_id: str,
) -> int:
    """WS 连接时从持久化离线队列补投消息，并推进每成员投递游标。

    v7.180 起投递来源从 ``im_conv_message.delivered_at``（全局单标志，群聊下投递给
    任意一名成员后其余成员会漏收）改为 ``im_offline_message``（按 recipient_id 分行，
    真正的 per-member 队列）。

    - 只取本 workspace、``recipient_id = user_id``、``delivered_at IS NULL`` 的行；
    - 逐条推送后批量 ``UPDATE delivered_at = now()``，并按会话写入
      ``im_delivery_cursor.last_delivered_message_id``；
    - best-effort：发送失败即停止，未标记的行下次连接继续投递（至少一次语义，
      客户端可用 message id 去重）。

    返回本次实际投递的条数。
    """
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT * FROM im_offline_message
            WHERE recipient_id = %s AND workspace_id = %s AND delivered_at IS NULL
            ORDER BY created_at ASC
            LIMIT %s
            """,
            (user_id, workspace_id, _OFFLINE_BACKFILL_LIMIT),
        )
        rows = await result.fetchall()
    delivered_ids: list[str] = []
    # conversation_id → 该会话本次投递的最后一条源消息 ID（用于推进游标）
    last_message_by_conversation: dict[str, Any] = {}
    for row in rows:
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                payload = None
        if isinstance(payload, dict):
            envelope = dict(payload)
        else:
            # 兼容 payload 列引入前写入的历史行：用队列行自身的列重建信封。
            envelope = {
                "id": row.get("message_id") or row["id"],
                "conversation_id": row.get("conversation_id"),
                "sender_id": row.get("sender_id"),
                "content": row.get("content"),
                "created_at": _isoformat(row.get("created_at")),
            }
        envelope.setdefault("type", "message")
        envelope["offline_id"] = row["id"]
        envelope["backfilled"] = True
        try:
            await websocket.send_json(envelope)
        except Exception:
            # 连接已断开或发送失败，停止投递剩余消息
            break
        delivered_ids.append(row["id"])
        last_message_by_conversation[row["conversation_id"]] = row.get("message_id")
    if delivered_ids:
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE im_offline_message SET delivered_at = now() WHERE id = ANY(%s)",
                    (delivered_ids,),
                )
                for conv_id, msg_id in last_message_by_conversation.items():
                    await conn.execute(
                        """
                        INSERT INTO im_delivery_cursor(
                            workspace_id, conversation_id, user_id,
                            last_delivered_message_id, last_delivered_at
                        ) VALUES (%s, %s, %s, %s, now())
                        ON CONFLICT (conversation_id, user_id) DO UPDATE
                          SET last_delivered_message_id = EXCLUDED.last_delivered_message_id,
                              last_delivered_at = now(),
                              updated_at = now()
                        """,
                        (workspace_id, conv_id, user_id, msg_id),
                    )
    return len(delivered_ids)


@router.websocket("/ws")
async def messaging_ws_endpoint(
    websocket: WebSocket,
    token: str | None = Query(default=None),
) -> None:
    """WebSocket 实时推送端点。

    鉴权：从 query 参数 ``token`` 获取 JWT，复用 ``core.decode_token_cached``
    解析出 user_id(``sub``)/workspace_id(``ws``)。WS 不走 HTTP 中间件，
    无法使用 ``Depends(get_actor)``。

    客户端消息协议（JSON）：
    - ``{"type":"ping"}`` → ``{"type":"pong"}``
    - ``{"type":"message","conversation_id":"...","content":"..."}``
      → 校验成员 → INSERT im_conv_message → 广播给会话所有在线成员
    - ``{"type":"typing","conversation_id":"..."}``
      → 广播 typing 事件给会话其他在线成员

    心跳：客户端 30s 发一次 ping，服务端 ``_WS_IDLE_TIMEOUT_SECONDS``(60s)
    无消息则 ``close(code=4408)``。
    """
    if not token:
        await websocket.close(code=_WS_CLOSE_UNAUTHORIZED)
        return
    try:
        payload = await decode_token_cached(token)
    except Exception:
        # decode_token 失败会抛 HTTPException(401)；统一转 ws close(4401)
        await websocket.close(code=_WS_CLOSE_UNAUTHORIZED)
        return
    user_id = payload.get("sub")
    workspace_id = payload.get("ws")
    if not user_id or not workspace_id:
        await websocket.close(code=_WS_CLOSE_UNAUTHORIZED)
        return

    await manager.connect(user_id, websocket)
    try:
        await websocket.send_json({"type": "connected", "user_id": user_id})
        # 离线消息投递：连接建立后自动推送所有 delivered_at IS NULL 的消息
        try:
            await _deliver_undelivered_messages(websocket, user_id, workspace_id)
        except Exception:
            # 投递失败不阻塞连接，下次连接再投递
            pass
        while True:
            try:
                data = await asyncio.wait_for(
                    websocket.receive_json(), timeout=_WS_IDLE_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                await websocket.close(code=_WS_CLOSE_IDLE_TIMEOUT)
                break
            if not isinstance(data, dict):
                await websocket.send_json(
                    {"type": "error", "detail": "payload must be a JSON object"}
                )
                continue
            msg_type = data.get("type")
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
            elif msg_type == "message":
                await _handle_ws_message(websocket, user_id, workspace_id, data)
            elif msg_type == "typing":
                await _handle_ws_typing(websocket, user_id, workspace_id, data)
            else:
                await websocket.send_json(
                    {"type": "error", "detail": f"unknown message type: {msg_type}"}
                )
    except WebSocketDisconnect:
        # 客户端正常断开，无需额外处理
        pass
    finally:
        await manager.disconnect(user_id, websocket)


# 7. 在线状态查询
@router.get("/conversations/{conversation_id}/presence")
async def get_presence(
    conversation_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """返回会话成员的在线状态。非成员 403。

    响应：``{"members":[{"user_id":"...","online":true/false}]}``
    """
    async with pool.connection() as conn:
        await _get_owned_conversation(conn, conversation_id, actor)
        await _assert_member(conn, conversation_id, actor)
        result = await conn.execute(
            "SELECT user_id FROM im_conversation_member WHERE conversation_id = %s",
            (conversation_id,),
        )
        rows = await result.fetchall()
    members = [
        {"user_id": r["user_id"], "online": manager.is_online(r["user_id"])}
        for r in rows
    ]
    return {"members": members}


# ============================================================================
# 离线消息 + 群组管理增强端点
# ============================================================================


# 8. 获取未读消息数
@router.get("/conversations/{conversation_id}/unread")
async def get_unread_count(
    conversation_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """获取当前用户在该会话的未读消息数。非成员 403。"""
    async with pool.connection() as conn:
        await _get_owned_conversation(conn, conversation_id, actor)
        await _assert_member(conn, conversation_id, actor)
        result = await conn.execute(
            "SELECT unread_count FROM im_conversation_member "
            "WHERE conversation_id = %s AND user_id = %s",
            (conversation_id, actor.user_id),
        )
        row = await result.fetchone()
    return {
        "conversation_id": conversation_id,
        "unread_count": row["unread_count"] if row else 0,
    }


# 9. 添加成员到群组会话
@router.post(
    "/conversations/{conversation_id}/members",
    status_code=status.HTTP_201_CREATED,
)
async def add_member(
    conversation_id: str,
    body: AddMemberRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """添加成员到群组会话（仅 group 类型，仅创建者/admin）。"""
    async with pool.connection() as conn:
        async with conn.transaction():
            conv = await _get_owned_conversation(conn, conversation_id, actor)
            # 仅创建者或 admin/owner 可操作
            if conv["created_by"] != actor.user_id and actor.role not in ("admin", "owner"):
                raise HTTPException(
                    status_code=403,
                    detail="Only conversation creator or admin can add members",
                )
            # 仅 group 类型允许加人
            if conv["type"] != "group":
                raise HTTPException(
                    status_code=422,
                    detail="Cannot add members to a direct conversation",
                )
            # 检查 user_id 未已在成员中
            existing = await conn.execute(
                "SELECT 1 FROM im_conversation_member "
                "WHERE conversation_id = %s AND user_id = %s",
                (conversation_id, body.user_id),
            )
            if await existing.fetchone():
                raise HTTPException(
                    status_code=409,
                    detail="User is already a member of the conversation",
                )
            await conn.execute(
                "INSERT INTO im_conversation_member(conversation_id, user_id) "
                "VALUES (%s, %s)",
                (conversation_id, body.user_id),
            )
    return {"conversation_id": conversation_id, "user_id": body.user_id}


# 10. 移除群组成员
@router.delete("/conversations/{conversation_id}/members/{user_id}")
async def remove_member(
    conversation_id: str,
    user_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """移除群组成员（仅创建者/admin，不能移除自己）。"""
    if user_id == actor.user_id:
        raise HTTPException(
            status_code=422,
            detail="Cannot remove yourself; use DELETE /conversations/{id} to leave",
        )
    async with pool.connection() as conn:
        async with conn.transaction():
            conv = await _get_owned_conversation(conn, conversation_id, actor)
            if conv["created_by"] != actor.user_id and actor.role not in ("admin", "owner"):
                raise HTTPException(
                    status_code=403,
                    detail="Only conversation creator or admin can remove members",
                )
            await conn.execute(
                "DELETE FROM im_conversation_member "
                "WHERE conversation_id = %s AND user_id = %s",
                (conversation_id, user_id),
            )
            # 移除后若成员数=0 则删会话（CASCADE 清理消息与残留成员）
            result = await conn.execute(
                "SELECT 1 FROM im_conversation_member "
                "WHERE conversation_id = %s LIMIT 1",
                (conversation_id,),
            )
            if not await result.fetchone():
                await conn.execute(
                    "DELETE FROM im_conversation WHERE id = %s",
                    (conversation_id,),
                )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 11. 更新会话信息
@router.patch("/conversations/{conversation_id}")
async def update_conversation(
    conversation_id: str,
    body: UpdateConversationRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """更新会话信息（title 等，仅创建者/admin）。"""
    async with pool.connection() as conn:
        async with conn.transaction():
            conv = await _get_owned_conversation(conn, conversation_id, actor)
            if conv["created_by"] != actor.user_id and actor.role not in ("admin", "owner"):
                raise HTTPException(
                    status_code=403,
                    detail="Only conversation creator or admin can update the conversation",
                )
            result = await conn.execute(
                "UPDATE im_conversation SET title = %s WHERE id = %s RETURNING *",
                (body.title, conversation_id),
            )
            row = await result.fetchone()
    return _conversation_summary(row)


# 12. 列出会话成员（含在线状态）
@router.get("/conversations/{conversation_id}/members")
async def list_members(
    conversation_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """列出会话成员（含在线状态）。非成员 403。"""
    async with pool.connection() as conn:
        await _get_owned_conversation(conn, conversation_id, actor)
        await _assert_member(conn, conversation_id, actor)
        result = await conn.execute(
            "SELECT user_id, joined_at, last_read_at, unread_count "
            "FROM im_conversation_member WHERE conversation_id = %s "
            "ORDER BY joined_at ASC",
            (conversation_id,),
        )
        rows = await result.fetchall()
    members = [
        {
            "user_id": r["user_id"],
            "joined_at": r["joined_at"],
            "last_read_at": r.get("last_read_at"),
            "unread_count": r.get("unread_count", 0),
            "online": manager.is_online(r["user_id"]),
        }
        for r in rows
    ]
    return {"members": members}


__all__ = [
    "router",
    "SCHEMA_STATEMENTS",
    "ensure_messaging_schema",
    "ConversationCreateRequest",
    "MessageCreateRequest",
    "AddMemberRequest",
    "UpdateConversationRequest",
    "ConnectionManager",
    "manager",
    "im_router",
    "GroupCreateRequest",
    "GroupUpdateRequest",
    "GroupInviteRequest",
    "MessageEditRequest",
    "OfflineAckBatchRequest",
    "GroupOwnershipTransferRequest",
    "GroupMemberRoleRequest",
    "RETRACTED_CONTENT",
]


# ============================================================================
# P3 v7.179: IM 离线消息 + 群组管理增强 + 消息撤回/编辑
# ----------------------------------------------------------------------------
# 新增独立 im_router（prefix=/api/v1/im），与既有 messaging_router
# （prefix=/api/v1/messaging）解耦，避免路径遮蔽。所有端点：
# - workspace 隔离：查询 WHERE workspace_id = actor.workspace_id
# - JWT 鉴权：Depends(get_actor)
# - capability 检查：actor.capabilities 含 messaging:* 或 im:*（成员默认）
# - 审计日志：撤回/编辑写入 im_message_edit_log
# ============================================================================


# 消息撤回/编辑时间窗口（秒），默认 5 分钟
_RETRACT_EDIT_WINDOW_SECONDS = 300


im_router = APIRouter(prefix="/api/v1/im", tags=["im"])


# ============================================================================
# Pydantic 模型
# ============================================================================


class GroupCreateRequest(BaseModel):
    """POST /api/v1/im/groups 请求体。"""

    name: str = Field(min_length=1, max_length=200)
    announcement: str | None = Field(default=None, max_length=2000)
    member_user_ids: list[str] = Field(default_factory=list, max_length=500)


class GroupUpdateRequest(BaseModel):
    """PATCH /api/v1/im/groups/{group_id} 请求体。"""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    announcement: str | None = Field(default=None, max_length=2000)


class GroupInviteRequest(BaseModel):
    """POST /api/v1/im/groups/{group_id}/members 请求体。"""

    user_ids: list[str] = Field(min_length=1, max_length=500)
    role: Literal["admin", "member"] = "member"


class MessageEditRequest(BaseModel):
    """PATCH /api/v1/im/messages/{message_id} 请求体。"""

    content: str = Field(min_length=1, max_length=8000)


# ============================================================================
# 辅助函数
# ============================================================================


def _group_summary(row: dict[str, Any]) -> dict[str, Any]:
    """将群组行转为 API 响应 dict。"""
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "name": row["name"],
        "owner_id": row["owner_id"],
        "announcement": row.get("announcement"),
        "created_at": row["created_at"],
        "updated_at": row.get("updated_at"),
    }


def _offline_message_summary(row: dict[str, Any]) -> dict[str, Any]:
    """将离线消息行转为 API 响应 dict。payload 反序列化为 dict。"""
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            payload = None
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "conversation_id": row["conversation_id"],
        "sender_id": row["sender_id"],
        "recipient_id": row["recipient_id"],
        "payload": payload,
        "created_at": row["created_at"],
        "delivered_at": row.get("delivered_at"),
    }


async def _get_owned_group(
    conn: Any, group_id: str, actor: Actor
) -> dict[str, Any]:
    """查询群组并校验 workspace 归属；不存在/跨 workspace → 404。"""
    result = await conn.execute(
        "SELECT * FROM im_group WHERE id = %s",
        (group_id,),
    )
    row = await result.fetchone()
    if not row or row["workspace_id"] != actor.workspace_id:
        raise HTTPException(status_code=404, detail="Group not found")
    return row


async def _get_group_member(
    conn: Any, group_id: str, user_id: str
) -> dict[str, Any] | None:
    """查询群成员行；不存在返回 None。"""
    result = await conn.execute(
        "SELECT * FROM im_group_member WHERE group_id = %s AND user_id = %s",
        (group_id, user_id),
    )
    return await result.fetchone()


async def _assert_group_member(
    conn: Any, group_id: str, actor: Actor
) -> dict[str, Any]:
    """校验调用者是群成员，否则 403；返回成员行。"""
    row = await _get_group_member(conn, group_id, actor.user_id)
    if not row:
        raise HTTPException(
            status_code=403, detail="User is not a member of the group"
        )
    return row


async def _assert_group_admin(
    conn: Any, group_id: str, actor: Actor
) -> dict[str, Any]:
    """校验调用者是 owner/admin，否则 403；返回成员行。"""
    row = await _assert_group_member(conn, group_id, actor)
    if row["role"] not in ("owner", "admin"):
        raise HTTPException(
            status_code=403,
            detail="Only group owner or admin can perform this action",
        )
    return row


def _require_capability(actor: Actor, capability: str) -> None:
    """简单 capability 校验：actor.capabilities 含 * / domain:* / 完整 capability。"""
    domain = capability.split(":", 1)[0]
    if (
        "*" in actor.capabilities
        or capability in actor.capabilities
        or f"{domain}:*" in actor.capabilities
    ):
        return
    raise HTTPException(status_code=403, detail=f"Missing capability: {capability}")


# ============================================================================
# 离线消息端点
# ============================================================================


# 1. 拉取当前用户离线消息
@im_router.get("/offline-messages")
async def list_offline_messages(
    actor: Annotated[Actor, Depends(get_actor)],
    conversation_id: str | None = Query(default=None),
    since: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
):
    """拉取当前用户的离线消息（按 created_at DESC），支持按 conversation_id /
    since 时间过滤 / cursor（上页最后一条 created_at ISO 字符串）分页。仅返回
    本 workspace 内、recipient_id = actor.user_id 的消息。
    """
    _require_capability(actor, "im:read")
    async with pool.connection() as conn:
        # 动态拼装 WHERE 子句；参数化绑定防止 SQL 注入
        conditions = [
            "recipient_id = %s",
            "workspace_id = %s",
        ]
        params: list[Any] = [actor.user_id, actor.workspace_id]
        if conversation_id is not None:
            conditions.append("conversation_id = %s")
            params.append(conversation_id)
        if since is not None:
            conditions.append("created_at >= %s")
            params.append(since)
        if cursor is not None:
            # cursor 取上一页最后一条的 created_at（ISO 字符串）
            conditions.append("created_at < %s")
            params.append(cursor)
        where_clause = " AND ".join(conditions)
        # 多取一条用于判断是否有下一页
        fetch_limit = limit + 1
        result = await conn.execute(
            f"""
            SELECT * FROM im_offline_message
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (*params, fetch_limit),
        )
        rows = await result.fetchall()
    has_more = len(rows) > limit
    items = [_offline_message_summary(r) for r in rows[:limit]]
    next_cursor = None
    if has_more and items:
        last_created_at = items[-1]["created_at"]
        next_cursor = (
            last_created_at.isoformat()
            if hasattr(last_created_at, "isoformat")
            else str(last_created_at)
        )
    return {
        "items": items,
        "count": len(items),
        "limit": limit,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


# 2. 确认离线消息已读（幂等）
@im_router.post("/offline-messages/{message_id}/ack")
async def ack_offline_message(
    message_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """确认已读：将 delivered_at 置为 now()（若已确认则保持幂等）。
    仅本人可确认自己的离线消息。
    """
    _require_capability(actor, "im:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                "SELECT * FROM im_offline_message WHERE id = %s",
                (message_id,),
            )
            row = await result.fetchone()
            if not row or row["workspace_id"] != actor.workspace_id:
                raise HTTPException(
                    status_code=404, detail="Offline message not found"
                )
            if row["recipient_id"] != actor.user_id:
                raise HTTPException(
                    status_code=403,
                    detail="Cannot ack offline message of another user",
                )
            update_result = await conn.execute(
                "UPDATE im_offline_message SET delivered_at = now() "
                "WHERE id = %s AND delivered_at IS NULL RETURNING id",
                (message_id,),
            )
            updated = await update_result.fetchone()
    return {
        "id": message_id,
        "acked": updated is not None,
        "delivered_at": datetime.now(UTC) if updated else row["delivered_at"],
    }


# 3. 删除离线消息（仅本人）
@im_router.delete("/offline-messages/{message_id}")
async def delete_offline_message(
    message_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """删除离线消息（仅本人）。"""
    _require_capability(actor, "im:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                "SELECT * FROM im_offline_message WHERE id = %s",
                (message_id,),
            )
            row = await result.fetchone()
            if not row or row["workspace_id"] != actor.workspace_id:
                raise HTTPException(
                    status_code=404, detail="Offline message not found"
                )
            if row["recipient_id"] != actor.user_id:
                raise HTTPException(
                    status_code=403,
                    detail="Cannot delete offline message of another user",
                )
            await conn.execute(
                "DELETE FROM im_offline_message WHERE id = %s",
                (message_id,),
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ============================================================================
# 群组管理端点
# ============================================================================


# 4. 创建群组
@im_router.post("/groups", status_code=status.HTTP_201_CREATED)
async def create_group(
    body: GroupCreateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """创建群组。创建者自动加入为 owner。可选 member_user_ids 作为初始成员。"""
    _require_capability(actor, "im:write")
    # 去重 member_user_ids，排除创建者自身
    unique_members: list[str] = []
    seen: set[str] = {actor.user_id}
    for member_id in body.member_user_ids:
        if member_id not in seen:
            seen.add(member_id)
            unique_members.append(member_id)

    group_id = new_id("img")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                INSERT INTO im_group(id, workspace_id, name, owner_id, announcement)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    group_id,
                    actor.workspace_id,
                    body.name,
                    actor.user_id,
                    body.announcement,
                ),
            )
            group_row = await result.fetchone()
            # 创建者作为 owner 加入
            await conn.execute(
                "INSERT INTO im_group_member(group_id, user_id, role) "
                "VALUES (%s, %s, 'owner')",
                (group_id, actor.user_id),
            )
            # 其他成员作为 member 加入
            for member_id in unique_members:
                await conn.execute(
                    "INSERT INTO im_group_member(group_id, user_id, role) "
                    "VALUES (%s, %s, 'member')",
                    (group_id, member_id),
                )
    return _group_summary(group_row)


# 5. 列出我的群组
@im_router.get("/groups")
async def list_my_groups(
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """列出当前用户参与的群组（分页）。"""
    _require_capability(actor, "im:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT g.* FROM im_group g
            JOIN im_group_member gm ON gm.group_id = g.id
            WHERE gm.user_id = %s AND g.workspace_id = %s
            ORDER BY g.created_at DESC
            LIMIT %s OFFSET %s
            """,
            (actor.user_id, actor.workspace_id, limit, offset),
        )
        rows = await result.fetchall()
    items = [_group_summary(row) for row in rows]
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


# 6. 群详情
@im_router.get("/groups/{group_id}")
async def get_group_detail(
    group_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """获取群详情。非成员 403。"""
    _require_capability(actor, "im:read")
    async with pool.connection() as conn:
        group = await _get_owned_group(conn, group_id, actor)
        await _assert_group_member(conn, group_id, actor)
    return _group_summary(group)


# 7. 更新群信息（仅 owner/admin）
@im_router.patch("/groups/{group_id}")
async def update_group(
    group_id: str,
    body: GroupUpdateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """更新群信息（name/announcement）。仅 owner/admin。"""
    _require_capability(actor, "im:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _get_owned_group(conn, group_id, actor)
            await _assert_group_admin(conn, group_id, actor)
            # 仅更新非 None 字段
            sets: list[str] = []
            params: list[Any] = []
            if body.name is not None:
                sets.append("name = %s")
                params.append(body.name)
            if body.announcement is not None:
                sets.append("announcement = %s")
                params.append(body.announcement)
            if not sets:
                raise HTTPException(
                    status_code=422,
                    detail="At least one of name/announcement must be provided",
                )
            sets.append("updated_at = now()")
            params.append(group_id)
            result = await conn.execute(
                f"UPDATE im_group SET {', '.join(sets)} WHERE id = %s RETURNING *",
                tuple(params),
            )
            row = await result.fetchone()
    return _group_summary(row)


# 8. 解散群组（仅 owner）
@im_router.delete("/groups/{group_id}")
async def dissolve_group(
    group_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """解散群组（仅 owner）。CASCADE 删除成员关系。"""
    _require_capability(actor, "im:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            group = await _get_owned_group(conn, group_id, actor)
            if group["owner_id"] != actor.user_id:
                raise HTTPException(
                    status_code=403,
                    detail="Only group owner can dissolve the group",
                )
            await conn.execute(
                "DELETE FROM im_group WHERE id = %s",
                (group_id,),
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 9. 邀请成员（owner/admin）
@im_router.post(
    "/groups/{group_id}/members",
    status_code=status.HTTP_201_CREATED,
)
async def invite_group_members(
    group_id: str,
    body: GroupInviteRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """邀请成员加入群组（owner/admin）。已存在的成员跳过（幂等）。"""
    _require_capability(actor, "im:write")
    # 去重 user_ids
    unique_user_ids: list[str] = []
    seen: set[str] = set()
    for uid in body.user_ids:
        if uid not in seen:
            seen.add(uid)
            unique_user_ids.append(uid)

    async with pool.connection() as conn:
        async with conn.transaction():
            await _get_owned_group(conn, group_id, actor)
            await _assert_group_admin(conn, group_id, actor)
            inserted: list[str] = []
            skipped: list[str] = []
            for uid in unique_user_ids:
                existing = await _get_group_member(conn, group_id, uid)
                if existing:
                    skipped.append(uid)
                    continue
                await conn.execute(
                    "INSERT INTO im_group_member(group_id, user_id, role) "
                    "VALUES (%s, %s, %s)",
                    (group_id, uid, body.role),
                )
                inserted.append(uid)
    return {
        "group_id": group_id,
        "inserted": inserted,
        "skipped": skipped,
        "role": body.role,
    }


# 10. 移除成员（owner/admin）
@im_router.delete("/groups/{group_id}/members/{user_id}")
async def remove_group_member(
    group_id: str,
    user_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """移除群成员（owner/admin）。不能移除 owner。"""
    _require_capability(actor, "im:write")
    if user_id == actor.user_id:
        raise HTTPException(
            status_code=422,
            detail="Cannot remove yourself; use POST /groups/{id}/leave to leave",
        )
    async with pool.connection() as conn:
        async with conn.transaction():
            await _get_owned_group(conn, group_id, actor)
            await _assert_group_admin(conn, group_id, actor)
            target = await _get_group_member(conn, group_id, user_id)
            if not target:
                raise HTTPException(
                    status_code=404, detail="User is not a member of the group"
                )
            if target["role"] == "owner":
                raise HTTPException(
                    status_code=403,
                    detail="Cannot remove group owner; dissolve the group instead",
                )
            await conn.execute(
                "DELETE FROM im_group_member WHERE group_id = %s AND user_id = %s",
                (group_id, user_id),
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 11. 主动退出（非 owner）
@im_router.post("/groups/{group_id}/leave")
async def leave_group(
    group_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """主动退出群组。owner 不能退出（应使用解散）。"""
    _require_capability(actor, "im:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _get_owned_group(conn, group_id, actor)
            member = await _assert_group_member(conn, group_id, actor)
            if member["role"] == "owner":
                raise HTTPException(
                    status_code=403,
                    detail="Group owner cannot leave; dissolve the group instead",
                )
            await conn.execute(
                "DELETE FROM im_group_member WHERE group_id = %s AND user_id = %s",
                (group_id, actor.user_id),
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 12. 列出群成员（分页）
@im_router.get("/groups/{group_id}/members")
async def list_group_members(
    group_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """列出群成员（含角色 + 在线状态，分页）。非成员 403。"""
    _require_capability(actor, "im:read")
    async with pool.connection() as conn:
        await _get_owned_group(conn, group_id, actor)
        await _assert_group_member(conn, group_id, actor)
        result = await conn.execute(
            "SELECT user_id, role, joined_at FROM im_group_member "
            "WHERE group_id = %s ORDER BY "
            "CASE role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, "
            "joined_at ASC LIMIT %s OFFSET %s",
            (group_id, limit, offset),
        )
        rows = await result.fetchall()
    members = [
        {
            "user_id": r["user_id"],
            "role": r["role"],
            "joined_at": r["joined_at"],
            "online": manager.is_online(r["user_id"]),
        }
        for r in rows
    ]
    return {
        "members": members,
        "count": len(members),
        "limit": limit,
        "offset": offset,
    }


# ============================================================================
# 消息撤回与编辑端点
# ----------------------------------------------------------------------------
# - 仅发送者本人可操作（sender_id == actor.user_id）
# - 时间窗口：5 分钟内（基于 im_conv_message.created_at 与 now() 的差值）
# - 撤回：UPDATE content 为占位符 + 写入 im_message_edit_log(action='retract')
# - 编辑：UPDATE content + 写入 im_message_edit_log(action='edit')
# - 撤回后的消息不可再编辑（content == '__retracted__' → 409）
# ============================================================================


async def _get_owned_message_for_actor(
    conn: Any, message_id: str, actor: Actor
) -> dict[str, Any]:
    """查询消息（联表 im_conversation 校验 workspace 归属 + 校验 sender_id）。"""
    result = await conn.execute(
        """
        SELECT m.*, c.workspace_id AS conv_workspace_id
        FROM im_conv_message m
        JOIN im_conversation c ON c.id = m.conversation_id
        WHERE m.id = %s
        """,
        (message_id,),
    )
    row = await result.fetchone()
    if not row or row["conv_workspace_id"] != actor.workspace_id:
        raise HTTPException(status_code=404, detail="Message not found")
    if row["sender_id"] != actor.user_id:
        raise HTTPException(
            status_code=403,
            detail="Only the sender can retract or edit this message",
        )
    return row


def _assert_within_window(row: dict[str, Any]) -> None:
    """校验消息发送时间在 RETRACT_EDIT_WINDOW 内，否则 409。"""
    created_at = row["created_at"]
    if isinstance(created_at, str):
        try:
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                status_code=500, detail="Invalid created_at format"
            )
    if not hasattr(created_at, "timestamp"):
        raise HTTPException(status_code=500, detail="Invalid created_at type")
    now = datetime.now(UTC)
    # 兼容 naive datetime
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    elapsed = (now - created_at).total_seconds()
    if elapsed > _RETRACT_EDIT_WINDOW_SECONDS:
        raise HTTPException(
            status_code=409,
            detail=(
                "Message is outside the retract/edit time window "
                f"({_RETRACT_EDIT_WINDOW_SECONDS}s)"
            ),
        )


# 13. 撤回消息（5 分钟内）
@im_router.post("/messages/{message_id}/retract")
async def retract_message(
    message_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """撤回消息：UPDATE content 为占位符 + 写审计。撤回后不可编辑。"""
    _require_capability(actor, "im:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await _get_owned_message_for_actor(conn, message_id, actor)
            # 已撤回（content 为 __retracted__）→ 409
            if row["content"] == RETRACTED_CONTENT or row.get("retracted_at"):
                raise HTTPException(
                    status_code=409,
                    detail="Message has already been retracted",
                )
            _assert_within_window(row)
            old_payload = {"content": row["content"]}
            new_payload = {"content": RETRACTED_CONTENT}
            await conn.execute(
                "UPDATE im_conv_message SET content = %s, retracted_at = now() "
                "WHERE id = %s",
                (RETRACTED_CONTENT, message_id),
            )
            await conn.execute(
                """
                INSERT INTO im_message_edit_log(
                    id, message_id, workspace_id, edited_by,
                    old_payload, new_payload, action
                ) VALUES (%s, %s, %s, %s, %s, %s, 'retract')
                """,
                (
                    new_id("imel"),
                    message_id,
                    actor.workspace_id,
                    actor.user_id,
                    json.dumps(old_payload),
                    json.dumps(new_payload),
                ),
            )
            # 广播撤回事件 + 同步尚未投递的离线副本，避免离线成员上线后读到原文
            await _broadcast_message_mutation(
                conn,
                conversation_id=row["conversation_id"],
                action="message_retracted",
                message_id=message_id,
                content=None,
                actor_user_id=actor.user_id,
            )
    return {
        "id": message_id,
        "retracted": True,
        "retracted_at": datetime.now(UTC),
    }


# 14. 编辑消息（5 分钟内）
@im_router.patch("/messages/{message_id}")
async def edit_message(
    message_id: str,
    body: MessageEditRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """编辑消息：UPDATE content + 写审计。已撤回的消息不可编辑。"""
    _require_capability(actor, "im:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await _get_owned_message_for_actor(conn, message_id, actor)
            if row["content"] == RETRACTED_CONTENT or row.get("retracted_at"):
                raise HTTPException(
                    status_code=409,
                    detail="Cannot edit a retracted message",
                )
            _assert_within_window(row)
            old_payload = {"content": row["content"]}
            new_payload = {"content": body.content}
            result = await conn.execute(
                "UPDATE im_conv_message SET content = %s, edited_at = now() "
                "WHERE id = %s RETURNING *",
                (body.content, message_id),
            )
            updated = await result.fetchone()
            await conn.execute(
                """
                INSERT INTO im_message_edit_log(
                    id, message_id, workspace_id, edited_by,
                    old_payload, new_payload, action
                ) VALUES (%s, %s, %s, %s, %s, %s, 'edit')
                """,
                (
                    new_id("imel"),
                    message_id,
                    actor.workspace_id,
                    actor.user_id,
                    json.dumps(old_payload),
                    json.dumps(new_payload),
                ),
            )
            await _broadcast_message_mutation(
                conn,
                conversation_id=row["conversation_id"],
                action="message_edited",
                message_id=message_id,
                content=body.content,
                actor_user_id=actor.user_id,
            )
    return _message_summary(updated)


# ============================================================================
# P3 v7.180 新增：离线投递游标 / 批量 ack / 群主转让 / 成员角色管理 / 审计查询
# ----------------------------------------------------------------------------
# 全部 fail-closed：先校验 workspace 归属（404），再校验成员身份与角色（403），
# 最后才执行写操作；capability 缺失直接 403。
# ============================================================================


class OfflineAckBatchRequest(BaseModel):
    """POST /api/v1/im/offline-messages/ack-batch 请求体。

    二选一：``message_ids`` 精确确认指定离线消息；或 ``conversation_id`` +
    ``up_to``（ISO 时间）按会话批量确认到某个时间点。
    """

    message_ids: list[str] | None = Field(default=None, max_length=500)
    conversation_id: str | None = Field(default=None, max_length=200)
    up_to: datetime | None = None


class GroupOwnershipTransferRequest(BaseModel):
    """POST /api/v1/im/groups/{group_id}/transfer-ownership 请求体。"""

    new_owner_id: str = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=500)


class GroupMemberRoleRequest(BaseModel):
    """PATCH /api/v1/im/groups/{group_id}/members/{user_id}/role 请求体。

    只允许在 admin / member 之间切换；owner 角色只能通过转让接口变更。
    """

    role: Literal["admin", "member"]


async def _assert_group_owner(
    conn: Any, group_id: str, actor: Actor
) -> dict[str, Any]:
    """校验调用者是群 owner，否则 403；返回成员行。

    双重校验：``im_group_member.role == 'owner'`` 且 ``im_group.owner_id`` 匹配，
    任一不满足即拒绝（fail-closed，避免两张表不一致时被绕过）。
    """
    member = await _assert_group_member(conn, group_id, actor)
    if member["role"] != "owner":
        raise HTTPException(
            status_code=403,
            detail="Only the group owner can perform this action",
        )
    return member


# 15. 列出当前用户的每会话投递游标
@im_router.get("/delivery-cursors")
async def list_delivery_cursors(
    actor: Annotated[Actor, Depends(get_actor)],
    conversation_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """列出当前用户在各会话的投递/确认游标与待确认数（workspace 隔离）。"""
    _require_capability(actor, "im:read")
    conditions = ["workspace_id = %s", "user_id = %s"]
    params: list[Any] = [actor.workspace_id, actor.user_id]
    if conversation_id is not None:
        conditions.append("conversation_id = %s")
        params.append(conversation_id)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT * FROM im_delivery_cursor
            WHERE {" AND ".join(conditions)}
            ORDER BY updated_at DESC
            LIMIT %s OFFSET %s
            """,
            (*params, limit, offset),
        )
        rows = await result.fetchall()
    items = [
        {
            "conversation_id": r["conversation_id"],
            "user_id": r["user_id"],
            "last_delivered_message_id": r.get("last_delivered_message_id"),
            "last_delivered_at": r.get("last_delivered_at"),
            "last_acked_message_id": r.get("last_acked_message_id"),
            "last_acked_at": r.get("last_acked_at"),
            "pending_count": r.get("pending_count", 0),
            "updated_at": r.get("updated_at"),
        }
        for r in rows
    ]
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


# 16. 批量确认离线消息并推进游标
@im_router.post("/offline-messages/ack-batch")
async def ack_offline_messages_batch(
    body: OfflineAckBatchRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """批量确认离线消息：置 delivered_at/acked_at 并推进 im_delivery_cursor。

    仅影响 ``recipient_id = actor.user_id`` 且同 workspace 的行（跨用户/跨
    workspace 的行不会被匹配，因此天然不可越权确认）。幂等：重复确认返回
    ``acked=0``。
    """
    _require_capability(actor, "im:write")
    if not body.message_ids and not body.conversation_id:
        raise HTTPException(
            status_code=422,
            detail="Either message_ids or conversation_id must be provided",
        )
    conditions = [
        "recipient_id = %s",
        "workspace_id = %s",
        "acked_at IS NULL",
    ]
    params: list[Any] = [actor.user_id, actor.workspace_id]
    if body.message_ids:
        conditions.append("id = ANY(%s)")
        params.append(list(body.message_ids))
    if body.conversation_id:
        conditions.append("conversation_id = %s")
        params.append(body.conversation_id)
    if body.up_to is not None:
        conditions.append("created_at <= %s")
        params.append(body.up_to)
    where_clause = " AND ".join(conditions)
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                f"""
                UPDATE im_offline_message
                SET acked_at = now(),
                    delivered_at = COALESCE(delivered_at, now())
                WHERE {where_clause}
                RETURNING id, conversation_id, message_id, created_at
                """,
                tuple(params),
            )
            rows = await result.fetchall()
            acked_ids = [r["id"] for r in rows]
            # 每个会话取本批最后一条（created_at 最大）推进 acked 游标
            latest_by_conversation: dict[str, dict[str, Any]] = {}
            for r in rows:
                conv_id = r["conversation_id"]
                current = latest_by_conversation.get(conv_id)
                if current is None or r["created_at"] >= current["created_at"]:
                    latest_by_conversation[conv_id] = r
            for conv_id, r in latest_by_conversation.items():
                await conn.execute(
                    """
                    INSERT INTO im_delivery_cursor(
                        workspace_id, conversation_id, user_id,
                        last_acked_message_id, last_acked_at, pending_count
                    ) VALUES (%s, %s, %s, %s, now(), 0)
                    ON CONFLICT (conversation_id, user_id) DO UPDATE
                      SET last_acked_message_id = EXCLUDED.last_acked_message_id,
                          last_acked_at = now(),
                          pending_count = GREATEST(
                              im_delivery_cursor.pending_count - %s, 0
                          ),
                          updated_at = now()
                    """,
                    (
                        actor.workspace_id,
                        conv_id,
                        actor.user_id,
                        r.get("message_id"),
                        sum(
                            1
                            for item in rows
                            if item["conversation_id"] == conv_id
                        ),
                    ),
                )
    return {
        "acked": len(acked_ids),
        "message_ids": acked_ids,
        "conversations": sorted(latest_by_conversation.keys()),
    }


# 17. 群主转让
@im_router.post("/groups/{group_id}/transfer-ownership")
async def transfer_group_ownership(
    group_id: str,
    body: GroupOwnershipTransferRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """转让群主：仅现任 owner 可操作，新 owner 必须已是群成员。

    事务内完成：新 owner 升为 owner、原 owner 降为 admin、``im_group.owner_id``
    更新、写入转让审计与两条角色变更审计。
    """
    _require_capability(actor, "im:write")
    if body.new_owner_id == actor.user_id:
        raise HTTPException(
            status_code=422,
            detail="New owner must be different from the current owner",
        )
    async with pool.connection() as conn:
        async with conn.transaction():
            await _get_owned_group(conn, group_id, actor)
            await _assert_group_owner(conn, group_id, actor)
            target = await _get_group_member(conn, group_id, body.new_owner_id)
            if not target:
                raise HTTPException(
                    status_code=404,
                    detail="New owner is not a member of the group",
                )
            old_target_role = target["role"]
            await conn.execute(
                "UPDATE im_group_member SET role = 'owner' "
                "WHERE group_id = %s AND user_id = %s",
                (group_id, body.new_owner_id),
            )
            await conn.execute(
                "UPDATE im_group_member SET role = 'admin' "
                "WHERE group_id = %s AND user_id = %s",
                (group_id, actor.user_id),
            )
            result = await conn.execute(
                "UPDATE im_group SET owner_id = %s, updated_at = now() "
                "WHERE id = %s RETURNING *",
                (body.new_owner_id, group_id),
            )
            group_row = await result.fetchone()
            await conn.execute(
                """
                INSERT INTO im_group_ownership_transfer(
                    id, workspace_id, group_id, from_user_id,
                    to_user_id, performed_by, reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    new_id("imgt"),
                    actor.workspace_id,
                    group_id,
                    actor.user_id,
                    body.new_owner_id,
                    actor.user_id,
                    body.reason,
                ),
            )
            for target_user, old_role, new_role in (
                (body.new_owner_id, old_target_role, "owner"),
                (actor.user_id, "owner", "admin"),
            ):
                await conn.execute(
                    """
                    INSERT INTO im_group_role_change(
                        id, workspace_id, group_id, target_user_id,
                        old_role, new_role, changed_by
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        new_id("imgr"),
                        actor.workspace_id,
                        group_id,
                        target_user,
                        old_role,
                        new_role,
                        actor.user_id,
                    ),
                )
    return {
        "group": _group_summary(group_row),
        "previous_owner_id": actor.user_id,
        "new_owner_id": body.new_owner_id,
    }


# 18. 变更群成员角色（admin ↔ member）
@im_router.patch("/groups/{group_id}/members/{user_id}/role")
async def update_group_member_role(
    group_id: str,
    user_id: str,
    body: GroupMemberRoleRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """提升/降级群成员角色（admin ↔ member）。仅 owner 可操作。

    - admin 不能自行提升他人（fail-closed，避免权限横向扩散）；
    - owner 不能通过本接口改自己的角色，也不能把他人的 owner 角色改掉，
      owner 变更必须走 ``/transfer-ownership``。
    """
    _require_capability(actor, "im:write")
    if user_id == actor.user_id:
        raise HTTPException(
            status_code=422,
            detail="Cannot change your own role; use transfer-ownership instead",
        )
    async with pool.connection() as conn:
        async with conn.transaction():
            await _get_owned_group(conn, group_id, actor)
            await _assert_group_owner(conn, group_id, actor)
            target = await _get_group_member(conn, group_id, user_id)
            if not target:
                raise HTTPException(
                    status_code=404, detail="User is not a member of the group"
                )
            if target["role"] == "owner":
                raise HTTPException(
                    status_code=403,
                    detail="Cannot change the owner role; use transfer-ownership",
                )
            old_role = target["role"]
            if old_role == body.role:
                return {
                    "group_id": group_id,
                    "user_id": user_id,
                    "role": body.role,
                    "changed": False,
                }
            await conn.execute(
                "UPDATE im_group_member SET role = %s "
                "WHERE group_id = %s AND user_id = %s",
                (body.role, group_id, user_id),
            )
            await conn.execute(
                """
                INSERT INTO im_group_role_change(
                    id, workspace_id, group_id, target_user_id,
                    old_role, new_role, changed_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    new_id("imgr"),
                    actor.workspace_id,
                    group_id,
                    user_id,
                    old_role,
                    body.role,
                    actor.user_id,
                ),
            )
    return {
        "group_id": group_id,
        "user_id": user_id,
        "role": body.role,
        "previous_role": old_role,
        "changed": True,
    }


# 19. 群治理审计（角色变更 + 群主转让）
@im_router.get("/groups/{group_id}/audit")
async def list_group_audit(
    group_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=50, ge=1, le=200),
):
    """列出群的角色变更与群主转让审计记录。仅 owner/admin 可查看。"""
    _require_capability(actor, "im:read")
    async with pool.connection() as conn:
        await _get_owned_group(conn, group_id, actor)
        await _assert_group_admin(conn, group_id, actor)
        role_result = await conn.execute(
            "SELECT * FROM im_group_role_change "
            "WHERE group_id = %s AND workspace_id = %s "
            "ORDER BY changed_at DESC LIMIT %s",
            (group_id, actor.workspace_id, limit),
        )
        role_rows = await role_result.fetchall()
        transfer_result = await conn.execute(
            "SELECT * FROM im_group_ownership_transfer "
            "WHERE group_id = %s AND workspace_id = %s "
            "ORDER BY created_at DESC LIMIT %s",
            (group_id, actor.workspace_id, limit),
        )
        transfer_rows = await transfer_result.fetchall()
    return {
        "group_id": group_id,
        "role_changes": [
            {
                "id": r["id"],
                "target_user_id": r["target_user_id"],
                "old_role": r["old_role"],
                "new_role": r["new_role"],
                "changed_by": r["changed_by"],
                "changed_at": r["changed_at"],
            }
            for r in role_rows
        ],
        "ownership_transfers": [
            {
                "id": r["id"],
                "from_user_id": r["from_user_id"],
                "to_user_id": r["to_user_id"],
                "performed_by": r["performed_by"],
                "reason": r.get("reason"),
                "created_at": r["created_at"],
            }
            for r in transfer_rows
        ],
    }


# 20. 消息撤回/编辑审计轨迹
@im_router.get("/messages/{message_id}/history")
async def list_message_history(
    message_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=50, ge=1, le=200),
):
    """查询某条消息的撤回/编辑审计轨迹。仅会话成员可查看。"""
    _require_capability(actor, "im:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT m.id, m.conversation_id, m.sender_id, m.content,
                   m.created_at, m.retracted_at, m.edited_at,
                   c.workspace_id AS conv_workspace_id
            FROM im_conv_message m
            JOIN im_conversation c ON c.id = m.conversation_id
            WHERE m.id = %s
            """,
            (message_id,),
        )
        row = await result.fetchone()
        if not row or row["conv_workspace_id"] != actor.workspace_id:
            raise HTTPException(status_code=404, detail="Message not found")
        await _assert_member(conn, row["conversation_id"], actor)
        log_result = await conn.execute(
            "SELECT * FROM im_message_edit_log "
            "WHERE message_id = %s AND workspace_id = %s "
            "ORDER BY edited_at ASC LIMIT %s",
            (message_id, actor.workspace_id, limit),
        )
        log_rows = await log_result.fetchall()
    entries = []
    for r in log_rows:
        old_payload = r.get("old_payload")
        new_payload = r.get("new_payload")
        if isinstance(old_payload, str):
            try:
                old_payload = json.loads(old_payload)
            except (json.JSONDecodeError, TypeError):
                old_payload = None
        if isinstance(new_payload, str):
            try:
                new_payload = json.loads(new_payload)
            except (json.JSONDecodeError, TypeError):
                new_payload = None
        entries.append(
            {
                "id": r["id"],
                "action": r["action"],
                "edited_by": r["edited_by"],
                "edited_at": r["edited_at"],
                "old_payload": old_payload,
                "new_payload": new_payload,
            }
        )
    return {
        "message": {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "sender_id": row["sender_id"],
            "content": row["content"],
            "created_at": row["created_at"],
            "retracted_at": row.get("retracted_at"),
            "edited_at": row.get("edited_at"),
        },
        "entries": entries,
        "count": len(entries),
    }
