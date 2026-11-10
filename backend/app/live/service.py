"""Live session service — public entrypoints.

Owns live-session state: lifecycle, participants, the current slide and the
transcript. The audio/STT pipeline and the WebSocket routes call these
functions; they never touch the tables directly.

All state changes publish envelopes on `session:{session_id}`. Lifecycle, slide
and presence envelopes (`ev.MIRRORED_SESSION_TYPES`) are also published, with the
same data, on the owning event's `event:{event_id}` topic so planner UIs see them;
transcript, audio and STT envelopes stay on the session topic only.

Runtime state: each session that is not ended has an in-memory `_Runtime`
(in `ctx.service("live_sessions")`) holding its latest row, the deck's cues,
the transcript `seq` counter and the `SlideTracker`. `ingest_transcript` runs
for every interim STT result and reads only that state; the database is
touched when something is persisted (finals, slide changes). The runtime is
dropped when the session ends.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import load_owned_event, resolve_user
from app.core.ids import new_join_code, utcnow
from app.core.security import TokenError, create_participant_token, decode_participant_token
from app.live.slide_tracker import SlideCue, SlideDecision, SlideTracker, TrackerConfig
from app.models import Event, LiveSession, Participant, Presentation, Slide, TranscriptSegment, User
from app.observability import metrics
from app.realtime import events as ev
from app.schemas.common import dump
from app.schemas.live import LiveSessionOut, ParticipantOut, SessionSettings, TranscriptSegmentOut
from app.transcription.base import TranscriptEvent

if TYPE_CHECKING:
    from app.core.context import AppContext

log = logging.getLogger(__name__)

RECENT_TRANSCRIPT_LIMIT = 50
MAX_TRANSCRIPT_PAGE = 500
_JOIN_CODE_ATTEMPTS = 5
_KEEP: Any = object()  # "leave unchanged", for arguments where None is itself a meaningful value


class SessionNotFound(LookupError):
    pass


class DeckNotFound(LookupError):
    """The presentation does not exist or belongs to another event."""


class SessionEnded(Exception):
    """The session has ended; it cannot be started or joined again."""


@dataclass(frozen=True)
class SessionClient:
    """Who is on the other end of a live-session socket."""

    session_id: str
    role: str  # host | speaker | listener
    participant_id: str | None  # None when the event owner connects with a user access token
    display_name: str
    user_id: str | None = None

    @property
    def can_control(self) -> bool:
        """May change slides, send audio and submit transcript text."""
        return self.role in {"host", "speaker"}


@dataclass(frozen=True)
class Admission:
    """A participant row plus the token that lets it connect to the session."""

    session: LiveSession
    participant: Participant
    token: str


# --- runtime state -----------------------------------------------------------


@dataclass
class _Runtime:
    """In-memory state of one session.

    `session` is the latest committed row; it is replaced, never mutated, so
    rows handed to callers stay stable. `lock` serialises this session's writes
    (seq allocation, slide changes, lifecycle).
    """

    session: LiveSession
    cues: list[SlideCue]
    next_seq: int
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tracker: SlideTracker | None = None
    tracker_stale: bool = True  # rebuild from `cues` and settings on next use


@dataclass
class _Registry:
    runtimes: dict[str, _Runtime] = field(default_factory=dict)
    load_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Open sockets per participant: a participant may hold a control and an audio socket.
    connections: dict[str, int] = field(default_factory=dict)
    presence_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _registry(ctx: AppContext) -> _Registry:
    return ctx.service("live_sessions", _Registry)


def build_tracker(cues: list[SlideCue], config: TrackerConfig, current_index: int) -> SlideTracker:
    """The one place a `SlideTracker` is constructed; tests substitute a fake here."""
    return SlideTracker(cues, config, current_index=current_index)


async def _deck_rows(db: AsyncSession, presentation_id: str | None) -> list[Any]:
    """Slides of a deck in order (id, position, title, keywords, notes); [] without a deck."""
    if presentation_id is None:
        return []
    query = (
        select(Slide.id, Slide.position, Slide.title, Slide.keywords, Slide.notes)
        .where(Slide.presentation_id == presentation_id)
        .order_by(Slide.position)
    )
    return list((await db.execute(query)).all())


def _cues(rows: list[Any]) -> list[SlideCue]:
    return [
        SlideCue(index=row.position, title=row.title or "", keywords=list(row.keywords or []), script=row.notes or "")
        for row in rows
    ]


async def _load_runtime(ctx: AppContext, session_id: str) -> _Runtime:
    async with ctx.db() as db:
        row = await db.get(LiveSession, session_id)
        if row is None:
            raise SessionNotFound(session_id)
        cues = _cues(await _deck_rows(db, row.presentation_id))
        last_seq = await db.scalar(
            select(func.max(TranscriptSegment.seq)).where(TranscriptSegment.session_id == session_id)
        )
    return _Runtime(session=row, cues=cues, next_seq=(last_seq or 0) + 1)


async def _runtime(ctx: AppContext, session_id: str) -> _Runtime:
    """The session's runtime, loaded on first use. Ended sessions are never cached."""
    registry = _registry(ctx)
    runtime = registry.runtimes.get(session_id)
    if runtime is not None:
        return runtime
    # One loader at a time, so two concurrent first calls share one runtime (and one seq counter).
    async with registry.load_lock:
        runtime = registry.runtimes.get(session_id)
        if runtime is None:
            runtime = await _load_runtime(ctx, session_id)
            if runtime.session.status != "ended":
                registry.runtimes[session_id] = runtime
    return runtime


