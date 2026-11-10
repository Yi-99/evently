"""Chat orchestrator: streaming envelopes, tools, history replay, follow-ups, failure."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date

from sqlalchemy import select

from app.agents import orchestrator, presentations, venues
from app.agents.orchestrator import store
from app.agents.orchestrator.prompts import SYSTEM_PROMPT
from app.agents.orchestrator.stream import ReplyStream
from app.core.ids import utcnow
from app.integrations import outreach
from app.llm.base import LLMError
from app.llm.scripted import ScriptedTurn
from app.models import ChatMessage, EmailMessage, Event, LumaEvent, Presentation, Slide
from app.realtime import events as ev
from app.schemas.outreach import EmailDraftIn, LumaEventDraft
from app.schemas.presentation import PresentationBrief
from app.schemas.venue import VenueCriteria
from app.services import board
from tests.helpers import create_event, published

TOOL_NAMES = {
    "search_venue_memory",
    "search_event_history",
    "search_venues",
    "refine_venue_card",
    "update_requirements",
    "get_card",
    "add_note",
    "remove_card",
    "create_comparison",
    "create_presentation",
    "edit_presentation",
    "draft_email",
    "send_email",
    "draft_luma_event",
    "publish_luma_event",
    "invite_luma_guests",
}


# -- helpers --------------------------------------------------------------------


async def chat(ctx, event, text, context=None):
    """Send one message and wait for the orchestrator run to finish."""
    message, run = await orchestrator.handle_user_message(
        ctx, event_id=event.id, user_id="usr_test", text=text, context=context
    )
    return message, await ctx.runtime.wait(run.id, timeout=5)


async def eventually(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def rows(ctx, event_id) -> list[ChatMessage]:
    async with ctx.db() as session:
        result = await session.execute(
            select(ChatMessage).where(ChatMessage.event_id == event_id).order_by(ChatMessage.created_at)
        )
        return list(result.scalars())


def chat_types(ctx, event_id) -> list[str]:
    return [e.type for e in published(ctx, event_id, "chat.")]


def text_of(message: dict) -> str:
    content = message["content"]
    if isinstance(content, str):
        return content
    return "\n".join(b["text"] for b in content if b.get("type") == "text")


class FakeVenues:
    """Stand-in for `app.agents.venues` that spawns real runs so watchers have something to wait for."""

    def __init__(self) -> None:
        self.searches: list[dict] = []
        self.refines: list[dict] = []
        self.release = asyncio.Event()
        self.release.set()
        self.error: str | None = None
        self.summary = "Found 3 venues. The Foundry ranks first."

    async def start_venue_search(self, ctx, *, event_id, criteria, parent_run_id=None):
        self.searches.append({"event_id": event_id, "criteria": criteria, "parent_run_id": parent_run_id})

        async def body(run):
            await self.release.wait()
            if self.error:
                raise RuntimeError(self.error)
            return {"card_ids": [], "summary_card_id": None, "summary": self.summary}

        return await ctx.runtime.spawn(
            event_id=event_id,
            kind="venue_search",
            title="Venue search",
            fn=body,
            parent_run_id=parent_run_id,
            limited=False,
        )

    async def start_card_refine(self, ctx, *, card_id, instruction, parent_run_id=None):
        self.refines.append({"card_id": card_id, "instruction": instruction, "parent_run_id": parent_run_id})
        card = await board.get_card(ctx, card_id)

        async def body(run):
            return {}

        return await ctx.runtime.spawn(
            event_id=card.event_id, kind="card_refine", title="Refine", fn=body, parent_run_id=parent_run_id
        )


class FakePresentations:
    def __init__(self) -> None:
        self.builds: list[dict] = []
        self.edits: list[dict] = []

    async def start_presentation_build(self, ctx, *, event_id, brief, parent_run_id=None):
        self.builds.append({"event_id": event_id, "brief": brief, "parent_run_id": parent_run_id})
        deck = Presentation(event_id=event_id, title=brief.title or "Untitled deck", brief=brief.model_dump())
        async with ctx.db() as session:
            session.add(deck)
            await session.commit()

        async def body(run):
            return {"presentation_id": deck.id}

        run = await ctx.runtime.spawn(
            event_id=event_id,
            kind="presentation_build",
            title="Deck",
            fn=body,
            parent_run_id=parent_run_id,
            limited=False,
        )
        return deck, run

    async def start_presentation_edit(self, ctx, *, presentation_id, instruction, slide_id=None, parent_run_id=None):
        self.edits.append({"presentation_id": presentation_id, "instruction": instruction, "slide_id": slide_id})
        async with ctx.db() as session:
            deck = await session.get(Presentation, presentation_id)

        async def body(run):
            return {}

        return await ctx.runtime.spawn(
            event_id=deck.event_id, kind="presentation_edit", title="Edit", fn=body, parent_run_id=parent_run_id
        )


class FakeOutreach:
    """Stand-in for `app.integrations.outreach`; persists rows so the orchestrator's lookups work."""

    def __init__(self) -> None:
        self.drafts: list[EmailDraftIn] = []
        self.sent: list[str] = []
        self.luma_drafts: list[LumaEventDraft] = []
        self.published = 0
        self.invited: list[list[str]] = []
        self.date_missing = False

    async def draft_email(self, ctx, *, event_id, draft, created_by="user"):
        self.drafts.append(draft)
        email = EmailMessage(
            event_id=event_id,
            kind=draft.kind,
            to=list(draft.to),
            cc=list(draft.cc),
            subject=draft.subject,
            body_text=draft.body_text,
            related_card_id=draft.related_card_id,
            created_by=created_by,
        )
        async with ctx.db() as session:
            session.add(email)
            await session.commit()
        return email

    async def send_email(self, ctx, email_id):
        self.sent.append(email_id)
        async with ctx.db() as session:
            email = await session.get(EmailMessage, email_id)
            email.status = "sent"
            await session.commit()
        return email

    async def build_luma_draft_from_event(self, ctx, *, event_id, venue_card_id=None):
        if self.date_missing:
            raise ValueError("Set the event date first.")
        return LumaEventDraft(name="Test event", start_at="2026-12-05T18:00:00Z", venue_card_id=venue_card_id)

    async def draft_luma_event(self, ctx, *, event_id, draft, created_by="user"):
        self.luma_drafts.append(draft)
        async with ctx.db() as session:
            luma = (await session.execute(select(LumaEvent).where(LumaEvent.event_id == event_id))).scalar_one_or_none()
            if luma is None:
                luma = LumaEvent(event_id=event_id)
                session.add(luma)
            luma.draft = draft.model_dump(mode="json")
            luma.status = "draft"
            await session.commit()
        return luma

    async def publish_luma_event(self, ctx, event_id):
        self.published += 1
        async with ctx.db() as session:
            luma = (await session.execute(select(LumaEvent).where(LumaEvent.event_id == event_id))).scalar_one()
            luma.status, luma.url = "published", "https://lu.ma/test-event"
            await session.commit()
        return luma

    async def invite_luma_guests(self, ctx, event_id, emails):
        self.invited.append(emails)
        async with ctx.db() as session:
            luma = (await session.execute(select(LumaEvent).where(LumaEvent.event_id == event_id))).scalar_one()
            luma.guests_invited += len(emails)
            await session.commit()
        return luma


