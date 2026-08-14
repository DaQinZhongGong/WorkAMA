import {
  ArrowLeft, ArrowUp, Bell, BellOff, BellRing, BookOpen, Bot, Check, CheckCircle2, ChevronRight,
  CircleDollarSign, Clock3, FileText, KeyRound, LockKeyhole, LogOut, Menu, Mic,
  MessageCircle, Plus, RefreshCw, Search, Settings, ShieldCheck, Sparkles, UserRound, WifiOff, X,
} from 'lucide-react'
import { type FormEvent, type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, Navigate, Outlet, Route, Routes, useLocation, useNavigate, useParams } from 'react-router-dom'
import { api, agentWsUrl, clearSessionToken, getSessionToken, setSessionToken } from './api'
import { cacheMessage, getCachedMessages } from './db'
import { applyStreamEvent, asAgentEvent, emptyMobileProjection, projectSnapshot, type AgentEvent, type SessionProjection } from './model'
import { LocaleToggle, useLocale } from './locale'
import type { MessageKey } from '@workama/i18n'

type Tab = 'chat' | 'agents' | 'knowledge' | 'settings'
type ProfileSection = 'home' | 'balance' | 'security' | 'privacy'
type User = { display_name: string; email: string; role: string }
type LoginResponse = { access_token: string; user: User } | { mfa_required: true; mfa_ticket: string }
type Session = { id: string; title: string; model: string; status: string; updated_at?: string; toolset?: string[] }
type Approval = { id: string; tool_name: string; risk: string; preview: unknown; status: string; created_at: string; expires_at: string; reason?: string }
type Notification = { id: string; event_type: string; priority: string; title: string; summary: string; read_at?: string | null; created_at: string }
type Dataset = { id: string; name: string; description?: string; status?: string; document_count?: number }
type Balance = { total_balance?: number | string; available_balance?: number | string; frozen_balance?: number | string }
type Transaction = { id: string; kind: string; amount: number | string; description?: string; created_at?: string }
type SecurityInfo = { mfa_enabled?: boolean; sessions?: Array<{ id?: string; device?: string; last_seen_at?: string; current?: boolean }> }
type Assistant = { id: string; name: string; description?: string; model?: string; status?: string; kind?: string }

const navItems: Array<{ id: Tab; path: string; icon: typeof MessageCircle }> = [
  { id: 'chat', path: '/chat', icon: MessageCircle },
  { id: 'agents', path: '/agents', icon: Bot },
  { id: 'knowledge', path: '/knowledge', icon: BookOpen },
  { id: 'settings', path: '/settings', icon: Settings },
]

const navKeys: Record<Tab, MessageKey> = { chat: 'mobile.chat', agents: 'mobile.agents', knowledge: 'mobile.knowledge', settings: 'mobile.settings' }

function errorMessage(value: unknown, fallback: string) { return value instanceof Error && value.message ? value.message : fallback }
function formatDate(value: string | null | undefined, fallback: string): string { if (!value) return fallback; const date = new Date(value); return Number.isNaN(date.getTime()) ? fallback : date.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) }
function numeric(value: number | string | undefined) { const parsed = Number(value ?? 0); return Number.isFinite(parsed) ? parsed : 0 }

export default function App() {
  return <AppShell />
}

