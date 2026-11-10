// Presenter view for a live session (/present/:sessionId), used by the host.
//
// Auth: the host token the deck viewer stored in sessionStorage (`evently.session.{id}.host_token`)
// drives the control socket, the audio socket and the deck iframe. Without one (new tab, reload in
// another window) the event owner mints a fresh host token via POST /live-sessions/{id}/participants.
// Start / end / settings are owner REST routes (user access token, or the dev user).
import {
  ChevronLeft,
  ChevronRight,
  Copy,
  ExternalLink,
  LoaderCircle,
  Mic,
  MicOff,
  Play,
  Presentation,
  Settings2,
  Square,
  Users,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useAuth, useHealth } from '../../api/auth'
import { isApiError } from '../../api/client'
import SignIn from '../../components/SignIn'
import { DeckFrame, Dot, FlashStyles, LevelMeter, QrCode, SlidePlaceholder, TranscriptFeed } from './components'
import {
  STT_PROVIDERS,
  endSession,
  joinLink,
  mintHostToken,
  patchSettings,
  readHostToken,
  readSettings,
  startSession,
  storeHostToken,
  clearHostToken,
  type AutoAdvance,
  type LiveSessionOut,
  type LiveSettings,
} from './session'
import { useAudioSpeaker, type SpeakerStatus } from './useAudioSpeaker'
import { browserSttSupported, useBrowserStt } from './useBrowserStt'
import { describeChange, isAutoChange, useSessionSocket, type UseSessionSocket } from './useSessionSocket'

export interface PresenterPageProps {
  /** Live session id from the URL (/present/:sessionId). */
  sessionId: string
}

export default function PresenterPage({ sessionId }: PresenterPageProps) {
  const auth = useAuth()
  const [token, setToken] = useState(() => readHostToken(sessionId))
  const [mintError, setMintError] = useState<string | null>(null)
  const [minting, setMinting] = useState(false)

  const userName = auth.user?.name || 'Host'
  const mint = useCallback(async () => {
    setMinting(true)
    setMintError(null)
    try {
      const out = await mintHostToken(sessionId, userName)
      storeHostToken(sessionId, out.token)
      setToken(out.token)
    } catch (e) {
      setMintError(
        isApiError(e, 404)
          ? 'This session was not found, or it belongs to someone else’s event.'
          : isApiError(e, 409)
            ? 'This session has ended.'
            : isApiError(e, 401)
              ? 'Sign in as the event owner to present this session.'
              : e instanceof Error
                ? e.message
                : 'Could not open the session.',
      )
    } finally {
      setMinting(false)
    }
  }, [sessionId, userName])

  // No stored host token: the event owner can mint one (once the auth probe has settled).
  const tried = useRef(false)
  useEffect(() => {
    if (token || tried.current || auth.mode === 'loading' || auth.needsSignIn) return
    tried.current = true
    void mint()
  }, [token, auth.mode, auth.needsSignIn, mint])

  if (!token) {
    if (auth.needsSignIn) return <SignIn onBack={() => window.location.assign('/')} />
    return (
      <main className="grid h-full place-items-center bg-gray-100 p-6">
        <div className="w-full max-w-sm rounded-2xl bg-white p-8 text-center shadow-sm ring-1 ring-gray-900/5">
          <Presentation className="mx-auto text-accent" size={28} />
          <h1 className="mt-4 text-lg font-semibold text-ink">Presenter view</h1>
          {mintError ? (
            <>
              <p className="mt-2 text-sm text-warn">{mintError}</p>
              <p className="mt-2 text-sm text-muted">Open the session again from the deck viewer, or retry.</p>
              <div className="mt-5 flex justify-center gap-2">
                <button onClick={() => void mint()} disabled={minting} className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white disabled:opacity-60">
                  Retry
                </button>
                <a href="/" className="rounded-lg px-4 py-2 text-sm font-medium text-ink ring-1 ring-line hover:bg-gray-50">
                  Back to Evently
                </a>
              </div>
            </>
          ) : (
            <p className="mt-3 flex items-center justify-center gap-2 text-sm text-muted">
              <LoaderCircle className="animate-spin" size={16} /> Opening session…
            </p>
          )}
        </div>
      </main>
    )
  }

  return (
    <PresenterLive
      key={token}
      sessionId={sessionId}
      token={token}
      onTokenRejected={() => {
        clearHostToken(sessionId)
        tried.current = false
        setToken(null)
      }}
    />
  )
}

