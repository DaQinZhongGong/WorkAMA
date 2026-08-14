/**
 * 记忆向量：概览指标 + 向量列表 + 写入 + 语义召回。
 */
import { useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { Brain, Gauge, Hash, Plus, Radar, Search } from 'lucide-react'
import { api, errorMessage } from './api'
import { AdminPageShell, DeleteButton, useResource } from './admin-shared'
import { Button, Field, Kpi, Panel, SearchBox, StateView } from './ui'
import { useLocale } from './locale'

type MemoryVector = {
  id: string
  content: string
  metadata?: Record<string, unknown>
  score?: number
}

const PREVIEW_LENGTH = 80

/** 内容摘要：超长内容截断，保持列表行高一致。 */
function preview(content: string): string {
  return content.length > PREVIEW_LENGTH ? `${content.slice(0, PREVIEW_LENGTH)}…` : content
}

/** 相似度分数展示为两位小数，非法值回退为原始文本。 */
function formatScore(score: number): string {
  return Number.isFinite(score) ? score.toFixed(2) : String(score)
}

export default function AdminMemoryVectorsPage(): ReactNode {
  const { items, loading, error, reload, create, remove, busy } = useResource<MemoryVector>(
    '/api/v1/memory/vectors',
  )
  const [filter, setFilter] = useState('')
  const [content, setContent] = useState('')
  const [formError, setFormError] = useState('')
  const [query, setQuery] = useState('')
  const [recallResult, setRecallResult] = useState('')
  const [recallMatches, setRecallMatches] = useState<MemoryVector[]>([])
  const [recallError, setRecallError] = useState('')
  const [recalling, setRecalling] = useState(false)
  const contentRef = useRef<HTMLTextAreaElement>(null)
  const { t } = useLocale()

  const filtered = useMemo(() => {
    const keyword = filter.trim().toLowerCase()
    if (!keyword) return items
    return items.filter((item) => item.content.toLowerCase().includes(keyword))
  }, [items, filter])

  const stats = useMemo(() => {
    const scored = items.filter((item) => typeof item.score === 'number')
    const averageScore = scored.length
      ? scored.reduce((sum, item) => sum + (item.score ?? 0), 0) / scored.length
      : null
    const metadataKeys = new Set<string>()
    items.forEach((item) => {
      Object.keys(item.metadata ?? {}).forEach((key) => metadataKeys.add(key))
    })
    const characters = items.reduce((sum, item) => sum + item.content.length, 0)
    return { averageScore, metadataKeys: metadataKeys.size, characters }
  }, [items])

  function focusCreate() {
    contentRef.current?.focus()
    contentRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'center' })
  }

  async function submitCreate(event: FormEvent) {
    event.preventDefault()
    setFormError('')
    try {
      await create({ content })
      setContent('')
    } catch (caught) {
      setFormError(caught instanceof Error ? caught.message : t('common.createFailed'))
    }
  }

  async function recall(event: FormEvent) {
    event.preventDefault()
    if (!query) return
    setRecallError('')
    setRecalling(true)
    setRecallMatches([])
    setRecallResult(t('admin.memory.recalling'))
    try {
      const result = await api.post<{ matches?: MemoryVector[]; answer?: string }>(
        '/api/v1/memory/vectors/recall',
        { query },
      )
      const matches = result?.matches ?? []
      setRecallMatches(matches)
      setRecallResult(
        matches.length > 0
          ? matches.map((m) => m.content).join('\n---\n')
          : result?.answer ?? t('admin.memory.noMatch'),
      )
    } catch (caught) {
      setRecallError(errorMessage(caught, t('common.invokeFailed')))
      setRecallResult('')
      setRecallMatches([])
    } finally {
      setRecalling(false)
    }
  }

  return (
    <AdminPageShell
      title={t('admin.memory.title')}
      subtitle={t('admin.memory.subtitle')}
      testId="memory-vectors-page"
      loading={loading}
      error={error}
      onRetry={reload}
      actions={
        <Button variant="primary" icon={<Plus size={15} />} onClick={focusCreate}>
          {t('admin.memory.write')}
        </Button>
      }
    >
      <div className="kpi-grid">
        <Kpi
          label={t('admin.memory.kpi.count')}
          value={String(items.length).padStart(2, '0')}
          icon={<Brain size={18} />}
          trend={t('admin.memory.kpi.count.trend')}
        />
        <Kpi
          label={t('admin.memory.kpi.score')}
          value={stats.averageScore === null ? '--' : formatScore(stats.averageScore)}
          icon={<Gauge size={18} />}
          trend={stats.averageScore === null ? t('admin.memory.kpi.score.trend.none') : t('admin.memory.kpi.score.trend')}
        />
        <Kpi
          label={t('admin.memory.kpi.metadata')}
          value={String(stats.metadataKeys).padStart(2, '0')}
          icon={<Hash size={18} />}
          trend={t('admin.memory.kpi.metadata.trend')}
        />
        <Kpi
          label={t('admin.memory.kpi.corpus')}
          value={String(stats.characters)}
          icon={<Radar size={18} />}
          trend={t('admin.memory.kpi.corpus.trend')}
        />
      </div>

      <div className="ops-grid">
        <Panel
          title={t('admin.memory.list.title')}
          subtitle={t('admin.memory.list.subtitle')}
          actions={<SearchBox value={filter} onChange={setFilter} placeholder={t('admin.memory.search')} />}
        >
          {items.length === 0 ? (
            <StateView
              state="empty"
              title={t('admin.memory.empty.title')}
              description={t('admin.memory.empty.desc')}
            />
          ) : filtered.length === 0 ? (
            <StateView
              state="empty"
              title={t('admin.memory.emptyFiltered.title')}
              description={t('admin.memory.emptyFiltered.desc')}
            />
          ) : (
            <div className="resource-grid" data-testid="mv-list">
              {filtered.map((item) => {
                const metadataKeys = Object.keys(item.metadata ?? {})
                return (
                  <div key={item.id} className="resource-card" data-testid="mv-item">
                    <div className="resource-icon purple">
                      <Brain size={18} />
                    </div>
                    <div className="resource-main">
                      <strong>{preview(item.content)}</strong>
                      <p>
                        {metadataKeys.length > 0
                          ? `元数据 ${metadataKeys.join(' · ')}`
                          : t('admin.memory.noMetadata')}
                      </p>
                      <span>ID {item.id}</span>
                    </div>
                    <div className="panel-actions-inline">
                      {item.score !== undefined && (
                        <span className="badge badge-info">score {formatScore(item.score)}</span>
                      )}
                    </div>
                    <div className="knowledge-actions">
                      <DeleteButton
                        testId={`mv-delete-${item.id}`}
                        onDelete={() => void remove(item.id)}
                        busy={busy}
                      />
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </Panel>

        <div className="knowledge-column">
          <Panel
            title={t('admin.memory.create.title')}
            subtitle={t('admin.memory.create.subtitle')}
          >
            <form className="form-stack" data-testid="mv-create" onSubmit={submitCreate}>
              <Field
                label={t('admin.memory.field.content')}
                hint={t('admin.memory.field.content.hint')}
              >
                <textarea
                  ref={contentRef}
                  name="content"
                  rows={4}
                  value={content}
                  placeholder={t('admin.memory.field.content.placeholder')}
                  onChange={(event) => setContent(event.target.value)}
                  data-testid="mv-create-content"
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
            title={t('admin.memory.recall.title')}
            subtitle={t('admin.memory.recall.subtitle')}
          >
            <form className="form-stack" data-testid="mv-recall-form" onSubmit={recall}>
              <Field
                label={t('admin.memory.field.recall')}
                hint={t('admin.memory.field.recall.hint')}
              >
                <input
                  type="text"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder={t('admin.memory.field.recall.placeholder')}
                  data-testid="mv-recall-input"
                />
              </Field>
              <Button
                type="submit"
                variant="primary"
                loading={recalling}
                icon={<Search size={15} />}
                data-testid="mv-recall-submit"
              >
                {t('admin.memory.recall.submit')}
              </Button>
              {recallError && (
                <div className="alert alert-error" role="alert">
                  {recallError}
                </div>
              )}
              {(recallResult || recallMatches.length > 0) && (
                <div className="retrieval-query" data-testid="mv-recall-result">
                  {recallMatches.length > 0 ? (
                    <div className="hit-list">
                      {recallMatches.map((match, index) => (
                        <div className="hit" key={match.id ?? index}>
                          <div className="hit-topline">
                            <span className="badge badge-neutral">#{index + 1}</span>
                            {match.score !== undefined && (
                              <span>相似度 {formatScore(match.score)}</span>
                            )}
                          </div>
                          <p>{match.content}</p>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="alert alert-info">{recallResult}</div>
                  )}
                </div>
              )}
            </form>
          </Panel>
        </div>
      </div>
    </AdminPageShell>
  )
}
