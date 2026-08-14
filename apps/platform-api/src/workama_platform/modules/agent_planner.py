from __future__ import annotations

import hashlib
import json
import os
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from workama_platform.core import Actor, capability_allows, get_actor, json_dumps, new_id, pool

router = APIRouter(prefix="/api/v1/agent/planner", tags=["agent-planner"])


def _require(actor: Actor, capability: str) -> None:
    if not capability_allows(actor.capabilities, capability):
        raise HTTPException(status_code=403, detail=f"Missing capability: {capability}")


class PlannerSessionCreate(BaseModel):
    context: dict[str, Any] = Field(default_factory=dict)
    plan: dict[str, Any] = Field(default_factory=dict)
    budget_limit: float = Field(default=0.0, ge=0)


class PlannerStepCreate(BaseModel):
    action: str = Field(min_length=1, max_length=4000)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    next_choices: list[str] = Field(default_factory=list)
    cost: float = Field(default=0.0, ge=0)

    @field_validator("action")
    @classmethod
    def normalize_action(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("action is required")
        return value


class PlannerSessionFork(BaseModel):
    context: dict[str, Any] = Field(default_factory=dict)
    plan: dict[str, Any] = Field(default_factory=dict)
    budget_limit: float = Field(default=0.0, ge=0)


class PlannerConvergeCheck(BaseModel):
    threshold: float = Field(default=0.9, ge=0.0, le=1.0)


class CheckpointCreate(BaseModel):
    label: str | None = Field(default=None, max_length=200)
    current_step: int = Field(default=0, ge=0)
    executed_steps: list[str] = Field(default_factory=list)
    context_snapshot: dict[str, Any] = Field(default_factory=dict)


class SubsessionResult(BaseModel):
    """子会话结果回传父会话的正式协议（result schema）。"""
    status: Literal["success", "failed", "timeout", "cancelled"] = "success"
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    latency_ms: int = Field(default=0, ge=0)


# T-M4-005: 子会话协调器硬化配置（环境变量可配）
DEFAULT_MAX_SUBSESSION_DEPTH = 5
DEFAULT_SUBSESSION_TIMEOUT_SECONDS = 300


def _max_subsession_depth() -> int:
    """读取子会话最大嵌套深度（环境变量 ``WORKAMA_PLANNER_MAX_SUBSESSION_DEPTH``）。"""
    raw = os.getenv("WORKAMA_PLANNER_MAX_SUBSESSION_DEPTH", str(DEFAULT_MAX_SUBSESSION_DEPTH)).strip()
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_SUBSESSION_DEPTH
    return val if val > 0 else DEFAULT_MAX_SUBSESSION_DEPTH


def _subsession_timeout_seconds() -> int:
    """读取子会话超时秒数（环境变量 ``WORKAMA_PLANNER_SUBSESSION_TIMEOUT``）。"""
    raw = os.getenv("WORKAMA_PLANNER_SUBSESSION_TIMEOUT", str(DEFAULT_SUBSESSION_TIMEOUT_SECONDS)).strip()
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_SUBSESSION_TIMEOUT_SECONDS
    return val if val > 0 else DEFAULT_SUBSESSION_TIMEOUT_SECONDS


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS ag_planner_session (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      actor_id TEXT NOT NULL REFERENCES id_user(id),
      status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','completed','failed')),
      context JSONB NOT NULL DEFAULT '{}'::jsonb,
      plan JSONB NOT NULL DEFAULT '{}'::jsonb,
      iterations INTEGER NOT NULL DEFAULT 0 CHECK (iterations >= 0),
      budget_used NUMERIC(18,6) NOT NULL DEFAULT 0 CHECK (budget_used >= 0),
      budget_limit NUMERIC(18,6) NOT NULL DEFAULT 0 CHECK (budget_limit >= 0),
      parent_session_id TEXT REFERENCES ag_planner_session(id) ON DELETE SET NULL,
      convergence_score NUMERIC(5,4) DEFAULT NULL CHECK (convergence_score >= 0 AND convergence_score <= 1),
      dedup_hash TEXT DEFAULT NULL,
      metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
      planner_session_state TEXT NOT NULL DEFAULT 'active' CHECK (planner_session_state IN ('active','paused','completed','failed','recovering')),
      last_checkpoint_at TIMESTAMPTZ,
      checkpoint_data JSONB NOT NULL DEFAULT '{}'::jsonb,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ag_planner_session_workspace ON ag_planner_session(workspace_id,status,updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ag_planner_session_parent ON ag_planner_session(parent_session_id)",
    # T-M4-005: 幂等添加恢复列到既有库（CREATE TABLE IF NOT EXISTS 对已存在表是 no-op）
    "ALTER TABLE ag_planner_session ADD COLUMN IF NOT EXISTS planner_session_state TEXT NOT NULL DEFAULT 'active' CHECK (planner_session_state IN ('active','paused','completed','failed','recovering'))",
    "ALTER TABLE ag_planner_session ADD COLUMN IF NOT EXISTS last_checkpoint_at TIMESTAMPTZ",
    "ALTER TABLE ag_planner_session ADD COLUMN IF NOT EXISTS checkpoint_data JSONB NOT NULL DEFAULT '{}'::jsonb",
    "CREATE INDEX IF NOT EXISTS idx_ag_planner_session_recovering ON ag_planner_session(workspace_id,planner_session_state,updated_at DESC) WHERE planner_session_state = 'recovering'",
    """
    CREATE TABLE IF NOT EXISTS ag_planner_step (
      id TEXT PRIMARY KEY,
      session_id TEXT NOT NULL REFERENCES ag_planner_session(id) ON DELETE CASCADE,
      step_order INTEGER NOT NULL CHECK (step_order >= 0),
      action TEXT NOT NULL,
      tool_calls JSONB NOT NULL DEFAULT '[]'::jsonb,
      observations JSONB NOT NULL DEFAULT '[]'::jsonb,
      next_choices JSONB NOT NULL DEFAULT '[]'::jsonb,
      status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','skipped')),
      cost NUMERIC(18,6) NOT NULL DEFAULT 0 CHECK (cost >= 0),
      metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ag_planner_step_session ON ag_planner_step(session_id,step_order)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_ag_planner_step_session_order ON ag_planner_step(session_id,step_order)",
    """
    CREATE TABLE IF NOT EXISTS ag_planner_checkpoint (
      id TEXT PRIMARY KEY,
      session_id TEXT NOT NULL REFERENCES ag_planner_session(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      checkpoint_data JSONB NOT NULL DEFAULT '{}'::jsonb,
      size_bytes BIGINT NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
      label TEXT,
      created_by TEXT REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ag_planner_checkpoint_session ON ag_planner_checkpoint(session_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ag_planner_checkpoint_workspace ON ag_planner_checkpoint(workspace_id, created_at DESC)",
)


async def ensure_agent_planner_schema(conn) -> None:
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


def _session_public(row: dict[str, Any]) -> dict[str, Any]:
    public = dict(row)
    public["budget_used"] = float(public.get("budget_used", 0) or 0)
    public["budget_limit"] = float(public.get("budget_limit", 0) or 0)
    public["convergence_score"] = float(public["convergence_score"]) if public.get("convergence_score") is not None else None
    if "planner_session_state" not in public or public.get("planner_session_state") is None:
        public["planner_session_state"] = "active"
    if public.get("checkpoint_data") is None:
        public["checkpoint_data"] = {}
    return public


def _step_public(row: dict[str, Any]) -> dict[str, Any]:
    public = dict(row)
    public["cost"] = float(public.get("cost", 0) or 0)
    return public


def _checkpoint_public(row: dict[str, Any]) -> dict[str, Any]:
    public = dict(row)
    public["size_bytes"] = int(public.get("size_bytes", 0) or 0)
    return public


# --- Sub-session coordinator hardening --------------------------------


def detect_subsession_cycle(graph: dict[str, str | None]) -> bool:
    """对子会话嵌套调用图做深度优先循环检测。

    ``graph`` 映射 ``session_id -> parent_session_id``（``None`` 表示根）。
    返回 True 表示沿 parent 链回溯会出现环（数据损坏或异常编排导致）。
    """
    visited: set[str] = set()

    def _walk(start: str) -> bool:
        seen: set[str] = set()
        node: str | None = start
        while node is not None:
            if node in seen:
                return True
            if node in visited:
                return False
            seen.add(node)
            visited.add(node)
            node = graph.get(node)
        return False

    for session_id in graph:
        if session_id not in visited:
            if _walk(session_id):
                return True
    return False


async def get_subsession_depth(conn: Any, session_id: str) -> int:
    """沿 parent_session_id 链回溯，返回 ``session_id`` 的嵌套深度（根=0）。

    回溯过程中若检测到环则抛出 ``HTTPException(409)``，防止无限循环。
    """
    depth = 0
    seen: set[str] = {session_id}
    current: str | None = session_id
    while current is not None:
        result = await conn.execute(
            "SELECT parent_session_id FROM ag_planner_session WHERE id=%s",
            (current,),
        )
        row = await result.fetchone()
        if not row:
            break
        parent = row.get("parent_session_id")
        if parent is None:
            break
        if parent in seen:
            raise HTTPException(status_code=409, detail="Sub-session cycle detected")
        seen.add(parent)
        depth += 1
        current = parent
    return depth


def formalize_child_result(
    child_session_id: str,
    *,
    status: str = "success",
    data: dict[str, Any] | None = None,
    error: str | None = None,
    latency_ms: int = 0,
) -> dict[str, Any]:
    """将子会话结果规范为回传父会话的 result schema。"""
    result = SubsessionResult(
        status=status,  # type: ignore[arg-type]
        data=data or {},
        error=error,
        latency_ms=max(0, int(latency_ms)),
    ).model_dump()
    result["child_session_id"] = child_session_id
    return result



# --- Coordinator logic -------------------------------------------------


def build_dependency_graph(steps: list[dict[str, Any]]) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for step in steps:
        step_id = step.get("id", "")
        deps = set()
        for obs in step.get("observations") or []:
            depends_on = obs.get("depends_on") if isinstance(obs, dict) else None
            if depends_on and isinstance(depends_on, str):
                deps.add(depends_on)
        graph[step_id] = deps
    return graph


def detect_cycle(graph: dict[str, set[str]]) -> bool:
    visited: set[str] = set()
    rec_stack: set[str] = set()

    def _visit(node: str) -> bool:
        visited.add(node)
        rec_stack.add(node)
        for neighbor in graph.get(node, set()):
            if neighbor not in visited:
                if _visit(neighbor):
                    return True
            elif neighbor in rec_stack:
                return True
        rec_stack.discard(node)
        return False

    for node in graph:
        if node not in visited:
            if _visit(node):
                return True
    return False


def track_budget(budget_limit: float, steps: list[dict[str, Any]]) -> dict[str, Any]:
    used = sum(float((s.get("cost") or 0)) for s in steps)
    remaining = max(0.0, budget_limit - used)
    status: Literal["under", "at_limit", "over"] = "under"
    if budget_limit > 0:
        if remaining <= 0:
            status = "over" if used > budget_limit else "at_limit"
    return {"budget_limit": budget_limit, "budget_used": used, "remaining": remaining, "status": status}


def semantic_dedup(actions: list[str], threshold: float = 0.85) -> list[str]:
    """Simple deterministic dedup using normalized string overlap."""
    if not actions:
        return []
    unique: list[str] = []
    for action in actions:
        normalized = action.strip().lower()
        is_dup = False
        for existing in unique:
            if not existing:
                continue
            longer = max(len(normalized), len(existing))
            if longer == 0:
                continue
            distance = _levenshtein_distance(normalized, existing)
            similarity = 1.0 - (distance / longer)
            if similarity >= threshold:
                is_dup = True
                break
        if not is_dup:
            unique.append(normalized)
    return unique


def _levenshtein_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    curr = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        curr[0] = i
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev, curr = curr, prev
    return prev[len(b)]


# --- Persistence recovery helpers --------------------------------------


async def recover_session_state(session_id: str, actor: Actor) -> dict[str, Any] | None:
    """从数据库恢复 PlannerSession 的完整状态（用于 Gateway/API 重启后恢复）。"""
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT id,workspace_id,actor_id,status,context,plan,iterations,budget_used,budget_limit,
                      parent_session_id,convergence_score,dedup_hash,metadata,created_at,updated_at
               FROM ag_planner_session WHERE id=%s AND workspace_id=%s""",
            (session_id, actor.workspace_id),
        )
        row = await result.fetchone()
        if not row:
            return None
        steps_result = await conn.execute(
            """SELECT id,session_id,step_order,action,tool_calls,observations,next_choices,status,cost,metadata,created_at
               FROM ag_planner_step WHERE session_id=%s ORDER BY step_order""",
            (session_id,),
        )
        steps = [_step_public(r) for r in await steps_result.fetchall()]
    session = _session_public(row)
    session["steps"] = steps
    return session


async def bridge_child_results(
    parent_session_id: str,
    child_session_id: str,
    actor: Actor,
) -> dict[str, Any]:
    """将子会话的最终步骤结果汇总到父会话步骤。

    1. 读取子会话的所有 completed 步骤；
    2. 在父会话中插入一个汇总步骤（action='child_summary'）；
    3. 更新父会话 metadata.child_sessions。
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            child_result = await conn.execute(
                """SELECT id,workspace_id,status,context,plan,iterations,budget_used,budget_limit,
                          parent_session_id,convergence_score,dedup_hash,metadata,created_at,updated_at
                   FROM ag_planner_session WHERE id=%s AND workspace_id=%s""",
                (child_session_id, actor.workspace_id),
            )
            child_row = await child_result.fetchone()
            if not child_row:
                raise HTTPException(status_code=404, detail="Child planner session not found")
            if child_row["parent_session_id"] != parent_session_id:
                raise HTTPException(status_code=403, detail="Child session does not belong to parent")

            steps_result = await conn.execute(
                """SELECT id,action,tool_calls,observations,next_choices,status,cost,metadata
                   FROM ag_planner_step WHERE session_id=%s AND status='completed' ORDER BY step_order""",
                (child_session_id,),
            )
            child_steps = await steps_result.fetchall()

            order_result = await conn.execute(
                "SELECT COALESCE(MAX(step_order), -1) + 1 AS next_order FROM ag_planner_step WHERE session_id=%s",
                (parent_session_id,),
            )
            order_row = await order_result.fetchone()
            step_order = order_row["next_order"] if order_row else 0
            step_id = new_id("plstep")

            summary = {
                "child_session_id": child_session_id,
                "child_step_count": len(child_steps),
                "child_outputs": [
                    {
                        "action": s["action"],
                        "tool_calls": s["tool_calls"],
                        "observations": s["observations"],
                        "cost": float(s.get("cost") or 0),
                    }
                    for s in child_steps
                ],
            }

            await conn.execute(
                """INSERT INTO ag_planner_step(id,session_id,step_order,action,tool_calls,observations,next_choices,status,cost,metadata)
                   VALUES(%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,'completed',%s,%s::jsonb)""",
                (
                    step_id,
                    parent_session_id,
                    step_order,
                    "child_summary",
                    json_dumps([]),
                    json_dumps([summary]),
                    json_dumps([]),
                    sum(float(s.get("cost") or 0) for s in child_steps),
                    json_dumps({"child_session_id": child_session_id}),
                ),
            )

            parent_meta_result = await conn.execute(
                "SELECT metadata FROM ag_planner_session WHERE id=%s",
                (parent_session_id,),
            )
            parent_meta_row = await parent_meta_result.fetchone()
            parent_meta = dict(parent_meta_row["metadata"] or {})
            child_sessions: list[str] = list(parent_meta.get("child_sessions") or [])
            if child_session_id not in child_sessions:
                child_sessions.append(child_session_id)
            parent_meta["child_sessions"] = child_sessions

            await conn.execute(
                "UPDATE ag_planner_session SET metadata=%s::jsonb,updated_at=now() WHERE id=%s",
                (json_dumps(parent_meta), parent_session_id),
            )

    # T-M4-005: 以正式 result schema（status/data/error/latency_ms）回传父会话。
    started = child_row.get("created_at")
    ended = child_row.get("updated_at")
    latency_ms = 0
    if started and ended:
        try:
            latency_ms = int((ended - started).total_seconds() * 1000)
        except TypeError:
            latency_ms = 0
    child_status = "success" if child_row.get("status") == "completed" else "failed"
    result = formalize_child_result(
        child_session_id,
        status=child_status,
        data={
            "summarized_steps": len(child_steps),
            "total_cost": sum(float(s.get("cost") or 0) for s in child_steps),
        },
        error=None if child_status == "success" else f"child ended in status {child_row.get('status')}",
        latency_ms=latency_ms,
    )
    return {
        "parent_session_id": parent_session_id,
        "child_session_id": child_session_id,
        "summarized_steps": len(child_steps),
        "result": result,
    }


def compute_dedup_hash(plan: dict[str, Any]) -> str:
    """为 plan 计算去重哈希（确定性 SHA-256 前16位）。"""
    canonical = json.dumps(plan, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def check_convergence(steps: list[dict[str, Any]], threshold: float = 0.9) -> dict[str, Any]:
    """检测步骤序列是否收敛：最近两个非空 action 的相似度 >= threshold 视为收敛。"""
    actions = [s["action"] for s in steps if s.get("action")]
    if len(actions) < 2:
        return {"converged": False, "score": 0.0, "reason": "insufficient_steps"}
    a, b = actions[-2], actions[-1]
    longer = max(len(a), len(b))
    if longer == 0:
        return {"converged": False, "score": 0.0, "reason": "empty_actions"}
    distance = _levenshtein_distance(a.lower(), b.lower())
    score = 1.0 - (distance / longer)
    converged = score >= threshold
    return {"converged": converged, "score": round(score, 4), "reason": "similarity_check", "threshold": threshold}


# --- REST endpoints ----------------------------------------------------


@router.post("/sessions", status_code=201)
async def create_session(body: PlannerSessionCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:create")
    session_id = new_id("planner")
    dedup = compute_dedup_hash(body.plan)
    async with pool.connection() as conn:
        result = await conn.execute(
            """INSERT INTO ag_planner_session(id,workspace_id,actor_id,status,context,plan,budget_limit,dedup_hash,metadata)
               VALUES(%s,%s,%s,'active',%s::jsonb,%s::jsonb,%s,%s,%s::jsonb)
               RETURNING id,workspace_id,actor_id,status,context,plan,iterations,budget_used,budget_limit,parent_session_id,convergence_score,dedup_hash,metadata,created_at,updated_at""",
            (session_id, actor.workspace_id, actor.user_id, json_dumps(body.context), json_dumps(body.plan), body.budget_limit, dedup, json_dumps({})),
        )
        row = await result.fetchone()
        await conn.commit()
    return _session_public(row)


@router.get("/sessions")
async def list_sessions(
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None),
):
    _require(actor, "design:read")
    async with pool.connection() as conn:
        status_clause = ""
        params: list[object] = [actor.workspace_id]
        if status:
            status_clause = "AND status = %s"
            params.append(status)
        params.append(limit)
        params.append(offset)
        result = await conn.execute(
            f"""SELECT id,workspace_id,actor_id,status,context,plan,iterations,budget_used,budget_limit,parent_session_id,convergence_score,dedup_hash,metadata,created_at,updated_at
               FROM ag_planner_session WHERE workspace_id=%s {status_clause}
               ORDER BY updated_at DESC LIMIT %s OFFSET %s""",
            tuple(params),
        )
        rows = [_session_public(r) for r in await result.fetchall()]
        count_result = await conn.execute(
            "SELECT COUNT(*) AS total FROM ag_planner_session WHERE workspace_id=%s",
            (actor.workspace_id,),
        )
        total_row = await count_result.fetchone()
    return {
        "items": rows,
        "data": rows,
        "total": total_row["total"] if total_row else 0,
        "limit": limit,
        "offset": offset,
        "has_more": len(rows) == limit,
        "meta": {"request_id": None},
    }


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT id,workspace_id,actor_id,status,context,plan,iterations,budget_used,budget_limit,parent_session_id,convergence_score,dedup_hash,metadata,created_at,updated_at
               FROM ag_planner_session WHERE id=%s AND workspace_id=%s""",
            (session_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Planner session not found")
    return _session_public(row)


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:write")
    async with pool.connection() as conn:
        result = await conn.execute(
            "UPDATE ag_planner_session SET status='completed',updated_at=now() WHERE id=%s AND workspace_id=%s RETURNING id",
            (session_id, actor.workspace_id),
        )
        row = await result.fetchone()
        await conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Planner session not found")
    return {"id": session_id, "status": "completed"}


@router.post("/sessions/{session_id}/steps", status_code=201)
async def add_step(session_id: str, body: PlannerStepCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "design:create")
    async with pool.connection() as conn:
        session_result = await conn.execute(
            "SELECT id,status,budget_used,budget_limit FROM ag_planner_session WHERE id=%s AND workspace_id=%s",
            (session_id, actor.workspace_id),
        )
        session = await session_result.fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="Planner session not found")
        if session["status"] not in {"active", "paused"}:
            raise HTTPException(status_code=409, detail="Session is not active")
        step_order_result = await conn.execute(
            "SELECT COALESCE(MAX(step_order), -1) + 1 AS next_order FROM ag_planner_step WHERE session_id=%s",
            (session_id,),
        )
        order_row = await step_order_result.fetchone()
        step_order = order_row["next_order"] if order_row else 0
        step_id = new_id("plstep")
        new_budget = float(session.get("budget_used") or 0) + body.cost
        result = await conn.execute(
            """INSERT INTO ag_planner_step(id,session_id,step_order,action,tool_calls,observations,next_choices,status,cost,metadata)
               VALUES(%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,'completed',%s,%s::jsonb)
               RETURNING id,session_id,step_order,action,tool_calls,observations,next_choices,status,cost,metadata,created_at""",
            (step_id, session_id, step_order, body.action, json_dumps(body.tool_calls), json_dumps(body.observations), json_dumps(body.next_choices), body.cost, json_dumps({})),
        )
        row = await result.fetchone()
        await conn.execute(
            "UPDATE ag_planner_session SET iterations=iterations+1,budget_used=%s,updated_at=now() WHERE id=%s",
            (new_budget, session_id),
        )
        await conn.commit()
    return _step_public(row)


@router.get("/sessions/{session_id}/steps")
async def list_steps(
    session_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    _require(actor, "design:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT id,session_id,step_order,action,tool_calls,observations,next_choices,status,cost,metadata,created_at
               FROM ag_planner_step WHERE session_id=%s ORDER BY step_order LIMIT %s OFFSET %s""",
            (session_id, limit, offset),
        )
        rows = [_step_public(r) for r in await result.fetchall()]
    return {
        "items": rows,
        "data": rows,
        "total": len(rows),
        "limit": limit,
        "offset": offset,
        "has_more": len(rows) == limit,
        "meta": {"request_id": None},
    }


@router.post("/sessions/{session_id}/fork", status_code=201)
async def fork_session(
    session_id: str,
    body: PlannerSessionFork,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """分叉子会话：复制父会话上下文并创建新的子会话。

    T-M4-005: 强制子会话嵌套深度上限（``MAX_SUBSESSION_DEPTH``）与循环检测。
    """
    _require(actor, "design:create")
    max_depth = _max_subsession_depth()
    async with pool.connection() as conn:
        parent_result = await conn.execute(
            """SELECT id,workspace_id,actor_id,status,context,plan,budget_used,budget_limit,metadata
               FROM ag_planner_session WHERE id=%s AND workspace_id=%s""",
            (session_id, actor.workspace_id),
        )
        parent = await parent_result.fetchone()
        if not parent or parent.get("workspace_id") != actor.workspace_id:
            raise HTTPException(status_code=404, detail="Planner session not found")
        if parent["status"] not in {"active", "paused"}:
            raise HTTPException(status_code=409, detail="Parent session is not active")

        # 嵌套深度限制：父会话深度 + 1（子会话层数）不得超过上限。
        parent_depth = await get_subsession_depth(conn, session_id)
        if parent_depth + 1 > max_depth:
            raise HTTPException(
                status_code=409,
                detail=f"Sub-session nesting depth {parent_depth + 1} exceeds limit {max_depth}",
            )

        child_id = new_id("planner")
        child_context = {**(parent["context"] or {}), **body.context}
        child_plan = {**(parent["plan"] or {}), **body.plan}
        child_budget = body.budget_limit if body.budget_limit > 0 else float(parent.get("budget_limit") or 0)
        child_dedup = compute_dedup_hash(child_plan)

        result = await conn.execute(
            """INSERT INTO ag_planner_session(id,workspace_id,actor_id,status,context,plan,budget_limit,parent_session_id,dedup_hash,metadata)
               VALUES(%s,%s,%s,'active',%s::jsonb,%s::jsonb,%s,%s,%s,%s::jsonb)
               RETURNING id,workspace_id,actor_id,status,context,plan,iterations,budget_used,budget_limit,parent_session_id,convergence_score,dedup_hash,metadata,created_at,updated_at""",
            (
                child_id,
                actor.workspace_id,
                actor.user_id,
                json_dumps(child_context),
                json_dumps(child_plan),
                child_budget,
                session_id,
                child_dedup,
                json_dumps({"forked_from": session_id}),
            ),
        )
        row = await result.fetchone()
        await conn.commit()
    return _session_public(row)


@router.post("/sessions/{session_id}/converge")
async def converge_session(
    session_id: str,
    body: PlannerConvergeCheck,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """收敛检测：读取会话最近步骤，计算收敛分数并更新会话。"""
    _require(actor, "design:read")
    async with pool.connection() as conn:
        session_result = await conn.execute(
            "SELECT id,workspace_id,status FROM ag_planner_session WHERE id=%s AND workspace_id=%s",
            (session_id, actor.workspace_id),
        )
        session = await session_result.fetchone()
        if not session or session.get("workspace_id") != actor.workspace_id:
            raise HTTPException(status_code=404, detail="Planner session not found")

        steps_result = await conn.execute(
            """SELECT action FROM ag_planner_step WHERE session_id=%s ORDER BY step_order DESC LIMIT 10""",
            (session_id,),
        )
        recent_steps = [{"action": r["action"]} for r in await steps_result.fetchall()]

    convergence = check_convergence(recent_steps, threshold=body.threshold)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE ag_planner_session SET convergence_score=%s,updated_at=now() WHERE id=%s",
            (convergence["score"] if convergence["converged"] else None, session_id),
        )
        await conn.commit()

    return {
        "session_id": session_id,
        "converged": convergence["converged"],
        "score": convergence["score"],
        "threshold": body.threshold,
        "reason": convergence["reason"],
    }


# --- T-M4-005: Persistence recovery helpers & worker -------------------


# Worker job 类型常量（platform-worker 通过 job 队列路由恢复任务）
PLANNER_RECOVERY_JOB_TYPE = "agent_planner_recovery"

_SESSION_RECOVERY_COLUMNS = (
    "id,workspace_id,actor_id,status,context,plan,iterations,budget_used,budget_limit,"
    "parent_session_id,convergence_score,dedup_hash,metadata,planner_session_state,"
    "last_checkpoint_at,checkpoint_data,created_at,updated_at"
)


def _build_checkpoint_data(session_row: dict[str, Any], body: CheckpointCreate) -> dict[str, Any]:
    """从会话行与请求体构建完整可恢复的 checkpoint_data。"""
    return {
        "session_status": session_row.get("status"),
        "planner_session_state": session_row.get("planner_session_state") or "active",
        "current_step": body.current_step,
        "executed_steps": list(body.executed_steps),
        "context_snapshot": {**(session_row.get("context") or {}), **body.context_snapshot},
        "plan": session_row.get("plan") or {},
        "iterations": int(session_row.get("iterations") or 0),
        "budget_used": float(session_row.get("budget_used") or 0),
        "budget_limit": float(session_row.get("budget_limit") or 0),
        "label": body.label,
    }


async def _latest_checkpoint(conn: Any, session_id: str, workspace_id: str) -> dict[str, Any] | None:
    result = await conn.execute(
        """SELECT id,session_id,workspace_id,checkpoint_data,size_bytes,label,created_by,created_at
           FROM ag_planner_checkpoint WHERE session_id=%s AND workspace_id=%s
           ORDER BY created_at DESC LIMIT 1""",
        (session_id, workspace_id),
    )
    return await result.fetchone()


class PlannerRecoveryWorker:
    """持久化恢复 Worker：扫描 ``planner_session_state='recovering'`` 的会话并从最近
    检查点恢复执行。

    供 platform-worker 在重启时通过 job 队列调用：
    ``await worker.process_recovery_job({"workspace_id": "wsp_xxx"})``
    payload.workspace_id 为空时扫描全表。
    """

    async def scan_recovering_sessions(self, workspace_id: str | None = None) -> list[dict[str, Any]]:
        async with pool.connection() as conn:
            if workspace_id:
                result = await conn.execute(
                    f"""SELECT {_SESSION_RECOVERY_COLUMNS}
                        FROM ag_planner_session
                        WHERE workspace_id=%s AND planner_session_state='recovering'
                        ORDER BY updated_at ASC""",
                    (workspace_id,),
                )
            else:
                result = await conn.execute(
                    f"""SELECT {_SESSION_RECOVERY_COLUMNS}
                        FROM ag_planner_session
                        WHERE planner_session_state='recovering'
                        ORDER BY updated_at ASC""",
                )
            rows = await result.fetchall()
        return [_session_public(r) for r in rows]

    async def recover_session(self, session_id: str, workspace_id: str) -> dict[str, Any]:
        """从最近检查点恢复单个会话：应用 checkpoint_data 并将状态置回 ``active``。"""
        async with pool.connection() as conn:
            async with conn.transaction():
                checkpoint = await _latest_checkpoint(conn, session_id, workspace_id)
                if not checkpoint:
                    # 无检查点可恢复：保持 recovering 不变，由调用方决定后续处置。
                    return {"session_id": session_id, "recovered": False, "reason": "no_checkpoint"}
                snapshot = checkpoint.get("checkpoint_data") or {}
                restored_context = snapshot.get("context_snapshot") or snapshot.get("context") or {}
                result = await conn.execute(
                    """UPDATE ag_planner_session
                       SET context=%s::jsonb,
                           planner_session_state='active',
                           checkpoint_data=%s::jsonb,
                           updated_at=now()
                       WHERE id=%s AND workspace_id=%s
                       RETURNING """ + _SESSION_RECOVERY_COLUMNS,
                    (json_dumps(restored_context), json_dumps(snapshot), session_id, workspace_id),
                )
                row = await result.fetchone()
        if not row:
            return {"session_id": session_id, "recovered": False, "reason": "session_not_found"}
        return {
            "session_id": session_id,
            "recovered": True,
            "checkpoint_id": checkpoint["id"],
            "session": _session_public(row),
        }

    async def recover_all_recovering_sessions(self, workspace_id: str | None = None) -> dict[str, Any]:
        """扫描所有 recovering 会话并逐一恢复。返回汇总统计。"""
        sessions = await self.scan_recovering_sessions(workspace_id)
        recovered_ids: list[str] = []
        skipped: list[dict[str, Any]] = []
        for session in sessions:
            outcome = await self.recover_session(session["id"], session["workspace_id"])
            if outcome.get("recovered"):
                recovered_ids.append(session["id"])
            else:
                skipped.append({"session_id": session["id"], "reason": outcome.get("reason")})
        return {
            "scanned": len(sessions),
            "recovered": len(recovered_ids),
            "recovered_ids": recovered_ids,
            "skipped": skipped,
        }

    async def process_recovery_job(self, payload: dict) -> dict:
        """处理恢复 job（由 platform-worker 调用）。"""
        workspace_id = payload.get("workspace_id") or None
        return await self.recover_all_recovering_sessions(workspace_id)


# 模块级 Worker 实例（platform-worker 直接 import 使用）
recovery_worker = PlannerRecoveryWorker()


# --- Persistence recovery REST endpoints -------------------------------


@router.post("/sessions/{session_id}/checkpoint", status_code=201)
async def create_checkpoint(
    session_id: str,
    body: CheckpointCreate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """显式触发检查点保存：写入 ag_planner_checkpoint 并更新会话 last_checkpoint_at。"""
    _require(actor, "design:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            session_result = await conn.execute(
                f"""SELECT {_SESSION_RECOVERY_COLUMNS} FROM ag_planner_session
                    WHERE id=%s AND workspace_id=%s""",
                (session_id, actor.workspace_id),
            )
            session = await session_result.fetchone()
            if not session:
                raise HTTPException(status_code=404, detail="Planner session not found")

            checkpoint_data = _build_checkpoint_data(session, body)
            payload_json = json_dumps(checkpoint_data)
            size_bytes = len(payload_json.encode("utf-8"))
            checkpoint_id = new_id("plckpt")

            cp_result = await conn.execute(
                """INSERT INTO ag_planner_checkpoint(id,session_id,workspace_id,checkpoint_data,size_bytes,label,created_by)
                   VALUES(%s,%s,%s,%s::jsonb,%s,%s,%s)
                   RETURNING id,session_id,workspace_id,checkpoint_data,size_bytes,label,created_by,created_at""",
                (
                    checkpoint_id,
                    session_id,
                    actor.workspace_id,
                    payload_json,
                    size_bytes,
                    body.label,
                    actor.user_id,
                ),
            )
            cp_row = await cp_result.fetchone()
            await conn.execute(
                """UPDATE ag_planner_session
                   SET last_checkpoint_at=now(), checkpoint_data=%s::jsonb, updated_at=now()
                   WHERE id=%s""",
                (payload_json, session_id),
            )
    return _checkpoint_public(cp_row)


@router.get("/sessions/{session_id}/checkpoints")
async def list_checkpoints(
    session_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """列出会话的历史检查点（分页，workspace 隔离）。"""
    _require(actor, "design:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT id,session_id,workspace_id,checkpoint_data,size_bytes,label,created_by,created_at
               FROM ag_planner_checkpoint
               WHERE session_id=%s AND workspace_id=%s
               ORDER BY created_at DESC LIMIT %s OFFSET %s""",
            (session_id, actor.workspace_id, limit, offset),
        )
        rows = [_checkpoint_public(r) for r in await result.fetchall()]
        count_result = await conn.execute(
            "SELECT COUNT(*) AS total FROM ag_planner_checkpoint WHERE session_id=%s AND workspace_id=%s",
            (session_id, actor.workspace_id),
        )
        total_row = await count_result.fetchone()
    return {
        "items": rows,
        "data": rows,
        "total": total_row["total"] if total_row else 0,
        "limit": limit,
        "offset": offset,
        "has_more": len(rows) == limit,
        "meta": {"request_id": None},
    }


@router.post("/sessions/{session_id}/recover")
async def recover_session(
    session_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """从最近 checkpoint 恢复执行：先标记 recovering，再应用快照并恢复为 active。"""
    _require(actor, "design:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            session_result = await conn.execute(
                f"""SELECT {_SESSION_RECOVERY_COLUMNS} FROM ag_planner_session
                    WHERE id=%s AND workspace_id=%s""",
                (session_id, actor.workspace_id),
            )
            session = await session_result.fetchone()
            if not session:
                raise HTTPException(status_code=404, detail="Planner session not found")

            # 标记 recovering（崩溃后 worker 可据此续做）
            await conn.execute(
                "UPDATE ag_planner_session SET planner_session_state='recovering', updated_at=now() WHERE id=%s",
                (session_id,),
            )

            checkpoint = await _latest_checkpoint(conn, session_id, actor.workspace_id)
            if not checkpoint:
                raise HTTPException(status_code=404, detail="No checkpoint found for session")
            snapshot = checkpoint.get("checkpoint_data") or {}
            restored_context = snapshot.get("context_snapshot") or snapshot.get("context") or {}

            result = await conn.execute(
                """UPDATE ag_planner_session
                   SET context=%s::jsonb,
                       planner_session_state='active',
                       checkpoint_data=%s::jsonb,
                       updated_at=now()
                   WHERE id=%s AND workspace_id=%s
                   RETURNING """ + _SESSION_RECOVERY_COLUMNS,
                (json_dumps(restored_context), json_dumps(snapshot), session_id, actor.workspace_id),
            )
            row = await result.fetchone()
    return {
        "session_id": session_id,
        "recovered": True,
        "checkpoint_id": checkpoint["id"],
        "session": _session_public(row),
    }


# --- Sub-session state synchronization (parent -> children cascade) ----


@router.post("/sessions/{session_id}/pause")
async def pause_session(
    session_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """暂停会话并级联暂停所有子会话（父会话暂停时通知所有子会话）。"""
    _require(actor, "design:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            owned = await conn.execute(
                "SELECT id FROM ag_planner_session WHERE id=%s AND workspace_id=%s",
                (session_id, actor.workspace_id),
            )
            if not await owned.fetchone():
                raise HTTPException(status_code=404, detail="Planner session not found")
            result = await conn.execute(
                """WITH RECURSIVE descendants AS (
                     SELECT id FROM ag_planner_session WHERE id=%s AND workspace_id=%s
                     UNION ALL
                     SELECT s.id FROM ag_planner_session s JOIN descendants d ON s.parent_session_id=d.id
                   )
                   UPDATE ag_planner_session
                   SET planner_session_state='paused', status='paused', updated_at=now()
                   WHERE id IN (SELECT id FROM descendants) AND workspace_id=%s
                   RETURNING id""",
                (session_id, actor.workspace_id, actor.workspace_id),
            )
            paused_rows = await result.fetchall()
    paused_ids = [r["id"] for r in paused_rows]
    return {"session_id": session_id, "paused": True, "affected_ids": paused_ids, "count": len(paused_ids)}


@router.post("/sessions/{session_id}/resume")
async def resume_session(
    session_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """恢复会话并级联恢复所有子会话。"""
    _require(actor, "design:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            owned = await conn.execute(
                "SELECT id FROM ag_planner_session WHERE id=%s AND workspace_id=%s",
                (session_id, actor.workspace_id),
            )
            if not await owned.fetchone():
                raise HTTPException(status_code=404, detail="Planner session not found")
            result = await conn.execute(
                """WITH RECURSIVE descendants AS (
                     SELECT id FROM ag_planner_session WHERE id=%s AND workspace_id=%s
                     UNION ALL
                     SELECT s.id FROM ag_planner_session s JOIN descendants d ON s.parent_session_id=d.id
                   )
                   UPDATE ag_planner_session
                   SET planner_session_state='active', status='active', updated_at=now()
                   WHERE id IN (SELECT id FROM descendants) AND workspace_id=%s
                   RETURNING id""",
                (session_id, actor.workspace_id, actor.workspace_id),
            )
            resumed_rows = await result.fetchall()
    resumed_ids = [r["id"] for r in resumed_rows]
    return {"session_id": session_id, "resumed": True, "affected_ids": resumed_ids, "count": len(resumed_ids)}
