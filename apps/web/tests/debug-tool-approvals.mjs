import { chromium } from '@playwright/test'

const browser = await chromium.launch({
  headless: true,
  executablePath: '/usr/bin/chromium',
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
})
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
const page = await context.newPage()
await page.addInitScript(() => window.localStorage.setItem('workama.locale', 'en-US'))

// Token caching + API routing (mirrors e2e-journey.mjs setupApiRouting)
let cachedAccessToken = null

page.on('response', async (response) => {
  const url = response.url()
  if ((url.includes('/api/v1/auth/login') || url.includes('/api/v1/auth/refresh')) && response.ok()) {
    try {
      const body = await response.json()
      if (body.access_token) {
        cachedAccessToken = body.access_token
        console.log('[debug] cached access_token from', url.includes('login') ? 'login' : 'refresh')
      }
    } catch (e) {
      console.log('[debug] failed to parse response body from', url, e.message)
    }
  }
})

page.on('requestfailed', (request) => {
  console.log('[debug] requestfailed:', request.url(), request.failure()?.errorText)
})

await page.route('http://localhost:20200/**', (route) => {
  const original = route.request().url()
  if (original.includes('/api/v1/auth/refresh') && cachedAccessToken) {
    console.log('[debug] fulfilling refresh with cached token')
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ access_token: cachedAccessToken }),
    })
    return
  }
  const rewritten = original.replace('http://localhost:20200', 'http://platform-api:8000')
  route.continue({ url: rewritten })
})

// login
console.log('[debug] logging in...')
await page.goto('http://localhost:20204/login', { waitUntil: 'networkidle' })
await page.locator('#email').fill('tester@workama.example.com')
await page.locator('#password').fill('WorkAMA-Test-2026!')
await page.locator('button[type="submit"]').click()
await page.waitForURL(/\/chat|\/onboarding|\/admin/, { timeout: 20000 })
if (page.url().includes('/onboarding')) {
  await page.getByRole('button', { name: /Enter workspace/ }).click()
  await page.waitForURL(/\/chat/, { timeout: 15000 })
}
console.log('[debug] after login, URL:', page.url())
console.log('[debug] cachedAccessToken present:', !!cachedAccessToken)

// Check sessionStorage token
const sessionToken = await page.evaluate(() => sessionStorage.getItem('workama_access_token'))
console.log('[debug] sessionStorage token present:', !!sessionToken)

// go to tool-approvals via full page reload
console.log('[debug] navigating to /admin/tool-approvals...')
await page.goto('http://localhost:20204/admin/tool-approvals', { waitUntil: 'networkidle' })
await page.waitForTimeout(3000)
console.log('[debug] after goto, URL:', page.url())
const h1 = await page.locator('h1').allTextContents()
const bodyText = await page.locator('main').innerText().catch(() => 'no main')
console.log('[debug] H1:', JSON.stringify(h1))
console.log('[debug] Body excerpt:', bodyText.substring(0, 800))
await page.screenshot({ path: 'quality/evidence/web-react-final/debug-tool-approvals.png', fullPage: true })
await browser.close()