def _sync_deck(runtime: _Runtime, cues: list[SlideCue]) -> None:
    if cues != runtime.cues:
        runtime.cues = cues
        runtime.tracker_stale = True


def _tracker(runtime: _Runtime) -> SlideTracker | None:
    """The session's tracker, (re)built when stale. None for an empty deck or when the build fails."""
    if runtime.tracker_stale:
        runtime.tracker_stale = False
        runtime.tracker = None
        if runtime.cues:
            settings = SessionSettings.model_validate(runtime.session.settings)
            config = TrackerConfig(mode=settings.auto_advance, cooldown_ms=settings.advance_cooldown_ms)
            try:
                runtime.tracker = build_tracker(runtime.cues, config, runtime.session.current_slide_index)
            except Exception:  # noqa: BLE001 - auto-advance must never take the transcript down with it
                log.exception("building the slide tracker for session %s failed", runtime.session.id)
    return runtime.tracker


# `set_slide` reasons -> `evently.slide.changes` trigger values.
_SLIDE_TRIGGERS = {"command": "voice_command", "keyword": "keyword", "script": "script_alignment", "manual": "manual"}


def _feed_tracker(runtime: _Runtime, event: TranscriptEvent) -> SlideDecision | None:
    tracker = _tracker(runtime)
    if tracker is None:
        return None
    started = time.perf_counter()
    try:
        decision = tracker.feed(event.text, event.is_final)
    except Exception:  # noqa: BLE001 - see _tracker
        log.exception("slide tracker failed on %r for session %s", event.text, runtime.session.id)
        return None
    outcome = "changed" if decision is not None else ("ignored" if tracker.config.mode == "off" else "no_match")
    metrics.slide_decision_latency(time.perf_counter() - started, outcome)
    return decision


def _topic(session_id: str) -> str:
    return ev.session_topic(session_id)


async def _publish(ctx: AppContext, session_id: str, event_id: str | None, type_: str, data: dict[str, Any]) -> None:
    """Publish on the session topic and, for `ev.MIRRORED_SESSION_TYPES`, on the event topic too.

    Each topic numbers its own copy (`seq` stays per topic); `data` is identical.
    """
    await ctx.bus.publish(_topic(session_id), type_, data)
    if event_id is not None and type_ in ev.MIRRORED_SESSION_TYPES:
        await ctx.bus.publish(ev.event_topic(event_id), type_, data)


async def _publish_session(ctx: AppContext, session: LiveSession) -> None:
    await _publish(ctx, session.id, session.event_id, ev.SESSION_UPDATED, {"session": dump(LiveSessionOut, session)})


# --- lookups -------------------------------------------------------------------


