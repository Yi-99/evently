// Friendly names for the generated OpenAPI types, plus the realtime protocol (which OpenAPI can't describe).
// Regenerate schema.d.ts with `npm run api:types`; never edit it by hand.
import type { components, paths } from './schema'

export type { components, paths }
export type Schemas = components['schemas']

// --- REST models ---------------------------------------------------------------
export type UserOut = Schemas['UserOut']
export type TokenOut = Schemas['TokenOut']
export type EventOut = Schemas['EventOut']
export type EventCreate = Schemas['EventCreate']
export type EventUpdate = Schemas['EventUpdate']
export type EventSnapshot = Schemas['EventSnapshot']
export type RequirementsPatch = Schemas['RequirementsPatch']
export type VenueCriteria = Schemas['VenueCriteria']

export type ChatMessageIn = Schemas['ChatMessageIn']
export type ChatMessageOut = Schemas['ChatMessageOut']
export type ChatTurnOut = Schemas['ChatTurnOut']

export type AgentRunOut = Schemas['AgentRunOut']
export type RunAccepted = Schemas['RunAccepted']
export type RunStarted = Schemas['RunStarted']

export type BoardOut = Schemas['BoardOut']
export type BoardSnapshot = Schemas['BoardSnapshot']
export type BoardPatch = Schemas['BoardPatch']
export type Viewport = Schemas['Viewport']
export type CardOut = Schemas['CardOut']
export type CardCreate = Schemas['CardCreate']
export type CardPatch = Schemas['CardPatch']
export type CardMove = Schemas['CardMove']
export type CardPrompt = Schemas['CardPrompt']
export type EdgeOut = Schemas['EdgeOut']
export type EdgeCreate = Schemas['EdgeCreate']

export type PresentationOut = Schemas['PresentationOut']
export type PresentationDetail = Schemas['PresentationDetail']
export type PresentationBrief = Schemas['PresentationBrief']
export type PresentationPatch = Schemas['PresentationPatch']
export type PresentationStarted = Schemas['PresentationStarted']
export type SlideOut = Schemas['SlideOut']
export type SlideCreate = Schemas['SlideCreate']
export type SlidePatch = Schemas['SlidePatch']
export type SlideSummary = Schemas['SlideSummary']

export type LiveSessionOut = Schemas['LiveSessionOut']
export type SessionCreate = Schemas['SessionCreate']
export type SessionCreated = Schemas['SessionCreated']
export type SessionPatch = Schemas['SessionPatch']
export type SessionSettings = Schemas['SessionSettings']
export type SessionState = Schemas['SessionState']
export type ParticipantOut = Schemas['ParticipantOut']
export type ParticipantAdmitted = Schemas['ParticipantAdmitted']
export type JoinBody = Schemas['JoinBody']
export type Joined = Schemas['Joined']
export type TranscriptSegmentOut = Schemas['TranscriptSegmentOut']
export type LiveKitTokenOut = Schemas['LiveKitTokenOut']

export type EmailMessageOut = Schemas['EmailMessageOut']
export type EmailDraftIn = Schemas['EmailDraftIn']
export type EmailPatch = Schemas['EmailPatch']
export type LumaEventOut = Schemas['LumaEventOut']
export type LumaEventDraft = Schemas['LumaEventDraft']
export type LumaDraftRequest = Schemas['LumaDraftRequest']
export type LumaInviteRequest = Schemas['LumaInviteRequest']

/** `GET /api/v1/health` (the endpoint returns a plain dict, so this is hand-written). */
export interface HealthOut {
  status: string
  environment: string
  llm_configured: boolean
  /** Providers configured + purpose routing, when the LLM router is active. */
  llm: Record<string, unknown> | null
  maps_configured: boolean
  stt_keys: { deepgram: boolean; assemblyai: boolean; openai: boolean }
  livekit_configured: boolean
  active_agent_runs: number
}

// --- Realtime ------------------------------------------------------------------

/** A bus envelope, identical on WS and SSE. `seq` is 0 for ephemeral envelopes (presence): never resume from those. */
export interface Envelope<T = Record<string, unknown>, K extends string = string> {
  id: string
  type: K
  topic: string
  seq: number
  ts: string
  data: T
  trace_id?: string | null
}

type Ev<K extends string, T> = Envelope<T, K>

export interface PresenceUser {
  id: string
  name: string
}
/** `board.presence` for a join / cursor move / selection change. */
export interface PresenceState {
  client_id: string
  user: PresenceUser
  color: string
  cursor: { x: number; y: number } | null
  selection: string[]
}
/** `board.presence` when a client disconnects. */
export interface PresenceLeft {
  client_id: string
  left: true
}

export interface ChatToolData {
  message_id: string
  name: string
  status: 'started' | 'finished'
  summary: string
  is_error: boolean
}

export type CardActor = 'user' | 'agent'

