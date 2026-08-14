import { chromium } from '@playwright/test'
const browser = await chromium.launch({ headless: true, executablePath: '/usr/bin/chromium', args: ['--no-sandbox','--disable-dev-shm-usage','--disable-gpu'] })
const page = await (await browser.newContext({ viewport: { width: 1440, height: 1000 } })).newPage()
let token = null
page.on('response', async r => { const u=r.url(); if((u.includes('/auth/login')||u.includes('/auth/refresh'))&&r.ok()){ try{const b=await r.json(); if(b.access_token) token=b.access_token}catch{}}})
page.on('pageerror', e=>console.log('pageerror',e.message))
page.on('console', m=>{ if(m.type()==='error') console.log('console.error',m.text())})
page.on('response', r=>{ if(r.status()>=500) console.log('HTTP-500', r.url(), r.status())})
await page.addInitScript(() => window.localStorage.setItem('workama.locale', 'en-US'))
await page.route('http://localhost:20200/**', route=>{const o=route.request().url(); if(o.includes('/auth/refresh')&&token) return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({access_token:token})}); return route.continue({url:o.replace('http://localhost:20200','http://platform-api:8000')})})
await page.routeWebSocket('ws://localhost:20201/**', ws=>ws.connectToServer(ws.url().replace('ws://localhost:20201','ws://agent-server:8001')))
await page.goto('http://localhost:3000/login',{waitUntil:'networkidle'})
await page.locator('#email').fill('tester@workama.example.com')
await page.locator('#password').fill('WorkAMA-Test-2026!')
await page.locator('button[type="submit"]').click()
await page.waitForURL(/\/chat|\/onboarding|\/admin/)

// 模拟 smoke 跑过的步骤：privacy -> devices -> enterprise-identity -> compliance (zh+en) -> audit
for (const path of ['/admin/privacy', '/admin/devices', '/admin/enterprise-identity', '/admin/compliance']) {
  await page.goto(`http://localhost:3000${path}`,{waitUntil:'networkidle'})
  console.log('VISITED', path)
}
// compliance locale toggle zh->en
await page.locator('button.locale-toggle').click()
await page.waitForTimeout(1000)
await page.locator('button.locale-toggle').click()
await page.waitForTimeout(1000)

// 现在去 audit
await page.goto('http://localhost:3000/admin/audit',{waitUntil:'networkidle'})
await page.waitForTimeout(5000)
console.log('audit url=', page.url())
console.log('audit h1=', await page.locator('h1').allInnerTexts())
console.log('audit locale=', await page.evaluate(() => window.localStorage.getItem('workama.locale')))
console.log('audit body excerpt=', (await page.locator('body').innerText()).slice(0, 1500))
await page.screenshot({path:'/tmp/audit-diag.png', fullPage:false})
await browser.close()
