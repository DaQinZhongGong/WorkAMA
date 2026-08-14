import { test, expect, type Page } from '@playwright/test'
import { login } from './helpers'

/**
 * SSO/SCIM 配置管理 E2E 测试 (P3 v7.171 已落地)。
 *
 * 覆盖场景：
 *   1. 未登录访问 /identity-federation 重定向到 /login
 *   2. 登录后 SSO 配置列表 API 返回 200（空列表或现有配置）
 *   3. SSO 配置创建表单渲染（OIDC/SAML 切换、issuer/client_id/redirect_uris 字段）
 *   4. 创建 OIDC 配置 POST /providers 返回 201
 *   5. 触发 SSO 测试连接 POST /providers/{id}/test
 *   6. SCIM 同步历史 GET /providers/{id}/sync-history 返回 200
 */

const PROVIDERS_API = '/api/v1/identity-federation/providers'

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
    if (Array.isArray(obj.providers)) return obj.providers
  }
  return []
}

test.describe('SSO/SCIM 配置管理', () => {
  test.beforeEach(async ({ page }) => {
    await login(page)
  })

  test('未登录访问 /identity-federation 重定向到 /login', async ({ page }) => {
    // 清除 token 模拟未登录态：addInitScript 在下次导航的文档创建阶段执行，
    // 早于 SPA 启动与路由守卫，确保跳转触发。
    await page.addInitScript(() => {
      window.localStorage.removeItem('workama_token')
    })
    await page.goto('/identity-federation', { waitUntil: 'networkidle' })
    await page.waitForURL(/\/login/, { timeout: 15_000 })
    await expect(page).toHaveURL(/\/login/)
    // 验证 redirect 参数携带原始路径
    const redirect = new URL(page.url()).searchParams.get('redirect')
    expect(redirect).toBeTruthy()
    expect(redirect).toContain('/identity-federation')
    await expect(page.locator('#email')).toBeVisible()
  })

  test('登录后 SSO 配置列表 API 返回 200（空列表或现有配置）', async ({ page }) => {
    const token = await getAuthToken(page)
    const response = await page.request.get(PROVIDERS_API, {
      headers: { Authorization: `Bearer ${token}` },
    })
    expect(response.status()).toBe(200)
    const body = await response.json()
    const items = extractItems(body)
    expect(Array.isArray(items)).toBe(true)
  })

  test('SSO 配置创建表单渲染（OIDC/SAML 切换、issuer/client_id/redirect_uris 字段）', async ({ page }) => {
    await page.goto('/identity-federation', { waitUntil: 'networkidle' })
    // 进入"新建"表单（按钮文本可能为 "New" / "新建" / "Create" / "添加"）
    const createButton = page
      .getByRole('button', { name: /new|新建|create|添加/i })
      .first()
    if (await createButton.isVisible().catch(() => false)) {
      await createButton.click()
    }
    // 验证 OIDC/SAML 协议切换存在
    const oidcOption = page
      .locator('[data-testid="provider-type-oidc"], label:has-text("OIDC"), button:has-text("OIDC")')
      .first()
    const samlOption = page
      .locator('[data-testid="provider-type-saml"], label:has-text("SAML"), button:has-text("SAML")')
      .first()
    await expect(oidcOption.or(samlOption).first()).toBeVisible({ timeout: 15_000 })
    // 验证核心字段存在（name 或 id 二者之一）
    await expect(page.locator('[name="issuer"], #issuer').first()).toBeVisible()
    await expect(page.locator('[name="client_id"], #client_id').first()).toBeVisible()
    await expect(page.locator('[name="redirect_uris"], #redirect_uris').first()).toBeVisible()
  })

  test('创建 OIDC 配置 POST /providers 返回 201', async ({ page }) => {
    const token = await getAuthToken(page)
    const payload = {
      name: `e2e-oidc-${Date.now()}`,
      type: 'oidc',
      issuer: 'https://accounts.example.com',
      client_id: 'e2e-client-id',
      client_secret: 'e2e-client-secret',
      redirect_uris: ['https://workama.example.com/auth/callback'],
    }
    const response = await page.request.post(PROVIDERS_API, {
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      data: payload,
    })
    expect(response.status()).toBe(201)
    const body = await response.json()
    const id = body.id ?? body.provider_id ?? body.data?.id
    expect(id, '创建 OIDC 配置响应必须包含 id').toBeTruthy()
  })

  test('触发 SSO 测试连接 POST /providers/{id}/test', async ({ page }) => {
    const token = await getAuthToken(page)
    // 先列出已有 provider，取第一个 id 用于测试连接
    const listRes = await page.request.get(PROVIDERS_API, {
      headers: { Authorization: `Bearer ${token}` },
    })
    expect(listRes.status()).toBe(200)
    const items = extractItems(await listRes.json())
    test.skip(items.length === 0, '当前无 SSO 配置可测试连接')
    const target = items[0] as Record<string, unknown>
    const targetId = (target.id ?? target.provider_id) as string
    expect(targetId).toBeTruthy()

    const testRes = await page.request.post(`${PROVIDERS_API}/${targetId}/test`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    // 测试连接可能成功(200) 或因配置不完整/外部 IdP 不可达失败(4xx/5xx)
    expect([200, 400, 422, 502, 504]).toContain(testRes.status())
  })

  test('SCIM 同步历史 GET /providers/{id}/sync-history 返回 200', async ({ page }) => {
    const token = await getAuthToken(page)
    // 取已有 provider id
    const listRes = await page.request.get(PROVIDERS_API, {
      headers: { Authorization: `Bearer ${token}` },
    })
    expect(listRes.status()).toBe(200)
    const items = extractItems(await listRes.json())
    test.skip(items.length === 0, '当前无 SSO 配置可查询 SCIM 同步历史')
    const target = items[0] as Record<string, unknown>
    const targetId = (target.id ?? target.provider_id) as string
    expect(targetId).toBeTruthy()

    const historyRes = await page.request.get(`${PROVIDERS_API}/${targetId}/sync-history`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    expect(historyRes.status()).toBe(200)
    const historyBody = await historyRes.json()
    const syncItems = extractItems(historyBody)
    expect(Array.isArray(syncItems)).toBe(true)
  })
})
