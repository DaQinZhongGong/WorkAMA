import { test, expect } from '@playwright/test'
import { login } from './helpers'

/**
 * 助手管理 E2E 测试。
 *
 * 覆盖场景：
 *   1. 登录后访问 /admin/assistants 页面加载
 *   2. 助手列表容器渲染
 *   3. 创建表单可见
 */
test.describe('助手管理', () => {
  test.beforeEach(async ({ page }) => {
    await login(page)
  })

  test('助手页面加载并渲染标题', async ({ page }) => {
    await page.goto('/admin/assistants', { waitUntil: 'networkidle' })
    await page.waitForURL(/\/admin\/assistants/, { timeout: 15_000 })
    // AdminPageShell 渲染的标题（中文 "助手管理"）
    await expect(
      page.getByRole('heading', { name: '助手管理', exact: true }),
    ).toBeVisible()
  })

  test('助手列表容器或加载状态渲染', async ({ page }) => {
    await page.goto('/admin/assistants', { waitUntil: 'networkidle' })
    await expect(page.locator('[data-testid="assistants-page"]')).toBeVisible()
    // 验证列表容器存在
    const listContainer = page.locator('[data-testid="assistants-list"]')
    const loadingState = page.locator('.state-loading, .state-view')
    await expect(listContainer.or(loadingState).first()).toBeVisible({ timeout: 15_000 })
  })

  test('创建助手表单可见', async ({ page }) => {
    await page.goto('/admin/assistants', { waitUntil: 'networkidle' })
    // AdminCreateForm 的 testId
    await expect(page.locator('[data-testid="assistants-create"]')).toBeVisible()
    // 验证表单字段存在
    await expect(page.locator('[data-testid="assistants-create-name"]')).toBeVisible()
    await expect(page.locator('[data-testid="assistants-create-model"]')).toBeVisible()
  })
})
