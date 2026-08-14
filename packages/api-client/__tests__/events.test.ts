import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  createEventManager,
  filterToRegex,
  matchFilter,
  sseToEnvelope,
  wsToEnvelope,
} from '../src/events'
import type { EventEnvelope, EventHandler } from '../src/events-types'

// ============ Mock 工具 ============

class MockWebSocket {
  static instances: MockWebSocket[] = []
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3

  url: string
  protocols?: string | string[]
  readyState = MockWebSocket.CONNECTING

  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null

  closed = false
  closeCode: number | undefined
  closeReason: string | undefined
  sent: string[] = []

  constructor(url: string, protocols?: string | string[]) {
    this.url = url
    this.protocols = protocols
    MockWebSocket.instances.push(this)
  }

  send(data: string): void {
    this.sent.push(data)
  }

  close(code?: number, reason?: string): void {
    this.closed = true
    this.closeCode = code
    this.closeReason = reason
    this.readyState = MockWebSocket.CLOSED
  }

  emitOpen(): void {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.({ type: 'open' } as Event)
  }

  emitMessage(data: unknown): void {
    this.onmessage?.({ data } as MessageEvent)
  }

  emitError(): void {
    this.onerror?.({ type: 'error' } as Event)
  }

  emitClose(code = 1000, reason = ''): void {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.({ type: 'close', code, reason, wasClean: true } as CloseEvent)
  }
}

function mockSSEResponse(
  chunks: string[],
  status = 200,
  headers: Record<string, string> = {},
) {
  const encoder = new TextEncoder()
  let i = 0
  const reader = {
    read: async () => {
      if (i < chunks.length) {
        return { done: false, value: encoder.encode(chunks[i++]) }
      }
      return { done: true, value: undefined }
    },
    cancel: async () => {},
  }
  const h = new Headers(headers)
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    body: { getReader: () => reader },
    headers: h,
  } as unknown as Response
}

