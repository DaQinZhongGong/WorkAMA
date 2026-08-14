import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'

test('miniapp uses the React adapter and memory-only provider boundary', async () => {
  const source = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8')
  assert.match(source, /react-miniapp-adapter|miniapp\/bootstrap/)
  assert.match(source, /memory|pending_external/)
})

test('miniapp does not persist access tokens', async () => {
  const source = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /localStorage|sessionStorage|indexedDB/)
})
