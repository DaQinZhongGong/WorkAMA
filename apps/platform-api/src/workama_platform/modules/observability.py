from __future__ import annotations

import math
import os
import asyncio
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException

from workama_platform.core import Actor, get_actor


router = APIRouter(prefix="/api/v1/admin/observability", tags=["observability"])

SLO_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {"key": "gateway", "name": "Gateway availability", "target": 0.9995, "owner": "Gateway"},
    {"key": "platform_api", "name": "Platform API availability", "target": 0.9995, "owner": "Platform"},
    {"key": "agent", "name": "Agent session recovery", "target": 0.995, "owner": "Agent"},
    {"key": "notifications", "name": "Notification delivery", "target": 0.99, "owner": "Integrations"},
    {"key": "operations", "name": "Async operation start", "target": 0.999, "owner": "Platform Ops"},
    {"key": "search", "name": "Authorized search", "target": 0.995, "owner": "Search"},
)

SEMANTIC_CONTRACT: dict[str, Any] = {
    "schema_version": "workama.ai-mcp.v1",
    "gen_ai": {
        "attributes": [
            "ai.operation", "ai.model", "ai.provider", "ai.usage.input_tokens",
            "ai.usage.output_tokens", "ai.cost", "ai.status", "ai.semantic_conventions.version",
        ],
        "content_fields": "forbidden",
    },
    "mcp": {
        "attributes": [
            "mcp.server_id_hash", "mcp.transport", "mcp.method", "mcp.capability",
            "mcp.status", "mcp.risk_level", "mcp.semantic_conventions.version",
        ],
        "raw_endpoint": "forbidden",
        "raw_session_or_credentials": "forbidden",
    },
    "trace_contract": {
        "request_id": "x-wama-request-id",
        "propagation": "W3C traceparent",
        "async_link": "operation/job span link",
    },
}


def _require_admin(actor: Actor) -> None:
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")


def calculate_error_budget(valid_total: float, bad_total: float, target: float) -> dict[str, Any]:
    """Calculate a bounded error-budget snapshot from Prometheus counters."""

    valid = max(float(valid_total), 0.0) if math.isfinite(float(valid_total)) else 0.0
    bad = max(float(bad_total), 0.0) if math.isfinite(float(bad_total)) else 0.0
    bad = min(bad, valid) if valid else 0.0
    target = min(max(float(target), 0.0), 1.0)
    budget_rate = 1.0 - target
    if valid <= 0:
        return {
            "valid_total": 0, "good_total": 0, "bad_total": 0, "target": target,
            "error_rate": None, "burn_rate": None, "budget_remaining_percent": None, "status": "no_data",
        }
    error_rate = bad / valid
    burn_rate = error_rate / budget_rate if budget_rate else None
    budget_total = valid * budget_rate
    remaining = max(budget_total - bad, 0.0)
    if burn_rate is None:
        health = "no_data"
    elif burn_rate >= 14.4:
        health = "critical"
    elif burn_rate >= 6:
        health = "warning"
    elif burn_rate >= 3:
        health = "watch"
    else:
        health = "healthy"
    return {
        "valid_total": round(valid, 6), "good_total": round(valid - bad, 6), "bad_total": round(bad, 6),
        "target": target, "error_rate": round(error_rate, 8),
        "burn_rate": round(burn_rate, 6) if burn_rate is not None else None,
        "budget_remaining_percent": round((remaining / budget_total) * 100, 4) if budget_total else 0,
        "status": health,
    }


def _prometheus_value(payload: dict[str, Any]) -> float:
    try:
        result = payload.get("data", {}).get("result", [])
        value = result[0].get("value", [None, "0"])[1] if result else "0"
        parsed = float(value)
        return parsed if math.isfinite(parsed) else 0.0
    except (AttributeError, IndexError, TypeError, ValueError):
        return 0.0


