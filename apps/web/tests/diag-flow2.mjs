import { chromium } from '@playwright/test'
const browser = await chromium.launch({ headless: true, executablePath: '/usr/bin/chromium', args: ['--no-sandbox','--disable-dev-shm-usage','--disable-gpu'] })
const page = await (await browser.newContext({ viewport: { width: 1440, height: 1000 } })).newPage()
let token = null
page.on('response', async r => { const u=r.url(); if((u.includes('/auth/login')||u.includes('/auth/refresh'))&&r.ok()){ try{const b=await r.json(); if(b.access_token) token=b.access_token}catch{}}})
page.on('pageerror', e=>console.log('pageerror',e.message))
await page.addInitScript(() => window.localStorage.setItem('workama.locale', 'en-US'))
await page.route('http://localhost:20200/**', route=>{const o=route.request().url(); if(o.includes('/auth/refresh')&&token) return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({access_token:token})}); return route.continue({url:o.replace('http://localhost:20200','http://platform-api:8000')})})
await page.routeWebSocket('ws://localhost:20201/**', ws=>ws.connectToServer(ws.url().replace('ws://localhost:20201','ws://agent-server:8001')))
await page.goto('http://localhost:3000/login',{waitUntil:'networkidle'})
await page.locator('#email').fill('tester@workama.example.com')
await page.locator('#password').fill('WorkAMA-Test-2026!')
await page.locator('button[type="submit"]').click()
await page.waitForURL(/chat|onboarding|admin/)

// 模拟 smoke 从 audit 一直到 studio apps
async function step(name, fn) {
  try { await fn(); console.log('OK', name) }
  catch (e) { console.log('FAIL', name, '|', e.message.slice(0, 250)); throw e }
}

await page.goto('http://localhost:3000/admin/audit',{waitUntil:'networkidle'})
await step('audit H1', () => page.getByRole('heading', { name: /Audit & evidence|审计与证据/ }).first().waitFor({timeout: 10000}))
await step('audit ledger', () => page.getByRole('heading', { name: /Audit ledger|审计账本/ }).waitFor({timeout: 10000}))
await step('export history', () => page.getByRole('heading', { name: 'Export history', exact: true }).waitFor({timeout: 10000}))
await step('zh audit', () => page.locator('button.locale-toggle').click().then(async () => {
  await page.getByRole('heading', { name: '审计与证据', exact: true }).waitFor({timeout: 10000})
  await page.getByRole('heading', { name: '审计账本', exact: true }).waitFor({timeout: 10000})
}))
await page.locator('button.locale-toggle').click()
await step('en audit again', () => page.getByRole('heading', { name: /Audit & evidence|审计与证据/ }).first().waitFor({timeout: 10000}))

await page.goto('http://localhost:3000/admin/observability',{waitUntil:'networkidle'})
await step('observability', () => page.getByRole('heading', { name: 'Observability', exact: true }).waitFor({timeout: 10000}))
await page.locator('button.locale-toggle').click()
await page.getByRole('heading', { name: '可观测性', exact: true }).waitFor({timeout: 10000})
await step('zh observability', () => page.getByRole('heading', { name: 'SLO 与错误预算', exact: true }).waitFor({timeout: 10000}))
await page.locator('button.locale-toggle').click()

await page.goto('http://localhost:3000/admin/notifications',{waitUntil:'networkidle'})
await step('notifications', () => page.getByRole('heading', { name: /Notifications|通知/ }).first().waitFor({timeout: 10000}))
console.log('Reached workflow start')
await browser.close()
