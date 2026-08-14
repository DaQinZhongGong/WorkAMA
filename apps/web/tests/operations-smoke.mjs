import { chromium } from '@playwright/test'
import { mkdir, writeFile } from 'node:fs/promises'

const outputDir = process.env.EVIDENCE_DIR ?? 'quality/evidence/operations-react-final'
const baseURL = process.env.BROWSER_BASE_URL ?? 'http://localhost:20204'
await mkdir(outputDir, { recursive: true })
const errors = []
const browser = await chromium.launch({ headless: true, ...(process.env.BROWSER_EXECUTABLE ? { executablePath: process.env.BROWSER_EXECUTABLE, args: ['--no-sandbox', '--disable-dev-shm-usage'] } : {}) })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
const page = await context.newPage()
page.on('pageerror', (error) => errors.push(`pageerror: ${error.message}`))
page.on('console', (message) => { if (message.type() === 'error' && !message.text().includes('Failed to load resource')) errors.push(`console: ${message.text()}`) })

await page.goto(`${baseURL}/login`, { waitUntil: 'networkidle' })
await page.locator('#email').fill(process.env.WORKAMA_BROWSER_EMAIL ?? 'tester@workama.example.com')
await page.locator('#password').fill(process.env.WORKAMA_BROWSER_PASSWORD ?? 'WorkAMA-Test-2026!')
await page.locator('button[type="submit"]').click()
await page.waitForURL(/\/chat|\/onboarding|\/admin/)
if (page.url().includes('/onboarding')) { await page.getByRole('button', { name: /Enter workspace/ }).click(); await page.waitForURL(/\/chat/) }
await page.goto(`${baseURL}/admin/operations`, { waitUntil: 'networkidle' })
await page.getByRole('heading', { name: 'Operations', exact: true }).waitFor()
await page.getByRole('heading', { name: 'Async operations', exact: true }).waitFor()
await page.getByRole('tab', { name: 'Jobs & DLQ', exact: true }).click()
await page.getByRole('heading', { name: 'Jobs', exact: true }).waitFor()
await page.waitForFunction(() => {
  const text = document.querySelector('.ops-dlq-panel')?.textContent ?? ''
  return /No dead letters|Replay|Replayed/.test(text)
})
const deadLetterText = await page.locator('.ops-dlq-panel').innerText()
if (!/No dead letters|Replay|Replayed/.test(deadLetterText)) errors.push('dead-letter panel did not render an empty or replayable state')
await page.getByRole('tab', { name: 'overview', exact: true }).click()
await page.getByRole('heading', { name: 'Async operations', exact: true }).waitFor()
await page.getByRole('button', { name: 'Refresh' }).click()
await page.screenshot({ path: `${outputDir}/operations-desktop.png`, fullPage: true })

await page.setViewportSize({ width: 390, height: 844 })
await page.reload({ waitUntil: 'networkidle' })
const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
if (overflow > 1) errors.push(`mobile horizontal overflow: ${overflow}px`)
await page.screenshot({ path: `${outputDir}/operations-mobile.png`, fullPage: true })

const result = { ok: errors.length === 0, route: '/admin/operations', viewports: ['1440x1000', '390x844'], errors }
await writeFile(`${outputDir}/operations-smoke.json`, JSON.stringify(result, null, 2))
await context.close()
await browser.close()
process.stdout.write(`${JSON.stringify(result)}\n`)
if (!result.ok) process.exit(1)
