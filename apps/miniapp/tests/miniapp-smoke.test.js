import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile, access } from 'node:fs/promises'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const root = join(__dirname, '..')

async function read(path) {
  return readFile(join(root, path), 'utf8')
}

async function exists(path) {
  try {
    await access(join(root, path))
    return true
  } catch {
    return false
  }
}

test('app.json exists and declares required pages', async () => {
  const source = await read('app.json')
  const config = JSON.parse(source)
  assert.ok(Array.isArray(config.pages))
  assert.ok(config.pages.includes('pages/index/index'))
  assert.ok(config.pages.includes('pages/chat/chat'))
  assert.ok(config.pages.includes('pages/me/me'))
})

test('app.json has tabBar with three items', async () => {
  const source = await read('app.json')
  const config = JSON.parse(source)
  assert.ok(config.tabBar)
  assert.strictEqual(config.tabBar.list.length, 3)
  const paths = config.tabBar.list.map((i) => i.pagePath)
  assert.ok(paths.includes('pages/index/index'))
  assert.ok(paths.includes('pages/chat/chat'))
  assert.ok(paths.includes('pages/me/me'))
})

test('app.js exists and exports global request helper', async () => {
  const source = await read('app.js')
  assert.match(source, /App\(/)
  assert.match(source, /request\(/)
  assert.match(source, /setToken/)
  assert.match(source, /getToken/)
  assert.match(source, /clearToken/)
})

test('login page files exist and call auth endpoint', async () => {
  assert.ok(await exists('pages/index/index.wxml'))
  assert.ok(await exists('pages/index/index.wxss'))
  assert.ok(await exists('pages/index/index.js'))
  assert.ok(await exists('pages/index/index.json'))

  const js = await read('pages/index/index.js')
  assert.match(js, /\/api\/v1\/auth\/login/)
  assert.match(js, /app\.request/)
})

test('chat page files exist and use WebSocket', async () => {
  assert.ok(await exists('pages/chat/chat.wxml'))
  assert.ok(await exists('pages/chat/chat.wxss'))
  assert.ok(await exists('pages/chat/chat.js'))
  assert.ok(await exists('pages/chat/chat.json'))

  const js = await read('pages/chat/chat.js')
  assert.match(js, /wx\.connectSocket/)
  assert.match(js, /ws:\/\/localhost:20201/)
})

test('me page files exist', async () => {
  assert.ok(await exists('pages/me/me.wxml'))
  assert.ok(await exists('pages/me/me.wxss'))
  assert.ok(await exists('pages/me/me.js'))
  assert.ok(await exists('pages/me/me.json'))
})

test('login page wxml contains account and password inputs', async () => {
  const wxml = await read('pages/index/index.wxml')
  assert.match(wxml, /bindinput/)
  assert.match(wxml, /account/)
  assert.match(wxml, /password/)
  assert.match(wxml, /bindtap="onLogin"/)
})

test('chat page wxml contains message list and input bar', async () => {
  const wxml = await read('pages/chat/chat.wxml')
  assert.match(wxml, /message-list/)
  assert.match(wxml, /bindinput/)
  assert.match(wxml, /sendMessage/)
})

test('me page js handles logout and clears token', async () => {
  const js = await read('pages/me/me.js')
  assert.match(js, /app\.clearToken/)
  assert.match(js, /wx\.reLaunch/)
})

test('global styles exist in app.wxss', async () => {
  const wxss = await read('app.wxss')
  assert.match(wxss, /\.container/)
  assert.match(wxss, /\.primary-btn/)
  assert.match(wxss, /\.message/)
})
