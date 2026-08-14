import asyncio

from workama_agent.main import AGENT_EVENT_TYPES, DeliveryState, deliveries, send_event


EXPECTED = {
    "connection.ready", "session.snapshot", "session.created", "user.message", "agent.thought",
    "agent.message.delta", "agent.message.completed", "task.list.updated", "tool.call",
    "tool.approval_required", "tool.approval_decided", "tool.result", "terminal.output",
    "browser.frame", "code.diff", "test.report", "citation.created", "artifact.created",
    "sandbox.status", "usage.updated", "step.finished", "session.status", "connection.warning", "error",
}


class FakeSocket:
    def __init__(self):
        self.messages = []
        self.close_code = None

    async def send_json(self, value):
        self.messages.append(value)

    async def close(self, code, reason):
        self.close_code = code


def test_frozen_agent_event_registry_has_exactly_24_types():
    assert AGENT_EVENT_TYPES == EXPECTED
    assert len(AGENT_EVENT_TYPES) == 24


def test_unacknowledged_buffer_warns_and_closes_at_limit():
    async def scenario():
        socket = FakeSocket(); state = DeliveryState(); deliveries[id(socket)] = state
        try:
            for seq in range(1, 1002):
                await send_event(socket, {"seq": seq, "type": "agent.message.delta", "payload": {"delta": "x"}})
            assert socket.close_code == 4410
            assert socket.messages[-1]["type"] == "connection.warning"
            assert socket.messages[-1]["payload"]["pending_events"] == 1001
        finally:
            deliveries.pop(id(socket), None)
    asyncio.run(scenario())


def test_acknowledged_long_stream_stays_open():
    async def scenario():
        socket = FakeSocket(); state = DeliveryState(); deliveries[id(socket)] = state
        try:
            for seq in range(1, 1201):
                await send_event(socket, {"seq": seq, "type": "agent.message.delta", "payload": {"delta": "x"}})
                state.acknowledge(seq)
            assert socket.close_code is None
            assert not state.pending and state.pending_bytes == 0
        finally:
            deliveries.pop(id(socket), None)
    asyncio.run(scenario())
