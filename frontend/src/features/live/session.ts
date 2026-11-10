// REST helpers, token storage and shared types for live sessions (presenter + audience pages).
import { api, apiUrl } from '../../api/client'
import type { Joined, LiveSessionOut, ParticipantAdmitted, ParticipantOut, SessionState, TranscriptSegmentOut } from '../../api/types'

export type { Joined, LiveSessionOut, ParticipantOut, SessionState, TranscriptSegmentOut }

export type AutoAdvance = 'off' | 'commands' | 'keywords' | 'script'
export const STT_PROVIDERS = ['auto', 'browser', 'deepgram', 'assemblyai', 'openai', 'mock'] as const

/** `SessionSettings` with the backend defaults applied (stored settings are a loose dict). */
export interface LiveSettings {
  auto_advance: AutoAdvance
  stt_provider: string
  language: string
  advance_cooldown_ms: number
  audio_broadcast: boolean
  show_transcript_to_listeners: boolean
}

export function readSettings(raw: Record<string, unknown> | null | undefined): LiveSettings {
  const s = raw ?? {}
  return {
    auto_advance: (['off', 'commands', 'keywords', 'script'].includes(s.auto_advance as string) ? s.auto_advance : 'keywords') as AutoAdvance,
    stt_provider: typeof s.stt_provider === 'string' ? s.stt_provider : 'auto',
    language: typeof s.language === 'string' && s.language ? s.language : 'en',
    advance_cooldown_ms: typeof s.advance_cooldown_ms === 'number' ? s.advance_cooldown_ms : 2500,
    audio_broadcast: s.audio_broadcast !== false,
    show_transcript_to_listeners: s.show_transcript_to_listeners !== false,
  }
}

// --- tokens -------------------------------------------------------------------------

/** Where the deck viewer stores the host token when it creates a session. */
export const hostTokenKey = (sessionId: string) => `evently.session.${sessionId}.host_token`
const joinKey = (code: string) => `evently.join.${code.toUpperCase()}`
const NAME_KEY = 'evently.join.name'

function storageGet(store: () => Storage, key: string): string | null {
  try {
    return store().getItem(key)
  } catch {
    return null
  }
}
function storageSet(store: () => Storage, key: string, value: string | null) {
  try {
    if (value === null) store().removeItem(key)
    else store().setItem(key, value)
  } catch {
    // storage unavailable (private mode): the page still works for this load
  }
}
const session = () => sessionStorage
const local = () => localStorage

export const readHostToken = (sessionId: string) => storageGet(session, hostTokenKey(sessionId)) ?? storageGet(local, hostTokenKey(sessionId))
export const storeHostToken = (sessionId: string, token: string) => storageSet(session, hostTokenKey(sessionId), token)
export const clearHostToken = (sessionId: string) => storageSet(session, hostTokenKey(sessionId), null)

/** A previous join of this code in this tab, so a reload reconnects instead of re-joining. */
export function readJoin(code: string): Joined | null {
  try {
    return JSON.parse(storageGet(session, joinKey(code)) ?? 'null') as Joined | null
  } catch {
    return null
  }
}
export const storeJoin = (code: string, joined: Joined | null) => storageSet(session, joinKey(code), joined ? JSON.stringify(joined) : null)
export const readName = () => storageGet(local, NAME_KEY) ?? ''
export const storeName = (name: string) => storageSet(local, NAME_KEY, name)

// --- REST -----------------------------------------------------------------------------

/** Request options that authenticate with a session (host/participant) token instead of the user token. */
export const withToken = (token: string) => ({ auth: false, headers: { Authorization: `Bearer ${token}` } })

export const fetchState = (sessionId: string, token: string) => api.get<SessionState>(`/live-sessions/${sessionId}/state`, withToken(token))

export const joinSession = (code: string, displayName: string) =>
  api.post<Joined>('/live-sessions/join', { join_code: code, display_name: displayName }, { auth: false })

/** Owner route: mint another host token for a session (used when this tab has none stored). */
export const mintHostToken = (sessionId: string, displayName: string) =>
  api.post<ParticipantAdmitted>(`/live-sessions/${sessionId}/participants`, { display_name: displayName, role: 'host' })

// Owner routes (user access token / dev user).
export const startSession = (id: string) => api.post<LiveSessionOut>(`/live-sessions/${id}/start`)
export const endSession = (id: string) => api.post<LiveSessionOut>(`/live-sessions/${id}/end`)
export const patchSettings = (id: string, settings: Partial<LiveSettings>) => api.patch<LiveSessionOut>(`/live-sessions/${id}`, { settings })

/** The deck as an HTML page that follows `evently:goto` (participant/host token in the query: iframes can't send headers). */
export const deckUrl = (presentationId: string, token: string, slide: number) =>
  apiUrl(`/presentations/${presentationId}/render`, { slide: Math.max(0, slide), controls: false, token })

// --- misc -------------------------------------------------------------------------------

/** Exponential backoff with jitter: 0.5 s, 1 s, 2 s … capped at `max`. */
export function backoff(attempt: number, max = 15000): number {
  const base = Math.min(max, 500 * 2 ** attempt)
  return Math.round(base * (0.75 + Math.random() * 0.5))
}

export const joinLink = (code: string) => `${location.origin}/join/${code}`
