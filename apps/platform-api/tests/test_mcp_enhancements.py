"""Tests for MCP protocol enhancements."""
from __future__ import annotations

import pytest

from workama_platform.modules import mcp_protocol


# Health Monitor Tests
def test_health_monitor_records_successful_checks():
    monitor = mcp_protocol.McpServerHealthMonitor(max_failures=3)
    monitor.record_check("server1", healthy=True)
    assert monitor.is_healthy("server1")
    status = monitor.get_health_status("server1")
    assert status["healthy"] is True
    assert status["consecutive_failures"] == 0


def test_health_monitor_records_failures():
    monitor = mcp_protocol.McpServerHealthMonitor(max_failures=3)
    monitor.record_check("server1", healthy=False, error="timeout")
    assert monitor.is_healthy("server1")
    status = monitor.get_health_status("server1")
    assert status["consecutive_failures"] == 1
    assert status["last_error"] == "timeout"


def test_health_monitor_marks_unhealthy_after_max_failures():
    monitor = mcp_protocol.McpServerHealthMonitor(max_failures=3)
    for _ in range(3):
        monitor.record_check("server1", healthy=False, error="timeout")
    assert not monitor.is_healthy("server1")
    status = monitor.get_health_status("server1")
    assert status["consecutive_failures"] == 3


def test_health_monitor_resets_on_success():
    monitor = mcp_protocol.McpServerHealthMonitor(max_failures=3)
    monitor.record_check("server1", healthy=False, error="timeout")
    monitor.record_check("server1", healthy=False, error="timeout")
    monitor.record_check("server1", healthy=True)
    assert monitor.is_healthy("server1")
    status = monitor.get_health_status("server1")
    assert status["consecutive_failures"] == 0
    assert status["last_error"] is None


def test_health_monitor_unknown_server_is_healthy():
    monitor = mcp_protocol.McpServerHealthMonitor()
    assert monitor.is_healthy("unknown")
    status = monitor.get_health_status("unknown")
    assert status["healthy"] is True


def test_health_monitor_clear():
    monitor = mcp_protocol.McpServerHealthMonitor()
    monitor.record_check("server1", healthy=False)
    monitor.clear()
    assert monitor.is_healthy("server1")


# Capability Cache tests
def test_capability_cache_set_and_get():
    cache = mcp_protocol.McpCapabilityCache(ttl_seconds=60.0)
    capabilities = {"tools": ["tool1"]}
    cache.set("server1", capabilities)
    retrieved = cache.get("server1")
    assert retrieved == capabilities


def test_capability_cache_expires():
    now = [100.0]
    cache = mcp_protocol.McpCapabilityCache(ttl_seconds=5.0, clock=lambda: now[0])
    capabilities = {"tools": ["tool1"]}
    cache.set("server1", capabilities)
    now[0] = 106.0
    assert cache.get("server1") is None


def test_capability_cache_invalidate():
    cache = mcp_protocol.McpCapabilityCache()
    cache.set("server1", {"tools": []})
    cache.invalidate("server1")
    assert cache.get("server1") is None


def test_capability_cache_clear():
    cache = mcp_protocol.McpCapabilityCache()
    cache.set("server1", {"tools": []})
    cache.set("server2", {"tools": []})
    cache.clear()
    assert cache.get("server1") is None
    assert cache.get("server2") is None


# Load Balancer tests
def test_load_balancer_round_robin():
    lb = mcp_protocol.McpLoadBalancer()
    lb.register_instance("pool1", "server1")
    lb.register_instance("pool1", "server2")
    lb.register_instance("pool1", "server3")
    assert lb.select_server("pool1") == "server1"
    assert lb.select_server("pool1") == "server2"
    assert lb.select_server("pool1") == "server3"
    assert lb.select_server("pool1") == "server1"


def test_load_balancer_empty_pool():
    lb = mcp_protocol.McpLoadBalancer()
    assert lb.select_server("pool1") is None


def test_load_balancer_unregister():
    lb = mcp_protocol.McpLoadBalancer()
    lb.register_instance("pool1", "server1")
    lb.register_instance("pool1", "server2")
    lb.unregister_instance("pool1", "server1")
    assert lb.get_pool_size("pool1") == 1
    assert lb.select_server("pool1") == "server2"


