import assert from 'node:assert/strict'
import test from 'node:test'
import { redactSensitiveText } from '../src/shared/safety.ts'
import { normalizeBaseUrl } from '../src/shared/storage.ts'

test('redactSensitiveText redacts Bearer tokens', () => {
  const input = 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.SflKxwRJSMeKKF2QT4f'
  const result = redactSensitiveText(input)
  assert.equal(result.includes('eyJhbGci'), false)
  assert.ok(result.includes('[REDACTED]'))
})

test('redactSensitiveText redacts API key prefixes (sk-, rk-, ghp-, xox-)', () => {
  const input = 'key=sk-proj-abc123def456ghi789jkl012mno345pqr678stu901vwx234'
  const result = redactSensitiveText(input)
  assert.equal(result.includes('sk-proj-abc123'), false)
  assert.ok(result.includes('[REDACTED]'))
})

test('redactSensitiveText redacts GitHub/AWS/Slack token prefixes', () => {
  const samples = [
    'ghp_0123456789abcdef0123',
    'github_pat_0123456789abcdef',
    'xoxb-0123456789012-abcdef',
    'AKIA0123456789ABCDEF',
    'AIzaSyA0123456789abcdef',
  ]
  for (const token of samples) {
    const result = redactSensitiveText(`token: ${token}`)
    assert.equal(result.includes(token), false, `expected ${token} to be redacted`)
    assert.ok(result.includes('[REDACTED]'))
  }
})

test('redactSensitiveText redacts password/secret/token assignments', () => {
  const cases = [
    'password=hunter2-value',
    'secret: "my-secret-value"',
    'token=abc123def456',
    'api_key=AKIAIOSFODNN7EXAMPLE',
    'api-key: secret123',
  ]
  for (const input of cases) {
    const result = redactSensitiveText(input)
    assert.ok(result.includes('[REDACTED]'), `expected redaction in: ${input}`)
  }
})

test('redactSensitiveText redacts credit-card-like digit sequences', () => {
  const input = 'card 4111 1111 1111 1111 expires 12/30'
  const result = redactSensitiveText(input)
  assert.equal(result.includes('4111 1111 1111 1111'), false)
})

test('redactSensitiveText leaves normal text untouched', () => {
  const input = 'The quick brown fox jumps over the lazy dog.'
  assert.equal(redactSensitiveText(input), input)
})

test('redactSensitiveText handles empty strings and strings without secrets', () => {
  assert.equal(redactSensitiveText(''), '')
  assert.equal(redactSensitiveText('Hello world 12345'), 'Hello world 12345')
})

test('redactSensitiveText redacts multiple secrets in a single string', () => {
  const input = 'token=sk-abc123def456ghi789jkl and password=hunter2'
  const result = redactSensitiveText(input)
  assert.equal(result.includes('sk-abc123'), false)
  assert.equal(result.includes('hunter2'), false)
  assert.ok(result.includes('[REDACTED]'))
})

test('normalizeBaseUrl accepts http URLs', () => {
  assert.equal(normalizeBaseUrl('http://localhost:3000'), 'http://localhost:3000')
  assert.equal(normalizeBaseUrl('http://example.com'), 'http://example.com')
})

test('normalizeBaseUrl accepts https URLs', () => {
  assert.equal(normalizeBaseUrl('https://example.com'), 'https://example.com')
  assert.equal(normalizeBaseUrl('https://workama.ai/api'), 'https://workama.ai/api')
})

test('normalizeBaseUrl trims surrounding whitespace', () => {
  assert.equal(normalizeBaseUrl('  https://example.com  '), 'https://example.com')
})

test('normalizeBaseUrl strips a single trailing slash', () => {
  assert.equal(normalizeBaseUrl('https://example.com/'), 'https://example.com')
  assert.equal(normalizeBaseUrl('http://localhost:3000/'), 'http://localhost:3000')
})

test('normalizeBaseUrl is case-insensitive for the protocol', () => {
  assert.equal(normalizeBaseUrl('HTTPS://example.com'), 'HTTPS://example.com')
  assert.equal(normalizeBaseUrl('HtTp://localhost:3000'), 'HtTp://localhost:3000')
})

test('normalizeBaseUrl throws on non-http(s) protocols', () => {
  assert.throws(() => normalizeBaseUrl('ftp://files.example.com'), /http\(s\)/)
  assert.throws(() => normalizeBaseUrl('file:///etc/passwd'), /http\(s\)/)
  assert.throws(() => normalizeBaseUrl('ws://sockets.example.com'), /http\(s\)/)
})

test('normalizeBaseUrl throws when the protocol is missing', () => {
  assert.throws(() => normalizeBaseUrl('example.com'), /http\(s\)/)
  assert.throws(() => normalizeBaseUrl('localhost:3000'), /http\(s\)/)
})
