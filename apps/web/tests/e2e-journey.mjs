/**
 * WorkAMA 端到端业务旅程测试。
 *
 * 通过 Playwright 驱动 chromium 浏览器，覆盖真实用户在 Web 控制台中的
 * 核心业务流程：登录 → Chat → 导航 → 创建会话 → 错误处理 → 响应式。
 * 每个旅程都通过浏览器 UI 操作触发，并对 URL/文本/元素可见性做断言。
 * 测试结果以 JSON evidence 形式输出到 quality/evidence/web-react-final/。
 */
import { chromium } from '@playwright/test'
import { mkdir, stat, writeFile } from 'node:fs/promises'

// 运行配置（与 browser-smoke.mjs 保持一致，便于在容器内复用环境变量）
const baseURL = process.env.BROWSER_BASE_URL ?? 'http://localhost:20204'
const outputDir = process.env.EVIDENCE_DIR ?? 'quality/evidence/web-react-final'
const email = process.env.WORKAMA_BROWSER_EMAIL ?? 'tester@workama.example.com'
const password = process.env.WORKAMA_BROWSER_PASSWORD ?? 'WorkAMA-Test-2026!'
// 容器内可能使用系统 chromium（如 Alpine apk 安装），通过环境变量指定可执行路径；
// 未指定时自动探测容器内常见的 chromium-browser 路径。
const executablePath = await (async () => {
  if (process.env.BROWSER_EXECUTABLE) return process.env.BROWSER_EXECUTABLE
  try {
    await stat('/usr/bin/chromium-browser')
    return '/usr/bin/chromium-browser'
  } catch { /* ignore */ }
  return undefined
})()
// 容器内 API 重定向：web 应用构建时注入了 http://localhost:20200 作为平台 API 地址，
// 但容器内 localhost:20200 不可达。通过环境变量 API_UPSTREAM_HOST 指定可达的上游地址
// （默认 platform-api，Docker Compose 服务名）。仅对 HTTP 请求生效，WS 连接容错。
const apiUpstreamHost = process.env.API_UPSTREAM_HOST ?? 'platform-api'

await mkdir(outputDir, { recursive: true })

// 浏览器启动：headless 模式，关闭沙箱以兼容容器环境
const browser = await chromium.launch({
  headless: true,
  ...(executablePath ? { executablePath } : {}),
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
})

// 旅程结果收集
const journeys = []
const started_at = new Date().toISOString()
const suiteStart = Date.now()

/**
 * 设置 API 请求重定向：将 http://localhost:20200/** 的请求转发到容器内可达的上游地址。
 * 这解决了容器内浏览器无法通过 localhost 访问 platform-api 的问题。
 *
 * 额外处理 auth/refresh：page.goto() 全量刷新页面时，AuthProvider 会调用
 * /api/v1/auth/refresh 获取新 token。由于 cookie 跨域重定向不跟随，
 * refresh 请求会失败导致登出。这里从 login/refresh 响应中缓存 access_token，
 * 对后续 refresh 请求直接用缓存 token 应答，保证全量刷新后仍保持登录态。
 */
async function setupApiRouting(page) {
  // 闭包内缓存 access_token，在 login/refresh 响应到达时填充
  let cachedAccessToken = null

  // 监听响应，从 login/refresh 成功响应中提取 access_token
  page.on('response', async (response) => {
    const url = response.url()
    if ((url.includes('/api/v1/auth/login') || url.includes('/api/v1/auth/refresh')) && response.ok()) {
      try {
        const body = await response.json()
        if (body.access_token) cachedAccessToken = body.access_token
      } catch { /* 响应体解析失败时忽略，不影响主流程 */ }
    }
  })

  await page.route('http://localhost:20200/**', (route) => {
    const original = route.request().url()
    // 对 refresh 请求，如有缓存 token 则直接应答，避免 cookie 缺失导致登出
    if (original.includes('/api/v1/auth/refresh') && cachedAccessToken) {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ access_token: cachedAccessToken }),
      })
      return
    }
    // 其他请求重定向到容器内可达的上游地址
    const rewritten = original.replace('http://localhost:20200', `http://${apiUpstreamHost}:8000`)
    route.continue({ url: rewritten })
  })
}

