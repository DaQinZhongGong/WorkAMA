/**
 * 审计日志：概览指标 + 过滤（动作 / 操作者 / 严重度 / 时间）+ 表格视图。
 * 对应《410-安全与合规设计》FR-X-07（审计导出/SIEM）。
 */
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import {
  Activity,
  Clock,
  Eye,
  Filter,
  RefreshCw,
  ShieldCheck,
  User,
} from 'lucide-react'
import { api, asItems, errorMessage } from './api'
import { Badge, Button, Field, Kpi, Panel, SearchBox, StateView, Status } from './ui'
import { useLocale } from './locale'

type AuditLog = {
  id: string
  action?: string
  actor?: string
  resource?: string
  resource_type?: string
  severity?: string
  ip?: string
  timestamp?: string
  detail?: string
}

const SEVERITY_TONE: Record<string, string> = {
  info: 'badge-info',
  warning: 'badge-warning',
  danger: 'badge-danger',
  critical: 'badge-danger',
}

/** 严重度归一化：unknown / info / warning / danger。 */
function normalizeSeverity(severity?: string): string {
  const v = (severity ?? '').toLowerCase()
  if (['critical', 'danger', 'error', 'high'].includes(v)) return 'danger'
  if (['warn', 'warning', 'medium'].includes(v)) return 'warning'
  return 'info'
}

/** 时间戳格式化：相对 + 绝对。 */
function formatTime(value?: string): string {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString('zh-CN', { hour12: false })
}

export default function AdminAuditLogsPage(): ReactNode {
  const [items, setItems] = useState<AuditLog[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [severity, setSeverity] = useState('')
  const { t } = useLocale()

  function reload() {
    setLoading(true)
    setError('')
    void api
      .get<unknown>('/api/v1/audit-logs')
      .then((payload) => setItems(asItems<AuditLog>(payload)))
      .catch((caught) => setError(errorMessage(caught, t('common.loadFailed'))))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    reload()
  }, [])

  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase()
    return items.filter((item) => {
      if (severity && normalizeSeverity(item.severity) !== severity) return false
      if (!keyword) return true
      return [item.action, item.actor, item.resource, item.resource_type, item.detail, item.ip]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
        .includes(keyword)
    })
  }, [items, query, severity])

  const stats = useMemo(() => {
    const actors = new Set(items.map((item) => item.actor ?? 'system')).size
    const danger = items.filter((item) => normalizeSeverity(item.severity) === 'danger').length
    const recent = items.filter((item) => {
      if (!item.timestamp) return false
      const ageMs = Date.now() - new Date(item.timestamp).getTime()
      return Number.isFinite(ageMs) && ageMs < 24 * 60 * 60 * 1000
    }).length
    return { actors, danger, recent }
  }, [items])

  const severityOptions = useMemo(() => {
    const set = new Set(items.map((item) => normalizeSeverity(item.severity)))
    return Array.from(set).sort()
  }, [items])

  return (
    <div data-testid="audit-logs-page">
      <header className="page-header">
        <div>
          <div className="eyebrow">{t('admin.audit.eyebrow')}</div>
          <h1>{t('admin.audit.title')}</h1>
          <p>{t('admin.audit.subtitle')}</p>
        </div>
        <div className="page-actions">
          <Button icon={<RefreshCw size={15} />} onClick={reload} disabled={loading} data-testid="audit-refresh">
            {t('common.refresh')}
          </Button>
        </div>
      </header>

      {loading ? (
        <StateView state="loading" />
      ) : error ? (
        <StateView state="error" description={error} onRetry={reload} />
      ) : (
        <>
          {items.length > 0 && (
            <div className="kpi-grid" data-testid="audit-kpis">
              <Kpi
                label={t('admin.audit.kpi.total')}
                value={String(items.length).padStart(3, '0')}
                icon={<Activity size={18} />}
                trend={t('admin.audit.kpi.total.trend')}
              />
              <Kpi
                label={t('admin.audit.kpi.recent')}
                value={String(stats.recent).padStart(2, '0')}
                icon={<Clock size={18} />}
                trend={t('admin.audit.kpi.recent.trend')}
              />
              <Kpi
                label={t('admin.audit.kpi.danger')}
                value={String(stats.danger).padStart(2, '0')}
                icon={<ShieldCheck size={18} />}
                trend={t('admin.audit.kpi.danger.trend')}
              />
              <Kpi
                label={t('admin.audit.kpi.actors')}
                value={String(stats.actors).padStart(2, '0')}
                icon={<User size={18} />}
                trend={t('admin.audit.kpi.actors.trend')}
              />
            </div>
          )}

          {items.length === 0 && (
            <StateView
              state="empty"
              title={t('admin.audit.empty.title')}
              description={t('admin.audit.empty.desc')}
            />
          )}

          <Panel
            title={t('admin.audit.list.title')}
            subtitle={t('admin.audit.list.subtitle')}
            actions={
              <Badge tone="info">
                <Eye size={12} /> {t('common.total')} {filtered.length}/{items.length}
              </Badge>
            }
          >
            <div className="filters-row" data-testid="audit-filters">
              <label className="search-box">
                <input
                  type="search"
                  aria-label={t('admin.audit.filter.label')}
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder={t('admin.audit.filter.placeholder')}
                  data-testid="audit-filter-input"
                />
              </label>
              <Field label={t('common.severity')}>
                <select
                  value={severity}
                  onChange={(e) => setSeverity(e.target.value)}
                  data-testid="audit-severity-filter"
                >
                  <option value="">{t('common.all')}</option>
                  {severityOptions.map((opt) => (
                    <option key={opt} value={opt}>
                      {opt}
                    </option>
                  ))}
                </select>
              </Field>
              <Button
                variant="ghost"
                onClick={() => {
                  setQuery('')
                  setSeverity('')
                }}
                data-testid="audit-filter-reset"
                icon={<Filter size={14} />}
              >
                {t('common.reset')}
              </Button>
            </div>

            {filtered.length === 0 ? (
              <StateView
                state="empty"
                title={t('admin.audit.emptyFiltered.title')}
                description={t('admin.audit.emptyFiltered.desc')}
              />
            ) : (
              <ul className="resource-list" data-testid="audit-logs-list">
                {filtered.map((item) => {
                  const sev = normalizeSeverity(item.severity)
                  const sevTone = SEVERITY_TONE[sev] ?? 'badge-neutral'
                  return (
                    <li
                      key={item.id}
                      className="resource-item"
                      data-testid="audit-logs-item"
                      data-severity={sev}
                    >
                      <div className="resource-info">
                        <strong>
                          <span className={`badge ${sevTone}`}>{sev}</span>
                          <span>{item.action ?? 'unknown'}</span>
                        </strong>
                        <small>
                          {item.actor ?? 'system'}
                          {item.resource_type ? ` · ${item.resource_type}` : ''}
                          {item.resource ? ` · ${item.resource}` : ''}
                          {item.ip ? ` · ${item.ip}` : ''}
                        </small>
                        {item.detail && <span>{item.detail}</span>}
                      </div>
                      <div className="panel-actions-inline">
                        <span className="muted">{formatTime(item.timestamp)}</span>
                        <Status value={sev} />
                      </div>
                    </li>
                  )
                })}
              </ul>
            )}
          </Panel>
        </>
      )}
    </div>
  )
}