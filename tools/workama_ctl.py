#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "compose" / "docker-compose.yml"
DEFAULT_ENV = ROOT / ".env"


def run(command: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=ROOT, check=check, text=True, capture_output=capture)


def compose(env_file: Path, *args: str, project: str | None = None) -> list[str]:
    command = ["docker", "compose", "--env-file", str(env_file), "-f", str(COMPOSE)]
    if project:
        command += ["-p", project]
    return command + list(args)


def env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
    return values


def preflight(_: argparse.Namespace) -> None:
    if not shutil.which("docker"):
        raise SystemExit("Docker CLI was not found in PATH")
    run(["docker", "compose", "version"], capture=True)
    run(["docker", "info"], capture=True)
    print("Preflight passed: Docker Engine and Compose are available.")


def init(args: argparse.Namespace) -> None:
    path = Path(args.env_file).resolve()
    if path.exists() and not args.force:
        raise SystemExit(f"{path} already exists; use --force to replace it")
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    values = {
        "POSTGRES_DB": "workama", "POSTGRES_USER": "workama",
        "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "JWT_SECRET": secrets.token_urlsafe(48), "INTERNAL_TOKEN": secrets.token_urlsafe(48),
        "KEY_PEPPER": secrets.token_urlsafe(48), "ENCRYPTION_KEY": key,
        "MINIO_ROOT_USER": "workama", "MINIO_ROOT_PASSWORD": secrets.token_urlsafe(32),
        "SETUP_TOKEN": secrets.token_urlsafe(32), "AUTH_DEBUG_TOKENS": "false",
        "VITE_PLATFORM_API_URL": "http://localhost:20200", "VITE_AGENT_WS_URL": "ws://localhost:20201",
        "WEB_PORT": "3000", "PLATFORM_API_PORT": "8000", "AGENT_PORT": "8001", "SANDBOX_FLEET_PORT": "8002", "GATEWAY_PORT": "8080",
        "SANDBOX_RUNTIME": "runsc", "SANDBOX_REQUIRE_GVISOR": "false", "SANDBOX_IDLE_SECONDS": "900", "SANDBOX_TTL_SECONDS": "86400",
        "MINIO_PORT": "9010", "MINIO_CONSOLE_PORT": "9011", "SMTP_HOST": "", "SMTP_PORT": "25",
        "POSTGRES_PORT": "55432", "REDIS_PORT": "56379", "NATS_PORT": "54222", "NATS_MONITOR_PORT": "58222",
        "OTEL_GRPC_PORT": "14317", "OTEL_HTTP_PORT": "14318", "OTEL_METRICS_PORT": "19464", "OTEL_HEALTH_PORT": "13133",
        "SMTP_FROM": "notifications@workama.local", "OTEL_ENABLED": "true",
    }
    path.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n", encoding="utf-8")
    print(f"Environment created: {path}")
    print(f"Setup URL: http://localhost:{values['WEB_PORT']}/setup")
    print(f"Setup token: {values['SETUP_TOKEN']}")


def wait_health(env_file: Path, timeout: int) -> None:
    values = env_values(env_file)
    urls = [
        f"http://localhost:{values.get('PLATFORM_API_PORT', '8000')}/readyz",
        f"http://localhost:{values.get('GATEWAY_PORT', '8080')}/healthz",
        f"http://localhost:{values.get('AGENT_PORT', '8001')}/healthz",
        f"http://localhost:{values.get('SANDBOX_FLEET_PORT', '8002')}/healthz",
        f"http://localhost:{values.get('WEB_PORT', '3000')}/",
    ]
    deadline = time.monotonic() + timeout
    pending = set(urls)
    while pending and time.monotonic() < deadline:
        for url in list(pending):
            try:
                with urllib.request.urlopen(url, timeout=3) as response:
                    if response.status < 400:
                        pending.remove(url)
            except Exception:
                pass
        if pending:
            time.sleep(2)
    if pending:
        raise SystemExit("Health check timed out: " + ", ".join(sorted(pending)))
    print("All public services are healthy.")


def up(args: argparse.Namespace) -> None:
    run(compose(Path(args.env_file), "up", "--build", "-d", project=args.project))
    wait_health(Path(args.env_file), args.timeout)
    values = env_values(Path(args.env_file))
    print(f"Web: http://localhost:{values.get('WEB_PORT', '3000')}")
    print(f"Setup: http://localhost:{values.get('WEB_PORT', '3000')}/setup")


def status_cmd(args: argparse.Namespace) -> None:
    run(compose(Path(args.env_file), "ps", project=args.project))
    wait_health(Path(args.env_file), args.timeout)


def backup(env_file: Path, project: str | None) -> Path:
    target = ROOT / "quality" / "evidence" / "install" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target.mkdir(parents=True, exist_ok=True)
    values = env_values(env_file)
    dump = run(compose(env_file, "exec", "-T", "postgres", "pg_dump", "-U", values.get("POSTGRES_USER", "workama"), values.get("POSTGRES_DB", "workama"), project=project), capture=True)
    (target / "postgres.sql").write_text(dump.stdout, encoding="utf-8")
    (target / "manifest.json").write_text(json.dumps({"created_at": datetime.now(timezone.utc).isoformat(), "compose": str(COMPOSE), "project": project}, indent=2), encoding="utf-8")
    return target


def upgrade(args: argparse.Namespace) -> None:
    target = backup(Path(args.env_file), args.project)
    print(f"Backup point created: {target}")
    run(compose(Path(args.env_file), "pull", "--ignore-buildable", project=args.project), check=False)
    run(compose(Path(args.env_file), "up", "--build", "-d", project=args.project))
    wait_health(Path(args.env_file), args.timeout)


def down(args: argparse.Namespace) -> None:
    command = compose(Path(args.env_file), "down", project=args.project)
    if args.volumes:
        command.append("--volumes")
    run(command)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="workama-ctl")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight").set_defaults(func=preflight)
    init_parser = sub.add_parser("init"); init_parser.add_argument("--env-file", default=str(DEFAULT_ENV)); init_parser.add_argument("--force", action="store_true"); init_parser.set_defaults(func=init)
    for name, function in (("up", up), ("status", status_cmd), ("upgrade", upgrade), ("down", down)):
        command = sub.add_parser(name); command.add_argument("--env-file", default=str(DEFAULT_ENV)); command.add_argument("--project"); command.add_argument("--timeout", type=int, default=180)
        if name == "down": command.add_argument("--volumes", action="store_true")
        command.set_defaults(func=function)
    return result


if __name__ == "__main__":
    parsed = parser().parse_args()
    parsed.func(parsed)
