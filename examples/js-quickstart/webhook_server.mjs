/**
 * WorkAMA Webhook 接收端示例（P2 第三方集成，JavaScript 版）。
 *
 * 使用 Node 18+ 内置的 `node:http` 模块实现（无需 Express 依赖），完整覆盖：
 *
 * 1. HMAC-SHA256 签名校验（使用 crypto.timingSafeEqual 防时序攻击）
 * 2. 事件分发：
 *    - `automation.triggered.v1`    自动化触发
 *    - `workflow.run.updated.v1`    工作流运行状态变更
 *    - `billing.meter_event.v1`     计量计费事件
 * 3. 幂等去重（基于事件 ID 的内存缓存）
 * 4. 可配置监听端口与签名密钥
 *
 * 签名约定（与平台 automation_v2 / audit_exports 一致）：
 *     请求头 `X-WorkAMA-Signature`，值为 HMAC-SHA256(secret, raw_body) 的十六进制；
 *     也兼容 `sha256=<hex>` 前缀格式。
 *
 * 运行方式：
 *   cd examples/js-quickstart
 *   node webhook_server.mjs
 *
 * 环境变量：
 *   WORKAMA_WEBHOOK_SECRET  签名密钥（必填，需与平台触发器配置一致）
 *   WORKAMA_WEBHOOK_PORT    监听端口，默认 8099
 */

import crypto from 'node:crypto'
import http from 'node:http'

const WEBHOOK_SECRET = process.env.WORKAMA_WEBHOOK_SECRET || ''
const WEBHOOK_PORT = parseInt(process.env.WORKAMA_WEBHOOK_PORT || '8099', 10)

// ---------------------------------------------------------------------------
// 签名校验
// ---------------------------------------------------------------------------

/**
 * 计算 HMAC-SHA256 十六进制签名。
 * @param {string} secret
 * @param {Buffer} rawBody
 * @returns {string}
 */
function computeSignature(secret, rawBody) {
  return crypto.createHmac('sha256', secret).update(rawBody).digest('hex')
}

/**
 * 校验签名，使用 `crypto.timingSafeEqual` 防止时序攻击。
 *
 * 兼容两种头格式：
 *   - 纯十六进制：`<hex>`
 *   - 带前缀：`sha256=<hex>`
 *
 * @param {string} secret
 * @param {Buffer} rawBody
 * @param {string|undefined} provided
 * @returns {boolean}
 */
function verifySignature(secret, rawBody, provided) {
  if (!provided) {
    return false
  }
  const expected = computeSignature(secret, rawBody)
  let candidate = provided
  // 兼容 sha256=<hex> 前缀格式
  if (candidate.startsWith('sha256=')) {
    candidate = candidate.slice('sha256='.length)
  }
  // 长度不一致直接拒绝，避免 timingSafeEqual 抛出异常
  if (candidate.length !== expected.length) {
    return false
  }
  // timingSafeEqual 要求两个 Buffer 长度相同，否则抛出 RangeError
  try {
    return crypto.timingSafeEqual(
      Buffer.from(candidate, 'utf-8'),
      Buffer.from(expected, 'utf-8'),
    )
  } catch {
    return false
  }
}

// ---------------------------------------------------------------------------
// 事件处理器注册
// ---------------------------------------------------------------------------

/** 已处理事件 ID 的内存去重缓存（生产环境建议用 Redis）。 */
const seenEventIds = new Set()

/** 事件类型 -> 处理函数。 */
const handlers = new Map()

/**
 * 注册某类事件的处理函数。
 * @param {string} eventType
 * @param {(event: object) => void} fn
 */
function handle(eventType, fn) {
  handlers.set(eventType, fn)
}

handle('automation.triggered.v1', (event) => {
  /** 自动化触发事件。 */
  const data = event.data || {}
  console.log(
    `[automation.triggered] trigger_id=${data.trigger_id} ` +
    `run_id=${data.run_id} payload=${brief(data.payload)}`,
  )
})

handle('workflow.run.updated.v1', (event) => {
  /** 工作流运行状态变更事件。 */
  const data = event.data || {}
  console.log(
    `[workflow.run.updated] workflow_id=${data.workflow_id} ` +
    `run_id=${data.run_id} status=${data.status} error=${data.error}`,
  )
})

handle('billing.meter_event.v1', (event) => {
  /** 计量计费事件：累加 token / 调用次数等用量。 */
  const data = event.data || {}
  console.log(
    `[billing.meter_event] workspace_id=${data.workspace_id} ` +
    `metric=${data.metric} quantity=${data.quantity} subject=${data.subject}`,
  )
})