def patch_outreach(monkeypatch) -> FakeOutreach:
    fake = FakeOutreach()
    for name in (
        "draft_email",
        "send_email",
        "build_luma_draft_from_event",
        "draft_luma_event",
        "publish_luma_event",
        "invite_luma_guests",
    ):
        monkeypatch.setattr(outreach, name, getattr(fake, name))
    return fake


def tool_results(reply: ChatMessage) -> list[dict]:
    return [
        b for m in reply.llm_messages if m["role"] == "user" for b in m["content"] if b.get("type") == "tool_result"
    ]


# -- plain reply ----------------------------------------------------------------


async def test_plain_reply_streams_and_completes(ctx, event, llm):
    llm.on_run("orchestrator", [ScriptedTurn(text="Hello! Tell me about the event you're planning.")])

    user_message, run = await chat(ctx, event, "hi")

    assert run.status == "succeeded"
    envelopes = published(ctx, event.id, "chat.")
    types = [e.type for e in envelopes]
    assert types[0] == types[1] == ev.CHAT_MESSAGE_CREATED
    assert types[-1] == ev.CHAT_MESSAGE_COMPLETED
    assert set(types[2:-1]) == {ev.CHAT_DELTA}
    assert envelopes[0].data["message"]["id"] == user_message.id and envelopes[0].data["message"]["role"] == "user"
    assistant = envelopes[1].data["message"]
    assert assistant["role"] == "assistant" and assistant["status"] == "streaming"
    streamed = "".join(e.data["text"] for e in envelopes if e.type == ev.CHAT_DELTA)
    completed = envelopes[-1].data["message"]
    assert completed["id"] == assistant["id"] and completed["status"] == "complete"
    assert completed["content"] == streamed == "Hello! Tell me about the event you're planning."
    assert all(e.data["message_id"] == assistant["id"] for e in envelopes if e.type == ev.CHAT_DELTA)

    stored = await rows(ctx, event.id)
    assert [m.role for m in stored] == ["user", "assistant"]
    assert stored[1].llm_messages == [
        {"role": "assistant", "content": [{"type": "text", "text": completed["content"]}]}
    ]
    assert stored[1].meta["reply_to"] == user_message.id and stored[1].meta["agent_run_id"] == run.id
    assert run.output["message_id"] == assistant["id"]

    call = llm.calls_for("orchestrator")[0]
    assert call.tier == "orchestrator" and call.system == SYSTEM_PROMPT and set(call.tool_names) == TOOL_NAMES
    assert call.messages[0]["role"] == "user"


