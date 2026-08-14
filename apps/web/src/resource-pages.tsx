import { Fragment, useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Activity, AlertCircle, ArrowUpRight, BarChart3, Check, ChevronDown, ChevronRight, Clock3, Database, Download, FileText, GitBranch, Image as ImageIcon, ListChecks, Play, Plus, Radio, RefreshCw, Search, ShieldCheck, SlidersHorizontal, Sparkles, Square, Zap } from 'lucide-react'
import { CanvasEditor } from './design-canvas'
import type { MessageKey } from '@workama/i18n'
import { api } from './api'
import { useLocale } from './locale'
import { Badge, Button, DataTable, Field, IconButton, Kpi, Modal, PageHeader, Panel, SearchBox, StateView, Status, Toast } from './ui'

type Item = Record<string, unknown>
type WorkflowGraph = { nodes: Item[]; edges: Item[] }
function useResource(endpoint: string) {
  const { t } = useLocale()
  const [items, setItems] = useState<Item[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const reload = useCallback(() => {
    setLoading(true)
    setError('')
    return api.get<{ items?: Item[] } | Item>(endpoint).then((result) => {
      const next = Array.isArray(result) ? result : Array.isArray(result.items) ? result.items : result && typeof result === 'object' ? [result] : []
      setItems(next)
    }).catch((caught) => setError(caught instanceof Error ? caught.message : t('errors.requestFailed'))).finally(() => setLoading(false))
  }, [endpoint, t])
  useEffect(() => { void reload() }, [reload])
  return { items, loading, error, reload }
}

function resourceItems(value: unknown): Item[] { return Array.isArray(value) ? value.filter((item): item is Item => Boolean(item && typeof item === 'object')) : [] }
function resourceObject(value: unknown): Item { return value && typeof value === 'object' && !Array.isArray(value) ? value as Item : {} }
function resourceJson(value: unknown) { try { return JSON.stringify(value ?? {}, null, 2) || '{}' } catch { return '{}' } }
function parseResourceJson(value: string, label: string, t: (key: MessageKey) => string): unknown { if (!value.trim()) return {}; try { return JSON.parse(value) } catch { throw new Error(t('errors.jsonValidRequired').replace('{label}', label)) } }
function resourceError(value: Item, fallback: string) { return Array.isArray(value.errors) ? value.errors.join('; ') : String(value.detail ?? value.message ?? fallback) }

export function OperationsPage() {
  const { t } = useLocale()
  const [tab, setTab] = useState('overview')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const operations = useResource('/api/v1/admin/operations')
  const jobs = useResource('/api/v1/admin/jobs')
  const deadLetters = useResource('/api/v1/admin/dead-letters')
  const flags = useResource('/api/v1/admin/feature-flags')
  const configs = useResource('/api/v1/admin/dynamic-configs')
  const catalog = useResource('/api/v1/admin/event-catalog')
  const releaseEvidence = useResource('/api/v1/admin/release-evidence')
  const [selectedOperation, setSelectedOperation] = useState<Item | null>(null)
  const [selectedJob, setSelectedJob] = useState<Item | null>(null)
  const [jobRuns, setJobRuns] = useState<Item[]>([])
  const [selectedFlag, setSelectedFlag] = useState<Item | null>(null)
  const [flagVersions, setFlagVersions] = useState<Item[]>([])
  const [flagEvaluation, setFlagEvaluation] = useState<Item | null>(null)
  const [selectedConfig, setSelectedConfig] = useState<Item | null>(null)
  const [configVersions, setConfigVersions] = useState<Item[]>([])
  const [resolvedConfig, setResolvedConfig] = useState<Item | null>(null)
  const [flagOpen, setFlagOpen] = useState(false)
  const [configOpen, setConfigOpen] = useState(false)
  const [releaseOpen, setReleaseOpen] = useState(false)
  const [flagKey, setFlagKey] = useState('')
  const [flagType, setFlagType] = useState('release')
  const [flagOwner, setFlagOwner] = useState('platform')
  const [flagStatus, setFlagStatus] = useState('draft')
  const [flagDefault, setFlagDefault] = useState('false')
  const [flagSafe, setFlagSafe] = useState('false')
  const [flagTargeting, setFlagTargeting] = useState('{"percentage":0}')
  const [flagRunbook, setFlagRunbook] = useState('')
  const [flagSubject, setFlagSubject] = useState('operations-console')
  const [targetVersion, setTargetVersion] = useState('1')
  const [configKey, setConfigKey] = useState('')
  const [configSchema, setConfigSchema] = useState('{"type":"object","properties":{}}')
  const [configValue, setConfigValue] = useState('{}')
  const [configStatus, setConfigStatus] = useState('draft')
  const [configRisk, setConfigRisk] = useState('normal')
  const [releaseVersion, setReleaseVersion] = useState('')
  const [releaseEnvironment, setReleaseEnvironment] = useState('ci')
  const [releaseStatus, setReleaseStatus] = useState('draft')
  const [releaseCommit, setReleaseCommit] = useState('')
  const [releaseTestSummary, setReleaseTestSummary] = useState('{"web":"passed"}')
  const [releaseMigrationSummary, setReleaseMigrationSummary] = useState('{"migrations":"passed"}')
  const [releaseSecuritySummary, setReleaseSecuritySummary] = useState('{"scan":"passed"}')
  const [releaseRollbackSummary, setReleaseRollbackSummary] = useState('{"strategy":"forward-fix"}')
  const [searchIndex, setSearchIndex] = useState<{ document_count: number; tombstone_count: number; last_indexed_at: string; source_updated_at: string } | null>(null)
  const [searchIndexLoading, setSearchIndexLoading] = useState(false)
  const [searchIndexError, setSearchIndexError] = useState('')
  const [rebuildingIndex, setRebuildingIndex] = useState(false)

  const activeOperations = operations.items.filter((item) => ['queued', 'running', 'cancel_requested'].includes(String(item.status ?? '').toLowerCase()))
  const pendingReview = flags.items.filter((item) => ['draft', 'pending'].includes(String(item.status ?? '').toLowerCase())).length + releaseEvidence.items.filter((item) => String(item.status ?? '').toLowerCase() === 'draft').length
  const releasedEvidence = releaseEvidence.items.filter((item) => ['verified', 'approved', 'released'].includes(String(item.status ?? '').toLowerCase())).length
  const readiness = releaseEvidence.items.length ? `${Math.round((releasedEvidence / releaseEvidence.items.length) * 100)}%` : '--'
  const queueHealth = deadLetters.items.length ? t('operations.queueAttention') : jobs.items.length ? t('operations.queueClear') : '--'
  const tabs = [{ id: 'overview', key: 'operations.tabOverview' as MessageKey }, { id: 'Jobs & DLQ', key: 'operations.tabJobsDlq' as MessageKey }, { id: 'Feature Flags', key: 'operations.tabFeatureFlags' as MessageKey }, { id: 'Dynamic Config', key: 'operations.tabDynamicConfig' as MessageKey }, { id: 'Event Catalog', key: 'operations.tabEventCatalog' as MessageKey }, { id: 'Release Evidence', key: 'operations.tabReleaseEvidence' as MessageKey }, { id: 'Search Index', key: 'operations.tabSearchIndex' as MessageKey }]

  async function loadSearchIndexStatus() {
    setSearchIndexLoading(true)
    setSearchIndexError('')
    try {
      const result = await api.get<{ document_count?: number; tombstone_count?: number; last_indexed_at?: string; source_updated_at?: string }>('/api/v1/admin/search-index-status')
      setSearchIndex({
        document_count: Number(result?.document_count ?? 0),
        tombstone_count: Number(result?.tombstone_count ?? 0),
        last_indexed_at: String(result?.last_indexed_at ?? ''),
        source_updated_at: String(result?.source_updated_at ?? ''),
      })
    } catch (caught) {
      setSearchIndex(null)
      setSearchIndexError(caught instanceof Error ? caught.message : t('operations.searchIndexEmpty'))
    } finally {
      setSearchIndexLoading(false)
    }
  }

  useEffect(() => { if (tab === 'Search Index' && !searchIndex && !searchIndexLoading && !searchIndexError) { void loadSearchIndexStatus() } }, [tab, searchIndex, searchIndexLoading, searchIndexError])

  async function rebuildSearchIndex() {
    setRebuildingIndex(true)
    try {
      const queued = await api.post<{ operation_id?: string; status?: string }>('/api/v1/admin/search-index-rebuilds', { resource_types: ['session', 'artifact', 'gateway_channel', 'gateway_token', 'member', 'knowledge_base', 'knowledge_document'] })
      const operationId = String(queued?.operation_id ?? '--')
      setNotice(t('operations.rebuildSearchIndexQueued').replace('{operationId}', operationId))
      await new Promise((resolve) => setTimeout(resolve, 4000))
      await loadSearchIndexStatus()
    } catch (caught) {
      setNotice(caught instanceof Error ? caught.message : t('operations.rebuildSearchIndexFailed'))
    } finally {
      setRebuildingIndex(false)
    }
  }

  async function refreshAll() { await Promise.all([operations.reload(), jobs.reload(), deadLetters.reload(), flags.reload(), configs.reload(), catalog.reload(), releaseEvidence.reload(), loadSearchIndexStatus()]) }
  async function inspectOperation(item: Item) { if (!item.id) return; setBusy(true); try { setSelectedOperation(await api.get<Item>(`/api/v1/operations/${encodeURIComponent(String(item.id))}`)) } catch (caught) { setNotice(caught instanceof Error ? caught.message : t('operations.operationDetailsFailed')) } finally { setBusy(false) } }
  async function cancelOperation() { if (!selectedOperation?.id) return; setBusy(true); try { await api.post(`/api/v1/operations/${encodeURIComponent(String(selectedOperation.id))}/cancellations`, { reason: 'Cancelled from Operations console' }); setNotice(t('operations.operationCancellationRequested')); await refreshAll(); await inspectOperation(selectedOperation) } catch (caught) { setNotice(caught instanceof Error ? caught.message : t('operations.operationCancellationFailed')) } finally { setBusy(false) } }
  async function inspectJob(item: Item) { if (!item.id) return; setBusy(true); try { const [job, runs] = await Promise.all([api.get<Item>(`/api/v1/admin/jobs/${encodeURIComponent(String(item.id))}`), api.get<{ items?: Item[] }>(`/api/v1/admin/jobs/${encodeURIComponent(String(item.id))}/runs`)]); setSelectedJob(job); setJobRuns(resourceItems(runs.items)) } catch (caught) { setNotice(caught instanceof Error ? caught.message : t('operations.operationDetailsFailed')) } finally { setBusy(false) } }
  async function cancelJob() { if (!selectedJob?.id) return; setBusy(true); try { await api.post(`/api/v1/admin/jobs/${encodeURIComponent(String(selectedJob.id))}/cancellations`, { reason: 'Cancelled from Operations console' }); setNotice(t('operations.jobCancellationRequested')); await refreshAll(); await inspectJob(selectedJob) } catch (caught) { setNotice(caught instanceof Error ? caught.message : t('operations.jobCancellationFailed')) } finally { setBusy(false) } }
  async function replayDeadLetter(item: Item) { if (!item.id) return; setBusy(true); try { await api.post(`/api/v1/admin/dead-letters/${encodeURIComponent(String(item.id))}/replays`, { reason: 'Replayed from Operations console' }); setNotice(t('operations.deadLetterReplayQueued')); await refreshAll() } catch (caught) { setNotice(caught instanceof Error ? caught.message : t('operations.deadLetterReplayFailed')) } finally { setBusy(false) } }
  async function saveFlag(event: FormEvent) { event.preventDefault(); setBusy(true); try { const body = { flag_type: flagType, default_value: flagDefault === 'true', safe_value: flagSafe === 'true', targeting: parseResourceJson(flagTargeting, t('operations.targeting'), t), status: flagStatus, owner: flagOwner, runbook: flagRunbook || null, metrics: {} }; const validation = await api.post<Item>('/api/v1/admin/feature-flag-validations', body); if (!validation.valid) throw new Error(resourceError(validation, t('operations.flagValidationFailed'))); await api.put(`/api/v1/admin/feature-flags/${encodeURIComponent(flagKey)}`, body); setFlagOpen(false); setNotice(t('operations.flagSavedNotice')); setFlagKey(''); void flags.reload() } catch (caught) { setNotice(caught instanceof Error ? caught.message : t('operations.flagSaveFailed')) } finally { setBusy(false) } }
  async function openFlag(item: Item) { if (!item.key) return; setBusy(true); try { const result = await api.get<Item>(`/api/v1/admin/feature-flags/${encodeURIComponent(String(item.key))}`); setSelectedFlag(result.current as Item); setFlagVersions(resourceItems(result.versions)); setTargetVersion(String((result.current as Item | undefined)?.version ?? 1)); setFlagEvaluation(null) } catch (caught) { setNotice(caught instanceof Error ? caught.message : t('operations.flagDetailsFailed')) } finally { setBusy(false) } }
  async function rollbackFlag() { if (!selectedFlag?.key) return; setBusy(true); try { await api.post(`/api/v1/admin/feature-flags/${encodeURIComponent(String(selectedFlag.key))}/rollbacks`, { target_version: Number(targetVersion) }); setNotice(t('operations.flagRollbackCreated')); await flags.reload(); await openFlag(selectedFlag) } catch (caught) { setNotice(caught instanceof Error ? caught.message : t('operations.flagRollbackFailed')) } finally { setBusy(false) } }
  async function evaluateFlag() { if (!selectedFlag?.key) return; setBusy(true); try { setFlagEvaluation(await api.post<Item>(`/api/v1/admin/feature-flags/${encodeURIComponent(String(selectedFlag.key))}/evaluations`, { subject_id: flagSubject })); } catch (caught) { setNotice(caught instanceof Error ? caught.message : t('operations.flagEvaluationFailed')) } finally { setBusy(false) } }
  async function saveConfig(event: FormEvent) { event.preventDefault(); setBusy(true); try { const body = { schema_version: 1, value_schema: parseResourceJson(configSchema, t('operations.valueSchema'), t), config_value: parseResourceJson(configValue, t('operations.configValue'), t), status: configStatus, risk_level: configRisk }; const validation = await api.post<Item>('/api/v1/admin/dynamic-config-validations', body); if (!validation.valid) throw new Error(resourceError(validation, t('operations.configValidationFailed'))); await api.put(`/api/v1/admin/dynamic-configs/${encodeURIComponent(configKey)}`, body); setConfigOpen(false); setNotice(t('operations.configSavedNotice')); setConfigKey(''); void configs.reload() } catch (caught) { setNotice(caught instanceof Error ? caught.message : t('operations.configSaveFailed')) } finally { setBusy(false) } }
  async function openConfig(item: Item) { if (!item.config_key) return; setBusy(true); try { const [detail, resolved] = await Promise.all([api.get<Item>(`/api/v1/admin/dynamic-configs/${encodeURIComponent(String(item.config_key))}`), api.get<Item>(`/api/v1/admin/dynamic-configs/${encodeURIComponent(String(item.config_key))}/resolved`)]); setSelectedConfig(detail.current as Item); setConfigVersions(resourceItems(detail.versions)); setResolvedConfig(resolved) } catch (caught) { setNotice(caught instanceof Error ? caught.message : t('operations.configDetailsFailed')) } finally { setBusy(false) } }
  async function saveReleaseEvidence(event: FormEvent) { event.preventDefault(); setBusy(true); try { await api.post('/api/v1/admin/release-evidence', { release_version: releaseVersion, environment: releaseEnvironment, status: releaseStatus, commit_ref: releaseCommit || null, test_summary: parseResourceJson(releaseTestSummary, t('operations.testSummary'), t), migration_summary: parseResourceJson(releaseMigrationSummary, t('operations.migrationSummary'), t), security_summary: parseResourceJson(releaseSecuritySummary, t('operations.securitySummary'), t), rollback_summary: parseResourceJson(releaseRollbackSummary, t('operations.rollbackSummary'), t), image_refs: {}, approvals: [] }); setReleaseOpen(false); setNotice(t('operations.releaseEvidenceRecorded')); setReleaseVersion(''); void releaseEvidence.reload() } catch (caught) { setNotice(caught instanceof Error ? caught.message : t('operations.releaseEvidenceSaveFailed')) } finally { setBusy(false) } }

  return <><PageHeader eyebrow={t('operations.eyebrow')} title={t('page.operations')} description={t('operations.description')} actions={<><Button icon={<RefreshCw size={15} />} onClick={() => void refreshAll()}>{t('operations.refresh')}</Button>{tab === 'Release Evidence' && <Button variant="primary" icon={<Plus size={15} />} onClick={() => setReleaseOpen(true)}>{t('operations.recordEvidence')}</Button>}</>} />{notice && <Toast message={notice} onClose={() => setNotice('')} />}<div className="tab-strip" role="tablist">{tabs.map((item) => <button key={item.id} role="tab" aria-selected={tab === item.id} className={tab === item.id ? 'active' : ''} onClick={() => setTab(item.id)}>{t(item.key)}</button>)}</div>
    {tab === 'overview' && <><div className="kpi-grid"><Kpi label={t('operations.asyncOperations')} value={String(operations.items.length).padStart(2, '0')} icon={<Activity size={18} />} trend={t('operations.activeLabel').replace('{count}', String(activeOperations.length))} /><Kpi label={t('operations.queueHealth')} value={queueHealth} icon={<Check size={18} />} trend={t('operations.deadLetters').replace('{count}', String(deadLetters.items.length))} /><Kpi label={t('operations.pendingReview')} value={String(pendingReview).padStart(2, '0')} icon={<ShieldCheck size={18} />} trend={t('operations.draftFlagsAndEvidence')} /><Kpi label={t('operations.releaseReadiness')} value={readiness} icon={<GitBranch size={18} />} trend={releaseEvidence.items.length ? t('operations.verifiedFraction').replace('{released}', String(releasedEvidence)).replace('{total}', String(releaseEvidence.items.length)) : t('operations.noEvidenceYet')} /></div><div className="ops-grid"><Panel title={t('operations.asyncOperationsPanel')} subtitle={t('operations.asyncOperationsSubtitle')}><DataTable headers={[t('operations.operation'), t('operations.status'), t('operations.progress'), t('operations.created'), '']} >{operations.loading ? <tr><td colSpan={5}><StateView state="loading" /></td></tr> : operations.error ? <tr><td colSpan={5}><StateView state="error" description={operations.error} onRetry={() => void operations.reload()} /></td></tr> : operations.items.length ? operations.items.slice(0, 50).map((item, index) => <tr key={String(item.id ?? index)}><td><strong>{String(item.operation_type ?? item.name ?? t('operations.workamaOperation'))}</strong><small className="table-subtext">{String(item.id ?? '--')}</small></td><td><Status value={String(item.status ?? 'unknown')} /></td><td><div className="ops-progress"><i style={{ width: `${Math.min(100, Math.max(0, Number(item.progress ?? 0)))}%` }} /></div><small>{String(item.progress ?? 0)}%</small></td><td>{String(item.created_at ?? '--')}</td><td><Button variant="ghost" disabled={busy} onClick={() => void inspectOperation(item)}>{t('operations.inspect')}</Button></td></tr>) : <tr><td colSpan={5}><StateView state="empty" title={t('operations.noOperationsYet')} description={t('operations.noOperationsDescription')} /></td></tr>}</DataTable></Panel><Panel title={selectedOperation ? t('operations.operationDetail') : t('operations.selectOperation')} subtitle={selectedOperation ? String(selectedOperation.id) : t('operations.selectOperationSubtitle')}>{selectedOperation ? <div className="ops-detail"><div className="ops-detail-head"><Status value={String(selectedOperation.status ?? 'unknown')} /><span>{String(selectedOperation.operation_type ?? '--')}</span></div><div className="evidence-grid"><div><strong>{t('operations.progress')}</strong><span>{String(selectedOperation.progress ?? 0)}%</span></div><div><strong>{t('operations.attempts')}</strong><span>{String(selectedOperation.attempt_count ?? 0)}</span></div><div><strong>{t('operations.updated')}</strong><span>{String(selectedOperation.updated_at ?? selectedOperation.created_at ?? '--')}</span></div></div>{Boolean(selectedOperation.error_message) && <div className="alert alert-error">{String(selectedOperation.error_message)}</div>}<details className="ops-json"><summary>{t('operations.operationPayload')}</summary><pre>{resourceJson(selectedOperation)}</pre></details>{['queued', 'running', 'cancel_requested'].includes(String(selectedOperation.status ?? '').toLowerCase()) && <Button variant="danger" icon={<Square size={14} />} loading={busy} onClick={() => void cancelOperation()}>{t('operations.cancelOperation')}</Button>}</div> : <StateView state="empty" title={t('operations.noOperationSelected')} description={t('operations.chooseOperationDescription')} />}</Panel></div></>}
    {tab === 'Jobs & DLQ' && <div className="ops-grid"><Panel title={t('operations.jobsPanel')} subtitle={t('operations.jobsPanelSubtitle')}><DataTable headers={[t('operations.job'), t('operations.operation'), t('operations.status'), t('operations.attempts'), '']} >{jobs.loading ? <tr><td colSpan={5}><StateView state="loading" /></td></tr> : jobs.error ? <tr><td colSpan={5}><StateView state="error" description={jobs.error} onRetry={() => void jobs.reload()} /></td></tr> : jobs.items.length ? jobs.items.map((item, index) => <tr key={String(item.id ?? index)}><td><strong>{String(item.job_type ?? t('operations.job'))}</strong><small className="table-subtext">{String(item.id ?? '--')}</small></td><td><code>{String(item.operation_id ?? '--')}</code></td><td><Status value={String(item.status ?? 'unknown')} /></td><td>{String(item.attempt_count ?? item.attempts ?? 0)} / {String(item.max_attempts ?? '--')}</td><td><Button variant="ghost" disabled={busy} onClick={() => void inspectJob(item)}>{t('operations.inspect')}</Button></td></tr>) : <tr><td colSpan={5}><StateView state="empty" title={t('operations.noJobsYet')} /></td></tr>}</DataTable></Panel><Panel title={selectedJob ? t('operations.jobDetail') : t('operations.selectJob')} subtitle={selectedJob ? String(selectedJob.id) : t('operations.selectJobSubtitle')}>{selectedJob ? <div className="ops-detail"><div className="ops-detail-head"><Status value={String(selectedJob.status ?? 'unknown')} /><span>{String(selectedJob.queue ?? '--')}</span></div><div className="evidence-grid"><div><strong>{t('operations.operation')}</strong><code>{String(selectedJob.operation_id ?? '--')}</code></div><div><strong>{t('operations.heartbeat')}</strong><span>{String(selectedJob.heartbeat_at ?? '--')}</span></div><div><strong>{t('operations.timeout')}</strong><span>{String(selectedJob.timeout_seconds ?? '--')}s</span></div></div>{jobRuns.length > 0 && <div className="ops-run-list">{jobRuns.map((run, index) => <div key={String(run.id ?? index)}><strong>{t('operations.attemptN').replace('{n}', String(run.attempt ?? index + 1))}</strong><Status value={String(run.status ?? 'unknown')} /><small>{String(run.started_at ?? run.created_at ?? '--')}</small></div>)}</div>}{['queued', 'running', 'cancel_requested'].includes(String(selectedJob.status ?? '').toLowerCase()) && <Button variant="danger" icon={<Square size={14} />} loading={busy} onClick={() => void cancelJob()}>{t('operations.cancelJob')}</Button>}</div> : <StateView state="empty" title={t('operations.noJobSelected')} description={t('operations.chooseJobDescription')} />}</Panel><Panel className="ops-dlq-panel" title={t('operations.deadLettersPanel')} subtitle={t('operations.deadLettersSubtitle')}><DataTable headers={[t('operations.job'), t('operations.failed'), t('operations.error'), '']} >{deadLetters.loading ? <tr><td colSpan={4}><StateView state="loading" /></td></tr> : deadLetters.error ? <tr><td colSpan={4}><StateView state="error" description={deadLetters.error} onRetry={() => void deadLetters.reload()} /></td></tr> : deadLetters.items.length ? deadLetters.items.map((item, index) => <tr key={String(item.id ?? index)}><td><code>{String(item.job_id ?? item.id ?? '--')}</code></td><td>{String(item.failed_at ?? '--')}</td><td className="ops-error-cell">{String(item.error_message ?? item.error ?? '--')}</td><td><Button variant="ghost" disabled={busy || item.replayed_at != null} onClick={() => void replayDeadLetter(item)}>{item.replayed_at ? t('operations.replayed') : t('operations.replay')}</Button></td></tr>) : <tr><td colSpan={4}><StateView state="empty" title={t('operations.noDeadLetters')} description={t('operations.noDeadLettersDescription')} /></td></tr>}</DataTable></Panel></div>}
    {tab === 'Feature Flags' && <Panel title={t('operations.featureFlagsPanel')} subtitle={t('operations.featureFlagsSubtitle')}><div className="panel-actions-inline ops-toolbar"><Button variant="primary" icon={<Plus size={14} />} onClick={() => setFlagOpen(true)}>{t('operations.newFlagVersion')}</Button></div><DataTable headers={[t('operations.flagKey'), t('operations.type'), t('operations.status'), t('operations.version'), t('operations.rollout'), '']} >{flags.loading ? <tr><td colSpan={6}><StateView state="loading" /></td></tr> : flags.error ? <tr><td colSpan={6}><StateView state="error" description={flags.error} onRetry={() => void flags.reload()} /></td></tr> : flags.items.length ? flags.items.map((item, index) => { const targeting = resourceObject(item.targeting); const percentage = Number(targeting.percentage ?? 0); return <tr key={String(item.key ?? index)}><td><button className="ops-link-button" onClick={() => void openFlag(item)}><code>{String(item.key ?? '--')}</code></button><small className="table-subtext">{String(item.owner ?? '--')}</small></td><td>{String(item.flag_type ?? '--')}</td><td><Status value={String(item.status ?? 'unknown')} /></td><td>v{String(item.version ?? 1)}</td><td>{Number.isFinite(percentage) ? `${(percentage / 100).toFixed(1)}%` : '--'}</td><td><Button variant="ghost" disabled={busy} onClick={() => void openFlag(item)}>{t('operations.inspect')}</Button></td></tr> }) : <tr><td colSpan={6}><StateView state="empty" title={t('operations.noFeatureFlags')} description={t('operations.noFeatureFlagsDescription')} /></td></tr>}</DataTable></Panel>}
    {tab === 'Dynamic Config' && <Panel title={t('operations.dynamicConfigPanel')} subtitle={t('operations.dynamicConfigSubtitle')}><div className="panel-actions-inline ops-toolbar"><Button variant="primary" icon={<Plus size={14} />} onClick={() => setConfigOpen(true)}>{t('operations.newConfigVersion')}</Button></div><DataTable headers={[t('operations.configKey'), t('operations.status'), t('operations.risk'), t('operations.version'), t('operations.effective'), '']} >{configs.loading ? <tr><td colSpan={6}><StateView state="loading" /></td></tr> : configs.error ? <tr><td colSpan={6}><StateView state="error" description={configs.error} onRetry={() => void configs.reload()} /></td></tr> : configs.items.length ? configs.items.map((item, index) => <tr key={String(item.config_key ?? index)}><td><button className="ops-link-button" onClick={() => void openConfig(item)}><code>{String(item.config_key ?? '--')}</code></button></td><td><Status value={String(item.status ?? 'unknown')} /></td><td>{String(item.risk_level ?? 'normal')}</td><td>v{String(item.version ?? 1)}</td><td>{String(item.effective_at ?? t('operations.immediate'))}</td><td><Button variant="ghost" disabled={busy} onClick={() => void openConfig(item)}>{t('operations.inspect')}</Button></td></tr>) : <tr><td colSpan={6}><StateView state="empty" title={t('operations.noDynamicConfig')} description={t('operations.noDynamicConfigDescription')} /></td></tr>}</DataTable></Panel>}
    {tab === 'Event Catalog' && <Panel title={t('operations.eventCatalogPanel')} subtitle={t('operations.eventCatalogCount').replace('{count}', String(catalog.items.length))}><DataTable headers={[t('operations.eventType'), t('operations.domain'), t('operations.properties'), t('operations.hash'), t('operations.updated')]} >{catalog.loading ? <tr><td colSpan={5}><StateView state="loading" /></td></tr> : catalog.error ? <tr><td colSpan={5}><StateView state="error" description={catalog.error} onRetry={() => void catalog.reload()} /></td></tr> : catalog.items.length ? catalog.items.map((item, index) => <tr key={String(item.event_name ?? index)}><td><code>{String(item.event_name ?? '--')}</code></td><td>{String(item.domain ?? '--')}</td><td>{Array.isArray(item.allowed_properties) ? item.allowed_properties.length : '--'}</td><td><code>{String(item.content_hash ?? '--')}</code></td><td>{String(item.updated_at ?? '--')}</td></tr>) : <tr><td colSpan={5}><StateView state="empty" title={t('operations.eventCatalogUnavailable')} /></td></tr>}</DataTable></Panel>}
    {tab === 'Release Evidence' && <Panel title={t('operations.releaseEvidencePanel')} subtitle={t('operations.releaseEvidenceSubtitle')} actions={<Button variant="primary" icon={<Plus size={14} />} onClick={() => setReleaseOpen(true)}>{t('operations.recordEvidence')}</Button>}><DataTable headers={[t('operations.release'), t('operations.environment'), t('operations.status'), t('operations.commit'), t('operations.contentHash'), t('operations.created')]} >{releaseEvidence.loading ? <tr><td colSpan={6}><StateView state="loading" /></td></tr> : releaseEvidence.error ? <tr><td colSpan={6}><StateView state="error" description={releaseEvidence.error} onRetry={() => void releaseEvidence.reload()} /></td></tr> : releaseEvidence.items.length ? releaseEvidence.items.map((item, index) => <tr key={String(item.id ?? index)}><td><strong>{String(item.release_version ?? '--')}</strong><small className="table-subtext">{String(item.id ?? '--')}</small></td><td>{String(item.environment ?? '--')}</td><td><Status value={String(item.status ?? 'unknown')} /></td><td><code>{String(item.commit_ref ?? '--')}</code></td><td><code>{String(item.content_hash ?? '--')}</code></td><td>{String(item.created_at ?? '--')}</td></tr>) : <tr><td colSpan={6}><StateView state="empty" title={t('operations.noReleaseEvidence')} description={t('operations.noReleaseEvidenceDescription')} /></td></tr>}</DataTable></Panel>}
        {tab === 'Search Index' && <div className="ops-grid"><Panel title={t('operations.searchIndexPanel')} subtitle={t('operations.searchIndexPanelSubtitle')} actions={<><Button icon={<RefreshCw size={14} />} onClick={() => void loadSearchIndexStatus()} loading={searchIndexLoading}>{t('operations.refresh')}</Button><Button variant="primary" icon={<Database size={14} />} onClick={() => void rebuildSearchIndex()} loading={rebuildingIndex}>{rebuildingIndex ? t('operations.rebuildSearchIndexRebuilding') : t('operations.rebuildSearchIndexAll')}</Button></>}>{searchIndexLoading && !searchIndex ? <StateView state="loading" title={t('operations.searchIndexLoading')} /> : searchIndexError ? <StateView state="error" description={searchIndexError} onRetry={() => void loadSearchIndexStatus()} /> : searchIndex ? <div className="ops-detail"><div className="evidence-grid"><div><strong>{t('operations.searchIndexDocuments')}</strong><span>{String(searchIndex.document_count).padStart(4, '0')}</span></div><div><strong>{t('operations.searchIndexTombstones')}</strong><span>{String(searchIndex.tombstone_count).padStart(4, '0')}</span></div><div><strong>{t('operations.searchIndexLastIndexed')}</strong><span>{searchIndex.last_indexed_at || '--'}</span></div><div><strong>{t('operations.searchIndexSourceUpdated')}</strong><span>{searchIndex.source_updated_at || '--'}</span></div></div>{searchIndex.document_count === 0 && <div className="callout"><Database size={15} /><span>{t('operations.searchIndexEmpty')}</span></div>}</div> : <StateView state="empty" title={t('operations.searchIndexEmpty')} />}</Panel><Panel title={t('operations.searchIndexDocuments')} subtitle={t('operations.searchIndexPanelSubtitle')}><ul className="ops-resource-list"><li><code>session</code><span>{t('operations.searchIndexDocuments')}</span></li><li><code>artifact</code><span>{t('operations.searchIndexDocuments')}</span></li><li><code>gateway_channel</code><span>{t('operations.searchIndexDocuments')}</span></li><li><code>gateway_token</code><span>{t('operations.searchIndexDocuments')}</span></li><li><code>member</code><span>{t('operations.searchIndexDocuments')}</span></li><li><code>knowledge_base</code><span>{t('operations.searchIndexDocuments')}</span></li><li><code>knowledge_document</code><span>{t('operations.searchIndexDocuments')}</span></li></ul></Panel></div>}
    {selectedFlag && <Modal title={t('operations.featureFlagModalTitle').replace('{key}', String(selectedFlag.key))} onClose={() => setSelectedFlag(null)}><div className="form-stack"><div className="evidence-grid"><div><strong>{t('operations.status')}</strong><Status value={String(selectedFlag.status ?? '--')} /></div><div><strong>{t('operations.currentVersion')}</strong><span>v{String(selectedFlag.version ?? 1)}</span></div><div><strong>{t('operations.contentHash')}</strong><code>{String(selectedFlag.content_hash ?? '--')}</code></div></div><Field label={t('operations.evaluateSubject')}><input value={flagSubject} onChange={(event) => setFlagSubject(event.target.value)} /></Field><div className="button-row"><Button icon={<Search size={14} />} onClick={() => void evaluateFlag()} loading={busy}>{t('operations.evaluate')}</Button><Field label={t('operations.rollbackTarget')}><select value={targetVersion} onChange={(event) => setTargetVersion(event.target.value)}>{flagVersions.map((item, index) => <option key={String(item.version ?? index)} value={String(item.version ?? index + 1)}>{t('operations.versionN').replace('{n}', String(item.version ?? index + 1))}</option>)}</select></Field><Button variant="danger" icon={<RefreshCw size={14} />} onClick={() => void rollbackFlag()} loading={busy}>{t('operations.createRollback')}</Button></div>{flagEvaluation && <div className="callout"><Check size={15} /><span>{JSON.stringify(flagEvaluation)}</span></div>}<details className="ops-json"><summary>{t('operations.versionHistory').replace('{count}', String(flagVersions.length))}</summary><pre>{resourceJson(flagVersions)}</pre></details></div></Modal>}
    {selectedConfig && <Modal title={t('operations.dynamicConfigModalTitle').replace('{key}', String(selectedConfig.config_key))} onClose={() => setSelectedConfig(null)}><div className="form-stack"><div className="evidence-grid"><div><strong>{t('operations.status')}</strong><Status value={String(selectedConfig.status ?? '--')} /></div><div><strong>{t('operations.risk')}</strong><span>{String(selectedConfig.risk_level ?? 'normal')}</span></div><div><strong>{t('operations.resolvedVersion')}</strong><span>v{String(resolvedConfig?.version ?? selectedConfig.version ?? 1)}</span></div></div><details open className="ops-json"><summary>{t('operations.resolvedValueLabel')}</summary><pre>{resourceJson(resolvedConfig?.value ?? resolvedConfig)}</pre></details><details className="ops-json"><summary>{t('operations.versionHistory').replace('{count}', String(configVersions.length))}</summary><pre>{resourceJson(configVersions)}</pre></details></div></Modal>}
    {flagOpen && <Modal title={t('operations.createFlagModalTitle')} onClose={() => setFlagOpen(false)}><form className="form-stack" onSubmit={saveFlag}><div className="form-grid"><Field label={t('operations.flagKey')}><input value={flagKey} onChange={(event) => setFlagKey(event.target.value)} required placeholder={t('operations.flagKeyPlaceholder')} /></Field><Field label={t('operations.type')}><select value={flagType} onChange={(event) => setFlagType(event.target.value)}><option value="release">{t('operations.typeRelease')}</option><option value="experiment">{t('operations.typeExperiment')}</option><option value="ops">{t('operations.typeOps')}</option></select></Field><Field label={t('operations.owner')}><input value={flagOwner} onChange={(event) => setFlagOwner(event.target.value)} required /></Field><Field label={t('operations.status')}><select value={flagStatus} onChange={(event) => setFlagStatus(event.target.value)}><option value="draft">{t('governance.status.draft')}</option><option value="enabled">{t('operations.optionEnabled')}</option><option value="disabled">{t('operations.optionDisabled')}</option></select></Field></div><div className="form-grid"><Field label={t('operations.defaultValue')}><select value={flagDefault} onChange={(event) => setFlagDefault(event.target.value)}><option value="false">{t('operations.optionFalse')}</option><option value="true">{t('operations.optionTrue')}</option></select></Field><Field label={t('operations.safeValue')}><select value={flagSafe} onChange={(event) => setFlagSafe(event.target.value)}><option value="false">{t('operations.optionFalse')}</option><option value="true">{t('operations.optionTrue')}</option></select></Field></div><Field label={t('operations.targetingJson')}><textarea className="code-input" rows={5} value={flagTargeting} onChange={(event) => setFlagTargeting(event.target.value)} spellCheck={false} /></Field><Field label={t('operations.runbook')}><textarea value={flagRunbook} onChange={(event) => setFlagRunbook(event.target.value)} placeholder={t('operations.runbookPlaceholder')} /></Field><Button type="submit" variant="primary" loading={busy}>{t('operations.validateAndSave')}</Button></form></Modal>}
    {configOpen && <Modal title={t('operations.createConfigModalTitle')} onClose={() => setConfigOpen(false)}><form className="form-stack" onSubmit={saveConfig}><Field label={t('operations.configKey')}><input value={configKey} onChange={(event) => setConfigKey(event.target.value)} required placeholder={t('operations.configKeyPlaceholder')} /></Field><div className="form-grid"><Field label={t('operations.status')}><select value={configStatus} onChange={(event) => setConfigStatus(event.target.value)}><option value="draft">{t('governance.status.draft')}</option><option value="enabled">{t('operations.optionEnabled')}</option><option value="disabled">{t('operations.optionDisabled')}</option></select></Field><Field label={t('operations.riskLevel')}><select value={configRisk} onChange={(event) => setConfigRisk(event.target.value)}><option value="normal">{t('operations.optionNormal')}</option><option value="high">{t('operations.optionHigh')}</option></select></Field></div><Field label={t('operations.valueSchemaJson')}><textarea className="code-input" rows={6} value={configSchema} onChange={(event) => setConfigSchema(event.target.value)} spellCheck={false} /></Field><Field label={t('operations.configValueJson')}><textarea className="code-input" rows={6} value={configValue} onChange={(event) => setConfigValue(event.target.value)} spellCheck={false} /></Field><Button type="submit" variant="primary" loading={busy}>{t('operations.validateAndSave')}</Button></form></Modal>}
    {releaseOpen && <Modal title={t('operations.recordReleaseModalTitle')} onClose={() => setReleaseOpen(false)}><form className="form-stack" onSubmit={saveReleaseEvidence}><div className="form-grid"><Field label={t('operations.releaseVersion')}><input value={releaseVersion} onChange={(event) => setReleaseVersion(event.target.value)} required placeholder={t('operations.releaseVersionPlaceholder')} /></Field><Field label={t('operations.environment')}><select value={releaseEnvironment} onChange={(event) => setReleaseEnvironment(event.target.value)}><option value="dev">{t('operations.envDevelopment')}</option><option value="ci">{t('operations.envCi')}</option><option value="staging">{t('operations.envStaging')}</option><option value="preprod">{t('operations.envPreprod')}</option><option value="prod">{t('operations.envProd')}</option></select></Field><Field label={t('operations.status')}><select value={releaseStatus} onChange={(event) => setReleaseStatus(event.target.value)}><option value="draft">{t('governance.status.draft')}</option><option value="verified">{t('operations.optionVerified')}</option><option value="approved">{t('operations.optionApproved')}</option><option value="released">{t('operations.optionReleased')}</option><option value="rolled_back">{t('operations.optionRolledBack')}</option></select></Field><Field label={t('operations.commitReference')}><input value={releaseCommit} onChange={(event) => setReleaseCommit(event.target.value)} /></Field></div><div className="form-grid"><Field label={t('operations.testSummaryJson')}><textarea className="code-input" rows={5} value={releaseTestSummary} onChange={(event) => setReleaseTestSummary(event.target.value)} spellCheck={false} /></Field><Field label={t('operations.migrationSummaryJson')}><textarea className="code-input" rows={5} value={releaseMigrationSummary} onChange={(event) => setReleaseMigrationSummary(event.target.value)} spellCheck={false} /></Field><Field label={t('operations.securitySummaryJson')}><textarea className="code-input" rows={5} value={releaseSecuritySummary} onChange={(event) => setReleaseSecuritySummary(event.target.value)} spellCheck={false} /></Field><Field label={t('operations.rollbackSummaryJson')}><textarea className="code-input" rows={5} value={releaseRollbackSummary} onChange={(event) => setReleaseRollbackSummary(event.target.value)} spellCheck={false} /></Field></div><Button type="submit" variant="primary" loading={busy}>{t('operations.recordEvidence')}</Button></form></Modal>}
  </>
}

export function SearchPage() {
  const { t } = useLocale()
  const [query, setQuery] = useState('')
  const [items, setItems] = useState<Item[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  // v7.260: 分页状态 — offset 累计 / 每次 fetch 增加 / has_more 控制 Load more 按钮。
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const PAGE_SIZE = 20
  const reset = () => { setItems([]); setOffset(0); setTotal(0); setHasMore(false) }
  const search = async (event: FormEvent | undefined, append = false) => {
    event?.preventDefault()
    if (!query.trim()) return
    if (loading || loadingMore) return
    const nextOffset = append ? offset : 0
    if (append) setLoadingMore(true); else { setLoading(true); reset() }
    setError('')
    try {
      const url = `/api/v1/search?q=${encodeURIComponent(query.trim())}&limit=${PAGE_SIZE}&offset=${nextOffset}`
      const result = await api.get<{ items?: Item[]; total?: number; has_more?: boolean }>(url)
      const fetched = result.items ?? []
      setItems((prev) => append ? [...prev, ...fetched] : fetched)
      setTotal(Number(result?.total ?? fetched.length))
      setOffset(nextOffset + fetched.length)
      setHasMore(Boolean(result?.has_more))
    }
    catch (caught) { setError(caught instanceof Error ? caught.message : t('search.searchFailed')) }
    finally { setLoading(false); setLoadingMore(false) }
  }
  const loadMore = (event: FormEvent) => search(event, true)
  // 映射后端 /api/v1/search 返回的 resource_type 到用户可读的类型标签 + URL。
  const resourceTypeLabel = (raw: string): string => {
    const key = String(raw ?? '').toLowerCase()
    if (key === 'knowledge_base') return t('search.type.knowledgeBase')
    if (key === 'knowledge_document') return t('search.type.knowledgeDocument')
    if (key === 'gateway_channel') return t('search.type.gatewayChannel')
    if (key === 'gateway_token') return t('search.type.gatewayToken')
    return raw
  }
  const resourceTypeToPath = (raw: string): string => {
    const key = String(raw ?? '').toLowerCase()
    if (key === 'knowledge_base') return '/knowledge'
    if (key === 'knowledge_document') return '/knowledge'
    if (key === 'gateway_channel') return '/gateway/channels'
    if (key === 'gateway_token') return '/gateway/tokens'
    if (key === 'session' || key === 'artifact') return '/chat'
    if (key === 'member') return '/admin/members'
    return '/search'
  }
  return <><PageHeader eyebrow={t('search.eyebrow')} title={t('search.globalSearch')} description={t('search.description')} /><Panel title={t('search.searchEverything')}><form className="global-search" onSubmit={(e) => search(e, false)}><SearchBox value={query} onChange={(v) => { setQuery(v); if (items.length) { reset() } }} placeholder={t('search.searchByKeyword')} /><Button type="submit" variant="primary" icon={<Search size={16} />}>{t('search.search')}</Button></form>{loading ? <StateView state="loading" /> : error ? <StateView state="error" description={error} onRetry={() => search(undefined, false)} /> : items.length ? <><div className="search-results-meta">{t('search.showingResults').replace('{from}', '1').replace('{to}', String(items.length)).replace('{total}', String(total))}</div><div className="search-results">{items.map((item, index) => { const rType = String(item.resource_type ?? item.type ?? 'resource'); const title = String(item.title ?? item.name ?? t('search.untitledResource')); const snippet = String(item.summary ?? item.snippet ?? item.description ?? t('search.accessibleResource')); const href = String(item.url ?? resourceTypeToPath(rType)); return <Link className="search-result" to={href} key={String(item.id ?? index)}><span className="result-type">{resourceTypeLabel(rType)}</span><div><strong>{title}</strong><p>{snippet}</p>{Array.isArray(item.tags) && item.tags.length > 0 && <small>{item.tags.join(' · ')}</small>}</div><ArrowUpRight size={15} /></Link>; })}</div>{hasMore && <div className="search-load-more"><Button variant="ghost" onClick={loadMore} loading={loadingMore} icon={<ChevronDown size={14} />}>{t('search.loadMore')}</Button></div>}</> : <StateView state="empty" title={query ? t('search.noMatchingResources') : t('search.searchYourWorkspace')} description={query ? t('search.tryBroaderKeyword') : t('search.scopedToResources')} />}</Panel></>
}

const workflowStatusKeys: Record<string, MessageKey> = { draft: 'governance.status.draft', published: 'governance.status.published', queued: 'governance.status.queued', running: 'governance.status.running', pending_approval: 'governance.status.pending_approval', succeeded: 'governance.status.succeeded', failed: 'governance.status.failed', cancelled: 'governance.status.cancelled' }
const workflowNodeKeys: Record<string, MessageKey> = { input: 'workflow.node.input', start: 'workflow.node.input', knowledge_retrieval: 'workflow.node.retrieve', retrieve: 'workflow.node.retrieve', prompt: 'workflow.node.prompt', llm: 'workflow.node.generate', generate: 'workflow.node.generate', approval: 'workflow.node.approve', approve: 'workflow.node.approve', output: 'workflow.node.output', condition: 'workflow.node.condition', transform: 'workflow.node.transform', http_request: 'workflow.node.http', loop: 'workflow.node.loop', intent_classification: 'workflow.node.classifier', variable_aggregate: 'workflow.node.aggregate', code: 'workflow.node.code' }
const defaultWorkflowGraph: WorkflowGraph = { nodes: [{ id: 'input', type: 'input', config: {} }, { id: 'retrieve', type: 'knowledge_retrieval', config: {} }, { id: 'generate', type: 'llm', config: {} }, { id: 'output', type: 'output', config: {} }], edges: [{ source: 'input', target: 'retrieve' }, { source: 'retrieve', target: 'generate' }, { source: 'generate', target: 'output' }] }
function workflowStatus(t: (key: MessageKey) => string, value: unknown) { const raw = String(value ?? 'draft').toLowerCase(); return workflowStatusKeys[raw] ? t(workflowStatusKeys[raw]) : raw }
function workflowNodeLabel(t: (key: MessageKey) => string, value: unknown) { const raw = String(value ?? 'input').toLowerCase(); return workflowNodeKeys[raw] ? t(workflowNodeKeys[raw]) : raw }

function workflowGraph(value: unknown): WorkflowGraph {
  if (!value || typeof value !== 'object') return defaultWorkflowGraph
  const graph = value as Item
  return { nodes: Array.isArray(graph.nodes) && graph.nodes.length ? graph.nodes as Item[] : defaultWorkflowGraph.nodes, edges: Array.isArray(graph.edges) ? graph.edges as Item[] : defaultWorkflowGraph.edges }
}

function workflowIcon(type: string) {
  if (['knowledge_retrieval', 'retrieve'].includes(type)) return <Database size={16} />
  if (['llm', 'generate', 'prompt'].includes(type)) return <Sparkles size={16} />
  if (['approval', 'approve'].includes(type)) return <ShieldCheck size={16} />
  if (['output', 'answer'].includes(type)) return <Check size={16} />
  return <Zap size={16} />
}

export function WorkflowPage() {
  const { t } = useLocale()
  const { items, loading, error, reload } = useResource('/api/v1/workflows')
  const [selected, setSelected] = useState<Item | null>(null)
  const [graph, setGraph] = useState<WorkflowGraph>(defaultWorkflowGraph)
  const [nameDraft, setNameDraft] = useState('')
  const [descriptionDraft, setDescriptionDraft] = useState('')
  const [nodeId, setNodeId] = useState('')
  const [nodeConfig, setNodeConfig] = useState('{}')
  const [input, setInput] = useState('{"name":"Ada"}')
  const [dryRun, setDryRun] = useState(true)
  const [run, setRun] = useState<Item | null>(null)
  const [events, setEvents] = useState<Item[]>([])
  const [validation, setValidation] = useState<Item | null>(null)
  const [notice, setNotice] = useState('')
  const [runError, setRunError] = useState('')
  const [running, setRunning] = useState(false)
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState(false)
  const [newName, setNewName] = useState('')
  const [newDescription, setNewDescription] = useState('')
  const streamAbort = useRef<AbortController | null>(null)

  useEffect(() => { if (!selected && items[0]) setSelected(items[0]) }, [items, selected])
  useEffect(() => () => streamAbort.current?.abort(), [])
  useEffect(() => {
    if (!selected) return
    const nextGraph = workflowGraph(selected.graph)
    const firstNode = nextGraph.nodes[0]
    setGraph(nextGraph)
    setNameDraft(String(selected.name ?? ''))
    setDescriptionDraft(String(selected.description ?? ''))
    setNodeId(String(firstNode?.id ?? ''))
    setNodeConfig(JSON.stringify(firstNode?.config ?? {}, null, 2))
    setValidation(null)
  }, [selected])

  function selectNode(node: Item) {
    setNodeId(String(node.id ?? ''))
    setNodeConfig(JSON.stringify(node.config ?? {}, null, 2))
  }

  async function createWorkflow(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    try {
      const created = await api.post<Item>('/api/v1/workflows', { name: newName, description: newDescription, graph: defaultWorkflowGraph })
      setOpen(false)
      setNewName('')
      setNewDescription('')
      setNotice(t('workflow.created'))
      await reload()
      setSelected(created)
    } catch (caught) {
      setNotice(caught instanceof Error ? caught.message : t('workflow.createFailed'))
    } finally {
      setBusy(false)
    }
  }

  async function saveDraft(status: 'draft' | 'published' = 'draft') {
    if (!selected?.id) return
    setBusy(true)
    try {
      const updated = await api.request<Item>(`/api/v1/workflows/${encodeURIComponent(String(selected.id))}`, { method: 'PATCH', body: JSON.stringify({ name: nameDraft, description: descriptionDraft, graph, status }) })
      setSelected(updated)
      setNotice(status === 'published' ? t('workflow.published') : t('workflow.draftSaved'))
    } catch (caught) {
      setNotice(caught instanceof Error ? caught.message : t('workflow.saveFailed'))
    } finally {
      setBusy(false)
    }
  }

  async function validateWorkflow() {
    if (!selected?.id) return
    setBusy(true)
    try {
      const result = await api.post<Item>(`/api/v1/workflows/${encodeURIComponent(String(selected.id))}/validate`, {})
      setValidation(result)
      setNotice(result.valid ? t('workflow.valid') : t('workflow.invalid'))
    } catch (caught) {
      setNotice(caught instanceof Error ? caught.message : t('workflow.validationFailed'))
    } finally {
      setBusy(false)
    }
  }

  function saveNode(event: FormEvent) {
    event.preventDefault()
    try {
      const config = JSON.parse(nodeConfig) as unknown
      if (!config || typeof config !== 'object' || Array.isArray(config)) throw new Error(t('workflow.configObject'))
      setGraph((current) => ({ ...current, nodes: current.nodes.map((node) => String(node.id) === nodeId ? { ...node, config } : node) }))
      setNotice(t('workflow.nodeSaved'))
    } catch (caught) {
      setNotice(caught instanceof Error ? caught.message : t('workflow.configInvalid'))
    }
  }

  async function consumeEvents(runId: string) {
    streamAbort.current?.abort()
    const controller = new AbortController()
    streamAbort.current = controller
    try {
      const response = await api.stream(`/api/v1/workflow-runs/${encodeURIComponent(runId)}/events/stream?timeout_seconds=120`, { signal: controller.signal })
      if (!response.body) throw new Error(t('workflow.streamUnavailable'))
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      while (true) {
        const next = await reader.read()
        if (next.done) break
        buffer += decoder.decode(next.value, { stream: true })
        const frames = buffer.split('\n\n')
        buffer = frames.pop() ?? ''
        for (const frame of frames) {
          const data = frame.split('\n').find((line) => line.startsWith('data: '))?.slice(6)
          if (!data) continue
          const event = JSON.parse(data) as Item
          if (event.event_type) setEvents((current) => current.some((item) => String(item.seq) === String(event.seq)) ? current : [...current, event])
        }
      }
      setRun(await api.get<Item>(`/api/v1/workflow-runs/${encodeURIComponent(runId)}`))
    } catch (caught) {
      if (!(caught instanceof DOMException && caught.name === 'AbortError')) setRunError(caught instanceof Error ? caught.message : t('workflow.streamFailed'))
    } finally {
      setRunning(false)
    }
  }

  async function startRun(event: FormEvent) {
    event.preventDefault()
    if (!selected?.id) return
    setRunError('')
    setRunning(true)
    setEvents([])
    try {
      const parsed = JSON.parse(input) as unknown
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error(t('workflow.inputObject'))
      const response = await api.request<Item>(`/api/v1/workflows/${encodeURIComponent(String(selected.id))}/runs`, { method: 'POST', headers: { 'Idempotency-Key': `console-${String(selected.id)}-${Date.now()}` }, body: JSON.stringify({ input: parsed, dry_run: dryRun }) })
      setRun(response)
      void consumeEvents(String(response.id))
    } catch (caught) {
      setRunning(false)
      setRunError(caught instanceof Error ? caught.message : t('workflow.queueFailed'))
    }
  }

  async function cancelRun() {
    const operationId = run?.operation_id
    if (!operationId) return
    try {
      await api.post(`/api/v1/operations/${encodeURIComponent(String(operationId))}/cancellations`, { reason: 'Cancelled from workflow console.' })
      setNotice(t('workflow.cancelRequested'))
    } catch (caught) {
      setRunError(caught instanceof Error ? caught.message : t('workflow.cancelFailed'))
    }
  }

  const runStatus = String(run?.status ?? 'queued')
  const canCancel = Boolean(run?.operation_id) && ['queued', 'running', 'cancel_requested'].includes(runStatus)
  const selectedNode = graph.nodes.find((node) => String(node.id) === nodeId)
  const operationLabel = run?.operation_id ? `${t('workflow.operation')} ${String(run.operation_id)}` : t('workflow.operationPending')
  return <>
    <PageHeader eyebrow={t('workflow.eyebrow')} title={t('page.workflows')} description={t('workflow.description')} actions={<><Button icon={<RefreshCw size={15} />} onClick={() => void reload()}>{t('workflow.refresh')}</Button><Button variant="primary" icon={<Plus size={16} />} onClick={() => setOpen(true)}>{t('workflow.new')}</Button></>} />
    {notice && <Toast message={notice} onClose={() => setNotice('')} />}
    <div className="workflow-layout">
      <Panel title={t('workflow.library')} subtitle={`${items.length} ${t('workflow.count')}`}>
        {loading ? <StateView state="loading" /> : error && !items.length ? <StateView state="error" description={error} onRetry={() => void reload()} /> : !items.length ? <StateView state="empty" title={t('workflow.emptyTitle')} description={t('workflow.emptyDescription')} /> : <div className="workflow-list">{items.map((item) => { const rawStatus = String(item.status ?? 'draft'); return <button className={`workflow-row ${selected?.id === item.id ? 'selected' : ''}`} key={String(item.id)} data-testid="workflow-row" data-workflow-id={String(item.id)} data-workflow-name={String(item.name ?? '')} onClick={() => { setSelected(item); setRun(null); setEvents([]); setRunError('') }}><GitBranch size={16} /><span><strong>{String(item.name ?? t('workflow.fallbackName'))}</strong><small>{String(item.description ?? t('workflow.fallbackDescription'))}</small></span><Status value={workflowStatus(t, rawStatus)} toneValue={rawStatus} /></button> })}</div>}
      </Panel>
      <Panel title={nameDraft || t('workflow.select')} subtitle={t('workflow.editorSubtitle')}>
        {selected ? <>
          <div className="form-grid"><Field label={t('workflow.name')}><input value={nameDraft} onChange={(event) => setNameDraft(event.target.value)} /></Field><Field label={t('workflow.descriptionField')}><input value={descriptionDraft} onChange={(event) => setDescriptionDraft(event.target.value)} /></Field></div>
          <div className="button-row"><Button icon={<Check size={15} />} loading={busy} onClick={() => void saveDraft()}>{t('workflow.saveDraft')}</Button><Button icon={<ShieldCheck size={15} />} loading={busy} onClick={() => void validateWorkflow()}>{t('workflow.validate')}</Button><Button variant="primary" icon={<ArrowUpRight size={15} />} loading={busy} onClick={() => void saveDraft('published')}>{t('workflow.publish')}</Button></div>
          <div className="flow-canvas">{graph.nodes.map((node, index) => <Fragment key={String(node.id ?? index)}><FlowStep icon={workflowIcon(String(node.type ?? 'input'))} label={workflowNodeLabel(t, node.type)} selected={String(node.id) === nodeId} onClick={() => selectNode(node)} />{index < graph.nodes.length - 1 && <ChevronRight size={18} />}</Fragment>)}</div>
          <div className="workflow-footer"><span>{t('workflow.version')} <strong>v{String(selected.version ?? 1)}</strong></span><span>{t('workflow.approval')} <strong>{t('workflow.required')}</strong></span><span>{t('workflow.replay')} <strong>{t('workflow.enabled')}</strong></span></div>
          <form className="workflow-node-editor" onSubmit={saveNode}><div><strong>{workflowNodeLabel(t, selectedNode?.type)}</strong><small>{t('workflow.nodeConfigSubtitle')}</small></div><Field label={t('workflow.nodeConfig')}><textarea value={nodeConfig} onChange={(event) => setNodeConfig(event.target.value)} rows={5} spellCheck={false} /></Field><Button type="submit" icon={<Check size={15} />}>{t('workflow.saveNode')}</Button></form>
          {validation && <div className={`workflow-validation ${validation.valid ? 'valid' : 'invalid'}`} role="status"><ShieldCheck size={15} /><span>{validation.valid ? t('workflow.valid') : `${t('workflow.invalid')}: ${Array.isArray(validation.errors) ? validation.errors.join('; ') : t('workflow.validationFailed')}`}</span></div>}
          <form className="workflow-run-controls" onSubmit={startRun}><Field label={dryRun ? t('workflow.testInput') : t('workflow.runInput')}><textarea value={input} onChange={(event) => setInput(event.target.value)} aria-label={t('workflow.runInput')} data-testid="workflow-run-input" rows={3} /></Field><label className="check-line"><input type="checkbox" checked={dryRun} onChange={(event) => setDryRun(event.target.checked)} data-testid="workflow-dry-run-checkbox" />{t('workflow.dryRun')}</label><div className="workflow-run-actions"><Button type="submit" variant="primary" icon={<Play size={15} />} loading={running} data-testid="workflow-run-button">{dryRun ? t('workflow.testRun') : t('workflow.runObserve')}</Button>{canCancel && <Button type="button" variant="danger" icon={<Square size={14} />} onClick={() => void cancelRun()} data-testid="workflow-cancel-run-button">{t('workflow.cancelRun')}</Button>}</div></form>
          {runError && <div className="workflow-run-error" role="alert"><AlertCircle size={15} />{runError}</div>}
          {run && <div className="workflow-run-summary" data-testid="workflow-run-summary" data-run-id={String(run.id)} data-run-status={runStatus}><div><span>{t('workflow.latestRun')}</span><strong data-testid="workflow-run-id">{String(run.id)}</strong></div><span data-testid="workflow-run-status"><Status value={workflowStatus(t, runStatus)} toneValue={runStatus} /></span><small data-testid="workflow-run-operation">{operationLabel}</small></div>}
          {events.length > 0 && <div className="workflow-event-list" data-testid="workflow-event-list"><div className="workflow-event-heading"><span><Radio size={14} />{t('workflow.liveStream')}</span><small>{events.length} {t('workflow.events')}</small></div>{events.map((event, index) => <div className="workflow-event" key={`${String(event.id ?? event.seq ?? index)}`} data-testid="workflow-event" data-event-type={String(event.event_type)}><ListChecks size={14} /><div><strong>{String(event.event_type ?? t('workflow.event'))}</strong><small>{String((event.payload as Item | undefined)?.node_id ?? (event.payload as Item | undefined)?.status ?? t('workflow.runEvidence'))}</small></div><code>#{String(event.seq ?? index + 1)}</code></div>)}</div>}
        </> : <StateView state="empty" title={t('workflow.select')} description={t('workflow.selectDescription')} />}
      </Panel>
    </div>
    {open && <Modal title={t('workflow.createTitle')} onClose={() => setOpen(false)}><form className="form-stack" onSubmit={createWorkflow}><Field label={t('workflow.name')}><input value={newName} onChange={(event) => setNewName(event.target.value)} required placeholder={t('workflow.namePlaceholder')} /></Field><Field label={t('workflow.descriptionField')}><textarea value={newDescription} onChange={(event) => setNewDescription(event.target.value)} placeholder={t('workflow.descriptionPlaceholder')} /></Field><div className="callout"><ShieldCheck size={16} /><span>{t('workflow.createBoundary')}</span></div><Button type="submit" variant="primary" loading={busy}>{t('workflow.create')}</Button></form></Modal>}
  </>
}

function FlowStep({ icon, label, selected = false, onClick }: { icon: ReactNode; label: string; selected?: boolean; onClick?: () => void }) { const { t } = useLocale(); return <button type="button" className={`flow-step ${selected ? 'selected' : ''}`} aria-pressed={selected} onClick={onClick}>{icon}<strong>{label}</strong><small>{t('workflow.governedNode')}</small></button> }

export function DesignPage() {
  const { t } = useLocale()
  const projects = useResource('/api/v1/design/projects')
  const [selectedProjectId, setSelectedProjectId] = useState('')
  const [project, setProject] = useState<Item | null>(null)
  const [assets, setAssets] = useState<Item[]>([])
  const [selectedAsset, setSelectedAsset] = useState<Item | null>(null)
  const [lastJob, setLastJob] = useState<Item | null>(null)
  const [brief, setBrief] = useState('Create a clear approval workflow for a B2B AI operations platform.')
  const [sourceRefs, setSourceRefs] = useState('mock://source/workama/brief')
  const [operation, setOperation] = useState('generate')
  const [outputFormat, setOutputFormat] = useState('png')
  const [canvasWidth, setCanvasWidth] = useState('1440')
  const [canvasHeight, setCanvasHeight] = useState('900')
  const [projectOpen, setProjectOpen] = useState(false)
  const [newProjectName, setNewProjectName] = useState('')
  const [newProjectDescription, setNewProjectDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [projectLoading, setProjectLoading] = useState(false)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewUrl, setPreviewUrl] = useState('')
  const [previewText, setPreviewText] = useState('')
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (!selectedProjectId && projects.items[0]?.id) setSelectedProjectId(String(projects.items[0].id))
  }, [projects.items, selectedProjectId])

  const loadProject = useCallback(async (projectId: string) => {
    setProjectLoading(true)
    setError('')
    try {
      const [nextProject, nextAssets] = await Promise.all([
        api.get<Item>(`/api/v1/design/projects/${encodeURIComponent(projectId)}`),
        api.get<{ items?: Item[] }>(`/api/v1/design/projects/${encodeURIComponent(projectId)}/assets`),
      ])
      const nextItems = resourceItems(nextAssets.items)
      setProject(nextProject)
      setAssets(nextItems)
      setSelectedAsset((current) => current && nextItems.some((item) => item.id === current.id) ? current : nextItems[0] ?? null)
      setBrief(String(nextProject.description ?? ''))
      setCanvasWidth(String(nextProject.canvas_width ?? 1440))
      setCanvasHeight(String(nextProject.canvas_height ?? 900))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : t('design.projectDetailsFailed'))
      setProject(null)
      setAssets([])
      setSelectedAsset(null)
    } finally { setProjectLoading(false) }
  }, [])

  useEffect(() => {
    if (selectedProjectId) void loadProject(selectedProjectId)
    else { setProject(null); setAssets([]); setSelectedAsset(null) }
  }, [loadProject, selectedProjectId])

  useEffect(() => {
    if (!selectedAsset?.artifact_ref) {
      setPreviewUrl('')
      setPreviewText('')
      return
    }
    let disposed = false
    setPreviewLoading(true)
    setPreviewUrl('')
    setPreviewText('')
    void api.download(`/api/v1/design/artifacts/download?artifact_ref=${encodeURIComponent(String(selectedAsset.artifact_ref))}`).then(async (blob) => {
      if (disposed) return
      if (String(selectedAsset.content_type ?? '').startsWith('image/')) setPreviewUrl(URL.createObjectURL(blob))
      else setPreviewText(await blob.text())
    }).catch((caught) => { if (!disposed) setError(caught instanceof Error ? caught.message : t('design.artifactPreviewFailed')) }).finally(() => { if (!disposed) setPreviewLoading(false) })
    return () => { disposed = true }
  }, [selectedAsset])

  useEffect(() => () => { if (previewUrl) URL.revokeObjectURL(previewUrl) }, [previewUrl])

  async function createProject(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    try {
      const created = await api.post<Item>('/api/v1/design/projects', { name: newProjectName, description: newProjectDescription, canvas_width: 1440, canvas_height: 900 })
      setProjectOpen(false)
      setNewProjectName('')
      setNewProjectDescription('')
      setSelectedProjectId(String(created.id))
      await projects.reload()
      await loadProject(String(created.id))
      setNotice(t('design.projectCreatedNotice'))
    } catch (caught) { setError(caught instanceof Error ? caught.message : t('design.projectCreationFailed')) }
    finally { setBusy(false) }
  }

  async function saveProject() {
    if (!selectedProjectId) return
    setBusy(true)
    try {
      await api.patch(`/api/v1/design/projects/${encodeURIComponent(selectedProjectId)}`, { description: brief, canvas_width: Number(canvasWidth), canvas_height: Number(canvasHeight) })
      await projects.reload()
      await loadProject(selectedProjectId)
      setNotice(t('design.directionSaved'))
    } catch (caught) { setError(caught instanceof Error ? caught.message : t('design.directionSaveFailed')) }
    finally { setBusy(false) }
  }

  async function generateDesign() {
    if (!selectedProjectId || !brief.trim()) return
    setBusy(true)
    setError('')
    try {
      const refs = sourceRefs.split(/\r?\n|,/).map((value) => value.trim()).filter(Boolean)
      const parentAssetIds = operation === 'edit' && selectedAsset?.id ? [String(selectedAsset.id)] : []
      const result = await api.post<Item>(`/api/v1/design/projects/${encodeURIComponent(selectedProjectId)}/jobs`, { operation, prompt: brief, source_refs: refs, parent_asset_ids: parentAssetIds, output_format: outputFormat, idempotency_key: `web-design-${Date.now()}` })
      setLastJob(result)
      await loadProject(selectedProjectId)
      setNotice(t('design.artifactGenerated'))
    } catch (caught) { setError(caught instanceof Error ? caught.message : t('design.generationFailed')) }
    finally { setBusy(false) }
  }

  function inspectAsset(item: Item) { setSelectedAsset(item) }

  async function downloadAsset() {
    if (!selectedAsset?.artifact_ref) return
    try {
      const blob = await api.download(`/api/v1/design/artifacts/download?artifact_ref=${encodeURIComponent(String(selectedAsset.artifact_ref))}`)
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = String(selectedAsset.name ?? 'workama-design-artifact')
      anchor.click()
      URL.revokeObjectURL(url)
    } catch (caught) { setError(caught instanceof Error ? caught.message : t('design.artifactDownloadFailed')) }
  }

  const selectedProject = project ?? projects.items.find((item) => String(item.id) === selectedProjectId) ?? null
  const credentialStatus = selectedAsset?.provenance && typeof selectedAsset.provenance === 'object' ? String((selectedAsset.provenance as Item).content_credentials && typeof (selectedAsset.provenance as Item).content_credentials === 'object' ? ((selectedAsset.provenance as Item).content_credentials as Item).signature_status ?? 'unknown' : 'unknown') : '--'
  return <><PageHeader eyebrow={t('design.eyebrow')} title={t('page.design')} description={t('design.description')} actions={<><Button icon={<RefreshCw size={15} />} onClick={() => void projects.reload()}>{t('design.refresh')}</Button><Button variant="primary" icon={<Plus size={16} />} onClick={() => setProjectOpen(true)}>{t('design.newProject')}</Button></>} />{notice && <Toast message={notice} onClose={() => setNotice('')} />}{(error || projects.error) && <div className="alert alert-error" role="alert">{error || projects.error}<Button variant="ghost" onClick={() => { setError(''); if (selectedProjectId) void loadProject(selectedProjectId) }}>{t('ui.retry')}</Button></div>}<div className="kpi-grid"><Kpi label={t('design.designProjects')} value={String(projects.items.length).padStart(2, '0')} icon={<ImageIcon size={18} />} trend={t('design.workspaceScoped')} /><Kpi label={t('design.generatedAssets')} value={String(assets.length).padStart(2, '0')} icon={<Database size={18} />} trend={selectedProject ? String(selectedProject.name ?? t('design.selectedProject')) : t('design.selectProject')} /><Kpi label={t('design.canvas')} value={selectedProject ? `${canvasWidth}×${canvasHeight}` : '--'} icon={<GitBranch size={18} />} trend={t('design.savedProjectBounds')} /><Kpi label={t('design.credentialState')} value={credentialStatus} icon={<ShieldCheck size={18} />} trend={t('design.detachedClaimVisible')} /></div><div className="design-layout"><Panel title={t('design.projectLibrary')} subtitle={t('design.workspaceProjectsCount').replace('{count}', String(projects.items.length))} actions={<Button variant="ghost" icon={<Plus size={14} />} onClick={() => setProjectOpen(true)}>{t('design.new')}</Button>}>{projects.loading ? <StateView state="loading" /> : !projects.items.length ? <StateView state="empty" title={t('design.noDesignProjects')} description={t('design.createProjectToStartCanvas')} /> : <div className="design-project-list">{projects.items.map((item, index) => <button className={`design-project-row ${String(item.id) === selectedProjectId ? 'selected' : ''}`} key={String(item.id ?? index)} onClick={() => setSelectedProjectId(String(item.id))}><ImageIcon size={16} /><span><strong>{String(item.name ?? t('design.untitledProject'))}</strong><small>{String(item.description ?? t('design.noProjectBrief'))}</small></span><Status value={String(item.status ?? 'active')} /></button>)}</div>}</Panel><div className="design-main"><Panel title={String(selectedProject?.name ?? t('design.selectDesignProject'))} subtitle={selectedProject ? `${String(selectedProject.slug ?? selectedProject.id)} · v${String(selectedProject.version ?? 1)}` : t('design.projectRequiredBeforeArtifact')} actions={selectedProject && <div className="button-row"><Button icon={<Check size={14} />} loading={busy} onClick={() => void saveProject()}>{t('design.saveDirection')}</Button><Button variant="primary" icon={<Sparkles size={14} />} loading={busy} disabled={projectLoading} onClick={() => void generateDesign()}>{t('design.generateDirection')}</Button></div>}><div className="design-workspace"><aside className="design-inspector"><Field label={t('design.designBrief')}><textarea aria-label={t('design.designBrief')} value={brief} onChange={(event) => setBrief(event.target.value)} rows={7} disabled={!selectedProject} /></Field><div className="form-grid"><Field label={t('design.canvasWidth')}><input type="number" min="1" max="8192" value={canvasWidth} onChange={(event) => setCanvasWidth(event.target.value)} disabled={!selectedProject} /></Field><Field label={t('design.canvasHeight')}><input type="number" min="1" max="8192" value={canvasHeight} onChange={(event) => setCanvasHeight(event.target.value)} disabled={!selectedProject} /></Field></div><Field label={t('design.operation')}><select value={operation} onChange={(event) => setOperation(event.target.value)} disabled={!selectedProject}><option value="generate">{t('design.operationGenerate')}</option><option value="prototype">{t('design.operationPrototype')}</option><option value="edit">{t('design.operationEdit')}</option></select></Field><Field label={t('design.outputFormat')}><select value={outputFormat} onChange={(event) => setOutputFormat(event.target.value)} disabled={!selectedProject}><option value="png">PNG</option><option value="jpeg">JPEG</option><option value="svg">SVG</option><option value="json">JSON</option></select></Field><Field label={t('design.controlledSourceRefs')} hint={t('design.sourceRefsHint')}><textarea value={sourceRefs} onChange={(event) => setSourceRefs(event.target.value)} rows={3} disabled={!selectedProject} /></Field>{operation === 'edit' && <div className="callout"><GitBranch size={15} /><span>{selectedAsset ? t('design.editClaimsParent').replace('{id}', String(selectedAsset.id)) : t('design.selectAssetBeforeEdit')}</span></div>}{selectedProject && <Button variant="primary" icon={<Sparkles size={15} />} loading={busy} disabled={!brief.trim() || (operation === 'edit' && !selectedAsset)} onClick={() => void generateDesign()}>{t('design.runOperation').replace('{operation}', operation)}</Button>}</aside><section className="design-canvas" style={{ padding: 0, background: '#fff' }}>{selectedProject ? <CanvasEditor projectName={String(selectedProject.name ?? t('design.untitledProject'))} canvasWidth={Number(canvasWidth) || 1440} canvasHeight={Number(canvasHeight) || 900} /> : <><div className="canvas-toolbar"><span><span className="live-dot" />{t('design.projectRequiredBeforeArtifact')}</span><Badge tone="info">{t('design.ready')}</Badge></div><StateView state="empty" title={t('design.noGeneratedArtifact')} description={t('design.runControlledOperation')} /></>}</section></div></Panel><div className="design-lower-grid"><Panel title={t('design.generatedAssets')} subtitle={t('design.generatedAssetsSubtitle')}>{assets.length ? <div className="design-asset-list">{assets.map((item, index) => <button className={`design-asset-row ${selectedAsset?.id === item.id ? 'selected' : ''}`} key={String(item.id ?? index)} onClick={() => void inspectAsset(item)}><ImageIcon size={16} /><span><strong>{String(item.name ?? t('design.designAsset'))}</strong><small>{String(item.kind ?? '--')} · {String(item.content_type ?? '--')}</small></span><Status value={String(item.status ?? 'ready')} /></button>)}</div> : <StateView state="empty" title={t('design.noAssets')} description={t('design.noAssetsDescription')} />}</Panel><Panel title={lastJob ? t('design.latestGeneration') : t('design.provenance')} subtitle={lastJob ? String(lastJob.id ?? t('design.designJob')) : selectedAsset ? t('design.verifiedMetadataForAsset') : t('design.selectAssetToInspect')}>{lastJob ? <div className="design-job-summary"><Status value={String(lastJob.status ?? 'unknown')} /><strong>{String(lastJob.operation ?? 'generate')}</strong><small>{String(lastJob.output_format ?? outputFormat)} · {String(lastJob.artifact_ref ?? '--')}</small><code>{String(lastJob.content_sha256 ?? '--')}</code></div> : selectedAsset ? <details open className="ops-json"><summary>{t('design.provenanceManifest')}</summary><pre tabIndex={0}>{resourceJson(selectedAsset.provenance ?? selectedAsset)}</pre></details> : <StateView state="empty" title={t('design.noAssetSelected')} description={t('design.chooseAssetToReview')} />}</Panel></div></div></div>{projectOpen && <Modal title={t('design.createDesignProject')} onClose={() => setProjectOpen(false)}><form className="form-stack" onSubmit={createProject}><Field label={t('design.projectName')}><input value={newProjectName} onChange={(event) => setNewProjectName(event.target.value)} required placeholder={t('design.projectNamePlaceholder')} /></Field><Field label={t('design.descriptionField')}><textarea value={newProjectDescription} onChange={(event) => setNewProjectDescription(event.target.value)} placeholder={t('design.descriptionPlaceholder')} /></Field><div className="callout"><ShieldCheck size={15} /><span>{t('design.generatedAssetsScopedNotice')}</span></div><Button type="submit" variant="primary" loading={busy}>{t('design.createProject')}</Button></form></Modal>}</>
}
