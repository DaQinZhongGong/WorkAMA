import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from '@playwright/test'

const baseURL = process.env.PWA_BASE_URL ?? 'http://127.0.0.1:4173'
const webRootPath = fileURLToPath(new URL('../', import.meta.url))
let server

async function isReady() {
  try {
    const response = await fetch(`${baseURL}/login`)
    return response.ok
  } catch {
    return false
  }
}

async function waitForServer() {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (await isReady()) return
    await new Promise((resolve) => setTimeout(resolve, 500))
  }
  throw new Error(`Web preview did not become ready at ${baseURL}`)
}

if (!(await isReady())) {
  const viteCLI = join(webRootPath, 'node_modules', 'vite', 'bin', 'vite.js')
  server = spawn(process.execPath, [viteCLI, 'preview', '--host', '127.0.0.1', '--port', '4173'], {
    cwd: webRootPath,
    stdio: 'ignore',
    windowsHide: true,
  })
}

await waitForServer()
const browser = await chromium.launch({
  headless: true,
  ...(process.env.BROWSER_EXECUTABLE ? { executablePath: process.env.BROWSER_EXECUTABLE, args: ['--no-sandbox', '--disable-dev-shm-usage'] } : {}),
})
const context = await browser.newContext({ viewport: { width: 390, height: 844 } })
const page = await context.newPage()

try {
  await page.goto(`${baseURL}/login`, { waitUntil: 'networkidle' })
  const manifest = await page.evaluate(async () => fetch('/manifest.webmanifest').then((response) => response.json()))
  assert.equal(manifest.name, 'WorkAMA')
  assert.equal(manifest.display, 'standalone')
  assert.equal(manifest.start_url, '/chat')
  assert.equal(manifest.theme_color, '#111827')
  assert.equal(manifest.icons.length, 2)
  assert.ok(manifest.icons.every((icon) => icon.src.startsWith('data:image/svg+xml,')))

  const serviceWorker = await page.evaluate(async () => {
    const registration = await navigator.serviceWorker.ready
    const cacheNames = await caches.keys()
    const cacheEntries = await Promise.all(cacheNames.map(async (name) => {
      const cache = await caches.open(name)
      return { name, urls: (await cache.keys()).map((request) => request.url) }
    }))
    return {
      scope: registration.scope,
      scriptURL: registration.active?.scriptURL,
      cacheNames,
      cacheEntries,
    }
  })
  assert.equal(serviceWorker.scope, `${baseURL}/`)
  assert.match(serviceWorker.scriptURL, /\/sw\.js$/)
  assert.ok(serviceWorker.cacheNames.includes('workama-shell-v1'))
  const cachedURLs = serviceWorker.cacheEntries.flatMap(({ urls }) => urls)
  assert.ok(cachedURLs.some((url) => url.endsWith('/index.html')))
  assert.ok(cachedURLs.every((url) => !new URL(url).pathname.startsWith('/api/')))
  assert.ok(cachedURLs.every((url) => !new URL(url).pathname.startsWith('/v1/')))

  await context.setOffline(true)
  await page.goto(`${baseURL}/chat`, { waitUntil: 'domcontentloaded' })
  assert.equal(await page.title(), 'WorkAMA')
  assert.ok(await page.locator('#app').count())
  process.stdout.write(JSON.stringify({
    ok: true,
    viewport: '390x844',
    service_worker: 'workama-shell-v1',
    offline_shell: true,
    push: 'pending',
    native_capabilities: 'pending',
  }) + '\n')
} finally {
  await context.close()
  await browser.close()
  if (server) server.kill()
}
