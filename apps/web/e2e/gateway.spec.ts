import { test, expect } from '@playwright/test'
import { login } from './helpers'

/**
 * 网关渠道页面 E2E 测试。
 *
 * 覆盖场景：
 *   1. 登录后访问 /gateway/channels 页面加载
 *   2. 网关页面标题渲染
 *   3. 渠道列表或空状态渲染
 */
test.describe('网关渠道页面', () => {
  test.beforeEach(async ({ page }) => {
    await login(page)
  })

  test('网关渠道页面加载成功', async ({ page }) => {
    await page.goto('/gateway/channels', { waitUntil: 'networkidle' })
    await page.waitForURL(/\/gateway\/channels/, { timeout: 15_000 })
    // 验证页面 header 存在（PageHeader 组件渲染）
    await expect(page.locator('.page-header')).toBeVisible()
    // 验证页面内容区域加载
    await expect(page.locator('.console-main .page-content')).toBeVisible()
  })

  test('网关渠道页面有可操作按钮', async ({ page }) => {
    await page.goto('/gateway/channels', { waitUntil: 'networkidle' })
    // 验证刷新按钮存在
    const refreshButton = page.locator('.page-header button', { hasText: /refresh|刷新/i }).first()
    await expect(refreshButton).toBeVisible({ timeout: 15_000 })
  })

  test('网关渠道页面面板渲染', async ({ page }) => {
    await page.goto('/gateway/channels', { waitUntil: 'networkidle' })
    // 验证 Panel 组件渲染（DataTable 或空状态）
    const panel = page.locator('.panel').first()
    await expect(panel).toBeVisible({ timeout: 15_000 })
  })
})
