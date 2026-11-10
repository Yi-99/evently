import { Background, Controls, ReactFlow, type Edge, type ReactFlowInstance } from '@xyflow/react'
import { ArrowLeft, Check, CheckCircle2, CircleAlert, Clock, Link2, Loader2, MapPin, Users } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Deck } from '../data/demoDeck'
import { draftDeck } from '../data/draftDecks'
import { EVENT_TYPES, INTEGRATION_LABEL, gatedByGoogle } from '../data/workflows'
import { optionDeckId, saveDeck, useDecks } from '../lib/deckStore'
import { applyReply, followUp, isAwaiting, isDone, isOpen, runChecklist, watchReply, type Progress, type TodoProgress } from '../lib/execute'
import { layout, type Box } from '../lib/layout'
import { audienceUrl, presenterUrl, uploadUrl } from '../lib/live'
import { addFields, addFormHeight, buildOption, type FieldKey } from '../lib/options'
import type { Review } from '../lib/recent'
import type { RunnerContext } from '../lib/runners'
import { useLiveVenueTasks } from '../features/venues/useLiveVenueTasks'
import type { EventDetails, Task, TaskResult } from '../types'
import ResultNode, { type Decision, type LiveInfo, type ResultItem, type ResultNodeType } from './ResultNode'
import TaskNode, { type TaskNodeType } from './TaskNode'

const nodeTypes = { task: TaskNode, result: ResultNode }
const MIN_READABLE_ZOOM = 0.75
const PAD = 32
const MAX_FOCUS_ZOOM = 1.1
// Matches the node transform transition in index.css.
const MOVE_MS = 450
const NO_PROGRESS: TodoProgress[] = []
const OPTION_ZOOM = 1.2

interface Props {
  event: EventDetails
  // Scopes the decks of this plan's live-slides steps.
  planId: string
  tasks: Task[]
  selectedId: string | null
  googleConnected: boolean
  approved: boolean
  running: boolean
  onApprove: () => void
  onBack: () => void
  onRunTask: (id: string) => void
  onAddOption: (id: string, result: TaskResult) => void
  onSelect: (id: string | null) => void
  onConnectGoogle: () => void
  // Decisions restored from a saved plan, and where to report changes so they get saved.
  initialReview?: Review
  onReviewChange: (review: Review) => void
  /** Backend context for result actions and live cards; null/absent for sample-only plans. */
  ctx?: RunnerContext | null
  /** Replace one option of a step (matched by `cardId`, else `title`). Without it the canvas keeps the edit locally. */
  onUpdateResult?: (taskId: string, next: TaskResult) => void
}

// Identity of an option within its step: its board card when it has one, else its title.
const sameResult = (a: TaskResult, b: TaskResult) => (a.cardId || b.cardId ? a.cardId === b.cardId : a.title === b.title)
// Key for an option's decision and checklist progress (saved in the plan's review).
const resultKey = (taskId: string, r: Pick<TaskResult, 'cardId' | 'title'>) => `${taskId}:${r.cardId ?? r.title}`

// Local replacements (used when App doesn't handle onUpdateResult), applied over the step's results.
function applyOverrides(tasks: Task[], overrides: Record<string, TaskResult[]>): Task[] {
  if (!Object.keys(overrides).length) return tasks
  return tasks.map((t) => {
    const mine = overrides[t.id]
    if (!mine?.length || !t.results) return t
    return { ...t, results: t.results.map((r) => mine.find((o) => sameResult(o, r)) ?? r) }
  })
}

// What fans out of an opened step: its results, or a placeholder while it can't show any yet.
// The agent's first option shapes the hand-add form and lends it a checklist and draft.
const templateOf = (task: Task | undefined) => task?.results?.find((r) => r.meta !== 'Added by you') ?? task?.results?.[0]

// Approving the whole plan approves every option the user hasn't ignored; explicit choices win.
function decisionFor(decisions: Record<string, Decision>, planApproved: boolean, key: string): Decision | undefined {
  return decisions[key] ?? (planApproved ? 'approved' : undefined)
}

// Where one option of a live-slides step is presented, and its deck (null until drafted or uploaded).
function liveInfo(deckId: string, deck: Deck | null | undefined): LiveInfo {
  return {
    slides: deck?.slides.length ?? null,
    uploaded: deck?.slides[0]?.kind === 'image',
    presentUrl: presenterUrl(deckId),
    audienceUrl: audienceUrl(deckId),
    uploadUrl: uploadUrl(deckId),
  }
}

