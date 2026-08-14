from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from workama_platform.core import (
    Actor,
    get_actor,
    hash_secret,
    json_dumps,
    new_id,
    pool,
    require_internal,
)
from workama_platform.modules.moderation_model import ModerationModelConfig, moderate_with_model
from workama_platform.modules.security.service import ModerationResult, evaluate_prompt, moderate_text

router = APIRouter(prefix="/api/v1/security", tags=["security"])
internal_router = APIRouter(prefix="/internal/security", tags=["security-internal"])


class PolicyUpdate(BaseModel):
    input_action: Literal["block", "mask", "log"] = "log"
    output_action: Literal["block", "mask", "log"] = "block"
    blocked_terms: list[str] = Field(default_factory=list, max_length=100)
    autonomy_level: Literal["A1", "A2", "A3", "A4"] = "A2"
    domain_allowlist: list[str] = Field(default_factory=list, max_length=100)
    domain_denylist: list[str] = Field(default_factory=list, max_length=100)


class ModerateRequest(BaseModel):
    workspace_id: str
    direction: Literal["input", "output"]
    text: str = Field(max_length=1_000_000)
    request_id: str | None = None


class PromptCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=100_000)


def _model_moderation_enabled() -> bool:
    return os.getenv("WORKAMA_MODERATION_MODEL_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _require_admin(actor: Actor) -> None:
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")


async def _ensure_policy(conn, workspace_id: str, user_id: str | None = None) -> None:
    await conn.execute(
        """
        INSERT INTO sec_policy(id, workspace_id, updated_by)
        VALUES (%s, %s, %s)
        ON CONFLICT(workspace_id) DO NOTHING
        """,
        (new_id("pol"), workspace_id, user_id),
    )


@router.get("/policy")
async def get_policy(actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        await _ensure_policy(conn, actor.workspace_id, actor.user_id)
        result = await conn.execute(
            """
            SELECT input_action, output_action, blocked_terms, autonomy_level,
                   domain_allowlist, domain_denylist, updated_at
            FROM sec_policy WHERE workspace_id = %s
            """,
            (actor.workspace_id,),
        )
        row = await result.fetchone()
        await conn.commit()
    return row


@router.put("/policy")
async def update_policy(
    body: PolicyUpdate, actor: Annotated[Actor, Depends(get_actor)]
):
    _require_admin(actor)
    terms = sorted({term.strip().lower() for term in body.blocked_terms if term.strip()})
    if any(len(term) > 120 for term in terms):
        raise HTTPException(status_code=422, detail="Blocked terms must be at most 120 characters")
    async with pool.connection() as conn:
        await _ensure_policy(conn, actor.workspace_id, actor.user_id)
        await conn.execute(
            """
            UPDATE sec_policy SET input_action = %s, output_action = %s,
                blocked_terms = %s, autonomy_level = %s, domain_allowlist = %s,
                domain_denylist = %s, updated_by = %s, updated_at = now()
            WHERE workspace_id = %s
            """,
            (
                body.input_action, body.output_action, terms, body.autonomy_level,
                body.domain_allowlist, body.domain_denylist, actor.user_id, actor.workspace_id,
            ),
        )
        await conn.commit()
    return await get_policy(actor)


@internal_router.post("/moderate", dependencies=[Depends(require_internal)])
async def moderate(body: ModerateRequest):
    async with pool.connection() as conn:
        await _ensure_policy(conn, body.workspace_id)
        result = await conn.execute(
            "SELECT input_action, output_action, blocked_terms FROM sec_policy WHERE workspace_id = %s",
            (body.workspace_id,),
        )
        policy = await result.fetchone()
        action = policy[f"{body.direction}_action"]
        decision = moderate_text(body.text, policy["blocked_terms"], action)
        model_review = None
        if _model_moderation_enabled():
            model_review = await moderate_with_model(
                body.text,
                body.direction,
                config=ModerationModelConfig.from_env(),
            )
            matches = list(decision.matches)
            matches.extend(f"model:{category}" for category in model_review.categories)
            if model_review.failed_closed and not matches:
                matches.append(f"model:{model_review.reason or 'failed_closed'}")
            matches = sorted(set(matches))
            if model_review.action == "block":
                decision = ModerationResult(action="block", text="", matches=matches)
            elif model_review.action == "mask" and decision.action != "block":
                decision = ModerationResult(action="mask", text=model_review.text or decision.text, matches=matches)
            elif matches:
                decision = replace(decision, matches=matches)
        if decision.matches:
            await conn.execute(
                """
                INSERT INTO sec_moderation_log(
                    id, workspace_id, direction, action, matched_terms, content_hash, request_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    new_id("mod"), body.workspace_id, body.direction, decision.action,
                    decision.matches, hash_secret(body.text), body.request_id,
                ),
            )
        await conn.commit()
    response = {"action": decision.action, "text": decision.text, "matches": decision.matches}
    if model_review is not None:
        response["model_review"] = {
            "action": model_review.action,
            "provider": model_review.provider,
            "model": model_review.model,
            "model_version_hash": model_review.model_version_hash,
            "failed_closed": model_review.failed_closed,
            "reason": model_review.reason,
        }
    return response


@router.get("/prompts")
async def list_prompts(actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT p.id, p.name, p.version, p.content, p.checksum, p.status,
                   p.created_at, p.published_at,
                   e.status AS eval_status, e.failures AS eval_failures
            FROM sec_prompt_version p
            LEFT JOIN LATERAL (
                SELECT status, failures FROM sec_eval_run
                WHERE prompt_version_id = p.id ORDER BY created_at DESC LIMIT 1
            ) e ON TRUE
            WHERE p.workspace_id = %s ORDER BY p.name, p.version DESC
            """,
            (actor.workspace_id,),
        )
        # Contract《720》listSecurityPrompts: ListQuery -> ListResponse<SecurityPromptVersionDTO>
        data = list(await result.fetchall())
        return {
            "items": data,
            "data": data,
            "next_cursor": None,
            "has_more": False,
            "meta": {"request_id": None, "count": len(data)},
        }


@router.post("/prompts", status_code=201)
async def create_prompt(
    body: PromptCreate, actor: Annotated[Actor, Depends(get_actor)]
):
    _require_admin(actor)
    prompt_id = new_id("prm")
    checksum = hashlib.sha256(body.content.encode()).hexdigest()
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT COALESCE(max(version), 0) + 1 AS version FROM sec_prompt_version WHERE workspace_id = %s AND name = %s",
            (actor.workspace_id, body.name.strip()),
        )
        version = (await result.fetchone())["version"]
        await conn.execute(
            """
            INSERT INTO sec_prompt_version(id, workspace_id, name, version, content, checksum, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (prompt_id, actor.workspace_id, body.name.strip(), version, body.content, checksum, actor.user_id),
        )
        await conn.commit()
    return {"id": prompt_id, "name": body.name.strip(), "version": version, "status": "draft"}


@router.post("/prompts/{prompt_id}/evaluate")
async def evaluate_prompt_version(
    prompt_id: str, actor: Annotated[Actor, Depends(get_actor)]
):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT content FROM sec_prompt_version WHERE id = %s AND workspace_id = %s",
            (prompt_id, actor.workspace_id),
        )
        prompt = await result.fetchone()
        if not prompt:
            raise HTTPException(status_code=404, detail="Prompt version not found")
        evaluation = evaluate_prompt(prompt["content"])
        run_id = new_id("evl")
        await conn.execute(
            """
            INSERT INTO sec_eval_run(id, workspace_id, prompt_version_id, status, total_cases, passed_cases, failures)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                run_id, actor.workspace_id, prompt_id,
                "passed" if evaluation.passed else "failed", evaluation.total_cases,
                evaluation.total_cases - len(evaluation.failures), json_dumps(evaluation.failures),
            ),
        )
        await conn.commit()
    return {
        "id": run_id, "status": "passed" if evaluation.passed else "failed",
        "total_cases": evaluation.total_cases,
        "passed_cases": evaluation.total_cases - len(evaluation.failures),
        "failures": evaluation.failures,
    }


@router.post("/prompts/{prompt_id}/publish")
async def publish_prompt(prompt_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT p.name, p.status,
                   (SELECT status FROM sec_eval_run e WHERE e.prompt_version_id = p.id ORDER BY created_at DESC LIMIT 1) AS eval_status
            FROM sec_prompt_version p WHERE p.id = %s AND p.workspace_id = %s FOR UPDATE
            """,
            (prompt_id, actor.workspace_id),
        )
        prompt = await result.fetchone()
        if not prompt:
            raise HTTPException(status_code=404, detail="Prompt version not found")
        if prompt["eval_status"] != "passed":
            raise HTTPException(status_code=409, detail="Prompt must pass the latest safety evaluation")
        async with conn.transaction():
            await conn.execute(
                "UPDATE sec_prompt_version SET status = 'archived' WHERE workspace_id = %s AND name = %s AND status = 'published'",
                (actor.workspace_id, prompt["name"]),
            )
            await conn.execute(
                "UPDATE sec_prompt_version SET status = 'published', published_at = now() WHERE id = %s",
                (prompt_id,),
            )
    return {"id": prompt_id, "status": "published"}
