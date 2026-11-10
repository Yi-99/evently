// Result-action registry: feature modules add buttons/panels to an option's expanded card
// without editing the canvas components.
//
//   registerResultAction({
//     id: 'open-deck',
//     applies: (task, result) => !!result.presentationId,
//     Component: ({ task, result, ctx }) => <button …>Open deck</button>,
//   })
//
// ResultNode renders every applicable action (in registration order) in the expanded card's footer.
// A Component may render its own modal/portal (deck viewer, email composer, …).
import type { ComponentType } from 'react'
import type { RunnerContext } from './runners'
import type { EventDetails, Task, TaskResult } from '../types'

export interface ResultActionProps {
  task: Task
  result: TaskResult
  event: EventDetails
  /** null while the plan has no backend event (sample data only). */
  ctx: RunnerContext | null
  /** Replace this result in the task's list (e.g. after a re-prompt or a status change). */
  onUpdate: (next: TaskResult) => void
}

export interface ResultAction {
  id: string
  applies: (task: Task, result: TaskResult, ctx: RunnerContext | null) => boolean
  Component: ComponentType<ResultActionProps>
}

const actions: ResultAction[] = []

export function registerResultAction(action: ResultAction): void {
  const at = actions.findIndex((a) => a.id === action.id)
  if (at >= 0) actions[at] = action
  else actions.push(action)
}

export const resultActionsFor = (task: Task, result: TaskResult, ctx: RunnerContext | null): ResultAction[] =>
  actions.filter((a) => a.applies(task, result, ctx))
