import type { EventDetails, Task } from '../types'

export type ChatAction =
  | { kind: 'add'; title: string; after?: string }
  | { kind: 'remove'; id: string }
  | { kind: 'details'; patch: Partial<EventDetails> }
  | { kind: 'run' }
  | { kind: 'none' }

const findTask = (tasks: Task[], q: string) => {
  const s = q.trim().toLowerCase()
  return tasks.find((t) => t.title.toLowerCase() === s || t.id === s) ??
    tasks.find((t) => t.title.toLowerCase().includes(s) || s.includes(t.title.toLowerCase()))
}

// Rule-based stand-in for the LLM planner. Replace with a call to the
// backend (e.g. POST /api/chat) that returns the same ChatAction shape.
export function interpret(input: string, tasks: Task[]): { reply: string; actions: ChatAction[] } {
  const text = input.trim()
  const lower = text.toLowerCase()
  const actions: ChatAction[] = []
  const notes: string[] = []

  if (/^(approve|run|start|go|confirm|looks good|launch)\b/.test(lower)) {
    return { reply: 'Plan approved. Spinning up agents — watch the flowchart as each step runs.', actions: [{ kind: 'run' }] }
  }

  const count = lower.match(/(\d[\d,]*)\s*(people|attendees|guests|hackers|pax|ppl)/)
  if (count) {
    const headcount = parseInt(count[1].replace(/,/g, ''), 10)
    actions.push({ kind: 'details', patch: { headcount } })
    notes.push(`headcount set to ${headcount}`)
  }

  const city = text.match(/\b(?:in|at|near)\s+([A-Z][\w.]*(?:\s+[A-Z][\w.]*)*)/)
  if (city) {
    actions.push({ kind: 'details', patch: { city: city[1] } })
    notes.push(`location set to ${city[1]}`)
  }

  const remove = lower.match(/^(?:remove|delete|drop|skip)\s+(?:the\s+)?(.+)$/)
  if (remove) {
    const task = findTask(tasks, remove[1])
    if (task) {
      actions.push({ kind: 'remove', id: task.id })
      notes.push(`removed “${task.title}”`)
    } else {
      notes.push(`couldn't find a step called “${remove[1]}”`)
    }
  }

  const add = text.match(/^(?:add|include|also)\s+(?:a\s+|an\s+)?(.+?)(?:\s+after\s+(.+))?$/i)
  if (add) {
    const after = add[2] ? findTask(tasks, add[2])?.id : undefined
    const title = add[1].replace(/\b\w/g, (c) => c.toUpperCase())
    actions.push({ kind: 'add', title, after })
    notes.push(`added “${title}”${after ? ` after ${tasks.find((t) => t.id === after)?.title}` : ''}`)
  }

  if (!actions.length && !notes.length) {
    return {
      reply: 'Try “add photo booth after venue”, “remove judges”, “300 people in Austin”, or “approve plan” to start the agents.',
      actions: [{ kind: 'none' }],
    }
  }
  const sentence = notes.join(', ')
  return { reply: `Done — ${sentence}. Anything else? Say “approve plan” when it looks right.`, actions }
}
