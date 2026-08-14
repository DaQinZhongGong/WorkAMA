from __future__ import annotations

import json
import logging

from workama_observability import (
    JsonFormatter,
    gen_ai_attributes,
    mcp_attributes,
    normalize_route,
    redact,
    valid_request_id,
)
from workama_platform.modules.observability import (
    _service_definitions,
    calculate_error_budget,
    router as observability_router,
)


def test_request_id_validation_rejects_headers_and_unbounded_values():
    assert valid_request_id("req_01KXTEST")
    assert valid_request_id("client-request.123")
    assert not valid_request_id("bad\nheader")
    assert not valid_request_id("x" * 129)
    assert not valid_request_id("")


def test_redact_recursively_removes_credentials_and_content():
    value = {
        "authorization": "Bearer secret",
        "nested": {"api_key": "sk-secret", "prompt": "private text"},
        "safe": "visible",
        "items": [{"cookie": "session=secret"}],
    }
    result = redact(value)
    assert result["safe"] == "visible"
    assert result["authorization"] == "[REDACTED]"
    assert result["nested"]["api_key"] == "[REDACTED]"
    assert result["nested"]["prompt"] == "[REDACTED]"
    assert result["items"][0]["cookie"] == "[REDACTED]"
    assert "secret" not in json.dumps(result)


def test_json_formatter_includes_stable_context_fields():
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "handled", (), None)
    output = json.loads(JsonFormatter("platform-api").format(record))
    assert output["service"] == "platform-api"
    assert output["level"] == "INFO"
    assert output["msg"] == "handled"
    assert {"ts", "trace_id", "request_id", "org_id", "workspace_id"} <= output.keys()


def test_route_normalization_uses_templates_and_bounds_unknown_paths():
    assert normalize_route("/api/v1/sessions/{session_id}", "/api/v1/sessions/ses_123") == "/api/v1/sessions/{session_id}"
    assert normalize_route(None, "/api/v1/sessions/ses_123") == "/unmatched"


def test_semantic_mappings_are_content_free_and_bounded():
    genai = gen_ai_attributes(
        operation="chat",
        model="workama-chat",
        provider="mock",
        input_tokens=4,
        output_tokens=7,
        cost=0.03,
        status="succeeded",
    )
    assert genai["ai.operation"] == "chat"
    assert genai["ai.usage.input_tokens"] == 4
    assert "prompt" not in json.dumps(genai).lower()
    mcp = mcp_attributes(server_id="srv_secret_identifier", transport="stdio", method="tools/call", capability="tools")
    assert len(mcp["mcp.server_id_hash"]) == 16
    assert "srv_secret_identifier" not in json.dumps(mcp)
    assert mcp["mcp.semantic_conventions.version"] == "workama.ai-mcp.v1"


def test_error_budget_calculation_exposes_multi_state_burn_contract():
    healthy = calculate_error_budget(1000, 0, 0.9995)
    assert healthy["status"] == "healthy"
    assert healthy["budget_remaining_percent"] == 100
    watch = calculate_error_budget(1000, 2, 0.9995)
    assert watch["status"] == "watch"
    assert watch["budget_remaining_percent"] == 0
    critical = calculate_error_budget(1000, 10, 0.9995)
    assert critical["status"] == "critical"
    assert critical["burn_rate"] == 20.0
    assert calculate_error_budget(0, 0, 0.9995)["status"] == "no_data"


def test_observability_admin_routes_are_explicit():
    routes = {(route.path, tuple(sorted(route.methods or ()))) for route in observability_router.routes}
    assert ("/api/v1/admin/observability/summary", ("GET",)) in routes
    assert ("/api/v1/admin/observability/semantic-contract", ("GET",)) in routes


def test_service_signal_definitions_use_safe_health_endpoints():
    definitions = _service_definitions()
    assert {item["key"] for item in definitions} == {
        "platform_api",
        "gateway",
        "agent_runtime",
        "sandbox_fleet",
    }
    assert all(item["url"].endswith(("/readyz", "/healthz")) for item in definitions)
