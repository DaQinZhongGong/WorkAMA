import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type FormEvent, type ReactNode } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Activity, Archive, ArrowLeft, ArrowUpRight, Bot, Check, ChevronRight, CircleDot, Clock3, Code2, Container, Database, Download, FileCode2, FileText, Filter, GitBranch, Globe2, KeyRound, LayoutDashboard, Link2, LockKeyhole, MessageSquare, MoreHorizontal, Paperclip, Pause, Play, Plus, RefreshCw, RotateCcw, Save, Search, Send, Server, Settings2, ShieldAlert, SlidersHorizontal, Sparkles, Table2, Terminal, Trash2, Upload, Users, Workflow, X, Zap } from 'lucide-react'
import { createWSClient } from '@workama/api-client'
import type { WSClient } from '@workama/api-client'
import { api, agentWsUrl, platformUrl } from './api'
import { useAuth } from './auth'
import { useLocale } from './locale'
import type { AgentEvent, Dataset, ListResponse, Project, Session, User, Workflow as WorkflowType } from './types'
import { applyEvent, emptyProjection, projectEvents, type SessionProjection } from '@workama/event-renderer'
import type { MessageKey } from '@workama/i18n'
import { Badge, Button, DataTable, EmptyAction, Field, IconButton, Kpi, Modal, PageHeader, Panel, SearchBox, StateView, Status, Toast } from './ui'

function errorMessage(error: unknown, t: (key: MessageKey) => string) { return error instanceof Error ? error.message : t('errors.requestFailed') }
function useList<T>(path: string) { const { t } = useLocale(); const query = useQuery({ queryKey: ['workama', path], queryFn: () => api.get<ListResponse<T>>(path) }); return { items: query.data?.items ?? [], setItems: (_items: T[]) => undefined, loading: query.isLoading, error: query.error ? errorMessage(query.error, t) : '', reload: () => { void query.refetch() } } }
function Notice({ children }: { children: ReactNode }) { return <div className="alert alert-info">{children}</div> }
function PageState({ loading, error, empty, onRetry, children }: { loading: boolean; error: string; empty: boolean; onRetry: () => void; children: ReactNode }) { if (loading) return <StateView state="loading" />; if (error) return <StateView state="error" description={error} onRetry={onRetry} />; if (empty) return <StateView state="empty" />; return <>{children}</> }
const knowledgeStatusKeys: Record<string, MessageKey> = { active: 'governance.status.active', indexed: 'governance.status.indexed', pending: 'governance.status.pending', processing: 'governance.status.processing', parsing: 'governance.status.parsing', chunking: 'governance.status.chunking', embedding: 'governance.status.embedding', failed: 'governance.status.failed', cancelled: 'governance.status.cancelled', deleting: 'governance.status.deleting', deleted: 'governance.status.deleted', building: 'governance.status.building', ready: 'governance.status.ready', retired: 'governance.status.retired' }
function localizedStatus(t: (key: MessageKey) => string, value: unknown, keys = knowledgeStatusKeys) { const raw = String(value ?? 'unknown').toLowerCase(); return keys[raw] ? t(keys[raw]) : String(value ?? 'unknown') }

export function AuthPage({ mode = 'login' }: { mode?: 'login' | 'register' }) {
  const { login, register } = useAuth(); const navigate = useNavigate(); const { t } = useLocale(); const [email, setEmail] = useState(''); const [password, setPassword] = useState(''); const [name, setName] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [notice, setNotice] = useState('')
  async function submit(event: FormEvent) { event.preventDefault(); setBusy(true); setError(''); try { if (mode === 'login') { const result = await login(email, password); if (result.mfaRequired) navigate('/mfa/challenge'); else navigate('/chat') } else { const result = await register(email, password, name) as { verification_required?: boolean }; setNotice(result.verification_required ? t('auth.verifyInbox') : t('auth.accountCreated')); navigate('/verify-email', { state: { email } }) } } catch (caught) { setError(errorMessage(caught, t)) } finally { setBusy(false) } }
  async function startSso() { setBusy(true); setError(''); try { const result = await api.get<{ authorization_url: string }>('/api/v1/auth/oauth/google/authorize'); window.location.assign(result.authorization_url) } catch (caught) { setError(errorMessage(caught, t)); setBusy(false) } }
  return <div className="auth-shell"><div className="auth-rail"><div className="auth-logo"><Sparkles size={18} />{t('public.brand')}</div><div className="auth-rail-copy"><span className="eyebrow">{t('auth.eyebrowAi')}</span><h2>{t('auth.tagline')}</h2><p>{t('auth.taglineDescription')}</p><div className="auth-quote"><CircleDot size={15} /><span>{t('auth.quote')}</span></div></div><small>{t('auth.infraNote')}</small></div><main className="auth-main"><Link to="/" className="auth-mobile-brand"><Sparkles size={18} />{t('public.brand')}</Link><div className="auth-panel"><span className="eyebrow">{mode === 'login' ? t('auth.welcomeBack') : t('auth.startWorkspace')}</span><h1>{mode === 'login' ? t('auth.signInTitle') : t('auth.registerTitle')}</h1><p>{mode === 'login' ? t('auth.signInDesc') : t('auth.registerDesc')}</p>{error && <div className="alert alert-error">{error}</div>}{notice && <Notice>{notice}</Notice>}<form className="form-stack" onSubmit={submit}>{mode === 'register' && <Field label={t('auth.displayName')}><input id="display-name" value={name} onChange={(event) => setName(event.target.value)} required autoComplete="name" placeholder={t('auth.displayNamePlaceholder')} /></Field>}<Field label={t('auth.workEmail')}><input id="email" type="email" value={email} onChange={(event) => setEmail(event.target.value)} required autoComplete="email" placeholder={t('auth.emailPlaceholder')} /></Field><Field label={t('auth.password')} hint={mode === 'register' ? t('auth.passwordHint') : undefined}><input id="password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} required minLength={mode === 'register' ? 12 : 1} autoComplete={mode === 'login' ? 'current-password' : 'new-password'} placeholder="••••••••••••" /></Field>{mode === 'login' && <Link className="form-link" to="/forgot-password">{t('auth.forgotPassword')}</Link>}<Button type="submit" variant="primary" loading={busy}>{mode === 'login' ? t('auth.signIn') : t('auth.createAccount')}<ArrowUpRight size={16} /></Button></form><div className="auth-divider"><span>{t('auth.or')}</span></div><button type="button" className="sso-button" disabled={busy} onClick={() => void startSso()}><Globe2 size={16} />{busy ? t('auth.connectingSso') : t('auth.continueSso')}</button><p className="auth-switch">{mode === 'login' ? t('auth.newToWorkama') : t('auth.haveAccount')} <Link to={mode === 'login' ? '/register' : '/login'}>{mode === 'login' ? t('auth.createAccount') : t('auth.signIn')}</Link></p></div><p className="auth-legal">{t('auth.legalPrefix')} <Link to="/terms">{t('auth.legalTerms')}</Link> <Link to="/privacy">{t('auth.legalPrivacy')}</Link>.</p></main></div>
}

