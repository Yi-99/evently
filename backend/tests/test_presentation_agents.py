"""Presentation agents: deck build pipeline, slide/deck edits, keyword and theme hygiene."""

from __future__ import annotations

import json

import pytest

from app.agents.presentations import start_presentation_build, start_presentation_edit, store
from app.agents.presentations.outline import clean_theme, distinct_keywords
from app.llm.base import LLMError
from app.llm.scripted import ScriptedTurn
from app.realtime import events as ev
from app.schemas.presentation import DeckOutline, DeckTheme, PresentationBrief, SlideOutline
from app.services import board, html_sanitize
from tests.helpers import create_event, published

MARK = "<!--sanitized-->"


@pytest.fixture
def sanitizer(monkeypatch):
    """Stand-in for the (not yet landed) sanitiser; its marker proves it ran on every saved slide."""
    monkeypatch.setattr(html_sanitize, "sanitize_slide_html", lambda html: MARK + html)


def outline() -> DeckOutline:
    return DeckOutline(
        title="The Foundry: our venue pick",
        # The accent could escape a CSS declaration; `clean_theme` must replace it.
        theme=DeckTheme(name="Night", accent="#f00; } body { background: red", heading_font="Georgia, serif"),
        slides=[
            SlideOutline(
                title="Welcome to the Foundry pitch",
                purpose="Set the scene",
                notes="Welcome everyone.",
                keywords=["The Foundry", "welcome", "Overview"],
            ),
            SlideOutline(
                title="Parking is solved",
                purpose="Remove the parking worry",
                key_points=["420 spaces", "2 min walk"],
                visual="big number",
                notes="Parking is solved: 420 spaces.",
                keywords=["parking", "the foundry", "Score", "capacity", "fifth"],
            ),
            SlideOutline(
                title="Our recommendation",
                purpose="Close",
                notes="Our recommendation is clear.",
                keywords=["Overview", "recommendation", "Next steps"],
            ),
            SlideOutline(title="Appendix", purpose="Extra", keywords=["extra"]),
        ],
    )


def render_script(call):
    """Scripted slide designer: echoes the slide number and includes a tag the sanitiser would strip."""
    number = call.last_user_text.split('<slide number="')[1].split('"')[0]
    return {"html": f"<section>Slide {number}</section><script>alert(1)</script>"}


async def venue_card(ctx, event):
    return await board.create_card(
        ctx,
        event_id=event.id,
        kind="venue",
        title="The Foundry",
        data={
            "place": {
                "name": "The Foundry",
                "place_id": "secret-id",
                "photo_urls": [f"https://p/{i}.jpg" for i in range(5)],
            },
            "score": {"overall": 88},
            "criteria": {"location": "Austin"},
            "capacity": None,
        },
    )


async def build_deck(ctx, event, llm, **brief):
    """Build a three-slide deck with scripted answers and wait for it."""
    card = await venue_card(ctx, event)
    llm.on_parse("deck_outline", outline())
    llm.on_parse("slide_render", render_script)
    presentation, run = await start_presentation_build(
        ctx,
        event_id=event.id,
        brief=PresentationBrief(
            prompt="Pitch the venue", audience="leadership", slide_count=3, card_ids=[card.id], **brief
        ),
    )
    done = await ctx.runtime.wait(run.id, timeout=10)
    return presentation, done


# -- build ----------------------------------------------------------------------


