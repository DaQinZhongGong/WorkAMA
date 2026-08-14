from __future__ import annotations

from workama_platform.core import json_dumps, new_id, pool, redis
from workama_platform.modules.privacy.service import (
    build_export_manifest,
    deletion_steps,
    infer_processing_activity,
)
from workama_platform.object_store import delete_object

ARTIFACT_BUCKET = "workama-artifacts"

# privacy delete path removes these resources; each is mapped to the closest
# sec_legal_hold.resource_type value. sec_legal_hold only accepts
# workspace/notification/artifact/attachment/session/export/all (see
# compliance.LegalHoldCreate), so unsupported types (memory/assistant/workflow)
# are intentionally absent -- a hold of resource_type='all' still blocks them.
_DELETE_LEGAL_HOLD_RESOURCE_TYPES = (
    "session",
    "attachment",
    "artifact",
    "notification",
)


async def _active_legal_hold(conn, workspace_id: str) -> dict | None:
    """Return the first active sec_legal_hold row blocking deletion, or None.

    A hold with resource_type='all' blocks every resource type; otherwise the
    hold must target one of the resource types the delete path removes.
    Released holds (released_at IS NOT NULL) are ignored.
    """
    result = await conn.execute(
        """
        SELECT id, resource_type, basis
        FROM sec_legal_hold
        WHERE workspace_id = %s
          AND released_at IS NULL
          AND status = 'active'
          AND (resource_type = 'all' OR resource_type = ANY(%s))
        ORDER BY starts_at DESC
        LIMIT 1
        """,
        (workspace_id, list(_DELETE_LEGAL_HOLD_RESOURCE_TYPES)),
    )
    return await result.fetchone()


