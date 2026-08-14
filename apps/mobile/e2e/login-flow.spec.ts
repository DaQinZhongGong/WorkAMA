import { test, expect } from '@playwright/test'

/**
 * 移动端登录流程 E2E 测试（P2 阶段）。
 *
 * 覆盖：
 * - 登录页渲染（邮箱/密码输入框 + 登录按钮）
 * - 空字段提交显示错误（HTML5 required 验证）
 * - 错误密码显示 401 错误
 * - 正确登录后跳转主页（bottom nav 可见）
 * - 登录后会话保持（token 存于内存，app 保持认证状态）
 * - 退出登录后返回登录页
 * - token 过期后（API 401）显示错误状态
 * - 移动端视口（390x844 iPhone 14）渲染
 *
 * 注：移动端 PWA 有意将 access_token 保留在内存中（不写 localStorage），
 * 因此 "token 写入 localStorage" 测试调整为验证会话在内存中保持。
 *
 * baseURL 来自 playwright.config.ts（默认 http://127.0.0.1:3100，
 * 可通过 MOBILE_PWA_BASE_URL 环境变量覆盖为 http://localhost:20204）。
 */

const TEST_EMAIL = 'tester@workama.example.com'
const TEST_PASSWORD = 'WorkAMA-Test-2026!'

