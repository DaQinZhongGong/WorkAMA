import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest'
import '@testing-library/jest-dom/vitest'
import type { ReactElement } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { LocaleProvider } from './locale'

import AdminFilesPage from './files-page'

afterEach(() => cleanup())

const apiGetMock = vi.fn()
const apiPostMock = vi.fn()
const apiDeleteMock = vi.fn()
const windowOpenMock = vi.fn()

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
  windowOpenMock.mockReset()
  vi.stubGlobal('open', windowOpenMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('AdminFilesPage', () => {
  it('渲染页面标题与副标题，并立即请求文件列表', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    renderWithProviders(<AdminFilesPage />)
    expect(screen.getByText('文件管理')).toBeInTheDocument()
    expect(screen.getByText('上传、下载与文件库检索')).toBeInTheDocument()
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith('/api/v1/files'))
  })

  it('成功加载后渲染文件列表项（名称/类型/大小）', async () => {
    apiGetMock.mockResolvedValue({
      items: [
        { id: 'f-1', name: 'doc.pdf', size: 1024, content_type: 'application/pdf' },
        { id: 'f-2', name: 'image.png', size: 2048, content_type: 'image/png' },
      ],
    })
    renderWithProviders(<AdminFilesPage />)
    await waitFor(() => expect(screen.getAllByTestId('files-item')).toHaveLength(2))
    expect(screen.getByText('doc.pdf')).toBeInTheDocument()
    expect(screen.getByText('image.png')).toBeInTheDocument()
    expect(screen.getAllByText((text, node) => {
      const target = node?.textContent ?? text
      return target.includes('application/pdf')
    }).length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText((text, node) => {
      const target = node?.textContent ?? text
      return target.includes('image/png')
    }).length).toBeGreaterThanOrEqual(1)
  })

  it('加载失败时显示错误信息', async () => {
    apiGetMock.mockRejectedValue(new Error('files err'))
    renderWithProviders(<AdminFilesPage />)
    await waitFor(() => expect(screen.getByText(/files err/i)).toBeInTheDocument(), {
      timeout: 5000,
    })
  })

  it('文件列表为空时仍渲染上传表单但不渲染列表项', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    renderWithProviders(<AdminFilesPage />)
    await waitFor(() => expect(screen.getByTestId('files-upload-form')).toBeInTheDocument())
    expect(screen.queryByTestId('files-item')).not.toBeInTheDocument()
  })

  it('渲染上传表单（input file 与提交按钮）', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    renderWithProviders(<AdminFilesPage />)
    await waitFor(() => expect(screen.getByTestId('files-upload-form')).toBeInTheDocument())
    expect(screen.getByTestId('files-upload-input')).toHaveAttribute('type', 'file')
    expect(screen.getByTestId('files-upload-submit')).toBeInTheDocument()
  })

  it('点击下载按钮且文件有 url 时调用 window.open 打开 url', async () => {
    apiGetMock.mockResolvedValue({
      items: [
        { id: 'f-1', name: 'doc.pdf', size: 1024, content_type: 'application/pdf', url: 'https://example.com/doc.pdf' },
      ],
    })
    renderWithProviders(<AdminFilesPage />)
    await waitFor(() => expect(screen.getByTestId('files-download-f-1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('files-download-f-1'))
    await waitFor(() => expect(windowOpenMock).toHaveBeenCalledWith('https://example.com/doc.pdf', '_blank'))
  })

  it('点击下载按钮且文件无 url 时调用 window.open 打开下载端点', async () => {
    apiGetMock.mockResolvedValue({
      items: [{ id: 'f-1', name: 'doc.pdf', size: 1024, content_type: 'application/pdf' }],
    })
    renderWithProviders(<AdminFilesPage />)
    await waitFor(() => expect(screen.getByTestId('files-download-f-1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('files-download-f-1'))
    await waitFor(() => expect(windowOpenMock).toHaveBeenCalledWith('/api/v1/files/f-1/download', '_blank'))
  })

  it('上传文件时调用 POST /api/v1/files 携带 FormData', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    apiPostMock.mockResolvedValue({})
    renderWithProviders(<AdminFilesPage />)
    await waitFor(() => expect(screen.getByTestId('files-upload-form')).toBeInTheDocument())
    const fileInput = screen.getByTestId('files-upload-input') as HTMLInputElement
    const file = new File(['hello'], 'upload.txt', { type: 'text/plain' })
    fireEvent.change(fileInput, { target: { files: [file] } })
    fireEvent.submit(screen.getByTestId('files-upload-form'))
    await waitFor(() => expect(apiPostMock).toHaveBeenCalledTimes(1))
    const [endpoint, body] = apiPostMock.mock.calls[0]
    expect(endpoint).toBe('/api/v1/files')
    expect(body).toBeInstanceOf(FormData)
  })

  it('上传失败时显示上传错误信息', async () => {
    apiGetMock.mockResolvedValue({ items: [] })
    apiPostMock.mockRejectedValue(new Error('upload failed'))
    renderWithProviders(<AdminFilesPage />)
    await waitFor(() => expect(screen.getByTestId('files-upload-form')).toBeInTheDocument())
    const fileInput = screen.getByTestId('files-upload-input') as HTMLInputElement
    const file = new File(['hello'], 'upload.txt', { type: 'text/plain' })
    fireEvent.change(fileInput, { target: { files: [file] } })
    fireEvent.submit(screen.getByTestId('files-upload-form'))
    await waitFor(() => expect(screen.getByText(/upload failed/i)).toBeInTheDocument(), {
      timeout: 5000,
    })
  })

  it('点击删除按钮时调用 DELETE /api/v1/files/{id}', async () => {
    apiGetMock.mockResolvedValue({
      items: [{ id: 'f-1', name: 'doc.pdf', size: 1024, content_type: 'application/pdf' }],
    })
    apiDeleteMock.mockResolvedValue({})
    renderWithProviders(<AdminFilesPage />)
    await waitFor(() => expect(screen.getByTestId('files-delete-f-1')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('files-delete-f-1'))
    await waitFor(() => expect(apiDeleteMock).toHaveBeenCalledWith('/api/v1/files/f-1'))
  })
})