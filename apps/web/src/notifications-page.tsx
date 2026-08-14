/**
 * 通知中心：概览指标 + 分类过滤 + 通知列表 + 标记已读。
 * 对应《550-异步任务通知搜索与平台支撑设计》通知中心页面。
 */
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import {
  AlertCircle,
  Bell,
  BellOff,
  CheckCheck,
  CircleAlert,
  CircleCheck,
  Filter,
  Info,
  RefreshCw,
  Zap,
} from 'lucide-react'
import { api, asItems, errorMessage } from './api'
import { Badge, Button, Field, Kpi, Panel, SearchBox, StateView, Status } from './ui'
import { useLocale } from './locale'

type Notification = {
  id: string
  title?: string
  message?: string
  read?: boolean
  type?: string
  severity?: string
  created_at?: string
}

type Tab = 'all' | 'unread' | 'read'

const TYPE_TONE: Record<string, string> = {
  info: 'badge-info',
  success: 'badge-success',
  warning: 'badge-warning',
  danger: 'badge-danger',
  alert: 'badge-warning',
  system: 'badge-neutral',
}

/** 通知类型 → 图标组件，未知类型回退为 Info。 */
function typeIcon(type?: string): ReactNode {
  switch ((type ?? '').toLowerCase()) {
    case 'success':
      return <CircleCheck size={15} />
    case 'warning':
    case 'alert':
      return <CircleAlert size={15} />
    case 'danger':
      return <AlertCircle size={15} />
    case 'system':
      return <Zap size={15} />
    default:
      return <Info size={15} />
  }
}

/** 时间戳格式化：相对 + 绝对双显示。 */
function formatTime(value?: string): string {
  if (!value) return '刚刚'
  const parsed = new Date(value).getTime()
  if (Number.isNaN(parsed)) return value
  const ageMs = Date.now() - parsed
  if (ageMs < 60 * 1000) return '刚刚'
  if (ageMs < 60 * 60 * 1000) return `${Math.round(ageMs / 60000)} 分钟前`
  if (ageMs < 24 * 60 * 60 * 1000) return `${Math.round(ageMs / 3600000)} 小时前`
  return new Date(parsed).toLocaleString('zh-CN', { hour12: false })
}

