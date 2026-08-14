import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useLocale } from './locale'
import { api } from './api'
import { StateView } from './ui'
import { ArrowUpRight, Code, Webhook, KeyRound, BookOpen, Terminal } from 'lucide-react'

interface PublicDoc {
  slug: string
  title: string
  content: string
  doc_type: string
}

interface PublicDocsResponse {
  openapi_url: string
  sdk_downloads: Record<string, string>
  quickstart_url: string
  webhook_guide_url: string
  oauth_guide_url: string
  docs: PublicDoc[]
}

function DeveloperCard({ icon, title, children }: { icon: React.ReactNode; title: string; children: React.ReactNode }) {
  return (
    <div className="public-panel" style={{ textAlign: 'left' }}>
      <div className="public-panel-icon">{icon}</div>
      <h3>{title}</h3>
      <div style={{ fontSize: '0.95rem', lineHeight: 1.6 }}>{children}</div>
    </div>
  )
}

export default function DevelopersPage() {
  const { t } = useLocale()
  const [data, setData] = useState<PublicDocsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    api.get<PublicDocsResponse>('/api/v1/public/docs')
      .then(setData)
      .catch((caught) => setError(caught instanceof Error ? caught.message : 'Failed to load'))
      .finally(() => setLoading(false))
  }, [])

  if (loading) return <div className="public-shell"><main className="public-main"><StateView state="loading" /></main></div>
  if (error) return <div className="public-shell"><main className="public-main"><StateView state="error" description={error} /></main></div>

  return (
    <div className="public-shell">
      <header className="public-topbar">
        <Link className="brand dark" to="/"><span className="brand-mark">⚡</span>{t('public.brand')}</Link>
        <nav>
          <Link to="/pricing">{t('public.nav.pricing')}</Link>
          <Link to="/docs">{t('public.nav.docs')}</Link>
          <Link to="/status">{t('public.nav.status')}</Link>
          <Link className="button button-primary" to="/login">{t('public.nav.openConsole')}</Link>
        </nav>
      </header>
      <main className="public-main">
        <span className="eyebrow">Developer Portal</span>
        <h1>Build on WorkAMA</h1>
        <p className="public-lead">API docs, Webhooks, OAuth tutorials, and SDKs to integrate with the WorkAMA platform.</p>
        <div className="public-plans" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: '1rem' }}>
          <DeveloperCard icon={<BookOpen size={20} />} title="API Reference">
            <p>OpenAPI specification and endpoint reference.</p>
            <a className="button" href={data?.openapi_url} target="_blank" rel="noreferrer">View OpenAPI <ArrowUpRight size={15} /></a>
          </DeveloperCard>
          <DeveloperCard icon={<Terminal size={20} />} title="SDKs">
            <ul>
              {Object.entries(data?.sdk_downloads || {}).map(([name, url]) => (
                <li key={name}><a href={url} target="_blank" rel="noreferrer">{name}</a></li>
              ))}
            </ul>
          </DeveloperCard>
          <DeveloperCard icon={<Code size={20} />} title="Quick Start">
            <p>Get up and running in minutes.</p>
            <Link className="button" to={data?.quickstart_url || '/docs'}>Read Guide <ArrowUpRight size={15} /></Link>
          </DeveloperCard>
          <DeveloperCard icon={<Webhook size={20} />} title="Webhooks">
            <p>Subscribe to events and verify signatures.</p>
            <Link className="button" to={data?.webhook_guide_url || '/docs'}>Webhook Guide <ArrowUpRight size={15} /></Link>
          </DeveloperCard>
          <DeveloperCard icon={<KeyRound size={20} />} title="OAuth 2.0">
            <p>PKCE-based authorization code flow.</p>
            <Link className="button" to={data?.oauth_guide_url || '/docs'}>OAuth Tutorial <ArrowUpRight size={15} /></Link>
          </DeveloperCard>
        </div>
        {data && data.docs.length > 0 && (
          <div className="public-panel" style={{ marginTop: '2rem', textAlign: 'left' }}>
            <h2>Documentation</h2>
            {data.docs.map((doc) => (
              <details key={doc.slug} style={{ marginBottom: '0.5rem' }}>
                <summary><strong>{doc.title}</strong> <small>({doc.doc_type})</small></summary>
                <p style={{ whiteSpace: 'pre-wrap' }}>{doc.content}</p>
              </details>
            ))}
          </div>
        )}
      </main>
      <footer><span>{t('public.copyright')}</span><span>{t('public.builtFor')}</span></footer>
    </div>
  )
}
