/**
 * 推送通知：订阅列表 + 发送推送 + 删除订阅。
 */
import { useState, type ReactNode } from 'react'
import { api, errorMessage } from './api'
import { useLocale } from './locale'
import { AdminCreateForm, AdminPageShell, DeleteButton, useResource } from './admin-shared'
import { Button } from './ui'

type PushSubscription = {
  id: string
  endpoint?: string
  user_id?: string
  workspace_id?: string
  created_at?: string
}

function maskEndpoint(endpoint?: string): string {
  if (!endpoint) return 'unknown'
  if (endpoint.length <= 16) return '****'
  return `${endpoint.slice(0, 8)}****${endpoint.slice(-4)}`
}

export default function AdminPushPage(): ReactNode {
  const { items, loading, error, reload, create, remove, busy } = useResource<PushSubscription>(
    '/api/v1/push/subscriptions',
  )
  const { t } = useLocale()
  const [sendTitle, setSendTitle] = useState('')
  const [sendBody, setSendBody] = useState('')
  const [sendTarget, setSendTarget] = useState('')
  const [sendResult, setSendResult] = useState('')
  const [sendError, setSendError] = useState('')

  async function send(event: React.FormEvent) {
    event.preventDefault()
    if (!sendTitle || !sendBody) return
    setSendError('')
    setSendResult(t('admin.push.sending'))
    try {
      const result = await api.post<{ message_id?: string; broadcast?: boolean }>(
        '/api/v1/push/send',
        {
          title: sendTitle,
          body: sendBody,
          target: sendTarget || undefined,
        },
      )
      setSendResult(
        result?.broadcast
          ? `${t('admin.push.broadcastSent')}${result.message_id ?? 'ok'}`
          : `${t('admin.push.pushSent')}${result?.message_id ?? 'ok'}`,
      )
      setSendTitle('')
      setSendBody('')
      setSendTarget('')
    } catch (caught) {
      setSendError(errorMessage(caught, t('admin.push.sendFailed')))
      setSendResult('')
    }
  }

  return (
    <AdminPageShell
      title={t('admin.push.title')}
      subtitle={t('admin.push.subtitle')}
      testId="push-page"
      loading={loading}
      error={error}
      onRetry={reload}
    >
      <AdminCreateForm
        testId="push-subscribe"
        fields={[
          { name: 'endpoint', label: t('admin.push.field.endpoint'), placeholder: t('admin.push.field.endpoint.placeholder') },
          { name: 'user_id', label: t('admin.push.field.userId'), placeholder: t('admin.push.field.userId.placeholder') },
        ]}
        onSubmit={(v) => create({ endpoint: v.endpoint, user_id: v.user_id })}
        busy={busy}
        submitLabel={t('admin.push.subscribe')}
      />
      <ul className="resource-list" data-testid="push-list">
        {items.map((item) => (
          <li key={item.id} className="resource-item" data-testid="push-item">
            <div className="resource-info">
              <strong>{maskEndpoint(item.endpoint)}</strong>
              {item.user_id && <small>{t('admin.push.userPrefix')} {item.user_id}</small>}
              {item.workspace_id && <small>{t('admin.push.workspacePrefix')} {item.workspace_id}</small>}
              {item.created_at && <span>{item.created_at}</span>}
            </div>
            <DeleteButton
              testId={`push-delete-${item.id}`}
              onDelete={() => void remove(item.id)}
              busy={busy}
            />
          </li>
        ))}
      </ul>
      <form className="form-stack" data-testid="push-send-form" onSubmit={send}>
        <label className="field">
          <span className="field-label">{t('admin.push.titleLabel')}</span>
          <input
            type="text"
            value={sendTitle}
            onChange={(e) => setSendTitle(e.target.value)}
            placeholder={t('admin.push.titlePlaceholder')}
            data-testid="push-send-title"
          />
        </label>
        <label className="field">
          <span className="field-label">{t('admin.push.bodyLabel')}</span>
          <input
            type="text"
            value={sendBody}
            onChange={(e) => setSendBody(e.target.value)}
            placeholder={t('admin.push.bodyPlaceholder')}
            data-testid="push-send-body"
          />
        </label>
        <label className="field">
          <span className="field-label">{t('admin.push.targetLabel')}</span>
          <input
            type="text"
            value={sendTarget}
            onChange={(e) => setSendTarget(e.target.value)}
            placeholder={t('admin.push.targetPlaceholder')}
            data-testid="push-send-target"
          />
        </label>
        <Button type="submit" variant="primary" data-testid="push-send-submit">
          {t('admin.push.send')}
        </Button>
        {sendError && (
          <div className="alert alert-error" data-testid="push-send-error">
            {sendError}
          </div>
        )}
        {sendResult && (
          <div className="alert alert-info" data-testid="push-send-result">
            {sendResult}
          </div>
        )}
      </form>
    </AdminPageShell>
  )
}
