import { test, expect, type Page } from '@playwright/test'
import { login } from './helpers'

/**
 * 企业审计增强功能 E2E 测试 (P3 v7.171 已落地)。
 *
 * 覆盖场景：
 *   1. legal-holds 列表 GET /audit-logs/legal-holds 返回 200
 *   2. 创建 legal hold POST /audit-logs/legal-holds（hold_reason + event_filter）
 *   3. 释放 legal hold DELETE /audit-logs/legal-holds/{id}（带 release_reason）
 *   4. SIEM 配置获取 GET /audit-logs/siem/config（未配置时 404）
 *   5. SIEM 配置保存 POST /audit-logs/siem/config（endpoint/protocol/format）
 *   6. SIEM 测试连接 POST /audit-logs/siem/test
 *   7. 批量导出 POST /audit-logs/export/batch（json/csv/syslog/cef 格式）
 */

const AUDIT_API = '/api/v1/audit-logs'

/**
 * 从 localStorage 取 workama_token，用于 API 请求的 Bearer 头。
 * login() 完成后 SPA 会将 access token 持久化到 workama_token。
 */
async function getAuthToken(page: Page): Promise<string> {
  const token = await page.evaluate(() => window.localStorage.getItem('workama_token'))
  expect(token, '登录后 localStorage 必须存在 workama_token').toBeTruthy()
  return token as string
}

/** 兼容列表响应的多种结构：数组 / { items: [] } / { data: [] }。 */
function extractItems(body: unknown): unknown[] {
  if (Array.isArray(body)) return body
  if (body && typeof body === 'object') {
    const obj = body as Record<string, unknown>
    if (Array.isArray(obj.items)) return obj.items
    if (Array.isArray(obj.data)) return obj.data
    if (Array.isArray(obj.holds)) return obj.holds
  }
  return []
}

test.describe('企业审计增强功能', () => {
  test.beforeEach(async ({ page }) => {
    await login(page)
  })

  test('legal-holds 列表 GET 返回 200', async ({ page }) => {
    const token = await getAuthToken(page)
    const response = await page.request.get(`${AUDIT_API}/legal-holds`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    expect(response.status()).toBe(200)
    const body = await response.json()
    const items = extractItems(body)
    expect(Array.isArray(items)).toBe(true)
  })

  test('创建 legal hold POST 返回 201（hold_reason + event_filter）', async ({ page }) => {
    const token = await getAuthToken(page)
    const payload = {
      hold_reason: `e2e-hold-${Date.now()}`,
      event_filter: {
        user_ids: ['user-e2e@example.com'],
        event_types: ['message.sent', 'file.uploaded'],
        start_time: new Date(Date.now() - 86_400_000).toISOString(),
      },
    }
    const response = await page.request.post(`${AUDIT_API}/legal-holds`, {
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      data: payload,
    })
    expect(response.status()).toBe(201)
    const body = await response.json()
    const id = body.id ?? body.hold_id ?? body.data?.id
    expect(id, '创建 legal hold 响应必须包含 id').toBeTruthy()
  })

  test('释放 legal hold DELETE 返回 200 或 204（带 release_reason）', async ({ page }) => {
    const token = await getAuthToken(page)
    // 先创建一个 hold 用于释放，避免依赖其他测试的副作用
    const createRes = await page.request.post(`${AUDIT_API}/legal-holds`, {
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      data: {
        hold_reason: `e2e-release-${Date.now()}`,
        event_filter: { event_types: ['audit.exported'] },
      },
    })
    expect(createRes.status()).toBe(201)
    const created = await createRes.json()
    const holdId = created.id ?? created.hold_id ?? created.data?.id
    expect(holdId, '创建后必须返回 hold id 供 DELETE 使用').toBeTruthy()

    // 释放 hold（带 release_reason）
    const releaseRes = await page.request.delete(`${AUDIT_API}/legal-holds/${holdId}`, {
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      data: { release_reason: `e2e-release-reason-${Date.now()}` },
    })
    expect([200, 204]).toContain(releaseRes.status())
  })

  test('SIEM 配置获取 GET /siem/config（未配置时 404）', async ({ page }) => {
    const token = await getAuthToken(page)
    const response = await page.request.get(`${AUDIT_API}/siem/config`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    // 已配置：200；未配置：404
    expect([200, 404]).toContain(response.status())
    if (response.status() === 200) {
      const body = await response.json()
      expect(body).toBeTruthy()
    }
  })

  test('SIEM 配置保存 POST /siem/config（endpoint/protocol/format）', async ({ page }) => {
    const token = await getAuthToken(page)
    const payload = {
      endpoint: 'https://siem.example.com:514',
      protocol: 'tcp',
      format: 'cef',
      facility: 'local0',
    }
    const response = await page.request.post(`${AUDIT_API}/siem/config`, {
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      data: payload,
    })
    expect([200, 201]).toContain(response.status())
    const body = await response.json()
    const endpoint = body.endpoint ?? body.data?.endpoint
    expect(endpoint, '保存响应应回显 endpoint').toContain('siem.example.com')
  })

  test('SIEM 测试连接 POST /siem/test', async ({ page }) => {
    const token = await getAuthToken(page)
    const payload = {
      endpoint: 'https://siem.example.com:514',
      protocol: 'tcp',
      format: 'cef',
    }
    const response = await page.request.post(`${AUDIT_API}/siem/test`, {
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      data: payload,
    })
    // 测试连接可能成功(200) 或因网络不可达/配置不完整失败(4xx/5xx)
    expect([200, 400, 422, 502, 504]).toContain(response.status())
  })

  test('批量导出 POST /export/batch 支持 json/csv/syslog/cef 格式', async ({ page }) => {
    const token = await getAuthToken(page)
    for (const format of ['json', 'csv', 'syslog', 'cef']) {
      const response = await page.request.post(`${AUDIT_API}/export/batch`, {
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
        },
        data: {
          format,
          filters: {
            start_time: new Date(Date.now() - 86_400_000).toISOString(),
            end_time: new Date().toISOString(),
          },
        },
      })
      // 接受 200（同步返回结果）或 202（异步导出任务已接受）
      expect([200, 202]).toContain(response.status())
    }
  })
})
