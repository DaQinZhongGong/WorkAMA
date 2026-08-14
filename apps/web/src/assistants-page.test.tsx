import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest'
import '@testing-library/jest-dom/vitest'
import type { ReactElement } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { LocaleProvider } from './locale'

import AdminAssistantsPage from './assistants-page'

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

describe('AdminAssistantsPage', () => {
  it('渲染页面标题与副标题，并立即请求助手列表', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    renderWithProviders(<AdminAssistantsPage />)
    expect(screen.getByText('助手管理')).toBeInTheDocument()
    expect(screen.getByText('管理 AI 助手列表与对话')).toBeInTheDocument()
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith('/api/v1/assistants'))
  })

  it('成功加载后渲染助手列表项（名称/模型/描述）', async () => {
    apiGetMock.mockResolvedValue({
      items: [
        { id: 'as-1', name: 'Helper', model: 'workama-chat', description: '通用助手' },
        { id: 'as-2', name: 'Coder', model: 'gpt-4', description: '代码助手' },
      ],
    })
    renderWithProviders(<AdminAssistantsPage />)
    await waitFor(() => expect(screen.getAllByTestId('assistants-item')).toHaveLength(2))
    expect(screen.getByText('Helper')).toBeInTheDocument()
    expect(screen.getByText('workama-chat')).toBeInTheDocument()
    expect(screen.getByText('通用助手')).toBeInTheDocument()
    expect(screen.getByText('Coder')).toBeInTheDocument()
  })

  it('加载失败时显示错误信息', async () => {
    apiGetMock.mockRejectedValue(new Error('assistants unavailable'))
    renderWithProviders(<AdminAssistantsPage />)
    await waitFor(() => expect(screen.getByText(/assistants unavailable/i)).toBeInTheDocument(), {
      timeout: 5000,
    })
  })

  it('助手列表为空时仍渲染创建表单但不渲染列表项', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    renderWithProviders(<AdminAssistantsPage />)
    await waitFor(() => expect(screen.getByTestId('assistants-create')).toBeInTheDocument())
    expect(screen.queryByTestId('assistants-item')).not.toBeInTheDocument()
  })

  it('提交创建表单时调用 POST /api/v1/assistants 并携带表单值', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    apiPostMock.mockResolvedValue({ id: 'as-new', name: 'New Bot', model: 'workama-chat', description: 'desc' })
    renderWithProviders(<AdminAssistantsPage />)
    await waitFor(() => expect(screen.getByTestId('assistants-create')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('assistants-create-name'), { target: { value: 'New Bot' } })
    fireEvent.change(screen.getByTestId('assistants-create-model'), { target: { value: 'workama-chat' } })
    fireEvent.change(screen.getByTestId('assistants-create-description'), { target: { value: 'desc' } })
    fireEvent.submit(screen.getByTestId('assistants-create'))
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/v1/assistants', {
        name: 'New Bot',
        model: 'workama-chat',
        description: 'desc',
      }),
    )
  })

  it('点击删除按钮时调用 DELETE /api/v1/assistants/{id}', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 'as-1', name: 'Helper', model: 'workama-chat' }] })
    apiDeleteMock.mockResolvedValue({})
    renderWithProviders(<AdminAssistantsPage />)
    await waitFor(() => expect(screen.getByTestId('assistants-delete-as-1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('assistants-delete-as-1'))
    await waitFor(() => expect(apiDeleteMock).toHaveBeenCalledWith('/api/v1/assistants/as-1'))
  })

  it('助手项渲染指向 /chat 的对话链接', async () => {
    apiGetMock.mockResolvedValue({ items: [{ id: 'as-1', name: 'Helper', model: 'workama-chat' }] })
    renderWithProviders(<AdminAssistantsPage />)
    await waitFor(() => expect(screen.getByTestId('assistants-item')).toBeInTheDocument())
    const link = screen.getByRole('link', { name: /对话/ })
    expect(link).toHaveAttribute('href', '/chat?assistant=as-1')
  })
})
