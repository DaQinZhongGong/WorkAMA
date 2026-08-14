/**
 * @workama/core-state
 *
 * 共享状态层（React 各端共用）：
 *   - UI 偏好 store 工厂（基于 zustand）
 *   - Agent 会话投影 reducer / 事件转换 helper
 *
 * 依赖：`@workama/event-renderer`、`zustand`；
 * 不依赖任何 React 组件、CSS、网络层。
 */
import { create } from 'zustand'
import {
  applyEvent,
  emptyProjection,
  projectEvents,
  type AgentEvent,
  type SessionProjection,
} from '@workama/event-renderer'

export type { AgentEvent, SessionProjection } from '@workama/event-renderer'

/* -------------------------------------------------------------------------- */
/* UI 偏好 store                                                               */
/* -------------------------------------------------------------------------- */

export type UiState = {
  sidebarCollapsed: boolean
  setSidebarCollapsed: (next: boolean | ((current: boolean) => boolean)) => void
}

/**
 * 创建一个 UI 偏好 store。各端通过 `useUiStore()` 消费同一份 store 实例，
 * 避免在 web/desktop/share/mobile 各自重新实现 sidebar 折叠等通用偏好。
 */
export const useUiStore = create<UiState>((set) => ({
  sidebarCollapsed: false,
  setSidebarCollapsed: (next) =>
    set((state) => ({
      sidebarCollapsed:
        typeof next === 'function' ? next(state.sidebarCollapsed) : next,
    })),
}))

/* -------------------------------------------------------------------------- */
/* Agent 会话投影 helper                                                       */
/* -------------------------------------------------------------------------- */

/**
 * 从一串事件计算会话投影（用于初始化或快照恢复）。
 * 直接转发到 `@workama/event-renderer` 的 `projectEvents`。
 */
export function projectSnapshot(events: AgentEvent[]): SessionProjection {
  return projectEvents(events)
}

/**
 * 把单个流式事件应用到现有投影上（不可变更新）。
 * 直接转发到 `@workama/event-renderer` 的 `applyEvent`。
 */
export function applyStreamEvent(
  state: SessionProjection,
  event: AgentEvent,
): SessionProjection {
  return applyEvent(state, event)
}

/** 返回一个空的会话投影，便于流式建立连接前的占位。 */
export function emptySessionProjection(): SessionProjection {
  return emptyProjection()
}

/**
 * 将任意 `unknown` 值规整为 `AgentEvent | null`。
 * 用于消费 WebSocket / SSE / postMessage 等不可信来源时做防御性解析。
 */
export function asAgentEvent(value: unknown): AgentEvent | null {
  if (!value || typeof value !== 'object') return null
  const item = value as Record<string, unknown>
  if (typeof item.type !== 'string') return null
  return {
    id: typeof item.id === 'string' ? item.id : undefined,
    seq: typeof item.seq === 'number' ? item.seq : undefined,
    type: item.type,
    payload:
      item.payload && typeof item.payload === 'object'
        ? (item.payload as Record<string, unknown>)
        : {},
  }
}
