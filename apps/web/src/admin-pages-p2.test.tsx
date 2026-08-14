import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest'
import '@testing-library/jest-dom/vitest'
import type { ReactElement } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { LocaleProvider } from './locale'

import AdminConnectorsPage from './admin-connectors-page'
import AdminAutomationsPage from './admin-automations-page'
import AdminPushPage from './admin-push-page'
import AdminDesignProjectsPage from './admin-design-projects-page'
import AdminExternalAppsPage from './admin-external-apps-page'
import AdminAgentPlannerPage from './admin-agent-planner-page'

afterEach(() => cleanup())

const apiGetMock = vi.fn()
const apiPostMock = vi.fn()
const apiDeleteMock = vi.fn()

vi.mock('./api', () => ({
  api: {
    get: (...args: unknown[]) => apiGetMock(...args),
    post: (...args: unknown[]) => apiPostMock(...args),
    delete: (...args: unknown[]) => apiDeleteMock(...args),
  },
  errorMessage: (error: unknown, fallback: string) =>
    error instanceof Error && error.message ? error.message : fallback,
  asItems: (payload: unknown) => {
    if (Array.isArray(payload)) return payload
    if (payload && typeof payload === 'object' && 'items' in payload) {
      return (payload as { items: unknown[] }).items
    }
    return []
  },
}))

vi.mock('./auth', () => ({
  useAuth: () => ({
    authenticated: true,
    isAdmin: true,
    user: { display_name: 'Admin', email: 'admin@example.com', role: 'admin' },
  }),
}))

function renderWithProviders(ui: ReactElement) {
  return render(<MemoryRouter><LocaleProvider>{ui}</LocaleProvider></MemoryRouter>)
}

beforeEach(() => {
  apiGetMock.mockReset()
  apiPostMock.mockReset()
  apiDeleteMock.mockReset()
})

// ============ AdminConnectorsPage ============
describe('AdminConnectorsPage', () => {
  it('渲染连接器页面标题', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    renderWithProviders(<AdminConnectorsPage />)
    await waitFor(() => expect(screen.getByTestId('connectors-page')).toBeInTheDocument())
    expect(screen.getByText('企业知识连接器 v2')).toBeInTheDocument()
  })

  it('加载并渲染连接器列表', async () => {
    apiGetMock.mockResolvedValue({
      items: [{ id: 'c-1', name: 'GitHub', provider: 'github', status: 'active', last_cursor: 'abc' }],
    })
    renderWithProviders(<AdminConnectorsPage />)
    await waitFor(() => expect(screen.getByTestId('connectors-item')).toBeInTheDocument())
    expect(screen.getByText('GitHub')).toBeInTheDocument()
    expect(screen.getByText('github')).toBeInTheDocument()
  })

  it('连接器加载失败时显示错误', async () => {
    apiGetMock.mockRejectedValue(new Error('connectors down'))
    renderWithProviders(<AdminConnectorsPage />)
    await waitFor(() => expect(screen.getByText(/connectors down/i)).toBeInTheDocument(), { timeout: 5000 })
  })

  it('创建连接器时调用 POST', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    apiPostMock.mockResolvedValue({ id: 'c-2', name: 'Confluence' })
    renderWithProviders(<AdminConnectorsPage />)
    await waitFor(() => expect(screen.getByTestId('connectors-create')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('connectors-create-name'), { target: { value: 'Confluence' } })
    fireEvent.change(screen.getByTestId('connectors-create-provider'), { target: { value: 'confluence' } })
    fireEvent.submit(screen.getByTestId('connectors-create'))
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/v1/connectors/v2', {
        name: 'Confluence',
        provider: 'confluence',
      }),
    )
  })

  it('删除连接器时调用 DELETE', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 'c-1', name: 'GitHub', provider: 'github' }] })
    apiDeleteMock.mockResolvedValue(undefined)
    renderWithProviders(<AdminConnectorsPage />)
    await waitFor(() => expect(screen.getByTestId('connectors-item')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('connectors-delete-c-1'))
    await waitFor(() =>
      expect(apiDeleteMock).toHaveBeenCalledWith('/api/v1/connectors/v2/c-1'),
    )
  })

  it('点击同步按钮调用 sync POST 并显示 operation_id', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 'c-1', name: 'GitHub', provider: 'github' }] })
    apiPostMock.mockResolvedValue({ operation_id: 'op-xyz' })
    renderWithProviders(<AdminConnectorsPage />)
    await waitFor(() => expect(screen.getByTestId('connectors-sync-c-1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('connectors-sync-c-1'))
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/v1/connectors/v2/c-1/sync'),
    )
    await waitFor(() => expect(screen.getByTestId('connectors-sync-result').textContent).toContain('op-xyz'))
  })

  it('点击 dry-run 按钮展示预览结果', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 'c-1', name: 'GitHub', provider: 'github' }] })
    apiPostMock.mockResolvedValue({ auth: { ok: true }, discover: { count: 5 }, acl: {}, deletion: { dry: true } })
    renderWithProviders(<AdminConnectorsPage />)
    await waitFor(() => expect(screen.getByTestId('connectors-dryrun-c-1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('connectors-dryrun-c-1'))
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/v1/connectors/v2/c-1/dry-run'),
    )
    await waitFor(() => expect(screen.getByTestId('connectors-dryrun-result')).toBeInTheDocument())
  })
})

