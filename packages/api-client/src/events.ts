/**
 * 统一事件订阅管理器。
 *
 * 在底层 `createSSEStream` 与 `createWSClient` 之上提供：
 * - SSE / WS 双传输自动选择与降级（`auto` 模式优先 WS，失败 N 次后切换 SSE）；
 * - 事件类型过滤（glob 模式：`notification.*`、`workspace.*`、`*`）；
 * - 多订阅者（同一事件可被多个监听器接收）；
 * - 自动重连与事件补发（SSE 走 Last-Event-ID，WS 走 after 游标，由底层负责）；
 * - 背压控制（`buffer` / `drop-oldest` / `drop-newest` 三种策略，每订阅者独立队列）。
 *
 * 不破坏现有 SSE/WS 客户端的 API；本模块仅作为可选的上层封装。
 */

import { createSSEStream } from './sse'
import { createWSClient } from './ws'
import type { SSEStreamEvent, WSEventEnvelope, WSClient } from './types'
import type {
  BackpressureStrategy,
  BackpressureWarningStats,
  ConnectionState,
  EventEnvelope,
  EventHandler,
  EventManager,
  EventManagerOptions,
  EventSubscriptionHandle,
  Transport,
} from './events-types'

// ============ 工具函数 ============

/**
 * 将 filter glob 转为正则。
 * `*` 视为 `.*`（匹配任意字符，包括 `.`），其他正则元字符转义。
 *
 * @example
 *   filterToRegex('notification.*') -> /^notification\..*$/
 *   filterToRegex('billing.invoice.paid') -> /^billing\.invoice\.paid$/
 *   filterToRegex('*') -> /^.*$/
 */
function filterToRegex(filter: string): RegExp {
  const escaped = filter.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*')
  return new RegExp(`^${escaped}$`)
}

function matchFilter(regex: RegExp, type: string): boolean {
  return regex.test(type)
}

/** 判断是否为 WS 事件信封（含 string type 字段） */
function isWSEnvelope(value: unknown): value is WSEventEnvelope {
  return (
    value !== null &&
    typeof value === 'object' &&
    typeof (value as { type?: unknown }).type === 'string'
  )
}

/** 是否为 thenable（Promise-like） */
function isThenable(value: unknown): value is PromiseLike<void> {
  return (
    value !== null &&
    typeof value === 'object' &&
    typeof (value as { then?: unknown }).then === 'function'
  )
}

/** SSE 事件转统一信封 */
function sseToEnvelope(event: SSEStreamEvent): EventEnvelope {
  let data: unknown = event.data
  if (typeof event.data === 'string' && event.data.length > 0) {
    try {
      data = JSON.parse(event.data)
    } catch {
      // 保留原始字符串
    }
  }
  return {
    type: event.event ?? 'message',
    id: event.id,
    data,
    transport: 'sse',
    raw: event,
  }
}

/** WS 信封转统一信封 */
function wsToEnvelope(envelope: WSEventEnvelope): EventEnvelope {
  return {
    type: envelope.type,
    id: envelope.seq ?? envelope.event_id,
    payload: envelope.payload,
    data: envelope.payload,
    occurredAt: envelope.occurred_at,
    traceId: envelope.trace_id,
    producer: envelope.producer,
    transport: 'ws',
    raw: envelope,
  }
}

// ============ 内部订阅者结构 ============

interface Subscriber {
  /** 订阅 filter 原文 */
  filter: string
  /** 编译后的正则 */
  filterRegex: RegExp
  /** 用户回调 */
  handler: EventHandler
  /** 仅 drop-* 策略使用的待消费队列 */
  buffer: EventEnvelope[]
  /** 累计丢弃事件数 */
  droppedCount: number
  /** 是否正在异步消费，避免重复调度 */
  consuming: boolean
  /** 是否已触发背压警告（用于滞回） */
  warningTriggered: boolean
  /** 是否已取消订阅 */
  unsubscribed: boolean
}

// ============ createEventManager ============

/**
 * 创建统一事件订阅管理器。
 *
 * @example
 * ```ts
 * const manager = createEventManager({
 *   wsUrl: 'wss://api.example.com/events',
 *   sseUrl: 'https://api.example.com/events/sse',
 *   transport: 'auto',
 *   token: 'my-token',
 *   backpressure: { strategy: 'drop-oldest', maxBufferSize: 500, warningThreshold: 400 },
 *   onStateChange: (state) => console.log('state:', state),
 * })
 *
 * const handle = manager.subscribe('notification.*', (event) => {
 *   console.log('notification:', event.type, event.data)
 * })
 *
 * // 同一事件可被多个订阅者接收
 * manager.subscribe('notification.*', (event) => {
 *   console.log('another listener:', event.type)
 * })
 *
 * // 取消订阅
 * handle.unsubscribe()
 *
 * // 关闭管理器
 * manager.close()
 * ```
 */
