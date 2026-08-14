/**
 * 设备管理：概览指标 + 设备列表 + 创建 + 心跳状态。
 * 对应《400-身份权限隐私与企业治理详细设计》FR-X-08（设备/凭据管理）。
 */
import { useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
import {
  Activity,
  Cpu,
  KeyRound,
  Laptop,
  Plus,
  ShieldCheck,
  Smartphone,
  Watch,
} from 'lucide-react'
import { AdminPageShell, DeleteButton, useResource } from './admin-shared'
import { Button, Field, Kpi, Panel, SearchBox, StateView, Status } from './ui'
import { useLocale } from './locale'

type Device = {
  id: string
  name: string
  device_type?: string
  status?: string
  last_heartbeat?: string
  ip?: string
  platform?: string
}

const DEVICE_TYPE_PRESETS = ['desktop', 'mobile', 'tablet', 'cli', 'browser']

/** 时间戳格式化：相对时间，缺省或非法回退占位文本。 */
function formatHeartbeat(value?: string): string {
  if (!value) return '尚无心跳'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return `心跳 ${value}`
  return `心跳 ${parsed.toLocaleString('zh-CN', { hour12: false })}`
}

/** 设备类型 → 图标。 */
function deviceIcon(type?: string): ReactNode {
  const t = (type ?? '').toLowerCase()
  if (t.includes('mobile') || t.includes('phone')) return <Smartphone size={18} />
  if (t.includes('tablet')) return <Watch size={18} />
  if (t.includes('browser') || t.includes('extension')) return <ShieldCheck size={18} />
  if (t.includes('cli')) return <Cpu size={18} />
  return <Laptop size={18} />
}

/** 设备类型 → 资源色调。 */
function deviceTone(type?: string): string {
  const t = (type ?? '').toLowerCase()
  if (t.includes('mobile')) return 'green'
  if (t.includes('cli')) return 'purple'
  if (t.includes('browser')) return 'amber'
  return 'blue'
}

/** 心跳新鲜度推断：缺省=未知、<5min=健康、<1h=告警、其它=失联。 */
function heartbeatTone(value?: string): 'success' | 'warning' | 'danger' | 'unknown' {
  if (!value) return 'unknown'
  const parsed = new Date(value).getTime()
  if (Number.isNaN(parsed)) return 'unknown'
  const ageMs = Date.now() - parsed
  if (ageMs < 5 * 60 * 1000) return 'success'
  if (ageMs < 60 * 60 * 1000) return 'warning'
  return 'danger'
}

export default function AdminDevicesPage(): ReactNode {
  const { items, loading, error, reload, create, remove, busy } = useResource<Device>(
    '/api/v1/devices',
  )
  const { t } = useLocale()
  const [query, setQuery] = useState('')
  const [name, setName] = useState('')
  const [deviceType, setDeviceType] = useState('')
  const [formError, setFormError] = useState('')
  const nameRef = useRef<HTMLInputElement>(null)

  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase()
    if (!keyword) return items
    return items.filter((item) =>
      `${item.name} ${item.device_type ?? ''} ${item.platform ?? ''} ${item.ip ?? ''}`
        .toLowerCase()
        .includes(keyword),
    )
  }, [items, query])

  const stats = useMemo(() => {
    const healthy = items.filter((item) => heartbeatTone(item.last_heartbeat) === 'success').length
    const types = new Set(items.map((item) => (item.device_type ?? 'unknown').toLowerCase()))
    return { healthy, types: types.size }
  }, [items])

  function focusCreate() {
    nameRef.current?.focus()
    nameRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'center' })
  }

  async function submitCreate(event: FormEvent) {
    event.preventDefault()
    setFormError('')
    try {
      await create({ name, device_type: deviceType })
      setName('')
      setDeviceType('')
    } catch (caught) {
      setFormError(caught instanceof Error ? caught.message : t('common.createFailed'))
    }
  }

  return (
    <AdminPageShell
      title={t('admin.devices.title')}
      subtitle={t('admin.devices.subtitle')}
      testId="devices-page"
      loading={loading}
      error={error}
      onRetry={reload}
      actions={
        <Button variant="primary" icon={<Plus size={15} />} onClick={focusCreate} data-testid="devices-new">
          {t('admin.devices.new')}
        </Button>
      }
    >
      <div className="kpi-grid">
        <Kpi
          label={t('admin.devices.kpi.total')}
          value={String(items.length).padStart(2, '0')}
          icon={<Laptop size={18} />}
          trend={t('admin.devices.kpi.total.trend')}
        />
        <Kpi
          label={t('admin.devices.kpi.healthy')}
          value={String(stats.healthy).padStart(2, '0')}
          icon={<Activity size={18} />}
          trend={t('admin.devices.kpi.healthy.trend')}
        />
        <Kpi
          label={t('admin.devices.kpi.types')}
          value={String(stats.types).padStart(2, '0')}
          icon={<Cpu size={18} />}
          trend={t('admin.devices.kpi.types.trend')}
        />
        <Kpi
          label={t('admin.devices.kpi.passkey')}
          value={String(items.length).padStart(2, '0')}
          icon={<KeyRound size={18} />}
          trend={t('admin.devices.kpi.passkey.trend')}
        />
      </div>

      <div className="ops-grid">
        <Panel
          title={t('admin.devices.list.title')}
          subtitle={t('admin.devices.list.subtitle')}
          actions={<SearchBox value={query} onChange={setQuery} placeholder={t('admin.devices.search')} />}
        >
          {items.length === 0 ? (
            <StateView
              state="empty"
              title={t('admin.devices.empty.title')}
              description={t('admin.devices.empty.desc')}
            />
          ) : filtered.length === 0 ? (
            <StateView
              state="empty"
              title={t('admin.devices.emptyFiltered.title')}
              description={t('admin.devices.emptyFiltered.desc')}
            />
          ) : (
            <div className="resource-grid" data-testid="devices-list">
              {filtered.map((item) => {
                const tone = heartbeatTone(item.last_heartbeat)
                return (
                  <div key={item.id} className="resource-card" data-testid="devices-item">
                    <div className={`resource-icon ${deviceTone(item.device_type)}`}>
                      {deviceIcon(item.device_type)}
                    </div>
                    <div className="resource-main">
                      <strong>{item.name}</strong>
                      <p>{formatHeartbeat(item.last_heartbeat)}</p>
                      <span>
                        ID {item.id}
                        {item.platform ? ` · ${item.platform}` : ''}
                        {item.ip ? ` · ${item.ip}` : ''}
                      </span>
                    </div>
                    <div className="panel-actions-inline">
                      <span className="badge badge-neutral">{item.device_type ?? 'unknown'}</span>
                      {item.status && <Status value={item.status} />}
                      <Status
                        value={
                          tone === 'success'
                            ? 'online'
                            : tone === 'warning'
                              ? 'stale'
                              : tone === 'danger'
                                ? 'offline'
                                : 'unknown'
                        }
                      />
                    </div>
                    <div className="knowledge-actions">
                      <DeleteButton
                        testId={`devices-delete-${item.id}`}
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

        <Panel
          title={t('admin.devices.create.title')}
          subtitle={t('admin.devices.create.subtitle')}
        >
          <form className="form-stack" data-testid="devices-create" onSubmit={submitCreate}>
            <Field
              label={t('admin.devices.field.name')}
              hint={t('admin.devices.field.name.hint')}
            >
              <input
                ref={nameRef}
                name="name"
                value={name}
                placeholder={t('admin.devices.field.name.placeholder')}
                onChange={(event) => setName(event.target.value)}
                data-testid="devices-create-name"
              />
            </Field>
            <Field
              label={t('admin.devices.field.type')}
              hint={t('admin.devices.field.type.hint')}
            >
              <input
                name="device_type"
                list="device-type-presets"
                value={deviceType}
                placeholder={t('admin.devices.field.type.placeholder')}
                onChange={(event) => setDeviceType(event.target.value)}
                data-testid="devices-create-type"
              />
            </Field>
            <datalist id="device-type-presets">
              {DEVICE_TYPE_PRESETS.map((preset) => (
                <option key={preset} value={preset} />
              ))}
            </datalist>
            {formError && (
              <div className="alert alert-error" role="alert">
                {formError}
              </div>
            )}
            <Button type="submit" variant="primary" loading={busy} icon={<Plus size={15} />}>
              {t('common.create')}
            </Button>
            <div className="callout">
              <ShieldCheck size={14} aria-hidden="true" />
              <span>{t('admin.devices.callout')}</span>
            </div>
          </form>
        </Panel>
      </div>
    </AdminPageShell>
  )
}