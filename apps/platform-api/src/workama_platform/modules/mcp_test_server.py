"""Built-in MCP test server for subprocess/SSE interoperability tests.

This module is intentionally dependency-light (stdlib only, plus ``uvicorn``
when run in SSE mode) so it can be launched as a standalone script by the
real-interop test suite. It implements a minimal MCP server that exposes a
single ``echo`` tool and supports both transports:

* ``stdio``  (default): line-delimited JSON-RPC 2.0 over stdin/stdout.
* ``sse``    (``--sse``): an ASGI app served by ``uvicorn`` implementing the
  legacy MCP SSE transport (GET ``/sse`` stream + POST ``/messages``).

The ``MCP_TEST_SCENARIO`` environment variable selects a failure mode used by
the error-handling tests:

* ``echo`` (default): normal behaviour.
* ``crash``: the stdio server exits with a non-zero status instead of
  responding to ``initialize``.
* ``invalid``: the stdio server writes malformed JSON instead of a response.
* ``timeout``: the stdio server sleeps past the client timeout before
  responding to ``initialize``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from urllib.parse import parse_qs

JSONRPC_VERSION = "2.0"
PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "workama-mcp-test-server"
SERVER_VERSION = "1.0.0"


def _ok(request_id: object, result: dict) -> dict:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def _err(request_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": {"code": code, "message": message}}


def _initialize_result(request: dict) -> dict:
    params = request.get("params") or {}
    return {
        "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def _tools_list() -> dict:
    return {
        "tools": [
            {
                "name": "echo",
                "description": "Echo the supplied text back to the caller.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }
        ]
    }


def handle_request(request: dict) -> dict | None:
    """Return a JSON-RPC response for a request, or ``None`` for notifications.

    This pure function is shared by the stdio and SSE transports so that both
    exercise identical server behaviour.
    """
    method = request.get("method")
    request_id = request.get("id")

    if method == "initialize":
        return _ok(request_id, _initialize_result(request))
    if method == "tools/list":
        return _ok(request_id, _tools_list())
    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        if name != "echo":
            return _err(request_id, -32601, f"Unknown tool: {name!r}")
        arguments = params.get("arguments") or {}
        text = arguments.get("text")
        if not isinstance(text, str):
            return _err(request_id, -32602, "echo requires string argument text")
        if len(text) > 8192:
            return _err(request_id, -32602, "echo text is too long")
        return _ok(
            request_id,
            {"content": [{"type": "text", "text": text}], "isError": False},
        )
    if method == "notifications/initialized":
        return None
    return _err(request_id, -32601, f"Method not found: {method!r}")


# --------------------------------------------------------------------------- #
# stdio transport
# --------------------------------------------------------------------------- #
def run_stdio() -> None:
    scenario = os.environ.get("MCP_TEST_SCENARIO", "echo").strip().lower() or "echo"
    stream = sys.stdin
    for raw in stream:
        raw = raw.strip()
        if not raw:
            continue
        try:
            request = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(request, dict):
            continue
        method = request.get("method")

        if scenario == "crash" and method == "initialize":
            # Flush any buffered output then exit with a non-zero status so the
            # client observes a closed stdout while waiting for a response.
            sys.stdout.flush()
            sys.exit(1)

        if scenario == "invalid" and method == "initialize":
            sys.stdout.write("not-valid-json{\n")
            sys.stdout.flush()
            continue

        if scenario == "timeout" and method == "initialize":
            # Sleep past any reasonable client timeout before responding.
            time.sleep(5)

        response = handle_request(request)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()


# --------------------------------------------------------------------------- #
# SSE transport (ASGI app served by uvicorn)
# --------------------------------------------------------------------------- #
_SSE_SESSIONS: dict[str, asyncio.Queue] = {}


def _session_id_from_scope(scope: dict) -> str | None:
    raw = scope.get("query_string") or b""
    if isinstance(raw, bytes):
        raw = raw.decode("latin-1")
    parsed = parse_qs(raw)
    values = parsed.get("session_id") or parsed.get("sid")
    return values[0] if values else None


async def _send_json(send, status: int, body: dict) -> None:
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


async def _handle_sse(receive, send) -> None:
    session_id = secrets.token_hex(8)
    queue: asyncio.Queue = asyncio.Queue()
    _SSE_SESSIONS[session_id] = queue
    headers = [
        (b"content-type", b"text/event-stream"),
        (b"cache-control", b"no-cache"),
        (b"connection", b"keep-alive"),
        (b"mcp-session-id", session_id.encode("ascii")),
    ]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    endpoint_chunk = (
        f"event: endpoint\ndata: /messages?session_id={session_id}\n\n"
    ).encode("utf-8")
    await send({"type": "http.response.body", "body": endpoint_chunk, "more_body": True})
    try:
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=10.0)
            except asyncio.TimeoutError:
                # Keep the connection alive with an SSE comment.
                await send(
                    {"type": "http.response.body", "body": b": keepalive\n\n", "more_body": True}
                )
                continue
            if msg is None:
                break
            body = (f"event: message\ndata: {json.dumps(msg, separators=(',', ':'))}\n\n").encode(
                "utf-8"
            )
            await send({"type": "http.response.body", "body": body, "more_body": True})
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        _SSE_SESSIONS.pop(session_id, None)
        try:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except (ConnectionError, RuntimeError):
            pass


async def _handle_post(scope, receive, send) -> None:
    session_id = _session_id_from_scope(scope)
    queue = _SSE_SESSIONS.get(session_id) if session_id else None
    body = b""
    more = True
    while more:
        event = await receive()
        if event["type"] == "http.request":
            body += event.get("body", b"")
            more = event.get("more_body", False)
        elif event["type"] == "http.disconnect":
            break

    if queue is None:
        await _send_json(send, 400, {"error": "unknown session"})
        return
    try:
        request = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # Accept the message but there is nothing to respond with.
        await _send_json(send, 202, {"accepted": True})
        return
    response = handle_request(request)
    if response is not None:
        await queue.put(response)
    await _send_json(send, 202, {"accepted": True})


async def sse_asgi_app(scope, receive, send) -> None:
    if scope["type"] == "lifespan":
        while True:
            event = await receive()
            if event["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif event["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
        return
    if scope["type"] != "http":
        return
    path = scope.get("path", "")
    method = scope.get("method", "")
    if path == "/sse" and method == "GET":
        await _handle_sse(receive, send)
    elif path == "/messages" and method == "POST":
        await _handle_post(scope, receive, send)
    else:
        await _send_json(send, 404, {"error": "not found"})


def main() -> None:
    parser = argparse.ArgumentParser(description="WorkAMA MCP interoperability test server")
    parser.add_argument("--sse", action="store_true", help="run as an SSE MCP server via uvicorn")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.sse:
        import uvicorn

        uvicorn.run(sse_asgi_app, host=args.host, port=args.port, log_level="warning")
    else:
        run_stdio()


if __name__ == "__main__":
    main()