/** 等待所有 microtask 与 setTimeout(0) 完成 */
function flushMicrotasks(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

// ============ 工具函数测试 ============

describe('工具函数', () => {
  describe('filterToRegex / matchFilter', () => {
    it('精确匹配', () => {
      const regex = filterToRegex('notification.created')
      expect(matchFilter(regex, 'notification.created')).toBe(true)
      expect(matchFilter(regex, 'notification.updated')).toBe(false)
      expect(matchFilter(regex, 'workspace.created')).toBe(false)
    })

    it('单层通配 notification.* 匹配单层与多层后缀', () => {
      const regex = filterToRegex('notification.*')
      expect(matchFilter(regex, 'notification.created')).toBe(true)
      expect(matchFilter(regex, 'notification.user.added')).toBe(true)
      expect(matchFilter(regex, 'workspace.created')).toBe(false)
    })

    it('通配 * 匹配所有事件', () => {
      const regex = filterToRegex('*')
      expect(matchFilter(regex, 'anything')).toBe(true)
      expect(matchFilter(regex, 'notification.created')).toBe(true)
      expect(matchFilter(regex, 'workspace.user.added.v2')).toBe(true)
    })

    it('billing.* 匹配 billing 命名空间', () => {
      const regex = filterToRegex('billing.*')
      expect(matchFilter(regex, 'billing.invoice.paid')).toBe(true)
      expect(matchFilter(regex, 'billing.subscription.cancelled')).toBe(true)
      expect(matchFilter(regex, 'workspace.created')).toBe(false)
    })

    it('转义正则元字符', () => {
      const regex = filterToRegex('a.b+c')
      expect(matchFilter(regex, 'a.b+c')).toBe(true)
      expect(matchFilter(regex, 'axb+c')).toBe(false)
    })
  })

  describe('sseToEnvelope', () => {
    it('解析 event 与 id 字段', () => {
      const env = sseToEnvelope({ data: 'hello', event: 'notification.created', id: '42' })
      expect(env.type).toBe('notification.created')
      expect(env.id).toBe('42')
      expect(env.data).toBe('hello')
      expect(env.transport).toBe('sse')
    })

    it('缺省 event 字段为 message', () => {
      const env = sseToEnvelope({ data: 'payload' })
      expect(env.type).toBe('message')
      expect(env.data).toBe('payload')
    })

    it('JSON data 自动解析', () => {
      const env = sseToEnvelope({ data: '{"key":"value"}' })
      expect(env.data).toEqual({ key: 'value' })
    })

    it('非法 JSON 保留原始字符串', () => {
      const env = sseToEnvelope({ data: '{not json' })
      expect(env.data).toBe('{not json')
    })
  })

  describe('wsToEnvelope', () => {
    it('映射 type 与 seq', () => {
      const env = wsToEnvelope({
        type: 'workspace.created',
        seq: 100,
        payload: { id: 'ws-1' },
        occurred_at: '2026-07-25T00:00:00Z',
        trace_id: 'trace-1',
        producer: 'workspace-service',
      })
      expect(env.type).toBe('workspace.created')
      expect(env.id).toBe(100)
      expect(env.payload).toEqual({ id: 'ws-1' })
      expect(env.data).toEqual({ id: 'ws-1' })
      expect(env.occurredAt).toBe('2026-07-25T00:00:00Z')
      expect(env.traceId).toBe('trace-1')
      expect(env.producer).toBe('workspace-service')
      expect(env.transport).toBe('ws')
    })

    it('缺省 seq 时回退到 event_id', () => {
      const env = wsToEnvelope({ type: 'x', event_id: 'evt-abc' })
      expect(env.id).toBe('evt-abc')
    })
  })
})

// ============ 事件过滤测试 ============

describe('事件过滤', () => {
  beforeEach(() => {
    MockWebSocket.instances = []
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('精确匹配：只收到匹配类型的事件', async () => {
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      transport: 'ws',
    })
    const handler = vi.fn()
    manager.subscribe('notification.created', handler as EventHandler)

    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    ws.emitMessage(JSON.stringify({ type: 'notification.created', seq: 1 }))
    ws.emitMessage(JSON.stringify({ type: 'notification.updated', seq: 2 }))
    ws.emitMessage(JSON.stringify({ type: 'workspace.created', seq: 3 }))

    await flushMicrotasks()
    manager.close()

    expect(handler).toHaveBeenCalledTimes(1)
    expect((handler.mock.calls[0][0] as EventEnvelope).id).toBe(1)
  })

  it('通配 notification.* 匹配命名空间下所有事件', async () => {
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      transport: 'ws',
    })
    const handler = vi.fn()
    manager.subscribe('notification.*', handler as EventHandler)

    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    ws.emitMessage(JSON.stringify({ type: 'notification.created', seq: 1 }))
    ws.emitMessage(JSON.stringify({ type: 'notification.user.added', seq: 2 }))
    ws.emitMessage(JSON.stringify({ type: 'workspace.created', seq: 3 }))
    ws.emitMessage(JSON.stringify({ type: 'billing.invoice.paid', seq: 4 }))

    await flushMicrotasks()
    manager.close()

    expect(handler).toHaveBeenCalledTimes(2)
    expect((handler.mock.calls[0][0] as EventEnvelope).id).toBe(1)
    expect((handler.mock.calls[1][0] as EventEnvelope).id).toBe(2)
  })

  it('通配 * 匹配所有事件', async () => {
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      transport: 'ws',
    })
    const handler = vi.fn()
    manager.subscribe('*', handler as EventHandler)

    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    ws.emitMessage(JSON.stringify({ type: 'a', seq: 1 }))
    ws.emitMessage(JSON.stringify({ type: 'b.c', seq: 2 }))
    ws.emitMessage(JSON.stringify({ type: 'd.e.f', seq: 3 }))

    await flushMicrotasks()
    manager.close()

    expect(handler).toHaveBeenCalledTimes(3)
  })

  it('不同 filter 互不干扰', async () => {
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      transport: 'ws',
    })
    const notifyHandler = vi.fn()
    const workspaceHandler = vi.fn()
    const billingHandler = vi.fn()
    manager.subscribe('notification.*', notifyHandler as EventHandler)
    manager.subscribe('workspace.*', workspaceHandler as EventHandler)
    manager.subscribe('billing.*', billingHandler as EventHandler)

    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    ws.emitMessage(JSON.stringify({ type: 'notification.created', seq: 1 }))
    ws.emitMessage(JSON.stringify({ type: 'workspace.updated', seq: 2 }))
    ws.emitMessage(JSON.stringify({ type: 'billing.invoice.paid', seq: 3 }))

    await flushMicrotasks()
    manager.close()

    expect(notifyHandler).toHaveBeenCalledTimes(1)
    expect(workspaceHandler).toHaveBeenCalledTimes(1)
    expect(billingHandler).toHaveBeenCalledTimes(1)
  })

  it('非 WS 信封消息不被分发', async () => {
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      transport: 'ws',
    })
    const handler = vi.fn()
    manager.subscribe('*', handler as EventHandler)

    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    // 缺少 type 字段，不是合法 WS 信封
    ws.emitMessage(JSON.stringify({ seq: 1, payload: 'no-type' }))
    ws.emitMessage('plain-string')
    ws.emitMessage(JSON.stringify({ type: 'valid', seq: 2 }))

    await flushMicrotasks()
    manager.close()

    expect(handler).toHaveBeenCalledTimes(1)
    expect((handler.mock.calls[0][0] as EventEnvelope).type).toBe('valid')
  })
})

