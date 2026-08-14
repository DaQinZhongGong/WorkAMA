/**
 * 性能基准测试
 *
 * 目的：建立 WorkAMA Web 关键路径渲染性能的初始基线，
 * 用于在后续重构（尤其是 admin/* 页面接入 i18n）过程中监控回归。
 *
 * 测试对象：
 *   1. AdminDashboardPage —— 单页统计仪表盘
 *   2. AdminAssistantsPage —— 资源 CRUD 页面
 *   3. AdminWorkspacesPage —— 列表 + 创建表单组合
 *   4. AdminCreateForm —— 受控表单（form 渲染）
 *   5. 100 条数据列表渲染 —— 高密度列表渲染
 *
 * 注：
 *  - 所有渲染均使用 jsdom 环境，不接入浏览器布局/绘制；
 *    数值仅作横向对比与回归监控，不作为绝对用户体验指标。
 *  - 文件扩展名采用 `.bench.tsx` 以支持 JSX 语法（与代码库 .test.tsx 一致）。
 */
import { bench, describe, vi } from 'vitest'
import '@testing-library/jest-dom/vitest'
import type { ReactElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { act } from 'react-dom/test-utils'
import { MemoryRouter } from 'react-router-dom'
import { LocaleProvider } from './locale'
import AdminDashboardPage from './admin-dashboard-page'
import AdminAssistantsPage from './assistants-page'
import AdminWorkspacesPage from './workspaces-page'
import { AdminCreateForm } from './admin-shared'

// ---- mock ./api ----------------------------------------------------------
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
  asItems: <T,>(payload: unknown): T[] => {
    if (Array.isArray(payload)) return payload as T[]
    if (payload && typeof payload === 'object' && 'items' in payload) {
      return (payload as { items: T[] }).items
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

// ---- 渲染基础设施 --------------------------------------------------------
let container: HTMLDivElement | null = null
let root: Root | null = null

function setupDOM() {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
}

function teardownDOM() {
  if (root) {
    act(() => {
      root!.unmount()
    })
    root = null
  }
  if (container) {
    container.remove()
    container = null
  }
  document.body.innerHTML = ''
}

function renderSync(ui: ReactElement) {
  if (!root || !container) setupDOM()
  act(() => {
    root!.render(
      <MemoryRouter>
        <LocaleProvider>{ui}</LocaleProvider>
      </MemoryRouter>,
    )
  })
}

// ---- 准备测试数据 --------------------------------------------------------
function makeWorkspaces(count: number) {
  return Array.from({ length: count }, (_, index) => ({
    id: `ws-${index}`,
    name: `Workspace ${index}`,
    slug: `ws-${index}`,
    member_count: index % 50,
  }))
}

function makeAssistants(count: number) {
  return Array.from({ length: count }, (_, index) => ({
    id: `asst-${index}`,
    name: `Assistant ${index}`,
    model: index % 2 === 0 ? 'workama-chat' : 'workama-reason',
    description: `Description for assistant ${index}`,
  }))
}

// ============================================================================
// 1. AdminDashboardPage —— 单页统计仪表盘
// ============================================================================
describe('AdminDashboardPage 渲染性能', () => {
  bench(
    'render dashboard with stats',
    () => {
      apiGetMock.mockResolvedValue({
        workspaces: 5,
        assistants: 3,
        knowledge_bases: 2,
        devices: 10,
        unread_notifications: 7,
        current_plan: 'pro',
      })
      renderSync(<AdminDashboardPage />)
    },
    {
      iterations: 10,
      warmupIterations: 3,
    },
  )

  bench(
    'render dashboard with loading state',
    () => {
      apiGetMock.mockReturnValue(new Promise(() => undefined)) // never resolves
      renderSync(<AdminDashboardPage />)
    },
    {
      iterations: 10,
      warmupIterations: 3,
    },
  )
})

// ============================================================================
// 2. AdminAssistantsPage —— 资源 CRUD 页面
// ============================================================================
describe('AdminAssistantsPage 渲染性能', () => {
  bench(
    'render with 10 assistants',
    () => {
      apiGetMock.mockResolvedValue({ items: makeAssistants(10) })
      renderSync(<AdminAssistantsPage />)
    },
    {
      iterations: 10,
      warmupIterations: 3,
    },
  )
})

// ============================================================================
// 3. AdminWorkspacesPage —— 列表 + 创建表单组合
// ============================================================================
describe('AdminWorkspacesPage 渲染性能', () => {
  bench(
    'render with 25 workspaces',
    () => {
      apiGetMock.mockResolvedValue({ items: makeWorkspaces(25) })
      renderSync(<AdminWorkspacesPage />)
    },
    {
      iterations: 10,
      warmupIterations: 3,
    },
  )
})

// ============================================================================
// 4. AdminCreateForm —— 受控表单（form 渲染）
// ============================================================================
describe('AdminCreateForm 表单渲染性能', () => {
  bench(
    'render create form with 3 fields',
    () => {
      renderSync(
        <AdminCreateForm
          testId="bench-create"
          fields={[
            { name: 'name', label: 'Name', placeholder: 'Workspace name' },
            { name: 'slug', label: 'Slug', placeholder: 'workspace-slug' },
            { name: 'description', label: 'Description', placeholder: 'Workspace description' },
          ]}
          onSubmit={async () => undefined}
          busy={false}
        />,
      )
    },
    {
      iterations: 50,
      warmupIterations: 10,
    },
  )
})

// ============================================================================
// 5. 100 条数据列表渲染 —— 高密度列表渲染
// ============================================================================
describe('100 条数据列表渲染性能', () => {
  bench(
    'render workspaces list with 100 items',
    () => {
      apiGetMock.mockResolvedValue({ items: makeWorkspaces(100) })
      renderSync(<AdminWorkspacesPage />)
    },
    {
      iterations: 10,
      warmupIterations: 3,
    },
  )

  bench(
    'render assistants list with 100 items',
    () => {
      apiGetMock.mockResolvedValue({ items: makeAssistants(100) })
      renderSync(<AdminAssistantsPage />)
    },
    {
      iterations: 10,
      warmupIterations: 3,
    },
  )
})

// ---- 清理 ----------------------------------------------------------------
describe('teardown', () => {
  bench(
    'cleanup DOM between suites',
    () => {
      teardownDOM()
    },
    {
      iterations: 1,
      warmupIterations: 0,
    },
  )
})
