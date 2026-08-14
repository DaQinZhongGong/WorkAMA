"""知识库 RAG 评测外部连接器与标注回流模块（T-M3-003）。

本模块在知识库 schema 中实现评测集管理、评测运行、标注回流功能，
与现有 ``rag_eval`` 模块（前缀 ``/api/v1/rag``）共存，使用 ``/api/v1/knowledge``
前缀并提供更丰富的指标（retrieval_recall / retrieval_precision /
answer_relevance / context_precision）与人工标注回流能力。

设计要点：
- 评测集与 ``pf_dataset`` 一一关联，复用现有 dataset 能力权限。
- 评测运行通过 ``ops_async_operation`` 异步框架调度，由 ``rag_worker`` 拉起。
- 指标计算与检索解耦：通过 ``knowledge._retrieve_rows`` 调用现有检索链路，
  保证不破坏现有 RAG 功能。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field

from workama_platform.core import (
    Actor,
    capability_allows,
    get_actor,
    json_dumps,
    new_id,
    pool,
)
from workama_platform.modules.gateway import llm_client
from workama_platform.modules.jobs import (
    ClaimedJob,
    IdempotencyConflict,
    heartbeat,
    request_cancellation,
    submit_operation,
)

LOGGER = logging.getLogger("workama.platform-api.knowledge_eval")


router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge-evaluation"])

# 支持的指标集合
SUPPORTED_METRICS = {
    "retrieval_recall",
    "retrieval_precision",
    "answer_relevance",
    "context_precision",
}
DEFAULT_METRICS = ["retrieval_recall", "retrieval_precision", "answer_relevance"]


# ------------------------- 建表语句（金标集 + 聚合报告） -------------------------


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS rag_golden_set (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      name TEXT NOT NULL,
      description TEXT NOT NULL DEFAULT '',
      dataset_id TEXT,
      created_by TEXT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rag_golden_set_workspace ON rag_golden_set(workspace_id,created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS rag_golden_case (
      id TEXT PRIMARY KEY,
      golden_set_id TEXT NOT NULL,
      workspace_id TEXT NOT NULL,
      query TEXT NOT NULL,
      expected_answer TEXT NOT NULL DEFAULT '',
      expected_context_ids TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
      tags TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rag_golden_case_set ON rag_golden_case(golden_set_id,created_at)",
    """
    CREATE TABLE IF NOT EXISTS rag_eval_report (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      golden_set_id TEXT NOT NULL,
      eval_run_id TEXT,
      status TEXT NOT NULL DEFAULT 'running',
      hit_at_k JSONB NOT NULL DEFAULT '{}'::jsonb,
      avg_recall DOUBLE PRECISION NOT NULL DEFAULT 0.0,
      avg_precision DOUBLE PRECISION NOT NULL DEFAULT 0.0,
      avg_f1 DOUBLE PRECISION NOT NULL DEFAULT 0.0,
      avg_faithfulness DOUBLE PRECISION NOT NULL DEFAULT 0.0,
      avg_answer_relevance DOUBLE PRECISION NOT NULL DEFAULT 0.0,
      total_cases INTEGER NOT NULL DEFAULT 0,
      passed_cases INTEGER NOT NULL DEFAULT 0,
      baseline_report_id TEXT,
      summary JSONB NOT NULL DEFAULT '{}'::jsonb,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      completed_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rag_eval_report_workspace ON rag_eval_report(workspace_id,created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS rag_eval_report_case (
      id TEXT PRIMARY KEY,
      report_id TEXT NOT NULL,
      case_id TEXT NOT NULL,
      query TEXT NOT NULL,
      expected_answer TEXT NOT NULL DEFAULT '',
      actual_answer TEXT NOT NULL DEFAULT '',
      retrieved_context_ids TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
      expected_context_ids TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
      hit BOOLEAN NOT NULL DEFAULT false,
      recall DOUBLE PRECISION NOT NULL DEFAULT 0.0,
      precision DOUBLE PRECISION NOT NULL DEFAULT 0.0,
      f1 DOUBLE PRECISION NOT NULL DEFAULT 0.0,
      faithfulness DOUBLE PRECISION NOT NULL DEFAULT 0.0,
      answer_relevance DOUBLE PRECISION NOT NULL DEFAULT 0.0,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rag_eval_report_case_report ON rag_eval_report_case(report_id,created_at)",
)


async def ensure_knowledge_eval_schema(conn) -> None:
    """执行金标集与评测报告相关建表语句（幂等）。"""
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


# ------------------------- Pydantic 模型 -------------------------


class EvalSetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2_000)
    dataset_id: str = Field(min_length=1, max_length=64)
    metrics: list[str] = Field(default_factory=lambda: list(DEFAULT_METRICS))


class EvalCaseCreate(BaseModel):
    question: str = Field(min_length=1, max_length=20_000)
    expected_answer: str = Field(default="", max_length=20_000)
    expected_chunks: list[str] = Field(default_factory=list, max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=50)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvalCaseImport(BaseModel):
    items: list[EvalCaseCreate] = Field(min_length=1, max_length=500)


class EvalRunCreate(BaseModel):
    top_k: int = Field(default=5, ge=1, le=50)
    candidate_k: int = Field(default=20, ge=5, le=200)
    rrf_k: int = Field(default=60, ge=1, le=200)
    score_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    use_llm_judge: bool = False


class AnnotationCreate(BaseModel):
    case_id: str = Field(min_length=1, max_length=64)
    rating: int = Field(ge=1, le=5)
    feedback: str = Field(default="", max_length=4_000)
    corrected_answer: str = Field(default="", max_length=20_000)
    corrected_chunks: list[str] = Field(default_factory=list, max_length=200)
    labels: list[str] = Field(default_factory=list, max_length=50)


class AnnotationImport(BaseModel):
    items: list[AnnotationCreate] = Field(min_length=1, max_length=500)


class EvalRunCompareRequest(BaseModel):
    baseline_run_id: str = Field(min_length=1, max_length=64)
    candidate_run_id: str = Field(min_length=1, max_length=64)


# ------------------------- 辅助函数 -------------------------


def _require(actor: Actor, capability: str) -> None:
    """检查 actor 是否拥有指定能力，否则抛 403。"""
    if not capability_allows(actor.capabilities, capability):
        raise HTTPException(status_code=403, detail=f"Missing capability: {capability}")


def _validate_metrics(metrics: list[str]) -> list[str]:
    """校验 metrics 列表，剔除空字符串并验证是否在支持集合内。"""
    cleaned = [m.strip() for m in metrics if m and m.strip()]
    if not cleaned:
        cleaned = list(DEFAULT_METRICS)
    invalid = [m for m in cleaned if m not in SUPPORTED_METRICS]
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported metrics: {invalid}. Supported: {sorted(SUPPORTED_METRICS)}",
        )
    return cleaned


