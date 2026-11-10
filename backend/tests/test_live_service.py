"""Live session service: lifecycle, join codes, tokens, transcript ingestion, slide tracking.

`make_deck` and `install_fake_tracker` are shared with the API and WebSocket tests.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from app.core.context import AppContext
from app.core.deps import get_dev_user
from app.core.security import create_access_token, decode_participant_token
from app.live import service
from app.live.slide_tracker import SlideCue, SlideDecision, SlideTracker, TrackerConfig
from app.models import Event, Participant, Presentation, Slide, TranscriptSegment, User
from app.realtime import events as ev
from app.schemas.live import SessionSettings
from app.transcription.base import TranscriptEvent
from tests.helpers import create_event, session_published

# --- shared helpers ---------------------------------------------------------------


async def make_deck(ctx: AppContext, event_id: str, count: int = 3, title: str = "Deck") -> Presentation:
    """A ready deck with `count` slides: slide i has keyword `kw{i}` and notes `notes {i}`."""
    deck = Presentation(event_id=event_id, title=title, status="ready")
    async with ctx.db() as db:
        db.add(deck)
        await db.flush()
        for i in range(count):
            db.add(
                Slide(
                    presentation_id=deck.id,
                    position=i,
                    title=f"Slide {i}",
                    html=f"<h1>{i}</h1>",
                    notes=f"notes {i}",
                    keywords=[f"kw{i}"],
                    status="ready",
                )
            )
        await db.commit()
    return deck


class FakeTracker:
    """Scripted stand-in for `SlideTracker`.

    "next slide" -> advance one; "jump to the end" -> slide 99 (to exercise clamping);
    "boom" -> raises. Like the real one, a decision moves `current_index`.
    """

    def __init__(self, cues: list[SlideCue], config: TrackerConfig, current_index: int = 0) -> None:
        self.cues = cues
        self.config = config
        self._current = current_index
        self.fed: list[tuple[str, bool]] = []
        self.synced: list[int] = []

    @property
    def current_index(self) -> int:
        return self._current

    def set_current(self, index: int) -> None:
        self.synced.append(index)
        self._current = index

    def feed(self, text: str, is_final: bool, now_ms: int | None = None) -> SlideDecision | None:
        self.fed.append((text, is_final))
        lowered = text.lower()
        if "boom" in lowered:
            raise RuntimeError("tracker exploded")
        if "next slide" in lowered:
            target = self._current + 1
        elif "jump to the end" in lowered:
            target = 99
        else:
            return None
        self._current = target
        return SlideDecision(target_index=target, reason="command", matched=lowered.strip())


def install_fake_tracker(monkeypatch: pytest.MonkeyPatch) -> list[FakeTracker]:
    """Route `service.build_tracker` to `FakeTracker`; returns the list of trackers built so far."""
    built: list[FakeTracker] = []

    def factory(cues: list[SlideCue], config: TrackerConfig, current_index: int) -> FakeTracker:
        built.append(FakeTracker(cues, config, current_index))
        return built[-1]

    monkeypatch.setattr(service, "build_tracker", factory)
    return built


def final(text: str, **extra) -> TranscriptEvent:
    return TranscriptEvent(text=text, is_final=True, **extra)


def interim(text: str) -> TranscriptEvent:
    return TranscriptEvent(text=text, is_final=False)


# --- fixtures -------------------------------------------------------------------------


@pytest.fixture
def trackers(monkeypatch) -> list[FakeTracker]:
    return install_fake_tracker(monkeypatch)


@pytest.fixture
async def host(ctx: AppContext) -> User:
    return await get_dev_user(ctx)


@pytest.fixture
async def deck(ctx: AppContext, event: Event) -> Presentation:
    return await make_deck(ctx, event.id)


@pytest.fixture
async def live(ctx: AppContext, event: Event, host: User, deck: Presentation) -> service.Admission:
    """A started session with a 3-slide deck."""
    admission = await service.create_session(ctx, event=event, host=host, presentation_id=deck.id)
    await service.start_session(ctx, admission.session.id)
    return admission


async def segments(ctx: AppContext, session_id: str) -> list[TranscriptSegment]:
    return await service.list_transcript(ctx, session_id, limit=500)


# --- lifecycle ---------------------------------------------------------------------------


async def test_create_session_has_join_code_host_participant_and_token(ctx, event, host, deck):
    admission = await service.create_session(ctx, event=event, host=host, presentation_id=deck.id)
    session, participant = admission.session, admission.participant

    assert session.status == "scheduled" and session.current_slide_index == 0
    assert session.title == "Deck" and session.settings == SessionSettings().model_dump()
    assert len(session.join_code) == 6 and session.join_code.isupper()
    assert participant.role == "host" and participant.user_id == host.id and participant.session_id == session.id
    claims = decode_participant_token(ctx.settings, admission.token)
    assert (claims.participant_id, claims.session_id, claims.role) == (participant.id, session.id, "host")

    untitled = await service.create_session(ctx, event=event, host=host)
    assert untitled.session.title == event.name and untitled.session.presentation_id is None
    assert [s.id for s in await service.list_sessions(ctx, event.id)] == [untitled.session.id, session.id]


async def test_create_session_rejects_deck_of_another_event(ctx, event, host):
    other = await create_event(ctx, "Other")
    foreign = await make_deck(ctx, other.id)
    with pytest.raises(service.DeckNotFound):
        await service.create_session(ctx, event=event, host=host, presentation_id=foreign.id)


async def test_join_code_collision_is_retried(ctx, event, host, monkeypatch):
    first = await service.create_session(ctx, event=event, host=host)
    codes = iter([first.session.join_code, "FRESH1"])
    monkeypatch.setattr(service, "new_join_code", lambda: next(codes))
    second = await service.create_session(ctx, event=event, host=host)
    assert second.session.join_code == "FRESH1"


async def test_start_and_end_publish_and_are_idempotent(ctx, event, host):
    session_id = (await service.create_session(ctx, event=event, host=host)).session.id

    started = await service.start_session(ctx, session_id)
    assert started.status == "live" and started.started_at is not None
    await service.start_session(ctx, session_id)  # no-op
    updates = session_published(ctx, session_id, ev.SESSION_UPDATED)
    assert len(updates) == 1 and updates[0].data["session"]["status"] == "live"

    ended = await service.end_session(ctx, session_id)
    assert ended.status == "ended" and ended.ended_at is not None
    assert session_id not in service._registry(ctx).runtimes
    await service.end_session(ctx, session_id)  # no-op
    assert len(session_published(ctx, session_id, ev.SESSION_UPDATED)) == 2
    with pytest.raises(service.SessionEnded):
        await service.start_session(ctx, session_id)
    with pytest.raises(service.SessionNotFound):
        await service.start_session(ctx, "ses_missing")


async def test_join_by_code(ctx, event, host):
    created = await service.create_session(ctx, event=event, host=host)
    code = created.session.join_code

    joined = await service.join_session(ctx, f" {code.lower()} ", "Ada")
    assert joined.session.id == created.session.id
    assert joined.participant.role == "listener" and joined.participant.display_name == "Ada"
    assert decode_participant_token(ctx.settings, joined.token).role == "listener"
    # Joining does not publish; the socket connecting does.
    assert session_published(ctx, created.session.id) == []

    with pytest.raises(service.SessionNotFound):
        await service.join_session(ctx, "NOPE42", "Bob")
    await service.end_session(ctx, created.session.id)
    with pytest.raises(service.SessionEnded):
        await service.join_session(ctx, code, "Late")


# --- authentication ------------------------------------------------------------------------


async def test_authenticate_session_client_by_role(ctx, event, host):
    admission = await service.create_session(ctx, event=event, host=host)
    sid = admission.session.id
    other = await service.create_session(ctx, event=event, host=host)

    as_host = await service.authenticate_session_client(ctx, sid, admission.token)
    assert as_host.role == "host" and as_host.participant_id == admission.participant.id and as_host.can_control
    assert as_host.display_name == admission.participant.display_name and as_host.user_id == host.id

    speaker = await service.add_participant(ctx, sid, display_name="Grace", role="speaker")
    as_speaker = await service.authenticate_session_client(ctx, sid, speaker.token)
    assert (as_speaker.role, as_speaker.display_name, as_speaker.can_control) == ("speaker", "Grace", True)

    listener = await service.join_session(ctx, admission.session.join_code, "Ada")
    as_listener = await service.authenticate_session_client(ctx, sid, listener.token)
    assert as_listener.role == "listener" and not as_listener.can_control

    # A token from another session, a garbage token: rejected.
    assert await service.authenticate_session_client(ctx, sid, other.token) is None
    assert await service.authenticate_session_client(ctx, sid, "not-a-token") is None

    # The owner's access token, or no token when auth is optional: host without a participant row.
    owner = await service.authenticate_session_client(ctx, sid, create_access_token(ctx.settings, host.id))
    assert owner.role == "host" and owner.participant_id is None and owner.user_id == host.id
    assert (await service.authenticate_session_client(ctx, sid, None)).role == "host"

    with pytest.raises(service.SessionNotFound):
        await service.authenticate_session_client(ctx, "ses_missing", admission.token)


async def test_authenticate_rejects_other_users_and_requires_tokens_when_auth_is_required(ctx, event, host):
    sid = (await service.create_session(ctx, event=event, host=host)).session.id
    async with ctx.db() as db:
        stranger = User(email="stranger@example.com", name="Stranger")
        db.add(stranger)
        await db.commit()
    assert await service.authenticate_session_client(ctx, sid, create_access_token(ctx.settings, stranger.id)) is None

    ctx.settings.auth_required = True
    try:
        assert await service.authenticate_session_client(ctx, sid, None) is None
    finally:
        ctx.settings.auth_required = False


# --- transcript ingestion -------------------------------------------------------------------


async def test_ingest_is_ignored_unless_live(ctx, event, host, trackers):
    scheduled = await service.create_session(ctx, event=event, host=host)
    await service.ingest_transcript(ctx, scheduled.session.id, final("hello"))
    await service.ingest_transcript(ctx, scheduled.session.id, interim("hel"))
    assert session_published(ctx, scheduled.session.id) == [] and await segments(ctx, scheduled.session.id) == []
    assert trackers == []

    await service.start_session(ctx, scheduled.session.id)
    await service.end_session(ctx, scheduled.session.id)
    await service.ingest_transcript(ctx, scheduled.session.id, final("too late"))
    assert await segments(ctx, scheduled.session.id) == []

    with pytest.raises(service.SessionNotFound):
        await service.ingest_transcript(ctx, "ses_missing", final("hello"))


async def test_partial_is_published_and_final_is_stored(ctx, live, trackers):
    sid = live.session.id
    await service.set_slide(ctx, sid, 1)
    await service.ingest_transcript(ctx, sid, interim("so the ven"))
    await service.ingest_transcript(ctx, sid, interim("   "))  # blank results are dropped
    await service.ingest_transcript(ctx, sid, final("so the venue", start_ms=10, end_ms=900, confidence=0.9))
    await service.ingest_transcript(ctx, sid, final("is downtown", speaker="A"), source="browser")

    partials = session_published(ctx, sid, ev.SESSION_TRANSCRIPT_PARTIAL)
    assert [p.data for p in partials] == [{"text": "so the ven", "slide_index": 1}]

    stored = await segments(ctx, sid)
    assert [(s.seq, s.text, s.slide_index, s.source) for s in stored] == [
        (1, "so the venue", 1, "stt"),
        (2, "is downtown", 1, "browser"),
    ]
    assert (stored[0].start_ms, stored[0].end_ms, stored[0].confidence, stored[1].speaker) == (10, 900, 0.9, "A")

    finals = session_published(ctx, sid, ev.SESSION_TRANSCRIPT_FINAL)
    assert [f.data["segment"]["seq"] for f in finals] == [1, 2]
    assert finals[0].data["segment"]["text"] == "so the venue" and finals[0].data["segment"]["slide_index"] == 1

    # Every result went through the tracker, in order.
    assert trackers[0].fed == [("so the ven", False), ("so the venue", True), ("is downtown", True)]

    assert [s.seq for s in await service.list_transcript(ctx, sid, after_seq=1)] == [2]
    assert [s.seq for s in await service.list_transcript(ctx, sid, limit=1)] == [1]


async def test_concurrent_finals_get_unique_ordered_seqs(ctx, live, trackers):
    sid = live.session.id
    await asyncio.gather(*(service.ingest_transcript(ctx, sid, final(f"sentence {i}")) for i in range(25)))

    stored = await segments(ctx, sid)
    assert [s.seq for s in stored] == list(range(1, 26))
    published = [e.data["segment"]["seq"] for e in session_published(ctx, sid, ev.SESSION_TRANSCRIPT_FINAL)]
    assert published == list(range(1, 26))

    # The counter survives a runtime reload (e.g. after a restart) by reading the stored maximum.
    service._registry(ctx).runtimes.clear()
    await service.ingest_transcript(ctx, sid, final("after reload"))
    assert (await segments(ctx, sid))[-1].seq == 26


# --- slide tracking -------------------------------------------------------------------------------


async def test_tracker_is_built_from_deck_and_settings_and_its_decisions_move_the_deck(ctx, live, trackers):
    sid = live.session.id
    await service.ingest_transcript(ctx, sid, interim("okay"))

    assert len(trackers) == 1
    tracker = trackers[0]
    assert [(c.index, c.title, c.keywords, c.script) for c in tracker.cues] == [
        (0, "Slide 0", ["kw0"], "notes 0"),
        (1, "Slide 1", ["kw1"], "notes 1"),
        (2, "Slide 2", ["kw2"], "notes 2"),
    ]
    assert tracker.config.mode == "keywords" and tracker.config.cooldown_ms == 2500 and tracker.current_index == 0

    await service.ingest_transcript(ctx, sid, final("and now, next slide"))
    assert (await service.get_session(ctx, sid)).current_slide_index == 1
    changes = session_published(ctx, sid, ev.SESSION_SLIDE_CHANGED)
    assert len(changes) == 1 and changes[0].data == {
        "session_id": sid,
        "index": 1,
        "previous_index": 0,
        "reason": "command",
        "matched": "and now, next slide",
        "by": None,
    }
    # The segment is tagged with the slide that was showing while it was spoken.
    assert (await segments(ctx, sid))[0].slide_index == 0
    # The tracker made the move itself, so it is not re-synced.
    assert tracker.synced == [] and len(trackers) == 1

    # An out-of-range decision is clamped and the tracker is pulled back in line.
    await service.ingest_transcript(ctx, sid, final("jump to the end"))
    assert (await service.get_session(ctx, sid)).current_slide_index == 2 and tracker.synced == [2]


async def test_manual_slide_changes_clamp_publish_and_sync_the_tracker(ctx, live, trackers):
    sid = live.session.id
    await service.ingest_transcript(ctx, sid, interim("warm up"))  # builds the tracker
    tracker = trackers[0]

    moved = await service.set_slide(ctx, sid, 2, by="prt_host")
    assert moved.current_slide_index == 2 and tracker.synced == [2]
    again = await service.set_slide(ctx, sid, 2)
    assert again.current_slide_index == 2
    assert (await service.set_slide(ctx, sid, 99)).current_slide_index == 2
    assert (await service.set_slide(ctx, sid, -4)).current_slide_index == 0

    changes = [e.data for e in session_published(ctx, sid, ev.SESSION_SLIDE_CHANGED)]
    assert changes == [
        {"session_id": sid, "index": 2, "previous_index": 0, "reason": "manual", "matched": None, "by": "prt_host"},
        {"session_id": sid, "index": 0, "previous_index": 2, "reason": "manual", "matched": None, "by": None},
    ]
    assert tracker.synced == [2, 0]


async def test_set_slide_without_a_deck_stays_on_zero(ctx, event, host):
    sid = (await service.create_session(ctx, event=event, host=host)).session.id
    assert (await service.set_slide(ctx, sid, 3)).current_slide_index == 0
    assert session_published(ctx, sid, ev.SESSION_SLIDE_CHANGED) == []


async def test_set_slide_sees_slides_added_after_the_runtime_loaded(ctx, live, deck, trackers):
    sid = live.session.id
    assert (await service.set_slide(ctx, sid, 3)).current_slide_index == 2
    async with ctx.db() as db:
        db.add(Slide(presentation_id=deck.id, position=3, title="Slide 3", keywords=["kw3"]))
        await db.commit()
    assert (await service.set_slide(ctx, sid, 3)).current_slide_index == 3
    await service.ingest_transcript(ctx, sid, interim("hi"))
    assert [c.index for c in trackers[-1].cues] == [0, 1, 2, 3]


async def test_settings_or_deck_change_rebuilds_the_tracker(ctx, live, event, trackers):
    sid = live.session.id
    await service.ingest_transcript(ctx, sid, interim("one"))
    assert len(trackers) == 1

    await service.update_session(ctx, sid, title="Renamed")
    await service.ingest_transcript(ctx, sid, interim("two"))
    assert len(trackers) == 1  # title changes do not touch the tracker

    updated = await service.update_session(ctx, sid, settings={"auto_advance": "off", "advance_cooldown_ms": 100})
    assert updated.settings["auto_advance"] == "off" and updated.settings["language"] == "en"  # merged
    await service.ingest_transcript(ctx, sid, interim("three"))
    assert len(trackers) == 2 and (trackers[1].config.mode, trackers[1].config.cooldown_ms) == ("off", 100)

    await service.set_slide(ctx, sid, 2)
    other_deck = await make_deck(ctx, event.id, count=2, title="Other")
    swapped = await service.update_session(ctx, sid, presentation_id=other_deck.id)
    assert swapped.presentation_id == other_deck.id and swapped.current_slide_index == 0
    await service.ingest_transcript(ctx, sid, interim("four"))
    assert len(trackers) == 3 and [c.index for c in trackers[2].cues] == [0, 1] and trackers[2].current_index == 0

    updates = session_published(ctx, sid, ev.SESSION_UPDATED)
    assert [u.data["session"]["title"] for u in updates][-3:] == ["Renamed", "Renamed", "Renamed"]
    assert updates[-1].data["session"]["presentation_id"] == other_deck.id

    # No-op patches publish nothing.
    before = len(session_published(ctx, sid))
    await service.update_session(ctx, sid, title="Renamed", settings={"auto_advance": "off"})
    assert len(session_published(ctx, sid)) == before

    detached = await service.update_session(ctx, sid, presentation_id=None)
    assert detached.presentation_id is None
    with pytest.raises(ValidationError):
        await service.update_session(ctx, sid, settings={"auto_advance": "sometimes"})
    foreign = await make_deck(ctx, (await create_event(ctx, "Other")).id)
    with pytest.raises(service.DeckNotFound):
        await service.update_session(ctx, sid, presentation_id=foreign.id)


async def test_tracker_failures_do_not_lose_the_transcript(ctx, live, trackers, monkeypatch):
    sid = live.session.id
    await service.ingest_transcript(ctx, sid, final("boom goes the tracker"))
    assert [s.text for s in await segments(ctx, sid)] == ["boom goes the tracker"]

    def broken(*_args, **_kwargs):
        raise RuntimeError("cannot build")

    monkeypatch.setattr(service, "build_tracker", broken)
    await service.update_session(ctx, sid, settings={"auto_advance": "commands"})  # forces a rebuild
    await service.ingest_transcript(ctx, sid, final("still transcribed"))
    assert [s.seq for s in await segments(ctx, sid)] == [1, 2]
    assert session_published(ctx, sid, ev.SESSION_SLIDE_CHANGED) == []


async def test_no_tracker_without_a_deck(ctx, event, host, trackers):
    sid = (await service.create_session(ctx, event=event, host=host)).session.id
    await service.start_session(ctx, sid)
    await service.ingest_transcript(ctx, sid, final("next slide"))
    assert trackers == [] and len(await segments(ctx, sid)) == 1


async def test_real_tracker_advances_on_a_spoken_command(ctx, event, host, deck):
    try:
        SlideTracker([SlideCue(index=0), SlideCue(index=1)], TrackerConfig(mode="commands"))
    except NotImplementedError:
        pytest.skip("SlideTracker is not implemented yet")
    settings = SessionSettings(auto_advance="commands")
    admission = await service.create_session(ctx, event=event, host=host, presentation_id=deck.id, settings=settings)
    sid = admission.session.id
    await service.start_session(ctx, sid)

    await service.ingest_transcript(ctx, sid, final("Okay, next slide please."))
    assert (await service.get_session(ctx, sid)).current_slide_index == 1
    change = session_published(ctx, sid, ev.SESSION_SLIDE_CHANGED)[-1].data
    assert change["index"] == 1 and change["reason"] == "command"


# --- state, keyterms, presence -----------------------------------------------------------------------


async def test_session_state_snapshot_and_listener_view(ctx, live, trackers):
    sid = live.session.id
    listener = await service.join_session(ctx, live.session.join_code, "Ada")
    await service.ingest_transcript(ctx, sid, final("first"))
    await service.ingest_transcript(ctx, sid, final("second"))

    state = await service.get_session_state(ctx, sid)
    assert state["session"]["id"] == sid and state["session"]["status"] == "live"
    assert [p["role"] for p in state["participants"]] == ["host", "listener"]
    assert state["slide_count"] == 3
    assert state["slides"][1] == {
        "id": state["slides"][1]["id"],
        "position": 1,
        "title": "Slide 1",
        "keywords": ["kw1"],
    }
    assert [s["text"] for s in state["recent_transcript"]] == ["first", "second"]

    client = await service.authenticate_session_client(ctx, sid, listener.token)
    assert [s["seq"] for s in (await service.get_client_state(ctx, client))["recent_transcript"]] == [1, 2]
    await service.update_session(ctx, sid, settings={"show_transcript_to_listeners": False})
    assert (await service.get_client_state(ctx, client))["recent_transcript"] == []
    host_client = await service.authenticate_session_client(ctx, sid, live.token)
    assert len((await service.get_client_state(ctx, host_client))["recent_transcript"]) == 2

    with pytest.raises(service.SessionNotFound):
        await service.get_session_state(ctx, "ses_missing")


async def test_keyterms_are_deduplicated_deck_keywords(ctx, event, host, deck):
    async with ctx.db() as db:
        db.add(Slide(presentation_id=deck.id, position=3, keywords=["KW1 ", "budget", "Budget", ""]))
        await db.commit()
    sid = (await service.create_session(ctx, event=event, host=host, presentation_id=deck.id)).session.id
    assert await service.get_keyterms(ctx, sid) == ["kw0", "kw1", "kw2", "budget"]
    assert await service.get_keyterms(ctx, (await service.create_session(ctx, event=event, host=host)).session.id) == []


async def test_participant_connections_are_counted_per_socket(ctx, event, host):
    admission = await service.create_session(ctx, event=event, host=host)
    sid, pid = admission.session.id, admission.participant.id

    await service.set_participant_connected(ctx, pid, True)  # control socket
    await service.set_participant_connected(ctx, pid, True)  # audio socket
    joined = session_published(ctx, sid, ev.SESSION_PARTICIPANT_JOINED)
    assert (
        len(joined) == 1 and joined[0].data["participant"]["id"] == pid and joined[0].data["participant"]["connected"]
    )

    await service.set_participant_connected(ctx, pid, False)
    assert session_published(ctx, sid, ev.SESSION_PARTICIPANT_LEFT) == []
    async with ctx.db() as db:
        assert (await db.get(Participant, pid)).connected is True

    await service.set_participant_connected(ctx, pid, False)
    left = session_published(ctx, sid, ev.SESSION_PARTICIPANT_LEFT)
    assert len(left) == 1 and left[0].data == {"session_id": sid, "participant_id": pid}
    async with ctx.db() as db:
        row = await db.get(Participant, pid)
    assert row.connected is False and row.left_at is not None

    await service.set_participant_connected(ctx, pid, False)  # unbalanced: ignored
    await service.set_participant_connected(ctx, "prt_unknown", True)  # unknown: ignored
    assert len(session_published(ctx, sid)) == 2