async def test_build_pipeline_end_to_end(ctx, event, llm, sanitizer):
    other = await create_event(ctx, name="Other")
    foreign = await board.create_card(ctx, event_id=other.id, kind="note", title="Leak", data={"secret": "nope"})
    card = await venue_card(ctx, event)
    llm.on_parse("deck_outline", outline())
    llm.on_parse("slide_render", render_script)

    brief = PresentationBrief(prompt="Pitch the venue", slide_count=3, card_ids=[card.id, foreign.id, "card_missing"])
    presentation, run = await start_presentation_build(ctx, event_id=event.id, brief=brief)
    assert presentation.status == "generating" and presentation.agent_run_id == run.id
    assert run.kind == "presentation_build" and run.card_id

    done = await ctx.runtime.wait(run.id, timeout=10)
    assert done.status == "succeeded", done.error
    assert done.output["presentation_id"] == presentation.id
    assert done.output["slide_count"] == 3 and done.output["failed_slides"] == 0
    assert done.output["summary"] == 'Your deck "The Foundry: our venue pick" is ready with 3 slides.'

    # Rows: title/theme from the outline, the unsafe accent replaced, slide_count respected.
    fresh = await store.get_presentation(ctx, presentation.id)
    assert fresh.status == "ready" and fresh.title == "The Foundry: our venue pick" and fresh.version > 1
    assert fresh.theme["name"] == "Night" and fresh.theme["heading_font"] == "Georgia, serif"
    assert fresh.theme["accent"] == DeckTheme().accent
    slides = await store.list_slides(ctx, presentation.id)
    assert [s.position for s in slides] == [0, 1, 2]
    assert [s.title for s in slides] == ["Welcome to the Foundry pitch", "Parking is solved", "Our recommendation"]
    assert all(s.status == "ready" and s.version == 3 for s in slides)
    assert [s.html for s in slides] == [
        f"{MARK}<section>Slide {n}</section><script>alert(1)</script>" for n in (1, 2, 3)
    ]
    assert slides[1].notes == "Parking is solved: 420 spaces." and slides[1].outline["key_points"] == [
        "420 spaces",
        "2 min walk",
    ]
    # Keywords: normalised, shared ones dropped from every slide, capped at four after that.
    assert [s.keywords for s in slides] == [
        ["welcome"],
        ["parking", "score", "capacity", "fifth"],
        ["recommendation", "next steps"],
    ]

    # Envelopes: skeleton first, then renders, then the presentation settles.
    types = [e.type for e in published(ctx, event.id)]
    assert types.count(ev.PRESENTATION_CREATED) == 1 and types.count(ev.SLIDE_CREATED) == 3
    assert types.count(ev.SLIDE_UPDATED) == 6  # rendering + ready per slide
    assert max(i for i, t in enumerate(types) if t == ev.SLIDE_CREATED) < types.index(ev.SLIDE_UPDATED)
    updates = [e.data["presentation"] for e in published(ctx, event.id, ev.PRESENTATION_UPDATED)]
    assert updates[-1]["status"] == "ready" and updates[-1]["id"] == presentation.id
    slide_envelope = published(ctx, event.id, ev.SLIDE_CREATED)[0].data["slide"]
    assert slide_envelope["status"] == "pending" and slide_envelope["presentation_id"] == presentation.id

    # Run tree: one slide_render child per slide under the coordinator.
    children = [
        e.data["run"] for e in published(ctx, event.id, ev.AGENT_RUN_CREATED) if e.data["run"]["kind"] == "slide_render"
    ]
    assert len(children) == 3 and all(c["parent_run_id"] == run.id for c in children)
    child_rows = await ctx.runtime.wait_all([c["id"] for c in children])
    assert [c.status for c in child_rows] == ["succeeded"] * 3

    # Board card mirrors the deck.
    (deck_card,) = [c for c in await board.list_cards(ctx, event.id) if c.kind == "presentation"]
    assert deck_card.id == run.card_id and deck_card.status == "ready" and deck_card.agent_run_id == run.id
    assert deck_card.data == {
        "presentation_id": presentation.id,
        "title": fresh.title,
        "slide_count": 3,
        "status": "ready",
    }

    # Prompts: the outline saw the brief and the selected card (without noise), never another event's card.
    (outline_call,) = llm.calls_for("deck_outline")
    assert outline_call.tier == "orchestrator" and outline_call.output_model is DeckOutline
    text = outline_call.last_user_text
    assert "exactly 3" in text and "The Foundry" in text and '"overall": 88' in text
    assert "https://p/2.jpg" in text and "https://p/3.jpg" not in text  # photos capped
    assert "secret-id" not in text and "Austin" not in text and "Leak" not in text and "nope" not in text
    renders = llm.calls_for("slide_render")
    assert len(renders) == 3 and all(c.tier == "worker" for c in renders)
    assert "--accent: #38bdf8" in renders[0].last_user_text and "The Foundry" in renders[0].last_user_text
    assert "Audience: leadership" not in renders[0].last_user_text  # brief had none
    assert "1280 x 720" in renders[0].system and "var(--accent)" in renders[0].system


