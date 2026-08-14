import { ArrowUpRight, CheckCircle2, Copy, FileText, LockKeyhole, ShieldCheck, Sparkles } from 'lucide-react'
import { Link, useLocation } from 'react-router-dom'
import { useState } from 'react'
import { LocaleProvider, LocaleToggle } from '@workama/ui'

/**
 * Share 应用。
 *
 * 通过 @workama/ui 共享包接入 LocaleProvider/LocaleToggle，
 * 使分享页面同样支持语言切换（页面文案暂为英文硬编码，
 * 后续可逐步迁移至 i18n key；LocaleProvider 不渲染 DOM，不影响测试）。
 */
export function ShareApp() {
  const location = useLocation()
  const [copied, setCopied] = useState(false)
  const isArtifact = location.pathname.startsWith('/artifact/')
  const copyLink = async () => {
    await navigator.clipboard?.writeText(window.location.href)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1800)
  }
  return (
    <LocaleProvider>
      <div className="share-shell">
        <header className="share-header">
          <Link to="/" className="share-brand">
            <span className="mark"><Sparkles size={15} /></span>WorkAMA
          </Link>
          <span className="secure-label"><LockKeyhole size={14} /> Scoped share</span>
          <LocaleToggle />
        </header>
        <main className="share-main">
          {isArtifact ? (
            <article className="artifact">
              <div className="artifact-label"><FileText size={15} /> SHARED ARTIFACT</div>
              <h1>Approval flow: launch readiness</h1>
              <p className="artifact-lead">A governed work artifact shared from the Product workspace.</p>
              <div className="artifact-meta">
                <span><CheckCircle2 size={15} /> Verified provenance</span>
                <span>Updated today</span>
                <span>Read-only</span>
              </div>
              <section className="artifact-body">
                <h2>Decision brief</h2>
                <p>WorkAMA keeps the question, sources, decision, and next action together so a shared artifact remains useful after the conversation ends.</p>
                <div className="artifact-callout"><ShieldCheck size={18} /><div><strong>Policy checked</strong><span>Share scope is limited to this artifact. Workspace credentials are never included.</span></div></div>
              </section>
              <button className="copy-button" onClick={() => void copyLink()}><Copy size={15} />{copied ? 'Link copied' : 'Copy share link'}</button>
            </article>
          ) : (
            <>
              <span className="eyebrow">WORKAMA PLATFORM</span>
              <h1>Share accountable work with context.</h1>
              <p className="share-lead">Publish a scoped artifact or an approved application view without exposing the workspace behind it.</p>
              <div className="share-actions">
                <Link className="primary-button" to="/artifact/demo">Open demo artifact <ArrowUpRight size={16} /></Link>
                <Link className="text-link" to="https://workama.example/docs">Read developer docs <ArrowUpRight size={15} /></Link>
              </div>
              <div className="share-grid">
                <div><span className="grid-icon"><FileText size={17} /></span><strong>Evidence stays attached</strong><p>Sources, approvals, and provenance travel with the published result.</p></div>
                <div><span className="grid-icon"><ShieldCheck size={17} /></span><strong>Scope is explicit</strong><p>Public links are read-only and bounded by workspace policy.</p></div>
                <div><span className="grid-icon"><Sparkles size={17} /></span><strong>Built for teams</strong><p>Give stakeholders a useful view without another account or meeting.</p></div>
              </div>
            </>
          )}
        </main>
        <footer><span>WorkAMA</span><span>Built for accountable AI work.</span></footer>
      </div>
    </LocaleProvider>
  )
}
