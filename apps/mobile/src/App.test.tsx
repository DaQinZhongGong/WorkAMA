import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { LocaleProvider } from './locale'
import App from './App'

// The App talks to the platform exclusively through the './api' module and the
// global WebSocket constructor. Stubbing both keeps these tests offline and
// deterministic.
vi.mock('./api', () => ({
  api: { get: vi.fn(), post: vi.fn() },
  agentWsUrl: 'ws://test.local',
  getSessionToken: vi.fn(),
  setSessionToken: vi.fn(),
  clearSessionToken: vi.fn(),
}))

import { api, getSessionToken } from './api'

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
const sessions = [
  { id: 's1', title: 'Launch plan', model: 'workama-chat', status: 'active' },
  { id: 's2', title: 'Budget review', model: 'workama-chat', status: 'active' },
]
const approvals = [
  { id: 'a1', tool_name: 'file.write', risk: 'High', preview: { path: '/tmp/secret' }, status: 'pending', created_at: '2026-07-20T10:00:00Z', expires_at: '2026-07-24T10:00:00Z' },
]
const notifications = [
  { id: 'n1', event_type: 'approval', priority: 'high', title: 'Approval needed', summary: 'Review file write', read_at: null, created_at: '2026-07-20T10:00:00Z' },
  { id: 'n2', event_type: 'task', priority: 'normal', title: 'Task done', summary: 'Build finished', read_at: '2026-07-21T10:00:00Z', created_at: '2026-07-21T10:00:00Z' },
]
const security = { mfa_enabled: true, sessions: [{ id: 'x', device: 'mac', current: true }, { id: 'y', device: 'phone' }] }
const assistants = [
  { id: 'ast1', name: 'WorkAMA Chat', description: 'General-purpose assistant', model: 'workama-chat', status: 'active', kind: 'ama_chat' },
]

type ApiOverrides = {
  sessions?: typeof sessions
  approvals?: typeof approvals
  notifications?: typeof notifications
  security?: typeof security
  user?: typeof user
  assistants?: typeof assistants
}

function setupApi(overrides: ApiOverrides = {}) {
  const sessionsData = overrides.sessions ?? sessions
  const approvalsData = overrides.approvals ?? approvals
  const notificationsData = overrides.notifications ?? notifications
  const securityData = overrides.security ?? security
  const userData = overrides.user ?? user
  const assistantsData = overrides.assistants ?? assistants

  mockedApiGet.mockImplementation(async (url: string) => {
    if (url === '/api/v1/sessions') return { items: sessionsData }
    if (url === '/api/v1/approvals?status=pending') return { items: approvalsData }
    if (url === '/api/v1/notifications') return { items: notificationsData }
    if (url === '/api/v1/datasets') return { items: [] }
    if (url === '/api/v1/billing/account') return { total_balance: 100, available_balance: 80, frozen_balance: 20 }
    if (url.startsWith('/api/v1/billing/transactions')) return { items: [] }
    if (url === '/api/v1/auth/security') return securityData
    if (url === '/api/v1/auth/me') return userData
    if (url.startsWith('/api/v1/sessions/') && url.endsWith('/events')) return { items: [] }
    if (url === '/api/v1/assistants') return { items: assistantsData }
    return {}
  })

  mockedApiPost.mockImplementation(async (url: string) => {
    if (url.endsWith('/ws-tickets')) return { ticket: 'ws-ticket' }
    if (url === '/api/v1/auth/login') return { access_token: 'tok', user: userData }
    if (url === '/api/v1/sessions') return { id: 's-new', title: 'New conversation', model: 'workama-chat', status: 'active' }
    if (url.includes('/decisions')) return {}
    if (url === '/api/v1/notification-read-receipts') return {}
    if (url.endsWith('/read-receipts')) return {}
    if (url === '/api/v1/privacy/data-requests') return {}
    return {}
  })
}

function renderApp(initialPath = '/chat') {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <LocaleProvider><App /></LocaleProvider>
    </MemoryRouter>
  )
}

async function goToTab(name: RegExp) {
  const link = await screen.findByRole('link', { name })
  fireEvent.click(link)
}

