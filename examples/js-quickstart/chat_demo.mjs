/**
 * WorkAMA Agent 对话示例（P2 第三方集成，JavaScript 版）。
 *
 * 演示：
 * 1. 创建 Agent（助手）→ 拿到 agent_id
 * 2. 通过 SDK sendChatMessage 同步对话，解析 token 用量
 * 3. 使用 SSE 流式调用网关 /v1/chat/completions（OpenAI 兼容）实时接收增量
 * 4. 错误处理 + 指数退避重试（针对 429/5xx）
 *
 * 运行方式：
 *   cd examples/js-quickstart
 *   node chat_demo.mjs
 *
 * 环境变量：
 *   WORKAMA_BASE_URL       平台 API 基地址，默认 http://localhost:20200
 *   WORKAMA_ACCESS_TOKEN   Bearer Token（优先）
 *   WORKAMA_API_KEY        API Key（与 token 二选一）
 *   WORKAMA_WORKSPACE_ID   可选，工作空间隔离标识
 *   WORKAMA_AGENT_ID       可选，复用已有 Agent；否则示例会尝试新建
 *   WORKAMA_GATEWAY_URL    网关地址，默认 http://localhost:20202
 *   WORKAMA_MODEL          网关对话模型，默认 gpt-4o-mini
 */

import {
  WorkAMAClient,
  WorkAMAError,
  ForbiddenError,
  RateLimitError,
} from '../../packages/sdk-js/src/index.ts'

const BASE_URL = process.env.WORKAMA_BASE_URL || 'http://localhost:20200'
const GATEWAY_URL = process.env.WORKAMA_GATEWAY_URL || 'http://localhost:20202'
const ACCESS_TOKEN = process.env.WORKAMA_ACCESS_TOKEN
const API_KEY = process.env.WORKAMA_API_KEY
const WORKSPACE_ID = process.env.WORKAMA_WORKSPACE_ID
const MODEL = process.env.WORKAMA_MODEL || 'gpt-4o-mini'

// ---------------------------------------------------------------------------
// 1. 创建 Agent（或复用已有）
// ---------------------------------------------------------------------------

/**
 * 返回一个可用 agent_id；优先用环境变量，否则新建一个临时助手。
 * @param {WorkAMAClient} client
 * @param {string|undefined} workspaceId
 * @returns {Promise<string|null>}
 */
async function ensureAgent(client, workspaceId) {
  const existing = process.env.WORKAMA_AGENT_ID
  if (existing) {
    console.log(`[INFO] 复用环境变量指定的 Agent: ${existing}`)
    return existing
  }

  console.log('\n=== 1. 创建 Agent ===')
  const payload = {
    name: 'sdk-chat-demo-js',
    description: 'JavaScript SDK chat_demo 临时助手',
    system_prompt: '你是 WorkAMA 助手，请用简洁的中文回答问题。',
    model: MODEL,
    temperature: 0.5,
    max_tokens: 512,
    memory_enabled: false,
    status: 'active',
  }
  try {
    const resp = await client.createAgent(payload, { workspaceId })
    const agentId = resp && resp.id
    console.log(`  created agent_id=${agentId}`)
    return agentId || null
  } catch (err) {
    if (err instanceof ForbiddenError) {
      console.log(`[ERR] 无权创建 Agent（403）: ${err.message}`)
    } else if (err instanceof WorkAMAError) {
      console.log(`[ERR] 创建 Agent 失败: ${err.message}（可设置 WORKAMA_AGENT_ID 复用已有）`)
    } else {
      console.log(`[ERR] 创建 Agent 异常: ${err.message || err}`)
    }
    return null
  }
}

// ---------------------------------------------------------------------------
// 2. 同步对话 + token 用量
// ---------------------------------------------------------------------------

/**
 * 带指数退避重试的同步对话。
 *
 * - 429（限流）/ 5xx：重试
 * - 401/403/404：不重试，直接返回
 *
 * @param {WorkAMAClient} client
 * @param {string} agentId
 * @param {string} message
 * @param {string|undefined} workspaceId
 * @param {number} [maxRetries=3]
 * @returns {Promise<object|null>}
 */
async function chatWithRetry(client, agentId, message, workspaceId, maxRetries = 3) {
  console.log('\n=== 2. 同步对话（sendChatMessage）===')
  let delay = 500
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      const resp = await client.sendChatMessage(agentId, message, {
        conversationId: `demo-${agentId}`,
        workspaceId,
      })
      return resp
    } catch (err) {
      if (err instanceof RateLimitError) {
        console.log(`  [重试 ${attempt}/${maxRetries}] 触发限流，${delay}ms 后重试: ${err.message}`)
      } else if (err instanceof WorkAMAError && err.statusCode && err.statusCode >= 500 && err.statusCode < 600 && attempt < maxRetries) {
        console.log(`  [重试 ${attempt}/${maxRetries}] 服务端 ${err.statusCode}，${delay}ms 后重试`)
      } else {
        console.log(`[ERR] 对话失败: ${err.message || err} (status=${err.statusCode})`)
        return null
      }
    }
    await sleep(delay)
    delay *= 2
  }
  console.log('[ERR] 重试次数耗尽')
  return null
}

/**
 * 从助手 run 响应里提取并打印 token 用量统计。
 * @param {object} resp
 */
