from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import HTTPException

from workama_platform.modules.billing import router as billing_router
from workama_platform.modules.billing.metering import (
    MeteringEvent,
    MeterRequest,
    settle_meter_event,
)


def _meter_event(event_id: str = "evt_billing_01", request_id: str = "req_billing_01") -> MeteringEvent:
    return MeteringEvent.model_validate(
        {
            "schema_version": 1,
            "event_id": event_id,
            "event_type": "metering.llm.v1",
            "occurred_at": datetime.now(UTC).isoformat(),
            "producer": "gateway",
            "workspace_id": "wsp_billing_test",
            "trace_id": request_id,
            "idempotency_key": request_id,
            "classification": "C2",
            "payload": {
                "request_id": request_id,
                "token_id": "gwt_billing_test",
                "channel_id": "chn_billing_test",
                "model": "workama-chat",
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "latency_ms": 100,
                "status_code": 200,
                "error_code": None,
            },
        }
    )


class _FakeResult:
    def __init__(self, row: dict[str, Any] | None = None, rows: list[dict[str, Any]] | None = None):
        self._row = row
        self._rows = rows or []

    async def fetchone(self) -> dict[str, Any] | None:
        return self._row

    async def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.next_insert_row: dict[str, Any] | None = {"id": "inb_test_01"}

    async def execute(self, statement: str, params: tuple[Any, ...] | None = None) -> _FakeResult:
        self.statements.append((statement, params or ()))
        if "INSERT INTO ops_inbox" in statement:
            return _FakeResult(self.next_insert_row)
        return _FakeResult()

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _FakePool:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def connection(self) -> _FakeConnection:
        return self._connection


class _FakeTransaction:
    async def __aenter__(self) -> _FakeTransaction:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


@pytest.fixture
def fake_conn() -> _FakeConnection:
    return _FakeConnection()


@pytest.mark.asyncio
async def test_settle_meter_event_processes_first_event(fake_conn: _FakeConnection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "workama_platform.modules.billing.metering.pool", _FakePool(fake_conn)
    )
    async def fake_settle_in_transaction(_conn: Any, _body: MeterRequest) -> dict[str, Any]:
        return {"duplicate": False, "cost_credits": "0.000150", "balance": "100.000000"}

    monkeypatch.setattr(
        "workama_platform.modules.billing.metering.settle_meter_in_transaction",
        fake_settle_in_transaction,
    )

    event = _meter_event()
    result = await settle_meter_event(event, "metering.llm.v1", "billing-metering-v1")

    assert result is True
    insert = next((s, p) for s, p in fake_conn.statements if "INSERT INTO ops_inbox" in s)
    assert "billing-metering-v1" in insert[1]
    assert event.payload.request_id in insert[1]
    update = next((s, p) for s, p in fake_conn.statements if "UPDATE ops_inbox" in s)
    assert update[1] == ("inb_test_01",)


@pytest.mark.asyncio
async def test_settle_meter_event_returns_false_for_duplicate(fake_conn: _FakeConnection, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_conn.next_insert_row = None
    monkeypatch.setattr(
        "workama_platform.modules.billing.metering.pool", _FakePool(fake_conn)
    )

    event = _meter_event()
    result = await settle_meter_event(event, "metering.llm.v1", "billing-metering-v1")

    assert result is False
    assert not any("UPDATE ops_inbox" in s for s, _ in fake_conn.statements)


@pytest.mark.asyncio
async def test_record_meter_event_endpoint_returns_processed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[MeteringEvent, str, str]] = []

    async def fake_settle(event: MeteringEvent, subject: str, consumer: str) -> bool:
        calls.append((event, subject, consumer))
        return True

    monkeypatch.setattr(billing_router, "settle_meter_event", fake_settle)

    event = _meter_event()
    response = await billing_router.record_meter_event(event)

    assert response == {"event_id": event.event_id, "status": "processed"}
    assert calls == [(event, event.event_type, "billing-metering-v1")]


@pytest.mark.asyncio
async def test_record_meter_event_endpoint_returns_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_settle(_event: MeteringEvent, _subject: str, _consumer: str) -> bool:
        return False

    monkeypatch.setattr(billing_router, "settle_meter_event", fake_settle)

    event = _meter_event()
    response = await billing_router.record_meter_event(event)

    assert response == {"event_id": event.event_id, "status": "duplicate"}


@pytest.mark.asyncio
async def test_get_meter_event_returns_row(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    row = {
        "event_id": "evt_billing_01",
        "subject": "metering.llm.v1",
        "consumer_name": "billing-metering-v1",
        "status": "processed",
        "request_id": "req_billing_01",
        "received_at": now,
        "processed_at": now,
        "last_error": None,
    }

    class _ResultConnection(_FakeConnection):
        async def execute(self, statement: str, params: tuple[Any, ...] | None = None) -> _FakeResult:
            self.statements.append((statement, params or ()))
            if "FROM ops_inbox" in statement:
                return _FakeResult(row)
            return _FakeResult()

    monkeypatch.setattr(billing_router.pool, "connection", lambda: _ResultConnection())
    result = await billing_router.get_meter_event("evt_billing_01")

    assert result["event_id"] == "evt_billing_01"
    assert result["status"] == "processed"


@pytest.mark.asyncio
async def test_get_meter_event_returns_404_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    class _EmptyConnection(_FakeConnection):
        async def execute(self, statement: str, params: tuple[Any, ...] | None = None) -> _FakeResult:
            self.statements.append((statement, params or ()))
            return _FakeResult(None)

    monkeypatch.setattr(billing_router.pool, "connection", lambda: _EmptyConnection())

    with pytest.raises(HTTPException) as exc_info:
        await billing_router.get_meter_event("evt_missing")

    assert exc_info.value.status_code == 404


def test_metering_event_requires_idempotency_key_matches_request_id() -> None:
    payload = {
        "schema_version": 1,
        "event_id": "evt_01",
        "event_type": "metering.llm.v1",
        "occurred_at": datetime.now(UTC).isoformat(),
        "producer": "gateway",
        "workspace_id": "wsp_01",
        "trace_id": "req_01",
        "idempotency_key": "req_other",
        "classification": "C2",
        "payload": {
            "request_id": "req_01",
            "model": "workama-chat",
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "latency_ms": 1,
            "status_code": 200,
        },
    }
    with pytest.raises(ValueError, match="idempotency_key"):
        MeteringEvent.model_validate(payload)


def test_meter_request_forbids_private_prompt_field() -> None:
    with pytest.raises(ValueError, match="prompt"):
        MeterRequest.model_validate(
            {
                "request_id": "req_01",
                "model": "workama-chat",
                "prompt": "secret",
                "prompt_tokens": 1,
                "completion_tokens": 1,
            }
        )
