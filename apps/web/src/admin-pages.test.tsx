import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest'
import '@testing-library/jest-dom/vitest'
import type { ReactElement } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { LocaleProvider } from './locale'

import AdminDashboardPage from './admin-dashboard-page'
import AdminWorkspacesPage from './workspaces-page'
import AdminAssistantsPage from './assistants-page'
import AdminWorkflowsPage from './workflows-page'
import AdminKnowledgeBasesPage from './knowledge-bases-page'
import AdminDevicesPage from './devices-page'
import AdminBillingPage from './billing-page'
import AdminAuditLogsPage from './audit-logs-page'
import AdminMcpToolsPage from './mcp-tools-page'
import AdminNotificationsPage from './notifications-page'
import AdminFilesPage from './files-page'
import AdminMemoryVectorsPage from './memory-vectors-page'

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
  useAuth: () => ({ authenticated: true, isAdmin: true, user: { display_name: 'Admin', email: 'admin@example.com', role: 'admin' } }),
}))

function renderWithProviders(ui: ReactElement) {
  return render(<MemoryRouter><LocaleProvider>{ui}</LocaleProvider></MemoryRouter>)
}

function rejectOnce(message: string) {
  return Promise.reject(new Error(message))
}

beforeEach(() => {
  apiGetMock.mockReset()
  apiPostMock.mockReset()
  apiDeleteMock.mockReset()
})

describe('AdminDashboardPage', () => {
  it('渲染仪表盘并加载统计数据', async () => {
    apiGetMock.mockResolvedValue({ workspaces: 5, assistants: 3, knowledge_bases: 2, devices: 10, unread_notifications: 7, current_plan: 'pro' })
    renderWithProviders(<AdminDashboardPage />)
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith('/api/v1/admin/stats'))
    await waitFor(() => expect(screen.getByTestId('stat-workspaces').textContent).toContain('5'))
    expect(screen.getByTestId('stat-plan').textContent).toContain('pro')
  })

  it('仪表盘加载失败时容忍错误（Promise.allSettled 设计）', async () => {
    apiGetMock.mockRejectedValue(new Error('stats unavailable'))
    renderWithProviders(<AdminDashboardPage />)
    // 仪表盘使用 Promise.allSettled，单点失败不会抛出，页面应正常渲染占位符
    await waitFor(() => expect(screen.getByTestId('admin-dashboard-page')).toBeInTheDocument())
  })
})

describe('AdminWorkspacesPage', () => {
  it('渲染工作区列表', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 'ws-1', name: 'Alpha', slug: 'alpha', member_count: 3 }] })
    renderWithProviders(<AdminWorkspacesPage />)
    await waitFor(() => expect(screen.getByTestId('workspaces-item')).toBeInTheDocument())
    expect(screen.getByText('Alpha')).toBeInTheDocument()
  })

  it('工作区加载失败时显示错误', async () => {
    apiGetMock.mockRejectedValue(new Error('network error'))
    renderWithProviders(<AdminWorkspacesPage />)
    await waitFor(() => expect(screen.getByText(/network error/i)).toBeInTheDocument(), { timeout: 5000 })
  })

  it('创建工作区时调用 POST', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    apiPostMock.mockResolvedValue({ id: 'ws-2', name: 'New WS' })
    renderWithProviders(<AdminWorkspacesPage />)
    await waitFor(() => expect(screen.getByTestId('workspaces-create')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('workspaces-create-name'), { target: { value: 'New WS' } })
    fireEvent.submit(screen.getByTestId('workspaces-create'))
    await waitFor(() => expect(apiPostMock).toHaveBeenCalledWith('/api/v1/workspaces', { name: 'New WS', slug: '' }))
  })
})

describe('AdminAssistantsPage', () => {
  it('渲染助手列表', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 'as-1', name: 'Helper', model: 'workama-chat' }] })
    renderWithProviders(<AdminAssistantsPage />)
    await waitFor(() => expect(screen.getByTestId('assistants-item')).toBeInTheDocument())
    expect(screen.getByText('Helper')).toBeInTheDocument()
  })

  it('助手加载失败时显示错误', async () => {
    apiGetMock.mockRejectedValue(new Error(' assistants down'))
    renderWithProviders(<AdminAssistantsPage />)
    await waitFor(() => expect(screen.getByText(/assistants down/i)).toBeInTheDocument(), { timeout: 5000 })
  })
})

