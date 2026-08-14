export type AgentEvent = {
  id?: string
  seq?: number
  type: string
  payload?: Record<string, unknown>
}

export type ChatMessage = {
  id: string
  role: 'user' | 'assistant'
  content: string
  streaming?: boolean
  attachmentIds?: string[]
}

export type Artifact = {
  id: string
  name: string
  contentType: string
}

export type ToolActivity = { id: string; name: string; risk: string; status: string; summary?: string; output?: unknown }
export type ApprovalActivity = { id: string; callId: string; target: string; risk: string; status: string; preview?: unknown; expiry?: string }
export type AgentTask = { id: string; title: string; status: string }

export type SessionProjection = {
  messages: ChatMessage[]
  artifacts: Artifact[]
  tools: ToolActivity[]
  approvals: ApprovalActivity[]
  tasks: AgentTask[]
  taskProgress: number
  status: string
  usage: { steps: number; credits: number }
  budgetRemaining: { steps: number; credits: number }
  running: boolean
  error: string | null
  lastSeq: number
}

export const emptyProjection = (): SessionProjection => ({
  messages: [], artifacts: [], tools: [], approvals: [], tasks: [], taskProgress: 0, status: 'idle', usage: { steps: 0, credits: 0 }, budgetRemaining: { steps: 0, credits: 0 }, running: false, error: null, lastSeq: 0,
})

export function applyEvent(state: SessionProjection, event: AgentEvent): SessionProjection {
  const next: SessionProjection = {
    ...state,
    messages: [...state.messages],
    artifacts: [...state.artifacts],
    tools: state.tools.map((item) => ({ ...item })),
    approvals: state.approvals.map((item) => ({ ...item })),
    tasks: state.tasks.map((item) => ({ ...item })),
    lastSeq: Math.max(state.lastSeq, event.seq ?? 0),
  }
  const payload = event.payload ?? {}
  if (event.type === 'message.created' || event.type === 'user.message') {
    next.messages.push({
      id: event.id ?? `message-${next.messages.length}`,
      role: 'user',
      content: String(payload.content ?? ''),
      attachmentIds: Array.isArray(payload.attachment_ids) ? payload.attachment_ids.map(String) : [],
    })
    next.running = true
  } else if (event.type === 'message.delta' || event.type === 'agent.message.delta') {
    let message = [...next.messages].reverse().find((item) => item.role === 'assistant' && item.streaming)
    if (!message) {
      message = { id: event.id ?? `assistant-${next.messages.length}`, role: 'assistant', content: '', streaming: true }
      next.messages.push(message)
    }
    message.content += String(payload.content ?? payload.delta ?? '')
  } else if (event.type === 'message.completed' || event.type === 'agent.message.completed') {
    const message = [...next.messages].reverse().find((item) => item.role === 'assistant' && item.streaming)
    if (message) {
      message.content = String(payload.content ?? message.content)
      message.streaming = false
    } else {
      next.messages.push({
        id: event.id ?? `assistant-${next.messages.length}`,
        role: 'assistant',
        content: String(payload.content ?? ''),
      })
    }
  } else if (event.type === 'artifact.created') {
    next.artifacts.unshift({
      id: String(payload.id ?? payload.artifact_id ?? ''),
      name: String(payload.name ?? 'artifact'),
      contentType: String(payload.content_type ?? 'text/markdown'),
    })
  } else if (event.type === 'tool.call') {
    next.tools.unshift({ id: String(payload.call_id ?? event.id ?? ''), name: String(payload.tool ?? 'tool'), risk: String(payload.risk ?? 'A1'), status: 'running' })
    next.running = true
  } else if (event.type === 'tool.result') {
    const activity = next.tools.find((item) => item.id === String(payload.call_id ?? ''))
    if (activity) { activity.status = String(payload.status ?? 'completed'); activity.summary = String(payload.summary ?? ''); activity.output = payload.output }
    next.running = false
  } else if (event.type === 'tool.approval_required') {
    next.approvals.unshift({ id: String(payload.approval_id ?? event.id ?? ''), callId: String(payload.call_id ?? ''), target: String(payload.target ?? 'tool'), risk: String(payload.risk ?? 'A3'), status: 'pending', preview: payload.preview, expiry: String(payload.expiry ?? '') })
  } else if (event.type === 'tool.approval_decided') {
    const approval = next.approvals.find((item) => item.id === String(payload.approval_id ?? ''))
    if (approval) approval.status = String(payload.decision ?? 'decided')
  } else if (event.type === 'task.list.updated') {
    next.tasks = Array.isArray(payload.tasks) ? payload.tasks.map((item) => { const task = item as Record<string, unknown>; return { id: String(task.id ?? ''), title: String(task.title ?? ''), status: String(task.status ?? 'pending') } }) : []
    next.taskProgress = Number(payload.progress ?? 0)
  } else if (event.type === 'usage.updated') {
    const usage = (payload.session_usage ?? {}) as Record<string, unknown>; const remaining = (payload.budget_remaining ?? {}) as Record<string, unknown>
    next.usage = { steps: Number(usage.steps ?? 0), credits: Number(usage.credits ?? 0) }
    next.budgetRemaining = { steps: Number(remaining.steps ?? 0), credits: Number(remaining.credits ?? 0) }
  } else if (event.type === 'session.completed' || event.type === 'session.status') {
    next.status = String(payload.to ?? 'completed')
    next.running = next.status === 'running'
  } else if (event.type === 'run.failed' || event.type === 'error') {
    next.running = false
    next.error = String(payload.message ?? 'Agent run failed')
  }
  return next
}

export function projectEvents(events: AgentEvent[]): SessionProjection {
  return events.reduce(applyEvent, emptyProjection())
}
