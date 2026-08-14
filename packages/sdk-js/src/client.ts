/**
 * WorkAMA JavaScript SDK 主客户端实现。
 *
 * 基于 fetch API + AbortController 实现超时控制，
 * 不引入 axios 等第三方 HTTP 库。异常类一并定义在此文件，
 * 由顶层 index.ts 统一 re-export。
 */

import type {
  Agent,
  Automation,
  ChatMessageResponse,
  ChatOptions,
  ChatResponse,
  CreateMemoryOptions,
  Document,
  FileMetadata,
  IngestDocumentOptions,
  KnowledgeBase,
  ListAgentsOptions,
  ListPageOptions,
  ListResponse,
  MemoryResponse,
  QueryKnowledgeOptions,
  QueryResponse,
  RawObject,
  RecallOptions,
  RecallResponse,
  RunWorkflowOptions,
  SearchOptions,
  SearchResponse,
  SendMessageOptions,
  Skill,
  UploadFileOptions,
  WorkAMAClientOptions,
  Workflow,
  WorkflowRunResponse,
  WorkspaceOptions,
} from './types'

// 默认 User-Agent，便于服务端识别 SDK 流量
const USER_AGENT = 'workama-sdk-js/0.1.0'

// workspace 隔离头
const WORKSPACE_HEADER = 'X-Workspace-Id'

// ---------------------------------------------------------------------------
// 异常定义
// ---------------------------------------------------------------------------

/** 所有 SDK 异常的基类。 */
export class WorkAMAError extends Error {
  /** HTTP 状态码（网络层错误可能为 undefined）。 */
  readonly statusCode?: number
  /** 服务端返回的原始响应体。 */
  readonly body: unknown

  constructor(message: string, statusCode?: number, body?: unknown) {
    super(message)
    this.name = 'WorkAMAError'
    this.statusCode = statusCode
    this.body = body
  }
}

/** 鉴权失败（HTTP 401）。 */
export class AuthenticationError extends WorkAMAError {
  constructor(message: string, statusCode?: number, body?: unknown) {
    super(message, statusCode, body)
    this.name = 'AuthenticationError'
  }
}

/** 权限不足（HTTP 403）。 */
export class ForbiddenError extends WorkAMAError {
  constructor(message: string, statusCode?: number, body?: unknown) {
    super(message, statusCode, body)
    this.name = 'ForbiddenError'
  }
}

/** 资源不存在（HTTP 404）。 */
export class NotFoundError extends WorkAMAError {
  constructor(message: string, statusCode?: number, body?: unknown) {
    super(message, statusCode, body)
    this.name = 'NotFoundError'
  }
}

/** 触发限流（HTTP 429）。 */
export class RateLimitError extends WorkAMAError {
  constructor(message: string, statusCode?: number, body?: unknown) {
    super(message, statusCode, body)
    this.name = 'RateLimitError'
  }
}

// ---------------------------------------------------------------------------
// 主客户端
// ---------------------------------------------------------------------------

export class WorkAMAClient {
  /** 平台 API 基地址（已去除末尾斜杠）。 */
  readonly baseUrl: string
  /** API Key（可能为 undefined）。 */
  readonly apiKey?: string
  /** Bearer Token（可能为 undefined）。 */
  readonly accessToken?: string
  /** 默认超时毫秒数。 */
  readonly timeout: number

  constructor(opts: WorkAMAClientOptions) {
    if (!opts || !opts.baseUrl) {
      throw new WorkAMAError('baseUrl is required')
    }
    this.baseUrl = opts.baseUrl.replace(/\/+$/, '')
    this.apiKey = opts.apiKey
    this.accessToken = opts.accessToken
    this.timeout = opts.timeout ?? 30000
  }

  // ------------------------------------------------------------------
  // 公开 API
  // ------------------------------------------------------------------

  /** 与指定 Agent 进行对话。 */
  async chat(
    agentId: string,
    message: string,
    options: ChatOptions = {},
  ): Promise<ChatResponse> {
    const body: RawBody = {
      message,
      stream: options.stream ?? false,
    }
    if (options.sessionId !== undefined) {
      body.session_id = options.sessionId
    }
    return this.request<ChatResponse>(
      'POST',
      `/api/v1/agents/${agentId}/chat`,
      body,
      undefined,
      options.timeoutMs,
      options.workspaceId,
    )
  }

