from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from workama_platform.modules.billing.metering import (
    MeteringEvent,
    MeterRequest,
    _assert_workspace_match,
)
from workama_platform.worker import handle_metering_message
from workama_observability import request_id_var, workspace_id_var


def event_payload() -> dict:
    return {
        "schema_version": 1,
        "event_id": "evt_01KXTESTMETERING00000000000",
        "event_type": "metering.llm.v1",
        "occurred_at": datetime.now(UTC).isoformat(),
        "producer": "gateway",
        "workspace_id": "wsp_01KXTESTWORKSPACE0000000000",
        "trace_id": "req_01KXTESTREQUEST000000000000",
        "idempotency_key": "req_01KXTESTREQUEST000000000000",
        "classification": "C2",
        "payload": {
            "request_id": "req_01KXTESTREQUEST000000000000",
            "token_id": "gwt_01KXTESTTOKEN0000000000000",
            "channel_id": "chn_01KXTESTCHANNEL00000000000",
            "model": "workama-chat",
            "prompt_tokens": 120,
            "completion_tokens": 40,
            "latency_ms": 820,
            "status_code": 200,
            "error_code": None,
        },
    }


def test_metering_event_accepts_frozen_public_envelope():
    event = MeteringEvent.model_validate(event_payload())

    assert isinstance(event.payload, MeterRequest)
    assert event.payload.workspace_id == event.workspace_id
    assert event.payload.model == "workama-chat"
    assert event.idempotency_key == event.payload.request_id
    assert event.event_type == "metering.llm.v1"


def test_metering_event_rejects_mismatched_idempotency_key():
    payload = event_payload()
    payload["idempotency_key"] = "req_other"

    with pytest.raises(ValidationError, match="idempotency_key"):
        MeteringEvent.model_validate(payload)


def test_metering_event_rejects_private_prompt_content():
    payload = event_payload()
    payload["payload"]["prompt"] = "secret prompt"

    with pytest.raises(ValidationError, match="prompt"):
        MeteringEvent.model_validate(payload)


def test_metering_rejects_cross_workspace_request_references():
    with pytest.raises(HTTPException) as error:
        _assert_workspace_match(
            {"workspace_id": "wsp_other", "cost_credits": "1.000000"},
            "wsp_current",
            "Reservation",
        )

    assert error.value.status_code == 409
    assert "another workspace" in str(error.value.detail)
    _assert_workspace_match({"workspace_id": "wsp_current"}, "wsp_current", "Usage request")


class FakeMessage:
    def __init__(self, data: bytes):
        self.data = data
        self.subject = "metering.llm.v1"
        self.acked = False
        self.terminated = False
        self.nak_delay: int | None = None

    async def ack(self):
        self.acked = True

    async def term(self):
        self.terminated = True

    async def nak(self, delay: int):
        self.nak_delay = delay


@pytest.mark.asyncio
async def test_worker_acks_valid_metering_event():
    message = FakeMessage(MeteringEvent.model_validate(event_payload()).model_dump_json().encode())
    processed: list[str] = []

    async def processor(event: MeteringEvent, subject: str):
        assert request_id_var.get() == event.trace_id
        assert workspace_id_var.get() == event.workspace_id
        processed.append(f"{subject}:{event.event_id}")

    await handle_metering_message(message, processor=processor)

    assert message.acked is True
    assert processed == ["metering.llm.v1:evt_01KXTESTMETERING00000000000"]


@pytest.mark.asyncio
async def test_worker_terminates_invalid_metering_event():
    message = FakeMessage(b'{"schema_version":99}')

    await handle_metering_message(message)

    assert message.terminated is True
    assert message.acked is False


@pytest.mark.asyncio
async def test_worker_naks_transient_processing_failure():
    message = FakeMessage(MeteringEvent.model_validate(event_payload()).model_dump_json().encode())

    async def failing_processor(_: MeteringEvent, __: str):
        raise RuntimeError("database unavailable")

    await handle_metering_message(message, processor=failing_processor)

    assert message.nak_delay == 5
    assert message.acked is False
