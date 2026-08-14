/**
 * 订阅计费：套餐概览 + 当前订阅 KPI + 套餐对比卡片 + 用量进度 + 最近计费事件。
 * 对应《540-计费与商业化设计》FR-X-04/05（订阅、用量、积分账本）。
 */
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import {
  Activity,
  AlertCircle,
  BadgeCheck,
  Coins,
  CreditCard,
  Database,
  Gauge,
  Layers,
  Sparkles,
  Zap,
} from 'lucide-react'
import { api, errorMessage } from './api'
import { useLocale } from './locale'
import { Badge, Button, Kpi, Panel, StateView, Status } from './ui'

type Plan = {
  id: string
  name: string
  code?: string
  price?: number
  currency?: string
  seats?: number
  monthly_credits?: number
  features?: string[]
}

type Subscription = {
  plan_id?: string
  plan_code?: string
  plan_name?: string
  status?: string
  seats?: number
  renew_at?: string
  started_at?: string
}

type Usage = {
  requests?: number
  tokens?: number
  storage_mb?: number
  credits_used?: number
  month?: string
}

type BillingEvent = {
  id: string
  type?: string
  amount?: number
  currency?: string
  description?: string
  created_at?: string
}

type BillingData = {
  plans?: Plan[]
  subscription?: Subscription
  usage?: Usage
  events?: BillingEvent[]
}

const CURRENCY_SYMBOL: Record<string, string> = { CNY: '¥', USD: '$', EUR: '€' }

/** 整数千分位格式化，非法值回退为占位文本。 */
function formatNumber(value: number | undefined): string {
  if (value === undefined || value === null || Number.isNaN(value)) return '—'
  return value.toLocaleString('zh-CN')
}

/** 字节 → 人类可读；保留 1 位小数。 */
function formatBytes(mb?: number): string {
  if (mb === undefined || mb === null) return '—'
  if (mb < 1024) return `${mb.toFixed(0)} MB`
  const gb = mb / 1024
  return `${gb.toFixed(2)} GB`
}

/** ISO 时间戳 → 中文 locale 短格式；非法值回退原始字符串。 */
function formatTime(value?: string): string {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString('zh-CN', { hour12: false })
}

/** 货币价格展示：带货币符号，无值显示「免费」。 */
function formatPrice(plan: Plan): string {
  if (plan.price === undefined || plan.price === null || plan.price === 0) return '免费'
  const symbol = CURRENCY_SYMBOL[plan.currency ?? 'CNY'] ?? (plan.currency ?? '¥')
  return `${symbol}${plan.price}`
}

/** 计费事件类型 → 中文/图标色调。 */
const EVENT_TONE: Record<string, string> = {
  credit_grant: 'badge-info',
  usage: 'badge-neutral',
  refund: 'badge-warning',
  invoice: 'badge-success',
}

