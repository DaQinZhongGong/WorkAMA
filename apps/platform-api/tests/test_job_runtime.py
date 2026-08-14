from __future__ import annotations

import pytest

from workama_platform.modules.jobs import canonical_hash, retry_delay, validate_operation_transition


def test_operation_state_machine_accepts_documented_paths():
    validate_operation_transition("queued", "running")
    validate_operation_transition("running", "retry_wait")
    validate_operation_transition("retry_wait", "running")
    validate_operation_transition("running", "cancel_requested")
    validate_operation_transition("cancel_requested", "cancelled")
    validate_operation_transition("running", "partially_succeeded")


def test_operation_state_machine_rejects_illegal_paths():
    with pytest.raises(ValueError, match="invalid operation transition"):
        validate_operation_transition("queued", "succeeded")
    with pytest.raises(ValueError):
        validate_operation_transition("succeeded", "running")


def test_retry_delay_is_exponential_and_capped():
    assert [retry_delay(attempt) for attempt in range(1, 5)] == [5, 10, 20, 40]
    assert retry_delay(20) == 300


def test_payload_hash_is_canonical():
    assert canonical_hash({"b": [2, 3], "a": 1}) == canonical_hash({"a": 1, "b": [2, 3]})

