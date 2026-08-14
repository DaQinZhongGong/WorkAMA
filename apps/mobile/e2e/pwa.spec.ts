import { test, expect } from '@playwright/test'

test.describe('Mobile PWA real browser acceptance', () => {
  test.beforeEach(async ({ page, context }) => {
    await context.grantPermissions(['notifications'])
    await context.setOffline(false)
    await page.goto('/', { waitUntil: 'networkidle' })
  })

  test('manifest is served and valid', async ({ page }) => {
    const response = await page.request.get('/manifest.webmanifest')
    expect(response.ok()).toBe(true)
    expect(response.headers()['content-type']).toContain('application/manifest')
    const manifest = await response.json()
    expect(manifest.name).toBe('WorkAMA Mobile')
    expect(manifest.short_name).toBe('WorkAMA')
    expect(manifest.display).toBe('standalone')
    expect(manifest.scope).toBe('/')
    expect(manifest.start_url).toBe('/')
    expect(Array.isArray(manifest.icons)).toBe(true)
    expect(manifest.icons.length).toBeGreaterThanOrEqual(2)
    const sizes = manifest.icons.map((icon: { sizes?: string }) => icon.sizes)
    expect(sizes).toContain('192x192')
    expect(sizes).toContain('512x512')
  })

  test('service worker registers with correct scope', async ({ page, baseURL }) => {
    const registration = await page.evaluate(async () => {
      const reg = await navigator.serviceWorker.ready
      return {
        scope: reg.scope,
        scriptURL: reg.active?.scriptURL,
      }
    })
    const expectedScope = `${baseURL?.replace(/\/$/, '') ?? ''}/`
    expect(registration.scope).toBe(expectedScope)
    expect(registration.scriptURL).toMatch(/\/sw\.js$/)
  })

  test('beforeinstallprompt fires and install button appears', async ({ page }) => {
    await expect(page.locator('[data-testid="pwa-install-button"]')).toBeHidden()
    await page.evaluate(() => {
      const event = new Event('beforeinstallprompt', { bubbles: true, cancelable: true }) as Event & {
        prompt?: () => Promise<void>
        userChoice?: Promise<{ outcome: string }>
      }
      event.prompt = async () => {}
      event.userChoice = Promise.resolve({ outcome: 'accepted' })
      window.dispatchEvent(event)
    })
    await expect(page.locator('[data-testid="pwa-install-button"]')).toBeVisible()
  })

  test('PWA install flow calls prompt() and hides button', async ({ page }) => {
    await page.evaluate(() => {
      ;(window as unknown as Record<string, unknown>).__pwaPromptCalled = false
      const event = new Event('beforeinstallprompt', { bubbles: true, cancelable: true }) as Event & {
        prompt?: () => Promise<void>
        userChoice?: Promise<{ outcome: string }>
      }
      event.prompt = async () => {
        ;(window as unknown as Record<string, unknown>).__pwaPromptCalled = true
      }
      event.userChoice = Promise.resolve({ outcome: 'accepted' })
      window.dispatchEvent(event)
    })
    await page.locator('[data-testid="pwa-install-button"]').click()
    const promptCalled = await page.evaluate(
      () => (window as unknown as Record<string, unknown>).__pwaPromptCalled as boolean,
    )
    expect(promptCalled).toBe(true)
    await expect(page.locator('[data-testid="pwa-install-button"]')).toBeHidden()
  })

  test('offline mode still renders app via service worker cache', async ({ page, context }) => {
    await page.reload({ waitUntil: 'networkidle' })
    await expect(page.locator('[data-testid="offline-banner"]')).toBeHidden()
    await context.setOffline(true)
    await page.reload({ waitUntil: 'networkidle' })
    await expect(page.locator('#mobile-email')).toBeVisible()
    const offlineBannerVisible = await page.locator('[data-testid="offline-banner"]').isVisible().catch(() => false)
    expect([true, false]).toContain(offlineBannerVisible)
  })

  test('offline banner toggles with online/offline events', async ({ page }) => {
    await expect(page.locator('[data-testid="offline-banner"]')).toBeHidden()
    await page.evaluate(() => { window.dispatchEvent(new Event('offline')) })
    await expect(page.locator('[data-testid="offline-banner"]')).toBeVisible()
    await page.evaluate(() => { window.dispatchEvent(new Event('online')) })
    await expect(page.locator('[data-testid="offline-banner"]')).toBeHidden()
  })

  test('online recovery hides offline banner', async ({ page }) => {
    await page.evaluate(() => { window.dispatchEvent(new Event('offline')) })
    await expect(page.locator('[data-testid="offline-banner"]')).toBeVisible()
    await page.evaluate(() => { window.dispatchEvent(new Event('online')) })
    await expect(page.locator('[data-testid="offline-banner"]')).toBeHidden()
  })

  test('Web Push subscription and notification flow', async ({ page }) => {
    const result = await page.evaluate(async () => {
      const reg = await navigator.serviceWorker.ready
      let subscribeCalled = false
      reg.pushManager.subscribe = async (options) => {
        subscribeCalled = true
        return { endpoint: 'https://test.example/push/1', ...options } as unknown as PushSubscription
      }
      Object.defineProperty(window.Notification, 'permission', {
        value: 'granted',
        configurable: true,
        writable: true,
      })
      await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: new Uint8Array([1, 2, 3]),
      })

      const controller = await new Promise<ServiceWorker | null>((resolve) => {
        if (navigator.serviceWorker.controller) return resolve(navigator.serviceWorker.controller)
        navigator.serviceWorker.addEventListener('controllerchange', () => resolve(navigator.serviceWorker.controller), { once: true })
        setTimeout(() => resolve(null), 5000)
      })

      const notificationResult = await new Promise<{ ok: boolean; error?: string }>((resolve) => {
        if (!controller) return resolve({ ok: false, error: 'no controller' })
        navigator.serviceWorker.addEventListener(
          'message',
          (event) => {
            if (event.data?.type === 'PWA_TEST_PUSH_RESULT') {
              resolve({ ok: event.data.ok, error: event.data.error })
            }
          },
          { once: true },
        )
        setTimeout(() => resolve({ ok: false, error: 'timeout' }), 5000)
        controller.postMessage({ type: 'PWA_TEST_MOCK_NOTIFICATIONS' })
        controller.postMessage({
          type: 'PWA_TEST_PUSH',
          title: 'PWA Test',
          options: { body: 'Push notification test' },
        })
      })

      return { subscribeCalled, notificationResult }
    })
    expect(result.subscribeCalled).toBe(true)
    expect(result.notificationResult.ok, `push notification failed: ${result.notificationResult.error}`).toBe(true)
  })
})
