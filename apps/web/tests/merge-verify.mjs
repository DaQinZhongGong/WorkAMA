/**
 * WorkAMA R212 Round 1 合并验证脚本。
 *
 * 在所有子 Agent 完成页面提升后运行：
 *   1. TypeScript 类型检查
 *   2. Vitest 单元测试
 *   3. Docker web 镜像重建
 *   4. 容器重启
 *   5. 全量 UI 截图（round1-post）
 *   6. 前后对比报告
 *
 * 运行：
 *   node tests/merge-verify.mjs
 */
import { execSync } from 'node:child_process'
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs'
import path from 'node:path'

const ROOT = path.resolve(__dirname, '..', '..')
const WEB = path.join(ROOT, 'apps', 'web')
const EVIDENCE = path.join(ROOT, 'quality', 'evidence')

function run(cmd, opts = {}) {
  console.log(`\n▶ ${cmd}`)
  try {
    const out = execSync(cmd, { encoding: 'utf8', stdio: 'pipe', cwd: opts.cwd || ROOT, ...opts })
    return { ok: true, out: out.trim() }
  } catch (err) {
    return { ok: false, out: (err.stdout || '').trim(), err: (err.stderr || '').trim() }
  }
}

const report = {
  timestamp: new Date().toISOString(),
  phase: 'R212-Round1-Merge',
  checks: {},
  summary: '',
}

console.log('=== WorkAMA R212 Round 1 Merge Verification ===\n')

// Phase 1: TypeScript
console.log('--- Phase 1: TypeScript Type Check ---')
const tsc = run('npx tsc --noEmit', { cwd: WEB, timeout: 120_000 })
report.checks.typescript = { ok: tsc.ok, output: (tsc.err || tsc.out).slice(0, 2000) }
console.log(tsc.ok ? '✅ TypeScript OK' : `❌ TypeScript errors:\n${tsc.err || tsc.out}`)

// Phase 2: Vitest
console.log('\n--- Phase 2: Vitest Unit Tests ---')
const vitest = run('npx vitest run --passWithNoTests', { cwd: WEB, timeout: 180_000 })
report.checks.vitest = { ok: vitest.ok, output: vitest.out.slice(0, 3000) }
console.log(vitest.ok ? '✅ Vitest OK' : `❌ Vitest failed:\n${vitest.out}`)

// Phase 3: Docker Build
console.log('\n--- Phase 3: Docker Build Web Image ---')
const build = run('docker compose -p workama -f deploy/compose/docker-compose.yml build web', { timeout: 600_000 })
report.checks.dockerBuild = { ok: build.ok, output: build.out.slice(0, 2000) }
console.log(build.ok ? '✅ Docker Build OK' : `❌ Docker Build failed:\n${build.out.slice(0, 500)}`)

if (!build.ok) {
  report.summary = 'BLOCKED: Docker build failed'
  writeFileSync(path.join(EVIDENCE, 'merge-report.json'), JSON.stringify(report, null, 2))
  process.exit(1)
}

// Phase 4: Restart
console.log('\n--- Phase 4: Restart Web Container ---')
const up = run('docker compose -p workama -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.override.yml up -d web', { timeout: 60_000 })
report.checks.restart = { ok: up.ok }
console.log(up.ok ? '✅ Container restarted' : `❌ Restart failed`)

// Wait for healthy
console.log('\n--- Waiting for web to be ready ---')
await new Promise((r) => setTimeout(r, 5000))
const health = run('curl -s -o /dev/null -w "%{http_code}" http://localhost:20204/')
console.log(`HTTP status: ${health.out}`)
report.checks.health = { ok: health.out === '200', status: health.out }

if (health.out !== '200') {
  report.summary = 'BLOCKED: Web not healthy after restart'
  writeFileSync(path.join(EVIDENCE, 'merge-report.json'), JSON.stringify(report, null, 2))
  process.exit(1)
}

// Phase 5: UI Capture (post-agent)
console.log('\n--- Phase 5: UI Capture (post-agent) ---')
const captureTag = 'round1-post-agent'
mkdirSync(path.join(EVIDENCE, 'ui-capture', captureTag), { recursive: true })

// Import and run ui-capture
try {
  // Dynamic import of ui-capture which uses playwright-core
  const capture = await import(path.join(WEB, 'tests', 'ui-capture.mjs'))
  // The capture script runs automatically when imported
  // We set env vars before import
  process.env.UI_CAPTURE_TAG = captureTag
  process.env.BROWSER_BASE_URL = 'http://localhost:20204'
  
  // Re-run by spawning a separate process
  const capResult = run(`UI_CAPTURE_TAG=${captureTag} node tests/ui-capture.mjs`, { cwd: WEB, timeout: 180_000 })
  report.checks.uiCapture = { ok: capResult.ok, output: capResult.out.slice(0, 1000) }
  console.log(capResult.ok ? '✅ UI Capture done' : `⚠️ UI Capture issues:\n${capResult.out}`)
} catch (e) {
  report.checks.uiCapture = { ok: false, error: String(e).slice(0, 500) }
  console.log(`⚠️ UI Capture error: ${e}`)
}

// Summary
const allOk = Object.values(report.checks).every((c) => c.ok)
report.summary = allOk
  ? 'ALL CHECKS PASSED — Round 1 merge verification complete'
  : 'SOME CHECKS FAILED — review individual results'

console.log(`\n=== SUMMARY: ${report.summary} ===`)
writeFileSync(path.join(EVIDENCE, 'merge-report.json'), JSON.stringify(report, null, 2))

process.exit(allOk ? 0 : 1)
