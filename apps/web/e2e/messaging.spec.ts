import { test, expect, type Page } from '@playwright/test'
import { login } from './helpers'

/**
 * IM 通道基础模块 (messaging) E2E 测试。
 *
 * 覆盖场景：
 *   1. 创建 direct 会话 → 201 + conversation_id
 *   2. 创建 group 会话 → 201
 *   3. 列出会话 → 200 + 包含刚创建的
 *   4. 发送消息 → 201
 *   5. 列出消息 → 200 + 包含刚发送的
 *   6. 标记已读 → 200 + last_read_at
 *   7. 非成员访问会话消息 → 403
 *   8. 退出会话 → 204
 *
 * 端点前缀：POST/GET /api/v1/messaging/conversations
 * 复用 helpers.ts login() 完成 UI 登录后，从 localStorage 取 workama_token 调用 API。
 */

const MESSAGING_API = '/api/v1/messaging/conversations'

/**
 * 从 localStorage 取 workama_token，用于 API 请求的 Bearer 头。
 * login() 完成后 SPA 会将 access token 持久化到 workama_token。
 */
async function getAuthToken(page: Page): Promise<string> {
  const token = await page.evaluate(() => window.localStorage.getItem('workama_token'))
  expect(token, '登录后 localStorage 必须存在 workama_token').toBeTruthy()
  return token as string
}

/** 兼容列表响应的多种结构：数组 / { items: [] } / { data: [] }。 */
function extractItems(body: unknown): unknown[] {
  if (Array.isArray(body)) return body
  if (body && typeof body === 'object') {
    const obj = body as Record<string, unknown>
    if (Array.isArray(obj.items)) return obj.items
    if (Array.isArray(obj.data)) return obj.data
  }
  return []
}

/**
 * 生成唯一对端成员标识。
 *
 * create_conversation 端点仅将 member_user_ids 写入 im_conversation_member，
 * 不校验该 user_id 是否对应真实用户，故可用占位标识构造会话用于测试。
 */
function peerUserId(): string {
  return `e2e-peer-${Date.now()}-${Math.floor(Math.random() * 1_000_000)}@workama.example.com`
}

