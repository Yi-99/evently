import { api, isApiError } from '../api/client'
import type { EventDetails } from '../types'
import { QUESTIONS, type IntakeAnswers } from './intake'

interface ExtractBody {
  text: string
  eventType: string
  questions: { id: string; prompt: string }[]
}
type ExtractOut = { answers: Record<string, string | number> }

// The backend endpoint (POST /api/v1/intake/extract) when it exists; otherwise (404, backend down, no key)
// the Vite dev-server middleware at /api/extract, which takes the same request and returns the same shape.
// Any other backend error (422 refusal/validation, 502/503 model trouble) means "nothing extracted".
async function requestExtract(body: ExtractBody): Promise<ExtractOut | null> {
  try {
    return await api.post<ExtractOut>('/intake/extract', body)
  } catch (e) {
    if (!isApiError(e, 0) && !isApiError(e, 404)) return null
    const res = await fetch('/api/extract', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    return res.ok ? ((await res.json()) as ExtractOut) : null
  }
}

// Asks Claude which unanswered intake questions `text` already answers.
// `current` is the question on screen, so a short reply like "networking" lands on the right field.
// Returns {} when no endpoint works or none has an API key, so the caller falls back to plain answers.
export async function extractAnswers(
  text: string,
  eventType: string,
  answers: IntakeAnswers,
  current?: string,
): Promise<IntakeAnswers> {
  const open = QUESTIONS.filter((q) => q.id !== 'type' && !(q.id in answers))
  if (!open.length) return {}
  const prompt = (q: (typeof open)[number]) => (q.id === current ? `${q.prompt} (the question the user is answering now)` : q.prompt)
  try {
    const out = await requestExtract({ text, eventType, questions: open.map((q) => ({ id: q.id, prompt: prompt(q) })) })
    if (!out?.answers) return {}
    return Object.fromEntries(Object.entries(out.answers).map(([id, v]) => [id, String(v).trim()]))
  } catch {
    return {}
  }
}

// Adds `found` to `answers` without overwriting anything already answered.
// Answers a question's parser rejects are dropped. Returns the event fields they set too.
export function mergeFound(answers: IntakeAnswers, found: IntakeAnswers) {
  const merged = { ...answers }
  let patch: Partial<EventDetails> = {}
  const added: string[] = []
  for (const q of QUESTIONS) {
    const v = found[q.id]
    if (!v || q.id in merged) continue
    const p = q.parse ? q.parse(v) : {}
    if (!p) continue
    patch = { ...patch, ...p }
    merged[q.id] = v
    added.push(q.id)
  }
  return { answers: merged, patch, added }
}
