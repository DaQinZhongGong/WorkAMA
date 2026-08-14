import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createWSClient } from './ws'
import type { WSEventHandler } from './types'

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

describe('createWSClient', () => {
  beforeEach(() => {
    MockWebSocket.instances = []
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('建立连接并触发 onOpen', () => {
    const onOpen = vi.fn()
    createWSClient('wss://example.com/ws', { onOpen })
    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    expect(onOpen).toHaveBeenCalledTimes(1)
    expect(ws.url).toBe('wss://example.com/ws')
  })

  it('将 token 注入 URL query string', () => {
    createWSClient('wss://example.com/ws', { token: 'secret-token' })
    const ws = MockWebSocket.instances[0]
    expect(ws.url).toBe('wss://example.com/ws?token=secret-token')
  })

  it('将 after 游标注入 URL', () => {
    createWSClient('wss://example.com/ws', { after: 42 })
    const ws = MockWebSocket.instances[0]
    expect(ws.url).toBe('wss://example.com/ws?after=42')
  })

  it('发送消息并将对象 JSON.stringify', () => {
    const client = createWSClient('wss://example.com/ws', {})
    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    client.send({ type: 'message.create', content: 'hi' })
    expect(ws.sent).toEqual(['{"type":"message.create","content":"hi"}'])
  })

  it('未连接时发送消息会入队，连接成功后自动 flush', () => {
    const client = createWSClient('wss://example.com/ws', {})
    const ws = MockWebSocket.instances[0]
    client.send({ type: 'early' })
    expect(ws.sent).toEqual([])
    ws.emitOpen()
    expect(ws.sent).toEqual(['{"type":"early"}'])
  })

  it('ack(seq) 发送 event.ack', () => {
    const client = createWSClient('wss://example.com/ws', {})
    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    client.ack(7)
    expect(ws.sent).toEqual(['{"type":"event.ack","seq":7}'])
  })

  it('autoAck 为 true 时收到带 seq 的事件自动发送 ack', () => {
    const client = createWSClient('wss://example.com/ws', { autoAck: true })
    const ws = MockWebSocket.instances[0]
    ws.emitOpen()
    ws.emitMessage('{"type":"tool.call","seq":9,"payload":{}}')
    expect(ws.sent).toEqual(['{"type":"event.ack","seq":9}'])
  })

  it('subscribe / unsubscribe 事件分发', () => {
    const client = createWSClient('wss://example.com/ws', {})
    const ws = MockWebSocket.instances[0]
    const handler = vi.fn()
    client.subscribe('tool.call', handler as WSEventHandler)
    ws.emitMessage('{"type":"tool.call","seq":1}')
    expect(handler).toHaveBeenCalledTimes(1)
    expect(handler.mock.calls[0][0]).toMatchObject({ type: 'tool.call', seq: 1 })

    client.unsubscribe('tool.call', handler as WSEventHandler)
    ws.emitMessage('{"type":"tool.call","seq":2}')
    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('通配符 * 订阅所有事件', () => {
    const client = createWSClient('wss://example.com/ws', {})
    const ws = MockWebSocket.instances[0]
    const handler = vi.fn()
    client.subscribe('*', handler as WSEventHandler)
    ws.emitMessage('{"type":"status","seq":3}')
    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('onMessage 回调在 subscribe 之前触发', () => {
    const onMessage = vi.fn()
    const client = createWSClient('wss://example.com/ws', { onMessage })
    const ws = MockWebSocket.instances[0]
    client.subscribe('status', vi.fn() as WSEventHandler)
    ws.emitMessage('{"type":"status"}')
    expect(onMessage).toHaveBeenCalledTimes(1)
  })

  it('断线后按指数退避重连，并携带最新 after 游标', async () => {
    vi.useFakeTimers()
    const client = createWSClient('wss://example.com/ws', {
      after: 5,
      minReconnectDelay: 100,
      maxReconnectDelay: 1000,
    })
    const first = MockWebSocket.instances[0]
    first.emitOpen()
    first.emitMessage('{"type":"delta","seq":10}')
    first.emitClose(1001, 'going away')

    await vi.advanceTimersByTimeAsync(150)
    expect(MockWebSocket.instances).toHaveLength(2)
    const second = MockWebSocket.instances[1]
    expect(second.url).toBe('wss://example.com/ws?after=10')
    expect(client.isConnected()).toBe(false)
    second.emitOpen()
    expect(client.isConnected()).toBe(true)

    vi.useRealTimers()
  })

  it('主动 close 后不再自动重连', async () => {
    vi.useFakeTimers()
    const client = createWSClient('wss://example.com/ws', { minReconnectDelay: 50 })
    client.close(1000, 'bye')

    await vi.advanceTimersByTimeAsync(200)
    expect(MockWebSocket.instances).toHaveLength(1)

    vi.useRealTimers()
  })

  it('达到最大重连次数后触发 onError 且不再连接', async () => {
    vi.useFakeTimers()
    const onError = vi.fn()
    createWSClient('wss://example.com/ws', {
      onError,
      maxReconnectAttempts: 2,
      minReconnectDelay: 10,
      maxReconnectDelay: 100,
    })
    const first = MockWebSocket.instances[0]
    first.emitClose(1001, 'going away')

    // 第 1 次重连
    await vi.advanceTimersByTimeAsync(50)
    const second = MockWebSocket.instances[1]
    second.emitClose(1001, 'going away')

    // 第 2 次重连
    await vi.advanceTimersByTimeAsync(120)
    const third = MockWebSocket.instances[2]
    third.emitClose(1001, 'going away')

    // 再推进一段时间，不应有第 4 次连接
    await vi.advanceTimersByTimeAsync(500)
    expect(MockWebSocket.instances).toHaveLength(3)
    expect(onError).toHaveBeenCalled()

    vi.useRealTimers()
  })

  it('背压警告：事件数超过阈值时触发', () => {
    const onBackpressureWarning = vi.fn()
    const client = createWSClient('wss://example.com/ws', {
      onBackpressureWarning,
      backpressureEventThreshold: 3,
    })
    const ws = MockWebSocket.instances[0]
    ws.emitOpen()

    // 发送 3 个事件
    ws.emitMessage('{"type":"event1","seq":1}')
    ws.emitMessage('{"type":"event2","seq":2}')
    ws.emitMessage('{"type":"event3","seq":3}')

    expect(onBackpressureWarning).toHaveBeenCalledTimes(1)
    expect(onBackpressureWarning).toHaveBeenCalledWith(
      expect.objectContaining({
        bufferedEventCount: 3,
        bufferedByteCount: expect.any(Number),
        timestamp: expect.any(Number),
      })
    )
  })

  it('背压警告：字节数超过阈值时触发', () => {
    const onBackpressureWarning = vi.fn()
    const client = createWSClient('wss://example.com/ws', {
      onBackpressureWarning,
      backpressureByteThreshold: 100, // 100 bytes
    })
    const ws = MockWebSocket.instances[0]
    ws.emitOpen()

    // 发送一个大消息（超过 100 bytes）
    const largeMessage = '{"type":"large","data":"' + 'x'.repeat(150) + '","seq":1}'
    ws.emitMessage(largeMessage)

    expect(onBackpressureWarning).toHaveBeenCalledTimes(1)
    expect(onBackpressureWarning).toHaveBeenCalledWith(
      expect.objectContaining({
        bufferedEventCount: 1,
        bufferedByteCount: largeMessage.length,
        timestamp: expect.any(Number),
      })
    )
  })

  it('背压警告：重连后重置计数器', async () => {
    vi.useFakeTimers()
    const onBackpressureWarning = vi.fn()
    const client = createWSClient('wss://example.com/ws', {
      onBackpressureWarning,
      backpressureEventThreshold: 2,
      minReconnectDelay: 100,
      maxReconnectDelay: 1000,
    })
    const first = MockWebSocket.instances[0]
    first.emitOpen()

    // 触发第一次背压警告
    first.emitMessage('{"type":"event1","seq":1}')
    first.emitMessage('{"type":"event2","seq":2}')
    expect(onBackpressureWarning).toHaveBeenCalledTimes(1)

    // 断开连接
    first.emitClose(1001, 'going away')

    // 等待重连
    await vi.advanceTimersByTimeAsync(150)
    expect(MockWebSocket.instances).toHaveLength(2)

    // 重连
    const second = MockWebSocket.instances[1]
    second.emitOpen()

    // 发送 1 个事件，不应触发警告（计数器已重置为 0，阈值为 2）
    second.emitMessage('{"type":"event3","seq":3}')
    expect(onBackpressureWarning).toHaveBeenCalledTimes(1) // 仍然是 1 次，证明计数器已重置

    vi.useRealTimers()
  })

  it('背压警告：未配置回调时不报错', () => {
    const client = createWSClient('wss://example.com/ws', {
      backpressureEventThreshold: 2,
    })
    const ws = MockWebSocket.instances[0]
    ws.emitOpen()

    // 发送 3 个事件，不应报错
    expect(() => {
      ws.emitMessage('{"type":"event1","seq":1}')
      ws.emitMessage('{"type":"event2","seq":2}')
      ws.emitMessage('{"type":"event3","seq":3}')
    }).not.toThrow()
  })
})