function AppShell() {
  const { t } = useLocale()
  const [authenticated, setAuthenticated] = useState(Boolean(getSessionToken()))
  const [user, setUser] = useState<User | null>(null)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [authError, setAuthError] = useState('')
  const [busy, setBusy] = useState(false)
  const [sessions, setSessions] = useState<Session[]>([])
  const [approvals, setApprovals] = useState<Approval[]>([])
  const [notifications, setNotifications] = useState<Notification[]>([])
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [balance, setBalance] = useState<Balance | null>(null)
  const [transactions, setTransactions] = useState<Transaction[]>([])
  const [security, setSecurity] = useState<SecurityInfo | null>(null)
  const [assistants, setAssistants] = useState<Assistant[]>([])
  const [selectedSession, setSelectedSession] = useState<Session | null>(null)
  const [projection, setProjection] = useState<SessionProjection>(emptyMobileProjection())
  const [draft, setDraft] = useState('')
  const [appError, setAppError] = useState('')
  const [connected, setConnected] = useState(false)
  const [deferredPrompt, setDeferredPrompt] = useState<Event | null>(null)
  const [isOffline, setIsOffline] = useState(false)
  const [pushSupported, setPushSupported] = useState(false)
  const [pushEnabled, setPushEnabled] = useState(false)
  const [voiceSupported, setVoiceSupported] = useState(false)
  const [voiceListening, setVoiceListening] = useState(false)
  const socketRef = useRef<WebSocket | null>(null)
  const closingRef = useRef(false)
  const projectionRef = useRef(projection)
  const selectedSessionRef = useRef<Session | null>(selectedSession)
  const recognitionRef = useRef<SpeechRecognition | null>(null)
  projectionRef.current = projection
  selectedSessionRef.current = selectedSession
  const pendingApprovals = useMemo(() => approvals.filter((item) => item.status === 'pending').length, [approvals])
  const unreadNotifications = useMemo(() => notifications.filter((item) => !item.read_at).length, [notifications])

  function closeSocket() { socketRef.current?.close(); socketRef.current = null; setConnected(false) }

  async function connectSession(sessionId: string) {
    const ticket = await api.post<{ ticket: string }>(`/api/v1/sessions/${sessionId}/ws-tickets`)
    const ws = new WebSocket(`${agentWsUrl}/ws/sessions/${sessionId}?ticket=${encodeURIComponent(ticket.ticket)}&after=${projectionRef.current.lastSeq}`)
    socketRef.current = ws
    ws.onopen = () => setConnected(true)
    ws.onmessage = (message) => {
      try {
        const packet = JSON.parse(String(message.data)) as { type?: string; events?: unknown[]; payload?: Record<string, unknown>; seq?: number; id?: string }
        if (packet.type === 'connection.ready') { ws.send(JSON.stringify({ type: 'event.ack', seq: projectionRef.current.lastSeq })); return }
        if (packet.type === 'session.snapshot') {
          const next = projectSnapshot((packet.events ?? []).map(asAgentEvent).filter((item): item is AgentEvent => item !== null))
          setProjection(next); ws.send(JSON.stringify({ type: 'event.ack', seq: next.lastSeq })); return
        }
        const event = asAgentEvent(packet)
        if (!event) return
        setProjection((current) => applyStreamEvent(current, event))
        if (typeof event.seq === 'number' && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'event.ack', seq: event.seq }))
      } catch { setAppError(t('mobile.errorParseEvent')) }
    }
    ws.onerror = () => setConnected(false)
    ws.onclose = () => {
      setConnected(false)
      if (!closingRef.current && selectedSessionRef.current?.id === sessionId) window.setTimeout(() => { if (!closingRef.current && selectedSessionRef.current?.id === sessionId && !socketRef.current) void connectSession(sessionId).catch(() => setAppError(t('mobile.errorConnectionUnavailable'))) }, 1500)
    }
  }

  async function openSession(session: Session) {
    closeSocket(); setSelectedSession(session); selectedSessionRef.current = session; setAppError('')
    try { const result = await api.get<{ items: AgentEvent[] }>(`/api/v1/sessions/${session.id}/events`); const next = projectSnapshot(result.items); setProjection(next); projectionRef.current = next; await connectSession(session.id) }
    catch (caught) { setAppError(errorMessage(caught, t('mobile.errorOpenConversation'))) }
  }

  async function loadWorkspace() {
    setAppError('')
    const results = await Promise.allSettled([
      api.get<{ items: Session[] }>('/api/v1/sessions'), api.get<{ items: Approval[] }>('/api/v1/approvals?status=pending'),
      api.get<{ items: Notification[] }>('/api/v1/notifications'), api.get<{ items: Dataset[] }>('/api/v1/datasets'),
      api.get<Balance>('/api/v1/billing/account'), api.get<{ items: Transaction[] }>('/api/v1/billing/transactions?limit=8'),
      api.get<SecurityInfo>('/api/v1/auth/security'), api.get<User>('/api/v1/auth/me'),
      api.get<{ items: Assistant[] }>('/api/v1/assistants'),
    ])
    const [sessionResult, approvalResult, notificationResult, datasetResult, balanceResult, transactionResult, securityResult, userResult, assistantResult] = results
    if (sessionResult.status === 'fulfilled') { const items = sessionResult.value.items ?? []; setSessions(items); if (items.length && !selectedSessionRef.current) void openSession(items[0]) }
    if (approvalResult.status === 'fulfilled') setApprovals(approvalResult.value.items ?? [])
    if (notificationResult.status === 'fulfilled') setNotifications(notificationResult.value.items ?? [])
    if (datasetResult.status === 'fulfilled') setDatasets(datasetResult.value.items ?? [])
    if (balanceResult.status === 'fulfilled') setBalance(balanceResult.value)
    if (transactionResult.status === 'fulfilled') setTransactions(transactionResult.value.items ?? [])
    if (securityResult.status === 'fulfilled') setSecurity(securityResult.value)
    if (userResult.status === 'fulfilled') setUser(userResult.value)
    if (assistantResult.status === 'fulfilled') setAssistants(assistantResult.value.items ?? [])
    if (sessionResult.status === 'rejected' && approvalResult.status === 'rejected') setAppError(t('mobile.errorWorkspaceUnavailable'))
  }

  async function login(event: FormEvent) {
    event.preventDefault(); setAuthError(''); setBusy(true)
    try { const result = await api.post<LoginResponse>('/api/v1/auth/login', { email, password }); if ('mfa_required' in result) { setAuthError(t('mobile.errorMfaRequired')); return }; setSessionToken(result.access_token); setUser(result.user); setAuthenticated(true) }
    catch (caught) { setAuthError(errorMessage(caught, t('mobile.errorSignInFailed'))) }
    finally { setBusy(false) }
  }

  async function createSession() {
    setBusy(true)
    try { const created = await api.post<Session>('/api/v1/sessions', { title: t('mobile.sessionDefaultTitle'), model: 'workama-chat', agent_kind: 'ama_chat', model_config: { temperature: 0.7 }, toolset: ['file.read', 'file.search'], canvas_enabled: false, max_steps: 20 }); setSessions((current) => [created, ...current.filter((item) => item.id !== created.id)]); await openSession(created) }
    catch (caught) { setAppError(errorMessage(caught, t('mobile.errorCreateConversation'))) }
    finally { setBusy(false) }
  }

  function sendMessage(event?: FormEvent) {
    event?.preventDefault()
    const content = draft.trim()
    const socket = socketRef.current
    if (!content || !socket || !connected || projection.running) return
    socket.send(JSON.stringify({ type: 'message.create', content, attachment_ids: [] }))
    setDraft('')
    if (selectedSessionRef.current) {
      void cacheMessage(selectedSessionRef.current.id, { id: `local-${Date.now()}`, role: 'user', content })
    }
  }

  async function decide(id: string, decision: 'approved' | 'rejected') {
    setBusy(true)
    try { await api.post(`/api/v1/approvals/${id}/decisions`, { decision, reason: decision === 'approved' ? t('mobile.approvalReasonApproved') : t('mobile.approvalReasonRejected') }); setApprovals((current) => current.map((item) => item.id === id ? { ...item, status: decision } : item)) }
    catch (caught) { setAppError(errorMessage(caught, t('mobile.errorApprovalDecision'))) }
    finally { setBusy(false) }
  }

  async function markNotificationRead(item: Notification) { if (item.read_at) return; try { await api.post(`/api/v1/notifications/${item.id}/read-receipts`); setNotifications((current) => current.map((entry) => entry.id === item.id ? { ...entry, read_at: new Date().toISOString() } : entry)) } catch { /* read state is non-blocking */ } }
  async function markAllRead() { try { await api.post('/api/v1/notification-read-receipts'); setNotifications((current) => current.map((item) => ({ ...item, read_at: item.read_at ?? new Date().toISOString() }))) } catch (caught) { setAppError(errorMessage(caught, t('mobile.errorNotificationState'))) } }
  async function createDataset() { const name = window.prompt(t('mobile.knowledgeBaseNamePrompt'))?.trim(); if (!name) return; try { const created = await api.post<Dataset>('/api/v1/datasets', { name, description: t('mobile.knowledgeBaseCreatedDescription') }); setDatasets((current) => [created, ...current]) } catch (caught) { setAppError(errorMessage(caught, t('mobile.errorKnowledgeBaseCreate'))) } }

  function logout() { closingRef.current = true; closeSocket(); clearSessionToken(); setAuthenticated(false); setUser(null); setSessions([]); setApprovals([]); setNotifications([]); setDatasets([]); setAssistants([]); setSelectedSession(null); selectedSessionRef.current = null; setProjection(emptyMobileProjection()); closingRef.current = false }

  useEffect(() => {
    const handler = (event: Event) => { event.preventDefault(); setDeferredPrompt(event) }
    window.addEventListener('beforeinstallprompt', handler)
    return () => window.removeEventListener('beforeinstallprompt', handler)
  }, [])

  useEffect(() => {
    const update = (event?: Event) => {
      if (event?.type === 'offline') setIsOffline(true)
      else if (event?.type === 'online') setIsOffline(false)
      else setIsOffline(!navigator.onLine)
    }
    update()
    window.addEventListener('online', update)
    window.addEventListener('offline', update)
    return () => {
      window.removeEventListener('online', update)
      window.removeEventListener('offline', update)
    }
  }, [])

  useEffect(() => {
    setPushSupported('serviceWorker' in navigator && 'PushManager' in window)
    setVoiceSupported('webkitSpeechRecognition' in window || 'SpeechRecognition' in window)
  }, [])

  useEffect(() => {
    if (!pushSupported) return
    navigator.serviceWorker.ready.then((reg) => {
      reg.pushManager.getSubscription().then((sub) => setPushEnabled(Boolean(sub)))
    }).catch(() => {})
  }, [pushSupported])

  async function subscribePush() {
    if (!pushSupported) return
    try {
      const reg = await navigator.serviceWorker.ready
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array('d29ya2FtYS1tb2JpbGUtdmFwaWQtcHVibGljLWtleS1wbGFjZWhvbGRlci0xMjM0NTY3ODkw') as unknown as BufferSource,
      })
      setPushEnabled(true)
      await api.post('/api/v1/push/subscriptions', { endpoint: sub.endpoint, keys: sub.toJSON().keys })
    } catch {
      setPushEnabled(false)
    }
  }

  async function unsubscribePush() {
    if (!pushSupported) return
    try {
      const reg = await navigator.serviceWorker.ready
      const sub = await reg.pushManager.getSubscription()
      if (sub) { await sub.unsubscribe() }
      setPushEnabled(false)
      await api.post('/api/v1/push/subscriptions/remove', {})
    } catch {
      setPushEnabled(false)
    }
  }

  function startVoiceInput() {
    if (!voiceSupported) return
    const SpeechRecognitionCtor = ((window as unknown as Record<string, unknown>).SpeechRecognition as SpeechRecognitionConstructor | undefined) || ((window as unknown as Record<string, unknown>).webkitSpeechRecognition as SpeechRecognitionConstructor | undefined)
    if (!SpeechRecognitionCtor) return
    const recognition = new SpeechRecognitionCtor()
    recognition.lang = navigator.language || 'en-US'
    recognition.interimResults = true
    recognition.maxAlternatives = 1
    recognition.onstart = () => setVoiceListening(true)
    recognition.onend = () => setVoiceListening(false)
    recognition.onresult = (event: SpeechRecognitionEvent) => {
      const transcript = Array.from(event.results).map((r) => r[0].transcript).join('')
      setDraft((current) => current + transcript)
    }
    recognition.onerror = () => setVoiceListening(false)
    recognitionRef.current = recognition
    recognition.start()
  }

  function stopVoiceInput() {
    recognitionRef.current?.stop()
    recognitionRef.current = null
    setVoiceListening(false)
  }

  async function handleInstall() {
    if (!deferredPrompt) return
    const promptEvent = deferredPrompt as Event & { prompt?: () => Promise<void>; userChoice?: Promise<{ outcome: string }> }
    await promptEvent.prompt?.()
    setDeferredPrompt(null)
  }

  useEffect(() => { if (!authenticated) return; void loadWorkspace(); return () => { closingRef.current = true; closeSocket() } }, [authenticated])

  if (!authenticated) {
    return (
      <>
        {isOffline && <div className="notice warning page-notice" role="status" data-testid="offline-banner"><span><WifiOff size={14} />{t('mobile.offlineNotice')}</span></div>}
        <LoginScreen email={email} password={password} error={authError} busy={busy} setEmail={setEmail} setPassword={setPassword} onSubmit={login} />
      </>
    )
  }

  return (
    <>
      {deferredPrompt && (
        <button className="install-button global-install" type="button" data-testid="pwa-install-button" aria-label="Install app" title="Install app" onClick={handleInstall}>Install</button>
      )}
      {isOffline && <div className="notice warning page-notice" role="status" data-testid="offline-banner"><span><WifiOff size={14} />{t('mobile.offlineNotice')}</span></div>}
      <main className="mobile-app">
        <header className="app-header">
          <div className="brand-lockup">
            <div className="brand-mark" aria-hidden="true">W</div>
            <div><strong>WorkAMA</strong><span>{t('mobile.personalWorkspace')}</span></div>
          </div>
          <div className="header-actions">
            <span className={`live-status ${connected ? 'online' : ''}`}><i />{connected ? t('mobile.liveSync') : t('mobile.standby')}</span>
            <LocaleToggle />
            <button className="icon-button" type="button" aria-label={t('mobile.openMenu')} title={t('mobile.openMenu')}><Menu size={20} /></button>
          </div>
        </header>
        {appError && (
          <div className="notice error page-notice" role="alert">
            <span>{appError}</span>
            <button className="icon-button" type="button" aria-label={t('mobile.dismissNotice')} onClick={() => setAppError('')}><X size={16} /></button>
          </div>
        )}
        <div className="mobile-content">
          <Routes>
            <Route path="/chat" element={<ChatListView sessions={sessions} onCreate={createSession} busy={busy} />} />
            <Route path="/chat/:id" element={<ChatDetailView sessions={sessions} selectedSession={selectedSession} projection={projection} draft={draft} busy={busy} connected={connected} isOffline={isOffline} voiceSupported={voiceSupported} voiceListening={voiceListening} setDraft={setDraft} onOpen={openSession} onSend={sendMessage} onStartVoice={startVoiceInput} onStopVoice={stopVoiceInput} />} />
            <Route path="/agents" element={<AgentsView assistants={assistants} busy={busy} onRefresh={loadWorkspace} />} />
            <Route path="/knowledge" element={<KnowledgePageView datasets={datasets} onBack={() => {}} onCreate={createDataset} onRefresh={loadWorkspace} />} />
            <Route path="/settings" element={<SettingsView user={user} balance={balance} security={security} notifications={notifications} pendingApprovals={pendingApprovals} pushSupported={pushSupported} pushEnabled={pushEnabled} onSubscribePush={subscribePush} onUnsubscribePush={unsubscribePush} onRefresh={loadWorkspace} onLogout={logout} />} />
            <Route path="*" element={<Navigate to="/chat" replace />} />
          </Routes>
        </div>
        <BottomNav />
      </main>
    </>
  )
}

