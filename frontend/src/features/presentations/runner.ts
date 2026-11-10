// Runner for the deck steps (integration 'google-slides': hackathon `dayof`, business `presentation`,
// conference `slides`, reunion `memories`). Builds a deck with the backend presentation agents
// (POST /events/{id}/presentations → outline agent, then one render agent per slide) and returns one
// result per deck. Decks that already exist for the event are returned instead of building again.
import { isApiError } from '../../api/client'
import { slidesOf, type EventStore } from '../../api/realtime'
import type {
  AgentRunOut,
  CardOut,
  EventOut,
  PresentationBrief,
  PresentationDetail,
  PresentationOut,
  PresentationStarted,
} from '../../api/types'
import type { RunnerContext, TaskRunner } from '../../lib/runners'
import type { EventDetails, Task, TaskResult } from '../../types'

const BUILD_TIMEOUT_MS = 15 * 60_000

interface DeckRecipe {
  focus: string
  audience: string
  tone: string
  slideCount: number
  style?: string
}

/** What each deck step asks the presentation agents for. */
const RECIPES: Record<string, DeckRecipe> = {
  dayof: {
    focus:
      'A day-of run sheet to project in the room: the hour-by-hour schedule, key logistics (check-in, Wi-Fi, food, ' +
      'quiet spaces), volunteer roles and who to ask for help, judging and submission rules, and a closing slide ' +
      'with where to follow along on their phones.',
    audience: 'Participants, volunteers and organizers on the day',
    tone: 'energetic, clear and practical',
    slideCount: 7,
  },
  presentation: {
    focus:
      'An agenda deck to open the meeting: purpose and goals, the agenda with times, speakers or owners, logistics, ' +
      'and the outcomes we want to leave with.',
    audience: 'Attendees of the business event',
    tone: 'executive and polished',
    slideCount: 8,
  },
  slides: {
    focus:
      'The opening and housekeeping deck for the conference stage: welcome, the programme and tracks, venue map and ' +
      'logistics, how to join the live slides and transcript, sponsors, and a thank-you slide.',
    audience: 'Conference attendees in the main room',
    tone: 'confident and welcoming',
    slideCount: 8,
  },
  memories: {
    focus:
      'A photo-slideshow style memories deck: then-and-now moments, highlights and milestones, a tribute section, ' +
      'and a closing slide inviting everyone to share their photos.',
    audience: 'Old friends and classmates at the reunion',
    tone: 'warm, nostalgic and playful',
    slideCount: 8,
    style: 'Photo-forward layouts with generous whitespace and soft, warm colours.',
  },
}

const fallbackRecipe = (task: Task): DeckRecipe => ({
  focus: `${task.title}: ${task.description}`,
  audience: 'Event attendees',
  tone: 'clear and friendly',
  slideCount: 7,
})

const str = (v: unknown): string | null => (typeof v === 'string' && v.trim() ? v.trim() : null)

/** One line per known fact; the backend requirements win over the plan's (possibly default) details. */
function describeEvent(req: Record<string, unknown>, event: EventDetails): string[] {
  const lines: string[] = []
  const add = (label: string, v: unknown, fallback?: unknown) => {
    const value = v ?? fallback
    if (Array.isArray(value) && value.length) lines.push(`${label}: ${value.join(', ')}`)
    else if (typeof value === 'number' || typeof value === 'boolean') lines.push(`${label}: ${String(value)}`)
    else if (str(value)) lines.push(`${label}: ${str(value)}`)
  }
  add('Kind', req.event_type, event.type)
  add('Date', req.event_date, event.date || null)
  add('Starts', req.start_time)
  add('Ends', req.end_time)
  add('Location', req.location, event.city || null)
  add('Headcount', req.headcount, event.headcount || null)
  add('Budget', req.budget)
  add('Must-haves', req.must_haves)
  add('Notes', req.notes)
  return lines
}

/** Venue cards worth feeding to the deck: approved ones if any are marked, otherwise the ready shortlist. */
function venueCardIds(cards: Record<string, CardOut>): string[] {
  const venues = Object.values(cards).filter((c) => c.kind === 'venue' && c.status !== 'error')
  const approved = venues.filter((c) => {
    const d = c.data as Record<string, unknown>
    return d.approved === true || d.decision === 'approved' || d.selected === true || d.status === 'approved'
  })
  return (approved.length ? approved : venues).slice(0, 5).map((c) => c.id)
}

export function buildBrief(task: Task, event: EventDetails, store: EventStore): PresentationBrief {
  const recipe = RECIPES[task.id] ?? fallbackRecipe(task)
  const state = store.getState()
  const ev = state.event
  const req = (ev?.requirements ?? {}) as Record<string, unknown>
  const facts = [
    ev?.name ? `Event: ${ev.name}` : null,
    str(ev?.description) ? `About: ${ev?.description}` : null,
    ...describeEvent(req, event),
  ].filter((l): l is string => !!l)
  const prompt = `${recipe.focus}\n\nWhat we know about the event:\n${facts.map((l) => `- ${l}`).join('\n')}`
  return {
    prompt,
    title: ev?.name ? `${ev.name} — ${task.title}` : task.title,
    audience: recipe.audience,
    tone: recipe.tone,
    slide_count: recipe.slideCount,
    style: recipe.style ?? null,
    card_ids: venueCardIds(state.cards),
  }
}