// ============ AdminAutomationsPage ============
describe('AdminAutomationsPage', () => {
  it('渲染自动化页面标题', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    renderWithProviders(<AdminAutomationsPage />)
    await waitFor(() => expect(screen.getByTestId('automations-page')).toBeInTheDocument())
    expect(screen.getByText('自动化 v2')).toBeInTheDocument()
  })

  it('加载并渲染自动化触发器列表', async () => {
    apiGetMock.mockResolvedValue({
      items: [
        {
          id: 'a-1',
          name: 'Daily Report',
          trigger_type: 'cron',
          cron_or_webhook_url: '0 8 * * *',
          enabled: true,
          last_run_at: '2026-07-29',
        },
      ],
    })
    renderWithProviders(<AdminAutomationsPage />)
    await waitFor(() => expect(screen.getByTestId('automations-item')).toBeInTheDocument())
    expect(screen.getByText('Daily Report')).toBeInTheDocument()
    expect(screen.getByText('0 8 * * *')).toBeInTheDocument()
  })

  it('自动化加载失败时显示错误', async () => {
    apiGetMock.mockRejectedValue(new Error('automations unavailable'))
    renderWithProviders(<AdminAutomationsPage />)
    await waitFor(() => expect(screen.getByText(/automations unavailable/i)).toBeInTheDocument(), { timeout: 5000 })
  })

  it('创建自动化触发器时调用 POST', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    apiPostMock.mockResolvedValue({ id: 'a-2', name: 'Webhook Trigger' })
    renderWithProviders(<AdminAutomationsPage />)
    await waitFor(() => expect(screen.getByTestId('automations-create')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('automations-create-name'), { target: { value: 'Webhook Trigger' } })
    fireEvent.change(screen.getByTestId('automations-create-trigger_type'), { target: { value: 'webhook' } })
    fireEvent.change(screen.getByTestId('automations-create-cron_or_webhook_url'), { target: { value: 'https://example.com/hook' } })
    fireEvent.submit(screen.getByTestId('automations-create'))
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/v1/automations/v2/triggers', {
        name: 'Webhook Trigger',
        trigger_type: 'webhook',
        cron_or_webhook_url: 'https://example.com/hook',
      }),
    )
  })

  it('删除自动化触发器时调用 DELETE', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 'a-1', name: 'Daily', trigger_type: 'cron' }] })
    apiDeleteMock.mockResolvedValue(undefined)
    renderWithProviders(<AdminAutomationsPage />)
    await waitFor(() => expect(screen.getByTestId('automations-item')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('automations-delete-a-1'))
    await waitFor(() =>
      expect(apiDeleteMock).toHaveBeenCalledWith('/api/v1/automations/v2/triggers/a-1'),
    )
  })

  it('点击立即运行按钮调用 run POST', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 'a-1', name: 'Daily', trigger_type: 'cron', enabled: true }] })
    apiPostMock.mockResolvedValue({ run_id: 'run-123' })
    renderWithProviders(<AdminAutomationsPage />)
    await waitFor(() => expect(screen.getByTestId('automations-run-a-1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('automations-run-a-1'))
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/v1/automations/v2/triggers/a-1/run'),
    )
    await waitFor(() => expect(screen.getByTestId('automations-run-result').textContent).toContain('run-123'))
  })

  it('点击 toggle 按钮调用 toggle POST', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 'a-1', name: 'Daily', trigger_type: 'cron', enabled: true }] })
    apiPostMock.mockResolvedValue({})
    renderWithProviders(<AdminAutomationsPage />)
    await waitFor(() => expect(screen.getByTestId('automations-toggle-a-1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('automations-toggle-a-1'))
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/v1/automations/v2/triggers/a-1/toggle'),
    )
  })
})