export function UtilityPage({ mode }: { mode: string }) { const { t } = useLocale(); const [email, setEmail] = useState(''); const [token, setToken] = useState(''); const [password, setPassword] = useState(''); const [notice, setNotice] = useState(''); const [error, setError] = useState(''); const navigate = useNavigate(); async function submit(event: FormEvent) { event.preventDefault(); setError(''); try { if (mode === 'forgot-password') { await api.post('/api/v1/auth/forgot-password', { email }); setNotice(t('auth.utility.recoveryRequested')) } else if (mode === 'reset-password') { await api.post('/api/v1/auth/reset-password', { token, password }); navigate('/login') } else if (mode === 'verify-email') { const result = await api.post<{ access_token: string; user: User }>('/api/v1/auth/verify-email', { token }); if (result.access_token) navigate('/login') } else if (mode === 'mfa') { const result = await api.post<{ access_token: string }>('/api/v1/auth/mfa/challenge', { ticket: sessionStorage.getItem('workama_mfa_ticket'), code: token }); if (result.access_token) navigate('/chat') } } catch (caught) { setError(errorMessage(caught, t)) } } const labels: Record<string, [MessageKey, MessageKey]> = { 'forgot-password': ['auth.utility.forgotTitle', 'auth.utility.forgotDesc'], 'reset-password': ['auth.utility.resetTitle', 'auth.utility.resetDesc'], 'verify-email': ['auth.utility.verifyTitle', 'auth.utility.verifyDesc'], mfa: ['auth.utility.mfaTitle', 'auth.utility.mfaDesc'] }; const [titleKey, descKey] = labels[mode] ?? labels['forgot-password']; return <div className="auth-shell simple"><main className="auth-main"><Link to="/login" className="auth-mobile-brand"><Sparkles size={18} />{t('public.brand')}</Link><div className="auth-panel"><span className="eyebrow">{t('auth.utility.security')}</span><h1>{t(titleKey)}</h1><p>{t(descKey)}</p>{error && <div className="alert alert-error">{error}</div>}{notice && <Notice>{notice}</Notice>}<form className="form-stack" onSubmit={submit}>{mode === 'forgot-password' && <Field label={t('auth.utility.workEmail')}><input type="email" value={email} onChange={(event) => setEmail(event.target.value)} required /></Field>}{mode === 'reset-password' && <><Field label={t('auth.utility.resetToken')}><input value={token} onChange={(event) => setToken(event.target.value)} required /></Field><Field label={t('auth.utility.newPassword')}><input type="password" minLength={12} value={password} onChange={(event) => setPassword(event.target.value)} required /></Field></>}{mode === 'verify-email' && <Field label={t('auth.utility.verifyToken')}><input value={token} onChange={(event) => setToken(event.target.value)} required /></Field>}{mode === 'mfa' && <Field label={t('auth.utility.authCode')}><input inputMode="numeric" maxLength={6} value={token} onChange={(event) => setToken(event.target.value)} required /></Field>}<Button type="submit" variant="primary">{t('auth.utility.continue')}<ArrowUpRight size={16} /></Button></form><Link className="back-link" to="/login"><ArrowLeft size={15} />{t('auth.utility.backToSignIn')}</Link></div></main></div> }

const promptFamilies = [
  { id: 'decide', labelKey: 'chat.family.decide', promptKeys: ['chat.prompt.decide.risk', 'chat.prompt.decide.memo', 'chat.prompt.decide.compare', 'chat.prompt.decide.record', 'chat.prompt.decide.assumptions', 'chat.prompt.decide.evidence', 'chat.prompt.decide.brief', 'chat.prompt.decide.next'] },
  { id: 'operate', labelKey: 'chat.family.operate', promptKeys: ['chat.prompt.operate.owner', 'chat.prompt.operate.plan', 'chat.prompt.operate.incident', 'chat.prompt.operate.review', 'chat.prompt.operate.blockers', 'chat.prompt.operate.tasks', 'chat.prompt.operate.handoff', 'chat.prompt.operate.release'] },
  { id: 'learn', labelKey: 'chat.family.learn', promptKeys: ['chat.prompt.learn.explain', 'chat.prompt.learn.policy', 'chat.prompt.learn.changes', 'chat.prompt.learn.briefing', 'chat.prompt.learn.sources', 'chat.prompt.learn.gaps', 'chat.prompt.learn.study', 'chat.prompt.learn.verify'] },
  { id: 'build', labelKey: 'chat.family.build', promptKeys: ['chat.prompt.build.review', 'chat.prompt.build.checklist', 'chat.prompt.build.contract', 'chat.prompt.build.diagnose', 'chat.prompt.build.testPlan', 'chat.prompt.build.tasks', 'chat.prompt.build.diff', 'chat.prompt.build.next'] },
] as const