/**
 * 分发事件；返回 true 表示已处理，false 表示重复事件。
 *
 * 约定事件结构：`{ id, type, data }`。
 * @param {object} event
 * @returns {boolean}
 */
function dispatch(event) {
  const eventId = String(event.id || '')
  if (eventId && seenEventIds.has(eventId)) {
    console.log(`[skip] 重复事件 ${eventId}`)
    return false
  }
  if (eventId) {
    seenEventIds.add(eventId)
  }
  const etype = event.type || event.event_type || ''
  const handler = handlers.get(etype)
  if (handler) {
    handler(event)
  } else {
    console.log(`[unhandled] 事件类型 ${JSON.stringify(etype)} 无注册处理器，原始：${brief(event)}`)
  }
  return true
}

function brief(obj, limit = 160) {
  const text = typeof obj === 'string' ? obj : JSON.stringify(obj)
  return text.length <= limit ? text : text.slice(0, limit) + '...(已截断)'
}

// ---------------------------------------------------------------------------
// HTTP 服务
// ---------------------------------------------------------------------------

/**
 * 发送 JSON 响应。
 * @param {http.ServerResponse} res
 * @param {number} status
 * @param {object} body
 */
function respond(res, status, body) {
  const payload = Buffer.from(JSON.stringify(body), 'utf-8')
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Content-Length': payload.length,
  })
  res.end(payload)
}

/**
 * 读取请求体到 Buffer。
 * @param {http.IncomingMessage} req
 * @returns {Promise<Buffer>}
 */
function readBody(req) {
  return new Promise((resolve) => {
    const chunks = []
    req.on('data', (chunk) => chunks.push(chunk))
    req.on('end', () => resolve(Buffer.concat(chunks)))
    req.on('error', () => resolve(Buffer.alloc(0)))
  })
}

/** @type {http.RequestListener} */
const requestListener = async (req, res) => {
  if (req.method === 'GET') {
    // 健康检查端点
    if (req.url === '/' || req.url === '/healthz') {
      respond(res, 200, {
        status: 'ok',
        handlers: Array.from(handlers.keys()).sort(),
      })
    } else {
      respond(res, 404, { error: 'not found' })
    }
    return
  }

  if (req.method !== 'POST') {
    respond(res, 405, { error: 'method not allowed' })
    return
  }

  // 1) 读取原始请求体
  const rawBody = await readBody(req)

  // 2) 签名校验
  if (!WEBHOOK_SECRET) {
    respond(res, 500, { error: 'WORKAMA_WEBHOOK_SECRET not configured' })
    return
  }
  const provided = req.headers['x-workama-signature']
  if (!verifySignature(WEBHOOK_SECRET, rawBody, provided)) {
    console.log('[warn] 签名校验失败，拒绝请求')
    respond(res, 401, { error: 'invalid signature' })
    return
  }

  // 3) 解析事件
  let event
  try {
    event = JSON.parse(rawBody.toString('utf-8'))
  } catch (err) {
    respond(res, 400, { error: `invalid json: ${err.message}` })
    return
  }

  // 4) 分发（支持单事件与批量事件）
  if (Array.isArray(event)) {
    for (const ev of event) {
      dispatch(ev)
    }
  } else if (event && typeof event === 'object') {
    // 批量封装：{ events: [...] }
    if (Array.isArray(event.events)) {
      for (const ev of event.events) {
        dispatch(ev)
      }
    } else {
      dispatch(event)
    }
  } else {
    respond(res, 400, { error: 'unexpected payload shape' })
    return
  }

  respond(res, 200, { accepted: true })
}

// ---------------------------------------------------------------------------
// 入口
// ---------------------------------------------------------------------------

function main() {
  if (!WEBHOOK_SECRET) {
    console.error('[ERR] 请先设置环境变量 WORKAMA_WEBHOOK_SECRET')
    process.exit(2)
  }

  const server = http.createServer(requestListener)
  server.listen(WEBHOOK_PORT, '0.0.0.0', () => {
    console.log(
      `[webhook] 监听 0.0.0.0:${WEBHOOK_PORT}，已注册事件：` +
      Array.from(handlers.keys()).sort(),
    )
    console.log(
      '[webhook] 提示：在平台触发器配置中将 Webhook URL 指向本服务，' +
      '并使用相同的 secret',
    )
  })

  // 优雅关闭
  const shutdown = () => {
    console.log('\n[webhook] 停止服务')
    server.close(() => process.exit(0))
  }
  process.on('SIGINT', shutdown)
  process.on('SIGTERM', shutdown)
}

main()
