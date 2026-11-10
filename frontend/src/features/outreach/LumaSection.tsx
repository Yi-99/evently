import { CalendarPlus, ExternalLink, LoaderCircle, Save, Send, UserPlus } from 'lucide-react'
import { useState } from 'react'
import { isApiError, type Api } from '../../api/client'
import type { EventStore, EventStoreState } from '../../api/realtime'
import type { LumaEventDraft, LumaEventOut } from '../../api/types'
import { Field, btnGhost, btnPrimary, inputCls } from './Modal'
import StatusChip from './StatusChip'
import { errorText, isEmail, parseEmails } from './lib'

const NEEDS_KEY = 'Luma publishing needs LUMA_API_KEY (Luma Plus). Add the key to the backend environment and try again.'

function lumaError(e: unknown, publishing = false): string {
  if (isApiError(e, 503)) return publishing || /luma/i.test(e.message) ? `${NEEDS_KEY} (${e.message})` : e.message
  return errorText(e)
}

const str = (v: unknown): string => (typeof v === 'string' ? v : '')

/** ISO string → value for <input type="datetime-local"> in the browser's timezone. */
function toLocalInput(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`
}

interface Props {
  eventId: string
  store: EventStore
  api: Api
  state: EventStoreState
}

export default function LumaSection({ eventId, store, api, state }: Props) {
  const luma = state.luma
  const [venueId, setVenueId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const venues = Object.values(state.cards).filter((c) => c.kind === 'venue')

  const create = async () => {
    setBusy(true)
    setError(null)
    try {
      const out = await api.post<LumaEventOut>(`/events/${encodeURIComponent(eventId)}/luma/draft`, { venue_card_id: venueId || null })
      store.patch((s) => ({ ...s, luma: out }))
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  if (!luma)
    return (
      <div className="space-y-2.5">
        <p className="text-xs text-muted">
          Build a Luma event page from this event&apos;s date, headcount and venue. Nothing is published until you confirm.
        </p>
        {venues.length > 0 && (
          <select className={inputCls} value={venueId} onChange={(e) => setVenueId(e.target.value)} aria-label="Venue">
            <option value="">No venue</option>
            {venues.map((c) => (
              <option key={c.id} value={c.id}>
                {c.title}
              </option>
            ))}
          </select>
        )}
        <button type="button" className={btnPrimary} onClick={() => void create()} disabled={busy}>
          {busy ? <LoaderCircle size={14} className="animate-spin" /> : <CalendarPlus size={14} />}
          Create Luma draft
        </button>
        {error && <ErrorNote>{error}</ErrorNote>}
      </div>
    )

  return <LumaDraftForm key={luma.id} luma={luma} eventId={eventId} store={store} api={api} />
}

function ErrorNote({ children }: { children: string }) {
  return (
    <p role="alert" className="rounded-xl bg-red-50 px-3 py-2 text-xs text-red-700">
      {children}
    </p>
  )
}

function LumaDraftForm({ luma, eventId, store, api }: { luma: LumaEventOut; eventId: string; store: EventStore; api: Api }) {
  const d = luma.draft
  const startIso = str(d.start_at)
  const endIso = str(d.end_at)
  const [name, setName] = useState(str(d.name))
  const [desc, setDesc] = useState(str(d.description_md))
  const [start, setStart] = useState(toLocalInput(startIso))
  const [end, setEnd] = useState(toLocalInput(endIso))
  const [tz, setTz] = useState(str(d.timezone) || 'UTC')
  const [venue, setVenue] = useState(str(d.venue_name))
  const [address, setAddress] = useState(str(d.address))
  const [capacity, setCapacity] = useState(typeof d.capacity === 'number' ? String(d.capacity) : '')
  const [visibility, setVisibility] = useState<'public' | 'private'>(d.visibility === 'public' ? 'public' : 'private')
  const [approval, setApproval] = useState(Boolean(d.require_rsvp_approval))
  const [busy, setBusy] = useState<'save' | 'publish' | 'invite' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [guests, setGuests] = useState('')

  const published = luma.status === 'published'
  const locked = luma.status === 'publishing' || busy !== null
  const base = `/events/${encodeURIComponent(eventId)}/luma`

  const buildDraft = (): LumaEventDraft => {
    // Keep the original ISO string unless the user changed the field (the form shows browser-local time).
    const iso = (value: string, original: string) => (!value ? null : value === toLocalInput(original) ? original : new Date(value).toISOString())
    const startAt = iso(start, startIso)
    if (!startAt) throw new Error('Set a start time.')
    const cap = capacity.trim() ? Number(capacity) : null
    if (cap !== null && (!Number.isInteger(cap) || cap < 1)) throw new Error('Capacity must be a whole number of at least 1.')
    return {
      ...(d as Partial<LumaEventDraft>),
      name: name.trim(),
      description_md: desc,
      start_at: startAt,
      end_at: iso(end, endIso),
      timezone: tz.trim() || 'UTC',
      venue_name: venue.trim() || null,
      address: address.trim() || null,
      capacity: cap,
      visibility,
      require_rsvp_approval: approval,
    }
  }

  const run = async (kind: NonNullable<typeof busy>, fn: () => Promise<void>, publishing = false) => {
    setBusy(kind)
    setError(null)
    setNote(null)
    try {
      await fn()
    } catch (e) {
      setError(lumaError(e, publishing))
    } finally {
      setBusy(null)
    }
  }

  const save = async () => {
    if (!name.trim()) throw new Error('Give the event a name.')
    const out = await api.put<LumaEventOut>(`${base}/draft`, buildDraft())
    store.patch((s) => ({ ...s, luma: out }))
    return out
  }

  const publish = () =>
    void run(
      'publish',
      async () => {
        await save()
        const out = await api.post<LumaEventOut>(`${base}/publish`)
        store.patch((s) => ({ ...s, luma: out }))
        setConfirming(false)
        if (out.status === 'failed') setError(out.error ? `Luma rejected the event: ${out.error}` : 'Publishing failed.')
      },
      true,
    )

  const invite = () => {
    const emails = parseEmails(guests)
    const bad = emails.filter((e) => !isEmail(e))
    if (!emails.length) return setError('Enter at least one email address.')
    if (bad.length) return setError(`Not a valid email address: ${bad.join(', ')}`)
    void run('invite', async () => {
      const out = await api.post<LumaEventOut>(`${base}/invite`, { emails })
      store.patch((s) => ({ ...s, luma: out }))
      setGuests('')
      setNote(`Luma is inviting ${emails.length} ${emails.length === 1 ? 'guest' : 'guests'}.`)
    })
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <StatusChip status={luma.status} />
        {published && luma.url && (
          <a href={luma.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-xs font-medium text-accent hover:underline">
            Open on Luma <ExternalLink size={12} />
          </a>
        )}
        {luma.guests_invited > 0 && <span className="text-xs text-muted">{luma.guests_invited} invited</span>}
      </div>
      {luma.status === 'failed' && luma.error && <ErrorNote>{`Luma error: ${luma.error}`}</ErrorNote>}

      <Field label="Name">
        <input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} disabled={locked} />
      </Field>
      <div className="grid grid-cols-2 gap-2">
        <Field label="Starts">
          <input type="datetime-local" className={inputCls} value={start} onChange={(e) => setStart(e.target.value)} disabled={locked} />
        </Field>
        <Field label="Ends">
          <input type="datetime-local" className={inputCls} value={end} onChange={(e) => setEnd(e.target.value)} disabled={locked} />
        </Field>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <Field label="Time zone">
          <input className={inputCls} value={tz} onChange={(e) => setTz(e.target.value)} disabled={locked} placeholder="America/Chicago" />
        </Field>
        <Field label="Capacity">
          <input className={inputCls} inputMode="numeric" value={capacity} onChange={(e) => setCapacity(e.target.value)} disabled={locked} />
        </Field>
      </div>
      <Field label="Venue">
        <input className={inputCls} value={venue} onChange={(e) => setVenue(e.target.value)} disabled={locked} />
      </Field>
      <Field label="Address">
        <input className={inputCls} value={address} onChange={(e) => setAddress(e.target.value)} disabled={locked} />
      </Field>
      <Field label="Description">
        <textarea className={`${inputCls} min-h-24 resize-y`} value={desc} onChange={(e) => setDesc(e.target.value)} disabled={locked} />
      </Field>
      <div className="flex flex-wrap items-center gap-4 text-xs">
        <label className="flex items-center gap-1.5">
          Visibility
          <select className={`${inputCls} w-auto py-1`} value={visibility} onChange={(e) => setVisibility(e.target.value as 'public' | 'private')} disabled={locked}>
            <option value="private">Private</option>
            <option value="public">Public</option>
          </select>
        </label>
        <label className="flex items-center gap-1.5">
          <input type="checkbox" checked={approval} onChange={(e) => setApproval(e.target.checked)} disabled={locked} />
          Approve RSVPs
        </label>
      </div>

      {confirming ? (
        <div className="space-y-2 rounded-xl bg-amber-50 p-3 text-xs text-amber-900">
          <p>
            {published ? 'Update the event on Luma?' : 'Publish this event to Luma?'} It will be created in your Luma calendar with {visibility}{' '}
            visibility.
          </p>
          <div className="flex gap-2">
            <button type="button" className={btnPrimary} onClick={publish} disabled={locked}>
              {busy === 'publish' ? <LoaderCircle size={14} className="animate-spin" /> : <Send size={14} />}
              Confirm and publish
            </button>
            <button type="button" className={btnGhost} onClick={() => setConfirming(false)} disabled={locked}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
          <button type="button" className={btnGhost} onClick={() => void run('save', async () => void (await save()))} disabled={locked}>
            {busy === 'save' ? <LoaderCircle size={14} className="animate-spin" /> : <Save size={14} />}
            Save draft
          </button>
          <button type="button" className={btnPrimary} onClick={() => setConfirming(true)} disabled={locked}>
            <Send size={14} />
            {published ? 'Update on Luma' : 'Publish to Luma'}
          </button>
        </div>
      )}

      {published && (
        <div className="space-y-2 border-t border-line pt-3">
          <Field label="Invite guests" hint="Luma sends the invitations. Separate addresses with commas or new lines.">
            <textarea className={`${inputCls} min-h-16 resize-y`} value={guests} onChange={(e) => setGuests(e.target.value)} disabled={locked} placeholder="ada@example.com, grace@example.com" />
          </Field>
          <button type="button" className={btnGhost} onClick={invite} disabled={locked || !guests.trim()}>
            {busy === 'invite' ? <LoaderCircle size={14} className="animate-spin" /> : <UserPlus size={14} />}
            Invite guests
          </button>
        </div>
      )}
      {note && <p className="rounded-xl bg-emerald-50 px-3 py-2 text-xs text-emerald-700">{note}</p>}
      {error && <ErrorNote>{error}</ErrorNote>}
    </div>
  )
}
