const [url] = process.argv.slice(2)
if (!url) throw new Error('WebSocket URL is required')

const commands = [
  '/tool file.write {"path":"validation/result.txt","content":"WorkAMA tool runtime"}',
  '/tool file.read {"path":"validation/result.txt"}',
  '/tool code_interpreter {"code":"print(sum(i*i for i in range(6)))"}',
]
const results = []
const eventTypes = []
const socket = new WebSocket(url)
const timeout = setTimeout(() => { socket.close(); process.stderr.write('Tool smoke timed out\n'); process.exit(1) }, 30000)

function sendNext() {
  const command = commands[results.length]
  if (command) socket.send(JSON.stringify({ type: 'message.create', content: command, attachment_ids: [] }))
}
socket.addEventListener('open', sendNext)
socket.addEventListener('message', (message) => {
  const event = JSON.parse(message.data)
  eventTypes.push(event.type)
  if (typeof event.seq === 'number') socket.send(JSON.stringify({ type: 'event.ack', seq: event.seq }))
  if (event.type === 'tool.result') {
    if (event.payload?.status !== 'succeeded') { clearTimeout(timeout); process.stderr.write(JSON.stringify(event) + '\n'); process.exit(1) }
    results.push(event.payload)
  }
  if (event.type === 'session.status' && results.length === commands.length) {
    clearTimeout(timeout); socket.close()
    const valid = String(results[1]?.output).includes('WorkAMA tool runtime') && String(results[2]?.output?.output).trim() === '55' && eventTypes.includes('sandbox.status') && eventTypes.includes('artifact.created')
    process.stdout.write(JSON.stringify({ ok: valid, results, event_types: [...new Set(eventTypes)] }) + '\n')
    process.exit(valid ? 0 : 1)
  }
  if (event.type === 'session.status') sendNext()
})
socket.addEventListener('error', () => { clearTimeout(timeout); process.stderr.write('Tool WebSocket failed\n'); process.exit(1) })