async def get_session(ctx: AppContext, session_id: str) -> LiveSession:
    """Load a session or raise `SessionNotFound`."""
    async with ctx.db() as db:
        session = await db.get(LiveSession, session_id)
    if session is None:
        raise SessionNotFound(session_id)
    return session


async def list_sessions(ctx: AppContext, event_id: str) -> list[LiveSession]:
    async with ctx.db() as db:
        query = select(LiveSession).where(LiveSession.event_id == event_id).order_by(LiveSession.created_at.desc())
        return list((await db.execute(query)).scalars())


async def get_session_state(ctx: AppContext, session_id: str) -> dict[str, Any]:
    """Snapshot sent to a client when it connects.

    `{"session": LiveSessionOut, "participants": [ParticipantOut], "slide_count": int,
      "slides": [{"id", "position", "title", "keywords"}], "recent_transcript": [TranscriptSegmentOut]}`
    """
    async with ctx.db() as db:
        session = await db.get(LiveSession, session_id)
        if session is None:
            raise SessionNotFound(session_id)
        participants = await db.execute(
            select(Participant).where(Participant.session_id == session_id).order_by(Participant.joined_at)
        )
        deck = await _deck_rows(db, session.presentation_id)
        recent = await db.execute(
            select(TranscriptSegment)
            .where(TranscriptSegment.session_id == session_id)
            .order_by(TranscriptSegment.seq.desc())
            .limit(RECENT_TRANSCRIPT_LIMIT)
        )
    return {
        "session": dump(LiveSessionOut, session),
        "participants": [dump(ParticipantOut, p) for p in participants.scalars()],
        "slide_count": len(deck),
        "slides": [
            {"id": row.id, "position": row.position, "title": row.title, "keywords": list(row.keywords or [])}
            for row in deck
        ],
        "recent_transcript": [dump(TranscriptSegmentOut, s) for s in reversed(list(recent.scalars()))],
    }


def transcript_visible_to(role: str, settings: Mapping[str, Any]) -> bool:
    """Whether a client with `role` may see transcript text under a session's stored settings."""
    return role != "listener" or SessionSettings.model_validate(settings).show_transcript_to_listeners


async def get_client_state(ctx: AppContext, client: SessionClient) -> dict[str, Any]:
    """`get_session_state` as `client` may see it: listeners get no transcript when it is hidden."""
    state = await get_session_state(ctx, client.session_id)
    if not transcript_visible_to(client.role, state["session"]["settings"]):
        state["recent_transcript"] = []
    return state


async def list_transcript(
    ctx: AppContext, session_id: str, *, after_seq: int = 0, limit: int = 200
) -> list[TranscriptSegment]:
    """Final segments with `seq > after_seq`, oldest first, at most `limit` (capped)."""
    query = (
        select(TranscriptSegment)
        .where(TranscriptSegment.session_id == session_id, TranscriptSegment.seq > after_seq)
        .order_by(TranscriptSegment.seq)
        .limit(max(1, min(limit, MAX_TRANSCRIPT_PAGE)))
    )
    async with ctx.db() as db:
        return list((await db.execute(query)).scalars())


async def get_keyterms(ctx: AppContext, session_id: str) -> list[str]:
    """Slide trigger keywords of the session's deck, for STT keyword boosting."""
    session = await get_session(ctx, session_id)
    async with ctx.db() as db:
        rows = await _deck_rows(db, session.presentation_id)
    seen: set[str] = set()
    terms: list[str] = []
    for row in rows:
        for keyword in row.keywords or []:
            key = keyword.strip().lower()
            if key and key not in seen:
                seen.add(key)
                terms.append(keyword.strip())
    return terms


# --- authentication ------------------------------------------------------------


async def authenticate_session_client(ctx: AppContext, session_id: str, token: str | None) -> SessionClient | None:
    """Identify a live-session client, or return None to reject the connection.

    Accepts, in order:
      * a participant token minted for this session (role from the token);
      * a user access token belonging to the event owner (role "host");
      * no token, only when auth is optional and the dev user owns the event (role "host").
    Raises `SessionNotFound` if the session does not exist.
    """
    session = await get_session(ctx, session_id)
    if token:
        try:
            claims = decode_participant_token(ctx.settings, token)
        except TokenError:
            pass  # not a participant token; it may still be the owner's access token
        else:
            return await _participant_client(ctx, session_id, claims.participant_id, claims.role)
    user = await resolve_user(ctx, token)
    if user is None or await load_owned_event(ctx, session.event_id, user) is None:
        return None
    return SessionClient(
        session_id=session_id, role="host", participant_id=None, display_name=user.name or "Host", user_id=user.id
    )


