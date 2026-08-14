import { describe, expect, it } from 'vitest'
import { applyEvent, emptyProjection, projectEvents } from './index'
import type { AgentEvent } from './index'

describe('emptyProjection', () => {
  it('returns the correct initial structure', () => {
    const state = emptyProjection()
    expect(state.messages).toEqual([])
    expect(state.artifacts).toEqual([])
    expect(state.tools).toEqual([])
    expect(state.approvals).toEqual([])
    expect(state.tasks).toEqual([])
    expect(state.taskProgress).toBe(0)
    expect(state.status).toBe('idle')
    expect(state.usage).toEqual({ steps: 0, credits: 0 })
    expect(state.budgetRemaining).toEqual({ steps: 0, credits: 0 })
    expect(state.running).toBe(false)
    expect(state.error).toBeNull()
    expect(state.lastSeq).toBe(0)
  })

  it('returns a fresh, non-shared object on each call', () => {
    const a = emptyProjection()
    const b = emptyProjection()
    a.messages.push({ id: 'x', role: 'user', content: 'hi' })
    a.usage.steps = 5
    a.status = 'running'
    expect(b.messages).toHaveLength(0)
    expect(b.usage.steps).toBe(0)
    expect(b.status).toBe('idle')
  })
})

describe('event projection', () => {
  it('merges stream deltas into one assistant message', () => {
    let state = emptyProjection()
    state = applyEvent(state, { type: 'message.delta', payload: { content: 'hello ' }, seq: 1 })
    state = applyEvent(state, { type: 'message.delta', payload: { content: 'world' }, seq: 2 })
    state = applyEvent(state, { type: 'message.completed', payload: { content: 'hello world' }, seq: 3 })
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].content).toBe('hello world')
    expect(state.messages[0].streaming).toBe(false)
  })

  it('projects tool lifecycle and artifact references', () => {
    let state = emptyProjection()
    state = applyEvent(state, { type: 'tool.call', payload: { call_id: 'call_1', tool: 'file.write', risk: 'A2' }, seq: 1 })
    state = applyEvent(state, { type: 'artifact.created', payload: { artifact_id: 'art_1', name: 'a.txt', content_type: 'text/plain' }, seq: 2 })
    state = applyEvent(state, { type: 'tool.result', payload: { call_id: 'call_1', status: 'succeeded', summary: 'Wrote a.txt' }, seq: 3 })
    expect(state.tools[0]).toMatchObject({ id: 'call_1', status: 'succeeded' })
    expect(state.artifacts[0].id).toBe('art_1')
    expect(state.running).toBe(false)
  })

  it('projects approval request and decision', () => {
    let state = emptyProjection()
    state = applyEvent(state, { type: 'tool.approval_required', payload: { approval_id: 'apr_1', call_id: 'call_1', target: 'terminal', risk: 'A3' }, seq: 1 })
    expect(state.approvals[0]).toMatchObject({ id: 'apr_1', status: 'pending', target: 'terminal' })
    state = applyEvent(state, { type: 'tool.approval_decided', payload: { approval_id: 'apr_1', decision: 'approved' }, seq: 2 })
    expect(state.approvals[0].status).toBe('approved')
  })

  it('projects task progress and cooperative session controls', () => {
    let state = emptyProjection()
    state = applyEvent(state, { type: 'task.list.updated', payload: { progress: 50, tasks: [{ id: 'step_1', title: 'file.write', status: 'completed' }, { id: 'step_2', title: 'file.read', status: 'pending' }] }, seq: 1 })
    state = applyEvent(state, { type: 'session.status', payload: { to: 'paused' }, seq: 2 })
    expect(state.tasks).toHaveLength(2)
    expect(state.taskProgress).toBe(50)
    expect(state.status).toBe('paused')
    expect(state.running).toBe(false)
    state = applyEvent(state, { type: 'usage.updated', payload: { session_usage: { steps: 2, credits: 1.25 }, budget_remaining: { steps: 8, credits: 98.75 } }, seq: 3 })
    expect(state.usage).toEqual({ steps: 2, credits: 1.25 })
    state = applyEvent(state, { type: 'session.status', payload: { to: 'running' }, seq: 4 })
    expect(state.running).toBe(true)
  })
})