  /** 分页列出 Agent（实际路由 /api/v1/assistants）。 */
  async listAgents(options: ListAgentsOptions = {}): Promise<ListResponse> {
    const params: Record<string, unknown> = {
      limit: options.limit ?? 20,
    }
    if (options.cursor !== undefined) {
      params.cursor = options.cursor
    }
    return this.request<ListResponse>(
      'GET',
      '/api/v1/assistants',
      undefined,
      params,
      options.timeoutMs,
      options.workspaceId,
    )
  }

  /**
   * 创建 Agent（助手），POST /api/v1/assistants。
   *
   * payload 至少包含 name 与 system_prompt，其余字段按平台 schema 透传。
   */
  async createAgent(
    payload: RawObject,
    options: WorkspaceOptions = {},
  ): Promise<Agent> {
    return this.request<Agent>(
      'POST',
      '/api/v1/assistants',
      payload,
      undefined,
      options.timeoutMs,
      options.workspaceId,
    )
  }

  /**
   * 向 Agent 发送消息并同步获取回复，POST /api/v1/assistants/{id}/run。
   *
   * conversationId 会写入 metadata.conversation_id 以便多轮续接。
   */
  async sendChatMessage(
    agentId: string,
    message: string,
    options: SendMessageOptions = {},
  ): Promise<ChatMessageResponse> {
    const body: RawBody = { user_message: message }
    const metadata: RawBody = {}
    if (options.conversationId !== undefined) {
      metadata.conversation_id = options.conversationId
    }
    if (Object.keys(metadata).length > 0) {
      body.metadata = metadata
    }
    return this.request<ChatMessageResponse>(
      'POST',
      `/api/v1/assistants/${agentId}/run`,
      body,
      undefined,
      options.timeoutMs,
      options.workspaceId,
    )
  }

  /** 写入一条记忆向量。 */
  async createMemory(
    content: string,
    options: CreateMemoryOptions = {},
  ): Promise<MemoryResponse> {
    const body: RawBody = {
      content,
      importance: options.importance ?? 3,
    }
    if (options.metadata !== undefined) {
      body.metadata = options.metadata
    }
    return this.request<MemoryResponse>(
      'POST',
      '/api/v1/memory-vectors',
      body,
      undefined,
      options.timeoutMs,
      options.workspaceId,
    )
  }

  /** 根据 query 检索相关记忆。 */
  async recallMemory(
    query: string,
    options: RecallOptions = {},
  ): Promise<RecallResponse> {
    return this.request<RecallResponse>(
      'POST',
      '/api/v1/memory-vectors/recall',
      { query, limit: options.limit ?? 5 },
      undefined,
      options.timeoutMs,
      options.workspaceId,
    )
  }

  /** 搜索知识库。 */
  async searchKnowledge(
    query: string,
    options: SearchOptions = {},
  ): Promise<SearchResponse> {
    const body: RawBody = { query, limit: options.limit ?? 10 }
    if (options.datasetId !== undefined) {
      body.dataset_id = options.datasetId
    }
    return this.request<SearchResponse>(
      'POST',
      '/api/v1/knowledge/search',
      body,
      undefined,
      options.timeoutMs,
      options.workspaceId,
    )
  }

  // ------------------------------------------------------------------
  // 工作流
  // ------------------------------------------------------------------

  /** 列出工作流，GET /api/v1/workflows。 */
  async listWorkflows(options: ListPageOptions = {}): Promise<ListResponse> {
    const params: Record<string, unknown> = {
      limit: options.limit ?? 20,
    }
    if (options.cursor !== undefined) {
      params.cursor = options.cursor
    }
    return this.request<ListResponse>(
      'GET',
      '/api/v1/workflows',
      undefined,
      params,
      options.timeoutMs,
      options.workspaceId,
    )
  }

  /**
   * 创建工作流，POST /api/v1/workflows。
   *
   * payload 至少包含 name 与 graph（节点/边定义）。
   */
  async createWorkflow(
    payload: RawObject,
    options: WorkspaceOptions = {},
  ): Promise<Workflow> {
    return this.request<Workflow>(
      'POST',
      '/api/v1/workflows',
      payload,
      undefined,
      options.timeoutMs,
      options.workspaceId,
    )
  }

