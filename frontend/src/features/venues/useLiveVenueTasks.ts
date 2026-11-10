// Keep venue options in sync with their board cards while research agents fill them in (and after edits/re-prompts).
import { useMemo, useSyncExternalStore } from 'react'
import { EMPTY_EVENT_STATE, type EventStoreState } from '../../api/realtime'
import type { RunnerContext } from '../../lib/runners'
import type { Task } from '../../types'
import { refreshTask } from './model'

const NOOP_SUBSCRIBE = () => () => {}
const EMPTY = (): EventStoreState => EMPTY_EVENT_STATE

/** `tasks` with every venue result re-derived from the live cards/runs of `ctx.store`. Identity-stable when nothing changed. */
export function useLiveVenueTasks(tasks: Task[], ctx: RunnerContext | null): Task[] {
  const store = ctx?.store ?? null
  const state = useSyncExternalStore(store?.subscribe ?? NOOP_SUBSCRIBE, store?.getState ?? EMPTY)
  const { cards, runs } = state
  return useMemo(() => {
    if (!store) return tasks
    const next = tasks.map((t) => refreshTask(t, { cards, runs }))
    return next.every((t, i) => t === tasks[i]) ? tasks : next
  }, [tasks, cards, runs, store])
}
