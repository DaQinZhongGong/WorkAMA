import { test, expect, type Page } from '@playwright/test'

/**
 * WorkAMA Web 视觉回归测试。
 *
 * 对关键页面进行截图对比，基线首次运行时自动生成于
 * `tests/__screenshots__/<projectName>/...`。
 *
 * 覆盖页面：
 *   1. 登录页      /login
 *   2. 仪表盘      /chat（Your command center）
 *   3. 知识库列表  /knowledge
 *   4. 助手对话页  /agents
 *   5. 设置页      /admin/api-keys
 *
 * 配置：
 *   - maxDiffPixelRatio: 0.1（允许 10% 像素差异）
 *   - threshold: 0.2
 *   - animations: 'disabled'
 */

// 凭据默认值与 e2e-journey.mjs 中的 WORKAMA_BROWSER_EMAIL / WORKAMA_BROWSER_PASSWORD 默认值一致。
// 如需在 CI 覆盖，可通过 Playwright 的 storageState 或自定义 fixture 注入。
const EMAIL = 'tester@workama.example.com'
const PASSWORD = 'WorkAMA-Test-2026!'

const SNAPSHOT_OPTIONS = {
  maxDiffPixelRatio: 0.1,
  threshold: 0.2,
  animations: 'disabled' as const,
}

/**
 * 登录辅助：与 e2e-journey.mjs 中 login() 保持一致。
 * 锁定英文 locale，使断言文本与 UI 渲染稳定。
 */
async function login(page: Page) {
  await page.addInitScript(() => {
    window.localStorage.setItem('workama.locale', 'en-US')
  })
  await page.goto('/login', { waitUntil: 'networkidle' })
  await page.locator('#email').fill(EMAIL)
  await page.locator('#password').fill(PASSWORD)
  await page.locator('button[type="submit"]').click()
  await page.waitForURL(/\/chat/, { timeout: 20000 })
  await page
    .getByRole('heading', { name: 'Your command center', exact: true })
    .waitFor({ timeout: 20000 })
}

test.describe('视觉回归 - 关键页面截图对比', () => {
  test('登录页', async ({ page }) => {
    await page.addInitScript(() => {
      window.localStorage.setItem('workama.locale', 'en-US')
    })
    await page.goto('/login', { waitUntil: 'networkidle' })
    await page.locator('#email').waitFor({ state: 'visible' })
    await page.locator('#password').waitFor({ state: 'visible' })
    await expect(page).toHaveScreenshot('login-page.png', SNAPSHOT_OPTIONS)
  })

  test('仪表盘', async ({ page }) => {
    await login(page)
    await expect(page).toHaveScreenshot('dashboard.png', SNAPSHOT_OPTIONS)
  })

  test('知识库列表', async ({ page }) => {
    await login(page)
    await page.locator('aside.sidebar').getByRole('link', { name: 'Knowledge', exact: true }).first().click()
    await page.waitForURL(/\/knowledge/, { timeout: 15000 })
    await page.getByRole('heading', { name: 'Knowledge', exact: true }).waitFor({ timeout: 15000 })
    await expect(page).toHaveScreenshot('knowledge-list.png', SNAPSHOT_OPTIONS)
  })

  test('助手对话页', async ({ page }) => {
    await login(page)
    await page.locator('aside.sidebar').getByRole('link', { name: 'Agents', exact: true }).first().click()
    await page.waitForURL(/\/agents/, { timeout: 15000 })
    await page.getByRole('heading', { name: 'Agents', exact: true }).waitFor({ timeout: 15000 })
    await expect(page).toHaveScreenshot('agents-page.png', SNAPSHOT_OPTIONS)
  })

  test('设置页', async ({ page }) => {
    await login(page)
    await page.goto('/admin/api-keys', { waitUntil: 'networkidle' })
    await page.waitForURL(/\/admin\/api-keys/, { timeout: 15000 })
    // 设置页主容器稳定后截图；以 main/heading 任一可见为就绪信号
    await page.waitForLoadState('networkidle')
    await expect(page).toHaveScreenshot('settings-page.png', SNAPSHOT_OPTIONS)
  })
})
