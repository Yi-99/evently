import { CloudOff, LoaderCircle, MessageCircle, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { getAuth, useAuth, type AuthSnapshot } from './api/auth'
import { api, isApiError } from './api/client'
import { isRunFinished, useEventStore } from './api/realtime'
import type { ChatTurnOut } from './api/types'
import ChatPanel from './components/ChatPanel'
import IntakePage from './components/IntakePage'
import Landing from './components/Landing'
import LoginPage, { type AuthMode as FormMode } from './components/LoginPage'
import MarketingPage from './components/MarketingPage'
import PlanLoader from './components/PlanLoader'
import WorkflowCanvas from './components/WorkflowCanvas'
import { DEFAULT_HEADCOUNT, EVENT_TYPES, buildWorkflow, gatedByGoogle, removeStep } from './data/workflows'
import { localItem, mergeChat, serverTime, type ChatItem } from './features/chat/messages'
import { createEvent, getEvent, useRequirementsSync } from './features/events/api'
import { eventName, fromEvent, isoDate, toRequirements, type EventRequirements } from './features/events/requirements'
import { loadUser, signOut, type User } from './lib/auth'
import { interpret, type ChatAction } from './lib/chat'
import { extractAnswers, mergeFound } from './lib/extract'
import { newDeckId } from './lib/deckStore'
import { addRecent, findRecentByEvent, linkRecent, savePlan, type RecentEvent, type Review, type SavedPlan } from './lib/recent'
import { BUILD_NOW, QUESTIONS, SKIP, isRequired, missingRequired, nextQuestion, type IntakeAnswers } from './lib/intake'
import { runTask, type RunnerContext } from './lib/runners'
import type { ChatMessage, EventDetails, EventType, PlanStart, Task, TaskResult } from './types'

// Short names for intake questions, for "I filled in: date, budget".
const LABEL: Record<string, string> = {
  goal: 'goal', date: 'date', headcount: 'headcount', city: 'location', budget: 'budget', approver: 'approver',
  audience: 'audience', format: 'format', food: 'food', accessibility: 'accessibility', features: 'must-haves',
  booked: 'bookings', tickets: 'registration', branding: 'branding', constraints: 'constraints', contact: 'updates',
}
const describe = (ids: string[]) => ids.map((id) => LABEL[id] ?? id).join(', ')

// Wall-clock time for ordering local chat notes (handlers only).
const clock = () => Date.now()

const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')

// Plan-graph edits the backend orchestrator doesn't model; the rule-based interpreter handles them locally.
const isGraphEdit = (a: ChatAction) => a.kind === 'add' || a.kind === 'remove' || a.kind === 'run'

const AI_OFF = 'AI is not configured (set ANTHROPIC_API_KEY). Only plan edits work here.'
const OFFLINE = 'Offline: only plan edits work here.'
const UNSAVED = "This plan isn't saved to the server, so only plan edits work here."

function applyAction(tasks: Task[], a: ChatAction): Task[] {
  if (a.kind === 'add') {
    let id = slug(a.title) || 'task'
    while (tasks.some((t) => t.id === id)) id += '-2'
    const parent = a.after ?? tasks[0]?.id
    return [
      ...tasks,
      {
        id,
        title: a.title,
        description: `Custom step added from chat: ${a.title}.`,
        agent: 'Research',
        integration: 'web',
        dependsOn: parent ? [parent] : [],
        status: 'idle',
      },
    ]
  }
  if (a.kind === 'remove') return removeStep(tasks, a.id)
  return tasks
}

// Identity of an option within its step: its board card when it has one, else its title.
const sameResult = (a: TaskResult, b: TaskResult) => (a.cardId || b.cardId ? a.cardId === b.cardId : a.title === b.title)

function Pill({ text }: { text: string }) {
  return (
    <div className="pointer-events-none fixed bottom-4 left-1/2 z-30 -translate-x-1/2 flex items-center gap-1.5 rounded-full bg-white/90 px-3 py-1 text-xs font-semibold text-muted shadow-sm ring-1 ring-gray-900/10 backdrop-blur">
      <CloudOff size={12} /> {text}
    </div>
  )
}

function Centered({ children }: { children: ReactNode }) {
  return <main className="grid h-full place-items-center bg-gray-100 p-6 text-center text-muted">{children}</main>
}

// Signed in, or the backend lets requests through as its dev user.
const isOnline = (a: Pick<AuthSnapshot, 'mode' | 'token'>) => a.mode === 'optional' || (a.mode === 'required' && Boolean(a.token))

// Chat notes saved with a local plan (or shipped with a community plan) back into chat items, oldest first.
const toLocal = (messages: ChatMessage[]): ChatItem[] => {
  const base = Date.now() - messages.length
  return messages.map((m, i) => localItem(m.role, m.text, (m as Partial<ChatItem>).at ?? base + i))
}

interface AppProps {
  /** Backend event from the /events/:eventId route; App loads it, and follows changes (back/forward, links). */
  routeEventId?: string
}

export default function App({ routeEventId }: AppProps) {
  const auth = useAuth()
  const navigate = useNavigate()
  const online = isOnline(auth)
  // Accounts live on the server (it said 401 to an anonymous /auth/me): LoginPage signs in there.
  const accounts = auth.mode === 'required'
  // Demo profile from LoginPage's local flow (lib/auth), used whenever the server doesn't require accounts.
  const [localUser, setLocalUser] = useState<User | null>(loadUser)
  const user: User | null = accounts
    ? auth.token && auth.user
      ? { name: auth.user.name || auth.user.email, email: auth.user.email, provider: 'email' }
      : null
    : localUser
  // Sign-in / sign-up form on screen, and the plan to start once it's done.
  const [form, setForm] = useState<FormMode | null>(null)
  const [pending, setPending] = useState<PlanStart | null>(null)
  // Past the marketing page. Deep links to an event skip it.
  const [entered, setEntered] = useState(Boolean(routeEventId))
  // The backend event this plan belongs to (null for local-only plans, or before it's created).
  const [eventId, setEventId] = useState<string | null>(routeEventId ?? null)
  // Event id being loaded from the route, and why that failed.
  const [loadingId, setLoadingId] = useState<string | null>(routeEventId ?? null)
  const [loadError, setLoadError] = useState<string | null>(null)
  // True while POST /events for a freshly picked plan is in flight.
  const [creating, setCreating] = useState(false)
  // The event name is "<Type> in <City>" and follows the city, unless someone renamed it.
  const [autoNamed, setAutoNamed] = useState(true)
  const live = useEventStore(online ? eventId : null)
  const runnerCtx = useMemo<RunnerContext | null>(
    () => (eventId && live.store ? { eventId, store: live.store, api } : null),
    [eventId, live.store],
  )
  const [event, setEvent] = useState<EventDetails | null>(null)
  // Scopes this plan's live-slide decks, so a new plan doesn't reuse the last one's slides.
  const [planId, setPlanId] = useState('')
  const [tasks, setTasks] = useState<Task[]>([])
  // Chat notes that only live in this browser (intake recap, approvals, rule-based edits, offline chat).
  const [local, setLocal] = useState<ChatItem[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [building, setBuilding] = useState(false)
  const [approved, setApproved] = useState(false)
  const [chatOpen, setChatOpen] = useState(false)
  // Assistant messages newer than this (epoch ms) show as an unread badge on the chat button.
  const [seenAt, setSeenAt] = useState(0)
  const [googleConnected, setGoogleConnected] = useState(false)
  const [answers, setAnswers] = useState<IntakeAnswers>({})
  // True while the chat is asking its setup questions, before the plan is drawn.
  const [intake, setIntake] = useState(false)
  // Why the last intake answer was rejected, shown under the question.
  const [intakeNote, setIntakeNote] = useState<string | null>(null)
  // Question ids answered by the landing page (event type, plus any headcount or city in the prompt).
  const [prefilled, setPrefilled] = useState<string[]>([])
  // True while Claude reads a typed description for answers.
  const [extracting, setExtracting] = useState(false)
  // Tells the user what Claude filled in from their typed text.
  const [intakeInfo, setIntakeInfo] = useState<string | null>(null)
  // Chat: POST in flight, and the orchestrator run writing the reply.
  const [posting, setPosting] = useState(false)
  const [pendingRun, setPendingRun] = useState<string | null>(null)
  // Set when POST /chat/messages said 503 (no LLM key) although /health looked fine.
  const [aiDown, setAiDown] = useState(false)
  // Bumped on every pick, so a slow extraction or create for an abandoned event is ignored.
  const pickSeq = useRef(0)
  // Approve / ignore choices and checklist progress from the node map, saved with the plan.
  const [review, setReview] = useState<Review>({ decisions: {}, progress: {} })
  const started = useRef(new Set<string>())

  // --- Persistence: every intake answer, detail change, plan edit and approval → debounced PATCH /events/{id}.
  const desired = useMemo(
    () => (event ? toRequirements(event, answers, tasks, approved, !intake) : null),
    [event, answers, tasks, approved, intake],
  )
  const sync = useRequirementsSync(eventId, desired, event && autoNamed ? eventName(event) : null)

  const resetPlan = () => {
    setEvent(null)
    setTasks([])
    setLocal([])
    setSelectedId(null)
    setRunning(false)
    setBuilding(false)
    setApproved(false)
    setIntake(false)
    setChatOpen(false)
    setSeenAt(0)
    setAnswers({})
    setPrefilled([])
    setIntakeNote(null)
    setIntakeInfo(null)
    setExtracting(false)
    setPendingRun(null)
    setPosting(false)
  }

  // Follow the URL: /events/:id (back/forward, recent-event links) loads that event; / goes back to the landing page.
  const [routeSeen, setRouteSeen] = useState(routeEventId)
  if (routeSeen !== routeEventId) {
    setRouteSeen(routeEventId)
    if (routeEventId && routeEventId !== eventId) {
      resetPlan()
      setEventId(routeEventId)
      setLoadingId(routeEventId)
      setLoadError(null)
      setEntered(true)
    } else if (!routeEventId && eventId) {
      resetPlan()
      setEventId(null)
      setLoadingId(null)
    }
  }

  // Load a reopened event and rebuild the planner from its requirements.
  useEffect(() => {
    if (!loadingId || !online) return
    let current = true
    getEvent(loadingId).then(
      (out) => {
        if (!current) return
        const h = fromEvent(out)
        sync.markSaved(out.requirements, out.name)
        setAutoNamed(out.name === eventName(h.event))
        // This browser's copy of the plan keeps what the server doesn't: agent results, shared steps, the review.
        const cached = findRecentByEvent(out.id)
        const kept = new Map(cached?.plan?.tasks.map((t) => [t.id, t]))
        setEvent(h.event)
        setTasks(
          h.tasks.map((t) => {
            const c = kept.get(t.id)
            return c?.status === 'done' && c.results ? { ...t, status: 'done', results: c.results, shared: c.shared } : t
          }),
        )
        const saved = cached?.plan?.review
        setReview(saved ? { ...saved, progress: saved.progress ?? {} } : { decisions: {}, progress: {} })
        setPlanId(cached?.id ?? out.id)
        if (!cached) addRecent({ id: out.id, type: h.event.type, eventId: out.id })
        setAnswers(h.answers)
        setApproved(h.approved)
        setIntake(h.intake)
        setPrefilled(['type'])
        // History loaded with a reopened event isn't news.
        setSeenAt(Date.now())
        setLoadingId(null)
      },
      (e: unknown) => {
        // 401: the stale token was dropped and the sign-in form shows; this effect reruns after signing in.
        if (!current || isApiError(e, 401)) return
        setLoadError(isApiError(e, 404) ? "This event doesn't exist, or it isn't yours." : e instanceof Error ? e.message : 'Could not load the event.')
      },
    )
    return () => {
      current = false
    }
  }, [loadingId, online, sync])

  // Requirement changes made elsewhere (the orchestrator's update_requirements, a venue search, another tab).
  useEffect(() => {
    const store = live.store
    if (!store) return
    return store.onEvent((e) => {
      // Our own saves echo back too; while one is pending, local state is newer than any echo.
      if (e.type !== 'event.updated' || sync.busy()) return
      const req = e.data.event.requirements as Record<string, unknown>
      const prev = sync.saved()
      sync.markSaved(req, e.data.event.name)
      const changed = (k: string) => req[k] !== undefined && JSON.stringify(req[k]) !== JSON.stringify(prev[k])
      setEvent((ev) => {
        if (!ev) return ev
        const next = { ...ev }
        if (changed('headcount') && typeof req.headcount === 'number') next.headcount = req.headcount
        if (changed('location') && typeof req.location === 'string' && req.location) next.city = req.location
        if (changed('event_date') && typeof req.event_date === 'string' && req.event_date !== isoDate(ev.date)) next.date = req.event_date
        return next.headcount === ev.headcount && next.city === ev.city && next.date === ev.date ? ev : next
      })
    })
  }, [live.store, sync])

  const say = (text: string) => setLocal((m) => [...m, localItem('assistant', text)])

  // POST /events for a plan started here (picked, copied, or made offline), then move to /events/{id}.
  // Reads the auth store directly: right after signing in, this render's `online` is still stale.
  const saveAsEvent = (localId: string, details: EventDetails, note: string, req: EventRequirements, seq: number) => {
    if (!isOnline(getAuth())) return
    setCreating(true)
    createEvent(eventName(details), note, req)
      .then(
        (out) => {
          linkRecent(localId, out.id)
          if (seq !== pickSeq.current) return
          sync.markSaved(out.requirements, out.name)
          setAutoNamed(true)
          setEventId(out.id)
          navigate(`/events/${encodeURIComponent(out.id)}`, { replace: true })
        },
        () => undefined,
      )
      .finally(() => seq === pickSeq.current && setCreating(false))
  }

  // `skip`: steps a template leaves out.
  const pick = (type: EventType, note?: string, skip?: string[]) => {
    const meta = EVENT_TYPES.find((e) => e.id === type)!
    const wf = buildWorkflow(type, skip)
    const id = newDeckId()
    addRecent({ id, type, note })
    // Pull headcount / city out of a free-text landing prompt like "200-person hackathon in Austin".
    const patch = Object.assign(
      {},
      ...interpret(note?.replace(/-person/i, ' people') ?? '', wf).actions.flatMap((a) => (a.kind === 'details' ? [a.patch] : [])),
    ) as Partial<EventDetails>
    const details: EventDetails = { type, headcount: DEFAULT_HEADCOUNT[type], city: 'San Francisco', date: '', ...patch }
    setEvent(details)
    setTasks(wf)
    setPlanId(id)
    setReview({ decisions: {}, progress: {} })
    setSelectedId(null)
    setRunning(false)
    setApproved(false)
    setBuilding(false)
    setIntake(true)
    setChatOpen(false)
    setSeenAt(0)
    setPendingRun(null)
    started.current.clear()
    // Details already given in the landing prompt count as answered.
    const pre: IntakeAnswers = { type: meta.label }
    if (patch.headcount) pre.headcount = String(patch.headcount)
    if (patch.city) pre.city = patch.city
    setAnswers(pre)
    setPrefilled(Object.keys(pre))
    setIntakeNote(null)
    setIntakeInfo(null)
    setLocal([localItem('user', note ?? meta.label)])
    setEventId(null)
    const seq = ++pickSeq.current

    // Save the plan as a backend event (offline: it stays local, on sample data).
    saveAsEvent(id, details, note ?? '', toRequirements(details, pre, wf, false, false), seq)

    if (!note) return setExtracting(false)
    // Let Claude fill in anything else the landing prompt already says (date, budget, food, …).
    setExtracting(true)
    extractAnswers(note, meta.label, pre)
      .then((found) => {
        if (seq !== pickSeq.current) return
        const merged = mergeFound(pre, found)
        setEvent((e) => (e ? { ...e, ...merged.patch } : e))
        setAnswers(merged.answers)
        setPrefilled(Object.keys(merged.answers))
        if (merged.added.length) setIntakeInfo(`From your description I filled in: ${describe(merged.added)}.`)
        if (!nextQuestion(merged.answers)) finishIntake(wf.length)
      })
      .finally(() => seq === pickSeq.current && setExtracting(false))
  }

  // Opens a saved plan on the node map as it was left.
  const openSaved = (id: string, p: SavedPlan) => {
    const seq = ++pickSeq.current
    started.current.clear()
    setEvent(p.event)
    setTasks(p.tasks)
    setLocal(toLocal(p.messages))
    setAnswers(p.answers)
    setApproved(p.approved)
    // Plans saved before checklist progress existed have `checked` instead; the map converts it.
    setReview({ ...p.review, progress: p.review.progress ?? {} })
    setPlanId(id)
    setEventId(null)
    setSelectedId(null)
    setRunning(false)
    setBuilding(false)
    setIntake(false)
    setExtracting(false)
    setIntakeNote(null)
    setIntakeInfo(null)
    setPrefilled(['type'])
    setPendingRun(null)
    setChatOpen(false)
    setSeenAt(Date.now())
    return seq
  }

  // Reopens a recent event. One saved on the server reopens from there (the /events/:id route hydrates it and
  // lays this browser's cache over it); a local plan reopens as it was saved; one that never got that far starts over.
  const resume = (r: Pick<RecentEvent, 'id' | 'type' | 'note' | 'plan' | 'eventId'>) => {
    if (r.eventId && online) return navigate(`/events/${encodeURIComponent(r.eventId)}`)
    if (!r.plan) return pick(r.type, r.note)
    const p = r.plan
    const seq = openSaved(r.id, p)
    // Made offline: save it to the server now that it's reachable.
    if (!r.eventId) saveAsEvent(r.id, p.event, r.note ?? '', toRequirements(p.event, p.answers, p.tasks, p.approved, true), seq)
  }

  // Someone else's filled-out plan becomes a new event of the user's own, opened on the node map.
  const copyPlan = (plan: SavedPlan, note: string) => {
    const id = newDeckId()
    addRecent({ id, type: plan.event.type, note })
    const seq = openSaved(id, plan)
    // Leave the welcome message unread so the chat badge points to it.
    setSeenAt(0)
    saveAsEvent(id, plan.event, note, toRequirements(plan.event, plan.answers, plan.tasks, plan.approved, true), seq)
  }

  const start = (s: PlanStart) => ('copy' in s ? copyPlan(s.copy, s.note) : pick(s.type, s.note, s.skip))

  // Save the plan whenever it changes on the node map, so Recent Events reopens it there.
  const onMap = !!event && !intake && !building
  useEffect(() => {
    // Only this browser's chat notes; the server keeps the backend chat history.
    if (onMap && event && planId) savePlan(planId, { event, tasks, messages: local, answers, approved, review })
  }, [onMap, event, tasks, local, answers, approved, review, planId])

  // Ends the questions and draws the plan. The draft message waits in the chat as an unread message.
  const finishIntake = (steps = tasks.length) => {
    say(`Thanks! Here's a draft plan with ${steps} steps — each one gets its own agent. What would you like to edit? When it looks right, hit Approve plan.`)
    setIntake(false)
    setBuilding(true)
  }

  const answer = (text: string) => {
    const q = nextQuestion(answers)!
    setIntakeInfo(null)
    if (text.toLowerCase() === BUILD_NOW.toLowerCase()) {
      const missing = missingRequired(answers)
      if (!missing.length) return finishIntake()
      return setIntakeNote(`I still need ${missing.length} required answer${missing.length > 1 ? 's' : ''} first.`)
    }
    const skipped = text.toLowerCase() === SKIP.toLowerCase()
    if (skipped && isRequired(q)) return setIntakeNote("This one is required, so it can't be skipped.")
    const patch = skipped ? {} : q.parse ? q.parse(text) : {}
    if (!patch) return setIntakeNote(q.id === 'headcount' ? 'Please include a number, like “250 people”.' : "I didn't catch that. Try again?")
    setIntakeNote(null)
    if (Object.keys(patch).length) setEvent((e) => (e ? { ...e, ...patch } : e))
    const next = { ...answers, [q.id]: skipped ? '' : text }
    setAnswers(next)
    if (!nextQuestion(next)) finishIntake()
  }

  // Typed text can answer several questions at once ("Dec 5, about $10k, vegan food"), so Claude reads it first.
  // If it finds nothing, the text is taken as the answer to the question on screen.
  const answerFreeText = async (text: string) => {
    const q = nextQuestion(answers)
    if (!q || !event) return
    setExtracting(true)
    const seq = pickSeq.current
    const found = await extractAnswers(text, EVENT_TYPES.find((e) => e.id === event.type)!.label, answers, q.id)
    if (seq !== pickSeq.current) return
    setExtracting(false)
    const merged = mergeFound(answers, found)
    if (!merged.added.length) return answer(text)
    setIntakeNote(null)
    setIntakeInfo(`Got it. I filled in: ${describe(merged.added)}.`)
    setEvent((e) => (e ? { ...e, ...merged.patch } : e))
    setAnswers(merged.answers)
    if (!nextQuestion(merged.answers)) finishIntake()
  }

  // Reopens the closest answered question before the one on screen. Pre-filled answers stay locked.
  const intakeBack = () => {
    const at = QUESTIONS.indexOf(nextQuestion(answers)!)
    const prev = QUESTIONS.slice(0, at).findLast((q) => q.id in answers && !prefilled.includes(q.id))
    if (!prev) return
    const rest = { ...answers }
    delete rest[prev.id]
    setAnswers(rest)
    setIntakeNote(null)
  }

  // --- Chat -------------------------------------------------------------------------------------------
  const llmConfigured = auth.health?.llm_configured === true && !aiDown
  const aiReady = Boolean(runnerCtx && llmConfigured)
  const chatNotice = aiReady
    ? null
    : !online
      ? OFFLINE
      : !llmConfigured
        ? AI_OFF
        : !eventId && !creating
          ? UNSAVED
          : null

  // Applies rule-based actions; returns true when it started the agents.
  const applyActions = (actions: ChatAction[]) => {
    let next = tasks
    for (const a of actions) {
      if (a.kind === 'details') setEvent((e) => (e ? { ...e, ...a.patch } : e))
      else if (a.kind === 'run') setApproved(true)
      else next = applyAction(next, a)
    }
    if (next !== tasks) setTasks(next)
    if (actions.some((a) => a.kind === 'run')) startRun()
    // Any edit after approval sends the plan back to draft.
    else if (actions.some((a) => a.kind !== 'none')) setApproved(false)
  }

  const send = (text: string) => {
    const { reply, actions } = interpret(text, tasks)
    const now = clock()
    if (!aiReady || !runnerCtx) {
      applyActions(actions)
      setLocal((m) => [...m, localItem('user', text, now), localItem('assistant', reply, now + 1)])
      return
    }
    // Plan-graph edits apply instantly; the message still goes to the orchestrator so history is kept.
    const graph = actions.filter(isGraphEdit)
    if (graph.length) applyActions(graph)
    const { store, eventId: id } = runnerCtx
    setPosting(true)
    api
      .post<ChatTurnOut>(`/events/${encodeURIComponent(id)}/chat/messages`, {
        text,
        context: { selected_task_id: selectedId, selected_card_ids: [] },
      })
      .then(
        ({ message, run }) => {
          store.patch((s) => (s.messages.some((m) => m.id === message.id) ? s : { ...s, messages: [...s.messages, message] }))
          if (graph.length) setLocal((m) => [...m, localItem('assistant', reply, serverTime(message.created_at) + 1)])
          setPendingRun(run.id)
          // Backstop for a dropped socket: unlock once the run ends, and refetch what we missed.
          store
            .waitForRun(run.id, { timeoutMs: 5 * 60_000 })
            .catch(() => undefined)
            .then(() => {
              if (store.getState().channel !== 'open') void store.reload()
              setPendingRun((r) => (r === run.id ? null : r))
            })
        },
        (e: unknown) => {
          if (isApiError(e, 503)) {
            // No LLM key after all: answer with the rule-based editor.
            setAiDown(true)
            applyActions(actions.filter((a) => !isGraphEdit(a)))
            setLocal((m) => [...m, localItem('user', text, now), localItem('assistant', reply, now + 1)])
            return
          }
          const failed: ChatItem = {
            ...localItem('assistant', '', now + 1),
            status: 'error',
            error: `Couldn't reach the assistant: ${e instanceof Error ? e.message : 'unknown error'}`,
          }
          setLocal((m) => [...m, localItem('user', text, now), failed])
        },
      )
      .finally(() => setPosting(false))
  }

  const chatItems = useMemo(() => mergeChat(live.messages, live.tools, local), [live.messages, live.tools, local])
  const replyDone = (runId: string) =>
    (live.runs[runId] && isRunFinished(live.runs[runId])) ||
    live.messages.some((m) => m.role === 'assistant' && m.meta?.agent_run_id === runId && (m.status === 'complete' || m.status === 'error'))
  const chatBusy = posting || (pendingRun !== null && !replyDone(pendingRun))

  // --- Plan ------------------------------------------------------------------------------------------
  const approve = () => {
    setApproved(true)
    say('Plan approved. Spinning up agents — each step starts as soon as the ones it depends on finish.')
    startRun()
  }

  const finishBuilding = useCallback(() => setBuilding(false), [])

  const startRun = () => {
    started.current.clear()
    // Steps kept from a community plan stay done; agents fill in the rest.
    setTasks((ts) => ts.map((t) => (t.shared && t.status === 'done' ? t : { ...t, status: 'queued', results: undefined })))
    setRunning(true)
  }

  const failTask = (id: string, e: unknown) =>
    setTasks((ts) =>
      ts.map((t) => (t.id === id ? { ...t, status: 'error', error: e instanceof Error ? e.message : 'The agent run failed.' } : t)),
    )

  // Scheduler: start every queued task whose dependencies are done.
  useEffect(() => {
    if (!running || !event) return
    const status = new Map(tasks.map((t) => [t.id, t.status]))
    const ids = new Set(tasks.map((t) => t.id))
    const updates = new Map<string, Pick<Task, 'status' | 'blockedBy'>>()

    for (const t of tasks) {
      if (t.status !== 'queued' || started.current.has(t.id)) continue
      const deps = t.dependsOn.filter((d) => ids.has(d)).map((d) => status.get(d))
      // A missing connection outranks waiting on earlier steps.
      if (gatedByGoogle(t) && !googleConnected) updates.set(t.id, { status: 'blocked', blockedBy: 'google' })
      else if (deps.some((s) => s === 'blocked' || s === 'error')) updates.set(t.id, { status: 'blocked', blockedBy: 'deps' })
      else if (deps.every((s) => s === 'done')) {
        started.current.add(t.id)
        updates.set(t.id, { status: 'running' })
        runTask(t, event, runnerCtx).then(
          (results) => setTasks((ts) => ts.map((x) => (x.id === t.id ? { ...x, status: 'done', results } : x))),
          (e: unknown) => failTask(t.id, e),
        )
      }
    }

    if (updates.size) {
      setTasks((ts) => ts.map((t) => (updates.has(t.id) ? { ...t, blockedBy: undefined, ...updates.get(t.id)! } : t)))
      return
    }
    if (!tasks.some((t) => t.status === 'queued' || t.status === 'running')) {
      setRunning(false)
      const blocked = tasks.filter((t) => t.status === 'blocked').length
      setLocal((m) => [
        ...m,
        localItem(
          'assistant',
          blocked
            ? `Finished what I could. ${blocked} step${blocked > 1 ? 's are' : ' is'} waiting on Google access — connect Google and I’ll pick up from there.`
            : 'All agents finished. Click any step to review what they found.',
        ),
      ])
    }
  }, [tasks, running, event, googleConnected, runnerCtx])

  // Options the user adds by hand go at the end of the step's list.
  const addOption = (id: string, result: TaskResult) =>
    setTasks((ts) => ts.map((t) => (t.id === id ? { ...t, results: [...(t.results ?? []), result] } : t)))

  const updateResult = (taskId: string, next: TaskResult) =>
    setTasks((ts) =>
      ts.map((t) => (t.id === taskId && t.results ? { ...t, results: t.results.map((r) => (sameResult(r, next) ? next : r)) } : t)),
    )

  // Run one step's agent on demand from its details panel, ignoring dependencies.
  const runOne = (id: string) => {
    const task = tasks.find((t) => t.id === id)
    if (!task || !event) return
    setTasks((ts) => ts.map((t) => (t.id === id ? { ...t, status: 'running', results: undefined, error: undefined, shared: undefined } : t)))
    runTask(task, event, runnerCtx).then(
      (results) => {
        setTasks((ts) => ts.map((t) => (t.id === id ? { ...t, status: 'done', results } : t)))
        unblock()
      },
      (e: unknown) => failTask(id, e),
    )
  }

  // Put blocked steps back in the queue so the scheduler picks up where it stopped: it starts
  // each one whose dependencies are now done and re-blocks the rest. Before approval they go idle.
  const unblock = () => {
    setTasks((ts) => ts.map((t) => (t.status === 'blocked' ? { ...t, status: approved ? 'queued' : 'idle', blockedBy: undefined } : t)))
    if (approved) setRunning(true)
  }

  const connectGoogle = () => {
    // TODO: replace with real Google OAuth (Maps, Gmail, Calendar, Contacts, Slides scopes).
    setGoogleConnected((c) => !c)
    unblock()
  }

  // Leaving a plan detaches it from its backend event (and the /events/:id URL). The event stays saved.
  const leaveEvent = () => {
    pickSeq.current++
    resetPlan()
    setEventId(null)
    setLoadingId(null)
    setLoadError(null)
    setCreating(false)
    if (routeEventId) navigate('/')
  }

  // Already signed in: skip the form and go straight into the app.
  const enter = (mode: FormMode, plan?: PlanStart) => {
    if (!user) {
      setPending(plan ?? null)
      return setForm(mode)
    }
    setEntered(true)
    if (plan) start(plan)
  }

  // LoginPage finished: with server accounts the session token is already stored (api/auth); else it's the demo profile.
  const signedInAs = (u: User) => {
    if (!accounts) setLocalUser(u)
    setForm(null)
    setEntered(true)
    // Google sign-in also grants the Maps / Gmail / Calendar access the agents use.
    if (u.provider === 'google') setGoogleConnected(true)
    if (pending) start(pending)
    setPending(null)
  }

  const logOut = () => {
    signOut()
    setLocalUser(null)
    if (auth.token) auth.logout()
    setGoogleConnected(false)
    leaveEvent()
    setEntered(false)
  }

  // --- Render ----------------------------------------------------------------------------------------
  const pill = entered && event && !creating && !eventId ? <Pill text={auth.mode === 'offline' ? 'Offline — sample data' : 'Not saved — sample data'} /> : null

  if (!entered) {
    if (form) return <LoginPage mode={form} onMode={setForm} onDone={signedInAs} onBack={() => setForm(null)} backend={accounts} />
    return <MarketingPage onStart={() => enter('signup')} onSignIn={() => enter('signin')} onPlan={(plan) => enter('signup', plan)} />
  }
  if (auth.mode === 'loading')
    return (
      <Centered>
        <LoaderCircle className="animate-spin" size={22} />
      </Centered>
    )
  // Deep link or expired session while the server requires accounts.
  if (auth.needsSignIn)
    return (
      <LoginPage
        mode={form ?? 'signin'}
        onMode={setForm}
        onDone={signedInAs}
        onBack={() => {
          leaveEvent()
          setEntered(false)
        }}
        backend
      />
    )
  const deleted = Boolean(eventId && live.deleted)
  if (loadingId || deleted) {
    const error = deleted ? 'This event was deleted.' : auth.mode === 'offline' ? "Can't reach the Evently server to open this event." : loadError
    return (
      <Centered>
        {error ? (
          <div className="max-w-sm">
            <p className="text-sm text-ink">{error}</p>
            <button onClick={leaveEvent} className="mt-4 rounded-full bg-accent px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-500">
              Back to my events
            </button>
          </div>
        ) : (
          <LoaderCircle className="animate-spin" size={22} />
        )}
      </Centered>
    )
  }
  if (!event)
    return <Landing online={online} onPick={pick} onResume={resume} onHome={() => setEntered(false)} user={user} onSignOut={logOut} />
  if (intake)
    return (
      <>
        <IntakePage
          key={nextQuestion(answers)!.id}
          event={event}
          answers={answers}
          question={nextQuestion(answers)!}
          note={intakeNote}
          info={intakeInfo}
          extracting={extracting}
          onAnswer={answer}
          onFreeText={answerFreeText}
          prefilled={prefilled}
          onBack={intakeBack}
          onExit={leaveEvent}
        />
        {pill}
      </>
    )
  if (building) return <PlanLoader event={event} tasks={tasks} onDone={finishBuilding} />

  const unread = chatOpen ? 0 : chatItems.filter((m) => m.role === 'assistant' && m.at > seenAt).length
  const toggleChat = () => {
    setChatOpen((o) => !o)
    setSeenAt(Math.max(Date.now(), ...chatItems.map((m) => m.at)))
  }

  return (
    <main className="relative h-full bg-gray-100 p-3">
      <WorkflowCanvas
        key={planId}
        initialReview={review}
        onReviewChange={setReview}
        event={event}
        planId={planId}
        onBack={leaveEvent}
        onRunTask={runOne}
        onAddOption={addOption}
        onUpdateResult={updateResult}
        ctx={runnerCtx}
        tasks={tasks}
        selectedId={selectedId}
        googleConnected={googleConnected}
        approved={approved}
        running={running}
        onApprove={approve}
        onSelect={setSelectedId}
        onConnectGoogle={connectGoogle}
      />

      {chatOpen && (
        <div className="absolute right-6 bottom-24 z-20 h-[min(640px,calc(100%-9rem))] w-[420px] max-w-[calc(100%-3rem)] animate-rise">
          <ChatPanel event={event} messages={chatItems} busy={chatBusy} notice={chatNotice} onSend={send} onClose={toggleChat} />
        </div>
      )}
      <button
        onClick={toggleChat}
        aria-label={chatOpen ? 'Close chat' : 'Open chat'}
        className="absolute right-6 bottom-6 z-20 grid size-14 place-items-center rounded-full bg-accent text-white shadow-lg shadow-indigo-500/40 transition hover:scale-105 hover:bg-indigo-500"
      >
        {chatOpen ? <X size={22} /> : <MessageCircle size={22} />}
        {unread > 0 && (
          <span className="absolute -top-1 -right-1 grid size-5 place-items-center rounded-full bg-warn text-[11px] font-bold ring-2 ring-white">
            {unread}
          </span>
        )}
      </button>
      {pill}
    </main>
  )
}