def test_load_balancer_pool_size():
    lb = mcp_protocol.McpLoadBalancer()
    assert lb.get_pool_size("pool1") == 0
    lb.register_instance("pool1", "server1")
    assert lb.get_pool_size("pool1") == 1
    lb.register_instance("pool1", "server2")
    assert lb.get_pool_size("pool1") == 2


def test_load_balancer_clear():
    lb = mcp_protocol.McpLoadBalancer()
    lb.register_instance("pool1", "server1")
    lb.clear()
    assert lb.get_pool_size("pool1") == 0


# Graceful Shutdown tests
def test_graceful_shutdown_initial_state():
    shutdown = mcp_protocol.McpGracefulShutdown()
    assert not shutdown.is_shutting_down()
    assert shutdown.can_accept_new_session()


def test_graceful_shutdown_start():
    shutdown = mcp_protocol.McpGracefulShutdown()
    shutdown.start_shutdown()
    assert shutdown.is_shutting_down()
    assert not shutdown.can_accept_new_session()


def test_graceful_shutdown_register_sessions():
    shutdown = mcp_protocol.McpGracefulShutdown()
    shutdown.register_session("session1")
    shutdown.register_session("session2")
    assert shutdown.get_active_session_count() == 2


def test_graceful_shutdown_unregister_session():
    shutdown = mcp_protocol.McpGracefulShutdown()
    shutdown.register_session("session1")
    shutdown.unregister_session("session1")
    assert shutdown.get_active_session_count() == 0


@pytest.mark.asyncio
async def test_graceful_shutdown_wait_for_drain_success():
    now = [100.0]
    shutdown = mcp_protocol.McpGracefulShutdown(drain_timeout=5.0, clock=lambda: now[0])
    shutdown.start_shutdown()
    shutdown.register_session("session1")
    shutdown.unregister_session("session1")
    result = await shutdown.wait_for_drain()
    assert result is True


@pytest.mark.asyncio
async def test_graceful_shutdown_wait_for_drain_timeout():
    now = [100.0]
    shutdown = mcp_protocol.McpGracefulShutdown(drain_timeout=0.5, clock=lambda: now[0])
    shutdown.start_shutdown()
    shutdown.register_session("session1")
    now[0] = 101.0
    result = await shutdown.wait_for_drain()
    assert result is False


def test_graceful_shutdown_reset():
    shutdown = mcp_protocol.McpGracefulShutdown()
    shutdown.start_shutdown()
    shutdown.register_session("session1")
    shutdown.reset()
    assert not shutdown.is_shutting_down()
    assert shutdown.get_active_session_count() == 0


# Operation Tracker tests
def test_operation_tracker_start_operation():
    tracker = mcp_protocol.McpOperationTracker()
    tracker.start_operation("op1", session_id="session1", method="tools/call")
    op = tracker.get_operation("op1")
    assert op is not None
    assert op["session_id"] == "session1"
    assert op["method"] == "tools/call"
    assert op["progress"] == 0.0
    assert op["completed"] is False
    assert op["cancelled"] is False


def test_operation_tracker_update_progress():
    tracker = mcp_protocol.McpOperationTracker()
    tracker.start_operation("op1", session_id="session1", method="tools/call", total=100.0)
    result = tracker.update_progress("op1", 50.0)
    assert result is True
    op = tracker.get_operation("op1")
    assert op["progress"] == 50.0


def test_operation_tracker_complete():
    tracker = mcp_protocol.McpOperationTracker()
    tracker.start_operation("op1", session_id="session1", method="tools/call")
    tracker.complete_operation("op1")
    op = tracker.get_operation("op1")
    assert op["completed"] is True


def test_operation_tracker_cancel():
    tracker = mcp_protocol.McpOperationTracker()
    tracker.start_operation("op1", session_id="session1", method="tools/call")
    result = tracker.cancel_operation("op1")
    assert result is True
    op = tracker.get_operation("op1")
    assert op["cancelled"] is True


def test_operation_tracker_cannot_update_completed():
    tracker = mcp_protocol.McpOperationTracker()
    tracker.start_operation("op1", session_id="session1", method="tools/call")
    tracker.complete_operation("op1")
    result = tracker.update_progress("op1", 50.0)
    assert result is False


def test_operation_tracker_cannot_cancel_completed():
    tracker = mcp_protocol.McpOperationTracker()
    tracker.start_operation("op1", session_id="session1", method="tools/call")
    tracker.complete_operation("op1")
    result = tracker.cancel_operation("op1")
    assert result is False


