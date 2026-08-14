# Contract drift governance batch 6: contract regression tests.
#
# Covers ListResponse<T> contract for endpoints fixed in batch 6:
# - jobs.py: list_operations, list_jobs, list_job_runs, list_dlq
# - a2a.py: list_agent_cards
# - platform_support.py: list_templates, list_lifecycle_policies, list_lifecycle_runs
# - open_platform.py: list_oauth_clients, list_webhooks, list_webhook_deliveries
# - external_apps.py: list_external_apps, list_external_invocations, list_templates, list_marketplace_skills
#
# Each test verifies the response contains: data, items, next_cursor, has_more, meta.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from workama_platform.core import Actor
from workama_platform.modules import a2a, external_apps, jobs, open_platform, platform_support


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


class _ListConnection:
    # Simple connection mock: all execute calls return the same rows.

    def __init__(self, rows: list[Any] | None = None, row: Any = None) -> None:
        self._rows = rows or []
        self._row = row

    def transaction(self):
        return _Transaction(self)

    async def execute(self, statement, params=None):
        if 'RETURNING' in statement:
            return _Result(row=self._row)
        return _Result(row=self._row, rows=self._rows)

    async def commit(self):
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


def _assert_listresponse_envelope(result: dict[str, Any]) -> None:
    # Verify ListResponse<T> contract shape.
    assert 'items' in result, 'backward-compatible items field must be present'
    assert 'data' in result, 'contract field data must exist'
    assert result['data'] == result['items'], 'data and items must point to same data'
    assert 'next_cursor' in result
    assert 'has_more' in result
    assert isinstance(result['has_more'], bool)
    assert 'meta' in result and 'request_id' in result['meta']


# ---------------------------------------------------------------------------
# jobs.py contract tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jobs_list_operations_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [{'id': 'op_1', 'workspace_id': 'wsp_1', 'operation_type': 'export', 'status': 'succeeded', 'created_at': now}]
    monkeypatch.setattr(jobs, 'pool', _Pool(_ListConnection(rows=rows)))
    result = await jobs.list_operations(_admin(), limit=50)
    _assert_listresponse_envelope(result)
    assert result['meta']['count'] == 1


@pytest.mark.asyncio
async def test_jobs_list_jobs_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [{'id': 'job_1', 'workspace_id': 'wsp_1', 'operation_id': 'op_1', 'status': 'succeeded', 'created_at': now, 'operation_type': 'export'}]
    monkeypatch.setattr(jobs, 'pool', _Pool(_ListConnection(rows=rows)))
    result = await jobs.list_jobs(_admin(), limit=50)
    _assert_listresponse_envelope(result)
    assert result['meta']['count'] == 1


@pytest.mark.asyncio
async def test_jobs_list_job_runs_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    job_row = {'id': 'job_1', 'workspace_id': 'wsp_1'}
    run_rows = [{'id': 'run_1', 'job_id': 'job_1', 'attempt': 1, 'status': 'succeeded', 'started_at': now}]
    conn = _ListConnection(rows=run_rows, row=job_row)
    monkeypatch.setattr(jobs, 'pool', _Pool(conn))
    result = await jobs.list_job_runs('job_1', _admin())
    _assert_listresponse_envelope(result)
    assert result['meta']['count'] == 1


@pytest.mark.asyncio
async def test_jobs_list_dlq_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [{'id': 'dlq_1', 'workspace_id': 'wsp_1', 'job_id': 'job_1', 'failed_at': now, 'reason': 'timeout'}]
    monkeypatch.setattr(jobs, 'pool', _Pool(_ListConnection(rows=rows)))
    result = await jobs.list_dlq(_admin(), limit=50)
    _assert_listresponse_envelope(result)
    assert result['meta']['count'] == 1


