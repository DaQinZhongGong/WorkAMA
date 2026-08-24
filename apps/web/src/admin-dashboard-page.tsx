/**
 * Admin 仪表盘：显示各模块统计（工作区数、助手数、知识库数、设备数、未读通知数、当前套餐），
 * 并提供「快速上手」引导与近期的会话活动，形成更接近商业级产品的概览页。
 */
import { useEffect, useState, type ReactNode } from 'react'
import {
  RefreshCw,
  LayoutDashboard,
  Bot,
  BookOpen,
  KeyRound,
  Bell,
  Sparkles,
  CheckCircle2,
  ArrowRight,
  MessagesSquare,
} from 'lucide-react'
import { api, errorMessage } from './api'
import { type MessageKey } from '@workama/i18n'
import { useLocale } from './locale'
import { Button, StateView } from './ui'

type DashboardStats = {
  workspaces?: number
  assistants?: number
  knowledge_bases?: number
  devices?: number
  unread_notifications?: number
  current_plan?: string
}

type SessionItem = {
  id: string
  title: string
  model: string
  status: string
  updated_at?: string
}

const STAT_CARDS: { key: keyof DashboardStats; labelKey: MessageKey; testId: string; icon: typeof LayoutDashboard; trendKey: MessageKey }[] = [
  { key: 'workspaces', labelKey: 'admin.dashboard.stat.workspaces', testId: 'stat-workspaces', icon: LayoutDashboard, trendKey: 'admin.dashboard.trend.workspaces' },
  { key: 'assistants', labelKey: 'admin.dashboard.stat.assistants', testId: 'stat-assistants', icon: Bot, trendKey: 'admin.dashboard.trend.assistants' },
  { key: 'knowledge_bases', labelKey: 'admin.dashboard.stat.knowledge', testId: 'stat-knowledge-bases', icon: BookOpen, trendKey: 'admin.dashboard.trend.knowledge' },
  { key: 'devices', labelKey: 'admin.dashboard.stat.devices', testId: 'stat-devices', icon: KeyRound, trendKey: 'admin.dashboard.trend.devices' },
  { key: 'unread_notifications', labelKey: 'admin.dashboard.stat.notifications', testId: 'stat-notifications', icon: Bell, trendKey: 'admin.dashboard.trend.notifications' },
  { key: 'current_plan', labelKey: 'admin.dashboard.stat.plan', testId: 'stat-plan', icon: Sparkles, trendKey: 'admin.dashboard.trend.plan' },
]

const GETTING_STARTED: { titleKey: MessageKey; descKey: MessageKey; to: string; done: boolean }[] = [
  { titleKey: 'admin.dashboard.gs.createAssistant.title', descKey: 'admin.dashboard.gs.createAssistant.desc', to: '/agents', done: true },
  { titleKey: 'admin.dashboard.gs.connectChannels.title', descKey: 'admin.dashboard.gs.connectChannels.desc', to: '/gateway/channels', done: true },
  { titleKey: 'admin.dashboard.gs.buildKb.title', descKey: 'admin.dashboard.gs.buildKb.desc', to: '/knowledge', done: false },
  { titleKey: 'admin.dashboard.gs.inviteMembers.title', descKey: 'admin.dashboard.gs.inviteMembers.desc', to: '/admin/members', done: false },
]

export default function AdminDashboardPage(): ReactNode {
  const { t } = useLocale()
  const [stats, setStats] = useState<DashboardStats | null>(null)
  const [sessions, setSessions] = useState<SessionItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  function reload() {
    setLoading(true)
    setError('')
    void Promise.allSettled([
      api.get<DashboardStats>('/api/v1/admin/stats'),
      api.get<{ items: SessionItem[] }>('/api/v1/sessions?limit=5'),
    ])
      .then(([statsRes, sessionsRes]) => {
        if (statsRes.status === 'fulfilled') setStats(statsRes.value)
        if (sessionsRes.status === 'fulfilled') setSessions(sessionsRes.value.items ?? [])
      })
      .catch((caught) => setError(errorMessage(caught, t('common.loadFailed'))))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    reload()
  }, [])

  return (
    <div data-testid="admin-dashboard-page">
      <header className="page-header">
        <div>
          <div className="eyebrow">{t('admin.dashboard.eyebrow')}</div>
          <h1>{t('admin.dashboard.title')}</h1>
          <p>{t('admin.dashboard.subtitle')}</p>
        </div>
        <div className="page-actions">
          <Button icon={<RefreshCw size={15} />} onClick={reload} disabled={loading}>
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
          <div className="stats-grid" data-testid="dashboard-stats">
            {STAT_CARDS.map((card) => {
              const Icon = card.icon
              return (
                <div key={card.key} className="stat-card" data-testid={card.testId}>
                  <span className="stat-card-label" style={{ display: 'inline-flex', alignItems: 'center', gap: 7 }}>
                    <Icon size={15} color="var(--wama-accent)" /> {t(card.labelKey)}
                  </span>
                  <strong className="stat-card-value">{String(stats?.[card.key] ?? '—')}</strong>
                  <small style={{ color: 'var(--wama-muted-2)', fontSize: 11.5 }}>{t(card.trendKey)}</small>
                </div>
              )
            })}
          </div>

          <div className="dash-grid">
            <section className="dash-section">
              <h2>
                <Sparkles size={16} className="section-icon" /> {t('admin.dashboard.gettingStarted')}
              </h2>
              <ul className="getting-list">
                {GETTING_STARTED.map((step) => (
                  <li key={step.titleKey}>
                    <span className={`step-dot ${step.done ? 'done' : ''}`}>
                      {step.done ? <CheckCircle2 size={13} /> : '•'}
                    </span>
                    <span className="step-body">
                      <strong>{t(step.titleKey)}</strong>
                      <small>{t(step.descKey)}</small>
                    </span>
                    <a className="topbar-link" href={step.to}>
                      {t('common.goTo')} <ArrowRight size={14} />
                    </a>
                  </li>
                ))}
              </ul>
            </section>

            <section className="dash-section">
              <h2>
                <MessagesSquare size={16} className="section-icon" /> {t('admin.dashboard.recentSessions')}
              </h2>
              {sessions.length === 0 ? (
                <div className="empty-action">{t('admin.dashboard.noSessions')}</div>
              ) : (
                <ul className="activity-list">
                  {sessions.map((s) => (
                    <li key={s.id}>
                      <span className="activity-dot" />
                      <span className="activity-body">
                        <strong>{s.title || t('admin.dashboard.unnamedSession')}</strong>
                        <small>
                          {s.model} · {s.status === 'idle' ? t('admin.dashboard.idle') : s.status}
                        </small>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        </>
      )}
    </div>
  )
}
