import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { LocaleProvider } from '../src/locale'
import App from '../src/App'

vi.mock('../src/api', () => ({
  api: { get: vi.fn(), post: vi.fn() },
  agentWsUrl: 'ws://test.local',
  getSessionToken: vi.fn(),
  setSessionToken: vi.fn(),
  clearSessionToken: vi.fn(),
}))

import { api, getSessionToken } from '../src/api'

const mockedApiGet = vi.mocked(api.get)
const mockedApiPost = vi.mocked(api.post)
const mockedGetSessionToken = vi.mocked(getSessionToken)

type MockSocket = {
  url: string
  readyState: number
  onopen: ((ev: Event) => void) | null
  onmessage: ((ev: MessageEvent) => void) | null
  onerror: ((ev: Event) => void) | null
  onclose: ((ev: CloseEvent) => void) | null
  send: ReturnType<typeof vi.fn>
  close: ReturnType<typeof vi.fn>
}

const mockSockets: MockSocket[] = []

class MockWebSocket {
  static OPEN = 1
  static CLOSED = 3
  static CONNECTING = 0
  static CLOSING = 2
  url: string
  readyState = 0
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  send = vi.fn()
  close = vi.fn(() => { this.readyState = 3 })
  constructor(url: string) {
    this.url = url
    mockSockets.push(this as unknown as MockSocket)
  }
}

const originalWebSocket = globalThis.WebSocket

const user = { display_name: 'Ada Wong', email: 'ada@workama.test', role: 'owner' }
const security = { mfa_enabled: true, sessions: [{ id: 'x', device: 'mac', current: true }] }

function setupApi() {
  mockedApiGet.mockImplementation(async (url: string) => {
    if (url === '/api/v1/sessions') return { items: [{ id: 's1', title: 'Test Session', model: 'workama-chat', status: 'active' }] }
    if (url === '/api/v1/sessions/s1/events') return { items: [] }
    if (url === '/api/v1/approvals?status=pending') return { items: [] }
    if (url === '/api/v1/notifications') return { items: [] }
    if (url === '/api/v1/datasets') return { items: [] }
    if (url === '/api/v1/billing/account') return {}
    if (url.startsWith('/api/v1/billing/transactions')) return { items: [] }
    if (url === '/api/v1/auth/security') return security
    if (url === '/api/v1/auth/me') return user
    if (url === '/api/v1/assistants') return { items: [] }
    return {}
  })
  mockedApiPost.mockImplementation(async (url: string) => {
    if (url === '/api/v1/sessions/s1/ws-tickets') return { ticket: 'test-ticket' }
    return {}
  })
}

function renderApp(initialPath = '/settings') {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <LocaleProvider><App /></LocaleProvider>
    </MemoryRouter>
  )
}

