import type { WSEventEnvelope, WSEventHandler, WSClient, WSClientOptions, BackpressureStats } from './types'

function isNonNullObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object'
}

function isWSEnvelope(value: unknown): value is WSEventEnvelope {
  return isNonNullObject(value) && typeof value.type === 'string'
}

function parseWSMessage(data: unknown): unknown {
  if (typeof data !== 'string') return data
  try {
    return JSON.parse(data) as unknown
  } catch {
    return data
  }
}

function buildUrl(
  baseUrl: string,
  token: string | null,
  after: number | undefined,
): string {
  let url: URL
  try {
    url = new URL(baseUrl)
  } catch {
    // 相对路径或无法解析时，使用本地占位基址
    const base =
      typeof window !== 'undefined' && window.location != null
        ? window.location.href
        : 'http://localhost'
    url = new URL(baseUrl, base)
  }
  if (token) url.searchParams.set('token', token)
  if (after != null) url.searchParams.set('after', String(after))
  return url.toString()
}

/**
 * 创建具备自动重连、心跳、after 游标回放、ack 与事件订阅的 WebSocket 客户端。
 *
 * 特性：
 * - 断线后按指数退避自动重连；
 * - 重连时自动追加 `after=<lastSeq>` 实现事件回放；
 * - 支持 `subscribe(type, handler) / unsubscribe(type, handler)` 事件分发；
 * - 提供 `ack(seq)` 与 `autoAck` 两种确认方式；
 * - 可选应用层心跳（发送 `{type:'ping'}`，超时未收到任何消息则主动断开重连）。
 */
