import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest'
import '@testing-library/jest-dom/vitest'
import type { ReactElement } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { LocaleProvider } from './locale'

import AdminDevicesPage from './devices-page'

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

describe('AdminDevicesPage', () => {
  it('渲染页面标题与副标题，并立即请求设备列表', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    renderWithProviders(<AdminDevicesPage />)
    expect(screen.getByText('设备管理')).toBeInTheDocument()
    expect(screen.getByText('管理设备注册、心跳与凭据绑定')).toBeInTheDocument()
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith('/api/v1/devices'))
  })

  it('成功加载后渲染设备列表项（名称/类型/状态）', async () => {
    apiGetMock.mockResolvedValue({
      items: [
        { id: 'd-1', name: 'Laptop', device_type: 'desktop', last_heartbeat: '2024-01-01', status: 'online' },
        { id: 'd-2', name: 'Phone', device_type: 'mobile', last_heartbeat: '2024-01-02', status: 'offline' },
      ],
    })
    renderWithProviders(<AdminDevicesPage />)
    await waitFor(() => expect(screen.getAllByTestId('devices-item')).toHaveLength(2))
    expect(screen.getByText('Laptop')).toBeInTheDocument()
    expect(screen.getByText('desktop')).toBeInTheDocument()
    expect(screen.getByText('Phone')).toBeInTheDocument()
    expect(screen.getByText('mobile')).toBeInTheDocument()
  })

  it('加载失败时显示错误信息', async () => {
    apiGetMock.mockRejectedValue(new Error('device err'))
    renderWithProviders(<AdminDevicesPage />)
    await waitFor(() => expect(screen.getByText(/device err/i)).toBeInTheDocument(), {
      timeout: 5000,
    })
  })

  it('设备列表为空时仍渲染创建表单但不渲染列表项', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    renderWithProviders(<AdminDevicesPage />)
    await waitFor(() => expect(screen.getByTestId('devices-create')).toBeInTheDocument())
    expect(screen.queryByTestId('devices-item')).not.toBeInTheDocument()
  })

  it('提交创建表单时调用 POST /api/v1/devices 携带 name 与 device_type', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    apiPostMock.mockResolvedValue({ id: 'd-new', name: 'Tablet', device_type: 'tablet' })
    renderWithProviders(<AdminDevicesPage />)
    await waitFor(() => expect(screen.getByTestId('devices-create')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('devices-create-name'), { target: { value: 'Tablet' } })
    fireEvent.change(screen.getByTestId('devices-create-type'), { target: { value: 'tablet' } })
    fireEvent.submit(screen.getByTestId('devices-create'))
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/v1/devices', {
        name: 'Tablet',
        device_type: 'tablet',
      }),
    )
  })

  it('点击删除按钮时调用 DELETE /api/v1/devices/{id}', async () => {
    apiGetMock.mockResolvedValue({
      items: [{ id: 'd-1', name: 'Laptop', device_type: 'desktop', status: 'online' }],
    })
    apiDeleteMock.mockResolvedValue({})
    renderWithProviders(<AdminDevicesPage />)
    await waitFor(() => expect(screen.getByTestId('devices-delete-d-1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('devices-delete-d-1'))
    await waitFor(() => expect(apiDeleteMock).toHaveBeenCalledWith('/api/v1/devices/d-1'))
  })

  it('缺少 device_type 时回退显示 unknown', async () => {
    apiGetMock.mockResolvedValue({
      items: [{ id: 'd-1', name: 'Laptop', status: 'online' }],
    })
    renderWithProviders(<AdminDevicesPage />)
    await waitFor(() => expect(screen.getByTestId('devices-item')).toBeInTheDocument())
    expect(screen.getAllByText('unknown').length).toBeGreaterThanOrEqual(1)
  })
})