// Expanded venue option: research status, the four checked sections, pricing, a re-prompt box and manual edits.
import { AlertTriangle, Car, Check, CloudSun, ExternalLink, Loader2, Lock, LockOpen, Pencil, Send, Sparkles, TrafficCone, Users, X } from 'lucide-react'
import { useState, type FormEvent, type ReactNode } from 'react'
import type { RunnerContext } from '../../lib/runners'
import type { TaskResult } from '../../types'
import { errorMessage, patchCard, promptCard } from './cardApi'
import { isVenueBusy, venueExtra, type SectionKey, type VenueCardData, type VenueExtra } from './model'

const pct = (p: number) => `${Math.round(Math.max(0, Math.min(1, p)) * 100)}%`
const busy = isVenueBusy

export function VenueStatusChip({ ex }: { ex: VenueExtra }) {
  const [label, cls] = busy(ex)
    ? ['researching…', 'bg-indigo-50 text-accent ring-accent/20']
    : ex.status === 'error'
      ? ['failed', 'bg-rose-50 text-rose-700 ring-rose-300']
      : ['ready', 'bg-emerald-50 text-emerald-700 ring-emerald-300']
  return (
    <span className={`flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold ring-1 ${cls}`}>
      {busy(ex) && <Loader2 size={9} className="animate-spin" />}
      {label}
    </span>
  )
}

/** Thin progress line for the collapsed option while an agent works on the card. */
export function VenueProgressLine({ result }: { result: TaskResult }) {
  const ex = venueExtra(result)
  if (!ex) return null
  if (ex.status === 'error')
    return (
      <div className="flex items-center gap-1 truncate text-[11px] text-rose-600">
        <AlertTriangle size={10} /> Research failed
      </div>
    )
  if (!busy(ex)) return null
  return (
    <div className="flex items-center gap-1.5 truncate text-[11px] text-accent">
      <Loader2 size={10} className="shrink-0 animate-spin" />
      <span className="truncate">{ex.run?.currentStep || 'Researching…'}</span>
      {ex.run && <span className="ml-auto shrink-0 font-mono text-[10px]">{pct(ex.run.progress)}</span>}
    </div>
  )
}

export function VenueProgressBar({ result }: { result: TaskResult }) {
  const ex = venueExtra(result)
  if (!ex || !busy(ex)) return null
  return (
    <div className="absolute inset-x-0 bottom-0 h-1 bg-accent/10">
      <div className="h-full bg-accent transition-[width] duration-500" style={{ width: pct(ex.run?.progress ?? 0.05) }} />
    </div>
  )
}

const SECTION_META: Record<SectionKey, { label: string; Icon: typeof Users }> = {
  capacity: { label: 'Capacity', Icon: Users },
  parking: { label: 'Parking', Icon: Car },
  weather: { label: 'Weather', Icon: CloudSun },
  traffic: { label: 'Rush hour', Icon: TrafficCone },
}

function sectionLines(key: SectionKey, d: VenueCardData): { head?: string; body?: string | null } {
  switch (key) {
    case 'capacity': {
      const c = d.capacity
      if (!c) return {}
      const fit = { yes: 'fits', tight: 'tight fit', no: 'too small', unknown: '' }[c.fits_headcount ?? 'unknown']
      return { head: [c.estimated_capacity != null ? `~${c.estimated_capacity} people` : null, fit].filter(Boolean).join(' · '), body: c.notes }
    }
    case 'parking': {
      const p = d.parking
      if (!p) return {}
      const head = p.on_site ? 'On-site parking' : p.nearby?.length ? `${p.nearby.length} lot${p.nearby.length > 1 ? 's' : ''} nearby` : p.on_site === false ? 'No on-site parking' : undefined
      return { head, body: p.summary }
    }
    case 'weather': {
      const w = d.weather
      if (!w) return {}
      const temps = w.temp_high_c != null ? `${Math.round(w.temp_high_c)}°${w.temp_low_c != null ? `/${Math.round(w.temp_low_c)}°` : ''}C` : null
      const rain = w.precipitation_probability != null ? `${Math.round(w.precipitation_probability * 100)}% rain` : null
      const risk = w.risk && w.risk !== 'unknown' ? `${w.risk} risk` : null
      return { head: [temps, rain, risk].filter(Boolean).join(' · ') || undefined, body: [w.summary, w.basis === 'climatology' ? '(typical for the date)' : null].filter(Boolean).join(' ') }
    }
    case 'traffic': {
      const t = d.traffic
      if (!t) return {}
      const mins =
        t.event_time_minutes != null
          ? `${Math.round(t.event_time_minutes)} min${t.typical_minutes != null ? ` (vs ${Math.round(t.typical_minutes)})` : ''}`
          : t.typical_minutes != null
            ? `${Math.round(t.typical_minutes)} min`
            : null
      return { head: [mins, t.rush_hour_overlap ? 'rush hour' : t.rush_hour_overlap === false ? 'off-peak' : null].filter(Boolean).join(' · ') || undefined, body: t.summary }
    }
  }
}

