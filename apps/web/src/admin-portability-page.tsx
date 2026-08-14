/**
 * G3 工作区可移植性控制台（/admin/portability）。
 *
 * 全部数据来自真实后端，无任何本地伪造：
 *  - GET  /api/v1/workspace                                        当前工作区
 *  - GET  /api/v1/admin/operations                                 异步操作账本（作业清单来源）
 *  - POST /api/v1/workspaces/{id}/exports                          发起导出
 *  - GET  /api/v1/workspace-exports/{id}                           导出详情 + 清单
 *  - GET  /api/v1/workspace-exports/{id}/content                   下载数据包
 *  - POST /api/v1/workspace-imports/uploads                        创建导入并取得上传通道
 *  - POST /api/v1/workspace-imports/uploads/{uploadId}/content     上传数据包
 *  - POST /api/v1/workspace-imports/uploads/{uploadId}/complete    校验和确认
 *  - POST /api/v1/workspace-imports/{id}/dry-runs                  试运行
 *  - POST /api/v1/workspace-imports/{id}/applications              确认执行
 *  - GET  /api/v1/workspace-imports/{id}                           导入详情 + 差异报告 + 执行结果
 *
 * 后端没有导出/导入的列表端点，因此作业清单由 admin/operations 中
 * workspace.export / workspace.import.* 操作的 idempotency_key 反解资源 ID，
 * 再逐条读取真实详情组装，而不是在前端缓存或编造记录。
 */
import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { Download, FileJson2, GitCompareArrows, PlayCircle, Plus, RefreshCw, Upload } from 'lucide-react'
import { api } from './api'
import { useLocale } from './locale'
import {
  JsonDetails,
  MetaList,
  Notice,
  StateGate,
  displayBytes,
  displayDate,
  errorText,
  shortId,
  textOf,
  type Row,
} from './admin-ops-shared'
import { Badge, Button, DataTable, Field, Kpi, Modal, PageHeader, Panel, Status } from './ui'

type Operation = {
  id: string
  operation_type?: string
  idempotency_key?: string
  status?: string
  progress?: number
  attempt_count?: number
  max_attempts?: number
  error_code?: string | null
  error_message?: string | null
  created_at?: string
}

type ExportRow = Row & {
  id: string
  status?: string
  object_ref?: string
  checksum?: string
  size_bytes?: number
  manifest?: Row | null
  created_at?: string
  completed_at?: string | null
  expires_at?: string | null
}

type ImportRow = Row & {
  id: string
  status?: string
  upload_id?: string
  upload_checksum?: string
  manifest?: Row | null
  dry_run_report?: Row | null
  result_summary?: Row | null
  created_at?: string
  uploaded_at?: string | null
  completed_at?: string | null
}

type ExportJob = { operation: Operation; detail: ExportRow | null }
type ImportJob = { operation: Operation; detail: ImportRow | null }

const OPERATIONS_ENDPOINT = '/api/v1/admin/operations?limit=200'
const POLL_INTERVAL_MS = 1500
const POLL_MAX_TICKS = 40

function countResources(counts: unknown): number {
  if (!counts || typeof counts !== 'object') return 0
  return Object.values(counts as Record<string, unknown>).reduce<number>(
    (total, value) => total + (Number(value) || 0),
    0,
  )
}

function entriesOf(value: unknown): Array<[string, number]> {
  if (!value || typeof value !== 'object') return []
  return Object.entries(value as Record<string, unknown>).map(([key, raw]) => [key, Number(raw) || 0])
}

function stringEntriesOf(value: unknown): Array<[string, string]> {
  if (!value || typeof value !== 'object') return []
  return Object.entries(value as Record<string, unknown>).map(([key, raw]) => [key, String(raw)])
}

/** 从 idempotency_key 反解导入 ID：形如 `imp_xxx-req_yyy`。 */
function importIdFromKey(key: string): string {
  const marker = key.indexOf('-req_')
  return marker > 0 ? key.slice(0, marker) : key
}

async function sha256Hex(buffer: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', buffer)
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('')
}

