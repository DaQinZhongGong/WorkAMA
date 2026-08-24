/**
 * 配置中心控制台（/admin/platform-config）
 *
 * 以可视化界面取代 .env 的可运维配置管理：
 *  - GET  /api/v1/config/schema      分组目录 + 当前生效值 + 来源(db/env/default) + 是否需重启
 *  - PUT  /api/v1/config/values      批量更新（校验、落库加密、审计、发布、热生效）
 *  - POST /api/v1/config/test        连接探测（host:port TCP 可达性）
 *  - GET  /api/v1/config/history     逐键变更历史
 *  - GET  /api/v1/config/revisions   发布版本快照
 *  - POST /api/v1/config/rollback    回滚到指定 revision
 *
 * 优先级：可视化配置(DB) > ENV(.env) > 代码默认；非重启类配置写入即热生效。
 * 密钥字段落库加密，UI 仅显示掩码，支持"保持不变"语义。
 *
 * UX 契约：
 *  - 切换分组/搜索不会重新拉取或清空草稿——未保存编辑只在显式刷新/发布后重置；
 *  - 存在未发布变更时拦截窗口关闭/刷新（beforeunload）；
 *  - 每个字段显示来源徽标（UI 配置 / ENV / 默认）、需重启徽标与未保存标记。
 */
import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { RefreshCw, RotateCcw, Save, Plug, Search } from 'lucide-react'
import type { MessageKey } from '@workama/i18n'
import { api } from './api'
import { Badge, Button, DataTable, Field, Modal, PageHeader, Panel } from '@workama/ui'
import { Notice, StateGate, errorText } from './admin-ops-shared'
import { useLocale } from './locale'

const SCHEMA_ENDPOINT = '/api/v1/config/schema'
const VALUES_ENDPOINT = '/api/v1/config/values'
const TEST_ENDPOINT = '/api/v1/config/test'
const HISTORY_ENDPOINT = '/api/v1/config/history'
const REVISIONS_ENDPOINT = '/api/v1/config/revisions'
const ROLLBACK_ENDPOINT = '/api/v1/config/rollback'
const KEEP = '********'

type FieldDef = {
  key: string
  label: string
  type: string
  value: unknown
  secret: boolean
  secret_set: boolean
  source: 'db' | 'env' | 'default'
  restart_required: boolean
  required: boolean
  choices: string[]
  min?: number | null
  max?: number | null
  help: string
}

type GroupDef = { key: string; label: string; fields: FieldDef[] }
type SchemaResp = { groups: GroupDef[]; version: number }
type ValuesResp = { version: number; values: Record<string, FieldDef> }
type HistoryItem = {
  id: string
  revision: number
  key: string
  old_value: string | null
  new_value: string | null
  changed_by: string | null
  changed_at: string | null
}
type RevisionItem = {
  id: string
  revision: number
  changed_by: string | null
  changed_at: string | null
  note: string | null
}

function initialDraft(field: FieldDef): unknown {
  if (field.secret) return KEEP // 密钥默认保持
  if (field.type === 'list' && Array.isArray(field.value)) return (field.value as string[]).join(', ')
  return field.value ?? ''
}

function toPayloadValue(field: FieldDef, draft: unknown): unknown {
  if (field.type === 'bool') return Boolean(draft)
  if (field.type === 'int') return Number(draft)
  return draft
}

