/**
 * Authentication state.
 *
 * The token is restored from localStorage on load and immediately validated against
 * /auth/me, so a stale token from a previous deployment does not leave the UI
 * pretending to be signed in.
 *
 * `isAdmin` here decides what the navigation shows. It is *not* what decides what the
 * platform permits: every administrator-only route is enforced on the backend
 * (specification section 10), and hiding a button is presentation, not permission.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

import { api, getToken, setCsrfToken, setToken } from '../api/client'
import type { User } from '../api/types'

interface AuthState {
  user: User | null
  loading: boolean
  isAdmin: boolean
  /** Sign in with a username or an email address. */
  signIn: (username: string, password: string) => Promise<void>
  signOut: () => Promise<void>
  refresh: () => Promise<void>
}

const AuthContext = createContext<AuthState | undefined>(undefined)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    try {
      setUser(await api.me())
    } catch {
      setToken(null)
      setCsrfToken(null)
      setUser(null)
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    async function restore() {
      if (!getToken()) {
        setLoading(false)
        return
      }
      try {
        const me = await api.me()
        if (!cancelled) setUser(me)
      } catch {
        setToken(null)
        setCsrfToken(null)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void restore()
    return () => { cancelled = true }
  }, [])

  const signIn = useCallback(async (username: string, password: string) => {
    const result = await api.login(username, password)
    setToken(result.access_token)
    setCsrfToken(result.csrf_token)
    setUser(result.user)
  }, [])

  const signOut = useCallback(async () => {
    try {
      await api.logout()
    } finally {
      setToken(null)
      setCsrfToken(null)
      setUser(null)
    }
  }, [])

  const value = useMemo<AuthState>(
    () => ({ user, loading, isAdmin: user?.role === 'ADMIN', signIn, signOut, refresh }),
    [user, loading, signIn, signOut, refresh],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside an AuthProvider')
  return context
}