/**
 * 运行单个业务旅程，捕获异常并截图留证。
 * 每个旅程使用独立的浏览器上下文，确保旅程间状态隔离。
 */
async function runJourney(name, fn) {
  const start = Date.now()
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  const page = await context.newPage()
  // 锁定英文 locale，使断言文本稳定
  await page.addInitScript(() => window.localStorage.setItem('workama.locale', 'en-US'))
  // 设置 API 请求重定向（容器内 localhost:20200 不可达）
  await setupApiRouting(page)
  try {
    await fn(page)
    const duration_ms = Date.now() - start
    journeys.push({ name, status: 'passed', duration_ms })
  } catch (error) {
    const duration_ms = Date.now() - start
    // 失败时截图作为证据
    const screenshot = `${outputDir}/e2e-failure-${name.replace(/[^a-zA-Z0-9-]/g, '-')}.png`
    await page.screenshot({ path: screenshot, fullPage: true }).catch(() => undefined)
    journeys.push({ name, status: 'failed', duration_ms, error: error.message, screenshot })
  } finally {
    await context.close()
  }
}

/**
 * 辅助函数：执行登录流程并停留在 /chat。
 * 处理 onboarding 中间态，确保最终进入工作区主界面。
 */
async function login(page) {
  await page.goto(`${baseURL}/login`, { waitUntil: 'networkidle' })
  await page.locator('#email').fill(email)
  await page.locator('#password').fill(password)
  await page.locator('button[type="submit"]').click()
  await page.waitForURL(/\/chat|\/onboarding|\/admin/, { timeout: 20000 })
  // 如果进入 onboarding 引导，直接进入工作区
  if (page.url().includes('/onboarding')) {
    await page.getByRole('button', { name: /Enter workspace/ }).click()
    await page.waitForURL(/\/chat/, { timeout: 15000 })
  }
}

// ---------------------------------------------------------------------------
// 旅程 a：登录旅程
// 访问 /login → 填写邮箱密码 → 提交 → 验证跳转到 /chat
// ---------------------------------------------------------------------------
await runJourney('login', async (page) => {
  await page.goto(`${baseURL}/login`, { waitUntil: 'networkidle' })
  // 验证登录页核心元素可见
  await page.locator('#email').waitFor({ state: 'visible' })
  await page.locator('#password').waitFor({ state: 'visible' })
  await page.locator('button[type="submit"]').waitFor({ state: 'visible' })
  // 填写表单并提交
  await page.locator('#email').fill(email)
  await page.locator('#password').fill(password)
  await page.locator('button[type="submit"]').click()
  // 验证跳转到 /chat（或 /onboarding 中间态）
  await page.waitForURL(/\/chat|\/onboarding|\/admin/, { timeout: 20000 })
  if (page.url().includes('/onboarding')) {
    await page.getByRole('button', { name: /Enter workspace/ }).click()
    await page.waitForURL(/\/chat/, { timeout: 15000 })
  }
  // 最终断言：URL 必须包含 /chat
  if (!page.url().includes('/chat')) {
    throw new Error(`登录后期望跳转到 /chat，实际为 ${page.url()}`)
  }
})