describe('user message events', () => {
  it('user.message creates a user message and flags the run as active', () => {
    const state = applyEvent(emptyProjection(), { type: 'user.message', payload: { content: 'hi there' }, seq: 1 })
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0]).toMatchObject({ role: 'user', content: 'hi there' })
    expect(state.running).toBe(true)
  })

  it('user.message maps attachment_ids to attachmentIds', () => {
    const state = applyEvent(emptyProjection(), { type: 'user.message', payload: { content: 'see attached', attachment_ids: ['a1', 'a2'] }, seq: 1 })
    expect(state.messages[0].attachmentIds).toEqual(['a1', 'a2'])
  })

  it('user.message generates a fallback id when event.id is absent', () => {
    const state = applyEvent(emptyProjection(), { type: 'user.message', payload: { content: 'x' }, seq: 1 })
    expect(state.messages[0].id).toBe('message-0')
  })

  it('user.message uses event.id when provided', () => {
    const state = applyEvent(emptyProjection(), { type: 'user.message', id: 'msg-42', payload: { content: 'x' }, seq: 1 })
    expect(state.messages[0].id).toBe('msg-42')
  })

  it('user.message with missing content defaults to an empty string and empty attachments', () => {
    const state = applyEvent(emptyProjection(), { type: 'user.message', seq: 1 })
    expect(state.messages[0].content).toBe('')
    expect(state.messages[0].attachmentIds).toEqual([])
  })

  it('message.created alias behaves like user.message', () => {
    const state = applyEvent(emptyProjection(), { type: 'message.created', payload: { content: 'aliased' }, seq: 1 })
    expect(state.messages[0]).toMatchObject({ role: 'user', content: 'aliased' })
    expect(state.running).toBe(true)
  })
})

describe('agent message delta events', () => {
  it('agent.message.delta creates a new streaming assistant message when none exists', () => {
    const state = applyEvent(emptyProjection(), { type: 'agent.message.delta', payload: { content: 'Hello' }, seq: 1 })
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0]).toMatchObject({ role: 'assistant', content: 'Hello', streaming: true })
  })

  it('agent.message.delta appends to an existing streaming assistant message', () => {
    let state = applyEvent(emptyProjection(), { type: 'agent.message.delta', payload: { content: 'Hello' }, seq: 1 })
    state = applyEvent(state, { type: 'agent.message.delta', payload: { content: ' world' }, seq: 2 })
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].content).toBe('Hello world')
    expect(state.messages[0].streaming).toBe(true)
  })

  it('message.delta alias behaves like agent.message.delta', () => {
    const state = applyEvent(emptyProjection(), { type: 'message.delta', payload: { content: 'via alias' }, seq: 1 })
    expect(state.messages[0]).toMatchObject({ role: 'assistant', content: 'via alias', streaming: true })
  })

  it('agent.message.delta falls back to the delta field when content is absent', () => {
    const state = applyEvent(emptyProjection(), { type: 'agent.message.delta', payload: { delta: 'chunk' }, seq: 1 })
    expect(state.messages[0].content).toBe('chunk')
  })

  it('multiple deltas concatenate in order into a single message', () => {
    let state = emptyProjection()
    for (const part of ['A', 'B', 'C', 'D']) {
      state = applyEvent(state, { type: 'agent.message.delta', payload: { content: part }, seq: 1 })
    }
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].content).toBe('ABCD')
  })
})

describe('agent message completed events', () => {
  it('agent.message.completed finalizes a streaming message and overrides its content', () => {
    let state = applyEvent(emptyProjection(), { type: 'agent.message.delta', payload: { content: 'draft' }, seq: 1 })
    state = applyEvent(state, { type: 'agent.message.completed', payload: { content: 'final answer' }, seq: 2 })
    expect(state.messages[0].content).toBe('final answer')
    expect(state.messages[0].streaming).toBe(false)
  })

  it('message.completed with no streaming message pushes a new assistant message', () => {
    const state = applyEvent(emptyProjection(), { type: 'message.completed', payload: { content: 'standalone' }, seq: 1 })
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0]).toMatchObject({ role: 'assistant', content: 'standalone' })
    expect(state.messages[0].streaming).toBeUndefined()
  })

  it('message.completed alias behaves like agent.message.completed', () => {
    let state = applyEvent(emptyProjection(), { type: 'message.delta', payload: { content: 'x' }, seq: 1 })
    state = applyEvent(state, { type: 'message.completed', payload: { content: 'y' }, seq: 2 })
    expect(state.messages[0].content).toBe('y')
    expect(state.messages[0].streaming).toBe(false)
  })
})

