const [url, mode] = process.argv.slice(2)
if (!url || !mode) throw new Error('URL and mode required')
const socket = new WebSocket(url); let artifact = false, completed = false, disabled = false
const command = mode === 'disabled-tool' ? '/tool terminal {"argv":["echo","blocked"]}' : '/artifact Summarize this profile test.'
const timer = setTimeout(() => process.exit(1), 30000)
socket.addEventListener('open', () => socket.send(JSON.stringify({ type: 'message.create', content: command, attachment_ids: [] })))
socket.addEventListener('message', ({ data }) => {
  const event = JSON.parse(data); if (typeof event.seq === 'number') socket.send(JSON.stringify({ type: 'event.ack', seq: event.seq }))
  if (event.type === 'artifact.created') artifact = true
  if (event.type === 'error' && event.payload?.code === 'E07001') disabled = true
  if (event.type === 'session.status' && event.payload?.to === 'idle') completed = true
  const done = mode === 'disabled-tool' ? disabled : completed
  if (done) { clearTimeout(timer); socket.close(); const ok = mode === 'disabled-tool' ? disabled : mode === 'canvas-on' ? artifact : !artifact; process.stdout.write(JSON.stringify({ ok, artifact, completed, disabled })); process.exit(ok ? 0 : 1) }
})