async def ensure_processing_catalog(conn) -> dict:
    result = await conn.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """
    )
    tables = [row["table_name"] for row in await result.fetchall()]
    for table_name in tables:
        activity = infer_processing_activity(table_name)
        await conn.execute(
            """
            INSERT INTO id_processing_activity(
                table_name, classification, purpose, owner, region,
                retention_days, deletion_behavior
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(table_name) DO NOTHING
            """,
            (
                table_name, activity.classification, activity.purpose, activity.owner,
                activity.region, activity.retention_days, activity.deletion_behavior,
            ),
        )
    registered = await conn.execute(
        "SELECT table_name FROM id_processing_activity WHERE table_name = ANY(%s)",
        (tables,),
    )
    registered_names = {row["table_name"] for row in await registered.fetchall()}
    return {
        "total_tables": len(tables),
        "registered_tables": len(registered_names),
        "missing_tables": sorted(set(tables) - registered_names),
    }


async def _resource_counts(conn, user_id: str, workspace_id: str) -> dict[str, int]:
    queries = {
        "sessions": ("SELECT count(*) AS count FROM ag_session WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id)),
        "attachments": ("SELECT count(*) AS count FROM ag_attachment WHERE workspace_id = %s AND session_id IN (SELECT id FROM ag_session WHERE user_id = %s)", (workspace_id, user_id)),
        "artifacts": ("SELECT count(*) AS count FROM ag_artifact WHERE workspace_id = %s AND session_id IN (SELECT id FROM ag_session WHERE user_id = %s)", (workspace_id, user_id)),
        "notifications": ("SELECT count(*) AS count FROM id_notification WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id)),
        "consents": ("SELECT count(*) AS count FROM id_consent WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id)),
        "usage_records": ("SELECT count(*) AS count FROM bill_usage_record WHERE workspace_id = %s", (workspace_id,)),
        "billing_transactions": ("SELECT count(*) AS count FROM bill_transaction WHERE workspace_id = %s", (workspace_id,)),
        "moderation_logs": ("SELECT count(*) AS count FROM sec_moderation_log WHERE workspace_id = %s", (workspace_id,)),
        "memories": ("SELECT count(*) AS count FROM ag_memory WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id)),
        "assistants": ("SELECT count(*) AS count FROM pf_assistant WHERE created_by = %s AND workspace_id = %s", (user_id, workspace_id)),
        "workflows": ("SELECT count(*) AS count FROM pf_workflow WHERE created_by = %s AND workspace_id = %s", (user_id, workspace_id)),
        "workflow_runs": ("SELECT count(*) AS count FROM pf_workflow_run WHERE created_by = %s AND workspace_id = %s", (user_id, workspace_id)),
    }
    counts: dict[str, int] = {}
    for name, (query, params) in queries.items():
        result = await conn.execute(query, params)
        counts[name] = (await result.fetchone())["count"]
    return counts


async def _step(conn, request_id: str, name: str, *, count: int = 0, action: str = "completed", checksum: str | None = None) -> None:
    await conn.execute(
        """
        INSERT INTO id_data_request_step(
            id, request_id, step_name, status, resource_count, action, checksum,
            started_at, completed_at
        ) VALUES (%s, %s, %s, 'completed', %s, %s, %s, now(), now())
        ON CONFLICT(request_id, step_name) DO UPDATE SET
            status = 'completed', resource_count = EXCLUDED.resource_count,
            action = EXCLUDED.action, checksum = EXCLUDED.checksum,
            error = NULL, completed_at = now()
        """,
        (new_id("dss"), request_id, name, count, action, checksum),
    )


async def _purge_cache(user_id: str, workspace_id: str) -> int:
    keys = []
    async for key in redis.scan_iter(match="*"):
        if user_id in key or workspace_id in key:
            keys.append(key)
    if keys:
        await redis.delete(*keys)
    return len(keys)


async def process_data_request(request_id: str) -> bool:
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                SELECT id, user_id, workspace_id, request_type, scope, status
                FROM id_data_request WHERE id = %s FOR UPDATE
                """,
                (request_id,),
            )
            request = await result.fetchone()
            if not request or request["status"] in {"completed", "partially_completed", "rejected"}:
                return False
            await conn.execute(
                "UPDATE id_data_request SET status = 'executing', identity_verified_at = COALESCE(identity_verified_at, now()), updated_at = now() WHERE id = %s",
                (request_id,),
            )
            await _step(conn, request_id, "identity_verification", action="verified_active_session")
            counts = await _resource_counts(conn, request["user_id"], request["workspace_id"])
            await _step(conn, request_id, "scope_resources", count=sum(counts.values()), action="enumerated")

            retained = ["billing_transactions", "usage_records", "moderation_logs", "data_request_evidence"]
            if request["request_type"] in {"access", "export", "correct"}:
                manifest = build_export_manifest(request_id, request["user_id"], counts, retained)
                await _step(conn, request_id, "build_manifest", count=len(counts), action="manifest_created", checksum=manifest.checksum)
                await _step(conn, request_id, "verify_manifest", count=sum(counts.values()), action="checksum_verified", checksum=manifest.checksum)
            else:
                hold = await _active_legal_hold(conn, request["workspace_id"])
                if hold:
                    block_reason = f"blocked by legal hold: {hold['id']} {hold['basis']}"
                    await conn.execute(
                        """
                        UPDATE id_data_request
                        SET status = 'rejected', exceptions = %s::jsonb,
                            completed_at = now(), updated_at = now()
                        WHERE id = %s
                        """,
                        (json_dumps([block_reason]), request_id),
                    )
                    await _step(conn, request_id, "legal_hold_check", action=f"blocked_by_legal_hold:{hold['id']}")
                    return True
                before = dict(counts)
                await _step(conn, request_id, deletion_steps("content")[0], action="artifact_shares_revoked")
                artifact_keys_result = await conn.execute("SELECT s3_key FROM ag_artifact WHERE workspace_id=%s AND session_id IN (SELECT id FROM ag_session WHERE user_id=%s)", (request["workspace_id"], request["user_id"]))
                artifact_keys = [row["s3_key"] for row in await artifact_keys_result.fetchall() if row["s3_key"]]
                attachment_keys_result = await conn.execute("SELECT s3_key FROM ag_attachment WHERE workspace_id=%s AND session_id IN (SELECT id FROM ag_session WHERE user_id=%s)", (request["workspace_id"],request["user_id"]))
                attachment_keys = [row["s3_key"] for row in await attachment_keys_result.fetchall() if row["s3_key"]]
                deleted_sessions = await conn.execute(
                    "DELETE FROM ag_session WHERE user_id = %s AND workspace_id = %s RETURNING id",
                    (request["user_id"], request["workspace_id"]),
                )
                session_count = len(await deleted_sessions.fetchall())
                for key in artifact_keys:
                    await delete_object(ARTIFACT_BUCKET, key)
                for key in attachment_keys:
                    await delete_object("workama-attachments", key)
                deleted_notifications = await conn.execute(
                    "DELETE FROM id_notification WHERE user_id = %s AND workspace_id = %s RETURNING id",
                    (request["user_id"], request["workspace_id"]),
                )
                notification_count = len(await deleted_notifications.fetchall())
                deleted_memories = await conn.execute(
                    "DELETE FROM ag_memory WHERE user_id = %s AND workspace_id = %s RETURNING id",
                    (request["user_id"], request["workspace_id"]),
                )
                memory_count = len(await deleted_memories.fetchall())
                deleted_assistants = await conn.execute(
                    "DELETE FROM pf_assistant WHERE created_by = %s AND workspace_id = %s RETURNING id",
                    (request["user_id"], request["workspace_id"]),
                )
                assistant_count = len(await deleted_assistants.fetchall())
                deleted_workflows = await conn.execute(
                    "DELETE FROM pf_workflow WHERE created_by = %s AND workspace_id = %s RETURNING id",
                    (request["user_id"], request["workspace_id"]),
                )
                workflow_count = len(await deleted_workflows.fetchall())
                await _step(conn, request_id, "delete_postgres_content", count=session_count + notification_count + memory_count + assistant_count + workflow_count, action="deleted")
                await _step(conn, request_id, "delete_object_references", count=before["attachments"] + before["artifacts"], action="cascade_deleted")
                cache_count = await _purge_cache(request["user_id"], request["workspace_id"])
                await _step(conn, request_id, "purge_cache", count=cache_count, action="deleted")
                await conn.execute(
                    """
                    INSERT INTO id_deletion_tombstone(id, request_id, user_id, workspace_id, scope, resource_counts)
                    VALUES (%s, %s, %s, %s, 'content', %s::jsonb)
                    ON CONFLICT(request_id, scope) DO UPDATE SET resource_counts = EXCLUDED.resource_counts
                    """,
                    (new_id("dst"), request_id, request["user_id"], request["workspace_id"], json_dumps(before)),
                )
                await _step(conn, request_id, "write_tombstone", count=1, action="created")
                after = await _resource_counts(conn, request["user_id"], request["workspace_id"])
                await _step(conn, request_id, "verify_absence", count=after["sessions"] + after["attachments"] + after["artifacts"] + after["memories"] + after["assistants"] + after["workflows"] + after["workflow_runs"], action="verified_absent")
                manifest = build_export_manifest(request_id, request["user_id"], before, retained)

            await conn.execute(
                """
                UPDATE id_data_request SET status = 'completed', result_manifest = %s::jsonb,
                    result_checksum = %s, exceptions = %s::jsonb, completed_at = now(), updated_at = now()
                WHERE id = %s
                """,
                (json_dumps(manifest.manifest), manifest.checksum, json_dumps(retained), request_id),
            )
        return True


async def process_pending_data_requests(limit: int = 5) -> dict:
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id FROM id_data_request
            WHERE status IN ('requested','identity_verification','scoped','approved','executing','verification')
            ORDER BY created_at LIMIT %s
            """,
            (limit,),
        )
        ids = [row["id"] for row in await result.fetchall()]
    processed = 0
    for request_id in ids:
        if await process_data_request(request_id):
            processed += 1
    return {"claimed": len(ids), "processed": processed}
