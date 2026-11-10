// Realtime: the planner workspace channel (WS /ws/events/{id}) and an event store kept current from it.
//
// Protocol (backend/app/api/ws_events.py): the server sends a `hello` frame first ({last_seq, presence, …}),
// then bus envelopes ({id, type, topic, seq, ts, data}). Reconnecting with `?since=<seq>` replays everything
// newer; `{"type":"resync"}` means history was lost and the client must refetch the snapshot. Envelopes with
// seq 0 are ephemeral (presence) and are never used as a resume point. Close codes 4401/4404 are final.
//
// Channels and stores are shared per event id and reference-counted, so any number of components can call
// useEventChannel / useEventStore for the same event over a single socket.
import { useEffect, useMemo, useRef, useSyncExternalStore } from 'react'
import { api, isApiError, wsUrl } from './client'
import type {
  AgentRunOut,
  BoardOut,
  CardOut,
  ChatMessageOut,
  ChatToolData,
  ControlFrame,
  EdgeOut,
  EmailMessageOut,
  EventClientMessage,
  EventHello,
  EventOut,
  EventSnapshot,
  LiveSessionOut,
  LumaEventOut,
  PresenceState,
  PresentationDetail,
  PresentationOut,
  RealtimeEvent,
  SlideOut,
} from './types'

export type ChannelStatus = 'connecting' | 'open' | 'closed'
export type ClientRef = string | number

const CLOSE_UNAUTHENTICATED = 4401
const CLOSE_NOT_FOUND = 4404
const PING_MS = 25_000
const RELEASE_DELAY_MS = 1_000

// --- Channel ---------------------------------------------------------------------------------------

export class EventChannel {
  readonly eventId: string
  status: ChannelStatus = 'closed'
  /** Newest retained seq seen; the `?since=` resume point. */
  lastSeq = 0
  /** The latest `hello` payload (null until the first one). */
  hello: EventHello['data'] | null = null
  /** Close code that stopped reconnecting (4401 not authenticated, 4404 event not found). */
  fatalCode: number | null = null

  private ws: WebSocket | null = null
  private attempt = 0
  private retryTimer: ReturnType<typeof setTimeout> | null = null
  private pingTimer: ReturnType<typeof setInterval> | null = null
  private releaseTimer: ReturnType<typeof setTimeout> | null = null
  private refs = 0
  private readonly eventListeners = new Set<(e: RealtimeEvent) => void>()
  private readonly controlListeners = new Set<(f: ControlFrame) => void>()
  private readonly statusListeners = new Set<() => void>()

  constructor(eventId: string) {
    this.eventId = eventId
  }

  onEvent(l: (e: RealtimeEvent) => void) {
    this.eventListeners.add(l)
    return () => void this.eventListeners.delete(l)
  }
  onControl(l: (f: ControlFrame) => void) {
    this.controlListeners.add(l)
    return () => void this.controlListeners.delete(l)
  }
  onStatus(l: () => void) {
    this.statusListeners.add(l)
    return () => void this.statusListeners.delete(l)
  }

  /** Send a client operation. Returns false when the socket isn't open (the op is dropped, not queued). */
  send(message: EventClientMessage & { ref?: ClientRef }): boolean {
    if (this.ws?.readyState !== WebSocket.OPEN) return false
    this.ws.send(JSON.stringify(message))
    return true
  }

  /** Reconnect now (e.g. after signing in following a 4401). */
  reconnect() {
    this.fatalCode = null
    this.attempt = 0
    this.teardown()
    if (this.refs > 0) this.connect()
  }

  retain(): () => void {
    if (!channels.has(this.eventId)) channels.set(this.eventId, this)
    this.refs++
    if (this.releaseTimer) clearTimeout(this.releaseTimer)
    this.releaseTimer = null
    if (!this.ws && !this.retryTimer && this.fatalCode === null) this.connect()
    let released = false
    return () => {
      if (released) return
      released = true
      if (--this.refs > 0) return
      // Deferred so StrictMode's mount/unmount/mount and quick route changes don't churn the socket.
      this.releaseTimer = setTimeout(() => {
        if (this.refs > 0) return
        this.teardown()
        this.setStatus('closed')
        if (channels.get(this.eventId) === this) channels.delete(this.eventId)
      }, RELEASE_DELAY_MS)
    }
  }

