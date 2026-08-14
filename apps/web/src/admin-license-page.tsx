/**
 * G4 企业许可与功能开关控制台（/admin/license）。
 *
 * 全部数据来自真实后端，没有任何本地伪造或硬编码兜底：
 *  - GET  /api/v1/enterprise/compliance/licenses/current        当前生效许可证（无许可证时返回 status=missing）
 *  - GET  /api/v1/enterprise/compliance/licenses                组织+工作区维度的全部许可证记录（需 admin/owner）
 *  - GET  /api/v1/enterprise/compliance/entitlements            许可证明细 + 许可状态 + SLA + 区域策略
 *  - GET  /api/v1/enterprise/features                           已授权功能 + 平台声明的全部企业功能及描述
 *  - GET  /api/v1/enterprise/version                            平台版本 / 企业能力开关 / 构建日期
 *  - POST /api/v1/enterprise/compliance/licenses/{id}/renew     续期（extend_days 1..365）
 *  - POST /api/v1/enterprise/compliance/licenses                签发新许可证
 *
 * 功能矩阵的“已授权/未授权”以 /enterprise/features 的 licensed 列表为准，
 * 该列表由服务端只读取处于 active 且未过期的许可证计算得出；
 * 前端只负责把判定依据（显式授权 / 通配 / 不在套餐内 / 无许可证 / 已过期）解释给人看，
 * 不自行改写授权结论。
 */
import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { KeyRound, Plus, RefreshCw, RotateCw, ShieldCheck } from 'lucide-react'
import { api } from './api'
import { useLocale } from './locale'
import {
  JsonDetails,
  MetaList,
  Notice,
  StateGate,
  displayDate,
  errorText,
  shortId,
} from './admin-ops-shared'
import { Badge, Button, DataTable, Field, Kpi, Modal, PageHeader, Panel, Status } from './ui'

const CURRENT_ENDPOINT = '/api/v1/enterprise/compliance/licenses/current'
const LICENSES_ENDPOINT = '/api/v1/enterprise/compliance/licenses'
const ENTITLEMENTS_ENDPOINT = '/api/v1/enterprise/compliance/entitlements'
const FEATURES_ENDPOINT = '/api/v1/enterprise/features'
const VERSION_ENDPOINT = '/api/v1/enterprise/version'

type CurrentLicense = {
  license_id?: string | null
  status?: string
  valid_until?: string | null
  days_remaining?: number | null
  features?: Record<string, unknown>
  plan_code?: string | null
}

type FeatureDescriptor = { name: string; description?: string }

type FeatureCatalog = {
  licensed?: string[]
  available?: FeatureDescriptor[]
  plan_code?: string | null
}

type LicenseRow = {
  id: string
  plan_code?: string
  status?: string
  seats?: number | null
  credit_limit?: number | null
  concurrency_limit?: number | null
  features?: Record<string, unknown>
  license_key_last_four?: string | null
  issued_by?: string | null
  valid_from?: string | null
  valid_until?: string | null
  revoked_at?: string | null
  revoke_reason?: string | null
  created_at?: string | null
}

type Entitlements = {
  license?: LicenseRow | null
  license_state?: string | null
  sla?: unknown
  region_policy?: unknown
  external_provider_exchange?: string | null
}

type VersionInfo = {
  platform_version?: string
  enterprise_enabled?: boolean
  build_date?: string
  features?: string[]
}

type IssueResponse = LicenseRow & { license_key?: string; replayed?: boolean }

type Entitlement = { enabled: boolean; reason: string }

function countGranted(features: Record<string, unknown> | undefined | null): number {
  if (!features) return 0
  return Object.values(features).filter((value) => value === true).length
}

/** datetime-local 的值没有时区，交给浏览器按本地时区转成后端可解析的 ISO 串。 */
function toIsoOrNull(value: string): string | null {
  if (!value) return null
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString()
}

