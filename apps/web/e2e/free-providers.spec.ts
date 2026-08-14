import { test, expect } from '@playwright/test'

/**
 * 免费供应商公开页面 E2E 测试。
 *
 * /free-providers 是公开路由，无需登录即可访问。
 *
 * 覆盖场景：
 *   1. 访问 /free-providers 返回 200 且页面有内容
 *   2. 页面标题渲染
 *   3. 供应商卡片或空状态渲染
 */
test.describe('免费供应商页面', () => {
  test('页面返回 200 且有内容', async ({ page }) => {
    const response = await page.goto('/free-providers', { waitUntil: 'networkidle' })
    expect(response).not.toBeNull()
    expect(response?.status()).toBe(200)
    // 验证页面有实际内容（非空白页）
    const bodyText = await page.locator('body').innerText()
    expect(bodyText.trim().length).toBeGreaterThan(0)
  })

  test('页面标题与头部渲染', async ({ page }) => {
    await page.goto('/free-providers', { waitUntil: 'networkidle' })
    // 验证 page-header 存在
    await expect(page.locator('.page-header, .free-providers-header').first()).toBeVisible()
    // 验证 h1 标题存在
    await expect(page.locator('h1').first()).toBeVisible()
  })

  test('供应商卡片或加载/空状态渲染', async ({ page }) => {
    await page.goto('/free-providers', { waitUntil: 'networkidle' })
    // 验证至少有卡片、加载状态或空状态之一
    const cards = page.locator('[data-testid="free-providers-card"]')
    const stateView = page.locator('.state-loading, .state-view, .state-empty')
    const cardCount = await cards.count()
    if (cardCount === 0) {
      // 没有卡片时，至少应有加载/空状态
      await expect(stateView.first()).toBeVisible({ timeout: 15_000 })
    } else {
      expect(cardCount).toBeGreaterThan(0)
    }
  })
})