const scoreTone = (s: number) => (s >= 75 ? 'bg-emerald-100 text-emerald-700' : s >= 50 ? 'bg-amber-100 text-amber-700' : 'bg-rose-100 text-rose-700')

function ScoreBadge({ score }: { score: number | null | undefined }) {
  if (score == null) return null
  return <span className={`rounded-md px-1.5 py-px font-mono text-[10px] font-bold ${scoreTone(score)}`}>{score}</span>
}

export default function VenueDetails({ result, ctx }: { result: TaskResult; ctx: RunnerContext | null }) {
  const ex = venueExtra(result)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<string | null>(null)
  if (!ex || !result.cardId) return null
  const cardId = result.cardId
  const d = ex.data
  const locked = new Set(ex.lockedFields)

  const act = async (what: string, fn: (c: RunnerContext) => Promise<unknown>) => {
    if (!ctx) return
    setError(null)
    setPending(what)
    try {
      await fn(ctx)
    } catch (e) {
      setError(errorMessage(e, 'That did not work.'))
    } finally {
      setPending(null)
    }
  }

  const toggleLock = (key: SectionKey | 'pricing') =>
    act(`lock:${key}`, (c) =>
      locked.has(key) ? patchCard(c, cardId, { unlock_fields: [key] }) : patchCard(c, cardId, { data: { [key]: d[key] } }),
    )

  const refineNote = ex.run?.kind === 'card_refine' && ex.run.status === 'succeeded' ? ex.run.note : undefined
  const runFailed = ex.run && (ex.run.status === 'failed' || ex.run.status === 'cancelled') ? ex.run.error : null

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <VenueStatusChip ex={ex} />
        {ex.sample && (
          <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-700 ring-1 ring-amber-300" title="No Google Maps key on the server: these venues are generated sample data.">
            sample venues
          </span>
        )}
        {d.rank != null && <span className="rounded-full bg-accent-soft px-2 py-0.5 text-[10px] font-semibold text-accent">#{d.rank} of shortlist</span>}
        {d.score && (
          <span className="ml-auto flex items-center gap-1 text-[10px] font-semibold text-muted uppercase">
            Overall <ScoreBadge score={d.score.overall} />
          </span>
        )}
      </div>

      {busy(ex) && ex.run && (
        <div>
          <div className="mb-1 flex items-center gap-1.5 text-[11px] text-accent">
            <Loader2 size={11} className="animate-spin" />
            <span className="truncate">{ex.run.currentStep || 'Researching…'}</span>
            <span className="ml-auto font-mono text-[10px]">{pct(ex.run.progress)}</span>
          </div>
          <div className="h-1 overflow-hidden rounded-full bg-accent/10">
            <div className="h-full bg-accent transition-[width] duration-500" style={{ width: pct(ex.run.progress) }} />
          </div>
        </div>
      )}

      {ex.status === 'error' && (
        <p className="rounded-xl bg-rose-50 p-2.5 text-xs text-rose-700 ring-1 ring-rose-200">
          {d.error?.message ?? 'Research on this venue failed.'} Ask the agent to try again below.
        </p>
      )}
      {runFailed && ex.status !== 'error' && <p className="rounded-xl bg-rose-50 p-2.5 text-xs text-rose-700 ring-1 ring-rose-200">Last request failed: {runFailed}</p>}
      {refineNote && (
        <p className="flex gap-1.5 rounded-xl bg-indigo-50 p-2.5 text-xs text-indigo-900 ring-1 ring-indigo-200">
          <Sparkles size={12} className="mt-0.5 shrink-0 text-accent" /> {refineNote}
        </p>
      )}
      {ex.shortlist && (
        <p className="rounded-xl bg-white/80 p-2.5 text-[11px] leading-relaxed text-gray-600 ring-1 ring-gray-900/5">
          <span className="font-semibold text-ink">Shortlist: </span>
          {ex.shortlist}
        </p>
      )}

      <div className="grid grid-cols-2 gap-2">
        {(['capacity', 'parking', 'weather', 'traffic'] as const).map((key) => {
          const { label, Icon } = SECTION_META[key]
          const { head, body } = sectionLines(key, d)
          const has = d[key] != null
          const score = d.score?.[key] ?? (key === 'parking' ? d.parking?.score : key === 'traffic' ? d.traffic?.score : null)
          return (
            <div key={key} className="flex min-h-[86px] flex-col rounded-xl bg-white/80 p-2.5 ring-1 ring-gray-900/5">
              <div className="flex items-center gap-1.5 text-[10px] font-semibold tracking-wide text-muted uppercase">
                <Icon size={11} className="text-accent" />
                {label}
                <span className="ml-auto flex items-center gap-1">
                  <ScoreBadge score={score} />
                  <LockButton locked={locked.has(key)} disabled={!has || !ctx || pending !== null} onClick={() => toggleLock(key)} />
                </span>
              </div>
              {has ? (
                <>
                  {head && <div className="mt-1 text-[12px] font-semibold text-ink">{head}</div>}
                  {body && <div className="mt-0.5 line-clamp-3 text-[11px] leading-snug text-gray-600">{body}</div>}
                </>
              ) : (
                <div className="mt-1.5 flex items-center gap-1 text-[11px] text-muted">
                  {busy(ex) ? (
                    <>
                      <Loader2 size={10} className="animate-spin" /> checking…
                    </>
                  ) : (
                    'Not researched'
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>

      <PriceAndName result={result} ex={ex} ctx={ctx} locked={locked.has('pricing')} onLock={() => toggleLock('pricing')} act={act} pending={pending} />

      {(d.pros?.length || d.cons?.length) ? (
        <div className="grid grid-cols-2 gap-2 text-[11px] leading-snug">
          <ul className="space-y-0.5 text-emerald-800">
            {d.pros?.slice(0, 4).map((p) => (
              <li key={p}>+ {p}</li>
            ))}
          </ul>
          <ul className="space-y-0.5 text-rose-800">
            {d.cons?.slice(0, 4).map((c) => (
              <li key={c}>− {c}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {!!d.sources?.length && (
        <div className="flex flex-wrap gap-x-2 gap-y-0.5 text-[10px] text-muted">
          Sources:
          {d.sources.slice(0, 5).map((s) =>
            s.url ? (
              <a key={s.url} href={s.url} target="_blank" rel="noreferrer" className="flex items-center gap-0.5 underline-offset-2 hover:text-accent hover:underline">
                {s.title} <ExternalLink size={8} />
              </a>
            ) : (
              <span key={s.title}>{s.title}</span>
            ),
          )}
        </div>
      )}

      {ctx && <RePrompt cardId={cardId} ctx={ctx} disabled={busy(ex)} onError={setError} />}
      {error && <p className="text-[11px] text-rose-600">{error}</p>}
    </div>
  )
}

function LockButton({ locked, disabled, onClick }: { locked: boolean; disabled: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={locked ? 'Locked: agents won’t change this. Click to unlock.' : 'Lock so agents won’t change this'}
      className={`rounded-md p-0.5 transition disabled:opacity-30 ${locked ? 'text-amber-600 hover:bg-amber-50' : 'text-gray-400 hover:bg-gray-100 hover:text-ink'}`}
    >
      {locked ? <Lock size={11} /> : <LockOpen size={11} />}
    </button>
  )
}

function PriceAndName({
  result,
  ex,
  ctx,
  locked,
  onLock,
  act,
  pending,
}: {
  result: TaskResult
  ex: VenueExtra
  ctx: RunnerContext | null
  locked: boolean
  onLock: () => void
  act: (what: string, fn: (c: RunnerContext) => Promise<unknown>) => Promise<void>
  pending: string | null
}) {
  const [editing, setEditing] = useState(false)
  const [title, setTitle] = useState(result.title)
  const [price, setPrice] = useState(ex.data.pricing?.estimate ?? '')
  const pricing = ex.data.pricing

  const save = (e: FormEvent) => {
    e.preventDefault()
    const nextTitle = title.trim()
    const nextPrice = price.trim()
    const patch: { title?: string; data?: Record<string, unknown> } = {}
    if (nextTitle && nextTitle !== result.title) patch.title = nextTitle
    if (nextPrice !== (pricing?.estimate ?? '')) patch.data = { pricing: { ...pricing, estimate: nextPrice || null } }
    if (!patch.title && !patch.data) return setEditing(false)
    void act('edit', (c) => patchCard(c, result.cardId!, patch)).then(() => setEditing(false))
  }

  if (editing)
    return (
      <form onSubmit={save} className="flex flex-col gap-2 rounded-xl bg-white/80 p-2.5 ring-1 ring-accent/30">
        <label className="text-[10px] font-semibold tracking-wide text-muted uppercase">
          Name
          <input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={300} className="mt-0.5 w-full rounded-lg bg-white px-2 py-1 text-[12px] font-normal tracking-normal text-ink normal-case ring-1 ring-gray-900/10 outline-none focus:ring-2 focus:ring-accent" />
        </label>
        <label className="text-[10px] font-semibold tracking-wide text-muted uppercase">
          Price estimate
          <input value={price} onChange={(e) => setPrice(e.target.value)} placeholder="e.g. $4,500 per day" className="mt-0.5 w-full rounded-lg bg-white px-2 py-1 text-[12px] font-normal tracking-normal text-ink normal-case ring-1 ring-gray-900/10 outline-none focus:ring-2 focus:ring-accent" />
        </label>
        <p className="text-[10px] text-muted">Saving locks the price so agents keep your value.</p>
        <div className="flex gap-2">
          <button type="submit" disabled={pending !== null} className="flex items-center gap-1 rounded-full bg-accent px-3 py-1 text-[11px] font-semibold text-white disabled:opacity-50">
            {pending === 'edit' ? <Loader2 size={10} className="animate-spin" /> : <Check size={10} strokeWidth={3} />} Save
          </button>
          <button type="button" onClick={() => setEditing(false)} className="flex items-center gap-1 rounded-full px-3 py-1 text-[11px] font-semibold text-muted hover:bg-gray-100">
            <X size={10} /> Cancel
          </button>
        </div>
      </form>
    )

  return (
    <Row
      label="Price"
      action={
        <>
          <LockButton locked={locked} disabled={!pricing || !ctx || pending !== null} onClick={onLock} />
          {ctx && (
            <button
              type="button"
              onClick={() => {
                setTitle(result.title)
                setPrice(pricing?.estimate ?? '')
                setEditing(true)
              }}
              title="Edit name and price"
              className="rounded-md p-0.5 text-gray-400 hover:bg-gray-100 hover:text-ink"
            >
              <Pencil size={11} />
            </button>
          )}
        </>
      }
    >
      <span className="font-semibold text-ink">{pricing?.estimate ?? result.price ?? 'Unknown'}</span>
      {pricing?.within_budget && pricing.within_budget !== 'unknown' && (
        <span className="text-muted"> · {{ yes: 'within budget', tight: 'near budget', no: 'over budget' }[pricing.within_budget]}</span>
      )}
      {pricing?.summary && <div className="mt-0.5 line-clamp-2 text-[11px] text-gray-600">{pricing.summary}</div>}
    </Row>
  )
}

function Row({ label, action, children }: { label: string; action?: ReactNode; children: ReactNode }) {
  return (
    <div className="rounded-xl bg-white/80 p-2.5 text-[12px] ring-1 ring-gray-900/5">
      <div className="mb-0.5 flex items-center text-[10px] font-semibold tracking-wide text-muted uppercase">
        {label}
        <span className="ml-auto flex items-center gap-1">{action}</span>
      </div>
      {children}
    </div>
  )
}

function RePrompt({ cardId, ctx, disabled, onError }: { cardId: string; ctx: RunnerContext; disabled: boolean; onError: (e: string | null) => void }) {
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    const instruction = text.trim()
    if (!instruction || disabled || sending) return
    setSending(true)
    onError(null)
    try {
      await promptCard(ctx, cardId, instruction)
      setText('')
    } catch (err) {
      onError(errorMessage(err, 'Could not reach the agent.'))
    } finally {
      setSending(false)
    }
  }
  return (
    <form onSubmit={submit} className="flex items-center gap-1.5 rounded-full bg-white py-1 pr-1 pl-3 ring-1 ring-gray-900/10 focus-within:ring-2 focus-within:ring-accent">
      <input
        value={text}
        onChange={(e) => setText(e.target.value)}
        maxLength={4000}
        placeholder={disabled ? 'Agent is working on this venue…' : 'e.g. check if they allow outside catering'}
        disabled={disabled}
        className="min-w-0 flex-1 bg-transparent text-[12px] outline-none placeholder:text-gray-400 disabled:opacity-60"
      />
      <button type="submit" disabled={disabled || sending || !text.trim()} aria-label="Ask the agent" className="grid size-7 shrink-0 place-items-center rounded-full bg-accent text-white disabled:opacity-40">
        {sending ? <Loader2 size={12} className="animate-spin" /> : <Send size={12} />}
      </button>
    </form>
  )
}
