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
  return (
    <aside className="chat-sidebar" data-testid="chat-sidebar">
      <div className="chat-sidebar-header">
        <strong>Conversations</strong>
        <span>{conversations.length}</span>
      </div>
      <div className="chat-sidebar-list">
        {loading ? (
          <div className="state-view" data-testid="chat-sidebar-loading">Loading…</div>
        ) : conversations.length === 0 ? (
          <div className="state-view" data-testid="chat-sidebar-empty">No conversations</div>
        ) : (
          conversations.map((conv) => (
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
      </div>
    </aside>
  )
}
