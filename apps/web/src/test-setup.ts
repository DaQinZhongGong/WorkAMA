/**
 * Vitest 全局 setup：固定 UI 语言环境为 zh-CN。
 *
 * LocaleProvider 按 localStorage → navigator.language 解析初始语言；
 * jsdom 的 navigator.language 默认为 en-US，会导致所有断言中文文案的
 * 组件测试随运行环境漂移。这里在每个用例前显式钉住语言与存储状态，
 * 保证测试确定性（与生产默认中文一致）。
 */
import { beforeEach } from 'vitest'

beforeEach(() => {
  window.localStorage.clear()
  try {
    Object.defineProperty(window.navigator, 'language', {
      value: 'zh-CN',
      configurable: true,
      writable: true,
    })
  } catch {
    // 某些环境下 navigator 属性不可重定义；此时依赖 localStorage 已清空的默认行为。
  }
})
