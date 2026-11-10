import type { Decision } from '../components/ResultNode'
import type { ChatMessage, EventDetails, EventType, Task } from '../types'
import type { Progress } from './execute'
import type { IntakeAnswers } from './intake'

// What happened on the node map: approve / ignore per option, and how far each option's
// checklist has got (by its agent or by the user), both keyed `${step}:${title}`.
export interface Review {
  decisions: Record<string, Decision>
  progress: Progress
  // Plans saved before checklist progress existed: indexes the user ticked.
  checked?: Record<string, number[]>
}

// Everything needed to reopen a plan on the node map where the user left it.
export interface SavedPlan {
  event: EventDetails
  tasks: Task[]
  messages: ChatMessage[]
  answers: IntakeAnswers
  approved: boolean
  review: Review
}

// Events the user started, newest first. Kept in this browser only.
export interface RecentEvent {
  // The plan's id (also scopes its live-slide decks).
  id: string
  type: EventType
  note?: string
  at: number
  // Set once the plan reaches the node map.
  plan?: SavedPlan
  // The backend event this plan was saved as (when the server was reachable). The server copy is the
  // source of truth; this entry keeps what it doesn't store (results, review) for when it reopens.
  eventId?: string
}

const KEY = 'evently.recent'
const MAX = 12

export function loadRecent(): RecentEvent[] {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? '[]')
    // Entries from before plans were saved have no id; give them one so they still list.
    return Array.isArray(raw) ? raw.map((r, i) => ({ ...r, id: r.id ?? `old-${r.at ?? i}` })) : []
  } catch {
    return []
  }
}

function store(list: RecentEvent[]) {
  try {
    localStorage.setItem(KEY, JSON.stringify(list.slice(0, MAX)))
  } catch {
    // Storage blocked (private window) or full: history just isn't kept.
  }
}

// A newly started event goes on top. An unfinished start with the same prompt is replaced;
// saved plans (locally or as a backend event) are kept, since each is its own event.
export function addRecent(e: Omit<RecentEvent, 'at' | 'plan'>) {
  const rest = loadRecent().filter((r) => r.id !== e.id && (r.plan || r.eventId || r.type !== e.type || r.note !== e.note))
  store([{ ...e, at: Date.now() }, ...rest])
}

// Records the backend event a plan was saved as.
export function linkRecent(id: string, eventId: string) {
  const list = loadRecent()
  if (list.some((r) => r.id === id)) store(list.map((r) => (r.id === id ? { ...r, eventId } : r)))
}

// The plan cached in this browser for a backend event, if any.
export const findRecentByEvent = (eventId: string) => loadRecent().find((r) => r.eventId === eventId)

// Agents mid-run can't resume after a reload, so those steps are saved as not started.
export function savePlan(id: string, plan: SavedPlan) {
  const list = loadRecent()
  const entry = list.find((r) => r.id === id)
  if (!entry) return
  const tasks = plan.tasks.map((t) =>
    t.status === 'running' || t.status === 'queued' ? { ...t, status: 'idle' as const, results: undefined } : t,
  )
  // Checklist items an agent was partway through restart from that item when the plan reopens.
  const progress = Object.fromEntries(
    Object.entries(plan.review.progress).map(([k, list]) => [k, list.map((p) => (p?.status === 'working' ? { status: 'pending' as const } : p))]),
  )
  store([{ ...entry, at: Date.now(), plan: { ...plan, tasks, review: { ...plan.review, progress } } }, ...list.filter((r) => r.id !== id)])
}
