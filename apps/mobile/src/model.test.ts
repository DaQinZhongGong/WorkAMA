import { describe, expect, it } from 'vitest'
import { applyStreamEvent, emptyMobileProjection, projectSnapshot } from './model'

describe('mobile conversation model', () => {
  it('projects a snapshot and keeps task progress', () => {
    const projection = projectSnapshot([
      { type: 'message.created', payload: { content: 'Hello' }, seq: 1 },
      { type: 'task.list.updated', payload: { progress: 50, tasks: [{ id: 'one', title: 'Review', status: 'completed' }] }, seq: 2 },
    ])
    expect(projection.messages[0]).toMatchObject({ role: 'user', content: 'Hello' })
    expect(projection.taskProgress).toBe(50)
  })

  it('rejects malformed stream packets before applying them', () => {
    const start = emptyMobileProjection()
    expect(applyStreamEvent(start, { type: 'message.delta', payload: { content: 'ok' }, seq: 1 }).messages[0].content).toBe('ok')
  })
})