describe('artifact events', () => {
  it('artifact.created prepends a new artifact to the list', () => {
    let state = applyEvent(emptyProjection(), { type: 'artifact.created', payload: { artifact_id: 'art_1', name: 'a.txt', content_type: 'text/plain' }, seq: 1 })
    state = applyEvent(state, { type: 'artifact.created', payload: { artifact_id: 'art_2', name: 'b.txt', content_type: 'text/plain' }, seq: 2 })
    expect(state.artifacts).toHaveLength(2)
    expect(state.artifacts[0].id).toBe('art_2')
    expect(state.artifacts[1].id).toBe('art_1')
  })

  it('artifact.created prefers payload.id over artifact_id', () => {
    const state = applyEvent(emptyProjection(), { type: 'artifact.created', payload: { id: 'id_field', artifact_id: 'should_be_ignored', name: 'n', content_type: 't' }, seq: 1 })
    expect(state.artifacts[0].id).toBe('id_field')
  })

  it('artifact.created applies defaults for name and content_type', () => {
    const state = applyEvent(emptyProjection(), { type: 'artifact.created', payload: { artifact_id: 'art_x' }, seq: 1 })
    expect(state.artifacts[0].name).toBe('artifact')
    expect(state.artifacts[0].contentType).toBe('text/markdown')
  })
})

describe('tool events', () => {
  it('tool.call prepends a running tool activity and flags the run as active', () => {
    const state = applyEvent(emptyProjection(), { type: 'tool.call', payload: { call_id: 'call_1', tool: 'file.write', risk: 'A2' }, seq: 1 })
    expect(state.tools).toHaveLength(1)
    expect(state.tools[0]).toMatchObject({ id: 'call_1', name: 'file.write', risk: 'A2', status: 'running' })
    expect(state.running).toBe(true)
  })

  it('tool.call applies defaults for name and risk', () => {
    const state = applyEvent(emptyProjection(), { type: 'tool.call', id: 'evt_1', seq: 1 })
    expect(state.tools[0].name).toBe('tool')
    expect(state.tools[0].risk).toBe('A1')
    expect(state.tools[0].id).toBe('evt_1')
  })

  it('tool.result updates the matching tool and stops the run', () => {
    let state = applyEvent(emptyProjection(), { type: 'tool.call', payload: { call_id: 'call_1', tool: 't', risk: 'A1' }, seq: 1 })
    state = applyEvent(state, { type: 'tool.result', payload: { call_id: 'call_1', status: 'succeeded', summary: 'done', output: { ok: true } }, seq: 2 })
    expect(state.tools[0]).toMatchObject({ id: 'call_1', status: 'succeeded', summary: 'done' })
    expect(state.tools[0].output).toEqual({ ok: true })
    expect(state.running).toBe(false)
  })

  it('tool.result with no matching tool still stops the run', () => {
    const state = applyEvent(emptyProjection(), { type: 'tool.result', payload: { call_id: 'missing', status: 'succeeded' }, seq: 1 })
    expect(state.tools).toHaveLength(0)
    expect(state.running).toBe(false)
  })
})

describe('approval events', () => {
  it('tool.approval_required prepends a pending approval with preview and expiry', () => {
    const state = applyEvent(emptyProjection(), { type: 'tool.approval_required', payload: { approval_id: 'apr_1', call_id: 'call_1', target: 'terminal', risk: 'A3', preview: { cmd: 'rm -rf' }, expiry: '2025-12-31' }, seq: 1 })
    expect(state.approvals[0]).toMatchObject({ id: 'apr_1', callId: 'call_1', target: 'terminal', risk: 'A3', status: 'pending', expiry: '2025-12-31' })
    expect(state.approvals[0].preview).toEqual({ cmd: 'rm -rf' })
  })

  it('tool.approval_decided updates the matching approval status', () => {
    let state = applyEvent(emptyProjection(), { type: 'tool.approval_required', payload: { approval_id: 'apr_1', call_id: 'call_1', target: 't', risk: 'A3' }, seq: 1 })
    state = applyEvent(state, { type: 'tool.approval_decided', payload: { approval_id: 'apr_1', decision: 'denied' }, seq: 2 })
    expect(state.approvals[0].status).toBe('denied')
  })

  it('tool.approval_decided with no matching approval leaves approvals unchanged', () => {
    let state = applyEvent(emptyProjection(), { type: 'tool.approval_required', payload: { approval_id: 'apr_1', call_id: 'call_1', target: 't', risk: 'A3' }, seq: 1 })
    state = applyEvent(state, { type: 'tool.approval_decided', payload: { approval_id: 'apr_missing', decision: 'approved' }, seq: 2 })
    expect(state.approvals).toHaveLength(1)
    expect(state.approvals[0].status).toBe('pending')
  })
})

