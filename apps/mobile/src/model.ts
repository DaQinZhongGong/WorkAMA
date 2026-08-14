// Mobile 端会话投影助手统一来自 @workama/core-state，
// 此处仅保留 emptyMobileProjection 别名以兼容现有引用。
export {
  applyStreamEvent,
  asAgentEvent,
  projectSnapshot,
  emptySessionProjection as emptyMobileProjection,
  type AgentEvent,
  type SessionProjection,
} from '@workama/core-state'
