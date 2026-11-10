import { Handle, Position, type Node, type NodeProps } from '@xyflow/react'
import { AlertTriangle, Check, ChevronUp, Clock, ExternalLink, Link2, Loader2, Mail, Phone, Plus, RotateCw, Send, Star, Upload, Users, X } from 'lucide-react'
import QRCode from 'qrcode'
import { useEffect, useState, type FormEvent, type MouseEvent, type ReactNode } from 'react'
import { isAwaiting, isDone, type TodoProgress } from '../lib/execute'
import type { Field, FieldKey } from '../lib/options'
import { resultActionsFor } from '../lib/resultActions'
import type { RunnerContext } from '../lib/runners'
import type { EventDetails, Task, TaskResult } from '../types'
import { venueActivity, venueExtra } from '../features/venues/model'
import VenueDetails, { VenueProgressBar, VenueProgressLine } from '../features/venues/VenueDetails'

export type Decision = 'approved' | 'ignored'

// One live-slides option's deck. `slides` is null until there's a draft or an upload; `uploaded`: the user's own file.
export interface LiveInfo {
  slides: number | null
  uploaded: boolean
  presentUrl: string
  audienceUrl: string
  uploadUrl: string
}

export type ResultItem =
  // `implied`: approved only because the whole plan was approved. `planApproved`: agents act on approved options.
  // `live`: set on options of a live-slides step, each of which is its own deck with its own QR code.
  | { kind: 'result'; n: number; result: TaskResult; decision?: Decision; implied?: boolean; planApproved?: boolean; live?: LiveInfo }
  | { kind: 'loading'; agent: string }
  | { kind: 'locked'; integration: string }
  | { kind: 'error'; message: string }
  | { kind: 'add'; fields: Field[] }

export interface ResultNodeData extends Record<string, unknown> {
  item: ResultItem
  // The step this option belongs to, the event, and the backend context (null for sample-only plans).
  task: Task
  event: EventDetails
  ctx: RunnerContext | null
  size: { w: number; h: number }
  expanded: boolean
  city: string
  // How far this option's checklist has got, per item (missing = not started).
  progress: TodoProgress[]
  onOpen: () => void
  onDecide: (d: Decision) => void
  onToggleTodo: (i: number) => void
  // Has the agent email the thread again after a question or a no.
  onFollowUp: (i: number) => void
  onConnectGoogle: () => void
  onRetry: () => void
  onAdd: (values: Partial<Record<FieldKey, string>>) => void
  // Replace this option in its step (matched by cardId, else title).
  onUpdateResult: (next: TaskResult) => void
}

export type ResultNodeType = Node<ResultNodeData, 'result'>

// Keep clicks on controls from toggling the node.
const stop = (e: MouseEvent) => e.stopPropagation()

// Whole-card colour for a decision.
const TONE = {
  approved: 'bg-emerald-50 ring-emerald-300',
  ignored: 'bg-rose-50 ring-rose-300',
  none: 'bg-white ring-accent/20',
} as const

const mapsUrl = (r: TaskResult, city: string) =>
  venueExtra(r)?.mapsUrl ?? `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(`${r.title} ${r.address ?? city}`)}`

