// Venue cards (backend `VenueCardData`, see backend/app/schemas/venue.py) → workflow `TaskResult`s.
//
// A venue search is one coordinator run (`venue_search`) that spawns a scout, then one `venue` card plus one
// `venue_research` child run per candidate, and finally a `summary` card (agent_run_id = the coordinator).
// Everything here is pure so the runner and the live-refresh hook derive results the same way.
import type { AgentRunOut, CardOut } from '../../api/types'
import type { EventStoreState } from '../../api/realtime'
import type { Task, TaskResult } from '../../types'

export type Fit = 'yes' | 'tight' | 'no' | 'unknown'
export type Risk = 'low' | 'medium' | 'high' | 'unknown'

export interface PlaceSummary {
  name: string
  address?: string | null
  lat?: number | null
  lng?: number | null
  place_id?: string | null
  website?: string | null
  phone?: string | null
  rating?: number | null
  review_count?: number | null
  price_level?: string | null
  types?: string[] | null
  photo_urls?: string[] | null
  maps_url?: string | null
}
export interface CapacitySection {
  estimated_capacity?: number | null
  fits_headcount?: Fit
  notes?: string | null
  source?: string | null
}
export interface ParkingSection {
  on_site?: boolean | null
  summary?: string | null
  nearby?: { name: string; distance_m?: number | null; kind?: string | null }[]
  score?: number | null
}
export interface WeatherSection {
  summary?: string | null
  temp_high_c?: number | null
  temp_low_c?: number | null
  precipitation_probability?: number | null
  basis?: 'forecast' | 'climatology' | 'unknown'
  risk?: Risk
  notes?: string | null
}
export interface TrafficSection {
  summary?: string | null
  origin?: string | null
  typical_minutes?: number | null
  event_time_minutes?: number | null
  delay_ratio?: number | null
  rush_hour_overlap?: boolean | null
  transit_notes?: string | null
  score?: number | null
}
export interface PricingSection {
  summary?: string | null
  estimate?: string | null
  within_budget?: Fit
}
export interface VenueScore {
  overall: number
  capacity?: number | null
  parking?: number | null
  weather?: number | null
  traffic?: number | null
  value?: number | null
}
export interface VenueCardData {
  place?: PlaceSummary
  capacity?: CapacitySection | null
  parking?: ParkingSection | null
  weather?: WeatherSection | null
  traffic?: TrafficSection | null
  pricing?: PricingSection | null
  pros?: string[]
  cons?: string[]
  score?: VenueScore | null
  summary?: string | null
  sources?: { title: string; url?: string | null }[]
  user_notes?: string | null
  rank?: number | null
  error?: { message?: string } | null
}

/** One line of the summary card's ranking. */
export interface RankingEntry {
  card_id: string
  name: string
  score: number | null
  one_liner: string
}

export type CardStatus = 'running' | 'ready' | 'error' | string

/** What a venue `TaskResult` carries in `extra` (JSON-safe). */
export interface VenueExtra {
  kind: 'venue'
  searchRunId?: string
  status: CardStatus
  version: number
  lockedFields: string[]
  /** The agent currently (or last) working on the card. */
  run?: { id: string; kind: string; status: string; progress: number; currentStep: string; error: string | null; note?: string }
  data: VenueCardData
  mapsUrl?: string
  sample: boolean
  /** The search's closing summary and this venue's line in the ranking, once the search finished. */
  shortlist?: string
  oneLiner?: string
}

export const SECTIONS = ['capacity', 'parking', 'weather', 'traffic'] as const
export type SectionKey = (typeof SECTIONS)[number]

export const venueExtra = (r: TaskResult): VenueExtra | undefined =>
  r.cardId && (r.extra as Partial<VenueExtra> | undefined)?.kind === 'venue' ? (r.extra as unknown as VenueExtra) : undefined

export const isVenueResult = (r: TaskResult) => venueExtra(r) !== undefined

/** An agent is (about to be) working on the card. */
export const isVenueBusy = (ex: VenueExtra) =>
  ex.status === 'running' || (!!ex.run && (ex.run.status === 'running' || ex.run.status === 'queued'))

/** What the collapsed option should show instead of its address line, if anything. */
export function venueActivity(r: TaskResult): 'busy' | 'error' | null {
  const ex = venueExtra(r)
  return !ex ? null : isVenueBusy(ex) ? 'busy' : ex.status === 'error' ? 'error' : null
}

const MOCK_PLACE_PREFIX = 'mock_'

/** Sample venues come from the backend's MockPlacesProvider (no GOOGLE_MAPS_API_KEY). */
export const isSampleVenue = (d: VenueCardData, title: string) =>
  !!d.place?.place_id?.startsWith(MOCK_PLACE_PREFIX) || /^\[Sample\]/.test(d.place?.name ?? title)

const PRICE_LEVEL: Record<string, string> = {
  free: 'Free',
  inexpensive: '$',
  moderate: '$$',
  expensive: '$$$',
  very_expensive: '$$$$',
}

