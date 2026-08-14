// 集中管理 web 端运行时配置。
//
// 设计说明（参见《340-代码目录与工程结构设计》§3）：
// - 共享设计令牌（design tokens）统一存放在共享包 @workama/config/tokens.json，
//   此处将其作为唯一来源引入并对外暴露，使 @workama/config 包被 web 应用真实消费。
// - 环境变量（VITE_*）仍由构建时（Vite）注入到 import.meta.env；本文件集中读取
//   并提供类型化的 getter，供 api.ts 等模块使用，避免散落的 import.meta.env 直接访问。
//   注意：@workama/config 当前仅导出设计令牌，并未提供环境变量 getter（无 src/index.ts），
//   且任务约束不得修改 packages/config，故此处 getter 就近实现于 web 端；
//   VITE_PLATFORM_API_URL / VITE_AGENT_WS_URL / VITE_GRAFANA_URL 的取值逻辑
//   与历史 api.ts 完全一致，运行时行为不变。
import designTokens from '@workama/config/tokens.json'

// 平台 API 基址：构建时通过 VITE_PLATFORM_API_URL 注入；
// 缺省时开发态走空串（交由 Vite 开发代理），生产态回落到 localhost:20200。
export const platformApiUrl: string =
  import.meta.env.VITE_PLATFORM_API_URL ?? (import.meta.env.DEV ? '' : 'http://localhost:20200')

// Agent 实时 WebSocket 基址：构建时通过 VITE_AGENT_WS_URL 注入；
// 缺省时开发态基于当前页面 origin 推导 ws://，生产态回落到 ws://localhost:20201。
export const agentWsUrl: string =
  import.meta.env.VITE_AGENT_WS_URL ??
  (import.meta.env.DEV && typeof window !== 'undefined'
    ? window.location.origin.replace(/^http/, 'ws')
    : 'ws://localhost:20201')

// Grafana 仪表盘地址：构建时通过 VITE_GRAFANA_URL 注入；
// 缺省回落到本地看板地址（与 apps/web/Dockerfile 默认 ARG 保持一致）。
export const grafanaUrl: string =
  import.meta.env.VITE_GRAFANA_URL ??
  'http://localhost:20230/d/workama-overview/workama-platform-overview?orgId=1&kiosk=1'

// 对外暴露共享设计令牌（颜色/间距/圆角/阴影/字体），供需要 JS 侧取值的场景使用
// （如 ECharts 图表配色、Canvas 绘制等）。来源为 @workama/config，禁止在端内复制常量。
export { designTokens }