// ============ AdminPushPage ============
describe('AdminPushPage', () => {
  it('渲染推送通知页面标题', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    renderWithProviders(<AdminPushPage />)
    await waitFor(() => expect(screen.getByTestId('push-page')).toBeInTheDocument())
    expect(screen.getByText('推送通知')).toBeInTheDocument()
  })

  it('加载并渲染推送订阅列表（掩码 endpoint）', async () => {
    apiGetMock.mockResolvedValue({
      items: [
        {
          id: 's-1',
          endpoint: 'https://fcm.googleapis.com/fcm/send/abcdefghijklmnop',
          user_id: 'u-1',
        },
      ],
    })
    renderWithProviders(<AdminPushPage />)
    await waitFor(() => expect(screen.getByTestId('push-item')).toBeInTheDocument())
    // endpoint 被掩码：前 8 + **** + 后 4
    expect(screen.getByText(/https:/)).toBeInTheDocument()
    expect(screen.getByText(/u-1/)).toBeInTheDocument()
  })

  it('推送订阅加载失败时显示错误', async () => {
    apiGetMock.mockRejectedValue(new Error('push err'))
    renderWithProviders(<AdminPushPage />)
    await waitFor(() => expect(screen.getByText(/push err/i)).toBeInTheDocument(), { timeout: 5000 })
  })

  it('订阅推送时调用 POST', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    apiPostMock.mockResolvedValue({ id: 's-2', endpoint: 'https://fcm.example.com/xyz' })
    renderWithProviders(<AdminPushPage />)
    await waitFor(() => expect(screen.getByTestId('push-subscribe')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('push-subscribe-endpoint'), { target: { value: 'https://fcm.example.com/xyz' } })
    fireEvent.change(screen.getByTestId('push-subscribe-user_id'), { target: { value: 'u-2' } })
    fireEvent.submit(screen.getByTestId('push-subscribe'))
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/v1/push/subscriptions', {
        endpoint: 'https://fcm.example.com/xyz',
        user_id: 'u-2',
      }),
    )
  })

  it('删除推送订阅时调用 DELETE', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 's-1', endpoint: 'https://fcm.example.com/abcdef' }] })
    apiDeleteMock.mockResolvedValue(undefined)
    renderWithProviders(<AdminPushPage />)
    await waitFor(() => expect(screen.getByTestId('push-item')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('push-delete-s-1'))
    await waitFor(() =>
      expect(apiDeleteMock).toHaveBeenCalledWith('/api/v1/push/subscriptions/s-1'),
    )
  })

  it('发送推送时调用 /push/send POST', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    apiPostMock.mockResolvedValue({ message_id: 'm-1' })
    renderWithProviders(<AdminPushPage />)
    await waitFor(() => expect(screen.getByTestId('push-send-form')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('push-send-title'), { target: { value: 'Hello' } })
    fireEvent.change(screen.getByTestId('push-send-body'), { target: { value: 'World' } })
    fireEvent.change(screen.getByTestId('push-send-target'), { target: { value: 'u-1' } })
    fireEvent.click(screen.getByTestId('push-send-submit'))
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/v1/push/send', {
        title: 'Hello',
        body: 'World',
        target: 'u-1',
      }),
    )
  })
})

