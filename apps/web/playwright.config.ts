import { defineConfig, devices } from '@playwright/test'

/**
 * WorkAMA Web E2E 测试配置。
 *
 * 浏览器矩阵覆盖：
 *   - chromium / firefox / webkit（桌面端三浏览器）
 *   - mobile-chrome / mobile-safari（移动端视口）
 *
 * 仅匹配 `*.spec.ts`，不会影响 tests/ 下已有的独立 .mjs 脚本
 * （e2e-journey.mjs / browser-smoke.mjs 等通过 chromium.launch() 直接驱动）。
 */
const baseURL = process.env.BROWSER_BASE_URL ?? 'http://localhost:20204'

export default defineConfig({
  testDir: './tests',
  testMatch: /.*\.spec\.ts$/,
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: [['list']],
  snapshotPathTemplate: '{testDir}/__screenshots__/{projectName}/{testFilePath}/{arg}{ext}',
  use: {
    baseURL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    locale: 'en-US',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'firefox',
      use: { ...devices['Desktop Firefox'] },
    },
    {
      name: 'webkit',
      use: { ...devices['Desktop Safari'] },
    },
    {
      name: 'mobile-chrome',
      use: { ...devices['Pixel 7'] },
    },
    {
      name: 'mobile-safari',
      use: { ...devices['iPhone 14'] },
    },
  ],
  webServer: {
    command: 'pnpm preview',
    url: baseURL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
})
