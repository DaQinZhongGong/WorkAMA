"""Memory vector index module (pgvector-backed).

v7.136: 真实 pgvector 实现，1536 维余弦相似度向量索引。

提供：
- 8 个 REST 端点（写入 / 召回 / 查询 / 删除 / touch / 列表 / forget-sweep / extract）
- Worker 类（MemoryVectorIndex / MemoryExtractionWorker / MemoryForgettingWorker），
  供 platform-worker 通过 job 队列调用
- 确定性 1536 维 hash-based embedding（不调用外部 LLM / embedding API）

设计文档：910-进度追踪与任务清单.md「记忆真实向量索引（并行C）」
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from workama_platform.core import (
    Actor,
    capability_allows,
    get_actor,
    json_dumps,
    new_id,
    pool,
)
from workama_platform.modules.gateway.llm_client import call_llm
from workama_platform.modules.jobs import submit_operation

LOGGER = logging.getLogger("workama.platform-api.memory_vector")

# ============================================================================
# 常量
# ============================================================================

router = APIRouter(prefix="/api/v1/memory-vectors", tags=["memory-vector"])

EMBEDDING_DIMENSION = 1536
VECTOR_DIMENSIONS = EMBEDDING_DIMENSION  # 别名，供 router 内部使用

# Worker job 类型常量（platform-worker 通过这些常量路由 job）
MEMORY_EXTRACT_JOB_TYPE = "memory_extract"
MEMORY_FORGET_JOB_TYPE = "memory_forget"
MEMORY_REINDEX_JOB_TYPE = "memory_reindex"

# 遗忘曲线：importance(1-5) -> 保留天数
# importance=1: 1 天后遗忘；importance=5: 365 天
FORGET_RETENTION_DAYS: dict[int, int] = {
    1: 1,
    2: 7,
    3: 30,
    4: 90,
    5: 365,
}
# 别名（向后兼容 worker 已有引用）
FORGETTING_RETENTION_DAYS = FORGET_RETENTION_DAYS

# LLM 抽取类型 -> memory_vector.kind 映射
EXTRACTION_KIND_MAP: dict[str, str] = {
    "fact": "semantic",
    "preference": "preference",
    "event": "episodic",
    "relationship": "semantic",
    "skill": "semantic",
}

VectorKind = Literal["semantic", "episodic", "profile", "preference"]
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


# ============================================================================
# 异常
# ============================================================================


class MemoryVectorError(Exception):
    """向量操作失败时抛出。"""


# ============================================================================
# Pydantic 模型
# ============================================================================


class VectorCreate(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    kind: VectorKind = "semantic"
    importance: int = Field(default=3, ge=1, le=5)
    memory_id: str | None = Field(default=None, max_length=80)
    metadata: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = None


class VectorRecallRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)
    kind: VectorKind | None = None


class VectorUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=8000)
    importance: int | None = Field(default=None, ge=1, le=5)
    metadata: dict[str, Any] | None = None
    expires_at: datetime | None = None


class VectorResponse(BaseModel):
    id: str
    workspace_id: str
    memory_id: str | None = None
    content: str
    kind: str
    importance: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    last_referenced_at: datetime
    created_at: datetime
    expires_at: datetime | None = None


class ExtractRequest(BaseModel):
    conversation_text: str = Field(min_length=1, max_length=20000)


class ExtractResponse(BaseModel):
    extracted_ids: list[str] = Field(default_factory=list)
    count: int = 0


class ForgetSweepResponse(BaseModel):
    forgotten_ids: list[str] = Field(default_factory=list)
    count: int = 0


class ForgetPolicyRequest(BaseModel):
    retention_days_by_importance: dict[int, int] | None = None
    default_importance: int | None = Field(default=None, ge=1, le=5)


class ForgetPolicyResponse(BaseModel):
    workspace_id: str
    retention_days_by_importance: dict[int, int]
    default_importance: int
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ============================================================================
# 向量工具函数（确定性，不调用外部 API）
# ============================================================================


def _tokens(value: str) -> list[str]:
    """分词：英文按单词，中文按单字。"""
    return [t.lower() for t in _TOKEN_RE.findall(value or "")]


def vector_embedding(*parts: str) -> list[float]:
    """生成确定性 1536 维 hash-based embedding。

    对每个 token 做 8 轮 SHA256 哈希，将结果映射到 1536 维向量的不同槽位，
    最后归一化为单位向量。同一输入始终产生同一向量，不依赖外部 API。
    """
    values = [0.0] * EMBEDDING_DIMENSION
    for token in _tokens(" ".join(parts)):
        for round_idx in range(8):
            data = f"{token}:{round_idx}".encode("utf-8")
            digest = hashlib.sha256(data).digest()
            slot = int.from_bytes(digest[:2], "big") % EMBEDDING_DIMENSION
            sign = 1.0 if digest[2] & 1 else -1.0
            magnitude = 1.0 + min(len(token), 8) * 0.05
            values[slot] += sign * magnitude
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0:
        return [0.0] * EMBEDDING_DIMENSION
    return [round(v / norm, 8) for v in values]


def _vector_literal(embedding: list[float]) -> str:
    """将 list[float] 转为 pgvector 字符串字面量 ``[v1,v2,...]``。"""
    return "[" + ",".join(f"{v:.8f}" for v in embedding) + "]"


def _normalize_vector(vec: list[float]) -> list[float]:
    """归一化为单位向量。零向量或含 NaN 时抛出 MemoryVectorError。"""
    if not vec or any(v != v for v in vec):  # NaN check
        raise MemoryVectorError("vector is empty or contains NaN")
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        raise MemoryVectorError("cannot normalize zero vector")
    return [v / norm for v in vec]


def _importance_to_score(importance: int) -> float:
    """将 importance 1-5 映射到 score 0.0-1.0。"""
    clamped = max(1, min(5, importance))
    return (clamped - 1) / 4.0


def _score_to_importance(score: float) -> int:
    """将 score 0.0-1.0 映射到 importance 1-5。"""
    clamped = max(0.0, min(1.0, score))
    return max(1, min(5, round(clamped * 4) + 1))



def cosine_similarity(left: list[float], right: list[float]) -> float:
    """计算两个向量的余弦相似度。"""
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm_l = math.sqrt(sum(a * a for a in left))
    norm_r = math.sqrt(sum(b * b for b in right))
    if norm_l == 0 or norm_r == 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (norm_l * norm_r)))
# ============================================================================
# Gateway 调用函数（确定性 fallback，TODO: 接入真实 gateway）
# ============================================================================


async def _call_embedding(
    workspace_id: str,
    text: str,
    *,
    model: str | None = None,
    request_id: str | None = None,
) -> list[float]:
    """通过网关调用 embedding API。

    TODO: 接入真实 gateway（参考 memory.py 的 gateway 调用模式）。
    当前使用确定性 hash-based embedding 作为 fallback，不依赖外部服务。
    """
    return vector_embedding(text)


async def _call_llm(
    workspace_id: str,
    messages: list[dict],
    *,
    model: str | None = None,
    request_id: str | None = None,
    temperature: float = 0.2,
) -> str:
    """通过网关调用 LLM API。

    TODO: 接入真实 gateway。
    当前返回空 JSON 数组（无抽取结果）作为 fallback。
    """
    return "[]"


# ============================================================================
# LLM 抽取（v7.140）：通过 gateway 调用 LLM 抽取结构化记忆
# ============================================================================

# 允许的 memory_vector.kind 值
_VALID_KINDS: frozenset[str] = frozenset({"semantic", "episodic", "profile", "preference"})

# LLM 抽取 system prompt：要求输出 JSON 数组，元素含 content/kind/importance/metadata
_EXTRACTION_SYSTEM_PROMPT = (
    "You are a memory extraction assistant. Analyze the conversation and extract "
    "structured memories worth remembering about the user.\n\n"
    "Output a JSON array. Each element MUST have these fields:\n"
    '- "content": concise memory text (string)\n'
    '- "kind": one of "semantic", "episodic", "profile", "preference"\n'
    '- "importance": integer 1-5 (1=trivial, 5=critical)\n'
    '- "metadata": object, e.g. {"source": "extraction", "type": "fact"}\n\n'
    "Only extract clear, factual, actionable memories. If nothing is worth "
    "remembering, return []. Output ONLY the JSON array, no other text."
)


def _mock_extract_entries(text: str) -> list[dict]:
    """确定性 mock 抽取（v7.139 关键词匹配逻辑，作为 LLM 失败时的 fallback）。

    返回 list[dict]，每个 dict 含 content/kind/importance/metadata。
    """
    entries: list[dict] = []
    if "用户叫" in text:
        after = text.split("用户叫", 1)[1]
        name = re.split(r"[，。,\s\n]", after, 1)[0].strip() or "未知"
        entries.append(
            {
                "content": f"用户的名字是{name}",
                "kind": "profile",
                "importance": 4,
                "metadata": {"source": "extract", "type": "name"},
            }
        )
    if "喜欢" in text:
        after = text.split("喜欢", 1)[1]
        what = re.split(r"[，。,\s\n]", after, 1)[0].strip() or "某些东西"
        entries.append(
            {
                "content": f"用户喜欢{what}",
                "kind": "preference",
                "importance": 3,
                "metadata": {"source": "extract", "type": "preference"},
            }
        )
    return entries


def _strip_code_fence(raw: str) -> str:
    """剥离 markdown code fence（```json ... ``` 或 ``` ... ```）。"""
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw or "", re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()
    return raw or ""


def _parse_llm_extraction(raw: str) -> list[dict]:
    """解析 LLM 抽取输出（JSON 数组），返回标准化的 entries。

    容错：
    - 剥离 markdown code fence
    - 提取第一个 JSON 数组
    - 校验/钳制每个 entry 的字段
    - 解析失败返回空列表（由调用方回退到 mock）
    """
    cleaned = _strip_code_fence(raw)
    match = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if not match:
        return []
    try:
        items = json.loads(match.group())
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    result: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        kind = str(item.get("kind") or "semantic")
        if kind not in _VALID_KINDS:
            kind = "semantic"
        try:
            importance = max(1, min(5, int(item.get("importance", 3))))
        except (TypeError, ValueError):
            importance = 3
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        if "source" not in metadata:
            metadata["source"] = "extraction"
        result.append(
            {
                "content": content,
                "kind": kind,
                "importance": importance,
                "metadata": metadata,
            }
        )
    return result


def _llm_disabled() -> bool:
    """WORKAMA_EXTRACTION_DISABLE_LLM=1 时强制走 mock。"""
    return os.getenv("WORKAMA_EXTRACTION_DISABLE_LLM", "").strip() in ("1", "true", "yes")


async def _call_llm_for_extraction(
    conversation_text: str,
    workspace_id: str,
    actor: Actor,
) -> tuple[list[dict], str]:
    """通过 gateway 调用 LLM 抽取结构化记忆。

    返回 ``(entries, method)``：
    - method="llm"：LLM 抽取成功
    - method="mock"：LLM 被禁用或失败，回退到确定性 mock 抽取

    容错策略（v7.159：改用 ``gateway.llm_client.call_llm`` 统一入口）：
    - ``WORKAMA_EXTRACTION_DISABLE_LLM=1`` → 直接走 mock
    - ``WORKAMA_INTERNAL_LLM_API_KEY`` 未配置 → 直接走 mock（不调用 gateway），log warning
    - ``WORKAMA_INTERNAL_LLM_API_KEY`` 已配置 → 调用 ``llm_client.call_llm``
      （内部走 gateway ``/v1/chat/completions``）
    - httpx 超时/连接错误/4xx/5xx → 回退 mock（由 ``llm_client`` 处理）
    - LLM 返回非法 JSON → 回退 mock
    - LLM 返回空数组 → 视为成功（method="llm"，entries=[]）
    """
    mock_entries = _mock_extract_entries(conversation_text)
    if _llm_disabled():
        return mock_entries, "mock"

    api_key = os.getenv("WORKAMA_INTERNAL_LLM_API_KEY", "").strip()
    if not api_key:
        LOGGER.warning(
            "WORKAMA_INTERNAL_LLM_API_KEY not set; skipping gateway LLM call "
            "and falling back to mock extraction."
        )
        return mock_entries, "mock"

    model = os.getenv("WORKAMA_EXTRACTION_MODEL", "gpt-4o-mini")
    messages = [
        {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
        {"role": "user", "content": conversation_text},
    ]
    result = await call_llm(
        messages=messages,
        model=model,
        workspace_id=workspace_id,
        actor=actor,
        temperature=0.2,
    )
    if result["method"] != "llm":
        return mock_entries, "mock"

    raw_content = result["content"]
    entries = _parse_llm_extraction(raw_content)
    if not entries:
        # LLM 返回空或解析失败：若 raw_content 明确为 "[]" 视为 LLM 成功无记忆；
        # 否则回退到 mock（保证端点不返回 500 且总有可用结果）
        cleaned = _strip_code_fence(raw_content).strip()
        if cleaned == "[]":
            return [], "llm"
        return mock_entries, "mock"
    return entries, "llm"


async def _insert_extracted_memory(conn: Any, workspace_id: str, entry: dict) -> str | None:
    """将一条抽取记忆写入 memory_vector 表，返回新 id 或 None。"""
    content = entry["content"]
    kind = entry.get("kind", "semantic")
    importance = entry.get("importance", 3)
    metadata = entry.get("metadata") or {}
    emb = vector_embedding(content)
    vid = new_id("mv")
    result = await conn.execute(
        """
        INSERT INTO memory_vector(id, workspace_id, memory_id, content, kind,
            importance, embedding, last_referenced_at, created_at, expires_at, metadata)
        VALUES (%s, %s, NULL, %s, %s, %s, %s::vector, now(), now(), NULL, %s::jsonb)
        RETURNING id
        """,
        (
            vid,
            workspace_id,
            content,
            kind,
            importance,
            _vector_literal(emb),
            json_dumps(metadata),
        ),
    )
    row = await result.fetchone()
    return row["id"] if row else None


# ============================================================================
# Worker 类（供 platform-worker 通过 job 队列调用）
# ============================================================================


class MemoryVectorIndex:
    """管理记忆向量索引操作（供 platform-worker 调用）。"""

    async def index_memory(
        self,
        memory_id: str,
        content: str,
        workspace_id: str,
        metadata: dict | None = None,
    ) -> str | None:
        """为记忆计算 embedding 并写入 memory_vector 表。"""
        embedding = await _call_embedding(workspace_id, content)
        if len(embedding) != EMBEDDING_DIMENSION:
            return None
        vec_id = new_id("mv")
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO memory_vector(id, workspace_id, memory_id, content, kind,
                        importance, embedding, last_referenced_at, created_at, metadata)
                    VALUES (%s, %s, %s, %s, 'semantic', 3, %s::vector, now(), now(), %s::jsonb)
                    """,
                    (
                        vec_id,
                        workspace_id,
                        memory_id,
                        content,
                        _vector_literal(embedding),
                        json_dumps(metadata or {}),
                    ),
                )
        return vec_id

    async def remove_memory(self, memory_id: str) -> None:
        """按 memory_id 删除向量。"""
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM memory_vector WHERE memory_id = %s",
                (memory_id,),
            )

    async def search_memories(
        self,
        query: str,
        workspace_id: str,
        *,
        limit: int = 5,
        threshold: float = 0.0,
    ) -> list[dict]:
        """语义搜索：返回相似度 >= threshold 的记忆。"""
        embedding = await _call_embedding(workspace_id, query)
        vec_str = _vector_literal(embedding)
        async with pool.connection() as conn:
            result = await conn.execute(
                """
                SELECT id, memory_id, content, metadata,
                       1 - (embedding <=> %s::vector) AS score,
                       last_referenced_at, created_at
                FROM memory_vector
                WHERE workspace_id = %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (vec_str, workspace_id, vec_str, limit),
            )
            rows = await result.fetchall()
        return [dict(r) for r in rows if float(r.get("score") or 0) >= threshold]

    async def reindex_workspace(self, workspace_id: str, vector_ids: list[str] | None = None) -> int:
        """重建工作区所有记忆的 embedding。"""
        async with pool.connection() as conn:
            if vector_ids:
                result = await conn.execute(
                    "SELECT id, content FROM memory_vector WHERE workspace_id = %s AND id = ANY(%s)",
                    (workspace_id, vector_ids),
                )
            else:
                result = await conn.execute(
                    "SELECT id, content FROM memory_vector WHERE workspace_id = %s",
                    (workspace_id,),
                )
            rows = await result.fetchall()
            count = 0
            for row in rows:
                embedding = await _call_embedding(workspace_id, row["content"])
                await conn.execute(
                    "UPDATE memory_vector SET embedding = %s::vector WHERE id = %s",
                    (_vector_literal(embedding), row["id"]),
                )
                count += 1
        return count


class MemoryExtractionWorker:
    """从对话中抽取结构化记忆（供 platform-worker 调用）。"""

    def _parse_extraction(self, raw: str) -> list[dict]:
        """解析 LLM 抽取输出（JSON 数组）。"""
        try:
            match = re.search(r"\[.*\]", raw or "", re.DOTALL)
            if not match:
                return []
            entries = json.loads(match.group())
        except (json.JSONDecodeError, ValueError):
            return []
        result: list[dict] = []
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("content"):
                continue
            entry_type = entry.get("type", "fact")
            if entry_type not in EXTRACTION_KIND_MAP:
                entry_type = "fact"
            importance = max(1, min(5, int(entry.get("importance", 3))))
            confidence = max(0.0, min(1.0, float(entry.get("confidence", 0.5))))
            result.append(
                {
                    "type": entry_type,
                    "content": entry["content"],
                    "importance": importance,
                    "confidence": confidence,
                }
            )
        return result

    async def extract_memories_from_conversation(
        self,
        conversation_text: str,
        workspace_id: str,
        *,
        actor: Any = None,
    ) -> list[dict]:
        """从对话文本抽取记忆并写入向量索引。

        v7.140：通过 gateway 调用 LLM 抽取，失败回退到 mock。
        """
        # worker 上下文可能没有 actor，构造一个最小 service actor 供 LLM 调用使用
        if actor is None:
            actor = Actor(
                user_id="system",
                workspace_id=workspace_id,
                org_id="",
                role="admin",
                email="system@workama.local",
                display_name="System",
                onboarding_completed=True,
                capabilities=("memory:*",),
            )
        entries, method = await _call_llm_for_extraction(
            conversation_text, workspace_id, actor
        )
        created: list[dict] = []
        async with pool.connection() as conn:
            async with conn.transaction():
                for entry in entries:
                    vec_id = await _insert_extracted_memory(conn, workspace_id, entry)
                    if vec_id:
                        created.append({"id": vec_id, "extraction_method": method, **entry})
        return created

    async def process_extraction_job(self, payload: dict) -> dict:
        """处理记忆抽取 job（由 platform-worker 调用）。

        v7.140：通过 gateway 调用 LLM 抽取，失败回退到 mock。
        """
        workspace_id = payload.get("workspace_id", "")
        conversation_text = payload.get("conversation_text", "")
        created = await self.extract_memories_from_conversation(
            conversation_text, workspace_id
        )
        return {"extracted": len(created), "ids": [c["id"] for c in created]}


class MemoryForgettingWorker:
    """应用遗忘曲线清理过期记忆（供 platform-worker 调用）。"""

    async def apply_forgetting_curve(self, workspace_id: str) -> dict:
        """扫描并删除超过保留期的记忆向量。"""
        conditions = " OR ".join(
            f"(importance = {imp} AND last_referenced_at < now() - interval '{days} days')"
            for imp, days in FORGET_RETENTION_DAYS.items()
        )
        async with pool.connection() as conn:
            count_result = await conn.execute(
                "SELECT count(*) AS cnt FROM memory_vector WHERE workspace_id = %s",
                (workspace_id,),
            )
            count_row = await count_result.fetchone()
            scanned = int(count_row["cnt"]) if count_row else 0
            result = await conn.execute(
                f"""
                DELETE FROM memory_vector
                WHERE workspace_id = %s AND ({conditions})
                RETURNING id
                """,
                (workspace_id,),
            )
            rows = await result.fetchall()
        forgotten_ids = [r["id"] for r in rows]
        return {
            "scanned": scanned,
            "forgotten": len(forgotten_ids),
            "forgotten_ids": forgotten_ids,
        }

    async def run_forget_sweep(self, conn, workspace_id: str, threshold_days: int | None = None) -> dict:
        """扫描并删除过期记忆向量。

        - 先删除 ``expires_at < now()`` 的记录
        - 再删除缺少 ``expires_at`` 但 ``created_at + 保留期 < now()`` 的记录
        - ``threshold_days`` 暂作兼容参数，实际保留期由 importance 决定
        """
        result = await conn.execute(
            "DELETE FROM memory_vector WHERE workspace_id = %s AND expires_at < now() RETURNING id",
            (workspace_id,),
        )
        expired_rows = await result.fetchall()

        conditions = " OR ".join(
            f"(importance = {imp} AND created_at < now() - interval '{days} days')"
            for imp, days in FORGET_RETENTION_DAYS.items()
        )
        result = await conn.execute(
            f"DELETE FROM memory_vector WHERE workspace_id = %s AND expires_at IS NULL AND ({conditions}) RETURNING id",
            (workspace_id,),
        )
        retention_rows = await result.fetchall()

        all_ids = [r["id"] for r in expired_rows + retention_rows]
        return {"processed": len(all_ids), "forgotten_ids": all_ids}

    async def process_forgetting_job(self, payload: dict) -> dict:
        """处理遗忘曲线 job（由 platform-worker 调用）。"""
        workspace_id = payload.get("workspace_id", "")
        threshold_days = payload.get("threshold_days")
        async with pool.connection() as conn:
            result = await self.run_forget_sweep(conn, workspace_id, threshold_days)
        return result


# 模块级 Worker 实例（platform-worker 直接 import 使用）
vector_index = MemoryVectorIndex()
extraction_worker = MemoryExtractionWorker()
forgetting_worker = MemoryForgettingWorker()


# ============================================================================
# Router 辅助函数
# ============================================================================


def _require(actor: Actor, action: str) -> None:
    """检查 actor 是否拥有 memory:{action} 能力。"""
    if not capability_allows(actor.capabilities, f"memory:{action}"):
        raise HTTPException(
            status_code=403, detail=f"Missing capability: memory:{action}"
        )


def _summary(row: dict) -> dict:
    """将数据库行转为 API 响应 dict。"""
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "memory_id": row.get("memory_id"),
        "content": row["content"],
        "kind": row["kind"],
        "importance": int(row["importance"]),
        "metadata": row.get("metadata") or {},
        "last_referenced_at": row["last_referenced_at"],
        "created_at": row["created_at"],
        "expires_at": row.get("expires_at"),
    }


async def _owned_vector(conn: Any, vector_id: str, actor: Actor) -> dict:
    """查询向量并校验 workspace 归属。

    - 不存在 → 404
    - 存在但 workspace 不匹配 → 403
    """
    result = await conn.execute(
        "SELECT * FROM memory_vector WHERE id = %s",
        (vector_id,),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Memory vector not found")
    if row["workspace_id"] != actor.workspace_id:
        raise HTTPException(
            status_code=403, detail="Memory vector belongs to another workspace"
        )
    return row


# ============================================================================
# Router 端点
# ============================================================================
#
# 路由声明顺序很重要：具体路径（/health, /recall, /forget-sweep, /extract）
# 必须在参数化路径 /{vector_id} 之前声明，否则 FastAPI 会将 "health" 等
# 当作 vector_id 参数匹配。


@router.get("/health")
async def health(actor: Annotated[Actor, Depends(get_actor)]) -> dict:
    """健康检查。"""
    return {
        "module": "memory_vector",
        "status": "ok",
        "impl": "pgvector",
        "dimensions": EMBEDDING_DIMENSION,
    }


@router.post("/recall")
async def recall_vectors(
    body: VectorRecallRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """向量召回：按余弦相似度排序返回 top_k 结果。

    相似度 = 1 - cosine_distance（pgvector `<=>` 操作符）。
    召回的记忆会自动 touch（重置 last_referenced_at）。
    """
    _require(actor, "read")
    query_embedding = vector_embedding(body.query)
    query_vec_str = _vector_literal(query_embedding)
    async with pool.connection() as conn:
        kind_clause = ""
        params: list[object] = [query_vec_str, actor.workspace_id]
        if body.kind:
            kind_clause = "AND kind = %s"
            params.append(body.kind)
        params.append(query_vec_str)
        params.append(body.top_k)
        result = await conn.execute(
            f"""
            SELECT *, 1 - (embedding <=> %s::vector) AS similarity
            FROM memory_vector
            WHERE workspace_id = %s {kind_clause}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            tuple(params),
        )
        rows = await result.fetchall()
        if rows:
            ids = [r["id"] for r in rows]
            await conn.execute(
                "UPDATE memory_vector SET last_referenced_at = now() "
                "WHERE id = ANY(%s) AND workspace_id = %s",
                (ids, actor.workspace_id),
            )
    items = [
        {**_summary(row), "similarity": float(row.get("similarity") or 0)}
        for row in rows
    ]
    return {
        "query": body.query,
        "top_k": body.top_k,
        "items": items,
        "data": items,
        "count": len(items),
    }


