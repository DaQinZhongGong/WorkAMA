import pytest

from workama_platform.modules.workflows import CORE_NODE_TYPES, WORKFLOW_NODE_TYPES, ensure_workflow_schema_statements, execute_graph, render_template, validate_graph


def graph():
    return {
        "nodes": [
            {"id": "input", "type": "input"},
            {"id": "prompt", "type": "prompt", "config": {"template": "Hello {input.name}"}},
            {"id": "output", "type": "output", "config": {"from": "prompt"}},
        ],
        "edges": [{"source": "input", "target": "prompt"}, {"source": "prompt", "target": "output"}],
    }


def test_workflow_graph_requires_input_output_and_rejects_cycles():
    assert validate_graph(graph()) == []
    cyclic = {"nodes": [{"id": "input", "type": "input"}, {"id": "output", "type": "output"}], "edges": [{"source": "input", "target": "output"}, {"source": "output", "target": "input"}]}
    assert "workflow graph must be acyclic" in validate_graph(cyclic)
    assert "workflow must contain an output node" in validate_graph({"nodes": [{"id": "input", "type": "input"}], "edges": []})


def test_template_rendering_is_bounded_to_context():
    assert render_template("Hi {user.name} {missing}", {"user": {"name": "Ada"}}) == "Hi Ada "


@pytest.mark.asyncio
async def test_design_node_vocabulary_is_accepted_by_the_same_executor():
    design_graph = {
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "template", "type": "template", "config": {"template": "Hello {input.name}"}},
            {"id": "answer", "type": "answer", "config": {"from": "template"}},
        ],
        "edges": [
            {"source": "start", "target": "template"},
            {"source": "template", "target": "answer"},
        ],
    }
    assert validate_graph(design_graph) == []
    status, output, trace, error = await execute_graph(design_graph, {"name": "Ada"}, None, True)
    assert (status, error) == ("succeeded", None)
    assert output["output"] == "Hello Ada"
    assert [item["type"] for item in trace] == ["input", "prompt", "output"]


@pytest.mark.asyncio
async def test_code_node_dry_run_is_safe_and_validates_source_without_execution():
    code_graph = {
        "nodes": [
            {"id": "input", "type": "input"},
            {"id": "code", "type": "code", "config": {"code": "result = {'value': input['value'] * 2}"}},
            {"id": "output", "type": "output", "config": {"from": "code"}},
        ],
        "edges": [{"source": "input", "target": "code"}, {"source": "code", "target": "output"}],
    }
    assert "code" in WORKFLOW_NODE_TYPES and len(CORE_NODE_TYPES) == 12
    assert validate_graph(code_graph) == []
    status, output, trace, error = await execute_graph(code_graph, {"value": 21}, None, True)
    assert (status, error) == ("succeeded", None)
    assert output["output"]["dry_run"] is True
    assert "code" not in output["output"]
    assert trace[-1]["status"] == "succeeded"
    invalid = {**code_graph, "nodes": [*code_graph["nodes"][:1], {"id": "code", "type": "code", "config": {"code": "import os"}}, code_graph["nodes"][-1]]}
    assert "code node imports and global scope mutation are disabled" in validate_graph(invalid)


@pytest.mark.asyncio
async def test_dry_run_executes_core_nodes_without_gateway_credentials():
    events = []
    result = await execute_graph(graph(), {"name": "Ada"}, None, True, events)
    assert result[0] == "succeeded"
    assert result[1]["output"] == "Hello Ada"
    assert [item["status"] for item in result[2]] == ["succeeded", "succeeded", "succeeded"]
    assert [item["event_type"] for item in events] == [
        "workflow.node.started", "workflow.node.succeeded",
        "workflow.node.started", "workflow.node.succeeded",
        "workflow.node.started", "workflow.node.succeeded",
    ]


