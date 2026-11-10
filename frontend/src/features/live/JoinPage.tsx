// Audience page for a live session (/join/:code): phone-first, dark.
// Name -> POST /live-sessions/join -> participant token -> control socket (slides, transcript) and,
// after a tap, the audio socket as a listener.
import { Headphones, LoaderCircle, LogOut, Radio, VolumeX } from 'lucide-react'
import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { isApiError } from '../../api/client'
import { DeckFrame, Dot, FlashStyles, SlidePlaceholder, TranscriptFeed } from './components'
import { joinSession, readJoin, readName, readSettings, storeJoin, storeName, type Joined } from './session'
import { useAudioListener, type UseAudioListener } from './useAudioListener'
import { useSessionSocket } from './useSessionSocket'

export interface JoinPageProps {
  /** Join code from the URL (/join/:code). */
  code: string
}

export default function JoinPage({ code }: JoinPageProps) {
  const [joined, setJoined] = useState<Joined | null>(() => readJoin(code))
  if (!joined)
    return (
      <JoinForm
        code={code}
        onJoined={(j) => {
          storeJoin(code, j)
          setJoined(j)
        }}
      />
    )
  return (
    <Audience
      key={joined.token}
      joined={joined}
      onLeave={() => {
        storeJoin(code, null)
        setJoined(null)
      }}
    />
  )
}

// --- join form ---------------------------------------------------------------------------------

