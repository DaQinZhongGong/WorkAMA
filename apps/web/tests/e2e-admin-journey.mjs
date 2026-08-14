/**
 * WorkAMA Admin 后台 E2E 旅程测试。
 *
 * 登录 -> 访问 /admin -> 验证侧边栏导航 -> 访问 3 个子页面验证渲染。
 * 通过 Playwright 驱动 chromium，与 e2e-journey.mjs 保持一致的 API 路由策略。
 */
import { chromium } from '@playwright/test'
import { mkdir, writeFile } from 'node:fs/promises'

const baseURL = process.env.BROWSER_BASE_URL ?? 'http://localhost:20204'
const outputDir = process.env.EVIDENCE_DIR ?? 'quality/evidence/web-admin-journey'
const email = process.env.WORKAMA_BROWSER_EMAIL ?? 'tester@workama.example.com'
const password = process.env.WORKAMA_BROWSER_PASSWORD ?? 'WorkAMA-Test-2026!'

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'] })
const context = await browser.newContext()
const page = await context.newPage()

await mkdir(outputDir, { recursive: true })
const results = []
const started_at = new Date().toISOString()

function log(step, status, detail = '') {
  results.push({ step, status, detail, timestamp: new Date().toISOString() })
  console.log(`[${status}] ${step}${detail ? ': ' + detail : ''}`)
}

try {
  // 1. Login
  log('navigate', 'start', baseURL)
  await page.goto(`${baseURL}/login`, { waitUntil: 'networkidle' })
  await page.fill('input[type="email"]', email)
  await page.fill('input[type="password"]', password)
  await page.click('button[type="submit"]')
  await page.waitForURL(/\/chat|\/onboarding|\/admin/, { timeout: 20000 })
  log('login', 'pass', page.url())

  // 2. Navigate to /admin
  await page.goto(`${baseURL}/admin`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-testid="admin-layout"]', { timeout: 10000 })
  log('admin-dashboard', 'pass', 'Admin layout rendered')

  // 3. Verify sidebar navigation
  const sidebar = page.locator('[data-testid="admin-sidebar"]')
  await sidebar.waitFor({ state: 'visible' })
  const navLinks = page.locator('[data-testid="admin-nav"] a')
  const navCount = await navLinks.count()
  if (navCount >= 12) {
    log('sidebar-nav', 'pass', `${navCount} nav items found`)
  } else {
    log('sidebar-nav', 'warn', `Expected >=12 nav items, found ${navCount}`)
  }

  // 4. Visit workspaces page
  await page.goto(`${baseURL}/admin/workspaces`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-testid="workspaces-page"]', { timeout: 10000 })
  log('admin-workspaces', 'pass', 'Workspaces page rendered')

  // 5. Visit billing page
  await page.goto(`${baseURL}/admin/billing`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-testid="billing-page"]', { timeout: 10000 })
  log('admin-billing', 'pass', 'Billing page rendered')

  // 6. Visit audit-logs page
  await page.goto(`${baseURL}/admin/audit-logs`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-testid="audit-logs-page"]', { timeout: 10000 })
  log('admin-audit-logs', 'pass', 'Audit logs page rendered')
} catch (error) {
  log('journey', 'fail', error instanceof Error ? error.message : String(error))
  try { await page.screenshot({ path: `${outputDir}/admin-journey-fail.png` }) } catch { /* ignore */ }
} finally {
  await browser.close()
}

const report = { started_at, finished_at: new Date().toISOString(), results, all_passed: !results.some((r) => r.status === 'fail') }
await writeFile(`${outputDir}/admin-journey-report.json`, JSON.stringify(report, null, 2))
console.log(`\nE2E journey complete. All passed: ${report.all_passed}`)
process.exit(report.all_passed ? 0 : 1)
