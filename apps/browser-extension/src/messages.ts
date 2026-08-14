// WorkAMA 浏览器插件消息协议定义
// background / content / popup 之间通过 chrome.runtime.sendMessage 通信，
// 统一的消息类型便于测试和扩展。

/** 消息类型枚举 */
export type WorkamaMessageType =
  | 'LOGIN'
  | 'CHAT'
  | 'EXTRACT_PAGE_CONTENT'
  | 'SAVE_TO_KNOWLEDGE';

/** 登录请求负载 */
export interface LoginPayload {
  email: string;
  password: string;
}

/** 登录响应数据 */
export interface LoginResponse {
  token: string;
  user?: {
    id: string;
    email: string;
  };
}

/** 聊天请求负载 */
export interface ChatPayload {
  message: string;
  conversationId?: string;
}

/** 聊天响应数据 */
export interface ChatResponse {
  reply: string;
  conversationId: string;
}

/** 提取到的页面内容 */
export interface PageContent {
  url: string;
  title: string;
  text: string;
  selection: string;
}

/** 保存到知识库的负载 */
export interface SaveToKnowledgePayload {
  title: string;
  content: string;
  source: string;
}

/** 通用消息结构 */
export interface WorkamaMessage<T = unknown> {
  type: WorkamaMessageType;
  payload: T;
}

/** background 统一返回结构 */
export interface ApiResponse<T = unknown> {
  ok: boolean;
  data?: T;
  error?: string;
}
