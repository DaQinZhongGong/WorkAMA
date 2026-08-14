import { WorkamaClient } from '@workama/api-client'
import { agentWsUrl, platformApiUrl } from './config'

// 历史调用方（如 pages.tsx）仍从 './api' 取用 platformUrl / agentWsUrl，此处保留再导出，
// 真实取值已下沉到 ./config（经由 @workama/config 共享包与集中化的 import.meta.env 读取）。
export const platformUrl = platformApiUrl
export { agentWsUrl }

let accessToken: string | null = sessionStorage.getItem('workama_access_token')

export function setWebAccessToken(token: string) {
  accessToken = token
  sessionStorage.setItem('workama_access_token', token)
}

export function clearWebAccessToken() {
  accessToken = null
  sessionStorage.removeItem('workama_access_token')
}

export const api = new WorkamaClient({
  baseUrl: platformUrl,
  getToken: () => accessToken,
})

export function errorMessage(error: unknown, fallback = '') {
  return error instanceof Error && error.message ? error.message : fallback
}

export function asItems<T>(payload: unknown): T[] {
  if (Array.isArray(payload)) return payload as T[]
  if (payload && typeof payload === 'object' && 'items' in payload && Array.isArray(payload.items)) return payload.items as T[]
  return []
}