export default function ResultNode({ data }: NodeProps<ResultNodeType>) {
  const { item, size, expanded, onConnectGoogle } = data
  // An approved option whose agent is still working or waiting on a reply shares the in-progress gradient.
  const inProgress =
    item.kind === 'result' && item.decision === 'approved' && item.planApproved && data.progress.some((p) => p?.status === 'working' || isAwaiting(p))
  const tone =
    item.kind === 'add' && !expanded
      ? 'bg-white/60 ring-transparent'
      : inProgress && !expanded
        ? 'node-running ring-indigo-300'
        : TONE[(item.kind === 'result' && item.decision) || 'none']
  const clickable = item.kind === 'result' || (item.kind === 'add' && !expanded)

  return (
    <div
      style={{ width: size.w, height: size.h }}
      onClick={clickable ? data.onOpen : stop}
      className={`nodrag nopan relative flex flex-col overflow-hidden rounded-2xl ring-1 transition-[background-color,box-shadow] duration-300 ${tone} ${
        expanded ? 'shadow-xl outline-2 outline-offset-3 outline-accent' : 'shadow-md hover:shadow-lg'
      } ${item.kind === 'add' && !expanded ? 'border-2 border-dashed border-accent/40 hover:border-accent' : ''} ${clickable ? 'cursor-pointer' : 'cursor-default'}`}
    >
      <Handle type="target" position={Position.Left} className="!size-1.5 !border-0 !bg-accent/50" />

      {item.kind === 'loading' && (
        <div className="flex h-full items-center gap-2 px-4 text-sm text-accent">
          <Loader2 size={15} className="animate-spin" /> {item.agent} is searching…
        </div>
      )}

      {item.kind === 'add' &&
        (expanded ? (
          <AddForm fields={item.fields} onAdd={data.onAdd} onCancel={data.onOpen} />
        ) : (
          <div className="flex h-full items-center justify-center gap-2 text-sm font-semibold text-accent">
            <span className="grid size-6 place-items-center rounded-full bg-accent-soft">
              <Plus size={14} strokeWidth={2.5} />
            </span>
            Add option
          </div>
        ))}

      {item.kind === 'error' && (
        <div className="flex h-full items-center gap-3 bg-rose-50 px-4">
          <AlertTriangle size={16} className="shrink-0 text-rose-600" />
          <div className="min-w-0 flex-1 text-xs">
            <div className="font-semibold text-rose-700">Agent run failed</div>
            <div className="truncate text-rose-700/70">{item.message}</div>
          </div>
          <button
            onClick={(e) => {
              stop(e)
              data.onRetry()
            }}
            className="flex shrink-0 items-center gap-1 rounded-full bg-rose-600 px-3 py-1.5 text-xs font-semibold text-white"
          >
            <RotateCw size={12} /> Retry
          </button>
        </div>
      )}

      {item.kind === 'locked' && (
        <div className="flex h-full items-center gap-3 px-4">
          <div className="min-w-0 flex-1 text-xs">
            <div className="font-semibold">Needs {item.integration}</div>
            <div className="text-muted">Connect Google to fetch results</div>
          </div>
          <button
            onClick={(e) => {
              stop(e)
              onConnectGoogle()
            }}
            className="flex shrink-0 items-center gap-1 rounded-full bg-gray-900 px-3 py-1.5 text-xs font-semibold text-white"
          >
            <Link2 size={12} /> Connect
          </button>
        </div>
      )}

      {item.kind === 'result' && (
        <>
          <Summary item={item} expanded={expanded} progress={data.progress} />
          {expanded && <Details data={data} item={item} />}
          {!expanded && <VenueProgressBar result={item.result} />}
        </>
      )}
    </div>
  )
}