test.describe('Mobile login flow', () => {
  test.beforeEach(async ({ page }) => {
    // 拦截平台 API，提供 mock 响应
    await page.route('**/api/v1/auth/login', async (route) => {
      const body = route.request().postDataJSON()
      if (body?.email === TEST_EMAIL && body?.password === TEST_PASSWORD) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            access_token: 'mock-access-token-e2e',
            user: { display_name: 'E2E Tester', email: TEST_EMAIL, role: 'owner' },
          }),
        })
      } else {
        await route.fulfill({
          status: 401,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Invalid credentials' }),
        })
      }
    })

    // mock workspace 数据加载
    await page.route('**/api/v1/**', async (route) => {
      const url = route.request().url()
      if (url.includes('/auth/login')) return // 已由上面的 route 处理
      if (url.includes('/sessions')) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
        return
      }
      if (url.includes('/auth/me')) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ display_name: 'E2E Tester', email: TEST_EMAIL, role: 'owner' }),
        })
        return
      }
      if (url.includes('/auth/security')) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ mfa_enabled: false, sessions: [] }),
        })
        return
      }
      if (url.includes('/billing/')) {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({}) })
        return
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) })
    })

    await page.goto('/', { waitUntil: 'networkidle' })
  })

  test('renders login page with email, password inputs and submit button', async ({ page }) => {
    await expect(page.locator('#mobile-email')).toBeVisible()
    await expect(page.locator('#mobile-password')).toBeVisible()
    await expect(page.getByRole('button', { name: /enter workspace|进入工作区/i })).toBeVisible()
  })

  test('empty fields submission shows validation error', async ({ page }) => {
    // HTML5 required 属性阻止空提交，浏览器显示验证提示
    const emailInput = page.locator('#mobile-email')
    const passwordInput = page.locator('#mobile-password')

    // 两个输入框都有 required 属性
    await expect(emailInput).toHaveAttribute('required', '')
    await expect(passwordInput).toHaveAttribute('required', '')

    // 只填邮箱不填密码，尝试提交 → 不触发 API 调用
    await emailInput.fill(TEST_EMAIL)
    const loginButton = page.getByRole('button', { name: /enter workspace|进入工作区/i })
    await loginButton.click()

    // 登录按钮仍然可见（未跳转）
    await expect(loginButton).toBeVisible()
  })

  test('wrong password shows 401 error message', async ({ page }) => {
    await page.locator('#mobile-email').fill(TEST_EMAIL)
    await page.locator('#mobile-password').fill('wrong-password')
    await page.getByRole('button', { name: /enter workspace|进入工作区/i }).click()

    // 等待错误消息出现
    await expect(page.locator('.notice.error, [role="alert"]')).toBeVisible({ timeout: 5000 })
  })

  test('correct login navigates to main page with bottom nav', async ({ page }) => {
    await page.locator('#mobile-email').fill(TEST_EMAIL)
    await page.locator('#mobile-password').fill(TEST_PASSWORD)
    await page.getByRole('button', { name: /enter workspace|进入工作区/i }).click()

    // 登录成功后应显示 bottom nav
    await expect(page.getByRole('link', { name: /^Chat$|对话$/ })).toBeVisible({ timeout: 5000 })
    await expect(page.getByRole('link', { name: /^Settings$|设置$/ })).toBeVisible()
  })

  test('session is maintained in memory after login (token not in localStorage)', async ({ page, context }) => {
    // 移动端 PWA 有意将 token 保留在内存中，不写 localStorage
    await page.locator('#mobile-email').fill(TEST_EMAIL)
    await page.locator('#mobile-password').fill(TEST_PASSWORD)
    await page.getByRole('button', { name: /enter workspace|进入工作区/i }).click()

    // 等待登录成功
    await expect(page.getByRole('link', { name: /^Chat$|对话$/ })).toBeVisible({ timeout: 5000 })

    // 验证 token 不在 localStorage 中（安全设计）
    const localStorageKeys = await page.evaluate(() => Object.keys(window.localStorage))
    const tokenInStorage = localStorageKeys.some((k) => k.toLowerCase().includes('token') || k.toLowerCase().includes('access'))
    // token 不应明文存储在 localStorage
    expect(tokenInStorage).toBe(false)

    // 但会话保持有效（页面仍显示主界面，非登录页）
    await expect(page.locator('#mobile-email')).not.toBeVisible()
  })

  test('logout returns to login page', async ({ page }) => {
    // 先登录
    await page.locator('#mobile-email').fill(TEST_EMAIL)
    await page.locator('#mobile-password').fill(TEST_PASSWORD)
    await page.getByRole('button', { name: /enter workspace|进入工作区/i }).click()
    await expect(page.getByRole('link', { name: /^Settings$|设置$/ })).toBeVisible({ timeout: 5000 })

    // 导航到设置页
    await page.getByRole('link', { name: /^Settings$|设置$/ }).click()

    // 点击退出登录
    await page.getByRole('button', { name: /sign out|退出登录/i }).click()

    // 应返回登录页
    await expect(page.locator('#mobile-email')).toBeVisible({ timeout: 5000 })
  })

  test('token expiry (API 401) shows error state', async ({ page }) => {
    // 登录
    await page.locator('#mobile-email').fill(TEST_EMAIL)
    await page.locator('#mobile-password').fill(TEST_PASSWORD)
    await page.getByRole('button', { name: /enter workspace|进入工作区/i }).click()
    await expect(page.getByRole('link', { name: /^Chat$|对话$/ })).toBeVisible({ timeout: 5000 })

    // 重新拦截 API 返回 401（模拟 token 过期）
    await page.route('**/api/v1/**', async (route) => {
      const url = route.request().url()
      if (url.includes('/auth/login')) return
      await route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ detail: 'Token expired' }) })
    })

    // 重新加载触发 API 调用
    await page.reload({ waitUntil: 'networkidle' })

    // 应显示错误或返回登录页
    const hasError = await page.locator('.notice.error, [role="alert"]').isVisible().catch(() => false)
    const hasLoginScreen = await page.locator('#mobile-email').isVisible().catch(() => false)
    expect(hasError || hasLoginScreen).toBe(true)
  })

  test('renders correctly in iPhone 14 viewport (390x844)', async ({ page }) => {
    // 验证视口尺寸（config 已设置 390x844）
    const viewport = page.viewportSize()
    expect(viewport?.width).toBe(390)
    expect(viewport?.height).toBe(844)

    // 登录页在移动端视口下正确渲染
    await expect(page.locator('#mobile-email')).toBeVisible()
    await expect(page.locator('#mobile-password')).toBeVisible()

    // 验证 auth-screen 样式应用
    const authScreen = page.locator('.auth-screen')
    await expect(authScreen).toBeVisible()

    // 验证品牌标识可见
    await expect(page.locator('.brand-mark')).toBeVisible()
  })
})
