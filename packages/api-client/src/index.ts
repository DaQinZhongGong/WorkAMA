export type ApiErrorBody = {
  detail?: string
  message?: string
  error?: { message?: string; code?: string }
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public requestId?: string,
    public body: ApiErrorBody = {},
  ) {
    super(message)
  }
}

export type ClientOptions = {
  baseUrl: string
  getToken: () => string | null
}

export class WorkamaClient {
  constructor(private options: ClientOptions) {
    this.options = { ...options, baseUrl: options.baseUrl.replace(/\/+$/, '') }
  }

  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers)
    const token = this.options.getToken()
    if (token) headers.set('Authorization', `Bearer ${token}`)
    if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json')
    const response = await fetch(`${this.options.baseUrl}${path}`, {
      ...init,
      headers,
      credentials: 'include',
    })
    if (!response.ok) {
      let body: ApiErrorBody = {}
      const text = await response.text().catch(() => '')
      try {
        body = JSON.parse(text)
      } catch {
        body = text ? { message: text } : {}
      }
      throw new ApiError(
        response.status,
        body.error?.code ?? `HTTP_${response.status}`,
        body.detail ?? body.error?.message ?? body.message ?? response.statusText,
        response.headers.get('x-request-id') ?? undefined,
        body,
      )
    }
    if (response.status === 204) return undefined as T
    return response.json() as Promise<T>
  }

  async download(path: string, init: RequestInit = {}): Promise<Blob> {
    const headers = new Headers(init.headers)
    const token = this.options.getToken()
    if (token) headers.set('Authorization', `Bearer ${token}`)
    const response = await fetch(`${this.options.baseUrl}${path}`, {
      ...init,
      headers,
      credentials: 'include',
    })
    if (!response.ok) {
      throw new ApiError(
        response.status,
        `HTTP_${response.status}`,
        response.statusText,
        response.headers.get('x-request-id') ?? undefined,
      )
    }
    return response.blob()
  }

  async stream(path: string, init: RequestInit = {}): Promise<Response> {
    const headers = new Headers(init.headers)
    const token = this.options.getToken()
    if (token) headers.set('Authorization', `Bearer ${token}`)
    const response = await fetch(`${this.options.baseUrl}${path}`, {
      ...init,
      headers,
      credentials: 'include',
    })
    if (!response.ok) {
      throw new ApiError(
        response.status,
        `HTTP_${response.status}`,
        response.statusText,
        response.headers.get('x-request-id') ?? undefined,
      )
    }
    return response
  }

  get<T>(path: string) {
    return this.request<T>(path)
  }

  post<T>(path: string, body?: unknown) {
    return this.request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })
  }

  put<T>(path: string, body?: unknown) {
    return this.request<T>(path, { method: 'PUT', body: body === undefined ? undefined : JSON.stringify(body) })
  }

  patch<T>(path: string, body?: unknown) {
    return this.request<T>(path, { method: 'PATCH', body: body === undefined ? undefined : JSON.stringify(body) })
  }

  delete(path: string, body?: unknown) {
    return this.request<void>(path, { method: 'DELETE', body: body === undefined ? undefined : JSON.stringify(body) })
  }

  upload<T>(path: string, file: File) {
    const form = new FormData()
    form.append('file', file)
    return this.request<T>(path, { method: 'POST', body: form })
  }
}

// ============ SSE（Server-Sent Events）流式通信 ============

export type SSEEvent = {
  /** 事件数据（多行 data 用 \n 连接） */
  data: string
  /** 事件类型（event: 字段） */
  event?: string
  /** 事件 ID（id: 字段） */
  id?: string
  /** 重试间隔毫秒（retry: 字段） */
  retry?: number
}

export type StreamSSEOptions = {
  /** Bearer token，与 getToken 互斥，优先级更高 */
  token?: string
  /** 从 ApiClient 实例获取 token 的函数 */
  getToken?: () => string | null
  /** 额外的请求头 */
  headers?: HeadersInit
  /** 额外的 fetch init（method/credentials 等） */
  init?: RequestInit
  /** 收到事件时回调 */
  onEvent?: (event: SSEEvent) => void
  /** 出错时回调（网络错误、HTTP 错误、读取中断） */
  onError?: (err: unknown) => void
  /** 流正常结束时回调 */
  onDone?: () => void
  /** AbortSignal，用于取消流 */
  signal?: AbortSignal
}

/**
 * 通过 fetch + ReadableStream 解析 SSE 流。
 * 使用 fetch 而非 EventSource，以便支持自定义 Authorization header。
 */