const FIT_TAG: Partial<Record<Fit, string>> = { yes: 'Fits headcount', tight: 'Tight fit', no: 'Too small' }
const BUDGET_TAG: Partial<Record<Fit, string>> = { yes: 'Within budget', tight: 'Near budget', no: 'Over budget' }

const runsOf = (s: Pick<EventStoreState, 'runs'>) => Object.values(s.runs)

/** The summary card a finished search left behind. */
export function summaryCardOf(s: Pick<EventStoreState, 'cards'>, searchRunId: string): CardOut | undefined {
  return Object.values(s.cards).find((c) => c.kind === 'summary' && c.agent_run_id === searchRunId)
}

/**
 * Venue card ids of one search, best first once ranked. Sources (any of them is enough):
 * the coordinator's output `card_ids`, its `venue_research` children's `card_id`, and cards whose
 * `agent_run_id` is one of those children.
 */
export function searchCardIds(s: Pick<EventStoreState, 'runs' | 'cards'>, searchRunId: string): string[] {
  const ids: string[] = []
  const add = (id: unknown) => typeof id === 'string' && s.cards[id]?.kind === 'venue' && !ids.includes(id) && ids.push(id)
  const output = s.runs[searchRunId]?.output as { card_ids?: unknown } | undefined
  if (Array.isArray(output?.card_ids)) output.card_ids.forEach(add)
  const children = runsOf(s)
    .filter((r) => r.parent_run_id === searchRunId)
    .sort((a, b) => a.created_at.localeCompare(b.created_at))
  const childIds = new Set(children.map((r) => r.id))
  children.forEach((r) => add(r.card_id))
  Object.values(s.cards)
    .filter((c) => c.kind === 'venue' && c.agent_run_id && childIds.has(c.agent_run_id))
    .sort((a, b) => a.created_at.localeCompare(b.created_at))
    .forEach((c) => add(c.id))
  return ids
}

const round = (n: number) => Math.round(n)

