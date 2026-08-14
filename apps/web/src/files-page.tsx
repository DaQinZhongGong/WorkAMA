/**
 * 文件管理：概览指标 + 上传 + 卡片网格 + 类型/大小过滤 + 下载。
 * 对应《550-异步任务通知搜索与平台支撑设计》文件管理页面。
 */
import { useEffect, useMemo, useState, type ChangeEvent, type FormEvent, type ReactNode } from 'react'
import {
  File as FileIcon,
  FileArchive,
  FileCode,
  FileImage,
  FileText,
  FileVideo,
  Filter,
  HardDrive,
  RefreshCw,
  Trash2,
  Upload,
} from 'lucide-react'
import { api, asItems, errorMessage } from './api'
import { Badge, Button, Field, Kpi, Panel, SearchBox, StateView } from './ui'
import { useLocale } from './locale'

type StoredFile = {
  id: string
  name: string
  size?: number
  content_type?: string
  url?: string
  created_at?: string
  uploader?: string
}

const IMAGE_TYPES = ['image/']
const VIDEO_TYPES = ['video/']
const ARCHIVE_TYPES = ['application/zip', 'application/x-tar', 'application/gzip', 'application/x-7z']
const CODE_EXT = ['.ts', '.tsx', '.js', '.py', '.json', '.yaml', '.yml', '.md', '.go', '.rs', '.sql']

/** 字节 → 人类可读。 */
function formatBytes(bytes?: number): string {
  if (bytes === undefined || bytes === null) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(2)} MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
}

/** 文件类型 → 图标组件。 */
function fileIcon(contentType?: string, name?: string): ReactNode {
  const ct = (contentType ?? '').toLowerCase()
  if (IMAGE_TYPES.some((p) => ct.startsWith(p))) return <FileImage size={18} />
  if (VIDEO_TYPES.some((p) => ct.startsWith(p))) return <FileVideo size={18} />
  if (ARCHIVE_TYPES.some((p) => ct.startsWith(p) || ct.includes(p))) return <FileArchive size={18} />
  if (ct.startsWith('text/') || (name && CODE_EXT.some((ext) => name.endsWith(ext))))
    return <FileCode size={18} />
  return <FileIcon size={18} />
}

/** 文件类型 → 资源色调。 */
function fileTone(contentType?: string, name?: string): string {
  const ct = (contentType ?? '').toLowerCase()
  if (IMAGE_TYPES.some((p) => ct.startsWith(p))) return 'green'
  if (VIDEO_TYPES.some((p) => ct.startsWith(p))) return 'purple'
  if (ARCHIVE_TYPES.some((p) => ct.startsWith(p))) return 'amber'
  if (ct.startsWith('text/') || (name && CODE_EXT.some((ext) => name.endsWith(ext)))) return 'blue'
  return 'neutral'
}

/** 文件类别推断。 */
function fileCategory(contentType?: string, name?: string): 'image' | 'video' | 'archive' | 'code' | 'text' | 'other' {
  const ct = (contentType ?? '').toLowerCase()
  if (IMAGE_TYPES.some((p) => ct.startsWith(p))) return 'image'
  if (VIDEO_TYPES.some((p) => ct.startsWith(p))) return 'video'
  if (ARCHIVE_TYPES.some((p) => ct.startsWith(p))) return 'archive'
  if (ct.startsWith('text/')) return 'text'
  if (name && CODE_EXT.some((ext) => name.endsWith(ext))) return 'code'
  return 'other'
}

/** 时间戳格式化。 */
function formatTime(value?: string): string {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString('zh-CN', { hour12: false })
}