  /**
   * 执行指定工作流，POST /api/v1/workflows/{id}/runs。
   *
   * 平台运行 schema 使用 input 字段，SDK 将 inputs 包装为
   * { input: inputs } 下发，并支持幂等键透传。
   */
  async runWorkflow(
    workflowId: string,
    inputs: RawObject,
    options: RunWorkflowOptions = {},
  ): Promise<WorkflowRunResponse> {
    const extraHeaders: Record<string, string> = {}
    if (options.idempotencyKey) {
      extraHeaders['Idempotency-Key'] = options.idempotencyKey
    }
    return this.request<WorkflowRunResponse>(
      'POST',
      `/api/v1/workflows/${workflowId}/runs`,
      { input: inputs },
      undefined,
      options.timeoutMs,
      options.workspaceId,
      extraHeaders,
    )
  }

  // ------------------------------------------------------------------
  // 知识库
  // ------------------------------------------------------------------

  /** 列出知识库，GET /api/v1/knowledge-bases。 */
  async listKnowledgeBases(
    options: ListPageOptions = {},
  ): Promise<ListResponse> {
    const params: Record<string, unknown> = {
      limit: options.limit ?? 20,
    }
    if (options.cursor !== undefined) {
      params.cursor = options.cursor
    }
    return this.request<ListResponse>(
      'GET',
      '/api/v1/knowledge-bases',
      undefined,
      params,
      options.timeoutMs,
      options.workspaceId,
    )
  }

  /** 创建知识库，POST /api/v1/knowledge-bases。 */
  async createKnowledgeBase(
    payload: RawObject,
    options: WorkspaceOptions = {},
  ): Promise<KnowledgeBase> {
    return this.request<KnowledgeBase>(
      'POST',
      '/api/v1/knowledge-bases',
      payload,
      undefined,
      options.timeoutMs,
      options.workspaceId,
    )
  }

  /**
   * 向知识库写入文档，POST /api/v1/knowledge-bases/{id}/documents。
   *
   * 平台 schema 要求 title；缺失时由 SDK 生成默认值。
   * options.metadata 中的 title/source_type/source_url 会提升到顶层字段。
   */
  async ingestDocument(
    kbId: string,
    content: string,
    options: IngestDocumentOptions = {},
  ): Promise<Document> {
    const body: RawBody = { content }
    const meta: RawObject = options.metadata ? { ...options.metadata } : {}
    // title 优先取 options.title，其次取 metadata.title，缺失则自动生成
    const titleFromMeta = typeof meta.title === 'string' ? meta.title : undefined
    body.title = options.title || titleFromMeta || `doc-${randomHex(8)}`
    delete meta.title
    if (options.source_type !== undefined) {
      body.source_type = options.source_type
    }
    if (options.source_url !== undefined) {
      body.source_url = options.source_url
    }
    body.metadata = meta
    return this.request<Document>(
      'POST',
      `/api/v1/knowledge-bases/${kbId}/documents`,
      body,
      undefined,
      options.timeoutMs,
      options.workspaceId,
    )
  }

  /**
   * 检索知识库，POST /api/v1/knowledge-bases/{id}/rag/query。
   *
   * 返回体包含 results（含 similarity 相关性分数）。
   */
  async queryKnowledge(
    kbId: string,
    query: string,
    options: QueryKnowledgeOptions = {},
  ): Promise<QueryResponse> {
    return this.request<QueryResponse>(
      'POST',
      `/api/v1/knowledge-bases/${kbId}/rag/query`,
      { query, top_k: options.topK ?? 5 },
      undefined,
      options.timeoutMs,
      options.workspaceId,
    )
  }

  // ------------------------------------------------------------------
  // 文件
  // ------------------------------------------------------------------

  /** 列出文件元数据，GET /api/v1/files。 */
  async listFiles(options: ListPageOptions = {}): Promise<ListResponse> {
    const params: Record<string, unknown> = {
      limit: options.limit ?? 20,
    }
    if (options.cursor !== undefined) {
      params.cursor = options.cursor
    }
    return this.request<ListResponse>(
      'GET',
      '/api/v1/files',
      undefined,
      params,
      options.timeoutMs,
      options.workspaceId,
    )
  }

