/**
 * GROUP B 管理台（可移植性 / 企业许可 / 韧性演练）共用的轻量数据与展示助手。
 *
 * 这里只放三张页面都会用到的部分：错误文案、时间/字节格式化、
 * 单对象拉取 hook、加载态包装与轻量通知。业务逻辑保留在各自页面中。
 */
import { useCallback, useEffect, useState, type ReactNode } from 'react'
import type { MessageKey } from '@workama/i18n'
import { api } from './api'
import { useLocale } from './locale'
import { StateView, Toast } from './ui'

export type Row = Record<string, unknown>

export function errorText(caught: unknown, t: (key: MessageKey) => string): string {
  return caught instanceof Error && caught.message ? caught.message : t('errors.requestFailed')
}

export function displayDate(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  const date = new Date(String(value))
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString()
}

export function displayBytes(value: unknown): string {
  const bytes = Number(value)
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

export function shortId(value: unknown, keep = 10): string {
  const raw = String(value ?? '')
  return raw.length > keep * 2 ? `${raw.slice(0, keep)}…${raw.slice(-6)}` : raw || '—'
}

export function textOf(row: Row | null | undefined, key: string, fallback = '—'): string {
  const value = row?.[key]
  return value === null || value === undefined || value === '' ? fallback : String(value)
}

/** 拉取单个 JSON 对象；`allowMissing` 用于 404/402 等“没有资源”是正常业务态的端点。 */
export function useApiObject<T>(endpoint: string, options: { allowMissing?: boolean } = {}) {
  const { allowMissing = false } = options
  const { t } = useLocale()
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const reload = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setData(await api.get<T>(endpoint))
    } catch (caught) {
      setData(null)
      if (!allowMissing) setError(errorText(caught, t))
    } finally {
      setLoading(false)
    }
  }, [endpoint, allowMissing, t])

  useEffect(() => {
    void reload()
  }, [reload])

  return { data, loading, error, reload }
}

export function StateGate({
  loading,
  error,
  empty,
  retry,
  children,
}: {
  loading: boolean
  error: string
  empty?: boolean
  retry: () => void
  children: ReactNode
}) {
  if (loading) return <StateView state="loading" />
  if (error) return <StateView state="error" description={error} onRetry={retry} />
  if (empty) return <StateView state="empty" />
  return <>{children}</>
}

export function Notice({ notice, clear }: { notice: string; clear: () => void }) {
  return notice ? <Toast message={notice} onClose={clear} /> : null
}

/** 折叠展示原始 JSON。scroll 区域带 tabIndex 以满足 WCAG 2.2 键盘可达要求。 */
export function JsonDetails({ summary, value }: { summary: string; value: unknown }) {
  return (
    <details className="ops-json">
      <summary>{summary}</summary>
      <pre tabIndex={0} role="region" aria-label={summary}>
        {JSON.stringify(value, null, 2)}
      </pre>
    </details>
  )
}

/** key/value 明细列表，复用 eval-meta-list 既有样式。 */
export function MetaList({ rows }: { rows: Array<{ label: string; value: ReactNode }> }) {
  return (
    <div className="eval-meta-list">
      {rows.map((row) => (
        <div key={row.label}>
          <span>{row.label}</span>
          <strong>{row.value}</strong>
        </div>
      ))}
    </div>
  )
}