export default function AdminFilesPage(): ReactNode {
  const [items, setItems] = useState<StoredFile[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [uploadError, setUploadError] = useState('')
  const [uploading, setUploading] = useState(false)
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState('')
  const { t } = useLocale()

  function reload() {
    setLoading(true)
    setError('')
    void api
      .get<unknown>('/api/v1/files')
      .then((payload) => setItems(asItems<StoredFile>(payload)))
      .catch((caught) => setError(errorMessage(caught, '加载文件列表失败')))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    reload()
  }, [])

  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase()
    return items.filter((item) => {
      if (category && fileCategory(item.content_type, item.name) !== category) return false
      if (!keyword) return true
      return `${item.name} ${item.content_type ?? ''} ${item.uploader ?? ''}`
        .toLowerCase()
        .includes(keyword)
    })
  }, [items, query, category])

  const stats = useMemo(() => {
    const totalBytes = items.reduce((sum, item) => sum + (item.size ?? 0), 0)
    const types = new Set(items.map((item) => fileCategory(item.content_type, item.name))).size
    return { totalBytes, types }
  }, [items])

  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget
    const input = form.elements.namedItem('file') as HTMLInputElement
    const file = input?.files?.[0]
    if (!file) return
    setUploading(true)
    setUploadError('')
    try {
      const formData = new FormData()
      formData.append('file', file)
      await api.post('/api/v1/files', formData)
      void reload()
      form.reset()
    } catch (caught) {
      setUploadError(errorMessage(caught, t('common.uploadFailed')))
    } finally {
      setUploading(false)
    }
  }

  function download(file: StoredFile) {
    if (file.url) {
      window.open(file.url, '_blank')
    } else {
      window.open(`/api/v1/files/${encodeURIComponent(file.id)}/download`, '_blank')
    }
  }

  async function remove(id: string) {
    try {
      await api.delete(`/api/v1/files/${encodeURIComponent(id)}`)
      setItems((current) => current.filter((item) => item.id !== id))
    } catch (caught) {
      setError(errorMessage(caught, '删除失败'))
    }
  }

  return (
    <div data-testid="files-page">
      <header className="page-header">
        <div>
          <div className="eyebrow">{t('admin.files.eyebrow')}</div>
          <h1>{t('admin.files.title')}</h1>
          <p>{t('admin.files.subtitle')}</p>
        </div>
        <div className="page-actions">
          <Button icon={<RefreshCw size={15} />} onClick={reload} disabled={loading} data-testid="files-refresh">
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
          {items.length > 0 && (
            <div className="kpi-grid" data-testid="files-kpis">
              <Kpi
                label={t('admin.files.kpi.count')}
                value={String(items.length).padStart(3, '0')}
                icon={<FileText size={18} />}
                trend={t('admin.files.kpi.count.trend')}
              />
              <Kpi
                label={t('admin.files.kpi.size')}
                value={formatBytes(stats.totalBytes)}
                icon={<HardDrive size={18} />}
                trend={t('admin.files.kpi.size.trend')}
              />
              <Kpi
                label={t('admin.files.kpi.types')}
                value={String(stats.types).padStart(2, '0')}
                icon={<Filter size={18} />}
                trend={t('admin.files.kpi.types.trend')}
              />
              <Kpi
                label={t('admin.files.kpi.shown')}
                value={String(filtered.length).padStart(2, '0')}
                icon={<FileText size={18} />}
                trend={t('admin.files.kpi.shown.trend')}
              />
            </div>
          )}

          <div className="ops-grid">
            <Panel
              title={t('admin.files.library.title')}
              subtitle={t('admin.files.library.subtitle')}
              actions={
                <div className="filters-row">
                  <SearchBox
                    value={query}
                    onChange={setQuery}
                    placeholder={t('admin.files.search')}
                  />
                  <Field label={t('common.category')}>
                    <select
                      value={category}
                      onChange={(e: ChangeEvent<HTMLSelectElement>) => setCategory(e.target.value)}
                      data-testid="files-category-filter"
                    >
                      <option value="">{t('common.all')}</option>
                      <option value="image">{t('admin.files.category.image')}</option>
                      <option value="video">{t('admin.files.category.video')}</option>
                      <option value="archive">{t('admin.files.category.archive')}</option>
                      <option value="text">{t('admin.files.category.text')}</option>
                      <option value="code">{t('admin.files.category.code')}</option>
                      <option value="other">{t('admin.files.category.other')}</option>
                    </select>
                  </Field>
                </div>
              }
            >
              {filtered.length === 0 ? (
                <StateView
                  state="empty"
                  title={t('admin.files.emptyFiltered.title')}
                  description={t('admin.files.emptyFiltered.desc')}
                />
              ) : (
                <div className="resource-grid" data-testid="files-list">
                  {filtered.map((item) => (
                    <div
                      key={item.id}
                      className="resource-card"
                      data-testid="files-item"
                      data-category={fileCategory(item.content_type, item.name)}
                    >
                      <div className={`resource-icon ${fileTone(item.content_type, item.name)}`}>
                        {fileIcon(item.content_type, item.name)}
                      </div>
                      <div className="resource-main">
                        <strong>{item.name}</strong>
                        <p>{formatTime(item.created_at)}</p>
                        <span>
                          {formatBytes(item.size)} · {item.content_type ?? 'application/octet-stream'}
                        </span>
                      </div>
                      <div className="panel-actions-inline">
                        <Badge tone="neutral">{fileCategory(item.content_type, item.name)}</Badge>
                      </div>
                      <div className="knowledge-actions">
                        <Button
                          variant="ghost"
                          onClick={() => download(item)}
                          data-testid={`files-download-${item.id}`}
                        >
                          {t('common.download')}
                        </Button>
                        <Button
                          variant="ghost"
                          onClick={() => void remove(item.id)}
                          data-testid={`files-delete-${item.id}`}
                          icon={<Trash2 size={14} />}
                        >
                          {t('common.delete')}
                        </Button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Panel>

            <Panel
              title={t('admin.files.upload.title')}
              subtitle={t('admin.files.upload.subtitle')}
            >
              <form
                className="form-stack"
                data-testid="files-upload-form"
                onSubmit={upload}
              >
                <label className="field">
                  <span className="field-label">{t('admin.files.upload.label')}</span>
                  <input type="file" name="file" data-testid="files-upload-input" />
                </label>
                <Button
                  type="submit"
                  variant="primary"
                  loading={uploading}
                  icon={<Upload size={15} />}
                  data-testid="files-upload-submit"
                >
                  {t('admin.files.upload.submit')}
                </Button>
                {uploadError && (
                  <div className="alert alert-error" role="alert">
                    {uploadError}
                  </div>
                )}
              </form>
            </Panel>
          </div>
        </>
      )}
    </div>
  )
}