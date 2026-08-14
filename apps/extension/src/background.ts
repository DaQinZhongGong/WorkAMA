const MAX_SELECTION_LENGTH = 8000

function isWebPage(url: unknown): url is string {
  return typeof url === 'string' && (url.startsWith('https://') || url.startsWith('http://'))
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => undefined)
})

async function captureSelection() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true })
  if (!tab?.id || !isWebPage(tab.url)) {
    return { ok: false, error: 'This page cannot be captured.' }
  }
  const result = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: (limit) => {
      const active = document.activeElement
      if (active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement) {
        const sensitive = active.type === 'password' || /pass|secret|token|card|cvv|payment/i.test(active.name || active.id || '')
        if (sensitive) return { ok: false, error: 'Sensitive input is not capturable.' }
      }
      const text = window.getSelection()?.toString().trim() || ''
      if (!text) return { ok: false, error: 'Select text before capturing.' }
      const redact = (value: string) => value
        .replace(/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, '[REDACTED]')
        .replace(/\b(?:password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*[^\s,;]+/gi, '[REDACTED]')
        .replace(/\b(?:\d[ -]*?){13,19}\b/g, '[REDACTED]')
      const safeUrl = (() => {
        try { const url = new URL(location.href); return `${url.origin}${url.pathname}` } catch { return location.origin } 
      })()
      return { ok: true, text: redact(text.slice(0, limit)), title: redact(document.title), url: safeUrl }
    },
    args: [MAX_SELECTION_LENGTH],
  })
  return result?.[0]?.result || { ok: false, error: 'Selection capture failed.' }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== 'capture-selection') return false
  captureSelection().then(sendResponse).catch((error: unknown) => {
    const detail = error instanceof Error ? error.message : 'Selection capture failed.'
    sendResponse({ ok: false, error: detail })
  })
  return true
})
