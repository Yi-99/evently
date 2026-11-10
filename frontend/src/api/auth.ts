// Auth store: bearer token + user (localStorage) and the backend's auth mode.
//
// authMode is probed once: GET /health, then GET /auth/me *without* a token.
//   'optional' — the backend answered with its dev user (ENVIRONMENT != production, AUTH_REQUIRED unset): no sign-in.
//   'required' — /auth/me said 401: sign in or register.
//   'offline'  — the backend is unreachable: the app keeps working on sample data.
//   'loading'  — probe in flight.
import { useEffect, useSyncExternalStore } from 'react'
import { api, isApiError } from './client'
import { getStoredUser, getToken, setSession, setStoredUser, subscribeSession } from './token'
import type { HealthOut, TokenOut, UserOut } from './types'

export type AuthMode = 'loading' | 'optional' | 'required' | 'offline'

export interface AuthSnapshot {
  token: string | null
  /** The signed-in user, or the dev user in 'optional' mode. */
  user: UserOut | null
  mode: AuthMode
  health: HealthOut | null
}

let mode: AuthMode = 'loading'
let health: HealthOut | null = null
let devUser: UserOut | null = null
let probing: Promise<void> | null = null
let snapshot: AuthSnapshot = build()
const listeners = new Set<() => void>()

function build(): AuthSnapshot {
  const token = getToken()
  return { token, user: token ? getStoredUser() : devUser, mode, health }
}
function emit() {
  snapshot = build()
  listeners.forEach((l) => l())
}
subscribeSession(emit)

/** Probe the backend's auth mode (once; pass force to re-probe, e.g. after the backend restarts). */
export function probeAuth(force = false): Promise<void> {
  if (probing && !force) return probing
  probing = (async () => {
    try {
      health = await api.get<HealthOut>('/health', { auth: false })
    } catch {
      health = null
    }
    try {
      devUser = await api.get<UserOut>('/auth/me', { auth: false })
      mode = 'optional'
    } catch (e) {
      devUser = null
      mode = isApiError(e, 401) ? 'required' : 'offline'
    }
    // Validate a stored token (clears it on 401) and refresh the cached user.
    if (getToken() && mode !== 'offline') await me().catch(() => undefined)
    emit()
  })()
  return probing
}

export const getAuth = (): AuthSnapshot => snapshot
export const authMode = (): AuthMode => mode

export async function login(email: string, password: string): Promise<UserOut> {
  const out = await api.post<TokenOut>('/auth/login', { email, password }, { auth: false })
  setSession({ token: out.access_token, user: out.user })
  return out.user
}

export async function register(email: string, password: string, name = ''): Promise<UserOut> {
  const out = await api.post<TokenOut>('/auth/register', { email, password, name }, { auth: false })
  setSession({ token: out.access_token, user: out.user })
  return out.user
}

/** The current user per the backend (the dev user when auth is optional and no token is stored). */
export async function me(): Promise<UserOut> {
  const user = await api.get<UserOut>('/auth/me')
  if (getToken()) setStoredUser(user)
  else devUser = user
  emit()
  return user
}

export function logout() {
  setSession({ token: null, user: null })
}

const subscribe = (l: () => void) => {
  listeners.add(l)
  return () => listeners.delete(l)
}

export interface UseAuth extends AuthSnapshot {
  /** True when the UI must show the sign-in form. */
  needsSignIn: boolean
  login: typeof login
  register: typeof register
  logout: typeof logout
  refresh: () => Promise<void>
}

export function useAuth(): UseAuth {
  const s = useSyncExternalStore(subscribe, getAuth)
  useEffect(() => void probeAuth(), [])
  return {
    ...s,
    needsSignIn: s.mode === 'required' && !s.token,
    login,
    register,
    logout,
    refresh: () => probeAuth(true),
  }
}

/** `GET /api/v1/health` (null until loaded, or when the backend is down). */
export function useHealth(): HealthOut | null {
  const s = useSyncExternalStore(subscribe, getAuth)
  useEffect(() => void probeAuth(), [])
  return s.health
}