function Summary({ item, expanded, progress }: { item: Extract<ResultItem, { kind: 'result' }>; expanded: boolean; progress: TodoProgress[] }) {
  const { n, result: r, decision, implied, planApproved } = item
  const todo = r.todo ?? []
  const done = todo.filter((_, i) => isDone(progress[i])).length
  // Once agents act on this option, its pill tracks the checklist instead of the decision.
  const acting = decision === 'approved' && planApproved && todo.length > 0
  const working = acting && progress.some((p) => p?.status === 'working')
  const waiting = acting && !working ? progress.filter(isAwaiting).length : 0
  const needsYou = acting && !working && !waiting && progress.some((p) => p?.status === 'needs-you')
  const badge = decision === 'approved' ? 'bg-emerald-500 text-white' : decision === 'ignored' ? 'bg-rose-500 text-white' : 'bg-accent text-white'
  return (
    <div className={`flex shrink-0 items-center gap-3 px-3.5 ${expanded ? 'border-b border-gray-900/5 py-3' : 'h-full'}`}>
      <span className={`grid size-6 shrink-0 place-items-center rounded-full text-[11px] font-bold ${badge}`}>
        {decision === 'approved' ? <Check size={12} strokeWidth={3} /> : decision === 'ignored' ? <X size={12} strokeWidth={3} /> : n}
      </span>
      <div className="min-w-0 flex-1">
        <div className={`truncate font-semibold ${expanded ? 'text-base' : 'text-[13px]'}`}>{r.title}</div>
        <div className="mt-0.5 flex items-center gap-2 truncate text-[11px] text-gray-600">
          {r.rating !== undefined && (
            <span className="flex items-center gap-0.5 font-medium text-ink">
              <Star size={10} className="fill-amber-400 text-amber-400" />
              {r.rating.toFixed(1)}
            </span>
          )}
          {r.capacity && (
            <span className="flex items-center gap-0.5">
              <Users size={10} /> {r.capacity.replace('Fits ', '')}
            </span>
          )}
          <span className="truncate">{r.price ?? r.subtitle}</span>
        </div>
        {!expanded && venueActivity(r) ? (
          <VenueProgressLine result={r} />
        ) : (
          <div className="truncate text-[11px] text-muted">{r.address ?? r.contact?.name ?? r.meta}</div>
        )}
      </div>
      {item.live && !expanded && <QrImage url={item.live.audienceUrl} className="size-14 shrink-0 rounded-lg ring-1 ring-gray-900/10" />}
      {expanded ? (
        <ChevronUp size={16} className="shrink-0 text-muted" />
      ) : (
        <span
          className={`flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold ring-1 ${
            working
              ? 'bg-accent-soft text-accent ring-accent/30'
              : waiting
                ? 'bg-white/80 text-accent ring-accent/30'
                : needsYou
                  ? 'bg-amber-50 text-amber-700 ring-amber-300'
                  : decision === 'approved'
              ? 'bg-emerald-100 text-emerald-700 ring-emerald-300'
              : decision === 'ignored'
                ? 'bg-rose-100 text-rose-700 ring-rose-300'
                : 'bg-white text-muted ring-gray-900/10'
          }`}
        >
          {working && <Loader2 size={10} className="animate-spin" />}
          {waiting > 0 && <Clock size={10} />}
          {acting
            ? working
              ? `Working ${done}/${todo.length}`
              : waiting
                ? `Awaiting ${waiting === 1 ? 'reply' : `${waiting} replies`} · ${done}/${todo.length}`
                : needsYou
                  ? `Needs you · ${done}/${todo.length}`
                  : done === todo.length
                  ? `Done ${done}/${todo.length}`
                  : `Approved · ${done}/${todo.length}`
            : decision === 'approved'
              ? implied
                ? 'Auto-approved'
                : 'Approved'
              : decision === 'ignored'
                ? planApproved
                  ? 'Ignored · won’t run'
                  : 'Ignored'
                : 'Pending'}
        </span>
      )}
    </div>
  )
}