export default function AdminBillingPage(): ReactNode {
  const { t } = useLocale()
  const [data, setData] = useState<BillingData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  function reload() {
    setLoading(true)
    setError('')
    void api
      .get<BillingData>('/api/v1/billing/overview')
      .then(setData)
      .catch((caught) => setError(errorMessage(caught, t('billing.loadFailed'))))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    reload()
  }, [])

  const sub = data?.subscription
  const usage = data?.usage
  const plans = data?.plans ?? []
  const events = data?.events ?? []

  const usageProgress = useMemo(() => {
    if (!usage?.credits_used || !sub?.seats) return null
    // 仅当 plan 暴露月度积分上限时才有完整进度；否则只展示绝对值。
    const plan = plans.find((p) => p.id === sub.plan_id)
    if (!plan?.monthly_credits) return null
    return Math.min(100, Math.round((usage.credits_used / plan.monthly_credits) * 100))
  }, [plans, sub, usage])

  return (
    <div data-testid="billing-page">
      <header className="page-header">
        <div>
          <div className="eyebrow">{t('billing.eyebrow')}</div>
          <h1>{t('page.billing')}</h1>
          <p>{t('billing.description')}</p>
        </div>
        <div className="page-actions">
          <Button variant="ghost" onClick={reload} disabled={loading} data-testid="billing-refresh">
            {t('billing.refresh')}
          </Button>
        </div>
      </header>

      {loading ? (
        <StateView state="loading" />
      ) : error ? (
        <StateView state="error" description={error} onRetry={reload} />
      ) : (
        <>
          <div className="kpi-grid" data-testid="billing-kpis">
            <Kpi
              label={t('billing.kpi.currentPlan')}
              value={sub?.plan_name ?? t('billing.freeTrial')}
              icon={<BadgeCheck size={18} />}
              trend={sub?.status ? `${t('billing.status')} · ${sub.status}` : t('billing.noCommercialPlan')}
            />
            <Kpi
              label={t('billing.kpi.requests')}
              value={formatNumber(usage?.requests)}
              icon={<Activity size={18} />}
              trend={usage?.month ? `${t('billing.asOf')} ${usage.month}` : t('billing.monthToDate')}
            />
            <Kpi
              label={t('billing.kpi.tokens')}
              value={formatNumber(usage?.tokens)}
              icon={<Zap size={18} />}
              trend={t('billing.inputOutput')}
            />
            <Kpi
              label={t('billing.kpi.credits')}
              value={formatNumber(usage?.credits_used)}
              icon={<Coins size={18} />}
              trend={
                sub?.seats
                  ? t('billing.perSeat').replace('%s', String(sub.seats))
                  : t('billing.accountBasis')
              }
            />
          </div>

          <div className="ops-grid">
            <Panel
              title={t('billing.compareTitle')}
              subtitle={t('billing.compareSubtitle')}
              actions={
                sub?.status && (
                  <Badge tone="info">
                    {sub.plan_name ?? sub.plan_code ?? sub.plan_id} · {sub.status}
                  </Badge>
                )
              }
            >
              {plans.length === 0 ? (
                <StateView
                  state="empty"
                  title={t('billing.noPlans')}
                  description={t('billing.noPlansDesc')}
                />
              ) : (
                <div className="resource-grid" data-testid="plans-grid">
                  {plans.map((plan) => {
                    const isCurrent = sub?.plan_id === plan.id
                    return (
                      <div
                        key={plan.id}
                        className={`resource-card ${isCurrent ? 'is-current' : ''}`}
                        data-testid={`billing-plan-${plan.id}`}
                      >
                        <div className="resource-icon blue">
                          <Layers size={18} />
                        </div>
                        <div className="resource-main">
                          <strong>{plan.name}</strong>
                          <p>
                            {(plan.price ?? 0) > 0 ? formatPrice(plan) : t('billing.free')}
                            {plan.seats ? ` · ${plan.seats} ${t('billing.seatUnit')}` : ''}
                          </p>
                          {plan.monthly_credits ? (
                            <span>{t('billing.monthlyCredits')} {formatNumber(plan.monthly_credits)}</span>
                          ) : (
                            <span>{t('billing.usageBased')}</span>
                          )}
                        </div>
                        <div className="panel-actions-inline">
                          {isCurrent && <Status value="current" />}
                          {plan.features?.slice(0, 2).map((feature) => (
                            <Badge key={feature} tone="neutral">
                              {feature}
                            </Badge>
                          ))}
                        </div>
                        <div className="knowledge-actions">
                          {isCurrent ? (
                            <span className="muted">{t('billing.currentSub')}</span>
                          ) : (
                            <Button
                              variant="primary"
                              data-testid={`billing-cta-${plan.id}`}
                              onClick={() => {
                                // 商业级切换动作需进入审批流：暂以 console 提示占位，避免前端伪造订阅状态
                                void api
                                  .post(`/api/v1/billing/plans/${encodeURIComponent(plan.id)}/quote`)
                                  .catch(() => {})
                              }}
                            >
                              {t('billing.viewQuote')}
                            </Button>
                          )}
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}
            </Panel>

            <div className="knowledge-column">
              <Panel title={t('billing.usageProgressTitle')} subtitle={t('billing.usageProgressSubtitle')}>
                <div className="usage-progress" data-testid="billing-usage-progress">
                  <div className="usage-progress-row">
                    <span>
                      <Gauge size={15} /> {t('billing.usedCredits')} {formatNumber(usage?.credits_used)} {t('billing.creditUnit')}
                    </span>
                    {usageProgress !== null ? (
                      <strong>{usageProgress}%</strong>
                    ) : (
                      <span className="muted">{t('billing.noMonthlyLimit')}</span>
                    )}
                  </div>
                  <div
                    className="usage-progress-bar"
                    role="progressbar"
                    aria-label={`${t('billing.usageProgressAria')} ${usageProgress ?? 0}%`}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={usageProgress ?? 0}
                  >
                    <span
                      style={{ width: `${usageProgress ?? 0}%` }}
                      className={
                        usageProgress !== null && usageProgress >= 90
                          ? 'usage-progress-fill danger'
                          : usageProgress !== null && usageProgress >= 70
                            ? 'usage-progress-fill warn'
                            : 'usage-progress-fill'
                      }
                    />
                  </div>
                  <ul className="usage-mini-grid">
                    <li>
                      <Activity size={14} />
                      <span>{t('billing.requestsLabel')} {formatNumber(usage?.requests)}</span>
                    </li>
                    <li>
                      <Zap size={14} />
                      <span>{t('billing.tokensLabel')} {formatNumber(usage?.tokens)}</span>
                    </li>
                    <li>
                      <Database size={14} />
                      <span>{t('billing.storageLabel')} {formatBytes(usage?.storage_mb)}</span>
                    </li>
                  </ul>
                </div>
              </Panel>

              <Panel
                title={t('billing.recentEventsTitle')}
                subtitle={t('billing.recentEventsSubtitle')}
                actions={
                  <Badge tone="info">
                    <CreditCard size={12} /> {t('billing.eventsCount').replace('%s', String(events.length))}
                  </Badge>
                }
              >
                {events.length === 0 ? (
                  <StateView
                    state="empty"
                    title={t('billing.noEvents')}
                    description={t('billing.noEventsDesc')}
                  />
                ) : (
                  <ul className="resource-list" data-testid="billing-events">
                    {events.slice(0, 8).map((event) => {
                      const tone = EVENT_TONE[event.type ?? ''] ?? 'badge-neutral'
                      return (
                        <li
                          key={event.id}
                          className="resource-item"
                          data-testid={`billing-event-${event.id}`}
                        >
                          <div className="resource-info">
                            <strong>
                              <Badge tone={tone.includes('info') ? 'info' : tone.includes('warning') ? 'warning' : tone.includes('success') ? 'success' : 'neutral'}>
                                {event.type ?? 'event'}
                              </Badge>
                              <span>{event.description ?? event.id}</span>
                            </strong>
                            <small>{formatTime(event.created_at)}</small>
                          </div>
                          {event.amount !== undefined && (
                            <span className="badge badge-neutral">
                              {event.amount >= 0 ? '+' : ''}
                              {event.amount} {event.currency ?? t('billing.creditUnit')}
                            </span>
                          )}
                        </li>
                      )
                    })}
                  </ul>
                )}
              </Panel>

              <Panel title={t('billing.subscriptionTitle')} subtitle={t('billing.subscriptionSubtitle')}>
                {sub ? (
                  <ul className="resource-list" data-testid="billing-subscription">
                    <li className="resource-item">
                      <div className="resource-info">
                        <strong>
                          <Sparkles size={14} /> {t('billing.plan')}
                        </strong>
                        <span>{sub.plan_name ?? sub.plan_code ?? sub.plan_id ?? '—'}</span>
                      </div>
                      {sub.status && <Status value={sub.status} />}
                    </li>
                    <li className="resource-item">
                      <div className="resource-info">
                        <strong>{t('billing.seats')}</strong>
                        <span>{sub.seats ?? '—'}</span>
                      </div>
                    </li>
                    <li className="resource-item">
                      <div className="resource-info">
                        <strong>{t('billing.effectiveTime')}</strong>
                        <span>{formatTime(sub.started_at)}</span>
                      </div>
                    </li>
                    <li className="resource-item">
                      <div className="resource-info">
                        <strong>{t('billing.renewalTime')}</strong>
                        <span>{formatTime(sub.renew_at)}</span>
                      </div>
                    </li>
                  </ul>
                ) : (
                  <div className="empty-action">
                    <AlertCircle size={14} aria-hidden="true" />
                    {t('billing.notSubscribed')}
                  </div>
                )}
              </Panel>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