def test_workflow_exposes_twelve_safe_node_types():
    assert len(CORE_NODE_TYPES) == 12
    extended = {
        "nodes": [
            {"id": "input", "type": "input"},
            {"id": "http", "type": "http_request", "config": {"url": "mock://echo", "response": {"ok": True}}},
            {"id": "loop", "type": "loop", "config": {"items_from": "input.items", "max_iterations": 2}},
            {"id": "intent", "type": "intent_classification", "config": {"text": "{input.intent}", "labels": {"billing": ["invoice"]}}},
            {"id": "aggregate", "type": "variable_aggregate", "config": {"fields": ["http", "loop", "intent"]}},
            {"id": "output", "type": "output", "config": {"from": "aggregate"}},
        ],
        "edges": [
            {"source": "input", "target": "http"}, {"source": "input", "target": "loop"},
            {"source": "input", "target": "intent"}, {"source": "http", "target": "aggregate"},
            {"source": "loop", "target": "aggregate"}, {"source": "intent", "target": "aggregate"},
            {"source": "aggregate", "target": "output"},
        ],
    }
    assert validate_graph(extended) == []


def test_assistant_runs_use_the_app_fact_source_and_redacted_metadata_contract():
    schema = "\n".join(ensure_workflow_schema_statements())
    assert "CREATE TABLE IF NOT EXISTS pf_app_run" in schema
    assert "CREATE TABLE IF NOT EXISTS pf_app_run_event" in schema
    assert "input_meta JSONB" in schema and "output_meta JSONB" in schema
    assert "message" not in schema.split("CREATE TABLE IF NOT EXISTS pf_app_run", 1)[1].split("CREATE TABLE IF NOT EXISTS pf_app_run_event", 1)[0]


@pytest.mark.asyncio
async def test_extended_nodes_are_deterministic_and_external_http_is_rejected():
    extended = {
        "nodes": [
            {"id": "input", "type": "input"},
            {"id": "http", "type": "http_request", "config": {"url": "mock://echo", "response": {"ok": True}}},
            {"id": "loop", "type": "loop", "config": {"items_from": "input.items", "max_iterations": 2}},
            {"id": "intent", "type": "intent_classification", "config": {"text": "{input.intent}", "labels": {"billing": ["invoice"]}}},
            {"id": "aggregate", "type": "variable_aggregate", "config": {"fields": ["http", "loop", "intent"]}},
            {"id": "output", "type": "output", "config": {"from": "aggregate"}},
        ],
        "edges": [
            {"source": "input", "target": "http"}, {"source": "input", "target": "loop"},
            {"source": "input", "target": "intent"}, {"source": "http", "target": "aggregate"},
            {"source": "loop", "target": "aggregate"}, {"source": "intent", "target": "aggregate"},
            {"source": "aggregate", "target": "output"},
        ],
    }
    status, output, trace, error = await execute_graph(extended, {"items": [1, 2, 3], "intent": "invoice overdue"}, None, True)
    assert status == "succeeded"
    assert error is None
    assert output["output"]["loop"]["count"] == 2
    assert output["output"]["intent"]["label"] == "billing"
    assert all(item["status"] == "succeeded" for item in trace)
    invalid = {**extended, "nodes": [*extended["nodes"][:1], {"id": "http", "type": "http_request", "config": {"url": "https://example.com"}}, extended["nodes"][-1]]}
    assert "http_request only permits mock:// endpoints" in validate_graph(invalid)


@pytest.mark.asyncio
async def test_workflow_supports_async_event_sink_and_cancellation_boundaries():
    observed = []
    checks = 0

    async def sink(event_type, payload):
        observed.append((event_type, payload))

    async def cancel_after_first_node():
        nonlocal checks
        checks += 1
        return checks > 2

    status, output, trace, error = await execute_graph(
        graph(), {"name": "Ada"}, None, True, sink, cancel_after_first_node,
    )
    assert status == "cancelled"
    assert output["context"]["input"]["name"] == "Ada"
    assert error == "Workflow run was cancelled."
    assert any(event[0] == "workflow.node.succeeded" for event in observed)
    assert len(trace) < 3