describe('task events', () => {
  it('task.list.updated replaces the tasks array and sets progress', () => {
    const state = applyEvent(emptyProjection(), { type: 'task.list.updated', payload: { progress: 75, tasks: [{ id: 't1', title: 'A', status: 'completed' }, { id: 't2', title: 'B', status: 'pending' }] }, seq: 1 })
    expect(state.tasks).toEqual([{ id: 't1', title: 'A', status: 'completed' }, { id: 't2', title: 'B', status: 'pending' }])
    expect(state.taskProgress).toBe(75)
  })

  it('task.list.updated with a non-array tasks payload yields an empty tasks array', () => {
    const state = applyEvent(emptyProjection(), { type: 'task.list.updated', payload: { progress: 10, tasks: null }, seq: 1 })
    expect(state.tasks).toEqual([])
    expect(state.taskProgress).toBe(10)
  })

  it('task.list.updated defaults missing task fields', () => {
    const state = applyEvent(emptyProjection(), { type: 'task.list.updated', payload: { tasks: [{ id: 't1' }] }, seq: 1 })
    expect(state.tasks[0]).toEqual({ id: 't1', title: '', status: 'pending' })
    expect(state.taskProgress).toBe(0)
  })
})

describe('usage events', () => {
  it('usage.updated sets usage and budgetRemaining', () => {
    const state = applyEvent(emptyProjection(), { type: 'usage.updated', payload: { session_usage: { steps: 3, credits: 2.5 }, budget_remaining: { steps: 7, credits: 97.5 } }, seq: 1 })
    expect(state.usage).toEqual({ steps: 3, credits: 2.5 })
    expect(state.budgetRemaining).toEqual({ steps: 7, credits: 97.5 })
  })

  it('usage.updated with missing fields defaults to zero', () => {
    const state = applyEvent(emptyProjection(), { type: 'usage.updated', payload: {}, seq: 1 })
    expect(state.usage).toEqual({ steps: 0, credits: 0 })
    expect(state.budgetRemaining).toEqual({ steps: 0, credits: 0 })
  })

  it('usage.updated reflects the latest reported accumulated values', () => {
    let state = emptyProjection()
    state = applyEvent(state, { type: 'usage.updated', payload: { session_usage: { steps: 1, credits: 0.5 }, budget_remaining: { steps: 9, credits: 99.5 } }, seq: 1 })
    state = applyEvent(state, { type: 'usage.updated', payload: { session_usage: { steps: 5, credits: 2.0 }, budget_remaining: { steps: 5, credits: 98.0 } }, seq: 2 })
    expect(state.usage).toEqual({ steps: 5, credits: 2.0 })
    expect(state.budgetRemaining).toEqual({ steps: 5, credits: 98.0 })
  })
})

describe('session status events', () => {
  it('session.status sets the status and reflects the running flag', () => {
    const state = applyEvent(emptyProjection(), { type: 'session.status', payload: { to: 'paused' }, seq: 1 })
    expect(state.status).toBe('paused')
    expect(state.running).toBe(false)
  })

  it('session.status with to=running sets running to true', () => {
    const state = applyEvent(emptyProjection(), { type: 'session.status', payload: { to: 'running' }, seq: 1 })
    expect(state.status).toBe('running')
    expect(state.running).toBe(true)
  })

  it('session.completed alias defaults to the completed status', () => {
    const state = applyEvent(emptyProjection(), { type: 'session.completed', seq: 1 })
    expect(state.status).toBe('completed')
    expect(state.running).toBe(false)
  })
})

