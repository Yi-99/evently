import type { TaskResult } from '../types'

// Fields for adding an option by hand, mirrored from what the step's agent returns,
// so a hand-added venue asks for capacity and price while a judge asks for role and email.

export type FieldKey =
  | 'title'
  | 'address'
  | 'subtitle'
  | 'rating'
  | 'capacity'
  | 'price'
  | 'distance'
  | 'why'
  | 'contactName'
  | 'contactRole'
  | 'contactEmail'
  | 'contactPhone'

export interface Field {
  key: FieldKey
  label: string
  placeholder: string
  type?: 'number' | 'email' | 'textarea'
  required?: boolean
  // Half-width fields sit two to a row.
  half?: boolean
}

const SUBTITLE_LABEL: Record<string, string> = {
  sponsors: 'Industry',
  judges: 'Role',
  schedule: 'Time',
  prizes: 'Award',
  registration: 'Cost',
  av: 'Cost',
  brief: 'Tag',
  dayof: 'Tool',
}

const PRICE_LABEL: Record<string, string> = {
  venue: 'Price per day',
  catering: 'Price per person',
  sponsors: 'Tier & amount',
}

export function addFields(stepId: string, template: TaskResult | undefined): Field[] {
  const t = template ?? ({} as TaskResult)
  const fields: Field[] = [{ key: 'title', label: 'Name', placeholder: t.title ? `e.g. ${t.title}` : 'Name', required: true }]
  if (t.address) fields.push({ key: 'address', label: 'Area or address', placeholder: t.address })
  else fields.push({ key: 'subtitle', label: SUBTITLE_LABEL[stepId] ?? 'Summary', placeholder: t.subtitle || 'Short summary' })
  if (t.rating !== undefined) fields.push({ key: 'rating', label: 'Rating (0–5)', placeholder: String(t.rating), type: 'number', half: true })
  if (t.capacity) fields.push({ key: 'capacity', label: 'Capacity (people)', placeholder: t.capacity.replace(/\D/g, ''), type: 'number', half: true })
  if (t.price) fields.push({ key: 'price', label: PRICE_LABEL[stepId] ?? 'Cost', placeholder: t.price, half: true })
  if (t.distance) fields.push({ key: 'distance', label: 'Distance', placeholder: t.distance, half: true })
  fields.push({ key: 'why', label: 'Why it fits', placeholder: t.why || 'What makes this a good option', type: 'textarea' })
  if (t.contact) {
    fields.push({ key: 'contactName', label: 'Contact name', placeholder: t.contact.name, half: true })
    if (t.contact.role) fields.push({ key: 'contactRole', label: 'Contact role', placeholder: t.contact.role, half: true })
    if (t.contact.email) fields.push({ key: 'contactEmail', label: 'Email', placeholder: t.contact.email, type: 'email', half: true })
    if (t.contact.phone) fields.push({ key: 'contactPhone', label: 'Phone', placeholder: t.contact.phone, half: true })
  }
  return fields
}

// Height of the expanded add form for these fields (half-width fields pair up).
export function addFormHeight(fields: Field[]): number {
  let rows = 0
  let pending = false
  for (const f of fields) {
    if (f.half) {
      if (pending) pending = false
      else {
        rows++
        pending = true
      }
    } else {
      rows += f.type === 'textarea' ? 1.6 : 1
      pending = false
    }
  }
  return Math.round(120 + rows * 62)
}

// Turn the form into an option shaped like the agent's, reusing its checklist and draft.
export function buildOption(values: Partial<Record<FieldKey, string>>, template: TaskResult | undefined): TaskResult {
  const v = (k: FieldKey) => values[k]?.trim() || undefined
  const title = v('title')!
  const rating = v('rating') ? Math.min(5, Math.max(0, Number(v('rating')))) : undefined
  const contact = v('contactName') || v('contactEmail') || v('contactPhone')
    ? { name: v('contactName') ?? 'Contact', role: v('contactRole'), email: v('contactEmail'), phone: v('contactPhone') }
    : undefined
  // Swap the template's own name (and contact's first name) for the new option's in the draft.
  let draft = template?.draft
  if (draft && template) {
    draft = draft.split(template.title).join(title)
    const oldFirst = template.contact?.name.split(' ')[0]
    const newFirst = contact?.name.split(' ')[0]
    if (oldFirst && newFirst && template.contact?.name !== 'Events team') draft = draft.split(`Hi ${oldFirst}`).join(`Hi ${newFirst}`)
  }
  return {
    title,
    subtitle: v('address') ?? v('subtitle') ?? 'Added by you',
    address: v('address'),
    meta: 'Added by you',
    rating: rating !== undefined && !Number.isNaN(rating) ? rating : undefined,
    capacity: v('capacity') ? `Fits ${v('capacity')}` : undefined,
    price: v('price'),
    distance: v('distance'),
    // Places get a pin so the Maps link shows.
    pin: template?.pin && v('address') ? { x: 50, y: 50 } : undefined,
    why: v('why'),
    contact,
    todo: template?.todo ? template.todo.map((t) => t.split(template.title).join(title)) : ['Confirm the details', 'Reach out and lock it in'],
    draft,
  }
}