function Details({ data, item }: { data: ResultNodeData; item: Extract<ResultItem, { kind: 'result' }> }) {
  const { city, progress, onDecide, onToggleTodo, onFollowUp } = data
  const r = item.result
  const todo = r.todo ?? []
  const done = todo.filter((_, i) => isDone(progress[i])).length

  return (
    <div onClick={stop} className="nowheel flex min-h-0 flex-1 cursor-default flex-col gap-4 overflow-y-auto p-4 text-sm">
      {item.live && <LivePanel live={item.live} />}

      {r.why && (
        <div>
          <Label>Why it fits</Label>
          <p className="text-[13px] leading-relaxed text-gray-700">{r.why}</p>
        </div>
      )}

      {venueExtra(r) && <VenueDetails result={r} ctx={data.ctx} />}

      {todo.length > 0 && (
        <div>
          <Label>
            What we need to do
            <span className="ml-auto font-mono text-[10px] text-muted normal-case">
              {done}/{todo.length}
            </span>
          </Label>
          {!item.planApproved && item.decision !== 'ignored' && (
            <p className="mb-2 text-[11px] text-muted">The agent starts on these once the plan is approved.</p>
          )}
          <ul className="space-y-2">
            {todo.map((t, i) => (
              <TodoRow key={t} text={t} progress={progress[i]} onToggle={() => onToggleTodo(i)} onFollowUp={() => onFollowUp(i)} />
            ))}
          </ul>
        </div>
      )}

      {r.contact && (
        <div>
          <Label>Contact</Label>
          <div className="rounded-xl bg-white/80 p-3 text-xs ring-1 ring-gray-900/5">
            <div className="font-semibold">
              {r.contact.name}
              {r.contact.role && <span className="font-normal text-muted"> · {r.contact.role}</span>}
            </div>
            {r.contact.email && (
              <div className="mt-1 flex items-center gap-1.5 text-gray-600">
                <Mail size={11} /> {r.contact.email}
              </div>
            )}
            {r.contact.phone && (
              <div className="mt-0.5 flex items-center gap-1.5 text-gray-600">
                <Phone size={11} /> {r.contact.phone}
              </div>
            )}
          </div>
        </div>
      )}

      {r.draft && (
        <div>
          <Label>Draft outreach</Label>
          <p className="rounded-xl bg-white/80 p-3 text-xs leading-relaxed text-gray-700 ring-1 ring-gray-900/5">{r.draft}</p>
        </div>
      )}

      <div className="mt-auto flex flex-col gap-2 pt-1">
        <ResultActions data={data} result={r} />
        <div className="flex items-center gap-2">
        <button
          onClick={() => onDecide('approved')}
          className={`flex flex-1 items-center justify-center gap-1.5 rounded-full py-2 text-xs font-semibold transition ${
            item.decision === 'approved'
              ? 'bg-emerald-600 text-white shadow-sm shadow-emerald-500/30'
              : 'bg-white text-emerald-700 ring-1 ring-emerald-300 hover:bg-emerald-50'
          }`}
        >
          <Check size={12} strokeWidth={3} /> {item.decision === 'approved' ? 'Approved' : 'Approve'}
        </button>
        <button
          onClick={() => onDecide('ignored')}
          className={`flex flex-1 items-center justify-center gap-1.5 rounded-full py-2 text-xs font-semibold transition ${
            item.decision === 'ignored'
              ? 'bg-rose-600 text-white shadow-sm shadow-rose-500/30'
              : 'bg-white text-rose-700 ring-1 ring-rose-300 hover:bg-rose-50'
          }`}
        >
          <X size={12} strokeWidth={3} /> {item.decision === 'ignored' ? 'Ignored' : 'Ignore'}
        </button>
        {r.pin && (
          <a
            href={mapsUrl(r, city)}
            target="_blank"
            rel="noreferrer"
            className="flex items-center gap-1 rounded-full bg-white px-3.5 py-2 text-xs font-semibold text-accent ring-1 ring-gray-900/10 hover:ring-accent"
          >
            Maps <ExternalLink size={11} />
          </a>
        )}
        </div>
      </div>
    </div>
  )
}

// Buttons/panels feature modules registered for this option (see lib/resultActions).
function ResultActions({ data, result }: { data: ResultNodeData; result: TaskResult }) {
  const { task, event, ctx, onUpdateResult } = data
  const actions = resultActionsFor(task, result, ctx)
  if (!actions.length) return null
  return (
    <div className="flex flex-wrap items-center gap-2">
      {actions.map((a) => (
        <a.Component key={a.id} task={task} result={result} event={event} ctx={ctx} onUpdate={onUpdateResult} />
      ))}
    </div>
  )
}

