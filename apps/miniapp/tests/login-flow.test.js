import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const __dirname = dirname(fileURLToPath(import.meta.url))
const root = join(__dirname, '..')

async function read(path) {
  return readFile(join(root, path), 'utf8')
}

/**
 * 在 mock 微信小程序环境中加载页面 JS，返回 Page 配置对象。
 * @param {object} mockApp - mock 的 app 实例
 * @param {object} mockWx - mock 的 wx 全局对象
 * @param {string} pagePath - 页面 JS 相对路径
 * @returns {Promise<object>} Page 配置对象
 */
async function loadPage(mockApp, mockWx, pagePath) {
  const source = await read(pagePath)
  let pageConfig = null
  const sandbox = {
    getApp: () => mockApp,
    Page: (config) => { pageConfig = config },
    wx: mockWx,
    console: { log: () => {}, error: () => {}, warn: () => {} },
    Promise,
    setTimeout,
    Date,
    JSON,
  }
  vm.createContext(sandbox)
  vm.runInContext(source, sandbox)
  assert.ok(pageConfig, `Page() 未被调用（${pagePath}）`)
  return pageConfig
}

/**
 * 创建 mock app 实例（模拟 app.js 的 App({...}) 行为）。
 */
function createMockApp() {
  const storage = {}
  return {
    globalData: {
      apiBase: 'http://localhost:20200',
      accessToken: null,
      userInfo: null,
    },
    setToken(token) {
      this.globalData.accessToken = token
      storage['access_token'] = token
    },
    getToken() {
      if (!this.globalData.accessToken) {
        this.globalData.accessToken = storage['access_token'] || null
      }
      return this.globalData.accessToken
    },
    clearToken() {
      this.globalData.accessToken = null
      delete storage['access_token']
    },
    request(path, method = 'GET', data = {}) {
      // 由测试覆盖此方法
      return Promise.reject(new Error('request not mocked'))
    },
    _storage: storage,
  }
}

/**
 * 创建 mock wx 全局对象。
 */
function createMockWx(options = {}) {
  const calls = []
  return {
    calls,
    setStorageSync(key, value) { calls.push({ type: 'setStorageSync', key, value }) },
    getStorageSync(key) { return null },
    removeStorageSync(key) { calls.push({ type: 'removeStorageSync', key }) },
    switchTab(opts) { calls.push({ type: 'switchTab', ...opts }) },
    reLaunch(opts) { calls.push({ type: 'reLaunch', ...opts }) },
    showToast(opts) { calls.push({ type: 'showToast', ...opts }) },
    showModal(opts) {
      calls.push({ type: 'showModal', ...opts })
      // 模拟用户点击确认
      if (opts.success) {
        opts.success({ confirm: true, cancel: false })
      }
    },
    login(opts) {
      if (options.loginCode) {
        opts.success && opts.success({ code: options.loginCode })
      } else {
        opts.fail && opts.fail({ errMsg: 'login failed' })
      }
    },
  }
}

/**
 * 创建页面实例：先展开 Page 配置（含方法），再用自定义 data 覆盖默认 data，
 * 最后注入 mock setData。确保 onLogin 读取到测试数据而非页面默认空值。
 */
function createPageInstance(pageConfig, data, setDataOverride) {
  return {
    ...pageConfig,
    data: { ...data },
    setData: setDataOverride || function (updates) { Object.assign(this.data, updates) },
  }
}

test('pages/index/index.js defines login functions', async () => {
  const js = await read('pages/index/index.js')
  assert.match(js, /onLogin/)
  assert.match(js, /onWxLogin/)
  assert.match(js, /onAccountInput/)
  assert.match(js, /onPasswordInput/)
})

test('empty account shows error message', async () => {
  const app = createMockApp()
  const wx = createMockWx()
  const page = await loadPage(app, wx, 'pages/index/index.js')

  const pageInstance = createPageInstance(page, { account: '', password: 'somepass', loading: false, errorMsg: '' })

  pageInstance.onLogin.call(pageInstance)

  assert.ok(pageInstance.data.errorMsg, '应显示错误消息')
  assert.match(pageInstance.data.errorMsg, /账号|密码|请输入/)
})

test('empty password shows error message', async () => {
  const app = createMockApp()
  const wx = createMockWx()
  const page = await loadPage(app, wx, 'pages/index/index.js')

  const pageInstance = createPageInstance(page, { account: 'test@example.com', password: '', loading: false, errorMsg: '' })

  pageInstance.onLogin.call(pageInstance)

  assert.ok(pageInstance.data.errorMsg, '应显示错误消息')
  assert.match(pageInstance.data.errorMsg, /账号|密码|请输入/)
})

test('successful login switches to tabBar chat page', async () => {
  const app = createMockApp()
  app.request = () => Promise.resolve({
    access_token: 'test-token-123',
    user: { name: 'Test' },
  })
  const wx = createMockWx()
  const page = await loadPage(app, wx, 'pages/index/index.js')

  const pageInstance = createPageInstance(page, { account: 'test@example.com', password: 'password123', loading: false, errorMsg: '' })

  await pageInstance.onLogin.call(pageInstance)

  // 验证跳转到 tabBar
  const switchTabCall = wx.calls.find((c) => c.type === 'switchTab')
  assert.ok(switchTabCall, '应调用 wx.switchTab')
  assert.match(switchTabCall.url, /pages\/chat\/chat/)
  // loading 应回到 false
  assert.strictEqual(pageInstance.data.loading, false)
})

