import { defineConfig, devices } from '@playwright/test'

const baseURL = process.env.MOBILE_PWA_BASE_URL || 'http://127.0.0.1:3100'

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'list',
  use: {
    baseURL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium-mobile',
      use: {
        ...devices['Desktop Chrome'],
        browserName: 'chromium',
        viewport: { width: 390, height: 844 },
        userAgent:
          'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
        isMobile: true,
        hasTouch: true,
        launchOptions: {
          executablePath: process.env.BROWSER_EXECUTABLE || undefined,
          args: process.env.BROWSER_EXECUTABLE
            ? ['--no-sandbox', '--disable-dev-shm-usage']
            : [],
        },
      },
    },
  ],
  webServer: {
    command: 'pnpm preview',
    url: baseURL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
})
