/**
 * 知识库管理：概览指标 + 知识库列表 + 创建 + RAG 查询（最小 CRUD）。
 */
import { useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import {
  ArrowUpRight,
  Database,
  FileText,
  Layers,
  Plus,
  Search,
  Sparkles,
} from 'lucide-react'
import { api, errorMessage } from './api'
import { AdminPageShell, DeleteButton, useResource } from './admin-shared'
import { Button, Field, Kpi, Panel, SearchBox, StateView, Status } from './ui'
import { useLocale } from './locale'

type KnowledgeBase = {
  id: string
  name: string
  document_count?: number
  status?: string
  created_at?: string
}

/** 时间戳格式化，缺省时返回占位文案而不是伪造数据。 */
function formatCreated(value?: string, missing = '创建时间未记录'): string {
  if (!value) return missing
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return `创建于 ${value}`
  return `创建于 ${parsed.toLocaleDateString()}`
}

export default function AdminKnowledgeBasesPage(): ReactNode {
  const { items, loading, error, reload, create, remove, busy } = useResource<KnowledgeBase>(
    '/api/v1/knowledge-bases',
  )
  const [filter, setFilter] = useState('')
  const [name, setName] = useState('')
  const [formError, setFormError] = useState('')
  const [query, setQuery] = useState('')
  const [queryResult, setQueryResult] = useState('')
  const [queryError, setQueryError] = useState('')
  const [querying, setQuerying] = useState(false)
  const { t } = useLocale()
  const nameRef = useRef<HTMLInputElement>(null)

  const filtered = useMemo(() => {
    const keyword = filter.trim().toLowerCase()
    if (!keyword) return items
    return items.filter((item) => item.name.toLowerCase().includes(keyword))
  }, [items, filter])

  const stats = useMemo(() => {
    const documents = items.reduce((sum, item) => sum + (item.document_count ?? 0), 0)
    const indexed = items.filter((item) =>
      ['indexed', 'active', 'ready'].includes(String(item.status ?? '').toLowerCase()),
    ).length
    const average = items.length ? Math.round(documents / items.length) : 0
    return { documents, indexed, average }
  }, [items])

  function focusCreate() {
    nameRef.current?.focus()
    nameRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'center' })
  }

  async function submitCreate(event: FormEvent) {
    event.preventDefault()
    setFormError('')
    try {
      await create({ name })
      setName('')
    } catch (caught) {
      setFormError(caught instanceof Error ? caught.message : t('common.createFailed'))
    }
  }

  async function runRagQuery(event: FormEvent) {
    event.preventDefault()
    if (!query) return
    setQueryError('')
    setQuerying(true)
    setQueryResult(t('common.querying'))
    try {
      const result = await api.post<{ answer?: string }>('/api/v1/knowledge-bases/query', {
        query,
      })
      setQueryResult(result?.answer ?? t('common.noResult'))
    } catch (caught) {
      setQueryError(errorMessage(caught, t('common.queryFailed')))
      setQueryResult('')
    } finally {
      setQuerying(false)
    }
  }

  return (
    <AdminPageShell
      title={t('admin.knowledge.title')}
      subtitle={t('admin.knowledge.subtitle')}
      testId="knowledge-bases-page"
      loading={loading}
      error={error}
      onRetry={reload}
      actions={
        <>
          <Link className="button button-secondary" to="/knowledge">
            知识工作台 <ArrowUpRight size={14} />
          </Link>
          <Button variant="primary" icon={<Plus size={15} />} onClick={focusCreate}>
            {t('admin.knowledge.new')}
          </Button>
        </>
      }
    >
      <div className="kpi-grid">
        <Kpi
          label={t('admin.knowledge.kpi.count')}
          value={String(items.length).padStart(2, '0')}
          icon={<Database size={18} />}
          trend={t('admin.knowledge.kpi.count.trend')}
        />
        <Kpi
          label={t('admin.knowledge.kpi.documents')}
          value={String(stats.documents)}
          icon={<FileText size={18} />}
          trend={t('admin.knowledge.kpi.documents.trend')}
        />
        <Kpi
          label={t('admin.knowledge.kpi.average')}
          value={String(stats.average)}
          icon={<Layers size={18} />}
          trend={t('admin.knowledge.kpi.average.trend')}
        />
        <Kpi
          label={t('admin.knowledge.kpi.indexed')}
          value={items.length ? `${stats.indexed}/${items.length}` : '0'}
          icon={<Sparkles size={18} />}
          trend={t('admin.knowledge.kpi.indexed.trend')}
        />
      </div>

      <div className="ops-grid">
        <Panel
          title={t('admin.knowledge.list.title')}
          subtitle={t('admin.knowledge.list.subtitle')}
          actions={<SearchBox value={filter} onChange={setFilter} placeholder={t('admin.knowledge.search')} />}
        >
          {items.length === 0 ? (
            <StateView
              state="empty"
              title={t('admin.knowledge.empty.title')}
              description={t('admin.knowledge.empty.desc')}
            />
          ) : filtered.length === 0 ? (
            <StateView
              state="empty"
              title={t('admin.knowledge.emptyFiltered.title')}
              description={t('admin.knowledge.emptyFiltered.desc')}
            />
          ) : (
            <div className="resource-grid" data-testid="kb-list">
              {filtered.map((item) => (
                <div key={item.id} className="resource-card" data-testid="kb-item">
                  <div className="resource-icon blue">
                    <Database size={18} />
                  </div>
                  <div className="resource-main">
                    <strong>{item.name}</strong>
                    <p>{formatCreated(item.created_at, t('admin.knowledge.created'))}</p>
                    <span>ID {item.id}</span>
                  </div>
                  <div className="panel-actions-inline">
                    {item.document_count !== undefined && (
                      <span className="badge badge-neutral">
                        {t('admin.knowledge.documents')} {item.document_count}
                      </span>
                    )}
                    {item.status && <Status value={item.status} />}
                  </div>
                  <div className="knowledge-actions">
                    <DeleteButton
                      testId={`kb-delete-${item.id}`}
                      onDelete={() => void remove(item.id)}
                      busy={busy}
                    />
                  </div>
                </div>
              ))}
            </div>
          )}
        </Panel>

        <div className="knowledge-column">
          <Panel
            title={t('admin.knowledge.create.title')}
            subtitle={t('admin.knowledge.create.subtitle')}
          >
            <form className="form-stack" data-testid="kb-create" onSubmit={submitCreate}>
              <Field
                label={t('admin.knowledge.field.name')}
                hint={t('admin.knowledge.field.name.hint')}
              >
                <input
                  ref={nameRef}
                  name="name"
                  value={name}
                  placeholder={t('admin.knowledge.field.name.placeholder')}
                  onChange={(event) => setName(event.target.value)}
                  data-testid="kb-create-name"
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
            </form>
          </Panel>

          <Panel
            title={t('admin.knowledge.query.title')}
            subtitle={t('admin.knowledge.query.subtitle')}
          >
            <form className="form-stack" data-testid="kb-query-form" onSubmit={runRagQuery}>
              <Field
                label={t('admin.knowledge.field.query')}
                hint={t('admin.knowledge.field.query.hint')}
              >
                <input
                  type="text"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder={t('admin.knowledge.field.query.placeholder')}
                  data-testid="kb-query-input"
                />
              </Field>
              <Button
                type="submit"
                variant="primary"
                loading={querying}
                icon={<Search size={15} />}
                data-testid="kb-query-submit"
              >
                {t('admin.knowledge.query.submit')}
              </Button>
              {queryError && (
                <div className="alert alert-error" role="alert">
                  {queryError}
                </div>
              )}
              {queryResult && (
                <div className="hit" data-testid="kb-query-result">
                  <div className="hit-topline">
                    <span className="badge badge-info">回答</span>
                  </div>
                  <p>{queryResult}</p>
                </div>
              )}
            </form>
          </Panel>
        </div>
      </div>
    </AdminPageShell>
  )
}
