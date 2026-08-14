// WorkAMA web console end-to-end smoke verification.
// Runs against the local docker compose stack; writes evidence JSON + screenshots.
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const shotDir = join(here, 'screenshots')
const evidencePath = resolve(here, '..', 'evidence', 'web-e2e-smoke.json')
const baseUrl = process.env.WORKAMA_WEB_URL ?? 'http://localhost:20204'
const email = process.env.WORKAMA_TEST_EMAIL ?? 'tester@workama.example.com'
const password = process.env.WORKAMA_TEST_PASSWORD ?? 'WorkAMA-Test-2026!'

mkdirSync(shotDir, { recursive: true })
mkdirSync(dirname(evidencePath), { recursive: true })

const consoleErrors = []
const screenshotPaths = []
const pagesVisited = []
const notes = []

function recordConsole(where, text) {
  const entry = `${where}: ${text}`
  if (consoleErrors.length < 60 && !consoleErrors.includes(entry)) consoleErrors.push(entry)
}

async function shoot(page, name) {
  const path = join(shotDir, `${name}.png`)
  await page.screenshot({ path, fullPage: false })
  screenshotPaths.push(path)
  return path
}

// A page counts as rendered when the console shell mounted and real content is present.
async function assertRendered(page, label, path) {
  const shell = page.locator('main.console-main')
  let ok = false
  let detail = ''
  try {
    await shell.first().waitFor({ state: 'visible', timeout: 20000 })
    // Wait for async panels to settle instead of asserting on a loading skeleton.
    await page.waitForTimeout(1200)
    const text = (await shell.first().innerText()).replace(/\s+/g, ' ').trim()
    const heading = (await page.locator('main.console-main h1, main.console-main h2').first().innerText().catch(() => '')).trim()
    ok = text.length > 40
    detail = heading || text.slice(0, 80)
    if (!ok) detail = `console shell rendered but content was near-empty (${text.length} chars)`
  } catch (error) {
    detail = `shell not visible: ${error.message.split('\n')[0]}`
  }
  pagesVisited.push({ path, ok, label, detail })
  return ok
}

async function goto(page, path) {
  await page.goto(baseUrl + path, { waitUntil: 'domcontentloaded', timeout: 30000 })
  await page.waitForLoadState('networkidle', { timeout: 20000 }).catch(() => {})
}

let loginSuccess = false
let status = 'failed'
const browser = await chromium.launch({ headless: true })

try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 } })
  const page = await context.newPage()
  page.on('console', (message) => { if (message.type() === 'error') recordConsole(page.url(), message.text()) })
  page.on('pageerror', (error) => recordConsole(page.url(), `pageerror ${error.message}`))
  page.on('requestfailed', (request) => {
    const failure = request.failure()?.errorText ?? 'unknown'
    if (failure !== 'net::ERR_ABORTED') recordConsole(page.url(), `requestfailed ${request.url()} ${failure}`)
  })

  // 1. App shell loads.
  await goto(page, '/')
  const title = await page.title()
  if (!title.toLowerCase().includes('workama')) throw new Error(`unexpected document title: ${title}`)
  await page.locator('#app').waitFor({ state: 'attached', timeout: 15000 })
  const mountedText = (await page.locator('#app').innerText()).trim()
  if (!mountedText) throw new Error('React root #app mounted but rendered nothing')
  notes.push(`app shell loaded, title="${title}", landing url=${page.url()}`)

  // 2. Login. The root route redirects unauthenticated visitors to /login.
  if (!page.url().includes('/login')) await goto(page, '/login')
  const emailInput = page.locator('input[type="email"], input#email, input[autocomplete="email"]').first()
  const passwordInput = page.locator('input[type="password"], input#password').first()
  await emailInput.waitFor({ state: 'visible', timeout: 15000 })
  await passwordInput.waitFor({ state: 'visible', timeout: 15000 })
  await emailInput.fill(email)
  await passwordInput.fill(password)
  await shoot(page, '01-login')
  await page.locator('form button[type="submit"]').first().click()

  // Post-login state: routed away from /login into the authenticated console shell.
  try {
    await page.waitForURL((url) => !/\/(login|register)$/.test(new URL(url).pathname), { timeout: 25000 })
    await page.locator('main.console-main').first().waitFor({ state: 'visible', timeout: 20000 })
    loginSuccess = true
    notes.push(`login succeeded, landed on ${new URL(page.url()).pathname}`)
  } catch (error) {
    const alert = await page.locator('.alert-error, [role="alert"]').first().innerText().catch(() => '')
    notes.push(`login failed: ${alert || error.message.split('\n')[0]}`)
  }

  if (loginSuccess) {
    // 3. Core areas reachable after login.
    const targets = [
      { path: '/chat', label: 'Chat command center' },
      { path: '/knowledge', label: 'Knowledge bases' },
      { path: '/settings', label: 'Workspace settings' },
      { path: '/agents', label: 'Agents' },
    ]
    for (const target of targets) {
      await goto(page, target.path)
      const ok = await assertRendered(page, target.label, target.path)
      if (target.path === '/chat' && ok) await shoot(page, '02-chat')
      if (target.path === '/settings' && ok) await shoot(page, '03-settings')
    }
    // Verify in-app navigation works, not just direct URL entry.
    await goto(page, '/chat')
    const sidebarLink = page.locator('#primary-sidebar a[href="/knowledge"], nav a[href="/knowledge"]').first()
    if (await sidebarLink.count()) {
      await sidebarLink.click()
      await page.waitForURL(/\/knowledge/, { timeout: 15000 }).catch(() => {})
      notes.push(`sidebar navigation to /knowledge -> ${new URL(page.url()).pathname}`)
    } else {
      notes.push('sidebar link to /knowledge not found; only direct URL navigation verified')
    }
  }

  const allOk = pagesVisited.length >= 3 && pagesVisited.every((item) => item.ok)
  status = loginSuccess && allOk ? 'verified_local' : 'failed'
} catch (error) {
  notes.push(`fatal: ${error.message.split('\n')[0]}`)
  status = 'failed'
} finally {
  await browser.close()
}

const evidence = {
  verification_scope: 'local-compose',
  url: baseUrl,
  login_success: loginSuccess,
  pages_visited: pagesVisited,
  console_errors: consoleErrors,
  screenshot_paths: screenshotPaths,
  status,
  timestamp: new Date().toISOString(),
  notes,
}
writeFileSync(evidencePath, `${JSON.stringify(evidence, null, 2)}\n`)
console.log(JSON.stringify(evidence, null, 2))
process.exit(status === 'verified_local' ? 0 : 1)
