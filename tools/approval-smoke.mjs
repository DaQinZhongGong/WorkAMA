const [url, accessToken, decision, marker] = process.argv.slice(2)
if (!url || !accessToken || !['approved', 'rejected'].includes(decision)) throw new Error('url, access token and decision are required')

const command = decision === 'approved'
  ? `/tool terminal {"argv":["printf","${marker}"],"timeout_seconds":10}`
  : `/tool terminal {"argv":["touch","${marker}"],"timeout_seconds":10}`
const socket = new WebSocket(url)
const observed = []
let approval = null
let resultEvent = null
const timeout = setTimeout(() => { socket.close(); process.stderr.write('Approval smoke timed out\n'); process.exit(1) }, 30000)

socket.addEventListener('open', () => socket.send(JSON.stringify({ type: 'message.create', content: command, attachment_ids: [] })))
socket.addEventListener('message', async (message) => {
  const event = JSON.parse(message.data)
  observed.push(event.type)
  if (typeof event.seq === 'number') socket.send(JSON.stringify({ type: 'event.ack', seq: event.seq }))
  if (event.type === 'tool.approval_required') {
    approval = event.payload
    const response = await fetch(`http://localhost:20200/api/v1/approvals/${approval.approval_id}/decisions`, {
      method: 'POST', headers: { Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, reason: `Automated ${decision} acceptance` }),
    })
    if (!response.ok) { clearTimeout(timeout); process.stderr.write(await response.text()); process.exit(1) }
  }
  if (event.type === 'tool.result') {
    resultEvent = event
  }
  if (event.type === 'session.status' && resultEvent) {
    const expectedStatus = decision === 'approved' ? 'succeeded' : 'rejected'
    const outputValid = decision === 'rejected' || String(resultEvent.payload?.output?.output ?? '') === marker
    const valid = approval && resultEvent.payload?.status === expectedStatus && outputValid && observed.includes('tool.approval_decided') && !observed.includes('error')
    clearTimeout(timeout); socket.close()
    process.stdout.write(JSON.stringify({ ok: valid, approval_id: approval?.approval_id, action_hash: approval?.action_hash, decision, result_status: resultEvent.payload?.status, event_types: [...new Set(observed)] }) + '\n')
    process.exit(valid ? 0 : 1)
  }
})
socket.addEventListener('error', () => { clearTimeout(timeout); process.stderr.write('Approval WebSocket failed\n'); process.exit(1) })
