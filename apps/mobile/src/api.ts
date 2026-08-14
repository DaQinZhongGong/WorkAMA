import { WorkamaClient } from '@workama/api-client'

export const platformUrl = import.meta.env.VITE_PLATFORM_API_URL ?? 'http://localhost:20200'
export const agentWsUrl = import.meta.env.VITE_AGENT_WS_URL ?? 'ws://localhost:20201'

// The mobile entry intentionally keeps the access token in memory only.
let currentSessionToken: string | null = null

export const sessionToken = {
  get value() { return currentSessionToken },
}

export function getSessionToken() {
  return currentSessionToken
}

export function setSessionToken(token: string) {
  currentSessionToken = token
}

export function clearSessionToken() {
  currentSessionToken = null
}

export const api = new WorkamaClient({
  baseUrl: platformUrl,
  getToken: () => sessionToken.value,
})