function BottomNav() {
  const { t } = useLocale()
  const location = useLocation()
  const activeTab = useMemo<Tab>(() => {
    const path = location.pathname
    if (path.startsWith('/chat')) return 'chat'
    if (path.startsWith('/agents')) return 'agents'
    if (path.startsWith('/knowledge')) return 'knowledge'
    if (path.startsWith('/settings')) return 'settings'
    return 'chat'
  }, [location.pathname])

  return (
    <nav className="bottom-nav" aria-label="Primary navigation">
      {navItems.map(({ id, path, icon: Icon }) => (
        <Link key={id} to={path} className={`nav-link ${activeTab === id ? 'active' : ''}`} aria-label={t(navKeys[id])}>
          <span className="nav-icon"><Icon size={21} strokeWidth={activeTab === id ? 2.5 : 2} /></span>
          <span>{t(navKeys[id])}</span>
        </Link>
      ))}
    </nav>
  )
}

function LoginScreen(props: { email: string; password: string; error: string; busy: boolean; setEmail: (value: string) => void; setPassword: (value: string) => void; onSubmit: (event: FormEvent) => void }) { const { t } = useLocale(); return <main className="auth-screen"><div className="auth-art"><div className="brand-mark large">W</div><span className="auth-orbit orbit-one" /><span className="auth-orbit orbit-two" /></div><div className="auth-copy"><span className="kicker">{t('mobile.loginKicker')}</span><h1>{t('mobile.loginTitle')}</h1><p>{t('mobile.loginSubtitle')}</p></div><form className="auth-form" onSubmit={props.onSubmit}><label htmlFor="mobile-email">{t('mobile.loginEmail')}</label><input id="mobile-email" value={props.email} onChange={(event) => props.setEmail(event.target.value)} type="email" autoComplete="username" placeholder={t('mobile.loginEmailPlaceholder')} required /><label htmlFor="mobile-password">{t('mobile.loginPassword')}</label><input id="mobile-password" value={props.password} onChange={(event) => props.setPassword(event.target.value)} type="password" autoComplete="current-password" placeholder={t('mobile.loginPasswordPlaceholder')} required />{props.error && <p className="notice error" role="alert">{props.error}</p>}<button className="primary-button full" type="submit" disabled={props.busy}>{props.busy ? t('mobile.loginSigningIn') : t('mobile.enterWorkspace')}<ArrowUp size={18} /></button></form><p className="auth-footnote"><LockKeyhole size={14} />{t('mobile.loginFootnote')}</p></main> }
function PageTitle(props: { eyebrow: string; title: string; subtitle?: string; action?: ReactNode }) { return <div className="page-title"><div><span className="kicker">{props.eyebrow}</span><h1>{props.title}</h1>{props.subtitle && <p>{props.subtitle}</p>}</div>{props.action}</div> }

