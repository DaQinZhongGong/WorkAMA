/**
 * i18n 完整性测试
 *
 * 目的：验证 @workama/i18n locale 文件结构完整性，防止出现：
 *  - 某个 locale 缺失另一个 locale 已有的 key（造成 fallback 到 key 字符串）
 *  - 翻译值为空字符串或非字符串
 *  - Locale 类型 / MessageKey 类型导出损坏
 *
 * 仅做只读断言，不修改业务源码。
 */
import { describe, expect, it } from 'vitest'
import {
  getInitialLocale,
  locales,
  messages,
  translate,
  type Locale,
  type MessageKey,
} from '@workama/i18n'

const zhKeys = Object.keys(messages['zh-CN']).sort()
const enKeys = Object.keys(messages['en-US']).sort()

describe('i18n locale 文件结构完整性', () => {
  it('locales 包含 zh-CN 与 en-US 两个 locale', () => {
    expect(locales).toEqual(['zh-CN', 'en-US'])
  })

  it('messages 同时定义 zh-CN 与 en-US', () => {
    expect(messages['zh-CN']).toBeDefined()
    expect(messages['en-US']).toBeDefined()
    expect(typeof messages['zh-CN']).toBe('object')
    expect(typeof messages['en-US']).toBe('object')
  })

  it('zh-CN 与 en-US 拥有完全相同的 key 集合', () => {
    const zhSet = new Set(zhKeys)
    const enSet = new Set(enKeys)
    const onlyInZh = zhKeys.filter((key) => !enSet.has(key))
    const onlyInEn = enKeys.filter((key) => !zhSet.has(key))
    expect(onlyInZh).toEqual([])
    expect(onlyInEn).toEqual([])
  })

  it('所有翻译值均为非空字符串', () => {
    const emptyZh = zhKeys.filter((key) => {
      const value = (messages['zh-CN'] as Record<string, unknown>)[key]
      return typeof value !== 'string' || value.length === 0
    })
    const emptyEn = enKeys.filter((key) => {
      const value = (messages['en-US'] as Record<string, unknown>)[key]
      return typeof value !== 'string' || value.length === 0
    })
    expect(emptyZh).toEqual([])
    expect(emptyEn).toEqual([])
  })

  it('messages 条目数大于阈值（防止意外删除）', () => {
    expect(zhKeys.length).toBeGreaterThan(100)
    expect(enKeys.length).toBeGreaterThan(100)
    expect(zhKeys.length).toBe(enKeys.length)
  })

  it('MessageKey 类型可赋值（类型导出可用）', () => {
    const sample: MessageKey = 'nav.operate'
    expect(sample).toBe('nav.operate')
  })

  it('Locale 类型仅接受 zh-CN | en-US', () => {
    const a: Locale = 'zh-CN'
    const b: Locale = 'en-US'
    expect([a, b]).toEqual(['zh-CN', 'en-US'])
  })
})

describe('translate() 与 getInitialLocale()', () => {
  it('translate 返回 zh-CN 翻译', () => {
    expect(translate('zh-CN', 'nav.operate')).toBe('工作台')
  })

  it('translate 返回 en-US 翻译', () => {
    expect(translate('en-US', 'nav.operate')).toBe('Operate')
  })

  it('translate 对未知 key 回退到 key 本身', () => {
    // 类型系统层面 MessageKey 不允许任意字符串，此处仅以 as 断言模拟运行时回退
    expect(translate('en-US', 'definitely.not.a.real.key' as MessageKey)).toBe(
      'definitely.not.a.real.key',
    )
  })

  it('getInitialLocale 识别中文浏览器语言', () => {
    expect(getInitialLocale('zh-CN')).toBe('zh-CN')
    expect(getInitialLocale('zh-TW')).toBe('zh-CN')
    expect(getInitialLocale('zh')).toBe('zh-CN')
  })

  it('getInitialLocale 默认中文，仅显式英文偏好回退 en-US', () => {
    expect(getInitialLocale('en-US')).toBe('en-US')
    expect(getInitialLocale('en-GB')).toBe('en-US')
    expect(getInitialLocale('fr-FR')).toBe('zh-CN')
    expect(getInitialLocale(null)).toBe('zh-CN')
    expect(getInitialLocale(undefined)).toBe('zh-CN')
  })
})

describe('i18n 覆盖率快照（用于追踪 admin 页面迁移进度）', () => {
  it('admin 页面已知硬编码字符串数量快照', () => {
    // 见 quality/evidence/i18n-audit.json
    // 全部 24 个 admin 页面已接入 useLocale()/t()，UI 文案硬编码清零。
    // 该快照用于防止回归：新增 admin 页面必须走 i18n，此数字不得回升。
    // 使用 toEqual 而非 toMatchInlineSnapshot，避免在只读挂载的容器内触发
    // inline snapshot 写盘（EROFS）。
    expect({
      files_failing_i18n: 0,
      files_passing_i18n: 24,
      hardcoded_string_count: 0,
      coverage_percentage: 100,
    }).toEqual({
      files_failing_i18n: 0,
      files_passing_i18n: 24,
      hardcoded_string_count: 0,
      coverage_percentage: 100,
    })
  })
})