function childItems(
  task: Task | undefined,
  googleConnected: boolean,
  decisions: Record<string, Decision>,
  planApproved: boolean,
  liveOf: (task: Task, result: TaskResult) => LiveInfo | undefined,
): ResultItem[] {
  if (!task) return []
  if (task.status === 'running') return [{ kind: 'loading', agent: task.agent }]
  if (task.status === 'error') return [{ kind: 'error', message: task.error ?? 'The agent run failed.' }]
  if (task.results)
    return [
      ...task.results.map((result, i): ResultItem => {
        const key = resultKey(task.id, result)
        return {
          kind: 'result',
          n: i + 1,
          result,
          decision: decisionFor(decisions, planApproved, key),
          implied: !decisions[key] && planApproved,
          planApproved,
          live: liveOf(task, result),
        }
      }),
      // Always last: lets the user add an option the agent missed.
      { kind: 'add', fields: addFields(task.id, templateOf(task)) },
    ]
  if (gatedByGoogle(task) && !googleConnected) return [{ kind: 'locked', integration: INTEGRATION_LABEL[task.integration] }]
  return []
}

export default function WorkflowCanvas({
  event,
  planId,
  tasks: rawTasks,
  selectedId,
  googleConnected,
  approved,
  running,
  onApprove,
  onBack,
  onRunTask,
  onAddOption,
  onSelect,
  onConnectGoogle,
  initialReview,
  onReviewChange,
  ctx = null,
  onUpdateResult,
}: Props) {
  // Edits kept here only when App doesn't take onUpdateResult.
  const [overrides, setOverrides] = useState<Record<string, TaskResult[]>>({})
  const edited = useMemo(() => applyOverrides(rawTasks, overrides), [rawTasks, overrides])
  // Venue options follow their board cards live while research agents fill them in.
  const tasks = useLiveVenueTasks(edited, ctx)
  const updateResult = useCallback(
    (taskId: string, next: TaskResult) => {
      if (onUpdateResult) return onUpdateResult(taskId, next)
      setOverrides((o) => ({ ...o, [taskId]: [...(o[taskId] ?? []).filter((r) => !sameResult(r, next)), next] }))
    },
    [onUpdateResult],
  )
  // `open` is the step whose children are mounted; `spread` animates them out of / back into it.
  const [open, setOpen] = useState<string | null>(null)
  const [spread, setSpread] = useState(false)
  // Approve / ignore per option, keyed by resultKey (`${step}:${cardId ?? title}`).
  const [decisions, setDecisions] = useState<Record<string, Decision>>(() => initialReview?.decisions ?? {})
  // The option the user clicked into, plus per-option progress (keyed like decisions).
  const [focus, setFocus] = useState<number | null>(null)
  // Checklist progress per option. Older saved plans only have ticked indexes; those count as done by you.
  const [progress, setProgress] = useState<Progress>(() => ({
    ...Object.fromEntries(
      Object.entries(initialReview?.checked ?? {}).map(([k, ticked]) => [k, ticked.reduce<TodoProgress[]>((l, i) => ((l[i] = { status: 'done', by: 'you' }), l), [])]),
    ),
    ...initialReview?.progress,
  }))

  useEffect(() => {
    onReviewChange({ decisions, progress })
  }, [decisions, progress]) // eslint-disable-line react-hooks/exhaustive-deps

  // Once the plan is approved, each approved option's agent works through its checklist and ticks
  // items off as it finishes them. Un-approving an option stops its agent; finished items stay done.
  const runs = useRef(new Map<string, () => void>())
  useEffect(() => {
    const want = new Set<string>()
    for (const t of tasks)
      for (const r of t.results ?? []) {
        const key = resultKey(t.id, r)
        const items = r.todo ?? []
        const cur = progress[key] ?? []
        if (!approved || decisionFor(decisions, approved, key) !== 'approved') continue
        if (items.every((_, i) => !isOpen(cur[i], googleConnected))) continue
        want.add(key)
        if (runs.current.has(key)) continue
        const stop = runChecklist(t, r, event, googleConnected, cur, (i, p) =>
          setProgress((all) => {
            const list = [...(all[key] ?? [])]
            // The user already ticked it by hand: keep theirs.
            if (list[i]?.status === 'done' && list[i]?.by === 'you') return all
            list[i] = p
            return { ...all, [key]: list }
          }),
        )
        runs.current.set(key, stop)
      }
    for (const [key, stop] of runs.current) {
      if (want.has(key)) continue
      stop()
      runs.current.delete(key)
      setProgress((all) => (all[key]?.some((p) => p?.status === 'working') ? { ...all, [key]: all[key].map((p) => (p?.status === 'working' ? { status: 'pending' } : p)) } : all))
    }
  }, [tasks, decisions, approved, progress, googleConnected]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => runs.current.forEach((stop) => stop()), [])

  // Items waiting on an email reply: watch each thread until someone answers. A confirmation
  // finishes the item; a question or a no hands it back to the user (see lib/execute).
  const watchers = useRef(new Map<string, () => void>())
  useEffect(() => {
    const want = new Set<string>()
    for (const [key, list] of Object.entries(progress))
      list.forEach((p, i) => {
        if (!isAwaiting(p) || !p.thread) return
        const id = `${key}#${i}`
        want.add(id)
        if (watchers.current.has(id)) return
        const stop = watchReply(p.thread, (reply) => {
          watchers.current.delete(id)
          setProgress((all) => {
            const cur = all[key]?.[i]
            if (!isAwaiting(cur) || !cur?.thread) return all
            const next = [...all[key]]
            next[i] = applyReply(cur.thread, reply)
            return { ...all, [key]: next }
          })
        })
        watchers.current.set(id, stop)
      })
    for (const [id, stop] of watchers.current) {
      if (want.has(id)) continue
      stop()
      watchers.current.delete(id)
    }
  }, [progress])
  useEffect(() => () => watchers.current.forEach((stop) => stop()), [])

  const openTask = tasks.find((t) => t.id === open)

  // Live-slides steps: every option is its own deck with its own QR code.
  const liveOptions = tasks
    .filter((t) => t.integration === 'live-slides')
    .flatMap((t) => (t.results ?? []).map((r) => ({ task: t, result: r, id: optionDeckId(planId, t.id, r.title) })))
  const decks = useDecks(liveOptions.map((o) => o.id))
  // Save each option's draft deck once the agent has proposed it, unless it already has slides.
  const drafted = useRef(new Set<string>())
  useEffect(() => {
    for (const { task, result, id } of liveOptions) {
      if (decks[id] !== null || drafted.current.has(id)) continue
      drafted.current.add(id)
      const draft = draftDeck(event, task, result)
      if (draft) saveDeck(id, draft).catch(() => drafted.current.delete(id))
    }
  }) // eslint-disable-line react-hooks/exhaustive-deps

  const liveOf = (task: Task, result: TaskResult) => {
    if (task.integration !== 'live-slides') return undefined
    const id = optionDeckId(planId, task.id, result.title)
    return liveInfo(id, decks[id])
  }
  // Rebuild the option cards when any deck appears or changes size.
  const decksKey = liveOptions.map((o) => `${o.id}:${decks[o.id]?.slides.length ?? decks[o.id]}:${decks[o.id]?.slides[0]?.kind}`).join('|')
  const items = useMemo(
    () => childItems(openTask, googleConnected, decisions, approved, liveOf),
    [openTask, googleConnected, decisions, approved, decksKey], // eslint-disable-line react-hooks/exhaustive-deps
  )
  // A live step shows as live once any of its options has slides.
  const liveSteps = new Set(liveOptions.filter((o) => decks[o.id]).map((o) => o.task.id))
  const itemsKey = `${open}:${items.length}:${items[0]?.kind ?? ''}`

  // Selection changed: tuck the current children back in, then open the new step.
  useEffect(() => {
    if (selectedId === open) return
    setFocus(null)
    setSpread(false)
    const t = window.setTimeout(() => setOpen(selectedId), open ? MOVE_MS : 0)
    return () => clearTimeout(t)
  }, [selectedId]) // eslint-disable-line react-hooks/exhaustive-deps

  // Children mounted (or swapped, e.g. "searching…" → results): start them at the parent, then fan out.
  useEffect(() => {
    if (!open || open !== selectedId || !items.length) return
    setSpread(false)
    let inner = 0
    const outer = requestAnimationFrame(() => (inner = requestAnimationFrame(() => setSpread(true))))
    return () => {
      cancelAnimationFrame(outer)
      cancelAnimationFrame(inner)
    }
  }, [itemsKey]) // eslint-disable-line react-hooks/exhaustive-deps

  const focused = focus != null ? items[focus] : undefined
  const focusedAdd = focused?.kind === 'add' ? focused : undefined

  const { boxes, children } = useMemo(
    () =>
      layout(
        tasks,
        open
          ? {
              id: open,
              count: items.length,
              spread,
              focus,
              focusH: focusedAdd ? addFormHeight(focusedAdd.fields) : undefined,
            }
          : null,
      ),
    [tasks, open, items, spread, focus, focusedAdd],
  )

  // Clicking the current decision again clears it.
  const decide = useCallback(
    (key: string, d: Decision) =>
      setDecisions((all) => {
        const next = { ...all }
        if (next[key] === d) delete next[key]
        else next[key] = d
        return next
      }),
    [],
  )

  // Per step: options approved (drives the check) and options reviewed either way (the label).
  const { approvedCount, reviewedCount } = useMemo(() => {
    const approvedCount: Record<string, number> = {}
    const reviewedCount: Record<string, number> = {}
    for (const t of tasks) {
      for (const r of t.results ?? []) {
        const d = decisionFor(decisions, approved, resultKey(t.id, r))
        if (d) reviewedCount[t.id] = (reviewedCount[t.id] ?? 0) + 1
        if (d === 'approved') approvedCount[t.id] = (approvedCount[t.id] ?? 0) + 1
      }
    }
    return { approvedCount, reviewedCount }
  }, [tasks, decisions, approved])

  // A step is complete only when every approved option's checklist is done, so emails still
  // waiting on a reply keep it open. `awaiting`: items per step waiting on a reply. `needsYou`: items
  // the agent handed back, such as a reply that declined or asked a question.
  const { awaiting, needsYou, finished } = useMemo(() => {
    const awaiting: Record<string, number> = {}
    const needsYou: Record<string, number> = {}
    const finished: Record<string, boolean> = {}
    for (const t of tasks) {
      const picked = (t.results ?? []).filter((r) => decisionFor(decisions, approved, resultKey(t.id, r)) === 'approved')
      finished[t.id] = picked.length > 0
      for (const r of picked) {
        const cur = progress[resultKey(t.id, r)] ?? []
        awaiting[t.id] = (awaiting[t.id] ?? 0) + cur.filter(isAwaiting).length
        needsYou[t.id] = (needsYou[t.id] ?? 0) + cur.filter((p) => p?.status === 'needs-you').length
        if (!(r.todo ?? []).every((_, i) => isDone(cur[i]))) finished[t.id] = false
      }
    }
    return { awaiting, needsYou, finished }
  }, [tasks, decisions, approved, progress])

  const keyOf = (item: ResultItem) => (item.kind === 'result' && open ? resultKey(open, item.result) : '')

  // Ticking by hand marks an item done by you; unticking sends it back to the agent's queue.
  const toggleTodo = useCallback((key: string, i: number) => {
    setProgress((all) => {
      const list = [...(all[key] ?? [])]
      list[i] = list[i]?.status === 'done' ? { status: 'pending' } : { status: 'done', by: 'you' }
      return { ...all, [key]: list }
    })
  }, [])

  // The agent emails the thread again after a question or a no, and goes back to waiting.
  const followUpTodo = useCallback((key: string, i: number) => {
    setProgress((all) => {
      const thread = all[key]?.[i]?.thread
      if (!thread) return all
      const next = [...all[key]]
      next[i] = followUp(thread)
      return { ...all, [key]: next }
    })
  }, [])

  // Save a hand-added option; it counts as approved since the user picked it.
  const addOption = (values: Partial<Record<FieldKey, string>>) => {
    if (!open || !openTask || !values.title?.trim()) return
    const taken = new Set(openTask.results?.map((r) => r.title))
    const base = values.title.trim()
    let name = base
    for (let n = 2; taken.has(name); n++) name = `${base} (${n})`
    const option = buildOption({ ...values, title: name }, templateOf(openTask))
    onAddOption(open, option)
    setDecisions((d) => ({ ...d, [resultKey(open, option)]: 'approved' }))
    setFocus(null)
  }

  const connectAndRun = useCallback(() => {
    onConnectGoogle()
    if (open) onRunTask(open)
  }, [onConnectGoogle, onRunTask, open])

  const nodes = useMemo<(TaskNodeType | ResultNodeType)[]>(
    () => [
      ...tasks.map(
        (task): TaskNodeType => ({
          id: task.id,
          type: 'task',
          position: { x: boxes[task.id].x, y: boxes[task.id].y },
          // Sizes are known from the layout. Passing them keeps React Flow from hiding nodes to
          // re-measure them every time the node list is rebuilt (e.g. on each checklist update).
          measured: { width: boxes[task.id].w, height: boxes[task.id].h },
          data: {
            task,
            selected: task.id === selectedId,
            approved: approvedCount[task.id] ?? 0,
            reviewed: reviewedCount[task.id] ?? 0,
            awaiting: awaiting[task.id] ?? 0,
            needsYou: needsYou[task.id] ?? 0,
            finished: finished[task.id] ?? false,
            googleConnected,
            planApproved: approved,
            live: liveSteps.has(task.id),
          },
          draggable: false,
          zIndex: task.id === open ? 2 : 1,
        }),
      ),
      ...items.map(
        (item, i): ResultNodeType => ({
          id: `${open}::${i}`,
          type: 'result',
          position: { x: children[i].x, y: children[i].y },
          measured: { width: children[i].w, height: children[i].h },
          data: {
            item,
            task: openTask!,
            event,
            ctx,
            size: { w: children[i].w, h: children[i].h },
            expanded: focus === i,
            city: event.city,
            progress: progress[keyOf(item)] ?? NO_PROGRESS,
            onOpen: () => setFocus((f) => (f === i ? null : i)),
            onDecide: (d: Decision) => decide(keyOf(item), d),
            onToggleTodo: (t: number) => toggleTodo(keyOf(item), t),
            onFollowUp: (t: number) => followUpTodo(keyOf(item), t),
            onConnectGoogle: connectAndRun,
            onRetry: () => open && onRunTask(open),
            onAdd: addOption,
            onUpdateResult: (next: TaskResult) => open && updateResult(open, next),
          },
          draggable: false,
          selectable: false,
          zIndex: focus === i ? 3 : 0,
          style: { opacity: spread ? 1 : 0, transition: 'opacity 0.3s' },
        }),
      ),
    ],
    [tasks, boxes, children, items, open, selectedId, spread, focus, progress, approvedCount, reviewedCount, awaiting, needsYou, finished, googleConnected, approved, event, ctx, openTask, decide, toggleTodo, followUpTodo, connectAndRun, updateResult, decks], // eslint-disable-line react-hooks/exhaustive-deps
  )

  const flowRef = useRef<ReactFlowInstance<TaskNodeType | ResultNodeType> | null>(null)
  const boxRef = useRef<HTMLDivElement>(null)

  const fitTo = (all: Box[], maxZoom: number, duration: number) => {
    const rf = flowRef.current
    const box = boxRef.current?.getBoundingClientRect()
    if (!rf || !box || !all.length) return
    const minX = Math.min(...all.map((b) => b.x))
    const minY = Math.min(...all.map((b) => b.y))
    const w = Math.max(...all.map((b) => b.x + b.w)) - minX
    const h = Math.max(...all.map((b) => b.y + b.h)) - minY
    const fit = Math.min((box.width - PAD * 2) / w, (box.height - PAD * 2) / h, maxZoom)
    const zoom = Math.max(fit, MIN_READABLE_ZOOM)
    // Too big to fit at a readable zoom: anchor on the left edge and let the user pan.
    const x = fit >= MIN_READABLE_ZOOM ? (box.width - w * zoom) / 2 - minX * zoom : PAD - minX * zoom
    rf.setViewport({ x, y: (box.height - h * zoom) / 2 - minY * zoom, zoom }, { duration })
  }

  // Whole chart when nothing is open; the opened step and its children once they've fanned out.
  // A clicked option gets zoomed onto on its own.
  const frame = () => {
    if (spread && focus != null && children[focus]) fitTo([children[focus]], OPTION_ZOOM, 700)
    else if (spread && open && boxes[open]) fitTo([boxes[open], ...children], MAX_FOCUS_ZOOM, 700)
    else if (!selectedId) fitTo(Object.values(boxes), 1, 500)
  }
  useEffect(frame, [tasks.length, spread, open, items.length, focus]) // eslint-disable-line react-hooks/exhaustive-deps

  const edges = useMemo<Edge[]>(() => {
    const ids = new Set(tasks.map((t) => t.id))
    const deps = tasks.flatMap((t) =>
      t.dependsOn
        .filter((d) => ids.has(d))
        .map(
          (d): Edge => ({
            id: `${d}-${t.id}`,
            source: d,
            target: t.id,
            type: 'smoothstep',
            animated: t.status === 'running',
            // Fade the rest of the graph while a step is fanned out.
            style: { opacity: spread ? 0.25 : 1, transition: 'opacity 0.3s' },
          }),
        ),
    )
    const fan = items.map(
      (_, i): Edge => ({
        id: `${open}->${i}`,
        source: open!,
        target: `${open}::${i}`,
        type: 'smoothstep',
        style: { stroke: '#818cf8', strokeDasharray: '4 4', opacity: spread ? 1 : 0, transition: 'opacity 0.3s' },
      }),
    )
    return [...deps, ...fan]
  }, [tasks, items, open, spread])

  // Matches the green check on step nodes: every approved option's checklist is done, replies included.
  const done = tasks.filter((t) => t.status === 'done' && finished[t.id]).length
  const waiting = Object.values(awaiting).reduce((a, b) => a + b, 0)
  const handedBack = Object.values(needsYou).reduce((a, b) => a + b, 0)
  const meta = EVENT_TYPES.find((e) => e.id === event.type)!

  return (
    <section className="relative flex h-full min-h-0 flex-col overflow-hidden rounded-3xl bg-gray-50 shadow-sm ring-1 ring-gray-900/5">
      <header className="flex items-center gap-3 border-b border-line bg-white/80 px-5 py-3.5 backdrop-blur">
        <button onClick={onBack} aria-label="Back" className="rounded-full p-2 text-muted hover:bg-line/60 hover:text-ink">
          <ArrowLeft size={18} />
        </button>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-lg leading-none font-bold tracking-tight">
              {meta.emoji} {meta.label} logistics
            </span>
            <span
              className={`rounded-full px-2.5 py-0.5 text-[11px] font-semibold ring-1 ${
                approved ? 'bg-accent-soft text-accent ring-accent/20' : 'bg-gray-50 text-muted ring-gray-900/10'
              }`}
            >
              {approved ? 'Approved' : 'Draft'}
            </span>
          </div>
          <div className="mt-1.5 flex gap-3 text-xs text-muted">
            <span className="flex items-center gap-1"><Users size={12} />{event.headcount} people</span>
            <span className="flex items-center gap-1"><MapPin size={12} />{event.city}</span>
            <span>{tasks.length} steps · {done} complete</span>
            {waiting > 0 && (
              <span className="flex items-center gap-1 font-medium text-accent">
                <Clock size={12} />
                {waiting} awaiting {waiting === 1 ? 'reply' : 'replies'}
              </span>
            )}
            {handedBack > 0 && (
              <span className="flex items-center gap-1 font-medium text-amber-700">
                <CircleAlert size={12} />
                {handedBack} {handedBack === 1 ? 'needs' : 'need'} you
              </span>
            )}
          </div>
        </div>
        <div className="h-1.5 w-28 overflow-hidden rounded-full bg-line">
          <div className="h-full bg-ok transition-all duration-500" style={{ width: `${(done / Math.max(tasks.length, 1)) * 100}%` }} />
        </div>
        <button
          onClick={onConnectGoogle}
          className={`flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-xs font-semibold shadow-sm ring-1 transition ${
            googleConnected ? 'bg-ok-soft text-ok ring-ok/30' : 'bg-white ring-gray-900/10 hover:ring-accent'
          }`}
        >
          {googleConnected ? <CheckCircle2 size={13} /> : <Link2 size={13} />}
          {googleConnected ? 'Google connected' : 'Connect Google'}
        </button>
        <button
          onClick={onApprove}
          disabled={approved}
          className="flex items-center gap-1.5 rounded-full bg-accent px-4 py-2 text-sm font-semibold text-white shadow-sm shadow-indigo-500/30 transition hover:bg-indigo-500 disabled:bg-accent-soft disabled:text-accent disabled:shadow-none"
        >
          {running ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} strokeWidth={3} />}
          {running ? 'Agents running' : approved ? 'Plan approved' : 'Approve plan'}
        </button>
      </header>

      <div ref={boxRef} className="min-h-0 flex-1">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodeClick={(_, n) => {
            if (n.type !== 'task') return
            if (n.id === selectedId) return onSelect(null)
            onSelect(n.id)
            // First open of a step with nothing yet: send its agent to fetch results.
            const t = tasks.find((x) => x.id === n.id)
            if (t && !t.results && t.status !== 'running' && !(gatedByGoogle(t) && !googleConnected)) onRunTask(t.id)
          }}
          onPaneClick={() => onSelect(null)}
          onInit={(rf) => {
            flowRef.current = rf
            fitTo(Object.values(boxes), 1, 0)
          }}
          panOnScroll
          nodesConnectable={false}
          minZoom={0.3}
        >
          <Background gap={22} size={1.2} color="#d1d5db" />
          <Controls showInteractive={false} position="top-right" orientation="horizontal" />
        </ReactFlow>
      </div>

    </section>
  )
}