def _case_hash(case: EvalCaseCreate) -> str:
    """根据用例内容生成稳定哈希，用于去重。"""
    payload = json.dumps(case.model_dump(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _strip_code_fences(text: str) -> str:
    """去除 markdown code fence，保留内部内容。"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


async def _call_llm_judge(messages: list[dict], actor: Actor, fallback_value: float) -> float:
    """统一调用 LLM judge，30s 超时，失败时回退到 fallback_value（不抛错）。"""
    try:
        resp = await llm_client.call_llm(
            messages=messages,
            model="gpt-4o-mini",
            workspace_id=actor.workspace_id,
            actor=actor,
            timeout=30,
            temperature=0.0,
            max_tokens=256,
        )
        content = _strip_code_fences(resp.get("content", ""))
        data = json.loads(content)
        score = float(data.get("score", fallback_value))
        return max(0.0, min(1.0, score))
    except Exception:
        return fallback_value


async def _judge_faithfulness(query: str, answer: str, contexts: list[str], actor: Actor) -> float:
    """让 LLM 判断答案是否忠实于检索上下文，返回 0-1 分数。

    System prompt 要求 LLM 输出 JSON ``{"score": 0.0~1.0, "reason": "..."}``。
    """
    context_text = "\n\n".join(f"[{i + 1}] {ctx}" for i, ctx in enumerate(contexts) if ctx)
    system_prompt = (
        'You are an expert evaluator. Assess whether the answer is fully supported by '
        'the provided contexts. Rate faithfulness on a scale of 0.0 to 1.0, where 1.0 means '
        'every claim in the answer is supported by the contexts, and 0.0 means completely '
        'unsupported or contradictory. Output strictly in JSON format: {"score": 0.0~1.0, "reason": "..."}'
    )
    user_prompt = (
        f"Question: {query}\n\n"
        f"Contexts:\n{context_text}\n\n"
        f"Answer: {answer}\n\n"
        f'Respond with JSON only: {{"score": 0.0~1.0, "reason": "..."}}'
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    return await _call_llm_judge(messages, actor, fallback_value=0.0)


async def _judge_answer_relevance_llm(query: str, answer: str, actor: Actor) -> float:
    """用 LLM 判断答案与问题的相关性，返回 0-1 分数。

    与现有确定性 ``answer_relevance`` 并存，LLM 评分优先级更高。
    失败时回退到确定性计算值。
    """
    system_prompt = (
        'You are an expert evaluator. Assess how relevant the answer is to the question. '
        'Rate relevance on a scale of 0.0 to 1.0, where 1.0 means perfectly relevant and '
        '0.0 means completely irrelevant. Output strictly in JSON format: {"score": 0.0~1.0, "reason": "..."}'
    )
    user_prompt = (
        f"Question: {query}\n\n"
        f"Answer: {answer}\n\n"
        f'Respond with JSON only: {{"score": 0.0~1.0, "reason": "..."}}'
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    fallback = answer_relevance(answer, query)
    return await _call_llm_judge(messages, actor, fallback_value=fallback)


async def _dataset(conn, dataset_id: str, workspace_id: str) -> dict[str, Any]:
    """获取数据集（必须为 active 状态）。"""
    result = await conn.execute(
        "SELECT * FROM pf_dataset WHERE id=%s AND workspace_id=%s AND status='active'",
        (dataset_id, workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return row


async def _eval_set(conn, eval_set_id: str, workspace_id: str, *, include_archived: bool = False) -> dict[str, Any]:
    """获取评测集，默认排除 archived。"""
    suffix = "" if include_archived else " AND status <> 'archived'"
    result = await conn.execute(
        f"SELECT * FROM kb_eval_set WHERE id=%s AND workspace_id=%s{suffix}",
        (eval_set_id, workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Evaluation set not found")
    return row


async def _eval_run(conn, run_id: str, workspace_id: str) -> dict[str, Any]:
    """获取评测运行记录。"""
    result = await conn.execute(
        "SELECT * FROM kb_eval_run WHERE id=%s AND workspace_id=%s",
        (run_id, workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Evaluation run not found")
    return row


def _set_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "dataset_id": row["dataset_id"],
        "name": row["name"],
        "description": row["description"],
        "metrics": row["metrics"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _case_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "eval_set_id": row["eval_set_id"],
        "question": row["question"],
        "expected_answer": row["expected_answer"],
        "expected_chunks": row["expected_chunks"],
        "tags": row["tags"],
        "metadata": row["metadata"],
        "created_at": row["created_at"],
    }


def _run_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "eval_set_id": row["eval_set_id"],
        "dataset_id": row["dataset_id"],
        "operation_id": row["operation_id"],
        "status": row["status"],
        "config": row["config"],
        "metrics_summary": row["metrics_summary"],
        "error": row.get("error"),
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "created_at": row["created_at"],
    }


def _result_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "case_id": row["case_id"],
        "question": row["question"],
        "retrieved_chunks": row["retrieved_chunks"],
        "generated_answer": row["generated_answer"],
        "metrics": row["metrics"],
        "latency_ms": row["latency_ms"],
        "error": row.get("error"),
        "created_at": row["created_at"],
    }


def _annotation_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "case_id": row["case_id"],
        "rating": row["rating"],
        "feedback": row["feedback"],
        "corrected_answer": row["corrected_answer"],
        "corrected_chunks": row["corrected_chunks"],
        "labels": row["labels"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


async def _outbox(conn, workspace_id: str, operation_id: str, payload: dict[str, Any]) -> None:
    """写入 ops_outbox，触发后续事件分发。"""
    await conn.execute(
        "INSERT INTO ops_outbox(id,event_type,workspace_id,trace_id,payload) VALUES (%s,%s,%s,%s,%s::jsonb)",
        (new_id("out"), "kb.eval.requested.v1", workspace_id, operation_id, json_dumps(payload)),
    )


# ------------------------- 指标计算 -------------------------


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _tokenize(text: str) -> set[str]:
    """简单分词：按 Unicode 字母数字切分并小写化，过滤掉单字符噪音。"""
    if not text:
        return set()
    return {token for token in _TOKEN_RE.findall(text.lower()) if len(token) > 1}


def retrieval_recall(retrieved_ids: list[str], expected_ids: list[str]) -> float:
    """检索召回率：期望文档被命中的比例。

    - 没有期望文档时返回 1.0（视为完美召回）。
    - 否则 = |retrieved ∩ expected| / |expected|。
    """
    expected_set = {item for item in expected_ids if item}
    if not expected_set:
        return 1.0
    if not retrieved_ids:
        return 0.0
    hit = len({item for item in retrieved_ids if item} & expected_set)
    return hit / len(expected_set)


def retrieval_precision(retrieved_ids: list[str], expected_ids: list[str]) -> float:
    """检索精度：检索结果中相关文档的占比。

    - 没有 retrieved 或 expected 时返回 0.0。
    - 否则 = |retrieved ∩ expected| / |retrieved|。
    """
    retrieved_set = {item for item in retrieved_ids if item}
    expected_set = {item for item in expected_ids if item}
    if not retrieved_set or not expected_set:
        return 0.0
    hit = len(retrieved_set & expected_set)
    return hit / len(retrieved_set)


def context_precision(retrieved_ids: list[str], expected_ids: list[str]) -> float:
    """上下文精度：位置加权的相关文档得分。

    相关文档越靠前得分越高（1/rank），最后归一化到 [0, 1]。
    """
    retrieved_ids = [item for item in retrieved_ids if item]
    expected_set = {item for item in expected_ids if item}
    if not retrieved_ids or not expected_set:
        return 0.0
    score = 0.0
    for position, chunk_id in enumerate(retrieved_ids, start=1):
        if chunk_id in expected_set:
            score += 1.0 / position
    # 理论最大值：相关文档全部排在最前
    max_possible = sum(1.0 / i for i in range(1, min(len(expected_set), len(retrieved_ids)) + 1))
    return score / max_possible if max_possible > 0 else 0.0


def answer_relevance(generated: str, expected: str) -> float:
    """答案相关性：基于 token 重叠的简化 F1 评分。

    - 无期望答案时返回 1.0（视为完美匹配）。
    - 无生成答案时返回 0.0。
    - 否则计算 token 级别的 F1。
    """
    if not expected:
        return 1.0
    if not generated:
        return 0.0
    gen_tokens = _tokenize(generated)
    exp_tokens = _tokenize(expected)
    if not exp_tokens:
        return 1.0
    if not gen_tokens:
        return 0.0
    overlap = len(gen_tokens & exp_tokens)
    if overlap == 0:
        return 0.0
    precision = overlap / len(gen_tokens)
    recall = overlap / len(exp_tokens)
    return 2 * precision * recall / (precision + recall)


def _percentile(values: list[float], p: float) -> float:
    """线性插值百分位计算，p 取 0-1。"""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return float(sorted_vals[f])
    return float(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))


def aggregate_metric(values: list[float]) -> dict[str, float]:
    """聚合统计：平均分、中位数、P90。"""
    if not values:
        return {"mean": 0.0, "median": 0.0, "p90": 0.0, "count": 0}
    return {
        "mean": sum(values) / len(values),
        "median": _percentile(values, 0.5),
        "p90": _percentile(values, 0.9),
        "count": len(values),
    }


def compute_case_metrics(
    retrieved_ids: list[str],
    expected_ids: list[str],
    generated_answer: str,
    expected_answer: str,
    metrics: list[str] | None = None,
) -> dict[str, float]:
    """根据指定的指标列表计算单个用例的指标。"""
    metrics = metrics or DEFAULT_METRICS
    result: dict[str, float] = {}
    if "retrieval_recall" in metrics:
        result["retrieval_recall"] = retrieval_recall(retrieved_ids, expected_ids)
    if "retrieval_precision" in metrics:
        result["retrieval_precision"] = retrieval_precision(retrieved_ids, expected_ids)
    if "context_precision" in metrics:
        result["context_precision"] = context_precision(retrieved_ids, expected_ids)
    if "answer_relevance" in metrics:
        result["answer_relevance"] = answer_relevance(generated_answer, expected_answer)
    return result


def _fuse_rows(keyword: list[dict[str, Any]], vector: list[dict[str, Any]], *, rrf_k: int, top_k: int) -> list[dict[str, Any]]:
    """RRF 融合关键字与向量召回结果，复用 rag_eval 同款算法。"""
    scores: dict[str, dict[str, Any]] = {}
    for rank, row in enumerate(keyword, 1):
        item = scores.setdefault(row["id"], {"row": dict(row), "score": 0.0})
        item["score"] += 1 / (rrf_k + rank)
    for rank, row in enumerate(vector, 1):
        item = scores.setdefault(row["id"], {"row": dict(row), "score": 0.0})
        item["score"] += 1 / (rrf_k + rank)
    ordered = sorted(scores.values(), key=lambda item: item["score"], reverse=True)
    return [item["row"] for item in ordered[:top_k]]


# ------------------------- 评测集管理 -------------------------


@router.post("/datasets/{dataset_id}/eval-sets", status_code=201)
async def create_eval_set(
    dataset_id: str,
    body: EvalSetCreate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """创建评测集（绑定到指定 dataset）。"""
    _require(actor, "dataset:write")
    if body.dataset_id != dataset_id:
        raise HTTPException(status_code=422, detail="dataset_id in body must match path")
    metrics = _validate_metrics(body.metrics)
    async with pool.connection() as conn:
        async with conn.transaction():
            await _dataset(conn, dataset_id, actor.workspace_id)
            try:
                result = await conn.execute(
                    """
                    INSERT INTO kb_eval_set(
                      id,dataset_id,org_id,workspace_id,name,description,metrics,status,created_by
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,'active',%s) RETURNING *
                    """,
                    (
                        new_id("kbes"), dataset_id, actor.org_id, actor.workspace_id,
                        body.name.strip(), body.description, json_dumps(metrics), actor.user_id,
                    ),
                )
            except Exception as exc:
                if "unique" in str(exc).lower():
                    raise HTTPException(
                        status_code=409,
                        detail="Evaluation set with this name already exists for the dataset",
                    ) from exc
                raise
            row = await result.fetchone()
    return _set_summary(row)


@router.get("/datasets/{dataset_id}/eval-sets")
async def list_eval_sets(
    dataset_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=50, ge=1, le=200),
):
    """列出指定数据集下的评测集。"""
    _require(actor, "dataset:read")
    async with pool.connection() as conn:
        await _dataset(conn, dataset_id, actor.workspace_id)
        result = await conn.execute(
            """
            SELECT * FROM kb_eval_set
            WHERE dataset_id=%s AND workspace_id=%s AND status <> 'archived'
            ORDER BY created_at DESC LIMIT %s
            """,
            (dataset_id, actor.workspace_id, limit),
        )
        rows = await result.fetchall()
    return {"items": [_set_summary(row) for row in rows]}


@router.get("/eval-sets/{eval_set_id}")
async def get_eval_set(eval_set_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    """获取评测集详情，附带用例计数。"""
    _require(actor, "dataset:read")
    async with pool.connection() as conn:
        row = await _eval_set(conn, eval_set_id, actor.workspace_id, include_archived=True)
        count_result = await conn.execute(
            "SELECT count(*)::int AS count FROM kb_eval_case WHERE eval_set_id=%s AND status='active'",
            (eval_set_id,),
        )
        case_count = (await count_result.fetchone())["count"]
    summary = _set_summary(row)
    summary["case_count"] = case_count
    return summary


@router.delete("/eval-sets/{eval_set_id}", status_code=204)
async def delete_eval_set(eval_set_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    """归档（软删除）评测集。"""
    _require(actor, "dataset:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _eval_set(conn, eval_set_id, actor.workspace_id)
            result = await conn.execute(
                "UPDATE kb_eval_set SET status='archived',updated_at=now() WHERE id=%s AND workspace_id=%s RETURNING id",
                (eval_set_id, actor.workspace_id),
            )
            if not await result.fetchone():
                raise HTTPException(status_code=404, detail="Evaluation set not found")
    return Response(status_code=204)


# ------------------------- 评测用例管理 -------------------------


@router.post("/eval-sets/{eval_set_id}/cases", status_code=201)
async def create_eval_case(
    eval_set_id: str,
    body: EvalCaseCreate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """添加评测用例，相同内容的用例会被去重。"""
    _require(actor, "dataset:write")
    case_hash = _case_hash(body)
    async with pool.connection() as conn:
        async with conn.transaction():
            eval_set = await _eval_set(conn, eval_set_id, actor.workspace_id)
            existing_result = await conn.execute(
                "SELECT * FROM kb_eval_case WHERE eval_set_id=%s AND case_hash=%s AND status='active'",
                (eval_set_id, case_hash),
            )
            existing = await existing_result.fetchone()
            if existing:
                return _case_summary(existing)
            result = await conn.execute(
                """
                INSERT INTO kb_eval_case(
                  id,eval_set_id,dataset_id,org_id,workspace_id,question,expected_answer,
                  expected_chunks,tags,metadata,case_hash,created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s) RETURNING *
                """,
                (
                    new_id("kec"), eval_set_id, eval_set["dataset_id"], actor.org_id, actor.workspace_id,
                    body.question, body.expected_answer,
                    json_dumps(body.expected_chunks), json_dumps(body.tags), json_dumps(body.metadata),
                    case_hash, actor.user_id,
                ),
            )
            row = await result.fetchone()
            await conn.execute(
                "UPDATE kb_eval_set SET updated_at=now() WHERE id=%s",
                (eval_set_id,),
            )
    return _case_summary(row)


@router.post("/eval-sets/{eval_set_id}/cases/import", status_code=201)
async def import_eval_cases(
    eval_set_id: str,
    body: EvalCaseImport,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """批量导入评测用例（同步处理，去重）。"""
    _require(actor, "dataset:write")
    created = 0
    skipped = 0
    async with pool.connection() as conn:
        async with conn.transaction():
            eval_set = await _eval_set(conn, eval_set_id, actor.workspace_id)
            for item in body.items:
                case_hash = _case_hash(item)
                existing_result = await conn.execute(
                    "SELECT id FROM kb_eval_case WHERE eval_set_id=%s AND case_hash=%s AND status='active'",
                    (eval_set_id, case_hash),
                )
                if await existing_result.fetchone():
                    skipped += 1
                    continue
                await conn.execute(
                    """
                    INSERT INTO kb_eval_case(
                      id,eval_set_id,dataset_id,org_id,workspace_id,question,expected_answer,
                      expected_chunks,tags,metadata,case_hash,created_by
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s)
                    """,
                    (
                        new_id("kec"), eval_set_id, eval_set["dataset_id"], actor.org_id, actor.workspace_id,
                        item.question, item.expected_answer,
                        json_dumps(item.expected_chunks), json_dumps(item.tags), json_dumps(item.metadata),
                        case_hash, actor.user_id,
                    ),
                )
                created += 1
            if created:
                await conn.execute(
                    "UPDATE kb_eval_set SET updated_at=now() WHERE id=%s",
                    (eval_set_id,),
                )
    return {"created": created, "skipped": skipped}


@router.get("/eval-sets/{eval_set_id}/cases")
async def list_eval_cases(
    eval_set_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=200, ge=1, le=500),
):
    """列出评测集下的所有用例。"""
    _require(actor, "dataset:read")
    async with pool.connection() as conn:
        await _eval_set(conn, eval_set_id, actor.workspace_id)
        result = await conn.execute(
            """
            SELECT * FROM kb_eval_case
            WHERE eval_set_id=%s AND workspace_id=%s AND status='active'
            ORDER BY created_at LIMIT %s
            """,
            (eval_set_id, actor.workspace_id, limit),
        )
        rows = await result.fetchall()
    return {"items": [_case_summary(row) for row in rows]}


@router.delete("/eval-sets/{eval_set_id}/cases/{case_id}", status_code=204)
async def delete_eval_case(
    eval_set_id: str,
    case_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """删除评测用例（软删除）。"""
    _require(actor, "dataset:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _eval_set(conn, eval_set_id, actor.workspace_id)
            result = await conn.execute(
                """
                UPDATE kb_eval_case SET status='deleted',deleted_at=now(),updated_at=now()
                WHERE id=%s AND eval_set_id=%s AND workspace_id=%s AND status='active'
                RETURNING id
                """,
                (case_id, eval_set_id, actor.workspace_id),
            )
            if not await result.fetchone():
                raise HTTPException(status_code=404, detail="Evaluation case not found")
    return Response(status_code=204)


# ------------------------- 评测运行 -------------------------


@router.post("/eval-sets/{eval_set_id}/runs", status_code=202)
async def create_eval_run(
    eval_set_id: str,
    body: EvalRunCreate,
    actor: Annotated[Actor, Depends(get_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    """启动评测运行（异步任务）。"""
    _require(actor, "dataset:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            eval_set = await _eval_set(conn, eval_set_id, actor.workspace_id)
            dataset = await _dataset(conn, eval_set["dataset_id"], actor.workspace_id)
            if not dataset["active_generation_id"]:
                raise HTTPException(status_code=409, detail="E03003 Dataset index is not ready")
            case_count_result = await conn.execute(
                "SELECT count(*)::int AS count FROM kb_eval_case WHERE eval_set_id=%s AND status='active'",
                (eval_set_id,),
            )
            if (await case_count_result.fetchone())["count"] == 0:
                raise HTTPException(status_code=422, detail="Evaluation set has no active cases")
            run_id = new_id("kerun")
            config = body.model_dump()
            payload = {
                "run_id": run_id,
                "eval_set_id": eval_set_id,
                "dataset_id": eval_set["dataset_id"],
                "generation_id": dataset["active_generation_id"],
                "config": config,
                "metrics": eval_set["metrics"],
                "actor_id": actor.user_id,
                "org_id": actor.org_id,
            }
            try:
                operation = await submit_operation(
                    conn,
                    operation_type="kb.eval.run",
                    workspace_id=actor.workspace_id,
                    org_id=actor.org_id,
                    actor_id=actor.user_id,
                    actor_role=actor.role,
                    idempotency_key=idempotency_key
                    or f"kb-eval-run:{eval_set_id}:{dataset['active_generation_id']}:{body.top_k}:{body.candidate_k}",
                    payload=payload,
                    job_type="kb.eval.run",
                    queue="rag",
                    max_attempts=2,
                    priority=70,
                )
            except IdempotencyConflict as exc:
                raise HTTPException(
                    status_code=409,
                    detail="E00008 Idempotency key was already used with different input",
                ) from exc
            existing_result = await conn.execute(
                "SELECT * FROM kb_eval_run WHERE operation_id=%s AND workspace_id=%s",
                (operation["id"], actor.workspace_id),
            )
            existing = await existing_result.fetchone()
            if existing:
                return {"run": _run_summary(existing), "operation": operation}
            result = await conn.execute(
                """
                INSERT INTO kb_eval_run(
                  id,eval_set_id,dataset_id,generation_id,org_id,workspace_id,operation_id,config,status,created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'pending',%s) RETURNING *
                """,
                (
                    run_id, eval_set_id, eval_set["dataset_id"], dataset["active_generation_id"],
                    actor.org_id, actor.workspace_id, operation["id"], json_dumps(config), actor.user_id,
                ),
            )
            row = await result.fetchone()
            await _outbox(conn, actor.workspace_id, operation["id"], payload)
    return {"run": _run_summary(row), "operation": operation}


@router.get("/eval-runs")
async def list_eval_runs(
    actor: Annotated[Actor, Depends(get_actor)],
    eval_set_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
):
    """列出评测运行。"""
    _require(actor, "dataset:read")
    clauses = ["workspace_id=%s"]
    params: list[Any] = [actor.workspace_id]
    if eval_set_id:
        clauses.append("eval_set_id=%s")
        params.append(eval_set_id)
    params.append(limit)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"SELECT * FROM kb_eval_run WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT %s",
            tuple(params),
        )
        rows = await result.fetchall()
    return {"items": [_run_summary(row) for row in rows]}


@router.get("/eval-runs/{run_id}")
async def get_eval_run(run_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    """获取评测运行状态。"""
    _require(actor, "dataset:read")
    async with pool.connection() as conn:
        row = await _eval_run(conn, run_id, actor.workspace_id)
    return _run_summary(row)


@router.get("/eval-runs/{run_id}/results")
async def get_eval_results(
    run_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=500, ge=1, le=2000),
):
    """获取评测运行结果（含每条用例的指标）。"""
    _require(actor, "dataset:read")
    async with pool.connection() as conn:
        run = await _eval_run(conn, run_id, actor.workspace_id)
        result = await conn.execute(
            "SELECT * FROM kb_eval_result WHERE run_id=%s ORDER BY created_at LIMIT %s",
            (run_id, limit),
        )
        rows = await result.fetchall()
    return {
        "run": _run_summary(run),
        "items": [_result_summary(row) for row in rows],
    }


@router.post("/eval-runs/{run_id}/cancel")
async def cancel_eval_run(run_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    """取消评测运行。"""
    _require(actor, "dataset:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            run = await _eval_run(conn, run_id, actor.workspace_id)
            if run["status"] in {"completed", "failed", "cancelled"}:
                return _run_summary(run)
            operation = await request_cancellation(
                conn,
                operation_id=run["operation_id"],
                workspace_id=actor.workspace_id,
                reason="Knowledge evaluation run cancelled",
            )
            if operation and operation["status"] == "cancelled":
                await conn.execute(
                    "UPDATE kb_eval_run SET status='cancelled',completed_at=now(),updated_at=now() WHERE id=%s",
                    (run_id,),
                )
            result = await conn.execute(
                "SELECT * FROM kb_eval_run WHERE id=%s",
                (run_id,),
            )
            row = await result.fetchone()
    return _run_summary(row)


# ------------------------- 标注回流 -------------------------


@router.post("/eval-runs/{run_id}/annotations", status_code=201)
async def create_annotation(
    run_id: str,
    body: AnnotationCreate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """提交人工标注（每用户对每条用例唯一）。"""
    _require(actor, "dataset:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            run = await _eval_run(conn, run_id, actor.workspace_id)
            case_result = await conn.execute(
                "SELECT id FROM kb_eval_case WHERE id=%s AND eval_set_id=%s AND status='active'",
                (body.case_id, run["eval_set_id"]),
            )
            if not await case_result.fetchone():
                raise HTTPException(status_code=404, detail="Evaluation case not found")
            try:
                result = await conn.execute(
                    """
                    INSERT INTO kb_eval_annotation(
                      id,run_id,case_id,org_id,workspace_id,rating,feedback,corrected_answer,
                      corrected_chunks,labels,created_by
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s) RETURNING *
                    """,
                    (
                        new_id("kean"), run_id, body.case_id, actor.org_id, actor.workspace_id,
                        body.rating, body.feedback, body.corrected_answer,
                        json_dumps(body.corrected_chunks), json_dumps(body.labels), actor.user_id,
                    ),
                )
            except Exception as exc:
                if "unique" in str(exc).lower():
                    raise HTTPException(
                        status_code=409,
                        detail="Annotation already exists for this case by the same user",
                    ) from exc
                raise
            row = await result.fetchone()
    return _annotation_summary(row)


@router.get("/eval-runs/{run_id}/annotations")
async def list_annotations(
    run_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=500, ge=1, le=2000),
):
    """获取评测运行下的所有人工标注。"""
    _require(actor, "dataset:read")
    async with pool.connection() as conn:
        await _eval_run(conn, run_id, actor.workspace_id)
        result = await conn.execute(
            """
            SELECT * FROM kb_eval_annotation
            WHERE run_id=%s AND workspace_id=%s
            ORDER BY created_at DESC LIMIT %s
            """,
            (run_id, actor.workspace_id, limit),
        )
        rows = await result.fetchall()
    return {"items": [_annotation_summary(row) for row in rows]}


@router.post("/eval-runs/{run_id}/annotations/import", status_code=201)
async def import_annotations(
    run_id: str,
    body: AnnotationImport,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """批量导入人工标注。"""
    _require(actor, "dataset:write")
    created = 0
    skipped = 0
    async with pool.connection() as conn:
        async with conn.transaction():
            run = await _eval_run(conn, run_id, actor.workspace_id)
            for item in body.items:
                case_result = await conn.execute(
                    "SELECT id FROM kb_eval_case WHERE id=%s AND eval_set_id=%s AND status='active'",
                    (item.case_id, run["eval_set_id"]),
                )
                if not await case_result.fetchone():
                    skipped += 1
                    continue
                existing_result = await conn.execute(
                    "SELECT id FROM kb_eval_annotation WHERE run_id=%s AND case_id=%s AND created_by=%s",
                    (run_id, item.case_id, actor.user_id),
                )
                if await existing_result.fetchone():
                    skipped += 1
                    continue
                await conn.execute(
                    """
                    INSERT INTO kb_eval_annotation(
                      id,run_id,case_id,org_id,workspace_id,rating,feedback,corrected_answer,
                      corrected_chunks,labels,created_by
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
                    """,
                    (
                        new_id("kean"), run_id, item.case_id, actor.org_id, actor.workspace_id,
                        item.rating, item.feedback, item.corrected_answer,
                        json_dumps(item.corrected_chunks), json_dumps(item.labels), actor.user_id,
                    ),
                )
                created += 1
    return {"created": created, "skipped": skipped}


# ------------------------- 评测结果对比 -------------------------


@router.post("/eval-runs/compare")
async def compare_eval_runs(
    body: EvalRunCompareRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """对比两次评测运行的指标差异（baseline vs candidate）。"""
    _require(actor, "dataset:read")
    async with pool.connection() as conn:
        baseline = await _eval_run(conn, body.baseline_run_id, actor.workspace_id)
        candidate = await _eval_run(conn, body.candidate_run_id, actor.workspace_id)
        if baseline["eval_set_id"] != candidate["eval_set_id"]:
            raise HTTPException(
                status_code=422,
                detail="Baseline and candidate runs must belong to the same evaluation set",
            )
        baseline_metrics = baseline.get("metrics_summary") or {}
        candidate_metrics = candidate.get("metrics_summary") or {}
        # 对比每个指标的平均值
        comparison: dict[str, dict[str, Any]] = {}
        all_metric_names = set(baseline_metrics.keys()) | set(candidate_metrics.keys())
        for name in all_metric_names:
            if name in ("total_cases", "error_cases", "config", "processed", "total"):
                continue
            baseline_val = baseline_metrics.get(name)
            candidate_val = candidate_metrics.get(name)
            baseline_mean = baseline_val.get("mean") if isinstance(baseline_val, dict) else baseline_val
            candidate_mean = candidate_val.get("mean") if isinstance(candidate_val, dict) else candidate_val
            try:
                baseline_mean_f = float(baseline_mean) if baseline_mean is not None else None
                candidate_mean_f = float(candidate_mean) if candidate_mean is not None else None
            except (TypeError, ValueError):
                baseline_mean_f = None
                candidate_mean_f = None
            delta = None
            delta_pct = None
            if baseline_mean_f is not None and candidate_mean_f is not None:
                delta = round(candidate_mean_f - baseline_mean_f, 6)
                if baseline_mean_f != 0:
                    delta_pct = round(delta / baseline_mean_f * 100, 4)
            comparison[name] = {
                "baseline_mean": baseline_mean_f,
                "candidate_mean": candidate_mean_f,
                "delta": delta,
                "delta_pct": delta_pct,
            }
    return {
        "baseline": _run_summary(baseline),
        "candidate": _run_summary(candidate),
        "comparison": comparison,
    }


# ------------------------- LLM-as-judge 批量评分 -------------------------


@router.post("/eval-runs/{run_id}/llm-judge", status_code=202)
async def batch_llm_judge(
    run_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    """对已有评测运行结果批量追加 LLM 评分，异步返回 operation_id。"""
    _require(actor, "dataset:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            run = await _eval_run(conn, run_id, actor.workspace_id)
            payload = {
                "run_id": run_id,
                "eval_set_id": run["eval_set_id"],
                "dataset_id": run["dataset_id"],
                "actor_id": actor.user_id,
                "org_id": actor.org_id,
            }
            try:
                operation = await submit_operation(
                    conn,
                    operation_type="kb.eval.llm_judge",
                    workspace_id=actor.workspace_id,
                    org_id=actor.org_id,
                    actor_id=actor.user_id,
                    actor_role=actor.role,
                    idempotency_key=idempotency_key or f"kb-eval-llm-judge:{run_id}",
                    payload=payload,
                    job_type="kb.eval.llm_judge",
                    queue="rag",
                    max_attempts=2,
                    priority=70,
                )
            except IdempotencyConflict as exc:
                raise HTTPException(
                    status_code=409,
                    detail="E00008 Idempotency key was already used with different input",
                ) from exc
    return {"operation_id": operation["id"]}


# ------------------------- 异步作业处理 -------------------------


async def _eval_not_cancelled(job: ClaimedJob) -> None:
    """检查异步作业是否被取消。"""
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT status FROM ops_async_operation WHERE id=%s",
            (job.operation_id,),
        )
        operation = await result.fetchone()
    if not operation or operation["status"] in {"cancel_requested", "cancelled"}:
        from workama_platform.modules.knowledge import RagJobCancelled

        raise RagJobCancelled("Knowledge evaluation operation was cancelled")


async def _process_run(job: ClaimedJob) -> dict[str, Any]:
    """执行评测运行作业。"""
    from workama_platform.modules.knowledge import RagJobCancelled, _retrieve_rows

    payload = job.payload
    run_id = payload["run_id"]
    config = payload["config"]
    metric_names: list[str] = payload.get("metrics") or list(DEFAULT_METRICS)
    use_llm_judge = bool(config.get("use_llm_judge"))

    actor = Actor(
        user_id=payload.get("actor_id", ""),
        workspace_id=job.workspace_id,
        org_id=payload.get("org_id", ""),
        role="system",
        email="",
        display_name="",
        onboarding_completed=True,
    )

    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM kb_eval_run WHERE id=%s AND workspace_id=%s",
            (run_id, job.workspace_id),
        )
        run = await result.fetchone()
        if not run:
            raise ValueError("Evaluation run not found")
        await conn.execute(
            "UPDATE kb_eval_run SET status='running',started_at=COALESCE(started_at,now()),updated_at=now() WHERE id=%s",
            (run_id,),
        )
        dataset_result = await conn.execute(
            "SELECT * FROM pf_dataset WHERE id=%s AND workspace_id=%s",
            (payload["dataset_id"], job.workspace_id),
        )
        dataset = await dataset_result.fetchone()
        cases_result = await conn.execute(
            """
            SELECT * FROM kb_eval_case
            WHERE eval_set_id=%s AND workspace_id=%s AND status='active'
            ORDER BY created_at
            """,
            (payload["eval_set_id"], job.workspace_id),
        )
        cases = await cases_result.fetchall()
        await conn.commit()

    if not dataset:
        raise ValueError("Dataset not found")
    dataset = dict(dataset)
    dataset["active_generation_id"] = payload["generation_id"]

    case_results: list[dict[str, Any]] = []
    errors = 0
    metric_values: dict[str, list[float]] = {name: [] for name in metric_names}
    llm_faithfulness_values: list[float] = []

    for index, case in enumerate(cases, 1):
        await _eval_not_cancelled(job)
        started = time.monotonic()
        retrieved_ids: list[str] = []
        generated_answer = ""
        error_msg: str | None = None
        try:
            keyword, vector = await _retrieve_rows(
                dataset, job.workspace_id, case["question"], config["candidate_k"]
            )
            rows = _fuse_rows(keyword, vector, rrf_k=config["rrf_k"], top_k=config["top_k"])
            retrieved_ids = [row["id"] for row in rows]
            # 简化处理：将检索结果拼接作为生成答案（避免引入额外 LLM 调用）
            generated_answer = "\n".join(row.get("content", "") for row in rows)
            case_metrics = compute_case_metrics(
                retrieved_ids=retrieved_ids,
                expected_ids=list(case["expected_chunks"] or []),
                generated_answer=generated_answer,
                expected_answer=case["expected_answer"] or "",
                metrics=metric_names,
            )
            if use_llm_judge:
                contexts = [row.get("content", "") for row in rows]
                faithfulness = await _judge_faithfulness(
                    case["question"], generated_answer, contexts, actor
                )
                answer_rel_llm = await _judge_answer_relevance_llm(
                    case["question"], generated_answer, actor
                )
                case_metrics["faithfulness"] = faithfulness
                case_metrics["answer_relevance"] = answer_rel_llm
                llm_faithfulness_values.append(faithfulness)
        except Exception as exc:
            errors += 1
            error_msg = str(exc)[:500]
            case_metrics = {}

        latency_ms = int((time.monotonic() - started) * 1000)
        for name in metric_names:
            if name in case_metrics:
                metric_values[name].append(float(case_metrics[name]))

        case_results.append(
            {
                "case_id": case["id"],
                "question": case["question"],
                "retrieved_chunks": retrieved_ids,
                "generated_answer": generated_answer,
                "metrics": case_metrics,
                "latency_ms": latency_ms,
                "error": error_msg,
            }
        )

        async with pool.connection() as conn:
            await heartbeat(
                conn, job,
                progress=min(95, int(index * 100 / max(len(cases), 1))),
                stage="scoring",
                lease_seconds=180,
            )
            await conn.execute(
                """
                UPDATE kb_eval_run
                SET metrics_summary=jsonb_build_object('processed',%s,'total',%s),
                    updated_at=now()
                WHERE id=%s
                """,
                (index, len(cases), run_id),
            )
            await conn.commit()

    # 聚合统计
    summary: dict[str, Any] = {
        "total_cases": len(cases),
        "error_cases": errors,
        "config": config,
    }
    for name in metric_names:
        summary[name] = aggregate_metric(metric_values[name])
    if llm_faithfulness_values:
        summary["faithfulness"] = aggregate_metric(llm_faithfulness_values)

    async with pool.connection() as conn:
        async with conn.transaction():
            # 持久化每条用例结果
            for item in case_results:
                await conn.execute(
                    """
                    INSERT INTO kb_eval_result(
                      id,run_id,case_id,workspace_id,question,retrieved_chunks,generated_answer,
                      metrics,latency_ms,error
                    ) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s)
                    """,
                    (
                        new_id("keres"), run_id, item["case_id"], job.workspace_id, item["question"],
                        json_dumps(item["retrieved_chunks"]), item["generated_answer"],
                        json_dumps(item["metrics"]), item["latency_ms"], item["error"],
                    ),
                )
            await conn.execute(
                """
                UPDATE kb_eval_run
                SET status='completed',metrics_summary=%s::jsonb,completed_at=now(),updated_at=now()
                WHERE id=%s AND status NOT IN ('cancelled')
                """,
                (json_dumps(summary), run_id),
            )
    return {"run_id": run_id, "metrics": summary}


async def _process_llm_judge(job: ClaimedJob) -> dict[str, Any]:
    """对已有评测运行结果批量追加 LLM 评分。"""
    payload = job.payload
    run_id = payload["run_id"]

    actor = Actor(
        user_id=payload.get("actor_id", ""),
        workspace_id=job.workspace_id,
        org_id=payload.get("org_id", ""),
        role="system",
        email="",
        display_name="",
        onboarding_completed=True,
    )

    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM kb_eval_run WHERE id=%s AND workspace_id=%s",
            (run_id, job.workspace_id),
        )
        run = await result.fetchone()
        if not run:
            raise ValueError("Evaluation run not found")
        results_query = await conn.execute(
            "SELECT * FROM kb_eval_result WHERE run_id=%s ORDER BY created_at",
            (run_id,),
        )
        results = await results_query.fetchall()
        await conn.commit()

    updated = 0
    errors = 0

    for index, result_row in enumerate(results, 1):
        await _eval_not_cancelled(job)
        try:
            contexts = (
                result_row.get("generated_answer", "").split("\n")
                if result_row.get("generated_answer")
                else []
            )
            faithfulness = await _judge_faithfulness(
                result_row["question"],
                result_row["generated_answer"] or "",
                contexts,
                actor,
            )
            answer_rel = await _judge_answer_relevance_llm(
                result_row["question"],
                result_row["generated_answer"] or "",
                actor,
            )
            metrics = dict(result_row.get("metrics") or {})
            metrics["faithfulness"] = faithfulness
            metrics["answer_relevance"] = answer_rel

            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE kb_eval_result SET metrics=%s::jsonb, updated_at=now() WHERE id=%s",
                    (json_dumps(metrics), result_row["id"]),
                )
                await heartbeat(
                    conn,
                    job,
                    progress=min(95, int(index * 100 / max(len(results), 1))),
                    stage="llm_judge",
                    lease_seconds=180,
                )
                await conn.commit()
            updated += 1
        except Exception as exc:
            errors += 1
            LOGGER.warning("LLM judge failed for result %s: %s", result_row["id"], exc)

    # 重新聚合运行摘要
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT metrics FROM kb_eval_result WHERE run_id=%s",
            (run_id,),
        )
        all_rows = await result.fetchall()
        run_summary = dict(run.get("metrics_summary") or {})
        run_summary.setdefault("total_cases", len(all_rows))
        run_summary.setdefault("error_cases", run_summary.get("error_cases", 0))
        run_summary.setdefault("config", run_summary.get("config", {}))

        all_metric_values: dict[str, list[float]] = {}
        for r in all_rows:
            m = r.get("metrics") or {}
            for k, v in m.items():
                if isinstance(v, (int, float)):
                    all_metric_values.setdefault(k, []).append(float(v))
        for name, values in all_metric_values.items():
            run_summary[name] = aggregate_metric(values)

        await conn.execute(
            "UPDATE kb_eval_run SET metrics_summary=%s::jsonb, updated_at=now() WHERE id=%s",
            (json_dumps(run_summary), run_id),
        )
        await conn.commit()

    return {"run_id": run_id, "updated": updated, "errors": errors}


async def process_kb_eval_job(job: ClaimedJob) -> dict[str, Any]:
    """知识库评测作业入口。"""
    if job.job_type == "kb.eval.run":
        try:
            return await _process_run(job)
        except Exception as exc:
            run_id = job.payload.get("run_id")
            if run_id:
                from workama_platform.modules.knowledge import RagJobCancelled

                status = "cancelled" if isinstance(exc, RagJobCancelled) else "failed"
                async with pool.connection() as conn:
                    await conn.execute(
                        """
                        UPDATE kb_eval_run
                        SET status=%s,error=%s,completed_at=now(),updated_at=now()
                        WHERE id=%s AND status NOT IN ('completed','cancelled')
                        """,
                        (status, str(exc)[:500], run_id),
                    )
                    await conn.commit()
            raise
    if job.job_type == "kb.eval.llm_judge":
        return await _process_llm_judge(job)
    raise ValueError(f"Unknown knowledge evaluation job type: {job.job_type}")


# ============================================================
# 金标集管理 + 聚合报告 + 基线对比 + 导出（T-M3-003 扩展）
# ============================================================


# ------------------------- Pydantic 模型（金标集） -------------------------


class GoldenSetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2_000)
    dataset_id: str | None = Field(default=None, max_length=64)


class GoldenSetPatch(BaseModel):
    """金标集局部更新；字段为 None 表示不修改。"""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2_000)
    dataset_id: str | None = Field(default=None, max_length=64)


class GoldenCaseCreate(BaseModel):
    query: str = Field(min_length=1, max_length=20_000)
    expected_answer: str = Field(default="", max_length=20_000)
    expected_context_ids: list[str] = Field(default_factory=list, max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=50)


class GoldenCasePatch(BaseModel):
    """金标用例局部更新；字段为 None 表示不修改。"""

    query: str | None = Field(default=None, min_length=1, max_length=20_000)
    expected_answer: str | None = Field(default=None, max_length=20_000)
    expected_context_ids: list[str] | None = Field(default=None, max_length=200)
    tags: list[str] | None = Field(default=None, max_length=50)


class GoldenCaseImport(BaseModel):
    """金标用例批量导入。"""

    items: list[GoldenCaseCreate] = Field(min_length=1, max_length=500)


class GoldenEvalRequest(BaseModel):
    eval_set_id: str | None = Field(default=None, max_length=64)
    baseline_report_id: str | None = Field(default=None, max_length=64)
    top_k: int = Field(default=5, ge=1, le=50)


class ReportExportRequest(BaseModel):
    format: Literal["json", "csv"] = "json"


# ------------------------- 辅助函数（金标集） -------------------------


async def _golden_set(conn, golden_set_id: str, workspace_id: str) -> dict[str, Any]:
    """获取金标集（workspace 隔离），不存在抛 404。"""
    result = await conn.execute(
        "SELECT * FROM rag_golden_set WHERE id=%s AND workspace_id=%s",
        (golden_set_id, workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Golden set not found")
    return row


async def _golden_report(conn, report_id: str, workspace_id: str) -> dict[str, Any]:
    """获取评测报告（workspace 隔离），不存在抛 404。"""
    result = await conn.execute(
        "SELECT * FROM rag_eval_report WHERE id=%s AND workspace_id=%s",
        (report_id, workspace_id),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Evaluation report not found")
    return row


def _golden_set_view(row: dict[str, Any]) -> dict[str, Any]:
    """金标集对外视图。"""
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "name": row["name"],
        "description": row["description"],
        "dataset_id": row.get("dataset_id"),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _golden_case_view(row: dict[str, Any]) -> dict[str, Any]:
    """金标用例对外视图。"""
    return {
        "id": row["id"],
        "golden_set_id": row["golden_set_id"],
        "query": row["query"],
        "expected_answer": row["expected_answer"],
        "expected_context_ids": list(row.get("expected_context_ids") or []),
        "tags": list(row.get("tags") or []),
        "created_at": row["created_at"],
    }


def _report_view(row: dict[str, Any]) -> dict[str, Any]:
    """评测报告对外视图。"""
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "golden_set_id": row["golden_set_id"],
        "eval_run_id": row.get("eval_run_id"),
        "status": row["status"],
        "hit_at_k": row.get("hit_at_k") or {},
        "avg_recall": row["avg_recall"],
        "avg_precision": row["avg_precision"],
        "avg_f1": row["avg_f1"],
        "avg_faithfulness": row["avg_faithfulness"],
        "avg_answer_relevance": row["avg_answer_relevance"],
        "total_cases": row["total_cases"],
        "passed_cases": row["passed_cases"],
        "baseline_report_id": row.get("baseline_report_id"),
        "summary": row.get("summary") or {},
        "created_at": row["created_at"],
        "completed_at": row.get("completed_at"),
    }


def _report_case_view(row: dict[str, Any]) -> dict[str, Any]:
    """报告用例明细对外视图。"""
    return {
        "id": row["id"],
        "report_id": row["report_id"],
        "case_id": row["case_id"],
        "query": row["query"],
        "expected_answer": row["expected_answer"],
        "actual_answer": row["actual_answer"],
        "retrieved_context_ids": list(row.get("retrieved_context_ids") or []),
        "expected_context_ids": list(row.get("expected_context_ids") or []),
        "hit": row["hit"],
        "recall": row["recall"],
        "precision": row["precision"],
        "f1": row["f1"],
        "faithfulness": row["faithfulness"],
        "answer_relevance": row["answer_relevance"],
        "created_at": row["created_at"],
    }


async def _mock_retrieve_contexts(query: str, workspace_id: str, top_k: int = 5) -> list[str]:
    """Mock 检索：返回确定性伪上下文 ID 列表。

    评测过程同步执行，使用此 mock 替代真实检索链路。
    测试中可被 monkeypatch 替换以控制命中/未命中场景。
    """
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:8]
    return [f"ctx_{digest}_{i}" for i in range(min(top_k, 3))]


def _f1_from_pr(recall: float, precision: float) -> float:
    """根据 recall 与 precision 计算 F1。"""
    if recall + precision <= 0:
        return 0.0
    return 2 * recall * precision / (recall + precision)


# ------------------------- 金标集 CRUD -------------------------


@router.post("/golden-sets", status_code=201)
async def create_golden_set(
    body: GoldenSetCreate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """创建金标集（workspace 隔离，dataset_id 可选）。"""
    _require(actor, "dataset:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                INSERT INTO rag_golden_set(id,workspace_id,name,description,dataset_id,created_by)
                VALUES (%s,%s,%s,%s,%s,%s) RETURNING *
                """,
                (
                    new_id("rgs"), actor.workspace_id, body.name.strip(),
                    body.description, body.dataset_id, actor.user_id,
                ),
            )
            row = await result.fetchone()
    return _golden_set_view(row)


@router.get("/golden-sets")
async def list_golden_sets(
    actor: Annotated[Actor, Depends(get_actor)],
    dataset_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """金标集列表（分页，支持 dataset_id 过滤，workspace 隔离）。"""
    _require(actor, "dataset:read")
    clauses = ["workspace_id=%s"]
    params: list[Any] = [actor.workspace_id]
    if dataset_id:
        clauses.append("dataset_id=%s")
        params.append(dataset_id)
    params.append(limit)
    params.append(offset)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"SELECT * FROM rag_golden_set WHERE {' AND '.join(clauses)} "
            f"ORDER BY created_at DESC LIMIT %s OFFSET %s",
            tuple(params),
        )
        rows = await result.fetchall()
    return {"items": [_golden_set_view(row) for row in rows]}


@router.get("/golden-sets/{golden_set_id}")
async def get_golden_set(golden_set_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    """金标集详情（含全部 golden cases）。"""
    _require(actor, "dataset:read")
    async with pool.connection() as conn:
        row = await _golden_set(conn, golden_set_id, actor.workspace_id)
        cases_result = await conn.execute(
            "SELECT * FROM rag_golden_case WHERE golden_set_id=%s ORDER BY created_at",
            (golden_set_id,),
        )
        cases = await cases_result.fetchall()
    view = _golden_set_view(row)
    view["cases"] = [_golden_case_view(c) for c in cases]
    view["case_count"] = len(cases)
    return view


@router.patch("/golden-sets/{golden_set_id}")
async def update_golden_set(
    golden_set_id: str,
    body: GoldenSetPatch,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """更新金标集元数据（name / description / dataset_id，None 表示不修改）。"""
    _require(actor, "dataset:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _golden_set(conn, golden_set_id, actor.workspace_id)
            result = await conn.execute(
                """
                UPDATE rag_golden_set
                SET name=COALESCE(%s,name), description=COALESCE(%s,description),
                    dataset_id=COALESCE(%s,dataset_id), updated_at=now()
                WHERE id=%s AND workspace_id=%s RETURNING *
                """,
                (
                    body.name.strip() if body.name is not None else None,
                    body.description, body.dataset_id, golden_set_id, actor.workspace_id,
                ),
            )
            row = await result.fetchone()
    return _golden_set_view(row)


@router.delete("/golden-sets/{golden_set_id}", status_code=204)
async def delete_golden_set(golden_set_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    """删除金标集，并级联删除其用例、评测报告与报告明细。"""
    _require(actor, "dataset:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _golden_set(conn, golden_set_id, actor.workspace_id)
            await conn.execute(
                """
                DELETE FROM rag_eval_report_case WHERE report_id IN (
                  SELECT id FROM rag_eval_report WHERE golden_set_id=%s AND workspace_id=%s
                )
                """,
                (golden_set_id, actor.workspace_id),
            )
            await conn.execute(
                "DELETE FROM rag_eval_report WHERE golden_set_id=%s AND workspace_id=%s",
                (golden_set_id, actor.workspace_id),
            )
            await conn.execute(
                "DELETE FROM rag_golden_case WHERE golden_set_id=%s AND workspace_id=%s",
                (golden_set_id, actor.workspace_id),
            )
            await conn.execute(
                "DELETE FROM rag_golden_set WHERE id=%s AND workspace_id=%s",
                (golden_set_id, actor.workspace_id),
            )
    return Response(status_code=204)


# ------------------------- 金标用例管理 -------------------------


@router.post("/golden-sets/{golden_set_id}/cases", status_code=201)
async def create_golden_case(
    golden_set_id: str,
    body: GoldenCaseCreate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """添加金标用例（query/expected_answer/expected_context_ids/tags）。"""
    _require(actor, "dataset:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _golden_set(conn, golden_set_id, actor.workspace_id)
            result = await conn.execute(
                """
                INSERT INTO rag_golden_case(
                  id,golden_set_id,workspace_id,query,expected_answer,expected_context_ids,tags
                ) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *
                """,
                (
                    new_id("rgc"), golden_set_id, actor.workspace_id, body.query,
                    body.expected_answer, body.expected_context_ids, body.tags,
                ),
            )
            row = await result.fetchone()
            await conn.execute(
                "UPDATE rag_golden_set SET updated_at=now() WHERE id=%s",
                (golden_set_id,),
            )
    return _golden_case_view(row)


@router.patch("/golden-sets/{golden_set_id}/cases/{case_id}")
async def update_golden_case(
    golden_set_id: str,
    case_id: str,
    body: GoldenCasePatch,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """更新金标用例（query / expected_answer / expected_context_ids / tags）。"""
    _require(actor, "dataset:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _golden_set(conn, golden_set_id, actor.workspace_id)
            result = await conn.execute(
                """
                UPDATE rag_golden_case
                SET query=COALESCE(%s,query), expected_answer=COALESCE(%s,expected_answer),
                    expected_context_ids=COALESCE(%s,expected_context_ids), tags=COALESCE(%s,tags)
                WHERE id=%s AND golden_set_id=%s AND workspace_id=%s RETURNING *
                """,
                (
                    body.query, body.expected_answer, body.expected_context_ids, body.tags,
                    case_id, golden_set_id, actor.workspace_id,
                ),
            )
            row = await result.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Golden case not found")
            await conn.execute(
                "UPDATE rag_golden_set SET updated_at=now() WHERE id=%s",
                (golden_set_id,),
            )
    return _golden_case_view(row)


@router.delete("/golden-sets/{golden_set_id}/cases/{case_id}", status_code=204)
async def delete_golden_case(
    golden_set_id: str,
    case_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """删除单条金标用例。"""
    _require(actor, "dataset:write")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _golden_set(conn, golden_set_id, actor.workspace_id)
            result = await conn.execute(
                "DELETE FROM rag_golden_case WHERE id=%s AND golden_set_id=%s AND workspace_id=%s RETURNING id",
                (case_id, golden_set_id, actor.workspace_id),
            )
            if not await result.fetchone():
                raise HTTPException(status_code=404, detail="Golden case not found")
            await conn.execute(
                "UPDATE rag_golden_set SET updated_at=now() WHERE id=%s",
                (golden_set_id,),
            )
    return Response(status_code=204)


@router.post("/golden-sets/{golden_set_id}/cases/import", status_code=201)
async def import_golden_cases(
    golden_set_id: str,
    body: GoldenCaseImport,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """批量导入金标用例（同步写入，返回已创建条数与明细）。"""
    _require(actor, "dataset:write")
    created: list[dict[str, Any]] = []
    async with pool.connection() as conn:
        async with conn.transaction():
            await _golden_set(conn, golden_set_id, actor.workspace_id)
            for item in body.items:
                result = await conn.execute(
                    """
                    INSERT INTO rag_golden_case(
                      id,golden_set_id,workspace_id,query,expected_answer,expected_context_ids,tags
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *
                    """,
                    (
                        new_id("rgc"), golden_set_id, actor.workspace_id, item.query,
                        item.expected_answer, item.expected_context_ids, item.tags,
                    ),
                )
                created.append(_golden_case_view(await result.fetchone()))
            await conn.execute(
                "UPDATE rag_golden_set SET updated_at=now() WHERE id=%s",
                (golden_set_id,),
            )
    return {"golden_set_id": golden_set_id, "created": len(created), "items": created}


@router.get("/golden-sets/{golden_set_id}/cases/export")
async def export_golden_cases(
    golden_set_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    format: str = Query(default="json"),
):
    """导出金标用例（json 可直接回灌 import 接口 / csv 为明细表）。"""
    _require(actor, "dataset:read")
    if format not in {"json", "csv"}:
        raise HTTPException(status_code=422, detail="format must be 'json' or 'csv'")
    async with pool.connection() as conn:
        await _golden_set(conn, golden_set_id, actor.workspace_id)
        result = await conn.execute(
            "SELECT * FROM rag_golden_case WHERE golden_set_id=%s AND workspace_id=%s ORDER BY created_at",
            (golden_set_id, actor.workspace_id),
        )
        cases = [_golden_case_view(row) for row in await result.fetchall()]
    if format == "json":
        payload = {
            "items": [
                {
                    "query": case["query"],
                    "expected_answer": case["expected_answer"],
                    "expected_context_ids": case["expected_context_ids"],
                    "tags": case["tags"],
                }
                for case in cases
            ]
        }
        return Response(
            content=json_dumps(payload),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="golden_cases_{golden_set_id}.json"'},
        )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "query", "expected_answer", "expected_context_ids", "tags"])
    for case in cases:
        writer.writerow([
            case["id"], case["query"], case["expected_answer"],
            ";".join(case["expected_context_ids"]), ";".join(case["tags"]),
        ])
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="golden_cases_{golden_set_id}.csv"'},
    )


# ------------------------- 金标集评测 -------------------------


@router.post("/golden-sets/{golden_set_id}/evaluate", status_code=201)
async def evaluate_golden_set(
    golden_set_id: str,
    body: GoldenEvalRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """对金标集执行评测（同步，mock 检索），生成聚合报告。

    - 遍历金标用例，对每个 query 调用 _mock_retrieve_contexts 检索
    - 计算 hit@k（k=1,3,5）/ recall / precision / f1
    - 确定性计算 faithfulness / answer_relevance（不调用 LLM）
    - 聚合到 rag_eval_report，逐用例写入 rag_eval_report_case
    - 状态直接写 completed
    """
    _require(actor, "dataset:write")
    ks = (1, 3, 5)
    async with pool.connection() as conn:
        async with conn.transaction():
            await _golden_set(conn, golden_set_id, actor.workspace_id)
            cases_result = await conn.execute(
                "SELECT * FROM rag_golden_case WHERE golden_set_id=%s AND workspace_id=%s ORDER BY created_at",
                (golden_set_id, actor.workspace_id),
            )
            cases = await cases_result.fetchall()
            if not cases:
                raise HTTPException(status_code=422, detail="Golden set has no cases to evaluate")
            # 可选基线报告校验
            if body.baseline_report_id:
                baseline_result = await conn.execute(
                    "SELECT * FROM rag_eval_report WHERE id=%s AND workspace_id=%s",
                    (body.baseline_report_id, actor.workspace_id),
                )
                if not await baseline_result.fetchone():
                    raise HTTPException(status_code=404, detail="Baseline report not found")
            report_id = new_id("rgr")
            await conn.execute(
                """
                INSERT INTO rag_eval_report(id,workspace_id,golden_set_id,eval_run_id,status,baseline_report_id)
                VALUES (%s,%s,%s,%s,'running',%s)
                """,
                (report_id, actor.workspace_id, golden_set_id, body.eval_set_id, body.baseline_report_id),
            )
            hit_counts = {k: 0 for k in ks}
            recall_values: list[float] = []
            precision_values: list[float] = []
            f1_values: list[float] = []
            faithfulness_values: list[float] = []
            answer_relevance_values: list[float] = []
            passed = 0
            for case in cases:
                retrieved = await _mock_retrieve_contexts(case["query"], actor.workspace_id, body.top_k)
                expected = list(case.get("expected_context_ids") or [])
                # hit@k：expected 为空时一律记为未命中
                hit_flags = {
                    k: (bool(expected) and any(exp in retrieved[:k] for exp in expected))
                    for k in ks
                }
                case_hit = hit_flags[5]
                if case_hit:
                    passed += 1
                for k in ks:
                    if hit_flags[k]:
                        hit_counts[k] += 1
                recall = retrieval_recall(retrieved, expected)
                precision = retrieval_precision(retrieved, expected)
                f1 = _f1_from_pr(recall, precision)
                actual_answer = "\n".join(retrieved)
                ar = answer_relevance(actual_answer, case.get("expected_answer") or "")
                # 简化 faithfulness：检索非空且生成答案非空则 1.0，否则 0.0
                faith = 1.0 if (retrieved and actual_answer) else 0.0
                recall_values.append(recall)
                precision_values.append(precision)
                f1_values.append(f1)
                faithfulness_values.append(faith)
                answer_relevance_values.append(ar)
                await conn.execute(
                    """
                    INSERT INTO rag_eval_report_case(
                      id,report_id,case_id,query,expected_answer,actual_answer,
                      retrieved_context_ids,expected_context_ids,hit,recall,precision,f1,faithfulness,answer_relevance
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        new_id("rgrc"), report_id, case["id"], case["query"],
                        case.get("expected_answer") or "", actual_answer, retrieved, expected,
                        case_hit, recall, precision, f1, faith, ar,
                    ),
                )
            total = len(cases)
            hit_at_k = {str(k): (hit_counts[k] / total if total else 0.0) for k in ks}
            avg_recall = sum(recall_values) / total if total else 0.0
            avg_precision = sum(precision_values) / total if total else 0.0
            avg_f1 = sum(f1_values) / total if total else 0.0
            avg_faithfulness = sum(faithfulness_values) / total if total else 0.0
            avg_answer_relevance = sum(answer_relevance_values) / total if total else 0.0
            summary = {
                "hit_at_k": hit_at_k,
                "total_cases": total,
                "passed_cases": passed,
                "baseline_report_id": body.baseline_report_id,
            }
            await conn.execute(
                """
                UPDATE rag_eval_report
                SET status='completed',hit_at_k=%s::jsonb,avg_recall=%s,avg_precision=%s,avg_f1=%s,
                    avg_faithfulness=%s,avg_answer_relevance=%s,total_cases=%s,passed_cases=%s,
                    summary=%s::jsonb,completed_at=now()
                WHERE id=%s
                """,
                (
                    json_dumps(hit_at_k), avg_recall, avg_precision, avg_f1, avg_faithfulness,
                    avg_answer_relevance, total, passed, json_dumps(summary), report_id,
                ),
            )
            report_row = {
                "id": report_id,
                "workspace_id": actor.workspace_id,
                "golden_set_id": golden_set_id,
                "eval_run_id": body.eval_set_id,
                "status": "completed",
                "hit_at_k": hit_at_k,
                "avg_recall": avg_recall,
                "avg_precision": avg_precision,
                "avg_f1": avg_f1,
                "avg_faithfulness": avg_faithfulness,
                "avg_answer_relevance": avg_answer_relevance,
                "total_cases": total,
                "passed_cases": passed,
                "baseline_report_id": body.baseline_report_id,
                "summary": summary,
                "created_at": "2026-07-28T10:00:00+00:00",
                "completed_at": "2026-07-28T10:00:00+00:00",
            }
    return _report_view(report_row)


# ------------------------- 报告列表与详情 -------------------------


@router.get("/golden-sets/{golden_set_id}/reports")
async def list_golden_reports(
    golden_set_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """金标集评测报告列表（分页，按 created_at 倒序，workspace 隔离）。"""
    _require(actor, "dataset:read")
    async with pool.connection() as conn:
        await _golden_set(conn, golden_set_id, actor.workspace_id)
        result = await conn.execute(
            "SELECT * FROM rag_eval_report WHERE golden_set_id=%s AND workspace_id=%s "
            "ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (golden_set_id, actor.workspace_id, limit, offset),
        )
        rows = await result.fetchall()
    return {"items": [_report_view(row) for row in rows]}


@router.get("/reports/{report_id}")
async def get_report(report_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    """评测报告详情（聚合指标 + by_case 明细 + 与基线对比 diff）。"""
    _require(actor, "dataset:read")
    async with pool.connection() as conn:
        report = await _golden_report(conn, report_id, actor.workspace_id)
        cases_result = await conn.execute(
            "SELECT * FROM rag_eval_report_case WHERE report_id=%s ORDER BY created_at",
            (report_id,),
        )
        cases = await cases_result.fetchall()
        diff: dict[str, Any] = {}
        if report.get("baseline_report_id"):
            baseline_result = await conn.execute(
                "SELECT * FROM rag_eval_report WHERE id=%s AND workspace_id=%s",
                (report["baseline_report_id"], actor.workspace_id),
            )
            baseline = await baseline_result.fetchone()
            if baseline:
                for key in (
                    "avg_recall", "avg_precision", "avg_f1",
                    "avg_faithfulness", "avg_answer_relevance",
                ):
                    diff[key] = round(report[key] - baseline[key], 6)
                for k in (1, 3, 5):
                    cur = (report.get("hit_at_k") or {}).get(str(k), 0.0)
                    base = (baseline.get("hit_at_k") or {}).get(str(k), 0.0)
                    diff[f"hit_at_{k}"] = round(cur - base, 6)
                diff["total_cases"] = report["total_cases"] - baseline["total_cases"]
                diff["passed_cases"] = report["passed_cases"] - baseline["passed_cases"]
    view = _report_view(report)
    view["by_case"] = [_report_case_view(c) for c in cases]
    view["baseline_diff"] = diff
    return view


# ------------------------- 报告导出 -------------------------


@router.post("/reports/{report_id}/export")
async def export_report(
    report_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    format: str = Query(default="json"),
):
    """导出报告（json 返回完整 JSON / csv 返回 case 级明细 CSV）。"""
    _require(actor, "dataset:read")
    if format not in {"json", "csv"}:
        raise HTTPException(status_code=422, detail="format must be 'json' or 'csv'")
    async with pool.connection() as conn:
        report = await _golden_report(conn, report_id, actor.workspace_id)
        cases_result = await conn.execute(
            "SELECT * FROM rag_eval_report_case WHERE report_id=%s ORDER BY created_at",
            (report_id,),
        )
        cases = await cases_result.fetchall()
    view = _report_view(report)
    view["by_case"] = [_report_case_view(c) for c in cases]
    if format == "json":
        content = json_dumps(view)
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="report_{report_id}.json"'},
        )
    # csv：case 级明细，使用标准库 csv 模块
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "case_id", "query", "expected_answer", "actual_answer",
        "retrieved_context_ids", "expected_context_ids", "hit",
        "recall", "precision", "f1", "faithfulness", "answer_relevance",
    ])
    for c in view["by_case"]:
        writer.writerow([
            c["case_id"], c["query"], c["expected_answer"], c["actual_answer"],
            ";".join(c["retrieved_context_ids"]),
            ";".join(c["expected_context_ids"]),
            c["hit"], c["recall"], c["precision"], c["f1"],
            c["faithfulness"], c["answer_relevance"],
        ])
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="report_{report_id}.csv"'},
    )
