import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useLocale } from './locale'
import { api } from './api'
import { StateView } from './ui'
import { ArrowUpRight, BookOpen, Code, KeyRound, Terminal, Webhook, Zap } from 'lucide-react'

interface OpenApiPath {
  path: string
  methods: string[]
  summary?: string
}

interface OpenApiDoc {
  info: { title: string; version: string }
  paths: Record<string, Record<string, { summary?: string; operationId?: string }>>
}

function Section({ id, icon, title, children }: { id: string; icon: React.ReactNode; title: string; children: React.ReactNode }) {
  return (
    <section id={id} className="public-panel" style={{ textAlign: 'left', marginBottom: '1.5rem' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.75rem' }}>
        <div className="public-panel-icon">{icon}</div>
        <h2 style={{ margin: 0 }}>{title}</h2>
      </div>
      <div style={{ fontSize: '0.95rem', lineHeight: 1.7 }}>{children}</div>
    </section>
  )
}

function PreCode({ children, label }: { children: string; label?: string }) {
  return (
    <div style={{ margin: '0.75rem 0' }}>
      {label && <small style={{ color: 'var(--muted)', fontWeight: 600 }}>{label}</small>}
      <pre style={{ background: '#0f172a', color: '#e2e8f0', padding: '1rem', borderRadius: '0.5rem', overflowX: 'auto', fontSize: '0.85rem' }}>
        <code>{children}</code>
      </pre>
    </div>
  )
}

export default function OpenPlatformDocsPage() {
  const { t } = useLocale()
  const [openapi, setOpenapi] = useState<OpenApiDoc | null>(null)
  const [paths, setPaths] = useState<OpenApiPath[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    api.get<OpenApiDoc>('/api/openapi.json')
      .then((data) => {
        setOpenapi(data)
        const extracted: OpenApiPath[] = []
        Object.entries(data.paths || {}).forEach(([path, methodsObj]) => {
          const methods = Object.keys(methodsObj || {}).filter((m) => m !== 'parameters')
          const firstOp = methods[0] ? methodsObj[methods[0]] : undefined
          extracted.push({ path, methods, summary: firstOp?.summary || firstOp?.operationId })
        })
        setPaths(extracted.slice(0, 60))
      })
      .catch((caught) => setError(caught instanceof Error ? caught.message : 'Failed to load'))
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return (
      <div className="public-shell">
        <main className="public-main"><StateView state="loading" /></main>
      </div>
    )
  }

  if (error) {
    return (
      <div className="public-shell">
        <main className="public-main"><StateView state="error" description={error} /></main>
      </div>
    )
  }

  return (
    <div className="public-shell">
      <header className="public-topbar">
        <Link className="brand dark" to="/">
          <span className="brand-mark"><Zap size={16} /></span>
          {t('public.brand')}
        </Link>
        <nav>
          <Link to="/pricing">{t('public.nav.pricing')}</Link>
          <Link to="/docs">{t('public.nav.docs')}</Link>
          <Link to="/status">{t('public.nav.status')}</Link>
          <Link className="button button-primary" to="/login">{t('public.nav.openConsole')}</Link>
        </nav>
      </header>

      <main className="public-main">
        <span className="eyebrow">Developer Portal</span>
        <h1>WorkAMA Open Platform</h1>
        <p className="public-lead">
          Build integrations, automations, and extensions on top of WorkAMA.
          OAuth 2.0 + PKCE, Webhooks, REST API, and native SDKs.
        </p>

        <div className="docs-layout">
          <aside>
            <strong>On this page</strong>
            <a href="#quickstart">Quick Start</a>
            <a href="#endpoints">API Endpoints</a>
            <a href="#oauth">OAuth 2.0</a>
            <a href="#webhooks">Webhooks</a>
            <a href="#sdk">SDKs & CLI</a>
          </aside>

          <article className="doc-article">
            <Section id="quickstart" icon={<BookOpen size={20} />} title="Quick Start">
              <p>
                Get your first API call working in under 5 minutes. You need a WorkAMA account
                and a workspace-scoped API key (or an OAuth client).
              </p>
              <ol>
                <li>Create an OAuth client in Console → Settings → OAuth.</li>
                <li>Register your redirect URI (HTTPS only; localhost over HTTP is allowed for dev).</li>
                <li>Obtain an access token via the authorization code + PKCE flow.</li>
                <li>Call the API with <code>Authorization: Bearer &lt;token&gt;</code>.</li>
              </ol>
              <PreCode label="Minimal curl">
{`curl https://api.workama.example/api/v1/sessions \\
  -H "Authorization: Bearer \$WORKAMA_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"title":"Research brief","model":"workama-chat"}'`}
              </PreCode>
            </Section>

            <Section id="endpoints" icon={<Code size={20} />} title="API Endpoint Reference">
              <p>
                The OpenAPI specification is served at <code>/api/openapi.json</code>.
                Below is a quick-view of available paths ({paths.length} shown).
              </p>
              <div style={{ maxHeight: '24rem', overflowY: 'auto', border: '1px solid var(--border)', borderRadius: '0.5rem' }}>
                <table style={{ width: '100%', fontSize: '0.85rem', borderCollapse: 'collapse' }}>
                  <thead>
                    <tr style={{ background: 'var(--surface)', textAlign: 'left' }}>
                      <th style={{ padding: '0.5rem 0.75rem' }}>Method</th>
                      <th style={{ padding: '0.5rem 0.75rem' }}>Path</th>
                      <th style={{ padding: '0.5rem 0.75rem' }}>Summary</th>
                    </tr>
                  </thead>
                  <tbody>
                    {paths.map((p) => (
                      <tr key={p.path} style={{ borderTop: '1px solid var(--border)' }}>
                        <td style={{ padding: '0.4rem 0.75rem', whiteSpace: 'nowrap' }}>
                          {p.methods.map((m) => (
                            <span key={m} style={{ marginRight: '0.35rem', fontWeight: 600, color: m === 'GET' ? 'var(--green)' : m === 'POST' ? 'var(--blue)' : 'var(--orange)' }}>
                              {m.toUpperCase()}
                            </span>
                          ))}
                        </td>
                        <td style={{ padding: '0.4rem 0.75rem', fontFamily: 'monospace' }}>{p.path}</td>
                        <td style={{ padding: '0.4rem 0.75rem', color: 'var(--muted)' }}>{p.summary}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p style={{ marginTop: '0.5rem' }}>
                <a className="button" href="/api/openapi.json" target="_blank" rel="noreferrer">
                  Download OpenAPI JSON <ArrowUpRight size={15} />
                </a>
              </p>
            </Section>

            <Section id="oauth" icon={<KeyRound size={20} />} title="OAuth 2.0 with PKCE">
              <p>
                WorkAMA supports the authorization code grant with PKCE (S256) for public and
                confidential clients. Refresh tokens are rotated on every exchange.
              </p>
              <h3>1. Generate PKCE parameters</h3>
              <PreCode label="JavaScript">
{`const verifier = btoa(crypto.getRandomValues(new Uint8Array(32)).join(''))
  .replace(/[+\/=]/g, '')
  .slice(0, 128);

const challenge = btoa(
  String.fromCharCode(
    ...new Uint8Array(
      await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))
    )
  )
).replace(/[+\/=]/g, '');`}
              </PreCode>
              <h3>2. Redirect user to authorize</h3>
              <PreCode label="URL">
{`GET /api/v1/oauth/authorize
  ?client_id=wama_client_xxxx
  &redirect_uri=https://app.example.com/oauth/callback
  &response_type=code
  &code_challenge=<challenge>
  &code_challenge_method=S256
  &scope=openid
  &state=<csrf-token>`}
              </PreCode>
              <h3>3. Exchange code for tokens</h3>
              <PreCode label="curl">
{`curl -X POST https://api.workama.example/api/v1/oauth/token \\
  -H "Content-Type: application/json" \\
  -d '{
    "grant_type": "authorization_code",
    "client_id": "wama_client_xxxx",
    "client_secret": "wama_secret_xxxx",
    "code": "wama_code_xxxx",
    "redirect_uri": "https://app.example.com/oauth/callback",
    "code_verifier": "<verifier>"
  }'`}
              </PreCode>
            </Section>

            <Section id="webhooks" icon={<Webhook size={20} />} title="Webhook Signature Verification">
              <p>
                WorkAMA signs every webhook delivery with HMAC-SHA256. The signature is sent in the
                <code>x-workama-signature</code> header. Verify it to ensure the payload originated from WorkAMA.
              </p>
              <PreCode label="Python">
{`import hmac, hashlib

def verify_signature(secret: str, payload: bytes, signature_header: str) -> bool:
    # signature_header format: t=<timestamp>,v1=<digest>
    parts = dict(p.split("=") for p in signature_header.split(","))
    timestamp = parts.get("t", "")
    expected = parts.get("v1", "")
    computed = hmac.new(
        secret.encode(),
        f"{timestamp}.{payload.decode('utf-8')}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, expected)`}
              </PreCode>
              <PreCode label="Node.js">
{`const crypto = require('crypto');

function verifySignature(secret, payload, signatureHeader) {
  const parts = Object.fromEntries(
    signatureHeader.split(',').map(p => p.split('='))
  );
  const computed = crypto
    .createHmac('sha256', secret)
    .update(\`\${parts.t}.\${payload}\`)
    .digest('hex');
  return crypto.timingSafeEqual(Buffer.from(computed), Buffer.from(parts.v1));
}`}
              </PreCode>
            </Section>

            <Section id="sdk" icon={<Terminal size={20} />} title="SDKs & CLI">
              <h3>Official SDKs</h3>
              <ul>
                <li><strong>Python</strong>: <code>pip install workama-sdk</code></li>
                <li><strong>JavaScript / TypeScript</strong>: <code>npm install @workama/sdk</code></li>
                <li><strong>Go</strong>: <code>go get github.com/workama/workama-go-sdk</code></li>
              </ul>
              <h3>CLI Installation</h3>
              <PreCode label="bash">
{`# macOS / Linux
curl -fsSL https://cli.workama.com/install.sh | sh

# Windows (PowerShell)
iwr -useb https://cli.workama.com/install.ps1 | iex

# Verify
workama --version`}
              </PreCode>
              <p>
                Authenticate the CLI with your workspace:
              </p>
              <PreCode label="bash">
{`workama auth login
workama workspace use <workspace-id>`}
              </PreCode>
            </Section>
          </article>
        </div>
      </main>

      <footer>
        <span>{t('public.copyright')}</span>
        <span>{t('public.builtFor')}</span>
      </footer>
    </div>
  )
}
