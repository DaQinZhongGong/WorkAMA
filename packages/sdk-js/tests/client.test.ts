/**
 * WorkAMA JavaScript SDK 单元测试。
 *
 * 通过 vi.stubGlobal('fetch', ...) 替换全局 fetch，
 * 不发起真实网络请求。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  WorkAMAClient,
  WorkAMAError,
  AuthenticationError,
  ForbiddenError,
  NotFoundError,
  RateLimitError,
  buildMultipart,
} from '../src/client'

// ---------------------------------------------------------------------------
// Mock 基础设施
// ---------------------------------------------------------------------------

/** 构造一个成功的 fetch 响应。 */
function mockOk(json: unknown, status = 200): Response {
  const text = typeof json === 'string' ? json : JSON.stringify(json)
  return {
    ok: true,
    status,
    statusText: 'OK',
    text: () => Promise.resolve(text),
    json: () => Promise.resolve(json),
    headers: new Headers(),
  } as unknown as Response
}

/** 构造一个失败的 fetch 响应。 */
function mockError(
  status: number,
  json: unknown,
  statusText = 'error',
): Response {
  const text = typeof json === 'string' ? json : JSON.stringify(json)
  return {
    ok: false,
    status,
    statusText,
    text: () => Promise.resolve(text),
    json: () => Promise.resolve(json),
    headers: new Headers(),
  } as unknown as Response
}

/** 从 mock 调用中取出 fetch 的 Request 参数。 */
function getCall(fetchMock: ReturnType<typeof vi.fn>, index = 0) {
  return fetchMock.mock.calls[index]
}

// ---------------------------------------------------------------------------
// 测试套件
// ---------------------------------------------------------------------------

