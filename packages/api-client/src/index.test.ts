import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, WorkamaClient, connectWS, streamSSE } from './index'

describe('WorkamaClient', () => {
  const fetchMock = vi.fn()

  beforeEach(() => {
    fetchMock.mockReset()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function mockOk(json: unknown, status = 200) {
    return {
      ok: true,
      status,
      json: () => Promise.resolve(json),
      text: () => Promise.resolve(typeof json === 'string' ? json : JSON.stringify(json)),
      headers: new Headers(),
    }
  }

  function mockError(status: number, statusText: string, text: string, headers: Record<string, string> = {}) {
    return {
      ok: false,
      status,
      statusText,
      text: () => Promise.resolve(text),
      headers: new Headers(headers),
    }
  }

  describe('constructor', () => {
    it('trims a single trailing slash from baseUrl', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({}))
      const client = new WorkamaClient({
        baseUrl: 'https://api.example.com/',
        getToken: () => null,
      })
      await client.get('/users')
      expect(fetchMock.mock.calls[0][0]).toBe('https://api.example.com/users')
    })

    it('trims multiple trailing slashes from baseUrl', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({}))
      const client = new WorkamaClient({
        baseUrl: 'https://api.example.com///',
        getToken: () => null,
      })
      await client.get('/users')
      expect(fetchMock.mock.calls[0][0]).toBe('https://api.example.com/users')
    })

    it('leaves baseUrl without trailing slash unchanged', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({}))
      const client = new WorkamaClient({
        baseUrl: 'https://api.example.com',
        getToken: () => null,
      })
      await client.get('/users')
      expect(fetchMock.mock.calls[0][0]).toBe('https://api.example.com/users')
    })
  })

  describe('Authorization header injection', () => {
    it('injects Bearer token on get', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({}))
      const client = new WorkamaClient({ baseUrl: 'https://api.example.com', getToken: () => 'tok123' })
      await client.get('/users')
      const init = fetchMock.mock.calls[0][1] as RequestInit
      expect((init.headers as Headers).get('Authorization')).toBe('Bearer tok123')
    })

    it('injects Bearer token on post', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({}))
      const client = new WorkamaClient({ baseUrl: 'https://api.example.com', getToken: () => 'tok123' })
      await client.post('/users', { name: 'foo' })
      const init = fetchMock.mock.calls[0][1] as RequestInit
      expect((init.headers as Headers).get('Authorization')).toBe('Bearer tok123')
    })

    it('injects Bearer token on patch', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({}))
      const client = new WorkamaClient({ baseUrl: 'https://api.example.com', getToken: () => 'tok123' })
      await client.patch('/users/1', { name: 'bar' })
      const init = fetchMock.mock.calls[0][1] as RequestInit
      expect((init.headers as Headers).get('Authorization')).toBe('Bearer tok123')
    })

    it('injects Bearer token on delete', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({}, 204))
      const client = new WorkamaClient({ baseUrl: 'https://api.example.com', getToken: () => 'tok123' })
      await client.delete('/users/1')
      const init = fetchMock.mock.calls[0][1] as RequestInit
      expect((init.headers as Headers).get('Authorization')).toBe('Bearer tok123')
    })

    it('omits Authorization header when getToken returns null', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({}))
      const client = new WorkamaClient({ baseUrl: 'https://api.example.com', getToken: () => null })
      await client.get('/users')
      const init = fetchMock.mock.calls[0][1] as RequestInit
      expect((init.headers as Headers).get('Authorization')).toBeNull()
    })
  })

  describe('get', () => {
    it('returns parsed JSON data', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ id: 1, name: 'foo' }))
      const client = new WorkamaClient({ baseUrl: 'https://api.example.com', getToken: () => null })
      const result = await client.get<{ id: number; name: string }>('/users/1')
      expect(result).toEqual({ id: 1, name: 'foo' })
    })

    it('returns undefined for 204 No Content without throwing SyntaxError', async () => {
      fetchMock.mockResolvedValueOnce({
        ok: true,
        status: 204,
        json: () => Promise.reject(new SyntaxError('Unexpected end of JSON input')),
        text: () => Promise.resolve(''),
        headers: new Headers(),
      })
      const client = new WorkamaClient({ baseUrl: 'https://api.example.com', getToken: () => null })
      const result = await client.get('/users')
      expect(result).toBeUndefined()
    })
  })

  describe('post/patch JSON body', () => {
    it('post auto-sets Content-Type application/json and stringifies body', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({}))
      const client = new WorkamaClient({ baseUrl: 'https://api.example.com', getToken: () => null })
      await client.post('/users', { name: 'foo' })
      const init = fetchMock.mock.calls[0][1] as RequestInit
      expect((init.headers as Headers).get('Content-Type')).toBe('application/json')
      expect(init.body).toBe(JSON.stringify({ name: 'foo' }))
      expect(init.method).toBe('POST')
    })

    it('patch auto-sets Content-Type application/json and stringifies body', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({}))
      const client = new WorkamaClient({ baseUrl: 'https://api.example.com', getToken: () => null })
      await client.patch('/users/1', { name: 'bar' })
      const init = fetchMock.mock.calls[0][1] as RequestInit
      expect((init.headers as Headers).get('Content-Type')).toBe('application/json')
      expect(init.body).toBe(JSON.stringify({ name: 'bar' }))
      expect(init.method).toBe('PATCH')
    })
  })

  describe('error handling', () => {
    it('throws ApiError with correct status and body for 4xx JSON response', async () => {
      const errorBody = { detail: 'not found', error: { code: 'NOT_FOUND', message: 'resource missing' } }
      fetchMock.mockResolvedValue(mockError(404, 'Not Found', JSON.stringify(errorBody), { 'x-request-id': 'req-123' }))
      const client = new WorkamaClient({ baseUrl: 'https://api.example.com', getToken: () => null })
      expect.assertions(6)
      try {
        await client.get('/users/1')
      } catch (e) {
        const err = e as ApiError
        expect(err).toBeInstanceOf(ApiError)
        expect(err.status).toBe(404)
        expect(err.code).toBe('NOT_FOUND')
        expect(err.message).toBe('not found')
        expect(err.requestId).toBe('req-123')
        expect(err.body).toEqual(errorBody)
      }
    })

    it('throws ApiError with correct status and body for 5xx JSON response', async () => {
      fetchMock.mockResolvedValue(mockError(500, 'Internal Server Error', JSON.stringify({ detail: 'server error' })))
      const client = new WorkamaClient({ baseUrl: 'https://api.example.com', getToken: () => null })
      expect.assertions(4)
      try {
        await client.get('/users')
      } catch (e) {
        const err = e as ApiError
        expect(err).toBeInstanceOf(ApiError)
        expect(err.status).toBe(500)
        expect(err.code).toBe('HTTP_500')
        expect(err.message).toBe('server error')
      }
    })

    it('falls back to { message: text } for non-JSON error response body', async () => {
      fetchMock.mockResolvedValue(mockError(502, 'Bad Gateway', 'Upstream is down'))
      const client = new WorkamaClient({ baseUrl: 'https://api.example.com', getToken: () => null })
      expect.assertions(4)
      try {
        await client.get('/users')
      } catch (e) {
        const err = e as ApiError
        expect(err).toBeInstanceOf(ApiError)
        expect(err.status).toBe(502)
        expect(err.body).toEqual({ message: 'Upstream is down' })
        expect(err.message).toBe('Upstream is down')
      }
    })
  })

  describe('download', () => {
    it('returns Blob and does not set Content-Type header', async () => {
      const blob = new Blob(['file-content'], { type: 'application/octet-stream' })
      fetchMock.mockResolvedValueOnce({
        ok: true,
        status: 200,
        blob: () => Promise.resolve(blob),
        headers: new Headers(),
      })
      const client = new WorkamaClient({ baseUrl: 'https://api.example.com', getToken: () => 'tok123' })
      const result = await client.download('/files/1')
      expect(result).toBe(blob)
      const init = fetchMock.mock.calls[0][1] as RequestInit
      expect((init.headers as Headers).get('Content-Type')).toBeNull()
      expect((init.headers as Headers).get('Authorization')).toBe('Bearer tok123')
    })
  })

  describe('upload', () => {
    it('uses FormData body and does not set Content-Type header', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ id: 1 }))
      const client = new WorkamaClient({ baseUrl: 'https://api.example.com', getToken: () => 'tok123' })
      const file = new File(['hello'], 'report.txt', { type: 'text/plain' })
      await client.upload('/files', file)
      const init = fetchMock.mock.calls[0][1] as RequestInit
      expect(init.body).toBeInstanceOf(FormData)
      expect((init.headers as Headers).get('Content-Type')).toBeNull()
      expect((init.headers as Headers).get('Authorization')).toBe('Bearer tok123')
      expect(init.method).toBe('POST')
    })
  })

  describe('stream', () => {
    it('returns the raw Response object', async () => {
      const mockResponse = {
        ok: true,
        status: 200,
        body: {},
        headers: new Headers(),
      }
      fetchMock.mockResolvedValueOnce(mockResponse)
      const client = new WorkamaClient({ baseUrl: 'https://api.example.com', getToken: () => 'tok123' })
      const result = await client.stream('/events')
      expect(result).toBe(mockResponse)
    })
  })
})

