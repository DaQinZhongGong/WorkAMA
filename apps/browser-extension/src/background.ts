// WorkAMA Assistant 后台 Service Worker
// 职责：
//  1. 监听 chrome.runtime.onMessage，分发 LOGIN / CHAT / EXTRACT_PAGE_CONTENT / SAVE_TO_KNOWLEDGE
//  2. 封装 workamaApiCall：自动附带 Bearer 令牌、统一错误处理
//  3. 监听 action.onClicked 打开侧边栏
//  4. 安装时创建右键菜单 "发送到 WorkAMA"

import type {
  WorkamaMessage,
  LoginPayload,
  LoginResponse,
  ChatPayload,
  SaveToKnowledgePayload,
} from './messages';

// 开发环境 API 基址；生产环境可通过构建期 env 覆盖
const API_BASE = (import.meta as any).env?.VITE_API_BASE ?? 'http://localhost:20200';
const TOKEN_KEY = 'workama_token';

/** 从 chrome.storage.local 读取访问令牌 */
export async function getAuthToken(): Promise<string | null> {
  const result = await chrome.storage.local.get(TOKEN_KEY);
  return result[TOKEN_KEY] ?? null;
}

/** 保存访问令牌到 chrome.storage.local */
export async function setAuthToken(token: string): Promise<void> {
  await chrome.storage.local.set({ [TOKEN_KEY]: token });
}

/**
 * 封装对 WorkAMA 后端 API 的调用。
 * 自动从 chrome.storage.local 读取令牌并附带 Authorization 头。
 * @param endpoint API 路径，例如 "/api/v1/auth/login"
 * @param method HTTP 方法
 * @param body 可选请求体，会被序列化为 JSON
 */
export async function workamaApiCall(
  endpoint: string,
  method: string,
  body?: unknown,
): Promise<unknown> {
  const token = await getAuthToken();
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }
  const response = await fetch(`${API_BASE}${endpoint}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    throw new Error(`WorkAMA API error: ${response.status} ${response.statusText}`);
  }
  return response.json();
}

/** 处理 LOGIN：调用登录接口并保存返回的令牌 */
export async function handleLogin(payload: LoginPayload): Promise<LoginResponse> {
  const result = (await workamaApiCall('/api/v1/auth/login', 'POST', payload)) as LoginResponse;
  if (result?.token) {
    await setAuthToken(result.token);
  }
  return result;
}

/** 处理 CHAT：转发到聊天接口 */
export async function handleChat(payload: ChatPayload): Promise<unknown> {
  return workamaApiCall('/api/v1/chat', 'POST', payload);
}

/** 处理 SAVE_TO_KNOWLEDGE：保存页面内容到知识库 */
export async function handleSaveToKnowledge(
  payload: SaveToKnowledgePayload,
): Promise<unknown> {
  return workamaApiCall('/api/v1/knowledge', 'POST', payload);
}

/**
 * 消息分发器：根据 type 路由到对应处理器。
 * 导出便于单元测试直接调用，无需走 chrome 事件通道。
 */
export async function dispatchMessage(message: WorkamaMessage): Promise<unknown> {
  switch (message.type) {
    case 'LOGIN':
      return handleLogin(message.payload as LoginPayload);
    case 'CHAT':
      return handleChat(message.payload as ChatPayload);
    case 'EXTRACT_PAGE_CONTENT':
      // 页面内容在 content script 中已提取，直接回传
      return message.payload;
    case 'SAVE_TO_KNOWLEDGE':
      return handleSaveToKnowledge(message.payload as SaveToKnowledgePayload);
    default:
      throw new Error(`Unknown message type: ${(message as WorkamaMessage).type}`);
  }
}

// ---- 顶层事件注册（真实扩展中运行；测试中由 chrome mock 捕获）----

chrome.runtime.onMessage.addListener(
  (message: WorkamaMessage, _sender, sendResponse) => {
    dispatchMessage(message)
      .then((data) => sendResponse({ ok: true, data }))
      .catch((err: Error) => sendResponse({ ok: false, error: err.message }));
    return true; // 返回 true 以保持 sendResponse 在异步流程中有效
  },
);

chrome.action.onClicked.addListener(async (tab) => {
  if (tab.id != null && chrome.sidePanel) {
    await chrome.sidePanel.open({ tabId: tab.id });
  }
});

chrome.runtime.onInstalled?.addListener(() => {
  chrome.contextMenus.create({
    id: 'send-to-workama',
    title: '发送到 WorkAMA',
    contexts: ['selection', 'page'],
  });
});
