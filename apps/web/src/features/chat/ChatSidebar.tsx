import { useState } from 'react'
import { MessageSquare, ChevronRight } from 'lucide-react'
import { Status } from '../../ui'

export type ConversationSummary = {
  id: string
  title: string
  model: string
  status: string
  updated_at?: string
  used_steps?: number
}

export function ChatSidebar({
  conversations,
  activeId,
  onSelect,
  loading,
  recentlyUpdatedLabel,
}: {
  conversations: ConversationSummary[]
  activeId?: string
  onSelect: (id: string) => void
  loading: boolean
  recentlyUpdatedLabel?: string
}) {
  const [query, setQuery] = useState('')
  const [limit, setLimit] = useState(20)
  const filtered = query.trim()
    ? conversations.filter((c) => `${c.title} ${c.model} ${c.status}`.toLowerCase().includes(query.trim().toLowerCase()))
    : conversations
  const visible = filtered.slice(0, limit)
  const hasMore = filtered.length > visible.length
  return (
    <aside className="chat-sidebar" data-testid="chat-sidebar">
      <div className="chat-sidebar-header">
        <strong>Conversations</strong>
        <span>{filtered.length}</span>
      </div>
      <div style={{ padding: '8px 10px', borderBottom: '1px solid var(--wama-border)' }}>
        <input
          type="search"
          placeholder="搜索会话…"
          value={query}
          onChange={(e) => { setQuery(e.target.value); setLimit(20) }}
          data-testid="chat-sidebar-search"
          style={{
            width: '100%',
            padding: '7px 10px',
            borderRadius: 8,
            border: '1px solid var(--wama-border-strong)',
            background: 'var(--wama-surface)',
            fontSize: 12.5,
          }}
        />
        {filtered.length !== conversations.length && (
          <div style={{ marginTop: 6, fontSize: 11, color: 'var(--wama-muted)', fontVariantNumeric: 'tabular-nums' }}>
            已筛选 {filtered.length} / {conversations.length}
          </div>
        )}
      </div>
      <div className="chat-sidebar-list" style={{ maxHeight: 'calc(100vh - 220px)', overflowY: 'auto' }}>
        {loading ? (
          <div className="state-view" data-testid="chat-sidebar-loading">Loading…</div>
        ) : filtered.length === 0 ? (
          <div className="state-view" data-testid="chat-sidebar-empty">No conversations</div>
        ) : (
          visible.map((conv) => (
            <button
              key={conv.id}
              className={`session-card ${conv.id === activeId ? 'active' : ''}`}
              onClick={() => onSelect(conv.id)}
              data-testid={`chat-sidebar-item-${conv.id}`}
            >
              <div className="session-card-icon">
                <MessageSquare size={17} />
              </div>
              <div className="session-card-content">
                <strong>{conv.title || 'Untitled'}</strong>
                <span>
                  {conv.model} · {conv.updated_at ? new Date(conv.updated_at).toLocaleString() : (recentlyUpdatedLabel ?? 'Recent')}
                </span>
              </div>
              <Status value={conv.status} />
              <ChevronRight size={16} />
            </button>
          ))
        )}
        {hasMore && (
          <div style={{ padding: '10px', textAlign: 'center', borderTop: '1px solid var(--wama-border)' }}>
            <button
              onClick={() => setLimit((l) => l + 20)}
              data-testid="chat-sidebar-load-more"
              style={{
                padding: '6px 14px',
                borderRadius: 999,
                border: '1px solid var(--wama-border-strong)',
                background: 'var(--wama-surface)',
                fontSize: 12.5,
                cursor: 'pointer',
              }}
            >
              加载更多 · 剩余 {filtered.length - visible.length}
            </button>
          </div>
        )}
      </div>
    </aside>
  )
}