describe('AdminWorkflowsPage', () => {
  it('渲染工作流列表', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 'wf-1', name: 'Flow A', trigger: 'manual', status: 'active' }] })
    renderWithProviders(<AdminWorkflowsPage />)
    await waitFor(() => expect(screen.getByTestId('workflows-item')).toBeInTheDocument())
    expect(screen.getByText('Flow A')).toBeInTheDocument()
  })

  it('工作流加载失败时显示错误', async () => {
    apiGetMock.mockRejectedValue(new Error('wf err'))
    renderWithProviders(<AdminWorkflowsPage />)
    await waitFor(() => expect(screen.getByText(/wf err/i)).toBeInTheDocument(), { timeout: 5000 })
  })
})

describe('AdminKnowledgeBasesPage', () => {
  it('渲染知识库列表', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 'kb-1', name: 'Docs', document_count: 5 }] })
    renderWithProviders(<AdminKnowledgeBasesPage />)
    await waitFor(() => expect(screen.getByTestId('kb-item')).toBeInTheDocument())
    expect(screen.getByText('Docs')).toBeInTheDocument()
  })

  it('知识库加载失败时显示错误', async () => {
    apiGetMock.mockRejectedValue(new Error('kb fail'))
    renderWithProviders(<AdminKnowledgeBasesPage />)
    await waitFor(() => expect(screen.getByText(/kb fail/i)).toBeInTheDocument(), { timeout: 5000 })
  })
})

describe('AdminDevicesPage', () => {
  it('渲染设备列表', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 'd-1', name: 'Laptop', device_type: 'desktop', status: 'online' }] })
    renderWithProviders(<AdminDevicesPage />)
    await waitFor(() => expect(screen.getByTestId('devices-item')).toBeInTheDocument())
    expect(screen.getByText('Laptop')).toBeInTheDocument()
  })

  it('设备加载失败时显示错误', async () => {
    apiGetMock.mockRejectedValue(new Error('device err'))
    renderWithProviders(<AdminDevicesPage />)
    await waitFor(() => expect(screen.getByText(/device err/i)).toBeInTheDocument(), { timeout: 5000 })
  })
})

describe('AdminBillingPage', () => {
  it('渲染计费信息', async () => {
    apiGetMock.mockResolvedValue({
      plans: [{ id: 'p1', name: 'Pro', price: 99 }],
      subscription: { plan_name: 'Pro', status: 'active' },
      usage: { requests: 1000, tokens: 50000, storage_mb: 500 },
    })
    renderWithProviders(<AdminBillingPage />)
    await waitFor(() => expect(screen.getByTestId('billing-kpis')).toBeInTheDocument())
    expect(screen.getAllByText('Pro').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByTestId(/^billing-plan-[a-z0-9]/)).toHaveLength(1)
  })

  it('计费加载失败时显示错误', async () => {
    apiGetMock.mockRejectedValue(new Error('billing down'))
    renderWithProviders(<AdminBillingPage />)
    await waitFor(() => expect(screen.getByText(/billing down/i)).toBeInTheDocument(), { timeout: 5000 })
  })
})

describe('AdminAuditLogsPage', () => {
  it('渲染审计日志列表', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 'a-1', action: 'create', actor: 'admin', resource: 'workspace' }] })
    renderWithProviders(<AdminAuditLogsPage />)
    await waitFor(() => expect(screen.getByTestId('audit-logs-item')).toBeInTheDocument())
    expect(screen.getByText('create')).toBeInTheDocument()
  })

  it('审计日志加载失败时显示错误', async () => {
    apiGetMock.mockRejectedValue(new Error('audit err'))
    renderWithProviders(<AdminAuditLogsPage />)
    await waitFor(() => expect(screen.getByText(/audit err/i)).toBeInTheDocument(), { timeout: 5000 })
  })

  it('审计日志支持过滤', async () => {
    apiGetMock.mockResolvedValue({ items: [
      { id: 'a-1', action: 'create', actor: 'admin', resource: 'workspace' },
      { id: 'a-2', action: 'delete', actor: 'user', resource: 'file' },
    ] })
    renderWithProviders(<AdminAuditLogsPage />)
    await waitFor(() => expect(screen.getAllByTestId('audit-logs-item')).toHaveLength(2))
    fireEvent.change(screen.getByTestId('audit-filter-input'), { target: { value: 'delete' } })
    await waitFor(() => expect(screen.getAllByTestId('audit-logs-item')).toHaveLength(1))
  })
})

