"""金标集管理控制台补齐端点 单元测试（AC-RAG-008）。

覆盖范围（均为 knowledge_eval 模块新增端点）：
- PATCH /golden-sets/{id}          局部更新 name/description/dataset_id、404、403
- DELETE /golden-sets/{id}         级联删除报告明细/报告/用例/集合、workspace 隔离、404、403
- PATCH /golden-sets/{id}/cases/{case_id}   局部更新用例、404、403
- DELETE /golden-sets/{id}/cases/{case_id}  删除用例、404、403
- POST /golden-sets/{id}/cases/import       批量导入、计数、404、403
- GET  /golden-sets/{id}/cases/export       json / csv 导出、表头、非法 format 422、404、403

测试风格与 test_rag_golden.py 一致：内联 _Result/_SeqConnection/_Pool + monkeypatch。
"""
from __future__ import annotations

import csv
import io
import json

import pytest
from fastapi import HTTPException, Response

from workama_platform.core import Actor
from workama_platform.modules import knowledge_eval as ke


# --- mock 基础设施 ---------------------------------------------------------


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []
        self.rowcount = len(self._rows)

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return list(self._rows)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _SeqConnection:
    """按调用顺序返回预设 Result，记录所有 execute 调用。"""

    def __init__(self, results=None):
        self._results = list(results) if results else []
        self.calls: list[tuple[str, tuple]] = []
        self._idx = 0

    def transaction(self):
        return _Transaction()

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if self._idx < len(self._results):
            r = self._results[self._idx]
            self._idx += 1
            return r
        return _Result()

    async def commit(self):
        return None


class _Pool:
    def __init__(self, connection):
        self._connection = connection

    def connection(self):
        connection = self._connection

        class _Context:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *_args):
                return False

        return _Context()


def _actor(
    role="admin",
    capabilities=("dataset:write", "dataset:read"),
    workspace_id="wsp_test",
) -> Actor:
    return Actor(
        user_id="usr_test",
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email="admin@example.test",
        display_name="Admin",
        onboarding_completed=True,
        capabilities=capabilities,
    )


def _golden_set_row(**overrides) -> dict:
    base = {
        "id": "rgs_1",
        "workspace_id": "wsp_test",
        "name": "golden-smoke",
        "description": "",
        "dataset_id": None,
        "created_by": "usr_test",
        "created_at": "2026-07-28T10:00:00+00:00",
        "updated_at": "2026-07-28T10:00:00+00:00",
    }
    base.update(overrides)
    return base


def _golden_case_row(**overrides) -> dict:
    base = {
        "id": "rgc_1",
        "golden_set_id": "rgs_1",
        "workspace_id": "wsp_test",
        "query": "Q1",
        "expected_answer": "",
        "expected_context_ids": ["chk_a"],
        "tags": ["smoke"],
        "created_at": "2026-07-28T10:00:00+00:00",
    }
    base.update(overrides)
    return base


def _find(conn, needle: str, extra: str | None = None):
    """在 conn.calls 中找到第一条同时包含 needle / extra 的 SQL。"""
    for query, params in conn.calls:
        if needle in query and (extra is None or extra in query):
            return query, params
    raise AssertionError(f"SQL not found: {needle} / {extra}")


# ============================================================================
# PATCH /golden-sets/{id}
# ============================================================================


@pytest.mark.asyncio
async def test_update_golden_set_patches_name(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),
            _Result(row=_golden_set_row(name="renamed")),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.update_golden_set("rgs_1", ke.GoldenSetPatch(name="renamed"), _actor())

    assert result["name"] == "renamed"
    query, _ = _find(conn, "UPDATE rag_golden_set", "COALESCE")
    assert "updated_at=now()" in query


@pytest.mark.asyncio
async def test_update_golden_set_partial_leaves_other_fields_none(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),
            _Result(row=_golden_set_row(description="new desc")),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.update_golden_set(
        "rgs_1", ke.GoldenSetPatch(description="new desc"), _actor()
    )

    assert result["description"] == "new desc"
    _, params = _find(conn, "UPDATE rag_golden_set", "COALESCE")
    assert params[0] is None
    assert params[1] == "new desc"


