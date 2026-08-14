import { create } from 'zustand'
import { api } from '../api'

interface Workspace {
  id: string
  name: string
  slug?: string
  role?: string
}

interface WorkspaceState {
  workspaces: Workspace[]
  currentWorkspaceId: string | null
  loading: boolean
  error: string | null
  setWorkspaces: (workspaces: Workspace[]) => void
  setCurrentWorkspace: (id: string | null) => void
  fetchWorkspaces: () => Promise<void>
  clearError: () => void
}

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  workspaces: [],
  currentWorkspaceId: null,
  loading: false,
  error: null,

  setWorkspaces: (workspaces) => set({ workspaces }),

  setCurrentWorkspace: (id) => set({ currentWorkspaceId: id }),

  fetchWorkspaces: async () => {
    set({ loading: true, error: null })
    try {
      const payload = await api.get<{ items: Workspace[] }>('/api/v1/workspaces')
      const items = payload?.items ?? []
      set({ workspaces: items, loading: false })
      if (items.length > 0 && !get().currentWorkspaceId) {
        set({ currentWorkspaceId: items[0].id })
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to load workspaces'
      set({ error: message, loading: false })
    }
  },

  clearError: () => set({ error: null }),
}))
