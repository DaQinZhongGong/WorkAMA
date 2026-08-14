from __future__ import annotations

import pytest

from workama_platform.core import Actor
from workama_platform.modules import approvals


class Result:
    def __init__(self, rows=None):
        self.rows = rows or []

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return None


class Connection:
    def __init__(self):
        self.calls = []

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        return Result([{"id": "apr_test"}]) if "ORDER BY" in query else Result()

    async def commit(self):
        return None


class Pool:
    def __init__(self, connection):
        self.connection_value = connection

    def connection(self):
        pool = self

        class Context:
            async def __aenter__(self):
                return pool.connection_value

            async def __aexit__(self, *_args):
                return False

        return Context()


def actor() -> Actor:
    return Actor(
        user_id="usr_test",
        workspace_id="wsp_test",
        org_id="org_test",
        role="owner",
        email="owner@example.test",
        display_name="Owner",
        onboarding_completed=True,
    )


@pytest.mark.asyncio
async def test_list_approvals_without_filter_uses_typed_workspace_query(monkeypatch):
    connection = Connection()
    monkeypatch.setattr(approvals, "pool", Pool(connection))

    result = await approvals.list_approvals(actor(), None)

    # 契约《720》listApprovals 已升级为 ListResponse<ApprovalDTO>，保留 items 向后兼容
    assert result["items"] == [{"id": "apr_test"}]
    assert result["data"] == result["items"]
    assert result["next_cursor"] is None
    assert result["has_more"] is False
    assert result["meta"]["count"] == 1
    query, params = connection.calls[-1]
    assert "IS NULL" not in query
    assert params == ("wsp_test",)


@pytest.mark.asyncio
async def test_list_approvals_status_filter_keeps_status_parameter_typed(monkeypatch):
    connection = Connection()
    monkeypatch.setattr(approvals, "pool", Pool(connection))

    await approvals.list_approvals(actor(), "pending")

    query, params = connection.calls[-1]
    assert "status=%s" in query
    assert params == ("wsp_test", "pending")
