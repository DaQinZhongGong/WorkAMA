import type { Page } from '@playwright/test'

/**
 * WorkAMA E2E 测试共享辅助函数。
 *
 * 凭据默认值与项目现有 tests/e2e-journey.mjs 保持一致，
 * 可通过环境变量 WORKAMA_BROWSER_EMAIL / WORKAMA_BROWSER_PASSWORD 覆盖。
 */
export const EMAIL = process.env.WORKAMA_BROWSER_EMAIL ?? 'tester@workama.example.com'
export const PASSWORD = process.env.WORKAMA_BROWSER_PASSWORD ?? 'WorkAMA-Test-2026!'

/**
 * 通过 UI 执行登录流程。
 *
 * 流程：访问 /login → 填写邮箱密码 → 提交 → 等待跳转。
 * 处理 onboarding 中间态，确保最终进入 /chat 主界面。
 *
 * 锁定英文 locale，使断言文本与 UI 渲染稳定。
 */
export async function login(page: Page): Promise<void> {
  await page.addInitScript(() => {
    window.localStorage.setItem('workama.locale', 'en-US')
  })
  await page.goto('/login', { waitUntil: 'networkidle' })
  await page.locator('#email').fill(EMAIL)
  await page.locator('#password').fill(PASSWORD)
  await page.locator('button[type="submit"]').click()
  await page.waitForURL(/\/chat|\/onboarding|\/admin/, { timeout: 30_000 })
  // 处理 onboarding 引导中间态
  if (page.url().includes('/onboarding')) {
    await page.getByRole('button', { name: /Enter workspace/ }).click()
    await page.waitForURL(/\/chat/, { timeout: 15_000 })
  }
  // 确保最终进入 /chat
  await page
    .getByRole('heading', { name: 'Your command center', exact: true })
    .waitFor({ timeout: 20_000 })
}
