import { mkdir, readFile, writeFile } from 'node:fs/promises'

const outputDir = process.env.EVIDENCE_DIR ?? 'quality/evidence'
const outputFile = `${outputDir}/rag-smoke.json`
const timeoutMs = Number(process.env.RAG_SMOKE_TIMEOUT_MS ?? 30000)
const suffix = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`

async function loadDotEnv() {
  try {
    const content = await readFile('.env', 'utf8')
    return Object.fromEntries(
      content
        .split(/\r?\n/)
        .map((line) => line.match(/^\s*([^#=]+?)\s*=\s*(.*?)\s*$/))
        .filter(Boolean)
        .map((match) => [match[1], match[2].replace(/^(['"])(.*)\1$/, '$2')]),
    )
  } catch {
    return {}
  }
}

const dotEnv = await loadDotEnv()
const env = (name, fallback = undefined) => process.env[name] ?? dotEnv[name] ?? fallback
const baseUrl = String(env('RAG_PLATFORM_API_URL', env('PLATFORM_API_URL', env('VITE_PLATFORM_API_URL', 'http://localhost:20200')))).replace(/\/+$/, '')
const explicitEmail = process.env.RAG_SMOKE_EMAIL
const explicitPassword = process.env.RAG_SMOKE_PASSWORD
const email = explicitEmail ?? env('TEST_ACCOUNT_EMAIL', `rag-smoke-${suffix}@example.com`)
const password = explicitPassword ?? env('TEST_ACCOUNT_PASSWORD', `WorkAMA-Rag-Smoke-${suffix}!`)
const generatedAccount = !explicitEmail && !env('TEST_ACCOUNT_EMAIL')

const evidence = {
  ok: false,
  checked_at: new Date().toISOString(),
  base_url: baseUrl,
  account_mode: generatedAccount ? 'generated' : 'configured',
  dataset_id: null,
  document_id: null,
  phases: {},
  cleanup: {},
  errors: [],
}

let token = ''
let datasetId = ''
let documentId = ''
let datasetDeleted = false
let evalSetId = ''
let evalSetDeleted = false

function assert(condition, message) {
  if (!condition) throw new Error(message)
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

async function request(path, options = {}) {
  const {
    expectedStatuses = [200, 201, 202, 204],
    headers = {},
    json,
    ...init
  } = options
  const requestHeaders = new Headers(headers)
  if (token) requestHeaders.set('Authorization', `Bearer ${token}`)
  if (json !== undefined) {
    requestHeaders.set('Content-Type', 'application/json')
    init.body = JSON.stringify(json)
  }
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: requestHeaders,
    signal: AbortSignal.timeout(timeoutMs),
  })
  const text = await response.text()
  let payload = null
  if (text) {
    try { payload = JSON.parse(text) } catch { payload = text }
  }
  if (!expectedStatuses.includes(response.status)) {
    const detail = typeof payload === 'string' ? payload : payload?.detail ?? payload?.message ?? JSON.stringify(payload)
    throw new Error(`${init.method ?? 'GET'} ${path} returned ${response.status}: ${detail}`)
  }
  return { response, payload }
}

async function waitForReady() {
  let lastError = null
  for (let attempt = 0; attempt < 30; attempt += 1) {
    try {
      const result = await request('/healthz', { expectedStatuses: [200, 503] })
      if (result.response.status === 200 && result.payload?.status === 'ok') return
      lastError = new Error(`healthz returned ${result.response.status}`)
    } catch (error) {
      lastError = error
    }
    await sleep(1000)
  }
  throw new Error(`Platform API did not become ready: ${lastError?.message ?? 'unknown error'}`)
}

async function authenticate() {
  const login = await request('/api/v1/auth/login', {
    method: 'POST',
    json: { email, password },
    expectedStatuses: [200, 401],
  })
  if (login.response.status === 200) return login.payload

  const registration = await request('/api/v1/auth/register', {
    method: 'POST',
    json: { email, password, display_name: 'RAG Smoke' },
    expectedStatuses: [201],
  })
  const verificationToken = registration.payload?.debug_token ?? process.env.RAG_SMOKE_VERIFY_TOKEN
  assert(verificationToken, 'Registration succeeded but no debug verification token was returned; set RAG_SMOKE_VERIFY_TOKEN for a mail-backed environment')
  const verified = await request('/api/v1/auth/verify-email', {
    method: 'POST',
    json: { token: verificationToken },
  })
  return verified.payload
}

async function waitForOperation(operationId) {
  assert(operationId, 'Expected an asynchronous operation id')
  let latest = null
  for (let attempt = 0; attempt < 120; attempt += 1) {
    latest = (await request(`/api/v1/operations/${operationId}`)).payload
    if (['succeeded', 'partially_succeeded', 'failed', 'cancelled', 'expired'].includes(latest.status)) return latest
    await sleep(500)
  }
  throw new Error(`Operation ${operationId} did not finish: ${latest?.status ?? 'unknown'}`)
}

async function ensureOnboarding() {
  const current = await request('/api/v1/auth/me')
  if (!current.payload?.onboarding_completed) {
    await request('/api/v1/auth/onboarding', {
      method: 'POST',
      json: {
        user_role: 'developer',
        primary_goal: 'knowledge',
        team_size: '1',
        data_sensitivity: 'standard',
        preferred_model: 'workama-chat',
        notification_preference: 'in_app',
      },
    })
  }
}

async function cleanupDataset() {
  if (!datasetId || datasetDeleted) return
  try {
    const current = await request(`/api/v1/datasets/${datasetId}`, { expectedStatuses: [200, 404] })
    if (current.response.status === 404 || current.payload?.status === 'deleted') {
      datasetDeleted = true
      evidence.cleanup.dataset_status = 'deleted'
      return
    }
    const accepted = await request(`/api/v1/datasets/${datasetId}`, {
      method: 'DELETE',
      headers: {
        'If-Match': `W/"${current.payload.version}"`,
        'Idempotency-Key': `rag-smoke-cleanup:${datasetId}`,
      },
      json: { reason: 'RAG smoke test cleanup' },
    })
    const operation = await waitForOperation(accepted.payload.operation?.id)
    evidence.cleanup.dataset_operation_status = operation.status
    datasetDeleted = operation.status === 'succeeded' || operation.status === 'partially_succeeded'
    evidence.cleanup.dataset_status = datasetDeleted ? 'deleted' : current.payload.status
  } catch (error) {
    evidence.cleanup.error = error instanceof Error ? error.message : String(error)
  }
}

async function cleanupEvalSet() {
  if (!evalSetId || evalSetDeleted) return
  try {
    const current = await request(`/api/v1/rag/eval-sets/${evalSetId}`, { expectedStatuses: [200, 404] })
    if (current.response.status === 404) {
      evalSetDeleted = true
      evidence.cleanup.eval_set_status = 'deleted'
      return
    }
    const deleted = await request(`/api/v1/rag/eval-sets/${evalSetId}`, {
      method: 'DELETE',
      headers: { 'If-Match': `W/"${current.payload.resource_version}"` },
      json: { reason: 'RAG smoke evaluation cleanup' },
      expectedStatuses: [204],
    })
    evalSetDeleted = deleted.response.status === 204
    evidence.cleanup.eval_set_status = evalSetDeleted ? 'deleted' : 'active'
  } catch (error) {
    evidence.cleanup.eval_set_error = error instanceof Error ? error.message : String(error)
  }
}

try {
  await waitForReady()
  const auth = await authenticate()
  token = auth?.access_token ?? ''
  assert(token, 'Authentication did not return an access token')
  await ensureOnboarding()
  evidence.phases.authenticated = true

  const dataset = await request('/api/v1/datasets', {
    method: 'POST',
    json: {
      name: `RAG smoke ${suffix}`,
      description: 'Live RAG pipeline verification dataset',
      embedding_model: 'workama-embed',
    },
  })
  datasetId = dataset.payload.id
  evidence.dataset_id = datasetId
  assert(datasetId && dataset.payload.status === 'active', 'Dataset was not created as active')
  evidence.phases.dataset_created = true

  const source = [
    '# WorkAMA RAG runbook',
    '',
    'The gateway routes embeddings through the workspace policy.',
    '',
    'Hybrid retrieval combines vector search, full text search, and reciprocal rank fusion.',
  ].join('\n')
  const form = new FormData()
  form.append('file', new Blob([source], { type: 'text/markdown' }), `rag-smoke-${suffix}.md`)
  const accepted = await request(`/api/v1/datasets/${datasetId}/documents`, { method: 'POST', body: form })
  documentId = accepted.payload.document?.id ?? ''
  evidence.document_id = documentId
  assert(accepted.response.status === 202 && documentId, 'Document upload was not accepted')
  const operation = await waitForOperation(accepted.payload.operation?.id)
  evidence.phases.document_operation_status = operation.status
  evidence.phases.document_operation = {
    status: operation.status,
    error_code: operation.error_code ?? null,
    error_message: operation.error_message ?? null,
    result_summary: operation.result_summary ?? null,
  }
  assert(operation.status === 'succeeded', `Document pipeline ended with ${operation.status}: ${operation.error_code ?? operation.error_message ?? 'unknown error'}`)

  const documentResult = await request(`/api/v1/datasets/${datasetId}/documents/${documentId}`)
  const document = documentResult.payload
  evidence.phases.document_status = document.status
  evidence.phases.document_chunk_count = document.chunk_count
  assert(document.status === 'indexed', `Document was not indexed: ${document.status}`)
  assert(document.chunk_count >= 1, 'Indexed document did not produce chunks')

  const chunks = await request(`/api/v1/datasets/${datasetId}/chunks`)
  assert(Array.isArray(chunks.payload.items) && chunks.payload.items.length >= 1, 'No indexed chunks were returned')
  const chunk = chunks.payload.items.find((item) => item.document_id === documentId) ?? chunks.payload.items[0]
  assert(String(chunk.content).toLowerCase().includes('gateway'), 'Indexed chunk did not preserve source content')
  evidence.phases.chunk_count = chunks.payload.items.length

  const config = await request(`/api/v1/datasets/${datasetId}/retrieval-config`)
  assert(config.payload.config?.top_k >= 1 && config.payload.config?.candidate_k >= config.payload.config.top_k, 'Retrieval configuration is invalid')
  evidence.phases.retrieval_config = config.payload.config

  const retrieved = await request(`/api/v1/datasets/${datasetId}/retrieve`, {
    method: 'POST',
    json: { query: 'gateway embeddings' },
  })
  const hits = retrieved.payload.items ?? []
  assert(hits.length > 0, 'Hybrid retrieval returned no hits')
  assert(hits[0].document_id === documentId, 'Top retrieval hit came from the wrong document')
  assert(String(hits[0].content).toLowerCase().includes('gateway'), 'Top retrieval hit did not contain the expected source passage')
  assert(Number.isFinite(Number(hits[0].rrf_score)), 'Hybrid retrieval hit is missing an RRF score')
  evidence.phases.retrieval_hit_count = hits.length
  evidence.phases.top_hit = { document_id: hits[0].document_id, rrf_score: hits[0].rrf_score, keyword_rank: hits[0].keyword_rank, vector_rank: hits[0].vector_rank }

  const editedContent = 'The platform indexes edited chunks through the governed gateway embedding route.'
  const edited = await request(`/api/v1/datasets/${datasetId}/chunks/${chunk.id}`, {
    method: 'PATCH',
    headers: { 'If-Match': `W/"${chunk.version}"`, 'Idempotency-Key': `rag-smoke-edit:${chunk.id}` },
    json: { content: editedContent },
  })
  const editOperation = await waitForOperation(edited.payload.operation?.id)
  assert(editOperation.status === 'succeeded', `Chunk edit ended with ${editOperation.status}`)
  const editedChunk = await request(`/api/v1/datasets/${datasetId}/chunks/${chunk.id}`)
  assert(editedChunk.payload.content === editedContent, 'Chunk edit did not persist')
  evidence.phases.chunk_edit_status = editOperation.status

  const evalSet = await request('/api/v1/rag/eval-sets', {
    method: 'POST',
    headers: { 'Idempotency-Key': `rag-smoke-eval-set:${suffix}` },
    json: { name: `RAG smoke evaluation ${suffix}`, description: 'Live RAG evaluation workflow verification', domain: 'knowledge', version: 1, dataset_id: datasetId, sampling_policy: {} },
  })
  evalSetId = evalSet.payload.id
  assert(evalSetId, 'Evaluation set was not created')
  evidence.phases.eval_set_created = true

  const evalCase = await request(`/api/v1/rag/eval-sets/${evalSetId}/cases`, {
    method: 'POST',
    json: { query: 'edited chunks gateway embedding', expected_chunk_ids: [chunk.id], expected_answer: 'The platform indexes edited chunks through the gateway embedding route.', labels: { source: 'smoke' }, provenance: { test: 'rag-smoke' } },
  })
  assert(evalCase.payload.id, 'Evaluation case was not created')
  const importedCases = await request(`/api/v1/rag/eval-sets/${evalSetId}/case-imports`, {
    method: 'POST',
    headers: { 'Idempotency-Key': `rag-smoke-eval-import:${suffix}` },
    json: { items: [{ query: `imported evaluation case ${suffix}`, expected_chunk_ids: [chunk.id], labels: { source: 'import' } }] },
  })
  const importOperation = await waitForOperation(importedCases.payload.operation?.id)
  assert(importOperation.status === 'succeeded', `Evaluation case import ended with ${importOperation.status}`)
  const evalCases = await request(`/api/v1/rag/eval-sets/${evalSetId}/cases`)
  assert(evalCases.payload.items.length >= 2, 'Evaluation case import did not create an active case')
  evidence.phases.eval_case_count = evalCases.payload.items.length

  const updatedEvalSet = await request(`/api/v1/rag/eval-sets/${evalSetId}`, {
    method: 'PATCH',
    headers: { 'If-Match': `W/"${evalSet.payload.resource_version}"` },
    json: { description: 'Updated live RAG evaluation workflow verification' },
  })
  assert(updatedEvalSet.payload.description.includes('Updated'), 'Evaluation set update did not persist')
  evidence.phases.eval_set_updated = true

  const evalRun = await request('/api/v1/rag/eval-runs', {
    method: 'POST',
    headers: { 'Idempotency-Key': `rag-smoke-eval-run:${suffix}` },
    json: { eval_set_id: evalSetId, dataset_id: datasetId, top_k: 5, candidate_k: 20, rrf_k: 60, score_threshold: 0 },
  })
  const evalRunOperation = await waitForOperation(evalRun.payload.operation?.id)
  assert(evalRunOperation.status === 'succeeded', `Evaluation run ended with ${evalRunOperation.status}`)
  const evalRunResult = await request(`/api/v1/rag/eval-runs/${evalRun.payload.run.id}`)
  assert(evalRunResult.payload.status === 'succeeded', `Evaluation run result was ${evalRunResult.payload.status}`)
  assert(Number(evalRunResult.payload.metrics?.hit_rate_at_k) === 1, 'Evaluation run did not report a perfect hit rate')
  evidence.phases.eval_run_status = evalRunResult.payload.status
  evidence.phases.eval_metrics = evalRunResult.payload.metrics

  const feedback = await request('/api/v1/rag/feedback', {
    method: 'POST',
    headers: { 'Idempotency-Key': `rag-smoke-feedback:${suffix}` },
    json: { dataset_id: datasetId, query: 'edited chunks gateway embedding', chunk_ids: [chunk.id], rating: 1, comment: 'RAG smoke feedback', eval_run_id: evalRun.payload.run.id, eval_case_id: evalCase.payload.id },
  })
  assert(feedback.payload.id, 'RAG feedback was not recorded')
  evidence.phases.feedback_created = true

  const importedCase = evalCases.payload.items.find((item) => item.id !== evalCase.payload.id)
  if (importedCase) {
    await request(`/api/v1/rag/eval-sets/${evalSetId}/cases/${importedCase.id}`, { method: 'DELETE', headers: { 'If-Match': `W/"${importedCase.version}"` }, expectedStatuses: [204] })
    evidence.phases.eval_case_deleted = true
  }
  const currentEvalSet = await request(`/api/v1/rag/eval-sets/${evalSetId}`)
  await request(`/api/v1/rag/eval-sets/${evalSetId}`, { method: 'DELETE', headers: { 'If-Match': `W/"${currentEvalSet.payload.resource_version}"` }, json: { reason: 'RAG smoke evaluation cleanup' }, expectedStatuses: [204] })
  evalSetDeleted = true
  evidence.cleanup.eval_set_status = 'deleted'

  const currentDocument = await request(`/api/v1/datasets/${datasetId}/documents/${documentId}`)
  const documentDelete = await request(`/api/v1/datasets/${datasetId}/documents/${documentId}`, {
    method: 'DELETE',
    headers: { 'If-Match': `W/"${currentDocument.payload.version}"`, 'Idempotency-Key': `rag-smoke-document-delete:${documentId}` },
    json: { reason: 'RAG smoke test cleanup' },
  })
  const documentDeleteOperation = await waitForOperation(documentDelete.payload.operation?.id)
  assert(documentDeleteOperation.status === 'succeeded', `Document cleanup ended with ${documentDeleteOperation.status}`)
  evidence.cleanup.document_operation_status = documentDeleteOperation.status

  const remainingDocuments = await request(`/api/v1/datasets/${datasetId}/documents`)
  assert(!(remainingDocuments.payload.items ?? []).some((item) => item.id === documentId), 'Deleted document is still listed')
  evidence.cleanup.document_status = 'deleted'

  const currentDataset = await request(`/api/v1/datasets/${datasetId}`)
  const datasetDelete = await request(`/api/v1/datasets/${datasetId}`, {
    method: 'DELETE',
    headers: { 'If-Match': `W/"${currentDataset.payload.version}"`, 'Idempotency-Key': `rag-smoke-dataset-delete:${datasetId}` },
    json: { reason: 'RAG smoke test cleanup' },
  })
  const datasetDeleteOperation = await waitForOperation(datasetDelete.payload.operation?.id)
  assert(datasetDeleteOperation.status === 'succeeded', `Dataset cleanup ended with ${datasetDeleteOperation.status}`)
  datasetDeleted = true
  evidence.cleanup.dataset_operation_status = datasetDeleteOperation.status
  evidence.cleanup.dataset_status = 'deleted'
  evidence.ok = true
} catch (error) {
  const message = error instanceof Error ? error.message : String(error)
  evidence.errors.push(message)
  process.exitCode = 1
} finally {
  await cleanupEvalSet()
  await cleanupDataset()
  evidence.checked_at = new Date().toISOString()
  await mkdir(outputDir, { recursive: true })
  await writeFile(outputFile, `${JSON.stringify(evidence, null, 2)}\n`, 'utf8')
  process.stdout.write(`${JSON.stringify(evidence)}\n`)
}
