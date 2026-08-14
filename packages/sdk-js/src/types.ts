/**
 * WorkAMA JavaScript SDK 类型定义。
 *
 * 所有响应类型与服务端返回的 JSON 结构对齐，调用方可以直接使用这些类型。
 */

/** 通用记录类型：服务端返回的任意对象。 */
export type RawObject = Record<string, unknown>

/** Agent 对话响应。 */
export interface ChatResponse {
  agent_id?: string
  session_id?: string
  message?: string
  role?: string
  usage?: RawObject
  [key: string]: unknown
}

/** chat 方法的可选参数。 */
export interface ChatOptions extends WorkspaceOptions {
  /** 会话 ID，用于多轮对话续接。 */
  sessionId?: string
  /** 是否流式返回。 */
  stream?: boolean
}

/** 通用分页列表响应。 */
export interface ListResponse {
  items?: unknown[]
  next_cursor?: string | null
  total?: number
  [key: string]: unknown
}

/** listAgents 的可选参数。 */
export interface ListAgentsOptions extends WorkspaceOptions {
  limit?: number
  cursor?: string
}

/** 记忆响应。 */
export interface MemoryResponse {
  id?: string
  content?: string
  metadata?: RawObject
  importance?: number
  score?: number
  [key: string]: unknown
}

/** createMemory 的可选参数。 */
export interface CreateMemoryOptions extends WorkspaceOptions {
  metadata?: RawObject
  importance?: number
}

/** recallMemory 的可选参数。 */
export interface RecallOptions extends WorkspaceOptions {
  limit?: number
}

/** 记忆检索响应。 */
export interface RecallResponse {
  items?: MemoryResponse[]
  [key: string]: unknown
}

/** searchKnowledge 的可选参数。 */
export interface SearchOptions extends WorkspaceOptions {
  datasetId?: string
  limit?: number
}

/** 知识库命中条目。 */
export interface KnowledgeHit {
  id?: string
  content?: string
  score?: number
  dataset_id?: string
  metadata?: RawObject
  [key: string]: unknown
}

/** 知识搜索响应。 */
export interface SearchResponse {
  items?: KnowledgeHit[]
  total?: number
  [key: string]: unknown
}

/** listWorkflows 的可选参数（保留以保持向后兼容，等同于 ListPageOptions）。 */
export interface ListWorkflowsOptions extends ListPageOptions {}

/** 工作流执行响应。 */
export interface WorkflowRunResponse {
  run_id?: string
  status?: string
  outputs?: RawObject
  [key: string]: unknown
}

/** 构造函数参数。 */
export interface WorkAMAClientOptions {
  /** 平台 API 基地址，例如 http://localhost:20200。 */
  baseUrl: string
  /** 可选，API Key，会以 X-WorkAMA-API-Key 头部发送。 */
  apiKey?: string
  /** 可选，Bearer Token，优先级高于 apiKey。 */
  accessToken?: string
  /** 默认超时毫秒数，默认 30000。 */
  timeout?: number
}

// ---------------------------------------------------------------------------
// P2：第三方集成扩展类型
// ---------------------------------------------------------------------------

/** 工作空间隔离标识透传选项（绝大多数方法可使用）。 */
export interface WorkspaceOptions {
  /** 透传到 X-Workspace-Id 头。 */
  workspaceId?: string
  /** 覆盖默认超时（毫秒）。 */
  timeoutMs?: number
}

/** 分页 + 工作空间选项。 */
export interface ListPageOptions extends WorkspaceOptions {
  limit?: number
  cursor?: string
}

/** 工作流定义。 */
export interface Workflow {
  id?: string
  name?: string
  description?: string
  status?: string
  version?: number
  graph?: RawObject
  [key: string]: unknown
}

/** 工作流运行实例。 */
export interface WorkflowRun {
  id?: string
  run_id?: string
  workflow_id?: string
  status?: string
  input?: RawObject
  output?: RawObject
  error?: string
  [key: string]: unknown
}

/** runWorkflow 的可选参数。 */
export interface RunWorkflowOptions extends WorkspaceOptions {
  /** 幂等键，透传到 Idempotency-Key 头。 */
  idempotencyKey?: string
}

/** 知识库。 */
export interface KnowledgeBase {
  id?: string
  name?: string
  description?: string
  kind?: string
  embedding_model?: string
  chunk_size?: number
  chunk_overlap?: number
  status?: string
  [key: string]: unknown
}

/** 知识库文档。 */
export interface Document {
  id?: string
  knowledge_base_id?: string
  title?: string
  content?: string
  chunk_count?: number
  status?: string
  metadata?: RawObject
  [key: string]: unknown
}

/** ingestDocument 的可选参数。 */
export interface IngestDocumentOptions extends WorkspaceOptions {
  title?: string
  source_type?: string
  source_url?: string
  metadata?: RawObject
}

/** 知识库检索结果条目。 */
export interface QueryResult {
  id?: string
  content?: string
  score?: number
  similarity?: number
  document_id?: string
  metadata?: RawObject
  [key: string]: unknown
}

/** 知识库检索响应。 */
export interface QueryResponse {
  query?: string
  results?: QueryResult[]
  data?: QueryResult[]
  count?: number
  top_k?: number
  [key: string]: unknown
}

/** queryKnowledge 的可选参数。 */
export interface QueryKnowledgeOptions extends WorkspaceOptions {
  topK?: number
}

/** Agent（助手）。 */
export interface Agent {
  id?: string
  name?: string
  description?: string
  system_prompt?: string
  model?: string
  temperature?: number
  max_tokens?: number
  tools?: string[]
  knowledge_base_ids?: string[]
  memory_enabled?: boolean
  status?: string
  [key: string]: unknown
}

/** sendChatMessage 的可选参数。 */
export interface SendMessageOptions extends WorkspaceOptions {
  /** 会话 ID，写入 metadata.conversation_id 以便多轮续接。 */
  conversationId?: string
  model?: string
  temperature?: number
  maxTokens?: number
}

/** Agent 对话响应。 */
export interface ChatMessageResponse {
  id?: string
  run_id?: string
  assistant_id?: string
  assistant_message?: string
  message?: string
  model?: string
  tokens_used?: number
  usage?: RawObject
  [key: string]: unknown
}

/** 文件元数据。 */
export interface FileMetadata {
  id?: string
  name?: string
  kind?: string
  mime_type?: string
  size_bytes?: number
  sha256?: string
  status?: string
  [key: string]: unknown
}

/** uploadFile 的可选参数。 */
export interface UploadFileOptions extends WorkspaceOptions {
  kind?: string
  metadata?: RawObject
}

/** 自动化触发器。 */
export interface Automation {
  id?: string
  trigger_id?: string
  name?: string
  type?: string
  enabled?: boolean
  status?: string
  [key: string]: unknown
}

/** 技能。 */
export interface Skill {
  id?: string
  skill_id?: string
  name?: string
  description?: string
  version?: string
  category?: string
  status?: string
  installed?: boolean
  [key: string]: unknown
}
