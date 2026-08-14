import pytest
from fastapi import HTTPException

from workama_platform.modules import design


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _SeqConnection:
    def __init__(self, results=None):
        self._results = list(results or [])
        self.calls = []

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if self._results:
            return self._results.pop(0)
        return _Result()

    async def commit(self):
        return None

    async def rollback(self):
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


def _actor():
    from workama_platform.core import Actor, ROLE_CAPABILITIES
    return Actor(
        user_id="usr_test",
        workspace_id="wsp_test",
        org_id="org_test",
        role="admin",
        email="admin@example.test",
        display_name="Admin",
        onboarding_completed=True,
        capabilities=ROLE_CAPABILITIES["admin"],
    )


# ------------------------------------------------------------------------------
# Layer CRUD
# ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_layer_creates_new_layer(monkeypatch):
    canvas = {"id": "dcanvas_1", "state": {"layers": []}, "version": 1}
    updated = {"id": "dcanvas_1", "project_id": "proj_1", "state": {"layers": [{"id": "L1", "type": "rect", "name": "Box", "transform": {"x": 10}, "properties": {"fill": "#fff"}}]}, "version": 2, "created_at": None, "updated_at": None}
    conn = _SeqConnection(results=[
        _Result(row=canvas),
        _Result(),  # delete future
        _Result(),  # insert history
        _Result(),  # delete old history
        _Result(row=updated),
    ])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.add_layer(
        "proj_1",
        design.LayerCreate(layer_id="L1", type="rect", name="Box", transform={"x": 10}, properties={"fill": "#fff"}),
        _actor(),
    )
    assert result["state"]["layers"][0]["id"] == "L1"
    assert result["version"] == 2


