// 集中管理 web 端运行时配置。
//
// 配置解析优先级（高 → 低）：
//   1. 运行时覆盖：容器入口在启动时把 /config.js 重写为
//      `window.__WORKAMA_CONFIG__={"platformApiUrl":...}` —— 同一镜像可跨环境
//      部署，改端点无需重新构建（12-factor）；见 apps/web/docker-entrypoint.sh。
//   2. 构建期注入：Vite 的 VITE_*（import.meta.env）。
//   3. 缺省回落：开发态走 Vite 代理/origin 推导，生产态回落 localhost 默认端口。
//
// 共享设计令牌统一存放在共享包 @workama/config/tokens.json，此处作为唯一来源
// 引入并对外暴露；@workama/config 当前仅导出设计令牌，故运行时配置 getter 就近
// 实现于 web 端。空字符串一律视为「未设置」，逐级回退，运行行为与历史一致。
import designTokens from '@workama/config/tokens.json'

type RuntimeConfig = {
  platformApiUrl?: string
  agentWsUrl?: string
  grafanaUrl?: string
}

declare global {
  interface Window {
    __WORKAMA_CONFIG__?: RuntimeConfig
  }
}

function runtimeConfig(): RuntimeConfig {
  if (typeof window === 'undefined') return {}
  const c = window.__WORKAMA_CONFIG__
  return c && typeof c === 'object' ? c : {}
}

/** 取第一个非空值；全部为空返回 ''。 */
function pick(...values: Array<string | undefined | null>): string {
  for (const v of values) if (v) return v
  return ''
}

// 平台 API 基址。
export const platformApiUrl: string = pick(
  runtimeConfig().platformApiUrl,
  import.meta.env.VITE_PLATFORM_API_URL,
  import.meta.env.DEV ? '' : 'http://localhost:20200',
)

// Agent 实时 WebSocket 基址。
export const agentWsUrl: string =
  pick(runtimeConfig().agentWsUrl, import.meta.env.VITE_AGENT_WS_URL) ||
  (import.meta.env.DEV && typeof window !== 'undefined'
    ? window.location.origin.replace(/^http/, 'ws')
    : 'ws://localhost:20201')

// Grafana 仪表盘地址。
export const grafanaUrl: string = pick(
  runtimeConfig().grafanaUrl,
  import.meta.env.VITE_GRAFANA_URL,
  'http://localhost:20230/d/workama-overview/workama-platform-overview?orgId=1&kiosk=1',
)

// 对外暴露共享设计令牌（颜色/间距/圆角/阴影/字体），来源为 @workama/config，
// 禁止在端内复制常量。
export { designTokens }