# -- venue search tool + follow-up ----------------------------------------------


async def test_search_tool_calls_entrypoint_and_saves_requirements(ctx, llm, monkeypatch):
    event = await create_event(ctx, requirements={"budget": "under $10k"})
    fake = FakeVenues()
    fake.release.clear()
    monkeypatch.setattr(venues, "start_venue_search", fake.start_venue_search)
    llm.on_run(
        "orchestrator",
        [
            ScriptedTurn(
                text="Let me start a search.",
                tool_calls=[
                    ("search_venues", {"location": "Austin, TX", "headcount": 120, "event_date": "2026-12-05"})
                ],
            ),
            ScriptedTurn(text="I've started a search around Austin for 120 guests on 5 December."),
        ],
    )

    _, run = await chat(ctx, event, "Find venues in Austin for 120 people on Dec 5")

    assert run.status == "succeeded"
    [search] = fake.searches
    criteria = search["criteria"]
    assert isinstance(criteria, VenueCriteria)
    assert (criteria.location, criteria.headcount, criteria.event_date) == ("Austin, TX", 120, date(2026, 12, 5))
    assert criteria.budget == "under $10k", "fields not passed fall back to saved requirements"
    assert search["parent_run_id"] == run.id and search["event_id"] == event.id
    async with ctx.db() as session:
        saved = (await session.get(Event, event.id)).requirements
    assert saved["location"] == "Austin, TX" and saved["headcount"] == 120 and saved["budget"] == "under $10k"

    tool_events = [e.data for e in published(ctx, event.id, ev.CHAT_TOOL)]
    assert [(t["name"], t["status"], t["is_error"]) for t in tool_events] == [
        ("search_venues", "started", False),
        ("search_venues", "finished", False),
    ]
    assert tool_events[0]["summary"] == "Venue search near Austin, TX for 120 guests"
    search_run_id = run.output["launched_run_ids"][0]
    assert (await ctx.runtime.get(search_run_id)).kind == "venue_search"

    completed = [e for e in published(ctx, event.id, ev.CHAT_MESSAGE_COMPLETED)][0].data["message"]
    assert (
        completed["content"]
        == "Let me start a search.\n\nI've started a search around Austin for 120 guests on 5 December."
    )
    assert completed["meta"]["tools"] == [
        {"name": "search_venues", "summary": "Venue search near Austin, TX for 120 guests", "is_error": False}
    ]

    # Nothing posted while the search is running; the note arrives when it finishes.
    assert len(await rows(ctx, event.id)) == 2
    fake.release.set()
    await eventually(lambda: len(published(ctx, event.id, ev.CHAT_MESSAGE_CREATED)) == 3)
    stored = await rows(ctx, event.id)
    note = stored[-1]
    assert note.role == "assistant" and note.status == "complete" and note.content == fake.summary
    assert note.meta == {
        "kind": "run_completed",
        "agent_run_id": search_run_id,
        "run_kind": "venue_search",
        "run_status": "succeeded",
    }
    assert note.llm_messages == [{"role": "assistant", "content": [{"type": "text", "text": fake.summary}]}]

    # The next turn sees the note in its history.
    llm.on_run("orchestrator", [ScriptedTurn(text="The Foundry came out on top.")])
    await chat(ctx, event, "which one is best?")
    second = llm.calls_for("orchestrator")[1].messages
    assert fake.summary in [text_of(m) for m in second if m["role"] == "assistant"]


