/**
 * Playwright 运行时加载器（Windows / pnpm 兼容层）。
 *
 * 背景：Windows + pnpm 环境下 `@playwright+test/node_modules/playwright` 的
 * 符号链接可能因 ACL 损坏导致 `Cannot find module 'playwright/lib/program'`。
 * 本模块按优先级回退到可用实现，保证脚本类浏览器验证不被依赖问题阻塞。
 *
 * 解析顺序：
 *   1. @playwright/test（正常安装）
 *   2. playwright
 *   3. playwright-core（pnpm store 直连，最后兜底）
 *
 * 同时导出 resolveExecutablePath()，用于在没有 PW 浏览器注册表时
 * 定位本机已下载的 chromium。
 */
import { pathToFileURL } from 'node:url'
import { existsSync, readdirSync } from 'node:fs'
import path from 'node:path'
import os from 'node:os'

const HERE = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'))
const REPO_ROOT = path.resolve(HERE, '..', '..', '..')

function pnpmStoreCandidates(pkg) {
  const store = path.join(REPO_ROOT, 'node_modules', '.pnpm')
  if (!existsSync(store)) return []
  return readdirSync(store)
    .filter((d) => d.startsWith(`${pkg}@`))
    .map((d) => path.join(store, d, 'node_modules', pkg, 'index.mjs'))
    .filter((p) => existsSync(p))
}

async function tryImport(spec) {
  try {
    return await import(spec)
  } catch {
    return null
  }
}

export async function loadPlaywright() {
  for (const spec of ['@playwright/test', 'playwright']) {
    const mod = await tryImport(spec)
    if (mod?.chromium) return { mod, source: spec }
  }
  for (const file of [...pnpmStoreCandidates('playwright'), ...pnpmStoreCandidates('playwright-core')]) {
    const mod = await tryImport(pathToFileURL(file).href)
    if (mod?.chromium) return { mod, source: file }
  }
  throw new Error('无法加载 playwright / playwright-core，请检查依赖安装')
}

/** 定位本机 chromium 可执行文件（PW 浏览器缓存目录）。 */
export function resolveExecutablePath() {
  if (process.env.BROWSER_EXECUTABLE) return process.env.BROWSER_EXECUTABLE
  for (const p of ['/usr/bin/chromium-browser', '/usr/bin/chromium', '/usr/bin/google-chrome']) {
    if (existsSync(p)) return p
  }
  const root =
    process.env.PLAYWRIGHT_BROWSERS_PATH ||
    (process.platform === 'win32'
      ? path.join(os.homedir(), 'AppData', 'Local', 'ms-playwright')
      : path.join(os.homedir(), '.cache', 'ms-playwright'))
  if (!existsSync(root)) return undefined
  const dirs = readdirSync(root)
    .filter((d) => d.startsWith('chromium-') && !d.includes('headless_shell'))
    .sort((a, b) => Number(b.split('-')[1] || 0) - Number(a.split('-')[1] || 0))
  for (const d of dirs) {
    for (const rel of [
      ['chrome-win64', 'chrome.exe'],
      ['chrome-win', 'chrome.exe'],
      ['chrome-linux', 'chrome'],
      ['chrome-mac', 'Chromium.app', 'Contents', 'MacOS', 'Chromium'],
    ]) {
      const exe = path.join(root, d, ...rel)
      if (existsSync(exe)) return exe
    }
  }
  return undefined
}
