/**
 * 配置中心控制台组件测试（admin-platform-config-page）
 *
 * 覆盖：
 * - 分组 tab 与字段渲染（来源徽标 / 需重启徽标）
 * - 编辑产生未保存标记，保存时按字段类型编码 payload 并热生效提示
 * - 密钥字段掩码语义（默认保持；点击设置后输入新值随 PUT 明文提交）
 * - 分组内搜索过滤与空态
 * - 变更历史 tab 渲染发布版本并完成回滚确认流
 *
 * 语言显式钉住 zh-CN，断言中文文案（与生产默认一致）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import '@testing-library/jest-dom/vitest'
import type { ReactElement } from 'react'
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { LocaleProvider } from './locale'
import AdminPlatformConfigPage from './admin-platform-config-page'

afterEach(() => cleanup())

const apiGetMock = vi.fn()
const apiPutMock = vi.fn()
const apiPostMock = vi.fn()

vi.mock('./api', () => ({
  api: {
    get: (...args: unknown[]) => apiGetMock(...args),
    put: (...args: unknown[]) => apiPutMock(...args),
    post: (...args: unknown[]) => apiPostMock(...args),
  },
}))

function renderWithProviders(ui: ReactElement) {
  return render(
    <MemoryRouter>
      <LocaleProvider initialLocale="zh-CN">{ui}</LocaleProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  apiGetMock.mockReset()
  apiPutMock.mockReset()
  apiPostMock.mockReset()
})

const SCHEMA = {
  version: 3,
  groups: [
    {
      key: 'auth',
      label: '认证与授权',
      fields: [
        {
          key: 'rate_limit_default_per_min',
          label: '默认限流(次/分)',
          type: 'int',
          value: 60,
          secret: false,
          secret_set: false,
          source: 'default',
          restart_required: false,
          required: false,
          choices: [],
          min: 1,
          max: 10000,
          help: '通用 API 默认频率上限。',
        },
        {
          key: 'smtp_password',
          label: 'SMTP 密码',
          type: 'str',
          value: '********',
          secret: true,
          secret_set: false,
          source: 'default',
          restart_required: false,
          required: false,
          choices: [],
          min: null,
          max: null,
          help: '',
        },
      ],
    },
    {
      key: 'smtp',
      label: '邮件 (SMTP)',
      fields: [
        {
          key: 'smtp_mock',
          label: 'SMTP 模拟投递',
          type: 'bool',
          value: true,
          secret: false,
          secret_set: false,
          source: 'env',
          restart_required: false,
          required: false,
          choices: [],
          min: null,
          max: null,
          help: '',
        },
      ],
    },
  ],
}

describe('AdminPlatformConfigPage', () => {
  it('渲染分组 tab、字段与来源/需重启徽标', async () => {
    apiGetMock.mockResolvedValue(SCHEMA)
    renderWithProviders(<AdminPlatformConfigPage />)
    expect(await screen.findByTestId('cfg-tab-auth')).toBeInTheDocument()
    expect(screen.getByText('默认限流(次/分)')).toBeInTheDocument()
    // 两个默认来源字段 → 两个"默认值"徽标
    expect(screen.getAllByText('默认值')).toHaveLength(2)
    expect(screen.getByTestId('cfg-tab-history')).toBeInTheDocument()
    // 版本号来自 schema.version
    expect(screen.getByTestId('cfg-version')).toHaveTextContent('3')
  })

  it('编辑整数字段后保存，PUT 携带数值类型并显示热生效提示', async () => {
    apiGetMock.mockResolvedValue(SCHEMA)
    apiPutMock.mockResolvedValue({ version: 4, revision: 4, restart_required: [] })
    renderWithProviders(<AdminPlatformConfigPage />)
    await screen.findByTestId('cfg-rate_limit_default_per_min')
    fireEvent.change(screen.getByTestId('cfg-rate_limit_default_per_min'), {
      target: { value: '120' },
    })
    expect(screen.getAllByText('未保存').length).toBeGreaterThan(0)
    fireEvent.click(screen.getByTestId('cfg-save'))
    await waitFor(() =>
      expect(apiPutMock).toHaveBeenCalledWith('/api/v1/config/values', {
        items: [{ key: 'rate_limit_default_per_min', value: 120 }],
        note: 'console-publish',
      }),
    )
    await waitFor(() =>
      expect(screen.getByText(/配置已发布并生效（revision 4）/)).toBeInTheDocument(),
    )
    // 发布后重新拉取 schema
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledTimes(2))
  })

  it('密钥字段默认保持掩码；点击设置后输入新值才进入 PUT payload', async () => {
    apiGetMock.mockResolvedValue(SCHEMA)
    apiPutMock.mockResolvedValue({ version: 5, revision: 5, restart_required: [] })
    renderWithProviders(<AdminPlatformConfigPage />)
    const masked = await screen.findByTestId('cfg-smtp_password')
    expect(masked).toBeDisabled()
    expect(masked).toHaveValue('********')
    // 未编辑密钥时保存不应包含该键
    fireEvent.click(screen.getByTestId('cfg-save'))
    await waitFor(() => expect(apiPutMock).not.toHaveBeenCalled())
    // 进入编辑态输入新密钥
    fireEvent.click(screen.getByTestId('cfg-edit-secret-smtp_password'))
    const input = screen.getByTestId('cfg-smtp_password')
    expect(input).not.toBeDisabled()
    fireEvent.change(input, { target: { value: 'new-secret-123' } })
    fireEvent.click(screen.getByTestId('cfg-save'))
    await waitFor(() =>
      expect(apiPutMock).toHaveBeenCalledWith('/api/v1/config/values', {
        items: [{ key: 'smtp_password', value: 'new-secret-123' }],
        note: 'console-publish',
      }),
    )
  })

  it('搜索过滤无匹配时显示空态', async () => {
    apiGetMock.mockResolvedValue(SCHEMA)
    renderWithProviders(<AdminPlatformConfigPage />)
    await screen.findByTestId('cfg-search')
    fireEvent.change(screen.getByTestId('cfg-search'), { target: { value: '不存在的关键词xyz' } })
    expect(screen.getByText('该分组没有匹配的配置项。')).toBeInTheDocument()
  })

  it('变更历史渲染发布版本并完成回滚确认流', async () => {
    apiGetMock.mockImplementation((url: string) => {
      if (url.startsWith('/api/v1/config/revisions')) {
        return Promise.resolve({
          items: [{ id: 'rev_1', revision: 2, changed_by: 'u1', changed_at: '2026-08-20T10:00:00Z', note: 'console-publish' }],
        })
      }
      if (url.startsWith('/api/v1/config/history')) {
        return Promise.resolve({
          items: [{ id: 'cfg_1', revision: 2, key: 'smtp_mock', old_value: 'false', new_value: 'true', changed_by: 'u1', changed_at: '2026-08-20T10:00:00Z' }],
        })
      }
      return Promise.resolve(SCHEMA)
    })
    apiPostMock.mockResolvedValue({ version: 6, revision: 6, values: {} })
    renderWithProviders(<AdminPlatformConfigPage />)
    fireEvent.click(await screen.findByTestId('cfg-tab-history'))
    expect(await screen.findByTestId('cfg-rev-2')).toBeInTheDocument()
    expect(screen.getByTestId('cfg-hist-cfg_1')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('cfg-rollback-2'))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('cfg-rollback-confirm'))
    await waitFor(() =>
      expect(apiPostMock).toHaveBeenCalledWith('/api/v1/config/rollback', {
        revision: 2,
        note: 'console-rollback-to-2',
      }),
    )
    await waitFor(() => expect(screen.getByText(/已回滚到 2。/)).toBeInTheDocument())
  })
})