  private setStatus(s: ChannelStatus) {
    if (this.status === s) return
    this.status = s
    this.statusListeners.forEach((l) => l())
  }

  private connect() {
    this.setStatus('connecting')
    const path = `/ws/events/${encodeURIComponent(this.eventId)}${this.lastSeq > 0 ? `?since=${this.lastSeq}` : ''}`
    let ws: WebSocket
    try {
      ws = new WebSocket(wsUrl(path))
    } catch {
      return this.scheduleReconnect()
    }
    this.ws = ws
    let resumed = this.lastSeq > 0
    ws.onopen = () => {
      this.attempt = 0
      this.setStatus('open')
      this.pingTimer = setInterval(() => this.send({ type: 'ping' }), PING_MS)
    }
    ws.onmessage = (msg) => {
      let frame: Record<string, unknown>
      try {
        frame = JSON.parse(String(msg.data)) as Record<string, unknown>
      } catch {
        return
      }
      if (typeof frame.seq === 'number' && typeof frame.topic === 'string') {
        const e = frame as unknown as RealtimeEvent
        if (e.seq > 0) {
          if (e.seq <= this.lastSeq) return // already seen (replay overlap)
          this.lastSeq = e.seq
        }
        this.eventListeners.forEach((l) => l(e))
        return
      }
      const control = frame as unknown as ControlFrame
      if (control.type === 'hello') {
        this.hello = control.data
        // A fresh connection starts from the server's head; the snapshot covers everything before it.
        if (!resumed) this.lastSeq = control.data.last_seq
        resumed = true
      } else if (control.type === 'resync') {
        // History was lost (or the server restarted and its seqs reset): resume from the server's head.
        this.lastSeq = this.hello?.last_seq ?? 0
      }
      this.controlListeners.forEach((l) => l(control))
    }
    ws.onclose = (e) => {
      if (this.ws !== ws) return
      this.clearTimers()
      this.ws = null
      if (e.code === CLOSE_UNAUTHENTICATED || e.code === CLOSE_NOT_FOUND) {
        this.fatalCode = e.code
        this.setStatus('closed')
        return
      }
      this.scheduleReconnect()
    }
  }

  private scheduleReconnect() {
    if (this.refs <= 0) return this.setStatus('closed')
    this.setStatus('connecting')
    const delay = Math.min(10_000, 500 * 2 ** this.attempt++) * (0.8 + Math.random() * 0.4)
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null
      this.connect()
    }, delay)
  }

  private clearTimers() {
    if (this.pingTimer) clearInterval(this.pingTimer)
    if (this.retryTimer) clearTimeout(this.retryTimer)
    this.pingTimer = null
    this.retryTimer = null
  }

  private teardown() {
    this.clearTimers()
    const ws = this.ws
    this.ws = null
    if (ws) {
      ws.onopen = ws.onmessage = ws.onclose = null
      if (ws.readyState === WebSocket.CONNECTING) ws.onopen = () => ws.close(1000)
      else ws.close(1000)
    }
  }
}

const channels = new Map<string, EventChannel>()
const NOOP_SUBSCRIBE = () => () => {}

/** The shared channel for an event (created on demand). Call `retain()` to keep it connected. */
export function getEventChannel(eventId: string): EventChannel {
  let ch = channels.get(eventId)
  if (!ch) channels.set(eventId, (ch = new EventChannel(eventId)))
  return ch
}

export interface UseEventChannelOptions {
  /** hello / resync / pong / ack / error frames. */
  onControl?: (frame: ControlFrame) => void
}

export interface UseEventChannel {
  status: ChannelStatus
  /** Send a client op (card.move, card.update, presence.*). False when not connected. */
  send: (message: EventClientMessage & { ref?: ClientRef }) => boolean
  channel: EventChannel | null
}

/**
 * Subscribe to an event's realtime envelopes. `onEvent` (and `options.onControl`) live in refs, so passing new
 * closures on every render never reconnects. Pass `null` to stay disconnected.
 */