/** Agent errors arrive as "VenueSearchError: LLMError: Claude API error 400: … {'message': '…'}"; keep the readable part. */
export function cleanError(raw: string | null | undefined): string | null {
  if (!raw) return null
  const quoted = raw.match(/['"]message['"]:\s*['"]([^'"]+)['"]/)?.[1]
  return (quoted ?? raw.replace(/^(\w+Error:\s*)+/, '')).trim() || null
}

function runInfo(run: AgentRunOut | undefined): VenueExtra['run'] {
  if (!run) return undefined
  const note = (run.output as { note?: unknown } | undefined)?.note
  return {
    id: run.id,
    kind: run.kind,
    status: run.status,
    progress: run.progress,
    currentStep: run.current_step,
    error: cleanError(run.error),
    note: typeof note === 'string' && note ? note : undefined,
  }
}

/** Map one venue card (plus the run working on it and the search's ranking) to a workflow option. */
export function cardToResult(
  card: CardOut,
  s: Pick<EventStoreState, 'runs' | 'cards'>,
  searchRunId?: string,
  dateText?: string,
): TaskResult {
  const d = (card.data ?? {}) as VenueCardData
  const place = d.place ?? { name: card.title }
  const summaryCard = searchRunId ? summaryCardOf(s, searchRunId) : undefined
  const ranking = (summaryCard?.data as { ranking?: RankingEntry[]; summary?: string } | undefined) ?? undefined
  const entry = ranking?.ranking?.find((r) => r.card_id === card.id)
  const sample = isSampleVenue(d, card.title)
  const title = card.title || place.name

  const tags: string[] = []
  if (d.rank) tags.push(d.rank === 1 ? 'Top pick' : `#${d.rank} pick`)
  if (d.score?.overall != null) tags.push(`Score ${d.score.overall}`)
  const fit = d.capacity?.fits_headcount && FIT_TAG[d.capacity.fits_headcount]
  if (fit) tags.push(fit)
  const budget = d.pricing?.within_budget && BUDGET_TAG[d.pricing.within_budget]
  if (budget) tags.push(budget)
  if (d.parking?.on_site) tags.push('On-site parking')
  if (d.weather?.risk === 'high' || d.weather?.risk === 'medium') tags.push(`Weather risk: ${d.weather.risk}`)
  if (d.traffic?.rush_hour_overlap) tags.push('Rush hour')
  if (sample) tags.push('Sample venue')

  const t = d.traffic
  const distance =
    t?.event_time_minutes != null
      ? `${round(t.event_time_minutes)} min drive${t.rush_hour_overlap ? ' (rush hour)' : ''}`
      : t?.typical_minutes != null
        ? `${round(t.typical_minutes)} min drive`
        : undefined

  const why = [entry?.one_liner, d.summary].filter((x): x is string => !!x && !!x.trim())
  const contact = place.phone || place.website ? { name: 'Events team', role: place.website ?? undefined, phone: place.phone ?? undefined } : undefined

  const extra: VenueExtra = {
    kind: 'venue',
    searchRunId,
    status: card.status,
    version: card.version,
    lockedFields: card.locked_fields ?? [],
    run: runInfo(card.agent_run_id ? s.runs[card.agent_run_id] : undefined),
    data: d,
    mapsUrl: place.maps_url ?? undefined,
    sample,
    shortlist: ranking?.summary,
    oneLiner: entry?.one_liner || undefined,
  }

  return {
    title,
    subtitle: place.address ?? 'Venue',
    address: place.address ?? undefined,
    meta: sample ? 'Sample venues' : undefined,
    rating: place.rating ?? undefined,
    capacity: d.capacity?.estimated_capacity != null ? `Fits ${d.capacity.estimated_capacity}` : undefined,
    price: d.pricing?.estimate ?? (place.price_level ? PRICE_LEVEL[place.price_level] ?? place.price_level : undefined),
    distance,
    tags,
    pin: place.lat != null && place.lng != null ? { x: place.lng, y: place.lat } : place.maps_url ? { x: 0, y: 0 } : undefined,
    why: why.length ? why.join(' ') : undefined,
    todo: [`Confirm ${title} is free${dateText ? ` on ${dateText}` : ''}`, 'Request a quote and floor plan', 'Book a site visit'],
    contact,
    cardId: card.id,
    agentRunId: card.agent_run_id ?? undefined,
    extra: extra as unknown as Record<string, unknown>,
  }
}

/** Results for every venue card of a search, best first (failed/unranked cards keep scout order after the ranked ones). */
export function searchResults(s: Pick<EventStoreState, 'runs' | 'cards'>, searchRunId: string, dateText?: string): TaskResult[] {
  const results = searchCardIds(s, searchRunId).map((id) => cardToResult(s.cards[id], s, searchRunId, dateText))
  const rankOf = (r: TaskResult) => venueExtra(r)?.data.rank ?? Number.POSITIVE_INFINITY
  return results.map((r, i) => ({ r, i })).sort((a, b) => rankOf(a.r) - rankOf(b.r) || a.i - b.i).map(({ r }) => r)
}

/**
 * Re-derive a step's venue results from the live board: refresh every result that has a venue card, add cards
 * the same search created since, and drop cards that were deleted. Hand-added options stay at the end.
 * Fields other modules set on a result (presentationId, emailId, extra keys…) survive the refresh.
 */
export function refreshTask(task: Task, s: Pick<EventStoreState, 'runs' | 'cards'>): Task {
  const results = task.results
  // Board not loaded yet: keep what we had rather than flashing an empty list.
  if (!results?.some(isVenueResult) || !Object.keys(s.cards).length) return task
  const searchIds = [...new Set(results.map((r) => venueExtra(r)?.searchRunId).filter((x): x is string => !!x))]
  const prior = new Map(results.filter((r) => r.cardId).map((r) => [r.cardId!, r]))
  const dateText = results.map((r) => (r.extra as { dateText?: string } | undefined)?.dateText).find(Boolean)

  const fresh: TaskResult[] = []
  const seen = new Set<string>()
  for (const id of searchIds)
    for (const r of searchResults(s, id, dateText)) {
      if (seen.has(r.cardId!)) continue
      seen.add(r.cardId!)
      fresh.push(r)
    }
  // Venue results whose search we can no longer trace (e.g. runs not loaded) but whose card still exists.
  for (const r of results) {
    const ex = venueExtra(r)
    if (!ex || seen.has(r.cardId!) || !s.cards[r.cardId!]) continue
    seen.add(r.cardId!)
    fresh.push(cardToResult(s.cards[r.cardId!], s, ex.searchRunId, dateText))
  }

  const merged = fresh.map((r) => {
    const old = prior.get(r.cardId!)
    return old ? { ...old, ...r, extra: { ...old.extra, ...r.extra, dateText } } : { ...r, extra: { ...r.extra, dateText } }
  })
  const others = results.filter((r) => !isVenueResult(r) && !(r.cardId && seen.has(r.cardId)))
  const next = [...merged, ...others]
  return sameResults(results, next) ? task : { ...task, results: next }
}

// Cheap structural check so unrelated store updates don't re-render the canvas.
function sameResults(a: TaskResult[], b: TaskResult[]): boolean {
  if (a.length !== b.length) return false
  return a.every((r, i) => {
    const x = venueExtra(r)
    const y = venueExtra(b[i])
    if (!x || !y) return r === b[i]
    return (
      r.cardId === b[i].cardId &&
      x.version === y.version &&
      x.status === y.status &&
      x.run?.id === y.run?.id &&
      x.run?.progress === y.run?.progress &&
      x.run?.status === y.run?.status &&
      x.run?.currentStep === y.run?.currentStep &&
      x.shortlist === y.shortlist &&
      x.oneLiner === y.oneLiner &&
      r.title === b[i].title
    )
  })
}
