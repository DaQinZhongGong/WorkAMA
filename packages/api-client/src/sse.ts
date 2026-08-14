import { ApiError } from './index'
import type { SSEStreamEvent, SSEStreamOptions, SubscribeSSEOptions, SubscribeSSEResult } from './types'

const SSE_BOUNDARY = /\r?\n\r?\n/

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason)
      return
    }
    const timer = setTimeout(resolve, ms)
    signal?.addEventListener(
      'abort',
      () => {
        clearTimeout(timer)
        reject(signal.reason)
      },
      { once: true },
    )
  })
}

/** 解析单个 SSE 事件块 */
function parseSSEBlock(block: string): SSEStreamEvent | null {
  if (block === '') return null
  const lines = block.split(/\r?\n/)
  const dataLines: string[] = []
  let event: string | undefined
  let id: string | undefined
  let retry: number | undefined
  let hasField = false

  for (const rawLine of lines) {
    if (rawLine === '') continue
    if (rawLine.startsWith(':')) continue
    hasField = true
    const colonIdx = rawLine.indexOf(':')
    let field: string
    let value: string
    if (colonIdx === -1) {
      field = rawLine
      value = ''
    } else {
      field = rawLine.slice(0, colonIdx)
      value = rawLine.slice(colonIdx + 1)
      if (value.startsWith(' ')) value = value.slice(1)
    }
    switch (field) {
      case 'data':
        dataLines.push(value)
        break
      case 'event':
        event = value
        break
      case 'id':
        id = value
        break
      case 'retry': {
        const n = Number(value)
        if (!Number.isNaN(n)) retry = n
        break
      }
      default:
        break
    }
  }

  if (!hasField) return null
  return { data: dataLines.join('\n'), event, id, retry }
}

/**
 * 创建 SSE 异步可迭代流。
 *
 * 特性：
 * - 使用 fetch + ReadableStream 解析，支持自定义 Authorization header；
 * - 自动注入 Bearer token；
 * - 使用 `Last-Event-ID` 恢复断线重连；
 * - 出错时通过 `onError` 回调通知并按指数退避重连；
 * - 通过 `AbortSignal` 彻底取消。
 */
