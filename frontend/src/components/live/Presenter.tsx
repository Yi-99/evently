import { ChevronLeft, ChevronRight, ExternalLink, Upload, Users, X } from 'lucide-react'
import QRCode from 'qrcode'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { DEMO_ID, type Deck } from '../../data/demoDeck'
import { useDeck } from '../../lib/deckStore'
import { HEARTBEAT_MS, TIMEOUT_MS, audienceUrl, joinLive, savedState, type LiveSession } from '../../lib/live'
import Slide from './Slide'

export default function Presenter({ id }: { id: string }) {
  const deck = useDeck(id)
  if (deck === undefined) return <DeckMessage title="Loading slides…" />
  if (deck === null)
    return (
      <DeckMessage title="These slides aren’t on this device" body="Uploaded decks are saved in the browser they were uploaded from.">
        <a href="#/present/new" className="mt-6 inline-flex items-center gap-1.5 rounded-full bg-accent px-5 py-2.5 text-sm font-semibold text-white hover:bg-indigo-500">
          <Upload size={15} /> Upload slides
        </a>
      </DeckMessage>
    )
  return <PresenterView id={id} deck={deck} />
}

export function DeckMessage({ title, body, children }: { title: string; body?: string; children?: ReactNode }) {
  return (
    <div className="grid h-full place-items-center bg-gray-100 p-6 text-center text-gray-900">
      <div>
        <h1 className="text-xl font-semibold">{title}</h1>
        {body && <p className="mt-2 text-gray-500">{body}</p>}
        {children}
        <p className="mt-6">
          <a href="#" className="text-sm font-medium text-gray-500 hover:text-accent">
            Back to Evently
          </a>
        </p>
      </div>
    </div>
  )
}