// ============ AdminDesignProjectsPage ============
describe('AdminDesignProjectsPage', () => {
  it('渲染 AMA-Design 项目页面标题', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    renderWithProviders(<AdminDesignProjectsPage />)
    await waitFor(() => expect(screen.getByTestId('design-projects-page')).toBeInTheDocument())
    expect(screen.getByText('AMA-Design 项目')).toBeInTheDocument()
  })

  it('加载并渲染设计项目列表', async () => {
    apiGetMock.mockResolvedValue({
      items: [{ id: 'p-1', name: 'Logo Set', status: 'ready', asset_count: 12 }],
    })
    renderWithProviders(<AdminDesignProjectsPage />)
    await waitFor(() => expect(screen.getByTestId('design-projects-item')).toBeInTheDocument())
    expect(screen.getByText('Logo Set')).toBeInTheDocument()
    expect(screen.getByText(/资产 12/)).toBeInTheDocument()
  })

  it('设计项目加载失败时显示错误', async () => {
    apiGetMock.mockRejectedValue(new Error('design fail'))
    renderWithProviders(<AdminDesignProjectsPage />)
    await waitFor(() => expect(screen.getByText(/design fail/i)).toBeInTheDocument(), { timeout: 5000 })
  })

  it('创建设计项目时调用 POST', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    apiPostMock.mockResolvedValue({ id: 'p-2', name: 'New Project' })
    renderWithProviders(<AdminDesignProjectsPage />)
    await waitFor(() => expect(screen.getByTestId('design-projects-create')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('design-projects-create-name'), { target: { value: 'New Project' } })
    fireEvent.submit(screen.getByTestId('design-projects-create'))
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/v1/design/projects', { name: 'New Project' }),
    )
  })

  it('删除设计项目时调用 DELETE', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 'p-1', name: 'Logo Set' }] })
    apiDeleteMock.mockResolvedValue(undefined)
    renderWithProviders(<AdminDesignProjectsPage />)
    await waitFor(() => expect(screen.getByTestId('design-projects-item')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('design-projects-delete-p-1'))
    await waitFor(() =>
      expect(apiDeleteMock).toHaveBeenCalledWith('/api/v1/design/projects/p-1'),
    )
  })

  it('点击详情按钮加载资产与任务', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 'p-1', name: 'Logo Set', asset_count: 1 }] })
    // 详情加载时调用两次 api.get（assets + tasks）
    apiGetMock
      .mockResolvedValueOnce({ items: [{ id: 'p-1', name: 'Logo Set', asset_count: 1 }] })
      .mockResolvedValueOnce([{ id: 'a-1', name: 'logo.png', type: 'image' }])
      .mockResolvedValueOnce([{ id: 't-1', status: 'done', prompt: 'generate logo' }])
    renderWithProviders(<AdminDesignProjectsPage />)
    await waitFor(() => expect(screen.getByTestId('design-projects-detail-p-1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('design-projects-detail-p-1'))
    await waitFor(() => expect(screen.getByTestId('design-projects-detail-panel')).toBeInTheDocument())
    await waitFor(() => expect(screen.getByTestId('design-projects-asset')).toBeInTheDocument())
    expect(screen.getByText('logo.png')).toBeInTheDocument()
    expect(screen.getByText('generate logo')).toBeInTheDocument()
  })
})

