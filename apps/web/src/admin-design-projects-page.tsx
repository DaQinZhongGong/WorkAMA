/**
 * AMA-Design 项目：列表 + 创建 + 资产 + 生成任务历史。
 */
import { useState, type ReactNode } from 'react'
import { api, errorMessage } from './api'
import { useLocale } from './locale'
import { AdminCreateForm, AdminPageShell, DeleteButton, useResource } from './admin-shared'
import { Button } from './ui'

type DesignProject = {
  id: string
  name: string
  status?: string
  asset_count?: number
}

type DesignAsset = { id: string; name?: string; type?: string; url?: string }
type DesignTask = { id: string; status?: string; created_at?: string; prompt?: string }

type ProjectDetail = {
  assets: DesignAsset[]
  tasks: DesignTask[]
}

export default function AdminDesignProjectsPage(): ReactNode {
  const { items, loading, error, reload, create, remove, busy } = useResource<DesignProject>(
    '/api/v1/design/projects',
  )
  const { t } = useLocale()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<ProjectDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState('')

  async function loadDetail(project: DesignProject) {
    setSelectedId(project.id)
    setDetail(null)
    setDetailError('')
    setDetailLoading(true)
    try {
      const [assets, tasks] = await Promise.all([
        api.get<DesignAsset[] | { items?: DesignAsset[] }>(
          `/api/v1/design/projects/${encodeURIComponent(project.id)}/assets`,
        ),
        api.get<DesignTask[] | { items?: DesignTask[] }>(
          `/api/v1/design/projects/${encodeURIComponent(project.id)}/tasks`,
        ),
      ])
      const assetsList = Array.isArray(assets) ? assets : (assets?.items ?? [])
      const tasksList = Array.isArray(tasks) ? tasks : (tasks?.items ?? [])
      setDetail({ assets: assetsList, tasks: tasksList })
    } catch (caught) {
      setDetailError(errorMessage(caught, t('admin.designProjects.loadDetailFailed')))
    } finally {
      setDetailLoading(false)
    }
  }

  return (
    <AdminPageShell
      title={t('admin.designProjects.title')}
      subtitle={t('admin.designProjects.subtitle')}
      testId="design-projects-page"
      loading={loading}
      error={error}
      onRetry={reload}
    >
      <AdminCreateForm
        testId="design-projects-create"
        fields={[{ name: 'name', label: t('admin.designProjects.field.name'), placeholder: t('admin.designProjects.field.name.placeholder') }]}
        onSubmit={(v) => create({ name: v.name })}
        busy={busy}
      />
      <ul className="resource-list" data-testid="design-projects-list">
        {items.map((item) => (
          <li key={item.id} className="resource-item" data-testid="design-projects-item">
            <div className="resource-info">
              <strong>{item.name}</strong>
              {item.status && <small>{item.status}</small>}
              {item.asset_count !== undefined && <span>{t('admin.designProjects.assetCount')} {item.asset_count}</span>}
            </div>
            <div className="resource-actions">
              <Button
                variant="ghost"
                onClick={() => void loadDetail(item)}
                data-testid={`design-projects-detail-${item.id}`}
              >
                {t('admin.designProjects.detail')}
              </Button>
              <DeleteButton
                testId={`design-projects-delete-${item.id}`}
                onDelete={() => void remove(item.id)}
                busy={busy}
              />
            </div>
          </li>
        ))}
      </ul>
      {selectedId && (
        <div className="panel" data-testid="design-projects-detail-panel">
          <h2>{t('admin.designProjects.detailTitle')} {selectedId}</h2>
          {detailLoading && <div>{t('admin.designProjects.loading')}</div>}
          {detailError && (
            <div className="alert alert-error" data-testid="design-projects-detail-error">
              {detailError}
            </div>
          )}
          {detail && (
            <>
              <h3>{t('admin.designProjects.assetsHeading')}</h3>
              <ul data-testid="design-projects-assets">
                {detail.assets.map((asset) => (
                  <li key={asset.id} data-testid="design-projects-asset">
                    {asset.name ?? asset.id} {asset.type && <small>{asset.type}</small>}
                  </li>
                ))}
                {detail.assets.length === 0 && <li>{t('admin.designProjects.noAssets')}</li>}
              </ul>
              <h3>{t('admin.designProjects.tasksHeading')}</h3>
              <ul data-testid="design-projects-tasks">
                {detail.tasks.map((task) => (
                  <li key={task.id} data-testid="design-projects-task">
                    {task.prompt ?? task.id}
                    {task.status && <small>{task.status}</small>}
                    {task.created_at && <small>{task.created_at}</small>}
                  </li>
                ))}
                {detail.tasks.length === 0 && <li>{t('admin.designProjects.noTasks')}</li>}
              </ul>
            </>
          )}
        </div>
      )}
    </AdminPageShell>
  )
}
