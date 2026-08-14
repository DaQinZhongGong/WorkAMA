from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import smtplib
from datetime import UTC, datetime
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import aiosmtplib
import httpx

from workama_platform.core import pool, settings
from workama_platform.modules.notification.service import retry_delay_seconds


def send_email(recipient: str, title: str, summary: str, *, mock: bool = False) -> str:
    """Send email or return a deterministic provider id for a testable mock."""
    if mock:
        digest = hashlib.sha256(f"{recipient}\0{title}\0{summary}".encode()).hexdigest()[:24]
        return f"mock-email:{digest}"
    if not settings.smtp_host:
        raise RuntimeError("smtp_not_configured")
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = recipient
    message["Subject"] = title
    message.set_content(summary)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as client:
        client.send_message(message)
    sep = "\0"
    return f"smtp:{hashlib.sha256(f'{recipient}{sep}{title}'.encode()).hexdigest()[:24]}"


async def send_email_real(
    to_addr: str,
    subject: str,
    html_body: str,
    text_body: str | None = None,
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_username: str,
    smtp_password: str,
    from_addr: str,
    use_tls: bool = True,
) -> str:
    """通过真实 SMTP（aiosmtplib 异步）发送邮件，返回 provider_id。"""
    msg = MIMEMultipart("alternative")
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    sep = "\0"
    provider_id = f"smtp:{hashlib.sha256(f'{to_addr}{sep}{subject}'.encode()).hexdigest()[:24]}"
    msg["Message-ID"] = f"<{provider_id}@workama>"
    msg.attach(MIMEText(text_body or html_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    await aiosmtplib.send(
        msg,
        hostname=smtp_host,
        port=smtp_port,
        username=smtp_username or None,
        password=smtp_password or None,
        start_tls=use_tls,
    )
    return provider_id


def _compute_webhook_signature(timestamp: str, secret: str, raw_body: str) -> str:
    """计算 Webhook HMAC-SHA256 签名，返回 t=<ts>,v1=<hex> 形式。"""
    digest = hmac.new(
        secret.encode(), f"{timestamp}.{raw_body}".encode(), hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def _webhook_raw_body(payload: dict[str, Any]) -> str:
    """序列化 Webhook 载荷，mock 与真实投递共用以保持签名一致。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def send_webhook_mock(
    url: str,
    secret_key: str,
    payload: dict[str, Any],
    idempotency_key: str,
    *,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a signed mock webhook response without making a network request."""
    if not url.startswith("mock://"):
        raise ValueError("mock_webhook_url_required")
    timestamp = str(int((occurred_at or datetime.now(UTC)).timestamp()))
    raw_body = _webhook_raw_body(payload)
    signature = _compute_webhook_signature(timestamp, secret_key, raw_body)
    provider_id = "mock-webhook:" + hashlib.sha256(
        f"{url}\0{idempotency_key}".encode()
    ).hexdigest()[:24]
    return {
        "provider_id": provider_id,
        "status_code": 202,
        "timestamp": timestamp,
        "signature": signature,
        "body": raw_body,
    }


async def send_webhook_real(
    url: str,
    secret: str,
    payload: dict[str, Any],
    idempotency_key: str,
    *,
    occurred_at: datetime | None = None,
    timeout: float = 30.0,
    max_retries: int = 3,
) -> dict[str, Any]:
    """通过真实 HTTP（httpx 异步）发送 Webhook，含 HMAC 签名与指数退避重试。"""
    ts = occurred_at or datetime.now(UTC)
    timestamp = str(int(ts.timestamp()))
    raw_body = _webhook_raw_body(payload)
    signature = _compute_webhook_signature(timestamp, secret, raw_body)
    headers = {
        "Content-Type": "application/json",
        "X-Workama-Signature": signature,
        "X-Workama-Idempotency-Key": idempotency_key,
        "X-Workama-Timestamp": timestamp,
        "User-Agent": "WorkAMA-Webhook/1",
    }
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for attempt in range(max_retries):
            try:
                resp = await client.post(url, content=raw_body, headers=headers)
                if resp.status_code < 400:
                    provider_id = (
                        resp.headers.get("x-request-id")
                        or "webhook:"
                        + hashlib.sha256(f"{url}\0{idempotency_key}".encode()).hexdigest()[:24]
                    )
                    return {
                        "provider_id": provider_id,
                        "status_code": resp.status_code,
                        "timestamp": timestamp,
                        "signature": signature,
                        "body": raw_body,
                    }
                if resp.status_code == 410:
                    raise RuntimeError(f"webhook endpoint gone: HTTP {resp.status_code}")
                last_exc = RuntimeError(f"webhook HTTP {resp.status_code}")
            except httpx.TimeoutException as exc:
                last_exc = exc
            except httpx.TransportError as exc:
                last_exc = exc
            except httpx.HTTPError as exc:
                last_exc = exc
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
    if isinstance(last_exc, (httpx.TimeoutException, httpx.TransportError)):
        raise last_exc
    if last_exc is not None:
        raise RuntimeError(f"webhook delivery failed after {max_retries} retries: {last_exc}")
    raise RuntimeError(f"webhook delivery failed after {max_retries} retries: unknown")


def classify_delivery_error(exc: Exception) -> str:
    if isinstance(
        exc,
        (
            TimeoutError,
            asyncio.TimeoutError,
            OSError,
            smtplib.SMTPException,
            httpx.TimeoutException,
            httpx.TransportError,
        ),
    ):
        return "transient_provider_error"
    return str(exc)[:120] or exc.__class__.__name__


async def deliver_email(
    to_addr: str,
    subject: str,
    html_body: str,
    text_body: str | None = None,
    *,
    settings: Any = None,
) -> str:
    """根据配置自动选择 mock 或真实 SMTP 发送。"""
    cfg = settings
    if cfg is None or getattr(cfg, "smtp_mock", True):
        return send_email(to_addr, subject, text_body or html_body, mock=True)
    if not cfg.smtp_host:
        raise RuntimeError("smtp_not_configured")
    return await send_email_real(
        to_addr, subject, html_body, text_body,
        smtp_host=cfg.smtp_host, smtp_port=cfg.smtp_port,
        smtp_username=cfg.smtp_username, smtp_password=cfg.smtp_password,
        from_addr=cfg.smtp_from, use_tls=cfg.smtp_use_tls,
    )


async def deliver_webhook(
    url: str,
    secret: str,
    payload: dict[str, Any],
    idempotency_key: str,
    *,
    occurred_at: datetime | None = None,
    settings: Any = None,
) -> dict[str, Any]:
    """根据配置自动选择 mock 或真实 Webhook 投递。"""
    cfg = settings
    if cfg is None or getattr(cfg, "notification_webhook_mock", True):
        return send_webhook_mock(url, secret, payload, idempotency_key, occurred_at=occurred_at)
    return await send_webhook_real(url, secret, payload, idempotency_key, occurred_at=occurred_at)


async def _claim_deliveries(channel: str, limit: int) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        async with conn.transaction():
            extension_check = await conn.execute(
                """
                SELECT EXISTS(
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema=current_schema()
                      AND table_name='id_notification_delivery'
                      AND column_name='max_attempts'
                ) AS available
                """
            )
            extended = bool((await extension_check.fetchone())["available"])
            if extended:
                result = await conn.execute(
                    """
                    SELECT d.id, d.notification_id, d.webhook_id, d.attempt,
                           COALESCE(d.max_attempts, 5) AS max_attempts,
                           d.idempotency_key, n.title, n.summary, n.event_type,
                           n.priority, n.action_url, n.resource_ref, n.workspace_id,
                           u.email
                    FROM id_notification_delivery d
                    JOIN id_notification n ON n.id = d.notification_id
                    JOIN id_user u ON u.id = n.user_id
                    WHERE d.channel = %s
                      AND d.status IN ('pending', 'retry_wait')
                      AND (d.next_attempt_at IS NULL OR d.next_attempt_at <= now())
                    ORDER BY d.created_at
                    FOR UPDATE OF d SKIP LOCKED LIMIT %s
                    """,
                    (channel, min(max(limit, 1), 100)),
                )
            else:
                result = await conn.execute(
                    """
                    SELECT d.id, d.notification_id, d.attempt, n.title, n.summary,
                           n.event_type, n.priority, n.action_url, n.resource_ref,
                           n.workspace_id, u.email
                    FROM id_notification_delivery d
                    JOIN id_notification n ON n.id = d.notification_id
                    JOIN id_user u ON u.id = n.user_id
                    WHERE d.channel = %s AND d.status = 'pending'
                      AND (d.next_attempt_at IS NULL OR d.next_attempt_at <= now())
                    ORDER BY d.created_at
                    FOR UPDATE OF d SKIP LOCKED LIMIT %s
                    """,
                    (channel, min(max(limit, 1), 100)),
                )
            claimed = await result.fetchall()
            for row in claimed:
                if extended:
                    await conn.execute(
                        """
                        UPDATE id_notification_delivery
                        SET status='sending', attempt=attempt+1, last_attempt_at=now(),
                            updated_at=now()
                        WHERE id=%s
                        """,
                        (row["id"],),
                    )
                else:
                    await conn.execute(
                        "UPDATE id_notification_delivery SET attempt=attempt+1, updated_at=now() WHERE id=%s",
                        (row["id"],),
                    )
                row["_schema_extended"] = extended
                row.setdefault("webhook_id", None)
                row.setdefault("max_attempts", 3)
                row.setdefault("idempotency_key", row["id"])
    return claimed


async def _record_failure(row: dict[str, Any], exc: Exception, *, retryable: bool) -> bool:
    attempt = int(row["attempt"]) + 1
    extended = bool(row.get("_schema_extended"))
    max_attempts = int(row.get("max_attempts") or (5 if extended else 3))
    final = not retryable or attempt >= max_attempts
    status = "failed" if final else "pending"
    delay = retry_delay_seconds(attempt)
    async with pool.connection() as conn:
        if extended:
            await conn.execute(
                """
                UPDATE id_notification_delivery
                SET status=%s, error_class=%s,
                    next_attempt_at=CASE WHEN %s THEN NULL ELSE now()+make_interval(secs => %s) END,
                    updated_at=now()
                WHERE id=%s AND status='sending'
                """,
                (status, classify_delivery_error(exc), final, delay, row["id"]),
            )
        else:
            await conn.execute(
                """
                UPDATE id_notification_delivery
                SET status=%s, error_class=%s,
                    next_attempt_at=CASE WHEN %s THEN NULL ELSE now()+make_interval(secs => %s) END,
                    updated_at=now()
                WHERE id=%s
                """,
                (status, classify_delivery_error(exc), final, delay, row["id"]),
            )
        await conn.commit()
    return final


async def _record_success(row: dict[str, Any], provider_id: str, response_code: int | None = None) -> None:
    async with pool.connection() as conn:
        if row.get("_schema_extended"):
            await conn.execute(
                """
                UPDATE id_notification_delivery
                SET status='sent', provider_id=%s, response_code=%s,
                    error_class=NULL, next_attempt_at=NULL, delivered_at=now(), updated_at=now()
                WHERE id=%s AND status='sending'
                """,
                (provider_id, response_code, row["id"]),
            )
        else:
            await conn.execute(
                """
                UPDATE id_notification_delivery
                SET status='sent', provider_id=%s,
                    error_class=NULL, next_attempt_at=NULL, updated_at=now()
                WHERE id=%s
                """,
                (provider_id, row["id"]),
            )
        await conn.commit()


async def process_pending_email_deliveries(limit: int = 20, *, mock: bool = False) -> dict[str, int]:
    claimed = await _claim_deliveries("email", limit)
    sent = failed = retried = 0
    for row in claimed:
        try:
            if mock:
                provider_id = await asyncio.to_thread(
                    send_email, row["email"], row["title"], row["summary"], mock=True
                )
            else:
                provider_id = await deliver_email(
                    row["email"], row["title"], row["summary"], row["summary"],
                )
        except Exception as exc:
            retryable = bool(settings.smtp_host) and classify_delivery_error(exc) == "transient_provider_error"
            final = await _record_failure(row, exc, retryable=retryable)
            failed += int(final)
            retried += int(not final)
            continue
        await _record_success(row, provider_id)
        sent += 1
    return {"claimed": len(claimed), "sent": sent, "failed": failed, "retried": retried}


async def process_pending_webhook_deliveries(limit: int = 20) -> dict[str, int]:
    """Deliver enabled endpoints through mock or real webhook provider."""
    claimed = await _claim_deliveries("webhook", limit)
    sent = failed = retried = 0
    for row in claimed:
        async with pool.connection() as conn:
            endpoint_result = await conn.execute(
                """
                SELECT id, url, secret_hash, status
                FROM id_notification_webhook
                WHERE id=%s AND workspace_id=%s
                """,
                (row["webhook_id"], row["workspace_id"]),
            )
            endpoint = await endpoint_result.fetchone()
        if not endpoint or endpoint["status"] != "active":
            final = await _record_failure(row, RuntimeError("webhook_disabled"), retryable=False)
            failed += int(final)
            continue
        payload = {
            "schema_version": 1,
            "event_type": row["event_type"],
            "priority": row["priority"],
            "title": row["title"],
            "summary": row["summary"],
            "action_url": row["action_url"],
            "resource_ref": row["resource_ref"],
            "notification_id": row["notification_id"],
        }
        try:
            result = await deliver_webhook(
                endpoint["url"], endpoint["secret_hash"], payload,
                row["idempotency_key"] or row["id"],
            )
        except Exception as exc:
            retryable = classify_delivery_error(exc) == "transient_provider_error"
            final = await _record_failure(row, exc, retryable=retryable)
            failed += int(final)
            retried += int(not final)
            continue
        await _record_success(row, result["provider_id"], result["status_code"])
        sent += 1
    return {"claimed": len(claimed), "sent": sent, "failed": failed, "retried": retried}


async def process_pending_deliveries(limit: int = 20, *, mock_email: bool = False) -> dict[str, int]:
    email = await process_pending_email_deliveries(limit, mock=mock_email)
    webhook = await process_pending_webhook_deliveries(limit)
    return {
        "claimed": email["claimed"] + webhook["claimed"],
        "sent": email["sent"] + webhook["sent"],
        "failed": email["failed"] + webhook["failed"],
        "retried": email["retried"] + webhook["retried"],
    }