/* ─── Chat List ─────────────────────────────────────────────────── */

function ChatListView(props: { sessions: Session[]; onCreate: () => void; busy: boolean }) {
  const { t } = useLocale()
  const navigate = useNavigate()
  return <section className="view"><PageTitle eyebrow={t('mobile.chatEyebrow')} title={t('mobile.chatTitle')} subtitle={t('mobile.chatSubtitle')} action={<button className="round-action" type="button" aria-label={t('mobile.chatNewConversation')} title={t('mobile.chatNewConversation')} disabled={props.busy} onClick={props.onCreate}><Plus size={20} /></button>} />{props.sessions.length === 0 && <EmptyState icon={<MessageCircle size={23} />} title={t('mobile.chatEmptyTitle')} body={t('mobile.chatEmptyBody')} />}{props.sessions.map((session) => <Link key={session.id} to={`/chat/${session.id}`} className="session-list-card"><span className="session-avatar"><Sparkles size={15} /></span><span className="session-list-info"><strong>{session.title}</strong><small>{session.model} · {session.status}</small></span><ChevronRight size={18} /></Link>)}</section>
}

/* ─── Chat Detail ───────────────────────────────────────────────── */

function ChatDetailView(props: {
  sessions: Session[]
  selectedSession: Session | null
  projection: SessionProjection
  draft: string
  busy: boolean
  connected: boolean
  isOffline: boolean
  voiceSupported: boolean
  voiceListening: boolean
  setDraft: (value: string) => void
  onOpen: (session: Session) => void
  onSend: (event?: FormEvent) => void
  onStartVoice: () => void
  onStopVoice: () => void
}) {
  const { t } = useLocale()
  const navigate = useNavigate()
  const { id } = useParams<{ id: string }>()
  const sessionFromParams = useMemo(() => props.sessions.find((s) => s.id === id) ?? null, [props.sessions, id])
  const session = props.selectedSession?.id === id ? props.selectedSession : sessionFromParams
  const hasOpenedRef = useRef(false)
  const [cachedMessages, setCachedMessages] = useState<Array<{ id: string; role: 'user' | 'assistant'; content: string; timestamp: number }>>([])

  useEffect(() => {
    if (session && session.id !== props.selectedSession?.id && !hasOpenedRef.current) {
      hasOpenedRef.current = true
      void props.onOpen(session)
    }
  }, [session, props.selectedSession, props.onOpen])

  useEffect(() => { hasOpenedRef.current = false }, [id])

  useEffect(() => {
    if (!session) { setCachedMessages([]); return }
    getCachedMessages(session.id).then((msgs) => setCachedMessages(msgs)).catch(() => setCachedMessages([]))
  }, [session?.id])

  const displayMessages = useMemo(() => {
    if (props.projection.messages.length > 0) return props.projection.messages
    return cachedMessages.map((m) => ({ id: m.id, role: m.role, content: m.content, streaming: false }))
  }, [props.projection.messages, cachedMessages])

  return <section className="view chat-view"><div className="chat-detail-header"><button className="icon-button" type="button" aria-label={t('mobile.backToProfile')} title={t('mobile.backToProfile')} onClick={() => navigate('/chat')}><ArrowLeft size={19} /></button><div><span className="kicker">{t('mobile.chatEyebrow')}</span><h1>{session?.title ?? t('mobile.chatChooseConversation')}</h1></div></div><section className="conversation-card"><div className="conversation-toolbar"><div className={`connection-label ${props.connected ? 'online' : ''}`}><i />{props.connected ? t('mobile.chatRealtimeSync') : t('mobile.chatConnecting')}</div>{session && <span>{session.model}</span>}</div><div className="message-list" aria-live="polite">{!session && <EmptyState icon={<MessageCircle size={23} />} title={t('mobile.chatEmptyTitle')} body={t('mobile.chatEmptyBody')} />}{session && displayMessages.length === 0 && <EmptyState icon={<Sparkles size={23} />} title={t('mobile.chatReadyTitle')} body={t('mobile.chatReadyBody')} />}{displayMessages.map((message) => <article key={message.id} className={`message ${message.role}`}><span className="message-role">{message.role === 'user' ? t('mobile.chatRoleYou') : t('mobile.chatRoleAma')}</span><div><p>{message.content}{message.streaming && <span className="cursor" />}</p><time>{message.role === 'user' ? t('mobile.justNow') : 'WorkAMA'}</time></div></article>)}{props.projection.tasks.length > 0 && <TaskProgress projection={props.projection} />}</div><form className="composer" onSubmit={props.onSend}><textarea value={props.draft} aria-label={t('mobile.chatMessageLabel')} rows={2} placeholder={props.connected ? t('mobile.chatPlaceholder') : t('mobile.chatPlaceholderConnecting')} disabled={!props.connected && !props.isOffline} onChange={(event) => props.setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); props.onSend() } }} />{props.voiceSupported && <button className={`voice-button ${props.voiceListening ? 'listening' : ''}`} type="button" aria-label={props.voiceListening ? t('mobile.voiceStop') : t('mobile.voiceStart')} title={props.voiceListening ? t('mobile.voiceStop') : t('mobile.voiceStart')} onClick={props.voiceListening ? props.onStopVoice : props.onStartVoice} disabled={!props.connected && !props.isOffline}><Mic size={18} /></button>}<button className="send-button" type="submit" aria-label={t('mobile.chatSend')} title={t('mobile.chatSend')} disabled={(!props.connected && !props.isOffline) || !props.draft.trim()}><ArrowUp size={20} /></button></form></section></section>
}

