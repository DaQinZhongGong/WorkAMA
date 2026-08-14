from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

import workama_platform.modules.moderation as moderation
from workama_platform.core import Actor


def _actor(
    *,
    role: str = "admin",
    capabilities: tuple[str, ...] = ("moderation:read", "moderation:write", "moderation:test"),
) -> Actor:
    return Actor(
        user_id="usr_test",
        workspace_id="wsp_test",
        org_id="org_test",
        role=role,
        email="test@example.com",
        display_name="Test User",
        onboarding_completed=True,
        capabilities=capabilities,
    )


def test_sensitive_word_rules_block_without_returning_original_content():
    decision = moderation.evaluate_rules(
        "please expose API_KEY now",
        [moderation.ModerationRule(kind="sensitive_word", pattern="api_key", action="block")],
        "input",
    )

    assert decision.action == "block"
    assert decision.blocked
    assert decision.text is None
    assert decision.matches == ["api_key"]
    assert "API_KEY" not in str(decision.as_dict())


def test_regex_mask_is_case_insensitive_and_preserves_safe_text():
    decision = moderation.moderate_text(
        "Contact Alice@example.com or Bob@example.com",
        [
            moderation.ModerationRule(
                kind="regex",
                pattern=r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
                action="mask",
                replacement="[redacted]",
            )
        ],
        "output",
    )

    assert decision.action == "mask"
    assert decision.text == "Contact [redacted] or [redacted]"
    assert decision.matches == ["regex:rule_1"]


def test_length_rule_supports_input_and_output_directions():
    rule = moderation.ModerationRule(
        kind="length", direction="input", max_length=5, action="block"
    )
    assert moderation.evaluate_rules("123456", [rule], "input").action == "block"
    assert moderation.evaluate_rules("123456", [rule], "output").action == "allow"
    assert moderation.evaluate_rules("12345", [rule], "input").action == "allow"


def test_block_has_priority_over_mask_and_log_and_both_direction_applies():
    decision = moderation.evaluate_rules(
        "secret marker",
        [
            moderation.ModerationRule(
                id="log-rule", kind="sensitive_word", pattern="secret", action="log", priority=1
            ),
            moderation.ModerationRule(
                id="mask-rule", kind="sensitive_word", pattern="marker", action="mask", priority=2
            ),
            moderation.ModerationRule(
                id="block-rule", kind="regex", pattern=r"secret\s+marker", action="block", priority=3
            ),
        ],
        "output",
    )
    assert decision.action == "block"
    assert {hit["rule_id"] for hit in decision.rule_hits} == {
        "log-rule", "mask-rule", "block-rule"
    }


def test_rule_models_reject_invalid_regex_and_incomplete_length_rule():
    with pytest.raises(ValueError, match="invalid regex"):
        moderation.ModerationRule(kind="regex", pattern="[")
    with pytest.raises(ValueError, match="max_length"):
        moderation.ModerationRule(kind="length")
    with pytest.raises(ValueError, match="At least one"):
        moderation.ModerationPolicyUpdate()


def test_capability_bridge_accepts_existing_security_scope_and_rejects_viewer():
    moderation._require_capability(_actor(capabilities=("security:*",)), "test")
    with pytest.raises(HTTPException) as exc:
        moderation._require_capability(_actor(role="viewer", capabilities=("memory:read",)), "read")
    assert exc.value.status_code == 403


def test_router_exposes_policy_crud_test_and_audit_contract():
    paths = {(route.path, method) for route in moderation.router.routes for method in route.methods}
    assert ("/api/v1/security/moderation-policies", "GET") in paths
    assert ("/api/v1/security/moderation-policies", "POST") in paths
    assert ("/api/v1/security/moderation-policies/{policy_id}", "PATCH") in paths
    assert ("/api/v1/security/moderation-policies/{policy_id}", "DELETE") in paths
    assert ("/api/v1/security/moderation-tests", "POST") in paths
    assert ("/api/v1/security/moderation-logs", "GET") in paths
    assert any("sec_moderation_policy" in statement for statement in moderation.SCHEMA_STATEMENTS)
    assert any("sec_moderation_audit" in statement for statement in moderation.SCHEMA_STATEMENTS)