function printUsage(resp) {
  if (!resp || typeof resp !== 'object') {
    return
  }
  // 平台 assistant_run 响应常见字段：tokens_used / tokens / usage
  const usage = resp.usage || {}
  const tokensUsed = resp.tokens_used
  const model = resp.model
  console.log(`  run_id=${resp.id || resp.run_id}`)
  console.log(`  model=${model}`)
  if (tokensUsed !== undefined) {
    console.log(`  tokens_used=${tokensUsed}`)
  }
  if (usage && Object.keys(usage).length > 0) {
    console.log(`  usage=${JSON.stringify(usage)}`)
  }
  const reply = resp.assistant_message || resp.message || resp.content || ''
  if (reply) {
    console.log(`  reply: ${String(reply).slice(0, 200)}`)
  }
}

// ---------------------------------------------------------------------------
// 3. SSE 流式对话（网关 OpenAI 兼容端点）
// ---------------------------------------------------------------------------

/**
 * 通过网关 /v1/chat/completions 的 SSE 流式增量接收回复。
 *
 * Node 18+ 的 fetch 支持 response.body.getReader()，逐 chunk 解析 ``data:`` 行。
 * 以 ``[DONE]`` 结束；最后一个 chunk 的 usage 包含累计 token 统计。
 *
 * @param {string} gatewayUrl
 * @param {string|undefined} accessToken
 * @param {string|undefined} apiKey
 * @param {string} model
 * @param {string} prompt
 */
async function streamChat(gatewayUrl, accessToken, apiKey, model, prompt) {
  console.log('\n=== 3. SSE 流式对话（gateway /v1/chat/completions）===')
  if (!accessToken && !apiKey) {
    console.log('  [跳过] 未提供凭证，无法调用网关')
    return
  }

  const url = gatewayUrl.replace(/\/+$/, '') + '/v1/chat/completions'
  const body = JSON.stringify({
    model,
    messages: [{ role: 'user', content: prompt }],
    stream: true,
  })
  const headers = {
    'Content-Type': 'application/json',
    Accept: 'text/event-stream',
    'User-Agent': 'workama-example-chat/0.1.0',
  }
  if (accessToken) {
    headers['Authorization'] = `Bearer ${accessToken}`
  } else if (apiKey) {
    headers['X-WorkAMA-API-Key'] = apiKey
  }

  let resp
  try {
    resp = await fetch(url, { method: 'POST', headers, body })
  } catch (err) {
    console.log(`\n  [ERR] 网关连接失败: ${err.message}（请确认 WORKAMA_GATEWAY_URL 可达）`)
    return
  }

  if (!resp.ok) {
    const text = await resp.text()
    console.log(`\n  [ERR] 网关返回 ${resp.status}: ${text.slice(0, 200)}`)
    return
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let collected = ''
  let finalUsage = null

  while (true) {
    const { done, value } = await reader.read()
    if (done) {
      break
    }
    buffer += decoder.decode(value, { stream: true })
    // 按 \n 切分，保留最后一段未结束的内容
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''
    for (const rawLine of lines) {
      const line = rawLine.replace(/\r$/, '')
      if (!line || !line.startsWith('data:')) {
        continue
      }
      const data = line.slice(5).trim()
      if (data === '[DONE]') {
        buffer = ''
        continue
      }
      try {
        const chunk = JSON.parse(data)
        const choices = chunk.choices || []
        if (choices.length > 0) {
          const delta = choices[0].delta || {}
          const piece = delta.content || ''
          if (piece) {
            collected += piece
            process.stdout.write(piece)
          }
        }
        if (chunk.usage) {
          finalUsage = chunk.usage
        }
      } catch {
        // 忽略非 JSON 行
      }
    }
  }

  console.log()
  console.log(`  [流式完成] 累计长度=${collected.length} 字符`)
  if (finalUsage) {
    console.log(`  [token 用量] ${JSON.stringify(finalUsage)}`)
  }
}

// ---------------------------------------------------------------------------
// 辅助
// ---------------------------------------------------------------------------

/** Promise 版的 sleep。 */
function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

// ---------------------------------------------------------------------------
// 入口
// ---------------------------------------------------------------------------

async function main() {
  if (!ACCESS_TOKEN && !API_KEY) {
    console.warn('[WARN] 未设置 WORKAMA_ACCESS_TOKEN / WORKAMA_API_KEY，请求可能 401')
  }

  const client = new WorkAMAClient({
    baseUrl: BASE_URL,
    apiKey: API_KEY,
    accessToken: ACCESS_TOKEN,
  })

  const agentId = await ensureAgent(client, WORKSPACE_ID)
  if (!agentId) {
    console.log('[INFO] 没有 agent_id，仍演示网关 SSE 流式对话')
  }

  if (agentId) {
    const resp = await chatWithRetry(
      client,
      agentId,
      '用一句话介绍 WorkAMA 平台的核心能力。',
      WORKSPACE_ID,
    )
    if (resp) {
      printUsage(resp)
    }
  }

  // SSE 流式对话（与上面同步对话并列演示）
  await streamChat(GATEWAY_URL, ACCESS_TOKEN, API_KEY, MODEL, '用三句话描述 RAG 的价值。')

  console.log('\n[OK] chat_demo 示例完成')
}

main().catch((err) => {
  console.error('[FATAL]', err)
  process.exit(1)
})