export function createWSClient(url: string, options: WSClientOptions = {}): WSClient {
  const {
    token,
    getToken,
    protocols,
    after,
    autoAck = false,
    heartbeatInterval = 0,
    heartbeatTimeout = 60000,
    reconnect = true,
    minReconnectDelay = 1000,
    maxReconnectDelay = 30000,
    maxReconnectAttempts = Number.POSITIVE_INFINITY,
    onOpen,
    onMessage,
    onError,
    onClose,
    onBackpressureWarning,
    backpressureEventThreshold = 1000,
    backpressureByteThreshold = 5 * 1024 * 1024, // 5MiB
  } = options

  let ws: WebSocket | null = null
  let intentionalClose = false
  let connected = false
  let reconnectAttempt = 0
  let lastSeq: number | undefined = after
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let heartbeatTimer: ReturnType<typeof setInterval> | null = null
  let pongTimer: ReturnType<typeof setTimeout> | null = null
  const sendQueue: string[] = []
  const handlers = new Map<string, Set<WSEventHandler>>()
  
  // 背压跟踪
  let bufferedEventCount = 0
  let bufferedByteCount = 0
  let backpressureWarningTriggered = false

  const resolveToken = (): string | null => token ?? getToken?.() ?? null

  const checkBackpressure = (messageSize: number) => {
    if (!onBackpressureWarning) return
    
    bufferedEventCount++
    bufferedByteCount += messageSize
    
    if (!backpressureWarningTriggered && 
        (bufferedEventCount >= backpressureEventThreshold || 
         bufferedByteCount >= backpressureByteThreshold)) {
      backpressureWarningTriggered = true
      const stats: BackpressureStats = {
        bufferedEventCount,
        bufferedByteCount,
        timestamp: Date.now(),
      }
      onBackpressureWarning(stats)
    }
  }

  const resetBackpressure = () => {
    bufferedEventCount = 0
    bufferedByteCount = 0
    backpressureWarningTriggered = false
  }

  const clearReconnectTimer = () => {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
  }

  const stopHeartbeat = () => {
    if (heartbeatTimer) {
      clearInterval(heartbeatTimer)
      heartbeatTimer = null
    }
    if (pongTimer) {
      clearTimeout(pongTimer)
      pongTimer = null
    }
  }

  const startHeartbeat = () => {
    stopHeartbeat()
    if (heartbeatInterval <= 0) return
    heartbeatTimer = setInterval(() => {
      if (!connected || !ws) return
      send({ type: 'ping' })
      if (pongTimer) clearTimeout(pongTimer)
      pongTimer = setTimeout(() => {
        onError?.(new Error('WebSocket heartbeat timeout'))
        ws?.close(1001, 'heartbeat timeout')
      }, heartbeatTimeout)
    }, heartbeatInterval)
  }

  const resetPongTimer = () => {
    if (pongTimer) {
      clearTimeout(pongTimer)
      pongTimer = null
    }
  }

  const flushQueue = () => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    while (sendQueue.length > 0) {
      const payload = sendQueue.shift()
      if (payload) ws.send(payload)
    }
  }

  const dispatch = (message: unknown) => {
    if (!isWSEnvelope(message)) return
    if (typeof message.seq === 'number') {
      lastSeq = message.seq
    }
    const typeHandlers = handlers.get(message.type)
    if (typeHandlers) {
      for (const handler of typeHandlers) {
        try {
          handler(message)
        } catch {
          // 消费者回调异常不应导致客户端崩溃
        }
      }
    }
    const wildcard = handlers.get('*')
    if (wildcard) {
      for (const handler of wildcard) {
        try {
          handler(message)
        } catch {
          // ignore
        }
      }
    }
  }

  const connect = () => {
    if (ws) {
      try {
        ws.onopen = null
        ws.onmessage = null
        ws.onerror = null
        ws.onclose = null
        if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
          ws.close()
        }
      } catch {
        // ignore cleanup errors
      }
    }

    const finalUrl = buildUrl(url, resolveToken(), lastSeq)
    ws = new WebSocket(finalUrl, protocols)

    ws.onopen = (event) => {
      connected = true
      reconnectAttempt = 0
      resetBackpressure()
      flushQueue()
      startHeartbeat()
      onOpen?.(event)
    }

    ws.onmessage = (event) => {
      resetPongTimer()
      const parsed = parseWSMessage(event.data)
      
      // 检查背压
      const messageSize = typeof event.data === 'string' ? event.data.length : 0
      checkBackpressure(messageSize)
      
      onMessage?.(parsed)
      dispatch(parsed)
      if (autoAck && isWSEnvelope(parsed) && typeof parsed.seq === 'number') {
        ack(parsed.seq)
      }
    }

    ws.onerror = (event) => {
      onError?.(event)
    }

    ws.onclose = (event) => {
      connected = false
      stopHeartbeat()
      onClose?.(event)
      ws = null
      if (!intentionalClose && reconnect) {
        scheduleReconnect()
      }
    }
  }

  const scheduleReconnect = () => {
    clearReconnectTimer()
    if (reconnectAttempt >= maxReconnectAttempts) {
      onError?.(new Error(`WebSocket reached max reconnect attempts (${maxReconnectAttempts})`))
      return
    }
    const delay = Math.min(minReconnectDelay * 2 ** reconnectAttempt, maxReconnectDelay)
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      reconnectAttempt++
      connect()
    }, delay)
  }

  const close: WSClient['close'] = (code, reason) => {
    intentionalClose = true
    clearReconnectTimer()
    stopHeartbeat()
    if (ws) {
      ws.close(code, reason)
      ws = null
    }
    connected = false
  }

  const send: WSClient['send'] = (message) => {
    let payload: string
    if (typeof message === 'string') {
      payload = message
    } else {
      try {
        payload = JSON.stringify(message)
      } catch (err) {
        onError?.(err instanceof Error ? err : new Error(String(err)))
        return
      }
    }
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(payload)
    } else {
      sendQueue.push(payload)
    }
  }

  const ack: WSClient['ack'] = (seq) => {
    send({ type: 'event.ack', seq })
  }

  const subscribe: WSClient['subscribe'] = (eventType, handler) => {
    let set = handlers.get(eventType)
    if (!set) {
      set = new Set()
      handlers.set(eventType, set)
    }
    set.add(handler)
  }

  const unsubscribe: WSClient['unsubscribe'] = (eventType, handler) => {
    handlers.get(eventType)?.delete(handler)
  }

  const isConnected = () => connected

  // 立即发起首次连接
  connect()

  return {
    close,
    send,
    ack,
    subscribe,
    unsubscribe,
    isConnected,
    get raw() {
      return ws
    },
  }
}