# ---------------------------------------------------------------------------
# a2a.py contract tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2a_list_agent_cards_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [{
        'id': 'card_1',
        'name': 'Test Agent',
        'agent_id': 'agent_1',
        'endpoint': 'https://example.test/agent',
        'version': '1.0.0',
        'capabilities': [],
        'skills': [],
        'authentication': {},
        'status': 'active',
        'created_by': 'usr_admin',
        'created_at': now,
        'updated_at': now,
    }]
    monkeypatch.setattr(a2a, 'pool', _Pool(_ListConnection(rows=rows)))

    async def _fake_list_trusted_keys(conn, card_id, workspace_id):
        return []

    monkeypatch.setattr(a2a, '_list_trusted_keys', _fake_list_trusted_keys)

    result = await a2a.list_agent_cards(_admin())
    _assert_listresponse_envelope(result)
    assert result['meta']['count'] == 1




# ---------------------------------------------------------------------------
# platform_support.py contract tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_platform_support_list_templates_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [{
        'id': 'tpl_1',
        'workspace_id': 'wsp_1',
        'template_id': 'welcome_email',
        'version': 1,
        'locale': 'zh-CN',
        'channel': 'email',
        'subject_template': 'Welcome',
        'body_template': 'Hello {{name}}',
        'variables_schema': {},
        'sensitive_level': 'normal',
        'status': 'published',
        'content_hash': 'abc',
        'created_by': 'usr_admin',
        'published_at': now,
        'created_at': now,
        'updated_at': now,
    }]
    monkeypatch.setattr(platform_support, 'pool', _Pool(_ListConnection(rows=rows)))
    result = await platform_support.list_templates(_admin())
    _assert_listresponse_envelope(result)
    assert result['meta']['count'] == 1


@pytest.mark.asyncio
async def test_platform_support_list_lifecycle_policies_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [{
        'id': 'lcp_1',
        'workspace_id': 'wsp_1',
        'resource_type': 'notification',
        'retention_days': 30,
        'batch_size': 100,
        'status': 'enabled',
        'runbook': 'https://example.test/runbook',
        'updated_by': 'usr_admin',
        'created_at': now,
        'updated_at': now,
    }]
    monkeypatch.setattr(platform_support, 'pool', _Pool(_ListConnection(rows=rows)))
    result = await platform_support.list_lifecycle_policies(_admin())
    _assert_listresponse_envelope(result)
    assert result['meta']['count'] == 1


@pytest.mark.asyncio
async def test_platform_support_list_lifecycle_runs_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [{
        'id': 'lcr_1',
        'operation_id': 'op_1',
        'workspace_id': 'wsp_1',
        'resource_type': 'notification',
        'dry_run': False,
        'created_by': 'usr_admin',
        'created_at': now,
        'updated_at': now,
    }]
    monkeypatch.setattr(platform_support, 'pool', _Pool(_ListConnection(rows=rows)))
    result = await platform_support.list_lifecycle_runs(_admin())
    _assert_listresponse_envelope(result)
    assert result['meta']['count'] == 1


# ---------------------------------------------------------------------------
# open_platform.py contract tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_platform_list_oauth_clients_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [{
        'id': 'oauth_1',
        'client_id': 'wama_client_xyz',
        'name': 'Test Client',
        'redirect_uris': ['https://example.test/cb'],
        'scopes': ['openid'],
        'grant_types': ['authorization_code', 'refresh_token'],
        'status': 'active',
        'client_secret_last4': 'abcd',
        'version': 1,
        'created_at': now,
        'updated_at': now,
    }]
    monkeypatch.setattr(open_platform, 'pool', _Pool(_ListConnection(rows=rows)))
    result = await open_platform.list_oauth_clients(_admin())
    _assert_listresponse_envelope(result)
    assert result['meta']['count'] == 1
    item = result['data'][0]
    assert item['client_id'] == 'wama_client_xyz'
    assert item['secret_status'] == 'configured'


