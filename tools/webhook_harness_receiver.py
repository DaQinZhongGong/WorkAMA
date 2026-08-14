#!/usr/bin/env python3
"""Independent local webhook receiver used to verify real WorkAMA webhook delivery.

The receiver is deliberately implemented with the standard library only so it can
run inside a bare ``python:3.12-slim`` container on the Compose network.  It

* accepts the platform worker's ``POST`` and captures the exact wire bytes,
* verifies ``x-workama-signature`` with the raw webhook secret (the value a real
  third-party integrator holds) and, separately, with the peppered
  ``hash_secret(secret)`` value the worker actually signs with,
* proves the signature is body-bound by re-checking a tampered copy,
* exposes the captures for the smoke script to re-verify independently.

Endpoints:
    GET  /healthz    liveness
    POST /expect     {"secret": "whsec_...", "pepper": "..."} register trust material
    POST /hook       delivery target
    GET  /captures   captured deliveries and their verification verdicts
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY_BYTES = 1024 * 1024

_LOCK = threading.Lock()
_STATE: dict[str, object] = {"secret": None, "pepper": None, "captures": []}


def hash_secret(pepper: str, value: str) -> str:
    """Mirror of ``workama_platform.core.hash_secret``."""
    return hmac.new(pepper.encode(), value.encode(), hashlib.sha256).hexdigest()


def parse_signature(header: str) -> tuple[int, str] | None:
    timestamp: int | None = None
    digest: str | None = None
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                return None
        elif key == "v1":
            digest = value
    if timestamp is None or not digest:
        return None
    return timestamp, digest


def expected_digest(key: str, timestamp: int, raw_body: bytes) -> str:
    message = f"{timestamp}.".encode() + raw_body
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def verify(key: str, timestamp: int, digest: str, raw_body: bytes) -> bool:
    return hmac.compare_digest(expected_digest(key, timestamp, raw_body), digest)


def evaluate(headers: dict[str, str], raw_body: bytes) -> dict[str, object]:
    """Verify one delivery against every trust anchor the harness knows about."""
    with _LOCK:
        secret = _STATE["secret"]
        pepper = _STATE["pepper"]
    verdict: dict[str, object] = {
        "signature_header_present": "x-workama-signature" in headers,
        "signature_header_parsed": False,
        "signature_age_seconds": None,
        "verified_with_secret": False,
        "verified_with_peppered_secret_hash": False,
        "tampered_body_rejected": False,
        "trust_material_registered": bool(secret and pepper),
    }
    parsed = parse_signature(headers.get("x-workama-signature", ""))
    if not parsed or not secret or not pepper:
        return verdict
    timestamp, digest = parsed
    verdict["signature_header_parsed"] = True
    verdict["signature_age_seconds"] = int(datetime.now(UTC).timestamp()) - timestamp
    verdict["verified_with_secret"] = verify(secret, timestamp, digest, raw_body)
    secret_hash = hash_secret(pepper, secret)
    verdict["verified_with_peppered_secret_hash"] = verify(secret_hash, timestamp, digest, raw_body)
    verdict["tampered_body_rejected"] = not verify(secret_hash, timestamp, digest, raw_body + b" ")
    return verdict


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003 - stdlib hook
        print(f"[receiver] {fmt % args}", flush=True)

    def _respond(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes | None:
        length = int(self.headers.get("content-length") or 0)
        if length > MAX_BODY_BYTES:
            return None
        return self.rfile.read(length) if length else b""

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook
        if self.path.startswith("/healthz"):
            self._respond(200, {"ok": True})
            return
        if self.path.startswith("/captures"):
            with _LOCK:
                captures = list(_STATE["captures"])  # type: ignore[arg-type]
            self._respond(200, {"count": len(captures), "items": captures})
            return
        self._respond(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook
        raw = self._read_body()
        if raw is None:
            self._respond(413, {"ok": False, "error": "body_too_large"})
            return
        if self.path.startswith("/expect"):
            try:
                document = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._respond(400, {"ok": False, "error": "invalid_json"})
                return
            with _LOCK:
                _STATE["secret"] = document.get("secret")
                _STATE["pepper"] = document.get("pepper")
                _STATE["captures"] = []
            self._respond(200, {"ok": True, "registered": bool(document.get("secret") and document.get("pepper"))})
            return
        if not self.path.startswith("/hook"):
            self._respond(404, {"ok": False, "error": "not_found"})
            return
        headers = {key.lower(): value for key, value in self.headers.items()}
        capture = {
            "received_at": datetime.now(UTC).isoformat(),
            "path": self.path,
            "event": headers.get("x-workama-event"),
            "idempotency_key": headers.get("idempotency-key"),
            "content_type": headers.get("content-type"),
            "user_agent": headers.get("user-agent"),
            "signature": headers.get("x-workama-signature"),
            "body_base64": base64.b64encode(raw).decode("ascii"),
            "body_sha256": hashlib.sha256(raw).hexdigest(),
            "verification": evaluate(headers, raw),
        }
        with _LOCK:
            _STATE["captures"].append(capture)  # type: ignore[union-attr]
        print(f"[receiver] captured delivery event={capture['event']} verification={capture['verification']}", flush=True)
        self._respond(200, {"ok": True})


def main() -> int:
    port = int(os.environ.get("HARNESS_PORT", "20255"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[receiver] listening on 0.0.0.0:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