export default function AdminNotificationsPage(): ReactNode {
  const [items, setItems] = useState<Notification[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [tab, setTab] = useState<Tab>('all')
  const [typeFilter, setTypeFilter] = useState('')
  const [query, setQuery] = useState('')
  const { t } = useLocale()

  function reload() {
    setLoading(true)
    setError('')
    void api
      .get<unknown>('/api/v1/notifications')
      .then((payload) => setItems(asItems<Notification>(payload)))
      .catch((caught) => setError(errorMessage(caught, '加载通知失败')))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    reload()
  }, [])

  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase()
    return items.filter((item) => {
      if (tab === 'unread' && item.read) return false
      if (tab === 'read' && !item.read) return false
      if (typeFilter && (item.type ?? '').toLowerCase() !== typeFilter.toLowerCase()) return false
      if (!keyword) return true
      return `${item.title ?? ''} ${item.message ?? ''} ${item.type ?? ''}`
        .toLowerCase()
        .includes(keyword)
    })
  }, [items, tab, typeFilter, query])

  const stats = useMemo(() => {
    const unread = items.filter((item) => !item.read).length
    const types = new Set(items.map((item) => (item.type ?? 'info').toLowerCase()))
    return { unread, types: types.size }
  }, [items])

  const typeOptions = useMemo(
    () => Array.from(new Set(items.map((item) => (item.type ?? 'info').toLowerCase()))).sort(),
    [items],
  )

  async function markRead(id: string) {
    try {
      await api.post(`/api/v1/notifications/${encodeURIComponent(id)}/read`)
      setItems((current) =>
        current.map((item) => (item.id === id ? { ...item, read: true } : item)),
      )
    } catch (caught) {
      setError(errorMessage(caught, '标记已读失败'))
    }
  }

  async function markAllRead() {
    try {
      await api.post('/api/v1/notifications/read-all')
      setItems((current) => current.map((item) => ({ ...item, read: true })))
    } catch (caught) {
      setError(errorMessage(caught, '全部标记已读失败'))
    }
  }

  return (
    <div data-testid="notifications-page">
      <header className="page-header">
        <div>
          <div className="eyebrow">{t('admin.notifications.eyebrow')}</div>
          <h1>{t('admin.notifications.title')}</h1>
          <p>{t('admin.notifications.subtitle')}</p>
        </div>
        <div className="page-actions">
          <Button variant="ghost" onClick={() => void markAllRead()} data-testid="notifications-mark-all">
            <CheckCheck size={15} /> {t('common.markAllRead')}
          </Button>
          <Button icon={<RefreshCw size={15} />} onClick={reload} disabled={loading} data-testid="notifications-refresh">
            {t('common.refresh')}
          </Button>
        </div>
      </header>

      {loading ? (
        <StateView state="loading" />
      ) : error ? (
        <StateView state="error" description={error} onRetry={reload} />
      ) : items.length === 0 ? (
        <StateView state="empty" title={t('admin.notifications.empty.title')} description={t('admin.notifications.empty.desc')} />
      ) : (
        <>
          <div className="kpi-grid" data-testid="notifications-kpis">
            <Kpi
              label={t('admin.notifications.kpi.unread')}
              value={String(stats.unread).padStart(2, '0')}
              icon={<Bell size={18} />}
              trend={t('admin.notifications.kpi.unread.trend')}
            />
            <Kpi
              label={t('admin.notifications.kpi.read')}
              value={String(items.length - stats.unread).padStart(2, '0')}
              icon={<BellOff size={18} />}
              trend={t('admin.notifications.kpi.read.trend')}
            />
            <Kpi
              label={t('admin.notifications.kpi.types')}
              value={String(stats.types).padStart(2, '0')}
              icon={<Filter size={18} />}
              trend={t('admin.notifications.kpi.types.trend')}
            />
            <Kpi
              label={t('admin.notifications.kpi.total')}
              value={String(items.length).padStart(2, '0')}
              icon={<Info size={18} />}
              trend={t('admin.notifications.kpi.total.trend')}
            />
          </div>

          <Panel
            title={t('admin.notifications.list.title')}
            subtitle={t('admin.notifications.list.subtitle')}
            actions={
              <div className="panel-actions-inline" role="tablist" data-testid="notifications-tabs">
                {(['all', 'unread', 'read'] as const).map((value) => (
                  <button
                    key={value}
                    type="button"
                    className="tab-pill"
                    data-testid={`notifications-tab-${value}`}
                    data-active={tab === value}
                    onClick={() => setTab(value)}
                    role="tab"
                  >
                    {t(
                      value === 'all'
                        ? 'admin.notifications.tab.all'
                        : value === 'unread'
                          ? 'admin.notifications.tab.unread'
                          : 'admin.notifications.tab.read',
                    )}
                  </button>
                ))}
              </div>
            }
          >
            <div className="filters-row">
              <SearchBox value={query} onChange={setQuery} placeholder={t('admin.notifications.search')} />
              <Field label={t('admin.notifications.type.label')}>
                <select
                  value={typeFilter}
                  onChange={(e) => setTypeFilter(e.target.value)}
                  data-testid="notifications-type-filter"
                >
                  <option value="">{t('admin.notifications.type.all')}</option>
                  {typeOptions.map((opt) => (
                    <option key={opt} value={opt}>
                      {opt}
                    </option>
                  ))}
                </select>
              </Field>
            </div>

            {filtered.length === 0 ? (
              <StateView
                state="empty"
                title={t('admin.notifications.emptyFiltered.title')}
                description={t('admin.notifications.emptyFiltered.desc')}
              />
            ) : (
              <ul className="resource-list" data-testid="notifications-list">
                {filtered.map((item) => (
                  <li
                    key={item.id}
                    className={`resource-item ${item.read ? 'is-read' : 'is-unread'}`}
                    data-testid="notifications-item"
                    data-notification-id={item.id}
                  >
                    <div className="resource-info">
                      <strong>
                        {typeIcon(item.type)} <span>{item.title ?? t('admin.notifications.fallback')}</span>
                      </strong>
                      {item.message && <span>{item.message}</span>}
                      <small>
                        {item.type ?? 'info'} · {formatTime(item.created_at)}
                      </small>
                    </div>
                    <div className="panel-actions-inline">
                      {item.severity && <Status value={item.severity} />}
                      {!item.read && (
                        <Button
                          variant="ghost"
                          onClick={() => void markRead(item.id)}
                          data-testid={`notifications-read-${item.id}`}
                        >
                          {t('common.markRead')}
                        </Button>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        </>
      )}
    </div>
  )
}