@pytest.mark.asyncio
async def test_open_platform_list_webhooks_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [{
        'id': 'wh_1',
        'url': 'https://example.test/hook',
        'events': ['artifact.created'],
        'description': 'test webhook',
        'secret_last4': 'wxyz',
        'status': 'active',
        'failure_count': 0,
        'version': 1,
        'created_at': now,
        'updated_at': now,
    }]
    monkeypatch.setattr(open_platform, 'pool', _Pool(_ListConnection(rows=rows)))
    result = await open_platform.list_webhooks(_admin())
    _assert_listresponse_envelope(result)
    assert result['meta']['count'] == 1
    item = result['data'][0]
    assert item['secret_status'] == 'configured'


@pytest.mark.asyncio
async def test_open_platform_list_webhook_deliveries_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    webhook_row = {'id': 'wh_1'}
    delivery_rows = [{
        'id': 'dlv_1',
        'event_type': 'artifact.created',
        'idempotency_key': 'idem_1',
        'payload_hash': 'hash_1',
        'status': 'delivered',
        'attempt': 1,
        'next_attempt_at': None,
        'response_code': 200,
        'error_code': None,
        'delivery_mode': 'async',
        'signature': 'sig',
        'delivered_at': now,
        'created_at': now,
        'updated_at': now,
    }]
    conn = _ListConnection(rows=delivery_rows, row=webhook_row)
    monkeypatch.setattr(open_platform, 'pool', _Pool(conn))
    result = await open_platform.list_webhook_deliveries('wh_1', _admin(), limit=50)
    _assert_listresponse_envelope(result)
    assert result['meta']['count'] == 1


@pytest.mark.asyncio
async def test_open_platform_list_webhook_deliveries_404_when_missing(monkeypatch):
    conn = _ListConnection(rows=[], row=None)
    monkeypatch.setattr(open_platform, 'pool', _Pool(conn))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await open_platform.list_webhook_deliveries('wh_missing', _admin(), limit=50)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# external_apps.py contract tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_apps_list_external_apps_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [{
        'id': 'extapp_1',
        'name': 'My App',
        'provider': 'anthropic',
        'endpoint': 'https://api.anthropic.test',
        'credential_hash': 'hash',
        'credential_last4': '1234',
        'config': {},
        'status': 'active',
        'enabled': True,
        'version': 1,
        'created_at': now,
        'updated_at': now,
    }]
    monkeypatch.setattr(external_apps, 'pool', _Pool(_ListConnection(rows=rows)))
    result = await external_apps.list_external_apps(_admin())
    _assert_listresponse_envelope(result)
    assert result['meta']['count'] == 1
    item = result['data'][0]
    assert item['credential_configured'] is True
    assert item['execution_mode'] in {'controlled_mock', 'http_test', 'external_http', 'external_pending'}


@pytest.mark.asyncio
async def test_external_apps_list_external_invocations_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [{
        'id': 'inv_1',
        'app_id': 'extapp_1',
        'operation': 'invoke',
        'idempotency_key': 'idem_1',
        'input_hash': 'hash_1',
        'status': 'succeeded',
        'execution_mode': 'sync_http',
        'result': {},
        'error_code': None,
        'attempt': 1,
        'max_attempts': 3,
        'next_attempt_at': None,
        'last_attempt_at': now,
        'response_code': 200,
        'claimed_at': None,
        'lease_expires_at': None,
        'created_at': now,
        'completed_at': now,
    }]
    monkeypatch.setattr(external_apps, 'pool', _Pool(_ListConnection(rows=rows)))
    result = await external_apps.list_external_invocations('extapp_1', _admin(), limit=50)
    _assert_listresponse_envelope(result)
    assert result['meta']['count'] == 1


