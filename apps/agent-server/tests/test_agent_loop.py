import json

import pytest

from workama_agent.main import parse_plan_command
from workama_agent.tool_runtime import ToolError


def test_plan_command_preserves_order_and_arguments():
    plan = parse_plan_command('/plan ' + json.dumps([
        {"tool": "file.write", "arguments": {"path": "a.txt", "content": "a"}},
        {"tool": "file.read", "arguments": {"path": "a.txt"}},
    ]))
    assert [step["tool"] for step in plan] == ["file.write", "file.read"]
    assert all(step["status"] == "pending" and step["id"].startswith("step_") for step in plan)


def test_non_plan_message_is_ignored():
    assert parse_plan_command("hello") is None


def test_plan_command_uses_explicit_ids_and_validates_dependencies():
    plan = parse_plan_command('/plan ' + json.dumps([
        {"id": "collect", "tool": "file.read", "arguments": {"path": "brief.md"}},
        {"id": "summarize", "tool": "file.write", "arguments": {"path": "summary.md"}, "depends_on": ["collect"]},
    ]))
    assert plan[1]["dependencies"] == ["collect"]

    with pytest.raises(ToolError, match="dependency not found"):
        parse_plan_command('/plan ' + json.dumps([
            {"id": "summarize", "tool": "file.write", "arguments": {}, "depends_on": ["missing"]},
        ]))


@pytest.mark.parametrize("payload", ["[]", "{}", '[{"tool": 4}]'])
def test_invalid_plan_is_rejected(payload):
    with pytest.raises(ToolError):
        parse_plan_command("/plan " + payload)