export function ChatPage() { const { sessionId } = useParams(); return sessionId ? <ChatSessionPage /> : <ChatIndexPage /> }
function ChatIndexPage() {
  const { t } = useLocale()
  const navigate = useNavigate()
  const { items, loading, error, reload } = useList<Session>('/api/v1/sessions')
  const datasets = useList<Dataset>('/api/v1/datasets')
  const policies = useQuery({ queryKey: ['workama', '/api/v1/security/moderation-policies'], queryFn: () => api.get<ListResponse<Record<string, unknown>>>('/api/v1/security/moderation-policies') })
  const [familyId, setFamilyId] = useState<(typeof promptFamilies)[number]['id']>('decide')
  const [busy, setBusy] = useState(false)
  const family = promptFamilies.find((item) => item.id === familyId) ?? promptFamilies[0]
  const activeSessions = items.filter((item) => ['running', 'waiting_approval'].includes(item.status)).length
  const waitingApproval = items.filter((item) => item.status === 'waiting_approval').length
  const recordedSteps = items.reduce((sum, item) => sum + Number(item.used_steps ?? 0), 0)
  const activePolicies = (policies.data?.items ?? []).filter((item) => String(item.status ?? '').toLowerCase() === 'active').length
  const policyCount = policies.data?.items.length ?? 0
  const policyCoverage = policies.isPending ? '--' : policyCount ? `${Math.round((activePolicies / policyCount) * 100)}%` : '--'
  const indexedDatasets = datasets.items.filter((item) => ['indexed', 'active', 'ready'].includes(String(item.status ?? '').toLowerCase())).length

  async function createSession(promptKey?: MessageKey) {
    setBusy(true)
    const prompt = promptKey ? t(promptKey) : ''
    try {
      const created = await api.post<Session>('/api/v1/sessions', {
        title: prompt ? prompt.slice(0, 52) : t('chat.newConversation'),
        model: 'workama-chat',
        agent_kind: 'ama_chat',
        toolset: ['web_search', 'file.read'],
        canvas_enabled: true,
        max_steps: 50,
      })
      navigate('/chat/' + created.id + (prompt ? '?prompt=' + encodeURIComponent(prompt) : ''))
    } catch {
      // The page state surfaces the failed refresh without hiding the workflow.
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <PageHeader
        eyebrow={t('chat.eyebrow')}
        title={t('chat.commandCenter')}
        description={t('chat.description')}
        actions={<Button variant="primary" icon={<Plus size={16} />} loading={busy} onClick={() => void createSession()}>{t('chat.newConversation')}</Button>}
      />
      <div className="kpi-grid">
        <Kpi label={t('chat.activeConversations')} value={loading ? '--' : String(activeSessions).padStart(2, '0')} icon={<MessageSquare size={18} />} trend={t('chat.liveAcrossWorkspace')} />
        <Kpi label={t('chat.knowledgeSources')} value={datasets.loading ? '--' : String(datasets.items.length).padStart(2, '0')} icon={<Database size={18} />} trend={datasets.error ? t('chat.dataUnavailable') : `${indexedDatasets}/${datasets.items.length} ${t('chat.indexed')}`} />
        <Kpi label={t('chat.stepsThisMonth')} value={loading ? '--' : recordedSteps.toLocaleString()} icon={<Zap size={18} />} trend={t('chat.belowBudget')} />
        <Kpi label={t('chat.policyCoverage')} value={policyCoverage} icon={<ShieldAlert size={18} />} trend={policies.isError ? t('chat.dataUnavailable') : activePolicies === policyCount && policyCount > 0 ? t('chat.allSystemsOperational') : t('security.actionRecommended')} />
      </div>
      <div className="chat-index-grid">
        <Panel title={t('chat.startWithPrompt')} subtitle={t('chat.governedEntryPoints')}>
          <div className="prompt-filters" role="tablist" aria-label={t('chat.promptFamilies')}>
            {promptFamilies.map((item) => <button key={item.id} type="button" role="tab" aria-selected={family.id === item.id} className={family.id === item.id ? 'active' : ''} onClick={() => setFamilyId(item.id)}>{t(item.labelKey)}<span>{item.promptKeys.length}</span></button>)}
          </div>
          <div className="prompt-grid">
            {family.promptKeys.map((promptKey) => <button className="prompt-card" key={promptKey} onClick={() => void createSession(promptKey)}><Sparkles size={17} /><span>{t(promptKey)}</span><ArrowUpRight size={15} /></button>)}
          </div>
        </Panel>
        <Panel title={t('chat.workspacePulse')} subtitle={t('chat.last24Hours')}>
          <div className="pulse-list">
            <div><span className="pulse-icon green"><Activity size={15} /></span><div><strong>{t('chat.governanceChecks')}</strong><small>{policyCount ? `${activePolicies}/${policyCount} ${t('chat.activePolicies')}` : t('chat.dataUnavailable')}</small></div><Badge tone={activePolicies > 0 ? 'success' : 'warning'}>{activePolicies > 0 ? t('chat.healthy') : t('chat.review')}</Badge></div>
            <div><span className="pulse-icon blue"><Database size={15} /></span><div><strong>{t('chat.knowledgeIndexing')}</strong><small>{datasets.loading ? '--' : `${indexedDatasets}/${datasets.items.length} ${t('chat.indexed')}`}</small></div><Badge tone={indexedDatasets > 0 ? 'info' : 'warning'}>{indexedDatasets > 0 ? t('chat.synced') : t('chat.review')}</Badge></div>
            <div><span className="pulse-icon purple"><Workflow size={15} /></span><div><strong>{t('chat.workflowRuntime')}</strong><small>{loading ? '--' : `${waitingApproval} ${t('chat.waitingApprovals')} / ${items.length} ${t('chat.sessions')}`}</small></div><Badge tone={waitingApproval > 0 ? 'warning' : 'success'}>{waitingApproval > 0 ? t('chat.review') : t('chat.healthy')}</Badge></div>
          </div>
        </Panel>
      </div>
      <Panel title={t('chat.recentConversations')} subtitle={String(items.length) + ' ' + t('chat.conversationsInWorkspace')} actions={<Link className="panel-action" to="/search">{t('chat.viewAll')}</Link>}>
        <PageState loading={loading} error={error} empty={!items.length} onRetry={reload}>
          <div className="session-grid">
            {items.slice(0, 8).map((item) => <button className="session-card" key={item.id} onClick={() => navigate('/chat/' + item.id)}><div className="session-card-icon"><MessageSquare size={17} /></div><div className="session-card-content"><strong>{item.title || t('chat.untitledConversation')}</strong><span>{item.model} · {item.updated_at ? new Date(item.updated_at).toLocaleString() : t('chat.recentlyUpdated')}</span></div><Status value={item.status} /><ChevronRight size={16} /></button>)}
          </div>
        </PageState>
      </Panel>
    </>
  )
}
function ChatSessionPage() { const { sessionId = '' } = useParams(); const navigate = useNavigate(); const { t } = useLocale(); const [session, setSession] = useState<Session | null>(null); const [projection, setProjection] = useState<SessionProjection>(emptyProjection()); const [draft, setDraft] = useState(''); const [connected, setConnected] = useState(false); const [loading, setLoading] = useState(true); const [error, setError] = useState(''); const socket = useRef<WSClient | null>(null); const endRef = useRef<HTMLDivElement>(null); const ingest = useCallback((event: AgentEvent) => { setProjection((current) => applyEvent(current, event)); }, []); useEffect(() => { let active = true; async function load() { try { const [sessionData, events] = await Promise.all([api.get<Session>(`/api/v1/sessions/${sessionId}`), api.get<ListResponse<AgentEvent>>(`/api/v1/sessions/${sessionId}/events`)]); if (!active) return; setSession(sessionData); setProjection(projectEvents(events.items)); const ticket = await api.post<{ ticket: string }>(`/api/v1/sessions/${sessionId}/ws-tickets`); if (!active) return; const afterSeq = events.items.at(-1)?.seq ?? 0; socket.current = createWSClient(`${agentWsUrl}/ws/sessions/${sessionId}?ticket=${encodeURIComponent(ticket.ticket)}`, { after: afterSeq, autoAck: true, reconnect: false, onOpen: () => setConnected(true), onClose: () => setConnected(false), onError: () => setError(t('chat.session.realtimeError')), onMessage: (message) => { try { const event = message as AgentEvent & { payload?: Record<string, unknown> }; if (event.type === 'session.snapshot') { setProjection(projectEvents((event.payload?.events as AgentEvent[]) ?? [])); return } if (event.type !== 'connection.ready' && event.type !== 'connection.warning') ingest(event) } catch { setError(t('chat.session.invalidEvent')) } } }); } catch (caught) { if (active) setError(errorMessage(caught, t)) } finally { if (active) setLoading(false) } } void load(); return () => { active = false; socket.current?.close() } }, [ingest, sessionId, t]); useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [projection.messages.length, projection.lastSeq]); function send() { const content = draft.trim(); if (!content || !connected || projection.running) return; socket.current?.send({ type: 'message.create', content, attachment_ids: [] }); setDraft('') } async function control(action: 'pause' | 'resume' | 'cancel') { try { const result = await api.post<{ status: string }>(`/api/v1/sessions/${sessionId}/${action}`, { reason: 'Console action' }); setSession((current) => current ? { ...current, status: result.status } : current) } catch (caught) { setError(errorMessage(caught, t)) } } return <div className="chat-workspace"><header className="chat-toolbar"><div className="chat-title"><IconButton label={t('chat.session.backToList')} onClick={() => navigate('/chat')}><ArrowLeft size={17} /></IconButton><div><strong>{session?.title ?? t('chat.session.conversationFallback')}</strong><span><i className={`status-dot ${connected ? 'status-success' : 'status-warning'}`} />{connected ? t('chat.session.connected') : t('chat.session.connecting')} · {session?.model ?? 'workama-chat'}</span></div></div><div className="chat-controls">{session?.status === 'running' && <IconButton label={t('chat.session.pauseRun')} onClick={() => void control('pause')}><Pause size={16} /></IconButton>}{session?.status === 'paused' && <IconButton label={t('chat.session.resumeRun')} onClick={() => void control('resume')}><Play size={16} /></IconButton>}<IconButton label={t('chat.session.cancelRun')} onClick={() => void control('cancel')}><X size={16} /></IconButton><Badge tone="info">{projection.usage.steps || session?.used_steps || 0} / {session?.max_steps ?? 50} {t('chat.session.steps')}</Badge></div></header><div className="message-list" aria-live="polite"><PageState loading={loading} error={error} empty={!projection.messages.length} onRetry={() => window.location.reload()}>{projection.messages.map((message) => <article className={`message ${message.role}`} key={message.id}><div className="message-avatar">{message.role === 'user' ? <Users size={16} /> : <Bot size={16} />}</div><div className="message-content"><span className="message-role">{message.role === 'user' ? t('chat.session.you') : t('chat.session.ama')}</span><p>{message.content}{message.streaming && <span className="stream-caret" />}</p></div></article>)}{projection.tasks.length > 0 && <div className="task-card"><div><strong>{t('chat.session.executionPlan')}</strong><span>{projection.taskProgress}% {t('chat.session.complete')}</span></div><div className="progress"><i style={{ width: `${projection.taskProgress}%` }} /></div>{projection.tasks.map((task) => <div className="task-row" key={task.id}><Check size={14} className={task.status === 'completed' ? 'task-done' : ''} /><span>{task.title}</span><Status value={task.status} /></div>)}</div>}{projection.approvals.map((approval) => <div className="approval-card" key={approval.id} data-testid="chat-approval-card" data-approval-id={approval.id} data-approval-status={approval.status} data-approval-tool={approval.target}><div><ShieldAlert size={17} /><strong>{t('chat.session.approvalRequired')}</strong><Badge tone="warning">{approval.risk}</Badge><span>{approval.target}</span></div>{approval.status === 'pending' && <div className="approval-actions"><Button variant="secondary" onClick={() => void api.post(`/api/v1/approvals/${approval.id}/decisions`, { decision: 'rejected', reason: 'Rejected in console' })} data-testid="chat-approval-reject-button">{t('chat.session.reject')}</Button><Button variant="primary" onClick={() => void api.post(`/api/v1/approvals/${approval.id}/decisions`, { decision: 'approved', reason: 'Approved in console' })} data-testid="chat-approval-approve-button">{t('chat.session.approveOnce')}</Button></div>}</div>)}<div ref={endRef} /></PageState></div><div className="composer-wrap"><div className="composer"><IconButton label={t('chat.session.attachFile')}><Paperclip size={17} /></IconButton><textarea aria-label={t('chat.session.messageLabel')} value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send() } }} placeholder={connected ? t('chat.session.placeholder') : t('chat.session.placeholderWaiting')} /><Button variant="primary" icon={<Send size={16} />} disabled={!connected || !draft.trim() || projection.running} onClick={send}>{t('chat.session.send')}</Button></div><div className="composer-hint"><span>{t('chat.session.hint')}</span><span><LockKeyhole size={13} />{t('chat.session.governed')}</span></div></div></div> }

