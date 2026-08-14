import { defineConfig, devices } from '@playwright/test'

/**
 * WorkAMA Web E2E 业务旅程测试配置。
 *
 * 独立于 tests/ 下的视觉回归与移动视口测试，仅扫描 e2e/ 目录。
 * 仅使用 chromium 浏览器以加快执行速度。
 *
 * 运行方式：
 *   npx playwright test --config=playwright.e2e.config.ts --reporter=list
 *
 * 环境要求：
 *   - Docker Compose 环境已启动（http://localhost:20204 可访问）
 *   - 平台 API 已启动（http://localhost:20200 可访问）
 */
const baseURL = process.env.BROWSER_BASE_URL ?? 'http://localhost:20204'

export default defineConfig({
  testDir: './e2e',
  testMatch: /.*\.spec\.ts$/,
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: [['list']],
  use: {
    baseURL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    locale: 'en-US',
    navigationTimeout: 30_000,
    actionTimeout: 15_000,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: {
    command: 'pnpm preview',
    url: baseURL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
})