/** Put a presentation (and optionally its slides / run) into the shared store, keeping newer copies. */
export function mergeDeck(store: EventStore, presentation: PresentationOut, slides?: PresentationDetail['slides'], run?: AgentRunOut) {
  store.patch((s) => {
    const prev = s.presentations[presentation.id]
    const presentations = !prev || presentation.version >= prev.version ? { ...s.presentations, [presentation.id]: presentation } : s.presentations
    let nextSlides = s.slides
    if (slides) {
      nextSlides = { ...s.slides }
      // Drop slides the server no longer has for this deck.
      const keep = new Set(slides.map((x) => x.id))
      for (const [id, x] of Object.entries(nextSlides)) if (x.presentation_id === presentation.id && !keep.has(id)) delete nextSlides[id]
      for (const x of slides) if (!nextSlides[x.id] || x.version >= nextSlides[x.id].version) nextSlides[x.id] = x
    }
    const runs = run && !s.runs[run.id] ? { ...s.runs, [run.id]: run } : s.runs
    return { ...s, presentations, slides: nextSlides, runs }
  })
}

export function deckResult(p: PresentationOut, slideCount: number, extra: Record<string, unknown> = {}): TaskResult {
  const prompt = str((p.brief as Record<string, unknown>).prompt)
  return {
    title: p.title,
    subtitle: `${slideCount} slide${slideCount === 1 ? '' : 's'}`,
    presentationId: p.id,
    agentRunId: p.agent_run_id ?? undefined,
    why: prompt ?? undefined,
    todo: ['Open and review the deck', 'Present live with voice-driven slides'],
    extra: { status: p.status, ...extra },
  }
}

/** Wait until the deck's build run finishes or the deck leaves "generating", then return its detail. */
async function settle(ctx: RunnerContext, p: PresentationOut, runId: string | null): Promise<PresentationDetail> {
  const { store, api } = ctx
  const ac = new AbortController()
  const waits: Promise<unknown>[] = [
    store.waitFor((s) => s.presentations[p.id] && s.presentations[p.id].status !== 'generating', { signal: ac.signal }),
  ]
  if (runId) waits.push(store.waitForRun(runId, { signal: ac.signal }))
  // Polling fallback for when neither the socket nor the run lookup reports progress.
  const poll = (async () => {
    while (!ac.signal.aborted) {
      await new Promise((r) => setTimeout(r, 6000))
      if (ac.signal.aborted) return
      const d = await api.get<PresentationDetail>(`/presentations/${encodeURIComponent(p.id)}`).catch(() => null)
      if (d) mergeDeck(store, d.presentation, d.slides)
      if (d && d.presentation.status !== 'generating') return
    }
  })()
  waits.push(poll)
  // The losers reject with AbortError once we abort below; mark them handled.
  for (const w of waits) w.catch(() => {})
  let timer: ReturnType<typeof setTimeout> | undefined
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error('The deck is taking too long to build.')), BUILD_TIMEOUT_MS)
  })
  try {
    await Promise.race([...waits, timeout])
  } finally {
    clearTimeout(timer)
    ac.abort()
  }
  const detail = await api.get<PresentationDetail>(`/presentations/${encodeURIComponent(p.id)}`)
  mergeDeck(store, detail.presentation, detail.slides)
  return detail
}

const readySlides = (d: PresentationDetail) => d.slides.filter((s) => s.status === 'ready').length

export const runDeckTask: TaskRunner = async (task, event, ctx) => {
  const { store, api, eventId } = ctx
  const release = store.retain()
  try {
    const id = encodeURIComponent(eventId)
    // Existing decks for this event: show them instead of building another one.
    const existing = (await api.get<PresentationOut[]>(`/events/${id}/presentations`)).filter((p) => p.status !== 'error')
    if (existing.length) {
      const results: TaskResult[] = []
      for (const p of existing) {
        const detail =
          p.status === 'generating'
            ? await settle(ctx, p, p.agent_run_id)
            : await api.get<PresentationDetail>(`/presentations/${encodeURIComponent(p.id)}`)
        mergeDeck(store, detail.presentation, detail.slides)
        results.push(deckResult(detail.presentation, detail.slides.length, { ready: readySlides(detail) }))
      }
      return results
    }

    // The brief reads the event's requirements and venue cards from the store: let its snapshot land first.
    await store.waitFor((s) => s.loaded || !!s.error, { timeoutMs: 5000 }).catch(() => {})
    if (!store.getState().event) {
      const ev = await api.get<EventOut>(`/events/${id}`).catch(() => null)
      if (ev) store.patch((s) => (s.event ? s : { ...s, event: ev }))
    }
    const brief = buildBrief(task, event, store)
    let started: PresentationStarted
    try {
      started = await api.post<PresentationStarted>(`/events/${id}/presentations`, brief)
    } catch (e) {
      if (isApiError(e, 503)) throw new Error('Deck building needs Claude: set ANTHROPIC_API_KEY on the backend.')
      throw e
    }
    mergeDeck(store, started.presentation, undefined, started.run)
    const detail = await settle(ctx, started.presentation, started.run.id)
    const ready = readySlides(detail)
    if (!ready) {
      // The deck turns "error" a moment before its run records why; give the run a few seconds to finish.
      const run = await store
        .waitForRun(started.run.id, { timeoutMs: 5000 })
        .catch(() => store.getState().runs[started.run.id])
      const reason = run?.error?.replace(/^\w*Error:\s*/, '').slice(0, 300)
      throw new Error(reason ? `The deck could not be built: ${reason}` : 'The deck could not be built.')
    }
    const slides = slidesOf(store.getState(), detail.presentation.id).length || detail.slides.length
    return [deckResult(detail.presentation, slides, { ready, failed: detail.slides.length - ready })]
  } finally {
    release()
  }
}