// ============ SSE mock 辅助 ============

/** 构造一个模拟 SSE 响应，body 通过分块 emit 指定字符串 */
function mockSSEResponse(chunks: string[], status = 200) {
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
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    body: { getReader: () => reader },
    headers: new Headers(),
  }
}

// ============ streamSSE 单元测试 ============

describe('streamSSE', () => {
  const fetchMock = vi.fn()

  beforeEach(() => {
    fetchMock.mockReset()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('解析单条事件', async () => {
    fetchMock.mockResolvedValueOnce(mockSSEResponse(['data: hello\n\n']))
    const onEvent = vi.fn()
    const onDone = vi.fn()
    await streamSSE('https://api.example.com/sse', { onEvent, onDone })
    expect(onEvent).toHaveBeenCalledTimes(1)
    expect(onEvent).toHaveBeenCalledWith({ data: 'hello' })
    expect(onDone).toHaveBeenCalledTimes(1)
  })

  it('解析多条事件', async () => {
    fetchMock.mockResolvedValueOnce(mockSSEResponse(['data: event1\n\ndata: event2\n\n']))
    const onEvent = vi.fn()
    await streamSSE('https://api.example.com/sse', { onEvent })
    expect(onEvent).toHaveBeenCalledTimes(2)
    expect(onEvent).toHaveBeenNthCalledWith(1, { data: 'event1' })
    expect(onEvent).toHaveBeenNthCalledWith(2, { data: 'event2' })
  })

  it('解析带 data: 前缀的多行 data（用 \\n 连接）', async () => {
    fetchMock.mockResolvedValueOnce(mockSSEResponse(['data: line1\ndata: line2\n\n']))
    const onEvent = vi.fn()
    await streamSSE('https://api.example.com/sse', { onEvent })
    expect(onEvent).toHaveBeenCalledTimes(1)
    expect(onEvent).toHaveBeenCalledWith({ data: 'line1\nline2' })
  })

  it('HTTP 错误状态触发 onError 回调', async () => {
    fetchMock.mockResolvedValueOnce(mockSSEResponse([], 500))
    const onError = vi.fn()
    const onDone = vi.fn()
    const onEvent = vi.fn()
    await streamSSE('https://api.example.com/sse', { onError, onDone, onEvent })
    expect(onError).toHaveBeenCalledTimes(1)
    expect((onError.mock.calls[0][0] as ApiError).status).toBe(500)
    expect(onEvent).not.toHaveBeenCalled()
    expect(onDone).toHaveBeenCalledTimes(1)
  })

  it('支持 AbortController 中断（fetch 拒绝时调用 onError）', async () => {
    const controller = new AbortController()
    controller.abort()
    const abortError = new Error('The user aborted a request.')
    fetchMock.mockRejectedValueOnce(abortError)

    const onError = vi.fn()
    const onDone = vi.fn()
    await streamSSE('https://api.example.com/sse', {
      signal: controller.signal,
      onError,
      onDone,
    })

    // 验证 signal 被传递给 fetch
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(init.signal).toBe(controller.signal)
    expect(onError).toHaveBeenCalledWith(abortError)
    expect(onDone).toHaveBeenCalledTimes(1)
  })

  it('注入 Bearer token 到 Authorization header 并设置 Accept', async () => {
    fetchMock.mockResolvedValueOnce(mockSSEResponse([]))
    await streamSSE('https://api.example.com/sse', { token: 'tok-sse' })
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect((init.headers as Headers).get('Authorization')).toBe('Bearer tok-sse')
    expect((init.headers as Headers).get('Accept')).toBe('text/event-stream')
  })

  it('支持跨分块的 SSE 事件拼接', async () => {
    // 将一个完整事件拆成两个 chunk
    fetchMock.mockResolvedValueOnce(mockSSEResponse(['data: par', 'tial\n\n']))
    const onEvent = vi.fn()
    await streamSSE('https://api.example.com/sse', { onEvent })
    expect(onEvent).toHaveBeenCalledTimes(1)
    expect(onEvent).toHaveBeenCalledWith({ data: 'partial' })
  })
})

// ============ WebSocket Mock ============

class MockWebSocket {
  static instances: MockWebSocket[] = []

  url: string
  protocols?: string | string[]
  readyState = 0

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
    this.readyState = 3
  }

  // 测试辅助方法
  emitOpen(): void {
    this.readyState = 1
    this.onopen?.({ type: 'open' } as Event)
  }

  emitMessage(data: unknown): void {
    this.onmessage?.({ data } as MessageEvent)
  }

  emitClose(code = 1000, reason = ''): void {
    this.readyState = 3
    this.onclose?.({ type: 'close', code, reason, wasClean: true } as CloseEvent)
  }
}

// ============ connectWS 单元测试 ============

describe('connectWS', () => {
  beforeEach(() => {
    MockWebSocket.instances = []
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('onMessage 回调被触发（JSON 字符串自动解析）', () => {
    const onMessage = vi.fn()
    connectWS('wss://example.com/ws', { onMessage })

    const ws = MockWebSocket.instances[0]
    ws.emitMessage('{"type":"ping","ts":123}')

    expect(onMessage).toHaveBeenCalledTimes(1)
    expect(onMessage).toHaveBeenCalledWith({ type: 'ping', ts: 123 })
  })

  it('onMessage 对非 JSON 字符串原样传递', () => {
    const onMessage = vi.fn()
    connectWS('wss://example.com/ws', { onMessage })

    const ws = MockWebSocket.instances[0]
    ws.emitMessage('plain text')

    expect(onMessage).toHaveBeenCalledWith('plain text')
  })

  it('close 控制器关闭底层 WebSocket', () => {
    const controller = connectWS('wss://example.com/ws', {})
    controller.close(1000, 'normal')

    const ws = MockWebSocket.instances[0]
    expect(ws.closed).toBe(true)
    expect(ws.closeCode).toBe(1000)
    expect(ws.closeReason).toBe('normal')
  })

  it('token 注入到 URL query string', () => {
    connectWS('wss://example.com/ws', { token: 'secret-token' })

    const ws = MockWebSocket.instances[0]
    expect(ws.url).toBe('wss://example.com/ws?token=secret-token')
  })

  it('URL 已有 query string 时追加 token', () => {
    connectWS('wss://example.com/ws?foo=bar', { token: 'secret-token' })

    const ws = MockWebSocket.instances[0]
    expect(ws.url).toBe('wss://example.com/ws?foo=bar&token=secret-token')
  })

  it('getToken 函数也可提供 token', () => {
    connectWS('wss://example.com/ws', { getToken: () => 'tok-from-fn' })

    const ws = MockWebSocket.instances[0]
    expect(ws.url).toBe('wss://example.com/ws?token=tok-from-fn')
  })

  it('send 方法将对象 JSON 序列化后发送', () => {
    const controller = connectWS('wss://example.com/ws', {})
    controller.send({ msg: 'hi' })

    const ws = MockWebSocket.instances[0]
    expect(ws.sent).toEqual(['{"msg":"hi"}'])
  })

  it('onClose 回调被触发', () => {
    const onClose = vi.fn()
    connectWS('wss://example.com/ws', { onClose })

    const ws = MockWebSocket.instances[0]
    ws.emitClose(1001, 'going away')

    expect(onClose).toHaveBeenCalledTimes(1)
    expect((onClose.mock.calls[0][0] as CloseEvent).code).toBe(1001)
  })
})
