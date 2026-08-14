/**
 * 助手管理：概览指标 + 助手列表 + 创建 + 进入对话（最小 CRUD）。
 */
import { useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ArrowUpRight, Bot, Cpu, MessageSquare, Plus, Sparkles } from 'lucide-react'
import { AdminPageShell, DeleteButton, useResource } from './admin-shared'
import { Button, Field, Kpi, Panel, SearchBox, StateView } from './ui'
import { useLocale } from './locale'

type Assistant = { id: string; name: string; model?: string; description?: string }

const DEFAULT_MODEL = 'workama-chat'
const MODEL_PRESETS = ['workama-chat', 'workama-reasoning', 'gpt-4o-mini', 'deepseek-chat']

/** 取名称首字符作为头像标识，空名称回退为通用图标。 */
function initial(name: string): string {
  const trimmed = name.trim()
  return trimmed ? trimmed.slice(0, 1).toUpperCase() : '·'
}

export default function AdminAssistantsPage(): ReactNode {
  const { t } = useLocale()
  const { items, loading, error, reload, create, remove, busy } = useResource<Assistant>(
    '/api/v1/assistants',
  )
  const [query, setQuery] = useState('')
  const [name, setName] = useState('')
  const [model, setModel] = useState('')
  const [description, setDescription] = useState('')
  const [formError, setFormError] = useState('')
  const nameRef = useRef<HTMLInputElement>(null)

  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase()
    if (!keyword) return items
    return items.filter((item) =>
      `${item.name} ${item.model ?? ''} ${item.description ?? ''}`.toLowerCase().includes(keyword),
    )
  }, [items, query])

  const stats = useMemo(() => {
    const models = new Set(items.map((item) => item.model ?? DEFAULT_MODEL))
    const documented = items.filter((item) => Boolean(item.description?.trim())).length
    return { models: models.size, documented }
  }, [items])

  function focusCreate() {
    nameRef.current?.focus()
    nameRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'center' })
  }

  async function submitCreate(event: FormEvent) {
    event.preventDefault()
    setFormError('')
    try {
      await create({ name, model, description })
      setName('')
      setModel('')
      setDescription('')
    } catch (caught) {
      setFormError(caught instanceof Error ? caught.message : t('common.createFailed'))
    }
  }

  return (
    <AdminPageShell
      title={t('admin.assistants.title')}
      subtitle={t('admin.assistants.subtitle')}
      testId="assistants-page"
      loading={loading}
      error={error}
      onRetry={reload}
      actions={
        <Button variant="primary" icon={<Plus size={15} />} onClick={focusCreate}>
          {t('admin.assistants.new')}
        </Button>
      }
    >
      <div className="kpi-grid">
        <Kpi
          label={t('admin.assistants.kpi.total')}
          value={String(items.length).padStart(2, '0')}
          icon={<Bot size={18} />}
          trend={t('admin.assistants.kpi.total.trend')}
        />
        <Kpi
          label={t('admin.assistants.kpi.models')}
          value={String(stats.models).padStart(2, '0')}
          icon={<Cpu size={18} />}
          trend={t('admin.assistants.kpi.models.trend')}
        />
        <Kpi
          label={t('admin.assistants.kpi.documented')}
          value={String(stats.documented).padStart(2, '0')}
          icon={<Sparkles size={18} />}
          trend={t('admin.assistants.kpi.documented.trend')}
        />
      </div>

      <div className="ops-grid">
        <Panel
          title={t('admin.assistants.list.title')}
          subtitle={t('admin.assistants.list.subtitle')}
          actions={<SearchBox value={query} onChange={setQuery} placeholder={t('admin.assistants.search')} />}
        >
          {items.length === 0 ? (
            <StateView
              state="empty"
              title={t('admin.assistants.empty.title')}
              description={t('admin.assistants.empty.desc')}
            />
          ) : filtered.length === 0 ? (
            <StateView
              state="empty"
              title={t('admin.assistants.emptyFiltered.title')}
              description={t('admin.assistants.emptyFiltered.desc')}
            />
          ) : (
            <div className="resource-grid" data-testid="assistants-list">
              {filtered.map((item) => (
                <div key={item.id} className="resource-card" data-testid="assistants-item">
                  <span className="avatar" aria-hidden="true">
                    {initial(item.name)}
                  </span>
                  <div className="resource-main">
                    <strong>{item.name}</strong>
                    {item.description && <p>{item.description}</p>}
                    <span>ID {item.id}</span>
                  </div>
                  <div className="panel-actions-inline">
                    <span className="badge badge-info">{item.model ?? DEFAULT_MODEL}</span>
                  </div>
                  <div className="knowledge-actions">
                    <Link className="button button-ghost" to={`/chat?assistant=${item.id}`}>
                      {t('admin.assistants.chat')} <ArrowUpRight size={14} />
                    </Link>
                    <DeleteButton
                      testId={`assistants-delete-${item.id}`}
                      onDelete={() => void remove(item.id)}
                      busy={busy}
                    />
                  </div>
                </div>
              ))}
            </div>
          )}
        </Panel>

        <Panel
          title={t('admin.assistants.create.title')}
          subtitle={t('admin.assistants.create.subtitle')}
        >
          <form className="form-stack" data-testid="assistants-create" onSubmit={submitCreate}>
            <Field
              label={t('admin.assistants.field.name')}
              hint={t('admin.assistants.field.name.hint')}
            >
              <input
                ref={nameRef}
                name="name"
                value={name}
                placeholder={t('admin.assistants.field.name.placeholder')}
                onChange={(event) => setName(event.target.value)}
                data-testid="assistants-create-name"
              />
            </Field>
            <Field
              label={t('admin.assistants.field.model')}
              hint={`${t('admin.assistants.field.model.hint')} ${DEFAULT_MODEL}`}
            >
              <input
                name="model"
                list="assistant-model-presets"
                value={model}
                placeholder={DEFAULT_MODEL}
                onChange={(event) => setModel(event.target.value)}
                data-testid="assistants-create-model"
              />
            </Field>
            <datalist id="assistant-model-presets">
              {MODEL_PRESETS.map((preset) => (
                <option key={preset} value={preset} />
              ))}
            </datalist>
            <Field
              label={t('admin.assistants.field.description')}
              hint={t('admin.assistants.field.description.hint')}
            >
              <textarea
                name="description"
                rows={3}
                value={description}
                placeholder={t('admin.assistants.field.description.placeholder')}
                onChange={(event) => setDescription(event.target.value)}
                data-testid="assistants-create-description"
              />
            </Field>
            {formError && (
              <div className="alert alert-error" role="alert">
                {formError}
              </div>
            )}
            <Button type="submit" variant="primary" loading={busy} icon={<Plus size={15} />}>
              {t('common.create')}
            </Button>
            <div className="callout">
              <MessageSquare size={14} aria-hidden="true" />
              <span>{t('admin.assistants.callout')}</span>
            </div>
          </form>
        </Panel>
      </div>
    </AdminPageShell>
  )
}