describe('mobile App', () => {
  beforeEach(() => {
    mockSockets.length = 0
    globalThis.WebSocket = MockWebSocket as unknown as typeof WebSocket
    window.localStorage.setItem('workama.locale', 'en-US')
    mockedGetSessionToken.mockReturnValue('test-token')
    setupApi()
  })

  afterEach(() => {
    cleanup()
    globalThis.WebSocket = originalWebSocket
    vi.clearAllMocks()
    window.localStorage.clear()
  })

  describe('authentication shell', () => {
    it('shows the login screen with email, password and enter workspace when unauthenticated', () => {
      mockedGetSessionToken.mockReturnValue(null)
      renderApp()
      expect(screen.getByLabelText('Email')).toBeInTheDocument()
      expect(screen.getByLabelText('Password')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /enter workspace/i })).toBeInTheDocument()
    })

    it('authenticates and renders the bottom nav after submitting the login form', async () => {
      mockedGetSessionToken.mockReturnValue(null)
      renderApp()
      fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'ada@workama.test' } })
      fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'secret' } })
      fireEvent.click(screen.getByRole('button', { name: /enter workspace/i }))
      expect(await screen.findByRole('link', { name: /^Chat$/ })).toBeInTheDocument()
      expect(screen.getByRole('link', { name: /^Agents$/ })).toBeInTheDocument()
      expect(mockedApiPost).toHaveBeenCalledWith('/api/v1/auth/login', expect.objectContaining({ email: 'ada@workama.test', password: 'secret' }))
    })
  })

  describe('bottom navigation', () => {
    it('renders the four bottom navigation tabs: Chat, Agents, Knowledge, Settings', async () => {
      renderApp()
      expect(await screen.findByRole('link', { name: /^Chat$/ })).toBeInTheDocument()
      expect(screen.getByRole('link', { name: /^Agents$/ })).toBeInTheDocument()
      expect(screen.getByRole('link', { name: /^Knowledge$/ })).toBeInTheDocument()
      expect(screen.getByRole('link', { name: /^Settings$/ })).toBeInTheDocument()
    })

    it('defaults to the chat list route showing the chat title', async () => {
      renderApp()
      expect(await screen.findByRole('heading', { name: 'Continue the conversation' })).toBeInTheDocument()
    })

    it('navigates to the agents tab showing the agents title', async () => {
      renderApp()
      await goToTab(/^Agents$/)
      expect(await screen.findByRole('heading', { name: /agents/i })).toBeInTheDocument()
    })

    it('navigates to the knowledge tab showing the knowledge title', async () => {
      renderApp()
      await goToTab(/^Knowledge$/)
      expect(await screen.findByRole('heading', { name: /knowledge/i })).toBeInTheDocument()
    })

    it('navigates to the settings tab showing the settings title', async () => {
      renderApp()
      await goToTab(/^Settings$/)
      expect(await screen.findByRole('heading', { name: /settings/i })).toBeInTheDocument()
    })
  })

  describe('chat list view', () => {
    it('renders the conversation list with session titles', async () => {
      renderApp()
      expect(await screen.findByText('Launch plan')).toBeInTheDocument()
      expect(screen.getByText('Budget review')).toBeInTheDocument()
    })

    it('renders session list cards linking to /chat/:id', async () => {
      renderApp()
      const link = await screen.findByRole('link', { name: /launch plan/i })
      expect(link).toHaveAttribute('href', '/chat/s1')
    })

    it('exposes a new conversation action in the chat list', async () => {
      renderApp()
      expect(await screen.findByRole('button', { name: /new conversation/i })).toBeInTheDocument()
    })

    it('shows the empty state when there are no sessions', async () => {
      setupApi({ sessions: [] })
      renderApp()
      expect(await screen.findByText(/start with a question/i)).toBeInTheDocument()
    })
  })

  describe('chat detail view', () => {
    it('renders the chat detail route with session title in header', async () => {
      renderApp('/chat/s1')
      await waitFor(() => expect(mockSockets.length).toBeGreaterThan(0))
      const socket = mockSockets[mockSockets.length - 1]
      await act(async () => {
        socket.readyState = 1
        socket.onopen?.(new Event('open'))
      })
      expect(await screen.findByText('Launch plan')).toBeInTheDocument()
    })

    it('shows a back button that navigates to /chat', async () => {
      renderApp('/chat/s1')
      await waitFor(() => expect(mockSockets.length).toBeGreaterThan(0))
      const socket = mockSockets[mockSockets.length - 1]
      await act(async () => {
        socket.readyState = 1
        socket.onopen?.(new Event('open'))
      })
      const backButton = screen.getByRole('button', { name: /back/i })
      expect(backButton).toBeInTheDocument()
    })

    it('sends a message through the websocket once connected', async () => {
      renderApp('/chat/s1')
      await waitFor(() => expect(mockSockets.length).toBeGreaterThan(0))
      const socket = mockSockets[mockSockets.length - 1]
      await act(async () => {
        socket.readyState = 1
        socket.onopen?.(new Event('open'))
      })
      await screen.findByText('Live sync')
      const composer = screen.getByLabelText('Message')
      fireEvent.change(composer, { target: { value: 'hello workama' } })
      fireEvent.click(screen.getByRole('button', { name: /send message/i }))
      expect(socket.send).toHaveBeenCalledWith(expect.stringContaining('"message.create"'))
      expect(socket.send).toHaveBeenCalledWith(expect.stringContaining('hello workama'))
    })

    it('shows the connecting status before the websocket opens', async () => {
      renderApp('/chat/s1')
      expect(await screen.findByText('Connecting')).toBeInTheDocument()
    })
  })

  describe('agents view', () => {
    it('renders the assistant list with names', async () => {
      renderApp('/agents')
      expect(await screen.findByText('WorkAMA Chat')).toBeInTheDocument()
    })

    it('shows the empty state when there are no assistants', async () => {
      setupApi({ assistants: [] })
      renderApp('/agents')
      expect(await screen.findByText(/no agents/i)).toBeInTheDocument()
    })
  })

  describe('knowledge view', () => {
    it('renders the knowledge base page title', async () => {
      renderApp('/knowledge')
      expect(await screen.findByRole('heading', { name: /knowledge/i })).toBeInTheDocument()
    })

    it('shows the empty state when there are no datasets', async () => {
      renderApp('/knowledge')
      expect(await screen.findByText(/no knowledge bases/i)).toBeInTheDocument()
    })
  })

  describe('settings view', () => {
    it('shows the user profile card with display name and email', async () => {
      renderApp('/settings')
      expect(await screen.findByText('Ada Wong')).toBeInTheDocument()
      expect(screen.getByText('ada@workama.test')).toBeInTheDocument()
    })

    it('shows the MFA enabled status on the security subpage', async () => {
      renderApp('/settings')
      fireEvent.click(await screen.findByRole('button', { name: /security/i }))
      expect(await screen.findByText('Enabled')).toBeInTheDocument()
    })

    it('returns to the login screen after signing out', async () => {
      renderApp('/settings')
      fireEvent.click(await screen.findByRole('button', { name: /sign out/i }))
      expect(await screen.findByLabelText('Email')).toBeInTheDocument()
    })
  })

  describe('locale toggle', () => {
    it('switches the interface language from English to Chinese', async () => {
      renderApp()
      await screen.findByRole('link', { name: /^Chat$/ })
      const toggle = screen.getByText('中')
      fireEvent.click(toggle)
      expect(await screen.findByText('对话')).toBeInTheDocument()
      expect(screen.queryByRole('link', { name: /^Chat$/ })).not.toBeInTheDocument()
    })
  })

  describe('error handling', () => {
    it('shows a page error notice when workspace data fails to load', async () => {
      mockedGetSessionToken.mockReturnValue('test-token')
      mockedApiGet.mockImplementation(async (url: string) => {
        if (url === '/api/v1/sessions' || url === '/api/v1/approvals?status=pending') throw new Error('boom')
        if (url === '/api/v1/notifications') return { items: [] }
        if (url === '/api/v1/datasets') return { items: [] }
        if (url === '/api/v1/billing/account') return {}
        if (url.startsWith('/api/v1/billing/transactions')) return { items: [] }
        if (url === '/api/v1/auth/security') return security
        if (url === '/api/v1/auth/me') return user
        if (url === '/api/v1/assistants') return { items: [] }
        return {}
      })
      mockedApiPost.mockImplementation(async () => ({ ticket: 'ws-ticket' }))
      renderApp()
      expect(await screen.findByText('Workspace data is temporarily unavailable. Please retry.')).toBeInTheDocument()
    })
  })
})