describe('WorkAMAClient', () => {
  const fetchMock = vi.fn()

  beforeEach(() => {
    fetchMock.mockReset()
    vi.stubGlobal('fetch', fetchMock)
    // 防止 setTimeout 超时挂起测试
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  // ---------------- 初始化与鉴权 ----------------

  describe('constructor & auth header', () => {
    it('trims trailing slashes from baseUrl', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({}))
      const client = new WorkAMAClient({ baseUrl: 'http://localhost:20200///', apiKey: 'wk' })
      await client.listAgents()
      const [url] = getCall(fetchMock)
      expect(String(url)).toBe('http://localhost:20200/api/v1/assistants?limit=20')
    })

    it('sets X-WorkAMA-API-Key header when apiKey provided', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({}))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'wk_secret' })
      await client.listAgents()
      const [, init] = getCall(fetchMock)
      const headers = new Headers(init.headers as HeadersInit)
      expect(headers.get('X-WorkAMA-API-Key')).toBe('wk_secret')
      expect(headers.get('Authorization')).toBeNull()
    })

    it('prefers accessToken (Bearer) over apiKey when both set', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({}))
      const client = new WorkAMAClient({
        baseUrl: 'http://x',
        apiKey: 'wk_x',
        accessToken: 'tok_abc',
      })
      await client.listAgents()
      const [, init] = getCall(fetchMock)
      const headers = new Headers(init.headers as HeadersInit)
      expect(headers.get('Authorization')).toBe('Bearer tok_abc')
      expect(headers.get('X-WorkAMA-API-Key')).toBeNull()
    })

    it('throws WorkAMAError when baseUrl missing', () => {
      expect(() => new WorkAMAClient({ baseUrl: '' })).toThrow(WorkAMAError)
    })

    it('sets default timeout to 30000ms', () => {
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      expect(client.timeout).toBe(30000)
    })
  })

  // ---------------- chat ----------------

  describe('chat', () => {
    it('sends POST with body and session_id', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ agent_id: 'a1', message: 'hi' }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', accessToken: 't' })
      const resp = await client.chat('a1', 'hello', { sessionId: 's1' })

      expect(resp).toEqual({ agent_id: 'a1', message: 'hi' })
      const [url, init] = getCall(fetchMock)
      expect(String(url)).toBe('http://x/api/v1/agents/a1/chat')
      expect(init.method).toBe('POST')
      expect(JSON.parse(init.body)).toEqual({
        message: 'hello',
        stream: false,
        session_id: 's1',
      })
      const headers = new Headers(init.headers as HeadersInit)
      expect(headers.get('Content-Type')).toBe('application/json')
    })

    it('omits session_id when not provided', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({}))
      const client = new WorkAMAClient({ baseUrl: 'http://x', accessToken: 't' })
      await client.chat('a1', 'hi')
      const [, init] = getCall(fetchMock)
      const body = JSON.parse(init.body)
      expect(body.session_id).toBeUndefined()
      expect(body.stream).toBe(false)
    })

    it('propagates stream=true', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({}))
      const client = new WorkAMAClient({ baseUrl: 'http://x', accessToken: 't' })
      await client.chat('a1', 'hi', { stream: true })
      const [, init] = getCall(fetchMock)
      expect(JSON.parse(init.body).stream).toBe(true)
    })

    it('maps 401 to AuthenticationError', async () => {
      fetchMock.mockResolvedValueOnce(mockError(401, { detail: 'invalid token' }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', accessToken: 'bad' })
      await expect(client.chat('a1', 'hi')).rejects.toMatchObject({
        name: 'AuthenticationError',
        statusCode: 401,
        body: { detail: 'invalid token' },
      })
    })
  })

  // ---------------- listAgents ----------------

  describe('listAgents', () => {
    it('sends GET with default limit=20 to /api/v1/assistants', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ items: [], total: 0 }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      const resp = await client.listAgents()
      expect(resp).toEqual({ items: [], total: 0 })
      const [url, init] = getCall(fetchMock)
      expect(String(url)).toBe('http://x/api/v1/assistants?limit=20')
      expect(init.method).toBe('GET')
    })

    it('passes limit and cursor as query params', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({}))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      await client.listAgents({ limit: 10, cursor: 'abc' })
      const [url] = getCall(fetchMock)
      expect(String(url)).toContain('limit=10')
      expect(String(url)).toContain('cursor=abc')
    })
  })

  // ---------------- memory ----------------

  describe('memory', () => {
    it('createMemory sends default body', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ id: 'm1' }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      const resp = await client.createMemory('hello')
      expect(resp).toEqual({ id: 'm1' })
      const [url, init] = getCall(fetchMock)
      expect(String(url)).toBe('http://x/api/v1/memory-vectors')
      expect(JSON.parse(init.body)).toEqual({ content: 'hello', importance: 3 })
    })

    it('createMemory forwards metadata and importance', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ id: 'm2' }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      await client.createMemory('prefers dark', {
        metadata: { category: 'ui' },
        importance: 5,
      })
      const [, init] = getCall(fetchMock)
      expect(JSON.parse(init.body)).toEqual({
        content: 'prefers dark',
        importance: 5,
        metadata: { category: 'ui' },
      })
    })

    it('recallMemory posts query and limit', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ items: [{ content: 'x' }] }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      const resp = await client.recallMemory('q', { limit: 8 })
      expect(resp).toEqual({ items: [{ content: 'x' }] })
      const [url, init] = getCall(fetchMock)
      expect(String(url)).toBe('http://x/api/v1/memory-vectors/recall')
      expect(JSON.parse(init.body)).toEqual({ query: 'q', limit: 8 })
    })
  })

  // ---------------- knowledge ----------------

  describe('knowledge', () => {
    it('searchKnowledge forwards datasetId when provided', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ items: [], total: 0 }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      const resp = await client.searchKnowledge('pricing', { datasetId: 'ds1', limit: 5 })
      expect(resp).toEqual({ items: [], total: 0 })
      const [url, init] = getCall(fetchMock)
      expect(String(url)).toBe('http://x/api/v1/knowledge/search')
      expect(JSON.parse(init.body)).toEqual({
        query: 'pricing',
        limit: 5,
        dataset_id: 'ds1',
      })
    })

    it('searchKnowledge omits datasetId by default', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ items: [] }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      await client.searchKnowledge('q')
      const [, init] = getCall(fetchMock)
      expect(JSON.parse(init.body).dataset_id).toBeUndefined()
    })
  })

  // ---------------- workflows ----------------

  describe('workflows', () => {
    it('listWorkflows sends GET with limit to /api/v1/workflows', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ items: [{ id: 'w1' }] }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      const resp = await client.listWorkflows({ limit: 20 })
      expect(resp).toEqual({ items: [{ id: 'w1' }] })
      const [url, init] = getCall(fetchMock)
      expect(init.method).toBe('GET')
      expect(String(url)).toContain('limit=20')
      expect(String(url)).toContain('/api/v1/workflows')
      expect(String(url)).not.toContain('/api/v1/workflows-v2')
    })

    it('runWorkflow posts inputs wrapped as { input: ... }', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ run_id: 'r1', status: 'succeeded' }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      const resp = await client.runWorkflow('w1', { topic: '周报' })
      expect(resp).toEqual({ run_id: 'r1', status: 'succeeded' })
      const [url, init] = getCall(fetchMock)
      expect(init.method).toBe('POST')
      expect(String(url)).toBe('http://x/api/v1/workflows/w1/runs')
      expect(JSON.parse(init.body)).toEqual({ input: { topic: '周报' } })
    })

    it('runWorkflow forwards idempotencyKey header', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ run_id: 'r2' }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      await client.runWorkflow('w1', {}, { idempotencyKey: 'idem-abc' })
      const [, init] = getCall(fetchMock)
      const headers = new Headers(init.headers as HeadersInit)
      expect(headers.get('Idempotency-Key')).toBe('idem-abc')
    })
  })

  // ---------------- 错误处理 ----------------

  describe('error handling', () => {
    it('maps 404 to NotFoundError', async () => {
      fetchMock.mockResolvedValueOnce(mockError(404, { detail: 'agent not found' }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      await expect(client.chat('missing', 'hi')).rejects.toMatchObject({
        name: 'NotFoundError',
        statusCode: 404,
        body: { detail: 'agent not found' },
      })
    })

    it('maps 429 to RateLimitError', async () => {
      fetchMock.mockResolvedValueOnce(mockError(429, { message: 'too many requests' }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      await expect(client.listAgents()).rejects.toMatchObject({
        name: 'RateLimitError',
        statusCode: 429,
      })
    })

    it('maps 500 to generic WorkAMAError (not subclass)', async () => {
      fetchMock.mockResolvedValueOnce(mockError(500, { error: 'boom' }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      const err = await client.listAgents().catch((e) => e)
      expect(err).toBeInstanceOf(WorkAMAError)
      expect(err).not.toBeInstanceOf(AuthenticationError)
      expect(err).not.toBeInstanceOf(ForbiddenError)
      expect(err).not.toBeInstanceOf(NotFoundError)
      expect(err).not.toBeInstanceOf(RateLimitError)
      expect(err.statusCode).toBe(500)
      expect(err.body).toEqual({ error: 'boom' })
    })

    it('maps 403 to ForbiddenError', async () => {
      fetchMock.mockResolvedValueOnce(mockError(403, { detail: 'missing scope' }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      await expect(client.listAgents()).rejects.toMatchObject({
        name: 'ForbiddenError',
        statusCode: 403,
        body: { detail: 'missing scope' },
      })
    })

    it('maps network failure to WorkAMAError with undefined status', async () => {
      fetchMock.mockRejectedValueOnce(new TypeError('failed to fetch'))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      const err = await client.listAgents().catch((e) => e)
      expect(err).toBeInstanceOf(WorkAMAError)
      expect(err.statusCode).toBeUndefined()
      expect(err.message).toContain('network error')
    })

    it('prefers body.message over statusText for error message', async () => {
      fetchMock.mockResolvedValueOnce(
        mockError(401, { message: 'token expired' }, 'Unauthorized'),
      )
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      const err = await client.listAgents().catch((e) => e)
      expect(err.message).toBe('token expired')
    })

    it('uses AbortController timeout when request exceeds timeoutMs', async () => {
      // fetch 返回一个永不 resolve 的 Promise，等待被 abort 中断
      fetchMock.mockImplementationOnce((_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => {
            const e = new DOMException('aborted', 'AbortError')
            reject(e)
          })
        }),
      )
      const client = new WorkAMAClient({
        baseUrl: 'http://x',
        apiKey: 'k',
        timeout: 50,
      })
      // 异步触发定时器
      const promise = client.listAgents()
      // 先附加 catch handler，避免 abort 触发的 rejection 被报为 unhandled
      const caught = promise.catch((e) => e)
      await vi.advanceTimersByTimeAsync(60)
      const err = await caught
      expect(err).toBeInstanceOf(WorkAMAError)
      expect(err.message).toContain('timed out')
    })
  })

  // ---------------- P2: createAgent ----------------

  describe('createAgent', () => {
    it('posts payload to /api/v1/assistants', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ id: 'a1', name: 'demo' }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', accessToken: 't' })
      const payload = { name: 'demo', system_prompt: 'hi', model: 'gpt-4o-mini' }
      const resp = await client.createAgent(payload)
      expect(resp).toEqual({ id: 'a1', name: 'demo' })
      const [url, init] = getCall(fetchMock)
      expect(init.method).toBe('POST')
      expect(String(url)).toBe('http://x/api/v1/assistants')
      expect(JSON.parse(init.body)).toEqual(payload)
    })

    it('forwards workspaceId as X-Workspace-Id header', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ id: 'a1' }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', accessToken: 't' })
      await client.createAgent({ name: 'x' }, { workspaceId: 'ws_123' })
      const [, init] = getCall(fetchMock)
      const headers = new Headers(init.headers as HeadersInit)
      expect(headers.get('X-Workspace-Id')).toBe('ws_123')
    })
  })

  // ---------------- P2: sendChatMessage ----------------

  describe('sendChatMessage', () => {
    it('posts user_message to /api/v1/assistants/{id}/run', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ id: 'run1', assistant_message: 'hi' }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', accessToken: 't' })
      const resp = await client.sendChatMessage('a1', '你好')
      expect(resp).toEqual({ id: 'run1', assistant_message: 'hi' })
      const [url, init] = getCall(fetchMock)
      expect(init.method).toBe('POST')
      expect(String(url)).toBe('http://x/api/v1/assistants/a1/run')
      const body = JSON.parse(init.body)
      expect(body.user_message).toBe('你好')
      expect(body.metadata).toBeUndefined()
    })

    it('wraps conversationId into metadata.conversation_id', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ id: 'run2' }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', accessToken: 't' })
      await client.sendChatMessage('a1', '续接', { conversationId: 'conv_abc' })
      const body = JSON.parse(getCall(fetchMock)[1].body)
      expect(body.metadata).toEqual({ conversation_id: 'conv_abc' })
    })
  })

  // ---------------- P2: createWorkflow ----------------

  describe('createWorkflow', () => {
    it('posts payload to /api/v1/workflows', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ id: 'wf1', name: 'demo' }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', accessToken: 't' })
      const payload = { name: 'demo', graph: { nodes: [], edges: [] } }
      const resp = await client.createWorkflow(payload, { workspaceId: 'ws_1' })
      expect(resp).toEqual({ id: 'wf1', name: 'demo' })
      const [url, init] = getCall(fetchMock)
      expect(init.method).toBe('POST')
      expect(String(url)).toBe('http://x/api/v1/workflows')
      expect(JSON.parse(init.body)).toEqual(payload)
      const headers = new Headers(init.headers as HeadersInit)
      expect(headers.get('X-Workspace-Id')).toBe('ws_1')
    })
  })

  // ---------------- P2: knowledge bases ----------------

  describe('knowledge bases', () => {
    it('listKnowledgeBases sends GET to /api/v1/knowledge-bases', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ items: [{ id: 'kb1' }] }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      const resp = await client.listKnowledgeBases({ workspaceId: 'ws_1', limit: 10 })
      expect(resp).toEqual({ items: [{ id: 'kb1' }] })
      const [url, init] = getCall(fetchMock)
      expect(init.method).toBe('GET')
      expect(String(url)).toContain('limit=10')
      expect(String(url)).toContain('/api/v1/knowledge-bases')
    })

    it('createKnowledgeBase posts payload', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ id: 'kb1', name: 'docs' }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', accessToken: 't' })
      const payload = { name: 'docs', kind: 'vector' }
      const resp = await client.createKnowledgeBase(payload)
      expect(resp).toEqual({ id: 'kb1', name: 'docs' })
      const [url, init] = getCall(fetchMock)
      expect(String(url)).toBe('http://x/api/v1/knowledge-bases')
      expect(JSON.parse(init.body)).toEqual(payload)
    })

    it('ingestDocument uses provided title and promotes source_type', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ id: 'doc1' }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', accessToken: 't' })
      await client.ingestDocument('kb1', '正文', {
        title: '自定义标题',
        source_type: 'manual',
        metadata: { category: 'faq' },
      })
      const [url, init] = getCall(fetchMock)
      expect(String(url)).toBe('http://x/api/v1/knowledge-bases/kb1/documents')
      const body = JSON.parse(init.body)
      expect(body.content).toBe('正文')
      expect(body.title).toBe('自定义标题')
      expect(body.source_type).toBe('manual')
      expect(body.metadata).toEqual({ category: 'faq' })
    })

    it('ingestDocument generates default title when missing', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ id: 'doc2' }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', accessToken: 't' })
      await client.ingestDocument('kb1', '正文', { metadata: { tag: 'x' } })
      const body = JSON.parse(getCall(fetchMock)[1].body)
      expect(body.title).toMatch(/^doc-[0-9a-f]{8}$/)
      expect(body.metadata).toEqual({ tag: 'x' })
    })

    it('queryKnowledge posts query and top_k', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ results: [{ id: 'c1', similarity: 0.92 }] }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      const resp = await client.queryKnowledge('kb1', '如何接入', { topK: 3 })
      expect((resp as { results: unknown[] }).results[0]).toMatchObject({ similarity: 0.92 })
      const [url, init] = getCall(fetchMock)
      expect(init.method).toBe('POST')
      expect(String(url)).toBe('http://x/api/v1/knowledge-bases/kb1/rag/query')
      expect(JSON.parse(init.body)).toEqual({ query: '如何接入', top_k: 3 })
    })
  })

  // ---------------- P2: files ----------------

  describe('files', () => {
    it('listFiles sends GET to /api/v1/files', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ items: [{ id: 'f1', name: 'a.txt' }] }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      const resp = await client.listFiles({ workspaceId: 'ws_1' })
      expect(resp).toEqual({ items: [{ id: 'f1', name: 'a.txt' }] })
      const [url, init] = getCall(fetchMock)
      expect(init.method).toBe('GET')
      expect(String(url)).toContain('/api/v1/files')
      const headers = new Headers(init.headers as HeadersInit)
      expect(headers.get('X-Workspace-Id')).toBe('ws_1')
    })

    it('uploadFile sends multipart body with file/kind/metadata fields', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ id: 'f1', name: 'test.txt' }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', accessToken: 't' })
      const resp = await client.uploadFile('test.txt', new TextEncoder().encode('hello'), {
        kind: 'document',
        metadata: { category: 'note' },
      })
      expect(resp).toEqual({ id: 'f1', name: 'test.txt' })
      const [url, init] = getCall(fetchMock)
      expect(init.method).toBe('POST')
      expect(String(url)).toBe('http://x/api/v1/files/upload')
      const headers = new Headers(init.headers as HeadersInit)
      const contentType = headers.get('Content-Type') || ''
      expect(contentType).toMatch(/^multipart\/form-data; boundary=/)
      // body 是 Uint8Array，转换为字符串检查内容
      const bodyText = new TextDecoder().decode(init.body as Uint8Array)
      expect(bodyText).toContain('name="file"; filename="test.txt"')
      expect(bodyText).toContain('name="kind"')
      expect(bodyText).toContain('"category":"note"')
      expect(bodyText).toContain('hello')
    })
  })

  // ---------------- P2: automations ----------------

  describe('automations', () => {
    it('listAutomations sends GET to /api/v1/automations/v2/triggers', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ items: [{ trigger_id: 't1' }] }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      const resp = await client.listAutomations({ workspaceId: 'ws_1', limit: 5 })
      expect(resp).toEqual({ items: [{ trigger_id: 't1' }] })
      const [url, init] = getCall(fetchMock)
      expect(init.method).toBe('GET')
      expect(String(url)).toContain('limit=5')
      expect(String(url)).toContain('/api/v1/automations/v2/triggers')
    })

    it('createAutomation posts payload', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ trigger_id: 't2', name: 'cron' }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', accessToken: 't' })
      const payload = { name: 'cron', type: 'schedule', schedule: '0 9 * * *' }
      const resp = await client.createAutomation(payload)
      expect(resp).toEqual({ trigger_id: 't2', name: 'cron' })
      const [url, init] = getCall(fetchMock)
      expect(String(url)).toBe('http://x/api/v1/automations/v2/triggers')
      expect(JSON.parse(init.body)).toEqual(payload)
    })
  })

  // ---------------- P2: skills ----------------

  describe('skills', () => {
    it('listSkills sends GET to /api/v1/skills', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ items: [{ skill_id: 's1' }] }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      const resp = await client.listSkills()
      expect(resp).toEqual({ items: [{ skill_id: 's1' }] })
      const [url, init] = getCall(fetchMock)
      expect(init.method).toBe('GET')
      expect(String(url)).toContain('/api/v1/skills')
    })

    it('installSkill posts to marketplace subscribe endpoint', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({ skill_id: 's2', installed: true }))
      const client = new WorkAMAClient({ baseUrl: 'http://x', accessToken: 't' })
      const resp = await client.installSkill('s2', { workspaceId: 'ws_1' })
      expect(resp).toEqual({ skill_id: 's2', installed: true })
      const [url, init] = getCall(fetchMock)
      expect(init.method).toBe('POST')
      expect(String(url)).toBe('http://x/api/v1/skills/marketplace/s2/subscribe')
      const headers = new Headers(init.headers as HeadersInit)
      expect(headers.get('X-Workspace-Id')).toBe('ws_1')
    })
  })

  // ---------------- P2: workspace_id 透传 ----------------

  describe('workspace_id propagation', () => {
    it('adds X-Workspace-Id header on GET requests', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({}))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      await client.listWorkflows({ workspaceId: 'ws_test' })
      const [, init] = getCall(fetchMock)
      const headers = new Headers(init.headers as HeadersInit)
      expect(headers.get('X-Workspace-Id')).toBe('ws_test')
    })

    it('does not add X-Workspace-Id when workspaceId omitted', async () => {
      fetchMock.mockResolvedValueOnce(mockOk({}))
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      await client.listWorkflows()
      const [, init] = getCall(fetchMock)
      const headers = new Headers(init.headers as HeadersInit)
      expect(headers.get('X-Workspace-Id')).toBeNull()
    })
  })

  // ---------------- P2: buildMultipart 辅助函数 ----------------

  describe('buildMultipart', () => {
    it('constructs multipart body with boundary, file, kind, metadata', () => {
      const { contentType, body } = buildMultipart(
        'test.txt',
        new TextEncoder().encode('hello'),
        'document',
        { category: 'note' },
      )
      expect(contentType).toMatch(/^multipart\/form-data; boundary=----WorkAMABoundary/)
      const text = new TextDecoder().decode(body as Uint8Array)
      expect(text).toContain('name="file"; filename="test.txt"')
      expect(text).toContain('name="kind"')
      expect(text).toContain('document')
      expect(text).toContain('name="metadata"')
      expect(text).toContain('"category":"note"')
      expect(text).toContain('hello')
      // 以 boundary-- 结尾
      expect(text).toMatch(/------WorkAMABoundary[0-9a-f]+--\r\n$/)
    })

    it('omits kind and metadata fields when not provided', () => {
      const { body } = buildMultipart('data.bin', new Uint8Array([0, 1, 2]))
      const text = new TextDecoder().decode(body as Uint8Array)
      expect(text).not.toContain('name="kind"')
      expect(text).not.toContain('name="metadata"')
      expect(text).toContain('name="file"; filename="data.bin"')
    })
  })

  // ---------------- P2: close ----------------

  describe('close', () => {
    it('close is a safe no-op and idempotent', () => {
      const client = new WorkAMAClient({ baseUrl: 'http://x', apiKey: 'k' })
      expect(() => {
        client.close()
        client.close()
      }).not.toThrow()
    })
  })
})
