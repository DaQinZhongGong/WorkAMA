// 认证状态：基于 zustand，内存中持有 token 与 user
import { create } from 'zustand'
import type { User } from '../services/api'

interface AuthState {
  token: string | null
  user: User | null
  // 写入登录态
  login: (token: string, user: User) => void
  // 清除登录态
  logout: () => void
  // 认证状态 getter
  isAuthenticated: () => boolean
}

export const useAuthStore = create<AuthState>((set, get) => ({
  token: null,
  user: null,
  login: (token, user) => set({ token, user }),
  logout: () => set({ token: null, user: null }),
  isAuthenticated: () => get().token !== null,
}))