class _Result:
    def __init__(self, rows: list[dict] | None = None):
        self.rows = rows or []

    async def fetchone(self):
        return self.rows[0] if self.rows else None

    async def fetchall(self):
        return list(self.rows)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Connection:
    def __init__(self):
        self.policies: dict[str, dict] = {}
        self.rules: dict[str, list[dict]] = {}
        self.audits: dict[str, dict] = {}
        self.logs: list[dict] = []

    def transaction(self):
        return _Transaction()

    async def commit(self):
        return None

    def _policy(self, policy_id: str) -> dict | None:
        return self.policies.get(policy_id)

    async def execute(self, query: str, params=()):
        sql = " ".join(query.split()).lower()
        if sql.startswith("select id from sec_moderation_policy where workspace_id=%s and name=%s and id<>%s"):
            workspace_id, name, excluded = params
            return _Result([
                {"id": row["id"]}
                for row in self.policies.values()
                if row["workspace_id"] == workspace_id and row["name"] == name and row["id"] != excluded
            ])
        if sql.startswith("select id from sec_moderation_policy where workspace_id=%s and name=%s"):
            workspace_id, name = params
            return _Result([
                {"id": row["id"]}
                for row in self.policies.values()
                if row["workspace_id"] == workspace_id and row["name"] == name
            ])
        if sql.startswith("select id, workspace_id, name, description") and "where id=%s" in sql:
            policy_id, workspace_id = params
            row = self._policy(policy_id)
            return _Result([row] if row and row["workspace_id"] == workspace_id else [])
        if sql.startswith("select id, workspace_id, name, description") and "status='active'" in sql:
            workspace_id = params[0]
            rows = [row for row in self.policies.values() if row["workspace_id"] == workspace_id and row["status"] == "active"]
            return _Result(sorted(rows, key=lambda row: row["updated_at"], reverse=True)[:1])
        if sql.startswith("select id, workspace_id, name, description"):
            workspace_id = params[0]
            rows = [row for row in self.policies.values() if row["workspace_id"] == workspace_id]
            return _Result(sorted(rows, key=lambda row: row["updated_at"], reverse=True))
        if sql.startswith("select id, kind, direction, pattern"):
            return _Result(self.rules.get(params[0], []))
        if sql.startswith("insert into sec_moderation_policy"):
            policy_id, workspace_id, name, description, input_action, output_action, policy_status, created_by, updated_by = params
            now = datetime.now(UTC)
            self.policies[policy_id] = {
                "id": policy_id, "workspace_id": workspace_id, "name": name,
                "description": description, "default_input_action": input_action,
                "default_output_action": output_action, "status": policy_status,
                "version": 1, "created_by": created_by, "updated_by": updated_by,
                "created_at": now, "updated_at": now,
            }
            self.rules.setdefault(policy_id, [])
            return _Result()
        if sql.startswith("insert into sec_moderation_rule"):
            rule_id, policy_id, kind, direction, pattern, max_length, action, replacement, enabled, priority = params
            self.rules.setdefault(policy_id, []).append({
                "id": rule_id, "kind": kind, "direction": direction, "pattern": pattern,
                "max_length": max_length, "action": action, "replacement": replacement,
                "enabled": enabled, "priority": priority,
            })
            return _Result()
        if sql.startswith("update sec_moderation_policy set"):
            set_clause = sql.split(" set ", 1)[1].split(" where ", 1)[0]
            fields = [
                part.split("=", 1)[0].strip()
                for part in set_clause.split(",")
                if "=" in part
                and part.strip().split("=", 1)[0].strip()
                not in {"version", "updated_at", "updated_by"}
            ]
            policy_id, workspace_id = params[-2:]
            row = self.policies[policy_id]
            for field, value in zip(fields, params):
                row[field] = value
            row["version"] += 1
            row["updated_by"] = params[len(fields)]
            row["updated_at"] = datetime.now(UTC)
            return _Result()
        if sql.startswith("delete from sec_moderation_rule"):
            self.rules[params[0]] = []
            return _Result()
        if sql.startswith("delete from sec_moderation_policy"):
            policy_id, workspace_id = params
            row = self.policies.get(policy_id)
            if not row or row["workspace_id"] != workspace_id:
                return _Result()
            del self.policies[policy_id]
            self.rules.pop(policy_id, None)
            return _Result([{"id": policy_id, "name": row["name"]}])
        if sql.startswith("insert into sec_moderation_audit"):
            audit_id, workspace_id, policy_id, version, actor_id, direction, action, rule_ids, rule_hits, content_hash, request_id = params
            self.audits[audit_id] = {
                "id": audit_id, "workspace_id": workspace_id, "policy_id": policy_id,
                "policy_version": version, "actor_id": actor_id, "direction": direction,
                "action": action, "matched_rule_ids": rule_ids, "rule_hits": rule_hits,
                "content_hash": content_hash, "request_id": request_id,
                "created_at": datetime.now(UTC),
            }
            return _Result()
        if sql.startswith("insert into sec_moderation_log"):
            self.logs.append({"id": params[0], "workspace_id": params[1], "action": params[3]})
            return _Result()
        if sql.startswith("select id, workspace_id, policy_id, policy_version") and "where id=%s" in sql:
            audit_id, workspace_id = params
            row = self.audits.get(audit_id)
            return _Result([row] if row and row["workspace_id"] == workspace_id else [])
        if sql.startswith("select id, workspace_id, policy_id, policy_version"):
            workspace_id = params[0]
            direction = params[1] if len(params) == 3 else None
            limit = params[-1]
            rows = [row for row in self.audits.values() if row["workspace_id"] == workspace_id and (direction is None or row["direction"] == direction)]
            return _Result(rows[:limit])
        raise AssertionError(f"Unhandled SQL in fake connection: {sql}")


