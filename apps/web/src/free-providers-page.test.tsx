import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest'
import '@testing-library/jest-dom/vitest'
import type { ReactElement } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { cleanup, render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { LocaleProvider } from './locale'
import { ApiError } from '@workama/api-client'
import FreeProvidersPage, { type FreeProviderPreset } from './free-providers-page'

// vitest 未启用 globals，testing-library 的自动 cleanup 不会注册，
// 此处显式在每个用例后卸载 DOM，避免跨用例累积导致查询命中多个元素。
afterEach(() => cleanup())

// 默认 mock：管理员登录 + 返回空供应商列表
const apiGetMock = vi.fn()
const apiPostMock = vi.fn()
const useAuthMock = vi.fn()

vi.mock('./api', () => ({
  api: {
    get: (...args: unknown[]) => apiGetMock(...args),
    post: (...args: unknown[]) => apiPostMock(...args),
  },
}))

vi.mock('./auth', () => ({
  useAuth: () => useAuthMock(),
}))

const sampleProviders: FreeProviderPreset[] = [
  {
    provider: 'openrouter-free',
    name: 'OpenRouter Free',
    base_url: 'https://openrouter.ai/api/v1',
    protocol: 'openai',
    signup_url: 'https://openrouter.ai/signup',
    free_quota: '10 req/min',
    free_models: ['meta-llama/llama-3-8b-instruct', 'google/gemma-7b', 'qwen/qwen-2-7b', 'mistral/mistral-7b'],
    capabilities: ['chat', 'tool_call', 'vision'],
    regions: ['global'],
    retention_mode: 'none',
    notes: 'Public free tier with rate limit.',
  },
  {
    provider: 'siliconflow-free',
    name: 'SiliconFlow Free',
    base_url: 'https://api.siliconflow.cn/v1',
    protocol: 'openai',
    signup_url: 'https://siliconflow.cn',
    free_quota: '¥20 credit / month',
    free_models: ['Qwen2-7B', 'GLM-4-9B'],
    capabilities: ['chat', 'embedding'],
    regions: ['cn'],
    retention_mode: 'none',
    notes: '中国区免费额度',
  },
]

function renderWithProviders(ui: ReactElement) {
  return render(
    <MemoryRouter>
      <LocaleProvider>{ui}</LocaleProvider>
    </MemoryRouter>,
  )
}

function setupAuth(opts: { authenticated?: boolean; isAdmin?: boolean } = {}) {
  useAuthMock.mockReturnValue({
    authenticated: opts.authenticated ?? true,
    isAdmin: opts.isAdmin ?? true,
    user: { role: 'admin', display_name: 'Admin', email: 'admin@example.com' },
  })
}

beforeEach(() => {
  apiGetMock.mockReset()
  apiPostMock.mockReset()
  useAuthMock.mockReset()
})

describe('FreeProvidersPage', () => {
  it('渲染 hero 标题与副标题', async () => {
    setupAuth()
    apiGetMock.mockResolvedValue({ providers: [] })
    renderWithProviders(<FreeProvidersPage />)
    expect(screen.getByRole('heading', { level: 1 })).toBeInTheDocument()
    // 等待加载完成
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith('/api/v1/gateway/free-providers'))
  })

  it('模拟 fetch 返回 2 个供应商，验证卡片渲染', async () => {
    setupAuth()
    apiGetMock.mockResolvedValue({ providers: sampleProviders })
    renderWithProviders(<FreeProvidersPage />)
    await waitFor(() => expect(screen.getAllByRole('article')).toHaveLength(2))
    expect(screen.getByText('OpenRouter Free')).toBeInTheDocument()
    expect(screen.getByText('SiliconFlow Free')).toBeInTheDocument()
    // 协议 badge：每张卡片各一个 openai badge（使用 within 限定作用域，避开筛选下拉的同名 option）
    const cards = screen.getAllByRole('article')
    cards.forEach((card) => {
      expect(within(card).getByText('openai')).toBeInTheDocument()
    })
    // 免费额度
    expect(screen.getByText('10 req/min')).toBeInTheDocument()
  })

  it('搜索框输入过滤逻辑：仅显示匹配的供应商', async () => {
    setupAuth()
    apiGetMock.mockResolvedValue({ providers: sampleProviders })
    renderWithProviders(<FreeProvidersPage />)
    await waitFor(() => expect(screen.getAllByRole('article')).toHaveLength(2))
    const input = screen.getByPlaceholderText(/Search|搜索/) as HTMLInputElement
    fireEvent.change(input, { target: { value: 'SiliconFlow' } })
    expect(screen.getByText('SiliconFlow Free')).toBeInTheDocument()
    expect(screen.queryByText('OpenRouter Free')).not.toBeInTheDocument()
  })

  it('管理员点击启用按钮调用 POST /enable', async () => {
    setupAuth({ authenticated: true, isAdmin: true })
    apiGetMock.mockResolvedValue({ providers: sampleProviders })
    apiPostMock.mockResolvedValue({ channel_id: 'ch-123' })
    renderWithProviders(<FreeProvidersPage />)
    await waitFor(() => expect(screen.getAllByRole('article')).toHaveLength(2))
    const enableButtons = screen.getAllByRole('button', { name: /Enable|启用/ })
    fireEvent.click(enableButtons[0])
    await waitFor(() => expect(apiPostMock).toHaveBeenCalledTimes(1))
    expect(apiPostMock).toHaveBeenCalledWith(
      '/api/v1/gateway/free-providers/openrouter-free/enable',
    )
  })

  it('401 时显示需登录提示', async () => {
    setupAuth({ authenticated: true, isAdmin: true })
    apiGetMock.mockResolvedValue({ providers: sampleProviders })
    apiPostMock.mockRejectedValue(
      new ApiError(401, 'HTTP_401', 'Not authenticated', undefined, { detail: 'Not authenticated' }),
    )
    renderWithProviders(<FreeProvidersPage />)
    await waitFor(() => expect(screen.getAllByRole('article')).toHaveLength(2))
    const enableButtons = screen.getAllByRole('button', { name: /Enable|启用/ })
    fireEvent.click(enableButtons[0])
    await waitFor(() => expect(apiPostMock).toHaveBeenCalledTimes(1))
    // 等待 toast 渲染错误消息（默认 en-US locale）
    await waitFor(() => {
      const status = screen.getByRole('status')
      expect(status).toHaveTextContent(/sign in|admin/i)
    })
    // 错误 wrapper 应包含 is-error class
    expect(document.querySelector('.free-providers-toast-wrap.is-error')).not.toBeNull()
  })

  it('只读模式下隐藏启用按钮', async () => {
    setupAuth({ authenticated: false, isAdmin: false })
    apiGetMock.mockResolvedValue({ providers: sampleProviders })
    renderWithProviders(<FreeProvidersPage readOnly />)
    await waitFor(() => expect(screen.getAllByRole('article')).toHaveLength(2))
    // 启用按钮不应出现
    expect(screen.queryByRole('button', { name: /Enable|启用/ })).not.toBeInTheDocument()
    // 但注册链接仍在
    expect(screen.getAllByRole('link', { name: /Sign up|前往注册/ }).length).toBeGreaterThan(0)
  })
})
