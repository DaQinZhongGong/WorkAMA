import { test, expect } from '@playwright/test'
import { EMAIL, PASSWORD, login } from './helpers'

/**
 * 登录业务旅程 E2E 测试。
 *
 * 覆盖场景：
 *   1. 登录页核心元素可见（邮箱、密码、提交按钮）
 *   2. 填写凭据并提交后跳转到 /chat 控制台
 *   3. 未登录访问受保护页面时跳转到 /login 并携带 redirect 参数
 */
test.describe('登录旅程', () => {
  test('登录页渲染核心元素', async ({ page }) => {
    await page.goto('/login', { waitUntil: 'networkidle' })
    await expect(page.locator('#email')).toBeVisible()
    await expect(page.locator('#password')).toBeVisible()
    await expect(page.locator('button[type="submit"]')).toBeVisible()
  })

  test('使用有效凭据登录后跳转到控制台', async ({ page }) => {
    await page.addInitScript(() => {
      window.localStorage.setItem('workama.locale', 'en-US')
    })
    await page.goto('/login', { waitUntil: 'networkidle' })
    await page.locator('#email').fill(EMAIL)
    await page.locator('#password').fill(PASSWORD)
    await page.locator('button[type="submit"]').click()

    // 验证跳转到 /chat（或经过 /onboarding 中间态）
    await page.waitForURL(/\/chat|\/onboarding|\/admin/, { timeout: 30_000 })
    if (page.url().includes('/onboarding')) {
      await page.getByRole('button', { name: /Enter workspace/ }).click()
      await page.waitForURL(/\/chat/, { timeout: 15_000 })
    }

    // 最终断言：URL 必须包含 /chat，且控制台主标题可见
    await expect(page).toHaveURL(/\/chat/)
    await expect(
      page.getByRole('heading', { name: 'Your command center', exact: true }),
    ).toBeVisible()
  })

  test('未登录访问受保护页面跳转到登录页', async ({ page }) => {
    await page.goto('/chat', { waitUntil: 'networkidle' })
    await page.waitForURL(/\/login/, { timeout: 15_000 })
    await expect(page).toHaveURL(/\/login/)
    // 验证 redirect 参数携带原始路径
    const redirect = new URL(page.url()).searchParams.get('redirect')
    expect(redirect).toBeTruthy()
    expect(redirect).toContain('/chat')
    await expect(page.locator('#email')).toBeVisible()
  })
})
