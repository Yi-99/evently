// The `venue` step runner: POST a venue search, then resolve as soon as the scout has created the venue cards.
// Research keeps filling those cards afterwards; `refreshTask` (see useLiveVenueTasks) keeps the results current.
import { isApiError } from '../../api/client'
import { isRunFinished, type EventStoreState } from '../../api/realtime'
import type { AgentRunOut, RunAccepted } from '../../api/types'
import type { RunnerContext } from '../../lib/runners'
import type { EventDetails, Task, TaskResult } from '../../types'
import { buildCriteria, dateTextOf } from './criteria'
import { cleanError, searchCardIds, searchResults } from './model'

// Cards are created back to back once the scout is done; wait this long without a new one before showing them.
const SETTLE_MS = 1200
const TIMEOUT_MS = 10 * 60_000
const POLL_MS = 4000

/** Merge runs/cards fetched over REST into the store (used while the socket is down). */
function mergeRuns(ctx: RunnerContext, runs: AgentRunOut[]) {
  ctx.store.patch((s) => {
    const next = { ...s.runs }
    for (const r of runs) {
      const known = next[r.id]
      if (!known || !isRunFinished(known)) next[r.id] = r
    }
    return { ...s, runs: next }
  })
}

const errorText = (e: unknown) =>
  isApiError(e) ? e.message : e instanceof Error ? e.message : 'The venue search could not start.'

export async function runVenueSearch(task: Task, event: EventDetails, ctx: RunnerContext): Promise<TaskResult[]> {
  const requirements = ctx.store.getState().event?.requirements
  const criteria = buildCriteria(task, event, requirements)
  const dateText = dateTextOf(event, requirements)

  let run: AgentRunOut
  try {
    ;({ run } = await ctx.api.post<RunAccepted>(`/events/${encodeURIComponent(ctx.eventId)}/venue-searches`, criteria))
  } catch (e) {
    // 503 = Claude isn't configured on the server; the detail says how to fix it. Surface it as the step's error.
    throw new Error(errorText(e))
  }
  mergeRuns(ctx, [run])
  const searchId = run.id
  const release = ctx.store.retain()

  try {
    const cards = await new Promise<TaskResult[]>((resolve, reject) => {
      let settle: ReturnType<typeof setTimeout> | null = null
      let lastCount = 0
      const finish = (fn: () => void) => {
        off()
        clearInterval(poll)
        clearTimeout(timeout)
        if (settle) clearTimeout(settle)
        fn()
      }
      const results = (s: EventStoreState) =>
        searchResults(s, searchId, dateText).map((r) => ({ ...r, extra: { ...r.extra, dateText } }))

      const check = () => {
        const s = ctx.store.getState()
        const coordinator = s.runs[searchId]
        const count = searchCardIds(s, searchId).length
        if (coordinator && isRunFinished(coordinator)) {
          if (count) return finish(() => resolve(results(ctx.store.getState())))
          return finish(() => reject(new Error(cleanError(coordinator.error) || `The venue search ${coordinator.status}.`)))
        }
        if (count && count !== lastCount) {
          lastCount = count
          if (settle) clearTimeout(settle)
          settle = setTimeout(() => finish(() => resolve(results(ctx.store.getState()))), SETTLE_MS)
        }
      }

      const off = ctx.store.subscribe(check)
      // Socket down: fall back to REST for this event's runs and the board.
      const poll = setInterval(() => {
        if (ctx.store.channel.status === 'open') return
        void ctx.api
          .get<AgentRunOut[]>(`/events/${encodeURIComponent(ctx.eventId)}/agent-runs`, { query: { limit: 50 } })
          .then((runs) => mergeRuns(ctx, runs))
          .then(() => ctx.store.reload())
          .catch(() => {})
      }, POLL_MS)
      const timeout = setTimeout(() => finish(() => reject(new Error('The venue search is taking too long. Try again.'))), TIMEOUT_MS)
      check()
    })
    return cards
  } finally {
    release()
  }
}
