import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { ArrowLeft, ArrowUp, Bell, BookOpen, CheckCircle2, LockKeyhole, MessageCircle, RefreshCw, ShieldCheck, Sparkles, UserRound } from 'lucide-react'
import { useLocale, LocaleToggle } from './locale'
import type { Locale, MessageKey } from '@workama/i18n'

type Row = Record<string, any>
const apiBase = import.meta.env.VITE_PLATFORM_API_URL ?? 'http://localhost:20200'
let accessToken: string | null = null

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  headers.set('content-type', 'application/json')
  if (accessToken) headers.set('authorization', `Bearer ${accessToken}`)
  const response = await fetch(`${apiBase}${path}`, { ...options, headers })
  if (!response.ok) throw new Error(await response.text() || `Request failed (${response.status})`)
  return response.json() as Promise<T>
}

function formatTime(value: any, t: (key: MessageKey) => string, locale: Locale): string {
  if (!value) return ''
  const date = new Date(typeof value === 'string' ? value : Number(value))
  if (Number.isNaN(date.getTime())) return String(value)
  const now = Date.now()
  const diff = now - date.getTime()
  if (diff < 60_000) return t('miniapp.justNow')
  if (diff < 3_600_000) return t('miniapp.minutesAgo').replace('{count}', String(Math.floor(diff / 60_000)))
  if (diff < 86_400_000) return t('miniapp.hoursAgo').replace('{count}', String(Math.floor(diff / 3_600_000)))
  if (diff < 604_800_000) return t('miniapp.daysAgo').replace('{count}', String(Math.floor(diff / 86_400_000)))
  return date.toLocaleDateString(locale)
}

