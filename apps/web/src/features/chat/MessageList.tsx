import { type ReactNode } from 'react'
import { MessageSquare } from 'lucide-react'
import { Status } from '../../ui'

export type ChatMessage = {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  streaming?: boolean
}

export function MessageList({
  messages,
  loading,
  error,
  empty,
  onRetry,
  children,
}: {
  messages: ChatMessage[]
  loading: boolean
  error: string
  empty: boolean
  onRetry: () => void
  children?: ReactNode
}) {
  if (loading) return <div className="state-view" data-testid="message-list-loading">Loading…</div>
  if (error) return <div className="state-view" data-testid="message-list-error"><p>{error}</p><button onClick={onRetry}>Retry</button></div>
  if (empty) return <div className="state-view" data-testid="message-list-empty">No messages yet</div>

  return (
    <div className="message-list" aria-live="polite" data-testid="message-list">
      {messages.map((message) => (
        <article className={`message ${message.role}`} key={message.id} data-testid={`message-${message.id}`} data-role={message.role}>
          <div className="message-avatar">
            {message.role === 'user' ? '👤' : '🤖'}
          </div>
          <div className="message-content">
            <span className="message-role">{message.role === 'user' ? 'You' : 'AMA'}</span>
            <p>{message.content}{message.streaming && <span className="stream-caret" />}</p>
          </div>
        </article>
      ))}
      {children}
    </div>
  )
}
