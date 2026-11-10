// Shared helpers for the outreach feature: error text, email parsing, drafting templates, store access.
import { useSyncExternalStore } from 'react'
import { isApiError } from '../../api/client'
import type { EventStore, EventStoreState } from '../../api/realtime'
import type { EmailMessageOut } from '../../api/types'
import type { EventDetails, Task, TaskResult } from '../../types'

export type EmailKind = 'venue_inquiry' | 'invite' | 'update' | 'custom'

export const errorText = (e: unknown): string => {
  if (isApiError(e)) {
    if (e.status === 0) return 'Could not reach the Evently server.'
    return e.message
  }
  return e instanceof Error ? e.message : 'Something went wrong.'
}

/** Subscribe to an event store without retaining it (the app already keeps it live). */
export function useStoreState(store: EventStore): EventStoreState {
  return useSyncExternalStore(store.subscribe, store.getState)
}

const EMAIL_RE = /^[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+$/
export const isEmail = (s: string) => EMAIL_RE.test(s)

/** Split on commas, semicolons, whitespace; drops empties and duplicates. */
export const parseEmails = (text: string): string[] => [
  ...new Set(
    text
      .split(/[\s,;]+/)
      .map((s) => s.trim())
      .filter(Boolean),
  ),
]

/** Put an email in the store right away (the websocket envelope may lag or be down). */
export function cacheEmail(store: EventStore, email: EmailMessageOut) {
  store.patch((s) => ({ ...s, emails: { ...s.emails, [email.id]: email } }))
}
export function uncacheEmail(store: EventStore, id: string) {
  store.patch((s) => {
    if (!(id in s.emails)) return s
    const emails = { ...s.emails }
    delete emails[id]
    return { ...s, emails }
  })
}

export const STATUS_LABEL: Record<string, string> = {
  draft: 'Draft',
  sending: 'Sending',
  sent: 'Sent',
  failed: 'Failed',
  publishing: 'Publishing',
  published: 'Published',
}
export const statusLabel = (s: string) => STATUS_LABEL[s] ?? s

export const statusTone = (s: string): string =>
  s === 'sent' || s === 'published'
    ? 'bg-emerald-50 text-emerald-700 ring-emerald-600/20'
    : s === 'failed'
      ? 'bg-red-50 text-red-700 ring-red-600/20'
      : s === 'sending' || s === 'publishing'
        ? 'bg-amber-50 text-amber-700 ring-amber-600/20'
        : 'bg-gray-100 text-gray-700 ring-gray-500/20'

// --- Drafting ----------------------------------------------------------------------

const TYPE_NOUN: Record<string, string> = {
  business: 'business event',
  conference: 'conference',
  hackathon: 'hackathon',
  wedding: 'wedding',
  reunion: 'reunion',
}

const dateText = (ev: EventDetails) => ev.date.trim() || 'a date still to be confirmed'

export function emailKindFor(task: Task, result: TaskResult): EmailKind {
  const fromExtra = result.extra?.emailKind
  if (fromExtra === 'venue_inquiry' || fromExtra === 'invite' || fromExtra === 'update' || fromExtra === 'custom') return fromExtra
  if (task.id === 'invites') return 'invite'
  if (task.id === 'followup') return 'update'
  if (task.id === 'venue' || result.cardId) return 'venue_inquiry'
  return 'custom'
}

export function defaultSubject(eventName: string, ev: EventDetails, kind: EmailKind, result?: TaskResult): string {
  const base = ev.date.trim() ? `${eventName} on ${ev.date.trim()}` : eventName
  const given = result?.extra?.subject
  if (typeof given === 'string' && given.trim()) return given
  if (kind === 'invite') return `You're invited: ${base}`
  if (kind === 'update') return `Thank you: ${eventName}`
  return `Inquiry: ${base}`
}

export function defaultBody(eventName: string, ev: EventDetails, kind: EmailKind, result: TaskResult): string {
  if (result.draft?.trim()) return result.draft.trim()
  const first = result.contact?.name?.split(' ')[0]
  const greeting = `Hi ${first || (kind === 'venue_inquiry' ? `${result.title} team` : 'there')},`
  const noun = TYPE_NOUN[ev.type] ?? 'event'
  const facts = `${eventName}, a ${noun} for about ${ev.headcount} people in ${ev.city} on ${dateText(ev)}`
  if (kind === 'venue_inquiry')
    return `${greeting}\n\nWe're planning ${facts}, and ${result.title} looks like a great fit. Is the space available on that date? Could you share pricing, capacity, and your cancellation policy? We'd also love to arrange a site visit.\n\nThank you,`
  if (kind === 'invite')
    return `${greeting}\n\nYou're invited to ${facts}. Please let us know whether you can make it.\n\nWe hope to see you there,`
  if (kind === 'update')
    return `${greeting}\n\nThank you for being part of ${eventName} in ${ev.city}. We appreciate your time and support, and we'll follow up with a recap shortly.\n\nWarm regards,`
  return `${greeting}\n\nI'm reaching out about ${facts}. Regarding ${result.title}: could you share availability and pricing?\n\nThank you,`
}

export const eventNameOf = (store: EventStore | null, ev: EventDetails): string =>
  store?.getState().event?.name?.trim() ||
  `${ev.city} ${TYPE_NOUN[ev.type] ?? 'event'}`