function App() {
  const { t, locale } = useLocale()
  const [email, setEmail] = useState(''); const [password, setPassword] = useState(''); const [authenticated, setAuthenticated] = useState(false); const [error, setError] = useState(''); const [busy, setBusy] = useState(false)
  const [manifest, setManifest] = useState<Row | null>(null); const [sessions, setSessions] = useState<Row[]>([]); const [selected, setSelected] = useState<Row | null>(null); const [messages, setMessages] = useState<Row[]>([]); const [draft, setDraft] = useState(''); const [notice, setNotice] = useState('')
  const [tab, setTab] = useState<'chat' | 'knowledge' | 'notifications' | 'me'>('chat')
  const [meSubView, setMeSubView] = useState<'profile' | 'security'>('profile')

  // Knowledge Q&A state (MP-04)
  const [datasets, setDatasets] = useState<Row[]>([]); const [datasetId, setDatasetId] = useState<string | null>(null)
  const [knowledgeQuery, setKnowledgeQuery] = useState(''); const [knowledgeResults, setKnowledgeResults] = useState<Row[] | null>(null)
  const [knowledgeBusy, setKnowledgeBusy] = useState(false); const [knowledgeError, setKnowledgeError] = useState('')

  // Notifications state (MP-05)
  const [notifications, setNotifications] = useState<Row[]>([]); const [notificationsBusy, setNotificationsBusy] = useState(false)
  const [notificationsError, setNotificationsError] = useState(''); const [unreadCount, setUnreadCount] = useState(0)

  // Security/privacy state (MP-07)
  const [securityInfo, setSecurityInfo] = useState<Row | null>(null); const [securityBusy, setSecurityBusy] = useState(false)
  const [securityError, setSecurityError] = useState('')

  async function login(event: FormEvent) { event.preventDefault(); setBusy(true); setError(''); try { const result = await request<Row>('/api/v1/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) }); if (result.mfa_required) throw new Error(t('miniapp.auth.errorMfa')); accessToken = result.access_token; setAuthenticated(true) } catch (caught) { setError(caught instanceof Error ? caught.message : t('miniapp.auth.errorSignIn')) } finally { setBusy(false) } }
  async function load() { try { const [nextManifest, nextSessions] = await Promise.all([request<Row>('/api/v1/miniapp/bootstrap'), request<{ items: Row[] }>('/api/v1/miniapp/sessions')]); setManifest(nextManifest); setSessions(nextSessions.items ?? []); if (nextSessions.items?.[0]) await openSession(nextSessions.items[0]) } catch (caught) { setError(caught instanceof Error ? caught.message : t('miniapp.errorDataUnavailable')) } }
  async function openSession(session: Row) { setSelected(session); const result = await request<{ items: Row[] }>(`/api/v1/miniapp/sessions/${encodeURIComponent(String(session.id))}/messages`); setMessages(result.items ?? []) }
  async function createSession() { const session = await request<Row>('/api/v1/miniapp/sessions', { method: 'POST' }); setSessions((current) => [session, ...current]); await openSession(session) }
  async function send(event: FormEvent) { event.preventDefault(); const content = draft.trim(); if (!content || !selected) return; setBusy(true); try { const result = await request<Row>(`/api/v1/miniapp/sessions/${encodeURIComponent(String(selected.id))}/messages`, { method: 'POST', body: JSON.stringify({ content }) }); setMessages((current) => [...current, { id: result.user_message_id, role: 'user', content, status: 'delivered' }, { id: result.assistant_message_id, role: 'assistant', content: result.content, status: 'delivered' }]); setDraft('') } catch (caught) { setNotice(caught instanceof Error ? caught.message : t('miniapp.errorMessageFailed')) } finally { setBusy(false) } }

  // Knowledge Q&A handlers (MP-04)
  async function loadDatasets() { setKnowledgeError(''); try { const result = await request<{ items: Row[] }>('/api/v1/datasets'); const items = result.items ?? []; setDatasets(items); if (!datasetId && items[0]) setDatasetId(items[0].id) } catch (caught) { setKnowledgeError(caught instanceof Error ? caught.message : t('miniapp.knowledge.error')) } }
  async function searchKnowledge(event: FormEvent) { event.preventDefault(); const query = knowledgeQuery.trim(); if (!query || !datasetId) return; setKnowledgeBusy(true); setKnowledgeError(''); setKnowledgeResults(null); try { const result = await request<{ items: Row[] }>(`/api/v1/datasets/${encodeURIComponent(datasetId)}/retrieve`, { method: 'POST', body: JSON.stringify({ query }) }); setKnowledgeResults(result.items ?? []) } catch (caught) { setKnowledgeError(caught instanceof Error ? caught.message : t('miniapp.knowledge.searchError')) } finally { setKnowledgeBusy(false) } }

  // Notifications handlers (MP-05)
  async function loadNotifications() { setNotificationsBusy(true); setNotificationsError(''); try { const result = await request<{ items: Row[]; unread_count: number }>('/api/v1/notifications'); setNotifications(result.items ?? []); setUnreadCount(result.unread_count ?? 0) } catch (caught) { setNotificationsError(caught instanceof Error ? caught.message : t('miniapp.notifications.error')) } finally { setNotificationsBusy(false) } }
  async function markNotificationRead(id: string) { try { await request<Row>(`/api/v1/notifications/${encodeURIComponent(id)}/read-receipts`, { method: 'POST' }); const now = new Date().toISOString(); setNotifications((current) => current.map((item) => item.id === id ? { ...item, read_at: item.read_at ?? now } : item)); setUnreadCount((current) => Math.max(0, current - 1)) } catch (caught) { setNotificationsError(caught instanceof Error ? caught.message : t('miniapp.notifications.markError')) } }
  async function markAllNotificationsRead() { try { await request<{ updated: number }>('/api/v1/notification-read-receipts', { method: 'POST' }); const now = new Date().toISOString(); setNotifications((current) => current.map((item) => ({ ...item, read_at: item.read_at ?? now }))); setUnreadCount(0) } catch (caught) { setNotificationsError(caught instanceof Error ? caught.message : t('miniapp.notifications.markError')) } }

  // Security/privacy handlers (MP-07)
  async function loadSecurity() { setSecurityBusy(true); setSecurityError(''); try { const result = await request<Row>('/api/v1/auth/security'); setSecurityInfo(result) } catch (caught) { setSecurityError(caught instanceof Error ? caught.message : t('miniapp.security.error')) } finally { setSecurityBusy(false) } }

  useEffect(() => { if (authenticated) void load() }, [authenticated])
  useEffect(() => { if (authenticated && tab === 'knowledge' && datasets.length === 0 && !knowledgeError) void loadDatasets() }, [authenticated, tab, datasets.length, knowledgeError])
  useEffect(() => { if (authenticated && tab === 'notifications' && notifications.length === 0 && !notificationsError && !notificationsBusy) void loadNotifications() }, [authenticated, tab, notifications.length, notificationsError, notificationsBusy])
  useEffect(() => { if (authenticated && tab === 'me' && meSubView === 'security' && !securityInfo && !securityError && !securityBusy) void loadSecurity() }, [authenticated, tab, meSubView, securityInfo, securityError, securityBusy])

  const unread = useMemo(() => unreadCount, [unreadCount])

  if (!authenticated) return <main className="mini-auth"><div className="mini-brand"><span className="mark"><Sparkles size={17} /></span><span>WorkAMA Miniapp</span></div><div className="auth-panel"><span className="eyebrow">REACT ADAPTER</span><h1>{t('miniapp.auth.title')}</h1><p>{t('miniapp.auth.subtitle')}</p><form onSubmit={login}><label htmlFor="email">{t('miniapp.auth.email')}</label><input id="email" type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="username" required /><label htmlFor="password">{t('miniapp.auth.password')}</label><input id="password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required />{error && <div className="error" role="alert">{error}</div>}<button className="primary" type="submit" disabled={busy}>{busy ? t('miniapp.auth.signingIn') : t('miniapp.auth.enter')}<ArrowUp size={16} /></button></form><small className="boundary"><LockKeyhole size={14} />{t('miniapp.auth.footnote')}</small></div></main>

  return <main className="mini-shell"><header className="mini-header"><div className="mini-brand"><span className="mark"><Sparkles size={16} /></span><div><strong>WorkAMA</strong><small>{t('miniapp.header.workspaceLabel')}</small></div></div><div className="header-state"><span className="dot" />{manifest?.provider_exchange === 'pending_external' ? t('miniapp.header.controlled') : t('miniapp.header.ready')}</div><LocaleToggle /></header><section className="mini-content">

    {tab === 'chat' && <section className="view"><div className="view-head"><div><span className="eyebrow">{t('miniapp.chat.eyebrow')}</span><h1>{t('miniapp.chat.title')}</h1><p>{t('miniapp.chat.subtitle')}</p></div><button className="icon" aria-label={t('miniapp.chat.refresh')} title={t('miniapp.chat.refresh')} onClick={() => void load()}><RefreshCw size={18} /></button></div><div className="session-strip">{sessions.map((session) => <button key={session.id} className={selected?.id === session.id ? 'selected' : ''} onClick={() => void openSession(session)}>{session.id.slice(-8)}</button>)}<button className="new" onClick={() => void createSession()}>{t('miniapp.chat.newConversation')}</button></div><div className="conversation"><div className="conversation-meta"><span><MessageCircle size={14} /> {selected ? t('miniapp.chat.workspaceConversation') : t('miniapp.chat.chooseSession')}</span><span>{t('miniapp.chat.controlledMock')}</span></div><div className="messages">{!messages.length && <div className="empty"><Sparkles size={24} /><strong>{t('miniapp.chat.readyTitle')}</strong><span>{t('miniapp.chat.readyBody')}</span></div>}{messages.map((message) => <article className={message.role === 'assistant' ? 'assistant' : 'user'} key={message.id}><span>{message.role === 'assistant' ? t('miniapp.chat.roleAma') : t('miniapp.chat.roleYou')}</span><p>{message.content}</p></article>)}</div><form className="composer" onSubmit={send}><input aria-label={t('miniapp.chat.send')} value={draft} onChange={(event) => setDraft(event.target.value)} placeholder={selected ? t('miniapp.chat.placeholder') : t('miniapp.chat.placeholderNoSession')} disabled={!selected || busy} /><button className="primary send" type="submit" aria-label={t('miniapp.chat.send')} disabled={!selected || !draft.trim() || busy}><ArrowUp size={18} /></button></form></div>{notice && <div className="notice">{notice}</div>}</section>}

    {tab === 'knowledge' && <section className="view"><div className="view-head"><div><span className="eyebrow">{t('miniapp.knowledge.eyebrow')}</span><h1>{t('miniapp.knowledge.title')}</h1><p>{t('miniapp.knowledge.subtitle')}</p></div><button className="icon" aria-label={t('miniapp.knowledge.refresh')} title={t('miniapp.knowledge.refresh')} onClick={() => void loadDatasets()}><RefreshCw size={18} /></button></div>

      {datasets.length > 0 && <div className="dataset-strip" role="tablist" aria-label="Datasets">{datasets.map((dataset) => <button key={dataset.id} role="tab" aria-selected={datasetId === dataset.id} className={datasetId === dataset.id ? 'selected' : ''} onClick={() => { setDatasetId(dataset.id); setKnowledgeResults(null); setKnowledgeError('') }}>{dataset.name ?? dataset.id.slice(-8)}</button>)}</div>}

      <form className="knowledge-search" onSubmit={searchKnowledge}><input aria-label={t('miniapp.knowledge.search')} value={knowledgeQuery} onChange={(event) => setKnowledgeQuery(event.target.value)} placeholder={datasetId ? t('miniapp.knowledge.searchPlaceholder') : t('miniapp.knowledge.selectDataset')} disabled={!datasetId || knowledgeBusy} /><button className="primary send" type="submit" aria-label={t('miniapp.knowledge.search')} disabled={!datasetId || !knowledgeQuery.trim() || knowledgeBusy}><ArrowUp size={18} /></button></form>

      {knowledgeError && <div className="state-bar"><span className="state-text">{knowledgeError}</span><button className="ghost" onClick={() => void loadDatasets()}>{t('miniapp.retry')}</button></div>}

      {knowledgeBusy && <div className="empty"><RefreshCw size={22} /><strong>{t('miniapp.knowledge.loading')}</strong><span>{t('miniapp.knowledge.loadingHint')}</span></div>}

      {!knowledgeBusy && !knowledgeError && knowledgeResults !== null && knowledgeResults.length === 0 && <div className="empty large"><BookOpen size={24} /><strong>{t('miniapp.knowledge.noResults')}</strong><span>{t('miniapp.knowledge.noResultsHint')}</span></div>}

      {!knowledgeBusy && !knowledgeError && knowledgeResults === null && datasets.length === 0 && !knowledgeError && <div className="empty large"><BookOpen size={24} /><strong>{t('miniapp.knowledge.noDatasets')}</strong><span>{t('miniapp.knowledge.noDatasetsHint')}</span></div>}

      {!knowledgeBusy && !knowledgeError && knowledgeResults !== null && knowledgeResults.length > 0 && <div className="knowledge-results">{knowledgeResults.map((item) => <article className="knowledge-result" key={item.id}><div className="meta"><span className="doc-name" title={item.document_name ?? ''}>{item.document_name ?? item.document_id ?? t('miniapp.knowledge.unknownSource')}</span>{typeof item.vector_score === 'number' && <span className="score">{t('miniapp.knowledge.score')} {item.vector_score.toFixed(3)}</span>}</div><p className="snippet">{item.content ?? ''}</p></article>)}</div>}
    </section>}

    {tab === 'notifications' && <section className="view"><div className="view-head"><div><span className="eyebrow">{t('miniapp.notifications.eyebrow')}</span><h1>{t('miniapp.notifications.title')}</h1><p>{t('miniapp.notifications.subtitle')}</p></div><div className="head-actions"><button className="icon" aria-label={t('miniapp.notifications.refresh')} title={t('miniapp.notifications.refresh')} onClick={() => void loadNotifications()}><RefreshCw size={18} /></button></div></div>

      {notifications.length > 0 && unreadCount > 0 && <div className="notification-toolbar"><span className="toolbar-text">{t('miniapp.notifications.unreadCount').replace('{count}', String(unreadCount))}</span><button className="ghost" onClick={() => void markAllNotificationsRead()}>{t('miniapp.notifications.markAllRead')}</button></div>}

      {notificationsError && <div className="state-bar"><span className="state-text">{notificationsError}</span><button className="ghost" onClick={() => void loadNotifications()}>{t('miniapp.retry')}</button></div>}

      {notificationsBusy && notifications.length === 0 && <div className="empty"><RefreshCw size={22} /><strong>{t('miniapp.notifications.loading')}</strong><span>{t('miniapp.notifications.loadingHint')}</span></div>}

      {!notificationsBusy && !notificationsError && notifications.length === 0 && <div className="empty large"><Bell size={24} /><strong>{t('miniapp.notifications.empty')}</strong><span>{t('miniapp.notifications.emptyHint')}</span></div>}

      {!notificationsError && notifications.length > 0 && <div className="notification-list">{notifications.map((item) => { const isUnread = !item.read_at; return <article className={`notification-item${isUnread ? ' unread' : ''}`} key={item.id}><div className="notification-head"><span className="dot-mark" aria-hidden={!isUnread} /><strong className="title">{item.title ?? item.event_type ?? t('miniapp.notifications.fallbackTitle')}</strong>{item.priority && <span className={`priority priority-${item.priority}`}>{item.priority}</span>}</div>{item.summary && <p className="summary">{item.summary}</p>}<div className="notification-meta"><span className="time">{formatTime(item.created_at, t, locale)}</span>{item.resource_ref && <span className="resource">{item.resource_ref}</span>}{isUnread && <button className="ghost mini" onClick={() => void markNotificationRead(String(item.id))}>{t('miniapp.notifications.markRead')}</button>}</div></article> })}</div>}
    </section>}

    {tab === 'me' && <section className="view"><div className="view-head"><div><span className="eyebrow">{t('miniapp.security.eyebrow')}</span><h1>{meSubView === 'security' ? t('miniapp.security.title') : t('miniapp.me.title')}</h1><p>{t('miniapp.security.subtitle')}</p></div>{meSubView === 'security' && <button className="icon" aria-label={t('miniapp.security.back')} title={t('miniapp.security.back')} onClick={() => setMeSubView('profile')}><ArrowLeft size={18} /></button>}</div>

      {meSubView === 'profile' && <><div className="profile-panel"><span className="profile-icon"><UserRound size={21} /></span><div><strong>{t('miniapp.me.memoryTitle')}</strong><span>{t('miniapp.me.memoryHint')}</span></div><ShieldCheck size={19} className="good" /></div><div className="profile-panel"><span className="profile-icon"><CheckCircle2 size={21} /></span><div><strong>{t('miniapp.me.providerTitle')}</strong><span>{manifest?.provider_exchange ?? t('miniapp.me.providerLoading')}</span></div><span className="status">{t('miniapp.me.providerReview')}</span></div><button className="profile-panel entry" onClick={() => setMeSubView('security')}><span className="profile-icon"><LockKeyhole size={21} /></span><div><strong>{t('miniapp.security.entryTitle')}</strong><span>{t('miniapp.security.entryHint')}</span></div><ArrowUp size={16} className="caret" /></button></>}

      {meSubView === 'security' && <>{securityError && <div className="state-bar"><span className="state-text">{securityError}</span><button className="ghost" onClick={() => void loadSecurity()}>{t('miniapp.retry')}</button></div>}{securityBusy && !securityInfo && <div className="empty"><RefreshCw size={22} /><strong>{t('miniapp.security.loading')}</strong><span>{t('miniapp.security.loadingHint')}</span></div>}{securityInfo && <><div className="profile-panel"><span className="profile-icon"><ShieldCheck size={21} /></span><div><strong>{t('miniapp.security.mfaTitle')}</strong><span>{securityInfo.mfa_enabled ? t('miniapp.security.mfaEnabled') : t('miniapp.security.mfaDisabled')}</span></div><span className={`status ${securityInfo.mfa_enabled ? 'good' : ''}`}>{securityInfo.mfa_enabled ? t('miniapp.status.on') : t('miniapp.status.off')}</span></div><div className="profile-panel"><span className="profile-icon"><MessageCircle size={21} /></span><div><strong>{t('miniapp.security.activeSessionsTitle')}</strong><span>{t('miniapp.security.activeSessionsHint')}</span></div><span className="status">{Number(securityInfo.active_sessions ?? 0)}</span></div><div className="profile-panel"><span className="profile-icon"><LockKeyhole size={21} /></span><div><strong>{t('miniapp.security.privacyTitle')}</strong><span>{t('miniapp.security.privacyHint')}</span></div><ShieldCheck size={19} className="good" /></div><div className="info-panel"><BookOpen size={22} /><div><strong>{t('miniapp.security.connectedTitle')}</strong><span>{t('miniapp.security.connectedHint')}</span></div></div></>}</>}
    </section>}

  </section><nav className="mini-nav"><button className={tab === 'chat' ? 'active' : ''} onClick={() => setTab('chat')}><MessageCircle size={18} />{t('miniapp.nav.chat')}</button><button className={tab === 'knowledge' ? 'active' : ''} onClick={() => setTab('knowledge')}><BookOpen size={18} />{t('miniapp.nav.knowledge')}</button><button className={tab === 'notifications' ? 'active' : ''} onClick={() => setTab('notifications')}><Bell size={18} />{t('miniapp.nav.notifications')}{unread > 0 && <b>{unread}</b>}</button><button className={tab === 'me' ? 'active' : ''} onClick={() => { setTab('me'); setMeSubView('profile') }}><UserRound size={18} />{t('miniapp.nav.me')}</button></nav></main>
}

export default App
