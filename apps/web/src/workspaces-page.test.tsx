import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest'
import '@testing-library/jest-dom/vitest'
import type { ReactElement } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { LocaleProvider } from './locale'

import AdminWorkspacesPage from './workspaces-page'

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

describe('AdminWorkspacesPage', () => {
  it('渲染页面标题与副标题，并立即请求工作区列表', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    renderWithProviders(<AdminWorkspacesPage />)
    expect(screen.getByText('工作区管理')).toBeInTheDocument()
    expect(screen.getByText('管理工作区列表与成员')).toBeInTheDocument()
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith('/api/v1/workspaces'))
  })

  it('成功加载后渲染工作区列表项（名称/slug/成员数）', async () => {
    apiGetMock.mockResolvedValue({
      items: [
        { id: 'ws-1', name: 'Alpha', slug: 'alpha', member_count: 3 },
        { id: 'ws-2', name: 'Beta', slug: 'beta', member_count: 0 },
      ],
    })
    renderWithProviders(<AdminWorkspacesPage />)
    await waitFor(() => expect(screen.getAllByTestId('workspaces-item')).toHaveLength(2))
    expect(screen.getByText('Alpha')).toBeInTheDocument()
    expect(screen.getByText('alpha')).toBeInTheDocument()
    expect(screen.getByText('成员 3')).toBeInTheDocument()
    expect(screen.getByText('Beta')).toBeInTheDocument()
  })

  it('加载失败时显示错误信息', async () => {
    apiGetMock.mockRejectedValue(new Error('workspaces unavailable'))
    renderWithProviders(<AdminWorkspacesPage />)
    await waitFor(() => expect(screen.getByText(/workspaces unavailable/i)).toBeInTheDocument(), {
      timeout: 5000,
    })
  })

  it('工作区列表为空时仍渲染创建表单但不渲染列表项', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    renderWithProviders(<AdminWorkspacesPage />)
    await waitFor(() => expect(screen.getByTestId('workspaces-create')).toBeInTheDocument())
    expect(screen.queryByTestId('workspaces-item')).not.toBeInTheDocument()
  })

  it('提交创建表单时调用 POST /api/v1/workspaces 携带 name 与 slug', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    apiPostMock.mockResolvedValue({ id: 'ws-new', name: 'New WS', slug: 'new-ws' })
    renderWithProviders(<AdminWorkspacesPage />)
    await waitFor(() => expect(screen.getByTestId('workspaces-create')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('workspaces-create-name'), { target: { value: 'New WS' } })
    fireEvent.change(screen.getByTestId('workspaces-create-slug'), { target: { value: 'new-ws' } })
    fireEvent.submit(screen.getByTestId('workspaces-create'))
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/v1/workspaces', {
        name: 'New WS',
        slug: 'new-ws',
      }),
    )
  })

  it('点击删除按钮时调用 DELETE /api/v1/workspaces/{id}', async () => {
    apiGetMock.mockResolvedValue({
      items: [{ id: 'ws-1', name: 'Alpha', slug: 'alpha', member_count: 3 }],
    })
    apiDeleteMock.mockResolvedValue({})
    renderWithProviders(<AdminWorkspacesPage />)
    await waitFor(() => expect(screen.getByTestId('workspaces-delete-ws-1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('workspaces-delete-ws-1'))
    await waitFor(() => expect(apiDeleteMock).toHaveBeenCalledWith('/api/v1/workspaces/ws-1'))
  })

  it('缺少 slug 时回退显示工作区 id', async () => {
    apiGetMock.mockResolvedValue({
      items: [{ id: 'ws-1', name: 'Alpha', member_count: 2 }],
    })
    renderWithProviders(<AdminWorkspacesPage />)
    await waitFor(() => expect(screen.getByTestId('workspaces-item')).toBeInTheDocument())
    expect(screen.getByText('ws-1')).toBeInTheDocument()
  })
})