function TaskProgress({ projection }: { projection: SessionProjection }) { const { t } = useLocale(); return <div className="task-progress"><div className="task-progress-heading"><span><CheckCircle2 size={16} />{t('mobile.taskProgress')}</span><strong>{projection.taskProgress}%</strong></div><div className="progress"><i style={{ width: `${Math.max(0, Math.min(100, projection.taskProgress))}%` }} /></div>{projection.tasks.slice(0, 3).map((task) => <div className="task-line" key={task.id}><span className={`task-dot ${task.status}`} />{task.title}<small>{task.status === 'completed' ? t('mobile.taskDone') : t('mobile.taskInProgress')}</small></div>)}</div> }

/* ─── Agents ────────────────────────────────────────────────────── */

function AgentsView(props: { assistants: Assistant[]; busy: boolean; onRefresh: () => void }) {
  const { t } = useLocale()
  return <section className="view"><PageTitle eyebrow={t('mobile.agentsEyebrow')} title={t('mobile.agentsTitle')} subtitle={t('mobile.agentsSubtitle')} action={<button className="icon-button" type="button" aria-label={t('mobile.tasksRefresh')} title={t('mobile.tasksRefresh')} onClick={props.onRefresh}><RefreshCw size={18} /></button>} />{props.assistants.length === 0 && <EmptyState icon={<Bot size={23} />} title={t('mobile.agentsEmptyTitle')} body={t('mobile.agentsEmptyBody')} />}{props.assistants.map((assistant) => <article className="dataset-card" key={assistant.id}><div className="dataset-icon"><Bot size={19} /></div><div><strong>{assistant.name}</strong><p>{assistant.description || t('mobile.agentsNoDescription')}</p><span>{assistant.kind ?? 'assistant'} · {assistant.status ?? 'active'}</span></div><ChevronRight size={18} /></article>)}</section>
}

