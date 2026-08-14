import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import '../styles.css'
import { captureSelection, openSidePanel } from '../shared/capture'
import { loadSession } from '../shared/storage'
import { redactSensitiveText } from '../shared/safety'

function Popup() {
  const [status, setStatus] = useState('Choose an action to begin.')
  const [busy, setBusy] = useState(false)
  async function openPanel() { try { await openSidePanel(); window.close() } catch (error) { setStatus(error instanceof Error ? error.message : 'Unable to open side panel.') } }
  async function captureAndOpen() {
    setBusy(true); setStatus('Capturing selected text…')
    try {
      const result = await captureSelection()
      if (!result.ok) return setStatus(result.error)
      const context = [result.title, result.url, '', redactSensitiveText(result.text)].filter(Boolean).join('\n')
      await chrome.storage.session.set({ ...(await loadSession()), context })
      await openSidePanel(); window.close()
    } catch (error) { setStatus(error instanceof Error ? error.message : 'Capture failed.') }
    finally { setBusy(false) }
  }
  return <main className="page page--compact"><div className="shell">
    <header className="brand"><div className="brand__identity"><span className="brand__mark" aria-hidden="true">W</span><div><p className="brand__name">WorkAMA</p><p className="eyebrow">Context capture</p></div></div></header>
    <section className="surface"><div className="surface__body"><div className="surface__header"><div><h1 className="surface__title">Ready when you are</h1><p className="surface__description">Keep the useful context close to your conversation.</p></div></div><div className="actions"><button className="button button--primary" type="button" disabled={busy} onClick={captureAndOpen}>{busy ? 'Capturing…' : 'Capture selection'}</button><button className="button" type="button" onClick={openPanel}>Open side panel</button></div><p className="status" role="status">{status}</p><p className="privacy">Only a user-selected excerpt is read. Sensitive patterns are filtered before the context is saved to session storage.</p></div></section>
  </div></main>
}

createRoot(document.getElementById('root')!).render(<Popup />)