async def _participant_client(ctx: AppContext, session_id: str, participant_id: str, role: str) -> SessionClient | None:
    async with ctx.db() as db:
        participant = await db.get(Participant, participant_id)
    if participant is None or participant.session_id != session_id:
        return None
    return SessionClient(
        session_id=session_id,
        role=role,
        participant_id=participant.id,
        display_name=participant.display_name,
        user_id=participant.user_id,
    )


# --- lifecycle -------------------------------------------------------------------


async def _require_deck(db: AsyncSession, event_id: str, presentation_id: str) -> Presentation:
    deck = await db.get(Presentation, presentation_id)
    if deck is None or deck.event_id != event_id:
        raise DeckNotFound(presentation_id)
    return deck


def _admit(ctx: AppContext, session: LiveSession, participant: Participant) -> Admission:
    token = create_participant_token(
        ctx.settings, participant_id=participant.id, session_id=session.id, role=participant.role
    )
    return Admission(session=session, participant=participant, token=token)


async def create_session(
    ctx: AppContext,
    *,
    event: Event,
    host: User,
    presentation_id: str | None = None,
    title: str | None = None,
    settings: SessionSettings | None = None,
) -> Admission:
    """Create a scheduled session with a unique join code and its host participant.

    Publishes `session.updated` on the event topic only (see the comment below).

    The title defaults to the deck's title, then the event's name.
    Raises `DeckNotFound` if the presentation is not part of the event.
    """
    stored = (settings or SessionSettings()).model_dump()
    async with ctx.db() as db:
        deck = await _require_deck(db, event.id, presentation_id) if presentation_id else None
        fields = {
            "event_id": event.id,
            "presentation_id": presentation_id,
            "host_user_id": host.id,
            "title": title or (deck.title if deck else event.name),
            "settings": stored,
        }
        for attempt in range(_JOIN_CODE_ATTEMPTS):
            session = LiveSession(join_code=new_join_code(), **fields)
            db.add(session)
            try:
                await db.flush()
                break
            except IntegrityError:  # join code already taken; try another
                await db.rollback()
                if attempt == _JOIN_CODE_ATTEMPTS - 1:
                    raise
        participant = Participant(session_id=session.id, user_id=host.id, display_name=host.name or "Host", role="host")
        db.add(participant)
        await db.commit()
    # Only the event topic hears about creation: nobody can be on the session topic yet, and its
    # first envelope stays the `session.updated` of `start_session`.
    await ctx.bus.publish(ev.event_topic(event.id), ev.SESSION_UPDATED, {"session": dump(LiveSessionOut, session)})
    return _admit(ctx, session, participant)


async def add_participant(
    ctx: AppContext, session_id: str, *, display_name: str, role: str, user_id: str | None = None
) -> Admission:
    """Register a participant (host | speaker | listener) and mint its token. Raises `SessionEnded`."""
    session = await get_session(ctx, session_id)
    if session.status == "ended":
        raise SessionEnded(session_id)
    participant = Participant(session_id=session_id, user_id=user_id, display_name=display_name, role=role)
    async with ctx.db() as db:
        db.add(participant)
        await db.commit()
    return _admit(ctx, session, participant)


async def join_session(ctx: AppContext, join_code: str, display_name: str) -> Admission:
    """Admit an attendee by join code (case-insensitive) as a listener.

    Raises `SessionNotFound` for an unknown code and `SessionEnded` for a finished session.
    """
    code = join_code.strip().upper()
    async with ctx.db() as db:
        session = (await db.execute(select(LiveSession).where(LiveSession.join_code == code))).scalar_one_or_none()
    if session is None:
        raise SessionNotFound(code)
    return await add_participant(ctx, session.id, display_name=display_name, role="listener")


