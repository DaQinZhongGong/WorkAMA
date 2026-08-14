/**
 * WorkAMA 控制台 UI 视觉核验脚本。
 *
 * 用途：每轮 UI 改动后，登录测试账号并对关键路由截图，
 * 输出到 quality/evidence/ui-capture/<ts>/，供人工复核与前后对比。
 *
 * 运行：
 *   node tests/ui-capture.mjs                      # 全量
 *   ROUTES=/,/chat node tests/ui-capture.mjs       # 指定路由
 *   UI_CAPTURE_TAG=round1 node tests/ui-capture.mjs
 */
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { loadPlaywright, resolveExecutablePath } from './pw-loader.mjs'

const { mod: pw, source: pwSource } = await loadPlaywright()
const { chromium } = pw
const executablePath = resolveExecutablePath()
console.log(`[ui-capture] playwright=${pwSource} chromium=${executablePath ?? 'bundled'}`)

const baseURL = process.env.BROWSER_BASE_URL ?? 'http://localhost:20204'
const email = process.env.WORKAMA_BROWSER_EMAIL ?? 'tester@workama.example.com'
const password = process.env.WORKAMA_BROWSER_PASSWORD ?? 'WorkAMA-Test-2026!'
const tag = process.env.UI_CAPTURE_TAG ?? new Date().toISOString().replace(/[:.]/g, '-')
const outputDir = process.env.EVIDENCE_DIR ?? path.join('..', '..', 'quality', 'evidence', 'ui-capture', tag)

const DEFAULT_ROUTES = [
  '/login',
  '/',
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
]

const routes = process.env.ROUTES ? process.env.ROUTES.split(',').map((r) => r.trim()).filter(Boolean) : DEFAULT_ROUTES

function slug(route) {
  return route === '/' ? 'root' : route.replace(/^\//, '').replace(/\//g, '-')
}

await mkdir(outputDir, { recursive: true })

const browser = await chromium.launch({
  executablePath,
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--disable-software-rasterizer'],
})
const context = await browser.newContext({
  viewport: { width: 1440, height: 900 },
  locale: 'zh-CN',
  deviceScaleFactor: 1,
})
await context.addInitScript(() => {
  window.localStorage.setItem('workama.locale', 'zh-CN')
})
const page = await context.newPage()

const consoleErrors = []
page.on('console', (msg) => {
  if (msg.type() === 'error') consoleErrors.push({ url: page.url(), text: msg.text().slice(0, 400) })
})
page.on('pageerror', (err) => {
  consoleErrors.push({ url: page.url(), text: `pageerror: ${String(err).slice(0, 400)}` })
})

const report = { baseURL, tag, startedAt: new Date().toISOString(), shots: [], consoleErrors: [] }

// 未登录态先截 /login
if (routes.includes('/login')) {
  await page.goto(`${baseURL}/login`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(600)
  const file = path.join(outputDir, '00-login.png')
  await page.screenshot({ path: file, fullPage: true })
  report.shots.push({ route: '/login', file, title: await page.title() })
}

// 登录
await page.goto(`${baseURL}/login`, { waitUntil: 'networkidle' })
await page.locator('#email').fill(email)
await page.locator('#password').fill(password)
await page.locator('button[type="submit"]').click()
await page.waitForURL(/\/chat|\/onboarding|\/admin|\/$/, { timeout: 30_000 }).catch(() => {})
if (page.url().includes('/onboarding')) {
  const enter = page.getByRole('button', { name: /进入工作区|Enter workspace/ })
  if (await enter.count()) {
    await enter.first().click()
    await page.waitForTimeout(1500)
  }
}
report.loggedInUrl = page.url()

let index = 1
for (const route of routes) {
  if (route === '/login') continue
  try {
    await page.goto(`${baseURL}${route}`, { waitUntil: 'networkidle', timeout: 30_000 })
    await page.waitForTimeout(900)
    const file = path.join(outputDir, `${String(index).padStart(2, '0')}-${slug(route)}.png`)
    await page.screenshot({ path: file, fullPage: true })
    report.shots.push({ route, file, title: await page.title() })
  } catch (err) {
    report.shots.push({ route, error: String(err).slice(0, 300) })
  }
  index += 1
}

report.consoleErrors = consoleErrors
report.finishedAt = new Date().toISOString()
await writeFile(path.join(outputDir, 'report.json'), JSON.stringify(report, null, 2), 'utf8')

await browser.close()

console.log(`captured ${report.shots.length} shots -> ${outputDir}`)
if (consoleErrors.length) {
  console.log(`console errors: ${consoleErrors.length}`)
  for (const e of consoleErrors.slice(0, 10)) console.log(`  - ${e.text}`)
}
