import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest'
import '@testing-library/jest-dom/vitest'
import type { ReactElement } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { LocaleProvider } from './locale'

import AdminNotificationsPage from './notifications-page'

afterEach(() => cleanup())

const apiGetMock = vi.fn()
const apiPostMock = vi.fn()

vi.mock('./api', () => ({
  api: {
    get: (...args: unknown[]) => apiGetMock(...args),
    post: (...args: unknown[]) => apiPostMock(...args),
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
})

describe('AdminNotificationsPage', () => {
  it('渲染页面标题并立即请求通知列表', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    renderWithProviders(<AdminNotificationsPage />)
    expect(screen.getByText('通知中心')).toBeInTheDocument()
    expect(screen.getByText('平台通知、告警与已读管理')).toBeInTheDocument()
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith('/api/v1/notifications'))
  })

  it('成功加载后渲染通知列表项（标题/消息/类型）', async () => {
    apiGetMock.mockResolvedValue({
      items: [
        { id: 'n-1', title: 'Welcome', message: 'Hello', read: false, type: 'info' },
        { id: 'n-2', title: 'Alert', message: 'Check now', read: true, type: 'warning' },
      ],
    })
    renderWithProviders(<AdminNotificationsPage />)
    await waitFor(() => expect(screen.getAllByTestId('notifications-item')).toHaveLength(2))
    expect(screen.getByText('Welcome')).toBeInTheDocument()
    expect(screen.getByText('Hello')).toBeInTheDocument()
    expect(screen.getByText('Alert')).toBeInTheDocument()
    expect(screen.getByText('warning')).toBeInTheDocument()
  })

  it('加载失败时显示错误信息', async () => {
    apiGetMock.mockRejectedValue(new Error('notif err'))
    renderWithProviders(<AdminNotificationsPage />)
    await waitFor(() => expect(screen.getByText(/notif err/i)).toBeInTheDocument(), {
      timeout: 5000,
    })
  })

  it('通知列表为空时显示空状态', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    renderWithProviders(<AdminNotificationsPage />)
    await waitFor(() => expect(screen.getByText('暂无通知')).toBeInTheDocument())
    expect(screen.queryByTestId('notifications-item')).not.toBeInTheDocument()
  })

  it('点击单条标记已读时调用 POST /api/v1/notifications/{id}/read', async () => {
    apiGetMock.mockResolvedValue({
      items: [{ id: 'n-1', title: 'Welcome', message: 'Hello', read: false, type: 'info' }],
    })
    apiPostMock.mockResolvedValue({})
    renderWithProviders(<AdminNotificationsPage />)
    await waitFor(() => expect(screen.getByTestId('notifications-read-n-1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('notifications-read-n-1'))
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/v1/notifications/n-1/read'),
    )
  })

  it('点击全部已读时调用 POST /api/v1/notifications/read-all', async () => {
    apiGetMock.mockResolvedValue({
      items: [
        { id: 'n-1', title: 'A', read: false },
        { id: 'n-2', title: 'B', read: false },
      ],
    })
    apiPostMock.mockResolvedValue({})
    renderWithProviders(<AdminNotificationsPage />)
    await waitFor(() => expect(screen.getByTestId('notifications-mark-all')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('notifications-mark-all'))
    await waitFor(() => expect(apiPostMock).toHaveBeenCalledWith('/api/v1/notifications/read-all'))
  })

  it('已读通知不渲染标记已读按钮', async () => {
    apiGetMock.mockResolvedValue({
      items: [{ id: 'n-1', title: 'Read', read: true, type: 'info' }],
    })
    renderWithProviders(<AdminNotificationsPage />)
    await waitFor(() => expect(screen.getByTestId('notifications-item')).toBeInTheDocument())
    expect(screen.queryByTestId('notifications-read-n-1')).not.toBeInTheDocument()
  })

  it('标记单条已读失败时显示错误', async () => {
    apiGetMock.mockResolvedValue({
      items: [{ id: 'n-1', title: 'Welcome', read: false, type: 'info' }],
    })
    apiPostMock.mockRejectedValue(new Error('mark read failed'))
    renderWithProviders(<AdminNotificationsPage />)
    await waitFor(() => expect(screen.getByTestId('notifications-read-n-1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('notifications-read-n-1'))
    await waitFor(() => expect(screen.getByText(/mark read failed/i)).toBeInTheDocument(), {
      timeout: 5000,
    })
  })
})
