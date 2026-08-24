#!/usr/bin/env python3
"""WorkAMA LLM upstream mock for staging-channel verification.

Runs an OpenAI-compatible /v1/chat/completions (+ /v1/models) HTTP server used
to verify the gateway's real-channel pipeline (model mapping -> routing ->
forward -> upstream -> output handling) end-to-end without a real LLM.

This is a verification fixture, NOT a real LLM. Evidence produced with it must
be labelled as mock-upstream (not real provider execution); real third-party
provider execution stays `pending_external`.

Usage:
    python tools/mock_llm_upstream.py [--port 9000] [--stream]

Handlers:
    GET  /v1/models                -> {"object":"list","data":[{"id":"<model>"}]}
    POST /v1/chat/completions      -> OpenAI chat completion (SSE when stream=true)
    GET  /healthz                  -> {"ok":true}
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

MODEL = "mock-upstream-model"
LOG_PREFIX = "[mock-llm-upstream]"


class Handler(BaseHTTPRequestHandler):
    server_version = "WorkAMA-MockLLM/1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"{LOG_PREFIX} {self.address_string()} {fmt % args}\n")

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send_json(200, {"ok": True})
            return
        if path == "/v1/models":
            self._send_json(200, {"object": "list", "data": [{"id": MODEL, "object": "model"}]})
            return
        self._send_json(404, {"error": {"message": f"mock upstream: unknown path {path}", "type": "not_found"}})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/v1/chat/completions":
            self._send_json(404, {"error": {"message": f"mock upstream: unknown path {path}", "type": "not_found"}})
            return
        body = self._read_body()
        auth = self.headers.get("Authorization", "")
        requested_model = body.get("model", "?")
        messages = body.get("messages", [])
        last_role = messages[-1]["role"] if messages else "user"
        last_text = messages[-1].get("content") if messages else ""
        if isinstance(last_text, list):
            last_text = " ".join(str(part.get("text", "")) for part in last_text if isinstance(part, dict))
        sys.stderr.write(
            f"{LOG_PREFIX} chat request model={requested_model!r} auth={'Bearer <set>' if auth else '<none>'} "
            f"stream={body.get('stream', False)} messages={len(messages)}\n"
        )
        content = (
            f"mock-upstream reply to [{last_role}]: {str(last_text)[:120] or '(empty)'} "
            f"(echo model={requested_model})"
        )
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            delta = json.dumps(
                {
                    "id": "mock-stream-" + uuid.uuid4().hex[:8],
                    "object": "chat.completion.chunk",
                    "model": requested_model,
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}],
                }
            )
            self.wfile.write(f"data: {delta}\n\n".encode("utf-8"))
            done = json.dumps(
                {
                    "id": "mock-stream-" + uuid.uuid4().hex[:8],
                    "object": "chat.completion.chunk",
                    "model": requested_model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
            )
            self.wfile.write(f"data: {done}\n\ndata: [DONE]\n\n".encode("utf-8"))
            self.wfile.flush()
            return
        self._send_json(
            200,
            {
                "id": "mock-" + uuid.uuid4().hex[:8],
                "object": "chat.completion",
                "model": requested_model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 9, "total_tokens": 21},
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="WorkAMA LLM upstream mock (OpenAI-compatible)")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    sys.stderr.write(f"{LOG_PREFIX} listening on :{args.port} model={MODEL}\n")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
