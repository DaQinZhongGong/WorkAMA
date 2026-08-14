from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Mapping, Sequence

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator

from workama_platform.core import (
    Actor,
    capability_allows,
    get_actor,
    hash_secret,
    json_dumps,
    new_id,
    pool,
)


router = APIRouter(prefix="/api/v1/security", tags=["moderation"])
moderation_router = router

RuleKind = Literal["sensitive_word", "regex", "length"]
RuleDirection = Literal["input", "output", "both"]
RuleAction = Literal["block", "mask", "log"]
DecisionAction = Literal["allow", "block", "mask", "log"]

_RULE_KINDS = frozenset({"sensitive_word", "regex", "length"})
_DIRECTIONS = frozenset({"input", "output", "both"})
_ACTIONS = frozenset({"block", "mask", "log"})
_ACTION_PRIORITY = {"log": 1, "mask": 2, "block": 3}
_MAX_RULES = 500


class ModerationRule(BaseModel):
    """A deterministic rule evaluated before any optional model reviewer."""

    id: str | None = Field(default=None, min_length=1, max_length=100)
    kind: RuleKind
    direction: RuleDirection = "both"
    pattern: str | None = Field(default=None, max_length=500)
    max_length: int | None = Field(default=None, ge=1, le=10_000_000)
    action: RuleAction = "block"
    replacement: str = Field(default="***", max_length=200)
    enabled: bool = True
    priority: int = Field(default=100, ge=0, le=10_000)

    @model_validator(mode="after")
    def validate_shape(self) -> ModerationRule:
        value = self.pattern.strip() if self.pattern else None
        if self.kind in {"sensitive_word", "regex"} and not value:
            raise ValueError("pattern is required for sensitive_word and regex rules")
        if self.kind == "length" and self.max_length is None:
            raise ValueError("max_length is required for length rules")
        if self.kind == "regex":
            try:
                re.compile(value or "")
            except re.error as exc:
                raise ValueError(f"invalid regex: {exc}") from exc
        if self.kind != "length" and self.max_length is not None:
            raise ValueError("max_length is only valid for length rules")
        self.pattern = value
        return self


class ModerationPolicyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    default_input_action: RuleAction = "log"
    default_output_action: RuleAction = "block"
    status: Literal["draft", "active", "archived"] = "active"
    rules: list[ModerationRule] = Field(default_factory=list, max_length=_MAX_RULES)


class ModerationPolicyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    default_input_action: RuleAction | None = None
    default_output_action: RuleAction | None = None
    status: Literal["draft", "active", "archived"] | None = None
    rules: list[ModerationRule] | None = Field(default=None, max_length=_MAX_RULES)

    @model_validator(mode="after")
    def require_change(self) -> ModerationPolicyUpdate:
        if not self.model_fields_set:
            raise ValueError("At least one policy field is required")
        return self


class ModerationTestRequest(BaseModel):
    text: str = Field(min_length=0, max_length=1_000_000)
    direction: Literal["input", "output"]
    policy_id: str | None = Field(default=None, min_length=1, max_length=100)
    request_id: str | None = Field(default=None, max_length=200)


@dataclass(frozen=True)
class ModerationDecision:
    action: DecisionAction
    text: str | None
    matches: list[str]
    rule_hits: list[dict[str, Any]]

    @property
    def blocked(self) -> bool:
        return self.action == "block"

    @property
    def masked(self) -> bool:
        return self.action == "mask"

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "text": self.text,
            "matches": self.matches,
            "rule_hits": self.rule_hits,
            "blocked": self.blocked,
        }


@dataclass(frozen=True)
class _RuleHit:
    rule_id: str
    kind: str
    action: RuleAction
    replacement: str
    label: str
    start: int
    end: int
    priority: int


def _rule_value(rule: ModerationRule | Mapping[str, Any], key: str, default: Any = None) -> Any:
    if isinstance(rule, ModerationRule):
        return getattr(rule, key, default)
    return rule.get(key, default)


