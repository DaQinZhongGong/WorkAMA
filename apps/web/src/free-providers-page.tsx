/**
 * 免费大模型供应商浏览器页面。
 *
 * - 拉取 `GET /api/v1/gateway/free-providers` 渲染卡片网格
 * - 管理员/所有者可一键启用 `POST /api/v1/gateway/free-providers/{provider}/enable`
 * - 普通成员/未登录用户以只读方式浏览
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowUpRight, ExternalLink, Gift, RefreshCw, Search, Sparkles } from 'lucide-react'
import type { MessageKey } from '@workama/i18n'
import { ApiError } from '@workama/api-client'
import { api } from './api'
import { useAuth } from './auth'
import { useLocale } from './locale'
import { Badge, Button, Field, StateView, Toast } from './ui'

type ProtocolFilter = 'all' | 'openai' | 'anthropic' | 'gemini'
type RegionFilter = 'all' | 'cn' | 'global' | 'us' | 'eu' | 'self_hosted'

// 后端 preset 结构（保持宽松以适配字段缺失）
export type FreeProviderPreset = {
  provider: string
  name: string
  base_url: string
  protocol: string
  signup_url?: string | null
  free_quota?: string | null
  free_models?: string[] | null
  capabilities?: string[] | null
  regions?: string[] | null
  retention_mode?: string | null
  notes?: string | null
}

type FreeProvidersResponse =
  | { providers: FreeProviderPreset[] }
  | { items: FreeProviderPreset[] }
  | FreeProviderPreset[]

const PROTOCOL_OPTIONS: ProtocolFilter[] = ['all', 'openai', 'anthropic', 'gemini']
const REGION_OPTIONS: RegionFilter[] = ['all', 'cn', 'global', 'us', 'eu', 'self_hosted']

const PROTOCOL_TONE: Record<string, string> = {
  openai: 'free-providers-protocol-openai',
  anthropic: 'free-providers-protocol-anthropic',
  gemini: 'free-providers-protocol-gemini',
}

const CAPABILITY_TONE: Record<string, string> = {
  chat: 'free-providers-cap-chat',
  vision: 'free-providers-cap-vision',
  tool_call: 'free-providers-cap-tool',
  tool_calls: 'free-providers-cap-tool',
  json_mode: 'free-providers-cap-json',
  embedding: 'free-providers-cap-embedding',
  embeddings: 'free-providers-cap-embedding',
  reasoning: 'free-providers-cap-reasoning',
}

const PAGE_SIZE = 20
const VISIBLE_MODELS = 3

function asArray(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((item) => String(item)).filter(Boolean)
  return []
}

function matchesQuery(provider: FreeProviderPreset, query: string): boolean {
  if (!query) return true
  const haystack = [
    provider.name,
    provider.provider,
    provider.base_url,
    ...asArray(provider.free_models),
    ...asArray(provider.regions),
  ].join(' ').toLowerCase()
  return haystack.includes(query.toLowerCase())
}

function matchesProtocol(provider: FreeProviderPreset, protocol: ProtocolFilter): boolean {
  if (protocol === 'all') return true
  return String(provider.protocol ?? '').toLowerCase() === protocol
}

function matchesRegion(provider: FreeProviderPreset, region: RegionFilter): boolean {
  if (region === 'all') return true
  const regions = asArray(provider.regions).map((item) => item.toLowerCase())
  return regions.includes(region)
}

function enableErrorText(error: unknown, t: (key: MessageKey) => string): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return t('freeProviders.errorAuth')
    if (error.status === 403) return t('freeProviders.errorForbidden')
    return t('freeProviders.enableFailed').replace('{reason}', error.message || `HTTP ${error.status}`)
  }
  const message = error instanceof Error && error.message ? error.message : ''
  return t('freeProviders.enableFailed').replace('{reason}', message || t('freeProviders.enableFailed'))
}

type FreeProvidersPageProps = {
  // 当为 true 时，未登录/非管理员只能查看（隐藏启用按钮）；默认根据路由判断。
  readOnly?: boolean
}

export default function FreeProvidersPage({ readOnly = false }: FreeProvidersPageProps) {
  const { t } = useLocale()
  const { isAdmin, authenticated } = useAuth()
  const [providers, setProviders] = useState<FreeProviderPreset[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [protocol, setProtocol] = useState<ProtocolFilter>('all')
  const [region, setRegion] = useState<RegionFilter>('all')
  const [page, setPage] = useState(1)
  const [notice, setNotice] = useState('')
  const [noticeTone, setNoticeTone] = useState<'info' | 'error'>('info')
  const [enablingKey, setEnablingKey] = useState<string | null>(null)
  const [enabledKeys, setEnabledKeys] = useState<Set<string>>(new Set())
  const [expandedModels, setExpandedModels] = useState<Set<string>>(new Set())

  // 只读模式：路由可显式声明（公开页）；否则根据当前用户判断
  const canEnable = !readOnly && authenticated && isAdmin

  const reload = useCallback(() => {
    setLoading(true)
    setError('')
    void api
      .get<FreeProvidersResponse>('/api/v1/gateway/free-providers')
      .then((result) => {
        const obj = result as { providers?: FreeProviderPreset[]; items?: FreeProviderPreset[] }
        const list = Array.isArray(result)
          ? result
          : Array.isArray(obj.providers)
            ? obj.providers
            : Array.isArray(obj.items)
              ? obj.items
              : []
        setProviders(list)
      })
      .catch((caught) => {
        setError(caught instanceof Error ? caught.message : t('freeProviders.loadFailed'))
      })
      .finally(() => setLoading(false))
  }, [t])

  useEffect(() => {
    void reload()
  }, [reload])

  const filtered = useMemo(() => {
    return providers.filter((item) => matchesQuery(item, query) && matchesProtocol(item, protocol) && matchesRegion(item, region))
  }, [providers, query, protocol, region])

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const safePage = Math.min(page, totalPages)
  const paged = filtered.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE)

  // 当筛选条件改变时重置分页
  useEffect(() => {
    setPage(1)
  }, [query, protocol, region])

  function toggleModels(key: string) {
    setExpandedModels((current) => {
      const next = new Set(current)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  async function enableProvider(item: FreeProviderPreset) {
    if (!item.provider || enablingKey) return
    setEnablingKey(item.provider)
    setNotice('')
    setNoticeTone('info')
    try {
      await api.post(`/api/v1/gateway/free-providers/${encodeURIComponent(item.provider)}/enable`)
      setEnabledKeys((current) => new Set(current).add(item.provider))
      setNoticeTone('info')
      setNotice(t('freeProviders.enableSuccess').replace('{name}', item.name))
    } catch (caught) {
      setNoticeTone('error')
      setNotice(enableErrorText(caught, t))
    } finally {
      setEnablingKey(null)
    }
  }

  const protocolOptions = PROTOCOL_OPTIONS.map((value) => ({
    value,
    label: value === 'all' ? t('freeProviders.protocolAll') : value,
  }))
  const regionOptions = REGION_OPTIONS.map((value) => ({
    value,
    label: value === 'all' ? t('freeProviders.regionAll') : value === 'self_hosted' ? 'self-hosted' : value,
  }))

  return (
    <>
      <header className="page-header free-providers-header">
        <div>
          <div className="eyebrow">{t('freeProviders.eyebrow')}</div>
          <h1>{t('page.freeProviders')}</h1>
          <p>{t('freeProviders.subtitle')}</p>
        </div>
        <div className="page-actions">
          <Button icon={<RefreshCw size={15} />} onClick={() => void reload()} disabled={loading}>
            {t('operations.refresh')}
          </Button>
          {canEnable && (
            <Link className="button button-primary" to="/gateway/channels">
              {t('nav.gateway')} <ArrowUpRight size={15} />
            </Link>
          )}
        </div>
      </header>

      {notice && (
        <div className={`free-providers-toast-wrap ${noticeTone === 'error' ? 'is-error' : ''}`}>
          <Toast message={notice} onClose={() => setNotice('')} />
        </div>
      )}

      <section className="free-providers-controls" aria-label={t('freeProviders.searchPlaceholder')}>
        <div className="search-box free-providers-search">
          <Search size={16} />
          <label className="sr-only" htmlFor="free-providers-query">
            {t('freeProviders.searchPlaceholder')}
          </label>
          <input
            id="free-providers-query"
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t('freeProviders.searchPlaceholder')}
            data-testid="free-providers-search"
          />
        </div>
        <Field label={t('freeProviders.protocolAll')}>
          <select value={protocol} onChange={(event) => setProtocol(event.target.value as ProtocolFilter)} aria-label={t('freeProviders.protocolAll')} data-testid="free-providers-protocol-filter">
            {protocolOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t('freeProviders.regionAll')}>
          <select value={region} onChange={(event) => setRegion(event.target.value as RegionFilter)} aria-label={t('freeProviders.regionAll')} data-testid="free-providers-region-filter">
            {regionOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </Field>
      </section>

      {!canEnable && !loading && !error && (
        <div className="alert alert-info free-providers-readonly-hint">{t('freeProviders.readOnlyHint')}</div>
      )}

      {loading ? (
        <StateView state="loading" />
      ) : error ? (
        <StateView state="error" description={error} onRetry={() => void reload()} />
      ) : filtered.length === 0 ? (
        <StateView state="empty" title={t('freeProviders.empty')} />
      ) : (
        <>
          <div className="free-providers-grid">
            {paged.map((item) => {
              const models = asArray(item.free_models)
              const expanded = expandedModels.has(item.provider)
              const visibleModels = expanded ? models : models.slice(0, VISIBLE_MODELS)
              const hiddenCount = models.length - VISIBLE_MODELS
              const capabilities = asArray(item.capabilities)
              const regions = asArray(item.regions)
              const protocolClass = PROTOCOL_TONE[String(item.protocol ?? '').toLowerCase()] ?? 'free-providers-protocol-default'
              const isEnabled = enabledKeys.has(item.provider)
              const enabling = enablingKey === item.provider
              return (
                <article key={item.provider} className="free-providers-card" aria-label={`${item.name || item.provider} card`} data-testid="free-providers-card" data-provider={item.provider}>
                  <header className="free-providers-card-head">
                    <div className="free-providers-card-title">
                      <Gift size={16} aria-hidden="true" />
                      <div>
                        <strong>{item.name || item.provider}</strong>
                        <small>{item.provider}</small>
                      </div>
                    </div>
                    <span className={`free-providers-protocol ${protocolClass}`}>{item.protocol || 'unknown'}</span>
                  </header>

                  {item.free_quota && (
                    <p className="free-providers-quota" data-testid="free-providers-quota">
                      <span>{t('freeProviders.freeQuotaLabel')}</span>
                      <strong>{item.free_quota}</strong>
                    </p>
                  )}

                  {models.length > 0 && (
                    <div className="free-providers-models">
                      <span className="free-providers-section-label">{t('freeProviders.modelsLabel')}</span>
                      <ul>
                        {visibleModels.map((model) => (
                          <li key={model}>{model}</li>
                        ))}
                      </ul>
                      {hiddenCount > 0 && !expanded && (
                        <button
                          type="button"
                          className="free-providers-more"
                          aria-label={t('freeProviders.moreModels').replace('{count}', String(hiddenCount))}
                          onClick={() => toggleModels(item.provider)}
                        >
                          {t('freeProviders.moreModels').replace('{count}', String(hiddenCount))}
                        </button>
                      )}
                      {expanded && models.length > VISIBLE_MODELS && (
                        <button
                          type="button"
                          className="free-providers-more"
                          onClick={() => toggleModels(item.provider)}
                        >
                          {t('freeProviders.showLess')}
                        </button>
                      )}
                    </div>
                  )}

                  {capabilities.length > 0 && (
                    <div className="free-providers-section">
                      <span className="free-providers-section-label">{t('freeProviders.capabilitiesLabel')}</span>
                      <div className="free-providers-badges">
                        {capabilities.map((cap) => (
                          <span key={cap} className={`free-providers-cap ${CAPABILITY_TONE[cap.toLowerCase()] ?? 'free-providers-cap-default'}`}>
                            {cap}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}

                  {regions.length > 0 && (
                    <div className="free-providers-section" data-testid="free-providers-regions">
                      <span className="free-providers-section-label">{t('freeProviders.regionsLabel')}</span>
                      <div className="free-providers-badges">
                        {regions.map((regionValue) => (
                          <Badge key={regionValue} tone="neutral">
                            {regionValue}
                          </Badge>
                        ))}
                      </div>
                    </div>
                  )}

                  {item.notes && <p className="free-providers-notes">{item.notes}</p>}

                  <footer className="free-providers-card-footer">
                    {item.signup_url ? (
                      <a
                        className="button button-ghost free-providers-signup"
                        href={item.signup_url}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        <ExternalLink size={14} />
                        {t('freeProviders.signupLabel')}
                      </a>
                    ) : (
                      <span />
                    )}
                    {canEnable ? (
                      <Button
                        variant="primary"
                        loading={enabling}
                        disabled={enabling || isEnabled}
                        onClick={() => void enableProvider(item)}
                        aria-label={`${t('freeProviders.enableLabel')} ${item.name}`}
                        icon={isEnabled ? <Sparkles size={14} /> : undefined}
                        data-testid="free-providers-enable"
                        data-provider={item.provider}
                        data-enabled={isEnabled ? 'true' : 'false'}
                      >
                        {isEnabled ? 'Enabled' : enabling ? t('freeProviders.enablingLabel') : t('freeProviders.enableLabel')}
                      </Button>
                    ) : (
                      <span className="free-providers-readonly-tag" aria-label={t('freeProviders.readOnlyHint')}>
                        {t('freeProviders.readOnlyHint')}
                      </span>
                    )}
                  </footer>
                </article>
              )
            })}
          </div>

          {totalPages > 1 && (
            <nav className="free-providers-pagination" aria-label={t('freeProviders.pageLabel').replace('{page}', String(safePage)).replace('{total}', String(totalPages))}>
              <Button variant="ghost" disabled={safePage <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>
                {t('freeProviders.pagePrev')}
              </Button>
              <span className="free-providers-page-label">{t('freeProviders.pageLabel').replace('{page}', String(safePage)).replace('{total}', String(totalPages))}</span>
              <Button variant="ghost" disabled={safePage >= totalPages} onClick={() => setPage((value) => Math.min(totalPages, value + 1))}>
                {t('freeProviders.pageNext')}
              </Button>
            </nav>
          )}
        </>
      )}
    </>
  )
}