export default function AdminLicensePage() {
  const { t } = useLocale()

  const [current, setCurrent] = useState<CurrentLicense | null>(null)
  const [catalog, setCatalog] = useState<FeatureCatalog | null>(null)
  const [entitlements, setEntitlements] = useState<Entitlements | null>(null)
  const [version, setVersion] = useState<VersionInfo | null>(null)
  const [licenses, setLicenses] = useState<LicenseRow[]>([])

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)

  const [renewTarget, setRenewTarget] = useState<LicenseRow | null>(null)
  const [extendDays, setExtendDays] = useState('30')
  const [issueOpen, setIssueOpen] = useState(false)
  const [issuePlan, setIssuePlan] = useState('enterprise')
  const [issueSeats, setIssueSeats] = useState('50')
  const [issueValidUntil, setIssueValidUntil] = useState('')
  const [issueFeatures, setIssueFeatures] = useState<string[]>([])
  const [issuedKey, setIssuedKey] = useState('')

  const reload = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [currentRow, featureRow, entitlementRow, versionRow, listRow] = await Promise.all([
        api.get<CurrentLicense>(CURRENT_ENDPOINT),
        api.get<FeatureCatalog>(FEATURES_ENDPOINT),
        api.get<Entitlements>(ENTITLEMENTS_ENDPOINT),
        api.get<VersionInfo>(VERSION_ENDPOINT),
        api.get<{ items?: LicenseRow[] }>(LICENSES_ENDPOINT),
      ])
      setCurrent(currentRow)
      setCatalog(featureRow)
      setEntitlements(entitlementRow)
      setVersion(versionRow)
      setLicenses(Array.isArray(listRow?.items) ? listRow.items : [])
    } catch (caught) {
      setError(errorText(caught, t))
    } finally {
      setLoading(false)
    }
  }, [t])

  useEffect(() => {
    void reload()
  }, [reload])

  const licenseRow = entitlements?.license ?? null
  const hasLicense = Boolean(current?.license_id) && current?.status !== 'missing'
  const daysRemaining = typeof current?.days_remaining === 'number' ? current.days_remaining : null
  const expired = hasLicense && (current?.status !== 'active' || (daysRemaining ?? 0) <= 0)

  const entitlementFor = useMemo(() => {
    const licensedSet = new Set(catalog?.licensed ?? [])
    const featureMap = (licenseRow?.features ?? current?.features ?? {}) as Record<string, unknown>
    const wildcard = licensedSet.has('*') || featureMap['*'] === true
    return (name: string): Entitlement => {
      if (licensedSet.has(name)) return { enabled: true, reason: t('licenseConsole.reasonGranted') }
      if (wildcard) return { enabled: true, reason: t('licenseConsole.reasonUnlimited') }
      if (!hasLicense) return { enabled: false, reason: t('licenseConsole.reasonNoLicense') }
      if (expired) return { enabled: false, reason: t('licenseConsole.reasonExpired') }
      return { enabled: false, reason: t('licenseConsole.reasonNotInPlan') }
    }
  }, [catalog, licenseRow, current, hasLicense, expired, t])

  const availableFeatures = catalog?.available ?? []

  const openRenew = (row: LicenseRow) => {
    setRenewTarget(row)
    setExtendDays('30')
  }

  const submitRenew = async (event: FormEvent) => {
    event.preventDefault()
    if (!renewTarget) return
    setBusy(true)
    setError('')
    try {
      await api.post(`${LICENSES_ENDPOINT}/${renewTarget.id}/renew`, {
        extend_days: Number(extendDays),
      })
      setRenewTarget(null)
      setNotice(t('licenseConsole.renewedNotice'))
      await reload()
    } catch (caught) {
      setError(errorText(caught, t))
    } finally {
      setBusy(false)
    }
  }

  const toggleIssueFeature = (name: string, checked: boolean) => {
    setIssueFeatures((previous) =>
      checked ? [...previous, name] : previous.filter((item) => item !== name),
    )
  }

  const submitIssue = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const features: Record<string, boolean> = {}
      for (const name of issueFeatures) features[name] = true
      const validUntil = toIsoOrNull(issueValidUntil)
      const created = await api.post<IssueResponse>(LICENSES_ENDPOINT, {
        plan_code: issuePlan,
        seats: Number(issueSeats),
        features,
        ...(validUntil ? { valid_until: validUntil } : {}),
      })
      setIssueOpen(false)
      setIssuedKey(String(created?.license_key ?? ''))
      setNotice(t('licenseConsole.issuedNotice'))
      await reload()
    } catch (caught) {
      setError(errorText(caught, t))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <PageHeader
        eyebrow={t('licenseConsole.eyebrow')}
        title={t('page.license')}
        description={t('licenseConsole.description')}
        actions={
          <>
            <Button icon={<RefreshCw size={15} />} onClick={() => void reload()} data-testid="license-refresh">
              {t('licenseConsole.refresh')}
            </Button>
            <Button
              icon={<Plus size={15} />}
              variant="primary"
              onClick={() => setIssueOpen(true)}
              data-testid="license-issue-open"
            >
              {t('licenseConsole.issue')}
            </Button>
          </>
        }
      />
      <Notice notice={notice} clear={() => setNotice('')} />

      <StateGate loading={loading} error={error} retry={reload}>
        <>
          <div className="kpi-grid">
            <Kpi
              label={t('licenseConsole.kpiPlan')}
              value={current?.plan_code ? String(current.plan_code) : '—'}
              trend={t('licenseConsole.kpiPlanTrend')}
              icon={<KeyRound size={16} />}
            />
            <Kpi
              label={t('licenseConsole.kpiStatus')}
              value={current?.status ? String(current.status) : '—'}
              trend={t('licenseConsole.kpiStatusTrend')}
              icon={<ShieldCheck size={16} />}
            />
            <Kpi
              label={t('licenseConsole.kpiSeats')}
              value={licenseRow?.seats === null || licenseRow?.seats === undefined ? '—' : String(licenseRow.seats)}
              trend={t('licenseConsole.kpiSeatsTrend')}
            />
            <Kpi
              label={t('licenseConsole.kpiDaysRemaining')}
              value={daysRemaining === null ? '—' : String(daysRemaining)}
              trend={t('licenseConsole.kpiDaysTrend')}
            />
          </div>

          <Panel title={t('licenseConsole.currentTitle')} subtitle={t('licenseConsole.currentSubtitle')}>
            {hasLicense ? (
              <MetaList
                rows={[
                  { label: t('licenseConsole.licenseId'), value: <code>{String(current?.license_id ?? '—')}</code> },
                  { label: t('licenseConsole.planCode'), value: String(current?.plan_code ?? '—') },
                  { label: t('licenseConsole.statusLabel'), value: <Status value={String(current?.status ?? '')} /> },
                  { label: t('licenseConsole.validFrom'), value: displayDate(licenseRow?.valid_from) },
                  { label: t('licenseConsole.validUntil'), value: displayDate(current?.valid_until) },
                  {
                    label: t('licenseConsole.seats'),
                    value: licenseRow?.seats === null || licenseRow?.seats === undefined ? '—' : String(licenseRow.seats),
                  },
                  {
                    label: t('licenseConsole.creditLimit'),
                    value:
                      licenseRow?.credit_limit === null || licenseRow?.credit_limit === undefined
                        ? t('licenseConsole.notSet')
                        : String(licenseRow.credit_limit),
                  },
                  {
                    label: t('licenseConsole.concurrencyLimit'),
                    value:
                      licenseRow?.concurrency_limit === null || licenseRow?.concurrency_limit === undefined
                        ? t('licenseConsole.notSet')
                        : String(licenseRow.concurrency_limit),
                  },
                  {
                    label: t('licenseConsole.keyLastFour'),
                    value: <code>{String(licenseRow?.license_key_last_four ?? '—')}</code>,
                  },
                  { label: t('licenseConsole.issuedBy'), value: <code>{shortId(licenseRow?.issued_by)}</code> },
                  {
                    label: t('licenseConsole.featureCount'),
                    value: String(countGranted(licenseRow?.features ?? current?.features)),
                  },
                ]}
              />
            ) : (
              <div className="alert">
                <strong>{t('licenseConsole.noLicense')}</strong>
                <p>{t('licenseConsole.noLicenseHint')}</p>
              </div>
            )}
          </Panel>

          <Panel title={t('licenseConsole.matrixTitle')} subtitle={t('licenseConsole.matrixSubtitle')}>
            <DataTable
              headers={[
                t('licenseConsole.colFeature'),
                t('licenseConsole.colDescription'),
                t('licenseConsole.colState'),
                t('licenseConsole.colReason'),
              ]}
            >
              {availableFeatures.map((feature) => {
                const state = entitlementFor(feature.name)
                return (
                  <tr key={feature.name} data-testid={`license-feature-${feature.name}`}>
                    <td>
                      <code>{feature.name}</code>
                    </td>
                    <td>{feature.description ?? '—'}</td>
                    <td>
                      <Badge tone={state.enabled ? 'success' : 'neutral'}>
                        {state.enabled ? t('licenseConsole.enabled') : t('licenseConsole.blocked')}
                      </Badge>
                    </td>
                    <td>
                      <small className="table-subtext">{state.reason}</small>
                    </td>
                  </tr>
                )
              })}
            </DataTable>
            <p className="admin-note">{t('licenseConsole.enforcementNote')}</p>
          </Panel>

          <Panel title={t('licenseConsole.licensesTitle')} subtitle={t('licenseConsole.licensesSubtitle')}>
            <DataTable
              headers={[
                t('licenseConsole.colLicense'),
                t('licenseConsole.colPlan'),
                t('licenseConsole.statusLabel'),
                t('licenseConsole.colSeats'),
                t('licenseConsole.colValidity'),
                t('licenseConsole.colFeatures'),
                t('licenseConsole.colState'),
              ]}
            >
              {licenses.map((row) => (
                <tr key={row.id} data-testid={`license-row-${row.id}`}>
                  <td>
                    <code>{shortId(row.id)}</code>
                    <small className="table-subtext">····{String(row.license_key_last_four ?? '')}</small>
                  </td>
                  <td>{String(row.plan_code ?? '—')}</td>
                  <td>
                    <Status value={String(row.status ?? '')} />
                  </td>
                  <td>{row.seats === null || row.seats === undefined ? '—' : String(row.seats)}</td>
                  <td>
                    {displayDate(row.valid_from)}
                    <small className="table-subtext">→ {displayDate(row.valid_until)}</small>
                  </td>
                  <td>{String(countGranted(row.features))}</td>
                  <td>
                    <Button
                      icon={<RotateCw size={14} />}
                      onClick={() => openRenew(row)}
                      data-testid={`license-renew-${row.id}`}
                    >
                      {t('licenseConsole.renew')}
                    </Button>
                  </td>
                </tr>
              ))}
            </DataTable>
          </Panel>

          <Panel title={t('licenseConsole.entitlementsTitle')} subtitle={t('licenseConsole.entitlementsSubtitle')}>
            <MetaList
              rows={[
                { label: t('licenseConsole.licenseState'), value: String(entitlements?.license_state ?? '—') },
                {
                  label: t('licenseConsole.sla'),
                  value: entitlements?.sla ? JSON.stringify(entitlements.sla) : t('licenseConsole.notSet'),
                },
                {
                  label: t('licenseConsole.regionPolicy'),
                  value: entitlements?.region_policy
                    ? JSON.stringify(entitlements.region_policy)
                    : t('licenseConsole.notSet'),
                },
                {
                  label: t('licenseConsole.externalExchange'),
                  value: String(entitlements?.external_provider_exchange ?? t('licenseConsole.notSet')),
                },
                { label: t('licenseConsole.platformVersion'), value: String(version?.platform_version ?? '—') },
                {
                  label: t('licenseConsole.enterpriseEnabled'),
                  value: version?.enterprise_enabled ? t('licenseConsole.yes') : t('licenseConsole.no'),
                },
                { label: t('licenseConsole.buildDate'), value: String(version?.build_date ?? '—') },
              ]}
            />
            <JsonDetails summary={t('licenseConsole.entitlementsTitle')} value={entitlements} />
          </Panel>
        </>
      </StateGate>

      {renewTarget && (
        <Modal title={t('licenseConsole.renewTitle')} onClose={() => setRenewTarget(null)}>
          <form className="form-stack" onSubmit={submitRenew}>
            <MetaList
              rows={[
                { label: t('licenseConsole.licenseId'), value: <code>{renewTarget.id}</code> },
                { label: t('licenseConsole.validUntil'), value: displayDate(renewTarget.valid_until) },
              ]}
            />
            <Field label={t('licenseConsole.extendDays')} hint={t('licenseConsole.extendDaysHint')}>
              <input
                type="number"
                min="1"
                max="365"
                required
                value={extendDays}
                onChange={(event) => setExtendDays(event.target.value)}
                data-testid="license-extend-days"
              />
            </Field>
            <Button type="submit" variant="primary" loading={busy} data-testid="license-renew-submit">
              {t('licenseConsole.renewButton')}
            </Button>
          </form>
        </Modal>
      )}

      {issueOpen && (
        <Modal title={t('licenseConsole.issueTitle')} onClose={() => setIssueOpen(false)}>
          <form className="form-stack" onSubmit={submitIssue}>
            <Field label={t('licenseConsole.issuePlanLabel')}>
              <input
                required
                value={issuePlan}
                onChange={(event) => setIssuePlan(event.target.value)}
                data-testid="license-issue-plan"
              />
            </Field>
            <Field label={t('licenseConsole.issueSeatsLabel')}>
              <input
                type="number"
                min="1"
                max="1000000"
                required
                value={issueSeats}
                onChange={(event) => setIssueSeats(event.target.value)}
                data-testid="license-issue-seats"
              />
            </Field>
            <Field label={t('licenseConsole.issueValidUntilLabel')}>
              <input
                type="datetime-local"
                value={issueValidUntil}
                onChange={(event) => setIssueValidUntil(event.target.value)}
                data-testid="license-issue-valid-until"
              />
            </Field>
            <fieldset className="form-stack">
              <legend>{t('licenseConsole.issueFeaturesLabel')}</legend>
              {availableFeatures.map((feature) => (
                <label className="check-line" key={feature.name}>
                  <input
                    type="checkbox"
                    checked={issueFeatures.includes(feature.name)}
                    onChange={(event) => toggleIssueFeature(feature.name, event.target.checked)}
                  />
                  {feature.name}
                </label>
              ))}
              <small>{t('licenseConsole.issueFeaturesHint')}</small>
            </fieldset>
            <Button type="submit" variant="primary" loading={busy} data-testid="license-issue-submit">
              {t('licenseConsole.issueButton')}
            </Button>
          </form>
        </Modal>
      )}

      {issuedKey && (
        <Modal title={t('licenseConsole.licenseKeyOnce')} onClose={() => setIssuedKey('')}>
          <div className="form-stack">
            <div className="alert">
              <p>{t('licenseConsole.licenseKeyOnceHint')}</p>
            </div>
            <pre tabIndex={0} role="region" aria-label={t('licenseConsole.licenseKeyOnce')}>
              {issuedKey}
            </pre>
          </div>
        </Modal>
      )}
    </>
  )
}
