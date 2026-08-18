#!/usr/bin/env python3
"""Parse WORKAMA_MP_SUMMARY JSON and enforce p99 / error-rate gates.

Input is either:
  - a single JSON object (the summary dict), or
  - generator stdout containing a line that starts with WORKAMA_MP_SUMMARY=.

Exit 0 when every supplied summary meets its mode gate; otherwise exit 1
and print the failing checks. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

# Thresholds include headroom vs §10.12 / §10.13 GIL-free measurements.
# healthz allows a higher p99 because a zero-work probe can still see
# Docker-bridge spikes; chat / assistants stay on the 30ms acceptance line.
# p95 is the short-sample gate (dur < P99_MIN_DUR_S). p99 is only enforceable
# on a long enough window; otherwise Docker-bridge spikes dominate the tail.
P99_MIN_DUR_S = 90.0
DEFAULT_GATES: dict[str, dict[str, float]] = {
    "healthz": {"p95_ms": 40.0, "p99_ms": 100.0, "error_rate": 0.0},
    "assistants": {"p95_ms": 25.0, "p99_ms": 30.0, "error_rate": 0.0},
    "mixed": {"p95_ms": 25.0, "p99_ms": 30.0, "error_rate": 0.0},
    "g_healthz": {"p95_ms": 20.0, "p99_ms": 30.0, "error_rate": 0.0},
    "g_models": {"p95_ms": 20.0, "p99_ms": 30.0, "error_rate": 0.0},
    "g_chat": {"p95_ms": 20.0, "p99_ms": 30.0, "error_rate": 0.0},
}

SUMMARY_PREFIX = "WORKAMA_MP_SUMMARY="


def parse_summaries(text: str) -> list[dict[str, Any]]:
    text = text.strip()
    if not text:
        raise ValueError("empty input")
    summaries: list[dict[str, Any]] = []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and "p99" in payload:
        summaries.append(payload)
        return summaries
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(SUMMARY_PREFIX):
            continue
        summaries.append(json.loads(line[len(SUMMARY_PREFIX) :]))
    if not summaries:
        raise ValueError("no WORKAMA_MP_SUMMARY line found")
    return summaries


def evaluate(summary: dict[str, Any], gates: dict[str, dict[str, float]]) -> list[str]:
    mode = str(summary.get("mode") or "")
    gate = gates.get(mode)
    if gate is None:
        return [f"{mode or '<missing-mode>'}: no gate defined"]
    failures: list[str] = []
    p95 = float(summary.get("p95") if summary.get("p95") is not None else summary["p99"])
    p99 = float(summary["p99"])
    error_rate = float(summary.get("error_rate") or 0.0)
    dur_s = float(summary.get("dur_s") or 0.0)
    if p95 > gate["p95_ms"]:
        failures.append(f"{mode}: p95={p95:.3f}ms exceeds {gate['p95_ms']:.1f}ms")
    if dur_s >= P99_MIN_DUR_S and p99 > gate["p99_ms"]:
        failures.append(f"{mode}: p99={p99:.3f}ms exceeds {gate['p99_ms']:.1f}ms")
    if error_rate > gate["error_rate"]:
        failures.append(f"{mode}: error_rate={error_rate:.4f} exceeds {gate['error_rate']:.4f}")
    n = int(summary.get("n") or 0)
    if n <= 0:
        failures.append(f"{mode}: n={n} (no samples)")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WorkAMA GIL-free p99 gate")
    parser.add_argument("path", nargs="?", help="file to read; default stdin")
    args = parser.parse_args(argv)
    raw = sys.stdin.read() if args.path in (None, "-") else open(args.path, encoding="utf-8").read()
    try:
        summaries = parse_summaries(raw)
    except ValueError as exc:
        print(f"PERF_GATE: FAIL parse ({exc})", file=sys.stderr)
        return 1
    failures: list[str] = []
    for summary in summaries:
        failures.extend(evaluate(summary, DEFAULT_GATES))
        print(
            "PERF_GATE_ITEM="
            + json.dumps(
                {
                    "mode": summary.get("mode"),
                    "p99": summary.get("p99"),
                    "error_rate": summary.get("error_rate"),
                    "n": summary.get("n"),
                    "ok": not evaluate(summary, DEFAULT_GATES),
                },
                ensure_ascii=False,
            )
        )
    if failures:
        print("PERF_GATE: FAIL")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"PERF_GATE: PASS ({len(summaries)} summaries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