export function useEventChannel(
  eventId: string | null,
  onEvent: (e: RealtimeEvent) => void,
  options?: UseEventChannelOptions,
): UseEventChannel {
  const onEventRef = useRef(onEvent)
  const onControlRef = useRef(options?.onControl)
  useEffect(() => {
    onEventRef.current = onEvent
    onControlRef.current = options?.onControl
  })
  const channel = useMemo(() => (eventId ? getEventChannel(eventId) : null), [eventId])
  const status = useSyncExternalStore(
    useMemo(() => (channel ? (l: () => void) => channel.onStatus(l) : NOOP_SUBSCRIBE), [channel]),
    () => channel?.status ?? 'closed',
  )

  useEffect(() => {
    if (!channel) return
    const offEvent = channel.onEvent((e) => onEventRef.current(e))
    const offControl = channel.onControl((f) => onControlRef.current?.(f))
    const release = channel.retain()
    return () => {
      offEvent()
      offControl()
      release()
    }
  }, [channel])

  const send = useMemo(() => (m: EventClientMessage & { ref?: ClientRef }) => channel?.send(m) ?? false, [channel])
  return { status, send, channel }
}

// --- Store -----------------------------------------------------------------------------------------

export interface EventStoreState {
  eventId: string | null
  /** True after the first successful snapshot load. */
  loaded: boolean
  loading: boolean
  /** Load error (e.g. "Event not found"). */
  error: string | null
  channel: ChannelStatus
  event: EventOut | null
  /** Set by `event.deleted`. */
  deleted: boolean
  board: BoardOut | null
  cards: Record<string, CardOut>
  edges: Record<string, EdgeOut>
  /** Active runs from the snapshot plus every run seen on the channel since. */
  runs: Record<string, AgentRunOut>
  /** Oldest first; assistant messages with status 'streaming' have `chat.delta` text appended live. */
  messages: ChatMessageOut[]
  /** `chat.tool` activity per assistant message id. */
  tools: Record<string, ChatToolData[]>
  presentations: Record<string, PresentationOut>
  slides: Record<string, SlideOut>
  emails: Record<string, EmailMessageOut>
  luma: LumaEventOut | null
  /** The newest live session for this event. */
  session: LiveSessionOut | null
  liveSessions: Record<string, LiveSessionOut>
  /** Other clients looking at the board, by client id. */
  presence: Record<string, PresenceState>
}

export const EMPTY_EVENT_STATE: EventStoreState = {
  eventId: null,
  loaded: false,
  loading: false,
  error: null,
  channel: 'closed',
  event: null,
  deleted: false,
  board: null,
  cards: {},
  edges: {},
  runs: {},
  messages: [],
  tools: {},
  presentations: {},
  slides: {},
  emails: {},
  luma: null,
  session: null,
  liveSessions: {},
  presence: {},
}

const TERMINAL_RUN = new Set(['succeeded', 'failed', 'cancelled'])
export const isRunFinished = (run: Pick<AgentRunOut, 'status'>) => TERMINAL_RUN.has(run.status)

const omit = <T>(rec: Record<string, T>, id: string): Record<string, T> => {
  if (!(id in rec)) return rec
  const next = { ...rec }
  delete next[id]
  return next
}
const byId = <T extends { id: string }>(rows: T[]): Record<string, T> => Object.fromEntries(rows.map((r) => [r.id, r]))
const newer = <T extends { version: number }>(prev: T | undefined, next: T) => !prev || next.version >= prev.version
const newestSession = (sessions: Record<string, LiveSessionOut>): LiveSessionOut | null =>
  Object.values(sessions).reduce<LiveSessionOut | null>((a, b) => (!a || b.created_at > a.created_at ? b : a), null)

