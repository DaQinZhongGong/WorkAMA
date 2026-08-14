// API 服务封装：基于 fetch，统一注入 Bearer token，401 自动登出
import { useAuthStore } from '../stores/authStore'

const DEFAULT_API_URL = 'http://localhost:20200'

// 从环境变量读取平台 API 地址，默认本地 20200
export function getBaseUrl(): string {
  return process.env.EXPO_PUBLIC_API_URL || process.env.API_URL || DEFAULT_API_URL
}

// 用户信息
export interface User {
  id?: string
  display_name?: string
  email: string
  role?: string
}

// 登录返回结构
export interface LoginResponse {
  access_token: string
  user: User
}

// Agent 信息
export interface Agent {
  id: string
  name: string
  description?: string
  model?: string
  status?: string
  kind?: string
}

// 对话返回结构（兼容 reply/message/content 三种字段）
export interface ChatResponse {
  reply?: string
  message?: string
  content?: string
}

// 记忆向量
export interface MemoryVector {
  id: string
  content?: string
  metadata?: Record<string, unknown>
}

// 统一 API 错误
export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

// 统一请求：注入 token，处理非 2xx，401 触发登出
async function request<T>(path: string, token: string | null, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((init.headers as Record<string, string> | undefined) ?? {}),
  }
  if (token) headers['Authorization'] = `Bearer ${token}`

  const response = await fetch(`${getBaseUrl()}${path}`, {
    ...init,
    headers,
  })

  if (!response.ok) {
    // 读取错误体（detail / message / error.message）
    let message = response.statusText
    try {
      const body = await response.json()
      message = body?.detail || body?.message || body?.error?.message || message
    } catch {
      // 非 JSON 错误体，沿用 statusText
    }
    // 401：清除本地 token，触发跳转登录
    if (response.status === 401) {
      useAuthStore.getState().logout()
    }
    throw new ApiError(response.status, message)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

// 邮箱密码登录：POST /api/v1/auth/login
export async function login(email: string, password: string): Promise<LoginResponse> {
  return request<LoginResponse>('/api/v1/auth/login', null, {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  })
}

// 获取 Agent 列表：GET /api/v1/agents
export async function listAgents(token: string): Promise<Agent[]> {
  const result = await request<{ items: Agent[] } | Agent[]>('/api/v1/agents', token, {
    method: 'GET',
  })
  // 兼容 { items: [] } 与裸数组两种返回结构
  return Array.isArray(result) ? result : (result.items ?? [])
}

// 与指定 Agent 对话：POST /api/v1/agents/{agentId}/chat
export async function chat(token: string, agentId: string, message: string): Promise<ChatResponse> {
  return request<ChatResponse>(`/api/v1/agents/${agentId}/chat`, token, {
    method: 'POST',
    body: JSON.stringify({ message }),
  })
}

// 获取记忆向量列表：GET /api/v1/memory-vectors
export async function listMemories(token: string): Promise<MemoryVector[]> {
  const result = await request<{ items: MemoryVector[] } | MemoryVector[]>('/api/v1/memory-vectors', token, {
    method: 'GET',
  })
  return Array.isArray(result) ? result : (result.items ?? [])
}
