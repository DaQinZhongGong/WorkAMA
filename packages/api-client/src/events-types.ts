/**
 * 统一事件订阅管理器类型定义。
 *
 * 融合 SSE 与 WebSocket 两种传输方式的事件模型，向上层提供统一的
 * `EventEnvelope` 抽象，屏蔽底层差异（SSE 的 event/data/id 字段 与
 * WS 的 type/payload/seq 字段）。
 *
 * 仅放置类型，不引入运行时依赖。
 */

import type { SSEStreamEvent, WSEventEnvelope } from './types'

/** 传输方式 */
export type Transport = 'sse' | 'ws' | 'auto'

/** 连接状态机 */
export type ConnectionState =
  | 'disconnected'
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'error'

/**
 * 背压丢弃策略。
 * - `buffer`：不丢弃，事件同步分发给 handler（默认，适合快消费者）
 * - `drop-oldest`：队列满时丢弃最旧事件，保留最新事件
 * - `drop-newest`：队列满时丢弃最新事件，保留最旧事件
 */
export type BackpressureStrategy = 'buffer' | 'drop-oldest' | 'drop-newest'

/**
 * 统一事件信封。
 *
 * 字段映射规则：
 * - 来自 SSE：`type` = `event` 字段（缺省为 `message`）；`id` = `id` 字段；
 *   `data` = `data` 字段尝试 JSON.parse，失败则保留字符串。
 * - 来自 WS：`type` = `envelope.type`；`id` = `seq` 或 `event_id`；
 *   `payload` / `data` = `envelope.payload`。
 */
export type EventEnvelope = {
  /** 事件类型（如 `notification.created`） */
  type: string
  /** 事件 ID（SSE 的 id 或 WS 的 seq/event_id） */
  id?: string | number
  /** 事件负载（WS 的 payload 字段） */
  payload?: unknown
  /** 数据字段（SSE 的 data 解析结果，或 WS 的 payload） */
  data?: unknown
  /** 发生时间（ISO 字符串） */
  occurredAt?: string
  /** trace id */
  traceId?: string
  /** 事件生产者 */
  producer?: string
  /** 来源传输 */
  transport: 'sse' | 'ws'
  /** 原始事件对象（SSEStreamEvent 或 WSEventEnvelope） */
  raw: SSEStreamEvent | WSEventEnvelope
}

/** 事件处理器；可返回 Promise 表示异步消费（参与背压控制） */
export type EventHandler = (event: EventEnvelope) => void | Promise<void>

/** 背压配置 */
export type BackpressureConfig = {
  /** 策略，默认 `buffer` */
  strategy?: BackpressureStrategy
  /** 每个订阅者的最大缓冲队列长度（仅 `drop-*` 策略生效） */
  maxBufferSize?: number
  /** 触发背压警告的阈值（缓冲长度达到此值时调用 `onBackpressureWarning`） */
  warningThreshold?: number
  /** 背压警告解除的滞回下限（缓冲降到此值以下时重置警告，避免抖动） */
  warningResetRatio?: number
}

/** 背压警告统计 */
export type BackpressureWarningStats = {
  /** 触发警告的订阅 filter */
  filter: string
  /** 当前缓冲长度 */
  bufferSize: number
  /** 配置的最大缓冲长度 */
  maxBufferSize: number
  /** 策略 */
  strategy: BackpressureStrategy
  /** 累计丢弃事件数 */
  droppedCount: number
  /** 触发时间戳 */
  timestamp: number
}

/**
 * 事件管理器选项。
 *
 * `wsUrl` 与 `sseUrl` 至少提供一个；`auto` 模式下两者都提供以支持降级。
 */
export type EventManagerOptions = {
  /** WebSocket 端点 URL（`auto` / `ws` 模式必需） */
  wsUrl?: string
  /** SSE 端点 URL（`auto` 降级 / `sse` 模式必需） */
  sseUrl?: string
  /** 传输方式，默认 `auto` */
  transport?: Transport
  /** Bearer token，与 `getToken` 互斥，优先级更高 */
  token?: string
  /** 动态获取 token */
  getToken?: () => string | null
  /** 初始 SSE Last-Event-ID（用于断线恢复时的事件补发） */
  lastEventId?: string
  /** 初始 WS after 游标（用于断线恢复时的事件补发） */
  after?: number
  /** 背压配置 */
  backpressure?: BackpressureConfig
  /** 背压警告回调 */
  onBackpressureWarning?: (stats: BackpressureWarningStats) => void
  /** 连接状态变化回调 */
  onStateChange?: (state: ConnectionState, previous: ConnectionState) => void
  /** 出错回调（不导致断开的中间错误） */
  onError?: (error: unknown) => void
  /** `auto` 模式下 WS 连续失败几次后降级到 SSE，默认 1 */
  wsFallbackThreshold?: number
  /** SSE 重连初始延迟（毫秒），默认 1000 */
  sseReconnectDelayMs?: number
  /** SSE 最大重连延迟（毫秒），默认 30000 */
  sseMaxReconnectDelayMs?: number
  /** WS 最小重连延迟（毫秒），默认 1000 */
  wsMinReconnectDelay?: number
  /** WS 最大重连延迟（毫秒），默认 30000 */
  wsMaxReconnectDelay?: number
  /** WS 心跳间隔（毫秒），默认 0 表示不启用 */
  wsHeartbeatInterval?: number
  /** WS 心跳超时（毫秒），默认 60000 */
  wsHeartbeatTimeout?: number
  /** 额外的 SSE 请求头 */
  sseHeaders?: HeadersInit
}

/** 订阅句柄 */
export type EventSubscriptionHandle = {
  /** 取消订阅 */
  unsubscribe: () => void
  /** 当前订阅的 filter */
  filter: string
}

/** 事件管理器实例 */
export type EventManager = {
  /** 订阅事件；`filter` 支持 `notification.*`、`workspace.*`、`*` 等 glob 模式 */
  subscribe: (filter: string, handler: EventHandler) => EventSubscriptionHandle
  /** 当前连接状态 */
  getState: () => ConnectionState
  /** 当前活动传输（无连接时为 null） */
  getActiveTransport: () => 'sse' | 'ws' | null
  /** 当前订阅者总数（含同一 filter 的多个订阅者） */
  getSubscriberCount: () => number
  /** 关闭管理器，断开传输并清除所有订阅 */
  close: () => void
}
