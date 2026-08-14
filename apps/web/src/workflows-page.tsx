/**
 * 工作流管理：概览指标 + 定义列表 + 创建（最小 CRUD）。
 */
import { useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
import {
  Activity,
  ListChecks,
  Play,
  Plus,
  Timer,
  Webhook,
  Workflow as WorkflowIcon,
  Zap,
} from 'lucide-react'
import { AdminPageShell, DeleteButton, useResource } from './admin-shared'
import { Button, Field, Kpi, Panel, SearchBox, StateView, Status } from './ui'
import { useLocale } from './locale'

type Workflow = {
  id: string
  name: string
  status?: string
  trigger?: string
  updated_at?: string
  last_run_at?: string
}

const TRIGGER_PRESETS = ['manual', 'schedule', 'webhook', 'event']

/** 触发方式 -> 图标，未知触发方式回退为通用闪电图标。 */
function triggerIcon(trigger: string): ReactNode {
  const normalized = trigger.toLowerCase()
  if (normalized.includes('schedule') || normalized.includes('cron')) return <Timer size={18} />
  if (normalized.includes('webhook') || normalized.includes('http')) return <Webhook size={18} />
  if (normalized.includes('manual')) return <Play size={18} />
  return <Zap size={18} />
}

/** 时间戳格式化，缺省时返回占位文案而不是伪造数据。 */
function formatLastRun(value?: string, missing = '尚未运行'): string {
  if (!value) return missing
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return `最近运行 ${value}`
  return `最近运行 ${parsed.toLocaleString()}`
}

export default function AdminWorkflowsPage(): ReactNode {
  const { items, loading, error, reload, create, remove, busy } = useResource<Workflow>(
    '/api/v1/workflows',
  )
  const [query, setQuery] = useState('')
  const [name, setName] = useState('')
  const [trigger, setTrigger] = useState('')
  const [formError, setFormError] = useState('')
  const nameRef = useRef<HTMLInputElement>(null)
  const { t } = useLocale()

  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase()
    if (!keyword) return items
    return items.filter((item) =>
      `${item.name} ${item.trigger ?? ''} ${item.status ?? ''}`.toLowerCase().includes(keyword),
    )
  }, [items, query])

  const stats = useMemo(() => {
    const active = items.filter((item) =>
      ['active', 'enabled', 'running'].includes(String(item.status ?? '').toLowerCase()),
    ).length
    const automated = items.filter((item) => {
      const value = String(item.trigger ?? 'manual').toLowerCase()
      return value !== 'manual' && value !== ''
    }).length
    const triggers = new Set(items.map((item) => String(item.trigger ?? 'manual').toLowerCase()))
    return { active, automated, triggerKinds: triggers.size }
  }, [items])

  function focusCreate() {
    nameRef.current?.focus()
    nameRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'center' })
  }

  async function submitCreate(event: FormEvent) {
    event.preventDefault()
    setFormError('')
    try {
      await create({ name, trigger })
      setName('')
      setTrigger('')
    } catch (caught) {
      setFormError(caught instanceof Error ? caught.message : t('common.createFailed'))
    }
  }

  return (
    <AdminPageShell
      title={t('admin.workflows.title')}
      subtitle={t('admin.workflows.subtitle')}
      testId="workflows-page"
      loading={loading}
      error={error}
      onRetry={reload}
      actions={
        <Button variant="primary" icon={<Plus size={15} />} onClick={focusCreate}>
          {t('admin.workflows.new')}
        </Button>
      }
    >
      <div className="kpi-grid">
        <Kpi
          label={t('admin.workflows.kpi.total')}
          value={String(items.length).padStart(2, '0')}
          icon={<WorkflowIcon size={18} />}
          trend={t('admin.workflows.kpi.total.trend')}
        />
        <Kpi
          label={t('admin.workflows.kpi.active')}
          value={String(stats.active).padStart(2, '0')}
          icon={<Activity size={18} />}
          trend={t('admin.workflows.kpi.active.trend')}
        />
        <Kpi
          label={t('admin.workflows.kpi.automated')}
          value={String(stats.automated).padStart(2, '0')}
          icon={<Zap size={18} />}
          trend={t('admin.workflows.kpi.automated.trend')}
        />
        <Kpi
          label={t('admin.workflows.kpi.triggers')}
          value={String(stats.triggerKinds).padStart(2, '0')}
          icon={<ListChecks size={18} />}
          trend={t('admin.workflows.kpi.triggers.trend')}
        />
      </div>

      <div className="ops-grid">
        <Panel
          title={t('admin.workflows.list.title')}
          subtitle={t('admin.workflows.list.subtitle')}
          actions={<SearchBox value={query} onChange={setQuery} placeholder={t('admin.workflows.search')} />}
        >
          {items.length === 0 ? (
            <StateView
              state="empty"
              title={t('admin.workflows.empty.title')}
              description={t('admin.workflows.empty.desc')}
            />
          ) : filtered.length === 0 ? (
            <StateView
              state="empty"
              title={t('admin.workflows.emptyFiltered.title')}
              description={t('admin.workflows.emptyFiltered.desc')}
            />
          ) : (
            <div className="resource-grid" data-testid="workflows-list">
              {filtered.map((item) => {
                const itemTrigger = item.trigger ?? 'manual'
                return (
                  <div key={item.id} className="resource-card" data-testid="workflows-item">
                    <div className="resource-icon">{triggerIcon(itemTrigger)}</div>
                    <div className="resource-main">
                      <strong>{item.name}</strong>
                      <p>{formatLastRun(item.last_run_at, t('admin.workflows.neverRun'))}</p>
                      <span>ID {item.id}</span>
                    </div>
                    <div className="panel-actions-inline">
                      <span className="badge badge-neutral">{itemTrigger}</span>
                      {item.status && <Status value={item.status} />}
                    </div>
                    <div className="knowledge-actions">
                      <DeleteButton
                        testId={`workflows-delete-${item.id}`}
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

        <Panel title="新建工作流" subtitle="定义名称与触发方式，创建后可继续编排节点">
          <form className="form-stack" data-testid="workflows-create" onSubmit={submitCreate}>
            <Field label="名称" hint="在编排、审计与运行记录中标识该工作流">
              <input
                ref={nameRef}
                name="name"
                value={name}
                placeholder="例如：合同审阅流水线"
                onChange={(event) => setName(event.target.value)}
                data-testid="workflows-create-name"
              />
            </Field>
            <Field label="触发方式" hint="manual 手动触发；schedule 定时；webhook 外部回调">
              <input
                name="trigger"
                list="workflow-trigger-presets"
                value={trigger}
                placeholder="manual"
                onChange={(event) => setTrigger(event.target.value)}
                data-testid="workflows-create-trigger"
              />
            </Field>
            <datalist id="workflow-trigger-presets">
              {TRIGGER_PRESETS.map((preset) => (
                <option key={preset} value={preset} />
              ))}
            </datalist>
            {formError && (
              <div className="alert alert-error" role="alert">
                {formError}
              </div>
            )}
            <Button type="submit" variant="primary" loading={busy} icon={<Plus size={15} />}>
              创建
            </Button>
          </form>
        </Panel>
      </div>
    </AdminPageShell>
  )
}