@pytest.mark.asyncio
async def test_external_apps_list_marketplace_templates_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [{
        'id': 'tpl_1',
        'name': 'skill_template',
        'display_name': 'Skill Template',
        'template_type': 'skill',
        'version': '1.0.0',
        'description': 'demo',
        'manifest': {},
        'artifact_ref': 'artifact_1',
        'review_status': 'approved',
        'visibility': 'public',
        'status': 'published',
        'created_by': 'usr_admin',
        'reviewed_at': now,
        'created_at': now,
        'updated_at': now,
    }]
    monkeypatch.setattr(external_apps, 'pool', _Pool(_ListConnection(rows=rows)))
    result = await external_apps.list_templates(_admin())
    _assert_listresponse_envelope(result)
    assert result['meta']['count'] == 1
    item = result['data'][0]
    assert 'org_id' not in item
    assert 'workspace_id' not in item


@pytest.mark.asyncio
async def test_external_apps_list_marketplace_templates_with_type_filter(monkeypatch):
    now = datetime.now(UTC)
    rows = [{
        'id': 'tpl_2',
        'name': 'workflow_template',
        'display_name': 'Workflow Template',
        'template_type': 'workflow',
        'version': '2.0.0',
        'description': 'workflow demo',
        'manifest': {},
        'artifact_ref': 'artifact_2',
        'review_status': 'approved',
        'visibility': 'public',
        'status': 'published',
        'created_by': 'usr_admin',
        'reviewed_at': now,
        'created_at': now,
        'updated_at': now,
    }]
    monkeypatch.setattr(external_apps, 'pool', _Pool(_ListConnection(rows=rows)))
    result = await external_apps.list_templates(_admin(), template_type='workflow')
    _assert_listresponse_envelope(result)
    assert result['meta']['count'] == 1
    assert result['data'][0]['template_type'] == 'workflow'


@pytest.mark.asyncio
async def test_external_apps_list_marketplace_skills_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [{
        'id': 'skill_1',
        'publisher': 'workama',
        'name': 'example_skill',
        'semver': '1.0.0',
        'manifest': {},
        'artifact_ref': 'artifact_3',
        'source_kind': 'git',
        'content_sha256': 'sha256',
        'signature_status': 'unsigned',
        'risk_level': 'low',
        'review_status': 'approved',
        'status': 'active',
        'revision': 1,
        'created_at': now,
        'updated_at': now,
    }]
    monkeypatch.setattr(external_apps, 'pool', _Pool(_ListConnection(rows=rows)))
    result = await external_apps.list_marketplace_skills(_admin(), limit=50)
    _assert_listresponse_envelope(result)
    assert result['meta']['count'] == 1
    item = result['data'][0]
    assert item['version'] == '1.0.0'
    assert item['publisher'] == 'workama'


# ---------------------------------------------------------------------------
# Edge cases: empty results still produce the contract envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_platform_support_list_templates_empty_returns_envelope(monkeypatch):
    monkeypatch.setattr(platform_support, 'pool', _Pool(_ListConnection(rows=[])))
    result = await platform_support.list_templates(_admin())
    _assert_listresponse_envelope(result)
    assert result['meta']['count'] == 0
    assert result['data'] == []
    assert result['items'] == []


@pytest.mark.asyncio
async def test_open_platform_list_oauth_clients_empty_returns_envelope(monkeypatch):
    monkeypatch.setattr(open_platform, 'pool', _Pool(_ListConnection(rows=[])))
    result = await open_platform.list_oauth_clients(_admin())
    _assert_listresponse_envelope(result)
    assert result['meta']['count'] == 0
    assert result['has_more'] is False
    assert result['next_cursor'] is None


@pytest.mark.asyncio
async def test_external_apps_list_marketplace_skills_empty_returns_envelope(monkeypatch):
    monkeypatch.setattr(external_apps, 'pool', _Pool(_ListConnection(rows=[])))
    result = await external_apps.list_marketplace_skills(_admin(), limit=50)
    _assert_listresponse_envelope(result)
    assert result['meta']['count'] == 0
    assert result['data'] == []