async def start_session(ctx: AppContext, session_id: str) -> LiveSession:
    """Scheduled -> live (idempotent when already live); publishes `session.updated`. Raises `SessionEnded`."""
    runtime = await _runtime(ctx, session_id)
    async with runtime.lock:
        async with ctx.db() as db:
            row = await db.get(LiveSession, session_id)
            if row is None:
                raise SessionNotFound(session_id)
            if row.status == "ended":
                raise SessionEnded(session_id)
            if row.status == "live":
                return row
            row.status = "live"
            row.started_at = utcnow()
            await db.commit()
            cues = _cues(await _deck_rows(db, row.presentation_id))
        runtime.session = row
        _sync_deck(runtime, cues)  # the deck may have been edited since the runtime was loaded
        await _publish_session(ctx, row)
    return row


async def end_session(ctx: AppContext, session_id: str) -> LiveSession:
    """Mark the session ended (idempotent), drop its runtime state and publish `session.updated`."""
    runtime = await _runtime(ctx, session_id)
    async with runtime.lock:
        async with ctx.db() as db:
            row = await db.get(LiveSession, session_id)
            if row is None:
                raise SessionNotFound(session_id)
            if row.status == "ended":
                return row
            row.status = "ended"
            row.ended_at = utcnow()
            await db.commit()
        runtime.session = row  # anyone still holding this runtime sees it ended
        _registry(ctx).runtimes.pop(session_id, None)
        await _publish_session(ctx, row)
    return row


async def update_session(
    ctx: AppContext,
    session_id: str,
    *,
    title: str | None = None,
    presentation_id: str | None = _KEEP,
    settings: Mapping[str, Any] | None = None,
) -> LiveSession:
    """Change title, deck and/or settings; publishes `session.updated` when something changed.

    `settings` is merged over the stored settings (validated as `SessionSettings`, so a
    bad value raises `pydantic.ValidationError`). Changing the deck returns to slide 0.
    The slide tracker is rebuilt when the deck or the settings change.
    Raises `DeckNotFound` if the presentation is not part of the session's event.
    """
    runtime = await _runtime(ctx, session_id)
    async with runtime.lock:
        async with ctx.db() as db:
            row = await db.get(LiveSession, session_id)
            if row is None:
                raise SessionNotFound(session_id)
            changed = False
            if title is not None and title != row.title:
                row.title = title
                changed = True
            if presentation_id is not _KEEP and presentation_id != row.presentation_id:
                if presentation_id is not None:
                    await _require_deck(db, row.event_id, presentation_id)
                row.presentation_id = presentation_id
                row.current_slide_index = 0
                changed = True
            if settings is not None:
                merged = SessionSettings.model_validate({**row.settings, **settings}).model_dump()
                if merged != row.settings:
                    row.settings = merged
                    changed = True
            if not changed:
                return row
            await db.commit()
            cues = _cues(await _deck_rows(db, row.presentation_id))
        if row.settings != runtime.session.settings:
            runtime.tracker_stale = True
        runtime.session = row
        _sync_deck(runtime, cues)
        await _publish_session(ctx, row)
    return row


# --- slides ------------------------------------------------------------------------


async def set_slide(
    ctx: AppContext,
    session_id: str,
    index: int,
    *,
    reason: str = "manual",
    matched: str | None = None,
    by: str | None = None,
) -> LiveSession:
    """Move the session to slide `index` (clamped to the deck) and publish `session.slide.changed`.

    reason: manual | command | keyword | script.  matched: the phrase that triggered it.
    by: participant id for manual changes. No-op (no envelope) if the index does not change.
    """
    runtime = await _runtime(ctx, session_id)
    async with runtime.lock:
        async with ctx.db() as db:
            row = await db.get(LiveSession, session_id)
            if row is None:
                raise SessionNotFound(session_id)
            # Re-read the deck: slides may have been added or removed since the runtime was loaded.
            _sync_deck(runtime, _cues(await _deck_rows(db, row.presentation_id)))
            previous = row.current_slide_index
            target = max(0, min(index, len(runtime.cues) - 1))
            if target != previous:
                row.current_slide_index = target
                await db.commit()
        runtime.session = row
        # Sync a tracker that did not make this decision itself (its own decisions already moved it).
        if runtime.tracker is not None and runtime.tracker.current_index != target:
            runtime.tracker.set_current(target)
        if target != previous:
            metrics.slide_changed(_SLIDE_TRIGGERS.get(reason, reason))
            await _publish(
                ctx,
                session_id,
                row.event_id,
                ev.SESSION_SLIDE_CHANGED,
                {
                    "session_id": session_id,
                    "index": target,
                    "previous_index": previous,
                    "reason": reason,
                    "matched": matched,
                    "by": by,
                },
            )
    return row