export default function AdminPlatformConfigPage() {
  const { t } = useLocale()
  const [groups, setGroups] = useState<GroupDef[]>([])
  const [version, setVersion] = useState(0)
  const [draft, setDraft] = useState<Record<string, unknown>>({})
  const [dirty, setDirty] = useState<Set<string>>(new Set())
  const [secretEditing, setSecretEditing] = useState<Set<string>>(new Set())
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; detail: string }>>({})
  const [query, setQuery] = useState('')
  const [activeTab, setActiveTab] = useState<string>('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [revisions, setRevisions] = useState<RevisionItem[]>([])
  const [history, setHistory] = useState<HistoryItem[]>([])
  const [rollbackTarget, setRollbackTarget] = useState<RevisionItem | null>(null)

  const reload = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const schema = await api.get<SchemaResp>(SCHEMA_ENDPOINT)
      setGroups(schema.groups)
      setVersion(schema.version ?? 0)
      const nextDraft: Record<string, unknown> = {}
      for (const g of schema.groups) for (const f of g.fields) nextDraft[f.key] = initialDraft(f)
      setDraft(nextDraft)
      setDirty(new Set())
      setSecretEditing(new Set())
      setActiveTab((cur) => cur && schema.groups.some((g) => g.key === cur) ? cur : schema.groups[0]?.key ?? '')
    } catch (caught) {
      setError(errorText(caught, t))
    } finally {
      setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const loadHistory = useCallback(async () => {
    try {
      const [rev, hist] = await Promise.all([
        api.get<{ items: RevisionItem[] }>(REVISIONS_ENDPOINT),
        api.get<{ items: HistoryItem[] }>(`${HISTORY_ENDPOINT}?limit=100`),
      ])
      setRevisions(rev.items ?? [])
      setHistory(hist.items ?? [])
    } catch (caught) {
      setError(errorText(caught, t))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    if (activeTab === '__history') void loadHistory()
  }, [activeTab, loadHistory])

  const dirtyItems = useMemo(() => {
    const items: { key: string; value: unknown }[] = []
    for (const g of groups) {
      for (const f of g.fields) {
        if (!dirty.has(f.key)) continue
        if (f.secret && draft[f.key] === KEEP) continue // 保持
        items.push({ key: f.key, value: toPayloadValue(f, draft[f.key]) })
      }
    }
    return items
  }, [groups, dirty, draft])

  // 未保存变更时拦截窗口关闭/刷新，防止误触丢失整批编辑。
  useEffect(() => {
    if (dirtyItems.length === 0) return
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [dirtyItems.length])

  const setField = (key: string, value: unknown) => {
    setDraft((d) => ({ ...d, [key]: value }))
    setDirty((s) => new Set(s).add(key))
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (dirtyItems.length === 0) {
      setNotice(t('admin.config.notice.nothingToSave'))
      return
    }
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const res = await api.put<{ version: number; revision: number; restart_required: string[] }>(
        VALUES_ENDPOINT,
        { items: dirtyItems, note: 'console-publish' },
      )
      const needRestart = (res.restart_required ?? []) as string[]
      await reload()
      setNotice(
        needRestart.length
          ? t('admin.config.notice.publishedRestart')
              .replace('{revision}', String(res.revision))
              .replace('{keys}', needRestart.join(', '))
          : t('admin.config.notice.published').replace('{revision}', String(res.revision)),
      )
    } catch (caught) {
      setError(errorText(caught, t))
    } finally {
      setBusy(false)
    }
  }

  const testConnection = async (field: FieldDef) => {
    try {
      const res = await api.post<{ ok: boolean; detail: string; key: string }>(TEST_ENDPOINT, {
        key: field.key,
        value: field.secret && draft[field.key] === KEEP ? undefined : draft[field.key],
      })
      setTestResults((r) => ({ ...r, [field.key]: { ok: res.ok, detail: res.detail } }))
    } catch (caught) {
      setTestResults((r) => ({ ...r, [field.key]: { ok: false, detail: errorText(caught, t) } }))
    }
  }

  const doRollback = async () => {
    if (!rollbackTarget) return
    const target = rollbackTarget
    setBusy(true)
    setError('')
    try {
      await api.post(ROLLBACK_ENDPOINT, {
        revision: target.revision,
        note: `console-rollback-to-${target.revision}`,
      })
      setRollbackTarget(null)
      await reload()
      await loadHistory()
      setNotice(t('admin.config.notice.rolledBack').replace('{revision}', String(target.revision)))
    } catch (caught) {
      setError(errorText(caught, t))
    } finally {
      setBusy(false)
    }
  }

  const sourceBadge = (f: FieldDef) => {
    if (f.source === 'db') return <Badge tone="success">{t('admin.config.source.db')}</Badge>
    if (f.source === 'env') return <Badge tone="info">{t('admin.config.source.env')}</Badge>
    return <Badge tone="neutral">{t('admin.config.source.default')}</Badge>
  }

  const renderInput = (field: FieldDef) => {
    const value = draft[field.key]
    if (field.type === 'bool') {
      return (
        <label className="check-line">
          <input
            type="checkbox"
            checked={Boolean(value)}
            onChange={(e) => setField(field.key, e.target.checked)}
            data-testid={`cfg-${field.key}`}
          />
          {value ? t('admin.config.enabled') : t('admin.config.disabled')}
        </label>
      )
    }
    if (field.type === 'enum') {
      return (
        <select
          value={String(value ?? '')}
          onChange={(e) => setField(field.key, e.target.value)}
          data-testid={`cfg-${field.key}`}
        >
          {(field.choices ?? []).map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      )
    }
    if (field.secret) {
      const editing = secretEditing.has(field.key)
      if (!editing) {
        return (
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <input
              type="password"
              value={KEEP}
              disabled
              readOnly
              aria-label={`${field.label} (${field.secret_set ? t('admin.config.secretSet') : t('admin.config.secretUnset')})`}
              data-testid={`cfg-${field.key}`}
            />
            {field.secret_set ? <Badge tone="success">{t('admin.config.secretSet')}</Badge> : <Badge tone="neutral">{t('admin.config.secretUnset')}</Badge>}
            <Button
              type="button"
              variant="ghost"
              onClick={() => setSecretEditing((s) => new Set(s).add(field.key))}
              data-testid={`cfg-edit-secret-${field.key}`}
            >
              {field.secret_set ? t('admin.config.secretEdit') : t('admin.config.secretNew')}
            </Button>
          </div>
        )
      }
      return (
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <input
            type="password"
            value={value === KEEP ? '' : String(value ?? '')}
            placeholder={t('admin.config.secretPlaceholder')}
            onChange={(e) => setField(field.key, e.target.value)}
            data-testid={`cfg-${field.key}`}
          />
          <Button
            type="button"
            variant="ghost"
            onClick={() => {
              setSecretEditing((s) => {
                const n = new Set(s)
                n.delete(field.key)
                return n
              })
              setField(field.key, KEEP)
            }}
          >
            {t('admin.config.secretCancel')}
          </Button>
        </div>
      )
    }
    const inputType = field.type === 'int' ? 'number' : 'text'
    return (
      <input
        type={inputType}
        value={value === null || value === undefined ? '' : String(value)}
        min={field.type === 'int' ? field.min ?? undefined : undefined}
        max={field.type === 'int' ? field.max ?? undefined : undefined}
        onChange={(e) => setField(field.key, e.target.value)}
        data-testid={`cfg-${field.key}`}
      />
    )
  }

  const canTest = (field: FieldDef) =>
    field.type === 'url' || field.key === 'minio_endpoint' || field.key === 'redis_sentinels'

  const matchesQuery = (f: FieldDef) => {
    const q = query.trim().toLowerCase()
    if (!q) return true
    return (
      f.key.toLowerCase().includes(q) ||
      f.label.toLowerCase().includes(q) ||
      (f.help ?? '').toLowerCase().includes(q)
    )
  }

  const tabStyle = (active: boolean) =>
    active ? { borderBottom: '2px solid var(--wama-accent, #2b6cb0)', fontWeight: 600 } : undefined

  return (
    <>
      <PageHeader
        eyebrow={t('admin.config.eyebrow')}
        title={t('admin.config.title')}
        description={t('admin.config.description')}
        actions={
          <>
            <span className="muted" data-testid="cfg-version" style={{ alignSelf: 'center', fontSize: 12 }}>
              {t('admin.config.versionLabel')} <code>{version}</code>
            </span>
            <Button icon={<RefreshCw size={15} />} onClick={() => void reload()} data-testid="cfg-refresh">
              {t('admin.config.refresh')}
            </Button>
            <Button
              icon={<Save size={15} />}
              variant="primary"
              onClick={submit}
              loading={busy}
              disabled={dirtyItems.length === 0}
              data-testid="cfg-save"
            >
              {dirtyItems.length
                ? `${t('admin.config.save')} (${dirtyItems.length})`
                : t('admin.config.save')}
            </Button>
          </>
        }
      />
      <Notice notice={notice} clear={() => setNotice('')} />

      <StateGate loading={loading} error={error} retry={reload}>
        <div
          role="tablist"
          aria-label={t('admin.config.title')}
          style={{ display: 'flex', gap: 4, borderBottom: '1px solid var(--wama-border, #d8dee6)', marginBottom: 12, flexWrap: 'wrap' }}
        >
          {groups.map((g) => (
            <button
              key={g.key}
              role="tab"
              aria-selected={activeTab === g.key}
              className="tab"
              onClick={() => setActiveTab(g.key)}
              data-testid={`cfg-tab-${g.key}`}
              style={tabStyle(activeTab === g.key)}
            >
              {g.label}
              {g.fields.some((f) => dirty.has(f.key)) ? ' •' : ''}
            </button>
          ))}
          <button
            role="tab"
            aria-selected={activeTab === '__history'}
            className="tab"
            onClick={() => setActiveTab('__history')}
            data-testid="cfg-tab-history"
            style={tabStyle(activeTab === '__history')}
          >
            {t('admin.config.tab.history')}
          </button>
        </div>

        {activeTab === '__history' ? (
          <Panel title={t('admin.config.history.title')} subtitle={t('admin.config.history.subtitle')}>
            <DataTable
              headers={[t('admin.config.col.revision'), t('admin.config.col.note'), t('admin.config.col.operator'), t('admin.config.col.time'), t('admin.config.col.actions')]}
              caption={t('admin.config.history.title')}
            >
              {revisions.map((r) => (
                <tr key={r.id} data-testid={`cfg-rev-${r.revision}`}>
                  <td>
                    <code>rev {r.revision}</code>
                  </td>
                  <td>{r.note ?? '—'}</td>
                  <td>
                    <code>{r.changed_by ?? '—'}</code>
                  </td>
                  <td>{r.changed_at ? new Date(r.changed_at).toLocaleString() : '—'}</td>
                  <td>
                    <Button
                      type="button"
                      variant="ghost"
                      icon={<RotateCcw size={14} />}
                      onClick={() => setRollbackTarget(r)}
                      data-testid={`cfg-rollback-${r.revision}`}
                    >
                      {t('admin.config.rollback.action')}
                    </Button>
                  </td>
                </tr>
              ))}
            </DataTable>
            <h4 style={{ marginTop: 16 }}>{t('admin.config.history.detailsTitle')}</h4>
            <DataTable
              headers={[t('admin.config.col.revision'), t('admin.config.col.key'), t('admin.config.col.oldValue'), t('admin.config.col.newValue'), t('admin.config.col.operator'), t('admin.config.col.time')]}
              caption={t('admin.config.history.detailsTitle')}
            >
              {history.map((h) => (
                <tr key={h.id} data-testid={`cfg-hist-${h.id}`}>
                  <td>
                    <code>rev {h.revision}</code>
                  </td>
                  <td>
                    <code>{h.key}</code>
                  </td>
                  <td>
                    <code>{h.old_value ?? '—'}</code>
                  </td>
                  <td>
                    <code>{h.new_value ?? '—'}</code>
                  </td>
                  <td>
                    <code>{h.changed_by ?? '—'}</code>
                  </td>
                  <td>{h.changed_at ? new Date(h.changed_at).toLocaleString() : '—'}</td>
                </tr>
              ))}
            </DataTable>
          </Panel>
        ) : (
          groups
            .filter((g) => g.key === activeTab)
            .map((g) => {
              const fields = g.fields.filter(matchesQuery)
              return (
                <Panel key={g.key} title={g.label} subtitle={g.key}>
                  <div style={{ marginBottom: 12 }}>
                    <label style={{ display: 'flex', alignItems: 'center', gap: 8, maxWidth: 420 }} className="search-wrap">
                      <Search size={14} aria-hidden />
                      <input
                        type="search"
                        value={query}
                        placeholder={t('admin.config.searchPlaceholder')}
                        onChange={(e) => setQuery(e.target.value)}
                        aria-label={t('admin.config.searchPlaceholder')}
                        data-testid="cfg-search"
                        style={{ flex: 1 }}
                      />
                    </label>
                  </div>
                  {fields.length === 0 ? (
                    <p className="muted">{t('admin.config.emptyGroup')}</p>
                  ) : (
                    <div className="form-stack">
                      {fields.map((f) => (
                        <Field key={f.key} label={f.label} hint={f.help}>
                          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                            {renderInput(f)}
                            {canTest(f) && (
                              <Button
                                type="button"
                                variant="ghost"
                                icon={<Plug size={14} />}
                                onClick={() => void testConnection(f)}
                                data-testid={`cfg-test-${f.key}`}
                              >
                                {t('admin.config.testConnection')}
                              </Button>
                            )}
                            {sourceBadge(f)}
                            {f.restart_required && <Badge tone="warning">{t('admin.config.restartRequired')}</Badge>}
                            {dirty.has(f.key) && !(f.secret && draft[f.key] === KEEP) && (
                              <Badge tone="danger">{t('admin.config.dirtyBadge')}</Badge>
                            )}
                          </div>
                          {testResults[f.key] && (
                            <small style={{ color: testResults[f.key].ok ? undefined : '#b00020' }} role="status">
                              {testResults[f.key].ok ? '✓ ' : '✗ '}
                              {testResults[f.key].detail}
                            </small>
                          )}
                        </Field>
                      ))}
                    </div>
                  )}
                </Panel>
              )
            })
        )}
      </StateGate>

      {rollbackTarget && (
        <Modal
          title={t('admin.config.rollback.modalTitle').replace('{revision}', `rev ${rollbackTarget.revision}`)}
          onClose={() => setRollbackTarget(null)}
        >
          <div className="form-stack">
            <div className="alert">
              <p>{t('admin.config.rollback.body')}</p>
            </div>
            <Button
              type="button"
              variant="primary"
              loading={busy}
              onClick={() => void doRollback()}
              data-testid="cfg-rollback-confirm"
            >
              {t('admin.config.rollback.confirm')}
            </Button>
          </div>
        </Modal>
      )}
    </>
  )
}
