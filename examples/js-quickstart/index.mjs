/**
 * WorkAMA JavaScript SDK 快速开始示例。
 *
 * 完整演示：列出 Agent -> 对话 -> 创建记忆 -> 检索记忆 -> 搜索知识 -> 执行工作流。
 *
 * 运行方式：
 *   cd examples/js-quickstart
 *   pnpm --filter @workama/sdk build  # 或直接引用 src
 *   node index.mjs
 *
 * 环境变量（可选）：
 *   WORKAMA_BASE_URL      平台 API 基地址，默认 http://localhost:20200
 *   WORKAMA_API_KEY       API Key（与 WORKAMA_ACCESS_TOKEN 二选一）
 *   WORKAMA_ACCESS_TOKEN  Bearer Token（优先级高于 API Key）
 *   WORKAMA_AGENT_ID      演示对话使用的 Agent ID
 */

import {
  WorkAMAClient,
  WorkAMAError,
  AuthenticationError,
  NotFoundError,
  RateLimitError,
} from '../../packages/sdk-js/src/index.ts'

const BASE_URL = process.env.WORKAMA_BASE_URL || 'http://localhost:20200'
const API_KEY = process.env.WORKAMA_API_KEY
const ACCESS_TOKEN = process.env.WORKAMA_ACCESS_TOKEN
const AGENT_ID = process.env.WORKAMA_AGENT_ID || 'agent_demo'

async function main() {
  if (!API_KEY && !ACCESS_TOKEN) {
    console.warn('[WARN] 未设置 WORKAMA_API_KEY / WORKAMA_ACCESS_TOKEN，后续请求可能 401')
  }

  const client = new WorkAMAClient({
    baseUrl: BASE_URL,
    apiKey: API_KEY,
    accessToken: ACCESS_TOKEN,
    timeout: 30000,
  })

  try {
    // 1) 列出 Agent
    console.log('\n=== 1. listAgents ===')
    const agents = await client.listAgents({ limit: 20 })
    console.log('agent 数量:', (agents.items || []).length)
    for (const a of (agents.items || []).slice(0, 3)) {
      console.log('  -', a)
    }

    // 2) 与 Agent 对话
    console.log('\n=== 2. chat ===')
    const chat = await client.chat(AGENT_ID, '你好，请用一句话介绍 WorkAMA')
    console.log('reply:', String(chat.message || '').slice(0, 200))

    // 3) 创建记忆
    console.log('\n=== 3. createMemory ===')
    const mem = await client.createMemory('用户喜欢简洁的回复，避免冗长输出', {
      metadata: { category: 'preference', source: 'quickstart' },
      importance: 4,
    })
    console.log('created memory:', mem)

    // 4) 检索记忆
    console.log('\n=== 4. recallMemory ===')
    const recall = await client.recallMemory('用户偏好', { limit: 3 })
    for (const item of (recall.items || []).slice(0, 3)) {
      const score = typeof item.score === 'number' ? item.score.toFixed(3) : 'N/A'
      console.log(`  - score=${score} content=${String(item.content || '').slice(0, 60)}`)
    }

    // 5) 搜索知识库
    console.log('\n=== 5. searchKnowledge ===')
    const results = await client.searchKnowledge('产品定价', { limit: 5 })
    for (const hit of (results.items || []).slice(0, 3)) {
      console.log(`  - score=${hit.score} content=${String(hit.content || '').slice(0, 60)}`)
    }

    // 6) 列出工作流并执行
    console.log('\n=== 6. listWorkflows & runWorkflow ===')
    const workflows = await client.listWorkflows({ limit: 10 })
    console.log('workflow 数量:', (workflows.items || []).length)
    const wfItems = workflows.items || []
    if (wfItems.length > 0) {
      const wfId = wfItems[0].id || 'wf_demo'
      const run = await client.runWorkflow(wfId, { topic: '本周项目进展' })
      console.log('run:', run)
    }

    console.log('\n[OK] quickstart 完成')
  } catch (err) {
    if (err instanceof AuthenticationError) {
      console.error(`[ERR] 鉴权失败: ${err.message} (body=${JSON.stringify(err.body)})`)
      process.exitCode = 2
    } else if (err instanceof NotFoundError) {
      console.error(`[ERR] 资源不存在: ${err.message} (body=${JSON.stringify(err.body)})`)
      process.exitCode = 3
    } else if (err instanceof RateLimitError) {
      console.error(`[ERR] 被限流: ${err.message} (body=${JSON.stringify(err.body)})`)
      process.exitCode = 4
    } else if (err instanceof WorkAMAError) {
      console.error(`[ERR] SDK 错误: ${err.message} (status=${err.statusCode}, body=${JSON.stringify(err.body)})`)
      process.exitCode = 5
    } else {
      throw err
    }
  }
}

main().catch((err) => {
  console.error('[FATAL]', err)
  process.exit(1)
})