// ============ 多订阅者测试 ============

describe('多订阅者', () => {
  beforeEach(() => {
    MockWebSocket.instances = []
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('同一 filter 的多个订阅者都收到事件', async () => {
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      transport: 'ws',
    })
    const handler1 = vi.fn()
    const handler2 = vi.fn()
    manager.subscribe('notification.*', handler1 as EventHandler)
    manager.subscribe('notification.*', handler2 as EventHandler)

    expect(manager.getSubscriberCount()).toBe(2)

    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    ws.emitMessage(JSON.stringify({ type: 'notification.created', seq: 1 }))

    await flushMicrotasks()
    manager.close()

    expect(handler1).toHaveBeenCalledTimes(1)
    expect(handler2).toHaveBeenCalledTimes(1)
    expect((handler1.mock.calls[0][0] as EventEnvelope).id).toBe(1)
    expect((handler2.mock.calls[0][0] as EventEnvelope).id).toBe(1)
  })

  it('不同 filter 订阅者独立接收', async () => {
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      transport: 'ws',
    })
    const a = vi.fn()
    const b = vi.fn()
    manager.subscribe('notification.*', a as EventHandler)
    manager.subscribe('workspace.*', b as EventHandler)

    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    ws.emitMessage(JSON.stringify({ type: 'notification.created', seq: 1 }))
    ws.emitMessage(JSON.stringify({ type: 'workspace.updated', seq: 2 }))

    await flushMicrotasks()
    manager.close()

    expect(a).toHaveBeenCalledTimes(1)
    expect(b).toHaveBeenCalledTimes(1)
  })

  it('取消订阅后不再收到事件', async () => {
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      transport: 'ws',
    })
    const handler = vi.fn()
    const handle = manager.subscribe('notification.*', handler as EventHandler)

    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    ws.emitMessage(JSON.stringify({ type: 'notification.created', seq: 1 }))
    await flushMicrotasks()

    expect(handler).toHaveBeenCalledTimes(1)

    handle.unsubscribe()
    expect(manager.getSubscriberCount()).toBe(0)

    ws.emitMessage(JSON.stringify({ type: 'notification.updated', seq: 2 }))
    await flushMicrotasks()

    // 仍然只有 1 次
    expect(handler).toHaveBeenCalledTimes(1)
    manager.close()
  })

  it('所有订阅者取消后传输关闭', async () => {
    const onStateChange = vi.fn()
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      transport: 'ws',
      onStateChange,
    })
    const handle = manager.subscribe('*', vi.fn() as EventHandler)

    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    // 状态：disconnected -> connecting -> connected
    handle.unsubscribe()
    // 没有订阅者，应触发 disconnected

    await flushMicrotasks()
    expect(manager.getState()).toBe('disconnected')
    expect(manager.getActiveTransport()).toBe(null)
    manager.close()
  })
})

// ============ 自动重连测试 ============

