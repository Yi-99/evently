// Small UI pieces shared by the presenter and audience pages.
import { useEffect, useMemo, useRef, useState } from 'react'
import { encodeQr, qrPath } from './qr'
import { deckUrl, type SessionState, type TranscriptSegmentOut } from './session'
import { describeChange, isAutoChange, type SlideChange } from './useSessionSocket'

// --- deck --------------------------------------------------------------------------------

interface DeckFrameProps {
  presentationId: string
  token: string
  index: number
  title?: string
  className?: string
}

/**
 * The rendered deck in a sandboxed iframe. The src is fixed at mount (starting slide), later slide
 * changes are sent as `evently:goto` so the iframe never reloads. Key it by presentation + token.
 */
export function DeckFrame({ presentationId, token, index, title, className = '' }: DeckFrameProps) {
  const frameRef = useRef<HTMLIFrameElement>(null)
  const [src] = useState(() => deckUrl(presentationId, token, index))
  const [ready, setReady] = useState(false)
  const [failed, setFailed] = useState(false)
  const indexRef = useRef(index)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)

  useEffect(() => {
    indexRef.current = index
    if (ready) frameRef.current?.contentWindow?.postMessage({ type: 'evently:goto', index }, '*')
  }, [index, ready])

  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      if (!frameRef.current || e.source !== frameRef.current.contentWindow) return
      const data = e.data as { type?: string } | null
      if (data?.type === 'evently:ready') {
        clearTimeout(timer.current)
        setReady(true)
        setFailed(false)
        frameRef.current.contentWindow?.postMessage({ type: 'evently:goto', index: indexRef.current }, '*')
      }
    }
    window.addEventListener('message', onMessage)
    return () => {
      window.removeEventListener('message', onMessage)
      clearTimeout(timer.current)
    }
  }, [])

  return (
    <div className={`relative aspect-video w-full overflow-hidden rounded-xl bg-black ${className}`}>
      <iframe
        ref={frameRef}
        src={src}
        title={title ?? 'Slides'}
        sandbox="allow-scripts"
        referrerPolicy="no-referrer"
        className="absolute inset-0 h-full w-full border-0"
        // An error response (401/404 JSON) never sends evently:ready.
        onLoad={() => {
          clearTimeout(timer.current)
          timer.current = setTimeout(() => setFailed(true), 3000)
        }}
      />
      {failed && !ready && (
        <div className="absolute inset-0 grid place-items-center bg-gray-900 p-6 text-center text-white">
          <div>
            <p className="text-xs tracking-wide text-white/50 uppercase">Slide {index + 1}</p>
            <p className="mt-2 text-xl font-semibold">{title || 'Slides unavailable'}</p>
          </div>
        </div>
      )}
    </div>
  )
}

/** Shown instead of the deck when the session has no presentation (or it cannot be rendered). */
export function SlidePlaceholder({ index, count, title, dark = false }: { index: number; count: number; title?: string; dark?: boolean }) {
  return (
    <div
      className={`grid aspect-video w-full place-items-center rounded-xl p-6 text-center ${dark ? 'bg-gray-900 text-white' : 'bg-gray-100 text-ink'}`}
    >
      <div>
        <p className={`text-xs tracking-wide uppercase ${dark ? 'text-white/50' : 'text-muted'}`}>
          {count ? `Slide ${index + 1} of ${count}` : 'No slides'}
        </p>
        <p className="mt-2 text-2xl font-semibold">{title || (count ? `Slide ${index + 1}` : 'Live session')}</p>
      </div>
    </div>
  )
}

// --- QR --------------------------------------------------------------------------------------

export function QrCode({ value, className = '' }: { value: string; className?: string }) {
  const { path, size } = useMemo(() => {
    const modules = encodeQr(value, 'M')
    return { path: qrPath(modules, 4), size: modules.length + 8 }
  }, [value])
  return (
    <svg viewBox={`0 0 ${size} ${size}`} className={className} role="img" aria-label={`QR code for ${value}`} shapeRendering="crispEdges">
      <rect width={size} height={size} fill="#fff" />
      <path d={path} fill="#000" />
    </svg>
  )
}