/** Pure reducer: apply one envelope to the store state. Exported so features can reuse it. */
export function applyRealtimeEvent(s: EventStoreState, e: RealtimeEvent): EventStoreState {
  switch (e.type) {
    case 'event.updated':
      return { ...s, event: e.data.event }
    case 'event.deleted':
      return { ...s, deleted: true }

    case 'chat.message.created':
    case 'chat.message.completed': {
      const m = e.data.message
      const i = s.messages.findIndex((x) => x.id === m.id)
      const messages = i < 0 ? [...s.messages, m] : s.messages.map((x, j) => (j === i ? m : x))
      return { ...s, messages }
    }
    case 'chat.delta': {
      const { message_id, text } = e.data
      if (!s.messages.some((m) => m.id === message_id)) return s
      return {
        ...s,
        messages: s.messages.map((m) => (m.id === message_id && m.status === 'streaming' ? { ...m, content: m.content + text } : m)),
      }
    }
    case 'chat.tool':
      return { ...s, tools: { ...s.tools, [e.data.message_id]: [...(s.tools[e.data.message_id] ?? []), e.data] } }
    case 'chat.error': {
      const { message_id, error } = e.data
      return {
        ...s,
        messages: s.messages.map((m) => (m.id === message_id ? { ...m, status: 'error', meta: { ...m.meta, error } } : m)),
      }
    }

    case 'agent.run.created':
    case 'agent.run.updated':
      return { ...s, runs: { ...s.runs, [e.data.run.id]: e.data.run } }

    case 'board.updated':
      return { ...s, board: e.data.board }
    case 'board.card.created':
    case 'board.card.updated': {
      const c = e.data.card
      return newer(s.cards[c.id], c) ? { ...s, cards: { ...s.cards, [c.id]: c } } : s
    }
    case 'board.card.deleted': {
      const id = e.data.card_id
      const edges = Object.fromEntries(
        Object.entries(s.edges).filter(([, x]) => x.source_card_id !== id && x.target_card_id !== id),
      )
      return { ...s, cards: omit(s.cards, id), edges }
    }
    case 'board.edge.created':
      return { ...s, edges: { ...s.edges, [e.data.edge.id]: e.data.edge } }
    case 'board.edge.deleted':
      return { ...s, edges: omit(s.edges, e.data.edge_id) }
    case 'board.presence': {
      const d = e.data
      if ('left' in d) return { ...s, presence: omit(s.presence, d.client_id) }
      return { ...s, presence: { ...s.presence, [d.client_id]: d } }
    }

    case 'presentation.created':
    case 'presentation.updated': {
      const p = e.data.presentation
      return newer(s.presentations[p.id], p) ? { ...s, presentations: { ...s.presentations, [p.id]: p } } : s
    }
    case 'presentation.deleted': {
      const id = e.data.presentation_id
      const slides = Object.fromEntries(Object.entries(s.slides).filter(([, x]) => x.presentation_id !== id))
      return { ...s, presentations: omit(s.presentations, id), slides }
    }
    case 'slide.created':
    case 'slide.updated': {
      const sl = e.data.slide
      return newer(s.slides[sl.id], sl) ? { ...s, slides: { ...s.slides, [sl.id]: sl } } : s
    }
    case 'slide.deleted':
      return { ...s, slides: omit(s.slides, e.data.slide_id) }
    case 'slides.reordered': {
      const slides = { ...s.slides }
      e.data.slide_ids.forEach((id, position) => {
        if (slides[id]) slides[id] = { ...slides[id], position }
      })
      return { ...s, slides }
    }

    case 'email.updated':
      return { ...s, emails: { ...s.emails, [e.data.email.id]: e.data.email } }
    case 'email.deleted':
      return { ...s, emails: omit(s.emails, e.data.email_id) }
    case 'luma.updated':
      return { ...s, luma: e.data.luma }

    case 'session.updated': {
      const liveSessions = { ...s.liveSessions, [e.data.session.id]: e.data.session }
      return { ...s, liveSessions, session: newestSession(liveSessions) }
    }
    default:
      // Other session.* envelopes belong to the live-session socket (WS /ws/sessions/{id}).
      return s
  }
}

/** Sorted slides of one presentation. */
export const slidesOf = (s: Pick<EventStoreState, 'slides'>, presentationId: string): SlideOut[] =>
  Object.values(s.slides)
    .filter((x) => x.presentation_id === presentationId)
    .sort((a, b) => a.position - b.position)

export interface WaitOptions {
  timeoutMs?: number
  signal?: AbortSignal
}

/** Shared, reference-counted state for one event. Usable outside React (task runners) via getState/subscribe. */
export class EventStore {
  readonly eventId: string
  private state: EventStoreState
  private readonly listeners = new Set<() => void>()
  private readonly eventListeners = new Set<(e: RealtimeEvent) => void>()
  private pending: RealtimeEvent[] | null = null
  private loadSeq = 0
  private refs = 0
  private stop: (() => void) | null = null
  private releaseTimer: ReturnType<typeof setTimeout> | null = null