export async function streamSSE(url: string, options: StreamSSEOptions = {}): Promise<void> {
  const { token, getToken, headers, init, onEvent, onError, onDone, signal } = options

  const finalHeaders = new Headers(headers ?? init?.headers)
  const resolvedToken = token ?? getToken?.() ?? null
  if (resolvedToken) finalHeaders.set('Authorization', `Bearer ${resolvedToken}`)
  if (!finalHeaders.has('Accept')) finalHeaders.set('Accept', 'text/event-stream')

  let response: Response
  try {
    response = await fetch(url, {
      ...init,
      method: init?.method ?? 'GET',
      headers: finalHeaders,
      credentials: init?.credentials ?? 'include',
      signal,
    })
  } catch (err) {
    onError?.(err)
    onDone?.()
    return
  }

  if (!response.ok) {
    onError?.(
      new ApiError(
        response.status,
        `HTTP_${response.status}`,
        response.statusText,
        response.headers.get('x-request-id') ?? undefined,
      ),
    )
    onDone?.()
    return
  }

  const body = response.body
  if (!body) {
    onDone?.()
    return
  }

  const reader = body.getReader()
  const decoder = new TextDecoder('utf-8')
  // SSE 事件以空行（\n\n 或 \r\n\r\n）分隔
  const BOUNDARY = /\r?\n\r?\n/
  let buffer = ''

  /** 解析并派发单个事件块 */
  const dispatch = (block: string): void => {
    if (block === '') return
    const lines = block.split(/\r?\n/)
    const dataLines: string[] = []
    let event: string | undefined
    let id: string | undefined
    let retry: number | undefined
    let hasField = false

    for (const rawLine of lines) {
      if (rawLine === '') continue
      // 以冒号开头为注释行
      if (rawLine.startsWith(':')) continue
      hasField = true
      const colonIdx = rawLine.indexOf(':')
      let field: string
      let value: string
      if (colonIdx === -1) {
        field = rawLine
        value = ''
      } else {
        field = rawLine.slice(0, colonIdx)
        value = rawLine.slice(colonIdx + 1)
        // 冒号后若紧跟一个空格，按规范跳过
        if (value.startsWith(' ')) value = value.slice(1)
      }
      switch (field) {
        case 'data':
          dataLines.push(value)
          break
        case 'event':
          event = value
          break
        case 'id':
          id = value
          break
        case 'retry': {
          const n = Number(value)
          if (!Number.isNaN(n)) retry = n
          break
        }
        default:
          break
      }
    }

    if (!hasField) return
    onEvent?.({ data: dataLines.join('\n'), event, id, retry })
  }

  try {
    while (true) {
      // 用 .catch 捕获读取中断，避免依赖 ReadableStreamDefaultReadResult 类型名
      const readResult = await reader.read().catch((err: unknown) => {
        onError?.(err)
        return null
      })
      if (readResult === null) break
      if (readResult.done) break
      buffer += decoder.decode(readResult.value as Uint8Array, { stream: true })
      let match: RegExpMatchArray | null
      while ((match = buffer.match(BOUNDARY)) !== null) {
        const block = buffer.slice(0, match.index as number)
        buffer = buffer.slice((match.index as number) + match[0].length)
        dispatch(block)
      }
    }
    // 刷新解码器剩余字节并派发最后未以空行结尾的事件块
    buffer += decoder.decode()
    if (buffer.length > 0) dispatch(buffer)
  } finally {
    try {
      await reader.cancel()
    } catch {
      // 忽略取消时的错误
    }
  }

  onDone?.()
}

// ============ WebSocket 流式通信 ============

export type ConnectWSOptions = {
  /** Bearer token，与 getToken 互斥，优先级更高 */
  token?: string
  /** 从 ApiClient 实例获取 token 的函数 */
  getToken?: () => string | null
  /** 子协议 */
  protocols?: string | string[]
  /** 连接打开时回调 */
  onOpen?: (ev: Event) => void
  /** 收到消息时回调；若消息体为 JSON 字符串则自动解析后传入 */
  onMessage?: (data: unknown) => void
  /** 出错时回调 */
  onError?: (ev: Event) => void
  /** 关闭时回调 */
  onClose?: (ev: CloseEvent) => void
}

export type WSController = {
  /** 关闭连接 */
  close: (code?: number, reason?: string) => void
  /** 发送消息（对象会自动 JSON.stringify，字符串原样发送） */
  send: (data: unknown) => void
  /** 底层原生 WebSocket 实例 */
  raw: WebSocket
}

/**
 * 建立 WebSocket 连接。
 * 由于 WebSocket 不支持自定义 header，token 通过 query string 注入。
 */
export function connectWS(url: string, options: ConnectWSOptions = {}): WSController {
  const { token, getToken, protocols, onOpen, onMessage, onError, onClose } = options

  // token 注入到 URL query string（WebSocket 不支持自定义 header）
  let finalUrl = url
  const resolvedToken = token ?? getToken?.() ?? null
  if (resolvedToken) {
    const sep = finalUrl.includes('?') ? '&' : '?'
    finalUrl = `${finalUrl}${sep}token=${encodeURIComponent(resolvedToken)}`
  }

  const ws = new WebSocket(finalUrl, protocols)

  if (onOpen) ws.onopen = onOpen
  if (onError) ws.onerror = onError
  if (onClose) ws.onclose = onClose
  if (onMessage) {
    ws.onmessage = (ev: MessageEvent): void => {
      const raw = ev.data
      if (typeof raw === 'string') {
        try {
          onMessage(JSON.parse(raw))
        } catch {
          onMessage(raw)
        }
      } else {
        onMessage(raw)
      }
    }
  }

  return {
    close: (code?: number, reason?: string): void => {
      ws.close(code, reason)
    },
    send: (data: unknown): void => {
      const payload = typeof data === 'string' ? data : JSON.stringify(data)
      ws.send(payload)
    },
    raw: ws,
  }
}

// ============ 新增 SSE / WebSocket 高级助手 ============

export * from './types'
export { createSSEStream, subscribeSSE } from './sse'
export { createWSClient } from './ws'

// ============ 统一事件订阅管理器（多端事件同步） ============

export * from './events-types'
export {
  createEventManager,
  filterToRegex,
  matchFilter,
  sseToEnvelope,
  wsToEnvelope,
  isWSEnvelope,
  TRANSPORTS,
} from './events'
