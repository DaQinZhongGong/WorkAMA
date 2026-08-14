"""WorkAMA SDK 数据模型（基于 pydantic v2）。

模型仅用于类型提示与结构化返回，HTTP 层不依赖这些模型，
服务端返回的 JSON 始终会被解析为 ``dict`` 后再选择性验证为模型。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ChatResponse(BaseModel):
    """Agent 对话响应。"""

    agent_id: str
    session_id: Optional[str] = None
    message: str = ""
    role: str = "assistant"
    usage: Dict[str, Any] = Field(default_factory=dict)
    raw: Dict[str, Any] = Field(default_factory=dict)


class AgentInfo(BaseModel):
    """Agent 摘要信息。"""

    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None


class ListResponse(BaseModel):
    """通用分页列表响应。"""

    items: List[Any] = Field(default_factory=list)
    next_cursor: Optional[str] = None
    total: Optional[int] = None


class MemoryRecord(BaseModel):
    """写入/检索记忆的响应。"""

    id: Optional[str] = None
    content: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)
    importance: int = 3
    score: Optional[float] = None


class RecallResponse(BaseModel):
    """记忆检索结果。"""

    items: List[MemoryRecord] = Field(default_factory=list)


class KnowledgeHit(BaseModel):
    """知识库命中条目。"""

    id: Optional[str] = None
    content: str = ""
    score: Optional[float] = None
    dataset_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    """知识搜索响应。"""

    items: List[KnowledgeHit] = Field(default_factory=list)
    total: Optional[int] = None


class WorkflowRunResponse(BaseModel):
    """工作流执行响应。"""

    run_id: Optional[str] = None
    status: Optional[str] = None
    outputs: Dict[str, Any] = Field(default_factory=dict)
    raw: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# P2：第三方集成扩展模型
# ---------------------------------------------------------------------------


class Workflow(BaseModel):
    """工作流定义。"""

    id: Optional[str] = None
    name: str = ""
    description: Optional[str] = None
    status: Optional[str] = None
    version: Optional[int] = None
    graph: Dict[str, Any] = Field(default_factory=dict)
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class WorkflowRun(BaseModel):
    """工作流运行实例。"""

    id: Optional[str] = None
    run_id: Optional[str] = None
    workflow_id: Optional[str] = None
    status: Optional[str] = None
    input: Dict[str, Any] = Field(default_factory=dict)
    output: Dict[str, Any] = Field(default_factory=dict)
    outputs: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    error_category: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class KnowledgeBase(BaseModel):
    """知识库。"""

    id: Optional[str] = None
    name: str = ""
    description: Optional[str] = None
    kind: Optional[str] = None
    embedding_model: Optional[str] = None
    embedding_dimensions: Optional[int] = None
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None
    status: Optional[str] = None
    document_count: Optional[int] = None
    created_at: Optional[str] = None


class Document(BaseModel):
    """知识库文档。"""

    id: Optional[str] = None
    knowledge_base_id: Optional[str] = None
    title: Optional[str] = None
    source_type: Optional[str] = None
    source_url: Optional[str] = None
    content: str = ""
    content_hash: Optional[str] = None
    chunk_count: Optional[int] = None
    status: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None


class QueryResult(BaseModel):
    """知识库检索结果条目。"""

    id: Optional[str] = None
    content: str = ""
    score: Optional[float] = None
    similarity: Optional[float] = None
    knowledge_base_id: Optional[str] = None
    document_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class Agent(BaseModel):
    """Agent（助手）。"""

    id: Optional[str] = None
    name: str = ""
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    tools: List[str] = Field(default_factory=list)
    knowledge_base_ids: List[str] = Field(default_factory=list)
    memory_enabled: Optional[bool] = None
    status: Optional[str] = None
    version: Optional[int] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    """Agent 对话消息（请求/响应统一结构）。"""

    role: str = "user"
    content: str = ""
    agent_id: Optional[str] = None
    conversation_id: Optional[str] = None
    run_id: Optional[str] = None
    usage: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class FileMetadata(BaseModel):
    """文件元数据。"""

    id: Optional[str] = None
    name: str = ""
    kind: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    sha256: Optional[str] = None
    storage_path: Optional[str] = None
    storage_bucket: Optional[str] = None
    status: Optional[str] = None
    uploaded_by: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None


class Automation(BaseModel):
    """自动化触发器。"""

    id: Optional[str] = None
    trigger_id: Optional[str] = None
    name: str = ""
    type: Optional[str] = None
    event_type: Optional[str] = None
    schedule: Optional[str] = None
    enabled: Optional[bool] = None
    status: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)
    action: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class AutomationRun(BaseModel):
    """自动化运行实例。"""

    id: Optional[str] = None
    run_id: Optional[str] = None
    trigger_id: Optional[str] = None
    status: Optional[str] = None
    input: Dict[str, Any] = Field(default_factory=dict)
    output: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class Skill(BaseModel):
    """技能市场条目。"""

    id: Optional[str] = None
    skill_id: Optional[str] = None
    name: str = ""
    description: Optional[str] = None
    version: Optional[str] = None
    author: Optional[str] = None
    category: Optional[str] = None
    status: Optional[str] = None
    installed: Optional[bool] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "ChatResponse",
    "AgentInfo",
    "ListResponse",
    "MemoryRecord",
    "RecallResponse",
    "KnowledgeHit",
    "SearchResponse",
    "WorkflowRunResponse",
    "Workflow",
    "WorkflowRun",
    "KnowledgeBase",
    "Document",
    "QueryResult",
    "Agent",
    "ChatMessage",
    "FileMetadata",
    "Automation",
    "AutomationRun",
    "Skill",
]
