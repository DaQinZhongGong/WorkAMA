export type Role = 'owner' | 'admin' | 'member' | 'viewer'
export type User = { id: string; email: string; display_name: string; workspace_id: string; org_id: string; role: Role; onboarding_completed: boolean }
export type Session = { id: string; title: string; model: string; status: string; toolset: string[]; canvas_enabled: boolean; used_steps: number; max_steps: number; used_credits: number; max_credits: number; updated_at?: string }
export type ListResponse<T> = { items: T[]; next_cursor?: string | null; has_more?: boolean }
export type Dataset = {
  id: string
  name: string
  description?: string
  document_count?: number
  status?: string
  version?: number
  active_generation_id?: string | null
  embedding_model?: string
  embedding_profile?: Record<string, unknown> | null
  retrieval_config?: Record<string, unknown> | null
  stats?: Record<string, unknown> | null
  created_at?: string
  updated_at?: string
}
export type Workflow = { id: string; name: string; description?: string; status?: string; version?: number; updated_at?: string }
export type Project = { id: string; name: string; slug?: string; description?: string; status?: string; updated_at?: string }
export type AgentEvent = { id?: string; seq?: number; type: string; payload?: Record<string, unknown> }
