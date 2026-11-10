import type { EventDetails, Task, TaskResult } from '../types'

// Agents working through an approved option's "What we need to do" checklist.
//
// Backend contract (for when a real LLM replaces `runChecklist`): the server runs the option's
// checklist in order and streams one update per change, e.g. over SSE from
//   POST /api/agents/:taskId/options/:optionTitle/run   { event, option, from }
// as `{ index, status, note }`. The agent must report every item it finishes with status 'done'
// and a short note saying what it did ("Email sent to dana@…"), and report 'needs-you' for any
// item only a person can do (printing, rehearsing, signing) instead of skipping it silently.
// Anything that goes public (publishing the Luma event page) must come back as 'needs-you': the
// user ticking it is the confirmation, and only then should the frontend call the publish endpoint.
//
// Items that need someone else to agree (a quote, a booking, a sponsorship, a judge saying yes)
// are never 'done' when the email goes out. The agent sends it from the user's Gmail and reports
// 'awaiting-reply' with the thread. The backend then watches that thread (Gmail users.watch push
// notifications + history, keyed by threadId) and classifies each reply as confirmed, declined or a
// question. Only 'confirmed' completes the item; anything else comes back as 'needs-you' with the reply.

export type TodoStatus = 'pending' | 'working' | 'awaiting-reply' | 'done' | 'needs-you'

export type Verdict = 'confirmed' | 'declined' | 'question'

// The email thread an item is waiting on.
export interface Thread {
  // Who has to reply, e.g. "Priya Nair (Nimbus Cloud)".
  with: string
  email?: string
  subject: string
  sentAt: number
  reply?: { text: string; at: number; verdict: Verdict }
}

export interface TodoProgress {
  status: TodoStatus
  // Who finished it: the step's agent, or the user ticking it by hand.
  by?: 'agent' | 'you'
  // What the agent did, or why it needs a person.
  note?: string
  thread?: Thread
  // Set when the item can't run until Google is connected (the agent emails from the user's Gmail).
  blockedBy?: 'google'
}

export type Progress = Record<string, TodoProgress[]>

export const isDone = (p?: TodoProgress) => p?.status === 'done'
export const isAwaiting = (p?: TodoProgress) => p?.status === 'awaiting-reply'
// Still for the agent to do: never started, interrupted, or an email held back until Gmail was connected.
export const isOpen = (p: TodoProgress | undefined, gmail: boolean) =>
  !p || p.status === 'pending' || p.status === 'working' || (gmail && p.blockedBy === 'google')

// Items only finished when someone outside the team answers yes. "Collect dietary restrictions from
// registration" or "Collect team names" read data we already have, so they don't wait on anyone.
const NEEDS_REPLY = /^(confirm|ask|request|get|book|propose|recruit)\b|^send the (invite|sponsorship)|^collect (logo|sponsor|pdfs)/i
export const needsReply = (item: string) => NEEDS_REPLY.test(item)

// Who has to answer for this item: the option's contact unless the item names someone else.
function counterpart(option: TaskResult, item: string): Pick<Thread, 'with' | 'email'> {
  if (/approver/i.test(item)) return { with: 'your approver' }
  if (/speakers?\b/i.test(item)) return { with: 'the speakers' }
  if (/volunteers/i.test(item)) return { with: 'volunteer sign-ups' }
  if (/guests/i.test(item)) return { with: 'your guests' }
  if (/sponsors?\b/i.test(item) && !option.contact) return { with: 'your sponsors' }
  if (option.contact) return { with: `${option.contact.name} (${option.title})`, email: option.contact.email }
  return { with: option.title }
}

// Sends the item as an email and returns what to show while the thread waits for an answer.
export function sendForReply(option: TaskResult, item: string, event: EventDetails): TodoProgress {
  const who = counterpart(option, item)
  return {
    status: 'awaiting-reply',
    note: `Emailed ${who.email ?? who.with}. Watching the thread; this completes only when they confirm.`,
    thread: { ...who, subject: `${item} · ${event.city}, ${event.date || 'dates TBD'}`, sentAt: Date.now() },
  }
}