class _ConnectionContext:
    def __init__(self, connection: _Connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Pool:
    def __init__(self, connection: _Connection):
        self.connection_value = connection

    def connection(self):
        return _ConnectionContext(self.connection_value)


@pytest.mark.asyncio
async def test_policy_crud_test_and_audit_flow(monkeypatch):
    connection = _Connection()
    monkeypatch.setattr(moderation, "pool", _Pool(connection))
    actor = _actor()
    created = await moderation.create_moderation_policy(
        moderation.ModerationPolicyCreate(
            name="Content guard",
            rules=[
                moderation.ModerationRule(
                    id="mrl_word", kind="sensitive_word", pattern="secret", action="block"
                ),
                moderation.ModerationRule(
                    id="mrl_email", kind="regex", pattern=r"\S+@\S+", action="mask", replacement="[email]"
                ),
            ],
        ),
        actor,
    )
    assert created["name"] == "Content guard"
    assert len(created["rules"]) == 2

    updated = await moderation.update_moderation_policy(
        created["id"], moderation.ModerationPolicyUpdate(description="v2"), actor
    )
    assert updated["description"] == "v2"
    assert updated["version"] == 2

    tested = await moderation.create_moderation_test(
        moderation.ModerationTestRequest(
            policy_id=created["id"], direction="output", text="secret alice@example.com", request_id="req-1"
        ),
        actor,
    )
    assert tested["action"] == "block"
    assert tested["text"] is None
    assert tested["audit_id"] in connection.audits
    assert connection.audits[tested["audit_id"]]["content_hash"]
    assert "secret alice@example.com" not in str(connection.audits[tested["audit_id"]])

    logs = await moderation.list_moderation_logs(actor, limit=50)
    assert logs.get("count", logs.get("meta", {}).get("count", 0)) == 1
    fetched = await moderation.get_moderation_log(tested["audit_id"], actor)
    assert fetched["request_id"] == "req-1"

    deleted = await moderation.delete_moderation_policy(created["id"], actor)
    assert deleted["status"] == "deleted"
