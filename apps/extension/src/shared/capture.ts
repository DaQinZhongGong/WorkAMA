import type { CaptureResult } from './types'

export async function captureSelection(): Promise<CaptureResult> {
  return await chrome.runtime.sendMessage({ type: 'capture-selection' }) as CaptureResult
}

export async function openWorkAMA(baseUrl: string): Promise<void> {
  const url = `${baseUrl.replace(/\/$/, '')}/chat`
  await chrome.tabs.create({ url })
}

export async function openSidePanel(): Promise<void> {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true })
  if (!tab?.id) throw new Error('No active browser tab is available.')
  await chrome.sidePanel.open({ tabId: tab.id })
}

export async function copyText(value: string): Promise<void> {
  if (!value.trim()) throw new Error('Capture text first.')
  await navigator.clipboard.writeText(value)
}
