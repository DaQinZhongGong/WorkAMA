import { test, expect } from '@playwright/test'
import { login } from './helpers'

/**
 * 工作区管理 E2E 测试。
 *
 * 覆盖场景：
 *   1. 登录后访问 /admin/workspaces 页面加载
 *   2. 工作区列表容器渲染
 *   3. Admin 侧边栏导航存在
 */
test.describe('工作区管理', () => {
  test.beforeEach(async ({ page }) => {
    await login(page)
  })

  test('工作区页面加载并渲染标题', async ({ page }) => {
    await page.goto('/admin/workspaces', { waitUntil: 'networkidle' })
    await page.waitForURL(/\/admin\/workspaces/, { timeout: 15_000 })
    // AdminPageShell 渲染的标题（中文 "工作区管理"）
    await expect(
      page.getByRole('heading', { name: '工作区管理', exact: true }),
    ).toBeVisible()
  })

  test('工作区列表容器或加载状态渲染', async ({ page }) => {
    await page.goto('/admin/workspaces', { waitUntil: 'networkidle' })
    // AdminPageShell 的 testId 容器
    await expect(page.locator('[data-testid="workspaces-page"]')).toBeVisible()
    // 验证列表容器存在（可能为空但有 ul.resource-list）
    const listContainer = page.locator('[data-testid="workspaces-list"]')
    const loadingState = page.locator('.state-loading, .state-view')
    // 至少应有列表容器或加载状态之一
    await expect(listContainer.or(loadingState).first()).toBeVisible({ timeout: 15_000 })
  })

  test('Admin 侧边栏导航存在', async ({ page }) => {
    await page.goto('/admin/workspaces', { waitUntil: 'networkidle' })
    await expect(page.locator('[data-testid="admin-sidebar"]')).toBeVisible()
    await expect(page.locator('[data-testid="admin-nav"]')).toBeVisible()
    // 验证导航项数量
    const navItems = page.locator('[data-testid="admin-nav"] .nav-item')
    const navCount = await navItems.count()
    expect(navCount).toBeGreaterThan(3)
  })
})
