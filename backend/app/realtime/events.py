"""Realtime envelope and the catalogue of event types.

Every state change the frontend cares about is published on the EventBus as an
`Envelope`. Two topic families exist:

- ``event:{event_id}``     planner workspace (chat, agents, board, decks)
- ``session:{session_id}`` live presentation (slides, transcript, participants)

The live-session types in `MIRRORED_SESSION_TYPES` are published on both the
session topic and the owning event's topic, with identical data (each topic
numbers its own copy). Transcript, audio and STT envelopes are session-only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.ids import new_id, utcnow

try:  # OpenTelemetry is optional (the `otel` extra); without it envelopes carry no trace id.
    from opentelemetry import trace as _otel_trace
except ImportError:  # pragma: no cover
    _otel_trace = None


def current_trace_id() -> str | None:
    """Hex trace id of the active OpenTelemetry span, so a UI can link an envelope to its trace."""
    if _otel_trace is None:
        return None
    span_context = _otel_trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return format(span_context.trace_id, "032x")


def event_topic(event_id: str) -> str:
    return f"event:{event_id}"


def session_topic(session_id: str) -> str:
    return f"session:{session_id}"


class Envelope(BaseModel):
    id: str = Field(default_factory=lambda: new_id("env"))
    type: str
    topic: str
    seq: int = 0  # per-topic, monotonically increasing; assigned by the bus
    ts: datetime = Field(default_factory=utcnow)
    data: dict[str, Any] = Field(default_factory=dict)
    # Trace that produced this change (when observability is on); lets the UI answer
    # "why did this slide change / card update?" by opening that trace in Dash0.
    trace_id: str | None = Field(default_factory=current_trace_id)


# --- event (topic: event) ------------------------------------------------------
EVENT_UPDATED = "event.updated"  # data: {event: EventOut}
EVENT_DELETED = "event.deleted"  # data: {event_id}

# --- chat (topic: event) -------------------------------------------------------
CHAT_MESSAGE_CREATED = "chat.message.created"  # data: {message: ChatMessageOut}
CHAT_DELTA = "chat.delta"  # data: {message_id, text}            streamed assistant text
CHAT_TOOL = "chat.tool"  # data: {message_id, name, status: started|finished, summary, is_error}
CHAT_MESSAGE_COMPLETED = "chat.message.completed"  # data: {message: ChatMessageOut}
CHAT_ERROR = "chat.error"  # data: {message_id, error}

# --- agent runs (topic: event) -----------------------------------------------
AGENT_RUN_CREATED = "agent.run.created"  # data: {run: AgentRunOut}
AGENT_RUN_UPDATED = "agent.run.updated"  # data: {run: AgentRunOut}  status/progress/current_step changed

# --- board (topic: event) ----------------------------------------------------
BOARD_UPDATED = "board.updated"  # data: {board: BoardOut}
BOARD_CARD_CREATED = "board.card.created"  # data: {card: CardOut}
BOARD_CARD_UPDATED = "board.card.updated"  # data: {card: CardOut, changed: [field...], by: user|agent}
BOARD_CARD_DELETED = "board.card.deleted"  # data: {card_id}
BOARD_EDGE_CREATED = "board.edge.created"  # data: {edge: EdgeOut}
BOARD_EDGE_DELETED = "board.edge.deleted"  # data: {edge_id}
BOARD_PRESENCE = "board.presence"  # data: {client_id, user, cursor: {x, y} | null, selection: [card_id]}

# --- presentations (topic: event) --------------------------------------------
PRESENTATION_CREATED = "presentation.created"  # data: {presentation: PresentationOut}
PRESENTATION_UPDATED = "presentation.updated"  # data: {presentation: PresentationOut}
PRESENTATION_DELETED = "presentation.deleted"  # data: {presentation_id}
SLIDE_CREATED = "slide.created"  # data: {slide: SlideOut}
SLIDE_UPDATED = "slide.updated"  # data: {slide: SlideOut}
SLIDE_DELETED = "slide.deleted"  # data: {slide_id, presentation_id}
SLIDES_REORDERED = "slides.reordered"  # data: {presentation_id, slide_ids: [...]}

# --- outreach (topic: event) -------------------------------------------------
EMAIL_UPDATED = "email.updated"  # data: {email: EmailMessageOut}   drafted / sent / failed
EMAIL_DELETED = "email.deleted"  # data: {email_id}
LUMA_UPDATED = "luma.updated"  # data: {luma: LumaEventOut}  draft / publishing / published / failed

# --- live sessions (topic: session; mirrored ones also on topic: event) ------
# data: {session: LiveSessionOut}  started / ended / settings (mirrored; on create: event topic only)
SESSION_UPDATED = "session.updated"
# data: {session_id, index, previous_index, reason, matched, by}  (mirrored)
SESSION_SLIDE_CHANGED = "session.slide.changed"
SESSION_TRANSCRIPT_PARTIAL = "session.transcript.partial"  # data: {text, slide_index}
SESSION_TRANSCRIPT_FINAL = "session.transcript.final"  # data: {segment: TranscriptSegmentOut}
SESSION_PARTICIPANT_JOINED = "session.participant.joined"  # data: {participant: ParticipantOut}  (mirrored)
SESSION_PARTICIPANT_LEFT = "session.participant.left"  # data: {session_id, participant_id}  (mirrored)
# data: {broadcasting, source_participant_id, listeners, codec, sample_rate}
SESSION_AUDIO_STATE = "session.audio.state"
SESSION_STT_STATE = "session.stt.state"  # data: {provider, status: connecting|active|error|stopped, error}

#: Session envelopes that are also published on the owning event's topic.
MIRRORED_SESSION_TYPES = frozenset(
    {SESSION_UPDATED, SESSION_SLIDE_CHANGED, SESSION_PARTICIPANT_JOINED, SESSION_PARTICIPANT_LEFT}
)