export default function AdminPortabilityPage(): ReactNode {
  const { t } = useLocale()
  const [workspaceId, setWorkspaceId] = useState('')
  const [exportJobs, setExportJobs] = useState<ExportJob[]>([])
  const [importJobs, setImportJobs] = useState<ImportJob[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')

  const [exportOpen, setExportOpen] = useState(false)
  const [includeHistory, setIncludeHistory] = useState(true)
  const [importOpen, setImportOpen] = useState(false)
  const [packageFile, setPackageFile] = useState<File | null>(null)
  const [manifestTarget, setManifestTarget] = useState<ExportRow | null>(null)
  const [diffTarget, setDiffTarget] = useState<ImportRow | null>(null)
  const [applyTarget, setApplyTarget] = useState<ImportRow | null>(null)
  const [applyConfirm, setApplyConfirm] = useState('')

  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const reload = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const workspace = await api.get<{ id?: string }>('/api/v1/workspace')
      const nextWorkspaceId = String(workspace?.id ?? '')
      const operations = await api.get<{ items?: Operation[] }>(OPERATIONS_ENDPOINT)
      const items = Array.isArray(operations?.items) ? operations.items : []

      const exportOps = items.filter((item) => item.operation_type === 'workspace.export')
      const importOps = items.filter((item) => String(item.operation_type ?? '').startsWith('workspace.import'))

      const seenImports = new Set<string>()
      const importPairs: Array<{ operation: Operation; importId: string }> = []
      for (const operation of importOps) {
        const importId = importIdFromKey(String(operation.idempotency_key ?? ''))
        if (!importId.startsWith('imp_') || seenImports.has(importId)) continue
        seenImports.add(importId)
        importPairs.push({ operation, importId })
      }

      const [exportDetails, importDetails] = await Promise.all([
        Promise.all(
          exportOps.map(async (operation) => {
            const exportId = String(operation.idempotency_key ?? '')
            if (!exportId.startsWith('exp_')) return { operation, detail: null }
            try {
              return { operation, detail: await api.get<ExportRow>(`/api/v1/workspace-exports/${encodeURIComponent(exportId)}`) }
            } catch {
              return { operation, detail: null }
            }
          }),
        ),
        Promise.all(
          importPairs.map(async ({ operation, importId }) => {
            try {
              return { operation, detail: await api.get<ImportRow>(`/api/v1/workspace-imports/${encodeURIComponent(importId)}`) }
            } catch {
              return { operation, detail: null }
            }
          }),
        ),
      ])

      if (!mounted.current) return
      setWorkspaceId(nextWorkspaceId)
      setExportJobs(exportDetails)
      setImportJobs(importDetails)
    } catch (caught) {
      if (mounted.current) setError(errorText(caught, t))
    } finally {
      if (mounted.current) setLoading(false)
    }
  }, [t])

  useEffect(() => {
    void reload()
  }, [reload])

  /** 轮询真实资源直到达到终态；返回最终一次读到的行。 */
  async function pollUntil<T extends Row>(endpoint: string, isTerminal: (row: T) => boolean): Promise<T | null> {
    for (let tick = 0; tick < POLL_MAX_TICKS; tick += 1) {
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS))
      if (!mounted.current) return null
      try {
        const row = await api.get<T>(endpoint)
        if (isTerminal(row)) return row
      } catch {
        return null
      }
    }
    return null
  }

  async function submitExport(event: FormEvent) {
    event.preventDefault()
    if (!workspaceId) {
      setNotice(t('portability.workspaceUnavailable'))
      return
    }
    setBusy(true)
    try {
      const created = await api.post<{ id: string }>(
        `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/exports`,
        { include_history: includeHistory },
      )
      setExportOpen(false)
      setNotice(t('portability.exportQueuedNotice'))
      await pollUntil<ExportRow>(
        `/api/v1/workspace-exports/${encodeURIComponent(created.id)}`,
        (row) => row.status === 'completed' || row.status === 'failed',
      )
      if (mounted.current) {
        setNotice(t('portability.exportCompletedNotice'))
        await reload()
      }
    } catch (caught) {
      setNotice(errorText(caught, t))
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  async function downloadExport(row: ExportRow) {
    if (row.status !== 'completed') {
      setNotice(t('portability.exportNotReady'))
      return
    }
    setBusy(true)
    try {
      const blob = await api.download(`/api/v1/workspace-exports/${encodeURIComponent(row.id)}/content`)
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `${row.id}.json`
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      URL.revokeObjectURL(url)
      setNotice(t('portability.downloadStartedNotice'))
    } catch (caught) {
      setNotice(errorText(caught, t))
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  async function submitImport(event: FormEvent) {
    event.preventDefault()
    if (!packageFile) {
      setNotice(t('portability.selectFileFirst'))
      return
    }
    if (!globalThis.crypto?.subtle) {
      setNotice(t('portability.hashUnavailable'))
      return
    }
    setBusy(true)
    try {
      const checksum = await sha256Hex(await packageFile.arrayBuffer())
      const prepared = await api.post<{ id: string; upload_id: string }>('/api/v1/workspace-imports/uploads')
      await api.upload<void>(
        `/api/v1/workspace-imports/uploads/${encodeURIComponent(prepared.upload_id)}/content`,
        packageFile,
      )
      await api.post(
        `/api/v1/workspace-imports/uploads/${encodeURIComponent(prepared.upload_id)}/complete`,
        { sha256: checksum },
      )
      setImportOpen(false)
      setPackageFile(null)
      setNotice(t('portability.uploadCompletedNotice'))
      await reload()
    } catch (caught) {
      setNotice(errorText(caught, t))
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  async function runDryRun(row: ImportRow) {
    setBusy(true)
    try {
      await api.post(`/api/v1/workspace-imports/${encodeURIComponent(row.id)}/dry-runs`)
      setNotice(t('portability.dryRunQueuedNotice'))
      await pollUntil<ImportRow>(
        `/api/v1/workspace-imports/${encodeURIComponent(row.id)}`,
        (current) => current.status === 'dry_run_ready' || current.status === 'invalid',
      )
      if (mounted.current) await reload()
    } catch (caught) {
      setNotice(errorText(caught, t))
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  async function confirmApply(event: FormEvent) {
    event.preventDefault()
    if (!applyTarget) return
    if (applyConfirm.trim().toUpperCase() !== 'APPLY') {
      setNotice(t('portability.confirmMismatch'))
      return
    }
    setBusy(true)
    try {
      await api.post(`/api/v1/workspace-imports/${encodeURIComponent(applyTarget.id)}/applications`, { confirm: true })
      setApplyTarget(null)
      setApplyConfirm('')
      setNotice(t('portability.applyQueuedNotice'))
      await pollUntil<ImportRow>(
        `/api/v1/workspace-imports/${encodeURIComponent(applyTarget.id)}`,
        (current) => current.status === 'completed' || current.status === 'failed',
      )
      if (mounted.current) await reload()
    } catch (caught) {
      setNotice(errorText(caught, t))
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  const completedExports = exportJobs.filter((job) => job.detail?.status === 'completed')
  const latestSize = completedExports[0]?.detail?.size_bytes
  const pendingApply = importJobs.filter((job) => job.detail?.status === 'dry_run_ready').length

  return (
    <>
      <PageHeader
        eyebrow={t('portability.eyebrow')}
        title={t('page.portability')}
        description={t('portability.description')}
        actions={
          <>
            <Button icon={<RefreshCw size={15} />} onClick={() => void reload()} data-testid="portability-refresh">
              {t('portability.refresh')}
            </Button>
            <Button
              icon={<Upload size={15} />}
              onClick={() => setImportOpen(true)}
              data-testid="portability-new-import"
            >
              {t('portability.newImport')}
            </Button>
            <Button
              variant="primary"
              icon={<Plus size={15} />}
              onClick={() => setExportOpen(true)}
              disabled={!workspaceId}
              data-testid="portability-new-export"
            >
              {t('portability.newExport')}
            </Button>
          </>
        }
      />
      <Notice notice={notice} clear={() => setNotice('')} />

      <div className="kpi-grid">
        <Kpi
          label={t('portability.kpiExports')}
          value={loading ? '--' : String(exportJobs.length).padStart(2, '0')}
          icon={<Download size={18} />}
          trend={t('portability.kpiExportsTrend')}
        />
        <Kpi
          label={t('portability.kpiImports')}
          value={loading ? '--' : String(importJobs.length).padStart(2, '0')}
          icon={<Upload size={18} />}
          trend={t('portability.kpiImportsTrend')}
        />
        <Kpi
          label={t('portability.kpiPackageSize')}
          value={latestSize === undefined ? '--' : displayBytes(latestSize)}
          icon={<FileJson2 size={18} />}
          trend={t('portability.kpiPackageSizeTrend')}
        />
        <Kpi
          label={t('portability.kpiPendingApply')}
          value={loading ? '--' : String(pendingApply).padStart(2, '0')}
          icon={<GitCompareArrows size={18} />}
          trend={t('portability.kpiPendingApplyTrend')}
        />
      </div>

      <Panel
        title={t('portability.exportsTitle')}
        subtitle={t('portability.exportsSubtitle')}
        className="portability-panel"
      >
        <StateGate loading={loading} error={error} empty={!exportJobs.length} retry={() => void reload()}>
          <DataTable
            caption={t('portability.exportsTitle')}
            headers={[
              t('portability.colJob'),
              t('portability.colStatus'),
              t('portability.colResources'),
              t('portability.colSize'),
              t('portability.colChecksum'),
              t('portability.colCreated'),
              t('portability.colActions'),
            ]}
          >
            {exportJobs.map((job) => {
              const detail = job.detail
              const manifest = (detail?.manifest ?? null) as Row | null
              const resourceCount = countResources(manifest?.resource_counts)
              const operationStatus = String(job.operation.status ?? '')
              return (
                <tr key={job.operation.id} data-testid="portability-export-row">
                  <td>
                    <strong>{shortId(detail?.id ?? job.operation.idempotency_key)}</strong>
                    <small className="table-subtext">
                      {t('portability.operationStatus')}: {operationStatus || '—'} ·{' '}
                      {t('portability.attempts')} {job.operation.attempt_count ?? 0}/{job.operation.max_attempts ?? 0}
                    </small>
                    {job.operation.error_message && (
                      <small className="ops-error-cell">
                        {t('portability.errorLabel')}: {job.operation.error_message}
                      </small>
                    )}
                  </td>
                  <td>
                    <Status value={textOf(detail, 'status', operationStatus || '—')} toneValue={detail?.status ?? operationStatus} />
                    <small className="table-subtext">
                      <span className="ops-progress" aria-hidden="true">
                        <i style={{ width: `${Math.max(0, Math.min(100, Number(job.operation.progress ?? 0)))}%` }} />
                      </span>
                      {t('portability.progress')} {Number(job.operation.progress ?? 0)}%
                    </small>
                  </td>
                  <td>{resourceCount || '—'}</td>
                  <td>{displayBytes(detail?.size_bytes)}</td>
                  <td>
                    <code>{shortId(detail?.checksum, 8)}</code>
                  </td>
                  <td>{displayDate(detail?.created_at ?? job.operation.created_at)}</td>
                  <td>
                    <span className="button-row">
                      <Button
                        variant="ghost"
                        disabled={!manifest}
                        onClick={() => detail && setManifestTarget(detail)}
                        data-testid={`portability-manifest-${job.operation.id}`}
                      >
                        {t('portability.viewManifest')}
                      </Button>
                      <Button
                        variant="ghost"
                        icon={<Download size={14} />}
                        loading={busy}
                        disabled={detail?.status !== 'completed'}
                        onClick={() => detail && void downloadExport(detail)}
                        data-testid={`portability-download-${job.operation.id}`}
                      >
                        {t('portability.download')}
                      </Button>
                    </span>
                  </td>
                </tr>
              )
            })}
          </DataTable>
          <p className="admin-note">{t('portability.historyNote')}</p>
        </StateGate>
      </Panel>

      <Panel title={t('portability.importsTitle')} subtitle={t('portability.importsSubtitle')}>
        <StateGate loading={loading} error={error} empty={!importJobs.length} retry={() => void reload()}>
          <DataTable
            caption={t('portability.importsTitle')}
            headers={[
              t('portability.colJob'),
              t('portability.colStatus'),
              t('portability.colValidation'),
              t('portability.colChecksum'),
              t('portability.colCreated'),
              t('portability.colActions'),
            ]}
          >
            {importJobs.map((job) => {
              const detail = job.detail
              const report = (detail?.dry_run_report ?? null) as Row | null
              const hasReport = !!report && Object.keys(report).length > 0
              const valid = report?.valid === true
              return (
                <tr key={job.operation.id} data-testid="portability-import-row">
                  <td>
                    <strong>{shortId(detail?.id ?? importIdFromKey(String(job.operation.idempotency_key ?? '')))}</strong>
                    <small className="table-subtext">
                      {t('portability.operationStatus')}: {textOf(job.operation as Row, 'status')} ·{' '}
                      {t('portability.attempts')} {job.operation.attempt_count ?? 0}/{job.operation.max_attempts ?? 0}
                    </small>
                    {job.operation.error_message && (
                      <small className="ops-error-cell">
                        {t('portability.errorLabel')}: {job.operation.error_message}
                      </small>
                    )}
                  </td>
                  <td>
                    <Status value={textOf(detail, 'status')} toneValue={detail?.status} />
                  </td>
                  <td>
                    {hasReport ? (
                      <Badge tone={valid ? 'success' : 'danger'}>
                        {valid ? t('portability.diffValid') : t('portability.diffInvalid')}
                      </Badge>
                    ) : (
                      '—'
                    )}
                  </td>
                  <td>
                    <code>{shortId(detail?.upload_checksum, 8)}</code>
                  </td>
                  <td>{displayDate(detail?.created_at ?? job.operation.created_at)}</td>
                  <td>
                    <span className="button-row">
                      <Button
                        variant="ghost"
                        icon={<PlayCircle size={14} />}
                        loading={busy}
                        disabled={!detail}
                        onClick={() => detail && void runDryRun(detail)}
                        data-testid={`portability-dryrun-${job.operation.id}`}
                      >
                        {t('portability.runDryRun')}
                      </Button>
                      <Button
                        variant="ghost"
                        disabled={!hasReport}
                        onClick={() => detail && setDiffTarget(detail)}
                        data-testid={`portability-diff-${job.operation.id}`}
                      >
                        {t('portability.viewDiff')}
                      </Button>
                      <Button
                        variant="primary"
                        disabled={detail?.status !== 'dry_run_ready'}
                        onClick={() => {
                          if (!detail) return
                          setApplyConfirm('')
                          setApplyTarget(detail)
                        }}
                        data-testid={`portability-apply-${job.operation.id}`}
                      >
                        {t('portability.applyImport')}
                      </Button>
                    </span>
                    {detail?.status !== 'dry_run_ready' && detail?.status !== 'completed' && (
                      <small className="table-subtext">{t('portability.dryRunRequired')}</small>
                    )}
                  </td>
                </tr>
              )
            })}
          </DataTable>
        </StateGate>
      </Panel>

      {exportOpen && (
        <Modal title={t('portability.createExportTitle')} onClose={() => setExportOpen(false)}>
          <form className="form-stack" onSubmit={submitExport}>
            <Field label={t('portability.includeHistory')} hint={t('portability.includeHistoryHint')}>
              <select
                value={includeHistory ? 'true' : 'false'}
                onChange={(event) => setIncludeHistory(event.target.value === 'true')}
                data-testid="portability-include-history"
              >
                <option value="true">{t('licenseConsole.yes')}</option>
                <option value="false">{t('licenseConsole.no')}</option>
              </select>
            </Field>
            <Button type="submit" variant="primary" loading={busy} data-testid="portability-export-submit">
              {t('portability.createExportButton')}
            </Button>
          </form>
        </Modal>
      )}

      {importOpen && (
        <Modal title={t('portability.createImportTitle')} onClose={() => setImportOpen(false)}>
          <form className="form-stack" onSubmit={submitImport}>
            <Field label={t('portability.packageFile')} hint={t('portability.packageFileHint')}>
              <input
                type="file"
                accept="application/json,.json"
                onChange={(event) => setPackageFile(event.target.files?.[0] ?? null)}
                data-testid="portability-package-input"
                required
              />
            </Field>
            <Button type="submit" variant="primary" loading={busy} data-testid="portability-import-submit">
              {t('portability.uploadButton')}
            </Button>
          </form>
        </Modal>
      )}

      {manifestTarget && (
        <Modal title={t('portability.manifestTitle')} onClose={() => setManifestTarget(null)}>
          <div className="ops-detail">
            <MetaList
              rows={[
                { label: t('portability.manifestVersion'), value: textOf(manifestTarget.manifest as Row, 'manifest_version') },
                { label: t('portability.productVersion'), value: textOf(manifestTarget.manifest as Row, 'product_version') },
                { label: t('portability.sourceRegion'), value: textOf(manifestTarget.manifest as Row, 'source_region') },
                {
                  label: t('portability.encryption'),
                  value: JSON.stringify((manifestTarget.manifest as Row | null)?.encryption ?? {}),
                },
                { label: t('portability.colChecksum'), value: <code>{textOf(manifestTarget, 'checksum')}</code> },
                { label: t('portability.colSize'), value: displayBytes(manifestTarget.size_bytes) },
              ]}
            />
            <DataTable
              caption={t('portability.resourceCounts')}
              headers={[t('portability.colResourceType'), t('portability.colCount')]}
            >
              {entriesOf((manifestTarget.manifest as Row | null)?.resource_counts).map(([name, count]) => (
                <tr key={name}>
                  <td>{name}</td>
                  <td>{count}</td>
                </tr>
              ))}
            </DataTable>
            <div className="callout">
              <strong>{t('portability.warnings')}</strong>
              <ul>
                {(((manifestTarget.manifest as Row | null)?.warnings as string[] | undefined) ?? []).map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            </div>
            <JsonDetails summary={t('portability.filesTitle')} value={(manifestTarget.manifest as Row | null)?.files ?? []} />
          </div>
        </Modal>
      )}

      {diffTarget && (
        <Modal title={t('portability.diffTitle')} onClose={() => setDiffTarget(null)}>
          <div className="ops-detail">
            <Badge tone={(diffTarget.dry_run_report as Row | null)?.valid === true ? 'success' : 'danger'}>
              {(diffTarget.dry_run_report as Row | null)?.valid === true
                ? t('portability.diffValid')
                : t('portability.diffInvalid')}
            </Badge>
            <DataTable
              caption={t('portability.resourceCounts')}
              headers={[t('portability.colResourceType'), t('portability.colCount'), t('portability.diffConflicts'), t('portability.diffStrategies')]}
            >
              {entriesOf((diffTarget.dry_run_report as Row | null)?.resource_counts).map(([name, count]) => {
                const conflicts = Object.fromEntries(entriesOf((diffTarget.dry_run_report as Row | null)?.conflicts))
                const strategies = Object.fromEntries(
                  stringEntriesOf((diffTarget.dry_run_report as Row | null)?.strategies),
                )
                return (
                  <tr key={name}>
                    <td>{name}</td>
                    <td>{count}</td>
                    <td>{conflicts[name] ?? 0}</td>
                    <td>{strategies[name] ?? '—'}</td>
                  </tr>
                )
              })}
            </DataTable>
            <MetaList
              rows={[
                {
                  label: t('portability.credentialReconfiguration'),
                  value: String((diffTarget.dry_run_report as Row | null)?.credential_reconfiguration ?? 0),
                },
                {
                  label: t('portability.diffErrors'),
                  value:
                    (((diffTarget.dry_run_report as Row | null)?.errors as string[] | undefined) ?? []).join('; ') || '—',
                },
              ]}
            />
            {!!diffTarget.result_summary && Object.keys(diffTarget.result_summary as Row).length > 0 && (
              <JsonDetails summary={t('portability.resultSummary')} value={diffTarget.result_summary} />
            )}
          </div>
        </Modal>
      )}

      {applyTarget && (
        <Modal title={t('portability.applyTitle')} onClose={() => setApplyTarget(null)}>
          <form className="form-stack" onSubmit={confirmApply}>
            <div className="alert alert-error">{t('portability.applyWarning')}</div>
            <MetaList
              rows={[
                { label: t('portability.colJob'), value: <code>{applyTarget.id}</code> },
                {
                  label: t('portability.createdLabel'),
                  value: String(countResources((applyTarget.dry_run_report as Row | null)?.resource_counts)),
                },
              ]}
            />
            <Field label={t('portability.applyConfirmLabel')} hint={t('portability.applyConfirmHint')}>
              <input
                value={applyConfirm}
                onChange={(event) => setApplyConfirm(event.target.value)}
                placeholder="APPLY"
                data-testid="portability-apply-confirm"
                required
              />
            </Field>
            <Button type="submit" variant="danger" loading={busy} data-testid="portability-apply-submit">
              {t('portability.applyConfirmButton')}
            </Button>
          </form>
        </Modal>
      )}
    </>
  )
}