async def test_follow_up_reports_failure(ctx, event, llm, monkeypatch):
    fake = FakeVenues()
    fake.error = "scout exploded"
    monkeypatch.setattr(venues, "start_venue_search", fake.start_venue_search)
    llm.on_run(
        "orchestrator",
        [ScriptedTurn(tool_calls=[("search_venues", {"location": "Lisbon"})]), ScriptedTurn(text="Started.")],
    )

    await chat(ctx, event, "venues in Lisbon please")

    await eventually(lambda: len(published(ctx, event.id, ev.CHAT_MESSAGE_CREATED)) == 3)
    note = (await rows(ctx, event.id))[-1]
    assert note.meta["run_status"] == "failed"
    assert note.content.startswith("The venue search failed: RuntimeError: scout exploded")


# -- history replay ----------------------------------------------------------------


async def test_history_replays_tool_pairs_and_starts_with_user(ctx, event, llm, monkeypatch):
    fake = FakeVenues()
    fake.release.clear()  # the search stays in progress, so no follow-up note lands between the turns
    monkeypatch.setattr(venues, "start_venue_search", fake.start_venue_search)
    llm.queue_run(
        "orchestrator",
        [
            ScriptedTurn(tool_calls=[("search_venues", {"location": "Berlin", "headcount": 40})]),
            ScriptedTurn(text="Started."),
        ],
    )
    llm.queue_run("orchestrator", [ScriptedTurn(text="It is still running.")])

    await chat(ctx, event, "venues in Berlin for 40")
    await chat(ctx, event, "how is it going?")

    second = llm.calls_for("orchestrator")[1].messages
    assert second[0]["role"] == "user" and text_of(second[0]) == "venues in Berlin for 40"
    assert "<workspace>" not in text_of(second[0]), "old turns replay only the planner's text"
    tool_uses = [b for m in second if m["role"] == "assistant" for b in m["content"] if b["type"] == "tool_use"]
    tool_results = [b for m in second if m["role"] == "user" for b in m["content"] if b.get("type") == "tool_result"]
    assert len(tool_uses) == len(tool_results) == 1
    assert tool_uses[0]["name"] == "search_venues" and tool_results[0]["tool_use_id"] == tool_uses[0]["id"]
    assert second.index(next(m for m in second if m["role"] == "user" and m is not second[0])) == 2, (
        "tool_result follows its tool_use"
    )
    assert all(b.get("type") not in {"thinking", "redacted_thinking"} for m in second for b in m["content"])
    assert second[-1]["role"] == "user" and "<workspace>" in text_of(second[-1])
    assert text_of(second[-1]).endswith("how is it going?")
    assert "Started." in text_of(second[-2])


async def test_history_window_cuts_at_exchange_boundaries(ctx, event):
    async def exchange(n: int) -> None:
        asked = await store.create_message(ctx, event_id=event.id, role="user", content=f"q{n}")
        await store.create_message(
            ctx,
            event_id=event.id,
            role="assistant",
            content=f"a{n}",
            llm_messages=[store.text_message("assistant", f"a{n}")],
            meta={"reply_to": asked.id},
        )

    await exchange(1)
    await store.create_message(
        ctx, event_id=event.id, role="assistant", content="note", llm_messages=[store.text_message("assistant", "note")]
    )
    await exchange(2)
    await exchange(3)
    await store.create_message(ctx, event_id=event.id, role="user", content="failed turn")
    await store.create_message(ctx, event_id=event.id, role="assistant", status="error", meta={"reply_to": "missing"})

    full = await store.load_history(ctx, event.id)
    assert [text_of(m) for m in full] == ["q1", "a1", "note", "q2", "a2", "q3", "a3"]

    windowed = await store.load_history(ctx, event.id, max_rows=4)
    assert [text_of(m) for m in windowed] == ["q2", "a2", "q3", "a3"], "a leading follow-up note is dropped"
    assert windowed[0]["role"] == "user"


# -- workspace snapshot -------------------------------------------------------------


async def test_board_snapshot_is_in_user_turn_not_system_prompt(ctx, event, llm):
    card = await board.create_card(
        ctx,
        event_id=event.id,
        kind="venue",
        title="The Foundry",
        data={"score": {"overall": 82}, "rank": 1, "summary": "Industrial hall"},
    )
    llm.on_run("orchestrator", [ScriptedTurn(text="Sure.")])

    await chat(ctx, event, "tell me about this one", context={"selected_card_ids": [card.id, "card_bogus"]})

    call = llm.calls_for("orchestrator")[0]
    today = utcnow().date().isoformat()
    assert card.id not in call.system and today not in call.system and call.system == SYSTEM_PROMPT
    user_turn = call.messages[-1]
    assert user_turn["role"] == "user" and len(user_turn["content"]) == 2
    block, text = (b["text"] for b in user_turn["content"])
    assert text == "tell me about this one"
    assert block.startswith("<workspace>") and block.endswith("</workspace>")
    assert today in block and event.name in block
    assert f'- {card.id} | venue | "The Foundry" | ready | score 82 | rank 1' in block
    assert f"Selected by the planner: {card.id}" in block and "card_bogus" not in block
    assert "Industrial hall" not in block, "the snapshot is compact; full data comes via get_card"