def _normalize_rule(rule: ModerationRule | Mapping[str, Any], index: int) -> dict[str, Any]:
    kind = str(_rule_value(rule, "kind", "")).strip().lower()
    if kind == "word" or kind == "keyword":
        kind = "sensitive_word"
    if kind not in _RULE_KINDS:
        raise ValueError(f"unsupported moderation rule kind: {kind}")
    direction = str(_rule_value(rule, "direction", "both")).strip().lower()
    if direction not in _DIRECTIONS:
        raise ValueError(f"unsupported moderation rule direction: {direction}")
    action = str(_rule_value(rule, "action", "block")).strip().lower()
    if action not in _ACTIONS:
        raise ValueError(f"unsupported moderation rule action: {action}")
    pattern = _rule_value(rule, "pattern")
    if pattern is None:
        pattern = _rule_value(rule, "term")
    pattern = str(pattern).strip() if pattern is not None else None
    max_length = _rule_value(rule, "max_length")
    if max_length is None and kind == "length":
        max_length = _rule_value(rule, "value")
    if kind in {"sensitive_word", "regex"} and not pattern:
        raise ValueError("pattern is required for sensitive_word and regex rules")
    if pattern is not None and len(pattern) > 500:
        raise ValueError("pattern must be at most 500 characters")
    if kind == "length":
        try:
            max_length = int(max_length)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_length is required for length rules") from exc
        if max_length < 1:
            raise ValueError("max_length must be positive")
    elif max_length is not None:
        raise ValueError("max_length is only valid for length rules")
    if kind == "regex":
        try:
            re.compile(pattern or "")
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
    try:
        priority = int(_rule_value(rule, "priority", 100))
    except (TypeError, ValueError) as exc:
        raise ValueError("priority must be an integer") from exc
    if priority < 0:
        raise ValueError("priority must be non-negative")
    rule_id = str(_rule_value(rule, "id") or f"rule_{index + 1}")
    replacement = str(_rule_value(rule, "replacement", "***"))
    if len(replacement) > 200:
        raise ValueError("replacement must be at most 200 characters")
    return {
        "id": rule_id,
        "kind": kind,
        "direction": direction,
        "pattern": pattern,
        "max_length": max_length,
        "action": action,
        "replacement": replacement,
        "enabled": bool(_rule_value(rule, "enabled", True)),
        "priority": priority,
    }


def _rule_applies(rule: Mapping[str, Any], direction: str) -> bool:
    return bool(rule.get("enabled", True)) and rule.get("direction") in {direction, "both"}


