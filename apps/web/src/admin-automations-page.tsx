/**
 * 自动化 v2 触发器：列表 + 创建 + 启停 + 立即运行。
 */
import { useState, type ReactNode } from 'react'
import { api, errorMessage } from './api'
import { useLocale } from './locale'
import { AdminCreateForm, AdminPageShell, DeleteButton, useResource } from './admin-shared'
import { Button } from './ui'

type Automation = {
  id: string
  name: string
  trigger_type?: string
  cron_or_webhook_url?: string
  enabled?: boolean
  last_run_at?: string
}

export default function AdminAutomationsPage(): ReactNode {
  const { items, loading, error, reload, create, remove, busy } = useResource<Automation>(
    '/api/v1/automations/v2/triggers',
  )
  const { t } = useLocale()
  const [actionError, setActionError] = useState('')
  const [runResult, setRunResult] = useState('')

  async function toggle(item: Automation) {
    setActionError('')
    try {
      await api.post(`/api/v1/automations/v2/triggers/${encodeURIComponent(item.id)}/toggle`)
      // 乐观更新：直接 reload 以同步后端最新状态
      reload()
    } catch (caught) {
      setActionError(errorMessage(caught, t('admin.automations.toggleFailed')))
    }
  }

  async function run(item: Automation) {
    setActionError('')
    setRunResult('')
    try {
      const result = await api.post<{ run_id?: string; status?: string }>(
        `/api/v1/automations/v2/triggers/${encodeURIComponent(item.id)}/run`,
      )
      setRunResult(`${t('admin.automations.triggeredPrefix')}${result?.run_id ?? result?.status ?? 'ok'}`)
    } catch (caught) {
      setActionError(errorMessage(caught, t('admin.automations.runFailed')))
    }
  }

  return (
    <AdminPageShell
      title={t('admin.automations.title')}
      subtitle={t('admin.automations.subtitle')}
      testId="automations-page"
      loading={loading}
      error={error}
      onRetry={reload}
    >
      <AdminCreateForm
        testId="automations-create"
        fields={[
          { name: 'name', label: t('admin.automations.field.name'), placeholder: t('admin.automations.field.name.placeholder') },
          { name: 'trigger_type', label: t('admin.automations.field.triggerType'), placeholder: t('admin.automations.field.triggerType.placeholder') },
          { name: 'cron_or_webhook_url', label: t('admin.automations.field.cron'), placeholder: t('admin.automations.field.cron.placeholder') },
        ]}
        onSubmit={(v) =>
          create({
            name: v.name,
            trigger_type: v.trigger_type,
            cron_or_webhook_url: v.cron_or_webhook_url,
          })
        }
        busy={busy}
      />
      <ul className="resource-list" data-testid="automations-list">
        {items.map((item) => (
          <li key={item.id} className="resource-item" data-testid="automations-item">
            <div className="resource-info">
              <strong>{item.name}</strong>
              <small>{item.trigger_type ?? 'unknown'}</small>
              {item.cron_or_webhook_url && <span>{item.cron_or_webhook_url}</span>}
              <span>{item.enabled ? t('admin.automations.enabled') : t('admin.automations.disabled')}</span>
              {item.last_run_at && <small>{t('admin.automations.lastRunPrefix')} {item.last_run_at}</small>}
            </div>
            <div className="resource-actions">
              <Button
                variant="ghost"
                onClick={() => void toggle(item)}
                data-testid={`automations-toggle-${item.id}`}
              >
                {item.enabled ? t('admin.automations.disable') : t('admin.automations.enable')}
              </Button>
              <Button
                variant="ghost"
                onClick={() => void run(item)}
                data-testid={`automations-run-${item.id}`}
              >
                {t('admin.automations.runNow')}
              </Button>
              <DeleteButton
                testId={`automations-delete-${item.id}`}
                onDelete={() => void remove(item.id)}
                busy={busy}
              />
            </div>
          </li>
        ))}
      </ul>
      {actionError && (
        <div className="alert alert-error" data-testid="automations-action-error">
          {actionError}
        </div>
      )}
      {runResult && (
        <div className="alert alert-info" data-testid="automations-run-result">
          {runResult}
        </div>
      )}
    </AdminPageShell>
  )
}
