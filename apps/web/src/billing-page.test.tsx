import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest'
import '@testing-library/jest-dom/vitest'
import type { ReactElement } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { LocaleProvider } from './locale'

import AdminBillingPage from './billing-page'

afterEach(() => cleanup())

const apiGetMock = vi.fn()

vi.mock('./api', () => ({
  api: {
    get: (...args: unknown[]) => apiGetMock(...args),
    post: (...args: unknown[]) => apiPostMock(...args),
  },
  errorMessage: (error: unknown, fallback: string) =>
    error instanceof Error && error.message ? error.message : fallback,
}))

const apiPostMock = vi.fn()

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

describe('AdminBillingPage', () => {
  it('渲染页面标题并立即请求计费总览', async () => {
    apiGetMock.mockResolvedValue({ plans: [], subscription: {}, usage: {} })
    renderWithProviders(<AdminBillingPage />)
    expect(screen.getByText('订阅计费')).toBeInTheDocument()
    expect(screen.getByText('当前订阅、用量趋势与可选套餐对比')).toBeInTheDocument()
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith('/api/v1/billing/overview'))
  })

  it('渲染当前订阅、用量与套餐列表', async () => {
    apiGetMock.mockResolvedValue({
      plans: [
        { id: 'p1', name: 'Pro', price: 99, features: ['chat'] },
        { id: 'p2', name: 'Enterprise', price: 999 },
      ],
      subscription: { plan_name: 'Pro', status: 'active', seats: 5 },
      usage: { requests: 1000, tokens: 50000, storage_mb: 500 },
    })
    renderWithProviders(<AdminBillingPage />)
    await waitFor(() => expect(screen.getByTestId('billing-kpis')).toBeInTheDocument())
    expect(screen.getByTestId('plans-grid')).toBeInTheDocument()
    expect(screen.getByTestId('billing-subscription')).toBeInTheDocument()
    expect(screen.getAllByTestId(/^billing-plan-[a-z]/)).toHaveLength(2)
    expect(screen.getAllByText('Pro').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('Enterprise')).toBeInTheDocument()
    expect(screen.getByTestId('billing-usage-progress')).toBeInTheDocument()
  })

  it('加载失败时显示错误信息', async () => {
    apiGetMock.mockRejectedValue(new Error('billing down'))
    renderWithProviders(<AdminBillingPage />)
    await waitFor(() => expect(screen.getByText(/billing down/i)).toBeInTheDocument(), {
      timeout: 5000,
    })
  })

  it('点击刷新按钮触发重新加载', async () => {
    apiGetMock.mockResolvedValue({
      plans: [],
      subscription: { plan_name: 'Free', status: 'active' },
      usage: { requests: 0, tokens: 0, storage_mb: 0 },
    })
    renderWithProviders(<AdminBillingPage />)
    await waitFor(() => expect(screen.getByTestId('billing-kpis')).toBeInTheDocument())
    expect(apiGetMock).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByTestId('billing-refresh'))
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledTimes(2))
  })

  it('套餐价格为 0 或缺失时显示免费', async () => {
    apiGetMock.mockResolvedValue({
      plans: [
        { id: 'p1', name: 'Free', price: 0 },
        { id: 'p2', name: 'Trial' },
      ],
      subscription: {},
      usage: {},
    })
    renderWithProviders(<AdminBillingPage />)
    await waitFor(() => expect(screen.getAllByTestId(/^billing-plan-[a-z0-9]/)).toHaveLength(2))
    expect(screen.getAllByText('免费').length).toBeGreaterThanOrEqual(2)
  })

  it('缺少订阅与用量数据时显示占位符', async () => {
    apiGetMock.mockResolvedValue({ plans: [], subscription: undefined, usage: undefined })
    renderWithProviders(<AdminBillingPage />)
    await waitFor(() => expect(screen.getByTestId('billing-kpis')).toBeInTheDocument())
    expect(screen.getByText('免费试用')).toBeInTheDocument()
  })
})