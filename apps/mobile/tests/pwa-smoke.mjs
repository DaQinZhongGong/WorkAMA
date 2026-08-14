import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from '@playwright/test'

const baseURL = process.env.MOBILE_PWA_BASE_URL ?? 'http://127.0.0.1:4174'
const root = fileURLToPath(new URL('../', import.meta.url))
let server

async function ready() {
  try { return (await fetch(`${baseURL}/`)).ok } catch { return false }
}
if (!(await ready())) {
  server = spawn(process.execPath, [join(root, 'node_modules', 'vite', 'bin', 'vite.js'), 'preview', '--host', '127.0.0.1', '--port', '4174'], { cwd: root, stdio: 'ignore', windowsHide: true })
  for (let attempt = 0; attempt < 40 && !(await ready()); attempt += 1) await new Promise((resolve) => setTimeout(resolve, 500))
}
assert.ok(await ready(), `Mobile preview did not become ready at ${baseURL}`)

const browser = await chromium.launch({ headless: true, ...(process.env.BROWSER_EXECUTABLE ? { executablePath: process.env.BROWSER_EXECUTABLE, args: ['--no-sandbox', '--disable-dev-shm-usage'] } : {}) })
const context = await browser.newContext({ viewport: { width: 390, height: 844 } })
const page = await context.newPage()
try {
  await page.goto(`${baseURL}/`, { waitUntil: 'networkidle' })
  const manifest = await page.evaluate(() => fetch('/manifest.webmanifest').then((response) => response.json()))
  assert.equal(manifest.name, 'WorkAMA Mobile')
  assert.equal(manifest.display, 'standalone')
  assert.equal(manifest.start_url, '/')
  assert.equal(manifest.icons.length, 2)

  const registration = await page.evaluate(async () => {
    const item = await navigator.serviceWorker.ready
    const names = await caches.keys()
    const entries = await Promise.all(names.map(async (name) => ({ name, urls: (await (await caches.open(name)).keys()).map((request) => new URL(request.url).pathname) })))
    return { scope: item.scope, scriptURL: item.active?.scriptURL, names, entries }
  })
  assert.equal(registration.scope, `${baseURL}/`)
  assert.match(registration.scriptURL, /\/sw\.js$/)
  assert.ok(registration.names.includes('workama-mobile-shell-v1'))
  assert.ok(registration.entries.flatMap((item) => item.urls).every((url) => !url.startsWith('/api/') && !url.startsWith('/v1/')))
  assert.ok(await page.locator('#mobile-email').count())
  const layout = await page.evaluate(() => ({ viewport: document.documentElement.clientWidth, scrollWidth: document.documentElement.scrollWidth }))
  assert.equal(layout.viewport, 390)
  assert.equal(layout.scrollWidth, layout.viewport, `Mobile layout overflows horizontally: ${JSON.stringify(layout)}`)
  process.stdout.write(JSON.stringify({ ok: true, viewport: '390x844', horizontal_overflow: false, service_worker: 'workama-mobile-shell-v1', credential_persistence: 'memory-only' }) + '\n')
} finally {
  await context.close()
  await browser.close()
  if (server) server.kill()
}
