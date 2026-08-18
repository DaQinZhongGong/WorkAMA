#!/usr/bin/env python3
"""Unit tests for gate_p99.py (stdlib unittest, no docker)."""
from __future__ import annotations

import json
import unittest

from gate_p99 import DEFAULT_GATES, evaluate, parse_summaries


class ParseSummariesTest(unittest.TestCase):
    def test_raw_object(self) -> None:
        payload = {"mode": "g_chat", "p99": 14.26, "error_rate": 0.0, "n": 100}
        got = parse_summaries(json.dumps(payload))
        self.assertEqual(got[0]["mode"], "g_chat")

    def test_prefixed_line(self) -> None:
        text = "noise\nWORKAMA_MP_SUMMARY=" + json.dumps(
            {"mode": "healthz", "p99": 8.1, "error_rate": 0.0, "n": 40}
        )
        got = parse_summaries(text)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["p99"], 8.1)

    def test_empty_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_summaries("")


class EvaluateTest(unittest.TestCase):
    def test_g_chat_pass(self) -> None:
        self.assertEqual(
            evaluate(
                {"mode": "g_chat", "p95": 10.5, "p99": 14.26, "error_rate": 0.0, "n": 200, "dur_s": 120},
                DEFAULT_GATES,
            ),
            [],
        )

    def test_g_chat_p99_fail_on_long_window(self) -> None:
        failures = evaluate(
            {"mode": "g_chat", "p95": 10.0, "p99": 31.0, "error_rate": 0.0, "n": 200, "dur_s": 120},
            DEFAULT_GATES,
        )
        self.assertTrue(any("p99=" in item for item in failures))

    def test_short_window_ignores_p99_spike(self) -> None:
        self.assertEqual(
            evaluate(
                {"mode": "g_chat", "p95": 9.9, "p99": 111.9, "error_rate": 0.0, "n": 200, "dur_s": 25},
                DEFAULT_GATES,
            ),
            [],
        )

    def test_error_rate_fail(self) -> None:
        failures = evaluate(
            {"mode": "healthz", "p95": 8.0, "p99": 10.0, "error_rate": 0.01, "n": 200, "dur_s": 25},
            DEFAULT_GATES,
        )
        self.assertTrue(any("error_rate=" in item for item in failures))

    def test_unknown_mode(self) -> None:
        failures = evaluate({"mode": "nope", "p99": 1.0, "error_rate": 0.0, "n": 1}, DEFAULT_GATES)
        self.assertEqual(len(failures), 1)


if __name__ == "__main__":
    unittest.main()