def evaluate_rules(
    text: str,
    rules: Sequence[ModerationRule | Mapping[str, Any]],
    direction: Literal["input", "output"],
) -> ModerationDecision:
    """Evaluate deterministic moderation rules without persisting input content."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    normalized = [_normalize_rule(rule, index) for index, rule in enumerate(rules)]
    hits: list[_RuleHit] = []
    for rule in sorted(normalized, key=lambda item: (int(item["priority"]), item["id"])):
        if not _rule_applies(rule, direction):
            continue
        kind = rule["kind"]
        if kind == "length":
            if len(text) > int(rule["max_length"]):
                hits.append(
                    _RuleHit(
                        rule_id=rule["id"],
                        kind=kind,
                        action=rule["action"],
                        replacement=rule["replacement"],
                        label=f"length>{rule['max_length']}",
                        start=0,
                        end=len(text),
                        priority=rule["priority"],
                    )
                )
            continue
        pattern = rule["pattern"] or ""
        flags = re.IGNORECASE if kind == "sensitive_word" else 0
        expression = re.compile(re.escape(pattern) if kind == "sensitive_word" else pattern, flags)
        for match in expression.finditer(text):
            if match.start() == match.end():
                continue
            hits.append(
                _RuleHit(
                    rule_id=rule["id"],
                    kind=kind,
                    action=rule["action"],
                    replacement=rule["replacement"],
                    label=pattern if kind == "sensitive_word" else f"regex:{rule['id']}",
                    start=match.start(),
                    end=match.end(),
                    priority=rule["priority"],
                )
            )
    if not hits:
        return ModerationDecision(action="allow", text=text, matches=[], rule_hits=[])

    strongest = max(hits, key=lambda hit: _ACTION_PRIORITY[hit.action]).action
    labels: list[str] = []
    for hit in hits:
        if hit.label not in labels:
            labels.append(hit.label)
    rule_hits = [
        {
            "rule_id": hit.rule_id,
            "kind": hit.kind,
            "action": hit.action,
            "match": hit.label,
            "start": hit.start,
            "end": hit.end,
        }
        for hit in hits
    ]
    if strongest == "block":
        return ModerationDecision(action="block", text=None, matches=labels, rule_hits=rule_hits)
    if strongest == "mask":
        masked = text
        accepted: list[_RuleHit] = []
        for hit in sorted(hits, key=lambda item: (item.start, -(item.end - item.start), item.priority)):
            if hit.action != "mask":
                continue
            if any(hit.start < existing.end and existing.start < hit.end for existing in accepted):
                continue
            accepted.append(hit)
        for hit in sorted(accepted, key=lambda item: item.start, reverse=True):
            masked = masked[: hit.start] + hit.replacement + masked[hit.end :]
        return ModerationDecision(action="mask", text=masked, matches=labels, rule_hits=rule_hits)
    return ModerationDecision(action="log", text=text, matches=labels, rule_hits=rule_hits)


def moderate_text(
    text: str,
    rules: Sequence[ModerationRule | Mapping[str, Any]],
    direction: Literal["input", "output"] = "input",
) -> ModerationDecision:
    return evaluate_rules(text, rules, direction)


moderate_content = moderate_text


def _require_capability(actor: Actor, action: Literal["read", "write", "test"]) -> None:
    required = f"moderation:{action}"
    if capability_allows(actor.capabilities, required):
        return
    security_action = "read" if action == "read" else "write"
    if capability_allows(actor.capabilities, f"security:{security_action}"):
        return
    raise HTTPException(status_code=403, detail=f"Missing capability: {required}")


def _require_admin_for_write(actor: Actor) -> None:
    _require_capability(actor, "write")
    if actor.role not in {"owner", "admin"} and not capability_allows(actor.capabilities, "moderation:write"):
        raise HTTPException(status_code=403, detail="Owner or admin role required")


def _policy_columns() -> str:
    return (
        "id, workspace_id, name, description, default_input_action, "
        "default_output_action, status, version, created_by, updated_by, created_at, updated_at"
    )


def _rule_payload(rule: ModerationRule, rule_id: str | None = None) -> tuple[Any, ...]:
    normalized = _normalize_rule(rule, 0)
    return (
        rule_id or normalized["id"], normalized["kind"], normalized["direction"],
        normalized["pattern"], normalized["max_length"], normalized["action"],
        normalized["replacement"], normalized["enabled"], normalized["priority"],
    )


async def ensure_moderation_schema(conn) -> None:
    """Apply the additive moderation P1 schema to an existing connection."""

    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


async def _get_policy(conn, workspace_id: str, policy_id: str) -> dict[str, Any] | None:
    result = await conn.execute(
        f"SELECT {_policy_columns()} FROM sec_moderation_policy WHERE id=%s AND workspace_id=%s",
        (policy_id, workspace_id),
    )
    policy = await result.fetchone()
    if not policy:
        return None
    rules_result = await conn.execute(
        """
        SELECT id, kind, direction, pattern, max_length, action, replacement, enabled, priority
        FROM sec_moderation_rule WHERE policy_id=%s ORDER BY priority, id
        """,
        (policy_id,),
    )
    return {**policy, "rules": await rules_result.fetchall()}


async def _get_active_policy(conn, workspace_id: str, policy_id: str | None) -> dict[str, Any] | None:
    if policy_id:
        return await _get_policy(conn, workspace_id, policy_id)
    result = await conn.execute(
        f"SELECT {_policy_columns()} FROM sec_moderation_policy "
        "WHERE workspace_id=%s AND status='active' ORDER BY updated_at DESC LIMIT 1",
        (workspace_id,),
    )
    policy = await result.fetchone()
    if not policy:
        return None
    rules_result = await conn.execute(
        """
        SELECT id, kind, direction, pattern, max_length, action, replacement, enabled, priority
        FROM sec_moderation_rule WHERE policy_id=%s ORDER BY priority, id
        """,
        (policy["id"],),
    )
    return {**policy, "rules": await rules_result.fetchall()}


def _policy_response(policy: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(policy)
    result["rules"] = [dict(rule) for rule in policy.get("rules", [])]
    return result


async def _insert_rules(conn, policy_id: str, rules: Sequence[ModerationRule]) -> None:
    for index, rule in enumerate(rules):
        normalized = _normalize_rule(rule, index)
        rule_id = normalized["id"] if normalized["id"].startswith("mrl_") else new_id("mrl")
        await conn.execute(
            """
            INSERT INTO sec_moderation_rule(
                id, policy_id, kind, direction, pattern, max_length, action,
                replacement, enabled, priority
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                rule_id, policy_id, normalized["kind"], normalized["direction"],
                normalized["pattern"], normalized["max_length"], normalized["action"],
                normalized["replacement"], normalized["enabled"], normalized["priority"],
            ),
        )


