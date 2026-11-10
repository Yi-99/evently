// Build the backend's VenueCriteria from the frontend event details and whatever requirements the backend event holds.
import type { VenueCriteria } from '../../api/types'
import { EVENT_TYPES } from '../../data/workflows'
import type { EventDetails, Task } from '../../types'

type Req = Record<string, unknown>

/** Requirements may be flat or keep intake answers under `answers` / `intake`; read all three. */
function reader(requirements: unknown) {
  const root = requirements && typeof requirements === 'object' ? (requirements as Req) : {}
  const layers = [root, root.answers, root.intake].filter((x): x is Req => !!x && typeof x === 'object' && !Array.isArray(x))
  const get = (...keys: string[]): unknown => {
    for (const k of keys) for (const l of layers) if (l[k] !== undefined && l[k] !== null && l[k] !== '') return l[k]
    return undefined
  }
  const str = (...keys: string[]) => {
    const v = get(...keys)
    if (typeof v === 'string') return v.trim() || undefined
    if (typeof v === 'number') return String(v)
    if (Array.isArray(v)) return v.filter((x) => typeof x === 'string').join(', ') || undefined
    return undefined
  }
  const num = (...keys: string[]) => {
    const v = get(...keys)
    const n = typeof v === 'number' ? v : typeof v === 'string' ? parseInt(v.replace(/,/g, '').match(/\d+/)?.[0] ?? '', 10) : NaN
    return Number.isFinite(n) && n > 0 ? n : undefined
  }
  const list = (...keys: string[]): string[] => {
    const v = get(...keys)
    if (Array.isArray(v)) return v.filter((x): x is string => typeof x === 'string' && !!x.trim())
    if (typeof v === 'string') return v.split(/[,;]|\band\b/).map((x) => x.trim()).filter(Boolean)
    return []
  }
  return { str, num, list }
}

const pad = (n: number) => String(n).padStart(2, '0')
const ymd = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`

/** An ISO or natural-language date ("Dec 5", "March 14, 2027") as YYYY-MM-DD, or null for windows like "next month". */
export function parseEventDate(text: string | undefined, now = new Date()): string | null {
  const t = text?.trim()
  if (!t) return null
  const iso = t.match(/\b(\d{4})-(\d{2})-(\d{2})\b/)
  if (iso) return `${iso[1]}-${iso[2]}-${iso[3]}`
  const cleaned = t.replace(/(\d)(st|nd|rd|th)\b/gi, '$1').replace(/^(on|around|about)\s+/i, '')
  // Only try texts that name a month or a numeric date; Date() accepts far too much otherwise.
  if (!/\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b|\b\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)|\b\d{1,2}\/\d{1,2}(\/\d{2,4})?\b/i.test(cleaned))
    return null
  const hasYear = /\b\d{4}\b|\b\d{1,2}\/\d{1,2}\/\d{2,4}\b/.test(cleaned)
  let d = new Date(hasYear ? cleaned : `${cleaned} ${now.getFullYear()}`)
  if (Number.isNaN(d.getTime())) {
    // "Dec 5 for 2 days" → retry with just the leading date.
    const head = cleaned.match(/^[a-z]+\.?\s+\d{1,2}(,?\s+\d{4})?|^\d{1,2}\s+[a-z]+(\s+\d{4})?|^\d{1,2}\/\d{1,2}(\/\d{2,4})?/i)?.[0]
    if (!head) return null
    d = new Date(hasYear ? head : `${head} ${now.getFullYear()}`)
    if (Number.isNaN(d.getTime())) return null
  }
  // A month/day without a year that already passed means next year.
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  if (!hasYear && d < today) d = new Date(d.getFullYear() + 1, d.getMonth(), d.getDate())
  return ymd(d)
}

const HHMM = /^(\d{1,2}):(\d{2})(?::\d{2})?$/
const time = (v: string | undefined) => (v && HHMM.test(v) ? v.replace(HHMM, (_, h: string, m: string) => `${pad(+h)}:${m}`) : null)

function settingOf(...texts: (string | undefined)[]): VenueCriteria['setting'] {
  const t = texts.filter(Boolean).join(' ').toLowerCase()
  const outdoor = /outdoor|outside|garden|park|rooftop|beach/.test(t)
  const indoor = /indoor|inside/.test(t)
  return outdoor && !indoor ? 'outdoor' : indoor && !outdoor ? 'indoor' : 'either'
}

/** The date text a venue result's checklist mentions ("Confirm X is free on Dec 5"). */
export function dateTextOf(event: EventDetails, requirements: unknown): string | undefined {
  return reader(requirements).str('event_date', 'date') ?? (event.date.trim() || undefined)
}

export function buildCriteria(task: Task, event: EventDetails, requirements: unknown): VenueCriteria {
  const r = reader(requirements)
  const dateText = r.str('event_date', 'date') ?? event.date
  const eventDate = parseEventDate(dateText)
  const notes: string[] = []
  if (dateText?.trim() && !eventDate) notes.push(`When: ${dateText.trim()}`)
  const extras: [string, string | undefined][] = [
    ['Goal', r.str('goal')],
    ['Audience', r.str('audience')],
    ['Format', r.str('format')],
    ['Food', r.str('food')],
    ['Accessibility', r.str('accessibility')],
    ['Already booked', r.str('booked')],
    ['Constraints', r.str('constraints')],
    ['Notes', r.str('notes')],
  ]
  for (const [label, v] of extras) if (v && !/^none$/i.test(v)) notes.push(`${label}: ${v}`)
  if (task.description) notes.push(`What this step needs: ${task.description}`)

  const mustHaves = [...new Set([...r.list('must_haves'), ...r.list('features')])].slice(0, 12)
  const accessibility = r.str('accessibility')
  if (accessibility && /wheelchair/i.test(accessibility) && !mustHaves.some((m) => /wheelchair/i.test(m))) mustHaves.push('Wheelchair access')

  return {
    location: r.str('location', 'city') ?? event.city,
    headcount: r.num('headcount') ?? (event.headcount > 0 ? event.headcount : null),
    radius_km: r.num('radius_km') ?? 15,
    event_date: eventDate,
    start_time: time(r.str('start_time')),
    end_time: time(r.str('end_time')),
    event_type: EVENT_TYPES.find((e) => e.id === event.type)?.label ?? event.type,
    setting: settingOf(r.str('setting'), r.str('format'), r.str('constraints')),
    budget: r.str('budget') ?? null,
    parking_required: true,
    attendee_origin: r.str('attendee_origin') ?? null,
    must_haves: mustHaves,
    notes: notes.join('\n').slice(0, 2000) || null,
    max_candidates: 5,
  }
}
