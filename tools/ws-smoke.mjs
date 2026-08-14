const [url, prompt = 'WorkAMA integration smoke test'] = process.argv.slice(2)
if (!url) throw new Error('WebSocket URL is required')

const socket = new WebSocket(url)
const timeout = setTimeout(() => {
  socket.close()
  process.stderr.write('WebSocket smoke test timed out\n')
  process.exit(1)
}, 30000)

let content = ''
socket.addEventListener('open', () => socket.send(JSON.stringify({ type: 'message.create', content: prompt, attachment_ids: [] })))
socket.addEventListener('message', (message) => {
  const event = JSON.parse(message.data)
  if (typeof event.seq === 'number') socket.send(JSON.stringify({ type: 'event.ack', seq: event.seq }))
  if (event.type === 'agent.message.delta' || event.type === 'message.delta') content += event.payload?.delta ?? event.payload?.content ?? ''
  if (event.type === 'error' || event.type === 'run.failed') {
    clearTimeout(timeout)
    process.stderr.write(`${event.payload?.message ?? 'Agent failed'}\n`)
    process.exit(1)
  }
  if (event.type === 'session.completed' || (event.type === 'session.status' && event.payload?.to === 'idle')) {
    clearTimeout(timeout)
    socket.close()
    process.stdout.write(JSON.stringify({ ok: content.length > 0, content }) + '\n')
    process.exit(content.length > 0 ? 0 : 1)
  }
})
socket.addEventListener('error', () => {
  clearTimeout(timeout)
  process.stderr.write('WebSocket connection failed\n')
  process.exit(1)
})
