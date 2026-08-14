import hashlib
import hmac

import httpx
import pytest

from workama_platform import worker
from workama_platform.modules import audit_exports


def test_audit_details_strip_sensitive_fields_and_chain_hash_is_stable():
    details = audit_exports._safe_details({"action": "role.updated", "api_key": "secret", "nested": {"content": "private", "count": 2}})
    assert "api_key" not in details
    assert "content" not in details["nested"]
    record = {"sequence": 1, "event_type": "audit.test", "resource_id": "r1"}
    assert audit_exports.chain_hash("", record) == audit_exports.chain_hash("", dict(record))


def test_siem_endpoint_validation_is_fail_closed():
    config = audit_exports.SiemConfigUpsert(name="SIEM", endpoint="https://siem.example.com/ingest", credential="secret")
    assert config.endpoint.startswith("https://")
    controlled = audit_exports.SiemConfigUpsert(name="Local SIEM", endpoint="mock://siem/test", credential="secret")
    assert controlled.endpoint == "mock://siem/test"
    with pytest.raises(ValueError):
        audit_exports.SiemConfigUpsert(name="Local SIEM", endpoint="http://127.0.0.1:9200")


def test_controlled_siem_signature_is_stable_and_hmac_shaped():
    first = audit_exports.siem_signature("credential-hash", "payload", fallback_key="fallback")
    assert first == audit_exports.siem_signature("credential-hash", "payload", fallback_key="fallback")
    assert first.startswith("sha256=") and len(first) == 71


def test_siem_retry_delay_is_bounded_exponential():
    assert [audit_exports.siem_retry_delay(attempt) for attempt in range(1, 5)] == [2, 4, 8, 16]
    assert audit_exports.siem_retry_delay(20) == audit_exports.SIEM_RETRY_MAX_SECONDS


@pytest.mark.asyncio
async def test_controlled_siem_attempt_is_signed_without_network_io():
    captured = {}

    async def executor(endpoint, raw_body, headers):
        captured.update({"endpoint": endpoint, "body": raw_body, "headers": headers})
        return {"success": True, "response_code": 204, "error_code": None, "retryable": False, "disable": False, "summary": {}}

    delivery = {
        "config_id": "siem-config-1",
        "endpoint": "local://siem/test",
        "credential_hash": "credential-hash",
        "workspace_id": "workspace-1",
        "event_type": "audit.test",
        "idempotency_key": "delivery-1",
    }
    result = await audit_exports.deliver_siem_attempt(delivery, executor=executor)
    assert result["success"] is True
    assert captured["body"] == audit_exports.siem_raw_body("audit.test", "workspace-1", "delivery-1")
    assert captured["headers"]["x-workama-signature"] == "sha256=" + hmac.new(
        b"credential-hash", captured["body"], hashlib.sha256
    ).hexdigest()
    assert b"credential-hash" not in captured["body"]


@pytest.mark.asyncio
async def test_public_siem_attempt_rechecks_dns_signs_raw_body_and_retries_429(monkeypatch):
    calls = []

    async def resolve(_url):
        return type("Validation", (), {"allowed": True, "reason": None})()

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429 if len(calls) == 1 else 204, content=b"ok", request=request)

    monkeypatch.setattr(audit_exports, "validate_resolved_outbound_url", resolve)
    delivery = {
        "config_id": "siem-config-2",
        "endpoint": "https://siem.example.com/ingest",
        "credential_hash": "credential-hash",
        "workspace_id": "workspace-1",
        "event_type": "audit.test",
        "idempotency_key": "delivery-2",
    }
    transport = httpx.MockTransport(handler)
    first = await audit_exports.deliver_siem_attempt(delivery, transport=transport)
    second = await audit_exports.deliver_siem_attempt(delivery, transport=transport)
    raw_body = audit_exports.siem_raw_body("audit.test", "workspace-1", "delivery-2")
    assert first["retryable"] is True and first["response_code"] == 429
    assert second["success"] is True
    assert calls[0].content == raw_body
    assert calls[0].headers["x-workama-signature"] == "sha256=" + hmac.new(
        b"credential-hash", raw_body, hashlib.sha256
    ).hexdigest()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_public_siem_attempt_rejects_redirects_disables_on_410_and_bounds_response(monkeypatch):
    async def resolve(_url):
        return type("Validation", (), {"allowed": True, "reason": None})()

    monkeypatch.setattr(audit_exports, "validate_resolved_outbound_url", resolve)
    delivery = {
        "config_id": "siem-config-3",
        "endpoint": "https://siem.example.com/ingest",
        "credential_hash": "credential-hash",
        "workspace_id": "workspace-1",
        "event_type": "audit.test",
        "idempotency_key": "delivery-3",
    }
    redirect = await audit_exports.deliver_siem_attempt(
        delivery, transport=httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "https://other.example"}, request=request))
    )
    gone = await audit_exports.deliver_siem_attempt(
        delivery, transport=httpx.MockTransport(lambda request: httpx.Response(410, request=request))
    )
    too_large = await audit_exports.deliver_siem_attempt(
        delivery,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=b"x" * (audit_exports.SIEM_MAX_RESPONSE_BYTES + 1), request=request
            )
        ),
    )
    assert redirect["retryable"] is False and redirect["error_code"] == "redirect_not_allowed"
    assert gone["disable"] is True
    assert too_large["error_code"] == "response_too_large" and too_large["retryable"] is False