function JoinForm({ code, onJoined }: { code: string; onJoined: (j: Joined) => void }) {
  const [name, setName] = useState(readName)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    const displayName = name.trim()
    if (!displayName) return
    setBusy(true)
    setError(null)
    try {
      const joined = await joinSession(code, displayName)
      storeName(displayName)
      onJoined(joined)
    } catch (err) {
      setError(
        isApiError(err, 404)
          ? `No live session uses the code ${code.toUpperCase()}. Check the code and try again.`
          : isApiError(err, 410)
            ? 'This session has already ended.'
            : isApiError(err, 0)
              ? 'Could not reach Evently. Check your connection.'
              : err instanceof Error
                ? err.message
                : 'Could not join.',
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="flex min-h-full flex-col items-center justify-center bg-gray-950 px-5 py-10 text-white">
      <form onSubmit={submit} className="w-full max-w-sm">
        <Radio className="mx-auto text-indigo-400" size={34} />
        <h1 className="mt-4 text-center text-2xl font-semibold">Join the live presentation</h1>
        <p className="mt-2 text-center text-white/60">
          Code <span className="font-mono font-semibold tracking-widest text-white">{code.toUpperCase()}</span>
        </p>
        <label className="mt-8 block text-sm font-medium text-white/70" htmlFor="join-name">
          Your name
        </label>
        <input
          id="join-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={100}
          autoComplete="name"
          autoFocus
          placeholder="e.g. Alex"
          className="mt-2 w-full rounded-xl bg-white/10 px-4 py-3.5 text-lg text-white ring-1 ring-white/15 outline-none placeholder:text-white/30 focus:ring-2 focus:ring-indigo-400"
        />
        {error && <p className="mt-3 text-sm text-rose-300">{error}</p>}
        <button
          type="submit"
          disabled={busy || !name.trim()}
          className="mt-6 flex w-full items-center justify-center gap-2 rounded-xl bg-indigo-500 px-4 py-3.5 text-lg font-semibold text-white hover:bg-indigo-400 disabled:opacity-50"
        >
          {busy && <LoaderCircle className="animate-spin" size={20} />} Join
        </button>
      </form>
    </main>
  )
}

// --- audience view ------------------------------------------------------------------------------

function Audience({ joined, onLeave }: { joined: Joined; onLeave: () => void }) {
  const sessionId = joined.session.id
  const sock = useSessionSocket(sessionId, joined.token)
  const listener = useAudioListener(sessionId, joined.token)
  const session = sock.session ?? joined.session
  const settings = readSettings(session.settings)
  const status = session.status
  const index = session.current_slide_index
  const slide = sock.slides[index]

  // Reconnect the audio as soon as the session goes live; stop it when the session ends.
  const { retry, stop } = listener
  useEffect(() => {
    if (status === 'live') retry()
    else if (status === 'ended') stop()
  }, [status, retry, stop])

  if (sock.status === 'closed')
    return (
      <Shell>
        <div className="flex flex-1 flex-col items-center justify-center px-6 text-center">
          <p className="text-lg font-semibold">You’ve been disconnected</p>
          <p className="mt-2 text-white/60">{sock.error}</p>
          <button onClick={onLeave} className="mt-6 rounded-xl bg-indigo-500 px-5 py-3 font-semibold">
            Join again
          </button>
        </div>
      </Shell>
    )

  return (
    <Shell>
      <FlashStyles />
      <header className="flex items-center gap-3 px-4 pt-4 pb-3">
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-lg leading-tight font-semibold">{session.title}</h1>
          <p className="mt-0.5 flex items-center gap-1.5 text-sm text-white/55">
            <Dot tone={sock.status === 'open' ? 'ok' : 'busy'} />
            {sock.status === 'open' ? `Joined as ${sock.client?.display_name ?? joined.participant.display_name}` : 'Reconnecting…'}
          </p>
        </div>
        <span
          className={`rounded-full px-2.5 py-1 text-xs font-semibold ${status === 'live' ? 'bg-rose-500/20 text-rose-300' : status === 'ended' ? 'bg-white/10 text-white/60' : 'bg-amber-400/15 text-amber-200'}`}
        >
          {status === 'live' ? '● Live' : status === 'ended' ? 'Ended' : 'Starting soon'}
        </span>
        <button onClick={onLeave} className="rounded-lg p-2 text-white/50 hover:bg-white/10 hover:text-white" aria-label="Leave">
          <LogOut size={18} />
        </button>
      </header>

      {status === 'ended' ? (
        <div className="flex flex-1 flex-col items-center justify-center px-6 text-center">
          <p className="text-2xl font-semibold">Thanks for joining!</p>
          <p className="mt-2 text-white/60">This session has ended.</p>
        </div>
      ) : (
        <>
          <section className="px-4">
            {session.presentation_id ? (
              <DeckFrame key={session.presentation_id} presentationId={session.presentation_id} token={joined.token} index={index} title={slide?.title} />
            ) : (
              <SlidePlaceholder dark index={index} count={sock.slideCount} title={slide?.title} />
            )}
            {sock.slideCount > 0 && (
              <p className="mt-2 truncate text-sm text-white/55">
                Slide {index + 1} of {sock.slideCount}
                {slide?.title ? ` · ${slide.title}` : ''}
              </p>
            )}
          </section>

          <section className="px-4 pt-4">
            <ListenButton listener={listener} sessionStatus={status} broadcastOn={settings.audio_broadcast} />
          </section>

          {settings.show_transcript_to_listeners ? (
            <section className="flex min-h-40 flex-1 flex-col px-4 pt-4 pb-6">
              <h2 className="mb-2 text-xs font-semibold tracking-wide text-white/40 uppercase">Live transcript</h2>
              <TranscriptFeed
                dark
                large
                segments={sock.transcript}
                partial={sock.partial}
                slideChanges={sock.slideChanges}
                slides={sock.slides}
                className="min-h-0 flex-1"
                empty={status === 'live' ? 'Waiting for the speaker…' : 'The transcript starts when the presentation does.'}
              />
            </section>
          ) : (
            <div className="flex-1" />
          )}
        </>
      )}
    </Shell>
  )
}

function Shell({ children }: { children: ReactNode }) {
  return <main className="mx-auto flex h-full max-w-xl flex-col overflow-y-auto bg-gray-950 text-white [color-scheme:dark]">{children}</main>
}

function ListenButton({ listener, sessionStatus, broadcastOn }: { listener: UseAudioListener; sessionStatus: string; broadcastOn: boolean }) {
  const { status } = listener
  if (status === 'off')
    return (
      <button
        onClick={listener.start}
        className="flex w-full items-center justify-center gap-3 rounded-2xl bg-indigo-500 px-5 py-5 text-xl font-semibold text-white shadow-lg shadow-indigo-500/20 active:scale-[0.99]"
      >
        <Headphones size={26} /> Tap to listen
      </button>
    )

  let title: string
  let detail: string | null = null
  let tone: 'ok' | 'busy' | 'warn' | 'off' = 'busy'
  if (status === 'error') {
    title = 'Audio unavailable'
    detail = listener.reason
    tone = 'warn'
  } else if (sessionStatus !== 'live') {
    title = 'Waiting for the presentation to start'
  } else if (status === 'disabled' || !broadcastOn) {
    title = 'The speaker isn’t broadcasting audio'
    detail = 'Follow along with the slides and transcript.'
    tone = 'off'
  } else if (status === 'playing') {
    title = 'Listening live'
    detail = listener.dropped > 20 ? `Weak connection · ${listener.dropped} audio frames lost` : null
    tone = 'ok'
  } else if (status === 'waiting') {
    title = 'Speaker is muted'
    detail = 'You’ll hear them as soon as their microphone is on.'
    tone = 'off'
  } else {
    title = 'Connecting audio…'
  }

  return (
    <div className="flex items-center gap-3 rounded-2xl bg-white/5 px-4 py-3.5 ring-1 ring-white/10">
      {status === 'playing' ? <Bars /> : status === 'disabled' || status === 'error' ? <VolumeX size={22} className="text-white/50" /> : <Dot tone={tone} />}
      <div className="min-w-0 flex-1">
        <p className="font-semibold">{title}</p>
        {detail && <p className="text-sm text-white/55">{detail}</p>}
      </div>
      <button onClick={listener.stop} className="rounded-xl bg-white/10 px-4 py-2 text-sm font-semibold hover:bg-white/15">
        Stop
      </button>
    </div>
  )
}

/** Animated "now playing" bars. */
function Bars() {
  return (
    <span className="flex h-5 w-6 items-end gap-0.5" aria-hidden>
      {[0, 0.2, 0.4, 0.1].map((d, i) => (
        <span key={i} className="w-1 animate-pulse rounded-sm bg-emerald-400" style={{ height: `${40 + i * 15}%`, animationDelay: `${d}s` }} />
      ))}
    </span>
  )
}
