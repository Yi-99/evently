import { registerRunner, runMock } from '../../lib/runners'
import type { EventDetails, Task, TaskResult } from '../../types'
import { defaultBody, eventNameOf, type EmailKind } from './lib'

// Email-based steps ("gmail" integration). The backend has no agent that finds judges, so that step keeps the
// labelled sample data. Invitations and follow-ups are generated here from the event details as editable
// message templates; the Email action on each option turns one into a real backend draft.

interface Template {
  title: string
  subtitle: string
  why: string
  kind: EmailKind
  to?: string
  todo: string[]
  subject: (name: string, ev: EventDetails) => string
  body: (name: string, ev: EventDetails) => string
}

const when = (ev: EventDetails) => ev.date.trim() || 'the date to be confirmed'

const INVITES: Template[] = [
  {
    title: 'Guest invitation',
    subject: (name) => `You're invited: ${name}`,
    subtitle: 'Main invite with the date and place',
    why: 'The message every guest gets first. Add recipients in the composer, or use Luma to collect RSVPs.',
    kind: 'invite',
    todo: ['Add recipient addresses', 'Add the RSVP link', 'Send, then track replies'],
    body: (name, ev) =>
      `Hi there,\n\nYou're invited to ${name} in ${ev.city} on ${when(ev)}. We're expecting about ${ev.headcount} guests and would love for you to join us.\n\nPlease reply to let us know if you can make it.\n\nHope to see you there,`,
  },
  {
    title: 'Save the date',
    subject: (name) => `Save the date: ${name}`,
    subtitle: 'Early heads-up before details are final',
    why: 'Goes out first so guests can hold the date while venue and schedule details settle.',
    kind: 'invite',
    todo: ['Add recipient addresses', 'Send 4 to 6 weeks ahead'],
    body: (name, ev) =>
      `Hi there,\n\nPlease save the date: ${name} will take place in ${ev.city} on ${when(ev)}. A formal invitation with all the details will follow soon.\n\nWarm regards,`,
  },
  {
    title: 'Reminder',
    subject: (name, ev) => `Reminder: ${name} on ${when(ev)}`,
    subtitle: 'A week-before nudge',
    why: 'Cuts no-shows and gives guests a last chance to RSVP.',
    kind: 'update',
    todo: ['Add recipient addresses', 'Include arrival and parking notes'],
    body: (name, ev) =>
      `Hi there,\n\nA quick reminder that ${name} is coming up on ${when(ev)} in ${ev.city}. If your plans have changed, please let us know so we can plan accurately.\n\nSee you soon,`,
  },
]

const FOLLOWUPS: Template[] = [
  {
    title: 'Thank-you to attendees',
    subject: (name) => `Thank you for joining ${name}`,
    subtitle: 'Sent the day after the event',
    why: 'Keeps the momentum going and invites feedback.',
    kind: 'update',
    todo: ['Add recipient addresses', 'Link the recap or photos', 'Ask for feedback'],
    body: (name, ev) =>
      `Hi there,\n\nThank you for joining us at ${name} in ${ev.city}. It meant a lot to have you there. We'd love your feedback, and we'll share a recap with slides and highlights shortly.\n\nWarm regards,`,
  },
  {
    title: 'Thank-you to sponsors and vendors',
    subject: (name) => `Thank you for supporting ${name}`,
    subtitle: 'Personal note to the people who made it happen',
    why: 'Builds goodwill for next time.',
    kind: 'update',
    todo: ['Add recipient addresses', 'Add event results and numbers'],
    body: (name, ev) =>
      `Hi there,\n\nThank you for your support of ${name}. With about ${ev.headcount} people attending, it would not have happened without you. We'll send a short recap with the results soon.\n\nWith thanks,`,
  },
  {
    title: 'Slides and recap',
    subject: (name) => `Recap: ${name}`,
    subtitle: 'Share the deck and key moments',
    why: 'Gives attendees something to revisit and share.',
    kind: 'update',
    todo: ['Add recipient addresses', 'Attach or link the slides'],
    body: (name) =>
      `Hi there,\n\nHere is the recap from ${name}: the slides, the highlights, and what comes next. Feel free to forward it to anyone who could not make it.\n\nBest,`,
  },
]

function fromTemplates(templates: Template[], name: string, ev: EventDetails): TaskResult[] {
  return templates.map((t) => {
    const result: TaskResult = {
      title: t.title,
      subtitle: t.subtitle,
      meta: 'Template',
      why: t.why,
      todo: t.todo,
      draft: t.body(name, ev),
      extra: { emailKind: t.kind },
    }
    result.extra = { ...result.extra, subject: t.subject(name, ev) }
    return result
  })
}

export function registerOutreachRunners() {
  registerRunner('gmail', async (task: Task, event, ctx) => {
    if (task.id === 'judges') return runMock(task, event)
    const name = eventNameOf(ctx.store, event)
    if (task.id === 'followup') return fromTemplates(FOLLOWUPS, name, event)
    if (task.id === 'invites') return fromTemplates(INVITES, name, event)
    // Any other email step: one generic draft, built from the step itself.
    const base: TaskResult = { title: task.title, subtitle: task.description, meta: 'Template' }
    return [{ ...base, draft: defaultBody(name, event, 'custom', base) }]
  })
}
