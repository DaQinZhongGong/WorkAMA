/**
 * 外部应用：列表 + 创建 + 健康检查。
 */
import { useState, type ReactNode } from 'react'
import { api, errorMessage } from './api'
import { useLocale } from './locale'
import { AdminCreateForm, AdminPageShell, DeleteButton, useResource } from './admin-shared'
import { Button } from './ui'

type ExternalApp = {
  id: string
  name: string
  provider?: string
  execution_mode?: string
  endpoint?: string
}

function maskEndpoint(endpoint?: string): string {
  if (!endpoint) return 'unknown'
  if (endpoint.length <= 16) return '****'
  return `${endpoint.slice(0, 8)}****${endpoint.slice(-4)}`
}

export default function AdminExternalAppsPage(): ReactNode {
  const { items, loading, error, reload, create, remove, busy } = useResource<ExternalApp>(
    '/api/v1/external-apps',
  )
  const { t } = useLocale()
  const [healthResult, setHealthResult] = useState('')
  const [healthError, setHealthError] = useState('')

  async function healthCheck(app: ExternalApp) {
    setHealthError('')
    setHealthResult(t('admin.externalApps.checking'))
    try {
      const result = await api.post<{ status?: string; latency_ms?: number; ok?: boolean }>(
        `/api/v1/external-apps/${encodeURIComponent(app.id)}/health`,
      )
      setHealthResult(
        `${t('admin.externalApps.healthStatusPrefix')}${result?.status ?? (result?.ok ? 'healthy' : 'unknown')}${
          result?.latency_ms !== undefined ? ` / ${t('admin.externalApps.latency')} ${result.latency_ms}ms` : ''
        }`,
      )
    } catch (caught) {
      setHealthError(errorMessage(caught, t('admin.externalApps.healthFailed')))
      setHealthResult('')
    }
  }

  return (
    <AdminPageShell
      title={t('admin.externalApps.title')}
      subtitle={t('admin.externalApps.subtitle')}
      testId="external-apps-page"
      loading={loading}
      error={error}
      onRetry={reload}
    >
      <AdminCreateForm
        testId="external-apps-create"
        fields={[
          { name: 'name', label: t('admin.externalApps.field.name'), placeholder: t('admin.externalApps.field.name.placeholder') },
          { name: 'provider', label: t('admin.externalApps.field.provider'), placeholder: t('admin.externalApps.field.provider.placeholder') },
          { name: 'execution_mode', label: t('admin.externalApps.field.executionMode'), placeholder: t('admin.externalApps.field.executionMode.placeholder') },
          { name: 'endpoint', label: t('admin.externalApps.field.endpoint'), placeholder: t('admin.externalApps.field.endpoint.placeholder') },
        ]}
        onSubmit={(v) =>
          create({
            name: v.name,
            provider: v.provider,
            execution_mode: v.execution_mode,
            endpoint: v.endpoint,
          })
        }
        busy={busy}
      />
      <ul className="resource-list" data-testid="external-apps-list">
        {items.map((item) => (
          <li key={item.id} className="resource-item" data-testid="external-apps-item">
            <div className="resource-info">
              <strong>{item.name}</strong>
              <small>{item.provider ?? 'unknown'}</small>
              {item.execution_mode && <span>{item.execution_mode}</span>}
              <small>{maskEndpoint(item.endpoint)}</small>
            </div>
            <div className="resource-actions">
              <Button
                variant="ghost"
                onClick={() => void healthCheck(item)}
                data-testid={`external-apps-health-${item.id}`}
              >
                {t('admin.externalApps.healthCheck')}
              </Button>
              <DeleteButton
                testId={`external-apps-delete-${item.id}`}
                onDelete={() => void remove(item.id)}
                busy={busy}
              />
            </div>
          </li>
        ))}
      </ul>
      {healthError && (
        <div className="alert alert-error" data-testid="external-apps-health-error">
          {healthError}
        </div>
      )}
      {healthResult && (
        <div className="alert alert-info" data-testid="external-apps-health-result">
          {healthResult}
        </div>
      )}
    </AdminPageShell>
  )
}
