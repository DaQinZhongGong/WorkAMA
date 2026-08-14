from __future__ import annotations

import ast
import base64
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import websockets


TOOL_DEFINITIONS = [
    {"name": "web_search", "version": "1.0.0", "description": "Search public web references", "risk": "A1", "sandbox": False, "input_schema": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string", "maxLength": 300}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}}}},
    {"name": "file.read", "version": "1.0.0", "description": "Read a UTF-8 file in the session workspace", "risk": "A1", "sandbox": True, "input_schema": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}}},
    {"name": "file.write", "version": "1.0.0", "description": "Write a UTF-8 file in the session workspace", "risk": "A2", "sandbox": True, "input_schema": {"type": "object", "required": ["path", "content"], "properties": {"path": {"type": "string"}, "content": {"type": "string", "maxLength": 262144}}}},
    {"name": "file.search", "version": "1.0.0", "description": "Search text files in the session workspace", "risk": "A1", "sandbox": True, "input_schema": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}, "glob": {"type": "string"}}}},
    {"name": "code_interpreter", "version": "1.0.0", "description": "Run constrained Python for calculations and data transforms", "risk": "A2", "sandbox": True, "input_schema": {"type": "object", "required": ["code"], "properties": {"code": {"type": "string", "maxLength": 32768}}}},
    {"name": "terminal", "version": "1.0.0", "description": "Run an approved command in the session sandbox", "risk": "A3", "sandbox": True, "input_schema": {"type": "object", "required": ["argv"], "properties": {"argv": {"type": "array", "minItems": 1, "maxItems": 32, "items": {"type": "string", "maxLength": 500}}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120}}}},
    {"name": "browser", "version": "1.0.0", "description": "Navigate, click, input, screenshot in sandbox browser", "risk": "A2", "sandbox": True, "input_schema": {"type": "object", "required": ["action"], "properties": {"action": {"type": "string", "enum": ["navigate", "click", "input", "screenshot", "eval", "wait_for", "close"]}, "target": {"type": "string", "maxLength": 2000}, "text": {"type": "string", "maxLength": 10000}, "expression": {"type": "string", "maxLength": 10000}, "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 60000}}}},
]
TOOLS = {item["name"]: item for item in TOOL_DEFINITIONS}


class ToolError(ValueError):
    pass


@dataclass
class ToolResult:
    status: str
    summary: str
    output: Any
    artifact: dict[str, Any] | None = None
    untrusted: bool = False


class ToolRuntime:
    def __init__(self, root: str = "/tmp/workama-sandboxes", search_endpoint: str = "https://en.wikipedia.org/w/api.php", fleet_url: str = "", internal_token: str = "", event_callback: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None):
        self.root = Path(root)
        self.search_endpoint = search_endpoint
        self.fleet_url = fleet_url.rstrip("/")
        self.internal_token = internal_token
        # 流式事件回调：签名为 (event_type, event_payload)，用于 terminal.output 等实时事件
        self.event_callback = event_callback

    def workspace(self, workspace_id: str, session_id: str) -> Path:
        safe = re.compile(r"^[A-Za-z0-9_-]{3,80}$")
        if not safe.fullmatch(workspace_id) or not safe.fullmatch(session_id):
            raise ToolError("Invalid workspace or session identifier")
        target = self.root / workspace_id / session_id
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _path(self, base: Path, value: str) -> Path:
        if not value or "\x00" in value:
            raise ToolError("File path is required")
        target = (base / value).resolve()
        if base.resolve() not in target.parents and target != base.resolve():
            raise ToolError("Path escapes the session workspace")
        return target

    async def execute(self, name: str, arguments: dict[str, Any], workspace_id: str, session_id: str) -> ToolResult:
        if name not in TOOLS:
            raise ToolError(f"Unknown tool: {name}")
        base = self.workspace(workspace_id, session_id)
        if name == "web_search":
            return await self._search(arguments)
        if self.fleet_url:
            return await self._execute_remote(name, arguments, workspace_id, session_id)
        if name == "terminal":
            raise ToolError("Terminal requires the managed sandbox fleet")
        if name == "file.read":
            path = self._path(base, str(arguments.get("path", "")))
            if not path.is_file() or path.stat().st_size > 262144:
                raise ToolError("File is missing or exceeds 256 KiB")
            return ToolResult("succeeded", f"Read {path.name}", path.read_text(encoding="utf-8"))
        if name == "file.write":
            path = self._path(base, str(arguments.get("path", "")))
            content = str(arguments.get("content", ""))
            if len(content.encode()) > 262144:
                raise ToolError("File content exceeds 256 KiB")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return ToolResult("succeeded", f"Wrote {path.name}", {"path": str(path.relative_to(base)), "size": path.stat().st_size}, {"name": path.name, "content_type": "text/plain", "content": content})
        if name == "file.search":
            query = str(arguments.get("query", ""))
            if not query or len(query) > 300:
                raise ToolError("Search query must contain 1-300 characters")
            pattern = str(arguments.get("glob", "**/*"))
            matches = []
            for path in base.glob(pattern):
                if path.is_file() and path.stat().st_size <= 262144:
                    for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                        if query.casefold() in line.casefold():
                            matches.append({"path": str(path.relative_to(base)), "line": line_number, "preview": line[:300]})
                            if len(matches) >= 100:
                                break
                if len(matches) >= 100:
                    break
            return ToolResult("succeeded", f"Found {len(matches)} matches", matches)
        return self._code(arguments, base)

    def _validate_code(self, code: str) -> None:
        if not code or len(code) > 32768:
            raise ToolError("Code must contain 1-32768 characters")
        tree = ast.parse(code, mode="exec")
        denied_calls = {"open", "exec", "eval", "compile", "__import__", "input", "breakpoint"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)):
                raise ToolError("Imports and global scope mutation are disabled")
            if isinstance(node, ast.Name) and node.id in denied_calls:
                raise ToolError(f"Call is disabled: {node.id}")
            if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
                raise ToolError("Dunder attribute access is disabled")

    async def _execute_remote(self, name: str, arguments: dict[str, Any], workspace_id: str, session_id: str) -> ToolResult:
        headers = {"X-Internal-Token": self.internal_token}
        # 根据工具名选择沙箱镜像：
        # - browser 工具需要 sandbox-browser（含 Chromium + CDP 桥）
        # - 其他工具（file/terminal/code_interpreter）用 sandbox-code（多语言工具链）
        image = "sandbox-browser" if name == "browser" else "sandbox-code"
        async with httpx.AsyncClient(timeout=30) as client:
            acquired = await client.post(self.fleet_url + "/internal/sandboxes", headers=headers, json={"workspace_id": workspace_id, "session_id": session_id, "image": image})
            acquired.raise_for_status(); sandbox_id = acquired.json()["id"]
            if name == "file.write":
                path, content = str(arguments.get("path", "")), str(arguments.get("content", ""))
                response = await client.put(f"{self.fleet_url}/internal/sandboxes/{sandbox_id}/files", headers=headers, json={"path": path, "content": content}); response.raise_for_status()
                payload = response.json(); return ToolResult("succeeded", f"Wrote {Path(path).name}", payload, {"name": Path(path).name, "content_type": "text/plain", "content": content})
            if name == "file.read":
                response = await client.get(f"{self.fleet_url}/internal/sandboxes/{sandbox_id}/files", headers=headers, params={"path": str(arguments.get("path", ""))}); response.raise_for_status(); payload=response.json()
                return ToolResult("succeeded", f"Read {Path(payload['path']).name}", payload["content"])
            if name == "file.search":
                query = str(arguments.get("query", "")); pattern = str(arguments.get("glob", "**/*"))
                if not query or len(query)>300: raise ToolError("Search query must contain 1-300 characters")
                script = "import json,pathlib,sys\nq=sys.argv[1].casefold();out=[]\nfor p in pathlib.Path('.').glob(sys.argv[2]):\n  if p.is_file() and p.stat().st_size<=262144:\n   for n,line in enumerate(p.read_text(errors='replace').splitlines(),1):\n    if q in line.casefold():out.append({'path':str(p),'line':n,'preview':line[:300]})\n    if len(out)>=100:break\nprint(json.dumps(out[:100]))"
                response=await client.post(f"{self.fleet_url}/internal/sandboxes/{sandbox_id}/exec",headers=headers,json={"argv":["python","-I","-S","-c",script,query,pattern],"timeout_seconds":10});response.raise_for_status();payload=response.json()
                if payload["exit_code"]!=0: raise ToolError(payload["output"][:500])
                found=json.loads(payload["output"] or "[]");return ToolResult("succeeded",f"Found {len(found)} matches",found)
            if name == "terminal":
                argv = arguments.get("argv")
                if not isinstance(argv, list) or not argv or len(argv) > 32 or any(not isinstance(value, str) or not value or len(value) > 500 for value in argv):
                    raise ToolError("Terminal argv must contain 1-32 non-empty strings")
                timeout_seconds = min(max(int(arguments.get("timeout_seconds", 30)), 1), 120)
                # 流式分支：调用方请求 stream 模式时走 WebSocket PTY 实时输出
                if arguments.get("stream") is True:
                    return await self._execute_terminal_stream(sandbox_id, argv, timeout_seconds)
                response=await client.post(f"{self.fleet_url}/internal/sandboxes/{sandbox_id}/exec",headers=headers,json={"argv":argv,"timeout_seconds":timeout_seconds});response.raise_for_status();payload=response.json()
                return ToolResult("succeeded" if payload["exit_code"]==0 else "failed",f"Command exited with code {payload['exit_code']}",payload)
            if name == "browser":
                # 构建 BrowserOp 请求，转发给 sandbox-fleet 的 browser 端点
                browser_payload = {"action": arguments.get("action", "")}
                if "target" in arguments:
                    browser_payload["target"] = arguments["target"]
                if "text" in arguments:
                    browser_payload["params"] = {"text": arguments["text"]}
                if "expression" in arguments:
                    browser_payload["params"] = {"expression": arguments["expression"]}
                browser_payload["timeout_ms"] = arguments.get("timeout_ms", 10000)

                response = await client.post(
                    f"{self.fleet_url}/internal/sandboxes/{sandbox_id}/browser",
                    headers=headers,
                    json=browser_payload,
                )
                response.raise_for_status()
                payload = response.json()
                # 根据 agentd 返回的 ok 字段构造成功/失败的 ToolResult
                if payload.get("ok"):
                    # 如果响应中包含截图，作为 image/png artifact 一并返回
                    artifact = None
                    if "screenshot" in payload:
                        artifact = {"type": "image/png", "data": payload["screenshot"], "encoding": "base64"}
                    return ToolResult("succeeded", f"Browser {arguments.get('action')} succeeded", payload, artifact)
                else:
                    return ToolResult("failed", f"Browser {arguments.get('action')} failed: {payload.get('error', 'unknown')}", payload)
            code = str(arguments.get("code", "")); self._validate_code(code)
            response=await client.post(f"{self.fleet_url}/internal/sandboxes/{sandbox_id}/exec",headers=headers,json={"argv":["python","-I","-S","-c",code],"timeout_seconds":10});response.raise_for_status();payload=response.json()
            return ToolResult("succeeded" if payload["exit_code"]==0 else "failed",f"Python exited with code {payload['exit_code']}",payload)

    async def _execute_terminal_stream(self, sandbox_id: str, argv: list[str], timeout_seconds: int) -> ToolResult:
        """通过 WebSocket 流式执行 terminal 命令。

        连接 sandbox-fleet 的 PTY 流式端点，发送 start 块后循环接收 output 块，
        解码 base64 数据并通过 event_callback 发出 terminal.output 事件，
        收到 exit 块后返回 ToolResult。未收到 exit 块时按失败处理。
        """
        # 将 fleet HTTP URL 转换为对应的 WebSocket 协议
        if self.fleet_url.startswith("https://"):
            ws_base = "wss://" + self.fleet_url[len("https://"):]
        elif self.fleet_url.startswith("http://"):
            ws_base = "ws://" + self.fleet_url[len("http://"):]
        else:
            ws_base = "ws://" + self.fleet_url
        ws_url = ws_base.rstrip("/") + f"/internal/sandboxes/{sandbox_id}/terminal/stream"
        start_chunk = json.dumps({"type": "start", "argv": argv, "rows": 24, "cols": 80, "timeout_seconds": timeout_seconds})
        accumulated: list[str] = []
        exit_code: int | None = None
        async with websockets.connect(ws_url) as ws:
            await ws.send(start_chunk)
            async for raw in ws:
                chunk = json.loads(raw)
                chunk_type = chunk.get("type")
                if chunk_type == "output":
                    encoded = chunk.get("data", "")
                    decoded = base64.b64decode(encoded).decode("utf-8", errors="replace") if encoded else ""
                    if decoded:
                        accumulated.append(decoded)
                    # 通过回调实时发出 terminal.output 事件，供 xterm.js 等终端组件消费
                    if self.event_callback is not None:
                        await self.event_callback("terminal.output", {"type": "terminal.output", "data": decoded})
                elif chunk_type == "exit":
                    exit_code = chunk.get("exit_code")
                    break
        # 连接关闭但未收到 exit 块时按失败处理
        if exit_code is None:
            exit_code = 1
        output_text = "".join(accumulated)
        status = "succeeded" if exit_code == 0 else "failed"
        return ToolResult(status, f"Command exited with code {exit_code}", {"exit_code": exit_code, "output": output_text, "streamed": True})

    async def _search(self, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query", "")).strip()
        limit = min(max(int(arguments.get("limit", 5)), 1), 10)
        if not query or len(query) > 300:
            raise ToolError("Search query must contain 1-300 characters")
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get(self.search_endpoint, params={"action": "query", "list": "search", "srsearch": query, "srlimit": limit, "format": "json", "origin": "*"})
            response.raise_for_status()
        rows = response.json().get("query", {}).get("search", [])
        results = [{"title": row.get("title", ""), "url": f"https://en.wikipedia.org/?curid={row.get('pageid')}", "snippet": re.sub(r"<[^>]+>", "", row.get("snippet", ""))[:500]} for row in rows]
        return ToolResult("succeeded", f"Found {len(results)} public references", results, untrusted=True)

    def _code(self, arguments: dict[str, Any], base: Path) -> ToolResult:
        code = str(arguments.get("code", ""))
        self._validate_code(code)
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            completed = subprocess.run(["python", "-I", "-S", "-c", code], cwd=base, env=env, capture_output=True, text=True, timeout=10, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ToolError("Code execution exceeded 10 seconds") from exc
        output = (completed.stdout + completed.stderr)[:65536]
        status = "succeeded" if completed.returncode == 0 else "failed"
        return ToolResult(status, f"Python exited with code {completed.returncode}", {"exit_code": completed.returncode, "output": output, "truncated": len(completed.stdout + completed.stderr) > 65536})


def parse_tool_command(content: str) -> tuple[str, dict[str, Any]] | None:
    if not content.startswith("/tool "):
        return None
    parts = content.split(" ", 2)
    if len(parts) < 2:
        raise ToolError("Tool name is required")
    arguments = json.loads(parts[2]) if len(parts) == 3 else {}
    if not isinstance(arguments, dict):
        raise ToolError("Tool arguments must be a JSON object")
    return parts[1], arguments
