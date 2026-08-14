import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Bot, Pause, Play, ShieldAlert, Users, X } from 'lucide-react'
import { createWSClient } from '@workama/api-client'
import type { WSClient } from '@workama/api-client'
import type { MessageKey } from '@workama/i18n'
import { api, agentWsUrl } from '../../api'
import { useLocale } from '../../locale'
import { Badge, Button, IconButton, StateView } from '../../ui'
import type { AgentEvent, ListResponse, Session } from '../../types'
import { applyEvent, emptyProjection, projectEvents, type SessionProjection } from '@workama/event-renderer'
import { MessageList, type ChatMessage } from './MessageList'
import { ChatInput } from './ChatInput'
import { ChatSidebar, type ConversationSummary } from './ChatSidebar'

function errorMessage(error: unknown, t: (key: MessageKey) => string): string {
  return error instanceof Error ? error.message : t('errors.requestFailed')
}

/** Convert projection messages to ChatMessage format. */
function toChatMessages(projection: SessionProjection): ChatMessage[] {
  return projection.messages.map((msg) => ({
    id: msg.id,
    role: msg.role as ChatMessage['role'],
    content: msg.content,
    streaming: msg.streaming,
  }))
}

/** Chat session detail page — displays messages and input for a single session. */
function ChatSessionView({ sessionId }: { sessionId: string }) {
  const navigate = useNavigate()
  const { t } = useLocale()
  const [session, setSession] = useState<Session | null>(null)
  const [projection, setProjection] = useState<SessionProjection>(emptyProjection())
  const [connected, setConnected] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const socket = useRef<WSClient | null>(null)
  const endRef = useRef<HTMLDivElement>(null)

  const ingest = useCallback((event: AgentEvent) => {
    setProjection((current) => applyEvent(current, event))
  }, [])

  useEffect(() => {
    let active = true
    async function load() {
      try {
        const [sessionData, events] = await Promise.all([
          api.get<Session>(`/api/v1/sessions/${sessionId}`),
          api.get<ListResponse<AgentEvent>>(`/api/v1/sessions/${sessionId}/events`),
        ])
        if (!active) return
        setSession(sessionData)
        setProjection(projectEvents(events.items))

        const ticket = await api.post<{ ticket: string }>(`/api/v1/sessions/${sessionId}/ws-tickets`)
        if (!active) return

        const afterSeq = events.items.at(-1)?.seq ?? 0
        socket.current = createWSClient(
          `${agentWsUrl}/ws/sessions/${sessionId}?ticket=${encodeURIComponent(ticket.ticket)}`,
          {
            after: afterSeq,
            autoAck: true,
            reconnect: false,
            onOpen: () => setConnected(true),
            onClose: () => setConnected(false),
            onError: () => setError(t('chat.session.realtimeError')),
            onMessage: (message) => {
              try {
                const event = message as AgentEvent & { payload?: Record<string, unknown> }
                if (event.type === 'session.snapshot') {
                  const snapEvents = (event.payload?.events as AgentEvent[]) ?? []
                  // v7.268 修复：after>0（增量模式）时 agent-server 推空 snapshot，
                  // 无条件覆盖会清空已加载的历史投影；空快照不覆盖。
                  if (snapEvents.length > 0) setProjection(projectEvents(snapEvents))
                  return
                }
                if (event.type !== 'connection.ready' && event.type !== 'connection.warning')
                  ingest(event)
              } catch {
                setError(t('chat.session.invalidEvent'))
              }
            },
          },
        )
      } catch (caught) {
        if (active) setError(errorMessage(caught, t))
      } finally {
        if (active) setLoading(false)
      }
    }
    void load()
    return () => {
      active = false
      socket.current?.close()
    }
  }, [ingest, sessionId, t])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [projection.messages.length, projection.lastSeq])

  function sendMessage(content: string) {
    if (!connected || projection.running) return
    socket.current?.send({ type: 'message.create', content, attachment_ids: [] })
  }

  async function control(action: 'pause' | 'resume' | 'cancel') {
    try {
      const result = await api.post<{ status: string }>(`/api/v1/sessions/${sessionId}/${action}`, { reason: 'Console action' })
      setSession((current) => current ? { ...current, status: result.status } : current)
    } catch (caught) {
      setError(errorMessage(caught, t))
    }
  }

  const chatMessages = toChatMessages(projection)

  return (
    <div className="chat-workspace" data-testid="chat-session">
      <header className="chat-toolbar">
        <div className="chat-title">
          <IconButton label={t('chat.session.backToList')} onClick={() => navigate('/chat')}>
            <ArrowLeft size={17} />
          </IconButton>
          <div>
            <strong>{session?.title ?? t('chat.session.conversationFallback')}</strong>
            <span>
              <i className={`status-dot ${connected ? 'status-success' : 'status-warning'}`} />
              {connected ? t('chat.session.connected') : t('chat.session.connecting')} · {session?.model ?? 'workama-chat'}
            </span>
          </div>
        </div>
        <div className="chat-controls">
          {session?.status === 'running' && (
            <IconButton label={t('chat.session.pauseRun')} onClick={() => void control('pause')}><Pause size={16} /></IconButton>
          )}
          {session?.status === 'paused' && (
            <IconButton label={t('chat.session.resumeRun')} onClick={() => void control('resume')}><Play size={16} /></IconButton>
          )}
          <IconButton label={t('chat.session.cancelRun')} onClick={() => void control('cancel')}><X size={16} /></IconButton>
          <Badge tone="info">{projection.usage.steps || session?.used_steps || 0} / {session?.max_steps ?? 50} {t('chat.session.steps')}</Badge>
        </div>
      </header>

      <MessageList
        messages={chatMessages}
        loading={loading}
        error={error}
        empty={!chatMessages.length}
        onRetry={() => window.location.reload()}
      >
        {projection.approvals.map((approval) => (
          <div className="approval-card" key={approval.id} data-testid="chat-approval-card" data-approval-id={approval.id} data-approval-status={approval.status} data-approval-tool={approval.target}>
            <div>
              <ShieldAlert size={17} />
              <strong>{t('chat.session.approvalRequired')}</strong>
              <Badge tone="warning">{approval.risk}</Badge>
              <span>{approval.target}</span>
            </div>
            {approval.status === 'pending' && (
              <div className="approval-actions">
                <Button variant="secondary" onClick={() => void api.post(`/api/v1/approvals/${approval.id}/decisions`, { decision: 'rejected', reason: 'Rejected in console' })} data-testid="chat-approval-reject-button">{t('chat.session.reject')}</Button>
                <Button variant="primary" onClick={() => void api.post(`/api/v1/approvals/${approval.id}/decisions`, { decision: 'approved', reason: 'Approved in console' })} data-testid="chat-approval-approve-button">{t('chat.session.approveOnce')}</Button>
              </div>
            )}
          </div>
        ))}
        <div ref={endRef} />
      </MessageList>

      <ChatInput
        connected={connected}
        running={projection.running}
        onSend={sendMessage}
      />
    </div>
  )
}