@pytest.mark.asyncio
async def test_worker_claims_siem_delivery_and_persists_delivered_state(monkeypatch):
    class Result:
        def __init__(self, rows=None):
            self.rows = list(rows or [])

        async def fetchall(self):
            return self.rows

    class Transaction:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Connection:
        def __init__(self, rows=None):
            self.rows = rows or []
            self.statements = []

        def transaction(self):
            return Transaction(self)

        async def execute(self, statement, params=()):
            self.statements.append((statement, params))
            if "FROM sec_siem_delivery d" in statement:
                return Result(self.rows)
            return Result()

    class Pool:
        def __init__(self, connections):
            self.connections = iter(connections)

        def connection(self):
            connection = next(self.connections)

            class Context:
                async def __aenter__(self):
                    return connection

                async def __aexit__(self, exc_type, exc, traceback):
                    return False

            return Context()

    claimed = Connection(
        [
            {
                "id": "siemd-1",
                "config_id": "siem-config-1",
                "workspace_id": "workspace-1",
                "event_type": "audit.test",
                "idempotency_key": "delivery-1",
                "payload_hash": "payload-hash",
                "status": "pending_external",
                "attempt": 0,
                "next_attempt_at": None,
                "claimed_at": None,
                "endpoint": "local://siem/test",
                "credential_hash": "credential-hash",
            }
        ]
    )
    finalized = Connection()
    monkeypatch.setattr(worker, "pool", Pool([claimed, finalized]))

    async def deliver(_delivery, **_kwargs):
        return {"success": True, "response_code": 204, "error_code": None, "retryable": False, "disable": False, "signature": "sha256=x", "summary": {}}

    monkeypatch.setattr(worker, "deliver_siem_attempt", deliver)
    result = await worker.process_pending_siem_deliveries()
    assert result == {"claimed": 1, "delivered": 1, "retried": 0, "failed": 0, "disabled": 0}
    assert any("status='delivering'" in statement for statement, _ in claimed.statements)
    assert any("SET status=%s" in statement for statement, _ in finalized.statements)


def test_audit_query_is_bounded_and_siem_test_requires_idempotency():
    query = audit_exports.AuditQuery(limit=200, cursor="12", action="role.updated")
    assert query.limit == 200
    assert audit_exports.SiemTestRequest(idempotency_key="siem-1").event_type == "audit.test"
    with pytest.raises(ValueError):
        audit_exports.AuditQuery(limit=501)
    with pytest.raises(ValueError):
        audit_exports.SiemTestRequest(idempotency_key="")


def test_routes_cover_audit_export_and_siem_contract():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in audit_exports.router.routes}
    for expected in (
        ("/api/v1/enterprise/audit/events", ("GET",)),
        ("/api/v1/enterprise/audit/exports", ("POST",)),
        ("/api/v1/enterprise/audit/exports", ("GET",)),
        ("/api/v1/enterprise/siem", ("GET",)),
        ("/api/v1/enterprise/siem", ("PUT",)),
        ("/api/v1/enterprise/siem/tests", ("POST",)),
        ("/api/v1/enterprise/siem/deliveries", ("GET",)),
    ):
        assert expected in paths


@pytest.mark.asyncio
async def test_schema_contains_chain_export_and_pending_delivery_tables():
    statements = []

    class Result:
        async def fetchall(self):
            return []

    class Connection:
        async def execute(self, statement):
            statements.append(statement)
            return Result()

    await audit_exports.ensure_audit_export_schema(Connection())
    schema = "\n".join(statements)
    for table in ("sec_audit_chain", "sec_audit_export", "sec_siem_config", "sec_siem_delivery"):
        assert table in schema
    assert "record_hash" in schema and "previous_hash" in schema
    assert "pending_external" in schema
    for field in ("attempt", "next_attempt_at", "response_code", "response_summary", "claimed_at", "delivered_at"):
        assert field in schema


@pytest.mark.asyncio
async def test_backfill_projects_existing_enterprise_audit_events_without_sensitive_details():
    statements = []

    class Result:
        async def fetchall(self):
            return []

    class Connection:
        async def execute(self, statement, params=None):
            statements.append((statement, params))
            return Result()

    await audit_exports._backfill_audit_chain(Connection())
    assert len(statements) == 1