  /**
   * 上传文件，POST /api/v1/files/upload（multipart/form-data）。
   *
   * @param filename 文件名（用于类型推断）
   * @param contentBytes 文件原始字节
   * @param options.kind 可选，文件类型；为空时由服务端推断
   * @param options.metadata 可选，以 JSON 字符串作为 form 字段下发
   */
  async uploadFile(
    filename: string,
    contentBytes: Uint8Array | ArrayBuffer | Blob,
    options: UploadFileOptions = {},
  ): Promise<FileMetadata> {
    const { contentType, body } = buildMultipart(
      filename,
      contentBytes,
      options.kind,
      options.metadata,
    )
    return this.request<FileMetadata>(
      'POST',
      '/api/v1/files/upload',
      undefined,
      undefined,
      options.timeoutMs,
      options.workspaceId,
      { 'Content-Type': contentType },
      body,
    )
  }

  // ------------------------------------------------------------------
  // 自动化
  // ------------------------------------------------------------------

  /** 列出自动化触发器，GET /api/v1/automations/v2/triggers。 */
  async listAutomations(
    options: ListPageOptions = {},
  ): Promise<ListResponse> {
    const params: Record<string, unknown> = {
      limit: options.limit ?? 20,
    }
    if (options.cursor !== undefined) {
      params.cursor = options.cursor
    }
    return this.request<ListResponse>(
      'GET',
      '/api/v1/automations/v2/triggers',
      undefined,
      params,
      options.timeoutMs,
      options.workspaceId,
    )
  }

  /** 创建自动化触发器，POST /api/v1/automations/v2/triggers。 */
  async createAutomation(
    payload: RawObject,
    options: WorkspaceOptions = {},
  ): Promise<Automation> {
    return this.request<Automation>(
      'POST',
      '/api/v1/automations/v2/triggers',
      payload,
      undefined,
      options.timeoutMs,
      options.workspaceId,
    )
  }

  // ------------------------------------------------------------------
  // 技能
  // ------------------------------------------------------------------

  /** 列出技能，GET /api/v1/skills。 */
  async listSkills(options: ListPageOptions = {}): Promise<ListResponse> {
    const params: Record<string, unknown> = {
      limit: options.limit ?? 20,
    }
    if (options.cursor !== undefined) {
      params.cursor = options.cursor
    }
    return this.request<ListResponse>(
      'GET',
      '/api/v1/skills',
      undefined,
      params,
      options.timeoutMs,
      options.workspaceId,
    )
  }

  /** 订阅/安装市场技能，POST /api/v1/skills/marketplace/{id}/subscribe。 */
  async installSkill(
    skillId: string,
    options: WorkspaceOptions = {},
  ): Promise<Skill> {
    return this.request<Skill>(
      'POST',
      `/api/v1/skills/marketplace/${skillId}/subscribe`,
      {},
      undefined,
      options.timeoutMs,
      options.workspaceId,
    )
  }

  /** 清理客户端持有的资源。当前实现为 no-op，预留以保持向前兼容。 */
  close(): void {
    // fetch 每次请求都会自行关闭，无需额外清理
  }

  // ------------------------------------------------------------------
  // 内部实现
  // ------------------------------------------------------------------

  /** 构造请求头，附加鉴权信息。 */
  private buildHeaders(): Record<string, string> {
    const headers: Record<string, string> = {
      'User-Agent': USER_AGENT,
      Accept: 'application/json',
    }
    if (this.accessToken) {
      headers['Authorization'] = `Bearer ${this.accessToken}`
    } else if (this.apiKey) {
      headers['X-WorkAMA-API-Key'] = this.apiKey
    }
    return headers
  }

