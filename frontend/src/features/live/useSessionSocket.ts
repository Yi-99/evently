// Control socket of a live session: WS /ws/sessions/{id}?token=<host or participant token>.
// Keeps a reduced view of the session (state snapshot + live envelopes), reconnects with backoff
// and refetches GET /live-sessions/{id}/state on `resync`.
import { useCallback, useEffect, useReducer, useRef } from 'react'
import { wsUrl } from '../../api/client'
import type { RealtimeEvent } from '../../api/types'
import { backoff, fetchState, type LiveSessionOut, type ParticipantOut, type SessionState, type TranscriptSegmentOut } from './session'

export type SocketStatus = 'connecting' | 'open' | 'reconnecting' | 'closed'

export interface SessionClientInfo {
  role: 'host' | 'speaker' | 'listener' | string
  participant_id: string | null
  display_name: string
}

export interface SlideChange {
  index: number
  previous_index: number
  reason: string
  matched: string | null
  by: string | null
  /** Envelope timestamp (ISO). */
  ts: string
  /** Local receive time, for highlight animations. */
  at: number
}

const REASON_LABEL: Record<string, string> = { command: 'voice command', keyword: 'keyword', script: 'script', manual: 'manual' }
/** "keyword “budget”", "voice command “next slide”"… */
export const describeChange = (c: SlideChange) => `${REASON_LABEL[c.reason] ?? c.reason}${c.matched ? ` “${c.matched}”` : ''}`
/** Moved by speech (command / keyword / script) rather than a click. */
export const isAutoChange = (c: SlideChange) => c.reason !== 'manual'

export interface AudioState {
  broadcasting: boolean
  source_participant_id: string | null
  listeners: number
  codec: string | null
  sample_rate: number | null
}

export interface SttState {
  provider: string
  status: 'connecting' | 'active' | 'error' | 'stopped' | string
  error: string | null
}

export interface SessionView {
  status: SocketStatus
  /** Fatal error (token rejected, unknown session) or the last server `error` frame. */
  error: string | null
  client: SessionClientInfo | null
  session: LiveSessionOut | null
  participants: ParticipantOut[]
  slideCount: number
  slides: SessionState['slides']
  transcript: TranscriptSegmentOut[]
  partial: { text: string; slide_index: number | null } | null
  slideChanges: SlideChange[]
  audio: AudioState | null
  stt: SttState | null
}

const INITIAL: SessionView = {
  status: 'connecting',
  error: null,
  client: null,
  session: null,
  participants: [],
  slideCount: 0,
  slides: [],
  transcript: [],
  partial: null,
  slideChanges: [],
  audio: null,
  stt: null,
}

const MAX_TRANSCRIPT = 500
const MAX_SLIDE_CHANGES = 50
const PING_MS = 25_000
/** Close codes after which reconnecting cannot help. */
const FATAL: Record<number, string> = { 4401: 'This link has expired or the token was rejected.', 4404: 'This live session no longer exists.' }

type Action =
  | { type: 'status'; status: SocketStatus; error?: string | null }
  | { type: 'hello'; client: SessionClientInfo; state: SessionState }
  | { type: 'state'; state: SessionState }
  | { type: 'event'; event: RealtimeEvent }
  | { type: 'error'; message: string }
  | { type: 'session'; session: LiveSessionOut }

function mergeSegments(a: TranscriptSegmentOut[], b: TranscriptSegmentOut[]): TranscriptSegmentOut[] {
  if (!b.length) return a
  const bySeq = new Map<number, TranscriptSegmentOut>()
  for (const s of a) bySeq.set(s.seq, s)
  for (const s of b) bySeq.set(s.seq, s)
  return [...bySeq.values()].sort((x, y) => x.seq - y.seq).slice(-MAX_TRANSCRIPT)
}

function applyState(s: SessionView, state: SessionState): SessionView {
  return {
    ...s,
    session: state.session,
    participants: state.participants,
    slideCount: state.slide_count,
    slides: state.slides,
    transcript: mergeSegments(s.transcript, state.recent_transcript),
  }
}

function reduce(s: SessionView, a: Action): SessionView {
  switch (a.type) {
    case 'status':
      return { ...s, status: a.status, error: a.error === undefined ? s.error : a.error }
    case 'hello':
      return { ...applyState(s, a.state), client: a.client, status: 'open', error: null }
    case 'state':
      return applyState(s, a.state)
    case 'error':
      return { ...s, error: a.message }
    case 'session':
      return { ...s, session: a.session }
    case 'event':
      return reduceEvent(s, a.event)
  }
}

