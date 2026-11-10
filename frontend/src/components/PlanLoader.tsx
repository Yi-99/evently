import { Check, Loader2, Sparkles } from 'lucide-react'
import { useEffect, useState } from 'react'
import { EVENT_TYPES } from '../data/workflows'
import type { EventDetails, Task } from '../types'

interface Props {
  event: EventDetails
  tasks: Task[]
  onDone: () => void
}

const DURATION = 3600

// Staged "building your plan" screen shown between picking an event and the editor.
// TODO: drive progress from the planner backend once plan generation is real.
export default function PlanLoader({ event, tasks, onDone }: Props) {
  const [progress, setProgress] = useState(0)
  const meta = EVENT_TYPES.find((e) => e.id === event.type)!

  useEffect(() => {
    const start = performance.now()
    let raf = 0
    let timer = 0
    const tick = (now: number) => {
      // Ease-out so it starts fast and settles into 100%.
      const t = Math.min((now - start) / DURATION, 1)
      setProgress(Math.round((1 - (1 - t) ** 2) * 100))
      if (t < 1) raf = requestAnimationFrame(tick)
      else timer = window.setTimeout(onDone, 450)
    }
    raf = requestAnimationFrame(tick)
    return () => {
      cancelAnimationFrame(raf)
      clearTimeout(timer)
    }
  }, [onDone])

  const doneCount = Math.floor((progress / 100) * tasks.length)
  const status =
    progress < 15 ? `Understanding your ${meta.label.toLowerCase()}` : progress < 100 ? 'Drafting logistics' : 'Plan ready'

  return (
    <div className="glow flex h-full items-center justify-center px-4">
      <div className="w-full max-w-lg animate-rise rounded-[32px] bg-white/90 p-8 shadow-xl ring-1 shadow-indigo-500/5 ring-gray-900/5 backdrop-blur">
        <div className="flex items-center gap-3">
          <span className="grid size-11 place-items-center rounded-full bg-accent-soft text-2xl">{meta.emoji}</span>
          <div>
            <div className="text-lg font-bold tracking-tight">Building your {meta.label.toLowerCase()} plan</div>
            <div className="text-sm text-muted">
              {event.headcount} people · {event.city}
            </div>
          </div>
        </div>

        <div className="mt-7 flex items-center justify-between text-sm">
          <span className="flex items-center gap-2 font-medium">
            <Sparkles size={14} className="text-accent" />
            {status}
          </span>
          <span className="font-mono text-xs text-muted tabular-nums">{progress}%</span>
        </div>
        <div className="mt-2 h-2.5 overflow-hidden rounded-full bg-accent-soft">
          <div
            className="h-full rounded-full bg-gradient-to-r from-sky-400 via-accent to-fuchsia-500"
            style={{ width: `${progress}%` }}
          />
        </div>

        <ul className="mt-7 space-y-2.5">
          {tasks.map((t, i) => {
            const state = i < doneCount ? 'done' : i === doneCount && progress < 100 ? 'active' : 'pending'
            return (
              <li key={t.id} className={`flex items-center gap-3 text-sm transition ${state === 'pending' ? 'opacity-35' : ''}`}>
                {state === 'done' ? (
                  <span className="grid size-5 place-items-center rounded-full bg-accent text-white">
                    <Check size={11} strokeWidth={3} />
                  </span>
                ) : state === 'active' ? (
                  <Loader2 size={20} className="animate-spin text-accent" />
                ) : (
                  <span className="size-5 rounded-full ring-1 ring-gray-300" />
                )}
                <span className={state === 'done' ? 'text-ink' : 'text-muted'}>
                  {state === 'done' ? 'Added' : state === 'active' ? 'Adding' : 'Queued'} <b className="font-semibold text-ink">{t.title}</b>
                </span>
              </li>
            )
          })}
        </ul>
      </div>
    </div>
  )
}
