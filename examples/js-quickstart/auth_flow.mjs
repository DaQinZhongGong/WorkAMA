/**
 * WorkAMA 认证流程完整示例（P2 第三方集成，JavaScript 版）。
 *
 * 本示例演示完整的认证生命周期，覆盖四种场景：
 *
 * 1. 账号密码登录 → 拿到 access_token（cookie 由本地手动管理）
 * 2. access_token 刷新 → 调用 /api/v1/auth/refresh 续期
 * 3. 登出 → 调用 /api/v1/auth/logout 撤销会话
 * 4. OAuth 授权码流程客户端模拟 → /api/v1/auth/oauth/{provider}/authorize
 * 5. API Key 与 Access Token 两种认证方式对比
 *
 * 为避免引入第三方依赖，本示例仅使用 Node 18+ 内置的 fetch + crypto + URL。
 *
 * 运行方式：
 *   cd examples/js-quickstart
 *   node auth_flow.mjs
 *
 * 环境变量：
 *   WORKAMA_BASE_URL        平台 API 基地址，默认 http://localhost:20200
 *   WORKAMA_EMAIL           登录邮箱（默认 tester@workama.example.com）
 *   WORKAMA_PASSWORD        登录密码（默认 WorkAMA-Test-2026!）
 *   WORKAMA_API_KEY         可选，演示 API Key 认证方式
 *   WORKAMA_OAUTH_PROVIDER  可选，OAuth 提供商，默认 github
 */

import { WorkAMAClient, WorkAMAError } from '../../packages/sdk-js/src/index.ts'

const BASE_URL = process.env.WORKAMA_BASE_URL || 'http://localhost:20200'
const EMAIL = process.env.WORKAMA_EMAIL || 'tester@workama.example.com'
const PASSWORD = process.env.WORKAMA_PASSWORD || 'WorkAMA-Test-2026!'
const API_KEY = process.env.WORKAMA_API_KEY
const OAUTH_PROVIDER = process.env.WORKAMA_OAUTH_PROVIDER || 'github'

// ---------------------------------------------------------------------------
// 工具函数：基于 fetch 的原始 HTTP 调用（用于认证端点，绕过 SDK）
// ---------------------------------------------------------------------------

/**
 * 发起一次 JSON 请求并返回 { status, body }。
 * @param {string} url
 * @param {object} [payload]
 * @param {object} [extraHeaders]
 * @param {string} [method]
 */
async function postJson(url, payload = null, extraHeaders = null, method = 'POST') {
  const headers = {
    'Content-Type': 'application/json',
    Accept: 'application/json',
    'User-Agent': 'workama-example-auth/0.1.0',
    ...(extraHeaders || {}),
  }
  const init = { method, headers }
  if (payload !== null) {
    init.body = JSON.stringify(payload)
  }
  let resp
  try {
    resp = await fetch(url, init)
  } catch (err) {
    return { status: 0, body: { error: `network error: ${err.message}` } }
  }
  const text = await resp.text()
  let body
  if (text) {
    try {
      body = JSON.parse(text)
    } catch {
      body = text
    }
  } else {
    body = {}
  }
  return { status: resp.status, body }
}

function brief(body, limit = 120) {
  const text = typeof body === 'string' ? body : JSON.stringify(body)
  return text.length <= limit ? text : text.slice(0, limit) + '...(已截断)'
}

// ---------------------------------------------------------------------------
// 场景 1/2/3：账号密码登录 → 刷新 → 登出
// ---------------------------------------------------------------------------

/**
 * 演示账号密码登录、token 刷新、登出的完整流程。
 *
 * 平台登录返回 { access_token, token_type, user }，
 * 同时通过 Set-Cookie 下发 workama_refresh / workama_access。
 * Node fetch 默认不会自动管理 cookie，这里仅演示 token 流程。
 *
 * @returns {Promise<{access_token?: string, user?: object} | null>}
 */
async function passwordLoginFlow(baseUrl, email, password) {
  console.log('\n=== 1. 账号密码登录 ===')
  const { status, body } = await postJson(
    `${baseUrl}/api/v1/auth/login`,
    { email, password },
  )
  console.log(`login status=${status}`)
  if (status !== 200 || typeof body !== 'object') {
    console.log(`[WARN] 登录未成功：${brief(body)}（账号可能未校验邮箱或被锁定）`)
    return null
  }

  const accessToken = body.access_token
  const user = body.user || {}
  console.log(`  user=${user.email} workspace_id=${user.workspace_id}`)
  console.log(`  access_token=${String(accessToken || '').slice(0, 24)}...(已截断)`)

  // 使用 access_token 验证会话有效性
  const meResp = await postJson(
    `${baseUrl}/api/v1/auth/me`,
    null,
    { Authorization: `Bearer ${accessToken}` },
    'GET',
  )
  console.log(`  /auth/me status=${meResp.status} body=${brief(meResp.body)}`)

  console.log('\n=== 2. Token 刷新 ===')
  // 注意：真实环境需带上 workama_refresh cookie；此处仅演示端点调用
  const refreshResp = await postJson(
    `${baseUrl}/api/v1/auth/refresh`,
    null,
    { Authorization: `Bearer ${accessToken}` },
  )
  console.log(`refresh status=${refreshResp.status}`)
  if (refreshResp.status === 200 && typeof refreshResp.body === 'object') {
    const newToken = refreshResp.body.access_token
    console.log(`  新 access_token=${String(newToken || '').slice(0, 24)}...(已截断)`)
    console.log('  旧 refresh token 已被旋转作废（reuse detection 机制）')
  } else {
    console.log(`  [WARN] 刷新失败：${brief(refreshResp.body)}`)
  }

  console.log('\n=== 3. 登出 ===')
  const logoutResp = await postJson(
    `${baseUrl}/api/v1/auth/logout`,
    null,
    { Authorization: `Bearer ${accessToken}` },
  )
  console.log(`logout status=${logoutResp.status}（204 表示成功撤销会话）`)
  return { access_token: accessToken, user }
}

