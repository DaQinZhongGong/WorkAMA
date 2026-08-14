from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Iterable

from psycopg.errors import UndefinedTable

from workama_platform.core import hash_secret, json_dumps, new_id


NOTIFICATION_CHANNELS = frozenset({"in_app", "email", "webhook"})
FORCED_IN_APP_PREFIXES = ("security.", "auth.", "billing.")
RETRY_DELAYS_SECONDS = (60, 300, 1800, 7200, 43200)

# This is intentionally additive. The original id_notification and
# id_notification_delivery tables remain the source of truth for existing
# P0 callers and retain their pending/sent/failed compatibility states.
NOTIFICATION_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS id_notification_preference (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
        workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
        event_type TEXT NOT NULL DEFAULT '*',
        channel TEXT NOT NULL CHECK (channel IN ('in_app', 'email', 'webhook')),
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        quiet_start TIME,
        quiet_end TIME,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(user_id, workspace_id, event_type, channel)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_id_notification_preference_scope ON id_notification_preference(user_id, workspace_id, event_type)",
    """
    CREATE TABLE IF NOT EXISTS id_notification_webhook (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
        workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
        owner_user_id TEXT NOT NULL REFERENCES id_user(id),
        name TEXT NOT NULL,
        url TEXT NOT NULL,
        secret_hash TEXT NOT NULL,
        events TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
        failure_count INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_id_notification_webhook_workspace ON id_notification_webhook(workspace_id, status, created_at DESC)",
    "ALTER TABLE id_notification_delivery ADD COLUMN IF NOT EXISTS webhook_id TEXT REFERENCES id_notification_webhook(id) ON DELETE CASCADE",
    "ALTER TABLE id_notification_delivery ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 5",
    "ALTER TABLE id_notification_delivery ADD COLUMN IF NOT EXISTS idempotency_key TEXT",
    "ALTER TABLE id_notification_delivery ADD COLUMN IF NOT EXISTS request_hash TEXT",
    "ALTER TABLE id_notification_delivery ADD COLUMN IF NOT EXISTS response_code INTEGER",
    "ALTER TABLE id_notification_delivery ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ",
    "ALTER TABLE id_notification_delivery ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ",
    "ALTER TABLE id_notification_delivery DROP CONSTRAINT IF EXISTS id_notification_delivery_status_check",
    """
    ALTER TABLE id_notification_delivery
    ADD CONSTRAINT id_notification_delivery_status_check
    CHECK (status IN ('pending', 'sending', 'accepted', 'delivered', 'sent', 'failed', 'retry_wait', 'permanent_failed', 'suppressed'))
    """,
    "ALTER TABLE id_notification_delivery DROP CONSTRAINT IF EXISTS id_notification_delivery_notification_id_channel_key",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_id_notification_delivery_target ON id_notification_delivery(notification_id, channel, COALESCE(webhook_id, ''))",
    "CREATE INDEX IF NOT EXISTS idx_id_notification_delivery_due ON id_notification_delivery(channel, status, next_attempt_at, created_at)",
)


async def ensure_notification_schema(conn) -> None:
    """Apply notification preferences/delivery additions to an existing connection."""
    for statement in NOTIFICATION_SCHEMA_STATEMENTS:
        await conn.execute(statement)


def should_notify_low_balance(available_balance: Decimal, threshold: Decimal) -> bool:
    return available_balance < threshold


def low_balance_dedupe_key(workspace_id: str, occurred_at: datetime) -> str:
    return f"billing.low_balance:{workspace_id}:{occurred_at.astimezone(UTC).date().isoformat()}"


def is_forced_in_app(event_type: str, channel: str) -> bool:
    return channel == "in_app" and event_type.startswith(FORCED_IN_APP_PREFIXES)


def preference_change_allowed(event_type: str, channel: str, enabled: bool) -> bool:
    """Keep safety and billing events visible in the in-app channel."""
    return enabled or not is_forced_in_app(event_type, channel)


def retry_delay_seconds(attempt: int) -> int:
    """Return the bounded 1m/5m/30m/2h/12h backoff for a 1-based attempt."""
    index = max(0, min(int(attempt) - 1, len(RETRY_DELAYS_SECONDS) - 1))
    return RETRY_DELAYS_SECONDS[index]


async def notification_channel_enabled(
    conn,
    *,
    user_id: str,
    workspace_id: str,
    event_type: str,
    channel: str,
) -> bool:
    if channel not in NOTIFICATION_CHANNELS:
        raise ValueError(f"Unsupported notification channel: {channel}")
    if is_forced_in_app(event_type, channel):
        return True
    try:
        result = await conn.execute(
            """
            SELECT enabled
            FROM id_notification_preference
            WHERE user_id=%s AND workspace_id=%s
              AND channel=%s AND event_type IN (%s, '*')
            ORDER BY CASE WHEN event_type=%s THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (user_id, workspace_id, channel, event_type, event_type),
        )
    except UndefinedTable:
        # Existing P0 volumes may not have migration 008 yet. Defaults remain
        # enabled until the additive migration is applied.
        return True
    row = await result.fetchone()
    return bool(row["enabled"]) if row else True