  /**
   * 执行一次 HTTP 请求并返回解析后的 JSON。
   *
   * @param workspaceId 可选，附加 X-Workspace-Id 头做工作空间隔离
   * @param extraHeaders 可选，附加/覆盖请求头（如 Idempotency-Key）
   * @param rawBody 可选，原始字节请求体（用于 multipart 上传）；与 body 互斥
   *
   * @throws AuthenticationError 401
   * @throws ForbiddenError 403
   * @throws NotFoundError 404
   * @throws RateLimitError 429
   * @throws WorkAMAError 其他 HTTP/网络/解析错误
   */
  private async request<T>(
    method: string,
    path: string,
    body?: RawBody,
    params?: Record<string, unknown>,
    timeoutMsOverride?: number,
    workspaceId?: string,
    extraHeaders?: Record<string, string>,
    rawBody?: BodyInit,
  ): Promise<T> {
    let url = this.baseUrl + path
    if (params) {
      const filtered: Record<string, string> = {}
      Object.entries(params).forEach(([k, v]) => {
        if (v !== undefined && v !== null) {
          filtered[k] = String(v)
        }
      })
      const qs = new URLSearchParams(filtered).toString()
      if (qs) {
        url = `${url}?${qs}`
      }
    }

    const headers = this.buildHeaders()
    if (workspaceId) {
      headers[WORKSPACE_HEADER] = workspaceId
    }
    if (extraHeaders) {
      for (const [k, v] of Object.entries(extraHeaders)) {
        headers[k] = v
      }
    }

    let payload: BodyInit | undefined
    if (rawBody !== undefined) {
      // 原始字节体（multipart 等）；Content-Type 已由 extraHeaders 设置
      payload = rawBody
    } else if (body !== undefined) {
      payload = JSON.stringify(body)
      headers['Content-Type'] = 'application/json'
    }

    // AbortController 实现超时控制
    const controller = new AbortController()
    const timeoutMs = timeoutMsOverride ?? this.timeout
    const timer = setTimeout(() => controller.abort(), timeoutMs)

    let response: Response
    try {
      response = await fetch(url, {
        method,
        headers,
        body: payload,
        signal: controller.signal,
      })
    } catch (err) {
      clearTimeout(timer)
      if (err instanceof DOMException && err.name === 'AbortError') {
        throw new WorkAMAError(
          `request timed out after ${timeoutMs}ms`,
          undefined,
          null,
        )
      }
      // 网络层错误
      const message = err instanceof Error ? err.message : String(err)
      throw new WorkAMAError(`network error: ${message}`, undefined, null)
    }
    clearTimeout(timer)

    return this.parseResponse<T>(response)
  }

  /** 解析响应：成功返回 JSON，失败按状态码映射异常。 */
  private async parseResponse<T>(response: Response): Promise<T> {
    const text = await response.text()
    let parsed: unknown = null
    if (text) {
      try {
        parsed = JSON.parse(text)
      } catch {
        parsed = text
      }
    }

    if (response.ok) {
      return (parsed ?? {}) as T
    }

    const message = this.extractMessage(parsed) || response.statusText || 'request failed'
    throw this.mapError(response.status, message, parsed)
  }

  /** 从响应体中提取错误描述。 */
  private extractMessage(body: unknown): string | null {
    if (body && typeof body === 'object') {
      const obj = body as Record<string, unknown>
      for (const key of ['message', 'detail', 'error']) {
        const val = obj[key]
        if (typeof val === 'string' && val) {
          return val
        }
      }
    }
    return null
  }

  /** 根据 HTTP 状态码构造对应异常。 */
  private mapError(statusCode: number, message: string, body: unknown): WorkAMAError {
    if (statusCode === 401) {
      return new AuthenticationError(message, statusCode, body)
    }
    if (statusCode === 403) {
      return new ForbiddenError(message, statusCode, body)
    }
    if (statusCode === 404) {
      return new NotFoundError(message, statusCode, body)
    }
    if (statusCode === 429) {
      return new RateLimitError(message, statusCode, body)
    }
    return new WorkAMAError(message, statusCode, body)
  }
}

/** 内部使用的请求体类型。 */
type RawBody = Record<string, unknown>

// ---------------------------------------------------------------------------
// 模块级辅助函数
// ---------------------------------------------------------------------------

/** 生成随机十六进制字符串（无 crypto.randomUUID 可用时的兜底实现）。 */
function randomHex(length: number): string {
  const chars = '0123456789abcdef'
  let out = ''
  for (let i = 0; i < length; i++) {
    out += chars[Math.floor(Math.random() * 16)]
  }
  return out
}