describe('error events', () => {
  it('error sets the error message and stops the run', () => {
    const state = applyEvent(emptyProjection(), { type: 'error', payload: { message: 'something broke' }, seq: 1 })
    expect(state.error).toBe('something broke')
    expect(state.running).toBe(false)
  })

  it('run.failed alias behaves like error', () => {
    const state = applyEvent(emptyProjection(), { type: 'run.failed', payload: { message: 'boom' }, seq: 1 })
    expect(state.error).toBe('boom')
    expect(state.running).toBe(false)
  })

  it('error with no message uses the default text', () => {
    const state = applyEvent(emptyProjection(), { type: 'error', seq: 1 })
    expect(state.error).toBe('Agent run failed')
    expect(state.running).toBe(false)
  })
})

describe('projectEvents', () => {
  it('returns an empty projection for an empty event list', () => {
    expect(projectEvents([])).toEqual(emptyProjection())
  })

  it('applies a full event sequence end-to-end into a final projection', () => {
    const events: AgentEvent[] = [
      { type: 'user.message', payload: { content: 'please build it' }, seq: 1 },
      { type: 'tool.call', payload: { call_id: 'c1', tool: 'file.write', risk: 'A2' }, seq: 2 },
      { type: 'agent.message.delta', payload: { content: 'Writing ' }, seq: 3 },
      { type: 'agent.message.delta', payload: { content: 'file' }, seq: 4 },
      { type: 'artifact.created', payload: { artifact_id: 'art_1', name: 'out.txt', content_type: 'text/plain' }, seq: 5 },
      { type: 'tool.result', payload: { call_id: 'c1', status: 'succeeded', summary: 'wrote' }, seq: 6 },
      { type: 'agent.message.completed', payload: { content: 'Writing file' }, seq: 7 },
      { type: 'usage.updated', payload: { session_usage: { steps: 2, credits: 1.0 }, budget_remaining: { steps: 8, credits: 99.0 } }, seq: 8 },
      { type: 'session.status', payload: { to: 'completed' }, seq: 9 },
    ]
    const state = projectEvents(events)
    expect(state.messages).toHaveLength(2)
    expect(state.messages[0]).toMatchObject({ role: 'user', content: 'please build it' })
    expect(state.messages[1]).toMatchObject({ role: 'assistant', content: 'Writing file', streaming: false })
    expect(state.artifacts[0].id).toBe('art_1')
    expect(state.tools[0]).toMatchObject({ id: 'c1', status: 'succeeded' })
    expect(state.usage).toEqual({ steps: 2, credits: 1.0 })
    expect(state.status).toBe('completed')
    expect(state.running).toBe(false)
    expect(state.lastSeq).toBe(9)
  })

  it('accumulates lastSeq and reflects the latest state across a batch', () => {
    const events: AgentEvent[] = [
      { type: 'usage.updated', payload: { session_usage: { steps: 1, credits: 0.5 }, budget_remaining: { steps: 9, credits: 99.5 } }, seq: 3 },
      { type: 'usage.updated', payload: { session_usage: { steps: 4, credits: 1.5 }, budget_remaining: { steps: 6, credits: 98.5 } }, seq: 7 },
      { type: 'task.list.updated', payload: { progress: 100, tasks: [{ id: 't1', title: 'A', status: 'completed' }] }, seq: 10 },
    ]
    const state = projectEvents(events)
    expect(state.lastSeq).toBe(10)
    expect(state.usage.steps).toBe(4)
    expect(state.taskProgress).toBe(100)
  })
})

describe('immutability & idempotency', () => {
  it('does not mutate the input state for the tool lifecycle', () => {
    const state0 = emptyProjection()
    const state1 = applyEvent(state0, { type: 'tool.call', payload: { call_id: 'c1', tool: 't', risk: 'A2' }, seq: 1 })
    expect(state0.tools).toHaveLength(0)
    const state2 = applyEvent(state1, { type: 'tool.result', payload: { call_id: 'c1', status: 'succeeded' }, seq: 2 })
    // Previous state's tool status must remain unchanged.
    expect(state1.tools[0].status).toBe('running')
    expect(state2.tools[0].status).toBe('succeeded')
  })

  it('repeating an unknown event keeps the projection stable (idempotent)', () => {
    const event: AgentEvent = { type: 'connection.ready', seq: 5, payload: { hello: 'world' } }
    const once = applyEvent(emptyProjection(), event)
    const twice = applyEvent(once, event)
    expect(twice).toEqual(once)
    expect(twice.lastSeq).toBe(5)
  })

  it('repeating a handled event does not break the projection', () => {
    const event: AgentEvent = { type: 'user.message', payload: { content: 'hi' }, seq: 1 }
    let state = applyEvent(emptyProjection(), event)
    state = applyEvent(state, event)
    expect(state.messages).toHaveLength(2)
    expect(state.running).toBe(true)
    expect(state.lastSeq).toBe(1)
  })
})

