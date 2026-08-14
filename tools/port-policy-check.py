#!/usr/bin/env python3
"""Port-policy check for the WorkAMA Compose stack.

Enforces the constraints defined in WorkAMA-Docs/815-端口规划与服务暴露约束.md:
  1. Every host port mapping for the `workama` Compose project MUST fall inside
     20200-20299 (partition A-F).
  2. Service container names published to the host MUST carry the `workama` prefix
     (either via `container_name:` or the `-p workama` project prefix).

This is the machine-checkable counterpart of the 815 document so the port
contract cannot silently drift. It intentionally does NOT shell out to docker;
it statically parses the compose files, which is enough to catch regressions in
the declared mapping.

Usage:
  python tools/port-policy-check.py [--compose-dir deploy/compose]
Exit code 0 when the policy holds, 1 on violations.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PORT_MIN, PORT_MAX = 20200, 20299
PREFIX = "workama"

# A port mapping line looks like one of:
#   - "20200:8000"
#   - - "${PLATFORM_API_PORT:-20200}:8000"
#   - - "127.0.0.1:20200:8000"
#   - - "[::1]:20200:8000"
PORT_LINE = re.compile(r'"([^"]*(?::\d+)+[^"]*)"')


def _default_of(token: str) -> str:
    """Resolve a compose interpolation like ${X:-20200} to its default number."""
    m = re.match(r"^\$\{([^}]*)\}$", token.strip())
    if not m:
        return token.strip()
    body = m.group(1)
    if ":-" in body:
        return body.split(":-", 1)[1].strip()
    if ":?" in body:
        return body.split(":?", 1)[1].strip()
    return ""  # ${VAR} with no default -> cannot statically assert


def _host_port(mapping: str) -> str | None:
    # Strip any leading IPv4/IPv6 bind address (one or two leading colon segments).
    segments = mapping.split(":")
    if len(segments) >= 3 and re.match(r"^[0-9.]+$|^\[", segments[0]):
        # ipv4:host:container  or  [ipv6]:host:container
        host = segments[1]
    else:
        host = segments[0]
    if not host:
        return None
    return _default_of(host)


def check_compose(path: Path) -> list[dict]:
    findings: list[dict] = []
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    in_ports = False
    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if re.match(r"^\-?\s*ports:\s*$", stripped):
            in_ports = True
            continue
        if in_ports:
            # A deeper-indented key ends the ports block.
            if re.match(r"^\s+[A-Za-z_]+:\s*", line) and not stripped.startswith("-"):
                in_ports = False
            m = PORT_LINE.search(line)
            if m:
                host = _host_port(m.group(1))
                if host and host.isdigit():
                    port = int(host)
                    if not (PORT_MIN <= port <= PORT_MAX):
                        findings.append(
                            {
                                "code": "port.out_of_range",
                                "file": str(path),
                                "line": lineno,
                                "port": port,
                                "expected": f"{PORT_MIN}-{PORT_MAX}",
                                "message": f"Host port {port} is outside the allowed {PORT_MIN}-{PORT_MAX} range",
                            }
                        )
    return findings


def check_container_prefix(path: Path) -> list[dict]:
    findings: list[dict] = []
    text = path.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), 1):
        m = re.search(r"container_name:\s*([\w.\-]+)", line)
        if m and not m.group(1).startswith(PREFIX):
            findings.append(
                {
                    "code": "container_name.no_prefix",
                    "file": str(path),
                    "line": lineno,
                    "name": m.group(1),
                    "message": f"container_name '{m.group(1)}' does not carry the '{PREFIX}' prefix",
                }
            )
    return findings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compose-dir", default="deploy/compose")
    args = ap.parse_args()
    root = Path(args.compose_dir)
    compose_files = sorted(root.glob("docker-compose*.yml"))
    if not compose_files:
        print(json.dumps({"ok": False, "error": f"no compose files in {root}"}, ensure_ascii=False))
        return 1

    findings: list[dict] = []
    for cf in compose_files:
        findings += check_compose(cf)
        findings += check_container_prefix(cf)

    result = {
        "ok": len(findings) == 0,
        "policy": f"workama host ports must be within {PORT_MIN}-{PORT_MAX}",
        "files_checked": [str(c) for c in compose_files],
        "findings": findings,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