// ---------------------------------------------------------------------------
// 旅程 b：Chat 旅程
// 登录后验证 /chat 页面加载、KPI 卡片渲染、会话列表加载
// ---------------------------------------------------------------------------
await runJourney('chat', async (page) => {
  await login(page)
  await page.waitForURL(/\/chat/)
  // 验证页面主标题渲染
  await page.getByRole('heading', { name: 'Your command center', exact: true }).waitFor({ timeout: 20000 })
  // 验证 KPI 卡片渲染（command center 有 4 张 KPI 卡片）
  await page.locator('.kpi-grid .kpi').first().waitFor({ state: 'visible', timeout: 15000 })
  const kpiCount = await page.locator('.kpi-grid .kpi').count()
  if (kpiCount < 4) {
    throw new Error(`KPI 卡片数量不足，期望至少 4 张，实际 ${kpiCount} 张`)
  }
  // 验证工作区动态面板加载（代表会话/数据列表区域）
  await page.getByText('Workspace pulse', { exact: true }).waitFor({ state: 'visible', timeout: 15000 })
  // 验证提示词入口区域加载
  await page.getByText('Start with a prompt', { exact: true }).waitFor({ state: 'visible', timeout: 15000 })
})

// ---------------------------------------------------------------------------
// 旅程 c：导航旅程
// 登录后点击侧边栏 Agents/Work plans/Knowledge 导航项，验证页面标题/内容渲染
// 同时访问 Tool approvals 页面验证审批队列渲染
// ---------------------------------------------------------------------------
await runJourney('navigation', async (page) => {
  await login(page)
  await page.waitForURL(/\/chat/)
  await page.getByRole('heading', { name: 'Your command center', exact: true }).waitFor()

  // 点击侧边栏 Agents 导航项
  await page.locator('aside.sidebar').getByRole('link', { name: 'Agents', exact: true }).first().click()
  await page.waitForURL(/\/agents/, { timeout: 15000 })
  await page.getByRole('heading', { name: 'Agents', exact: true }).waitFor({ timeout: 15000 })

  // 点击 Work plans 导航项
  await page.locator('aside.sidebar').getByRole('link', { name: 'Work plans', exact: true }).first().click()
  await page.waitForURL(/\/work/, { timeout: 15000 })
  await page.getByRole('heading', { name: 'Work plans', exact: true }).waitFor({ timeout: 15000 })

  // 点击 Knowledge 导航项
  await page.locator('aside.sidebar').getByRole('link', { name: 'Knowledge', exact: true }).first().click()
  await page.waitForURL(/\/knowledge/, { timeout: 15000 })
  await page.getByRole('heading', { name: 'Knowledge', exact: true }).waitFor({ timeout: 15000 })

  // 访问工具审批页面，验证审批队列渲染（覆盖审批业务入口）
  await page.goto(`${baseURL}/admin/tool-approvals`, { waitUntil: 'networkidle' })
  await page.getByRole('heading', { name: 'Tool approvals', exact: true }).waitFor({ timeout: 15000 })
  // 验证审批队列面板渲染
  await page.getByText('Approval queue', { exact: true }).waitFor({ state: 'visible', timeout: 15000 })
})

// ---------------------------------------------------------------------------
// 旅程 d：创建会话旅程
// 登录后点击 "New conversation" 按钮，验证跳转到会话详情页
// ---------------------------------------------------------------------------
await runJourney('create-session', async (page) => {
  await login(page)
  await page.waitForURL(/\/chat/)
  await page.getByRole('heading', { name: 'Your command center', exact: true }).waitFor()
  // 点击 "New conversation" 按钮触发会话创建（使用 exact 匹配避免与同名会话卡片冲突）
  const newButton = page.getByRole('button', { name: 'New conversation', exact: true })
  await newButton.waitFor({ state: 'visible' })
  await newButton.click()
  // 验证跳转到会话详情页 /chat/<sessionId>
  await page.waitForURL(/\/chat\/[^/]+/, { timeout: 20000 })
  // 验证会话详情页核心容器加载
  await page.locator('.chat-workspace').waitFor({ state: 'visible', timeout: 20000 })
  // 验证 URL 中包含会话 ID（非空）
  const match = page.url().match(/\/chat\/([^/?]+)/)
  if (!match || !match[1]) {
    throw new Error(`会话详情页 URL 缺少会话 ID：${page.url()}`)
  }
})