/** Main ChatPage — shows conversation list or session detail. */
export default function ChatPage() {
  const { sessionId } = useParams()
  const navigate = useNavigate()
  const { t } = useLocale()
  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let active = true
    api.get<ListResponse<Session>>('/api/v1/sessions')
      .then((result) => {
        if (active) {
          setConversations(
            result.items.map((s) => ({
              id: s.id,
              title: s.title ?? '',
              model: s.model ?? 'workama-chat',
              status: s.status,
              updated_at: s.updated_at,
              used_steps: s.used_steps,
            })),
          )
        }
      })
      .catch(() => {})
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => { active = false }
  }, [])

  async function createSession() {
    setBusy(true)
    try {
      const created = await api.post<Session>('/api/v1/sessions', {
        title: t('chat.newConversation'),
        model: 'workama-chat',
        agent_kind: 'ama_chat',
        toolset: ['web_search', 'file.read'],
        canvas_enabled: true,
        max_steps: 50,
      })
      navigate('/chat/' + created.id)
    } catch {
      // Error is surfaced by the session detail view on navigation
    } finally {
      setBusy(false)
    }
  }

  // When a sessionId is present, show the session detail view
  if (sessionId) {
    return <ChatSessionView sessionId={sessionId} />
  }

  // Otherwise show the conversation list with sidebar
  return (
    <div className="chat-page" data-testid="chat-page">
      <ChatSidebar
        conversations={conversations}
        activeId={sessionId}
        onSelect={(id) => navigate('/chat/' + id)}
        loading={loading}
        recentlyUpdatedLabel={t('chat.recentlyUpdated')}
      />
      <div className="chat-main">
        <header className="page-header" data-testid="chat-page-header">
          <div>
            <div className="eyebrow">{t('chat.eyebrow')}</div>
            <h1>{t('chat.commandCenter')}</h1>
            <p>{t('chat.description')}</p>
          </div>
          <div className="page-actions">
            <Button variant="primary" loading={busy} onClick={() => void createSession()} data-testid="chat-new-conversation">
              {t('chat.newConversation')}
            </Button>
          </div>
        </header>
        <div className="session-grid" data-testid="chat-conversation-grid">
          {loading ? (
            <StateView state="loading" />
          ) : conversations.length === 0 ? (
            <StateView state="empty" />
          ) : (
            conversations.slice(0, 12).map((conv) => (
              <button className="session-card" key={conv.id} onClick={() => navigate('/chat/' + conv.id)} data-testid={`chat-conv-${conv.id}`}>
                <div className="session-card-icon">💬</div>
                <div className="session-card-content">
                  <strong>{conv.title || t('chat.untitledConversation')}</strong>
                  <span>{conv.model} · {conv.updated_at ? new Date(conv.updated_at).toLocaleString() : t('chat.recentlyUpdated')}</span>
                </div>
              </button>
            ))
          )}
        </div>
      </div>
    </div>
  )
}
