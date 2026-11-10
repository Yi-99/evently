// Access token + signed-in user, persisted in localStorage. Kept apart from auth.ts so the HTTP client
// can read the token without importing the auth store (which itself uses the client).
import type { UserOut } from './types'

const TOKEN_KEY = 'evently.token'
const USER_KEY = 'evently.user'

const read = (key: string): string | null => {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}
const write = (key: string, value: string | null) => {
  try {
    if (value === null) localStorage.removeItem(key)
    else localStorage.setItem(key, value)
  } catch {
    // Storage unavailable (private mode); the session still works in memory.
  }
}

let token: string | null = read(TOKEN_KEY)
let user: UserOut | null = (() => {
  try {
    return JSON.parse(read(USER_KEY) ?? 'null') as UserOut | null
  } catch {
    return null
  }
})()
const listeners = new Set<() => void>()
const notify = () => listeners.forEach((l) => l())

export const getToken = () => token
export const getStoredUser = () => user

export function setSession(next: { token: string | null; user: UserOut | null }) {
  token = next.token
  user = next.user
  write(TOKEN_KEY, token)
  write(USER_KEY, user ? JSON.stringify(user) : null)
  notify()
}

export function setStoredUser(next: UserOut | null) {
  user = next
  write(USER_KEY, user ? JSON.stringify(user) : null)
  notify()
}

export function subscribeSession(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

// Another tab signed in or out.
if (typeof window !== 'undefined') {
  window.addEventListener('storage', (e) => {
    if (e.key !== TOKEN_KEY && e.key !== USER_KEY) return
    token = read(TOKEN_KEY)
    try {
      user = JSON.parse(read(USER_KEY) ?? 'null') as UserOut | null
    } catch {
      user = null
    }
    notify()
  })
}
