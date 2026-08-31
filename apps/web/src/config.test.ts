/**
 * 运行时配置解析测试（config.ts）
 *
 * 优先级契约：window.__WORKAMA_CONFIG__（容器入口注入）> VITE_* 构建期/环境 >
 * 缺省回落；空字符串一律视为未设置并逐级回退。
 *
 * 注意：容器内运行时（Dockerfile ENV）与本地 vitest 的 import.meta.env.VITE_*
 * 取值不同，因此回退级断言以「同环境下 import.meta.env 实际值」为基准，
 * 不硬编码具体端点。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

afterEach(() => {
  vi.resetModules()
  ;(window as any).__WORKAMA_CONFIG__ = undefined
})

async function loadConfig() {
  vi.resetModules()
  return await import('./config')
}

describe('web runtime config resolution', () => {
  it('运行时覆盖优先于构建期/环境 VITE_* 与缺省', async () => {
    ;(window as any).__WORKAMA_CONFIG__ = {
      platformApiUrl: 'https://api.prod.example.com',
      agentWsUrl: 'wss://ws.prod.example.com',
      grafanaUrl: 'https://grafana.prod.example.com/d/x',
    }
    const cfg = await loadConfig()
    expect(cfg.platformApiUrl).toBe('https://api.prod.example.com')
    expect(cfg.agentWsUrl).toBe('wss://ws.prod.example.com')
    expect(cfg.grafanaUrl).toBe('https://grafana.prod.example.com/d/x')
  })

  it('空字符串视为未设置，回退到下一级（VITE_* 或缺省），绝不输出运行时的空值', async () => {
    ;(window as any).__WORKAMA_CONFIG__ = { platformApiUrl: '', agentWsUrl: '', grafanaUrl: '' }
    const viteApi = (import.meta.env.VITE_PLATFORM_API_URL as string | undefined) ?? ''
    const viteWs = (import.meta.env.VITE_AGENT_WS_URL as string | undefined) ?? ''
    const cfg = await loadConfig()
    if (viteApi) {
      expect(cfg.platformApiUrl).toBe(viteApi)
    } else if (import.meta.env.DEV) {
      expect(cfg.platformApiUrl).toBe('') // dev 走 Vite 代理
    }
    if (viteWs) {
      expect(cfg.agentWsUrl).toBe(viteWs)
    } else {
      // dev 推导 origin；prod 缺省 ws://localhost:20201
      expect(cfg.agentWsUrl === window.location.origin.replace(/^http/, 'ws') ||
        cfg.agentWsUrl === 'ws://localhost:20201').toBe(true)
    }
    expect(cfg.grafanaUrl.length).toBeGreaterThan(0)
    expect((window as any).__WORKAMA_CONFIG__.platformApiUrl).toBe('')
  })

  it('designTokens 来自共享包且结构可用', async () => {
    const cfg = await loadConfig()
    expect(typeof cfg.designTokens).toBe('object')
  })
})
