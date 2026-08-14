/**
 * Agent Planner 会话：列表 + 步骤详情 + fork / converge。
 */
import { useState, type ReactNode } from 'react'
import { api, errorMessage } from './api'
import { useLocale } from './locale'
import { AdminCreateForm, AdminPageShell, DeleteButton, useResource } from './admin-shared'
import { Button } from './ui'

type PlannerSession = {
  id: string
  session_id?: string
  status?: string
  parent_session_id?: string
  convergence_score?: number
}

type PlannerStep = {
  id: string
  index?: number
  name?: string
  status?: string
  output?: string
}

function displayId(item: PlannerSession): string {
  return item.session_id ?? item.id
}

export default function AdminAgentPlannerPage(): ReactNode {
  const { items, loading, error, reload, create, remove, busy } = useResource<PlannerSession>(
    '/api/v1/agent/planner/sessions',
  )
  const { t } = useLocale()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [steps, setSteps] = useState<PlannerStep[]>([])
  const [stepsLoading, setStepsLoading] = useState(false)
  const [stepsError, setStepsError] = useState('')
  const [actionResult, setActionResult] = useState('')
  const [actionError, setActionError] = useState('')

  async function loadSteps(session: PlannerSession) {
    const id = session.id
    setSelectedId(displayId(session))
    setSteps([])
    setStepsError('')
    setStepsLoading(true)
    try {
      const result = await api.get<PlannerStep[] | { items?: PlannerStep[] }>(
        `/api/v1/agent/planner/sessions/${encodeURIComponent(id)}/steps`,
      )
      setSteps(Array.isArray(result) ? result : (result?.items ?? []))
    } catch (caught) {
      setStepsError(errorMessage(caught, t('admin.agentPlanner.loadStepsFailed')))
    } finally {
      setStepsLoading(false)
    }
  }

  async function fork(session: PlannerSession) {
    const id = session.id
    setActionError('')
    setActionResult(t('admin.agentPlanner.forking'))
    try {
      const result = await api.post<{ session_id?: string }>(
        `/api/v1/agent/planner/sessions/${encodeURIComponent(id)}/fork`,
      )
      setActionResult(`${t('admin.agentPlanner.forkedPrefix')}${result?.session_id ?? `${displayId(session)}-forked`}`)
    } catch (caught) {
      setActionError(errorMessage(caught, t('admin.agentPlanner.forkFailed')))
      setActionResult('')
    }
  }

  async function converge(session: PlannerSession) {
    const id = session.id
    setActionError('')
    setActionResult(t('admin.agentPlanner.converging'))
    try {
      const result = await api.post<{ convergence_score?: number; status?: string }>(
        `/api/v1/agent/planner/sessions/${encodeURIComponent(id)}/converge`,
      )
      setActionResult(
        `${t('admin.agentPlanner.convergedPrefix')}${result?.status ?? 'ok'}${
          result?.convergence_score !== undefined ? ` / score ${result.convergence_score}` : ''
        }`,
      )
    } catch (caught) {
      setActionError(errorMessage(caught, t('admin.agentPlanner.convergeFailed')))
      setActionResult('')
    }
  }

  return (
    <AdminPageShell
      title={t('admin.agentPlanner.title')}
      subtitle={t('admin.agentPlanner.subtitle')}
      testId="agent-planner-page"
      loading={loading}
      error={error}
      onRetry={reload}
    >
      <AdminCreateForm
        testId="agent-planner-create"
        fields={[
          { name: 'name', label: t('admin.agentPlanner.field.name'), placeholder: t('admin.agentPlanner.field.name.placeholder') },
          { name: 'parent_session_id', label: t('admin.agentPlanner.field.parentId'), placeholder: t('admin.agentPlanner.field.parentId.placeholder') },
        ]}
        onSubmit={(v) => create({ name: v.name, parent_session_id: v.parent_session_id || undefined })}
        busy={busy}
      />
      <ul className="resource-list" data-testid="agent-planner-list">
        {items.map((item) => (
          <li key={item.id} className="resource-item" data-testid="agent-planner-item">
            <div className="resource-info">
              <strong>{displayId(item)}</strong>
              {item.status && <small>{item.status}</small>}
              {item.parent_session_id && <span>{t('admin.agentPlanner.parentPrefix')} {item.parent_session_id}</span>}
              {item.convergence_score !== undefined && (
                <span>{t('admin.agentPlanner.scoreLabel')}{item.convergence_score}</span>
              )}
            </div>
            <div className="resource-actions">
              <Button
                variant="ghost"
                onClick={() => void loadSteps(item)}
                data-testid={`agent-planner-steps-${item.id}`}
              >
                {t('admin.agentPlanner.steps')}
              </Button>
              <Button
                variant="ghost"
                onClick={() => void fork(item)}
                data-testid={`agent-planner-fork-${item.id}`}
              >
                Fork
              </Button>
              <Button
                variant="ghost"
                onClick={() => void converge(item)}
                data-testid={`agent-planner-converge-${item.id}`}
              >
                Converge
              </Button>
              <DeleteButton
                testId={`agent-planner-delete-${item.id}`}
                onDelete={() => void remove(item.id)}
                busy={busy}
              />
            </div>
          </li>
        ))}
      </ul>
      {selectedId && (
        <div className="panel" data-testid="agent-planner-steps-panel">
          <h2>{t('admin.agentPlanner.stepsDetailTitle')} {selectedId}</h2>
          {stepsLoading && <div>{t('admin.agentPlanner.loading')}</div>}
          {stepsError && (
            <div className="alert alert-error" data-testid="agent-planner-steps-error">
              {stepsError}
            </div>
          )}
          <ul data-testid="agent-planner-steps-list">
            {steps.map((step) => (
              <li key={step.id} data-testid="agent-planner-step">
                <strong>{step.index ?? '#'} {step.name ?? step.id}</strong>
                {step.status && <small>{step.status}</small>}
                {step.output && <pre>{step.output}</pre>}
              </li>
            ))}
            {steps.length === 0 && !stepsLoading && <li>{t('admin.agentPlanner.noSteps')}</li>}
          </ul>
        </div>
      )}
      {actionError && (
        <div className="alert alert-error" data-testid="agent-planner-action-error">
          {actionError}
        </div>
      )}
      {actionResult && (
        <div className="alert alert-info" data-testid="agent-planner-action-result">
          {actionResult}
        </div>
      )}
    </AdminPageShell>
  )
}
