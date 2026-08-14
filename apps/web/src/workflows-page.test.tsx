import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest'
import '@testing-library/jest-dom/vitest'
import type { ReactElement } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { LocaleProvider } from './locale'

import AdminWorkflowsPage from './workflows-page'

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
  return render(
    <MemoryRouter>
      <LocaleProvider>{ui}</LocaleProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  apiGetMock.mockReset()
  apiPostMock.mockReset()
  apiDeleteMock.mockReset()
})

describe('AdminWorkflowsPage', () => {
  it('渲染页面标题与副标题，并立即请求工作流列表', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    renderWithProviders(<AdminWorkflowsPage />)
    expect(screen.getByText('工作流管理')).toBeInTheDocument()
    expect(screen.getByText('管理工作流定义与运行')).toBeInTheDocument()
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith('/api/v1/workflows'))
  })

  it('成功加载后渲染工作流列表项（名称/触发器/状态）', async () => {
    apiGetMock.mockResolvedValue({
      items: [
        { id: 'wf-1', name: 'Flow A', trigger: 'manual', status: 'active' },
        { id: 'wf-2', name: 'Flow B', trigger: 'schedule', status: 'paused' },
      ],
    })
    renderWithProviders(<AdminWorkflowsPage />)
    await waitFor(() => expect(screen.getAllByTestId('workflows-item')).toHaveLength(2))
    expect(screen.getByText('Flow A')).toBeInTheDocument()
    expect(screen.getByText('manual')).toBeInTheDocument()
    expect(screen.getByText('active')).toBeInTheDocument()
    expect(screen.getByText('Flow B')).toBeInTheDocument()
    expect(screen.getByText('paused')).toBeInTheDocument()
  })

  it('加载失败时显示错误信息', async () => {
    apiGetMock.mockRejectedValue(new Error('workflows down'))
    renderWithProviders(<AdminWorkflowsPage />)
    await waitFor(() => expect(screen.getByText(/workflows down/i)).toBeInTheDocument(), {
      timeout: 5000,
    })
  })

  it('工作流列表为空时仍渲染创建表单但不渲染列表项', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    renderWithProviders(<AdminWorkflowsPage />)
    await waitFor(() => expect(screen.getByTestId('workflows-create')).toBeInTheDocument())
    expect(screen.queryByTestId('workflows-item')).not.toBeInTheDocument()
  })

  it('提交创建表单时调用 POST /api/v1/workflows 携带 name 与 trigger', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    apiPostMock.mockResolvedValue({ id: 'wf-new', name: 'New Flow', trigger: 'manual' })
    renderWithProviders(<AdminWorkflowsPage />)
    await waitFor(() => expect(screen.getByTestId('workflows-create')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('workflows-create-name'), { target: { value: 'New Flow' } })
    fireEvent.change(screen.getByTestId('workflows-create-trigger'), { target: { value: 'manual' } })
    fireEvent.submit(screen.getByTestId('workflows-create'))
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/v1/workflows', {
        name: 'New Flow',
        trigger: 'manual',
      }),
    )
  })

  it('点击删除按钮时调用 DELETE /api/v1/workflows/{id}', async () => {
    apiGetMock.mockResolvedValue({
      items: [{ id: 'wf-1', name: 'Flow A', trigger: 'manual', status: 'active' }],
    })
    apiDeleteMock.mockResolvedValue({})
    renderWithProviders(<AdminWorkflowsPage />)
    await waitFor(() => expect(screen.getByTestId('workflows-delete-wf-1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('workflows-delete-wf-1'))
    await waitFor(() => expect(apiDeleteMock).toHaveBeenCalledWith('/api/v1/workflows/wf-1'))
  })

  it('缺少 trigger 时回退显示 manual', async () => {
    apiGetMock.mockResolvedValue({
      items: [{ id: 'wf-1', name: 'Flow A', status: 'active' }],
    })
    renderWithProviders(<AdminWorkflowsPage />)
    await waitFor(() => expect(screen.getByTestId('workflows-item')).toBeInTheDocument())
    expect(screen.getByText('manual')).toBeInTheDocument()
  })
})