# -- serialisation of turns --------------------------------------------------------------


async def test_second_message_waits_for_first_turn(ctx, event, llm):
    gate = asyncio.Event()

    async def script(call):
        if len(llm.calls_for("orchestrator")) == 1:
            await gate.wait()
            return [ScriptedTurn(text="first reply")]
        return [ScriptedTurn(text="second reply")]

    llm.on_run("orchestrator", script)

    _, run1 = await orchestrator.handle_user_message(ctx, event_id=event.id, user_id="u", text="one")
    _, run2 = await orchestrator.handle_user_message(ctx, event_id=event.id, user_id="u", text="two")
    await eventually(lambda: len(llm.calls_for("orchestrator")) == 1)
    await asyncio.sleep(0.05)
    assert len(llm.calls_for("orchestrator")) == 1, "the second turn waits for the first"
    gate.set()
    assert (await ctx.runtime.wait(run1.id, timeout=5)).status == "succeeded"
    assert (await ctx.runtime.wait(run2.id, timeout=5)).status == "succeeded"

    second = llm.calls_for("orchestrator")[1].messages
    assert [text_of(m) for m in second[:2]] == ["one", "first reply"]
    envelopes = published(ctx, event.id, "chat.")
    first_completed = [e.type for e in envelopes].index(ev.CHAT_MESSAGE_COMPLETED)
    assistant_created = [
        i
        for i, e in enumerate(envelopes)
        if e.type == ev.CHAT_MESSAGE_CREATED and e.data["message"]["role"] == "assistant"
    ]
    assert len(assistant_created) == 2
    assert assistant_created[0] < first_completed < assistant_created[1], "one streaming reply at a time"


# -- failure ----------------------------------------------------------------------


async def test_llm_failure_marks_message_and_publishes_error(ctx, event, llm):
    def script(call):
        raise LLMError("Claude API error 500: upstream")

    llm.on_run("orchestrator", script)

    user_message, run = await chat(ctx, event, "hello?")

    assert run.status == "failed" and "upstream" in run.error
    types = chat_types(ctx, event.id)
    assert types == [ev.CHAT_MESSAGE_CREATED, ev.CHAT_MESSAGE_CREATED, ev.CHAT_ERROR]
    error = published(ctx, event.id, ev.CHAT_ERROR)[0].data
    reply = (await rows(ctx, event.id))[1]
    assert error == {"message_id": reply.id, "error": "Claude API error 500: upstream"}
    assert reply.status == "error" and reply.meta["error"] == error["error"] and reply.llm_messages == []

    # The failed exchange keeps the planner's text in history so "try again" has a referent.
    llm.on_run("orchestrator", [ScriptedTurn(text="Hello!")])
    await chat(ctx, event, "try again")
    second = llm.calls_for("orchestrator")[1].messages
    assert len(second) == 2 and [m["role"] for m in second] == ["user", "user"]
    assert text_of(second[0]) == "hello?" and text_of(second[1]).endswith("try again")


# -- board tools -----------------------------------------------------------------


async def test_card_tools_validate_ids_against_event(ctx, event, llm):
    other = await create_event(ctx, name="Other")
    foreign = await board.create_card(ctx, event_id=other.id, kind="venue", title="Elsewhere")
    note = await board.create_card(ctx, event_id=event.id, kind="note", title="Todo")
    llm.on_run(
        "orchestrator",
        [
            ScriptedTurn(
                tool_calls=[
                    ("get_card", {"card_id": foreign.id}),
                    ("refine_venue_card", {"card_id": note.id, "instruction": "check parking"}),
                    ("get_card", {"card_id": note.id}),
                ]
            ),
            ScriptedTurn(text="Done."),
        ],
    )

    _, run = await chat(ctx, event, "look at those")

    assert run.status == "succeeded"
    reply = (await rows(ctx, event.id))[1]
    results = [b for m in reply.llm_messages if m["role"] == "user" for b in m["content"]]
    assert [r["is_error"] for r in results] == [True, True, False]
    assert "no card" in results[0]["content"].lower()
    assert "note card, not a venue card" in results[1]["content"]
    finished = [e.data for e in published(ctx, event.id, ev.CHAT_TOOL) if e.data["status"] == "finished"]
    assert [(f["is_error"], f["summary"]) for f in finished] == [
        (True, "Read a card (failed)"),
        (True, "Refine “Todo” (failed)"),
        (False, "Read “Todo”"),
    ]
    assert await board.get_card(ctx, foreign.id) is not None


