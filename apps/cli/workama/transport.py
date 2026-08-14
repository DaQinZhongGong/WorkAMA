from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


class TransportError(RuntimeError):
    pass


class ApiError(TransportError):
    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def endpoint(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    if path.startswith("/api/v1") and base.endswith("/api/v1"):
        base = base[:-7]
    if path.startswith("/v1") and base.endswith("/v1"):
        base = base[:-3]
    return f"{base}{path}"


def _decode_body(raw: bytes, content_type: str = "") -> Any:
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    if "json" in content_type.lower() or text.lstrip().startswith(("{", "[")):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return text


class HttpClient:
    def __init__(self, timeout: float = 60.0):
        self.timeout = timeout

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        body = None
        request_headers = {"Accept": "application/json", **(headers or {})}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = Request(url, data=body, headers=request_headers, method=method.upper())
        try:
            with urlopen(request, timeout=timeout or self.timeout) as response:
                return _decode_body(response.read(), response.headers.get("Content-Type", ""))
        except HTTPError as exc:
            body = _decode_body(exc.read(), exc.headers.get("Content-Type", ""))
            message = body.get("detail", body.get("error", body)) if isinstance(body, dict) else body
            raise ApiError(exc.code, f"HTTP {exc.code}: {message}", body) from exc
        except URLError as exc:
            raise TransportError(f"Request to {url} failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise TransportError(f"Request to {url} timed out") from exc

    def download(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> bytes:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request_headers = {"Accept": "*/*", **(headers or {})}
        if payload is not None:
            request_headers.setdefault("Content-Type", "application/json")
        request = Request(url, data=body, headers=request_headers, method=method.upper())
        try:
            with urlopen(request, timeout=timeout or self.timeout) as response:
                return response.read()
        except HTTPError as exc:
            body = _decode_body(exc.read(), exc.headers.get("Content-Type", ""))
            message = body.get("detail", body.get("error", body)) if isinstance(body, dict) else body
            raise ApiError(exc.code, f"HTTP {exc.code}: {message}", body) from exc
        except URLError as exc:
            raise TransportError(f"Request to {url} failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise TransportError(f"Request to {url} timed out") from exc

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request_headers = {"Accept": "text/event-stream", **(headers or {})}
        if payload is not None:
            request_headers.setdefault("Content-Type", "application/json")
        request = Request(url, data=body, headers=request_headers, method=method.upper())
        try:
            response = urlopen(request, timeout=timeout or self.timeout)
        except HTTPError as exc:
            body = _decode_body(exc.read(), exc.headers.get("Content-Type", ""))
            message = body.get("detail", body.get("error", body)) if isinstance(body, dict) else body
            raise ApiError(exc.code, f"HTTP {exc.code}: {message}", body) from exc
        except URLError as exc:
            raise TransportError(f"Request to {url} failed: {exc.reason}") from exc
        return response


def parse_sse(response):
    try:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                yield json.loads(data)
            except json.JSONDecodeError:
                continue
    finally:
        response.close()


def _read_until(sock: socket.socket, marker: bytes, limit: int = 65536) -> bytes:
    data = bytearray()
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise TransportError("WebSocket closed during handshake")
        data.extend(chunk)
        if len(data) > limit:
            raise TransportError("WebSocket handshake is too large")
    return bytes(data)


@dataclass
class WebSocketConnection:
    sock: socket.socket

    @classmethod
    def connect(
        cls,
        url: str,
        *,
        timeout: float = 60.0,
        headers: dict[str, str] | None = None,
    ) -> "WebSocketConnection":
        parsed = urlsplit(url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise TransportError(f"Unsupported WebSocket URL: {url}")
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        sock = socket.create_connection((parsed.hostname, port), timeout=timeout)
        if parsed.scheme == "wss":
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=parsed.hostname)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
        host = parsed.hostname
        if parsed.port:
            host += f":{parsed.port}"
        request_lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        for header, value in (headers or {}).items():
            request_lines.append(f"{header}: {value}")
        sock.sendall(("\r\n".join(request_lines) + "\r\n\r\n").encode("ascii"))
        raw_headers = _read_until(sock, b"\r\n\r\n")
        header_text = raw_headers.decode("iso-8859-1")
        status_line, *header_lines = header_text.split("\r\n")
        if " 101 " not in f"{status_line} ":
            sock.close()
            raise TransportError(f"WebSocket handshake failed: {status_line}")
        response_headers = {}
        for line in header_lines:
            if ":" in line:
                name, value = line.split(":", 1)
                response_headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if response_headers.get("sec-websocket-accept") != expected:
            sock.close()
            raise TransportError("WebSocket handshake returned an invalid accept key")
        return cls(sock)

    def _read_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise TransportError("WebSocket closed unexpectedly")
            data.extend(chunk)
        return bytes(data)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
        elif length <= 0xFFFF:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send_json(self, payload: dict[str, Any]) -> None:
        self._send_frame(0x1, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def receive_text(self) -> str:
        fragments: list[bytes] = []
        while True:
            first, second = self._read_exact(2)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else None
            payload = self._read_exact(length)
            if mask:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if opcode == 0x8:
                self._send_frame(0x8, payload[:125])
                raise TransportError("WebSocket closed by server")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                fragments = [payload]
            elif opcode == 0x0:
                fragments.append(payload)
            else:
                continue
            if fin:
                return b"".join(fragments).decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except (OSError, TransportError):
            pass
        finally:
            self.sock.close()

