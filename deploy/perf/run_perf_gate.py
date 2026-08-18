#!/usr/bin/env python3
"""Cross-container GIL-free p99 gate.

Copies the multiprocessing generator into platform-worker, unsets inherited
proxy env, hits gateway/platform-api over the compose network, then scores
the WORKAMA_MP_SUMMARY lines with gate_p99.py.

Defaults are a short CI-friendly sample (8 VU x 25s). Override with
PERF_GATE_VUS / PERF_GATE_DUR. Stdlib + docker CLI only.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

REPO = Path(__file__).resolve().parents[2]
# Prefer the compose-adjacent env (the running stack source of truth).
# Root `.env` may drift and silently rotate INTERNAL_TOKEN on recreate.
_COMPOSE_ENV = REPO / "deploy" / "compose" / ".env"
_ROOT_ENV = REPO / ".env"
_ENV_FILE = _COMPOSE_ENV if _COMPOSE_ENV.exists() else _ROOT_ENV
COMPOSE = [
    "docker",
    "compose",
    "--env-file",
    str(_ENV_FILE),
    "-f",
    str(REPO / "deploy" / "compose" / "docker-compose.yml"),
    "-p",
    "workama",
]
GENERATOR = REPO / "deploy" / "perf" / "python_stress_mp.py"
GATE = REPO / "deploy" / "perf" / "gate_p99.py"
DEFAULT_MODES = (
    ("g_healthz", "http://gateway:8080"),
    ("g_chat", "http://gateway:8080"),
)
PROXY_ENV = {
    "http_proxy": "",
    "https_proxy": "",
    "HTTP_PROXY": "",
    "HTTPS_PROXY": "",
    "no_proxy": "gateway,platform-api,127.0.0.1,localhost",
    "NO_PROXY": "gateway,platform-api,127.0.0.1,localhost",
}


def run(args: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=REPO,
        check=check,
        capture_output=capture,
        text=True,
    )


def compose_exec(service: str, command: list[str], extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    args = [*COMPOSE, "exec", "-T"]
    for key, value in {**PROXY_ENV, **(extra_env or {})}.items():
        args.extend(["-e", f"{key}={value}"])
    args.extend([service, *command])
    return run(args, capture=True)


def main() -> int:
    if shutil.which("docker") is None:
        print("PERF_GATE: FAIL docker CLI not found", file=sys.stderr)
        return 2
    vus = os.environ.get("PERF_GATE_VUS", "10")
    dur = os.environ.get("PERF_GATE_DUR", "90")
    warmup = os.environ.get("PERF_GATE_WARMUP", "10")
    modes_raw = os.environ.get("PERF_GATE_MODES", "g_healthz,g_chat")
    selected = []
    for mode in modes_raw.split(","):
        mode = mode.strip()
        match = next((item for item in DEFAULT_MODES if item[0] == mode), None)
        if match is None:
            print(f"PERF_GATE: FAIL unknown mode {mode}", file=sys.stderr)
            return 2
        selected.append(match)

    print(f"PERF_GATE: copy generator into platform-worker (vus={vus} warmup={warmup}s dur={dur}s)")
    run([*COMPOSE, "cp", str(GENERATOR), "platform-worker:/tmp/python_stress_mp.py"])
    run([*COMPOSE, "cp", str(GATE), "platform-worker:/tmp/gate_p99.py"])

    combined: list[str] = []
    for mode, base in selected:
        print(f"PERF_GATE: run mode={mode} base={base}")
        result = compose_exec(
            "platform-worker",
            ["python", "/tmp/python_stress_mp.py", "--mode", mode, "--vus", vus, "--dur", dur, "--warmup", warmup],
            extra_env={"STRESS_BASE_URL": base},
        )
        sys.stdout.write(result.stdout)
        if result.returncode != 0:
            sys.stderr.write(result.stderr)
            print(f"PERF_GATE: FAIL generator exit={result.returncode} mode={mode}", file=sys.stderr)
            return result.returncode or 1
        combined.append(result.stdout)

    text = "\n".join(combined)
    gate = subprocess.run(
        [sys.executable, str(GATE), "-"],
        input=text,
        text=True,
        cwd=REPO / "deploy" / "perf",
    )
    evidence = REPO / "deploy" / "perf" / "out" / "perf-gate-latest.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    summaries = []
    for line in text.splitlines():
        if line.startswith("WORKAMA_MP_SUMMARY="):
            summaries.append(json.loads(line.split("=", 1)[1]))
    evidence.write_text(
        json.dumps(
            {
                "status": "candidate",
                "vus": int(vus),
                "dur_s": float(dur),
                "warmup_s": float(warmup),
                "modes": [item[0] for item in selected],
                "summaries": summaries,
                "gate_exit": gate.returncode,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"PERF_GATE: evidence written to {evidence}")
    return gate.returncode


if __name__ == "__main__":
    raise SystemExit(main())