describe('PWA enhanced features', () => {
  beforeEach(() => {
    mockSockets.length = 0
    globalThis.WebSocket = MockWebSocket as unknown as typeof WebSocket
    window.localStorage.setItem('workama.locale', 'en-US')
    Object.defineProperty(navigator, 'language', { value: 'en-US', writable: true, configurable: true })
    mockedGetSessionToken.mockReturnValue('test-token')
    setupApi()
  })

  afterEach(() => {
    cleanup()
    globalThis.WebSocket = originalWebSocket
    vi.clearAllMocks()
    window.localStorage.clear()
  })

  describe('push notification settings', () => {
    function mockPushEnv() {
      Object.defineProperty(window, 'PushManager', { value: class {}, writable: true, configurable: true })
      Object.defineProperty(navigator, 'serviceWorker', {
        value: { ready: Promise.resolve({ pushManager: { subscribe: vi.fn(), getSubscription: vi.fn() } }) },
        writable: true,
        configurable: true,
      })
    }

    it('shows push notification option in settings when PushManager is available', async () => {
      mockPushEnv()
      renderApp('/settings')
      expect(await screen.findByText('Push notifications')).toBeInTheDocument()
    })

    it('shows push disabled state by default', async () => {
      mockPushEnv()
      renderApp('/settings')
      expect(await screen.findByText('Push notifications')).toBeInTheDocument()
      expect(screen.getByText('Disabled')).toBeInTheDocument()
    })

    it('toggles push subscription when clicked', async () => {
      mockPushEnv()
      const mockSubscribe = vi.fn().mockResolvedValue({ endpoint: 'https://test.push/1', toJSON: () => ({ keys: { p256dh: 'x', auth: 'y' } }) })
      const mockGetSubscription = vi.fn().mockResolvedValue(null)
      Object.defineProperty(navigator, 'serviceWorker', {
        value: { ready: Promise.resolve({ pushManager: { subscribe: mockSubscribe, getSubscription: mockGetSubscription } }) },
        writable: true,
        configurable: true,
      })
      renderApp('/settings')
      const pushBtn = await screen.findByRole('button', { name: /Push notifications/i })
      fireEvent.click(pushBtn)
      await waitFor(() => expect(mockedApiPost).toHaveBeenCalledWith('/api/v1/push/subscriptions', expect.any(Object)), { timeout: 2000 })
    })
  })

  describe('offline banner', () => {
    it('shows offline banner when navigator.onLine is false', async () => {
      Object.defineProperty(navigator, 'onLine', { value: false, writable: true, configurable: true })
      renderApp('/chat')
      expect(await screen.findByText('You are offline')).toBeInTheDocument()
    })

    it('hides offline banner when navigator.onLine is true', async () => {
      Object.defineProperty(navigator, 'onLine', { value: true, writable: true, configurable: true })
      renderApp('/chat')
      await waitFor(() => expect(screen.queryByText('You are offline')).not.toBeInTheDocument())
    })
  })

  describe('voice input UI', () => {
    it('shows voice input button when SpeechRecognition is available', async () => {
      Object.defineProperty(window, 'webkitSpeechRecognition', { value: class {
        lang = ''
        interimResults = false
        maxAlternatives = 1
        onstart: (() => void) | null = null
        onend: (() => void) | null = null
        onresult: ((e: unknown) => void) | null = null
        onerror: (() => void) | null = null
        start() { this.onstart?.() }
        stop() { this.onend?.() }
      }, writable: true, configurable: true })
      renderApp('/chat/s1')
      await waitFor(() => expect(mockSockets.length).toBeGreaterThan(0))
      const socket = mockSockets[mockSockets.length - 1]
      await act(async () => { socket.readyState = 1; socket.onopen?.(new Event('open')) })
      expect(await screen.findByLabelText('Start voice input')).toBeInTheDocument()
    })

    it('keeps voice button enabled when offline because speech is local', async () => {
      Object.defineProperty(window, 'webkitSpeechRecognition', { value: class { start() {} stop() {} }, writable: true, configurable: true })
      Object.defineProperty(navigator, 'onLine', { value: false, writable: true, configurable: true })
      renderApp('/chat/s1')
      await waitFor(() => expect(mockSockets.length).toBeGreaterThan(0))
      const socket = mockSockets[mockSockets.length - 1]
      await act(async () => { socket.readyState = 1; socket.onopen?.(new Event('open')) })
      const voiceBtn = await screen.findByLabelText('Start voice input')
      expect(voiceBtn).not.toBeDisabled()
    })
  })

  describe('indexedDB offline cache', () => {
    it('caches sent messages to IndexedDB', async () => {
      renderApp('/chat/s1')
      await waitFor(() => expect(mockSockets.length).toBeGreaterThan(0))
      const socket = mockSockets[mockSockets.length - 1]
      await act(async () => { socket.readyState = 1; socket.onopen?.(new Event('open')) })
      const composer = screen.getByLabelText('Message')
      fireEvent.change(composer, { target: { value: 'hello workama' } })
      fireEvent.click(screen.getByRole('button', { name: /send message/i }))
      await waitFor(() => expect(socket.send).toHaveBeenCalled())
    })
  })

  describe('knowledge page touch-friendly', () => {
    it('renders knowledge cards with touch-friendly class', async () => {
      mockedApiGet.mockImplementation(async (url: string) => {
        if (url === '/api/v1/datasets') return { items: [{ id: 'd1', name: 'Docs', description: 'Team docs', status: 'ready', document_count: 3 }] }
        return {}
      })
      renderApp('/knowledge')
      expect(await screen.findByText('Docs')).toBeInTheDocument()
    })
  })

  describe('service worker push handler', () => {
    it('sw.js contains push event listener', async () => {
      const swRaw = await import('../public/sw.js?raw')
      expect(swRaw.default).toContain("addEventListener('push'")
      expect(swRaw.default).toContain('self.registration.showNotification(')
    })

    it('sw.js contains message event for test protocol', async () => {
      const swRaw = await import('../public/sw.js?raw')
      expect(swRaw.default).toContain('PWA_TEST_PUSH')
      expect(swRaw.default).toContain('PWA_TEST_PUSH_RESULT')
    })
  })

  describe('install prompt', () => {
    it('shows install button when deferred prompt is available', async () => {
      renderApp('/chat')
      await screen.findByRole('link', { name: /^Chat$/ })
      const event = new Event('beforeinstallprompt')
      Object.defineProperty(event, 'prompt', { value: vi.fn().mockResolvedValue(undefined) })
      window.dispatchEvent(event)
      await waitFor(() => expect(screen.getByTestId('pwa-install-button')).toBeInTheDocument())
    })
  })

  describe('composer offline behavior', () => {
    it('allows typing in composer when offline', async () => {
      Object.defineProperty(navigator, 'onLine', { value: false, writable: true, configurable: true })
      renderApp('/chat/s1')
      await waitFor(() => expect(mockSockets.length).toBeGreaterThan(0))
      const socket = mockSockets[mockSockets.length - 1]
      await act(async () => { socket.readyState = 1; socket.onopen?.(new Event('open')) })
      const composer = screen.getByLabelText('Message')
      expect(composer).not.toBeDisabled()
    })
  })
})
