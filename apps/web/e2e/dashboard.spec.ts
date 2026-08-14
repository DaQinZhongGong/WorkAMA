import { test, expect } from '@playwright/test'
import { login } from './helpers'

/**
 * 控制台首页 E2E 测试。
 *
 * 覆盖场景：
 *   1. 登录后侧边栏导航存在
 *   2. 控制台页面标题渲染
 *   3. KPI 卡片区域加载
 */
test.describe('控制台首页', () => {
  test.beforeEach(async ({ page }) => {
    await login(page)
  })

  test('侧边栏导航存在且可交互', async ({ page }) => {
    // 验证侧边栏存在
    await expect(page.locator('aside.sidebar')).toBeVisible()
    // 验证品牌标识
    await expect(page.locator('aside.sidebar .brand-name')).toContainText('WorkAMA')
    // 验证导航项存在（Agents / Work plans / Knowledge 等）
    const navItems = page.locator('aside.sidebar .nav-item')
    await expect(navItems.first()).toBeVisible()
    const navCount = await navItems.count()
    expect(navCount).toBeGreaterThan(3)
  })

  test('控制台主标题与 KPI 卡片渲染', async ({ page }) => {
    // 验证控制台主标题
    await expect(
      page.getByRole('heading', { name: 'Your command center', exact: true }),
    ).toBeVisible()
    // 验证 KPI 卡片区域加载
    await expect(page.locator('.kpi-grid .kpi').first()).toBeVisible({ timeout: 15_000 })
    const kpiCount = await page.locator('.kpi-grid .kpi').count()
    expect(kpiCount).toBeGreaterThanOrEqual(1)
  })

  test('侧边栏导航可跳转到 Agents 页面', async ({ page }) => {
    await page
      .locator('aside.sidebar')
      .getByRole('link', { name: 'Agents', exact: true })
      .first()
      .click()
    await page.waitForURL(/\/agents/, { timeout: 15_000 })
    await expect(
      page.getByRole('heading', { name: 'Agents', exact: true }),
    ).toBeVisible()
  })
})
