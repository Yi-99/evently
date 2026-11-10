// Event.requirements <-> the planner's local state (event details, intake answers, plan graph).
//
// Persisted shape (PATCH merges key by key; `intake` and `plan` are replaced whole):
//
//   {
//     // VenueCriteria keys: backend agents (venue search, orchestrator, decks) read these.
//     event_type: 'hackathon',
//     location: 'Austin',            // = EventDetails.city
//     headcount: 200,
//     event_date?: '2026-12-05',     // only when the date answer contains a real date
//     budget?: '$5k–$20k',           // intake 'budget' answer
//     must_haves?: ['Photo booth'],  // intake 'features' answer, split on , ; and
//     // Web app state.
//     intake: { type: 'Hackathon', goal: '…', date: 'Dec 5', headcount: '200 people', city: 'Austin', format: 'In person', … },
//     plan: {
//       event: { type, headcount, city, date },   // EventDetails as shown in the UI (date is free text)
//       tasks: [{ id, title, agent, integration, dependsOn }],
//       approved: false,
//       intake_done: true,
//     },
//   }
import { DEFAULT_HEADCOUNT, EVENT_TYPES, buildWorkflow } from '../../data/workflows'
import { nextQuestion, type IntakeAnswers } from '../../lib/intake'
import type { EventDetails, EventType, Integration, Task } from '../../types'
import type { EventOut } from '../../api/types'

export interface PlanTaskSeed {
  id: string
  title: string
  agent: string
  integration: Integration
  dependsOn: string[]
}

export interface PlanState {
  event: EventDetails
  tasks: PlanTaskSeed[]
  approved: boolean
  intake_done: boolean
}

/** What the web app writes to `Event.requirements`. Other writers may add more VenueCriteria keys. */
export interface EventRequirements {
  event_type: EventType
  location: string
  headcount: number
  event_date?: string
  budget?: string
  must_haves?: string[]
  intake: IntakeAnswers
  plan: PlanState
}

const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
const pad = (n: number) => String(n).padStart(2, '0')

/** First calendar date in free text ("Dec 5-6", "2026-12-05", "12/5/2026"), as YYYY-MM-DD; undefined for "next month". */
export function isoDate(text: string): string | undefined {
  const iso = text.match(/\b(\d{4})-(\d{2})-(\d{2})\b/)
  if (iso) return iso[0]
  let month: number | undefined
  let day: number | undefined
  let year: number | undefined
  const named = text
    .toLowerCase()
    .match(/\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:[^\d]{0,12}?(?:\d{1,2}[^\d]{1,4})?(\d{4}))?/)
  if (named) {
    month = MONTHS.indexOf(named[1])
    day = Number(named[2])
    year = named[3] ? Number(named[3]) : undefined
  } else {
    const us = text.match(/\b(\d{1,2})\/(\d{1,2})(?:\/(\d{2,4}))?\b/)
    if (!us) return undefined
    month = Number(us[1]) - 1
    day = Number(us[2])
    year = us[3] ? Number(us[3].length === 2 ? `20${us[3]}` : us[3]) : undefined
  }
  if (month < 0 || month > 11 || day < 1 || day > 31) return undefined
  const today = new Date()
  if (year === undefined) {
    year = today.getFullYear()
    // "Dec 5" said in late December means next year.
    if (new Date(year, month, day) < new Date(today.getFullYear(), today.getMonth(), today.getDate())) year++
  }
  const d = new Date(year, month, day)
  if (d.getMonth() !== month) return undefined // e.g. Feb 31
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

const splitList = (s: string) =>
  s
    .split(/\s*(?:,|;|\band\b|\+)\s*/i)
    .map((x) => x.trim())
    .filter(Boolean)

export const eventLabel = (type: EventType) => EVENT_TYPES.find((e) => e.id === type)?.label ?? 'Event'

/** "Hackathon in Austin". */
export const eventName = (event: Pick<EventDetails, 'type' | 'city'>) =>
  `${eventLabel(event.type)}${event.city ? ` in ${event.city}` : ''}`

const seed = (t: Task): PlanTaskSeed => ({
  id: t.id,
  title: t.title,
  agent: t.agent,
  integration: t.integration,
  dependsOn: t.dependsOn,
})

export function toRequirements(
  event: EventDetails,
  answers: IntakeAnswers,
  tasks: Task[],
  approved: boolean,
  intakeDone: boolean,
): EventRequirements {
  const req: EventRequirements = {
    event_type: event.type,
    location: event.city,
    headcount: event.headcount,
    intake: answers,
    plan: { event: { ...event }, tasks: tasks.map(seed), approved, intake_done: intakeDone },
  }
  const date = isoDate(event.date)
  if (date) req.event_date = date
  if (answers.budget?.trim()) req.budget = answers.budget.trim()
  const musts = answers.features ? splitList(answers.features) : []
  if (musts.length) req.must_haves = musts
  return req
}

const isType = (v: unknown): v is EventType => typeof v === 'string' && EVENT_TYPES.some((e) => e.id === v)
const str = (v: unknown) => (typeof v === 'string' ? v : undefined)

export interface Hydrated {
  event: EventDetails
  tasks: Task[]
  answers: IntakeAnswers
  approved: boolean
  /** True when the intake questions still need answering. */
  intake: boolean
}

/** Rebuild the planner state from a backend event (also works for events the web app never wrote to). */
export function fromEvent(out: EventOut): Hydrated {
  const req = (out.requirements ?? {}) as Record<string, unknown>
  const plan = (req.plan && typeof req.plan === 'object' ? req.plan : {}) as Partial<PlanState>
  const intakeRaw = req.intake && typeof req.intake === 'object' ? (req.intake as Record<string, unknown>) : {}
  const answers: IntakeAnswers = Object.fromEntries(
    Object.entries(intakeRaw).flatMap(([k, v]) => (typeof v === 'string' || typeof v === 'number' ? [[k, String(v)]] : [])),
  )

  const byLabel = EVENT_TYPES.find((e) => e.label.toLowerCase() === (answers.type ?? '').toLowerCase())?.id
  const fromName = EVENT_TYPES.find((e) => out.name.toLowerCase().includes(e.id))?.id
  const type: EventType = isType(req.event_type)
    ? req.event_type
    : isType(plan.event?.type)
      ? plan.event.type
      : (byLabel ?? fromName ?? 'business')
  if (!answers.type) answers.type = eventLabel(type)

  const headcount = typeof req.headcount === 'number' && req.headcount > 0 ? req.headcount : (plan.event?.headcount ?? DEFAULT_HEADCOUNT[type])
  const city = str(req.location) || plan.event?.city || answers.city || 'San Francisco'
  // Keep the free-text date unless something else saved a different calendar date.
  const text = plan.event?.date ?? answers.date ?? ''
  const saved = str(req.event_date)
  const date = saved && saved !== isoDate(text) ? saved : text || saved || ''
  const event: EventDetails = { type, headcount, city, date }

  const template = new Map(buildWorkflow(type).map((t) => [t.id, t]))
  const tasks: Task[] = Array.isArray(plan.tasks)
    ? plan.tasks.map((s) => ({
        id: s.id,
        title: s.title,
        agent: s.agent,
        integration: s.integration,
        dependsOn: Array.isArray(s.dependsOn) ? s.dependsOn : [],
        description: template.get(s.id)?.description ?? `Custom step added from chat: ${s.title}.`,
        status: 'idle',
      }))
    : buildWorkflow(type)

  return {
    event,
    tasks,
    answers,
    approved: plan.approved === true,
    intake: plan.intake_done === false && nextQuestion(answers) !== undefined,
  }
}
