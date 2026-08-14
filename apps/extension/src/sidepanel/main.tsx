import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import '../styles.css'
import { captureSelection, copyText, openWorkAMA } from '../shared/capture'
import { loadSession, saveSession } from '../shared/storage'
import { redactSensitiveText } from '../shared/safety'
import type { SessionState } from '../shared/types'

const emptySession: SessionState = { baseUrl: 'http://localhost:20204', token: '', context: '' }

function SidePanel() {
  const [session, setSession] = useState(emptySession)
  const [status, setStatus] = useState('Ready for an explicit capture.')
  const [busy, setBusy] = useState(false)
  const [statusError, setStatusError] = useState(false)

  useEffect(() => { loadSession().then(setSession).catch((error: Error) => showError(error.message)) }, [])

  function showError(message: string) { setStatus(message); setStatusError(true) }
  function update(field: keyof SessionState, value: string) { setSession((current) => ({ ...current, [field]: value })) }
  async function persist(field: keyof SessionState, value: string) {
    const safeValue = field === 'context' ? redactSensitiveText(value) : value
    try { setSession(await saveSession({ [field]: safeValue })) }
    catch (error) { showError(error instanceof Error ? error.message : 'Unable to save session settings.') }
  }
  async function handleCapture() {
    setBusy(true); setStatusError(false); setStatus('Capturing selected page text…')
    try {
      const result = await captureSelection()
      if (!result.ok) return showError(result.error)
      const context = [result.title, result.url, '', redactSensitiveText(result.text)].filter(Boolean).join('\n')
      await persist('context', context)
      setStatus('Captured and filtered for this browser session.')
    } catch (error) { showError(error instanceof Error ? error.message : 'Capture failed.') }
    finally { setBusy(false) }
  }
  async function handleCopy() {
    try { await copyText(session.context); setStatusError(false); setStatus('Context copied to the clipboard.') }
    catch (error) { showError(error instanceof Error ? error.message : 'Copy failed.') }
  }
  async function handleOpen() {
    try { const saved = await saveSession(session); setSession(saved); await openWorkAMA(saved.baseUrl); setStatusError(false); setStatus('WorkAMA chat opened. Paste the captured context into the composer.') }
    catch (error) { showError(error instanceof Error ? error.message : 'Unable to open WorkAMA.') }
  }

  return <main className="page"><div className="shell">
    <header className="brand"><div className="brand__identity"><span className="brand__mark" aria-hidden="true">W</span><div><p className="brand__name">WorkAMA Context</p><p className="eyebrow">A deliberate bridge from browser to chat</p></div></div><button className="button button--quiet" type="button" onClick={() => chrome.runtime.openOptionsPage()}>Settings</button></header>
    <section className="surface capture-panel"><div className="surface__body">
      <div className="capture-panel__intro"><h1>Bring the useful part with you.</h1><p>Select page text, review it, then open the WorkAMA chat entry.</p></div>
      <div className="divider" />
      <div className="field"><label className="field__label" htmlFor="base-url">WorkAMA URL</label><input className="input" id="base-url" value={session.baseUrl} onChange={(event) => update('baseUrl', event.target.value)} onBlur={() => persist('baseUrl', session.baseUrl)} inputMode="url" /></div>
      <div className="field"><label className="field__label" htmlFor="token">Session token <span className="eyebrow">(optional)</span></label><input className="input" id="token" type="password" autoComplete="off" value={session.token} onChange={(event) => update('token', event.target.value)} onBlur={() => persist('token', session.token)} /></div>
      <div className="actions"><button className="button button--primary" type="button" disabled={busy} onClick={handleCapture}>{busy ? 'Capturing…' : 'Capture selection'}</button><button className="button" type="button" disabled={!session.context} onClick={handleCopy}>Copy context</button><button className="button" type="button" onClick={handleOpen}>Open chat</button></div>
      <p className={`status${statusError ? ' status--error' : ''}`} role="status">{status}</p>
      <div className="field"><label className="field__label" htmlFor="context">Review captured context</label><textarea className="textarea" id="context" value={session.context} onChange={(event) => update('context', event.target.value)} onBlur={() => persist('context', session.context)} placeholder="Captured text appears here after you select it on a page." /></div>
      <p className="privacy"><strong>Privacy by action.</strong> Nothing is collected in the background. Session settings and context are cleared when the browser session ends; credentials never use persistent storage.</p>
    </div></section>
  </div></main>
}

createRoot(document.getElementById('root')!).render(<SidePanel />)
