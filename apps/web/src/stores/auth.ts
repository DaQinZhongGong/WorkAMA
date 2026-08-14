import { create } from 'zustand'
import { api } from '../api'
import type { User } from '../types'

interface AuthState {
  user: User | null
  loading: boolean
  authenticated: boolean
  isAdmin: boolean
  setUser: (user: User | null) => void
  setLoading: (loading: boolean) => void
  refreshUser: () => Promise<void>
  logout: () => Promise<void>
}

export const useAuthStore = create<AuthState>((set, get) => ({
  user: null,
  loading: true,
  authenticated: false,
  isAdmin: false,

  setUser: (user) =>
    set({
      user,
      authenticated: Boolean(user),
      isAdmin: user?.role === 'owner' || user?.role === 'admin',
    }),

  setLoading: (loading) => set({ loading }),

  refreshUser: async () => {
    try {
      const me = await api.get<User>('/api/v1/auth/me')
      get().setUser(me)
    } catch {
      get().setUser(null)
    }
  },

  logout: async () => {
    try {
      await api.post('/api/v1/auth/logout')
    } finally {
      get().setUser(null)
    }
  },
}))
