import type { SessionState } from './types'

export const DEFAULT_BASE_URL = 'http://localhost:20204'

export async function loadSession(): Promise<SessionState> {
  const saved = await chrome.storage.session.get(['baseUrl', 'token', 'context'])
  return {
    baseUrl: typeof saved.baseUrl === 'string' && saved.baseUrl ? saved.baseUrl : DEFAULT_BASE_URL,
    token: typeof saved.token === 'string' ? saved.token : '',
    context: typeof saved.context === 'string' ? saved.context : '',
  }
}

export function normalizeBaseUrl(value: string): string {
  const url = value.trim().replace(/\/$/, '')
  if (!/^https?:\/\//i.test(url)) throw new Error('WorkAMA URL must be http(s).')
  return url
}

export async function saveSession(next: Partial<SessionState>): Promise<SessionState> {
  const current = await loadSession()
  const state = {
    ...current,
    ...next,
    baseUrl: normalizeBaseUrl(next.baseUrl ?? current.baseUrl),
  }
  await chrome.storage.session.set(state)
  return state
}

export async function clearSession(): Promise<void> {
  await chrome.storage.session.clear()
}