@pytest.mark.asyncio
async def test_update_golden_set_strips_name_whitespace(monkeypatch):
    conn = _SeqConnection(
        results=[_Result(row=_golden_set_row()), _Result(row=_golden_set_row(name="trimmed"))]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    await ke.update_golden_set("rgs_1", ke.GoldenSetPatch(name="  trimmed  "), _actor())

    _, params = _find(conn, "UPDATE rag_golden_set", "COALESCE")
    assert params[0] == "trimmed"


@pytest.mark.asyncio
async def test_update_golden_set_scopes_by_workspace(monkeypatch):
    conn = _SeqConnection(
        results=[_Result(row=_golden_set_row()), _Result(row=_golden_set_row())]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    await ke.update_golden_set("rgs_1", ke.GoldenSetPatch(name="x"), _actor())

    query, params = _find(conn, "UPDATE rag_golden_set", "COALESCE")
    assert "WHERE id=%s AND workspace_id=%s" in query
    assert params[-2:] == ("rgs_1", "wsp_test")


@pytest.mark.asyncio
async def test_update_golden_set_returns_404_when_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await ke.update_golden_set("rgs_missing", ke.GoldenSetPatch(name="x"), _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_update_golden_set_returns_403_without_capability():
    with pytest.raises(HTTPException) as exc:
        await ke.update_golden_set(
            "rgs_1", ke.GoldenSetPatch(name="x"), _actor(capabilities=("dataset:read",))
        )
    assert exc.value.status_code == 403


# ============================================================================
# DELETE /golden-sets/{id}
# ============================================================================


@pytest.mark.asyncio
async def test_delete_golden_set_cascades_children(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=_golden_set_row())])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    response = await ke.delete_golden_set("rgs_1", _actor())

    assert isinstance(response, Response)
    assert response.status_code == 204
    queries = [q for q, _ in conn.calls]
    assert any("DELETE FROM rag_eval_report_case" in q for q in queries)
    assert any("DELETE FROM rag_eval_report WHERE golden_set_id" in q for q in queries)
    assert any("DELETE FROM rag_golden_case" in q for q in queries)
    assert any("DELETE FROM rag_golden_set" in q for q in queries)


@pytest.mark.asyncio
async def test_delete_golden_set_scopes_by_workspace(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=_golden_set_row())])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    await ke.delete_golden_set("rgs_1", _actor())

    query, params = _find(conn, "DELETE FROM rag_golden_set")
    assert "workspace_id=%s" in query
    assert params == ("rgs_1", "wsp_test")