/* ─── Knowledge Page ────────────────────────────────────────────── */

function KnowledgePageView(props: { datasets: Dataset[]; onBack: () => void; onCreate: () => void; onRefresh: () => void }) { const { t } = useLocale(); return <section className="view"><PageTitle eyebrow={t('mobile.knowledgeEyebrow')} title={t('mobile.knowledgeTitle')} action={<div className="title-actions"><button className="icon-button" type="button" aria-label={t('mobile.knowledgeRefresh')} title={t('mobile.knowledgeRefresh')} onClick={props.onRefresh}><RefreshCw size={18} /></button><button className="round-action" type="button" aria-label={t('mobile.knowledgeNew')} title={t('mobile.knowledgeNew')} onClick={props.onCreate}><Plus size={20} /></button></div>} /><div className="search-field"><Search size={17} /><input aria-label={t('mobile.knowledgeSearch')} placeholder={t('mobile.knowledgeSearch')} /></div>{props.datasets.length === 0 && <EmptyState icon={<BookOpen size={23} />} title={t('mobile.knowledgeEmptyTitle')} body={t('mobile.knowledgeEmptyBody')} />}{props.datasets.map((dataset) => <article className="dataset-card knowledge-card-touch" key={dataset.id}><div className="dataset-icon"><BookOpen size={19} /></div><div><strong>{dataset.name}</strong><p>{dataset.description || t('mobile.knowledgeNoDescription')}</p><span>{dataset.document_count ?? 0} {t('mobile.knowledgeSources')} · {dataset.status || 'ready'}</span></div><ChevronRight size={18} /></article>)}</section> }

