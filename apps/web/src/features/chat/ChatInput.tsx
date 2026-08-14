import { useState, type KeyboardEvent } from 'react'
import { Send } from 'lucide-react'
import { Button } from '../../ui'
import { useLocale } from '../../locale'

export function ChatInput({
  connected,
  running,
  onSend,
}: {
  connected: boolean
  running: boolean
 onSend: (content: string) => void
}) {
  const { t } = useLocale()
  const [draft, setDraft] = useState('')

  function send() {
    const content = draft.trim()
    if (!content || !connected || running) return
    onSend(content)
    setDraft('')
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      send()
    }
  }

  return (
    <div className="composer-wrap" data-testid="chat-input-wrap">
      <div className="composer">
        <textarea
          aria-label={t('chat.session.messageLabel')}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={connected ? t('chat.session.placeholder') : t('chat.session.placeholderWaiting')}
          data-testid="chat-input"
        />
        <Button
          variant="primary"
          icon={<Send size={16} />}
          disabled={!connected || !draft.trim() || running}
          onClick={send}
          data-testid="chat-send-button"
        >
          {t('chat.session.send')}
        </Button>
      </div>
      <div className="composer-hint">
        <span>{t('chat.session.hint')}</span>
      </div>
    </div>
  )
}
