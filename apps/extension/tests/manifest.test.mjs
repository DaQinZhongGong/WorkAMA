import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const root = new URL('../', import.meta.url)
const manifest = JSON.parse(await readFile(new URL('manifest.json', root), 'utf8'))
const builtManifest = JSON.parse(await readFile(new URL('public/manifest.json', root), 'utf8'))
const background = await readFile(new URL('src/background.ts', root), 'utf8')
const sourceFiles = await Promise.all([
  readFile(new URL('src/shared/storage.ts', root), 'utf8'),
  readFile(new URL('src/shared/capture.ts', root), 'utf8'),
  readFile(new URL('src/shared/safety.ts', root), 'utf8'),
  readFile(new URL('src/sidepanel/main.tsx', root), 'utf8'),
  readFile(new URL('src/popup/main.tsx', root), 'utf8'),
  readFile(new URL('src/options/main.tsx', root), 'utf8'),
])
const source = sourceFiles.join('\n')

test('manifest keeps page access user-triggered', () => {
  assert.equal(manifest.manifest_version, 3)
  assert.ok(manifest.permissions.includes('activeTab'))
  assert.ok(manifest.permissions.includes('scripting'))
  assert.equal(manifest.action.default_popup, 'src/popup.html')
  assert.equal(manifest.side_panel.default_path, 'src/sidepanel.html')
  assert.equal(manifest.options_ui.page, 'src/options.html')
  assert.equal('host_permissions' in manifest, false)
  assert.equal('content_scripts' in manifest, false)
  assert.deepEqual(builtManifest, manifest)
})

test('capture rejects non-web pages and sensitive inputs', () => {
  assert.match(background, /startsWith\('https:\/\/'\)/)
  assert.match(background, /startsWith\('http:\/\/'\)/)
  assert.match(background, /type === 'password'/)
  assert.match(background, /MAX_SELECTION_LENGTH = 8000/)
  assert.match(background, /Bearer/)
  assert.match(source, /redactSensitiveText/)
})

test('credentials and captured context use session storage only', () => {
  assert.match(source, /chrome\.storage\.session\.get/)
  assert.match(source, /chrome\.storage\.session\.set/)
  assert.match(source, /chrome\.storage\.session\.clear/)
  assert.equal(source.includes('localStorage'), false)
  assert.equal(source.includes('storage.local'), false)
  assert.equal(source.includes('document.cookie'), false)
})

test('all React page entry points exist and old HTML/JS pages are removed', async () => {
  for (const entry of ['src/sidepanel/main.tsx', 'src/popup/main.tsx', 'src/options/main.tsx']) {
    await readFile(new URL(entry, root), 'utf8')
  }
  await assert.rejects(readFile(new URL('src/sidepanel.js', root)))
  assert.match(await readFile(new URL('src/sidepanel.html', root), 'utf8'), /sidepanel\/main\.tsx/)
})