async def test_one_failed_slide_does_not_fail_the_deck(ctx, event, llm, sanitizer):
    def flaky(call):
        if '<slide number="2"' in call.last_user_text:
            raise LLMError("Claude declined the request (slide_render).")
        return render_script(call)

    card = await venue_card(ctx, event)
    llm.on_parse("deck_outline", outline())
    llm.on_parse("slide_render", flaky)
    presentation, run = await start_presentation_build(
        ctx, event_id=event.id, brief=PresentationBrief(prompt="Pitch", slide_count=3, card_ids=[card.id])
    )
    done = await ctx.runtime.wait(run.id, timeout=10)

    assert done.status == "succeeded" and done.output["failed_slides"] == 1
    assert done.output["summary"].endswith("ready with 2 of 3 slides; 1 could not be designed and can be regenerated.")
    slides = await store.list_slides(ctx, presentation.id)
    assert [s.status for s in slides] == ["ready", "error", "ready"] and slides[1].html == ""
    assert (await store.get_presentation(ctx, presentation.id)).status == "ready"
    children = await ctx.runtime.wait_all([r.id for r in await _children(ctx, run.id)])
    assert sorted(c.status for c in children) == ["failed", "succeeded", "succeeded"]
    assert any("declined" in (c.error or "") for c in children)


async def test_deck_fails_when_no_slide_renders(ctx, event, llm, sanitizer):
    def refuse(call):
        raise LLMError("Claude declined the request (slide_render).")

    llm.on_parse("deck_outline", outline())
    llm.on_parse("slide_render", refuse)
    presentation, run = await start_presentation_build(
        ctx, event_id=event.id, brief=PresentationBrief(prompt="Pitch", slide_count=2)
    )
    done = await ctx.runtime.wait(run.id, timeout=10)

    assert done.status == "failed" and "none of its 2 slides could be designed" in done.error
    assert (await store.get_presentation(ctx, presentation.id)).status == "error"
    assert all(s.status == "error" for s in await store.list_slides(ctx, presentation.id))
    (card,) = [c for c in await board.list_cards(ctx, event.id) if c.kind == "presentation"]
    assert card.status == "error" and card.data["status"] == "error" and card.data["slide_count"] == 2


async def test_build_rejects_unknown_event(ctx):
    with pytest.raises(LookupError):
        await start_presentation_build(ctx, event_id="evt_missing", brief=PresentationBrief(prompt="x"))


# -- edit -----------------------------------------------------------------------


async def test_edit_rejects_unknown_ids(ctx, event, llm, sanitizer):
    presentation, _ = await build_deck(ctx, event, llm)
    other, _ = await build_deck(ctx, event, llm)
    foreign_slide = (await store.list_slides(ctx, other.id))[0]
    with pytest.raises(LookupError):
        await start_presentation_edit(ctx, presentation_id="prs_missing", instruction="x")
    with pytest.raises(LookupError):
        await start_presentation_edit(ctx, presentation_id=presentation.id, instruction="x", slide_id="sld_missing")
    with pytest.raises(LookupError):
        await start_presentation_edit(ctx, presentation_id=presentation.id, instruction="x", slide_id=foreign_slide.id)


async def test_single_slide_edit(ctx, event, llm, sanitizer):
    presentation, _ = await build_deck(ctx, event, llm)
    slides = await store.list_slides(ctx, presentation.id)
    target = slides[2]
    before = len(published(ctx, event.id))
    llm.on_parse(
        "slide_edit",
        {
            "html": "<section>Closing, bolder</section>",
            "title": "Book the Foundry",
            "notes": "Let's book the Foundry this week.",
            "keywords": ["welcome", "book the foundry", "this week"],  # "welcome" belongs to slide 1
        },
    )

    run = await start_presentation_edit(
        ctx, presentation_id=presentation.id, instruction="Make the closing slide bolder", slide_id=target.id
    )
    assert run.kind == "presentation_edit" and run.input["slide_id"] == target.id
    done = await ctx.runtime.wait(run.id, timeout=10)
    assert done.status == "succeeded"
    assert done.output == {
        "presentation_id": presentation.id,
        "slide_id": target.id,
        "summary": 'Updated slide 3, "Book the Foundry".',
    }

    after = await store.get_slide(ctx, target.id)
    assert after.html == f"{MARK}<section>Closing, bolder</section>" and after.status == "ready"
    assert after.title == "Book the Foundry" and after.notes.startswith("Let's book")
    assert after.keywords == ["book the foundry", "this week"] and after.version == target.version + 2
    assert [s.html for s in await store.list_slides(ctx, presentation.id)][:2] == [slides[0].html, slides[1].html]

    statuses = [e.data["slide"]["status"] for e in published(ctx, event.id)[before:] if e.type == ev.SLIDE_UPDATED]
    assert statuses == ["rendering", "ready"]
    (call,) = llm.calls_for("slide_edit")
    assert "Make the closing slide bolder" in call.last_user_text and target.html in call.last_user_text
    assert '<slide number="3" of="3">' in call.last_user_text


