const url = process.env.WORKAMA_WS_URL || process.argv.slice(2)[0]
if (!url) throw new Error('url is required (argv[1] or WORKAMA_WS_URL)')
const socket = new WebSocket(url)
const replayed = []
const timeout = setTimeout(() => { process.stderr.write('Replay smoke timed out\n'); process.exit(1) }, 30000)
socket.addEventListener('message', (message) => {
  const event = JSON.parse(message.data)
  if (typeof event.seq === 'number') {
    replayed.push(event)
    socket.send(JSON.stringify({ type: 'event.ack', seq: event.seq }))
  }
  if (event.type === 'session.status') {
    const contiguous = replayed.every((item, index) => item.seq === index + 2)
    const types = [...new Set(replayed.map((item) => item.type))]
    const valid = contiguous && replayed.length >= 6 && types.includes('tool.result') && types.includes('step.finished') && event.payload?.to === 'idle'
    clearTimeout(timeout)
    process.stdout.write(JSON.stringify({ ok: valid, replayed_count: replayed.length, contiguous, event_types: types }) + '\n')
    process.exit(valid ? 0 : 1)
  }
})
