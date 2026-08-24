/**
 * @workama/ui
 *
 * 共享 UI 组件库 + LocaleProvider：
 *   - 设计令牌驱动的基础展示组件（Button/Badge/Panel/Modal/Field/SearchBox/DataTable/Kpi/Toast/StateView/Status/EmptyAction/IconButton）
 *   - LocaleProvider/useLocale/LocaleToggle：从 web/locale.tsx 抽出，统一供 Web/Desktop/Share/Mobile 复用
 *
 * 依赖：`@workama/i18n`、`lucide-react`、`react-router-dom`、`react`/`react-dom`（peer）；
 * 不含业务页面/路由表/状态机。
 */
import {
  createContext,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { Link } from 'react-router-dom'
import {
  AlertCircle,
  Check,
  ChevronRight,
  LoaderCircle,
  RefreshCw,
  Search,
  X,
} from 'lucide-react'
import {
  getInitialLocale,
  translate,
  type Locale,
  type MessageKey,
} from '@workama/i18n'

/* -------------------------------------------------------------------------- */
/* LocaleProvider / useLocale / LocaleToggle                                   */
/* -------------------------------------------------------------------------- */

type LocaleContextValue = {
  locale: Locale
  setLocale: (locale: Locale) => void
  t: (key: MessageKey) => string
}

const LocaleContext = createContext<LocaleContextValue | null>(null)
const STORAGE_KEY = 'workama.locale'

export function LocaleProvider({
  children,
  initialLocale,
}: {
  children: ReactNode
  /** 显式钉定初始 locale（测试/嵌入场景用）；缺省时按 localStorage → navigator.language 解析。 */
  initialLocale?: Locale
}) {
  const [locale, setLocale] = useState<Locale>(() => {
    if (initialLocale === 'zh-CN' || initialLocale === 'en-US') return initialLocale
    const saved =
      typeof window !== 'undefined'
        ? window.localStorage.getItem(STORAGE_KEY)
        : null
    return saved === 'zh-CN' || saved === 'en-US'
      ? saved
      : getInitialLocale(
          typeof navigator === 'undefined' ? null : navigator.language,
        )
  })
  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEY, locale)
    document.documentElement.lang = locale
  }, [locale])
  const value = useMemo<LocaleContextValue>(
    () => ({ locale, setLocale, t: (key: MessageKey) => translate(locale, key) }),
    [locale],
  )
  return <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>
}

export function useLocale() {
  const value = useContext(LocaleContext)
  if (!value) throw new Error('useLocale must be used inside LocaleProvider')
  return value
}

export function LocaleToggle() {
  const { locale, setLocale, t } = useLocale()
  const nextLocale: Locale = locale === 'zh-CN' ? 'en-US' : 'zh-CN'
  return (
    <button
      className="locale-toggle"
      type="button"
      aria-label={`${t('ui.language')}: ${nextLocale}`}
      title={t('ui.language')}
      onClick={() => setLocale(nextLocale)}
    >
      {locale === 'zh-CN' ? 'EN' : '中'}
    </button>
  )
}

/* -------------------------------------------------------------------------- */
/* 通用展示组件                                                                */
/* -------------------------------------------------------------------------- */

export function Button({
  children,
  variant = 'secondary',
  icon,
  loading = false,
  className = '',
  type = 'button',
  disabled = false,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger'
  icon?: ReactNode
  loading?: boolean
}) {
  const isDisabled = loading || disabled
  return (
    <button
      type={type}
      className={`button button-${variant} ${className}`.trim()}
      aria-busy={loading || undefined}
      disabled={isDisabled}
      {...props}
    >
      {loading ? (
        <LoaderCircle size={16} className="spin" aria-hidden="true" />
      ) : (
        icon
      )}
      {children}
    </button>
  )
}

export function IconButton({
  label,
  children,
  className = '',
  type = 'button',
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button
      type={type}
      className={`icon-button ${className}`.trim()}
      aria-label={label}
      title={label}
      {...props}
    >
      {children}
    </button>
  )
}

export function Badge({
  children,
  tone = 'neutral',
}: {
  children: ReactNode
  tone?: 'neutral' | 'success' | 'warning' | 'danger' | 'info'
}) {
  return <span className={`badge badge-${tone}`}>{children}</span>
}

export function Panel({
  title,
  subtitle,
  actions,
  children,
  className = '',
}: {
  title?: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section className={`panel ${className}`}>
      <>
        {title && (
          <header className="panel-header">
            <div>
              <h2>{title}</h2>
              {subtitle && <p>{subtitle}</p>}
            </div>
            {actions}
          </header>
        )}
      </>
      <div className="panel-body">{children}</div>
    </section>
  )
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: ReactNode
}) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  )
}

export function SearchBox({
  value,
  onChange,
  placeholder,
}: {
  value: string
  onChange: (value: string) => void
  placeholder?: string
}) {
  const { t } = useLocale()
  const resolvedPlaceholder = placeholder ?? t('ui.search')
  return (
    <label className="search-box">
      <Search size={16} aria-hidden="true" />
      <input
        type="search"
        aria-label={resolvedPlaceholder}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={resolvedPlaceholder}
      />
    </label>
  )
}

