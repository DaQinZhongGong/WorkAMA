"""Integration tests for the strict-microVM fail-closed path on ``/healthz``.

These tests exercise the FastAPI ``/healthz`` endpoint end-to-end (via
``httpx.ASGITransport``) to prove that:

* Strict mode fail-closed returns HTTP 503 when ``SANDBOX_REQUIRE_MICROVM=true``
  and the runtime is not gVisor.
* The fail-closed check happens *before* any DB I/O, so the fleet refuses to
  report healthy even if Postgres is unreachable.
* When strict mode is satisfied (gVisor detected), ``/healthz`` proceeds past
  the guard and surfaces ``runtime_isolation`` in the response.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock

import httpx
import pytest

pytestmark = pytest.mark.asyncio

from workama_sandbox import main


class _FakeResult:
    async def fetchone(self):
        return {"count": 0}


class _FakeConn:
    async def execute(self, *_args, **_kwargs):
        return _FakeResult()


class _FakePool:
    """Stand-in for ``AsyncConnectionPool`` that yields a fake connection."""

    def connection(self):
        @contextlib.asynccontextmanager
        async def _cm():
            yield _FakeConn()

        return _cm()


@pytest.fixture
def _fake_pool(monkeypatch):
    """Replace the module-level pool so /healthz does not need a real DB."""
    monkeypatch.setattr(main, "pool", _FakePool())
    return _FakePool()


@pytest.fixture
def _disable_docker_io(monkeypatch):
    """Stop ``provider_status`` from trying to reach the real Docker daemon."""
    monkeypatch.setattr(
        main,
        "docker_client",
        MagicMock(),
    )
    # Make runtime_available return (False, []) so provider_status is deterministic.
    monkeypatch.setattr(main, "runtime_available", lambda: (False, []))


async def test_healthz_returns_503_when_strict_mode_fails(monkeypatch, _fake_pool, _disable_docker_io):
    """Strict-mode fail-closed: 503 when SANDBOX_REQUIRE_MICROVM=true and runtime is not gVisor."""
    monkeypatch.setattr(main.settings, "sandbox_require_microvm", True)
    monkeypatch.setattr(main, "detect_runtime_isolation", lambda: "runc-dev")

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://strict.test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 503
    body = response.json()
    assert "strict microVM required but runtime is not gVisor" in body["detail"]


async def test_healthz_strict_mode_503_takes_precedence_over_db(monkeypatch, _disable_docker_io):
    """The 503 must fire *before* any DB call: a broken pool must not change the outcome."""
    monkeypatch.setattr(main.settings, "sandbox_require_microvm", True)
    monkeypatch.setattr(main, "detect_runtime_isolation", lambda: "unknown")

    class _ExplodingPool:
        def connection(self):
            raise AssertionError("strict-mode 503 must short-circuit before pool.connection() is reached")

    monkeypatch.setattr(main, "pool", _ExplodingPool())

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://strict.test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 503
    assert "strict microVM required but runtime is not gVisor" in response.json()["detail"]


async def test_healthz_surfaces_runtime_isolation_when_strict_mode_satisfied(monkeypatch, _fake_pool, _disable_docker_io):
    """When gVisor is detected under strict mode, /healthz reports 200 and exposes runtime_isolation."""
    monkeypatch.setattr(main.settings, "sandbox_require_microvm", True)
    monkeypatch.setattr(main, "detect_runtime_isolation", lambda: "gvisor")

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://strict.test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["runtime_isolation"] == "gvisor"
    assert body["strict_microvm_enforced"] is True


async def test_healthz_passes_when_strict_mode_disabled_and_runtime_is_runc_dev(monkeypatch, _fake_pool, _disable_docker_io):
    """Local dev (SANDBOX_REQUIRE_MICROVM=false) keeps working with runc-dev."""
    monkeypatch.setattr(main.settings, "sandbox_require_microvm", False)
    monkeypatch.setattr(main, "detect_runtime_isolation", lambda: "runc-dev")

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://local.test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["runtime_isolation"] == "runc-dev"
    assert body["strict_microvm_enforced"] is False
