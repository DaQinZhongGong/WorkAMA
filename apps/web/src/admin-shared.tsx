/**
 * Admin 后台共享工具：通用 CRUD hook + 列表/表单壳。
 * 供 12 个 admin 页面复用，风格与 free-providers-page.tsx 一致。
 */
import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { RefreshCw } from 'lucide-react'
import { api, errorMessage, asItems } from './api'
import { type MessageKey } from '@workama/i18n'
import { useLocale } from './locale'
import { Button, StateView } from './ui'

export type ResourceState<T> = {
  items: T[]
  loading: boolean
  error: string
  reload: () => void
  create: (body: Record<string, unknown>) => Promise<void>
  remove: (id: string) => Promise<void>
  busy: boolean
}

/** 通用资源 hook：拉取列表 + 创建 + 删除。 */
export function useResource<T>(endpoint: string, idField = 'id'): ResourceState<T> {
  const [items, setItems] = useState<T[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const { t } = useLocale()

  const reload = useCallback(() => {
    setLoading(true)
    setError('')
    void api
      .get<unknown>(endpoint)
      .then((payload) => setItems(asItems<T>(payload)))
      .catch((caught) => setError(errorMessage(caught, t('common.loadFailed'))))
      .finally(() => setLoading(false))
  }, [endpoint])

  useEffect(() => {
    void reload()
  }, [reload])

  const create = useCallback(
    async (body: Record<string, unknown>) => {
      setBusy(true)
      setError('')
      try {
        const created = await api.post<T>(endpoint, body)
        if (created && typeof created === 'object') {
          setItems((current) => [created as T, ...current])
        } else {
          void reload()
        }
      } catch (caught) {
        setError(errorMessage(caught, t('common.createFailed')))
        throw caught
      } finally {
        setBusy(false)
      }
    },
    [endpoint, reload],
  )

  const remove = useCallback(
    async (id: string) => {
      setBusy(true)
      setError('')
      try {
        await api.delete(`${endpoint}/${encodeURIComponent(id)}`)
        setItems((current) =>
          current.filter((item) => String((item as Record<string, unknown>)[idField]) !== id),
        )
      } catch (caught) {
        setError(errorMessage(caught, t('common.deleteFailed')))
        throw caught
      } finally {
        setBusy(false)
      }
    },
    [endpoint, idField],
  )

  return { items, loading, error, reload, create, remove, busy }
}

/** Admin 页面壳：标题 + 刷新按钮 + 加载/错误/空状态 + 子内容。 */
export function AdminPageShell({
  title,
  subtitle,
  testId,
  loading,
  error,
  onRetry,
  isEmpty,
  emptyText,
  children,
  actions,
}: {
  title: string
  subtitle?: string
  testId: string
  loading: boolean
  error: string
  onRetry: () => void
  isEmpty?: boolean
  emptyText?: string
  children: ReactNode
  actions?: ReactNode
}) {
  const { t } = useLocale()
  return (
    <div data-testid={testId}>
      <header className="page-header">
        <div>
          <div className="eyebrow">{t('admin.eyebrow')}</div>
          <h1>{title}</h1>
          {subtitle && <p>{subtitle}</p>}
        </div>
        <div className="page-actions">
          <Button icon={<RefreshCw size={15} />} onClick={onRetry} disabled={loading}>
            {t('common.refresh')}
          </Button>
          {actions}
        </div>
      </header>
      {loading ? (
        <StateView state="loading" />
      ) : error ? (
        <StateView state="error" description={error} onRetry={onRetry} />
      ) : isEmpty ? (
        <StateView state="empty" title={emptyText ?? t('common.empty')} />
      ) : (
        children
      )}
    </div>
  )
}

/** 通用创建表单：受控输入 + 提交。 */
export function AdminCreateForm({
  testId,
  fields,
  onSubmit,
  busy,
  submitLabel = 'common.create',
}: {
  testId: string
  fields: { name: string; label: string; placeholder?: string; type?: string }[]
  onSubmit: (values: Record<string, string>) => Promise<void>
  busy: boolean
  submitLabel?: string
}) {
  const { t } = useLocale()
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(fields.map((f) => [f.name, ''])),
  )
  const [formError, setFormError] = useState('')

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setFormError('')
    try {
      await onSubmit(values)
      setValues(Object.fromEntries(fields.map((f) => [f.name, ''])))
    } catch (caught) {
      setFormError(caught instanceof Error ? caught.message : t('common.submitFailed'))
    }
  }

  return (
    <form className="form-stack" data-testid={testId} onSubmit={submit}>
      {fields.map((field) => (
        <label key={field.name} className="field">
          <span className="field-label">{t(field.label as MessageKey)}</span>
          <input
            type={field.type ?? 'text'}
            name={field.name}
            value={values[field.name] ?? ''}
            placeholder={field.placeholder ? t(field.placeholder as MessageKey) : undefined}
            onChange={(e) => setValues((v) => ({ ...v, [field.name]: e.target.value }))}
            data-testid={`${testId}-${field.name}`}
          />
        </label>
      ))}
      {formError && <div className="alert alert-error">{formError}</div>}
      <Button type="submit" variant="primary" loading={busy}>
        {t(submitLabel as MessageKey)}
      </Button>
    </form>
  )
}

/** 通用列表项删除按钮。 */
export function DeleteButton({
  testId,
  onDelete,
  busy,
}: {
  testId: string
  onDelete: () => void
  busy: boolean
}) {
  const { t } = useLocale()
  return (
    <Button variant="ghost" loading={busy} onClick={onDelete} data-testid={testId}>
      {t('common.delete')}
    </Button>
  )
}