  constructor(eventId: string) {
    this.eventId = eventId
    this.state = { ...EMPTY_EVENT_STATE, eventId }
  }

  /** The shared socket for this event. */
  get channel(): EventChannel {
    return getEventChannel(this.eventId)
  }

  getState = (): EventStoreState => this.state
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => void this.listeners.delete(listener)
  }
  /** Raw envelopes, after they were applied to the state. */
  onEvent(listener: (e: RealtimeEvent) => void): () => void {
    this.eventListeners.add(listener)
    return () => void this.eventListeners.delete(listener)
  }
  /** Client ops over the shared socket. */
  send = (message: EventClientMessage & { ref?: ClientRef }): boolean => this.channel.send(message)

  /** Replace part of the state locally (optimistic UI). The next envelope for the same object wins. */
  patch(fn: (s: EventStoreState) => EventStoreState) {
    this.set(fn(this.state))
  }

  private set(next: EventStoreState) {
    if (next === this.state) return
    this.state = next
    this.listeners.forEach((l) => l())
  }

  /** Keep the store live (connect + load). Returns the release function. */
  retain(): () => void {
    if (!stores.has(this.eventId)) stores.set(this.eventId, this)
    this.refs++
    if (this.releaseTimer) clearTimeout(this.releaseTimer)
    this.releaseTimer = null
    if (!this.stop) this.start()
    let released = false
    return () => {
      if (released) return
      released = true
      if (--this.refs > 0) return
      this.releaseTimer = setTimeout(() => {
        if (this.refs > 0) return
        this.stop?.()
        this.stop = null
        if (stores.get(this.eventId) === this) stores.delete(this.eventId)
      }, RELEASE_DELAY_MS)
    }
  }

  private start() {
    const ch = this.channel
    let helloSeen = false
    const offEvent = ch.onEvent((e) => {
      if (this.pending) this.pending.push(e)
      else this.set(applyRealtimeEvent(this.state, e))
      this.eventListeners.forEach((l) => l(e))
    })
    const offControl = ch.onControl((f) => {
      // Load once the socket has told us its head (so nothing falls between snapshot and stream), and again
      // whenever the server says history was lost.
      if ((f.type === 'hello' && !helloSeen) || f.type === 'resync') void this.reload()
      if (f.type === 'hello') {
        helloSeen = true
        this.set({ ...this.state, presence: Object.fromEntries(f.data.presence.map((p) => [p.client_id, p])) })
      }
    })
    const offStatus = ch.onStatus(() => {
      this.set({ ...this.state, channel: ch.status })
      // Socket refused (4401/4404) or not up yet: load over REST anyway so the UI gets data or a real error.
      if (ch.status === 'closed' && !this.state.loaded && !this.state.loading) void this.reload()
    })
    const release = ch.retain()
    this.set({ ...this.state, channel: ch.status })
    // If the socket can't connect (proxy missing, backend without WS), still paint from REST.
    const fallback = setTimeout(() => {
      if (!helloSeen && !this.state.loaded && !this.state.loading) void this.reload()
    }, 1500)
    this.stop = () => {
      clearTimeout(fallback)
      offEvent()
      offControl()
      offStatus()
      release()
    }
  }

  /** Refetch everything from REST, then re-apply envelopes that arrived meanwhile. Never rejects: see `error`. */
  async reload(): Promise<void> {
    const seq = ++this.loadSeq
    this.pending ??= []
    this.set({ ...this.state, loading: true })
    const id = encodeURIComponent(this.eventId)
    try {
      const [snap, messages, emails, luma] = await Promise.all([
        api.get<EventSnapshot>(`/events/${id}/snapshot`),
        api.get<ChatMessageOut[]>(`/events/${id}/chat/messages`).catch(() => [] as ChatMessageOut[]),
        api.get<EmailMessageOut[]>(`/events/${id}/emails`).catch(() => [] as EmailMessageOut[]),
        api.get<LumaEventOut>(`/events/${id}/luma`).catch(() => null),
      ])
      const details = await Promise.all(
        snap.presentations.map((p) =>
          api.get<PresentationDetail>(`/presentations/${encodeURIComponent(p.id)}`).catch(() => null),
        ),
      )
      if (seq !== this.loadSeq) return
      const liveSessions = byId(snap.live_sessions)
      let next: EventStoreState = {
        ...this.state,
        loaded: true,
        loading: false,
        error: null,
        event: snap.event,
        deleted: false,
        board: snap.board,
        cards: byId(snap.cards),
        edges: byId(snap.edges),
        // Keep finished runs we already know about; the snapshot only lists active ones.
        runs: { ...this.state.runs, ...byId(snap.active_agent_runs) },
        messages,
        presentations: byId(snap.presentations),
        slides: byId(details.flatMap((d) => d?.slides ?? [])),
        emails: byId(emails),
        luma,
        liveSessions,
        session: newestSession(liveSessions),
      }
      for (const e of this.pending ?? []) next = applyRealtimeEvent(next, e)
      this.pending = null
      this.set(next)
    } catch (e) {
      if (seq !== this.loadSeq) return
      let next = this.state
      for (const ev of this.pending ?? []) next = applyRealtimeEvent(next, ev)
      this.pending = null
      const error = isApiError(e) ? e.message : e instanceof Error ? e.message : 'Could not load the event.'
      this.set({ ...next, loading: false, error })
    }
  }

  /** Resolve with the first truthy value of `select(state)`, re-checked on every change. */
  waitFor<T>(select: (s: EventStoreState) => T | null | undefined | false, opts: WaitOptions = {}): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const check = () => {
        const v = select(this.state)
        if (v) {
          done()
          resolve(v)
        }
      }
      const off = this.subscribe(check)
      const timer = opts.timeoutMs ? setTimeout(() => (done(), reject(new Error('Timed out waiting for the event store'))), opts.timeoutMs) : null
      const onAbort = () => (done(), reject(opts.signal?.reason ?? new DOMException('Aborted', 'AbortError')))
      opts.signal?.addEventListener('abort', onAbort)
      function done() {
        off()
        if (timer) clearTimeout(timer)
        opts.signal?.removeEventListener('abort', onAbort)
      }
      check()
    })
  }

  /**
   * Resolve with the run once it reaches succeeded / failed / cancelled. Uses the channel, and polls
   * GET /agent-runs/{id} every few seconds as a fallback while the socket isn't open.
   */
  async waitForRun(runId: string, opts: WaitOptions = {}): Promise<AgentRunOut> {
    const release = this.retain()
    const fetchRun = async () => {
      try {
        const run = await api.get<AgentRunOut>(`/agent-runs/${encodeURIComponent(runId)}`)
        const known = this.state.runs[runId]
        if (!known || !isRunFinished(known)) this.set({ ...this.state, runs: { ...this.state.runs, [runId]: run } })
      } catch {
        // Transient; the next poll or envelope will catch up.
      }
    }
    const poll = setInterval(() => {
      if (this.channel.status !== 'open') void fetchRun()
    }, 4000)
    void fetchRun()
    try {
      return await this.waitFor((s) => {
        const run = s.runs[runId]
        return run && isRunFinished(run) ? run : null
      }, opts)
    } finally {
      clearInterval(poll)
      release()
    }
  }
}

const stores = new Map<string, EventStore>()

/** The shared store for an event (created on demand). Call `retain()` to keep it loaded and live. */
export function getEventStore(eventId: string): EventStore {
  let s = stores.get(eventId)
  if (!s) stores.set(eventId, (s = new EventStore(eventId)))
  return s
}

export type EventStoreView = EventStoreState & {
  /** The live store (null when eventId is null); hand it to runners or non-React code. */
  store: EventStore | null
  reload: () => Promise<void>
}

const EMPTY = () => EMPTY_EVENT_STATE

/** Live state for one event: snapshot on load, then kept current from WS /ws/events/{id}. */
export function useEventStore(eventId: string | null): EventStoreView {
  const store = useMemo(() => (eventId ? getEventStore(eventId) : null), [eventId])
  useEffect(() => store?.retain(), [store])
  const state = useSyncExternalStore(store?.subscribe ?? NOOP_SUBSCRIBE, store?.getState ?? EMPTY)
  return useMemo(
    () => ({ ...state, store, reload: () => store?.reload() ?? Promise.resolve() }),
    [state, store],
  )
}