/* ─── Settings ──────────────────────────────────────────────────── */

function SettingsView(props: {
  user: User | null
  balance: Balance | null
  security: SecurityInfo | null
  notifications: Notification[]
  pendingApprovals: number
  pushSupported: boolean
  pushEnabled: boolean
  onSubscribePush: () => void
  onUnsubscribePush: () => void
  onRefresh: () => void
  onLogout: () => void
}) {
  const { t } = useLocale()
  const [section, setSection] = useState<'home' | 'balance' | 'security' | 'privacy'>('home')
  if (section === 'balance') return <BalanceView balance={props.balance} transactions={[]} onBack={() => setSection('home')} onRefresh={props.onRefresh} />
  if (section === 'security') return <SecurityView security={props.security} onBack={() => setSection('home')} onRefresh={props.onRefresh} />
  if (section === 'privacy') return <PrivacyView onBack={() => setSection('home')} />
  return <section className="view profile-view"><PageTitle eyebrow={t('mobile.settingsEyebrow')} title={t('mobile.settingsTitle')} subtitle={t('mobile.settingsSubtitle')} /><div className="profile-card"><div className="profile-avatar">{(props.user?.display_name || props.user?.email || 'W').slice(0, 1).toUpperCase()}</div><div><strong>{props.user?.display_name || t('mobile.meMemberFallback')}</strong><span>{props.user?.email || t('mobile.meCurrentSession')}</span></div><span className="role-pill">{props.user?.role || t('mobile.meMemberRole')}</span></div><div className="profile-menu"><ProfileMenuItem icon={<CircleDollarSign size={19} />} title={t('mobile.meBalance')} detail={props.balance ? `${numeric(props.balance.available_balance).toFixed(2)} ${t('mobile.creditsAvailable')}` : t('mobile.meBalanceDetail')} onClick={() => setSection('balance')} /><ProfileMenuItem icon={<ShieldCheck size={19} />} title={t('mobile.meSecurity')} detail={props.security?.mfa_enabled ? t('mobile.meSecurityMfaEnabled') : t('mobile.meSecurityDetail')} onClick={() => setSection('security')} /><ProfileMenuItem icon={<FileText size={19} />} title={t('mobile.mePrivacy')} detail={t('mobile.mePrivacyDetail')} onClick={() => setSection('privacy')} /><ProfileMenuItem icon={<Bell size={19} />} title={t('mobile.settingsNotifications')} detail={`${props.notifications.length} ${t('mobile.settingsItems')}`} onClick={() => {}} /><ProfileMenuItem icon={<KeyRound size={19} />} title={t('mobile.settingsApprovals')} detail={`${props.pendingApprovals} ${t('mobile.tasksAwaitingReview')}`} onClick={() => {}} />{props.pushSupported && <ProfileMenuItem icon={props.pushEnabled ? <BellRing size={19} /> : <BellOff size={19} />} title={t('mobile.pushNotifications')} detail={props.pushEnabled ? t('mobile.pushEnabled') : t('mobile.pushDisabled')} onClick={() => { if (props.pushEnabled) props.onUnsubscribePush(); else props.onSubscribePush() }} />}</div><div className="profile-footer"><button className="logout-button" type="button" onClick={props.onLogout}><LogOut size={17} />{t('mobile.meSignOut')}</button><span>{t('mobile.meFooterVersion')}</span></div></section>
}

function ProfileMenuItem(props: { icon: ReactNode; title: string; detail: string; onClick: () => void }) { return <button className="profile-menu-item" type="button" onClick={props.onClick}><span className="menu-icon">{props.icon}</span><span><strong>{props.title}</strong><small>{props.detail}</small></span><ChevronRight size={18} /></button> }
function SubpageTitle(props: { eyebrow: string; title: string; onBack: () => void; action?: ReactNode }) { const { t } = useLocale(); return <div className="subpage-title"><button className="icon-button" type="button" aria-label={t('mobile.backToProfile')} title={t('mobile.backToProfile')} onClick={props.onBack}><ArrowLeft size={19} /></button><div><span className="kicker">{props.eyebrow}</span><h1>{props.title}</h1></div>{props.action}</div> }
function EmptyState(props: { icon: ReactNode; title: string; body: string }) { return <div className="empty-state"><span className="empty-icon">{props.icon}</span><strong>{props.title}</strong><p>{props.body}</p></div> }