async def test_single_slide_edit_failure_keeps_the_old_slide(ctx, event, llm, sanitizer):
    presentation, _ = await build_deck(ctx, event, llm)
    target = (await store.list_slides(ctx, presentation.id))[0]

    def refuse(call):
        raise LLMError("nope")

    llm.on_parse("slide_edit", refuse)
    run = await start_presentation_edit(ctx, presentation_id=presentation.id, instruction="x", slide_id=target.id)
    done = await ctx.runtime.wait(run.id, timeout=10)
    after = await store.get_slide(ctx, target.id)
    assert done.status == "failed" and after.status == "ready" and after.html == target.html


async def test_deck_edit_updates_theme_adds_deletes_and_reorders(ctx, event, llm, sanitizer):
    presentation, _ = await build_deck(ctx, event, llm)
    slides = await store.list_slides(ctx, presentation.id)
    before = len(published(ctx, event.id))
    version = (await store.get_presentation(ctx, presentation.id)).version

    def second_turn(records):
        new_id = json.loads(records[1].result)["slide_id"]
        return ScriptedTurn(
            tool_calls=[
                ("delete_slide", {"slide_id": slides[0].id}),
                ("reorder_slides", {"slide_ids": [slides[2].id, new_id, slides[1].id]}),
            ]
        )

    llm.on_run(
        "presentation_edit",
        [
            ScriptedTurn(
                tool_calls=[
                    ("update_theme", {"name": "Playful", "accent": "#ff6600"}),
                    (
                        "add_slide",
                        {
                            "position": 1,
                            "title": "Getting there",
                            "purpose": "Travel",
                            "notes": "Getting there is easy.",
                            "keywords": ["getting there", "parking", "Getting There"],  # "parking" is slide 2's
                        },
                    ),
                ]
            ),
            second_turn,
            ScriptedTurn(text="I made the deck playful, added a travel slide, dropped the intro and reordered."),
        ],
    )

    run = await start_presentation_edit(ctx, presentation_id=presentation.id, instruction="Make it playful...")
    done = await ctx.runtime.wait(run.id, timeout=10)
    assert done.status == "succeeded", done.error
    assert done.output["summary"].startswith("I made the deck playful")
    assert len(done.output["changes"]) == 4

    fresh = await store.get_presentation(ctx, presentation.id)
    assert fresh.theme["name"] == "Playful" and fresh.theme["accent"] == "#ff6600"
    assert fresh.theme["heading_font"] == "Georgia, serif" and fresh.version > version

    after = await store.list_slides(ctx, presentation.id)
    assert [s.position for s in after] == [0, 1, 2]
    assert [s.title for s in after] == ["Our recommendation", "Getting there", "Parking is solved"]
    new = after[1]
    assert new.status == "ready" and new.html == f"{MARK}<section>Slide 2</section><script>alert(1)</script>"
    assert new.keywords == ["getting there"] and new.outline["purpose"] == "Travel"
    assert "--accent: #ff6600" in llm.calls_for("slide_render")[-1].last_user_text  # rendered with the new theme

    envelopes = published(ctx, event.id)[before:]
    types = [e.type for e in envelopes]
    assert types.count(ev.SLIDE_CREATED) == 1 and types.count(ev.SLIDE_DELETED) == 1
    assert types.count(ev.SLIDES_REORDERED) == 3  # add, delete, reorder
    assert types.count(ev.PRESENTATION_UPDATED) == 4  # theme, add, delete, reorder
    reordered = [e for e in envelopes if e.type == ev.SLIDES_REORDERED][-1]
    assert reordered.data == {"presentation_id": presentation.id, "slide_ids": [s.id for s in after]}
    deleted = [e for e in envelopes if e.type == ev.SLIDE_DELETED][0]
    assert deleted.data == {"slide_id": slides[0].id, "presentation_id": presentation.id}
    (card,) = [c for c in await board.list_cards(ctx, event.id) if c.kind == "presentation"]
    assert card.data["slide_count"] == 3 and card.status == "ready"

    (call,) = llm.calls_for("presentation_edit")
    assert call.tier == "orchestrator" and "Make it playful" in call.last_user_text
    assert sorted(call.tool_names) == sorted(
        ["read_slides", "update_slide", "add_slide", "delete_slide", "reorder_slides", "update_theme"]
    )