# --- transcript --------------------------------------------------------------------


async def ingest_transcript(ctx: AppContext, session_id: str, event: TranscriptEvent, *, source: str = "stt") -> None:
    """Feed one transcript result into the session.

    Interim results are published as `session.transcript.partial`; final results are
    stored as `TranscriptSegment` rows and published as `session.transcript.final`.
    Every result is also run through the session's `SlideTracker` (according to
    `SessionSettings.auto_advance`), which may call `set_slide`.
    Ignored when the session is not live.
    """
    runtime = await _runtime(ctx, session_id)
    if runtime.session.status != "live" or not event.text.strip():
        return
    if event.is_final:
        if not await _store_final(ctx, runtime, event, source):
            return
    else:
        await ctx.bus.publish(
            _topic(session_id),
            ev.SESSION_TRANSCRIPT_PARTIAL,
            {"text": event.text, "slide_index": runtime.session.current_slide_index},
        )
    decision = _feed_tracker(runtime, event)
    if decision is not None:
        await set_slide(ctx, session_id, decision.target_index, reason=decision.reason, matched=decision.matched)


async def _store_final(ctx: AppContext, runtime: _Runtime, event: TranscriptEvent, source: str) -> bool:
    """Persist a final result under the session lock so `seq` values are unique and published in order."""
    async with runtime.lock:
        if runtime.session.status != "live":  # ended while we waited for the lock
            return False
        segment = TranscriptSegment(
            session_id=runtime.session.id,
            seq=runtime.next_seq,
            text=event.text,
            start_ms=event.start_ms,
            end_ms=event.end_ms,
            confidence=event.confidence,
            speaker=event.speaker,
            slide_index=runtime.session.current_slide_index,
            source=source,
        )
        async with ctx.db() as db:
            db.add(segment)
            await db.commit()
        runtime.next_seq += 1
        await ctx.bus.publish(
            _topic(segment.session_id), ev.SESSION_TRANSCRIPT_FINAL, {"segment": dump(TranscriptSegmentOut, segment)}
        )
    return True


# --- presence -------------------------------------------------------------------------


async def set_participant_connected(ctx: AppContext, participant_id: str, connected: bool) -> None:
    """Record a participant's socket connecting/disconnecting and publish joined/left.

    Sockets are counted per participant (control and audio sockets both call this):
    `session.participant.joined` goes out for the first socket, `session.participant.left`
    when the last one closes. Unknown participant ids are ignored.
    """
    registry = _registry(ctx)
    async with registry.presence_lock:
        before = registry.connections.get(participant_id, 0)
        after = before + 1 if connected else max(before - 1, 0)
        if after:
            registry.connections[participant_id] = after
        else:
            registry.connections.pop(participant_id, None)
        if (before > 0) == (after > 0):
            return
        async with ctx.db() as db:
            participant = await db.get(Participant, participant_id)
            if participant is None:
                return
            participant.connected = connected
            participant.left_at = None if connected else utcnow()
            await db.commit()
            event_id = await db.scalar(select(LiveSession.event_id).where(LiveSession.id == participant.session_id))
        session_id = participant.session_id
        if connected:
            payload = {"participant": dump(ParticipantOut, participant)}
            await _publish(ctx, session_id, event_id, ev.SESSION_PARTICIPANT_JOINED, payload)
        else:
            payload = {"session_id": session_id, "participant_id": participant_id}
            await _publish(ctx, session_id, event_id, ev.SESSION_PARTICIPANT_LEFT, payload)