describe('AdminMcpToolsPage', () => {
  it('渲染 MCP 工具列表', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 't-1', name: 'search', server: 'local', description: 'web search' }] })
    renderWithProviders(<AdminMcpToolsPage />)
    await waitFor(() => expect(screen.getByTestId('mcp-item')).toBeInTheDocument())
    expect(screen.getByText('search')).toBeInTheDocument()
  })

  it('MCP 工具加载失败时显示错误', async () => {
    apiGetMock.mockRejectedValue(new Error('mcp err'))
    renderWithProviders(<AdminMcpToolsPage />)
    await waitFor(() => expect(screen.getByText(/mcp err/i)).toBeInTheDocument(), { timeout: 5000 })
  })

  it('调用 MCP 工具时发送 POST', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 't-1', name: 'search', server: 'local' }] })
    apiPostMock.mockResolvedValue({ result: 'ok' })
    renderWithProviders(<AdminMcpToolsPage />)
    await waitFor(() => expect(screen.getByTestId('mcp-invoke-t-1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('mcp-invoke-t-1'))
    await waitFor(() => expect(apiPostMock).toHaveBeenCalledWith('/api/v1/mcp/tools/t-1/invoke'))
    await waitFor(() => expect(screen.getByTestId('mcp-invoke-result-t-1').textContent).toContain('ok'))
  })
})

describe('AdminNotificationsPage', () => {
  it('渲染通知列表', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 'n-1', title: 'Welcome', message: 'Hello', read: false, type: 'info' }] })
    renderWithProviders(<AdminNotificationsPage />)
    await waitFor(() => expect(screen.getByTestId('notifications-item')).toBeInTheDocument())
    expect(screen.getByText('Welcome')).toBeInTheDocument()
  })

  it('通知加载失败时显示错误', async () => {
    apiGetMock.mockRejectedValue(new Error('notif err'))
    renderWithProviders(<AdminNotificationsPage />)
    await waitFor(() => expect(screen.getByText(/notif err/i)).toBeInTheDocument(), { timeout: 5000 })
  })

  it('标记通知已读时调用 POST', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 'n-1', title: 'Test', read: false }] })
    apiPostMock.mockResolvedValue({})
    renderWithProviders(<AdminNotificationsPage />)
    await waitFor(() => expect(screen.getByTestId('notifications-read-n-1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('notifications-read-n-1'))
    await waitFor(() => expect(apiPostMock).toHaveBeenCalledWith('/api/v1/notifications/n-1/read'))
  })
})

describe('AdminFilesPage', () => {
  it('渲染文件列表', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 'f-1', name: 'doc.pdf', size: 1024, content_type: 'application/pdf' }] })
    renderWithProviders(<AdminFilesPage />)
    await waitFor(() => expect(screen.getByTestId('files-item')).toBeInTheDocument())
    expect(screen.getByText('doc.pdf')).toBeInTheDocument()
  })

  it('文件加载失败时显示错误', async () => {
    apiGetMock.mockRejectedValue(new Error('files err'))
    renderWithProviders(<AdminFilesPage />)
    await waitFor(() => expect(screen.getByText(/files err/i)).toBeInTheDocument(), { timeout: 5000 })
  })
})

describe('AdminMemoryVectorsPage', () => {
  it('渲染记忆向量列表', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 'mv-1', content: 'hello world memory', score: 0.95 }] })
    renderWithProviders(<AdminMemoryVectorsPage />)
    await waitFor(() => expect(screen.getByTestId('mv-item')).toBeInTheDocument())
    expect(screen.getByText(/hello world/i)).toBeInTheDocument()
  })

  it('记忆向量加载失败时显示错误', async () => {
    apiGetMock.mockRejectedValue(new Error('mv err'))
    renderWithProviders(<AdminMemoryVectorsPage />)
    await waitFor(() => expect(screen.getByText(/mv err/i)).toBeInTheDocument(), { timeout: 5000 })
  })

  it('创建记忆向量时调用 POST', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    apiPostMock.mockResolvedValue({ id: 'mv-2', content: 'new memory' })
    renderWithProviders(<AdminMemoryVectorsPage />)
    await waitFor(() => expect(screen.getByTestId('mv-create')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('mv-create-content'), { target: { value: 'new memory' } })
    fireEvent.submit(screen.getByTestId('mv-create'))
    await waitFor(() => expect(apiPostMock).toHaveBeenCalledWith('/api/v1/memory/vectors', { content: 'new memory' }))
  })
})
