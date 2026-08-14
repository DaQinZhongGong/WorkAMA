import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest'
import '@testing-library/jest-dom/vitest'
import type { ReactElement } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { LocaleProvider } from './locale'

import AdminAuditLogsPage from './audit-logs-page'

afterEach(() => cleanup())

const apiGetMock = vi.fn()

vi.mock('./api', () => ({
  api: {
    get: (...args: unknown[]) => apiGetMock(...args),
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
  return render(
    <MemoryRouter>
      <LocaleProvider>{ui}</LocaleProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  apiGetMock.mockReset()
})

const sampleLogs = [
  { id: 'a-1', action: 'create', actor: 'admin', resource: 'workspace', timestamp: '2024-01-01' },
  { id: 'a-2', action: 'delete', actor: 'user', resource: 'file', timestamp: '2024-01-02' },
  { id: 'a-3', action: 'update', actor: 'admin', resource: 'assistant', timestamp: '2024-01-03' },
]

describe('AdminAuditLogsPage', () => {
  it('渲染页面标题并立即请求审计日志列表', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    renderWithProviders(<AdminAuditLogsPage />)
    expect(screen.getByText('审计日志')).toBeInTheDocument()
    expect(screen.getByText('平台操作与安全事件的可追溯记录')).toBeInTheDocument()
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith('/api/v1/audit-logs'))
  })

  it('成功加载后渲染审计日志列表项（动作/操作者/资源）', async () => {
    apiGetMock.mockResolvedValue({ items: sampleLogs })
    renderWithProviders(<AdminAuditLogsPage />)
    await waitFor(() => expect(screen.getAllByTestId('audit-logs-item')).toHaveLength(3))
    expect(screen.getByText('create')).toBeInTheDocument()
    expect(screen.getByText('delete')).toBeInTheDocument()
    expect(screen.getByText('update')).toBeInTheDocument()
    expect(screen.getAllByText((text, node) => {
      const target = node?.textContent ?? text
      return target.toLowerCase().includes('admin')
    }).length).toBeGreaterThanOrEqual(2)
    expect(screen.getAllByText((text, node) => {
      const target = node?.textContent ?? text
      return target.includes('workspace')
    }).length).toBeGreaterThanOrEqual(1)
  })

  it('加载失败时显示错误信息', async () => {
    apiGetMock.mockRejectedValue(new Error('audit err'))
    renderWithProviders(<AdminAuditLogsPage />)
    await waitFor(() => expect(screen.getByText(/audit err/i)).toBeInTheDocument(), {
      timeout: 5000,
    })
  })

  it('审计日志列表为空时显示空状态', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    renderWithProviders(<AdminAuditLogsPage />)
    await waitFor(() =>
          expect(screen.getAllByText((text, node) => {
            const target = node?.textContent ?? text
            return target.includes('暂无审计日志')
          }).length).toBeGreaterThanOrEqual(1),
        )
    expect(screen.queryByTestId('audit-logs-item')).not.toBeInTheDocument()
  })

  it('过滤输入框按 action 过滤列表', async () => {
    apiGetMock.mockResolvedValue({ items: sampleLogs })
    renderWithProviders(<AdminAuditLogsPage />)
    await waitFor(() => expect(screen.getAllByTestId('audit-logs-item')).toHaveLength(3))
    fireEvent.change(screen.getByTestId('audit-filter-input'), { target: { value: 'delete' } })
    await waitFor(() => expect(screen.getAllByTestId('audit-logs-item')).toHaveLength(1))
    expect(screen.getByText('delete')).toBeInTheDocument()
    expect(screen.queryByText('create')).not.toBeInTheDocument()
    expect(screen.queryByText('update')).not.toBeInTheDocument()
  })

  it('过滤输入框按 actor 过滤列表（大小写不敏感）', async () => {
    apiGetMock.mockResolvedValue({ items: sampleLogs })
    renderWithProviders(<AdminAuditLogsPage />)
    await waitFor(() => expect(screen.getAllByTestId('audit-logs-item')).toHaveLength(3))
    fireEvent.change(screen.getByTestId('audit-filter-input'), { target: { value: 'ADMIN' } })
    await waitFor(() => expect(screen.getAllByTestId('audit-logs-item')).toHaveLength(2))
  })

  it('清空过滤输入框时恢复显示全部日志', async () => {
    apiGetMock.mockResolvedValue({ items: sampleLogs })
    renderWithProviders(<AdminAuditLogsPage />)
    await waitFor(() => expect(screen.getAllByTestId('audit-logs-item')).toHaveLength(3))
    const input = screen.getByTestId('audit-filter-input') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'delete' } })
    await waitFor(() => expect(screen.getAllByTestId('audit-logs-item')).toHaveLength(1))
    fireEvent.change(input, { target: { value: '' } })
    await waitFor(() => expect(screen.getAllByTestId('audit-logs-item')).toHaveLength(3))
  })

  it('点击刷新按钮触发重新加载', async () => {
    apiGetMock.mockResolvedValue({ items: sampleLogs })
    renderWithProviders(<AdminAuditLogsPage />)
    await waitFor(() => expect(screen.getAllByTestId('audit-logs-item')).toHaveLength(3))
    expect(apiGetMock).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByTestId('audit-refresh'))
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledTimes(2))
  })
})