function PresenterView({ id, deck }: { id: string; deck: Deck }) {
  const { slides } = deck
  const clamp = (i: number) => Math.max(0, Math.min(slides.length - 1, i))
  const [rawIndex, setIndex] = useState(() => savedState(id)?.index ?? 0)
  // Clamped on read: the deck can be replaced by a shorter one while this tab is open.
  const index = clamp(rawIndex)
  const [qr, setQr] = useState<string | null>(null)
  // Audience screen id -> last heartbeat time.
  const [seen, setSeen] = useState<Map<string, number>>(new Map())
  const [now, setNow] = useState(() => Date.now())
  const session = useRef<LiveSession | null>(null)
  const indexRef = useRef(index)
  const url = audienceUrl(id)

  useEffect(() => {
    const mark = (from: string) => setSeen((m) => new Map(m).set(from, Date.now()))
    const s = joinLive(id, {
      onHello: (from) => {
        mark(from)
        s.publish({ index: indexRef.current })
      },
      onHere: mark,
    })
    session.current = s
    // Re-announce the slide regularly so audience screens can tell the presenter is still live.
    const beat = setInterval(() => {
      s.publish({ index: indexRef.current })
      setNow(Date.now())
    }, HEARTBEAT_MS)
    return () => {
      clearInterval(beat)
      s.close()
    }
  }, [id])

  useEffect(() => {
    indexRef.current = index
    session.current?.publish({ index })
  }, [index])

  useEffect(() => {
    QRCode.toDataURL(url, { margin: 1, width: 320, color: { dark: '#1e1b4b', light: '#ffffff' } }).then(setQr, () => setQr(null))
  }, [url])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const last = slides.length - 1
      if (['ArrowRight', 'PageDown', ' '].includes(e.key)) setIndex((i) => Math.min(last, i + 1))
      else if (['ArrowLeft', 'PageUp'].includes(e.key)) setIndex((i) => Math.max(0, i - 1))
      else return
      e.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [slides.length])

  const watching = [...seen.values()].filter((t) => now - t < TIMEOUT_MS).length
  const next = slides[index + 1]

  return (
    <div className="flex h-full flex-col overflow-y-auto bg-gray-100 text-gray-900">
      <header className="flex items-center gap-3 px-6 py-4">
        <span className="inline-flex items-center gap-1.5 rounded-full bg-rose-50 px-2.5 py-1 text-xs font-semibold text-rose-600">
          <span className="size-1.5 animate-pulse rounded-full bg-rose-500" /> Live
        </span>
        <h1 className="truncate font-semibold">{deck.title}</h1>
        <span className="text-sm text-gray-500">Presenter view</span>
        <a
          href={id === DEMO_ID ? '#/present/new' : `#/upload/${id}`}
          className="ml-auto inline-flex shrink-0 items-center gap-1 rounded-full px-3 py-1.5 text-sm font-medium text-gray-600 hover:bg-white"
        >
          <Upload size={15} /> Use your own slides
        </a>
        <a href="#" className="inline-flex shrink-0 items-center gap-1 rounded-full px-3 py-1.5 text-sm font-medium text-gray-600 hover:bg-white">
          <X size={15} /> End
        </a>
      </header>

      <div className="mx-auto grid w-full max-w-7xl flex-1 gap-6 px-6 pb-6 lg:grid-cols-[1fr_300px]">
        <main className="min-w-0">
          <Slide key={index} slide={slides[index]} className="shadow-xl shadow-indigo-500/10 motion-safe:animate-rise" />

          <div className="mt-4 flex items-center justify-center gap-3">
            <button
              onClick={() => setIndex((i) => clamp(i - 1))}
              disabled={index === 0}
              aria-label="Previous slide"
              className="grid size-11 place-items-center rounded-full bg-white ring-1 ring-gray-900/10 hover:ring-accent disabled:opacity-40 disabled:hover:ring-gray-900/10"
            >
              <ChevronLeft size={18} />
            </button>
            <span className="w-16 text-center text-sm text-gray-600 tabular-nums">
              {index + 1} / {slides.length}
            </span>
            <button
              onClick={() => setIndex((i) => clamp(i + 1))}
              disabled={index === slides.length - 1}
              aria-label="Next slide"
              className="grid size-11 place-items-center rounded-full bg-accent text-white hover:bg-indigo-500 disabled:opacity-40 disabled:hover:bg-accent"
            >
              <ChevronRight size={18} />
            </button>
          </div>
          <p className="mt-2 text-center text-xs text-gray-500">Use the arrow keys or space to move between slides.</p>

          <ol className="mt-6 grid grid-cols-3 gap-3 sm:grid-cols-6">
            {slides.map((s, i) => (
              <li key={i}>
                <button
                  onClick={() => setIndex(i)}
                  aria-label={`Go to slide ${i + 1}`}
                  aria-current={i === index}
                  className={`block w-full rounded-md ring-2 transition ${i === index ? 'ring-accent' : 'ring-transparent hover:ring-gray-300'}`}
                >
                  <Slide slide={s} className="pointer-events-none" />
                </button>
              </li>
            ))}
          </ol>
        </main>

        <aside className="space-y-4">
          <section className="rounded-3xl bg-white p-5 ring-1 ring-gray-900/5">
            <h2 className="font-semibold">Audience</h2>
            <p className="mt-1 text-sm text-gray-500">Screens that open this link follow your slides.</p>
            {qr && <img src={qr} alt={`QR code for ${url}`} className="mx-auto mt-4 size-44" />}
            <a
              href={url}
              target="_blank"
              rel="noreferrer"
              className="mt-4 flex items-center justify-center gap-1.5 rounded-full bg-accent px-4 py-2.5 text-sm font-semibold text-white hover:bg-indigo-500"
            >
              Open audience view <ExternalLink size={14} />
            </a>
            <p className="mt-3 flex items-center justify-center gap-1.5 text-sm text-gray-600">
              <Users size={14} /> {watching} {watching === 1 ? 'screen' : 'screens'} watching
            </p>
            <p className="mt-3 text-xs text-gray-400">
              Demo mode: only tabs in this browser can follow along. Phones can scan once live sync has a server.
            </p>
          </section>

          <section className="rounded-3xl bg-white p-5 ring-1 ring-gray-900/5">
            <h2 className="text-sm font-semibold text-gray-500">Up next</h2>
            {next ? <Slide slide={next} className="mt-3" /> : <p className="mt-2 text-sm text-gray-500">This is the last slide.</p>}
          </section>
        </aside>
      </div>
    </div>
  )
}