export async function* createSSEStream(
  url: string,
  options: SSEStreamOptions = {},
): AsyncIterable<SSEStreamEvent> {
  const {
    token,
    getToken,
    headers,
    init,
    signal,
    onError,
    maxReconnectAttempts = Number.POSITIVE_INFINITY,
    reconnectDelayMs = 1000,
    maxReconnectDelayMs = 30000,
    lastEventId: initialLastEventId,
  } = options

  let lastEventId: string | undefined = initialLastEventId
  let attempt = 0
  let nextDelay = reconnectDelayMs

  while (true) {
    if (signal?.aborted) return

    const finalHeaders = new Headers(headers ?? init?.headers)
    if (lastEventId) finalHeaders.set('Last-Event-ID', lastEventId)
    const resolvedToken = token ?? getToken?.() ?? null
    if (resolvedToken) finalHeaders.set('Authorization', `Bearer ${resolvedToken}`)
    if (!finalHeaders.has('Accept')) finalHeaders.set('Accept', 'text/event-stream')
    if (!finalHeaders.has('Cache-Control')) finalHeaders.set('Cache-Control', 'no-cache')

    let response: Response
    try {
      response = await fetch(url, {
        ...init,
        method: init?.method ?? 'GET',
        headers: finalHeaders,
        credentials: init?.credentials ?? 'include',
        signal,
      })
    } catch (err) {
      onError?.(err)
      if (signal?.aborted) return
      if (attempt >= maxReconnectAttempts) throw err
      try {
        await sleep(nextDelay, signal)
      } catch {
        return
      }
      nextDelay = Math.min(nextDelay * 2, maxReconnectDelayMs)
      attempt++
      continue
    }

    if (!response.ok) {
      const err = new ApiError(
        response.status,
        `HTTP_${response.status}`,
        response.statusText,
        response.headers.get('x-request-id') ?? undefined,
      )
      onError?.(err)
      if (signal?.aborted) return
      if (attempt >= maxReconnectAttempts) throw err
      try {
        await sleep(nextDelay, signal)
      } catch {
        return
      }
      nextDelay = Math.min(nextDelay * 2, maxReconnectDelayMs)
      attempt++
      continue
    }

    // 成功建立连接后重置重连计数
    attempt = 0
    nextDelay = reconnectDelayMs

    const body = response.body
    if (!body) {
      // 空 body 视为流结束
      return
    }

    const reader = body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''
    let connectionBroken = false

    try {
      while (true) {
        if (signal?.aborted) {
          await reader.cancel()
          return
        }
        let readResult: ReadableStreamReadResult<Uint8Array>
        try {
          readResult = await reader.read()
        } catch (err) {
          onError?.(err)
          connectionBroken = true
          break
        }
        if (readResult.done) break
        buffer += decoder.decode(readResult.value, { stream: true })
        let match: RegExpMatchArray | null
        while ((match = buffer.match(SSE_BOUNDARY)) !== null) {
          const block = buffer.slice(0, match.index as number)
          buffer = buffer.slice((match.index as number) + match[0].length)
          const event = parseSSEBlock(block)
          if (event) {
            if (event.id) lastEventId = event.id
            if (typeof event.retry === 'number' && event.retry > 0) {
              nextDelay = event.retry
            }
            yield event
          }
        }
      }

      buffer += decoder.decode()
      if (buffer.length > 0) {
        const event = parseSSEBlock(buffer)
        if (event) {
          if (event.id) lastEventId = event.id
          yield event
        }
      }
    } finally {
      try {
        await reader.cancel()
      } catch {
        // 忽略取消时的错误
      }
    }

    if (!connectionBroken) {
      // 正常结束则不再重连
      return
    }

    if (signal?.aborted) return
    if (attempt >= maxReconnectAttempts) {
      throw new Error('SSE stream reached max reconnect attempts')
    }
    try {
      await sleep(nextDelay, signal)
    } catch {
      return
    }
    nextDelay = Math.min(nextDelay * 2, maxReconnectDelayMs)
    attempt++
  }
}

/**
 * 订阅 SSE 流并返回取消订阅函数。
 *
 * 这是对 `createSSEStream` 的包装，提供更简单的回调式 API。
 * 内部创建 AbortController 以支持取消操作。
 *
 * @example
 * ```ts
 * const { unsubscribe } = subscribeSSE('https://api.example.com/sse', {
 *   token: 'my-token',
 *   onEvent: (event) => console.log('Received:', event),
 *   onError: (err) => console.error('Error:', err),
 *   onDone: () => console.log('Stream ended'),
 * });
 *
 * // 稍后取消订阅
 * unsubscribe();
 * ```
 */
export function subscribeSSE(
  url: string,
  options: SubscribeSSEOptions,
): SubscribeSSEResult {
  const controller = new AbortController()
  const { onEvent, onDone, onError, signal: externalSignal, ...streamOptions } = options

  // 如果外部提供了 signal，监听其 abort 事件以联动取消
  if (externalSignal) {
    if (externalSignal.aborted) {
      controller.abort(externalSignal.reason)
    } else {
      externalSignal.addEventListener(
        'abort',
        () => {
          controller.abort(externalSignal.reason)
        },
        { once: true },
      )
    }
  }

  // 异步启动流
  // 注意：不将 onError 传递给 createSSEStream，以避免重连过程中的中间错误
  // 与最终失败时的错误被重复报告。createSSEStream 在超过最大重连次数后会
  // 抛出异常，由本处的 catch 统一报告给 onError。
  ;(async () => {
    try {
      for await (const event of createSSEStream(url, {
        ...streamOptions,
        signal: controller.signal,
      })) {
        if (controller.signal.aborted) break
        onEvent(event)
      }
      if (!controller.signal.aborted) {
        onDone?.()
      }
    } catch (err) {
      // 如果是主动取消则不报错
      if (!controller.signal.aborted) {
        onError?.(err)
      }
    }
  })()

  return {
    unsubscribe: () => {
      controller.abort()
    },
  }
}