test('login failure shows error message', async () => {
  const app = createMockApp()
  app.request = () => Promise.reject(new Error('登录失败：账号或密码错误'))
  const wx = createMockWx()
  const page = await loadPage(app, wx, 'pages/index/index.js')

  const pageInstance = createPageInstance(page, { account: 'test@example.com', password: 'wrongpass', loading: false, errorMsg: '' })

  await pageInstance.onLogin.call(pageInstance)

  assert.ok(pageInstance.data.errorMsg, '应显示错误消息')
  assert.match(pageInstance.data.errorMsg, /登录失败/)
  assert.strictEqual(pageInstance.data.loading, false)
})

test('token is written to global data after login', async () => {
  const app = createMockApp()
  app.request = () => Promise.resolve({
    access_token: 'global-token-xyz',
    user: { name: 'Test' },
  })
  const wx = createMockWx()
  const page = await loadPage(app, wx, 'pages/index/index.js')

  const pageInstance = createPageInstance(page, { account: 'test@example.com', password: 'password123', loading: false, errorMsg: '' })

  await pageInstance.onLogin.call(pageInstance)

  // token 写入 app.globalData
  assert.strictEqual(app.globalData.accessToken, 'global-token-xyz')
  // user info 写入 globalData
  assert.ok(app.globalData.userInfo, 'userInfo 应写入 globalData')
  // token 也写入 storage
  assert.strictEqual(app._storage['access_token'], 'global-token-xyz')
})

test('401 error is handled and displayed', async () => {
  const app = createMockApp()
  app.request = () => Promise.reject(new Error('Invalid credentials (401)'))
  const wx = createMockWx()
  const page = await loadPage(app, wx, 'pages/index/index.js')

  const pageInstance = createPageInstance(page, { account: 'test@example.com', password: 'badpass', loading: false, errorMsg: '' })

  await pageInstance.onLogin.call(pageInstance)

  assert.ok(pageInstance.data.errorMsg, '应显示 401 错误消息')
  assert.match(pageInstance.data.errorMsg, /401|credentials|Invalid/i)
})

test('network error is handled and displayed', async () => {
  const app = createMockApp()
  app.request = () => Promise.reject(new Error('Network error'))
  const wx = createMockWx()
  const page = await loadPage(app, wx, 'pages/index/index.js')

  const pageInstance = createPageInstance(page, { account: 'test@example.com', password: 'password123', loading: false, errorMsg: '' })

  await pageInstance.onLogin.call(pageInstance)

  assert.ok(pageInstance.data.errorMsg, '应显示网络错误消息')
  assert.match(pageInstance.data.errorMsg, /Network|网络/)
})

test('loading state toggles during login', async () => {
  const app = createMockApp()
  let resolveRequest
  app.request = () => new Promise((resolve) => { resolveRequest = resolve })
  const wx = createMockWx()
  const page = await loadPage(app, wx, 'pages/index/index.js')

  const loadingStates = []
  const pageInstance = createPageInstance(
    page,
    { account: 'test@example.com', password: 'password123', loading: false, errorMsg: '' },
    (updates) => {
      Object.assign(pageInstance.data, updates)
      if ('loading' in updates) loadingStates.push(updates.loading)
    },
  )

  const loginPromise = pageInstance.onLogin.call(pageInstance)

  // loading 应已设为 true
  assert.strictEqual(pageInstance.data.loading, true)
  assert.ok(loadingStates.includes(true), '应设置 loading=true')

  // 完成请求
  resolveRequest({ access_token: 'tok', user: {} })
  await loginPromise

  // loading 应回到 false
  assert.strictEqual(pageInstance.data.loading, false)
  assert.ok(loadingStates.includes(false), '应设置 loading=false')
})

test('logout clears global data and navigates to login page', async () => {
  const app = createMockApp()
  app.globalData.accessToken = 'existing-token'
  app.globalData.userInfo = { name: 'Test User' }
  app._storage['access_token'] = 'existing-token'

  const wx = createMockWx()
  const page = await loadPage(app, wx, 'pages/me/me.js')

  const pageInstance = createPageInstance(page, { userInfo: {}, userInitial: '?' })

  pageInstance.onLogout.call(pageInstance)

  // token 已清除
  assert.strictEqual(app.globalData.accessToken, null, 'accessToken 应被清除')
  assert.strictEqual(app.globalData.userInfo, null, 'userInfo 应被清除')
  assert.ok(!app._storage['access_token'], 'storage 中的 token 应被清除')

  // 应跳转到登录页
  const reLaunchCall = wx.calls.find((c) => c.type === 'reLaunch')
  assert.ok(reLaunchCall, '应调用 wx.reLaunch')
  assert.match(reLaunchCall.url, /pages\/index\/index/)
})
