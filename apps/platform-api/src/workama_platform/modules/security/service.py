from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ModerationResult:
    action: str
    text: str
    matches: list[str]


@dataclass(frozen=True)
class UrlValidationResult:
    allowed: bool
    reason: str | None = None


@dataclass(frozen=True)
class PromptEvalResult:
    passed: bool
    failures: list[str]
    total_cases: int = 3


def moderate_text(text: str, terms: list[str], configured_action: str) -> ModerationResult:
    matches: list[str] = []
    masked = text
    for raw_term in terms:
        term = raw_term.strip()
        if not term:
            continue
        pattern = re.compile(re.escape(term), re.IGNORECASE)
        if pattern.search(text):
            matches.append(term.lower())
            if configured_action == "mask":
                masked = pattern.sub("***", masked)
    if not matches:
        return ModerationResult("allow", text, [])
    action = configured_action if configured_action in {"block", "mask", "log"} else "block"
    return ModerationResult(action, masked if action == "mask" else text, sorted(set(matches)))


def _unsafe_ip(value: str) -> str | None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return "target resolves to a non-public address"
    return None


def validate_outbound_url(
    url: str, *, resolved_ips: list[str] | None = None
) -> UrlValidationResult:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return UrlValidationResult(False, "URL is malformed")
    if parsed.scheme not in {"http", "https"}:
        return UrlValidationResult(False, "only HTTP(S) endpoints are allowed")
    if not parsed.hostname or parsed.username or parsed.password:
        return UrlValidationResult(False, "userinfo and empty hosts are not allowed")
    if port is not None and not 1 <= port <= 65535:
        return UrlValidationResult(False, "port is invalid")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".internal", ".local")):
        return UrlValidationResult(False, "local and internal hostnames are blocked")
    literal_reason = _unsafe_ip(hostname)
    if literal_reason:
        return UrlValidationResult(False, literal_reason)
    for address in resolved_ips or []:
        reason = _unsafe_ip(address)
        if reason:
            return UrlValidationResult(False, reason)
    return UrlValidationResult(True)


async def validate_resolved_outbound_url(url: str) -> UrlValidationResult:
    initial = validate_outbound_url(url)
    if not initial.allowed:
        return initial
    hostname = urlsplit(url).hostname
    if not hostname:
        return UrlValidationResult(False, "host is required")
    try:
        records = await asyncio.to_thread(socket.getaddrinfo, hostname, None)
    except socket.gaierror:
        return UrlValidationResult(False, "hostname cannot be resolved")
    addresses = sorted({record[4][0] for record in records})
    return validate_outbound_url(url, resolved_ips=addresses)


def evaluate_prompt(content: str) -> PromptEvalResult:
    normalized = content.lower()
    checks = {
        "secret_protection": any(term in normalized for term in ("never reveal secret", "do not reveal secret", "api key")),
        "untrusted_input": "untrusted" in normalized and any(term in normalized for term in ("tool", "external", "retrieved")),
        "high_risk_approval": "approval" in normalized and any(term in normalized for term in ("high-risk", "high risk", "external action")),
    }
    failures = [name for name, passed in checks.items() if not passed]
    return PromptEvalResult(not failures, failures)
