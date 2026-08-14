import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { getInitialLocale, translate, type Locale, type MessageKey } from '@workama/i18n'

type LocaleContextValue = { locale: Locale; setLocale: (locale: Locale) => void; t: (key: MessageKey) => string }
const LocaleContext = createContext<LocaleContextValue | null>(null)
const storageKey = 'workama.locale'

export function LocaleProvider({ children }: { children: ReactNode }) {
  const [locale, setLocale] = useState<Locale>(() => {
    const saved = typeof window !== 'undefined' ? window.localStorage.getItem(storageKey) : null
    return saved === 'zh-CN' || saved === 'en-US' ? saved : getInitialLocale(typeof navigator === 'undefined' ? null : navigator.language)
  })
  useEffect(() => { window.localStorage.setItem(storageKey, locale); document.documentElement.lang = locale }, [locale])
  const value = useMemo(() => ({ locale, setLocale, t: (key: MessageKey) => translate(locale, key) }), [locale])
  return <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>
}

export function useLocale() {
  const value = useContext(LocaleContext)
  if (!value) throw new Error('useLocale must be used inside LocaleProvider')
  return value
}

export function LocaleToggle() {
  const { locale, setLocale, t } = useLocale()
  const nextLocale = locale === 'zh-CN' ? 'en-US' : 'zh-CN'
  return <button className="locale-toggle" type="button" aria-label={`${t('ui.language')}: ${nextLocale}`} title={t('ui.language')} onClick={() => setLocale(nextLocale)}>{locale === 'zh-CN' ? 'EN' : '中'}</button>
}
