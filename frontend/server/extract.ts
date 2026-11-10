import Anthropic from '@anthropic-ai/sdk'
import type { Connect } from 'vite'

// POST /api/extract — reads a free-text event description and returns the intake answers it states.
// Runs inside the Vite dev server so the API key never reaches the browser.
// Credentials come from ANTHROPIC_API_KEY (or an `ant auth login` profile) in the dev server's environment.

interface ExtractRequest {
  text: string
  eventType: string
  questions: { id: string; prompt: string }[]
}

const SYSTEM = `You fill in an event-planning intake form from what the user wrote.
For each field, copy what the user actually said, in a short phrase. Use null for anything they did not state.
Never guess or invent values. "headcount" is a whole number of guests. "city" is just the city or venue name.`

let client: Anthropic | null = null

async function extract({ text, eventType, questions }: ExtractRequest): Promise<Record<string, string | number>> {
  client ??= new Anthropic()
  const properties: Record<string, object> = {}
  for (const q of questions) {
    properties[q.id] = { type: q.id === 'headcount' ? ['integer', 'null'] : ['string', 'null'], description: q.prompt }
  }
  const response = await client.beta.messages.create({
    model: 'claude-opus-5-5',
    max_tokens: 2048,
    betas: ['server-side-fallback-2026-07-01'],
    fallbacks: 'default',
    output_config: {
      effort: 'low',
      format: {
        type: 'json_schema',
        schema: { type: 'object', properties, required: Object.keys(properties), additionalProperties: false },
      },
    },
    system: SYSTEM,
    messages: [{ role: 'user', content: `Event type: ${eventType}\n\nUser wrote:\n${text}` }],
  })
  if (response.stop_reason === 'refusal' || response.stop_reason === 'max_tokens') return {}
  const json = response.content.flatMap((b) => (b.type === 'text' ? [b.text] : [])).join('')
  const parsed = JSON.parse(json) as Record<string, string | number | null>
  // Drop nulls and empty strings so the client only sees what the user stated.
  return Object.fromEntries(Object.entries(parsed).filter((e): e is [string, string | number] => e[1] !== null && e[1] !== ''))
}

const readBody = (req: Connect.IncomingMessage) =>
  new Promise<string>((resolve, reject) => {
    let data = ''
    req.on('data', (c) => {
      data += c
      if (data.length > 20_000) reject(new Error('body too large'))
    })
    req.on('end', () => resolve(data))
    req.on('error', reject)
  })

export const extractMiddleware: Connect.NextHandleFunction = async (req, res, next) => {
  if (req.url !== '/api/extract' || req.method !== 'POST') return next()
  res.setHeader('Content-Type', 'application/json')
  try {
    const body = JSON.parse(await readBody(req)) as ExtractRequest
    const valid =
      typeof body.text === 'string' &&
      Array.isArray(body.questions) &&
      body.questions.length <= 40 &&
      body.questions.every((q) => /^[a-z]+$/.test(q.id) && typeof q.prompt === 'string')
    if (!valid) {
      res.statusCode = 400
      return res.end(JSON.stringify({ error: 'bad request' }))
    }
    res.end(JSON.stringify({ answers: await extract(body) }))
  } catch (err) {
    console.error('[api/extract]', err instanceof Error ? err.message : err)
    res.statusCode = 502
    res.end(JSON.stringify({ error: 'extraction failed' }))
  }
}