export function KnowledgePage() {
  const { datasetId } = useParams()
  return datasetId ? <DatasetDetail id={datasetId} /> : <KnowledgeIndexPage />
}

function KnowledgeIndexPage() {
  const { t } = useLocale()
  const { items, loading, error, reload } = useList<Dataset>('/api/v1/datasets')
  const evalRuns = useList<Record<string, unknown>>('/api/v1/rag/eval-runs')
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState('')
  const indexedDatasets = items.filter((item) => ['indexed', 'active', 'ready'].includes(String(item.status ?? '').toLowerCase())).length
  const latestRun = evalRuns.items
    .filter((item) => String(item.status ?? '').toLowerCase() === 'succeeded')
    .sort((left, right) => String(right.created_at ?? '').localeCompare(String(left.created_at ?? '')))[0]
  const latestMetrics = latestRun?.metrics && typeof latestRun.metrics === 'object' ? latestRun.metrics as Record<string, unknown> : null
  const retrievalQuality = evalRuns.loading ? '--' : latestMetrics?.hit_rate_at_k == null ? '--' : `${Math.round(Number(latestMetrics.hit_rate_at_k) * 100)}%`
  const syncHealth = loading ? '--' : items.length ? `${indexedDatasets}/${items.length}` : '--'

  async function create(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setActionError('')
    try {
      await api.post('/api/v1/datasets', { name, description })
      setOpen(false)
      setName('')
      setDescription('')
      reload()
    } catch (caught) {
      setActionError(errorMessage(caught, t))
    } finally {
      setBusy(false)
    }
  }

  const filtered = items.filter((item) => `${item.name} ${item.description ?? ''}`.toLowerCase().includes(query.toLowerCase()))
  return <>
    <PageHeader eyebrow="KNOWLEDGE OS" title="Knowledge" description={t('knowledge.description')} actions={<>
      <Button icon={<RefreshCw size={16} />} onClick={reload}>{t('knowledge.refresh')}</Button>
      <Button icon={<Upload size={16} />} onClick={() => setOpen(true)}>{t('knowledge.addSource')}</Button>
      <Button variant="primary" icon={<Plus size={16} />} onClick={() => setOpen(true)}>{t('knowledge.newBase')}</Button>
    </>} />
    <div className="kpi-grid">
      <Kpi label={t('knowledge.bases')} value={String(items.length).padStart(2, '0')} icon={<Database size={18} />} trend={t('knowledge.workspaceScoped')} />
      <Kpi label={t('knowledge.indexedDocuments')} value={String(items.reduce((sum, item) => sum + (item.document_count ?? 0), 0))} icon={<FileText size={18} />} trend={t('knowledge.readyForRetrieval')} />
      <Kpi label={t('knowledge.retrievalQuality')} value={retrievalQuality} icon={<Sparkles size={18} />} trend={latestRun ? t('knowledge.readyForRetrieval') : t('knowledge.awaitingEvaluation')} />
      <Kpi label={t('knowledge.syncHealth')} value={syncHealth} icon={<Activity size={18} />} trend={t('knowledge.readyForRetrieval')} />
    </div>
    <Panel title={t('knowledge.bases')} subtitle={t('knowledge.basesSubtitle')} actions={<SearchBox value={query} onChange={setQuery} placeholder={t('knowledge.searchBases')} />}>
      <PageState loading={loading} error={error} empty={!filtered.length} onRetry={reload}>
        <div className="resource-grid">{filtered.map((item) => {
          const status = String(item.status ?? 'active')
          return <Link to={`/knowledge/${item.id}`} className="resource-card" key={item.id}>
            <div className="resource-icon blue"><Database size={18} /></div>
            <div className="resource-main"><strong>{item.name}</strong><p>{item.description || t('knowledge.noDescription')}</p><span>{item.document_count ?? 0} {t('knowledge.documents')} · {localizedStatus(t, status)}</span></div>
            <ChevronRight size={16} />
          </Link>
        })}</div>
      </PageState>
    </Panel>
    {actionError && <div className="alert alert-error" role="alert">{actionError}</div>}
    {open && <Modal title={t('knowledge.createBase')} onClose={() => setOpen(false)}><form className="form-stack" onSubmit={create}>
      <Field label={t('knowledge.name')}><input id="dataset-name" value={name} onChange={(event) => setName(event.target.value)} required placeholder={t('knowledge.namePlaceholder')} /></Field>
      <Field label={t('knowledge.descriptionField')}><textarea id="dataset-description" value={description} onChange={(event) => setDescription(event.target.value)} placeholder={t('knowledge.descriptionPlaceholder')} /></Field>
      <Button type="submit" variant="primary" loading={busy}>{t('knowledge.createBase')}</Button>
    </form></Modal>}
  </>
}
type KnowledgeDocument = Record<string, unknown>
type KnowledgeChunk = Record<string, unknown>
type KnowledgeGeneration = Record<string, unknown>
type RetrievalConfig = { top_k: number; candidate_k: number; rrf_k: number; score_threshold: number }
type RetrievalConfigResponse = { dataset_id: string; config: RetrievalConfig; version: number }
type RetrievalResponse = { items: Record<string, unknown>[]; config?: RetrievalConfig }

