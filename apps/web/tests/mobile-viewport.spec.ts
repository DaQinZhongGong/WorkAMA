import { test, expect, type Page } from '@playwright/test'

/**
 * WorkAMA Web 移动视口专用测试。
 *
 * 仅在移动端 project（mobile-chrome / mobile-safari）执行；
 * 桌面端 project 自动跳过。
 *
 * 覆盖场景：
 *   1. 响应式布局（无水平滚动）
 *   2. 导航菜单可展开/收起
 *   3. 触摸友好的按钮尺寸（最小 44px）
 */

// 凭据默认值与 e2e-journey.mjs 中的 WORKAMA_BROWSER_EMAIL / WORKAMA_BROWSER_PASSWORD 默认值一致。
const EMAIL = 'tester@workama.example.com'
const PASSWORD = 'WorkAMA-Test-2026!'

/** 触摸目标最小边长（WCAG 2.5.5 推荐 / Apple HIG） */
const MIN_TOUCH_TARGET = 44

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

/**
 * 仅在移动端 project 运行；桌面 project 跳过。
 * 通过 viewport 宽度判定（移动 project 视口宽度 < 768）。
 */
test.beforeEach(({ page }, testInfo) => {
  const isMobileProject = ['mobile-chrome', 'mobile-safari'].includes(testInfo.project.name)
  const viewport = page.viewportSize()
  const isMobileViewport = !!viewport && viewport.width < 768
  test.skip(!isMobileProject && !isMobileViewport, '仅在移动视口下执行')
})

test.describe('移动视口 - 响应式布局', () => {
  test('登录页无水平滚动', async ({ page }) => {
    await page.addInitScript(() => {
      window.localStorage.setItem('workama.locale', 'en-US')
    })
    await page.goto('/login', { waitUntil: 'networkidle' })
    await page.locator('#email').waitFor({ state: 'visible' })
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    )
    expect(overflow).toBeLessThanOrEqual(1)
  })

  test('仪表盘无水平滚动', async ({ page }) => {
    await login(page)
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    )
    expect(overflow).toBeLessThanOrEqual(1)
  })

  test('知识库页无水平滚动', async ({ page }) => {
    await login(page)
    // 移动端侧边栏默认收起（position:fixed 左移出视口），直接导航至知识库页
    await page.goto('/knowledge', { waitUntil: 'networkidle' })
    await page
      .getByRole('heading', { name: 'Knowledge', exact: true })
      .waitFor({ timeout: 15000 })
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    )
    expect(overflow).toBeLessThanOrEqual(1)
  })
})

test.describe('移动视口 - 导航菜单可展开/收起', () => {
  test('侧边栏菜单可展开与收起', async ({ page }) => {
    await login(page)

    // 移动端侧边栏默认收起；查找常见触发按钮（hamburger / menu / toggle）
    // 兼容 aria-label 与 visible text 的常见命名
    const menuToggle = page
      .getByRole('button', { name: /menu|Menu|导航|菜单|toggle sidebar/i })
      .or(page.locator('button[aria-label*="menu" i]'))
      .or(page.locator('button[aria-label*="sidebar" i]'))
      .or(page.locator('button[aria-expanded]'))
      .first()

    // 若找到触发按钮，验证可展开/收起；否则跳过（部分布局可能采用 Tab bar 替代侧边栏）
    const toggleVisible = await menuToggle.isVisible().catch(() => false)
    test.skip(!toggleVisible, '当前布局未提供侧边栏切换按钮（可能使用 Tab bar）')

    // 展开
    await menuToggle.click()
    await expect(page.locator('aside.sidebar')).toBeVisible({ timeout: 5000 })

    // 收起（再次点击同一按钮）
    await menuToggle.click()
    // 侧边栏在移动端收起后应不可见或移出视口
    await expect(page.locator('aside.sidebar')).toBeHidden({ timeout: 5000 })
  })
})

test.describe('移动视口 - 触摸友好按钮尺寸', () => {
  /**
   * 校验页面上所有可见 button / role=button 元素的最小外接尺寸 ≥ 44px。
   * 容差 1px 以应对亚像素渲染。
   */
  async function assertTouchTargets(page: Page, label: string) {
    const boxes = await page.evaluate(() => {
      const els = Array.from(
        document.querySelectorAll('button, [role="button"], a[href]'),
      )
      return els
        .filter((el) => {
          const rect = el.getBoundingClientRect()
          const style = window.getComputedStyle(el)
          return (
            rect.width > 0 &&
            rect.height > 0 &&
            style.visibility !== 'hidden' &&
            style.display !== 'none'
          )
        })
        .map((el) => {
          const rect = el.getBoundingClientRect()
          return {
            tag: el.tagName.toLowerCase(),
            text: (el.textContent || '').trim().slice(0, 40),
            width: rect.width,
            height: rect.height,
          }
        })
    })

    const tooSmall = boxes.filter(
      (b) => b.width < MIN_TOUCH_TARGET - 1 || b.height < MIN_TOUCH_TARGET - 1,
    )
    expect(
      tooSmall,
      `[${label}] 发现 ${tooSmall.length} 个触摸目标小于 ${MIN_TOUCH_TARGET}px: ${JSON.stringify(
        tooSmall.slice(0, 5),
      )}`,
    ).toEqual([])
  }

  test('登录页按钮触摸友好', async ({ page }) => {
    await page.addInitScript(() => {
      window.localStorage.setItem('workama.locale', 'en-US')
    })
    await page.goto('/login', { waitUntil: 'networkidle' })
    await page.locator('button[type="submit"]').waitFor({ state: 'visible' })
    await assertTouchTargets(page, '登录页')
  })

  test('仪表盘按钮触摸友好', async ({ page }) => {
    await login(page)
    await assertTouchTargets(page, '仪表盘')
  })
})
