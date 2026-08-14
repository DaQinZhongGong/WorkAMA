import { describe, expect, it } from 'vitest'
import {
  applyStreamEvent,
  asAgentEvent,
  emptySessionProjection,
  projectSnapshot,
  useUiStore,
} from './index'

describe('useUiStore', () => {
  it('starts with sidebar expanded', () => {
    // 注意：zustand 单例在测试间共享，重置一次保证断言稳定
    useUiStore.setState({ sidebarCollapsed: false })
    expect(useUiStore.getState().sidebarCollapsed).toBe(false)
  })

  it('supports direct boolean set', () => {
    useUiStore.getState().setSidebarCollapsed(true)
    expect(useUiStore.getState().sidebarCollapsed).toBe(true)
  })

  it('supports functional updater', () => {
    useUiStore.getState().setSidebarCollapsed((current) => !current)
    expect(useUiStore.getState().sidebarCollapsed).toBe(false)
  })
})

describe('session projection helpers', () => {
  it('emptySessionProjection returns a usable baseline', () => {
    const projection = emptySessionProjection()
    expect(projection.messages).toEqual([])
    expect(projection.artifacts).toEqual([])
    expect(projection.tools).toEqual([])
    expect(projection.approvals).toEqual([])
    expect(projection.tasks).toEqual([])
    expect(projection.lastSeq).toBe(0)
    expect(projection.running).toBe(false)
  })

  it('asAgentEvent rejects malformed payloads', () => {
    expect(asAgentEvent(null)).toBeNull()
    expect(asAgentEvent(undefined)).toBeNull()
    expect(asAgentEvent('string')).toBeNull()
    expect(asAgentEvent({})).toBeNull()
    expect(asAgentEvent({ type: 123 })).toBeNull()
  })

  it('asAgentEvent accepts well-formed events and fills defaults', () => {
    const event = asAgentEvent({ type: 'message.delta', payload: { delta: 'hi' } })
    expect(event).not.toBeNull()
    expect(event?.type).toBe('message.delta')
    expect(event?.id).toBeUndefined()
    expect(event?.seq).toBeUndefined()
    expect(event?.payload).toEqual({ delta: 'hi' })
  })

  it('asAgentEvent tolerates missing payload', () => {
    const event = asAgentEvent({ type: 'session.completed' })
    expect(event).not.toBeNull()
    expect(event?.payload).toEqual({})
  })

  it('projectSnapshot + applyStreamEvent compose into a projection', () => {
    const initial = projectSnapshot([])
    expect(initial.messages).toEqual([])
    const next = applyStreamEvent(initial, {
      type: 'message.delta',
      seq: 1,
      payload: { role: 'assistant', delta: 'hello', index: 0 },
    })
    expect(next.lastSeq).toBe(1)
  })
})