async def test_board_tools_create_compare_and_remove_cards(ctx, event, llm, monkeypatch):
    fake = FakeVenues()
    monkeypatch.setattr(venues, "start_card_refine", fake.start_card_refine)
    a = await board.create_card(ctx, event_id=event.id, kind="venue", title="A", data={"summary": "big"})
    b = await board.create_card(ctx, event_id=event.id, kind="venue", title="B", data={"summary": "small"})
    old = await board.create_card(ctx, event_id=event.id, kind="note", title="Old")
    llm.on_run(
        "orchestrator",
        [
            ScriptedTurn(tool_calls=[("get_card", {"card_id": a.id}), ("get_card", {"card_id": b.id})]),
            ScriptedTurn(
                tool_calls=[
                    (
                        "create_comparison",
                        {
                            "title": "A vs B",
                            "card_ids": [a.id, b.id],
                            "rows": [{"criterion": "Size", "values": ["big", "small"]}],
                            "summary": "A is bigger.",
                            "recommended_card_id": a.id,
                        },
                    ),
                    (
                        "create_comparison",
                        {
                            "title": "bad",
                            "card_ids": [a.id, b.id],
                            "rows": [{"criterion": "Size", "values": ["big"]}],
                            "summary": "x",
                        },
                    ),
                    ("add_note", {"title": "Next steps", "text": "Call A about catering."}),
                    ("remove_card", {"card_id": old.id}),
                    ("refine_venue_card", {"card_id": a.id, "instruction": "confirm capacity"}),
                    ("update_requirements", {"headcount": 80, "budget": None}),
                ]
            ),
            ScriptedTurn(text="Compared A and B, added a note and removed the old one."),
        ],
    )
    async with ctx.db() as session:
        row = await session.get(Event, event.id)
        row.requirements = {"location": "Austin", "budget": "lots"}
        await session.commit()

    _, run = await chat(ctx, event, "compare A and B, note next steps, drop the old note")

    assert run.status == "succeeded"
    cards = {c.title: c for c in await board.list_cards(ctx, event.id)}
    assert "Old" not in cards
    comparison = cards["A vs B"]
    assert comparison.kind == "comparison" and comparison.created_by == "agent"
    assert comparison.data == {
        "card_ids": [a.id, b.id],
        "venues": [{"card_id": a.id, "title": "A"}, {"card_id": b.id, "title": "B"}],
        "rows": [{"criterion": "Size", "values": ["big", "small"]}],
        "summary": "A is bigger.",
        "recommended_card_id": a.id,
    }
    assert "bad" not in cards
    assert cards["Next steps"].kind == "note" and cards["Next steps"].data == {"text": "Call A about catering."}
    assert fake.refines == [{"card_id": a.id, "instruction": "confirm capacity", "parent_run_id": run.id}]
    async with ctx.db() as session:
        assert (await session.get(Event, event.id)).requirements == {"location": "Austin", "headcount": 80}
    reply = (await rows(ctx, event.id))[1]
    results = [b for m in reply.llm_messages if m["role"] == "user" for b in m["content"]]
    bad = next(r for r in results if r["is_error"])
    assert "1 values for 2 cards" in bad["content"]
    assert "big" in results[0]["content"], "get_card returns the full data"
    assert [e.type for e in published(ctx, event.id, "board.")].count(ev.BOARD_CARD_DELETED) == 1