def test_operation_tracker_get_session_operations():
    tracker = mcp_protocol.McpOperationTracker()
    tracker.start_operation("op1", session_id="session1", method="tools/call")
    tracker.start_operation("op2", session_id="session1", method="tools/list")
    tracker.start_operation("op3", session_id="session2", method="resources/list")
    ops = tracker.get_session_operations("session1")
    assert len(ops) == 2
    op_ids = {op["operation_id"] for op in ops}
    assert op_ids == {"op1", "op2"}


def test_operation_tracker_clear():
    tracker = mcp_protocol.McpOperationTracker()
    tracker.start_operation("op1", session_id="session1", method="tools/call")
    tracker.clear()
    assert tracker.get_operation("op1") is None


# Session Registry Heartbeat tests
def test_session_registry_heartbeat():
    registry = mcp_protocol.McpSessionRegistry(ttl_seconds=5.0)
    session_id = registry.create(server_key="server1", transport="stdio", protocol_version="2025-06-18")
    result = registry.heartbeat(session_id)
    assert result is True
    session = registry.get(session_id)
    assert session is not None
    assert session.last_heartbeat > 0


def test_session_registry_heartbeat_unknown_session():
    registry = mcp_protocol.McpSessionRegistry()
    result = registry.heartbeat("unknown")
    assert result is False


def test_session_registry_heartbeat_extends_expiration():
    now = [100.0]
    registry = mcp_protocol.McpSessionRegistry(ttl_seconds=5.0, clock=lambda: now[0])
    session_id = registry.create(server_key="server1", transport="stdio", protocol_version="2025-06-18")
    session = registry.get(session_id)
    initial_expires = session.expires_at
    now[0] = 103.0
    registry.heartbeat(session_id)
    session = registry.get(session_id)
    assert session.expires_at > initial_expires


# Integration tests
@pytest.mark.asyncio
async def test_bridge_tracks_operations():
    server = {
        "id": "test",
        "transport": "streamable_http",
        "endpoint_or_command": "mock://test",
        "auth_type": "none",
        "protocol_version": "2025-06-18",
    }
    mcp_protocol.reset_mcp_enhancements()
    initialized = await mcp_protocol.bridge_jsonrpc(
        server,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
    )
    session_id = initialized["result"]["session_id"]
    await mcp_protocol.bridge_jsonrpc(
        server,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"session_id": session_id}},
    )
    operations = mcp_protocol._OPERATION_TRACKER.get_session_operations(session_id)
    assert len(operations) == 1
    assert operations[0]["method"] == "tools/list"
    assert operations[0]["completed"] is True


@pytest.mark.asyncio
async def test_bridge_updates_session_heartbeat():
    server = {
        "id": "test",
        "transport": "streamable_http",
        "endpoint_or_command": "mock://test",
        "auth_type": "none",
        "protocol_version": "2025-06-18",
    }
    mcp_protocol.reset_mcp_enhancements()
    initialized = await mcp_protocol.bridge_jsonrpc(
        server,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
    )
    session_id = initialized["result"]["session_id"]
    session = mcp_protocol._SESSION_REGISTRY.get(session_id)
    initial_heartbeat = session.last_heartbeat
    await mcp_protocol.bridge_jsonrpc(
        server,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"session_id": session_id}},
    )
    session = mcp_protocol._SESSION_REGISTRY.get(session_id)
    assert session.last_heartbeat >= initial_heartbeat


def test_reset_mcp_enhancements():
    mcp_protocol._SERVER_HEALTH_MONITOR.record_check("server1", healthy=False)
    mcp_protocol._CAPABILITY_CACHE.set("server1", {"tools": []})
    mcp_protocol._LOAD_BALANCER.register_instance("pool1", "server1")
    mcp_protocol._GRACEFUL_SHUTDOWN.start_shutdown()
    mcp_protocol._OPERATION_TRACKER.start_operation("op1", session_id="session1", method="tools/call")
    mcp_protocol.reset_mcp_enhancements()
    assert mcp_protocol._SERVER_HEALTH_MONITOR.is_healthy("server1")
    assert mcp_protocol._CAPABILITY_CACHE.get("server1") is None
    assert mcp_protocol._LOAD_BALANCER.get_pool_size("pool1") == 0
    assert not mcp_protocol._GRACEFUL_SHUTDOWN.is_shutting_down()
    assert mcp_protocol._OPERATION_TRACKER.get_operation("op1") is None
