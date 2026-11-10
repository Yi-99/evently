// Task-runner registry: how each workflow step gets its results.
//
// Feature modules register real runners (backed by the API) for a task id or an integration:
//
//   registerRunner('venue', async (task, event, ctx) => { … ctx.api.post(…); await ctx.store.waitForRun(id) … })
//   registerRunner('google-slides', …)
//
// runTask() looks the runner up by `task.id`, then by `task.integration`, and falls back to the sample-data
// mock in ./agents (results marked `meta: 'Sample data'`). Runners only run when there is a backend event
// (ctx non-null); errors they throw surface as a failed step. A runner can call `runMock()` itself to degrade.
import { api } from '../api/client'
import type { EventStore } from '../api/realtime'
import type { EventDetails, Task, TaskResult } from '../types'
import { runAgent } from './agents'

export interface RunnerContext {
  eventId: string
  store: EventStore
  api: typeof api
}

export type TaskRunner = (task: Task, event: EventDetails, ctx: RunnerContext) => Promise<TaskResult[]>

const runners = new Map<string, TaskRunner>()

/** Register a runner for a task id ('venue', 'catering', …) or an integration ('google-maps', 'gmail', …). */
export function registerRunner(key: string, runner: TaskRunner): void {
  if (runners.has(key)) console.warn(`[runners] replacing the runner for "${key}"`)
  runners.set(key, runner)
}

export function findRunner(task: Pick<Task, 'id' | 'integration'>): TaskRunner | undefined {
  return runners.get(task.id) ?? runners.get(task.integration)
}

/** True when a real (non-mock) runner exists for the task. */
export const hasRunner = (task: Pick<Task, 'id' | 'integration'>) => findRunner(task) !== undefined

export const SAMPLE_META = 'Sample data'

/** The built-in mock, with every result labelled as sample data. */
export async function runMock(task: Task, event: EventDetails): Promise<TaskResult[]> {
  const results = await runAgent(task, event)
  return results.map((r) => ({ ...r, meta: SAMPLE_META }))
}

/** Run one workflow step. Pass `ctx: null` while the plan has no backend event yet (mock only). */
export function runTask(task: Task, event: EventDetails, ctx: RunnerContext | null): Promise<TaskResult[]> {
  const runner = ctx ? findRunner(task) : undefined
  return runner && ctx ? runner(task, event, ctx) : runMock(task, event)
}
