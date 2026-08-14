# Provider health check endpoint tests.
# Covers gateway/router.py health-check endpoints.
# Tests use monkeypatch to replace pool/redis/httpx dependencies.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from workama_platform.core import Actor
from workama_platform.modules.gateway import router as gw_router
from workama_platform.modules.gateway.router import (
    ProviderHealthCheckRequest,
    _health_result,
    _probe_channels_concurrently,
    _probe_provider_health,
    _provider_health_cache_key,
    get_provider_health,
    health_check_providers,
)


# ---------------------------------------------------------------------------
# Mock infrastructure
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, row: Any = None, rows: list[Any] | None = None) -> None:
        self._row = row
        self._rows = rows or []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _Transaction:
    def __init__(self, connection) -> None:
        self._connection = connection

    async def __aenter__(self):
        return self._connection

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RecordingConnection:
    # Mock connection: records execute calls, returns fetchall results.

    def __init__(self, rows: list[Any] | None = None, row: Any = None) -> None:
        self._rows = rows or []
        self._row = row
        self.executed: list[tuple[str, tuple]] = []
        self.committed = False

    def transaction(self):
        return _Transaction(self)

    async def execute(self, statement, params=None):
        self.executed.append((statement, params or ()))
        if 'RETURNING' in statement:
            return _Result(row=self._row)
        return _Result(rows=self._rows)

    async def commit(self):
        self.committed = True
        return None

    async def rollback(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    # Mock psycopg AsyncConnectionPool.

    def __init__(self, connection) -> None:
        self._connection = connection

    def connection(self):
        return self._connection


class _FakeRedis:
    # In-memory Redis mock with set/get support.

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value

    async def get(self, key: str) -> str | None:
        return self.store.get(key)


class _FailingRedis:
    # Mock Redis that fails all operations.

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        raise RuntimeError('redis unavailable')

    async def get(self, key: str) -> str | None:
        raise RuntimeError('redis unavailable')


def _admin(workspace_id: str = 'wsp_1', org_id: str = 'org_1') -> Actor:
    return Actor(
        user_id='usr_admin',
        workspace_id=workspace_id,
        org_id=org_id,
        role='admin',
        email='admin@example.test',
        display_name='Admin',
        onboarding_completed=True,
        capabilities=('*',),
        actor_type='user',
        auth_strength=2,
    )


def _non_admin(workspace_id: str = 'wsp_1', org_id: str = 'org_1') -> Actor:
    return Actor(
        user_id='usr_member',
        workspace_id=workspace_id,
        org_id=org_id,
        role='member',
        email='member@example.test',
        display_name='Member',
        onboarding_completed=True,
        capabilities=('gateway:read',),
        actor_type='user',
        auth_strength=2,
    )


def _channel(
    *,
    id: str = 'chn_1',
    name: str = 'OpenAI Primary',
    provider: str = 'openai',
    base_url: str = 'https://api.openai.com',
    status: str = 'enabled',
) -> dict[str, Any]:
    return {'id': id, 'name': name, 'provider': provider, 'base_url': base_url, 'status': status}


# ---------------------------------------------------------------------------
# Cache key & result construction tests
# ---------------------------------------------------------------------------


def test_provider_health_cache_key_includes_workspace():
    # Cache key must include workspace_id to isolate workspaces.
    key = _provider_health_cache_key('wsp_42')
    assert 'wsp_42' in key
    assert key.startswith('gw:provider_health:')


def test_health_result_contains_required_fields():
    # _health_result must return all contract-required fields.
    channel = _channel()
    result = _health_result(channel, 'healthy', 12, None)
    assert result['channel_id'] == 'chn_1'
    assert result['name'] == 'OpenAI Primary'
    assert result['provider'] == 'openai'
    assert result['base_url'] == 'https://api.openai.com'
    assert result['status'] == 'healthy'
    assert result['latency_ms'] == 12
    assert result['error_message'] is None
    assert 'last_checked' in result
    datetime.fromisoformat(result['last_checked'])


def test_health_result_strips_trailing_slash_from_base_url():
    # Trailing slash in base_url should be stripped for consistency.
    channel = _channel(base_url='https://api.openai.com/')
    result = _health_result(channel, 'healthy', 5, None)
    assert result['base_url'] == 'https://api.openai.com'


# ---------------------------------------------------------------------------
# _probe_provider_health unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_provider_health_returns_healthy_for_mock_url():
    # mock:// and local:// base_url should return healthy without HTTP request.
    channel = _channel(base_url='mock://provider')
    result = await _probe_provider_health(channel)
    assert result['status'] == 'healthy'
    assert result['latency_ms'] == 0
    assert result['error_message'] is None


@pytest.mark.asyncio
async def test_probe_provider_health_returns_healthy_for_empty_base_url():
    # Empty base_url should be treated as local, return healthy.
    channel = _channel(base_url='')
    result = await _probe_provider_health(channel)
    assert result['status'] == 'healthy'
    assert result['latency_ms'] == 0


@pytest.mark.asyncio
async def test_probe_provider_health_returns_healthy_on_2xx(monkeypatch):
    # 2xx response should be marked as healthy with latency recorded.

    class _Response:
        status_code = 200

    class _MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            return _Response()

    monkeypatch.setattr(httpx, 'AsyncClient', _MockAsyncClient)
    result = await _probe_provider_health(_channel(base_url='https://api.openai.com'))
    assert result['status'] == 'healthy'
    assert isinstance(result['latency_ms'], int)
    assert result['latency_ms'] >= 0
    assert result['error_message'] is None


@pytest.mark.asyncio
async def test_probe_provider_health_returns_unhealthy_on_non_2xx(monkeypatch):
    # Non-2xx response should be marked as unhealthy with HTTP status code.

    class _Response:
        status_code = 503

    class _MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            return _Response()

    monkeypatch.setattr(httpx, 'AsyncClient', _MockAsyncClient)
    result = await _probe_provider_health(_channel(base_url='https://api.openai.com'))
    assert result['status'] == 'unhealthy'
    assert '503' in result['error_message']


@pytest.mark.asyncio
async def test_probe_provider_health_returns_unhealthy_on_timeout(monkeypatch):
    # Timeout should be marked as unhealthy with error_message timeout.

    class _MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            raise httpx.TimeoutException('timed out')

    monkeypatch.setattr(httpx, 'AsyncClient', _MockAsyncClient)
    result = await _probe_provider_health(_channel(base_url='https://api.openai.com'))
    assert result['status'] == 'unhealthy'
    assert result['error_message'] == 'timeout'


@pytest.mark.asyncio
async def test_probe_provider_health_returns_unhealthy_on_http_error(monkeypatch):
    # httpx.HTTPError should be marked as unhealthy with error message.

    class _MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            raise httpx.ConnectError('connection refused')

    monkeypatch.setattr(httpx, 'AsyncClient', _MockAsyncClient)
    result = await _probe_provider_health(_channel(base_url='https://api.openai.com'))
    assert result['status'] == 'unhealthy'
    assert 'connection refused' in result['error_message']


# ---------------------------------------------------------------------------
# _probe_channels_concurrently unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_channels_concurrently_returns_empty_for_empty_input():
    # Empty channels list should return empty list immediately.
    result = await _probe_channels_concurrently([])
    assert result == []


@pytest.mark.asyncio
async def test_probe_channels_concurrently_probes_all_channels(monkeypatch):
    # Concurrent probing should handle all channels, preserving order.

    async def _fake_probe(channel):
        return _health_result(channel, 'healthy', 1, None)

    monkeypatch.setattr(gw_router, '_probe_provider_health', _fake_probe)
    channels = [
        _channel(id='chn_1', provider='openai'),
        _channel(id='chn_2', provider='anthropic', base_url='https://api.anthropic.com'),
        _channel(id='chn_3', provider='gemini', base_url='https://generativelanguage.googleapis.com'),
    ]
    results = await _probe_channels_concurrently(channels)
    assert len(results) == 3
    assert [r['channel_id'] for r in results] == ['chn_1', 'chn_2', 'chn_3']
    assert all(r['status'] == 'healthy' for r in results)


# ---------------------------------------------------------------------------
# POST /providers/health-check endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_returns_expected_envelope(monkeypatch):
    # POST /providers/health-check must return contract fields.
    rows = [_channel(id='chn_1', provider='openai', base_url='mock://openai')]
    conn = _RecordingConnection(rows=rows)
    monkeypatch.setattr(gw_router, 'pool', _Pool(conn))
    monkeypatch.setattr(gw_router, 'redis', _FakeRedis())

    async def _fake_probe(channels):
        return [_health_result(ch, 'healthy', 0, None) for ch in channels]

    monkeypatch.setattr(gw_router, '_probe_channels_concurrently', _fake_probe)

    payload = await health_check_providers(
        ProviderHealthCheckRequest(provider_keys=None), _admin()
    )
    assert 'results' in payload
    assert 'checked_at' in payload
    assert payload['total'] == 1
    assert payload['healthy'] == 1
    assert payload['unhealthy'] == 0
    assert payload['unknown'] == 0
    assert payload['cached'] is True
    assert payload['results'][0]['provider'] == 'openai'


@pytest.mark.asyncio
async def test_health_check_caches_result_in_redis(monkeypatch):
    # Successful probe must cache the result in Redis.
    rows = [_channel(id='chn_1', base_url='mock://openai')]
    conn = _RecordingConnection(rows=rows)
    redis_mock = _FakeRedis()
    monkeypatch.setattr(gw_router, 'pool', _Pool(conn))
    monkeypatch.setattr(gw_router, 'redis', redis_mock)

    async def _fake_probe(channels):
        return [_health_result(ch, 'healthy', 0, None) for ch in channels]

    monkeypatch.setattr(gw_router, '_probe_channels_concurrently', _fake_probe)

    await health_check_providers(ProviderHealthCheckRequest(), _admin())
    cache_key = _provider_health_cache_key('wsp_1')
    assert cache_key in redis_mock.store
    import json as _json

    cached = _json.loads(redis_mock.store[cache_key])
    assert cached['total'] == 1
    assert cached['healthy'] == 1


@pytest.mark.asyncio
async def test_health_check_writes_audit_log(monkeypatch):
    # Health check must record the operation in id_enterprise_audit_event table.
    rows = [_channel(id='chn_1', base_url='mock://openai')]
    conn = _RecordingConnection(rows=rows)
    monkeypatch.setattr(gw_router, 'pool', _Pool(conn))
    monkeypatch.setattr(gw_router, 'redis', _FakeRedis())

    async def _fake_probe(channels):
        return [_health_result(ch, 'healthy', 0, None) for ch in channels]

    monkeypatch.setattr(gw_router, '_probe_channels_concurrently', _fake_probe)

    await health_check_providers(ProviderHealthCheckRequest(), _admin())

    insert_calls = [s for s, _ in conn.executed if 'INSERT INTO id_enterprise_audit_event' in s]
    assert insert_calls, 'audit log INSERT must be called'
    assert conn.committed is True


@pytest.mark.asyncio
async def test_health_check_requires_admin(monkeypatch):
    # Non-admin calling health-check must get 403.
    from fastapi import HTTPException

    monkeypatch.setattr(gw_router, 'pool', _Pool(_RecordingConnection(rows=[])))
    monkeypatch.setattr(gw_router, 'redis', _FakeRedis())
    with pytest.raises(HTTPException) as exc:
        await health_check_providers(ProviderHealthCheckRequest(), _non_admin())
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_health_check_with_provider_keys_filters_channels(monkeypatch):
    # When provider_keys is provided, query must use ANY(%s) filter.
    rows = [_channel(id='chn_1', provider='openai')]
    conn = _RecordingConnection(rows=rows)
    monkeypatch.setattr(gw_router, 'pool', _Pool(conn))
    monkeypatch.setattr(gw_router, 'redis', _FakeRedis())

    async def _fake_probe(channels):
        return [_health_result(ch, 'healthy', 0, None) for ch in channels]

    monkeypatch.setattr(gw_router, '_probe_channels_concurrently', _fake_probe)

    payload = await health_check_providers(
        ProviderHealthCheckRequest(provider_keys=['openai']), _admin()
    )
    select_calls = [s for s, _ in conn.executed if 'SELECT' in s and 'gw_channel' in s]
    assert any('ANY(%s)' in s for s in select_calls)
    assert payload['total'] == 1


@pytest.mark.asyncio
async def test_health_check_unknown_provider_returns_unknown(monkeypatch):
    # provider_keys with configured but missing channel should return unknown status.
    conn = _RecordingConnection(rows=[])
    monkeypatch.setattr(gw_router, 'pool', _Pool(conn))
    monkeypatch.setattr(gw_router, 'redis', _FakeRedis())

    async def _fake_probe(channels):
        return []

    monkeypatch.setattr(gw_router, '_probe_channels_concurrently', _fake_probe)

    payload = await health_check_providers(
        ProviderHealthCheckRequest(provider_keys=['nonexistent_provider']), _admin()
    )
    assert payload['total'] == 1
    assert payload['unknown'] == 1
    assert payload['results'][0]['status'] == 'unknown'
    assert 'no channel configured' in payload['results'][0]['error_message']


@pytest.mark.asyncio
async def test_health_check_classifies_mixed_statuses(monkeypatch):
    # Mixed status results must correctly classify healthy/unhealthy/unknown counts.
    rows = [_channel(id='chn_1'), _channel(id='chn_2', provider='anthropic')]
    conn = _RecordingConnection(rows=rows)
    monkeypatch.setattr(gw_router, 'pool', _Pool(conn))
    monkeypatch.setattr(gw_router, 'redis', _FakeRedis())

    async def _fake_probe(channels):
        return [
            _health_result(channels[0], 'healthy', 10, None),
            _health_result(channels[1], 'unhealthy', 5, 'HTTP 503'),
        ]

    monkeypatch.setattr(gw_router, '_probe_channels_concurrently', _fake_probe)

    payload = await health_check_providers(ProviderHealthCheckRequest(), _admin())
    assert payload['total'] == 2
    assert payload['healthy'] == 1
    assert payload['unhealthy'] == 1
    assert payload['unknown'] == 0


@pytest.mark.asyncio
async def test_health_check_succeeds_even_if_redis_fails(monkeypatch):
    # Redis failure should not affect the health check main flow.
    rows = [_channel(id='chn_1', base_url='mock://openai')]
    conn = _RecordingConnection(rows=rows)
    monkeypatch.setattr(gw_router, 'pool', _Pool(conn))
    monkeypatch.setattr(gw_router, 'redis', _FailingRedis())

    async def _fake_probe(channels):
        return [_health_result(ch, 'healthy', 0, None) for ch in channels]

    monkeypatch.setattr(gw_router, '_probe_channels_concurrently', _fake_probe)

    payload = await health_check_providers(ProviderHealthCheckRequest(), _admin())
    assert payload['total'] == 1


# ---------------------------------------------------------------------------
# GET /providers/health endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_provider_health_returns_empty_when_no_cache(monkeypatch):
    # With no cache, GET /providers/health must return empty structure.
    monkeypatch.setattr(gw_router, 'pool', _Pool(_RecordingConnection()))
    monkeypatch.setattr(gw_router, 'redis', _FakeRedis())
    payload = await get_provider_health(_admin())
    assert payload['results'] == []
    assert payload['total'] == 0
    assert payload['healthy'] == 0
    assert payload['unhealthy'] == 0
    assert payload['unknown'] == 0
    assert payload['checked_at'] is None
    assert payload['cached'] is False


@pytest.mark.asyncio
async def test_get_provider_health_returns_cached_payload(monkeypatch):
    # With cache present, must return cached content marked cached=True.
    import json as _json

    cached_payload = {
        'results': [_health_result(_channel(), 'healthy', 5, None)],
        'checked_at': datetime.now(UTC).isoformat(),
        'total': 1,
        'healthy': 1,
        'unhealthy': 0,
        'unknown': 0,
    }
    redis_mock = _FakeRedis()
    redis_mock.store[_provider_health_cache_key('wsp_1')] = _json.dumps(cached_payload)
    monkeypatch.setattr(gw_router, 'pool', _Pool(_RecordingConnection()))
    monkeypatch.setattr(gw_router, 'redis', redis_mock)

    payload = await get_provider_health(_admin())
    assert payload['total'] == 1
    assert payload['healthy'] == 1
    assert payload['cached'] is True


@pytest.mark.asyncio
async def test_get_provider_health_requires_admin(monkeypatch):
    # Non-admin calling GET /providers/health must get 403.
    from fastapi import HTTPException

    monkeypatch.setattr(gw_router, 'pool', _Pool(_RecordingConnection()))
    monkeypatch.setattr(gw_router, 'redis', _FakeRedis())
    with pytest.raises(HTTPException) as exc:
        await get_provider_health(_non_admin())
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_provider_health_falls_back_when_redis_fails(monkeypatch):
    # Redis failure must fall back to empty result instead of raising.
    monkeypatch.setattr(gw_router, 'pool', _Pool(_RecordingConnection()))
    monkeypatch.setattr(gw_router, 'redis', _FailingRedis())
    payload = await get_provider_health(_admin())
    assert payload['total'] == 0
    assert payload['cached'] is False


@pytest.mark.asyncio
async def test_get_provider_health_handles_corrupt_cache(monkeypatch):
    # Corrupt cache (non-JSON) must fall back to empty result.
    redis_mock = _FakeRedis()
    redis_mock.store[_provider_health_cache_key('wsp_1')] = 'not-json{'
    monkeypatch.setattr(gw_router, 'pool', _Pool(_RecordingConnection()))
    monkeypatch.setattr(gw_router, 'redis', redis_mock)

    payload = await get_provider_health(_admin())
    assert payload['total'] == 0
    assert payload['cached'] is False


@pytest.mark.asyncio
async def test_health_check_end_to_end_with_mock_providers(monkeypatch):
    # End-to-end test: health check writes cache, then GET returns cached result.
    rows = [_channel(id='chn_1', provider='openai', base_url='mock://openai')]
    conn = _RecordingConnection(rows=rows)
    redis_mock = _FakeRedis()
    monkeypatch.setattr(gw_router, 'pool', _Pool(conn))
    monkeypatch.setattr(gw_router, 'redis', redis_mock)

    async def _fake_probe(channels):
        return [_health_result(ch, 'healthy', 0, None) for ch in channels]

    monkeypatch.setattr(gw_router, '_probe_channels_concurrently', _fake_probe)

    # POST: trigger health check
    post_payload = await health_check_providers(ProviderHealthCheckRequest(), _admin())
    assert post_payload['total'] == 1
    assert post_payload['healthy'] == 1

    # GET: retrieve cached result
    get_payload = await get_provider_health(_admin())
    assert get_payload['total'] == 1
    assert get_payload['healthy'] == 1
    assert get_payload['cached'] is True


@pytest.mark.asyncio
async def test_health_check_with_multiple_providers(monkeypatch):
    # Health check with multiple providers should probe all channels.
    rows = [
        _channel(id='chn_1', provider='openai', base_url='mock://openai'),
        _channel(id='chn_2', provider='anthropic', base_url='mock://anthropic'),
        _channel(id='chn_3', provider='gemini', base_url='mock://gemini'),
    ]
    conn = _RecordingConnection(rows=rows)
    monkeypatch.setattr(gw_router, 'pool', _Pool(conn))
    monkeypatch.setattr(gw_router, 'redis', _FakeRedis())

    async def _fake_probe(channels):
        return [_health_result(ch, 'healthy', 0, None) for ch in channels]

    monkeypatch.setattr(gw_router, '_probe_channels_concurrently', _fake_probe)

    payload = await health_check_providers(ProviderHealthCheckRequest(), _admin())
    assert payload['total'] == 3
    assert payload['healthy'] == 3
    assert payload['unhealthy'] == 0
    providers = [r['provider'] for r in payload['results']]
    assert 'openai' in providers
    assert 'anthropic' in providers
    assert 'gemini' in providers


