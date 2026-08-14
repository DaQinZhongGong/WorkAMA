const url = process.env.WORKAMA_WS_URL || process.argv.slice(2)[0]
if (!url) { process.stderr.write('url is required (argv[1] or WORKAMA_WS_URL)\n'); process.exit(2) }
const socket = new WebSocket(url)
const timeout = setTimeout(() => process.exit(1), 30000)
socket.addEventListener('open', () => socket.send(JSON.stringify({ type: 'message.create', content: '/tool file.write {"path":"replay/state.txt","content":"replay-ok"}', attachment_ids: [] })))
socket.addEventListener('message', (message) => {
  const event = JSON.parse(message.data)
  if (typeof event.seq === 'number') socket.send(JSON.stringify({ type: 'event.ack', seq: event.seq }))
  if (event.type === 'session.status' && event.payload?.to === 'idle') { clearTimeout(timeout); process.stdout.write('{"ok":true}\n'); process.exit(0) }
})
