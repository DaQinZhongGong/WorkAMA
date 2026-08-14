import asyncio
from pathlib import Path

import pytest

from workama_agent.tool_runtime import TOOL_DEFINITIONS, ToolError, ToolRuntime, parse_tool_command


def test_tool_registry_has_unique_json_schema_and_risk():
    assert {item["name"] for item in TOOL_DEFINITIONS} == {"web_search", "file.read", "file.write", "file.search", "code_interpreter", "terminal", "browser"}
    assert len({item["name"] for item in TOOL_DEFINITIONS}) == len(TOOL_DEFINITIONS)
    assert all(item["input_schema"]["type"] == "object" and item["risk"] in {"A1", "A2", "A3"} for item in TOOL_DEFINITIONS)
    terminal = next(item for item in TOOL_DEFINITIONS if item["name"] == "terminal")
    assert terminal["risk"] == "A3" and terminal["sandbox"] is True


def test_file_tools_are_confined_to_session_workspace(tmp_path: Path):
    runtime = ToolRuntime(str(tmp_path))
    written = asyncio.run(runtime.execute("file.write", {"path": "notes/a.txt", "content": "alpha\nbeta"}, "wsp_test", "ses_test"))
    assert written.status == "succeeded" and written.artifact
    read = asyncio.run(runtime.execute("file.read", {"path": "notes/a.txt"}, "wsp_test", "ses_test"))
    assert read.output == "alpha\nbeta"
    found = asyncio.run(runtime.execute("file.search", {"query": "beta"}, "wsp_test", "ses_test"))
    assert found.output[0]["line"] == 2
    with pytest.raises(ToolError, match="escapes"):
        asyncio.run(runtime.execute("file.read", {"path": "../../secret"}, "wsp_test", "ses_test"))


def test_code_interpreter_is_constrained_and_bounded(tmp_path: Path):
    runtime = ToolRuntime(str(tmp_path))
    result = asyncio.run(runtime.execute("code_interpreter", {"code": "print(sum(i*i for i in range(5)))"}, "wsp_test", "ses_test"))
    assert result.status == "succeeded" and result.output["output"].strip() == "30"
    with pytest.raises(ToolError, match="Imports"):
        asyncio.run(runtime.execute("code_interpreter", {"code": "import os\nprint(os.environ)"}, "wsp_test", "ses_test"))


def test_explicit_tool_command_parser():
    assert parse_tool_command('/tool file.write {"path":"a.txt","content":"x"}') == ("file.write", {"path": "a.txt", "content": "x"})
    assert parse_tool_command("normal message") is None


def test_web_search_marks_external_results_untrusted(tmp_path: Path, monkeypatch):
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"query": {"search": [{"title": "WorkAMA", "pageid": 42, "snippet": "<b>result</b>"}]}}
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def get(self, *_args, **_kwargs): return Response()
    monkeypatch.setattr("workama_agent.tool_runtime.httpx.AsyncClient", lambda **_kwargs: Client())
    result = asyncio.run(ToolRuntime(str(tmp_path)).execute("web_search", {"query": "WorkAMA"}, "wsp_test", "ses_test"))
    assert result.untrusted is True and result.output[0]["url"].endswith("42") and result.output[0]["snippet"] == "result"
