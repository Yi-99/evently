import { LoaderCircle } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { useAuth } from '../api/auth'

interface Props {
  /** Called after a successful sign-in or registration. */
  onSignedIn?: () => void
  onBack?: () => void
}

// Minimal email/password sign-in, shown only when the backend requires auth (production or AUTH_REQUIRED).
export default function SignIn({ onSignedIn, onBack }: Props) {
  const { login, register } = useAuth()
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      if (mode === 'login') await login(email.trim(), password)
      else await register(email.trim(), password, name.trim())
      onSignedIn?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Something went wrong.')
    } finally {
      setBusy(false)
    }
  }

  const input =
    'w-full rounded-xl border border-line bg-white px-3.5 py-2.5 text-sm text-ink outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20'

  return (
    <main className="grid h-full place-items-center bg-gray-100 p-6">
      <form onSubmit={submit} className="w-full max-w-sm rounded-2xl bg-white p-8 shadow-sm ring-1 ring-gray-900/5">
        <h1 className="text-xl font-semibold text-ink">{mode === 'login' ? 'Sign in to Evently' : 'Create your account'}</h1>
        <p className="mt-1 text-sm text-muted">{mode === 'login' ? 'Welcome back.' : 'Plan your first event in minutes.'}</p>

        <div className="mt-6 space-y-3">
          {mode === 'register' && (
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-muted">Name</span>
              <input className={input} value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" />
            </label>
          )}
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-muted">Email</span>
            <input
              className={input}
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="email"
              autoFocus
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-muted">Password</span>
            <input
              className={input}
              type="password"
              required
              minLength={mode === 'register' ? 8 : undefined}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            />
          </label>
        </div>

        {error && <p className="mt-3 rounded-lg bg-warn-soft px-3 py-2 text-sm text-warn">{error}</p>}

        <button
          type="submit"
          disabled={busy}
          className="mt-6 flex w-full items-center justify-center gap-2 rounded-xl bg-accent px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-indigo-500 disabled:opacity-60"
        >
          {busy && <LoaderCircle size={16} className="animate-spin" />}
          {mode === 'login' ? 'Sign in' : 'Create account'}
        </button>

        <div className="mt-4 flex items-center justify-between text-sm">
          <button
            type="button"
            className="font-medium text-accent hover:underline"
            onClick={() => {
              setMode(mode === 'login' ? 'register' : 'login')
              setError(null)
            }}
          >
            {mode === 'login' ? 'Create an account' : 'I already have an account'}
          </button>
          {onBack && (
            <button type="button" className="text-muted hover:text-ink" onClick={onBack}>
              Back
            </button>
          )}
        </div>
      </form>
    </main>
  )
}
