/**
 * 企业知识连接器 v2：列表 + 创建 + 同步 + dry-run 预览。
 */
import { useState, type ReactNode } from 'react'
import { api, errorMessage } from './api'
import { useLocale } from './locale'
import { AdminCreateForm, AdminPageShell, DeleteButton, useResource } from './admin-shared'
import { Button } from './ui'

type Connector = {
  id: string
  name: string
  provider?: string
  status?: string
  last_cursor?: string
}

type DryRunResult = {
  auth?: unknown
  discover?: unknown
  acl?: unknown
  deletion?: unknown
}

export default function AdminConnectorsPage(): ReactNode {
  const { items, loading, error, reload, create, remove, busy } = useResource<Connector>(
    '/api/v1/connectors/v2',
  )
  const { t } = useLocale()
  const [syncResult, setSyncResult] = useState('')
  const [syncError, setSyncError] = useState('')
  const [dryRunResult, setDryRunResult] = useState<DryRunResult | null>(null)
  const [dryRunError, setDryRunError] = useState('')

  async function sync(connector: Connector) {
    setSyncError('')
    setSyncResult(t('admin.connectors.syncing'))
    try {
      const result = await api.post<{ operation_id?: string }>(
        `/api/v1/connectors/v2/${encodeURIComponent(connector.id)}/sync`,
      )
      setSyncResult(`operation_id: ${result?.operation_id ?? t('admin.connectors.submitted')}`)
    } catch (caught) {
      setSyncError(errorMessage(caught, t('admin.connectors.syncFailed')))
      setSyncResult('')
    }
  }

  async function dryRun(connector: Connector) {
    setDryRunError('')
    setDryRunResult(null)
    try {
      const result = await api.post<DryRunResult>(
        `/api/v1/connectors/v2/${encodeURIComponent(connector.id)}/dry-run`,
      )
      setDryRunResult(result ?? {})
    } catch (caught) {
      setDryRunError(errorMessage(caught, t('admin.connectors.dryRunFailed')))
    }
  }

  return (
    <AdminPageShell
      title={t('admin.connectors.title')}
      subtitle={t('admin.connectors.subtitle')}
      testId="connectors-page"
      loading={loading}
      error={error}
      onRetry={reload}
    >
      <AdminCreateForm
        testId="connectors-create"
        fields={[
          { name: 'name', label: t('admin.connectors.field.name'), placeholder: t('admin.connectors.field.name.placeholder') },
          { name: 'provider', label: t('admin.connectors.field.provider'), placeholder: t('admin.connectors.field.provider.placeholder') },
        ]}
        onSubmit={(v) => create({ name: v.name, provider: v.provider })}
        busy={busy}
      />
      <ul className="resource-list" data-testid="connectors-list">
        {items.map((item) => (
          <li key={item.id} className="resource-item" data-testid="connectors-item">
            <div className="resource-info">
              <strong>{item.name}</strong>
              <small>{item.provider ?? 'unknown'}</small>
              {item.status && <span>{item.status}</span>}
              {item.last_cursor && <span>{t('admin.connectors.cursorLabel')}: {item.last_cursor}</span>}
            </div>
            <div className="resource-actions">
              <Button
                variant="ghost"
                onClick={() => void sync(item)}
                data-testid={`connectors-sync-${item.id}`}
              >
                {t('admin.connectors.sync')}
              </Button>
              <Button
                variant="ghost"
                onClick={() => void dryRun(item)}
                data-testid={`connectors-dryrun-${item.id}`}
              >
                {t('admin.connectors.dryRun')}
              </Button>
              <DeleteButton
                testId={`connectors-delete-${item.id}`}
                onDelete={() => void remove(item.id)}
                busy={busy}
              />
            </div>
          </li>
        ))}
      </ul>
      {syncError && (
        <div className="alert alert-error" data-testid="connectors-sync-error">
          {syncError}
        </div>
      )}
      {syncResult && (
        <div className="alert alert-info" data-testid="connectors-sync-result">
          {syncResult}
        </div>
      )}
      {dryRunError && (
        <div className="alert alert-error" data-testid="connectors-dryrun-error">
          {dryRunError}
        </div>
      )}
      {dryRunResult && (
        <div className="alert alert-info" data-testid="connectors-dryrun-result">
          <strong>{t('admin.connectors.preview')}</strong>
          <pre>auth: {JSON.stringify(dryRunResult.auth ?? null, null, 2)}</pre>
          <pre>discover: {JSON.stringify(dryRunResult.discover ?? null, null, 2)}</pre>
          <pre>ACL: {JSON.stringify(dryRunResult.acl ?? null, null, 2)}</pre>
          <pre>deletion: {JSON.stringify(dryRunResult.deletion ?? null, null, 2)}</pre>
        </div>
      )}
    </AdminPageShell>
  )
}