// --- live view ------------------------------------------------------------------------------

const SPEAKER_LABEL: Record<SpeakerStatus, string> = {
  off: 'Microphone off',
  starting: 'Starting microphone…',
  connecting: 'Connecting audio…',
  live: 'Broadcasting',
  reconnecting: 'Reconnecting audio…',
  error: 'Microphone stopped',
}

function PresenterLive({ sessionId, token, onTokenRejected }: { sessionId: string; token: string; onTokenRejected: () => void }) {
  const sock = useSessionSocket(sessionId, token)
  const speaker = useAudioSpeaker(sessionId, token)
  const health = useHealth()
  const session = sock.session
  const settings = readSettings(session?.settings)
  const status = session?.status ?? 'scheduled'
  const index = session?.current_slide_index ?? 0
  const count = sock.slideCount
  const slides = sock.slides
  const [actionError, setActionError] = useState<string | null>(null)
  const [busy, setBusy] = useState<'start' | 'end' | null>(null)

  // Browser STT: explicit `browser`, or `auto` with no server STT key configured.
  const noServerKeys = health ? !Object.values(health.stt_keys).some(Boolean) : false
  const detectedBrowserStt = settings.stt_provider === 'browser' || (settings.stt_provider === 'auto' && noServerKeys) || sock.stt?.provider === 'browser'
  const [browserOverride, setBrowserOverride] = useState<boolean | null>(null)
  const useBrowser = (browserOverride ?? detectedBrowserStt) && browserSttSupported
  const micOn = speaker.status !== 'off' && speaker.status !== 'error'
  const send = sock.send
  const onText = useCallback((text: string, isFinal: boolean) => void send({ type: 'transcript.text', data: { text, is_final: isFinal } }), [send])
  const stt = useBrowserStt(useBrowser && micOn && status === 'live', settings.language, onText)

  // Stop the mic when the session ends.
  const stopMic = speaker.stop
  useEffect(() => {
    if (status === 'ended') stopMic()
  }, [status, stopMic])

  const goto = useCallback(
    (i: number) => {
      if (!count) return
      send({ type: 'slide.goto', data: { index: Math.max(0, Math.min(count - 1, i)) } })
    },
    [send, count],
  )

  // Keyboard: → / PageDown / Space = next, ← / PageUp = previous.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      if (t && (t.isContentEditable || ['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON'].includes(t.tagName))) return
      if (['ArrowRight', 'PageDown', ' '].includes(e.key)) {
        e.preventDefault()
        goto(index + 1)
      } else if (['ArrowLeft', 'PageUp'].includes(e.key)) {
        e.preventDefault()
        goto(index - 1)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [goto, index])

  const lifecycle = async (action: 'start' | 'end') => {
    if (action === 'end' && !window.confirm('End this live session for everyone?')) return
    setBusy(action)
    setActionError(null)
    try {
      sock.applySession(await (action === 'start' ? startSession(sessionId) : endSession(sessionId)))
    } catch (e) {
      setActionError(
        isApiError(e, 401) || isApiError(e, 404) ? 'Only the event owner can start or end this session (sign in on the main app).' : e instanceof Error ? e.message : String(e),
      )
    } finally {
      setBusy(null)
    }
  }

  const lastChange = sock.slideChanges.length ? sock.slideChanges[sock.slideChanges.length - 1] : null
  const connected = sock.participants.filter((p) => p.connected && p.role === 'listener').length
  const tokenRejected = sock.status === 'closed'

  if (tokenRejected)
    return (
      <main className="grid h-full place-items-center bg-gray-100 p-6">
        <div className="w-full max-w-sm rounded-2xl bg-white p-8 text-center shadow-sm ring-1 ring-gray-900/5">
          <h1 className="text-lg font-semibold text-ink">Can’t open this session</h1>
          <p className="mt-2 text-sm text-muted">{sock.error}</p>
          <button onClick={onTokenRejected} className="mt-5 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white">
            Get a new host token
          </button>
        </div>
      </main>
    )

  return (
    <main className="flex h-full flex-col bg-gray-50">
      <FlashStyles />
      {/* header */}
      <header className="flex flex-wrap items-center gap-3 border-b border-line bg-white px-4 py-2.5">
        <Presentation className="text-accent" size={20} />
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-sm font-semibold text-ink">{session?.title ?? 'Live session'}</h1>
          <p className="flex items-center gap-1.5 text-xs text-muted">
            <Dot tone={sock.status === 'open' ? 'ok' : sock.status === 'connecting' ? 'busy' : 'warn'} />
            {sock.status === 'open' ? 'Connected' : sock.status === 'connecting' ? 'Connecting…' : 'Reconnecting…'}
            {sock.error && sock.status === 'open' && <span className="text-warn">· {sock.error}</span>}
          </p>
        </div>
        <StatusPill status={status} />
        <SettingsButton sessionId={sessionId} settings={settings} onSaved={sock.applySession} />
        {status === 'scheduled' && (
          <button
            onClick={() => void lifecycle('start')}
            disabled={busy !== null}
            className="flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-60"
          >
            {busy === 'start' ? <LoaderCircle className="animate-spin" size={15} /> : <Play size={15} />} Start session
          </button>
        )}
        {status === 'live' && (
          <button
            onClick={() => void lifecycle('end')}
            disabled={busy !== null}
            className="flex items-center gap-1.5 rounded-lg bg-rose-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-rose-700 disabled:opacity-60"
          >
            {busy === 'end' ? <LoaderCircle className="animate-spin" size={15} /> : <Square size={14} />} End session
          </button>
        )}
      </header>
      {actionError && (
        <div className="flex items-center gap-2 bg-warn-soft px-4 py-2 text-sm text-warn">
          <span className="flex-1">{actionError}</span>
          <button onClick={() => setActionError(null)} aria-label="Dismiss">
            <X size={15} />
          </button>
        </div>
      )}

      <div className="grid min-h-0 flex-1 gap-4 overflow-y-auto p-4 lg:grid-cols-[minmax(0,1fr)_360px] lg:overflow-hidden">
        {/* deck + navigation */}
        <section className="flex min-h-0 flex-col gap-3 lg:overflow-y-auto">
          <div className="relative">
            {session?.presentation_id && status !== 'ended' ? (
              <DeckFrame key={`${session.presentation_id}:${token}`} presentationId={session.presentation_id} token={token} index={index} title={slides[index]?.title} />
            ) : (
              <SlidePlaceholder index={index} count={count} title={status === 'ended' ? 'Session ended' : slides[index]?.title} />
            )}
            {lastChange && isAutoChange(lastChange) && (
              <div
                key={lastChange.at}
                className="pointer-events-none absolute top-3 left-1/2 -translate-x-1/2 rounded-full bg-accent px-3 py-1 text-xs font-medium text-white shadow"
                style={{ animation: 'evently-fade 4s ease-out forwards' }}
              >
                Auto-advanced by {describeChange(lastChange)}
              </div>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => goto(index - 1)}
              disabled={!count || index <= 0 || sock.status !== 'open'}
              className="flex items-center gap-1 rounded-lg bg-white px-3 py-2 text-sm font-medium text-ink ring-1 ring-line hover:bg-gray-50 disabled:opacity-40"
            >
              <ChevronLeft size={16} /> Prev
            </button>
            <select
              value={index}
              onChange={(e) => goto(Number(e.target.value))}
              disabled={!count || sock.status !== 'open'}
              className="min-w-0 flex-1 rounded-lg bg-white px-3 py-2 text-sm text-ink ring-1 ring-line disabled:opacity-40"
              aria-label="Go to slide"
            >
              {count ? (
                slides.map((s, i) => (
                  <option key={s.id} value={i}>
                    {i + 1} / {count} · {s.title || 'Untitled'}
                  </option>
                ))
              ) : (
                <option value={0}>No slides</option>
              )}
            </select>
            <button
              onClick={() => goto(index + 1)}
              disabled={!count || index >= count - 1 || sock.status !== 'open'}
              className="flex items-center gap-1 rounded-lg bg-accent px-3 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-40"
            >
              Next <ChevronRight size={16} />
            </button>
          </div>
          {count > 0 && <Cues slides={slides} index={index} mode={settings.auto_advance} />}
        </section>

        {/* sidebar */}
        <aside className="flex min-h-0 flex-col gap-4 lg:overflow-y-auto">
          {session && <JoinCard code={session.join_code} listeners={connected} audioListeners={sock.audio?.listeners ?? null} />}
          <MicCard
            sessionLive={status === 'live'}
            speaker={speaker}
            settings={settings}
            useBrowser={useBrowser}
            detectedBrowser={detectedBrowserStt}
            onBrowserChange={setBrowserOverride}
            browserStt={stt}
            serverStt={sock.stt}
          />
          <section className="flex min-h-48 flex-1 flex-col rounded-xl bg-white p-4 ring-1 ring-line">
            <h2 className="mb-2 text-xs font-semibold tracking-wide text-muted uppercase">Transcript</h2>
            <TranscriptFeed
              segments={sock.transcript}
              partial={sock.partial}
              slideChanges={sock.slideChanges}
              slides={slides}
              className="min-h-32 flex-1"
              empty={status === 'live' ? 'Turn the microphone on to transcribe.' : 'The transcript appears here once the session is live.'}
            />
          </section>
        </aside>
      </div>
    </main>
  )
}

function StatusPill({ status }: { status: string }) {
  const style =
    status === 'live' ? 'bg-rose-100 text-rose-700' : status === 'ended' ? 'bg-gray-200 text-gray-600' : 'bg-amber-100 text-amber-800'
  return (
    <span className={`flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold ${style}`}>
      {status === 'live' && <span className="size-1.5 animate-pulse rounded-full bg-rose-600" />}
      {status === 'live' ? 'Live' : status === 'ended' ? 'Ended' : 'Not started'}
    </span>
  )
}

function Cues({ slides, index, mode }: { slides: UseSessionSocket['slides']; index: number; mode: AutoAdvance }) {
  const next = slides[index + 1]
  return (
    <div className="grid gap-3 text-sm sm:grid-cols-2">
      <div className="rounded-xl bg-white p-3 ring-1 ring-line">
        <p className="text-xs font-semibold tracking-wide text-muted uppercase">Now</p>
        <p className="mt-1 font-medium text-ink">{slides[index]?.title || `Slide ${index + 1}`}</p>
      </div>
      <div className="rounded-xl bg-white p-3 ring-1 ring-line">
        <p className="text-xs font-semibold tracking-wide text-muted uppercase">Next {mode === 'keywords' || mode === 'script' ? '· say' : ''}</p>
        {next ? (
          <>
            <p className="mt-1 font-medium text-ink">{next.title || `Slide ${index + 2}`}</p>
            {(mode === 'keywords' || mode === 'script') && next.keywords.length > 0 && (
              <div className="mt-1.5 flex flex-wrap gap-1">
                {next.keywords.map((k) => (
                  <span key={k} className="rounded bg-accent-soft px-1.5 py-0.5 text-xs text-accent">
                    {k}
                  </span>
                ))}
              </div>
            )}
          </>
        ) : (
          <p className="mt-1 text-muted">End of deck</p>
        )}
      </div>
    </div>
  )
}

function JoinCard({ code, listeners, audioListeners }: { code: string; listeners: number; audioListeners: number | null }) {
  const link = joinLink(code)
  const [copied, setCopied] = useState(false)
  const local = ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname)
  return (
    <section className="rounded-xl bg-white p-4 ring-1 ring-line">
      <div className="flex gap-4">
        <QrCode value={link} className="size-28 shrink-0 rounded-md ring-1 ring-line" />
        <div className="min-w-0 flex-1">
          <p className="text-xs font-semibold tracking-wide text-muted uppercase">Join code</p>
          <p className="mt-0.5 font-mono text-2xl font-bold tracking-widest text-ink">{code}</p>
          <p className="mt-1 truncate text-xs text-muted" title={link}>
            {link.replace(/^https?:\/\//, '')}
          </p>
          <div className="mt-2 flex gap-1.5">
            <button
              onClick={() =>
                void navigator.clipboard?.writeText(link).then(() => {
                  setCopied(true)
                  setTimeout(() => setCopied(false), 1500)
                })
              }
              className="flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-ink ring-1 ring-line hover:bg-gray-50"
            >
              <Copy size={12} /> {copied ? 'Copied' : 'Copy link'}
            </button>
            <a href={link} target="_blank" rel="noreferrer" className="flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-ink ring-1 ring-line hover:bg-gray-50">
              <ExternalLink size={12} /> Open
            </a>
          </div>
        </div>
      </div>
      <p className="mt-3 flex items-center gap-1.5 text-sm text-ink">
        <Users size={15} className="text-muted" />
        <span className="font-semibold">{listeners}</span> in the audience
        {audioListeners !== null && <span className="text-muted">· {audioListeners} listening</span>}
      </p>
      {local && <p className="mt-2 text-xs text-amber-700">Phones can’t open “{location.hostname}”: open this page via your computer’s network address so the QR code works.</p>}
    </section>
  )
}

function MicCard({
  sessionLive,
  speaker,
  settings,
  useBrowser,
  detectedBrowser,
  onBrowserChange,
  browserStt,
  serverStt,
}: {
  sessionLive: boolean
  speaker: ReturnType<typeof useAudioSpeaker>
  settings: LiveSettings
  useBrowser: boolean
  detectedBrowser: boolean
  onBrowserChange: (v: boolean) => void
  browserStt: ReturnType<typeof useBrowserStt>
  serverStt: UseSessionSocket['stt']
}) {
  const on = speaker.status !== 'off' && speaker.status !== 'error'
  const tone = speaker.status === 'live' ? 'ok' : speaker.status === 'error' ? 'warn' : on ? 'busy' : 'off'
  let sttLine: { text: string; error?: boolean } | null = null
  if (useBrowser) {
    if (browserStt.error) sttLine = { text: browserStt.error, error: browserStt.status === 'error' }
    else if (on) sttLine = { text: browserStt.status === 'listening' ? 'Browser speech recognition: listening' : 'Browser speech recognition: starting…' }
  } else if (serverStt) {
    const label = serverStt.status === 'active' ? 'active' : serverStt.status
    sttLine = serverStt.status === 'error' ? { text: `Transcription (${serverStt.provider}) failed: ${serverStt.error ?? 'unknown error'}`, error: true } : { text: `Transcription (${serverStt.provider}): ${label}` }
  }
  return (
    <section className="rounded-xl bg-white p-4 ring-1 ring-line">
      <div className="flex items-center gap-3">
        <button
          onClick={() => (on ? speaker.stop() : void speaker.start())}
          disabled={!sessionLive && !on}
          className={`grid size-11 shrink-0 place-items-center rounded-full text-white disabled:opacity-40 ${on ? 'bg-rose-600 hover:bg-rose-700' : 'bg-accent hover:bg-indigo-700'}`}
          aria-label={on ? 'Turn microphone off' : 'Turn microphone on'}
        >
          {on ? <MicOff size={19} /> : <Mic size={19} />}
        </button>
        <div className="min-w-0 flex-1">
          <p className="flex items-center gap-1.5 text-sm font-medium text-ink">
            <Dot tone={tone} /> {SPEAKER_LABEL[speaker.status]}
          </p>
          <div className="mt-1.5">
            <LevelMeter level={speaker.level} active={on} />
          </div>
        </div>
      </div>
      {!sessionLive && !on && <p className="mt-2 text-xs text-muted">Start the session to use the microphone.</p>}
      {speaker.error && <p className="mt-2 text-xs text-warn">{speaker.error}</p>}
      {/* `ready.broadcast` is only sent at handshake; the setting tracks mid-stream toggles. */}
      {speaker.status === 'live' && !settings.audio_broadcast && (
        <p className="mt-2 text-xs text-amber-700">Audio broadcast is off: listeners don’t hear you (audio still feeds transcription).</p>
      )}
      {speaker.status === 'live' && speaker.framesDropped > 50 && <p className="mt-1 text-xs text-muted">{speaker.framesDropped} audio frames dropped (slow connection)</p>}
      {sttLine && <p className={`mt-2 text-xs ${sttLine.error ? 'text-warn' : 'text-muted'}`}>{sttLine.text}</p>}
      <label className="mt-3 flex items-center gap-2 text-xs text-ink">
        <input
          type="checkbox"
          checked={useBrowser}
          disabled={!browserSttSupported}
          onChange={(e) => onBrowserChange(e.target.checked)}
          className="accent-accent"
        />
        Transcribe in this browser (Web Speech API)
        {!browserSttSupported ? <span className="text-muted">· not supported here</span> : detectedBrowser ? <span className="text-muted">· session uses browser mode</span> : null}
      </label>
      <p className="mt-1 text-xs text-muted">
        Speech engine: <span className="font-mono">{settings.stt_provider}</span> · language <span className="font-mono">{settings.language}</span>
      </p>
    </section>
  )
}

function SettingsButton({
  sessionId,
  settings: stored,
  onSaved,
}: {
  sessionId: string
  settings: LiveSettings
  onSaved: (session: LiveSessionOut) => void
}) {
  // Optimistic overlay: a toggle flips at once, the PATCH response (or the session.updated echo) confirms it.
  const [optimistic, setOptimistic] = useState<Partial<LiveSettings>>({})
  const settings = { ...stored, ...optimistic }
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: PointerEvent) => !ref.current?.contains(e.target as Node) && setOpen(false)
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('pointerdown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const save = async (patch: Partial<LiveSettings>) => {
    setSaving(true)
    setError(null)
    setOptimistic((o) => ({ ...o, ...patch }))
    try {
      onSaved(await patchSettings(sessionId, patch))
    } catch (e) {
      setError(isApiError(e, 401) || isApiError(e, 404) ? 'Only the event owner can change settings.' : e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
      setOptimistic((o) => {
        const rest = { ...o }
        for (const key of Object.keys(patch) as (keyof LiveSettings)[]) if (rest[key] === patch[key]) delete rest[key]
        return rest
      })
    }
  }

  const modes = useMemo(
    () =>
      [
        ['off', 'Off', 'Manual only'],
        ['commands', 'Commands', '“next slide”, “go to slide 5”'],
        ['keywords', 'Keywords', 'Commands + next slide’s keywords'],
        ['script', 'Script', 'Keywords + follow the speaker notes'],
      ] as const,
    [],
  )

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium text-ink ring-1 ring-line hover:bg-gray-50"
        aria-expanded={open}
      >
        <Settings2 size={15} /> Settings
      </button>
      {open && (
        <div className="absolute right-0 z-20 mt-2 w-80 rounded-xl bg-white p-4 text-sm shadow-lg ring-1 ring-gray-900/10">
          <p className="text-xs font-semibold tracking-wide text-muted uppercase">Auto-advance</p>
          <div className="mt-2 grid gap-1">
            {modes.map(([value, label, hint]) => (
              <label key={value} className={`flex cursor-pointer items-start gap-2 rounded-lg p-2 ${settings.auto_advance === value ? 'bg-accent-soft' : 'hover:bg-gray-50'}`}>
                <input
                  type="radio"
                  name="auto_advance"
                  checked={settings.auto_advance === value}
                  onChange={() => void save({ auto_advance: value })}
                  className="mt-0.5 accent-accent"
                />
                <span>
                  <span className="font-medium text-ink">{label}</span>
                  <span className="block text-xs text-muted">{hint}</span>
                </span>
              </label>
            ))}
          </div>
          <div className="mt-3 grid gap-2 border-t border-line pt-3">
            <label className="flex items-center justify-between gap-2">
              <span className="text-ink">Broadcast audio to listeners</span>
              <input type="checkbox" checked={settings.audio_broadcast} onChange={(e) => void save({ audio_broadcast: e.target.checked })} className="accent-accent" />
            </label>
            <label className="flex items-center justify-between gap-2">
              <span className="text-ink">Show transcript to listeners</span>
              <input
                type="checkbox"
                checked={settings.show_transcript_to_listeners}
                onChange={(e) => void save({ show_transcript_to_listeners: e.target.checked })}
                className="accent-accent"
              />
            </label>
            <label className="flex items-center justify-between gap-2">
              <span className="text-ink">Speech engine</span>
              <select value={settings.stt_provider} onChange={(e) => void save({ stt_provider: e.target.value })} className="rounded-md px-2 py-1 text-sm ring-1 ring-line">
                {STT_PROVIDERS.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </label>
            <p className="text-xs text-muted">A new speech engine applies the next time the microphone starts.</p>
          </div>
          {(saving || error) && <p className={`mt-2 text-xs ${error ? 'text-warn' : 'text-muted'}`}>{error ?? 'Saving…'}</p>}
        </div>
      )}
    </div>
  )
}
