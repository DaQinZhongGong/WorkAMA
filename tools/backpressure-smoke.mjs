const url = process.env.WORKAMA_WS_URL || process.argv.slice(2)[0]
if (!url) throw new Error('url is required (argv[1] or WORKAMA_WS_URL)')
const socket = new WebSocket(url)
let warning = null
let events = 0
const timeout = setTimeout(() => { socket.close(); process.stderr.write('Backpressure smoke timed out\n'); process.exit(1) }, 30000)
socket.addEventListener('message', (message) => {
  const event = JSON.parse(message.data)
  if (typeof event.seq === 'number') events += 1
  if (event.type === 'connection.warning') warning = event.payload
})
socket.addEventListener('close', (event) => {
  clearTimeout(timeout)
  const valid = event.code === 4410 && warning?.backpressure === true && warning?.pending_events === 1001 && events === 1001
  process.stdout.write(JSON.stringify({ ok: valid, close_code: event.code, events, warning }) + '\n')
  process.exit(valid ? 0 : 1)
})
