import { Check, Eye, EyeOff, KeyRound, Loader2, ShieldCheck } from 'lucide-react'
import { useState, type FormEvent, type ReactNode } from 'react'
import { login, register } from '../api/auth'
import { signInWithEmail, signInWithGoogle, validEmail, type User } from '../lib/auth'
import { GoogleG, Logo, Ribbon } from './MarketingPage'

export type AuthMode = 'signin' | 'signup'

interface Props {
  mode: AuthMode
  onMode: (mode: AuthMode) => void
  onDone: (user: User) => void
  onBack: () => void
  // The Evently server requires accounts: email sign-in / sign-up go to its /auth endpoints. Otherwise the
  // demo's local profile (lib/auth) is used, so the app works without a backend.
  backend?: boolean
}

const COPY = {
  signin: { title: 'Sign in to your account', cta: 'Sign in', or: 'Or sign in with', swap: 'New to Evently?', swapCta: 'Create account' },
  signup: { title: 'Create your account', cta: 'Create account', or: 'Or sign up with', swap: 'Already have an account?', swapCta: 'Sign in' },
} as const

// Centered card over the marketing ribbon, like the hero it came from.
export default function LoginPage({ mode, onMode, onDone, onBack, backend = false }: Props) {
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [show, setShow] = useState(false)
  const [remember, setRemember] = useState(true)
  const [busy, setBusy] = useState<'email' | 'google' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const copy = COPY[mode]
  const signup = mode === 'signup'
  const filled = email.trim() && password && (!signup || name.trim())

  const swap = () => {
    setError(null)
    onMode(signup ? 'signin' : 'signup')
  }

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (busy) return
    if (signup && !name.trim()) return setError('Enter your name.')
    if (!validEmail(email)) return setError('Enter a valid email address.')
    if (signup && password.length < 8) return setError('Use at least 8 characters for your password.')
    if (!password) return setError('Enter your password.')
    setError(null)
    setBusy('email')
    try {
      if (backend) {
        const u = await (signup ? register(email.trim(), password, name.trim()) : login(email.trim(), password))
        onDone({ name: u.name || u.email, email: u.email, provider: 'email' })
      } else onDone(await signInWithEmail(email, password, signup ? name : undefined, remember))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Something went wrong. Try again.')
      setBusy(null)
    }
  }

  const google = async () => {
    if (busy || backend) return
    setError(null)
    setBusy('google')
    try {
      onDone(await signInWithGoogle(remember))
    } catch {
      setError('Google sign-in failed. Try again.')
      setBusy(null)
    }
  }

  return (
    <div className="relative h-full overflow-y-auto bg-white">
      <div className="pointer-events-none fixed inset-0">
        <Ribbon />
      </div>
      {/* Faint page frame: a rule under the header and guides down both sides. */}
      <div aria-hidden className="pointer-events-none fixed inset-x-0 top-[72px] h-px bg-gray-900/5" />
      <div aria-hidden className="pointer-events-none fixed inset-y-0 left-1/2 hidden w-full max-w-6xl -translate-x-1/2 border-x border-gray-900/5 lg:block" />

      <header className="relative mx-auto flex h-[72px] max-w-6xl items-center px-6">
        <button onClick={onBack} aria-label="Back to home">
          <Logo />
        </button>
      </header>

      <main className="relative flex justify-center px-4 pt-10 pb-16 sm:pt-16">
        <div key={mode} className="w-full max-w-[460px] animate-rise overflow-hidden rounded-[28px] bg-white shadow-2xl ring-1 shadow-indigo-500/10 ring-gray-900/5">
          <div className="px-6 pt-10 pb-8 sm:px-12">
            <h1 className="text-2xl font-bold tracking-tight text-gray-800">{copy.title}</h1>

            <form onSubmit={submit} noValidate className="mt-8 space-y-5">
              {signup && (
                <Field label="Name">
                  <input value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" autoFocus className={INPUT} />
                </Field>
              )}
              <Field label="Email">
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  autoComplete="email"
                  autoFocus={!signup}
                  className={INPUT}
                />
              </Field>
              <Field
                label="Password"
                extra={
                  !signup && (
                    <span title="Coming soon" className="cursor-default text-sm font-semibold text-accent">
                      Forgot your password?
                    </span>
                  )
                }
              >
                <div className="relative">
                  <input
                    type={show ? 'text' : 'password'}
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    autoComplete={signup ? 'new-password' : 'current-password'}
                    placeholder={signup ? 'At least 8 characters' : undefined}
                    className={`${INPUT} pr-11`}
                  />
                  <button
                    type="button"
                    onClick={() => setShow((s) => !s)}
                    aria-label={show ? 'Hide password' : 'Show password'}
                    className="absolute inset-y-0 right-0 grid w-11 place-items-center text-muted hover:text-ink"
                  >
                    {show ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                </div>
              </Field>

              {/* The server's session token is always kept on this device, so the choice only applies to the demo profile. */}
              {!backend && (
                <label className="flex cursor-pointer items-center gap-2.5 text-sm text-gray-700 select-none">
                  <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} className="peer sr-only" />
                  <span className="grid size-[18px] place-items-center rounded-md bg-white text-white ring-1 ring-gray-900/20 transition peer-checked:bg-accent peer-checked:ring-accent peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-accent">
                    {remember && <Check size={12} strokeWidth={3} />}
                  </span>
                  Remember me on this device
                </label>
              )}

              {error && (
                <p role="alert" className="rounded-xl bg-warn-soft px-3 py-2 text-sm text-warn">
                  {error}
                </p>
              )}

              <button
                type="submit"
                disabled={!!busy}
                className={`flex w-full items-center justify-center gap-2 rounded-full px-4 py-3 text-[15px] font-semibold text-white transition ${
                  filled ? 'bg-accent shadow-sm shadow-indigo-500/30 hover:bg-indigo-500' : 'bg-indigo-300'
                } disabled:opacity-70`}
              >
                {busy === 'email' && <Loader2 size={16} className="animate-spin" />}
                {copy.cta}
              </button>
            </form>

            <div className="my-6 flex items-center gap-3 text-sm text-gray-500">
              <span className="h-px flex-1 bg-line" /> {copy.or} <span className="h-px flex-1 bg-line" />
            </div>

            <div className="space-y-3">
              {/* Google sign-in is still a local mock; it can't create a server session, so it's hidden when accounts are required. */}
              {!backend && (
                <Provider onClick={google} disabled={!!busy}>
                  {busy === 'google' ? <Loader2 size={16} className="animate-spin" /> : <GoogleG />} Google
                </Provider>
              )}
              <Provider onClick={() => setError(`Passkeys are coming soon. Use ${backend ? 'email' : 'Google or email'} for now.`)} disabled={!!busy}>
                <KeyRound size={16} className="text-accent" /> Passkey
              </Provider>
              <Provider onClick={() => setError(`SSO is coming soon. Use ${backend ? 'email' : 'Google or email'} for now.`)} disabled={!!busy}>
                <ShieldCheck size={16} className="text-accent" /> SSO
              </Provider>
            </div>
          </div>

          <div className="m-2 mt-0 rounded-[20px] bg-gray-50 px-6 py-5 text-center text-sm text-gray-600 ring-1 ring-gray-900/5">
            {copy.swap}{' '}
            <button onClick={swap} className="font-semibold text-accent hover:text-indigo-500">
              {copy.swapCta}
            </button>
          </div>
        </div>
      </main>
    </div>
  )
}

const INPUT =
  'w-full rounded-xl bg-white px-3.5 py-3 text-[15px] ring-1 ring-gray-900/15 outline-none transition placeholder:text-gray-400 focus:ring-2 focus:ring-accent focus:shadow-[0_0_0_5px_rgba(79,70,229,0.12)]'

function Field({ label, extra, children }: { label: string; extra?: ReactNode; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-2 flex items-center justify-between text-sm font-medium text-gray-700">
        {label}
        {extra}
      </span>
      {children}
    </label>
  )
}

function Provider({ children, onClick, disabled }: { children: ReactNode; onClick: () => void; disabled: boolean }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="flex w-full items-center justify-center gap-2.5 rounded-full bg-white px-4 py-3 text-[15px] font-semibold text-gray-800 ring-1 ring-gray-900/15 transition hover:bg-gray-50 hover:ring-gray-900/25 disabled:opacity-60"
    >
      {children}
    </button>
  )
}
