/**
 * 简化的 axe + 截图测试：跳过 onboarding 文案断言，仅保留 axe + 截图核心。
 * 用于 R212 Round 1 商业级 UI 提升后的 axe-core WCAG 2.2 AA 验证。
 */
import { chromium } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'
import { mkdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

const baseURL = process.env.BROWSER_BASE_URL ?? 'http://localhost:20204'
const email = process.env.WORKAMA_BROWSER_EMAIL ?? 'tester@workama.example.com'
const password = process.env.WORKAMA_BROWSER_PASSWORD ?? 'WorkAMA-Test-2026!'
const outputDir = process.env.EVIDENCE_DIR ?? 'quality/evidence/web-react-final'
const tag = process.env.AXE_TAG ?? 'round1-post-agent'

const ROUTES = [
  '/',
  '/login',
  '/chat',
  '/agents',
  '/work',
  '/knowledge',
  '/workflows',
  '/admin/members',
  '/admin/api-keys',
  '/admin/billing',
  '/admin/security',
  '/admin/observability',
  // R215 v7.230 新增验证路由：保证 axe 在生产端口 20204 上也保持 0 violations
  '/admin/integrations',
  '/admin/settings',
  '/admin/operations',
  '/agents/code',
  '/agents/tools',
  '/agents/automations',
  '/studio/integrations',
  '/studio/marketplace',
  // R221 v7.236 新增 /help 路由（50 个 details FAQ）
  '/help',
  '/pricing',
  '/docs',
  '/status',
  '/trust',
  // v7.253: 补 /search 路由，保证 v7.251 SearchPage 真实字段呈现 + 类型路由 0 violations
  '/search',
]

await mkdir(outputDir, { recursive: true })

const browser = await chromium.launch({
  executablePath: process.env.BROWSER_EXECUTABLE ?? '/usr/bin/chromium',
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--disable-software-rasterizer'],
})

const axeResults = []
const axeBySeverity = { minor: 0, moderate: 0, serious: 0, critical: 0 }
const axeByRoute = {}

async function checkRoute(page, route) {
  await page.goto(`${baseURL}${route}`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1500)
  let pageViolations = []
  try {
    const result = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()
    pageViolations = result.violations
  } catch (err) {
    pageViolations = [{ id: 'axe-error', description: err.message, impact: 'minor', nodes: [], tags: [] }]
  }
  axeByRoute[route] = pageViolations.length
  for (const v of pageViolations) {
    const severity = v.impact || 'minor'
    if (axeBySeverity[severity] !== undefined) axeBySeverity[severity] += v.nodes.length
    else axeBySeverity[severity] = v.nodes.length
    axeResults.push({
      route,
      rule_id: v.id,
      description: v.description,
      severity,
      nodes: v.nodes.length,
      targets: v.nodes.map(n => n.target.join(' ')).slice(0, 3),
      html: v.nodes[0] ? v.nodes[0].html.slice(0, 200) : '',
      failureSummary: v.nodes[0] ? v.nodes[0].failureSummary : '',
    })
  }
  await page.screenshot({ path: join(outputDir, `${route.replace(/\//g, '_') || 'root'}.png`), fullPage: false })
}

const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' })
const page = await context.newPage()

const consoleErrors = []
page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push({ url: page.url(), text: msg.text().slice(0, 300) }) })
page.on('pageerror', (err) => consoleErrors.push({ url: page.url(), text: `pageerror: ${String(err).slice(0, 300)}` }))

let loginSuccess = false
try {
  await page.goto(`${baseURL}/login`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('input[type="email"], input#email', { timeout: 15000 })
  await page.fill('input[type="email"], input#email', email)
  await page.fill('input[type="password"], input#password', password)
  await page.click('button[type="submit"]')
  await page.waitForURL((url) => !/\/(login|register)$/.test(new URL(url).pathname), { timeout: 25000 }).catch(() => {})
  loginSuccess = !page.url().includes('/login')
} catch (e) {
  console.error('login failed:', e.message)
}

for (const route of ROUTES) {
  try {
    await checkRoute(page, route)
  } catch (e) {
    axeResults.push({ route, rule_id: 'check-error', description: e.message, severity: 'minor', nodes: 0 })
  }
}

await browser.close()

const report = {
  tag,
  baseURL,
  startedAt: new Date().toISOString(),
  loginSuccess,
  routesChecked: ROUTES.length,
  totalViolations: axeResults.length,
  bySeverity: axeBySeverity,
  byRoute: axeByRoute,
  violations: axeResults,
  consoleErrors,
}

const reportPath = join(outputDir, `axe-wcag-${tag}.json`)
await writeFile(reportPath, JSON.stringify(report, null, 2))

console.log(JSON.stringify({ ok: axeResults.length === 0, totalViolations: axeResults.length, bySeverity: axeBySeverity, byRoute: axeByRoute, loginSuccess }, null, 2))
process.exit(axeResults.length === 0 ? 0 : 1)