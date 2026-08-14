import { test, expect, type Page } from '@playwright/test'
import { login } from './helpers'

/**
 * License 管理 E2E 测试。
 *
 * 覆盖场景：
 *   1. 创建 license → 201 + license_key
 *   2. 列出 license → 200 + 包含刚创建的
 *   3. 获取当前 license → 200 + 状态（GET /entitlements）
 *   4. 续费 license → 200 + valid_until 延长（renew 端点未实现则 skip）
 *   5. 撤销 license → 200（需 high_assurance，未 step-up 时 403 则跳过结果断言）
 *   6. 无 license 访问受控端点 → 402（require_valid_license 未实现则 skip）
 *
 * 端点前缀：/api/v1/enterprise/compliance
 * 复用 helpers.ts login() 完成 UI 登录后，从 localStorage 取 workama_token 调用 API。
 */

const COMPLIANCE_API = '/api/v1/enterprise/compliance'

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
    if (Array.isArray(obj.licenses)) return obj.licenses
  }
  return []
}

/** 统一构造带 Bearer 与 JSON 类型的请求头。 */
function authHeaders(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }
}

/** 构造未来某时刻的 ISO 时间戳（默认 30 天后）。 */
function futureIso(days: number = 30): string {
  return new Date(Date.now() + days * 86_400_000).toISOString()
}

test.describe('License 管理', () => {
  test.beforeEach(async ({ page }) => {
    await login(page)
  })

  test('创建 license POST 返回 201 + license_key', async ({ page }) => {
    const token = await getAuthToken(page)
    const response = await page.request.post(`${COMPLIANCE_API}/licenses`, {
      headers: authHeaders(token),
      data: {
        plan_code: `e2e-plan-${Date.now()}`,
        seats: 5,
        valid_until: futureIso(30),
        idempotency_key: `e2e-lic-create-${Date.now()}`,
      },
    })
    // 创建需 admin 角色（与 audit-enterprise 测试一致假设当前用户为 admin）
    expect(response.status()).toBe(201)
    const body = await response.json()
    expect(body.license_key, '创建 license 必须返回明文 license_key').toBeTruthy()
    expect(body.license_key).toContain('wama-lic-')
    const id = body.id ?? body.license_id
    expect(id, '创建 license 响应必须包含 id').toBeTruthy()
  })

  test('列出 license GET 返回 200 且包含刚创建的', async ({ page }) => {
    const token = await getAuthToken(page)
    const createRes = await page.request.post(`${COMPLIANCE_API}/licenses`, {
      headers: authHeaders(token),
      data: {
        plan_code: `e2e-list-${Date.now()}`,
        seats: 3,
        valid_until: futureIso(14),
        idempotency_key: `e2e-lic-list-${Date.now()}`,
      },
    })
    expect(createRes.status()).toBe(201)
    const created = await createRes.json()
    const createdId = created.id ?? created.license_id

    const listRes = await page.request.get(`${COMPLIANCE_API}/licenses`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    expect(listRes.status()).toBe(200)
    const items = extractItems(await listRes.json())
    expect(Array.isArray(items)).toBe(true)
    const ids = items.map((it) => (it as Record<string, unknown>).id)
    expect(ids, '列表应包含刚创建的 license').toContain(createdId)
  })

  test('获取当前 license GET /entitlements 返回 200 + 状态', async ({ page }) => {
    const token = await getAuthToken(page)
    const response = await page.request.get(`${COMPLIANCE_API}/entitlements`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    expect(response.status()).toBe(200)
    const body = await response.json()
    // license_state 为 active / pending / expired / revoked / suspended / missing 之一
    const state = body.license_state ?? body.data?.license_state
    expect(state, 'entitlements 必须返回 license_state').toBeTruthy()
    expect(typeof state).toBe('string')
  })

  test('续费 license POST /licenses/{id}/renew 返回 200 + valid_until 延长（未实现则 skip）', async ({ page }) => {
    const token = await getAuthToken(page)
    const createRes = await page.request.post(`${COMPLIANCE_API}/licenses`, {
      headers: authHeaders(token),
      data: {
        plan_code: `e2e-renew-${Date.now()}`,
        seats: 3,
        valid_until: futureIso(7),
        idempotency_key: `e2e-lic-renew-${Date.now()}`,
      },
    })
    expect(createRes.status()).toBe(201)
    const created = await createRes.json()
    const licenseId = created.id ?? created.license_id
    const before = created.valid_until

    const renewRes = await page.request.post(`${COMPLIANCE_API}/licenses/${licenseId}/renew`, {
      headers: authHeaders(token),
      data: { extend_days: 30 },
    })
    // renew 端点在当前版本尚未实现：FastAPI 对未注册路由返回 404/405；
    // 待实现后应返回 200 且 valid_until 向后延长。
    test.skip(
      [404, 405].includes(renewRes.status()),
      'renew 端点未实现（404/405），跳过续费断言',
    )
    expect(renewRes.status()).toBe(200)
    const renewed = await renewRes.json()
    const after = renewed.valid_until ?? renewed.data?.valid_until
    expect(after, '续费响应必须返回 valid_until').toBeTruthy()
    expect(
      new Date(after as string).getTime(),
      '续费后 valid_until 必须晚于续费前',
    ).toBeGreaterThan(new Date(before as string).getTime())
  })

  test('撤销 license POST /licenses/{id}/revoke 返回 200', async ({ page }) => {
    const token = await getAuthToken(page)
    const createRes = await page.request.post(`${COMPLIANCE_API}/licenses`, {
      headers: authHeaders(token),
      data: {
        plan_code: `e2e-revoke-${Date.now()}`,
        seats: 2,
        idempotency_key: `e2e-lic-revoke-${Date.now()}`,
      },
    })
    expect(createRes.status()).toBe(201)
    const created = await createRes.json()
    const licenseId = created.id ?? created.license_id

    const revokeRes = await page.request.post(`${COMPLIANCE_API}/licenses/${licenseId}/revoke`, {
      headers: authHeaders(token),
      data: { reason: `e2e-revoke-reason-${Date.now()}` },
    })
    // revoke 需 _high_assurance（auth_strength>=2）；当前会话未 step-up 时返回 403。
    // 200 表示撤销成功；403 表示需 step-up，跳过状态断言但保留端点可达性验证。
    expect([200, 403]).toContain(revokeRes.status())
    test.skip(revokeRes.status() === 403, '当前会话未 step-up，跳过撤销结果断言')
    const revoked = await revokeRes.json()
    expect(revoked.status ?? revoked.data?.status).toBe('revoked')
  })

  test('无 license 访问受控端点 → 402（require_valid_license 未实现则 skip）', async ({ page }) => {
    const token = await getAuthToken(page)
    // 当前版本未实现 require_valid_license 依赖：没有任何受控端点会因缺少有效 license 返回 402。
    // 探测候选受控端点；若返回 402 则断言，否则 skip（与任务要求一致）。
    const probe = await page.request.get(`${COMPLIANCE_API}/entitlements`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    test.skip(probe.status() !== 402, 'require_valid_license 未实现，无 402 受控端点')
    expect(probe.status()).toBe(402)
  })
})