describe('自动重连与状态变化', () => {
  beforeEach(() => {
    MockWebSocket.instances = []
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('WS 断线后状态变为 reconnecting，重连成功后变 connected', async () => {
    vi.useFakeTimers()
    const onStateChange = vi.fn()
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      transport: 'ws',
      onStateChange,
      wsMinReconnectDelay: 100,
      wsMaxReconnectDelay: 1000,
    })
    manager.subscribe('*', vi.fn() as EventHandler)

    const first = MockWebSocket.instances[0]
    first.emitOpen()
    expect(manager.getState()).toBe('connected')

    // 断线
    first.emitClose(1001, 'going away')
    expect(manager.getState()).toBe('reconnecting')

    // 推进时间触发重连
    await vi.advanceTimersByTimeAsync(200)
    expect(MockWebSocket.instances).toHaveLength(2)
    const second = MockWebSocket.instances[1]
    second.emitOpen()
    expect(manager.getState()).toBe('connected')

    vi.useRealTimers()
    manager.close()
  })

  it('SSE 网络错误后自动重连并恢复事件', async () => {
    const fetchMock = vi.fn()
    fetchMock
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce(mockSSEResponse(['data: hello\n\n']))
    vi.stubGlobal('fetch', fetchMock)

    const onStateChange = vi.fn()
    const onEvent = vi.fn()
    const manager = createEventManager({
      sseUrl: 'https://api.example.com/sse',
      transport: 'sse',
      onStateChange,
      sseReconnectDelayMs: 0,
      sseMaxReconnectDelayMs: 0,
    })
    manager.subscribe('*', (e) => onEvent(e))

    await vi.waitFor(() => {
      expect(onEvent).toHaveBeenCalledTimes(1)
    })

    expect((onEvent.mock.calls[0][0] as EventEnvelope).data).toBe('hello')
    // 状态序列：disconnected -> connecting -> reconnecting -> ...
    expect(onStateChange).toHaveBeenCalledWith('connecting', 'disconnected')
    expect(onStateChange).toHaveBeenCalledWith('reconnecting', 'connecting')
    expect(manager.getActiveTransport()).toBe('sse')

    vi.unstubAllGlobals()
    manager.close()
  })

  it('Last-Event-ID 透传到首次 SSE 连接', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(mockSSEResponse(['data: ok\n\n']))
    vi.stubGlobal('fetch', fetchMock)

    const manager = createEventManager({
      sseUrl: 'https://api.example.com/sse',
      transport: 'sse',
      lastEventId: 'seed-99',
    })
    manager.subscribe('*', vi.fn() as EventHandler)

    await vi.waitFor(() => {
      expect(fetchMock).toHaveBeenCalled()
    })

    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect((init.headers as Headers).get('Last-Event-ID')).toBe('seed-99')

    vi.unstubAllGlobals()
    manager.close()
  })
})

// ============ 传输降级测试 ============

describe('传输降级（auto 模式）', () => {
  beforeEach(() => {
    MockWebSocket.instances = []
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('auto 模式下 WS 失败降级到 SSE', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(mockSSEResponse(['data: ok\n\n']))
    vi.stubGlobal('fetch', fetchMock)

    const onStateChange = vi.fn()
    const onEvent = vi.fn()
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      sseUrl: 'https://api.example.com/sse',
      transport: 'auto',
      wsFallbackThreshold: 1,
      onStateChange,
      sseReconnectDelayMs: 0,
    })
    manager.subscribe('*', (e) => onEvent(e))

    // WS 失败（从未成功连接）
    const ws = MockWebSocket.instances[0]
    ws.emitError()

    await vi.waitFor(() => {
      expect(onEvent).toHaveBeenCalledTimes(1)
    })

    expect(manager.getActiveTransport()).toBe('sse')
    expect((onEvent.mock.calls[0][0] as EventEnvelope).data).toBe('ok')

    vi.unstubAllGlobals()
    manager.close()
  })

  it('ws 模式直接用 WS 不降级', () => {
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      sseUrl: 'https://api.example.com/sse',
      transport: 'ws',
    })
    manager.subscribe('*', vi.fn() as EventHandler)

    expect(MockWebSocket.instances).toHaveLength(1)
    expect(manager.getActiveTransport()).toBe('ws')

    manager.close()
  })

  it('sse 模式直接用 SSE 不尝试 WS', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(mockSSEResponse(['data: ok\n\n']))
    vi.stubGlobal('fetch', fetchMock)

    const manager = createEventManager({
      sseUrl: 'https://api.example.com/sse',
      transport: 'sse',
    })
    manager.subscribe('*', vi.fn() as EventHandler)

    await vi.waitFor(() => {
      expect(fetchMock).toHaveBeenCalled()
    })

    expect(MockWebSocket.instances).toHaveLength(0)
    expect(manager.getActiveTransport()).toBe('sse')

    vi.unstubAllGlobals()
    manager.close()
  })

  it('auto 模式 WS 成功连接不降级', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      sseUrl: 'https://api.example.com/sse',
      transport: 'auto',
      wsFallbackThreshold: 1,
    })
    manager.subscribe('*', vi.fn() as EventHandler)

    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    expect(manager.getActiveTransport()).toBe('ws')

    // 后续 WS 错误不应触发降级（已成功连接过）
    ws.emitError()
    expect(manager.getActiveTransport()).toBe('ws')
    expect(fetchMock).not.toHaveBeenCalled()

    vi.unstubAllGlobals()
    manager.close()
  })
})

