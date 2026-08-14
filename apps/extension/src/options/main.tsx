import { FormEvent, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import '../styles.css'
import { clearSession, loadSession, saveSession } from '../shared/storage'

function Options() {
  const [baseUrl, setBaseUrl] = useState('http://localhost:20204')
  const [token, setToken] = useState('')
  const [hasContext, setHasContext] = useState(false)
  const [status, setStatus] = useState('Settings are scoped to this browser session.')
  const [error, setError] = useState(false)

  useEffect(() => { loadSession().then((session) => { setBaseUrl(session.baseUrl); setToken(session.token); setHasContext(Boolean(session.context)) }).catch(() => { setError(true); setStatus('Unable to load session settings.') }) }, [])
  async function handleSubmit(event: FormEvent) { event.preventDefault(); try { await saveSession({ baseUrl, token }); setError(false); setStatus('Saved for this browser session.') } catch (reason) { setError(true); setStatus(reason instanceof Error ? reason.message : 'Unable to save settings.') } }
  async function handleClear() { await clearSession(); setBaseUrl('http://localhost:20204'); setToken(''); setHasContext(false); setError(false); setStatus('Session settings and captured context cleared.') }
  return <main className="page"><div className="shell"><header className="brand"><div className="brand__identity"><span className="brand__mark" aria-hidden="true">W</span><div><p className="brand__name">WorkAMA settings</p><p className="eyebrow">Browser extension preferences</p></div></div></header><section className="surface"><div className="surface__body"><div className="surface__header"><div><h1 className="surface__title">Connection</h1><p className="surface__description">Configure where the extension opens the WorkAMA chat entry.</p></div></div><form className="capture-panel" onSubmit={handleSubmit}><div className="field"><label className="field__label" htmlFor="options-base-url">WorkAMA URL</label><input className="input" id="options-base-url" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} inputMode="url" /></div><div className="field"><label className="field__label" htmlFor="options-token">Session token <span className="eyebrow">(optional)</span></label><input className="input" id="options-token" type="password" autoComplete="off" value={token} onChange={(event) => setToken(event.target.value)} /></div><div className="actions"><button className="button button--primary" type="submit">Save settings</button><button className="button" type="button" onClick={handleClear}>Clear session</button></div></form><p className={`status${error ? ' status--error' : ''}`} role="status">{status}</p><div className="stats"><div className="stat"><span className="stat__value">Session only</span><span className="stat__label">Credential lifetime</span></div><div className="stat"><span className="stat__value">{hasContext ? 'Ready' : 'Empty'}</span><span className="stat__label">Captured context</span></div><div className="stat"><span className="stat__value">No hosts</span><span className="stat__label">Permission scope</span></div></div><div className="notice">WorkAMA reads the active page only after you press Capture selection. No content script runs continuously, and no token is written to local storage, cookies, or a remote service.</div></div></section></div></main>
}

createRoot(document.getElementById('root')!).render(<Options />)