@pytest.mark.asyncio
async def test_delete_golden_set_returns_404_when_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await ke.delete_golden_set("rgs_missing", _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_golden_set_returns_403_without_capability():
    with pytest.raises(HTTPException) as exc:
        await ke.delete_golden_set("rgs_1", _actor(capabilities=("dataset:read",)))
    assert exc.value.status_code == 403


# ============================================================================
# PATCH /golden-sets/{id}/cases/{case_id}
# ============================================================================


@pytest.mark.asyncio
async def test_update_golden_case_patches_query(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),
            _Result(row=_golden_case_row(query="Q1-updated")),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.update_golden_case(
        "rgs_1", "rgc_1", ke.GoldenCasePatch(query="Q1-updated"), _actor()
    )

    assert result["query"] == "Q1-updated"
    assert any("UPDATE rag_golden_set SET updated_at=now()" in q for q, _ in conn.calls)


@pytest.mark.asyncio
async def test_update_golden_case_patches_expected_context_ids(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),
            _Result(row=_golden_case_row(expected_context_ids=["chk_x", "chk_y"])),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.update_golden_case(
        "rgs_1",
        "rgc_1",
        ke.GoldenCasePatch(expected_context_ids=["chk_x", "chk_y"]),
        _actor(),
    )

    assert result["expected_context_ids"] == ["chk_x", "chk_y"]


@pytest.mark.asyncio
async def test_update_golden_case_patches_tags(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),
            _Result(row=_golden_case_row(tags=["regression", "p0"])),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    result = await ke.update_golden_case(
        "rgs_1", "rgc_1", ke.GoldenCasePatch(tags=["regression", "p0"]), _actor()
    )

    assert result["tags"] == ["regression", "p0"]


@pytest.mark.asyncio
async def test_update_golden_case_scopes_by_set_and_workspace(monkeypatch):
    conn = _SeqConnection(
        results=[_Result(row=_golden_set_row()), _Result(row=_golden_case_row())]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    await ke.update_golden_case("rgs_1", "rgc_1", ke.GoldenCasePatch(query="Q"), _actor())

    query, params = _find(conn, "UPDATE rag_golden_case")
    assert "WHERE id=%s AND golden_set_id=%s AND workspace_id=%s" in query
    assert params[-3:] == ("rgc_1", "rgs_1", "wsp_test")


@pytest.mark.asyncio
async def test_update_golden_case_returns_404_when_case_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=_golden_set_row()), _Result(row=None)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await ke.update_golden_case(
            "rgs_1", "rgc_missing", ke.GoldenCasePatch(query="Q"), _actor()
        )
    assert exc.value.status_code == 404
    assert "Golden case not found" in exc.value.detail


@pytest.mark.asyncio
async def test_update_golden_case_returns_404_when_set_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await ke.update_golden_case(
            "rgs_missing", "rgc_1", ke.GoldenCasePatch(query="Q"), _actor()
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_update_golden_case_returns_403_without_capability():
    with pytest.raises(HTTPException) as exc:
        await ke.update_golden_case(
            "rgs_1", "rgc_1", ke.GoldenCasePatch(query="Q"), _actor(capabilities=("dataset:read",))
        )
    assert exc.value.status_code == 403


# ============================================================================
# DELETE /golden-sets/{id}/cases/{case_id}
# ============================================================================


@pytest.mark.asyncio
async def test_delete_golden_case_returns_204(monkeypatch):
    conn = _SeqConnection(
        results=[_Result(row=_golden_set_row()), _Result(row={"id": "rgc_1"})]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    response = await ke.delete_golden_case("rgs_1", "rgc_1", _actor())

    assert isinstance(response, Response)
    assert response.status_code == 204
    query, params = _find(conn, "DELETE FROM rag_golden_case")
    assert params == ("rgc_1", "rgs_1", "wsp_test")
    assert "RETURNING id" in query


@pytest.mark.asyncio
async def test_delete_golden_case_touches_set_updated_at(monkeypatch):
    conn = _SeqConnection(
        results=[_Result(row=_golden_set_row()), _Result(row={"id": "rgc_1"})]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    await ke.delete_golden_case("rgs_1", "rgc_1", _actor())

    assert any("UPDATE rag_golden_set SET updated_at=now()" in q for q, _ in conn.calls)


@pytest.mark.asyncio
async def test_delete_golden_case_returns_404_when_case_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=_golden_set_row()), _Result(row=None)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await ke.delete_golden_case("rgs_1", "rgc_missing", _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_golden_case_returns_403_without_capability():
    with pytest.raises(HTTPException) as exc:
        await ke.delete_golden_case("rgs_1", "rgc_1", _actor(capabilities=("dataset:read",)))
    assert exc.value.status_code == 403


# ============================================================================
# POST /golden-sets/{id}/cases/import
# ============================================================================


@pytest.mark.asyncio
async def test_import_golden_cases_creates_all_items(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),
            _Result(row=_golden_case_row(id="rgc_1", query="Q1")),
            _Result(row=_golden_case_row(id="rgc_2", query="Q2")),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.GoldenCaseImport(
        items=[
            ke.GoldenCaseCreate(query="Q1", expected_context_ids=["chk_a"]),
            ke.GoldenCaseCreate(query="Q2", tags=["p0"]),
        ]
    )
    result = await ke.import_golden_cases("rgs_1", body, _actor())

    assert result["golden_set_id"] == "rgs_1"
    assert result["created"] == 2
    assert [item["query"] for item in result["items"]] == ["Q1", "Q2"]
    inserts = [q for q, _ in conn.calls if "INSERT INTO rag_golden_case" in q]
    assert len(inserts) == 2


@pytest.mark.asyncio
async def test_import_golden_cases_passes_fields_to_insert(monkeypatch):
    conn = _SeqConnection(
        results=[_Result(row=_golden_set_row()), _Result(row=_golden_case_row())]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    body = ke.GoldenCaseImport(
        items=[
            ke.GoldenCaseCreate(
                query="Q1",
                expected_answer="A1",
                expected_context_ids=["chk_a", "chk_b"],
                tags=["smoke"],
            )
        ]
    )
    await ke.import_golden_cases("rgs_1", body, _actor())

    _, params = _find(conn, "INSERT INTO rag_golden_case")
    assert params[1] == "rgs_1"
    assert params[2] == "wsp_test"
    assert params[3] == "Q1"
    assert params[4] == "A1"
    assert params[5] == ["chk_a", "chk_b"]
    assert params[6] == ["smoke"]


@pytest.mark.asyncio
async def test_import_golden_cases_touches_set_updated_at(monkeypatch):
    conn = _SeqConnection(
        results=[_Result(row=_golden_set_row()), _Result(row=_golden_case_row())]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    await ke.import_golden_cases(
        "rgs_1", ke.GoldenCaseImport(items=[ke.GoldenCaseCreate(query="Q1")]), _actor()
    )

    assert any("UPDATE rag_golden_set SET updated_at=now()" in q for q, _ in conn.calls)


@pytest.mark.asyncio
async def test_import_golden_cases_returns_404_when_set_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await ke.import_golden_cases(
            "rgs_missing", ke.GoldenCaseImport(items=[ke.GoldenCaseCreate(query="Q")]), _actor()
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_import_golden_cases_returns_403_without_capability():
    body = ke.GoldenCaseImport(items=[ke.GoldenCaseCreate(query="Q")])
    with pytest.raises(HTTPException) as exc:
        await ke.import_golden_cases("rgs_1", body, _actor(capabilities=("dataset:read",)))
    assert exc.value.status_code == 403


def test_import_golden_cases_rejects_empty_items():
    with pytest.raises(Exception):
        ke.GoldenCaseImport(items=[])


# ============================================================================
# GET /golden-sets/{id}/cases/export
# ============================================================================


@pytest.mark.asyncio
async def test_export_golden_cases_json_is_import_compatible(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),
            _Result(
                rows=[
                    _golden_case_row(id="rgc_1", query="Q1", expected_answer="A1"),
                    _golden_case_row(id="rgc_2", query="Q2", tags=["p0"]),
                ]
            ),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    response = await ke.export_golden_cases("rgs_1", _actor(), format="json")

    assert isinstance(response, Response)
    assert response.media_type == "application/json"
    payload = json.loads(response.body.decode())
    assert [item["query"] for item in payload["items"]] == ["Q1", "Q2"]
    # 导出结构可直接回灌 import 端点
    ke.GoldenCaseImport(items=[ke.GoldenCaseCreate(**item) for item in payload["items"]])


@pytest.mark.asyncio
async def test_export_golden_cases_json_sets_attachment_header(monkeypatch):
    conn = _SeqConnection(
        results=[_Result(row=_golden_set_row()), _Result(rows=[_golden_case_row()])]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    response = await ke.export_golden_cases("rgs_1", _actor(), format="json")

    assert "attachment" in response.headers["content-disposition"]
    assert "golden_cases_rgs_1.json" in response.headers["content-disposition"]


@pytest.mark.asyncio
async def test_export_golden_cases_csv_has_header_and_rows(monkeypatch):
    conn = _SeqConnection(
        results=[
            _Result(row=_golden_set_row()),
            _Result(
                rows=[
                    _golden_case_row(
                        id="rgc_1",
                        query="Q1",
                        expected_answer="A1",
                        expected_context_ids=["chk_a", "chk_b"],
                        tags=["smoke", "p0"],
                    )
                ]
            ),
        ]
    )
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    response = await ke.export_golden_cases("rgs_1", _actor(), format="csv")

    assert response.media_type == "text/csv"
    rows = list(csv.reader(io.StringIO(response.body.decode())))
    assert rows[0] == ["id", "query", "expected_answer", "expected_context_ids", "tags"]
    assert rows[1] == ["rgc_1", "Q1", "A1", "chk_a;chk_b", "smoke;p0"]


@pytest.mark.asyncio
async def test_export_golden_cases_csv_empty_only_header(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=_golden_set_row()), _Result(rows=[])])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    response = await ke.export_golden_cases("rgs_1", _actor(), format="csv")

    rows = [r for r in csv.reader(io.StringIO(response.body.decode())) if r]
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_export_golden_cases_rejects_invalid_format(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=_golden_set_row())])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await ke.export_golden_cases("rgs_1", _actor(), format="xlsx")
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_export_golden_cases_returns_404_when_set_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(ke, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await ke.export_golden_cases("rgs_missing", _actor(), format="json")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_export_golden_cases_returns_403_without_capability():
    with pytest.raises(HTTPException) as exc:
        await ke.export_golden_cases("rgs_1", _actor(capabilities=()), format="json")
    assert exc.value.status_code == 403