@router.post("/forget-sweep")
async def forget_sweep(actor: Annotated[Actor, Depends(get_actor)]):
    """触发异步遗忘清理任务（admin/owner only）。"""
    _require(actor, "delete")
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin or owner required")
    async with pool.connection() as conn:
        async with conn.transaction():
            operation = await submit_operation(
                conn,
                operation_type="memory.forget_sweep",
                workspace_id=actor.workspace_id,
                org_id=actor.org_id,
                actor_id=actor.user_id,
                actor_role=actor.role,
                idempotency_key=f"forget-sweep:{actor.workspace_id}:{datetime.now(UTC).date().isoformat()}",
                payload={"workspace_id": actor.workspace_id},
                job_type=MEMORY_FORGET_JOB_TYPE,
                queue="platform",
            )
    return {"operation_id": operation["id"], "status": "queued"}


@router.get("/forget-policy")
async def get_forget_policy(actor: Annotated[Actor, Depends(get_actor)]):
    """查询 workspace 遗忘策略配置。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM memory_governance_policy WHERE workspace_id = %s",
            (actor.workspace_id,),
        )
        row = await result.fetchone()
    if not row:
        return ForgetPolicyResponse(
            workspace_id=actor.workspace_id,
            retention_days_by_importance=FORGET_RETENTION_DAYS,
            default_importance=3,
        )
    return ForgetPolicyResponse(
        workspace_id=row["workspace_id"],
        retention_days_by_importance=row.get("retention_days_by_importance") or FORGET_RETENTION_DAYS,
        default_importance=row.get("default_importance", 3),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


@router.post("/forget-policy")
async def update_forget_policy(
    body: ForgetPolicyRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """更新 workspace 遗忘策略配置（admin/owner only）。"""
    _require(actor, "write")
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin or owner required")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                INSERT INTO memory_governance_policy(
                    workspace_id, retention_days_by_importance, default_importance, created_at, updated_at
                ) VALUES (%s, %s::jsonb, %s, now(), now())
                ON CONFLICT(workspace_id) DO UPDATE SET
                    retention_days_by_importance = EXCLUDED.retention_days_by_importance,
                    default_importance = EXCLUDED.default_importance,
                    updated_at = now()
                RETURNING *
                """,
                (
                    actor.workspace_id,
                    json_dumps(body.retention_days_by_importance or FORGET_RETENTION_DAYS),
                    body.default_importance or 3,
                ),
            )
            row = await result.fetchone()
    return ForgetPolicyResponse(
        workspace_id=row["workspace_id"],
        retention_days_by_importance=row.get("retention_days_by_importance") or FORGET_RETENTION_DAYS,
        default_importance=row.get("default_importance", 3),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


@router.post("/extract")
async def extract_memories(
    body: ExtractRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """LLM 自动抽取：从对话文本抽取结构化记忆并写入向量索引。

    v7.140：通过 gateway 调用 LLM 抽取结构化记忆（content/kind/importance/metadata）。
    LLM 不可用或失败时回退到确定性 mock 抽取（基于关键词匹配）：
    - 含 "用户叫" → 抽取名字记忆（profile, importance=4）
    - 含 "喜欢" → 抽取偏好记忆（preference, importance=3）

    返回 ``extraction_method`` 字段标识实际使用的抽取方式（"llm" 或 "mock"）。
    设置 ``WORKAMA_EXTRACTION_DISABLE_LLM=1`` 可强制走 mock（用于测试）。
    """
    _require(actor, "write")
    entries, method = await _call_llm_for_extraction(
        body.conversation_text, actor.workspace_id, actor
    )
    extracted_ids: list[str] = []
    async with pool.connection() as conn:
        async with conn.transaction():
            for entry in entries:
                vid = await _insert_extracted_memory(conn, actor.workspace_id, entry)
                if vid:
                    extracted_ids.append(vid)
    return {
        "extracted_ids": extracted_ids,
        "count": len(extracted_ids),
        "extraction_method": method,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_vector(
    body: VectorCreate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """写入向量：自动生成 1536 维确定性 embedding。"""
    _require(actor, "write")
    vector_id = new_id("mv")
    embedding = vector_embedding(body.content)
    embedding_str = _vector_literal(embedding)
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                INSERT INTO memory_vector(id, workspace_id, memory_id, content, kind, importance,
                    embedding, last_referenced_at, created_at, expires_at, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s::vector, now(), now(), %s, %s::jsonb) RETURNING *
                """,
                (
                    vector_id,
                    actor.workspace_id,
                    body.memory_id,
                    body.content,
                    body.kind,
                    body.importance,
                    embedding_str,
                    body.expires_at,
                    json_dumps(body.metadata),
                ),
            )
            row = await result.fetchone()
    return _summary(row)


@router.get("")
async def list_vectors(
    actor: Annotated[Actor, Depends(get_actor)],
    kind: VectorKind | None = None,
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """列表：分页查询，支持 kind 过滤和 workspace 隔离。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        kind_clause = ""
        params: list[object] = [actor.workspace_id]
        if kind:
            kind_clause = "AND kind = %s"
            params.append(kind)
        params.append(limit)
        params.append(offset)
        result = await conn.execute(
            f"""
            SELECT * FROM memory_vector
            WHERE workspace_id = %s {kind_clause}
            ORDER BY created_at DESC LIMIT %s OFFSET %s
            """,
            tuple(params),
        )
        rows = await result.fetchall()
    items = [_summary(row) for row in rows]
    return {
        "items": items,
        "data": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
    }



@router.get("/annotations")
async def list_annotations(
    actor: Annotated[Actor, Depends(get_actor)],
    vector_id: str | None = None,
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """查询人工标注列表（workspace 隔离）。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        where_clause = "WHERE workspace_id = %s"
        params: list[object] = [actor.workspace_id]
        if vector_id:
            where_clause += " AND vector_id = %s"
            params.append(vector_id)
        params.append(limit)
        params.append(offset)
        result = await conn.execute(
            f"""
            SELECT * FROM memory_vector_annotation
            {where_clause}
            ORDER BY created_at DESC LIMIT %s OFFSET %s
            """,
            tuple(params),
        )
        rows = await result.fetchall()
    items = [
        AnnotationResponse(
            id=r["id"],
            vector_id=r["vector_id"],
            relevance_score=float(r["relevance_score"]),
            accuracy_score=float(r["accuracy_score"]),
            feedback=r.get("feedback"),
            actor_id=r.get("actor_id"),
            created_at=r["created_at"],
        )
        for r in rows
    ]
    return {
        "items": items,
        "data": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
    }


# ============================================================================
# v7.170: 记忆语义层治理深化（聚类 / 重要性调整 / 衰减报告 / 重排序 / 统计 / 显式合并）
# ============================================================================

# --- SCHEMA_STATEMENTS：聚类表 + memory_vector 新列 + governance_log action 约束扩展 ---
SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS memory_cluster (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        cluster_label TEXT NOT NULL,
        centroid_text TEXT NOT NULL DEFAULT '',
        member_count INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_memory_cluster_workspace ON memory_cluster(workspace_id)",
    """
    CREATE TABLE IF NOT EXISTS memory_cluster_member (
        id TEXT PRIMARY KEY,
        cluster_id TEXT NOT NULL REFERENCES memory_cluster(id) ON DELETE CASCADE,
        vector_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(cluster_id, vector_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_memory_cluster_member_cluster ON memory_cluster_member(cluster_id)",
    "CREATE INDEX IF NOT EXISTS idx_memory_cluster_member_workspace ON memory_cluster_member(workspace_id)",
    "ALTER TABLE memory_vector ADD COLUMN IF NOT EXISTS cluster_id TEXT",
    "ALTER TABLE memory_vector ADD COLUMN IF NOT EXISTS merged_into TEXT",
    "ALTER TABLE memory_vector ADD COLUMN IF NOT EXISTS decay_score FLOAT",
    "ALTER TABLE memory_vector ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'",
    "ALTER TABLE memory_vector_governance_log DROP CONSTRAINT IF EXISTS memory_vector_governance_log_action_check",
    "ALTER TABLE memory_vector_governance_log ADD CONSTRAINT memory_vector_governance_log_action_check "
    "CHECK (action IN ('deduplicate', 'merge', 'forget', 'annotate', 'reindex', 'importance_adjust', 'explicit_merge'))",
)


async def ensure_memory_semantic_schema(conn: Any) -> None:
    """执行 SCHEMA_STATEMENTS 中的建表/补列语句。"""
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


# --- Pydantic 模型 ---


class ClusterRequest(BaseModel):
    threshold: float = Field(default=0.85, ge=0.0, le=1.0)


class ClusterMemberItem(BaseModel):
    vector_id: str
    content: str
    similarity: float = 0.0


class ClusterResponse(BaseModel):
    cluster_id: str
    workspace_id: str
    cluster_label: str
    centroid_text: str
    cluster_summary: str
    member_count: int
    members: list[ClusterMemberItem] = Field(default_factory=list)


class ImportanceAdjustRequest(BaseModel):
    importance: int = Field(ge=1, le=5)
    reason: str = Field(default="", max_length=2000)


class DecayReportItem(BaseModel):
    vector_id: str
    content: str
    importance: int
    reference_count: int
    last_referenced_at: datetime
    created_at: datetime | None = None
    decay_score: float
    predicted_retention_days: int
    decay_curve: list[dict[str, Any]] = Field(default_factory=list)


class DecayReportResponse(BaseModel):
    workspace_id: str
    total: int
    items: list[DecayReportItem] = Field(default_factory=list)


class RerankRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    candidate_vector_ids: list[str] = Field(min_length=1)


class RerankResultItem(BaseModel):
    vector_id: str
    score: float


class RerankResponse(BaseModel):
    query: str
    ranked: list[RerankResultItem] = Field(default_factory=list)


class StatsResponse(BaseModel):
    workspace_id: str
    total_memories: int
    by_importance: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, int] = Field(default_factory=dict)
    avg_reference_count: float
    recent_7d_trend: list[dict[str, Any]] = Field(default_factory=list)
    governance_action_counts: dict[str, int] = Field(default_factory=dict)


class MergeRequest(BaseModel):
    source_vector_ids: list[str] = Field(min_length=1)
    target_vector_id: str


class MergeResponse(BaseModel):
    target_vector_id: str
    merged_count: int
    merged_source_ids: list[str] = Field(default_factory=list)


# --- 辅助函数：衰减计算 ---


def _importance_factor(importance: int) -> float:
    """importance 1-5 → 0.2-1.0。"""
    return max(1, min(5, importance)) * 0.2


def _recency_factor(last_referenced_at: datetime, now: datetime | None = None) -> float:
    """基于 last_referenced_at 的近度因子，0-1，越近越接近 1。"""
    if now is None:
        now = datetime.now(UTC)
    if not last_referenced_at:
        return 0.0
    if last_referenced_at.tzinfo is None:
        last_referenced_at = last_referenced_at.replace(tzinfo=UTC)
    delta_days = (now - last_referenced_at).total_seconds() / 86400.0
    # 30 天线性衰减到 0
    return max(0.0, min(1.0, 1.0 - delta_days / 30.0))


def _reference_factor(reference_count: int) -> float:
    """引用因子 = 1.0 + min(reference_count, 10) * 0.05。"""
    return 1.0 + min(max(0, reference_count), 10) * 0.05


def _compute_decay_score(
    importance: int,
    last_referenced_at: datetime,
    reference_count: int,
    now: datetime | None = None,
) -> float:
    """计算当前衰减分（0-1）= importance_factor * recency_factor * reference_factor。"""
    score = (
        _importance_factor(importance)
        * _recency_factor(last_referenced_at, now)
        * _reference_factor(reference_count)
    )
    return max(0.0, min(1.0, score))


def _base_days(importance: int) -> int:
    """importance → 基础保留天数。"""
    return FORGET_RETENTION_DAYS.get(max(1, min(5, importance)), 30)


def _predicted_retention_days(importance: int, decay_score: float) -> int:
    """预计保留天数 = base_days(importance) * decay_score。"""
    return max(0, int(_base_days(importance) * decay_score))


def _compute_decay_curve(
    importance: int,
    last_referenced_at: datetime,
    reference_count: int,
    *,
    days: int = 30,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """采样最近 days 天每天的衰减分。"""
    if now is None:
        now = datetime.now(UTC)
    curve: list[dict[str, Any]] = []
    for i in range(days):
        sample_time = now - timedelta(days=days - 1 - i)
        score = _compute_decay_score(importance, last_referenced_at, reference_count, sample_time)
        curve.append(
            {"day": i, "date": sample_time.date().isoformat(), "decay_score": round(score, 4)}
        )
    return curve


# --- 辅助函数：视图转换 ---


def _cluster_view(row: dict) -> dict:
    """将聚类行转为 API 响应 dict。"""
    return {
        "cluster_id": row["id"],
        "workspace_id": row["workspace_id"],
        "cluster_label": row.get("cluster_label") or "",
        "centroid_text": row.get("centroid_text") or "",
        "member_count": int(row.get("member_count") or 0),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _decay_report_view(row: dict, now: datetime | None = None) -> dict:
    """将记忆行转为衰减报告项。"""
    importance = int(row.get("importance") or 3)
    last_ref = row.get("last_referenced_at") or datetime.now(UTC)
    ref_count = int(row.get("reference_count") or 0)
    decay_score = _compute_decay_score(importance, last_ref, ref_count, now)
    retention = _predicted_retention_days(importance, decay_score)
    curve = _compute_decay_curve(importance, last_ref, ref_count, now=now)
    return {
        "vector_id": row["id"],
        "content": row.get("content") or "",
        "importance": importance,
        "reference_count": ref_count,
        "last_referenced_at": last_ref,
        "created_at": row.get("created_at"),
        "decay_score": round(decay_score, 4),
        "predicted_retention_days": retention,
        "decay_curve": curve,
    }


def _stats_view(
    workspace_id: str,
    rows: list[dict],
    gov_rows: list[dict],
    trend_rows: list[dict],
) -> dict:
    """聚合统计视图。"""
    by_importance: dict[str, int] = {}
    by_status: dict[str, int] = {}
    ref_sum = 0
    for r in rows:
        imp = str(int(r.get("importance") or 0))
        by_importance[imp] = by_importance.get(imp, 0) + 1
        st = r.get("status") or "active"
        by_status[st] = by_status.get(st, 0) + 1
        ref_sum += int(r.get("reference_count") or 0)
    total = len(rows)
    avg_ref = ref_sum / total if total > 0 else 0.0
    gov_counts: dict[str, int] = {}
    for g in gov_rows:
        action = g.get("action") or "unknown"
        gov_counts[action] = gov_counts.get(action, 0) + int(g.get("cnt") or 0)
    trend = [
        {"date": t.get("date") or "", "count": int(t.get("cnt") or 0)}
        for t in trend_rows
    ]
    return {
        "workspace_id": workspace_id,
        "total_memories": total,
        "by_importance": by_importance,
        "by_status": by_status,
        "avg_reference_count": round(avg_ref, 4),
        "recent_7d_trend": trend,
        "governance_action_counts": gov_counts,
    }


def _merge_view(target_id: str, merged_ids: list[str]) -> dict:
    """合并结果视图。"""
    return {
        "target_vector_id": target_id,
        "merged_count": len(merged_ids),
        "merged_source_ids": merged_ids,
    }


async def _generate_cluster_summary(workspace_id: str, members: list[dict]) -> str:
    """用 LLM 生成聚类摘要，失败时 fallback 取最短成员 content 前 100 字符。"""
    if not members:
        return ""
    try:
        contents = [m.get("content") or "" for m in members]
        joined = "\n".join(contents[:10])
        messages = [
            {
                "role": "system",
                "content": "You are a clustering summarizer. Generate a concise summary "
                "(max 100 chars) of the following memory cluster members.",
            },
            {"role": "user", "content": joined},
        ]
        raw = await _call_llm(workspace_id, messages)
        cleaned = _strip_code_fence(raw).strip()
        # _call_llm 默认返回 "[]"，此时回退
        if cleaned and cleaned != "[]" and len(cleaned) > 2:
            return cleaned[:200]
    except Exception:
        LOGGER.warning("LLM cluster summary failed, falling back", exc_info=True)
    # fallback：取最短成员 content 前 100 字符
    shortest = min((m.get("content") or "" for m in members), key=len) if members else ""
    return shortest[:100]


# --- 新增端点（静态路径必须在 /{vector_id} 之前声明） ---


@router.post("/cluster")
async def cluster_memories(
    body: ClusterRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """语义聚类：基于 cosine similarity 将 workspace 记忆分组（贪心单链聚类）。"""
    _require(actor, "write")
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT id, content, embedding, importance, workspace_id "
            "FROM memory_vector WHERE workspace_id = %s AND status = 'active' "
            "ORDER BY created_at ASC",
            (actor.workspace_id,),
        )
        rows = await result.fetchall()

        clusters: list[dict] = []
        for row in rows:
            emb = row.get("embedding")
            if emb is None:
                emb = vector_embedding(row["content"])
            assigned = False
            for c in clusters:
                sim = cosine_similarity(emb, c["centroid_emb"])
                if sim >= body.threshold:
                    c["members"].append(row)
                    c["member_count"] += 1
                    assigned = True
                    break
            if not assigned:
                clusters.append(
                    {
                        "id": new_id("mc"),
                        "workspace_id": actor.workspace_id,
                        "cluster_label": f"cluster_{len(clusters) + 1}",
                        "centroid_text": row["content"][:200],
                        "centroid_emb": emb,
                        "members": [row],
                        "member_count": 1,
                    }
                )

        cluster_responses = []
        for c in clusters:
            summary = await _generate_cluster_summary(actor.workspace_id, c["members"])
            member_ids = [m["id"] for m in c["members"]]
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO memory_cluster(
                        id, workspace_id, cluster_label, centroid_text,
                        member_count, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, now(), now())
                    """,
                    (
                        c["id"],
                        actor.workspace_id,
                        c["cluster_label"],
                        c["centroid_text"],
                        c["member_count"],
                    ),
                )
                for mid in member_ids:
                    await conn.execute(
                        """
                        INSERT INTO memory_cluster_member(
                            id, cluster_id, vector_id, workspace_id, created_at)
                        VALUES (%s, %s, %s, %s, now())
                        ON CONFLICT DO NOTHING
                        """,
                        (new_id("mcm"), c["id"], mid, actor.workspace_id),
                    )
                    await conn.execute(
                        "UPDATE memory_vector SET cluster_id = %s "
                        "WHERE id = %s AND workspace_id = %s",
                        (c["id"], mid, actor.workspace_id),
                    )
            cluster_responses.append(
                {
                    "cluster_id": c["id"],
                    "workspace_id": actor.workspace_id,
                    "cluster_label": c["cluster_label"],
                    "centroid_text": c["centroid_text"],
                    "cluster_summary": summary,
                    "member_count": c["member_count"],
                    "members": [
                        {"vector_id": m["id"], "content": m["content"], "similarity": 1.0}
                        for m in c["members"]
                    ],
                }
            )
    return {
        "workspace_id": actor.workspace_id,
        "threshold": body.threshold,
        "cluster_count": len(cluster_responses),
        "clusters": cluster_responses,
    }


@router.get("/clusters")
async def list_clusters(
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """聚类结果列表（分页，按 member_count 倒序）。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT * FROM memory_cluster
            WHERE workspace_id = %s
            ORDER BY member_count DESC, created_at DESC
            LIMIT %s OFFSET %s
            """,
            (actor.workspace_id, limit, offset),
        )
        rows = await result.fetchall()
        count_result = await conn.execute(
            "SELECT count(*) AS cnt FROM memory_cluster WHERE workspace_id = %s",
            (actor.workspace_id,),
        )
        count_row = await count_result.fetchone()
    items = [_cluster_view(r) for r in rows]
    return {
        "items": items,
        "data": items,
        "count": len(items),
        "total": int(count_row["cnt"]) if count_row else 0,
        "limit": limit,
        "offset": offset,
    }


@router.get("/decay-report")
async def decay_report(
    actor: Annotated[Actor, Depends(get_actor)],
):
    """衰减可视化报告（排除已合并记忆）。"""
    _require(actor, "read")
    now = datetime.now(UTC)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, content, importance, reference_count,
                   last_referenced_at, created_at
            FROM memory_vector
            WHERE workspace_id = %s
              AND status = 'active'
              AND merged_into IS NULL
            ORDER BY created_at DESC
            """,
            (actor.workspace_id,),
        )
        rows = await result.fetchall()
    items = [_decay_report_view(r, now) for r in rows]
    return {
        "workspace_id": actor.workspace_id,
        "total": len(items),
        "items": items,
    }


@router.get("/stats")
async def memory_stats(
    actor: Annotated[Actor, Depends(get_actor)],
):
    """记忆统计仪表盘。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT importance, status, reference_count
            FROM memory_vector
            WHERE workspace_id = %s
            """,
            (actor.workspace_id,),
        )
        rows = await result.fetchall()

        trend_result = await conn.execute(
            """
            SELECT to_char(created_at, 'YYYY-MM-DD') AS date, count(*) AS cnt
            FROM memory_vector
            WHERE workspace_id = %s AND created_at >= now() - interval '7 days'
            GROUP BY 1 ORDER BY 1
            """,
            (actor.workspace_id,),
        )
        trend_rows = await trend_result.fetchall()

        gov_result = await conn.execute(
            """
            SELECT action, count(*) AS cnt
            FROM memory_vector_governance_log
            WHERE workspace_id = %s
            GROUP BY 1
            """,
            (actor.workspace_id,),
        )
        gov_rows = await gov_result.fetchall()
    return _stats_view(actor.workspace_id, rows, gov_rows, trend_rows)


@router.post("/rerank")
async def rerank_candidates(
    body: RerankRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """检索重排序：score = 0.5*cosine_sim + 0.3*importance_norm + 0.2*recency。"""
    _require(actor, "read")
    if not body.query.strip():
        raise HTTPException(status_code=422, detail="query must not be empty")
    query_emb = vector_embedding(body.query)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, content, importance, last_referenced_at, embedding
            FROM memory_vector
            WHERE workspace_id = %s AND id = ANY(%s) AND status = 'active'
            """,
            (actor.workspace_id, body.candidate_vector_ids),
        )
        rows = await result.fetchall()
    now = datetime.now(UTC)
    scored: list[dict] = []
    for r in rows:
        emb = r.get("embedding")
        if emb is None:
            emb = vector_embedding(r["content"])
        sim = cosine_similarity(query_emb, emb)
        imp_norm = _importance_to_score(int(r["importance"]))
        recency = _recency_factor(r.get("last_referenced_at") or now, now)
        score = 0.5 * sim + 0.3 * imp_norm + 0.2 * recency
        scored.append({"vector_id": r["id"], "score": round(score, 4)})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return {
        "query": body.query,
        "ranked": scored,
    }


@router.post("/merge")
async def merge_memories(
    body: MergeRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """显式合并：source 的 reference_count 累加到 target，source 标记 merged。"""
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            # 校验 target 存在且属于当前 workspace
            target_row = await _owned_vector(conn, body.target_vector_id, actor)
            if target_row.get("merged_into"):
                raise HTTPException(
                    status_code=400, detail="Target vector is already merged"
                )
            if target_row.get("status") == "merged":
                raise HTTPException(
                    status_code=400, detail="Target vector is already merged"
                )

            merged_ids: list[str] = []
            total_ref = 0
            for src_id in body.source_vector_ids:
                src_row = await _owned_vector(conn, src_id, actor)
                if src_row.get("status") == "merged" or src_row.get("merged_into"):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Source vector {src_id} is already merged",
                    )
                src_ref = int(src_row.get("reference_count") or 0)
                total_ref += src_ref
                merged_ids.append(src_id)
                await conn.execute(
                    """
                    UPDATE memory_vector
                    SET merged_into = %s, status = 'merged'
                    WHERE id = %s AND workspace_id = %s
                    """,
                    (body.target_vector_id, src_id, actor.workspace_id),
                )
                await conn.execute(
                    """
                    INSERT INTO memory_vector_governance_log(
                        id, workspace_id, action, source_vector_id,
                        target_vector_id, details, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, now())
                    """,
                    (
                        new_id("mvg"),
                        actor.workspace_id,
                        "explicit_merge",
                        src_id,
                        body.target_vector_id,
                        json_dumps({"reference_count_transferred": src_ref}),
                    ),
                )

            # 累加 reference_count 到 target
            await conn.execute(
                """
                UPDATE memory_vector
                SET reference_count = reference_count + %s
                WHERE id = %s AND workspace_id = %s
                """,
                (total_ref, body.target_vector_id, actor.workspace_id),
            )
    return _merge_view(body.target_vector_id, merged_ids)


@router.post("/{vector_id}/importance")
async def adjust_importance(
    vector_id: str,
    body: ImportanceAdjustRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """动态调整记忆重要性（1-5 级），写 governance_log。"""
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await _owned_vector(conn, vector_id, actor)
            old_importance = int(row["importance"])
            await conn.execute(
                "UPDATE memory_vector SET importance = %s "
                "WHERE id = %s AND workspace_id = %s",
                (body.importance, vector_id, actor.workspace_id),
            )
            await conn.execute(
                """
                INSERT INTO memory_vector_governance_log(
                    id, workspace_id, action, source_vector_id, details, created_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, now())
                """,
                (
                    new_id("mvg"),
                    actor.workspace_id,
                    "importance_adjust",
                    vector_id,
                    json_dumps(
                        {
                            "old": old_importance,
                            "new": body.importance,
                            "reason": body.reason,
                        }
                    ),
                ),
            )
    return {
        "vector_id": vector_id,
        "old_importance": old_importance,
        "new_importance": body.importance,
        "reason": body.reason,
    }


@router.get("/{vector_id}")
async def get_vector(
    vector_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """查询单条向量。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        return _summary(await _owned_vector(conn, vector_id, actor))


@router.delete("/{vector_id}", status_code=status.HTTP_200_OK)
async def delete_vector(
    vector_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """删除向量（硬删除）。"""
    _require(actor, "delete")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _owned_vector(conn, vector_id, actor)
            result = await conn.execute(
                "DELETE FROM memory_vector WHERE id = %s AND workspace_id = %s RETURNING id",
                (vector_id, actor.workspace_id),
            )
            row = await result.fetchone()
    return {"id": row["id"], "deleted": True}


@router.post("/{vector_id}/touch")
async def touch_vector(
    vector_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """引用重置计时：更新 last_referenced_at = now()，重置遗忘曲线。"""
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _owned_vector(conn, vector_id, actor)
            result = await conn.execute(
                "UPDATE memory_vector SET last_referenced_at = now() "
                "WHERE id = %s AND workspace_id = %s RETURNING *",
                (vector_id, actor.workspace_id),
            )
            row = await result.fetchone()
    return _summary(row)


# ============================================================================
# v7.164-A: 记忆完整形态（跨会话记忆治理、人工标注回流、遗忘曲线增强）
# ============================================================================

class GovernRequest(BaseModel):
    action: Literal["deduplicate", "merge", "forget_sweep"] = "deduplicate"
    threshold: float = Field(default=0.95, ge=0.0, le=1.0)


class GovernResponse(BaseModel):
    action: str
    processed: int
    removed: int
    merged: int
    details: list[dict] = Field(default_factory=list)


class AnnotationCreate(BaseModel):
    relevance_score: float = Field(default=0.5, ge=0.0, le=1.0)
    accuracy_score: float = Field(default=0.5, ge=0.0, le=1.0)
    feedback: str | None = Field(default=None, max_length=2000)


class AnnotationResponse(BaseModel):
    id: str
    vector_id: str
    relevance_score: float
    accuracy_score: float
    feedback: str | None
    actor_id: str | None
    created_at: datetime


class MemoryGovernanceWorker:
    """跨会话记忆治理 Worker（workspace 级去重、合并相似记忆）。"""

    async def deduplicate_workspace(self, workspace_id: str, threshold: float = 0.95) -> dict:
        """基于向量相似度去重：保留最新/最重要版本，删除重复。"""
        async with pool.connection() as conn:
            result = await conn.execute(
                """
                SELECT id, content, embedding, importance, created_at
                FROM memory_vector
                WHERE workspace_id = %s
                ORDER BY created_at DESC
                """,
                (workspace_id,),
            )
            rows = await result.fetchall()

        removed_ids: list[str] = []
        kept: list[dict] = []
        for row in rows:
            emb = row.get("embedding")
            if emb is None:
                emb = vector_embedding(row["content"])
            is_duplicate = False
            for k in kept:
                sim = cosine_similarity(emb, k["embedding"])
                if sim >= threshold:
                    is_duplicate = True
                    break
            if is_duplicate:
                removed_ids.append(row["id"])
            else:
                kept.append({"id": row["id"], "embedding": emb})

        if removed_ids:
            async with pool.connection() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "DELETE FROM memory_vector WHERE id = ANY(%s) AND workspace_id = %s",
                        (removed_ids, workspace_id),
                    )
                    for rid in removed_ids:
                        await conn.execute(
                            """
                            INSERT INTO memory_vector_governance_log(
                                id, workspace_id, action, source_vector_id, details, created_at)
                            VALUES (%s, %s, %s, %s, %s::jsonb, now())
                            """,
                            (new_id("mvg"), workspace_id, "deduplicate", rid,
                             json_dumps({"reason": "duplicate_by_similarity", "threshold": threshold})),
                        )
        return {
            "scanned": len(rows),
            "removed": len(removed_ids),
            "removed_ids": removed_ids,
        }

    async def merge_similar_memories(
        self, workspace_id: str, threshold: float = 0.90
    ) -> dict:
        """合并高相似度记忆：将相似记忆内容合并为一条，并更新引用计数。"""
        async with pool.connection() as conn:
            result = await conn.execute(
                """
                SELECT id, content, embedding, importance, metadata, reference_count
                FROM memory_vector
                WHERE workspace_id = %s
                ORDER BY importance DESC, created_at DESC
                """,
                (workspace_id,),
            )
            rows = await result.fetchall()

        merged_count = 0
        merged_details: list[dict] = []
        visited: set[str] = set()

        for i, row in enumerate(rows):
            if row["id"] in visited:
                continue
            emb_i = row.get("embedding") or vector_embedding(row["content"])
            group = [row]
            for j in range(i + 1, len(rows)):
                other = rows[j]
                if other["id"] in visited:
                    continue
                emb_j = other.get("embedding") or vector_embedding(other["content"])
                sim = cosine_similarity(emb_i, emb_j)
                if sim >= threshold:
                    group.append(other)
                    visited.add(other["id"])
            if len(group) > 1:
                visited.add(row["id"])
                merged_content = " | ".join(g["content"] for g in group)
                merged_importance = max(g["importance"] for g in group)
                merged_ref_count = sum(g.get("reference_count", 0) for g in group)
                merged_metadata = {}
                for g in group:
                    merged_metadata.update(g.get("metadata") or {})
                merged_metadata["merged_from"] = [g["id"] for g in group]

                new_id_val = new_id("mv")
                async with pool.connection() as conn:
                    async with conn.transaction():
                        await conn.execute(
                            """
                            INSERT INTO memory_vector(
                                id, workspace_id, memory_id, content, kind, importance,
                                embedding, last_referenced_at, created_at, expires_at, metadata, reference_count)
                            VALUES (%s, %s, NULL, %s, %s, %s, %s::vector, now(), now(), NULL, %s::jsonb, %s)
                            """,
                            (
                                new_id_val,
                                workspace_id,
                                merged_content,
                                "semantic",
                                merged_importance,
                                _vector_literal(vector_embedding(merged_content)),
                                json_dumps(merged_metadata),
                                merged_ref_count,
                            ),
                        )
                        for g in group:
                            await conn.execute(
                                "DELETE FROM memory_vector WHERE id = %s AND workspace_id = %s",
                                (g["id"], workspace_id),
                            )
                            await conn.execute(
                                """
                                INSERT INTO memory_vector_governance_log(
                                    id, workspace_id, action, source_vector_id, target_vector_id, details, created_at)
                                VALUES (%s, %s, %s, %s, %s, %s::jsonb, now())
                                """,
                                (new_id("mvg"), workspace_id, "merge", g["id"], new_id_val,
                                 json_dumps({"group_size": len(group)})),
                            )
                merged_count += len(group)
                merged_details.append({"merged_id": new_id_val, "sources": [g["id"] for g in group]})

        return {
            "scanned": len(rows),
            "merged_groups": len(merged_details),
            "merged_count": merged_count,
            "details": merged_details,
        }

    async def process_governance_job(self, payload: dict) -> dict:
        """处理治理 job（由 platform-worker 调用）。"""
        workspace_id = payload.get("workspace_id", "")
        action = payload.get("action", "deduplicate")
        threshold = float(payload.get("threshold", 0.95))
        if action == "deduplicate":
            return await self.deduplicate_workspace(workspace_id, threshold)
        if action == "merge":
            return await self.merge_similar_memories(workspace_id, threshold)
        return {"error": "unknown_action"}


# 增强遗忘 Worker：基于引用计数调整保留期
class _EnhancedForgettingWorker(MemoryForgettingWorker):
    """增强版遗忘 Worker，被引用记忆延长保留期。"""

    async def apply_forgetting_curve(self, workspace_id: str) -> dict:
        """扫描并删除超过保留期的记忆向量（引用计数越高，保留期越长）。"""
        conditions = " OR ".join(
            f"(importance = {imp} AND last_referenced_at < now() - interval '{days} days' * (1 + reference_count * 0.1))"
            for imp, days in FORGET_RETENTION_DAYS.items()
        )
        async with pool.connection() as conn:
            count_result = await conn.execute(
                "SELECT count(*) AS cnt FROM memory_vector WHERE workspace_id = %s",
                (workspace_id,),
            )
            count_row = await count_result.fetchone()
            scanned = int(count_row["cnt"]) if count_row else 0
            result = await conn.execute(
                f"""
                DELETE FROM memory_vector
                WHERE workspace_id = %s AND ({conditions})
                RETURNING id
                """,
                (workspace_id,),
            )
            rows = await result.fetchall()
        forgotten_ids = [r["id"] for r in rows]
        if forgotten_ids:
            async with pool.connection() as conn:
                async with conn.transaction():
                    for fid in forgotten_ids:
                        await conn.execute(
                            """
                            INSERT INTO memory_vector_governance_log(
                                id, workspace_id, action, source_vector_id, details, created_at)
                            VALUES (%s, %s, %s, %s, %s::jsonb, now())
                            """,
                            (new_id("mvg"), workspace_id, "forget", fid,
                             json_dumps({"reason": "forgetting_curve"})),
                        )
        return {
            "scanned": scanned,
            "forgotten": len(forgotten_ids),
            "forgotten_ids": forgotten_ids,
        }


# 模块级 Worker 实例（覆盖旧 forgetting_worker）
governance_worker = MemoryGovernanceWorker()
forgetting_worker = _EnhancedForgettingWorker()


# ============================================================================
# 新增 REST 端点
# ============================================================================

@router.post("/govern")
async def govern_vectors(
    body: GovernRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """触发记忆治理（去重 / 合并）。"""
    _require(actor, "write")
    if body.action == "deduplicate":
        result = await governance_worker.deduplicate_workspace(actor.workspace_id, body.threshold)
    elif body.action == "merge":
        result = await governance_worker.merge_similar_memories(actor.workspace_id, body.threshold)
    elif body.action == "forget_sweep":
        result = await forgetting_worker.apply_forgetting_curve(actor.workspace_id)
    else:
        raise HTTPException(status_code=400, detail="Unknown governance action")
    return {
        "action": body.action,
        "workspace_id": actor.workspace_id,
        **result,
    }


@router.post("/{vector_id}/annotate")
async def annotate_vector(
    vector_id: str,
    body: AnnotationCreate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """人工标注回流：对记忆进行相关性/准确性评分。"""
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            await _owned_vector(conn, vector_id, actor)
            aid = new_id("mva")
            result = await conn.execute(
                """
                INSERT INTO memory_vector_annotation(
                    id, workspace_id, vector_id, relevance_score, accuracy_score, feedback, actor_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                RETURNING *
                """,
                (
                    aid,
                    actor.workspace_id,
                    vector_id,
                    body.relevance_score,
                    body.accuracy_score,
                    body.feedback,
                    actor.user_id,
                ),
            )
            row = await result.fetchone()
            # 根据标注调整 importance（relevance_score > 0.8 提升，< 0.3 降低）
            delta = 0
            if body.relevance_score > 0.8:
                delta = 1
            elif body.relevance_score < 0.3:
                delta = -1
            if delta != 0:
                await conn.execute(
                    """
                    UPDATE memory_vector
                    SET importance = GREATEST(1, LEAST(5, importance + %s)),
                        reference_count = reference_count + 1
                    WHERE id = %s AND workspace_id = %s
                    """,
                    (delta, vector_id, actor.workspace_id),
                )
    return AnnotationResponse(
        id=row["id"],
        vector_id=row["vector_id"],
        relevance_score=float(row["relevance_score"]),
        accuracy_score=float(row["accuracy_score"]),
        feedback=row.get("feedback"),
        actor_id=row.get("actor_id"),
        created_at=row["created_at"],
    )
