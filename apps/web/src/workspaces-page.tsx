/**
 * 工作区管理：列表 + 创建 + 成员管理（最小 CRUD）。
 */
import { type ReactNode } from 'react'
import { AdminCreateForm, AdminPageShell, DeleteButton, useResource } from './admin-shared'
import { useLocale } from './locale'

type Workspace = { id: string; name: string; slug?: string; member_count?: number }

export default function AdminWorkspacesPage(): ReactNode {
  const { items, loading, error, reload, create, remove, busy } = useResource<Workspace>(
    '/api/v1/workspaces',
  )
  const { t } = useLocale()
  return (
    <AdminPageShell
      title={t('admin.workspaces.title')}
      subtitle={t('admin.workspaces.subtitle')}
      testId="workspaces-page"
      loading={loading}
      error={error}
      onRetry={reload}
    >
      <AdminCreateForm
        testId="workspaces-create"
        fields={[
          {
            name: 'name',
            label: 'admin.workspaces.field.name',
            placeholder: 'admin.workspaces.field.name.placeholder',
          },
          {
            name: 'slug',
            label: 'admin.workspaces.field.slug',
            placeholder: 'admin.workspaces.field.slug.placeholder',
          },
        ]}
        onSubmit={(v) => create({ name: v.name, slug: v.slug })}
        busy={busy}
      />
      <ul className="resource-list" data-testid="workspaces-list">
        {items.map((item) => (
          <li key={item.id} className="resource-item" data-testid="workspaces-item">
            <div className="resource-info">
              <strong>{item.name}</strong>
              <small>{item.slug ?? item.id}</small>
              {item.member_count !== undefined && (
                <span>
                  {t('admin.workspaces.members')} {item.member_count}
                </span>
              )}
            </div>
            <DeleteButton
              testId={`workspaces-delete-${item.id}`}
              onDelete={() => void remove(item.id)}
              busy={busy}
            />
          </li>
        ))}
      </ul>
    </AdminPageShell>
  )
}