export function DataTable({
  headers,
  children,
  caption,
}: {
  headers: string[]
  children: ReactNode
  caption?: string
}) {
  return (
    <div className="table-wrap" tabIndex={0}>
      <table className="data-table" aria-label={caption}>
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr>
            {headers.map((header) => (
              <th scope="col" key={header}>
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  )
}

export function Kpi({
  label,
  value,
  trend,
  icon,
}: {
  label: string
  value: string
  trend?: string
  icon?: ReactNode
}) {
  return (
    <div className="kpi">
      <div className="kpi-icon">{icon}</div>
      <div>
        <span>{label}</span>
        <strong>{value}</strong>
        {trend && <small>{trend}</small>}
      </div>
    </div>
  )
}

export function Toast({
  message,
  onClose,
}: {
  message: string
  onClose: () => void
}) {
  const { t } = useLocale()
  return (
    <div className="toast" role="status">
      <Check size={16} />
      {message}
      <IconButton label={t('ui.dismiss')} onClick={onClose}>
        <X size={14} />
      </IconButton>
    </div>
  )
}

export type StateViewState = 'loading' | 'empty' | 'error' | 'permission'

const stateTitleKeys: Record<StateViewState, MessageKey> = {
  loading: 'ui.loadingWorkspaceData',
  empty: 'ui.noRecordsYet',
  error: 'ui.unableLoadView',
  permission: 'ui.permissionRequired',
}
const stateDescriptionKeys: Record<StateViewState, MessageKey> = {
  loading: 'ui.syncingWorkspace',
  empty: 'ui.createFirstItem',
  error: 'ui.checkConnection',
  permission: 'ui.roleCannotAccess',
}

export function StateView({
  state,
  title,
  description,
  onRetry,
}: {
  state: StateViewState
  title?: string
  description?: string
  onRetry?: () => void
}) {
  const { t } = useLocale()
  const Icon =
    state === 'loading'
      ? LoaderCircle
      : state === 'error'
        ? AlertCircle
        : state === 'permission'
          ? X
          : Search
  return (
    <div
      className="state-view"
      role={state === 'error' ? 'alert' : 'status'}
    >
      <Icon
        size={22}
        className={state === 'loading' ? 'spin' : ''}
        aria-hidden="true"
      />
      <strong>{title ?? t(stateTitleKeys[state])}</strong>
      <span>{description ?? t(stateDescriptionKeys[state])}</span>
      {state === 'error' && onRetry && (
        <Button icon={<RefreshCw size={15} />} onClick={onRetry}>
          {t('ui.retry')}
        </Button>
      )}
    </div>
  )
}

export function Status({
  value,
  toneValue,
}: {
  value?: string
  toneValue?: string
}) {
  const { t } = useLocale()
  const normalized = String(toneValue ?? value ?? 'unknown').toLowerCase()
  const tone = ['active', 'healthy', 'enabled', 'completed', 'published', 'succeeded', 'passed', 'ready'].some((item) => normalized.includes(item))
    ? 'success'
    : ['running', 'pending', 'queued', 'draft', 'paused'].some((item) =>
          normalized.includes(item),
        )
      ? 'warning'
      : ['failed', 'error', 'revoked', 'disabled', 'cancelled', 'expired', 'exhausted'].some((item) => normalized.includes(item))
        ? 'danger'
        : 'neutral'
  return (
    <span className="status">
      <i className={`status-dot status-${tone}`} />
      {value ?? t('ui.unknown')}
    </span>
  )
}

export function EmptyAction({ to, label }: { to: string; label: string }) {
  return (
    <Link className="button button-primary" to={to}>
      <ChevronRight size={16} />
      {label}
    </Link>
  )
}

export function Modal({
  title,
  children,
  onClose,
}: {
  title: string
  children: ReactNode
  onClose: () => void
}) {
  const { t } = useLocale()
  const titleId = useId()
  const modalRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const previousFocus =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null
    modalRef.current?.focus()
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose()
        return
      }
      if (event.key !== 'Tab') return
      const focusable = Array.from(
        modalRef.current?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      )
      if (!focusable.length) {
        event.preventDefault()
        modalRef.current?.focus()
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      previousFocus?.focus()
    }
  }, [onClose])
  return (
    <div
      className="modal-backdrop"
      role="presentation"
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <div
        ref={modalRef}
        className="modal"
        role="dialog"
        tabIndex={-1}
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <header>
          <h2 id={titleId}>{title}</h2>
          <IconButton label={t('ui.close')} onClick={onClose}>
            <X size={17} />
          </IconButton>
        </header>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* PageHeader（业务无关版，pageTitleKeys 由各端自行维护）                       */
/* -------------------------------------------------------------------------- */

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string
  title: string
  description?: string
  actions?: ReactNode
}) {
  const { t } = useLocale()
  return (
    <header className="page-header">
      <div>
        <div className="eyebrow">{eyebrow ?? t('ui.workamaConsole')}</div>
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  )
}