// ============ 背压控制测试 ============

describe('背压控制', () => {
  beforeEach(() => {
    MockWebSocket.instances = []
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('buffer 策略：同步分发不丢弃', async () => {
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      transport: 'ws',
      backpressure: { strategy: 'buffer' },
    })
    const received: number[] = []
    manager.subscribe('*', (e) => {
      received.push(e.id as number)
    })

    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    for (let i = 1; i <= 10; i++) {
      ws.emitMessage(JSON.stringify({ type: 'event', seq: i }))
    }

    await flushMicrotasks()
    manager.close()

    // buffer 策略同步分发，全部收到
    expect(received).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
  })

  it('drop-oldest 策略：超过 maxBufferSize 时丢弃最旧事件', async () => {
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      transport: 'ws',
      backpressure: {
        strategy: 'drop-oldest',
        maxBufferSize: 2,
        warningThreshold: 100,
      },
    })
    const received: number[] = []
    manager.subscribe('*', (e) => {
      received.push(e.id as number)
    })

    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    // 同步连续发送 5 个事件，maxBufferSize=2
    for (let i = 1; i <= 5; i++) {
      ws.emitMessage(JSON.stringify({ type: 'event', seq: i }))
    }

    // 等待 microtask 异步消费完成
    await flushMicrotasks()
    manager.close()

    // drop-oldest：保留最新 2 个
    expect(received).toEqual([4, 5])
  })

  it('drop-newest 策略：超过 maxBufferSize 时丢弃最新事件', async () => {
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      transport: 'ws',
      backpressure: {
        strategy: 'drop-newest',
        maxBufferSize: 2,
        warningThreshold: 100,
      },
    })
    const received: number[] = []
    manager.subscribe('*', (e) => {
      received.push(e.id as number)
    })

    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    for (let i = 1; i <= 5; i++) {
      ws.emitMessage(JSON.stringify({ type: 'event', seq: i }))
    }

    await flushMicrotasks()
    manager.close()

    // drop-newest：保留最早 2 个
    expect(received).toEqual([1, 2])
  })

  it('背压警告在达到 warningThreshold 时触发', async () => {
    const onBackpressureWarning = vi.fn()
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      transport: 'ws',
      backpressure: {
        strategy: 'drop-oldest',
        maxBufferSize: 100,
        warningThreshold: 3,
      },
      onBackpressureWarning,
    })
    manager.subscribe('*', () => {
      // 故意让 handler 阻塞一次 microtask，使队列累积
    })

    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    ws.emitMessage(JSON.stringify({ type: 'event', seq: 1 }))
    ws.emitMessage(JSON.stringify({ type: 'event', seq: 2 }))
    ws.emitMessage(JSON.stringify({ type: 'event', seq: 3 }))

    // 警告在 deliver 中同步触发，不需要等待 microtask
    expect(onBackpressureWarning).toHaveBeenCalledTimes(1)
    const stats = onBackpressureWarning.mock.calls[0][0]
    expect(stats.filter).toBe('*')
    expect(stats.bufferSize).toBe(3)
    expect(stats.maxBufferSize).toBe(100)
    expect(stats.strategy).toBe('drop-oldest')
    expect(stats.droppedCount).toBe(0)
    expect(typeof stats.timestamp).toBe('number')

    manager.close()
  })

  it('drop-oldest 策略下 droppedCount 累计', async () => {
    const onBackpressureWarning = vi.fn()
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      transport: 'ws',
      backpressure: {
        strategy: 'drop-oldest',
        maxBufferSize: 2,
        warningThreshold: 1,
      },
      onBackpressureWarning,
    })
    const received: number[] = []
    manager.subscribe('*', (e) => {
      received.push(e.id as number)
    })

    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    for (let i = 1; i <= 5; i++) {
      ws.emitMessage(JSON.stringify({ type: 'event', seq: i }))
    }

    await flushMicrotasks()

    // 收到最新 2 个
    expect(received).toEqual([4, 5])
    // 警告中应反映丢弃
    const lastWarning = onBackpressureWarning.mock.calls.at(-1)?.[0]
    expect(lastWarning).toBeDefined()
    expect(lastWarning.droppedCount).toBeGreaterThan(0)

    manager.close()
  })

  it('背压警告解除：缓冲降低后可再次触发', async () => {
    const onBackpressureWarning = vi.fn()
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      transport: 'ws',
      backpressure: {
        strategy: 'drop-oldest',
        maxBufferSize: 10,
        warningThreshold: 3,
        warningResetRatio: 0.5,
      },
      onBackpressureWarning,
    })
    manager.subscribe('*', () => {
      // 同步空 handler，会快速消费
    })

    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    // 第一批：触发警告
    ws.emitMessage(JSON.stringify({ type: 'event', seq: 1 }))
    ws.emitMessage(JSON.stringify({ type: 'event', seq: 2 }))
    ws.emitMessage(JSON.stringify({ type: 'event', seq: 3 }))

    await flushMicrotasks()
    const firstCallCount = onBackpressureWarning.mock.calls.length
    expect(firstCallCount).toBeGreaterThanOrEqual(1)

    // 等待消费完成，缓冲清空
    await flushMicrotasks()

    // 第二批：再次触发警告
    ws.emitMessage(JSON.stringify({ type: 'event', seq: 4 }))
    ws.emitMessage(JSON.stringify({ type: 'event', seq: 5 }))
    ws.emitMessage(JSON.stringify({ type: 'event', seq: 6 }))

    await flushMicrotasks()

    // 应该再次触发（滞回已重置）
    expect(onBackpressureWarning.mock.calls.length).toBeGreaterThan(firstCallCount)

    manager.close()
  })

  it('多订阅者背压相互独立', async () => {
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      transport: 'ws',
      backpressure: {
        strategy: 'drop-oldest',
        maxBufferSize: 2,
        warningThreshold: 100,
      },
    })
    const receivedA: number[] = []
    const receivedB: number[] = []
    manager.subscribe('notification.*', (e) => {
      receivedA.push(e.id as number)
    })
    manager.subscribe('workspace.*', (e) => {
      receivedB.push(e.id as number)
    })

    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    // notification 5 个，workspace 2 个
    for (let i = 1; i <= 5; i++) {
      ws.emitMessage(JSON.stringify({ type: 'notification.event', seq: i }))
    }
    ws.emitMessage(JSON.stringify({ type: 'workspace.event', seq: 6 }))
    ws.emitMessage(JSON.stringify({ type: 'workspace.event', seq: 7 }))

    await flushMicrotasks()
    manager.close()

    // notification 队列满后丢弃旧事件
    expect(receivedA).toEqual([4, 5])
    // workspace 只有 2 个，不丢弃
    expect(receivedB).toEqual([6, 7])
  })
})

