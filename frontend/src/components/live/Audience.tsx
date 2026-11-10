import { useEffect, useState } from 'react'
import type { Deck } from '../../data/demoDeck'
import { useDeck } from '../../lib/deckStore'
import { HEARTBEAT_MS, TIMEOUT_MS, joinLive } from '../../lib/live'
import Slide from './Slide'

// One id per tab, so remounts don't count as extra screens.
const ME = crypto.randomUUID()

// Phone-first view that follows the presenter. There are no controls: the presenter drives.
export default function Audience({ id }: { id: string }) {
  const deck = useDeck(id)
  if (!deck)
    return (
      <div className="grid h-full place-items-center bg-[#0f1033] p-6 text-center text-white">
        <div>
          <p className="text-lg font-semibold">{deck === undefined ? 'Loading slides…' : 'Waiting for slides'}</p>
          {deck === null && (
            <p className="mt-1 max-w-sm text-sm text-white/60">
              They appear here as soon as the presenter adds them. In demo mode, this only works in the browser the slides are uploaded from.
            </p>
          )}
        </div>
      </div>
    )
  return <AudienceView id={id} deck={deck} />
}

function AudienceView({ id, deck }: { id: string; deck: Deck }) {
  const { slides } = deck
  const [index, setIndex] = useState<number | null>(null)
  const [lastSeen, setLastSeen] = useState(0)
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const s = joinLive(id, {
      onState: ({ index }) => {
        setIndex(Math.max(0, Math.min(slides.length - 1, index)))
        setLastSeen(Date.now())
      },
    })
    s.hello(ME)
    const beat = setInterval(() => {
      s.here(ME)
      setNow(Date.now())
    }, HEARTBEAT_MS)
    return () => {
      clearInterval(beat)
      s.close()
    }
  }, [id, slides.length])

  const live = index !== null && now - lastSeen < TIMEOUT_MS

  return (
    <div className="flex h-full flex-col bg-[#0f1033] text-white">
      <header className="flex items-center gap-3 px-4 py-3 sm:px-6">
        <span
          className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold ${
            live ? 'bg-rose-500/15 text-rose-300' : 'bg-white/10 text-white/60'
          }`}
        >
          <span className={`size-1.5 rounded-full ${live ? 'animate-pulse bg-rose-400' : 'bg-white/40'}`} />
          {live ? 'Live' : 'Offline'}
        </span>
        <h1 className="truncate text-sm font-semibold">{deck.title}</h1>
      </header>

      <main className="grid flex-1 place-items-center px-4 pb-6 sm:px-6">
        {index === null ? (
          <div className="text-center">
            <p className="text-lg font-semibold">Waiting for the presenter</p>
            <p className="mt-1 text-sm text-white/60">Slides appear here as soon as the talk starts.</p>
          </div>
        ) : (
          <div className="w-full max-w-5xl">
            <Slide key={index} slide={slides[index]} className="shadow-2xl shadow-black/40 motion-safe:animate-rise" />
            <p className="mt-4 text-center text-sm text-white/60">
              {live ? `Slide ${index + 1} of ${slides.length}. Following the presenter.` : 'The presenter went offline. Showing the last slide.'}
            </p>
          </div>
        )}
      </main>
    </div>
  )
}
