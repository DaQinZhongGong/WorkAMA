import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { api, clearWebAccessToken, setWebAccessToken } from './api'
import type { User } from './types'
type AuthResponse = { access_token: string; user: User }
type LoginResponse = AuthResponse | { mfa_required: true; mfa_ticket: string }
type AuthContextValue = { user: User | null; loading: boolean; authenticated: boolean; isAdmin: boolean; login: (email: string, password: string) => Promise<{ mfaRequired: boolean }>; register: (email: string, password: string, displayName: string) => Promise<unknown>; logout: () => Promise<void>; refreshUser: () => Promise<void> }
const AuthContext = createContext<AuthContextValue | null>(null)
export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null); const [loading, setLoading] = useState(true)
  const bootstrap = useCallback(async () => { try { const refreshed = await api.post<{ access_token: string }>('/api/v1/auth/refresh'); setWebAccessToken(refreshed.access_token); setUser(await api.get<User>('/api/v1/auth/me')) } catch { clearWebAccessToken(); setUser(null) } finally { setLoading(false) } }, [])
  useEffect(() => { void bootstrap() }, [bootstrap])
  const value = useMemo<AuthContextValue>(() => ({ user, loading, authenticated: Boolean(user), isAdmin: user?.role === 'owner' || user?.role === 'admin', async login(email, password) { const result = await api.post<LoginResponse>('/api/v1/auth/login', { email, password }); if ('mfa_required' in result) { sessionStorage.setItem('workama_mfa_ticket', result.mfa_ticket); return { mfaRequired: true } }; setWebAccessToken(result.access_token); setUser(result.user); return { mfaRequired: false } }, async register(email, password, displayName) { return api.post('/api/v1/auth/register', { email, password, display_name: displayName }) }, async refreshUser() { setUser(await api.get<User>('/api/v1/auth/me')) }, async logout() { try { await api.post('/api/v1/auth/logout') } finally { clearWebAccessToken(); setUser(null) } } }), [loading, user])
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
export function useAuth() { const value = useContext(AuthContext); if (!value) throw new Error('useAuth must be used inside AuthProvider'); return value }