/** 生成一个 multipart/form-data 边界字符串。 */
function makeBoundary(): string {
  // 优先使用平台 crypto.randomUUID
  const uuid = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID().replace(/-/g, '')
    : randomHex(32)
  return `----WorkAMABoundary${uuid}`
}

/** 根据 filename 推断 MIME 类型（最小实现，避免引入额外依赖）。 */
function guessMime(filename: string): string {
  const lower = filename.toLowerCase()
  const ext = lower.slice(lower.lastIndexOf('.') + 1)
  const table: Record<string, string> = {
    txt: 'text/plain',
    csv: 'text/csv',
    json: 'application/json',
    md: 'text/markdown',
    html: 'text/html',
    htm: 'text/html',
    pdf: 'application/pdf',
    doc: 'application/msword',
    docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    xls: 'application/vnd.ms-excel',
    xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    ppt: 'application/vnd.ms-powerpoint',
    pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    png: 'image/png',
    jpg: 'image/jpeg',
    jpeg: 'image/jpeg',
    gif: 'image/gif',
    webp: 'image/webp',
    svg: 'image/svg+xml',
    mp3: 'audio/mpeg',
    mp4: 'video/mp4',
    zip: 'application/zip',
  }
  return table[ext] || 'application/octet-stream'
}

/** 将输入字节统一转为 Uint8Array。 */
function toBytes(input: Uint8Array | ArrayBuffer | Blob): Uint8Array {
  if (input instanceof Uint8Array) {
    return input
  }
  if (input instanceof ArrayBuffer) {
    return new Uint8Array(input)
  }
  // Blob：Node 18+ 支持 arrayBuffer()
  // 这里通过同步切片构造 Uint8Array 视图，避免异步等待
  // 注意：在浏览器/Node 中 Blob.arrayBuffer() 是异步的，调用方应在调用前自行转换
  throw new TypeError(
    'Blob input must be converted to Uint8Array/ArrayBuffer before calling toBytes',
  )
}

/**
 * 构造 multipart/form-data 请求体。
 *
 * 表单字段：file（文件）、可选 kind、可选 metadata（JSON 字符串）。
 * 返回 { contentType, body }，body 为可直接传给 fetch 的 BodyInit。
 */
export function buildMultipart(
  filename: string,
  contentBytes: Uint8Array | ArrayBuffer | Blob,
  kind?: string,
  metadata?: RawObject,
): { contentType: string; body: BodyInit } {
  const boundary = makeBoundary()
  const encoder = new TextEncoder()
  const crlf = encoder.encode('\r\n')
  const parts: Uint8Array[] = []

  const addField = (name: string, value: string) => {
    parts.push(encoder.encode(`--${boundary}`))
    parts.push(crlf)
    parts.push(encoder.encode(`Content-Disposition: form-data; name="${name}"`))
    parts.push(crlf)
    parts.push(crlf)
    parts.push(encoder.encode(value))
    parts.push(crlf)
  }

  if (kind) {
    addField('kind', kind)
  }
  if (metadata !== undefined) {
    addField('metadata', JSON.stringify(metadata))
  }

  // 文件字段
  const mime = guessMime(filename)
  const baseName = filename.slice(filename.lastIndexOf('/') + 1) || filename
  const bytes = toBytes(contentBytes)
  parts.push(encoder.encode(`--${boundary}`))
  parts.push(crlf)
  parts.push(
    encoder.encode(
      `Content-Disposition: form-data; name="file"; filename="${baseName}"`,
    ),
  )
  parts.push(crlf)
  parts.push(encoder.encode(`Content-Type: ${mime}`))
  parts.push(crlf)
  parts.push(crlf)
  parts.push(bytes)
  parts.push(crlf)
  parts.push(encoder.encode(`--${boundary}--`))
  parts.push(crlf)

  // 合并所有分片为一个 Uint8Array，并以 BodyInit 形式返回（兼容 fetch 签名）
  const total = parts.reduce((sum, p) => sum + p.length, 0)
  const body = new Uint8Array(total)
  let offset = 0
  for (const p of parts) {
    body.set(p, offset)
    offset += p.length
  }
  return { contentType: `multipart/form-data; boundary=${boundary}`, body: body as BodyInit }
}
