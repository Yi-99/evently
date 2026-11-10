"""ORM tables. Enumerated string values are listed next to each column."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UTCDateTime
from app.core.ids import new_id, utcnow


def _pk(prefix: str):
    return mapped_column(String(40), primary_key=True, default=lambda: new_id(prefix))


def _created():
    return mapped_column(UTCDateTime, default=utcnow)


def _updated():
    return mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = _pk("usr")
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = _created()


class Event(Base):
    """A planning project (the thing being planned), not a realtime event."""

    __tablename__ = "events"

    id: Mapped[str] = _pk("evt")
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    # Latest known planning requirements; shape follows app.schemas.venue.VenueCriteria.
    requirements: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="planning")  # planning | confirmed | archived
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[str] = _pk("msg")
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # user | assistant | system
    content: Mapped[str] = mapped_column(Text, default="")  # display text
    # Claude-format messages produced by this turn (thinking blocks stripped), for history replay.
    llm_messages: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # Free-form extras: selected card ids, agent_run_id, tool activity summary…
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="complete")  # streaming | complete | error
    created_at: Mapped[datetime] = _created()


class Board(Base):
    __tablename__ = "boards"

    id: Mapped[str] = _pk("brd")
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="Board")
    viewport: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {x, y, zoom}
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Card(Base):
    __tablename__ = "cards"

    id: Mapped[str] = _pk("card")
    board_id: Mapped[str] = mapped_column(ForeignKey("boards.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # venue | note | summary | comparison | presentation | email | luma
    title: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(20), default="ready")  # pending | running | ready | error
    x: Mapped[float] = mapped_column(Float, default=0.0)
    y: Mapped[float] = mapped_column(Float, default=0.0)
    width: Mapped[float] = mapped_column(Float, default=320.0)
    height: Mapped[float] = mapped_column(Float, default=420.0)
    z_index: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Top-level `data` keys edited by the user; agent patches must not overwrite them.
    locked_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    agent_run_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    created_by: Mapped[str] = mapped_column(String(20), default="user")  # user | agent
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Edge(Base):
    __tablename__ = "edges"

    id: Mapped[str] = _pk("edge")
    board_id: Mapped[str] = mapped_column(ForeignKey("boards.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    source_card_id: Mapped[str] = mapped_column(ForeignKey("cards.id", ondelete="CASCADE"))
    target_card_id: Mapped[str] = mapped_column(ForeignKey("cards.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(200), default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _created()


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = _pk("run")
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    parent_run_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    # orchestrator | venue_search | venue_scout | venue_research | card_refine |
    # presentation_build | slide_render | presentation_edit
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300), default="")
    # queued | running | succeeded | failed | cancelled
    status: Mapped[str] = mapped_column(String(20), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)  # 0..1
    current_step: Mapped[str] = mapped_column(String(500), default="")
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # [{ts, message, data}]
    input: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    card_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _created()
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Presentation(Base):
    __tablename__ = "presentations"

    id: Mapped[str] = _pk("prs")
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300), default="Untitled deck")
    brief: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # app.schemas.presentation.PresentationBrief
    theme: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # app.schemas.presentation.DeckTheme
    status: Mapped[str] = mapped_column(String(20), default="generating")  # generating | ready | error
    agent_run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Slide(Base):
    __tablename__ = "slides"
    __table_args__ = (Index("ix_slides_presentation_position", "presentation_id", "position"),)

    id: Mapped[str] = _pk("sld")
    presentation_id: Mapped[str] = mapped_column(ForeignKey("presentations.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)  # 0-based order within the deck
    title: Mapped[str] = mapped_column(String(300), default="")
    # Self-contained HTML fragment (inline CSS/SVG, no scripts), rendered in a sandboxed iframe.
    html: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")  # speaker script
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)  # voice triggers that advance TO this slide
    outline: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # app.schemas.presentation.SlideOutline
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | rendering | ready | error
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class LiveSession(Base):
    __tablename__ = "live_sessions"

    id: Mapped[str] = _pk("ses")
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    presentation_id: Mapped[str | None] = mapped_column(
        ForeignKey("presentations.id", ondelete="SET NULL"), nullable=True
    )
    host_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(300), default="")
    join_code: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="scheduled")  # scheduled | live | ended
    current_slide_index: Mapped[int] = mapped_column(Integer, default=0)
    # app.schemas.live.SessionSettings
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _created()
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Participant(Base):
    __tablename__ = "participants"

    id: Mapped[str] = _pk("prt")
    session_id: Mapped[str] = mapped_column(ForeignKey("live_sessions.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    display_name: Mapped[str] = mapped_column(String(100), default="Guest")
    role: Mapped[str] = mapped_column(String(20), default="listener")  # host | speaker | listener
    device: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    joined_at: Mapped[datetime] = _created()
    left_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class EmailMessage(Base):
    """An outbound email: drafted by the planner or an agent, sent only after explicit confirmation."""

    __tablename__ = "email_messages"

    id: Mapped[str] = _pk("eml")
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30), default="custom")  # venue_inquiry | invite | update | custom
    to: Mapped[list[str]] = mapped_column(JSON, default=list)
    cc: Mapped[list[str]] = mapped_column(JSON, default=list)
    reply_to: Mapped[str | None] = mapped_column(String(320), nullable=True)
    subject: Mapped[str] = mapped_column(String(500), default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | sending | sent | failed
    provider: Mapped[str | None] = mapped_column(String(30), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    card_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    related_card_id: Mapped[str | None] = mapped_column(String(40), nullable=True)  # e.g. the venue asked about
    created_by: Mapped[str] = mapped_column(String(20), default="user")  # user | agent
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class LumaEvent(Base):
    """The Luma (lu.ma) event published for a planning event, and the draft that precedes it."""

    __tablename__ = "luma_events"

    id: Mapped[str] = _pk("luma")
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | publishing | published | failed
    # app.schemas.outreach.LumaEventDraft — what will be / was sent to Luma
    draft: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    luma_event_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    card_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    guests_invited: Mapped[int] = mapped_column(Integer, default=0)
    last_synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class TranscriptSegment(Base):
    """A finalised piece of transcript. Interim results are only broadcast, never stored."""

    __tablename__ = "transcript_segments"
    __table_args__ = (UniqueConstraint("session_id", "seq", name="uq_transcript_session_seq"),)

    id: Mapped[str] = _pk("seg")
    session_id: Mapped[str] = mapped_column(ForeignKey("live_sessions.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    speaker: Mapped[str | None] = mapped_column(String(100), nullable=True)
    slide_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(30), default="stt")  # stt | browser | manual
    created_at: Mapped[datetime] = _created()