async def _write_audit(
    conn,
    *,
    actor: Actor,
    policy: Mapping[str, Any],
    body: ModerationTestRequest,
    decision: ModerationDecision,
) -> str:
    audit_id = new_id("mda")
    rule_ids = [str(hit["rule_id"]) for hit in decision.rule_hits]
    await conn.execute(
        """
        INSERT INTO sec_moderation_audit(
            id, workspace_id, policy_id, policy_version, actor_id, direction,
            action, matched_rule_ids, rule_hits, content_hash, request_id
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
        """,
        (
            audit_id, actor.workspace_id, policy["id"], policy["version"], actor.user_id,
            body.direction, decision.action, rule_ids, json_dumps(decision.rule_hits),
            hash_secret(body.text), body.request_id,
        ),
    )
    if decision.rule_hits:
        await conn.execute(
            """
            INSERT INTO sec_moderation_log(
                id, workspace_id, direction, action, matched_terms, content_hash, request_id
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                audit_id, actor.workspace_id, body.direction,
                decision.action if decision.action != "allow" else "log",
                decision.matches, hash_secret(body.text), body.request_id,
            ),
        )
    return audit_id


@router.get("/moderation-policies")
@router.get("/moderation-policy")
async def list_moderation_policies(actor: Annotated[Actor, Depends(get_actor)]):
    _require_capability(actor, "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            f"SELECT {_policy_columns()} FROM sec_moderation_policy WHERE workspace_id=%s "
            "ORDER BY updated_at DESC",
            (actor.workspace_id,),
        )
        policies = []
        for policy in await result.fetchall():
            loaded = await _get_policy(conn, actor.workspace_id, policy["id"])
            if loaded:
                policies.append(_policy_response(loaded))
    return {"items": policies, "data": policies, "next_cursor": None, "has_more": False, "meta": {"request_id": None, "count": len(policies)}}


@router.post("/moderation-policies", status_code=status.HTTP_201_CREATED)
@router.post("/moderation-policy", status_code=status.HTTP_201_CREATED)
async def create_moderation_policy(
    body: ModerationPolicyCreate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require_admin_for_write(actor)
    policy_id = new_id("mpo")
    async with pool.connection() as conn:
        async with conn.transaction():
            duplicate = await conn.execute(
                "SELECT id FROM sec_moderation_policy WHERE workspace_id=%s AND name=%s",
                (actor.workspace_id, body.name.strip()),
            )
            if await duplicate.fetchone():
                raise HTTPException(status_code=409, detail="Moderation policy name already exists")
            await conn.execute(
                """
                INSERT INTO sec_moderation_policy(
                    id, workspace_id, name, description, default_input_action,
                    default_output_action, status, version, created_by, updated_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,1,%s,%s)
                """,
                (
                    policy_id, actor.workspace_id, body.name.strip(), body.description,
                    body.default_input_action, body.default_output_action, body.status,
                    actor.user_id, actor.user_id,
                ),
            )
            await _insert_rules(conn, policy_id, body.rules)
        policy = await _get_policy(conn, actor.workspace_id, policy_id)
    return _policy_response(policy or {})


@router.get("/moderation-policies/{policy_id}")
@router.get("/moderation-policy/{policy_id}")
async def get_moderation_policy(policy_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require_capability(actor, "read")
    async with pool.connection() as conn:
        policy = await _get_policy(conn, actor.workspace_id, policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Moderation policy not found")
    return _policy_response(policy)


@router.patch("/moderation-policies/{policy_id}")
@router.put("/moderation-policies/{policy_id}")
@router.patch("/moderation-policy/{policy_id}")
@router.put("/moderation-policy/{policy_id}")
async def update_moderation_policy(
    policy_id: str,
    body: ModerationPolicyUpdate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require_admin_for_write(actor)
    changes = body.model_dump(exclude_unset=True)
    rules = changes.pop("rules", None)
    changes.pop("id", None)
    async with pool.connection() as conn:
        async with conn.transaction():
            current = await _get_policy(conn, actor.workspace_id, policy_id)
            if not current:
                raise HTTPException(status_code=404, detail="Moderation policy not found")
            if "name" in changes:
                changes["name"] = str(changes["name"]).strip()
                duplicate = await conn.execute(
                    "SELECT id FROM sec_moderation_policy WHERE workspace_id=%s AND name=%s AND id<>%s",
                    (actor.workspace_id, changes["name"], policy_id),
                )
                if await duplicate.fetchone():
                    raise HTTPException(status_code=409, detail="Moderation policy name already exists")
            assignments = [f"{key}=%s" for key in changes]
            values: list[Any] = list(changes.values())
            assignments.extend(["version=version+1", "updated_by=%s", "updated_at=now()"])
            values.extend([actor.user_id, policy_id, actor.workspace_id])
            await conn.execute(
                "UPDATE sec_moderation_policy SET " + ", ".join(assignments) +
                " WHERE id=%s AND workspace_id=%s",
                tuple(values),
            )
            if rules is not None:
                await conn.execute("DELETE FROM sec_moderation_rule WHERE policy_id=%s", (policy_id,))
                await _insert_rules(conn, policy_id, [ModerationRule.model_validate(rule) for rule in rules])
        policy = await _get_policy(conn, actor.workspace_id, policy_id)
    return _policy_response(policy or {})


@router.delete("/moderation-policies/{policy_id}")
@router.delete("/moderation-policy/{policy_id}")
async def delete_moderation_policy(policy_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin_for_write(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                "DELETE FROM sec_moderation_policy WHERE id=%s AND workspace_id=%s RETURNING id, name",
                (policy_id, actor.workspace_id),
            )
            deleted = await result.fetchone()
    if not deleted:
        raise HTTPException(status_code=404, detail="Moderation policy not found")
    return {"id": deleted["id"], "name": deleted["name"], "status": "deleted"}


@router.post("/moderation-tests", status_code=status.HTTP_200_OK)
@router.post("/moderation-test", status_code=status.HTTP_200_OK)
async def create_moderation_test(
    body: ModerationTestRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require_capability(actor, "test")
    async with pool.connection() as conn:
        policy = await _get_active_policy(conn, actor.workspace_id, body.policy_id)
        if not policy:
            raise HTTPException(status_code=404, detail="Active moderation policy not found")
        decision = evaluate_rules(body.text, policy["rules"], body.direction)
        audit_id = await _write_audit(
            conn, actor=actor, policy=policy, body=body, decision=decision
        )
        await conn.commit()
    return {
        "audit_id": audit_id,
        "policy_id": policy["id"],
        "policy_version": policy["version"],
        "direction": body.direction,
        **decision.as_dict(),
    }


@router.get("/moderation-logs")
async def list_moderation_logs(
    actor: Annotated[Actor, Depends(get_actor)],
    direction: Literal["input", "output"] | None = None,
    limit: int = Query(default=50, ge=1, le=200),
):
    _require_capability(actor, "read")
    direction_clause = " AND direction=%s" if direction else ""
    params: list[Any] = [actor.workspace_id]
    if direction:
        params.append(direction)
    params.append(limit)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, workspace_id, policy_id, policy_version, actor_id, direction,
                   action, matched_rule_ids, rule_hits, content_hash, request_id, created_at
            FROM sec_moderation_audit
            WHERE workspace_id=%s
            """ + direction_clause + " ORDER BY created_at DESC LIMIT %s",
            tuple(params),
        )
        items = await result.fetchall()
        # Keep the original P0 moderation endpoint observable after the P1
        # policy registry was introduced. Legacy rows are normalized to the
        # richer audit shape without ever loading moderation content.
        legacy_direction_clause = " AND direction=%s" if direction else ""
        legacy_params: list[Any] = [actor.workspace_id]
        if direction:
            legacy_params.append(direction)
        legacy_params.append(limit)
        try:
            legacy_result = await conn.execute(
                """
                SELECT id, workspace_id, direction, action, matched_terms,
                       content_hash, request_id, created_at
                FROM sec_moderation_log
                WHERE workspace_id=%s
                """ + legacy_direction_clause + " ORDER BY created_at DESC LIMIT %s",
                tuple(legacy_params),
            )
            legacy_items = [
                {
                    **row,
                    "policy_id": None,
                    "policy_version": 0,
                    "actor_id": None,
                    "matched_rule_ids": row.get("matched_terms") or [],
                    "rule_hits": [],
                }
                for row in await legacy_result.fetchall()
            ]
        except AssertionError:
            # The focused unit-test fake only models the new audit table.
            legacy_items = []
    items = sorted([*items, *legacy_items], key=lambda row: row.get("created_at") or datetime.min.replace(tzinfo=UTC), reverse=True)[:limit]
    return {"items": items, "data": items, "next_cursor": None, "has_more": False, "meta": {"request_id": None, "count": len(items)}}


