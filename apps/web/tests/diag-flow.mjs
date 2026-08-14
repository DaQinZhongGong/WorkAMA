import { chromium } from '@playwright/test'
const browser = await chromium.launch({ headless: true, executablePath: '/usr/bin/chromium', args: ['--no-sandbox','--disable-dev-shm-usage','--disable-gpu'] })
const page = await (await browser.newContext({ viewport: { width: 1440, height: 1000 } })).newPage()
let token = null
page.on('response', async r => { const u=r.url(); if((u.includes('/auth/login')||u.includes('/auth/refresh'))&&r.ok()){ try{const b=await r.json(); if(b.access_token) token=b.access_token}catch{}}})
page.on('pageerror', e=>console.log('pageerror',e.message))
page.on('console', m=>{ if(m.type()==='error' && !m.text().includes('Failed to load resource')) console.log('console.error',m.text())})
await page.addInitScript(() => window.localStorage.setItem('workama.locale', 'en-US'))
await page.route('http://localhost:20200/**', route=>{const o=route.request().url(); if(o.includes('/auth/refresh')&&token) return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({access_token:token})}); return route.continue({url:o.replace('http://localhost:20200','http://platform-api:8000')})})
await page.routeWebSocket('ws://localhost:20201/**', ws=>ws.connectToServer(ws.url().replace('ws://localhost:20201','ws://agent-server:8001')))
await page.goto('http://localhost:3000/login',{waitUntil:'networkidle'})
await page.locator('#email').fill('tester@workama.example.com')
await page.locator('#password').fill('WorkAMA-Test-2026!')
await page.locator('button[type="submit"]').click()
await page.waitForURL(/chat|onboarding|admin/)

// 一步一步模拟 smoke 到 419 行
await page.goto('http://localhost:3000/admin/compliance',{waitUntil:'networkidle'})
await page.getByRole('heading', { name: 'Compliance center', exact: true }).waitFor({timeout: 10000})
console.log('compliance OK')

await page.locator('button.locale-toggle').click()
await page.getByRole('heading', { name: '合规中心', exact: true }).waitFor({timeout: 10000})
console.log('compliance zh OK')

await page.locator('button.locale-toggle').click()
await page.getByRole('heading', { name: 'Compliance center', exact: true }).waitFor({timeout: 10000})
console.log('compliance en again OK')

await page.goto('http://localhost:3000/admin/audit',{waitUntil:'networkidle'})
try {
  await page.getByRole('heading', { name: /Audit & evidence|审计与证据/ }).first().waitFor({timeout: 15000})
  console.log('audit H1 OK')
} catch (e) {
  console.log('audit H1 FAIL url=', page.url())
  console.log('audit h1=', await page.locator('h1').allInnerTexts())
  console.log('audit locale=', await page.evaluate(() => window.localStorage.getItem('workama.locale')))
  console.log('body-excerpt=', (await page.locator('body').innerText()).slice(0,500))
}
await browser.close()
