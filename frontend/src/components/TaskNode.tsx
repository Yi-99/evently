import { Handle, Position, type Node, type NodeProps } from '@xyflow/react'
import { AlertTriangle, Calendar, Check, ChevronRight, CircleAlert, Clock, Contact, Globe, Loader2, Lock, Mail, MapPin, Network, Presentation, QrCode, Ticket, Unlink } from 'lucide-react'
import { INTEGRATION_LABEL, gatedByGoogle } from '../data/workflows'
import { venueActivity } from '../features/venues/model'
import { NODE_H, NODE_W } from '../lib/layout'
import type { Integration, Task } from '../types'

// `approved` / `reviewed`: how many of this step's options the user has approved / approved or ignored.
export type TaskNodeType = Node<
  // `live`: a live-slides step whose deck is ready to present.
  // `awaiting`: checklist items waiting on an email reply. `needsYou`: items handed back to the user. `finished`: every approved option's checklist is done.
  { task: Task; selected: boolean; approved: number; reviewed: number; awaiting: number; needsYou: number; finished: boolean; googleConnected: boolean; planApproved: boolean; live?: boolean },
  'task'
>

const ICON: Record<Integration, typeof Globe> = {
  'google-maps': MapPin,
  gmail: Mail,
  'google-calendar': Calendar,
  'google-contacts': Contact,
  'google-slides': Presentation,
  'live-slides': QrCode,
  linkedin: Network,
  luma: Ticket,
  web: Globe,
}

// Done is green, in progress is an animated theme gradient, error is red.
const STATUS_STYLE = {
  idle: 'border-line bg-white',
  queued: 'border-line bg-white opacity-70',
  running: 'node-running border-indigo-400 ring-4 ring-indigo-300/50 shadow-lg shadow-indigo-500/20',
  approved: 'border-ok bg-white',
  awaiting: 'node-running border-indigo-400 ring-4 ring-indigo-300/50 shadow-lg shadow-indigo-500/20',
  attention: 'border-amber-300 bg-amber-50',
  done: 'border-emerald-300 bg-emerald-50',
  blocked: 'border-warn/50 border-dashed bg-white',
  error: 'border-rose-300 bg-rose-50',
} as const

export default function TaskNode({ data }: NodeProps<TaskNodeType>) {
  const { task, selected, approved, reviewed, awaiting, needsYou, finished, googleConnected, live } = data
  // Finished agents only count as done once the user approves an option, and the step is only
  // complete (green) once those options' checklists are done. An email still waiting on a
  // confirmation keeps it in progress, since sending alone settles nothing. Once nothing is
  // pending, a reply that declined or asked something hands the step back (amber) to the user.
  type Shown = Task['status'] | 'approved' | 'awaiting' | 'attention'
  const status: Shown =
    task.status !== 'done' ? task.status : !approved ? 'idle' : finished ? 'done' : awaiting ? 'awaiting' : needsYou ? 'attention' : 'approved'
  // Waiting steps that need an unlinked service show the disconnect icon instead of the lock.
  const unlinked = (status === 'blocked' || status === 'queued') && gatedByGoogle(task) && !googleConnected
  const blockedBy = unlinked ? 'google' : status === 'blocked' ? task.blockedBy : undefined
  // Stuck behind another step reads as waiting, not as an error.
  const look = unlinked ? 'blocked' : status === 'blocked' ? 'queued' : status
  const Icon = ICON[task.integration]
  // Options whose research agents are still filling them in (venue cards).
  const researching = task.results?.filter((r) => venueActivity(r) === 'busy').length ?? 0

  return (
    <div
      style={{ width: NODE_W, height: NODE_H }}
      className={`flex cursor-pointer flex-col justify-between rounded-2xl border px-4 py-3 transition-[box-shadow,background-color,border-color] duration-300 ${STATUS_STYLE[look]} ${
        selected ? 'shadow-lg outline-2 outline-offset-3 outline-accent' : 'shadow-sm hover:shadow-md'
      }`}
    >
      <Handle type="target" position={Position.Left} className="!size-1.5 !border-0 !bg-muted/40" />
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-[15px] font-semibold">{task.title}</div>
          <div className="flex items-center gap-1.5 text-xs text-muted">
            <span className="truncate">{task.agent} agent</span>
            {live && (
              <span className="inline-flex shrink-0 items-center gap-1 rounded-full bg-rose-50 px-1.5 text-[10px] font-semibold text-rose-600">
                <span className="size-1 rounded-full bg-rose-500" /> Live
              </span>
            )}
          </div>
        </div>
        <StatusBadge status={unlinked ? 'blocked' : status} blockedBy={blockedBy} awaiting={awaiting} needsYou={needsYou} />
      </div>
      <div className="flex items-center gap-1.5 font-mono text-[11px] whitespace-nowrap text-muted">
        <Icon size={12} />
        {INTEGRATION_LABEL[task.integration]}
        {task.results && (
          <span className="ml-auto flex items-center whitespace-nowrap text-accent">
            {reviewed ? `${reviewed} reviewed` : researching ? `${task.results.length} found · ${researching} researching` : `${task.results.length} found`}
            <ChevronRight size={12} className={`transition ${selected ? 'rotate-180' : ''}`} />
          </span>
        )}
      </div>
      <Handle type="source" position={Position.Right} className="!size-1.5 !border-0 !bg-muted/40" />
    </div>
  )
}

// Broken chain: needs a service that isn't linked. Lock: waiting on earlier steps to finish.
function StatusBadge({
  status,
  blockedBy,
  awaiting,
  needsYou,
}: {
  status: Task['status'] | 'approved' | 'awaiting' | 'attention'
  blockedBy: Task['blockedBy']
  awaiting: number
  needsYou: number
}) {
  if (status === 'running') return <Loader2 size={16} className="shrink-0 animate-spin text-run" />
  if (status === 'attention')
    return (
      <span
        title={`${needsYou} ${needsYou === 1 ? 'item needs' : 'items need'} you`}
        className="flex shrink-0 items-center gap-1 rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-700"
      >
        <CircleAlert size={11} /> {needsYou}
      </span>
    )
  if (status === 'awaiting')
    return (
      <span
        title={`Waiting on ${awaiting} ${awaiting === 1 ? 'reply' : 'replies'}`}
        className="flex shrink-0 items-center gap-1 rounded-full bg-white/80 px-1.5 py-0.5 text-[10px] font-semibold text-accent"
      >
        <Clock size={11} /> {awaiting}
      </span>
    )
  if (status === 'done' || status === 'approved')
    return (
      <span
        title={status === 'done' ? 'Complete' : 'Approved, still in progress'}
        className={`grid size-5 shrink-0 place-items-center rounded-full text-white ${status === 'done' ? 'bg-emerald-500' : 'bg-ok'}`}
      >
        <Check size={12} strokeWidth={3} />
      </span>
    )
  if (status === 'error')
    return (
      <span title="The agent run failed">
        <AlertTriangle size={15} className="shrink-0 text-rose-600" />
      </span>
    )
  if (status === 'blocked' && blockedBy === 'google')
    return (
      <span title="Needs a Google connection">
        <Unlink size={15} className="shrink-0 text-warn" />
      </span>
    )
  if (status === 'blocked' || status === 'queued')
    return (
      <span title="Waiting on earlier steps">
        <Lock size={15} className="shrink-0 text-muted" />
      </span>
    )
  return null
}