test.describe('IM 通道基础 (messaging)', () => {
  test.beforeEach(async ({ page }) => {
    await login(page)
  })

  test('创建 direct 会话 POST 返回 201 + conversation_id', async ({ page }) => {
    const token = await getAuthToken(page)
    const response = await page.request.post(MESSAGING_API, {
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      data: { type: 'direct', member_user_ids: [peerUserId()] },
    })
    expect(response.status()).toBe(201)
    const body = await response.json()
    const id = body.id ?? body.conversation_id ?? body.data?.id
    expect(id, '创建 direct 会话响应必须包含 id').toBeTruthy()
    expect(body.type ?? body.data?.type).toBe('direct')
  })

  test('创建 group 会话 POST 返回 201', async ({ page }) => {
    const token = await getAuthToken(page)
    const response = await page.request.post(MESSAGING_API, {
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      data: {
        type: 'group',
        title: `e2e-group-${Date.now()}`,
        member_user_ids: [peerUserId(), peerUserId()],
      },
    })
    expect(response.status()).toBe(201)
    const body = await response.json()
    const id = body.id ?? body.conversation_id ?? body.data?.id
    expect(id, '创建 group 会话响应必须包含 id').toBeTruthy()
    expect(body.type ?? body.data?.type).toBe('group')
  })

  test('列出会话 GET 返回 200 且包含刚创建的', async ({ page }) => {
    const token = await getAuthToken(page)
    // 先创建一个会话，确保列表非空且可命中
    const createRes = await page.request.post(MESSAGING_API, {
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      data: { type: 'direct', member_user_ids: [peerUserId()] },
    })
    expect(createRes.status()).toBe(201)
    const created = await createRes.json()
    const createdId = created.id ?? created.conversation_id

    const listRes = await page.request.get(MESSAGING_API, {
      headers: { Authorization: `Bearer ${token}` },
    })
    expect(listRes.status()).toBe(200)
    const items = extractItems(await listRes.json())
    expect(Array.isArray(items)).toBe(true)
    const ids = items.map((it) => (it as Record<string, unknown>).id)
    expect(ids, '列表应包含刚创建的会话').toContain(createdId)
  })

  test('发送消息 POST 返回 201', async ({ page }) => {
    const token = await getAuthToken(page)
    const createRes = await page.request.post(MESSAGING_API, {
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      data: { type: 'direct', member_user_ids: [peerUserId()] },
    })
    expect(createRes.status()).toBe(201)
    const conv = await createRes.json()
    const convId = conv.id ?? conv.conversation_id

    const content = `e2e-msg-${Date.now()}`
    const sendRes = await page.request.post(`${MESSAGING_API}/${convId}/messages`, {
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      data: { content },
    })
    expect(sendRes.status()).toBe(201)
    const msg = await sendRes.json()
    expect(msg.content ?? msg.data?.content).toBe(content)
    expect(msg.conversation_id ?? msg.data?.conversation_id).toBe(convId)
  })

  test('列出消息 GET 返回 200 且包含刚发送的', async ({ page }) => {
    const token = await getAuthToken(page)
    const createRes = await page.request.post(MESSAGING_API, {
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      data: { type: 'direct', member_user_ids: [peerUserId()] },
    })
    expect(createRes.status()).toBe(201)
    const conv = await createRes.json()
    const convId = conv.id ?? conv.conversation_id

    const content = `e2e-list-${Date.now()}`
    const sendRes = await page.request.post(`${MESSAGING_API}/${convId}/messages`, {
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      data: { content },
    })
    expect(sendRes.status()).toBe(201)

    const listRes = await page.request.get(`${MESSAGING_API}/${convId}/messages`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    expect(listRes.status()).toBe(200)
    const items = extractItems(await listRes.json())
    const contents = items.map((it) => (it as Record<string, unknown>).content)
    expect(contents, '消息列表应包含刚发送的消息').toContain(content)
  })

  test('标记已读 POST 返回 200 + last_read_at', async ({ page }) => {
    const token = await getAuthToken(page)
    const createRes = await page.request.post(MESSAGING_API, {
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      data: { type: 'direct', member_user_ids: [peerUserId()] },
    })
    expect(createRes.status()).toBe(201)
    const conv = await createRes.json()
    const convId = conv.id ?? conv.conversation_id

    const readRes = await page.request.post(`${MESSAGING_API}/${convId}/read`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    expect(readRes.status()).toBe(200)
    const body = await readRes.json()
    expect(body.conversation_id ?? body.data?.conversation_id).toBe(convId)
    expect(body.last_read_at ?? body.data?.last_read_at, 'last_read_at 必须被更新').toBeTruthy()
  })

  test('非成员访问会话消息 GET 返回 403', async ({ page }) => {
    const token = await getAuthToken(page)
    // 创建 group 会话（creator + 1 other = 2 成员）。creator 退出后仍残留 1 成员，
    // 会话不会被 CASCADE 删除；此时原 creator 已非成员，再访问消息应 403。
    // （create 端点不校验 member_user_ids 是否对应真实用户，占位标识即可。）
    const createRes = await page.request.post(MESSAGING_API, {
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      data: { type: 'group', member_user_ids: [peerUserId()] },
    })
    expect(createRes.status()).toBe(201)
    const conv = await createRes.json()
    const convId = conv.id ?? conv.conversation_id

    // creator 退出会话 → 204（残留 1 成员，会话保留）
    const leaveRes = await page.request.delete(`${MESSAGING_API}/${convId}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    expect(leaveRes.status()).toBe(204)

    // 退出后再访问消息 → 403（非成员）
    const msgsRes = await page.request.get(`${MESSAGING_API}/${convId}/messages`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    expect(msgsRes.status()).toBe(403)
  })

  test('退出会话 DELETE 返回 204', async ({ page }) => {
    const token = await getAuthToken(page)
    const createRes = await page.request.post(MESSAGING_API, {
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      data: { type: 'group', member_user_ids: [peerUserId()] },
    })
    expect(createRes.status()).toBe(201)
    const conv = await createRes.json()
    const convId = conv.id ?? conv.conversation_id

    const leaveRes = await page.request.delete(`${MESSAGING_API}/${convId}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    expect(leaveRes.status()).toBe(204)
  })
})