const defaultRetrievalConfig: RetrievalConfig = { top_k: 5, candidate_k: 20, rrf_k: 60, score_threshold: 0 }

function versionHeader(value: unknown) {
  const version = Number(value)
  return Number.isFinite(version) ? `W/"${version}"` : '*'
}

function requestKey(scope: string) {
  const random = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`
  return `web-knowledge:${scope}:${random}`
}

function displayValue(value: unknown, fallback = '--') {
  return value === undefined || value === null || value === '' ? fallback : String(value)
}

function displayDate(value: unknown, fallback = '--') {
  if (!value) return fallback
  const date = new Date(String(value))
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString()
}

function jsonValue(value: unknown) {
  if (value === undefined || value === null) return '--'
  if (typeof value === 'string') return value
  try { return JSON.stringify(value) } catch { return String(value) }
}

function DatasetDetail({ id }: { id: string }) {
  const { t } = useLocale()
  const [dataset, setDataset] = useState<Dataset | null>(null)
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([])
  const [selectedDocumentId, setSelectedDocumentId] = useState('')
  const [chunks, setChunks] = useState<KnowledgeChunk[]>([])
  const [generations, setGenerations] = useState<KnowledgeGeneration[]>([])
  const [configVersion, setConfigVersion] = useState<number | null>(null)
  const [configDraft, setConfigDraft] = useState<RetrievalConfig>(defaultRetrievalConfig)
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<Record<string, unknown>[]>([])
  const [hasSearched, setHasSearched] = useState(false)
  const [loading, setLoading] = useState(true)
  const [chunksLoading, setChunksLoading] = useState(false)
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [noticeTone, setNoticeTone] = useState<'info' | 'error'>('info')
  const [busy, setBusy] = useState('')
  const [sourceOpen, setSourceOpen] = useState(false)
  const [sourceUrl, setSourceUrl] = useState('')
  const [sourceName, setSourceName] = useState('')
  const [editingChunkId, setEditingChunkId] = useState('')
  const [chunkDraft, setChunkDraft] = useState('')
  const [rebuildReason, setRebuildReason] = useState('')

  const selectedDocument = useMemo(() => documents.find((item) => String(item.id) === selectedDocumentId) ?? null, [documents, selectedDocumentId])
  const stats = (dataset?.stats ?? {}) as Record<string, unknown>
  const embeddingProfile = (dataset?.embedding_profile ?? {}) as Record<string, unknown>
  const chunkCount = stats.chunk_count ?? documents.reduce((sum, item) => sum + Number(item.chunk_count ?? 0), 0)
  const activeGeneration = generations.find((item) => String(item.status) === 'active')

  function showNotice(message: string, tone: 'info' | 'error' = 'info') {
    setNotice(message)
    setNoticeTone(tone)
  }

  const reload = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [base, docs, config, generationResult] = await Promise.all([
        api.get<Dataset>(`/api/v1/datasets/${id}`),
        api.get<ListResponse<KnowledgeDocument>>(`/api/v1/datasets/${id}/documents?include_deleted=true`),
        api.get<RetrievalConfigResponse>(`/api/v1/datasets/${id}/retrieval-config`),
        api.get<ListResponse<KnowledgeGeneration>>(`/api/v1/datasets/${id}/index-generations`),
      ])
      setDataset(base)
      setDocuments(docs.items)
      setSelectedDocumentId((current) => current && docs.items.some((item) => String(item.id) === current) ? current : String(docs.items[0]?.id ?? ''))
      setConfigDraft(config.config)
      setConfigVersion(config.version)
      setGenerations(generationResult.items)
    } catch (caught) {
      setError(errorMessage(caught, t))
    } finally {
      setLoading(false)
    }
  }, [id])

  useEffect(() => { void reload() }, [reload])

  useEffect(() => {
    if (!selectedDocumentId) {
      setChunks([])
      return
    }
    let active = true
    setChunksLoading(true)
    api.get<ListResponse<KnowledgeChunk>>(`/api/v1/datasets/${id}/chunks?document_id=${encodeURIComponent(selectedDocumentId)}`).then((result) => {
      if (active) setChunks(result.items)
    }).catch((caught) => {
      if (active) showNotice(errorMessage(caught, t), 'error')
    }).finally(() => {
      if (active) setChunksLoading(false)
    })
    return () => { active = false }
  }, [id, selectedDocumentId])

  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    setBusy('upload')
    try {
      await api.upload(`/api/v1/datasets/${id}/documents`, file)
      await reload()
      showNotice(t('knowledge.retryStarted'))
    } catch (caught) {
      showNotice(errorMessage(caught, t), 'error')
    } finally {
      setBusy('')
    }
  }

  async function importUrl(event: FormEvent) {
    event.preventDefault()
    setBusy('url')
    try {
      await api.post(`/api/v1/datasets/${id}/documents`, { source_url: sourceUrl.trim(), name: sourceName.trim() })
      setSourceOpen(false)
      setSourceUrl('')
      setSourceName('')
      await reload()
      showNotice(t('knowledge.retryStarted'))
    } catch (caught) {
      showNotice(errorMessage(caught, t), 'error')
    } finally {
      setBusy('')
    }
  }

  async function documentAction(item: KnowledgeDocument, action: 'retry' | 'cancel' | 'delete' | 'restore') {
    const documentId = String(item.id)
    if (!documentId) return
    const actionKey = `${action}:${documentId}`
    setBusy(actionKey)
    try {
      if (action === 'delete') {
        await api.request(`/api/v1/datasets/${id}/documents/${documentId}`, {
          method: 'DELETE',
          headers: { 'If-Match': versionHeader(item.version), 'Idempotency-Key': requestKey(`document-delete:${documentId}`) },
          body: JSON.stringify({ reason: 'Deleted from the knowledge console' }),
        })
      } else if (action === 'restore') {
        await api.request(`/api/v1/datasets/${id}/documents/${documentId}/restore`, {
          method: 'POST',
          headers: { 'Idempotency-Key': requestKey(`document-restore:${documentId}`) },
          body: JSON.stringify({ reason: 'Restored from the knowledge console' }),
        })
      } else if (action === 'retry') {
        await api.request(`/api/v1/datasets/${id}/documents/${documentId}/retries`, {
          method: 'POST',
          headers: { 'Idempotency-Key': requestKey(`document-retry:${documentId}`) },
        })
      } else {
        await api.post(`/api/v1/datasets/${id}/documents/${documentId}/cancel`)
      }
      await reload()
      showNotice(t(action === 'retry' ? 'knowledge.retryStarted' : action === 'cancel' ? 'knowledge.cancelledNotice' : action === 'delete' ? 'knowledge.deleteStarted' : 'knowledge.restoreStarted'))
    } catch (caught) {
      showNotice(errorMessage(caught, t), 'error')
    } finally {
      setBusy('')
    }
  }

  async function search() {
    const trimmed = query.trim()
    if (!trimmed) return
    setSearching(true)
    setHasSearched(true)
    try {
      const result = await api.post<RetrievalResponse>(`/api/v1/datasets/${id}/retrieve`, {
        query: trimmed,
        top_k: configDraft.top_k,
        score_threshold: configDraft.score_threshold,
      })
      setHits(result.items)
    } catch (caught) {
      setHits([])
      showNotice(errorMessage(caught, t), 'error')
    } finally {
      setSearching(false)
    }
  }

  async function saveRetrievalConfig(event: FormEvent) {
    event.preventDefault()
    setBusy('config')
    try {
      const result = await api.request<RetrievalConfigResponse>(`/api/v1/datasets/${id}/retrieval-config`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', 'If-Match': versionHeader(configVersion ?? dataset?.version) },
        body: JSON.stringify(configDraft),
      })
      setConfigDraft(result.config)
      setConfigVersion(result.version)
      setDataset((current) => current ? { ...current, version: result.version, retrieval_config: result.config } : current)
      showNotice(t('knowledge.configSaved'))
    } catch (caught) {
      showNotice(errorMessage(caught, t), 'error')
    } finally {
      setBusy('')
    }
  }

  async function beginChunkEdit(chunk: KnowledgeChunk) {
    const chunkId = String(chunk.id)
    setBusy(`chunk-load:${chunkId}`)
    try {
      const detail = await api.get<KnowledgeChunk>(`/api/v1/datasets/${id}/chunks/${chunkId}`)
      setEditingChunkId(chunkId)
      setChunkDraft(String(detail.content ?? ''))
      setChunks((current) => current.map((item) => String(item.id) === chunkId ? detail : item))
    } catch (caught) {
      showNotice(errorMessage(caught, t), 'error')
    } finally {
      setBusy('')
    }
  }

  async function saveChunk(event: FormEvent) {
    event.preventDefault()
    const current = chunks.find((item) => String(item.id) === editingChunkId)
    if (!current) return
    setBusy('chunk-save')
    try {
      const result = await api.request<{ chunk: KnowledgeChunk }>(`/api/v1/datasets/${id}/chunks/${editingChunkId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', 'If-Match': versionHeader(current.version), 'Idempotency-Key': requestKey(`chunk-update:${editingChunkId}`) },
        body: JSON.stringify({ content: chunkDraft }),
      })
      setChunks((items) => items.map((item) => String(item.id) === editingChunkId ? result.chunk : item))
      setDocuments((items) => items.map((item) => String(item.id) === String(current.document_id) ? { ...item, status: 'embedding', updated_at: new Date().toISOString() } : item))
      setEditingChunkId('')
      setChunkDraft('')
      showNotice(t('knowledge.chunkSaved'))
    } catch (caught) {
      showNotice(errorMessage(caught, t), 'error')
    } finally {
      setBusy('')
    }
  }

  async function rebuildIndex() {
    setBusy('rebuild')
    try {
      await api.post(`/api/v1/datasets/${id}/index-generations`, { reason: rebuildReason.trim() || 'Manual rebuild from the knowledge console' })
      setRebuildReason('')
      await reload()
      showNotice(t('knowledge.rebuildStarted'))
    } catch (caught) {
      showNotice(errorMessage(caught, t), 'error')
    } finally {
      setBusy('')
    }
  }

  async function activateGeneration(generation: KnowledgeGeneration) {
    const generationId = String(generation.id)
    setBusy(`activate:${generationId}`)
    try {
      await api.request(`/api/v1/datasets/${id}/index-generations/${generationId}/activate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'If-Match': versionHeader(dataset?.version) },
        body: JSON.stringify({ reason: 'Activated from the knowledge console' }),
      })
      await reload()
      showNotice(t('knowledge.configSaved'))
    } catch (caught) {
      showNotice(errorMessage(caught, t), 'error')
    } finally {
      setBusy('')
    }
  }

  function documentActionButtons(item: KnowledgeDocument) {
    const status = String(item.status ?? 'indexed').toLowerCase()
    const documentId = String(item.id)
    const canRetry = ['failed', 'cancelled', 'indexed'].includes(status)
    const canCancel = ['pending', 'parsing', 'chunking', 'embedding'].includes(status)
    const canDelete = !['deleting', 'deleted'].includes(status)
    return <div className="knowledge-actions" onClick={(event) => event.stopPropagation()}>
      {canRetry && <IconButton label={t('knowledge.retry')} disabled={Boolean(busy)} onClick={() => void documentAction(item, 'retry')}><RefreshCw size={15} className={busy === `retry:${documentId}` ? 'spin' : ''} /></IconButton>}
      {canCancel && <IconButton label={t('knowledge.cancel')} disabled={Boolean(busy)} onClick={() => void documentAction(item, 'cancel')}><X size={15} /></IconButton>}
      {canDelete && <IconButton label={t('knowledge.delete')} disabled={Boolean(busy)} onClick={() => void documentAction(item, 'delete')}><Trash2 size={15} /></IconButton>}
      {status === 'deleted' && <IconButton label={t('knowledge.restore')} disabled={Boolean(busy)} onClick={() => void documentAction(item, 'restore')}><RotateCcw size={15} /></IconButton>}
    </div>
  }

  return <>
    <PageHeader eyebrow="KNOWLEDGE BASE" title={dataset?.name ?? t('knowledge.baseFallback')} description={dataset?.description ?? t('knowledge.description')} actions={<Link className="button button-secondary" to="/knowledge"><ArrowLeft size={16} />{t('knowledge.backToKnowledge')}</Link>} />
    {notice && <div className={`alert ${noticeTone === 'error' ? 'alert-error' : 'alert-info'}`} role={noticeTone === 'error' ? 'alert' : 'status'}>{notice}</div>}
    <div className="kpi-grid knowledge-kpis">
      <Kpi label={t('knowledge.documents')} value={String(stats.document_count ?? documents.filter((item) => item.status !== 'deleted').length)} icon={<FileText size={18} />} trend={t('knowledge.documentStats')} />
      <Kpi label={t('knowledge.chunkCount')} value={String(chunkCount)} icon={<Table2 size={18} />} trend={t('knowledge.readyForRetrieval')} />
      <Kpi label={t('knowledge.embeddingDimension')} value={displayValue(embeddingProfile.dimension)} icon={<Sparkles size={18} />} trend={displayValue(dataset?.embedding_model, t('knowledge.noActiveGeneration'))} />
      <Kpi label={t('knowledge.activeGeneration')} value={displayValue(activeGeneration?.generation ?? dataset?.active_generation_id, t('knowledge.noActiveGeneration'))} icon={<GitBranch size={18} />} trend={activeGeneration ? localizedStatus(t, activeGeneration.status) : t('knowledge.noActiveGeneration')} />
    </div>
    <div className="split-grid knowledge-detail-grid">
      <div className="knowledge-column">
        <Panel title={t('knowledge.documents')} subtitle={`${documents.length} ${t('knowledge.sourceFiles')}`} actions={<div className="panel-actions-inline"><Button icon={<Link2 size={15} />} onClick={() => setSourceOpen(true)}>{t('knowledge.addUrlSource')}</Button><label className="button button-primary"><Upload size={16} />{t('knowledge.upload')}<input type="file" hidden onChange={upload} /></label></div>}>
          <PageState loading={loading} error={error} empty={false} onRetry={() => void reload()}>
            {documents.length ? <DataTable headers={[t('knowledge.document'), t('knowledge.status'), t('knowledge.chunks'), t('knowledge.updated'), t('knowledge.documentActions')] } caption={t('knowledge.documents')}>
              {documents.map((item, index) => {
                const status = String(item.status ?? 'indexed').toLowerCase()
                const selected = String(item.id) === selectedDocumentId
                return <tr key={String(item.id ?? index)} className={selected ? 'selected-row' : undefined} onClick={() => setSelectedDocumentId(String(item.id))}>
                  <td><button type="button" className="knowledge-doc-link" aria-label={`${t('knowledge.selectDocument')}: ${displayValue(item.name, `${t('knowledge.document')} ${index + 1}`)}`} onClick={() => setSelectedDocumentId(String(item.id))}><FileText size={15} /><span><strong>{displayValue(item.name, `${t('knowledge.document')} ${index + 1}`)}</strong><small>{displayValue(item.source, t('knowledge.sourceUpload'))} · {displayValue(item.mime)}</small></span></button>{Boolean(item.error) && <small className="table-subtext error-text">{t('knowledge.errorDetail')}: {String(item.error)}</small>}</td>
                  <td><Status value={localizedStatus(t, status)} toneValue={status} /></td>
                  <td>{displayValue(item.chunk_count)}</td>
                  <td>{displayDate(item.updated_at, t('knowledge.today'))}</td>
                  <td>{documentActionButtons(item)}</td>
                </tr>
              })}
            </DataTable> : <div className="state-view"><FileText size={22} /><strong>{t('knowledge.noDocuments')}</strong><span>{t('knowledge.selectDocumentHint')}</span></div>}
          </PageState>
        </Panel>
        <Panel title={t('knowledge.chunks')} subtitle={selectedDocument ? `${displayValue(selectedDocument.name)} · ${chunks.length} ${t('knowledge.chunks')}` : t('knowledge.chunksSubtitle')}>
          {!selectedDocument ? <div className="callout"><Table2 size={16} /><span>{t('knowledge.selectDocumentHint')}</span></div> : chunksLoading ? <StateView state="loading" /> : chunks.length ? <DataTable headers={[t('knowledge.chunkPosition'), t('knowledge.tokens'), t('knowledge.content'), '']} caption={t('knowledge.chunks')}>
            {chunks.map((chunk, index) => <tr key={String(chunk.id ?? index)}><td>{displayValue(chunk.position, String(index + 1))}</td><td>{displayValue(chunk.token_count)}</td><td><div className="chunk-preview">{displayValue(chunk.content)}</div></td><td><IconButton label={t('knowledge.editChunk')} disabled={Boolean(busy)} onClick={() => void beginChunkEdit(chunk)}><MoreHorizontal size={16} /></IconButton></td></tr>)}
          </DataTable> : <div className="callout"><Table2 size={16} /><span>{t('knowledge.noChunks')}</span></div>}
        </Panel>
      </div>
      <Panel title={t('knowledge.retrievalPlayground')} subtitle={t('knowledge.retrievalSubtitle')}>
        <div className="knowledge-subsection">
          <div className="knowledge-subsection-heading"><div><h3>{t('knowledge.configTitle')}</h3><p>{t('knowledge.configSubtitle')}</p></div><SlidersHorizontal size={17} /></div>
          <form className="form-stack" onSubmit={saveRetrievalConfig}>
            <div className="retrieval-config-grid"><Field label={t('knowledge.topK')}><input type="number" min={1} max={50} value={configDraft.top_k} onChange={(event) => setConfigDraft((current) => ({ ...current, top_k: Number(event.target.value) || 1 }))} /></Field><Field label={t('knowledge.candidateK')}><input type="number" min={5} max={200} value={configDraft.candidate_k} onChange={(event) => setConfigDraft((current) => ({ ...current, candidate_k: Number(event.target.value) || 5 }))} /></Field><Field label={t('knowledge.rrfK')}><input type="number" min={1} max={200} value={configDraft.rrf_k} onChange={(event) => setConfigDraft((current) => ({ ...current, rrf_k: Number(event.target.value) || 1 }))} /></Field><Field label={t('knowledge.scoreThreshold')}><input type="number" min={0} max={1} step={0.01} value={configDraft.score_threshold} onChange={(event) => setConfigDraft((current) => ({ ...current, score_threshold: Number(event.target.value) || 0 }))} /></Field></div>
            <div className="knowledge-subsection-footer"><small>{t('knowledge.configHint')} · v{displayValue(configVersion ?? dataset?.version)}</small><Button type="submit" icon={<Save size={15} />} loading={busy === 'config'}>{t('knowledge.saveConfig')}</Button></div>
          </form>
        </div>
        <div className="knowledge-subsection retrieval-query">
          <Field label={t('knowledge.query')}><textarea aria-label={t('knowledge.query')} value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t('knowledge.queryPlaceholder')} /></Field>
          <Button variant="primary" icon={<Search size={15} />} loading={searching} onClick={() => void search()}>{t('knowledge.search')}</Button>
          {hasSearched && hits.length > 0 && <div className="hit-list">{hits.map((hit, index) => <div className="hit" key={String(hit.id ?? index)}><div className="hit-topline"><Badge tone="info">{t('knowledge.rrfScore')} {Number(hit.rrf_score ?? 0).toFixed(4)}</Badge><span>{t('knowledge.keywordRank')} #{displayValue(hit.keyword_rank)}</span><span>{t('knowledge.vectorRank')} #{displayValue(hit.vector_rank)}</span></div><p>{displayValue(hit.content, t('knowledge.noResults'))}</p><small>{displayValue(hit.document_name, t('knowledge.retrievedSource'))} · {t('knowledge.keywordScore')} {Number(hit.keyword_score ?? 0).toFixed(3)} · {t('knowledge.vectorScore')} {Number(hit.vector_score ?? 0).toFixed(3)}</small><code>{t('knowledge.metadata')}: {jsonValue(hit.metadata)}</code></div>)}</div>}
          {hasSearched && !hits.length && <div className="callout"><Search size={16} /><span>{t('knowledge.noResults')}</span></div>}
          {!hasSearched && <div className="callout"><Sparkles size={16} /><span>{t('knowledge.runQueryHint')}</span></div>}
        </div>
      </Panel>
    </div>
    <Panel title={t('knowledge.generationTitle')} subtitle={t('knowledge.generationSubtitle')} actions={<Button variant="primary" icon={<RefreshCw size={15} />} loading={busy === 'rebuild'} onClick={() => void rebuildIndex()}>{t('knowledge.rebuildIndex')}</Button>}>
      <div className="generation-toolbar"><Field label={t('knowledge.rebuildReason')}><input value={rebuildReason} onChange={(event) => setRebuildReason(event.target.value)} placeholder={t('knowledge.generationSubtitle')} /></Field><span className="generation-boundary"><GitBranch size={15} />{t('knowledge.readyToActivate')}</span></div>
      {generations.length ? <DataTable headers={[t('knowledge.generation'), t('knowledge.status'), t('knowledge.embedding'), t('knowledge.created'), '']} caption={t('knowledge.generationTitle')}>
        {generations.map((generation, index) => { const status = String(generation.status ?? 'unknown').toLowerCase(); const profile = (generation.embedding_profile ?? {}) as Record<string, unknown>; return <tr key={String(generation.id ?? index)}><td><strong>#{displayValue(generation.generation, String(index + 1))}</strong><small className="table-subtext"><code>{displayValue(generation.id)}</code></small></td><td><Status value={localizedStatus(t, status)} toneValue={status} /></td><td>{displayValue(profile.model ?? dataset?.embedding_model)} · {displayValue(profile.dimension)}d</td><td>{displayDate(generation.created_at, t('knowledge.today'))}</td><td>{status === 'ready' && <Button icon={<Check size={15} />} loading={busy === `activate:${String(generation.id)}`} onClick={() => void activateGeneration(generation)}>{t('knowledge.activate')}</Button>}</td></tr> })}
      </DataTable> : <div className="callout"><GitBranch size={16} /><span>{t('knowledge.noActiveGeneration')}</span></div>}
    </Panel>
    {sourceOpen && <Modal title={t('knowledge.addUrlSource')} onClose={() => setSourceOpen(false)}><form className="form-stack" onSubmit={importUrl}><Field label={t('knowledge.urlSource')} hint={t('knowledge.urlSourceHint')}><input type="url" value={sourceUrl} onChange={(event) => setSourceUrl(event.target.value)} required placeholder={t('knowledge.urlSourcePlaceholder')} /></Field><Field label={t('knowledge.urlSourceName')}><input value={sourceName} onChange={(event) => setSourceName(event.target.value)} placeholder={t('knowledge.namePlaceholder')} /></Field><Button type="submit" variant="primary" icon={<Globe2 size={15} />} loading={busy === 'url'}>{t('knowledge.importUrl')}</Button></form></Modal>}
    {editingChunkId && <Modal title={t('knowledge.editChunkTitle')} onClose={() => setEditingChunkId('')}><form className="form-stack" onSubmit={saveChunk}><Field label={t('knowledge.chunkContent')}><textarea value={chunkDraft} onChange={(event) => setChunkDraft(event.target.value)} required /></Field><div className="editor-meta"><span><Table2 size={13} />{t('knowledge.editChunk')}</span><span>v{displayValue(chunks.find((item) => String(item.id) === editingChunkId)?.version)}</span></div><Button type="submit" variant="primary" icon={<Save size={15} />} loading={busy === 'chunk-save'}>{t('knowledge.saveChunk')}</Button></form></Modal>}
  </>
}
