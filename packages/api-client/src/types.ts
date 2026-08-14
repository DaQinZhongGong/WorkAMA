/**
 * 共享类型定义。
 *
 * 本文件仅放置新增辅助函数所需的类型；既有 `index.ts` 中导出的类型
 *（ApiError、ClientOptions、WorkamaClient、SSEEvent、streamSSE 等）保持不变，
 * 避免命名冲突与重复导出。
 */

// ============ SSE 流式助手类型 ============

/** SSE 事件对象（与 index.ts 中的 SSEEvent 同构，避免 consumers 引入 index） */
export type SSEStreamEvent = {
  /** 事件数据（多行 data 用 \n 连接） */
  data: string
  /** 事件类型（event: 字段） */
  event?: string
  /** 事件 ID（id: 字段） */
  id?: string
  /** 重试间隔毫秒（retry: 字段） */
  retry?: number
}

export type SSEStreamOptions = {
  /** Bearer token，与 getToken 互斥，优先级更高 */
  token?: string
  /** 动态获取 token（例如从 sessionStorage） */
  getToken?: () => string | null
  /** 额外的请求头 */
  headers?: HeadersInit
  /** 额外的 fetch init（method/credentials/body 等） */
  init?: RequestInit
  /** 出错时回调；每次重连前都会调用 */
  onError?: (error: unknown) => void
  /** AbortSignal，用于彻底取消流 */
  signal?: AbortSignal
  /** 最大重连次数；默认 Infinity */
  maxReconnectAttempts?: number
  /** 初始重连延迟（毫秒），默认 1000 */
  reconnectDelayMs?: number
  /** 最大重连延迟（毫秒），默认 30000 */
  maxReconnectDelayMs?: number
  /** 初始 Last-Event-ID，用于断线恢复 */
  lastEventId?: string
}

/** subscribeSSE 选项：基于 SSEStreamOptions，增加 onEvent 回调 */
export type SubscribeSSEOptions = Omit<SSEStreamOptions, 'signal'> & {
  /** 收到事件时回调 */
  onEvent: (event: SSEStreamEvent) => void
  /** 流正常结束时回调 */
  onDone?: () => void
  /** 外部 AbortSignal（可选）；内部也会创建自己的 controller */
  signal?: AbortSignal
}

/** subscribeSSE 返回值 */
export type SubscribeSSEResult = {
  /** 取消订阅，终止 SSE 流 */
  unsubscribe: () => void
}

// ============ WebSocket 客户端助手类型 ============

/** WebSocket 事件信封（对应《710》§8.1 / 《720》§8） */
export type WSEventEnvelope = {
  schema_version?: string
  event_id?: string
  session_id?: string
  seq?: number
  type: string
  occurred_at?: string
  trace_id?: string
  producer?: string
  payload?: unknown
}

export type WSEventHandler = (envelope: WSEventEnvelope) => void

export type WSClientOptions = {
  /** Bearer token，与 getToken 互斥，优先级更高 */
  token?: string
  /** 动态获取 token */
  getToken?: () => string | null
  /** WebSocket 子协议 */
  protocols?: string | string[]
  /** 初始 after 游标；断线重连时会用最新 seq 覆盖 */
  after?: number
  /** 收到带 seq 的事件后是否自动发送 event.ack */
  autoAck?: boolean
  /** 心跳间隔（毫秒），默认 0 表示不启用心跳 */
  heartbeatInterval?: number
  /** 心跳超时（毫秒），默认 60000 */
  heartbeatTimeout?: number
  /** 是否自动重连，默认 true */
  reconnect?: boolean
  /** 最小重连延迟（毫秒），默认 1000 */
  minReconnectDelay?: number
  /** 最大重连延迟（毫秒），默认 30000 */
  maxReconnectDelay?: number
  /** 最大重连次数；默认 Infinity */
  maxReconnectAttempts?: number
  /** 连接打开回调 */
  onOpen?: (event: Event) => void
  /** 收到消息回调（在所有 subscribe 之前触发） */
  onMessage?: (message: unknown) => void
  /** 出错回调 */
  onError?: (event: Event | Error) => void
  /** 连接关闭回调 */
  onClose?: (event: CloseEvent) => void
  /** 背压警告回调：当缓冲事件数或字节数超过阈值时触发 */
  onBackpressureWarning?: (stats: BackpressureStats) => void
  /** 背压事件数阈值，默认 1000 */
  backpressureEventThreshold?: number
  /** 背压字节数阈值，默认 5MiB (5 * 1024 * 1024) */
  backpressureByteThreshold?: number
}

/** 背压统计信息 */
export type BackpressureStats = {
  /** 当前缓冲事件数 */
  bufferedEventCount: number
  /** 当前缓冲字节数 */
  bufferedByteCount: number
  /** 触发警告的时间戳 */
  timestamp: number
}

export type WSClient = {
  /** 主动关闭连接 */
  close: (code?: number, reason?: string) => void
  /** 发送消息（对象自动 JSON.stringify） */
  send: (message: unknown) => void
  /** 发送 event.ack */
  ack: (seq: number) => void
  /** 订阅指定类型的事件；`*` 表示所有类型 */
  subscribe: (eventType: string, handler: WSEventHandler) => void
  /** 取消订阅 */
  unsubscribe: (eventType: string, handler: WSEventHandler) => void
  /** 当前是否处于已连接状态 */
  isConnected: () => boolean
  /** 当前底层 WebSocket 实例 */
  raw: WebSocket | null
}