// ============ 生命周期测试 ============

describe('生命周期', () => {
  beforeEach(() => {
    MockWebSocket.instances = []
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('close 后状态为 disconnected 且不再分发', async () => {
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      transport: 'ws',
    })
    const handler = vi.fn()
    manager.subscribe('*', handler as EventHandler)

    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    manager.close()

    // close 后再 emit 不应触发 handler
    ws.emitMessage(JSON.stringify({ type: 'event', seq: 1 }))
    await flushMicrotasks()

    expect(handler).not.toHaveBeenCalled()
    expect(manager.getState()).toBe('disconnected')
    expect(manager.getActiveTransport()).toBe(null)
    expect(manager.getSubscriberCount()).toBe(0)
  })

  it('close 后 subscribe 抛错', () => {
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      transport: 'ws',
    })
    manager.close()
    expect(() => manager.subscribe('*', vi.fn())).toThrow(/closed/)
  })

  it('subscribe 参数校验', () => {
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      transport: 'ws',
    })
    expect(() => manager.subscribe('', vi.fn())).toThrow(/filter/)
    expect(() =>
      manager.subscribe('notification.*', 'not-a-function' as unknown as EventHandler),
    ).toThrow(/handler/)
    manager.close()
  })

  it('首个订阅者启动传输，最后一个取消后停止', async () => {
    const onStateChange = vi.fn()
    const manager = createEventManager({
      wsUrl: 'wss://example.com/ws',
      transport: 'ws',
      onStateChange,
    })
    expect(manager.getSubscriberCount()).toBe(0)
    expect(manager.getState()).toBe('disconnected')

    const h1 = manager.subscribe('*', vi.fn() as EventHandler)
    expect(manager.getState()).toBe('connecting')

    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    expect(manager.getState()).toBe('connected')

    h1.unsubscribe()
    expect(manager.getSubscriberCount()).toBe(0)
    expect(manager.getState()).toBe('disconnected')

    manager.close()
  })
})
