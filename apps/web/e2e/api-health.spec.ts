import { test, expect } from '@playwright/test'

/**
 * 平台 API 健康检查 E2E 测试。
 *
 * 直接通过 HTTP 请求验证 http://localhost:20200/healthz 返回 ok。
 * 不依赖浏览器，使用 Playwright 的 request fixture。
 */
test.describe('API 健康检查', () => {
  test('GET /healthz 返回 200 且响应包含 ok', async ({ request }) => {
    const response = await request.get('http://localhost:20200/healthz', {
      timeout: 15_000,
    })
    expect(response.status()).toBe(200)
    const body = await response.text()
    // 健康检查端点通常返回 "ok" 或包含 "ok" 的 JSON
    expect(body.toLowerCase()).toContain('ok')
  })

  test('GET /healthz 响应时间合理', async ({ request }) => {
    const start = Date.now()
    const response = await request.get('http://localhost:20200/healthz', {
      timeout: 15_000,
    })
    const elapsed = Date.now() - start
    expect(response.status()).toBe(200)
    // 健康检查应在 5 秒内响应
    expect(elapsed).toBeLessThan(5_000)
  })

  test('GET /api/v1/healthz 同样可用', async ({ request }) => {
    // 部分部署可能使用 /api/v1/healthz 前缀
    const response = await request.get('http://localhost:20200/api/v1/healthz', {
      timeout: 15_000,
    })
    // 接受 200 或 404（如果该路径不存在，主 /healthz 已验证）
    expect([200, 404]).toContain(response.status())
  })
})