function BalanceView(props: { balance: Balance | null; transactions: Transaction[]; onBack: () => void; onRefresh: () => void }) { const { t } = useLocale(); return <section className="view"><SubpageTitle eyebrow={t('mobile.balanceEyebrow')} title={t('mobile.balanceTitle')} onBack={props.onBack} action={<button className="icon-button" type="button" aria-label={t('mobile.balanceRefresh')} title={t('mobile.balanceRefresh')} onClick={props.onRefresh}><RefreshCw size={18} /></button>} /><div className="balance-hero"><span>{t('mobile.balanceAvailable')}</span><strong>{numeric(props.balance?.available_balance).toFixed(2)}</strong><small>{t('mobile.balanceCredits')}</small><div className="balance-breakdown"><span>{t('mobile.balanceTotal')} <b>{numeric(props.balance?.total_balance).toFixed(2)}</b></span><span>{t('mobile.balanceFrozen')} <b>{numeric(props.balance?.frozen_balance).toFixed(2)}</b></span></div></div><div className="section-label"><span>{t('mobile.balanceRecentTransactions')}</span><span className="muted-label">{props.transactions.length} {t('mobile.balanceItems')}</span></div>{props.transactions.length === 0 && <EmptyState icon={<CircleDollarSign size={23} />} title={t('mobile.balanceEmptyTitle')} body={t('mobile.balanceEmptyBody')} />}{props.transactions.map((item) => <div className="transaction-row" key={item.id}><span className={`transaction-icon ${numeric(item.amount) < 0 ? 'spent' : 'added'}`}>{numeric(item.amount) < 0 ? '−' : '+'}</span><span><strong>{item.description || item.kind}</strong><small>{formatDate(item.created_at, t('mobile.justNow'))}</small></span><b className={numeric(item.amount) < 0 ? 'negative' : 'positive'}>{numeric(item.amount) > 0 ? '+' : ''}{numeric(item.amount).toFixed(2)}</b></div>)}</section> }
function SecurityView(props: { security: SecurityInfo | null; onBack: () => void; onRefresh: () => void }) { const { t } = useLocale(); return <section className="view"><SubpageTitle eyebrow={t('mobile.securityEyebrow')} title={t('mobile.securityTitle')} onBack={props.onBack} action={<button className="icon-button" type="button" aria-label={t('mobile.securityRefresh')} title={t('mobile.securityRefresh')} onClick={props.onRefresh}><RefreshCw size={18} /></button>} /><div className="security-status"><span className="status-check"><ShieldCheck size={24} /></span><div><strong>{props.security?.mfa_enabled ? t('mobile.securityProtected') : t('mobile.securityUnprotected')}</strong><p>{props.security?.mfa_enabled ? t('mobile.securityMfaEnabledCopy') : t('mobile.securityMfaDisabledCopy')}</p></div></div><div className="settings-list"><div><span><LockKeyhole size={18} /><strong>{t('mobile.securityMfaLabel')}</strong></span><b className={props.security?.mfa_enabled ? 'enabled' : ''}>{props.security?.mfa_enabled ? t('mobile.securityEnabled') : t('mobile.securityNotEnabled')}</b></div><div><span><KeyRound size={18} /><strong>{t('mobile.securityAccessToken')}</strong></span><b className="enabled">{t('mobile.securityMemoryOnly')}</b></div><div><span><ShieldCheck size={18} /><strong>{t('mobile.securityActiveSessions')}</strong></span><b>{props.security?.sessions?.length ?? 1}</b></div></div><p className="hint">{t('mobile.securityHint')}</p></section> }
function PrivacyView(props: { onBack: () => void }) { const { t } = useLocale(); const [requested, setRequested] = useState(false); async function request(type: 'access' | 'export' | 'correct') { try { await api.post('/api/v1/privacy/data-requests', { request_type: type, scope: 'content' }) } catch { /* the request remains visible for retry in the Web console */ } setRequested(true) } return <section className="view"><SubpageTitle eyebrow={t('mobile.privacyEyebrow')} title={t('mobile.privacyTitle')} onBack={props.onBack} /><div className="privacy-intro"><span className="status-check"><LockKeyhole size={23} /></span><div><strong>{t('mobile.privacyHeadline')}</strong><p>{t('mobile.privacyIntro')}</p></div></div><div className="privacy-actions"><button type="button" onClick={() => void request('access')}><FileText size={18} /><span><strong>{t('mobile.privacyRequestData')}</strong><small>{t('mobile.privacyRequestDataDetail')}</small></span><ChevronRight size={18} /></button><button type="button" onClick={() => void request('export')}><ArrowUp size={18} /><span><strong>{t('mobile.privacyExportData')}</strong><small>{t('mobile.privacyExportDataDetail')}</small></span><ChevronRight size={18} /></button><button type="button" onClick={() => void request('correct')}><ShieldCheck size={18} /><span><strong>{t('mobile.privacyCorrectData')}</strong><small>{t('mobile.privacyCorrectDataDetail')}</small></span><ChevronRight size={18} /></button></div>{requested && <p className="notice success"><CheckCircle2 size={16} />{t('mobile.privacySubmitted')}</p>}<p className="hint">{t('mobile.privacyHint')}</p></section> }

function urlBase64ToUint8Array(base64String: string): Uint8Array {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4)
  const base64 = (base64String + padding).replace(/\-/g, '+').replace(/_/g, '/')
  const rawData = window.atob(base64)
  const outputArray = new Uint8Array(rawData.length)
  for (let i = 0; i < rawData.length; ++i) {
    outputArray[i] = rawData.charCodeAt(i)
  }
  return outputArray
}