async def test_presentation_tools(ctx, event, llm, monkeypatch):
    fake = FakePresentations()
    monkeypatch.setattr(presentations, "start_presentation_build", fake.start_presentation_build)
    monkeypatch.setattr(presentations, "start_presentation_edit", fake.start_presentation_edit)
    venue = await board.create_card(ctx, event_id=event.id, kind="venue", title="A")
    deck = Presentation(event_id=event.id, title="Kickoff", status="ready")
    other_deck = Presentation(event_id=(await create_event(ctx, name="Other")).id, title="Not mine", status="ready")
    async with ctx.db() as session:
        session.add_all([deck, other_deck])
        await session.commit()
        slide = Slide(presentation_id=deck.id, position=0, title="Agenda", status="ready")
        session.add(slide)
        await session.commit()
    llm.on_run(
        "orchestrator",
        [
            ScriptedTurn(
                tool_calls=[
                    (
                        "create_presentation",
                        {"prompt": "Pitch the top venues", "title": "Venue pitch", "card_ids": [venue.id]},
                    ),
                    ("edit_presentation", {"presentation_id": deck.id, "instruction": "shorter", "slide_id": slide.id}),
                    ("edit_presentation", {"presentation_id": other_deck.id, "instruction": "nope"}),
                    ("edit_presentation", {"presentation_id": deck.id, "instruction": "nope", "slide_id": "sld_x"}),
                ]
            ),
            ScriptedTurn(text="Building the deck; the agenda slide is being shortened."),
        ],
    )

    _, run = await chat(ctx, event, "make a pitch deck and shorten the agenda slide")

    assert run.status == "succeeded"
    [build] = fake.builds
    assert isinstance(build["brief"], PresentationBrief) and build["brief"].card_ids == [venue.id]
    assert build["brief"].title == "Venue pitch" and build["parent_run_id"] == run.id
    assert fake.edits == [{"presentation_id": deck.id, "instruction": "shorter", "slide_id": slide.id}]
    reply = (await rows(ctx, event.id))[1]
    results = [b for m in reply.llm_messages if m["role"] == "user" for b in m["content"]]
    assert [r["is_error"] for r in results] == [False, False, True, True]
    summaries = [e.data["summary"] for e in published(ctx, event.id, ev.CHAT_TOOL) if e.data["status"] == "started"]
    assert summaries[:2] == ["Build presentation “Venue pitch”", "Edit slide “Agenda”"]

    await eventually(lambda: len(published(ctx, event.id, ev.CHAT_MESSAGE_CREATED)) == 3)
    assert (await rows(ctx, event.id))[-1].content == "Your presentation is ready."

    # The new deck and its slides show up in the next snapshot.
    llm.on_run("orchestrator", [ScriptedTurn(text="ok")])
    await chat(ctx, event, "thanks")
    block = text_of(llm.calls_for("orchestrator")[1].messages[-1])
    assert f'{deck.id} | "Kickoff" | ready | 1 slides' in block and f'1. {slide.id} "Agenda"' in block
    assert "Venue pitch" in block


# -- delta coalescing ------------------------------------------------------------


async def test_deltas_are_coalesced(ctx, event):
    stream = ReplyStream(ctx, event.id, "msg_x")
    text = "".join(chr(97 + i % 26) for i in range(500))
    for ch in text:
        await stream.on_text(ch)
    await asyncio.sleep(0.15)  # lets the timer flush the tail
    assert len(published(ctx, event.id, ev.CHAT_DELTA)) == 500 // 48 + 1

    await stream.on_text("!")
    await stream.flush()
    deltas = published(ctx, event.id, ev.CHAT_DELTA)
    assert "".join(e.data["text"] for e in deltas) == text + "!" == stream.text
    assert all(e.data["message_id"] == "msg_x" for e in deltas)


# -- outreach ---------------------------------------------------------------------