// ---------------------------------------------------------------------------
// 场景 4：OAuth 授权码流程客户端模拟
// ---------------------------------------------------------------------------

/**
 * 模拟 OAuth 授权码（Authorization Code + PKCE）流程。
 *
 * 平台提供两步接口：
 *   - GET  /api/v1/auth/oauth/{provider}/authorize → 返回 authorization_url
 *   - GET  /api/v1/auth/oauth/{provider}/callback?code=...&state=... → 换发会话
 *
 * 真实场景下用户需在浏览器完成第三方授权后回调；本示例只演示第一步拿授权 URL，
 * 并说明第二步的拼装方式（不真正触发回调，避免污染 state）。
 */
async function oauthAuthorizationCodeFlow(baseUrl, provider) {
  console.log('\n=== 4. OAuth 授权码流程（客户端模拟）===')
  const { status, body } = await postJson(
    `${baseUrl}/api/v1/auth/oauth/${encodeURIComponent(provider)}/authorize`,
    null,
    null,
    'GET',
  )
  console.log(`authorize status=${status}`)
  if (status === 200 && typeof body === 'object') {
    const authUrl = body.authorization_url
    console.log(`  authorization_url=${authUrl}`)
    console.log('  -> 引导用户在浏览器打开该 URL 完成第三方授权')
    console.log('  -> 第三方回调 WorkAMA 后，平台会再回调 redirect_uri?code=...&state=...')
    const callbackUrl =
      `${baseUrl}/api/v1/auth/oauth/${encodeURIComponent(provider)}` +
      '/callback?code=<授权码>&state=<state>'
    console.log(`  第二步 callback 端点: ${callbackUrl}`)
  } else {
    console.log(`  [INFO] 该提供商可能未启用 OAuth：${brief(body)}`)
  }
}

// ---------------------------------------------------------------------------
// 场景 5：API Key 与 Access Token 两种认证方式对比
// ---------------------------------------------------------------------------

/**
 * 对比 API Key 与 Access Token 两种 SDK 认证方式。
 *
 * - Access Token：以 Authorization: Bearer <token> 头部发送，优先级更高
 * - API Key：以 X-WorkAMA-API-Key 头部发送，适合长期服务端集成
 * 两者可分别构造客户端，互不影响。
 */
async function compareAuthModes(baseUrl, accessToken, apiKey) {
  console.log('\n=== 5. API Key vs Access Token 认证对比 ===')
  if (accessToken) {
    const c1 = new WorkAMAClient({ baseUrl, accessToken })
    console.log('  [Access Token] client 已构造，将使用 Authorization: Bearer 头')
    await safeList(c1)
  } else {
    console.log('  [Access Token] 未提供，跳过')
  }

  if (apiKey) {
    const c2 = new WorkAMAClient({ baseUrl, apiKey })
    console.log('  [API Key] client 已构造，将使用 X-WorkAMA-API-Key 头')
    await safeList(c2)
  } else {
    console.log('  [API Key] 未提供 WORKAMA_API_KEY，跳过（API Key 需 owner/admin 角色创建）')
  }

  if (accessToken && apiKey) {
    // 同时提供两者时，SDK 优先使用 Bearer Token
    const c3 = new WorkAMAClient({ baseUrl, accessToken, apiKey })
    console.log('  [同时提供] SDK 优先使用 Access Token（Bearer）')
  }
}

/** 安全调用 listWorkflows，捕获并打印异常，避免示例因 401/403 中断。 */
async function safeList(client) {
  try {
    const resp = await client.listWorkflows({ limit: 3 })
    const count = (resp.items || []).length
    console.log(`    listWorkflows OK，items=${count}`)
  } catch (err) {
    console.log(`    listWorkflows 调用返回异常: ${err.message || err}`)
  }
}

// ---------------------------------------------------------------------------
// 入口
// ---------------------------------------------------------------------------

async function main() {
  const result = await passwordLoginFlow(BASE_URL, EMAIL, PASSWORD)
  const accessToken = result ? result.access_token : null

  await oauthAuthorizationCodeFlow(BASE_URL, OAUTH_PROVIDER)
  await compareAuthModes(BASE_URL, accessToken, API_KEY)

  console.log('\n[OK] auth_flow 示例完成')
}

main().catch((err) => {
  if (err instanceof WorkAMAError) {
    console.error(`[ERR] SDK 错误: ${err.message} (status=${err.statusCode})`)
    process.exitCode = 5
  } else {
    console.error('[FATAL]', err)
    process.exit(1)
  }
})