// ---------------------------------------------------------------------------
// 旅程 e：错误处理旅程
// 未登录访问受保护页面，验证跳转到 /login 并携带 redirect 参数
// ---------------------------------------------------------------------------
await runJourney('error-handling', async (page) => {
  // 未登录访问 /chat
  await page.goto(`${baseURL}/chat`, { waitUntil: 'networkidle' })
  await page.waitForURL(/\/login/, { timeout: 15000 })
  if (!page.url().includes('/login')) {
    throw new Error(`未登录访问 /chat 期望跳转到 /login，实际为 ${page.url()}`)
  }
  // 验证 redirect 参数携带原始路径
  const chatRedirect = new URL(page.url()).searchParams.get('redirect')
  if (!chatRedirect || !chatRedirect.includes('/chat')) {
    throw new Error(`期望 redirect 参数包含 /chat，实际为 ${chatRedirect}`)
  }
  // 验证登录页可见（确保是真正的登录页而非空白跳转）
  await page.locator('#email').waitFor({ state: 'visible' })

  // 未登录访问 /agents，同样验证跳转
  await page.goto(`${baseURL}/agents`, { waitUntil: 'networkidle' })
  await page.waitForURL(/\/login/, { timeout: 15000 })
  const agentsRedirect = new URL(page.url()).searchParams.get('redirect')
  if (!agentsRedirect || !agentsRedirect.includes('/agents')) {
    throw new Error(`期望 redirect 参数包含 /agents，实际为 ${agentsRedirect}`)
  }
})

// ---------------------------------------------------------------------------
// 旅程 f：响应式旅程
// 在 390px 移动视口下验证登录页和 chat 页可正常渲染，无水平溢出
// ---------------------------------------------------------------------------
await runJourney('responsive', async (page) => {
  // 设置移动端视口（iPhone 12 Pro 尺寸）
  await page.setViewportSize({ width: 390, height: 844 })

  // 验证移动端登录页渲染
  await page.goto(`${baseURL}/login`, { waitUntil: 'networkidle' })
  await page.locator('#email').waitFor({ state: 'visible' })
  await page.locator('#password').waitFor({ state: 'visible' })
  await page.locator('button[type="submit"]').waitFor({ state: 'visible' })
  const loginOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  if (loginOverflow > 1) {
    throw new Error(`移动端登录页水平溢出 ${loginOverflow}px`)
  }
  await page.screenshot({ path: `${outputDir}/e2e-responsive-login-mobile.png`, fullPage: true })

  // 移动端登录后验证 chat 页渲染
  await page.locator('#email').fill(email)
  await page.locator('#password').fill(password)
  await page.locator('button[type="submit"]').click()
  await page.waitForURL(/\/chat|\/onboarding|\/admin/, { timeout: 20000 })
  if (page.url().includes('/onboarding')) {
    await page.getByRole('button', { name: /Enter workspace/ }).click()
    await page.waitForURL(/\/chat/, { timeout: 15000 })
  }
  await page.getByRole('heading', { name: 'Your command center', exact: true }).waitFor({ timeout: 20000 })
  const chatOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  if (chatOverflow > 1) {
    throw new Error(`移动端 chat 页水平溢出 ${chatOverflow}px`)
  }
  await page.screenshot({ path: `${outputDir}/e2e-responsive-chat-mobile.png`, fullPage: true })
})

await browser.close()

// 汇总并输出 evidence JSON
const finished_at = new Date().toISOString()
const duration_ms = Date.now() - suiteStart
const passed = journeys.filter((j) => j.status === 'passed').length
const failed = journeys.filter((j) => j.status === 'failed').length

const evidence = {
  suite: 'web-react-final-e2e-journey',
  started_at,
  finished_at,
  duration_ms,
  journeys,
  summary: { total: journeys.length, passed, failed },
}

await writeFile(`${outputDir}/e2e-journey.json`, JSON.stringify(evidence, null, 2))
process.stdout.write(`${JSON.stringify(evidence)}\n`)

if (failed > 0) process.exit(1)