async def create_notification(
    conn,
    *,
    user_id: str,
    workspace_id: str,
    event_type: str,
    title: str,
    summary: str,
    priority: str = "normal",
    action_url: str | None = None,
    payload_min: dict[str, Any] | None = None,
    resource_ref: str | None = None,
    dedupe_key: str | None = None,
    channels: Iterable[str] = ("in_app", "email"),
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    """Create one notification fact and idempotent channel delivery rows."""
    selected_channels = tuple(dict.fromkeys(channels))
    if not selected_channels or any(channel not in NOTIFICATION_CHANNELS for channel in selected_channels):
        raise ValueError("Notification channels are invalid")
    key = dedupe_key or f"{event_type}:{resource_ref or ''}"
    inserted = await conn.execute(
        """
        INSERT INTO id_notification(
            id, user_id, workspace_id, event_type, priority, title, summary,
            action_url, payload_min, resource_ref, dedupe_key, expires_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
        ON CONFLICT(user_id, dedupe_key) DO NOTHING RETURNING id, created_at
        """,
        (
            new_id("ntf"), user_id, workspace_id, event_type, priority, title,
            summary, action_url, json_dumps(payload_min or {}), resource_ref, key,
            expires_at,
        ),
    )
    row = await inserted.fetchone()
    if not row:
        existing = await conn.execute(
            "SELECT id, created_at FROM id_notification WHERE user_id=%s AND dedupe_key=%s",
            (user_id, key),
        )
        row = await existing.fetchone()
        return {"id": row["id"], "created": False, "dedupe_key": key} if row else {"id": None, "created": False, "dedupe_key": key}

    notification_id = row["id"]
    for channel in selected_channels:
        if not await notification_channel_enabled(
            conn,
            user_id=user_id,
            workspace_id=workspace_id,
            event_type=event_type,
            channel=channel,
        ):
            continue
        in_app = channel == "in_app"
        await conn.execute(
            """
            INSERT INTO id_notification_delivery(
                id, notification_id, channel, provider, attempt, status
            ) VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING
            """,
            (
                new_id("ndl"), notification_id, channel,
                "workama" if in_app else None,
                1 if in_app else 0,
                "sent" if in_app else "pending",
            ),
        )
    return {"id": notification_id, "created": True, "dedupe_key": key}


async def create_low_balance_notifications(
    conn,
    workspace_id: str,
    available_balance: Decimal,
    threshold: Decimal = Decimal("1000"),
    occurred_at: datetime | None = None,
) -> int:
    if not should_notify_low_balance(available_balance, threshold):
        return 0
    now = occurred_at or datetime.now(UTC)
    recipients = await conn.execute(
        """
        SELECT DISTINCT u.id FROM id_member m
        JOIN id_user u ON u.id = m.user_id
        WHERE m.workspace_id = %s AND m.role IN ('owner', 'admin') AND u.status = 'active'
        """,
        (workspace_id,),
    )
    created = 0
    for recipient in await recipients.fetchall():
        result = await create_notification(
            conn,
            user_id=recipient["id"],
            workspace_id=workspace_id,
            event_type="billing.low_balance",
            priority="high",
            title="积分余额较低",
            summary=f"当前可用积分 {available_balance:.6f}，低于告警阈值 {threshold:.6f}。",
            action_url="/admin/billing",
            payload_min={"available_balance": str(available_balance), "threshold": str(threshold)},
            resource_ref=workspace_id,
            dedupe_key=low_balance_dedupe_key(workspace_id, now),
            channels=("in_app", "email"),
        )
        created += int(result["created"])
    return created


async def create_automation_run_notification(
    conn,
    *,
    user_id: str,
    workspace_id: str,
    run_id: str,
    target_type: str,
    target_id: str,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    """Create one result notification; the run ID is the replay key."""
    if status not in {"succeeded", "failed", "cancelled"}:
        raise ValueError("automation result status is not terminal")
    succeeded = status == "succeeded"
    event_type = f"automation.run.{status}"
    title = "Automation completed" if succeeded else "Automation failed"
    summary = (
        f"The {target_type} action {target_id} completed successfully."
        if succeeded
        else f"The {target_type} action {target_id} ended with status {status}."
    )
    return await create_notification(
        conn,
        user_id=user_id,
        workspace_id=workspace_id,
        event_type=event_type,
        priority="normal" if succeeded else "high",
        title=title,
        summary=summary,
        action_url=f"/automations/runs/{run_id}",
        payload_min={
            "run_id": run_id,
            "target_type": target_type,
            "target_id": target_id,
            "status": status,
            "error_code": error_code,
            "error_message": (error_message or "")[:300] if error_message else None,
        },
        resource_ref=run_id,
        dedupe_key=f"automation.run:{run_id}",
        channels=("in_app", "email"),
    )


async def create_mock_webhook_endpoint(
    conn,
    *,
    org_id: str,
    workspace_id: str,
    owner_user_id: str,
    name: str,
    url: str,
    secret: str,
    events: Iterable[str] = (),
) -> dict[str, Any]:
    """Register a mock endpoint while persisting only a keyed hash of its secret."""
    if not url.startswith("mock://"):
        raise ValueError("Mock webhook URLs must use mock://")
    endpoint_id = new_id("whk")
    await conn.execute(
        """
        INSERT INTO id_notification_webhook(
            id, org_id, workspace_id, owner_user_id, name, url, secret_hash, events
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (endpoint_id, org_id, workspace_id, owner_user_id, name.strip(), url, hash_secret(secret), list(events)),
    )
    return {"id": endpoint_id, "name": name.strip(), "url": url, "status": "active"}


async def enqueue_webhook_delivery(
    conn,
    *,
    notification_id: str,
    webhook_id: str,
    idempotency_key: str,
    max_attempts: int = len(RETRY_DELAYS_SECONDS),
) -> dict[str, Any]:
    """Create one webhook delivery; replays return the existing delivery."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    result = await conn.execute(
        """
        INSERT INTO id_notification_delivery(
            id, notification_id, webhook_id, channel, provider, attempt, status,
            max_attempts, idempotency_key
        ) VALUES (%s,%s,%s,'webhook','mock-webhook',0,'pending',%s,%s)
        ON CONFLICT DO NOTHING
        RETURNING id, notification_id, webhook_id, status, attempt, idempotency_key
        """,
        (new_id("ndl"), notification_id, webhook_id, max_attempts, idempotency_key),
    )
    row = await result.fetchone()
    if row:
        return {**row, "idempotent_replay": False}
    existing = await conn.execute(
        """
        SELECT id, notification_id, webhook_id, status, attempt, idempotency_key
        FROM id_notification_delivery
        WHERE notification_id=%s AND channel='webhook' AND webhook_id=%s
        """,
        (notification_id, webhook_id),
    )
    row = await existing.fetchone()
    if not row:
        raise RuntimeError("webhook delivery was not created")
    return {**row, "idempotent_replay": True}