describe('unknown & unhandled event types', () => {
  it('an unknown event type does not throw and only advances lastSeq', () => {
    const state = applyEvent(emptyProjection(), { type: 'totally.unknown', seq: 7, payload: { foo: 'bar' } })
    expect(state.lastSeq).toBe(7)
    expect(state.messages).toHaveLength(0)
    expect(state.status).toBe('idle')
    expect(state.running).toBe(false)
  })

  // These event type names appear in the platform spec but are not handled by
  // applyEvent's if/else chain. They must be treated as no-ops (only lastSeq
  // advances) rather than throwing.
  const unhandledSpecTypes = [
    'connection.ready',
    'connection.error',
    'agent.message.started',
    'agent.thought',
    'agent.tool_call',
    'agent.tool_result',
    'agent.sandbox',
    'task.started',
    'task.completed',
    'task.failed',
    'artifact.updated',
    'step.started',
    'step.completed',
    'approval.requested',
    'approval.resolved',
    'session.paused',
    'session.resumed',
    'session.cancelled',
  ]
  it.each(unhandledSpecTypes)('does not throw for unhandled spec event type "%s" and leaves projection unchanged', (type) => {
    const before = emptyProjection()
    const after = applyEvent(before, { type, seq: 4, payload: { any: 'thing' } })
    expect(after.lastSeq).toBe(4)
    expect(after.messages).toEqual([])
    expect(after.artifacts).toEqual([])
    expect(after.tools).toEqual([])
    expect(after.approvals).toEqual([])
    expect(after.tasks).toEqual([])
    expect(after.status).toBe('idle')
    expect(after.running).toBe(false)
    expect(after.error).toBeNull()
  })
})

describe('edge cases', () => {
  it('tolerates an undefined payload (treats it as empty object)', () => {
    const state = applyEvent(emptyProjection(), { type: 'user.message', seq: 1 })
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].content).toBe('')
    expect(state.messages[0].attachmentIds).toEqual([])
  })

  it('tolerates a null-like payload via missing field', () => {
    // payload is explicitly provided but empty; should not throw for any branch.
    const state = applyEvent(emptyProjection(), { type: 'artifact.created', payload: {}, seq: 1 })
    expect(state.artifacts[0]).toMatchObject({ name: 'artifact', contentType: 'text/markdown' })
  })

  it('advances lastSeq using the maximum of current and event seq', () => {
    let state = applyEvent(emptyProjection(), { type: 'unknown', seq: 10 })
    state = applyEvent(state, { type: 'unknown', seq: 5 })
    expect(state.lastSeq).toBe(10)
    state = applyEvent(state, { type: 'unknown', seq: 20 })
    expect(state.lastSeq).toBe(20)
  })

  it('keeps the previous lastSeq when an event has no seq', () => {
    let state = applyEvent(emptyProjection(), { type: 'unknown', seq: 5 })
    state = applyEvent(state, { type: 'unknown' })
    expect(state.lastSeq).toBe(5)
  })

  it('deltas concatenate correctly when interleaved with other event types', () => {
    const events: AgentEvent[] = [
      { type: 'agent.message.delta', payload: { content: 'Part1-' }, seq: 1 },
      { type: 'usage.updated', payload: { session_usage: { steps: 1, credits: 0.1 }, budget_remaining: { steps: 9, credits: 99.9 } }, seq: 2 },
      { type: 'agent.message.delta', payload: { content: 'Part2-' }, seq: 3 },
      { type: 'artifact.created', payload: { artifact_id: 'a1', name: 'n', content_type: 't' }, seq: 4 },
      { type: 'agent.message.delta', payload: { content: 'Part3' }, seq: 5 },
      { type: 'agent.message.completed', payload: { content: 'Part1-Part2-Part3' }, seq: 6 },
    ]
    const state = projectEvents(events)
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].content).toBe('Part1-Part2-Part3')
    expect(state.messages[0].streaming).toBe(false)
    expect(state.artifacts).toHaveLength(1)
    expect(state.usage.steps).toBe(1)
  })
})