// What a reply does to the item: a confirmation finishes it; anything else hands it to the user.
export function applyReply(thread: Thread, reply: NonNullable<Thread['reply']>): TodoProgress {
  const t = { ...thread, reply }
  if (reply.verdict === 'confirmed') return { status: 'done', by: 'agent', note: `Confirmed by ${thread.with}.`, thread: t }
  if (reply.verdict === 'declined')
    return { status: 'needs-you', note: `${thread.with} said no. Follow up or pick another option.`, thread: t }
  return { status: 'needs-you', note: `${thread.with} asked a question. Answer it, then follow up for a confirmation.`, thread: t }
}

// Sending a follow-up puts the item back on the thread, waiting again.
export const followUp = (thread: Thread): TodoProgress => ({
  status: 'awaiting-reply',
  note: `Followed up with ${thread.email ?? thread.with}. Waiting for a confirmation.`,
  thread: { ...thread, sentAt: Date.now(), reply: undefined },
})

// --- Stand-in agent, until the backend exists. Every note below is made up. ---

// Items an agent can't do on its own; a person has to.
const HUMAN = /\b(print|rehearse|test the|walk-through|tasting|tour|visit|sign|pick up|hand out|put the)\b/i

function noteFor(task: Task, option: TaskResult, item: string, event: EventDetails, gmail: boolean): TodoProgress {
  if (HUMAN.test(item)) return { status: 'needs-you', note: 'An agent can’t do this one. Tick it when it’s done.' }
  // Anything public (a Luma page, a form) only goes live once the user confirms.
  if (/^publish\b/i.test(item)) return { status: 'needs-you', note: 'Ready to go live. Tick it to confirm and publish.' }
  if (needsReply(item))
    return gmail
      ? sendForReply(option, item, event)
      : { status: 'needs-you', blockedBy: 'google', note: `Connect Google so the agent can email ${counterpart(option, item).with} from your Gmail.` }
  const verb = item.toLowerCase()
  const note = /^(send|email|share)/.test(verb)
    ? `Sent by the ${task.agent} agent.`
    : /^(check|collect)/.test(verb)
      ? `Checked by the ${task.agent} agent.`
      : `Done by the ${task.agent} agent.`
  return { status: 'done', by: 'agent', note }
}

// Stand-in replies, until the backend watches Gmail. Most say yes; some ask or decline,
// so the "not a confirmation" path shows up in demos.
const REPLIES: Record<Verdict, string[]> = {
  confirmed: ['Yes, confirmed. Looking forward to it!', 'Confirmed on our end. Invoice to follow.', 'We’re in. Count us as confirmed.'],
  question: ['Happy to help. Can you share the exact start time and expected headcount first?', 'Could you send the floor plan before we commit?'],
  declined: ['Unfortunately we can’t make that date work.', 'We’ll have to pass this time, sorry.'],
}

// Waits for a reply on the thread and calls `onReply` once. Returns a function that stops watching.
export function watchReply(_thread: Thread, onReply: (reply: NonNullable<Thread['reply']>) => void): () => void {
  const timer = window.setTimeout(() => {
    const roll = Math.random()
    const verdict: Verdict = roll < 0.72 ? 'confirmed' : roll < 0.88 ? 'question' : 'declined'
    const options = REPLIES[verdict]
    onReply({ verdict, text: options[Math.floor(Math.random() * options.length)], at: Date.now() })
  }, 7000 + Math.random() * 13000)
  return () => clearTimeout(timer)
}

// Works through the option's checklist from the first item not yet finished.
// Calls `onUpdate` for every change and returns a function that stops the run.
export function runChecklist(
  task: Task,
  option: TaskResult,
  event: EventDetails,
  // Whether the user's Gmail is connected; replies can only be tracked on threads it sent.
  gmail: boolean,
  current: TodoProgress[],
  onUpdate: (index: number, p: TodoProgress) => void,
): () => void {
  const items = option.todo ?? []
  let stopped = false
  let timer = 0
  const step = (i: number) => {
    while (i < items.length && !isOpen(current[i], gmail)) i++
    if (stopped || i >= items.length) return
    onUpdate(i, { status: 'working' })
    timer = window.setTimeout(() => {
      if (stopped) return
      onUpdate(i, noteFor(task, option, items[i], event, gmail))
      step(i + 1)
    }, 1100 + Math.random() * 900)
  }
  step(0)
  return () => {
    stopped = true
    clearTimeout(timer)
  }
}
