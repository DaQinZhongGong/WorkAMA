import { chromium } from '@playwright/test'

const baseURL = process.env.BROWSER_BASE_URL ?? 'http://web:3000'
const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.BROWSER_EXECUTABLE || '/usr/bin/chromium-browser',
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
})
const context = await browser.newContext()
const page = await context.newPage()
await page.addInitScript(() => window.localStorage.setItem('workama.locale', 'en-US'))
page.on('pageerror', (error) => console.log('pageerror:', error.message))
page.on('console', (message) => console.log('console:', message.type(), message.text()))
page.on('response', (response) => console.log('response:', response.status(), response.url()))
await page.setViewportSize({ width: 1440, height: 1000 })
await page.goto(`${baseURL}/`, { waitUntil: 'networkidle' })
await page.waitForTimeout(2000)
console.log('url:', page.url())
console.log('html lang:', await page.locator('html').getAttribute('lang'))
console.log('text:', await page.locator('body').innerText())
console.log('headings:', await page.locator('h1').allInnerTexts())
await page.screenshot({ path: '/tmp/landing-debug.png', fullPage: true })
await browser.close()