export type RealtimeEvent =
  // event
  | Ev<'event.updated', { event: EventOut }>
  | Ev<'event.deleted', { event_id: string }>
  // chat
  | Ev<'chat.message.created', { message: ChatMessageOut }>
  | Ev<'chat.delta', { message_id: string; text: string }>
  | Ev<'chat.tool', ChatToolData>
  | Ev<'chat.message.completed', { message: ChatMessageOut }>
  | Ev<'chat.error', { message_id: string; error: string }>
  // agent runs
  | Ev<'agent.run.created', { run: AgentRunOut }>
  | Ev<'agent.run.updated', { run: AgentRunOut }>
  // board
  | Ev<'board.updated', { board: BoardOut }>
  | Ev<'board.card.created', { card: CardOut }>
  | Ev<'board.card.updated', { card: CardOut; changed: string[]; by: CardActor }>
  | Ev<'board.card.deleted', { card_id: string }>
  | Ev<'board.edge.created', { edge: EdgeOut }>
  | Ev<'board.edge.deleted', { edge_id: string }>
  | Ev<'board.presence', PresenceState | PresenceLeft>
  // presentations
  | Ev<'presentation.created', { presentation: PresentationOut }>
  | Ev<'presentation.updated', { presentation: PresentationOut }>
  | Ev<'presentation.deleted', { presentation_id: string }>
  | Ev<'slide.created', { slide: SlideOut }>
  | Ev<'slide.updated', { slide: SlideOut }>
  | Ev<'slide.deleted', { slide_id: string; presentation_id: string }>
  | Ev<'slides.reordered', { presentation_id: string; slide_ids: string[] }>
  // outreach
  | Ev<'email.updated', { email: EmailMessageOut }>
  | Ev<'email.deleted', { email_id: string }>
  | Ev<'luma.updated', { luma: LumaEventOut }>
  // live sessions (topic session:{id}, delivered on WS /ws/sessions/{id})
  | Ev<'session.updated', { session: LiveSessionOut }>
  | Ev<
      'session.slide.changed',
      { index: number; previous_index: number; reason: 'manual' | 'command' | 'keyword' | 'script' | string; matched: string | null; by: string | null }
    >
  | Ev<'session.transcript.partial', { text: string; slide_index: number | null }>
  | Ev<'session.transcript.final', { segment: TranscriptSegmentOut }>
  | Ev<'session.participant.joined', { participant: ParticipantOut }>
  | Ev<'session.participant.left', { participant_id: string }>
  | Ev<
      'session.audio.state',
      { broadcasting: boolean; source_participant_id: string | null; listeners: number; codec: string | null; sample_rate: number | null }
    >
  | Ev<'session.stt.state', { provider: string; status: 'connecting' | 'active' | 'error' | 'stopped'; error: string | null }>

export type RealtimeEventType = RealtimeEvent['type']
/** The envelope for one event type, e.g. `EventOf<'board.card.updated'>`. */
export type EventOf<K extends RealtimeEventType> = Extract<RealtimeEvent, { type: K }>

/** Every realtime event type the backend defines (mirrors backend/app/realtime/events.py). */
export const REALTIME_EVENT_TYPES = [
  'event.updated',
  'event.deleted',
  'chat.message.created',
  'chat.delta',
  'chat.tool',
  'chat.message.completed',
  'chat.error',
  'agent.run.created',
  'agent.run.updated',
  'board.updated',
  'board.card.created',
  'board.card.updated',
  'board.card.deleted',
  'board.edge.created',
  'board.edge.deleted',
  'board.presence',
  'presentation.created',
  'presentation.updated',
  'presentation.deleted',
  'slide.created',
  'slide.updated',
  'slide.deleted',
  'slides.reordered',
  'email.updated',
  'email.deleted',
  'luma.updated',
  'session.updated',
  'session.slide.changed',
  'session.transcript.partial',
  'session.transcript.final',
  'session.participant.joined',
  'session.participant.left',
  'session.audio.state',
  'session.stt.state',
] as const satisfies readonly RealtimeEventType[]

/** `WS /ws/events/{id}` control frames (not bus envelopes; no seq). */
export interface EventHello {
  type: 'hello'
  data: { event_id: string; client_id: string; user: PresenceUser; last_seq: number; presence: PresenceState[] }
}
export type ControlFrame =
  | EventHello
  | { type: 'resync' }
  | { type: 'pong'; data: { ref: string | number | null } }
  | { type: 'ack'; data: { ref: string | number } }
  | { type: 'error'; data: { message: string; code?: string; ref: string | number | null } }

/** Client → server operations on `WS /ws/events/{id}`. */
export type EventClientMessage =
  | { type: 'ping' }
  | { type: 'card.move'; data: { card_id: string; x: number; y: number } }
  | {
      type: 'card.update'
      data: { card_id: string; title?: string; data?: Record<string, unknown>; width?: number; height?: number; expected_version?: number }
    }
  | { type: 'presence.cursor'; data: { x: number; y: number } }
  | { type: 'presence.selection'; data: { card_ids: string[] } }

export type AgentRunStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