function reduceEvent(s: SessionView, e: RealtimeEvent): SessionView {
  switch (e.type) {
    case 'session.updated':
      return { ...s, session: e.data.session }
    case 'session.slide.changed': {
      const change: SlideChange = { ...e.data, ts: e.ts, at: Date.now() }
      return {
        ...s,
        session: s.session ? { ...s.session, current_slide_index: e.data.index } : s.session,
        slideChanges: [...s.slideChanges, change].slice(-MAX_SLIDE_CHANGES),
      }
    }
    case 'session.transcript.partial':
      return { ...s, partial: e.data.text.trim() ? e.data : null }
    case 'session.transcript.final':
      return { ...s, partial: null, transcript: mergeSegments(s.transcript, [e.data.segment]) }
    case 'session.participant.joined': {
      const p = e.data.participant
      const rest = s.participants.filter((x) => x.id !== p.id)
      return { ...s, participants: [...rest, p] }
    }
    case 'session.participant.left':
      return {
        ...s,
        participants: s.participants.map((p) => (p.id === e.data.participant_id ? { ...p, connected: false } : p)),
      }
    case 'session.audio.state':
      return { ...s, audio: e.data }
    case 'session.stt.state':
      return { ...s, stt: e.data }
    default:
      return s
  }
}

export type ControlMessage =
  | { type: 'slide.goto'; data: { index: number } }
  | { type: 'slide.next' }
  | { type: 'slide.prev' }
  | { type: 'transcript.text'; data: { text: string; is_final: boolean } }

export interface UseSessionSocket extends SessionView {
  /** Send a control message; returns false when the socket is not open. */
  send: (message: ControlMessage) => boolean
  /** Refetch the state snapshot (also done automatically on `resync`). */
  refresh: () => Promise<void>
  /** Apply a session returned by a REST call (start/end/settings) without waiting for its `session.updated`. */
  applySession: (session: LiveSessionOut) => void
}

export function useSessionSocket(sessionId: string, token: string | null): UseSessionSocket {
  const [view, dispatch] = useReducer(reduce, INITIAL)
  const wsRef = useRef<WebSocket | null>(null)

  const refresh = useCallback(async () => {
    if (!token) return
    try {
      dispatch({ type: 'state', state: await fetchState(sessionId, token) })
    } catch (e) {
      dispatch({ type: 'error', message: e instanceof Error ? e.message : 'Could not load the session' })
    }
  }, [sessionId, token])

  useEffect(() => {
    if (!token) return
    let disposed = false
    let attempt = 0
    let retryTimer: ReturnType<typeof setTimeout> | undefined
    let pingTimer: ReturnType<typeof setInterval> | undefined

    const connect = () => {
      if (disposed) return
      const ws = new WebSocket(wsUrl(`/ws/sessions/${encodeURIComponent(sessionId)}`, token))
      wsRef.current = ws
      ws.onopen = () => {
        pingTimer = setInterval(() => ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify({ type: 'ping' })), PING_MS)
      }
      ws.onmessage = (msg) => {
        if (typeof msg.data !== 'string') return
        let frame: { type?: string; data?: unknown }
        try {
          frame = JSON.parse(msg.data)
        } catch {
          return
        }
        switch (frame.type) {
          case 'hello': {
            attempt = 0
            const data = frame.data as { client: SessionClientInfo; state: SessionState }
            dispatch({ type: 'hello', client: data.client, state: data.state })
            return
          }
          case 'resync':
            void fetchState(sessionId, token)
              .then((state) => !disposed && dispatch({ type: 'state', state }))
              .catch(() => undefined)
            return
          case 'pong':
            return
          case 'error':
            dispatch({ type: 'error', message: (frame.data as { message?: string })?.message ?? 'Server error' })
            return
          default:
            if (frame.type?.startsWith('session.')) dispatch({ type: 'event', event: frame as RealtimeEvent })
        }
      }
      ws.onclose = (ev) => {
        clearInterval(pingTimer)
        if (wsRef.current === ws) wsRef.current = null
        if (disposed) return
        const fatal = FATAL[ev.code]
        if (fatal) {
          dispatch({ type: 'status', status: 'closed', error: fatal })
          return
        }
        dispatch({ type: 'status', status: 'reconnecting' })
        retryTimer = setTimeout(connect, backoff(attempt++))
      }
    }
    connect()

    // Phones drop sockets in the background; reconnect right away when the page is visible again.
    const onVisible = () => {
      if (document.visibilityState !== 'visible' || disposed) return
      if (!wsRef.current && retryTimer !== undefined) {
        clearTimeout(retryTimer)
        attempt = 0
        connect()
      }
    }
    document.addEventListener('visibilitychange', onVisible)

    return () => {
      disposed = true
      clearTimeout(retryTimer)
      clearInterval(pingTimer)
      document.removeEventListener('visibilitychange', onVisible)
      const ws = wsRef.current
      wsRef.current = null
      if (ws) {
        ws.onclose = null
        ws.close(1000)
      }
    }
  }, [sessionId, token])

  const send = useCallback((message: ControlMessage) => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return false
    ws.send(JSON.stringify(message))
    return true
  }, [])

  const applySession = useCallback((session: LiveSessionOut) => dispatch({ type: 'session', session }), [])

  return { ...view, send, refresh, applySession }
}