// ============ AdminExternalAppsPage ============
describe('AdminExternalAppsPage', () => {
  it('渲染外部应用页面标题', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    renderWithProviders(<AdminExternalAppsPage />)
    await waitFor(() => expect(screen.getByTestId('external-apps-page')).toBeInTheDocument())
    expect(screen.getByText('外部应用')).toBeInTheDocument()
  })

  it('加载并渲染外部应用列表（掩码 endpoint）', async () => {
    apiGetMock.mockResolvedValue({
      items: [
        {
          id: 'app-1',
          name: 'Slack Bot',
          provider: 'slack',
          execution_mode: 'sync',
          endpoint: 'https://example.com/mock-slack-webhook',
        },
      ],
    })
    renderWithProviders(<AdminExternalAppsPage />)
    await waitFor(() => expect(screen.getByTestId('external-apps-item')).toBeInTheDocument())
    expect(screen.getByText('Slack Bot')).toBeInTheDocument()
    expect(screen.getByText('slack')).toBeInTheDocument()
    expect(screen.getByText('sync')).toBeInTheDocument()
  })

  it('外部应用加载失败时显示错误', async () => {
    apiGetMock.mockRejectedValue(new Error('ext apps err'))
    renderWithProviders(<AdminExternalAppsPage />)
    await waitFor(() => expect(screen.getByText(/ext apps err/i)).toBeInTheDocument(), { timeout: 5000 })
  })

  it('创建外部应用时调用 POST', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    apiPostMock.mockResolvedValue({ id: 'app-2', name: 'GitHub App' })
    renderWithProviders(<AdminExternalAppsPage />)
    await waitFor(() => expect(screen.getByTestId('external-apps-create')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('external-apps-create-name'), { target: { value: 'GitHub App' } })
    fireEvent.change(screen.getByTestId('external-apps-create-provider'), { target: { value: 'github' } })
    fireEvent.change(screen.getByTestId('external-apps-create-execution_mode'), { target: { value: 'async' } })
    fireEvent.change(screen.getByTestId('external-apps-create-endpoint'), { target: { value: 'https://api.github.com' } })
    fireEvent.submit(screen.getByTestId('external-apps-create'))
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/v1/external-apps', {
        name: 'GitHub App',
        provider: 'github',
        execution_mode: 'async',
        endpoint: 'https://api.github.com',
      }),
    )
  })

  it('删除外部应用时调用 DELETE', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 'app-1', name: 'Slack Bot', provider: 'slack' }] })
    apiDeleteMock.mockResolvedValue(undefined)
    renderWithProviders(<AdminExternalAppsPage />)
    await waitFor(() => expect(screen.getByTestId('external-apps-item')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('external-apps-delete-app-1'))
    await waitFor(() =>
      expect(apiDeleteMock).toHaveBeenCalledWith('/api/v1/external-apps/app-1'),
    )
  })

  it('点击健康检查按钮调用 health POST', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 'app-1', name: 'Slack Bot', provider: 'slack' }] })
    apiPostMock.mockResolvedValue({ status: 'healthy', latency_ms: 42 })
    renderWithProviders(<AdminExternalAppsPage />)
    await waitFor(() => expect(screen.getByTestId('external-apps-health-app-1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('external-apps-health-app-1'))
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/v1/external-apps/app-1/health'),
    )
    await waitFor(() => expect(screen.getByTestId('external-apps-health-result').textContent).toContain('healthy'))
    expect(screen.getByTestId('external-apps-health-result').textContent).toContain('42')
  })
})

