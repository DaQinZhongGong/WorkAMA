/**
 * WorkAMA JavaScript SDK 顶层入口。
 *
 * 统一导出主客户端、异常类型与所有响应/请求类型定义。
 */

export {
  WorkAMAClient,
  WorkAMAError,
  AuthenticationError,
  ForbiddenError,
  NotFoundError,
  RateLimitError,
  buildMultipart,
} from './client'
export type {
  WorkAMAClientOptions,
  ChatOptions,
  ChatResponse,
  ListAgentsOptions,
  ListResponse,
  CreateMemoryOptions,
  MemoryResponse,
  RecallOptions,
  RecallResponse,
  SearchOptions,
  SearchResponse,
  KnowledgeHit,
  ListWorkflowsOptions,
  ListPageOptions,
  WorkflowRunResponse,
  RawObject,
  WorkspaceOptions,
  Workflow,
  WorkflowRun,
  RunWorkflowOptions,
  KnowledgeBase,
  Document,
  IngestDocumentOptions,
  QueryResult,
  QueryResponse,
  QueryKnowledgeOptions,
  Agent,
  SendMessageOptions,
  ChatMessageResponse,
  FileMetadata,
  UploadFileOptions,
  Automation,
  Skill,
} from './types'

export const VERSION = '0.1.0'
