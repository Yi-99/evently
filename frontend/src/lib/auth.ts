// Mock auth so the sign-in flow can be demoed without a backend.
// TODO: replace with real auth (Google OAuth + email accounts) once the backend exists.
// Only the profile is kept in this browser; passwords are never stored.

export interface User {
  name: string
  email: string
  provider: 'email' | 'google'
}

// Not 'evently.user': that key holds the backend account (api/token.ts).
const KEY = 'evently.localUser'

export function loadUser(): User | null {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? sessionStorage.getItem(KEY) ?? 'null')
    return raw && typeof raw.email === 'string' ? raw : null
  } catch {
    return null
  }
}

// `remember` keeps the account across browser restarts; otherwise it lasts until the tab closes.
function store(user: User | null, remember = true) {
  try {
    localStorage.removeItem(KEY)
    sessionStorage.removeItem(KEY)
    if (user) (remember ? localStorage : sessionStorage).setItem(KEY, JSON.stringify(user))
  } catch {
    // Storage blocked: the user stays signed in until the tab closes.
  }
}

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms))

export const validEmail = (email: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim())

// "jordan.lee@company.example" -> "Jordan Lee"
const nameFromEmail = (email: string) =>
  email
    .split('@')[0]
    .split(/[._-]+/)
    .filter(Boolean)
    .map((w) => w[0].toUpperCase() + w.slice(1))
    .join(' ')

export async function signInWithEmail(email: string, password: string, name?: string, remember = true): Promise<User> {
  await wait(700)
  if (!validEmail(email)) throw new Error('Enter a valid email address.')
  if (!password) throw new Error('Enter your password.')
  const user: User = { name: name?.trim() || nameFromEmail(email), email: email.trim(), provider: 'email' }
  store(user, remember)
  return user
}

export async function signInWithGoogle(remember = true): Promise<User> {
  await wait(900)
  const user: User = { name: 'Demo Planner', email: 'planner@evently.example', provider: 'google' }
  store(user, remember)
  return user
}

export function signOut() {
  store(null)
}