// ============ AdminAgentPlannerPage ============
describe('AdminAgentPlannerPage', () => {
  it('渲染 Agent Planner 页面标题', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    renderWithProviders(<AdminAgentPlannerPage />)
    await waitFor(() => expect(screen.getByTestId('agent-planner-page')).toBeInTheDocument())
    expect(screen.getByText('Agent Planner 会话')).toBeInTheDocument()
  })

  it('加载并渲染 Planner 会话列表', async () => {
    apiGetMock.mockResolvedValue({
      items: [
        {
          id: 's-1',
          session_id: 'sess-001',
          status: 'running',
          parent_session_id: 'sess-000',
          convergence_score: 0.87,
        },
      ],
    })
    renderWithProviders(<AdminAgentPlannerPage />)
    await waitFor(() => expect(screen.getByTestId('agent-planner-item')).toBeInTheDocument())
    expect(screen.getByText('sess-001')).toBeInTheDocument()
    expect(screen.getByText('running')).toBeInTheDocument()
    expect(screen.getByText(/score: 0.87/)).toBeInTheDocument()
  })

  it('Planner 会话加载失败时显示错误', async () => {
    apiGetMock.mockRejectedValue(new Error('planner err'))
    renderWithProviders(<AdminAgentPlannerPage />)
    await waitFor(() => expect(screen.getByText(/planner err/i)).toBeInTheDocument(), { timeout: 5000 })
  })

  it('创建 Planner 会话时调用 POST', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    apiPostMock.mockResolvedValue({ id: 's-2', session_id: 'sess-002' })
    renderWithProviders(<AdminAgentPlannerPage />)
    await waitFor(() => expect(screen.getByTestId('agent-planner-create')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('agent-planner-create-name'), { target: { value: 'Plan A' } })
    fireEvent.change(screen.getByTestId('agent-planner-create-parent_session_id'), { target: { value: 'sess-001' } })
    fireEvent.submit(screen.getByTestId('agent-planner-create'))
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/v1/agent/planner/sessions', {
        name: 'Plan A',
        parent_session_id: 'sess-001',
      }),
    )
  })

  it('删除 Planner 会话时调用 DELETE', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 's-1', session_id: 'sess-001', status: 'done' }] })
    apiDeleteMock.mockResolvedValue(undefined)
    renderWithProviders(<AdminAgentPlannerPage />)
    await waitFor(() => expect(screen.getByTestId('agent-planner-item')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('agent-planner-delete-s-1'))
    await waitFor(() =>
      expect(apiDeleteMock).toHaveBeenCalledWith('/api/v1/agent/planner/sessions/s-1'),
    )
  })

  it('点击 fork 按钮调用 fork POST', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 's-1', session_id: 'sess-001', status: 'done' }] })
    apiPostMock.mockResolvedValue({ session_id: 'sess-001-forked' })
    renderWithProviders(<AdminAgentPlannerPage />)
    await waitFor(() => expect(screen.getByTestId('agent-planner-fork-s-1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('agent-planner-fork-s-1'))
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/v1/agent/planner/sessions/s-1/fork'),
    )
    await waitFor(() => expect(screen.getByTestId('agent-planner-action-result').textContent).toContain('sess-001-forked'))
  })

  it('点击 converge 按钮调用 converge POST', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 's-1', session_id: 'sess-001', status: 'done' }] })
    apiPostMock.mockResolvedValue({ status: 'converged', convergence_score: 0.95 })
    renderWithProviders(<AdminAgentPlannerPage />)
    await waitFor(() => expect(screen.getByTestId('agent-planner-converge-s-1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('agent-planner-converge-s-1'))
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/v1/agent/planner/sessions/s-1/converge'),
    )
    await waitFor(() => expect(screen.getByTestId('agent-planner-action-result').textContent).toContain('converged'))
    expect(screen.getByTestId('agent-planner-action-result').textContent).toContain('0.95')
  })

  it('点击步骤按钮加载步骤列表', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 's-1', session_id: 'sess-001', status: 'done' }] })
    apiGetMock
      .mockResolvedValueOnce({ items: [{ id: 's-1', session_id: 'sess-001', status: 'done' }] })
      .mockResolvedValueOnce([{ id: 'st-1', index: 1, name: 'analyze', status: 'done', output: 'ok' }])
    renderWithProviders(<AdminAgentPlannerPage />)
    await waitFor(() => expect(screen.getByTestId('agent-planner-steps-s-1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('agent-planner-steps-s-1'))
    await waitFor(() => expect(screen.getByTestId('agent-planner-steps-panel')).toBeInTheDocument())
    await waitFor(() => expect(screen.getByTestId('agent-planner-step')).toBeInTheDocument())
    expect(screen.getByText(/analyze/)).toBeInTheDocument()
  })
})