async def _query_prometheus(client: httpx.AsyncClient, expression: str) -> tuple[float, bool]:
    try:
        response = await client.get("/api/v1/query", params={"query": expression})
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            return 0.0, False
        if not payload.get("data", {}).get("result"):
            return 0.0, False
        return _prometheus_value(payload), True
    except (httpx.HTTPError, ValueError, TypeError):
        return 0.0, False


async def _load_slo_snapshots() -> tuple[list[dict[str, Any]], bool]:
    base_url = os.getenv("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
    telemetry_available = True
    snapshots: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=1.5, follow_redirects=False) as client:
            for definition in SLO_DEFINITIONS:
                key = definition["key"]
                valid, valid_ok = await _query_prometheus(client, f"workama:sli:{key}:valid_total")
                bad, bad_ok = await _query_prometheus(client, f"workama:sli:{key}:bad_total")
                telemetry_available = telemetry_available and valid_ok and bad_ok
                snapshots.append({**definition, **calculate_error_budget(valid, bad, definition["target"])})
    except (httpx.HTTPError, OSError):
        telemetry_available = False
        snapshots = [{**definition, **calculate_error_budget(0, 0, definition["target"])} for definition in SLO_DEFINITIONS]
    return snapshots, telemetry_available


def _service_definitions() -> tuple[dict[str, str], ...]:
    return (
        {
            "key": "platform_api",
            "name": "Platform API",
            "url": "http://127.0.0.1:8000/readyz",
            "endpoint": "readyz",
        },
        {
            "key": "gateway",
            "name": "Gateway",
            "url": f"{os.getenv('GATEWAY_URL', 'http://gateway:8080').rstrip('/')}/healthz",
            "endpoint": "healthz",
        },
        {
            "key": "agent_runtime",
            "name": "Agent runtime",
            "url": f"{os.getenv('AGENT_SERVER_URL', 'http://agent-server:8001').rstrip('/')}/healthz",
            "endpoint": "healthz",
        },
        {
            "key": "sandbox_fleet",
            "name": "Sandbox fleet",
            "url": f"{os.getenv('SANDBOX_FLEET_URL', 'http://sandbox-fleet:8002').rstrip('/')}/healthz",
            "endpoint": "healthz",
        },
    )


async def _check_service(client: httpx.AsyncClient, definition: dict[str, str]) -> dict[str, Any]:
    checked_at = datetime.now(UTC)
    try:
        response = await client.get(definition["url"])
        healthy = 200 <= response.status_code < 300
        return {
            "key": definition["key"],
            "name": definition["name"],
            "endpoint": definition["endpoint"],
            "status": "healthy" if healthy else "critical",
            "status_code": response.status_code,
            "checked_at": checked_at,
        }
    except (httpx.HTTPError, OSError):
        return {
            "key": definition["key"],
            "name": definition["name"],
            "endpoint": definition["endpoint"],
            "status": "critical",
            "status_code": None,
            "checked_at": checked_at,
        }


async def _load_service_signals() -> list[dict[str, Any]]:
    definitions = _service_definitions()
    async with httpx.AsyncClient(timeout=1.5, follow_redirects=False) as client:
        return list(await asyncio.gather(*(_check_service(client, definition) for definition in definitions)))


@router.get("/summary")
async def observability_summary(actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    snapshots, telemetry_available = await _load_slo_snapshots()
    service_signals = await _load_service_signals()
    return {
        "schema_version": "workama.observability.summary.v1",
        "generated_at": datetime.now(UTC),
        "verification_scope": "local-compose" if telemetry_available else "local-contract",
        "telemetry_available": telemetry_available,
        "slo_window": "5m recording rule / 30d policy",
        "snapshots": snapshots,
        "service_signals": service_signals,
        "external_boundary": "production alert routing, long-term Loki/Tempo retention and 60-day SLA remain pending_external",
    }


@router.get("/semantic-contract")
async def observability_semantic_contract(actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    return SEMANTIC_CONTRACT


__all__ = ["SEMANTIC_CONTRACT", "SLO_DEFINITIONS", "calculate_error_budget", "router"]