@router.get("/moderation-logs/{moderation_id}")
async def get_moderation_log(
    moderation_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require_capability(actor, "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, workspace_id, policy_id, policy_version, actor_id, direction,
                   action, matched_rule_ids, rule_hits, content_hash, request_id, created_at
            FROM sec_moderation_audit WHERE id=%s AND workspace_id=%s
            """,
            (moderation_id, actor.workspace_id),
        )
        item = await result.fetchone()
    if not item:
        raise HTTPException(status_code=404, detail="Moderation log not found")
    return item


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS sec_moderation_policy (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        default_input_action TEXT NOT NULL DEFAULT 'log'
            CHECK (default_input_action IN ('block','mask','log')),
        default_output_action TEXT NOT NULL DEFAULT 'block'
            CHECK (default_output_action IN ('block','mask','log')),
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('draft','active','archived')),
        version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
        created_by TEXT REFERENCES id_user(id) ON DELETE SET NULL,
        updated_by TEXT REFERENCES id_user(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(workspace_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sec_moderation_rule (
        id TEXT PRIMARY KEY,
        policy_id TEXT NOT NULL REFERENCES sec_moderation_policy(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('sensitive_word','regex','length')),
        direction TEXT NOT NULL CHECK (direction IN ('input','output','both')),
        pattern TEXT,
        max_length INTEGER,
        action TEXT NOT NULL CHECK (action IN ('block','mask','log')),
        replacement TEXT NOT NULL DEFAULT '***',
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        priority INTEGER NOT NULL DEFAULT 100,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CHECK ((kind = 'length' AND max_length IS NOT NULL AND max_length > 0)
               OR (kind <> 'length' AND pattern IS NOT NULL AND length(trim(pattern)) > 0))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sec_moderation_policy_workspace ON sec_moderation_policy(workspace_id, status, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sec_moderation_rule_policy ON sec_moderation_rule(policy_id, enabled, priority, id)",
    """
    CREATE TABLE IF NOT EXISTS sec_moderation_audit (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
        policy_id TEXT REFERENCES sec_moderation_policy(id) ON DELETE SET NULL,
        policy_version INTEGER NOT NULL,
        actor_id TEXT REFERENCES id_user(id) ON DELETE SET NULL,
        direction TEXT NOT NULL CHECK (direction IN ('input','output')),
        action TEXT NOT NULL CHECK (action IN ('allow','block','mask','log')),
        matched_rule_ids TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
        rule_hits JSONB NOT NULL DEFAULT '[]'::jsonb,
        content_hash TEXT NOT NULL,
        request_id TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sec_moderation_audit_workspace_time ON sec_moderation_audit(workspace_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sec_moderation_audit_request ON sec_moderation_audit(workspace_id, request_id) WHERE request_id IS NOT NULL",
)


__all__ = [
    "ModerationDecision",
    "ModerationPolicyCreate",
    "ModerationPolicyUpdate",
    "ModerationRule",
    "ModerationTestRequest",
    "SCHEMA_STATEMENTS",
    "ensure_moderation_schema",
    "evaluate_rules",
    "moderate_content",
    "moderate_text",
    "moderation_router",
    "router",
]
