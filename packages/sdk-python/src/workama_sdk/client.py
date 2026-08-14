"""WorkAMA Python SDK 主客户端实现。

仅使用 Python 标准库 ``urllib.request`` 发起 HTTP 请求，不引入 requests/httpx。
支持两种鉴权方式：Bearer Token（``access_token``）与 API Key（``api_key``）。

P2 扩展：新增工作流 / 知识库 / Agent / 文件 / 自动化 / 技能 等资源的完整 CRUD 方法，
所有方法均支持 ``workspace_id`` 透传（以 ``X-Workspace-Id`` 请求头下发）。
"""

from __future__ import annotations

import json as _json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid as _uuid
from typing import Any, Dict, Mapping, Optional

from .exceptions import (
    AuthenticationError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
    WorkAMAError,
)

# 默认 User-Agent，便于服务端识别 SDK 流量
_USER_AGENT = "workama-sdk-python/0.1.0"

# workspace 隔离头
_WORKSPACE_HEADER = "X-Workspace-Id"


class WorkAMAClient:
    """WorkAMA 平台 Python 客户端。

    :param base_url: 平台 API 基地址，例如 ``http://localhost:20200``
    :param api_key: 可选，API Key，会以 ``X-WorkAMA-API-Key`` 头部发送
    :param access_token: 可选，Bearer Token，优先级高于 ``api_key``
    :param timeout: 请求超时秒数，默认 30 秒

    ``api_key`` 与 ``access_token`` 至少传入一个，否则后续请求会返回 401。
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        access_token: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        # 去掉末尾斜杠，避免拼接出双斜杠
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.access_token = access_token
        self.timeout = timeout
        # 保留一个 opener，便于子类/测试替换
        self._opener = urllib.request.build_opener()

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def chat(
        self,
        agent_id: str,
        message: str,
        session_id: Optional[str] = None,
        stream: bool = False,
    ) -> Dict[str, Any]:
        """与指定 Agent 进行对话。

        :param agent_id: Agent ID
        :param message: 用户消息文本
        :param session_id: 可选，会话 ID，用于多轮对话续接
        :param stream: 是否以流式方式返回（当前实现仅作为请求参数透传）
        """
        body: Dict[str, Any] = {"message": message, "stream": bool(stream)}
        if session_id is not None:
            body["session_id"] = session_id
        return self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/chat",
            body=body,
        )

    def list_agents(
        self,
        workspace_id: Optional[str] = None,
        limit: int = 20,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """分页列出 Agent（实际路由 ``/api/v1/assistants``）。

        :param workspace_id: 可选，工作空间隔离标识，透传到 ``X-Workspace-Id`` 头
        :param limit: 分页大小
        :param cursor: 分页游标
        """
        params: Dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._request(
            "GET",
            "/api/v1/assistants",
            params=params,
            workspace_id=workspace_id,
        )

    def create_agent(
        self,
        payload: Mapping[str, Any],
        workspace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """创建 Agent（助手），POST ``/api/v1/assistants``。

        ``payload`` 至少包含 ``name`` 与 ``system_prompt``，其余字段（model /
        temperature / tools / knowledge_base_ids 等）按平台 schema 透传。
        """
        return self._request(
            "POST",
            "/api/v1/assistants",
            body=dict(payload),
            workspace_id=workspace_id,
        )

    def send_chat_message(
        self,
        agent_id: str,
        message: str,
        conversation_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """向 Agent 发送消息并同步获取回复，POST ``/api/v1/assistants/{id}/run``。

        :param agent_id: Agent（助手）ID
        :param message: 用户消息文本
        :param conversation_id: 可选，会话 ID，写入 ``metadata.conversation_id`` 以便多轮续接
        :param workspace_id: 可选，工作空间隔离标识
        """
        body: Dict[str, Any] = {"user_message": message}
        metadata: Dict[str, Any] = {}
        if conversation_id is not None:
            metadata["conversation_id"] = conversation_id
        if metadata:
            body["metadata"] = metadata
        return self._request(
            "POST",
            f"/api/v1/assistants/{agent_id}/run",
            body=body,
            workspace_id=workspace_id,
        )

    def create_memory(
        self,
        content: str,
        metadata: Optional[Mapping[str, Any]] = None,
        importance: int = 3,
    ) -> Dict[str, Any]:
        """写入一条记忆向量。"""
        body: Dict[str, Any] = {
            "content": content,
            "importance": importance,
        }
        if metadata is not None:
            body["metadata"] = dict(metadata)
        return self._request("POST", "/api/v1/memory-vectors", body=body)

    def recall_memory(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """根据 query 检索相关记忆。"""
        return self._request(
            "POST",
            "/api/v1/memory-vectors/recall",
            body={"query": query, "limit": limit},
        )

    def search_knowledge(
        self,
        query: str,
        dataset_id: Optional[str] = None,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """搜索知识库。"""
        body: Dict[str, Any] = {"query": query, "limit": limit}
        if dataset_id:
            body["dataset_id"] = dataset_id
        return self._request("POST", "/api/v1/knowledge/search", body=body)

    # ------------------------------------------------------------------
    # 工作流
    # ------------------------------------------------------------------

    def list_workflows(
        self,
        workspace_id: Optional[str] = None,
        limit: int = 20,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """列出工作流，GET ``/api/v1/workflows``。"""
        params: Dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._request(
            "GET",
            "/api/v1/workflows",
            params=params,
            workspace_id=workspace_id,
        )

    def create_workflow(
        self,
        payload: Mapping[str, Any],
        workspace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """创建工作流，POST ``/api/v1/workflows``。

        ``payload`` 至少包含 ``name`` 与 ``graph``（节点/边定义）。
        """
        return self._request(
            "POST",
            "/api/v1/workflows",
            body=dict(payload),
            workspace_id=workspace_id,
        )

    def run_workflow(
        self,
        workflow_id: str,
        inputs: Mapping[str, Any],
        workspace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """执行指定工作流，POST ``/api/v1/workflows/{id}/runs``。

        平台运行 schema 使用 ``input`` 字段，SDK 将 ``inputs`` 包装为
        ``{"input": inputs}`` 下发，并支持幂等键透传。
        """
        return self._request(
            "POST",
            f"/api/v1/workflows/{workflow_id}/runs",
            body={"input": dict(inputs)},
            workspace_id=workspace_id,
        )

    # ------------------------------------------------------------------
    # 知识库
    # ------------------------------------------------------------------

    def list_knowledge_bases(
        self,
        workspace_id: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """列出知识库，GET ``/api/v1/knowledge-bases``。"""
        return self._request(
            "GET",
            "/api/v1/knowledge-bases",
            params={"limit": limit},
            workspace_id=workspace_id,
        )

    def create_knowledge_base(
        self,
        payload: Mapping[str, Any],
        workspace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """创建知识库，POST ``/api/v1/knowledge-bases``。"""
        return self._request(
            "POST",
            "/api/v1/knowledge-bases",
            body=dict(payload),
            workspace_id=workspace_id,
        )

    def ingest_document(
        self,
        kb_id: str,
        content: str,
        metadata: Optional[Mapping[str, Any]] = None,
        workspace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """向知识库写入文档，POST ``/api/v1/knowledge-bases/{id}/documents``。

        :param kb_id: 知识库 ID
        :param content: 文档正文（自动分块 + embedding）
        :param metadata: 可选，写入文档 ``metadata``；若含 ``title``/``source_type``/
            ``source_url`` 会一并提升到顶层字段
        """
        body: Dict[str, Any] = {"content": content}
        meta = dict(metadata) if metadata else {}
        # 平台 schema 要求 title；缺失时给一个默认值
        body["title"] = meta.pop("title", None) or f"doc-{_uuid.uuid4().hex[:8]}"
        if "source_type" in meta:
            body["source_type"] = meta.pop("source_type")
        if "source_url" in meta:
            body["source_url"] = meta.pop("source_url")
        body["metadata"] = meta
        return self._request(
            "POST",
            f"/api/v1/knowledge-bases/{kb_id}/documents",
            body=body,
            workspace_id=workspace_id,
        )

    def query_knowledge(
        self,
        kb_id: str,
        query: str,
        top_k: int = 5,
        workspace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """检索知识库，POST ``/api/v1/knowledge-bases/{id}/rag/query``。

        返回体包含 ``results``（含 ``similarity`` 相关性分数）。
        """
        return self._request(
            "POST",
            f"/api/v1/knowledge-bases/{kb_id}/rag/query",
            body={"query": query, "top_k": top_k},
            workspace_id=workspace_id,
        )

    # ------------------------------------------------------------------
    # 文件
    # ------------------------------------------------------------------

    def list_files(
        self,
        workspace_id: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """列出文件元数据，GET ``/api/v1/files``。"""
        return self._request(
            "GET",
            "/api/v1/files",
            params={"limit": limit},
            workspace_id=workspace_id,
        )

    def upload_file(
        self,
        filename: str,
        content_bytes: bytes,
        workspace_id: Optional[str] = None,
        kind: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """上传文件，POST ``/api/v1/files/upload``（multipart/form-data）。

        :param filename: 文件名（用于类型推断）
        :param content_bytes: 文件原始字节
        :param kind: 可选，文件类型；为空时由服务端按扩展名/MIME 推断
        :param metadata: 可选，以 JSON 字符串作为 form 字段下发
        """
        content_type, form_body = self._build_multipart(
            filename=filename,
            content_bytes=content_bytes,
            kind=kind,
            metadata=metadata,
        )
        return self._request(
            "POST",
            "/api/v1/files/upload",
            data=form_body,
            content_type=content_type,
            workspace_id=workspace_id,
        )

    # ------------------------------------------------------------------
    # 自动化
    # ------------------------------------------------------------------

    def list_automations(
        self,
        workspace_id: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """列出自动化触发器，GET ``/api/v1/automations/v2/triggers``。"""
        return self._request(
            "GET",
            "/api/v1/automations/v2/triggers",
            params={"limit": limit},
            workspace_id=workspace_id,
        )

    def create_automation(
        self,
        payload: Mapping[str, Any],
        workspace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """创建自动化触发器，POST ``/api/v1/automations/v2/triggers``。"""
        return self._request(
            "POST",
            "/api/v1/automations/v2/triggers",
            body=dict(payload),
            workspace_id=workspace_id,
        )

    # ------------------------------------------------------------------
    # 技能
    # ------------------------------------------------------------------

    def list_skills(
        self,
        workspace_id: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """列出技能，GET ``/api/v1/skills``。"""
        return self._request(
            "GET",
            "/api/v1/skills",
            params={"limit": limit},
            workspace_id=workspace_id,
        )

    def install_skill(
        self,
        skill_id: str,
        workspace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """订阅/安装市场技能，POST ``/api/v1/skills/marketplace/{id}/subscribe``。"""
        return self._request(
            "POST",
            f"/api/v1/skills/marketplace/{skill_id}/subscribe",
            body={},
            workspace_id=workspace_id,
        )

    def close(self) -> None:
        """清理客户端持有的资源。

        当前实现没有持久连接需要关闭，预留该方法以保持向前兼容，
        并允许调用方在 ``with`` 语义之外显式释放。
        """
        # urllib 每次请求都会自行关闭响应，无需额外清理
        return None

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _build_headers(self) -> Dict[str, str]:
        """构造请求头，附加鉴权信息。"""
        headers: Dict[str, str] = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        elif self.api_key:
            headers["X-WorkAMA-API-Key"] = self.api_key
        return headers

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
        *,
        workspace_id: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        data: Optional[bytes] = None,
        content_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """执行一次 HTTP 请求并返回解析后的 JSON。

        :param workspace_id: 可选，附加 ``X-Workspace-Id`` 头做工作空间隔离
        :param headers: 可选，附加/覆盖请求头
        :param data: 可选，原始字节请求体（用于 multipart 上传）；与 ``body`` 互斥
        :param content_type: 可选，``data`` 模式下的 Content-Type

        :raises AuthenticationError: 401
        :raises ForbiddenError: 403
        :raises NotFoundError: 404
        :raises RateLimitError: 429
        :raises WorkAMAError: 其他 HTTP/网络/解析错误
        """
        url = self.base_url + path
        if params:
            filtered = {k: v for k, v in params.items() if v is not None}
            if filtered:
                url = f"{url}?{urllib.parse.urlencode(filtered)}"

        req_headers = self._build_headers()
        if workspace_id:
            req_headers[_WORKSPACE_HEADER] = workspace_id
        if headers:
            for k, v in headers.items():
                req_headers[k] = v

        if data is not None:
            # 原始字节体（multipart 等）
            if content_type:
                req_headers["Content-Type"] = content_type
            payload: Optional[bytes] = data
        elif body is not None:
            payload = _json.dumps(body).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        else:
            payload = None

        req = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers=req_headers,
        )

        try:
            response = self._opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            # HTTP 错误：根据状态码映射为对应异常
            parsed = self._safe_read(exc)
            raise self._map_error(exc.code, parsed, exc.reason) from exc
        except urllib.error.URLError as exc:
            # 网络层错误（DNS、连接拒绝等）
            raise WorkAMAError(
                f"network error: {exc.reason}",
                status_code=None,
                body=None,
            ) from exc
        except Exception as exc:  # pragma: no cover - 兜底
            raise WorkAMAError(f"unexpected error: {exc}") from exc

        with response:
            raw = response.read()

        return self._parse_json(raw)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _build_multipart(
        filename: str,
        content_bytes: bytes,
        kind: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> tuple[str, bytes]:
        """构造 ``multipart/form-data`` 请求体。

        返回 ``(Content-Type, body)``。boundary 使用随机 UUID，避免与正文冲突。
        表单字段：``file``（文件）、可选 ``kind``、可选 ``metadata``（JSON 字符串）。
        """
        boundary = "----WorkAMABoundary" + _uuid.uuid4().hex
        crlf = b"\r\n"
        parts: list[bytes] = []

        def add_field(name: str, value: str) -> None:
            parts.append(f"--{boundary}".encode("utf-8"))
            parts.append(crlf)
            parts.append(
                f'Content-Disposition: form-data; name="{name}"'.encode("utf-8")
            )
            parts.append(crlf)
            parts.append(crlf)
            parts.append(value.encode("utf-8"))
            parts.append(crlf)

        if kind:
            add_field("kind", kind)
        if metadata is not None:
            add_field("metadata", _json.dumps(dict(metadata)))

        # 文件字段
        mime = (
            mimetypes.guess_type(filename)[0]
            or "application/octet-stream"
        )
        parts.append(f"--{boundary}".encode("utf-8"))
        parts.append(crlf)
        parts.append(
            (
                'Content-Disposition: form-data; name="file"; '
                f'filename="{os.path.basename(filename)}"'
            ).encode("utf-8")
        )
        parts.append(crlf)
        parts.append(f"Content-Type: {mime}".encode("utf-8"))
        parts.append(crlf)
        parts.append(crlf)
        parts.append(content_bytes)
        parts.append(crlf)
        parts.append(f"--{boundary}--".encode("utf-8"))
        parts.append(crlf)

        body = b"".join(parts)
        content_type = f"multipart/form-data; boundary={boundary}"
        return content_type, body

    @staticmethod
    def _safe_read(http_error: urllib.error.HTTPError) -> Any:
        """安全读取 HTTPError 响应体并尝试 JSON 解析。"""
        try:
            raw = http_error.read()
        except Exception:  # pragma: no cover - 极端情况
            return None
        return WorkAMAClient._parse_json(raw)

    @staticmethod
    def _parse_json(raw: bytes) -> Any:
        """将字节流解析为 JSON，失败则返回原始字符串。"""
        if not raw:
            return {}
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw
        try:
            return _json.loads(text)
        except (ValueError, TypeError):
            return text

    @staticmethod
    def _map_error(status_code: int, body: Any, reason: str) -> WorkAMAError:
        """根据 HTTP 状态码构造对应异常。"""
        message = reason or "request failed"
        if isinstance(body, dict):
            # 优先使用服务端返回的 message/detail 字段
            for key in ("message", "detail", "error"):
                val = body.get(key)
                if isinstance(val, str) and val:
                    message = val
                    break
        if status_code == 401:
            return AuthenticationError(message, status_code=status_code, body=body)
        if status_code == 403:
            return ForbiddenError(message, status_code=status_code, body=body)
        if status_code == 404:
            return NotFoundError(message, status_code=status_code, body=body)
        if status_code == 429:
            return RateLimitError(message, status_code=status_code, body=body)
        return WorkAMAError(message, status_code=status_code, body=body)


__all__ = ["WorkAMAClient"]