async def test_deck_edit_tools_report_bad_input_to_the_model(ctx, event, llm, sanitizer):
    presentation, _ = await build_deck(ctx, event, llm)
    slides = await store.list_slides(ctx, presentation.id)
    llm.on_parse("slide_edit", {"html": "<section>Redone</section>"})
    llm.on_run(
        "presentation_edit",
        [
            ScriptedTurn(
                tool_calls=[
                    ("reorder_slides", {"slide_ids": [slides[0].id]}),
                    ("update_theme", {"accent": "red; } html { display: none"}),
                    ("update_slide", {"slide_id": slides[0].id}),
                    ("update_slide", {"slide_id": "sld_nope", "instruction": "x"}),
                    ("read_slides", {"slide_ids": [slides[1].id], "include_html": True}),
                    (
                        "update_slide",
                        {"slide_id": slides[1].id, "instruction": "Shorter", "keywords": ["welcome", "garage"]},
                    ),
                ]
            ),
            lambda records: seen.extend(records) or ScriptedTurn(text="Some of that was not possible."),
        ],
    )
    seen = []
    run = await start_presentation_edit(ctx, presentation_id=presentation.id, instruction="x")
    done = await ctx.runtime.wait(run.id, timeout=10)
    assert done.status == "succeeded"

    # The model is told what went wrong, and still gets the results of the valid calls.
    assert [r.is_error for r in seen] == [True, True, True, True, False, False]
    assert "exactly once" in seen[0].result and "accent" in seen[1].result
    assert "instruction or at least one" in seen[2].result and "No slide sld_nope" in seen[3].result
    (read,) = json.loads(seen[4].result)
    assert read["slide_id"] == slides[1].id and read["html"] == slides[1].html and read["notes"] == slides[1].notes

    # The deck is untouched by the rejected calls and changed by the valid ones.
    after = await store.list_slides(ctx, presentation.id)
    assert [s.id for s in after] == [s.id for s in slides]
    assert (await store.get_presentation(ctx, presentation.id)).theme["accent"] == DeckTheme().accent
    assert after[1].html == f"{MARK}<section>Redone</section>" and after[1].keywords == ["garage"]
    assert done.output["changes"] == ["updated slide 2 (Parking is solved)"]


# -- helpers --------------------------------------------------------------------


def test_distinct_keywords_drops_shared_and_caps_at_four():
    assert distinct_keywords([["Alpha", " beta "], ["alpha", "Gamma", "d", "e", "f", "g"], ["beta."]]) == [
        [],
        ["gamma", "d", "e", "f"],
        [],
    ]


def test_clean_theme_replaces_unsafe_css_values_only():
    theme = DeckTheme(background="#111", accent="url(http://x)", body_font="Inter, sans-serif", style_notes="a; b")
    cleaned = clean_theme(theme)
    assert cleaned.background == "#111" and cleaned.body_font == "Inter, sans-serif"
    assert cleaned.accent == DeckTheme().accent and cleaned.style_notes == "a; b"


async def _children(ctx, parent_id):
    from sqlalchemy import select

    from app.models import AgentRun

    async with ctx.db() as session:
        return list((await session.execute(select(AgentRun).where(AgentRun.parent_run_id == parent_id))).scalars())
