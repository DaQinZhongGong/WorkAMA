from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import math
import os
import re
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import set_meter_provider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
workspace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("workspace_id", default="")
org_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("org_id", default="")

_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SENSITIVE = {
    "authorization", "cookie", "set-cookie", "api_key", "apikey", "password",
    "secret", "token", "access_token", "refresh_token", "prompt", "response",
    "content", "body", "extracted_text",
}
_configured: set[str] = set()
SEMANTIC_MAPPING_VERSION = "workama.ai-mcp.v1"


def valid_request_id(value: str | None) -> bool:
    return bool(value and _REQUEST_ID.fullmatch(value))


def new_request_id() -> str:
    return f"req_{secrets.token_hex(16)}"


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if str(key).lower().replace("-", "_") in _SENSITIVE else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def normalize_route(route: str | None, _: str) -> str:
    return route if route and route.startswith("/") else "/unmatched"


def current_trace_id() -> str:
    span_context = trace.get_current_span().get_span_context()
    return f"{span_context.trace_id:032x}" if span_context.is_valid else ""


def _semantic_text(value: str | None, fallback: str = "unknown", limit: int = 120) -> str:
    clean = str(value or "").strip()
    return clean[:limit] or fallback


def gen_ai_attributes(
    *,
    operation: str,
    model: str | None = None,
    provider: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost: float | None = None,
    status: str | None = None,
    mapping_version: str = SEMANTIC_MAPPING_VERSION,
) -> dict[str, str | int | float]:
    """Return the stable, content-free GenAI span attribute mapping.

    The mapping deliberately contains identifiers, usage and policy state only;
    prompt, response, tool arguments and resource contents never belong here.
    """

    attributes: dict[str, str | int | float] = {
        "ai.operation": _semantic_text(operation),
        "ai.semantic_conventions.version": _semantic_text(mapping_version, SEMANTIC_MAPPING_VERSION, 64),
    }
    for key, value in (("ai.model", model), ("ai.provider", provider), ("ai.status", status)):
        if value is not None:
            attributes[key] = _semantic_text(value)
    for key, value in (("ai.usage.input_tokens", input_tokens), ("ai.usage.output_tokens", output_tokens)):
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            attributes[key] = value
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(float(cost)) and cost >= 0:
        attributes["ai.cost"] = float(cost)
    return attributes


def mcp_attributes(
    *,
    server_id: str,
    transport: str,
    method: str,
    capability: str | None = None,
    status: str | None = None,
    risk_level: str | None = None,
    mapping_version: str = SEMANTIC_MAPPING_VERSION,
) -> dict[str, str]:
    """Return the stable MCP span attribute mapping without raw endpoints."""

    attributes = {
        "mcp.server_id_hash": hashlib.sha256(str(server_id).encode("utf-8")).hexdigest()[:16],
        "mcp.transport": _semantic_text(transport),
        "mcp.method": _semantic_text(method),
        "mcp.semantic_conventions.version": _semantic_text(mapping_version, SEMANTIC_MAPPING_VERSION, 64),
    }
    for key, value in (("mcp.capability", capability), ("mcp.status", status), ("mcp.risk_level", risk_level)):
        if value is not None:
            attributes[key] = _semantic_text(value)
    return attributes


def set_span_attributes(span: Any, attributes: Mapping[str, Any]) -> None:
    """Apply a content-free semantic mapping to an OpenTelemetry span."""

    for key, value in attributes.items():
        if value is not None:
            span.set_attribute(key, value)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "trace_id": current_trace_id(),
            "request_id": request_id_var.get(),
            "org_id": org_id_var.get(),
            "workspace_id": workspace_id_var.get(),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), ensure_ascii=True, separators=(",", ":"))


def configure_logging(service: str, level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


def configure_observability(service: str) -> None:
    if service in _configured:
        return
    _configured.add(service)
    configure_logging(service)
    if os.getenv("OTEL_ENABLED", "false").lower() not in {"1", "true", "yes"}:
        return
    resource = Resource.create({"service.name": service, "service.version": os.getenv("SERVICE_VERSION", "0.1.0")})
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
    trace.set_tracer_provider(tracer_provider)
    metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint, insecure=True), export_interval_millis=5000)
    set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))


def traceparent() -> str:
    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier)
    return carrier.get("traceparent", "")


def install_fastapi(app, service: str) -> None:
    tracer = trace.get_tracer(service)
    meter = metrics.get_meter(service)
    requests = meter.create_counter(f"wama_{service.replace('-', '_')}_http_requests_total")
    duration = meter.create_histogram(f"wama_{service.replace('-', '_')}_http_request_duration_seconds", unit="s")

    @app.middleware("http")
    async def observability_middleware(request, call_next):
        incoming = request.headers.get("x-wama-request-id") or request.headers.get("x-request-id")
        request_id = incoming if valid_request_id(incoming) else new_request_id()
        token = request_id_var.set(request_id)
        parent = TraceContextTextMapPropagator().extract(dict(request.headers))
        started = perf_counter()
        status_code = 500
        with tracer.start_as_current_span(f"{request.method} {request.url.path}", context=parent) as span:
            span.set_attribute("wama.request_id", request_id)
            span.set_attribute("http.request.method", request.method)
            try:
                response = await call_next(request)
                status_code = response.status_code
                return response
            finally:
                route = normalize_route(getattr(request.scope.get("route"), "path", None), request.url.path)
                attributes = {"method": request.method, "route": route, "status_class": f"{status_code // 100}xx"}
                requests.add(1, attributes)
                duration.record(perf_counter() - started, attributes)
                span.set_attribute("http.route", route)
                span.set_attribute("http.response.status_code", status_code)
                if "response" in locals():
                    response.headers["x-wama-request-id"] = request_id
                    response.headers["x-request-id"] = request_id
                    current_parent = traceparent()
                    if current_parent:
                        response.headers["traceparent"] = current_parent
                request_id_var.reset(token)


__all__ = [
    "SEMANTIC_MAPPING_VERSION", "JsonFormatter", "configure_logging", "configure_observability", "current_trace_id",
    "gen_ai_attributes", "mcp_attributes", "set_span_attributes",
    "install_fastapi", "new_request_id", "normalize_route", "org_id_var", "redact",
    "request_id_var", "traceparent", "valid_request_id", "workspace_id_var",
]
