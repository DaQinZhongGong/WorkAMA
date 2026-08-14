import { describe, expect, it } from 'vitest'
import manifestRaw from '../public/manifest.webmanifest?raw'
import swRaw from '../public/sw.js?raw'
import mainRaw from './main.tsx?raw'
import indexRaw from '../index.html?raw'

const manifest = JSON.parse(manifestRaw) as {
  name: string
  short_name: string
  start_url: string
  scope: string
  display: string
  theme_color: string
  background_color: string
  icons: Array<{ src: string; sizes: string; type: string; purpose?: string }>
}

describe('PWA manifest - 结构验证', () => {
  it('包含 name 与 short_name 字段', () => {
    expect(manifest.name).toBe('WorkAMA Mobile')
    expect(manifest.short_name).toBe('WorkAMA')
  })

  it('start_url 指向 /chat 且 scope 指向根路径', () => {
    expect(manifest.start_url).toBe('/chat')
    expect(manifest.scope).toBe('/')
  })

  it('display 模式为 standalone（可安装 PWA）', () => {
    expect(manifest.display).toBe('standalone')
  })

  it('配置 theme_color 与 background_color', () => {
    expect(manifest.theme_color).toMatch(/^#[0-9a-fA-F]{6}$/)
    expect(manifest.background_color).toMatch(/^#[0-9a-fA-F]{6}$/)
  })

  it('提供 192x192 与 512x512 尺寸图标', () => {
    expect(Array.isArray(manifest.icons)).toBe(true)
    expect(manifest.icons.length).toBeGreaterThanOrEqual(2)
    const sizes = manifest.icons.map((icon) => icon.sizes)
    expect(sizes).toContain('192x192')
    expect(sizes).toContain('512x512')
  })

  it('至少一个图标声明 maskable purpose（适配自适应启动屏）', () => {
    expect(manifest.icons.some((icon) => (icon.purpose || '').includes('maskable'))).toBe(true)
  })

  it('图标 type 为 image/svg+xml 并以 data URI 内嵌（无外部网络依赖）', () => {
    for (const icon of manifest.icons) {
      expect(icon.type).toBe('image/svg+xml')
      expect(icon.src.startsWith('data:image/svg+xml')).toBe(true)
    }
  })

  it('声明 categories 与 orientation 字段（提升安装体验）', () => {
    expect(manifest).toHaveProperty('categories')
    expect(manifest).toHaveProperty('orientation')
  })
})

describe('service worker 注册逻辑', () => {
  it('main.tsx 检测 serviceWorker 能力并注册 /sw.js', () => {
    expect(mainRaw).toMatch(/'serviceWorker' in navigator/)
    expect(mainRaw).toMatch(/navigator\.serviceWorker\.register\(['"]\/sw\.js['"]/)
    expect(mainRaw).toMatch(/scope:\s*['"]\/['"]/)
  })

  it('注册调用附带 .catch 以避免未捕获异常', () => {
    expect(mainRaw).toMatch(/\.catch\(/)
  })

  it('index.html 在 load 事件中亦注册 /sw.js（双重保险）', () => {
    expect(indexRaw).toMatch(/navigator\.serviceWorker\.register\(['"]\/sw\.js['"]/)
    expect(indexRaw).toMatch(/updateViaCache:\s*['"]none['"]/)
  })

  it('index.html 引用 manifest.webmanifest', () => {
    expect(indexRaw).toMatch(/<link\s+rel=['"]manifest['"]\s+href=['"]\/manifest\.webmanifest['"]/)
  })

  it('index.html 声明 theme-color meta（与 manifest 一致）', () => {
    expect(indexRaw).toMatch(/<meta\s+name=['"]theme-color['"]\s+content=['"]#172033['"]/)
  })

  it('index.html 启用 PWA 相关 meta（mobile-web-app-capable / apple-mobile-web-app-capable）', () => {
    expect(indexRaw).toMatch(/name=['"]mobile-web-app-capable['"]\s+content=['"]yes['"]/)
    expect(indexRaw).toMatch(/name=['"]apple-mobile-web-app-capable['"]\s+content=['"]yes['"]/)
  })
})

describe('service worker 离线缓存策略', () => {
  it('install 事件预缓存应用外壳 URL', () => {
    expect(swRaw).toMatch(/addEventListener\(['"]install['"]/)
    expect(swRaw).toMatch(/SHELL_URLS/)
    expect(swRaw).toMatch(/cache\.addAll\(SHELL_URLS\)/)
  })

  it('SHELL_URLS 包含 /、/index.html、/manifest.webmanifest 及路由路径', () => {
    expect(swRaw).toMatch(/['"]\/index\.html['"]/)
    expect(swRaw).toMatch(/['"]\/manifest\.webmanifest['"]/)
    expect(swRaw).toMatch(/['"]\/chat['"]/)
    expect(swRaw).toMatch(/['"]\/agents['"]/)
    expect(swRaw).toMatch(/['"]\/knowledge['"]/)
    expect(swRaw).toMatch(/['"]\/settings['"]/)
  })

  it('install 后调用 skipWaiting 加速生效', () => {
    expect(swRaw).toMatch(/self\.skipWaiting\(\)/)
  })

  it('activate 事件清理旧版本缓存', () => {
    expect(swRaw).toMatch(/addEventListener\(['"]activate['"]/)
    expect(swRaw).toMatch(/caches\.keys\(\)/)
    expect(swRaw).toMatch(/caches\.delete\(/)
  })

  it('activate 后调用 clients.claim 接管页面', () => {
    expect(swRaw).toMatch(/self\.clients\.claim\(\)/)
  })

  it('使用稳定的缓存版本号前缀', () => {
    expect(swRaw).toMatch(/CACHE_VERSION\s*=\s*['"]workama-mobile-shell-/)
  })

  it('导航请求失败时回退到缓存的 /index.html（离线 fallback）', () => {
    expect(swRaw).toMatch(/request\.mode\s*===\s*['"]navigate['"]/)
    expect(swRaw).toMatch(/caches\.match\(['"]\/index\.html['"]\)/)
  })

  it('静态资源采用 cache-first 策略并回填缓存', () => {
    expect(swRaw).toMatch(/caches\.match\(event\.request\)/)
    expect(swRaw).toMatch(/cache\.put\(event\.request,\s*response\.clone\(\)\)/)
  })

  it('API 请求不被缓存（避免脏数据）', () => {
    expect(swRaw).toMatch(/API_PREFIXES\s*=\s*\[/)
    expect(swRaw).toMatch(/isApi\(url\)/)
    expect(swRaw).toMatch(/!isApi\(url\)/)
  })
})

describe('Web Push 推送处理', () => {
  it('监听 push 事件', () => {
    expect(swRaw).toMatch(/addEventListener\(['"]push['"]/)
  })

  it('push 事件触发 showNotification 显示通知', () => {
    expect(swRaw).toMatch(/self\.registration\.showNotification\(/)
  })

  it('默认使用 WorkAMA Mobile 作为通知标题', () => {
    expect(swRaw).toMatch(/data\.title\s*\?\?\s*['"]WorkAMA Mobile['"]/)
  })

  it('支持 PWA_TEST_PUSH 测试消息协议', () => {
    expect(swRaw).toMatch(/PWA_TEST_PUSH/)
    expect(swRaw).toMatch(/PWA_TEST_PUSH_RESULT/)
  })

  it('PWA_TEST_PUSH 失败时回报 ok:false 与错误信息', () => {
    expect(swRaw).toMatch(/ok:\s*false/)
    expect(swRaw).toMatch(/error:\s*String\(error\)/)
  })

  it('PWA_TEST_MOCK_NOTIFICATIONS 注入 mock showNotification（用于无通知权限环境）', () => {
    expect(swRaw).toMatch(/PWA_TEST_MOCK_NOTIFICATIONS/)
    expect(swRaw).toMatch(/__testShowNotification/)
  })
})

describe('离线 fallback 页面', () => {
  it('sw.js 把 /index.html 列入 SHELL_URLS 预缓存', () => {
    expect(swRaw).toMatch(/SHELL_URLS\s*=\s*\[[\s\S]*?['"]\/index\.html['"][\s\S]*?\]/)
  })

  it('index.html 包含 #app 挂载点（确保离线下 React 可启动）', () => {
    expect(indexRaw).toMatch(/<div\s+id=['"]app['"]/)
  })

  it('index.html 加载 /src/main.tsx 入口脚本', () => {
    expect(indexRaw).toMatch(/src=['"]\/src\/main\.tsx['"]/)
  })
})
