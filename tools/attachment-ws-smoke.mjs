const [url, attachmentId] = process.argv.slice(2)
if (!url || !attachmentId) throw new Error('WebSocket URL and attachment ID are required')
const socket = new WebSocket(url)
const timeout = setTimeout(() => { socket.close(); process.exit(1) }, 30000)
let answer = '', referenceObserved = false
socket.addEventListener('open', () => socket.send(JSON.stringify({ type: 'message.create', content: 'Use the selected file as context and confirm the answer.', attachment_ids: [attachmentId] })))
socket.addEventListener('message', ({ data }) => {
  const event = JSON.parse(data)
  if (typeof event.seq === 'number') socket.send(JSON.stringify({ type: 'event.ack', seq: event.seq }))
  if (event.type === 'user.message' && event.payload?.attachment_ids?.includes(attachmentId)) referenceObserved = true
  if (event.type === 'agent.message.delta') answer += event.payload?.delta ?? ''
  if (event.type === 'error') { clearTimeout(timeout); process.stderr.write(JSON.stringify(event)); process.exit(1) }
  if (event.type === 'session.status' && event.payload?.to === 'idle') {
    clearTimeout(timeout); socket.close(); const ok = referenceObserved && answer.length > 0
    process.stdout.write(JSON.stringify({ ok, reference_observed: referenceObserved, answer_bytes: answer.length })); process.exit(ok ? 0 : 1)
  }
})