// --- transcript -------------------------------------------------------------------------------

const FLASH_CSS = `
@keyframes evently-flash { 0% { background-color: rgba(99,102,241,.45) } 100% { background-color: transparent } }
@keyframes evently-fade { 0%, 70% { opacity: 1 } 100% { opacity: 0; visibility: hidden } }
`
export const FlashStyles = () => <style>{FLASH_CSS}</style>


type FeedItem = { kind: 'segment'; t: number; segment: TranscriptSegmentOut } | { kind: 'slide'; t: number; change: SlideChange }

interface TranscriptFeedProps {
  segments: TranscriptSegmentOut[]
  partial: { text: string } | null
  slideChanges?: SlideChange[]
  slides?: SessionState['slides']
  dark?: boolean
  large?: boolean
  empty?: string
  className?: string
}

/** Final segments (with slide-change markers between them) plus the current partial in grey; sticks to the bottom. */
export function TranscriptFeed({ segments, partial, slideChanges = [], slides = [], dark = false, large = false, empty, className = '' }: TranscriptFeedProps) {
  const box = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const items = useMemo(() => {
    const list: FeedItem[] = [
      ...segments.map((segment) => ({ kind: 'segment' as const, t: Date.parse(segment.created_at), segment })),
      ...slideChanges.filter(isAutoChange).map((change) => ({ kind: 'slide' as const, t: Date.parse(change.ts), change })),
    ]
    return list.sort((a, b) => a.t - b.t)
  }, [segments, slideChanges])
  const newestChange = slideChanges.length ? slideChanges[slideChanges.length - 1] : null

  useEffect(() => {
    const el = box.current
    if (el && stick.current) el.scrollTop = el.scrollHeight
  }, [items, partial])

  const muted = dark ? 'text-white/45' : 'text-muted'
  return (
    <div
      ref={box}
      onScroll={(e) => {
        const el = e.currentTarget
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
      }}
      className={`overflow-y-auto ${large ? 'text-lg leading-relaxed' : 'text-sm leading-6'} ${className}`}
      aria-live="polite"
    >
      {!items.length && !partial && <p className={`${muted} italic`}>{empty ?? 'Nothing said yet.'}</p>}
      {items.map((item) =>
        item.kind === 'segment' ? (
          <p key={`s${item.segment.seq}`} className="mb-1.5">
            {item.segment.text}
          </p>
        ) : (
          <p
            key={`c${item.change.at}`}
            className={`my-2 rounded-md px-2 py-1 text-xs font-medium ${dark ? 'bg-indigo-500/15 text-indigo-200' : 'bg-accent-soft text-accent'}`}
            // The animation runs once when the newest marker mounts; keys are stable, so it never replays.
            style={item.change === newestChange ? { animation: 'evently-flash 2.5s ease-out' } : undefined}
          >
            → Slide {item.change.index + 1}
            {slides[item.change.index]?.title ? ` · ${slides[item.change.index].title}` : ''} ({describeChange(item.change)})
          </p>
        ),
      )}
      {partial && <p className={`${muted}`}>{partial.text}</p>}
    </div>
  )
}

// --- misc -----------------------------------------------------------------------------------

export function Dot({ tone }: { tone: 'ok' | 'warn' | 'off' | 'busy' }) {
  const color = { ok: 'bg-emerald-500', warn: 'bg-rose-500', off: 'bg-gray-400', busy: 'bg-amber-400 animate-pulse' }[tone]
  return <span className={`inline-block size-2 shrink-0 rounded-full ${color}`} aria-hidden />
}

/** Mic level bar, 0..1. */
export function LevelMeter({ level, active }: { level: number; active: boolean }) {
  const pct = Math.min(100, Math.round(Math.sqrt(level) * 100))
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-gray-200" role="meter" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label="Microphone level">
      <div
        className={`h-full rounded-full transition-[width] duration-75 ${active ? (pct > 85 ? 'bg-rose-500' : 'bg-emerald-500') : 'bg-gray-300'}`}
        style={{ width: `${active ? pct : 0}%` }}
      />
    </div>
  )
}