async def test_email_is_drafted_then_sent_only_when_scripted(ctx, event, llm, monkeypatch):
    fake = patch_outreach(monkeypatch)
    venue = await board.create_card(ctx, event_id=event.id, kind="venue", title="The Foundry")
    draft_args = {
        "kind": "venue_inquiry",
        "to": ["events@foundry.example"],
        "subject": "Availability for 5 December",
        "body_text": "Hello, we are planning a 120-person offsite...",
        "related_card_id": venue.id,
    }
    llm.queue_run(
        "orchestrator",
        [
            ScriptedTurn(
                tool_calls=[("draft_email", draft_args), ("draft_email", {**draft_args, "related_card_id": "card_x"})]
            ),
            ScriptedTurn(text="I've drafted the inquiry; it's on the board. Shall I send it?"),
        ],
    )

    await chat(ctx, event, "ask The Foundry if they're free on Dec 5")

    [draft] = fake.drafts
    assert isinstance(draft, EmailDraftIn) and draft.kind == "venue_inquiry" and draft.to == ["events@foundry.example"]
    assert draft.related_card_id == venue.id and fake.sent == []
    reply = (await rows(ctx, event.id))[1]
    results = tool_results(reply)
    assert [r["is_error"] for r in results] == [False, True]
    assert "Nothing is sent until the planner confirms" in results[0]["content"]
    summaries = [e.data["summary"] for e in published(ctx, event.id, ev.CHAT_TOOL) if e.data["status"] == "started"]
    assert summaries == ["Draft email to events@foundry.example"] * 2
    async with ctx.db() as session:
        [email] = list((await session.execute(select(EmailMessage))).scalars())

    llm.queue_run(
        "orchestrator", [ScriptedTurn(tool_calls=[("send_email", {"email_id": email.id})]), ScriptedTurn(text="Sent.")]
    )
    await chat(ctx, event, "yes, send it")

    assert fake.sent == [email.id]
    block = text_of(llm.calls_for("orchestrator")[1].messages[-1])
    assert f'- {email.id} | venue_inquiry | to events@foundry.example | "Availability for 5 December" | draft' in block
    reply = (await rows(ctx, event.id))[3]
    assert '"status": "sent"' in tool_results(reply)[0]["content"]
    finished = [e.data for e in published(ctx, event.id, ev.CHAT_TOOL) if e.data["name"] == "send_email"]
    assert [f["summary"] for f in finished] == ["Send email “Availability for 5 December”"] * 2

    # Someone else's email id is rejected before the entrypoint is reached.
    llm.queue_run(
        "orchestrator", [ScriptedTurn(tool_calls=[("send_email", {"email_id": "eml_other"})]), ScriptedTurn(text="?")]
    )
    await chat(ctx, event, "send eml_other")
    assert fake.sent == [email.id]
    assert tool_results((await rows(ctx, event.id))[5])[0]["is_error"] is True


async def test_luma_draft_publish_and_invite(ctx, event, llm, monkeypatch):
    fake = patch_outreach(monkeypatch)
    fake.date_missing = True
    venue = await board.create_card(ctx, event_id=event.id, kind="venue", title="The Foundry")
    llm.queue_run(
        "orchestrator",
        [
            ScriptedTurn(tool_calls=[("draft_luma_event", {"venue_card_id": venue.id})]),
            ScriptedTurn(text="Need a date."),
        ],
    )

    await chat(ctx, event, "set up the luma page")

    [result] = tool_results((await rows(ctx, event.id))[1])
    assert result["is_error"] is True and result["content"] == "Set the event date first."
    assert fake.luma_drafts == []

    fake.date_missing = False
    llm.queue_run(
        "orchestrator",
        [
            ScriptedTurn(
                tool_calls=[
                    (
                        "draft_luma_event",
                        {"venue_card_id": venue.id, "overrides": {"name": "Winter Offsite", "visibility": "public"}},
                    ),
                    ("invite_luma_guests", {"emails": ["a@example.com"]}),
                ]
            ),
            ScriptedTurn(text="Drafted. Want me to publish it?"),
        ],
    )
    await chat(ctx, event, "the date is Dec 5; set up the luma page and invite a@example.com")

    [draft] = fake.luma_drafts
    assert draft.name == "Winter Offsite" and draft.visibility == "public" and draft.venue_card_id == venue.id
    assert draft.start_at.isoformat() == "2026-12-05T18:00:00+00:00", "pre-filled values survive the overrides"
    results = tool_results((await rows(ctx, event.id))[3])
    assert [r["is_error"] for r in results] == [False, True] and "not published yet" in results[1]["content"]
    assert fake.published == 0 and fake.invited == []

    llm.queue_run(
        "orchestrator",
        [
            ScriptedTurn(tool_calls=[("publish_luma_event", {})]),
            ScriptedTurn(tool_calls=[("invite_luma_guests", {"emails": ["a@example.com", "b@example.com"]})]),
            ScriptedTurn(text="Published and invited two guests."),
        ],
    )
    await chat(ctx, event, "yes publish it and invite a@example.com and b@example.com")

    assert fake.published == 1 and fake.invited == [["a@example.com", "b@example.com"]]
    results = tool_results((await rows(ctx, event.id))[5])
    assert (
        '"url": "https://lu.ma/test-event"' in results[0]["content"] and '"guests_invited": 2' in results[1]["content"]
    )
    block = text_of(llm.calls_for("orchestrator")[2].messages[-1])
    assert 'Luma event page: draft | "Winter Offsite"' in block

    llm.queue_run("orchestrator", [ScriptedTurn(text="ok")])
    await chat(ctx, event, "thanks")
    block = text_of(llm.calls_for("orchestrator")[3].messages[-1])
    assert 'Luma event page: published | "Winter Offsite" | https://lu.ma/test-event | 2 guests invited' in block
