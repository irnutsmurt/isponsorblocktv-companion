import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import { api, onUnauthorized } from './api'
import { stopLive } from './live'

export type Me = {
  username: string; user_id: number
  session: { created_at: string; expires_at: string; remember: boolean; auth_method: string; recent_auth: boolean }
  passkeys: any[]; passkey_origin: string | null; passkey_blocker: string | null; has_recovery_code: boolean; insecure_transport: boolean
}
type AuthState = { status: 'loading' | 'setup' | 'anon' | 'authed' | 'error'; me: Me | null; setupInfo: any; error?: string; refresh: () => Promise<void>; logout: () => Promise<void> }
const Ctx = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<Omit<AuthState, 'refresh' | 'logout'>>({ status: 'loading', me: null, setupInfo: null })
  const refresh = useCallback(async () => {
    try {
      const setup = await api('/api/auth/setup/status')
      if (!setup.setup_complete) { setState({ status: 'setup', me: null, setupInfo: setup }); return }
      try {
        const me = await api<Me>('/api/auth/me')
        setState({ status: 'authed', me, setupInfo: setup })
      } catch (e: any) {
        if (e?.status === 401) setState({ status: 'anon', me: null, setupInfo: setup })
        else setState({ status: 'error', me: null, setupInfo: setup, error: e?.message })
      }
    } catch (e: any) {
      setState({ status: 'error', me: null, setupInfo: null, error: e?.message })
    }
  }, [])
  const logout = useCallback(async () => {
    try { await api('/api/auth/logout', { method: 'POST', body: {} }) } catch { /* ignore */ }
    stopLive()
    try { sessionStorage.clear() } catch { /* ignore */ }
    setState((s) => ({ ...s, status: 'anon', me: null }))
  }, [])
  useEffect(() => { refresh() }, [refresh])
  useEffect(() => onUnauthorized(() => { stopLive(); setState((s) => (s.status === 'authed' ? { ...s, status: 'anon', me: null } : s)) }), [])
  return <Ctx.Provider value={{ ...state, refresh, logout }}>{children}</Ctx.Provider>
}
export function useAuth() { const c = useContext(Ctx); if (!c) throw new Error('no auth'); return c }
