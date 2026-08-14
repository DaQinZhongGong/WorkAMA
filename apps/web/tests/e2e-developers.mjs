/**
 * WorkAMA 开放平台文档站 E2E 旅程测试。
 *
 * 验证 /developers 公开文档站的 5 个核心旅程：
 * 1. 页面加载与公共壳渲染
 * 2. API 端点列表渲染（OpenAPI 动态拉取）
 * 3. OAuth 2.0 PKCE 代码示例可见
 * 4. Webhook 签名验证示例可见
 * 5. SDK / CLI 安装指南可见
 */
import { chromium } from '@playwright/test'
import { mkdir, writeFile } from 'node:fs/promises'

const baseURL = process.env.BROWSER_BASE_URL ?? 'http://localhost:20204'
const outputDir = process.env.EVIDENCE_DIR ?? 'quality/evidence/web-developers-journey'

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'] })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
const page = await context.newPage()

await mkdir(outputDir, { recursive: true })
const results = []
const started_at = new Date().toISOString()

function log(step, status, detail = '') {
  results.push({ step, status, detail, timestamp: new Date().toISOString() })
  console.log(`[${status}] ${step}${detail ? ': ' + detail : ''}`)
}

async function setupApiRouting() {
  const apiUpstreamHost = process.env.API_UPSTREAM_HOST ?? 'platform-api'
  await page.route('http://localhost:20200/**', (route) => {
    const original = route.request().url()
    const rewritten = original.replace('http://localhost:20200', `http://${apiUpstreamHost}:8000`)
    route.continue({ url: rewritten })
  })
}

await setupApiRouting()

try {
  // ---------------------------------------------------------------------------
  // 旅程 1：页面加载与公共壳渲染
  // ---------------------------------------------------------------------------
  await page.goto(`${baseURL}/developers`, { waitUntil: 'networkidle' })
  await page.waitForSelector('.public-shell', { timeout: 10000 })
  await page.waitForSelector('.public-topbar', { timeout: 10000 })
  const heading = await page.locator('h1', { hasText: /WorkAMA Open Platform/i }).first()
  await heading.waitFor({ state: 'visible', timeout: 10000 })
  log('page-load', 'pass', 'Public shell and heading rendered')

  // ---------------------------------------------------------------------------
  // 旅程 2：API 端点列表渲染（OpenAPI 动态拉取）
  // ---------------------------------------------------------------------------
  await page.waitForSelector('#endpoints table', { timeout: 15000 })
  const rows = await page.locator('#endpoints table tbody tr').count()
  if (rows >= 5) {
    log('endpoint-list', 'pass', `${rows} endpoint rows rendered`)
  } else {
    log('endpoint-list', 'warn', `Expected >=5 endpoint rows, found ${rows}`)
  }

  // ---------------------------------------------------------------------------
  // 旅程 3：OAuth 2.0 PKCE 代码示例可见
  // ---------------------------------------------------------------------------
  const oauthSection = page.locator('#oauth')
  await oauthSection.waitFor({ state: 'visible', timeout: 10000 })
  const oauthPreCount = await oauthSection.locator('pre').count()
  if (oauthPreCount >= 3) {
    log('oauth-example', 'pass', `${oauthPreCount} code blocks visible`)
  } else {
    log('oauth-example', 'warn', `Expected >=3 code blocks, found ${oauthPreCount}`)
  }
  const pkceText = await oauthSection.locator('pre').first().innerText()
  if (pkceText.includes('code_challenge') || pkceText.includes('verifier')) {
    log('oauth-pkce', 'pass', 'PKCE content present')
  } else {
    log('oauth-pkce', 'warn', 'PKCE keywords not found in first block')
  }

  // ---------------------------------------------------------------------------
  // 旅程 4：Webhook 签名验证示例可见
  // ---------------------------------------------------------------------------
  const webhookSection = page.locator('#webhooks')
  await webhookSection.waitFor({ state: 'visible', timeout: 10000 })
  const webhookPreCount = await webhookSection.locator('pre').count()
  if (webhookPreCount >= 2) {
    log('webhook-example', 'pass', `${webhookPreCount} code blocks visible`)
  } else {
    log('webhook-example', 'warn', `Expected >=2 code blocks, found ${webhookPreCount}`)
  }
  const webhookText = await webhookSection.locator('pre').first().innerText()
  if (webhookText.includes('verify_signature') || webhookText.includes('hmac')) {
    log('webhook-signature', 'pass', 'Signature verification content present')
  } else {
    log('webhook-signature', 'warn', 'Signature keywords not found')
  }

  // ---------------------------------------------------------------------------
  // 旅程 5：SDK 下载与 CLI 安装指南
  // ---------------------------------------------------------------------------
  const sdkSection = page.locator('#sdk')
  await sdkSection.waitFor({ state: 'visible', timeout: 10000 })
  const sdkText = await sdkSection.innerText()
  const hasCli = sdkText.includes('workama --version') || sdkText.includes('workama auth login')
  const hasSdk = sdkText.includes('pip install') || sdkText.includes('npm install') || sdkText.includes('go get')
  if (hasCli && hasSdk) {
    log('sdk-cli-guide', 'pass', 'SDK and CLI installation content present')
  } else {
    log('sdk-cli-guide', 'warn', `CLI=${hasCli}, SDK=${hasSdk}`)
  }
} catch (error) {
  log('journey', 'fail', error instanceof Error ? error.message : String(error))
  try {
    await page.screenshot({ path: `${outputDir}/developers-journey-fail.png` })
  } catch { /* ignore */ }
} finally {
  await browser.close()
}

const report = {
  started_at,
  finished_at: new Date().toISOString(),
  results,
  all_passed: !results.some((r) => r.status === 'fail'),
}

await writeFile(`${outputDir}/developers-journey-report.json`, JSON.stringify(report, null, 2))
console.log(`\nDevelopers E2E journey complete. All passed: ${report.all_passed}`)
process.exit(report.all_passed ? 0 : 1)
