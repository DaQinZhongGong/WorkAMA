"""WorkAMA Python SDK 顶层包。

公开 ``WorkAMAClient`` 主类与所有异常类型，便于 ``from workama_sdk import ...`` 使用。
"""

from __future__ import annotations

from .client import WorkAMAClient
from .exceptions import (
    AuthenticationError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
    WorkAMAError,
)
from .models import (
    Agent,
    AgentInfo,
    Automation,
    AutomationRun,
    ChatMessage,
    ChatResponse,
    Document,
    FileMetadata,
    KnowledgeBase,
    KnowledgeHit,
    ListResponse,
    MemoryRecord,
    QueryResult,
    RecallResponse,
    SearchResponse,
    Skill,
    Workflow,
    WorkflowRun,
    WorkflowRunResponse,
)

__version__ = "0.1.0"

__all__ = [
    "WorkAMAClient",
    "WorkAMAError",
    "AuthenticationError",
    "ForbiddenError",
    "NotFoundError",
    "RateLimitError",
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
    "__version__",
]
