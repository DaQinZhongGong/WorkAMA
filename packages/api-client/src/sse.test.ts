import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createSSEStream, subscribeSSE } from './sse'
import { ApiError } from './index'
import type { SSEStreamEvent } from './types'

function mockSSEResponse(
  chunks: string[],
  status = 200,
  headers: Record<string, string> = {},
  options?: { readErrorAfter?: number },
) {
  const encoder = new TextEncoder()
  let i = 0
  const reader = {
    read: async () => {
      if (options?.readErrorAfter != null && i >= options.readErrorAfter) {
        throw new Error('simulated read error')
      }
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

describe('createSSEStream', () => {
  const fetchMock = vi.fn()

  beforeEach(() => {
    fetchMock.mockReset()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  async function collect(url: string, options: Parameters<typeof createSSEStream>[1] = {}) {
    const events: SSEStreamEvent[] = []
    for await (const event of createSSEStream(url, options)) {
      events.push(event)
    }
    return events
  }

  it('解析单条 SSE 事件（含 event 与 id）', async () => {
    fetchMock.mockResolvedValueOnce(
      mockSSEResponse(['event: delta\nid: 7\ndata: hello\n\n']),
    )
    const events = await collect('https://api.example.com/sse')
    expect(events).toHaveLength(1)
    expect(events[0]).toEqual({ data: 'hello', event: 'delta', id: '7' })
  })

  it('解析多条事件与多行 data', async () => {
    fetchMock.mockResolvedValueOnce(
      mockSSEResponse(['data: line1\ndata: line2\n\ndata: second\n\n']),
    )
    const events = await collect('https://api.example.com/sse')
    expect(events).toHaveLength(2)
    expect(events[0]).toEqual({ data: 'line1\nline2' })
    expect(events[1]).toEqual({ data: 'second' })
  })

  it('跨 chunk 拼接事件', async () => {
    fetchMock.mockResolvedValueOnce(mockSSEResponse(['data: pa', 'rt1\n\ndata: part2\n\n']))
    const events = await collect('https://api.example.com/sse')
    expect(events).toHaveLength(2)
    expect(events[0]).toEqual({ data: 'part1' })
    expect(events[1]).toEqual({ data: 'part2' })
  })

  it('自动注入 Bearer token、Accept 与 Cache-Control', async () => {
    fetchMock.mockResolvedValueOnce(mockSSEResponse(['data: ok\n\n']))
    await collect('https://api.example.com/sse', { token: 'tok-sse' })
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect((init.headers as Headers).get('Authorization')).toBe('Bearer tok-sse')
    expect((init.headers as Headers).get('Accept')).toBe('text/event-stream')
    expect((init.headers as Headers).get('Cache-Control')).toBe('no-cache')
  })

  it('支持 getToken 动态获取 token', async () => {
    fetchMock.mockResolvedValueOnce(mockSSEResponse(['data: ok\n\n']))
    await collect('https://api.example.com/sse', { getToken: () => 'tok-dynamic' })
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect((init.headers as Headers).get('Authorization')).toBe('Bearer tok-dynamic')
  })

  it('AbortSignal 取消流', async () => {
    const controller = new AbortController()
    fetchMock.mockImplementationOnce(async () => {
      controller.abort()
      return mockSSEResponse(['data: ok\n\n'])
    })
    const events = await collect('https://api.example.com/sse', { signal: controller.signal })
    expect(events).toHaveLength(0)
  })

  it('fetch 网络错误后重连并继续消费', async () => {
    fetchMock
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce(mockSSEResponse(['data: resumed\n\n']))

    const onError = vi.fn()
    const events = await collect('https://api.example.com/sse', {
      onError,
      reconnectDelayMs: 0,
      maxReconnectAttempts: 1,
    })

    expect(onError).toHaveBeenCalledTimes(1)
    expect(onError.mock.calls[0][0]).toBeInstanceOf(Error)
    expect(events).toHaveLength(1)
    expect(events[0]).toEqual({ data: 'resumed' })
  })

  it('断线重连时携带 Last-Event-ID', async () => {
    fetchMock
      .mockResolvedValueOnce(
        mockSSEResponse(['id: 42\ndata: first\n\n'], 200, {}, { readErrorAfter: 1 }),
      )
      .mockResolvedValueOnce(mockSSEResponse(['data: second\n\n']))

    const events = await collect('https://api.example.com/sse', {
      reconnectDelayMs: 0,
      maxReconnectAttempts: 1,
    })

    expect(events).toHaveLength(2)
    expect(events[0]).toEqual({ data: 'first', id: '42' })
    expect(events[1]).toEqual({ data: 'second' })

    const secondInit = fetchMock.mock.calls[1][1] as RequestInit
    expect((secondInit.headers as Headers).get('Last-Event-ID')).toBe('42')
  })

  it('HTTP 错误状态触发 onError 并在超过最大重连次数后抛出', async () => {
    fetchMock.mockResolvedValue(mockSSEResponse([], 503))
    const onError = vi.fn()

    await expect(
      collect('https://api.example.com/sse', {
        onError,
        reconnectDelayMs: 0,
        maxReconnectAttempts: 2,
      }),
    ).rejects.toBeInstanceOf(ApiError)

    expect(onError).toHaveBeenCalledTimes(3)
    expect((onError.mock.calls[0][0] as ApiError).status).toBe(503)
  })
})

// ============ subscribeSSE 测试 ============

describe('subscribeSSE', () => {
  const fetchMock = vi.fn()

  beforeEach(() => {
    fetchMock.mockReset()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('收到事件后调用 onEvent 回调', async () => {
    fetchMock.mockResolvedValueOnce(
      mockSSEResponse(['data: hello\n\n']),
    )
    const onEvent = vi.fn()
    const onDone = vi.fn()

    subscribeSSE('https://api.example.com/sse', { onEvent, onDone })

    // 等待异步流完成
    await vi.waitFor(() => {
      expect(onEvent).toHaveBeenCalledTimes(1)
      expect(onEvent).toHaveBeenCalledWith({ data: 'hello' })
      expect(onDone).toHaveBeenCalledTimes(1)
    })
  })

  it('返回 unsubscribe 函数可取消订阅', async () => {
    // 使用延迟的 mock，确保可以在第一个事件后取消
    const encoder = new TextEncoder()
    let resolveSecondRead: (() => void) | null = null
    const reader = {
      read: async () => {
        if (resolveSecondRead === null) {
          // 第一次 read：立即返回第一个事件
          resolveSecondRead = () => {} // 标记已读过第一次
          return { done: false, value: encoder.encode('data: first\n\n') }
        }
        // 第二次 read：等待，模拟流还在继续
        await new Promise<void>((resolve) => {
          resolveSecondRead = resolve
        })
        return { done: true, value: undefined }
      },
      cancel: async () => {},
    }
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: 'OK',
      body: { getReader: () => reader },
      headers: new Headers(),
    } as unknown as Response)

    const onEvent = vi.fn()
    const onDone = vi.fn()
    const { unsubscribe } = subscribeSSE('https://api.example.com/sse', { onEvent, onDone })

    // 等待第一个事件
    await vi.waitFor(() => {
      expect(onEvent).toHaveBeenCalledTimes(1)
      expect(onEvent).toHaveBeenCalledWith({ data: 'first' })
    })

    // 取消订阅
    unsubscribe()

    // 等待一段时间确保流被取消，onDone 不被调用（因为是主动取消）
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(onEvent).toHaveBeenCalledTimes(1)
  })

  it('支持外部 AbortSignal', async () => {
    // 使用延迟的 mock，确保可以在第一个事件后 abort
    const encoder = new TextEncoder()
    let resolveSecondRead: (() => void) | null = null
    const reader = {
      read: async () => {
        if (resolveSecondRead === null) {
          // 第一次 read：立即返回第一个事件
          resolveSecondRead = () => {} // 标记已读过第一次
          return { done: false, value: encoder.encode('data: event1\n\n') }
        }
        // 第二次 read：等待，模拟流还在继续
        await new Promise<void>((resolve) => {
          resolveSecondRead = resolve
        })
        return { done: true, value: undefined }
      },
      cancel: async () => {},
    }
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: 'OK',
      body: { getReader: () => reader },
      headers: new Headers(),
    } as unknown as Response)

    const controller = new AbortController()
    const onEvent = vi.fn()

    subscribeSSE('https://api.example.com/sse', {
      onEvent,
      signal: controller.signal,
    })

    // 等待第一个事件
    await vi.waitFor(() => {
      expect(onEvent).toHaveBeenCalledTimes(1)
      expect(onEvent).toHaveBeenCalledWith({ data: 'event1' })
    })

    // 外部 abort
    controller.abort()

    // 等待一段时间确保没有更多事件
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(onEvent).toHaveBeenCalledTimes(1)
  })

  it('注入 Bearer token 和 Accept header', async () => {
    fetchMock.mockResolvedValueOnce(mockSSEResponse(['data: ok\n\n']))
    const onEvent = vi.fn()

    subscribeSSE('https://api.example.com/sse', {
      token: 'test-token',
      onEvent,
    })

    await vi.waitFor(() => {
      expect(fetchMock).toHaveBeenCalled()
    })

    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect((init.headers as Headers).get('Authorization')).toBe('Bearer test-token')
    expect((init.headers as Headers).get('Accept')).toBe('text/event-stream')
  })

  it('支持 getToken 动态获取 token', async () => {
    fetchMock.mockResolvedValueOnce(mockSSEResponse(['data: ok\n\n']))
    const onEvent = vi.fn()

    subscribeSSE('https://api.example.com/sse', {
      getToken: () => 'dynamic-token',
      onEvent,
    })

    await vi.waitFor(() => {
      expect(fetchMock).toHaveBeenCalled()
    })

    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect((init.headers as Headers).get('Authorization')).toBe('Bearer dynamic-token')
  })

  it('网络错误时调用 onError 回调', async () => {
    fetchMock.mockRejectedValueOnce(new Error('network error'))
    const onEvent = vi.fn()
    const onError = vi.fn()

    subscribeSSE('https://api.example.com/sse', {
      onEvent,
      onError,
      maxReconnectAttempts: 0,
    })

    await vi.waitFor(() => {
      expect(onError).toHaveBeenCalledTimes(1)
      expect(onError.mock.calls[0][0]).toBeInstanceOf(Error)
      expect(onError.mock.calls[0][0].message).toBe('network error')
    })
  })

  it('HTTP 错误时调用 onError 回调', async () => {
    fetchMock.mockResolvedValueOnce(mockSSEResponse([], 500))
    const onEvent = vi.fn()
    const onError = vi.fn()

    subscribeSSE('https://api.example.com/sse', {
      onEvent,
      onError,
      maxReconnectAttempts: 0,
    })

    await vi.waitFor(() => {
      expect(onError).toHaveBeenCalledTimes(1)
      expect(onError.mock.calls[0][0]).toBeInstanceOf(ApiError)
      expect((onError.mock.calls[0][0] as ApiError).status).toBe(500)
    })
  })

  it('流正常结束时调用 onDone 回调', async () => {
    fetchMock.mockResolvedValueOnce(
      mockSSEResponse(['data: event1\n\ndata: event2\n\n']),
    )
    const onEvent = vi.fn()
    const onDone = vi.fn()

    subscribeSSE('https://api.example.com/sse', { onEvent, onDone })

    await vi.waitFor(() => {
      expect(onEvent).toHaveBeenCalledTimes(2)
      expect(onDone).toHaveBeenCalledTimes(1)
    })
  })

  it('支持自定义 headers', async () => {
    fetchMock.mockResolvedValueOnce(mockSSEResponse(['data: ok\n\n']))
    const onEvent = vi.fn()

    subscribeSSE('https://api.example.com/sse', {
      headers: { 'X-Custom-Header': 'custom-value' },
      onEvent,
    })

    await vi.waitFor(() => {
      expect(fetchMock).toHaveBeenCalled()
    })

    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect((init.headers as Headers).get('X-Custom-Header')).toBe('custom-value')
  })
})