@pytest.mark.asyncio
async def test_add_layer_rejects_duplicate_id(monkeypatch):
    canvas = {"id": "dcanvas_1", "state": {"layers": [{"id": "L1"}]}, "version": 1}
    conn = _SeqConnection(results=[_Result(row=canvas)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.add_layer("proj_1", design.LayerCreate(layer_id="L1", type="rect", name="Box"), _actor())
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_update_layer_patches_properties(monkeypatch):
    canvas = {"id": "dcanvas_1", "state": {"layers": [{"id": "L1", "type": "rect", "name": "Old", "transform": {"x": 0}, "properties": {}}]}, "version": 1}
    updated = {"id": "dcanvas_1", "project_id": "proj_1", "state": {"layers": [{"id": "L1", "type": "rect", "name": "New", "transform": {"x": 10}, "properties": {}}]}, "version": 2, "created_at": None, "updated_at": None}
    conn = _SeqConnection(results=[
        _Result(row=canvas),
        _Result(),  # delete future
        _Result(),  # insert history
        _Result(),  # delete old history
        _Result(row=updated),
    ])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.update_layer("proj_1", "L1", design.LayerPatch(name="New", transform={"x": 10}), _actor())
    assert result["state"]["layers"][0]["name"] == "New"
    assert result["state"]["layers"][0]["transform"]["x"] == 10


@pytest.mark.asyncio
async def test_update_layer_returns_404_when_missing(monkeypatch):
    canvas = {"id": "dcanvas_1", "state": {"layers": []}, "version": 1}
    conn = _SeqConnection(results=[_Result(row=canvas)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.update_layer("proj_1", "L99", design.LayerPatch(name="X"), _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_layer_removes_layer(monkeypatch):
    canvas = {"id": "dcanvas_1", "state": {"layers": [{"id": "L1"}, {"id": "L2"}]}, "version": 1}
    updated = {"id": "dcanvas_1", "project_id": "proj_1", "state": {"layers": [{"id": "L2"}]}, "version": 2, "created_at": None, "updated_at": None}
    conn = _SeqConnection(results=[
        _Result(row=canvas),
        _Result(),  # delete future
        _Result(),  # insert history
        _Result(),  # delete old history
        _Result(row=updated),
    ])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.delete_layer("proj_1", "L1", _actor())
    assert len(result["state"]["layers"]) == 1
    assert result["state"]["layers"][0]["id"] == "L2"


@pytest.mark.asyncio
async def test_reorder_layers_changes_order(monkeypatch):
    canvas = {"id": "dcanvas_1", "state": {"layers": [{"id": "A"}, {"id": "B"}, {"id": "C"}]}, "version": 1}
    updated = {"id": "dcanvas_1", "project_id": "proj_1", "state": {"layers": [{"id": "C"}, {"id": "A"}, {"id": "B"}]}, "version": 2, "created_at": None, "updated_at": None}
    conn = _SeqConnection(results=[
        _Result(row=canvas),
        _Result(),  # delete future
        _Result(),  # insert history
        _Result(),  # delete old history
        _Result(row=updated),
    ])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.reorder_layers("proj_1", design.LayerReorder(layer_ids=["C", "A"]), _actor())
    ids = [l["id"] for l in result["state"]["layers"]]
    assert ids == ["C", "A", "B"]


# ------------------------------------------------------------------------------
# Align
# ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_align_layers_computes_left_alignment(monkeypatch):
    canvas = {
        "id": "dcanvas_1",
        "state": {
            "layers": [
                {"id": "L1", "transform": {"x": 10, "y": 0, "width": 20, "height": 10}},
                {"id": "L2", "transform": {"x": 50, "y": 0, "width": 30, "height": 10}},
            ]
        },
        "version": 1,
    }
    updated = {
        "id": "dcanvas_1",
        "project_id": "proj_1",
        "state": {
            "layers": [
                {"id": "L1", "transform": {"x": 10, "y": 0, "width": 20, "height": 10}},
                {"id": "L2", "transform": {"x": 10, "y": 0, "width": 30, "height": 10}},
            ]
        },
        "version": 2,
        "created_at": None,
        "updated_at": None,
    }
    conn = _SeqConnection(results=[
        _Result(row=canvas),
        _Result(),  # delete future
        _Result(),  # insert history
        _Result(),  # delete old history
        _Result(row=updated),
    ])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.align_layers("proj_1", design.AlignRequest(layer_ids=["L1", "L2"], alignment="left"), _actor())
    assert result["updated_layers"][1]["transform"]["x"] == 10


@pytest.mark.asyncio
async def test_align_layers_rejects_when_no_match(monkeypatch):
    canvas = {"id": "dcanvas_1", "state": {"layers": [{"id": "L1"}]}, "version": 1}
    conn = _SeqConnection(results=[_Result(row=canvas)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.align_layers("proj_1", design.AlignRequest(layer_ids=["L99"], alignment="center"), _actor())
    assert exc.value.status_code == 422


def test_align_layers_math():
    layers = [
        {"id": "a", "transform": {"x": 0, "y": 0, "width": 10, "height": 10}},
        {"id": "b", "transform": {"x": 20, "y": 30, "width": 20, "height": 20}},
    ]
    aligned = design._align_layers(layers, "right")
    assert aligned[0]["transform"]["x"] == 30  # max_right(40) - width(10)
    assert aligned[1]["transform"]["x"] == 20  # max_right(40) - width(20)

    aligned = design._align_layers(layers, "center")
    assert aligned[0]["transform"]["x"] == 15  # center(25) - 5
    assert aligned[1]["transform"]["x"] == 10  # center(25) - 10

    aligned = design._align_layers(layers, "middle")
    assert aligned[0]["transform"]["y"] == 20  # middle(25) - 5
    assert aligned[1]["transform"]["y"] == 15  # middle(25) - 10


# ------------------------------------------------------------------------------
# Export
# ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_export_project_creates_job_and_succeeds(monkeypatch):
    project = {"id": "proj_1", "status": "active"}
    canvas = {"state": {"layers": [{"id": "L1"}]}}
    job_row = {
        "id": "dexport_1", "project_id": "proj_1", "format": "png",
        "include_layers": True, "status": "succeeded",
        "result_sha256": "abc", "error_message": None,
        "created_at": None, "updated_at": None, "completed_at": None,
    }
    conn = _SeqConnection(results=[
        _Result(row=project),
        _Result(row=canvas),
        _Result(),  # insert job
        _Result(),  # update running
        _Result(),  # update succeeded
        _Result(row=job_row),
    ])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.export_project("proj_1", design.ExportRequest(format="png"), _actor())
    assert result["status"] == "succeeded"
    assert result["format"] == "png"


@pytest.mark.asyncio
async def test_export_project_returns_404_when_project_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.export_project("proj_missing", design.ExportRequest(format="svg"), _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_export_job_returns_row(monkeypatch):
    row = {
        "id": "dexport_1", "project_id": "proj_1", "format": "jpeg",
        "include_layers": False, "status": "succeeded",
        "result_sha256": "abc", "error_message": None,
        "created_at": None, "updated_at": None, "completed_at": None,
    }
    conn = _SeqConnection(results=[_Result(row=row)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.get_export_job("proj_1", "dexport_1", _actor())
    assert result["id"] == "dexport_1"
    assert result["format"] == "jpeg"


# ------------------------------------------------------------------------------
# Undo / Redo
# ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_undo_restores_previous_state(monkeypatch):
    canvas = {"id": "dcanvas_1", "state": {"layers": [{"id": "L2"}]}, "version": 2}
    past = {"id": "hist_1", "state": {"layers": [{"id": "L1"}]}}
    version_row = {"version": 1}
    conn = _SeqConnection(results=[
        _Result(row=canvas),   # select canvas
        _Result(row=past),     # select past
        _Result(),             # insert future
        _Result(),             # delete old history
        _Result(),             # update canvas
        _Result(),             # delete past
        _Result(row=version_row),  # select version
    ])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.undo_canvas("proj_1", _actor())
    assert result["state"]["layers"][0]["id"] == "L1"
    assert result["version"] == 1


@pytest.mark.asyncio
async def test_redo_restores_future_state(monkeypatch):
    canvas = {"id": "dcanvas_1", "state": {"layers": [{"id": "L1"}]}, "version": 1}
    future = {"id": "hist_2", "state": {"layers": [{"id": "L2"}]}}
    version_row = {"version": 2}
    conn = _SeqConnection(results=[
        _Result(row=canvas),   # select canvas
        _Result(row=future),   # select future
        _Result(),             # insert past
        _Result(),             # delete old history
        _Result(),             # update canvas
        _Result(),             # delete future
        _Result(row=version_row),  # select version
    ])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.redo_canvas("proj_1", _actor())
    assert result["state"]["layers"][0]["id"] == "L2"
    assert result["version"] == 2


@pytest.mark.asyncio
async def test_undo_returns_400_when_no_history(monkeypatch):
    canvas = {"id": "dcanvas_1", "state": {"layers": []}, "version": 1}
    conn = _SeqConnection(results=[
        _Result(row=canvas),
        _Result(row=None),  # no past
    ])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.undo_canvas("proj_1", _actor())
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_redo_returns_400_when_no_future(monkeypatch):
    canvas = {"id": "dcanvas_1", "state": {"layers": []}, "version": 1}
    conn = _SeqConnection(results=[
        _Result(row=canvas),
        _Result(row=None),  # no future
    ])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.redo_canvas("proj_1", _actor())
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_history_limit_is_enforced(monkeypatch):
    # _save_canvas_history inserts then deletes excess; verify via call inspection
    canvas = {"id": "dcanvas_1", "state": {"v": 0}, "version": 1}
    conn = _SeqConnection(results=[
        _Result(row={"id": "proj_1"}),  # project check
        _Result(row=canvas),
        _Result(),  # delete future
        _Result(),  # insert history
        _Result(),  # delete old history (limit enforcement)
        _Result(row={"id": "dcanvas_1", "project_id": "proj_1", "state": {"v": 1}, "version": 2, "created_at": None, "updated_at": None}),
    ])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    await design.sync_canvas("proj_1", design.CanvasSync(state={"v": 1}), _actor())
    # The last two calls should be insert history and limit cleanup
    queries = [q for q, _ in conn.calls]
    assert any("DELETE FROM ag_design_canvas_history" in q for q in queries)
    assert any("ORDER BY created_at DESC LIMIT 50" in q for q in queries)


# ------------------------------------------------------------------------------
# Canvas helpers
# ------------------------------------------------------------------------------

def test_get_layers_returns_list_or_empty():
    assert design._get_layers({}) == []
    assert design._get_layers({"layers": [{"id": "a"}]}) == [{"id": "a"}]


def test_set_layers_makes_shallow_copy():
    s = design._set_layers({"other": 1}, [{"id": "a"}])
    assert s["layers"] == [{"id": "a"}]
    assert s["other"] == 1


def test_find_layer_index():
    layers = [{"id": "a"}, {"id": "b"}]
    assert design._find_layer_index(layers, "b") == 1
    assert design._find_layer_index(layers, "c") == -1


def test_render_export_content_is_deterministic():
    c1 = design._render_export_content("svg", {"layers": [{"id": "L1"}]}, True)
    c2 = design._render_export_content("svg", {"layers": [{"id": "L1"}]}, True)
    assert c1 == c2
    assert c1.startswith(b"<svg")

    c3 = design._render_export_content("pdf", {}, False)
    assert c3.startswith(b"%PDF")

    c4 = design._render_export_content("png", {"layers": []}, True)
    assert c4.startswith(b"\x89PNG")

    c5 = design._render_export_content("jpeg", {"layers": []}, True)
    assert c5.startswith(b"\xff\xd8\xff")