function AddForm({
  fields,
  onAdd,
  onCancel,
}: {
  fields: Field[]
  onAdd: (values: Partial<Record<FieldKey, string>>) => void
  onCancel: () => void
}) {
  const [values, setValues] = useState<Partial<Record<FieldKey, string>>>({})
  const set = (k: FieldKey, v: string) => setValues((s) => ({ ...s, [k]: v }))
  const ready = !!values.title?.trim()
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (ready) onAdd(values)
  }
  const input =
    'mt-1 w-full rounded-xl bg-white px-3 py-1.5 text-sm font-normal tracking-normal text-ink normal-case ring-1 ring-gray-900/10 outline-none placeholder:text-gray-400 focus:ring-2 focus:ring-accent'

  return (
    <form onSubmit={submit} onClick={stop} className="nowheel flex h-full cursor-default flex-col gap-3 overflow-y-auto p-4 text-sm">
      <div className="flex items-center gap-2">
        <span className="grid size-6 place-items-center rounded-full bg-accent-soft text-accent">
          <Plus size={14} strokeWidth={2.5} />
        </span>
        <span className="flex-1 text-base font-semibold">Add an option</span>
        <button type="button" onClick={onCancel} aria-label="Cancel" className="rounded-full p-1 text-muted hover:bg-gray-100">
          <X size={16} />
        </button>
      </div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-2.5">
        {fields.map((f, i) => (
          <label key={f.key} className={`text-[10px] font-semibold tracking-wide text-muted uppercase ${f.half ? '' : 'col-span-2'}`}>
            {f.label}
            {f.required && <span className="text-rose-500"> *</span>}
            {f.type === 'textarea' ? (
              <textarea rows={2} value={values[f.key] ?? ''} onChange={(e) => set(f.key, e.target.value)} placeholder={f.placeholder} className={`${input} resize-none`} />
            ) : (
              <input
                autoFocus={i === 0}
                type={f.type ?? 'text'}
                step={f.key === 'rating' ? '0.1' : undefined}
                min={f.type === 'number' ? 0 : undefined}
                max={f.key === 'rating' ? 5 : undefined}
                value={values[f.key] ?? ''}
                onChange={(e) => set(f.key, e.target.value)}
                placeholder={f.placeholder}
                className={input}
              />
            )}
          </label>
        ))}
      </div>
      <button
        type="submit"
        disabled={!ready}
        className="mt-auto flex shrink-0 items-center justify-center gap-1.5 rounded-full bg-accent py-2 text-xs font-semibold text-white shadow-sm shadow-indigo-500/30 transition hover:bg-indigo-500 disabled:opacity-40"
      >
        <Plus size={12} strokeWidth={3} /> Add to list
      </button>
    </form>
  )
}

function Label({ children }: { children: ReactNode }) {
  return <div className="mb-1.5 flex items-center text-[10px] font-semibold tracking-wide text-muted uppercase">{children}</div>
}

function QrImage({ url, className }: { url: string; className: string }) {
  const [src, setSrc] = useState<string | null>(null)
  useEffect(() => {
    QRCode.toDataURL(url, { margin: 1, width: 240, color: { dark: '#1e1b4b', light: '#ffffff' } }).then(setSrc, () => setSrc(null))
  }, [url])
  return src ? <img src={src} alt={`QR code for ${url}`} className={`bg-white ${className}`} /> : <div className={`bg-white ${className}`} />
}

// This option's own deck: its audience QR code, and buttons that open the presenter or upload in a new tab.
function LivePanel({ live }: { live: LiveInfo }) {
  const ready = live.slides !== null
  const link = 'flex items-center gap-1 rounded-full px-3 py-1.5 text-xs font-semibold whitespace-nowrap transition'
  return (
    <div>
      <Label>
        Present live
        {ready && (
          <span className="ml-2 inline-flex items-center gap-1 rounded-full bg-rose-50 px-1.5 text-[10px] font-semibold tracking-normal text-rose-600 normal-case">
            <span className="size-1 rounded-full bg-rose-500" /> Live
          </span>
        )}
      </Label>
      <div className="flex gap-3 rounded-xl bg-white/80 p-3 ring-1 ring-gray-900/5">
        <QrImage url={live.audienceUrl} className="size-28 shrink-0 rounded-lg ring-1 ring-gray-900/10" />
        <div className="flex min-w-0 flex-1 flex-col text-xs">
          <div className="font-semibold">
            {ready ? `${live.uploaded ? 'Your slides' : 'Draft deck'} · ${live.slides} ${live.slides === 1 ? 'slide' : 'slides'}` : 'No slides yet'}
          </div>
          <p className="mt-0.5 leading-snug text-muted">
            {ready ? 'The audience scans this code to follow along.' : 'Upload a PDF or images. Anyone who scans early sees the slides once they’re added.'}
          </p>
          <div className="mt-auto flex gap-1.5 pt-2">
            {ready && (
              <a href={live.presentUrl} target="_blank" rel="noreferrer" className={`${link} bg-accent text-white hover:bg-indigo-500`}>
                Present <ExternalLink size={11} />
              </a>
            )}
            <a href={live.uploadUrl} target="_blank" rel="noreferrer" className={`${link} bg-white text-accent ring-1 ring-gray-900/10 hover:ring-accent`}>
              <Upload size={11} /> {live.uploaded ? 'Replace' : 'Upload'}
            </a>
          </div>
        </div>
      </div>
    </div>
  )
}

