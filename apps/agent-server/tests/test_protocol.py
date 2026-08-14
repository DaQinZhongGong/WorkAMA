from workama_agent.main import new_id, parse_sse_data


def test_parse_sse_delta():
    payload = parse_sse_data(
        'data: {"choices":[{"delta":{"content":"hello"}}]}'
    )
    assert payload["choices"][0]["delta"]["content"] == "hello"


def test_parse_sse_done_is_ignored():
    assert parse_sse_data("data: [DONE]") is None


def test_event_id_shape():
    value = new_id("evt")
    assert value.startswith("evt_")
    assert len(value) == 30