export function createEventManager(options: EventManagerOptions): EventManager {
  const {
    wsUrl,
    sseUrl,
    transport = 'auto',
    token,
    getToken,
    lastEventId,
    after,
    backpressure = {},
    onBackpressureWarning,
    onStateChange,
    onError,
    wsFallbackThreshold = 1,
    sseReconnectDelayMs = 1000,
    sseMaxReconnectDelayMs = 30000,
    wsMinReconnectDelay = 1000,
    wsMaxReconnectDelay = 30000,
    wsHeartbeatInterval = 0,
    wsHeartbeatTimeout = 60000,
    sseHeaders,
  } = options

  const {
    strategy = 'buffer' as BackpressureStrategy,
    maxBufferSize = 1000,
    warningThreshold = Math.max(1, Math.floor(maxBufferSize * 0.8)),
    warningResetRatio = 0.5,
  } = backpressure

  // filter -> 订阅者集合
  const subscribers = new Map<string, Set<Subscriber>>()
  // 总订阅者数（含同一 filter 的多个订阅者）
  let subscriberCount = 0

  let state: ConnectionState = 'disconnected'
  let activeTransport: 'sse' | 'ws' | null = null
  let sseController: AbortController | null = null
  let wsClient: WSClient | null = null
  let wsFailureCount = 0
  let wsEverConnected = false
  let fallbackToSse = false
  let closed = false

  // ============ 状态管理 ============

  const setState = (next: ConnectionState): void => {
    if (closed && next !== 'disconnected') return
    const prev = state
    if (prev === next) return
    state = next
    try {
      onStateChange?.(next, prev)
    } catch (err) {
      // onStateChange 异常不应影响内部逻辑
      onError?.(err)
    }
  }

  // ============ 订阅者管理 ============

  const addSubscriber = (filter: string, handler: EventHandler): Subscriber => {
    const sub: Subscriber = {
      filter,
      filterRegex: filterToRegex(filter),
      handler,
      buffer: [],
      droppedCount: 0,
      consuming: false,
      warningTriggered: false,
      unsubscribed: false,
    }
    let set = subscribers.get(filter)
    if (!set) {
      set = new Set()
      subscribers.set(filter, set)
    }
    set.add(sub)
    subscriberCount++
    return sub
  }

  const removeSubscriber = (sub: Subscriber): void => {
    sub.unsubscribed = true
    sub.buffer.length = 0
    const set = subscribers.get(sub.filter)
    if (set) {
      if (set.delete(sub)) {
        subscriberCount--
      }
      if (set.size === 0) {
        subscribers.delete(sub.filter)
      }
    }
    if (subscriberCount === 0) {
      stopTransport()
    }
  }

  // ============ 事件派发 ============

  const dispatch = (envelope: EventEnvelope): void => {
    if (closed) return
    for (const set of subscribers.values()) {
      for (const sub of set) {
        if (sub.unsubscribed) continue
        if (matchFilter(sub.filterRegex, envelope.type)) {
          deliver(sub, envelope)
        }
      }
    }
  }

  const emitWarning = (sub: Subscriber): void => {
    if (!onBackpressureWarning) return
    const stats: BackpressureWarningStats = {
      filter: sub.filter,
      bufferSize: sub.buffer.length,
      maxBufferSize,
      strategy,
      droppedCount: sub.droppedCount,
      timestamp: Date.now(),
    }
    try {
      onBackpressureWarning(stats)
    } catch (err) {
      onError?.(err)
    }
  }

  const deliver = (sub: Subscriber, event: EventEnvelope): void => {
    // buffer 策略：直接同步分发，不缓冲（无背压控制）
    if (strategy === 'buffer') {
      try {
        const result = sub.handler(event)
        if (isThenable(result)) {
          // 异步 handler，但 buffer 策略下不参与背压控制
          result.catch((err: unknown) => onError?.(err))
        }
      } catch (err) {
        onError?.(err)
      }
      return
    }

    // drop-* 策略：先入队，再按策略丢弃，最后异步消费
    sub.buffer.push(event)

    let droppedThisRound = 0
    while (sub.buffer.length > maxBufferSize) {
      if (strategy === 'drop-oldest') {
        sub.buffer.shift()
      } else {
        // drop-newest：丢弃刚入队的最新事件
        sub.buffer.pop()
      }
      sub.droppedCount++
      droppedThisRound++
    }

    const resetThreshold = Math.max(1, Math.floor(warningThreshold * warningResetRatio))
    // 阈值警告：首次达到 warningThreshold 时触发（滞回防抖）
    if (sub.buffer.length >= warningThreshold && !sub.warningTriggered) {
      sub.warningTriggered = true
      emitWarning(sub)
    } else if (sub.buffer.length < resetThreshold && sub.warningTriggered) {
      // 滞回：缓冲降到 resetThreshold 以下时重置，允许下次再次触发
      sub.warningTriggered = false
    }

    // 丢弃事件时报告进度（每次丢弃都报告，便于上层跟踪 droppedCount）
    if (droppedThisRound > 0) {
      emitWarning(sub)
    }

    scheduleConsume(sub)
  }

  const scheduleConsume = (sub: Subscriber): void => {
    if (sub.consuming) return
    sub.consuming = true
    // 使用 microtask 异步消费，避免同步递归与阻塞派发循环
    // consuming 在 consumeLoop 完成后才重置，防止异步 handler 期间并发派发
    queueMicrotask(() => {
      void consumeLoop(sub).finally(() => {
        sub.consuming = false
        // 消费期间若有新事件入队，重新调度一轮
        if (sub.buffer.length > 0 && !sub.unsubscribed && !closed) {
          scheduleConsume(sub)
        }
      })
    })
  }

  const consumeLoop = async (sub: Subscriber): Promise<void> => {
    while (sub.buffer.length > 0 && !sub.unsubscribed && !closed) {
      const event = sub.buffer[0]
      try {
        const result = sub.handler(event)
        if (isThenable(result)) {
          await result
        }
      } catch (err) {
        onError?.(err)
      }
      // 仅消费 buffer[0]；如果 handler 内部触发了新事件并递归入队，
      // 这里仍按当前快照处理。shift 在 try/catch 外，确保即使 handler
      // 抛错也能继续处理后续事件。
      sub.buffer.shift()

      const resetThreshold = Math.max(1, Math.floor(warningThreshold * warningResetRatio))
      if (sub.buffer.length < resetThreshold && sub.warningTriggered) {
        sub.warningTriggered = false
      }
    }
  }

  // ============ 传输层 ============

  const startTransport = (): void => {
    if (closed) return
    // 已有活动传输（连接中/已连接/重连中）则不重复启动；
    // disconnected/error 状态下允许重启（如 SSE 流自然结束后的再订阅）
    if (state === 'connecting' || state === 'connected' || state === 'reconnecting') {
      return
    }
    const effective = resolveTransport()
    if (effective === 'ws') {
      startWS()
    } else {
      startSSE()
    }
  }

  const resolveTransport = (): 'sse' | 'ws' => {
    if (transport === 'sse') return 'sse'
    if (transport === 'ws') return 'ws'
    // auto：优先 WS；若已降级或无 wsUrl，则用 SSE
    if (fallbackToSse || !wsUrl) {
      if (!sseUrl) {
        throw new Error('auto transport requires sseUrl when ws is unavailable')
      }
      return 'sse'
    }
    return 'ws'
  }

  const startSSE = (): void => {
    if (!sseUrl) {
      throw new Error('sseUrl is required for sse transport')
    }
    // 终止已有 SSE 流
    if (sseController) {
      sseController.abort()
    }
    // 用本地变量捕获 controller，避免 async 函数运行期间 sseController 被置 null
    const controller = new AbortController()
    sseController = controller
    activeTransport = 'sse'
    setState('connecting')

    const headers = new Headers(sseHeaders)
    if (lastEventId) headers.set('Last-Event-ID', lastEventId)

    void (async () => {
      try {
        for await (const event of createSSEStream(sseUrl, {
          token,
          getToken,
          headers,
          signal: controller.signal,
          reconnectDelayMs: sseReconnectDelayMs,
          maxReconnectDelayMs: sseMaxReconnectDelayMs,
          onError: (err) => {
            onError?.(err)
            if (!closed && activeTransport === 'sse') {
              setState('reconnecting')
            }
          },
        })) {
          if (controller.signal.aborted || closed) break
          dispatch(sseToEnvelope(event))
        }
        // 流自然结束（非 abort）：状态置 disconnected，但保留 activeTransport
        // 以便上层查询"上次使用的传输"；实际重连由底层 createSSEStream 负责，
        // 若流真的结束（服务器主动关闭），用户可调用 close 后重新 subscribe
        if (!controller.signal.aborted && !closed) {
          setState('disconnected')
        }
      } catch (err) {
        if (!controller.signal.aborted && !closed) {
          onError?.(err)
          setState('error')
        }
      }
      // 不在 finally 中清除 activeTransport；由 stopTransport/close 负责
    })()
  }

  const startWS = (): void => {
    if (!wsUrl) {
      throw new Error('wsUrl is required for ws transport')
    }
    if (wsClient) {
      wsClient.close()
    }
    wsFailureCount = 0
    wsEverConnected = false
    activeTransport = 'ws'
    setState('connecting')

    wsClient = createWSClient(wsUrl, {
      token,
      getToken,
      after,
      reconnect: true,
      minReconnectDelay: wsMinReconnectDelay,
      maxReconnectDelay: wsMaxReconnectDelay,
      heartbeatInterval: wsHeartbeatInterval,
      heartbeatTimeout: wsHeartbeatTimeout,
      onOpen: () => {
        wsEverConnected = true
        wsFailureCount = 0
        setState('connected')
      },
      onError: (err) => {
        onError?.(err)
        // auto 模式且从未成功连接过：累计失败次数，达到阈值则降级到 SSE
        if (
          transport === 'auto' &&
          !wsEverConnected &&
          !fallbackToSse &&
          !closed
        ) {
          wsFailureCount++
          if (wsFailureCount >= wsFallbackThreshold) {
            fallbackToSse = true
            // 主动关闭 WS，阻止其继续重连
            wsClient?.close()
            wsClient = null
            activeTransport = null
            if (sseUrl) {
              startSSE()
            } else {
              setState('error')
            }
          }
        }
      },
      onClose: () => {
        if (activeTransport === 'ws' && !closed && !fallbackToSse) {
          setState('reconnecting')
        }
      },
      onMessage: (msg) => {
        if (isWSEnvelope(msg)) {
          dispatch(wsToEnvelope(msg))
        }
      },
    })
  }

  const stopTransport = (): void => {
    if (wsClient) {
      wsClient.close()
      wsClient = null
    }
    if (sseController) {
      sseController.abort()
      sseController = null
    }
    activeTransport = null
    if (!closed) {
      setState('disconnected')
    }
  }

  // ============ 对外 API ============

  const subscribe = (
    filter: string,
    handler: EventHandler,
  ): EventSubscriptionHandle => {
    if (closed) {
      throw new Error('EventManager is closed; cannot subscribe')
    }
    if (typeof filter !== 'string' || filter.length === 0) {
      throw new Error('filter must be a non-empty string')
    }
    if (typeof handler !== 'function') {
      throw new Error('handler must be a function')
    }
    const wasEmpty = subscriberCount === 0
    const sub = addSubscriber(filter, handler)
    if (wasEmpty) {
      startTransport()
    }
    return {
      unsubscribe: () => removeSubscriber(sub),
      filter,
    }
  }

  const getState = (): ConnectionState => state

  const getActiveTransport = (): 'sse' | 'ws' | null => activeTransport

  const getSubscriberCount = (): number => subscriberCount

  const close = (): void => {
    if (closed) return
    closed = true
    // 标记所有订阅者为已取消，清空缓冲
    for (const set of subscribers.values()) {
      for (const sub of set) {
        sub.unsubscribed = true
        sub.buffer.length = 0
      }
    }
    subscribers.clear()
    subscriberCount = 0
    // 关闭传输
    if (wsClient) {
      wsClient.close()
      wsClient = null
    }
    if (sseController) {
      sseController.abort()
      sseController = null
    }
    activeTransport = null
    setState('disconnected')
  }

  return {
    subscribe,
    getState,
    getActiveTransport,
    getSubscriberCount,
    close,
  }
}

// ============ 导出工具函数（供测试与高级用户使用） ============

export { filterToRegex, matchFilter, sseToEnvelope, wsToEnvelope, isWSEnvelope }

// 重导出类型，便于 `import { EventManager } from '@workama/api-client'`
export type {
  BackpressureConfig,
  BackpressureStrategy,
  BackpressureWarningStats,
  ConnectionState,
  EventEnvelope,
  EventHandler,
  EventManager,
  EventManagerOptions,
  EventSubscriptionHandle,
  Transport,
} from './events-types'

// 兼容：允许通过 transport 字段判断
export const TRANSPORTS: ReadonlyArray<Transport> = ['sse', 'ws', 'auto']