// One checklist item. The agent ticks items as it finishes them; the user can tick any by hand.
// Items sent by email stay open (in progress) until the thread gets a confirming reply.
function TodoRow({ text, progress, onToggle, onFollowUp }: { text: string; progress?: TodoProgress; onToggle: () => void; onFollowUp: () => void }) {
  const status = progress?.status ?? 'pending'
  const done = status === 'done'
  const thread = progress?.thread
  const box =
    status === 'working'
      ? 'bg-accent-soft text-accent ring-accent/40'
      : status === 'awaiting-reply'
        ? 'bg-accent-soft text-accent ring-accent/40'
        : done
          ? progress?.by === 'agent'
            ? 'bg-emerald-500 text-white ring-emerald-500'
            : 'bg-accent text-white ring-accent'
          : status === 'needs-you'
            ? 'bg-amber-50 ring-amber-400'
            : 'bg-white ring-gray-300'
  const label = done ? 'done' : status === 'working' ? 'agent working' : status === 'awaiting-reply' ? 'awaiting reply' : status === 'needs-you' ? 'needs you' : 'not done'
  return (
    <li>
      <button
        onClick={onToggle}
        disabled={status === 'working'}
        aria-label={`${text}: ${label}`}
        className="flex w-full items-start gap-2.5 text-left text-[13px] disabled:cursor-progress"
      >
        <span className={`mt-0.5 grid size-4 shrink-0 place-items-center rounded-md ring-1 transition ${box}`}>
          {status === 'working' ? (
            <Loader2 size={10} className="animate-spin" />
          ) : status === 'awaiting-reply' ? (
            <Clock size={10} />
          ) : (
            done && <Check size={10} strokeWidth={3} />
          )}
        </span>
        <span className="min-w-0">
          <span className={done ? 'text-muted line-through' : ''}>{text}</span>
          {status === 'working' && <span className="block text-[11px] text-accent">Agent working on this…</span>}
          {status === 'awaiting-reply' && (
            <span className="block text-[11px] text-accent">
              Awaiting reply · sent {ago(thread?.sentAt)}. {progress?.note}
            </span>
          )}
          {done && progress?.by === 'agent' && progress.note && <span className="block text-[11px] text-emerald-700">{progress.note}</span>}
          {done && progress?.by === 'you' && <span className="block text-[11px] text-muted">Ticked by you</span>}
          {status === 'needs-you' && <span className="block text-[11px] text-amber-700">Needs you. {progress?.note}</span>}
        </span>
      </button>
      {thread?.reply && (
        <blockquote
          className={`mt-1.5 ml-6.5 rounded-lg px-2.5 py-1.5 text-[11px] leading-snug ring-1 ${
            thread.reply.verdict === 'confirmed' ? 'bg-emerald-50 text-emerald-800 ring-emerald-200' : 'bg-amber-50 text-amber-800 ring-amber-200'
          }`}
        >
          <span className="font-semibold">{thread.with}:</span> “{thread.reply.text}”
        </blockquote>
      )}
      {status === 'needs-you' && thread && (
        <button onClick={onFollowUp} className="mt-1.5 ml-6.5 inline-flex items-center gap-1 text-[11px] font-semibold text-accent hover:underline">
          <Send size={11} /> Send a follow-up
        </button>
      )}
    </li>
  )
}

// "just now", "3 min ago", "2 h ago".
function ago(at?: number) {
  if (!at) return 'just now'
  const min = Math.floor((Date.now() - at) / 60000)
  return min < 1 ? 'just now' : min < 60 ? `${min} min ago` : `${Math.floor(min / 60)} h ago`
}
