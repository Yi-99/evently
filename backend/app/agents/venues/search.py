"""Venue search coordinator: scout → one card and one research agent per venue → ranking and summary."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from app.agents.venues import prompts
from app.agents.venues.cards import CardWriter, place_summary
from app.agents.venues.models import SearchSummary
from app.agents.venues.research import make_researcher
from app.agents.venues.scout import VenueSearchError, make_scout
from app.llm.base import LLMError
from app.models import AgentRun, Card, Event
from app.providers.base import GeoPoint, PlaceCandidate
from app.realtime import events as ev
from app.schemas.common import EventOut, dump
from app.schemas.venue import VenueCriteria, VenueScore
from app.services import board

if TYPE_CHECKING:
    from app.agents.runtime import RunContext
    from app.core.context import AppContext

log = logging.getLogger(__name__)

SUMMARY_CARD_TITLE = "Venue shortlist"


async def _save_requirements(ctx: AppContext, event_id: str, criteria: VenueCriteria) -> None:
    async with ctx.db() as session:
        event = await session.get(Event, event_id)
        if event is None:
            raise LookupError(f"event {event_id} not found")
        # Merge: keep keys other writers own (e.g. the web app's `intake` / `plan`).
        event.requirements = {**(event.requirements or {}), **criteria.model_dump(mode="json")}
        await session.commit()
    await ctx.bus.publish(ev.event_topic(event_id), ev.EVENT_UPDATED, {"event": dump(EventOut, event)})


async def _run_scout(run: RunContext, criteria: VenueCriteria) -> tuple[list[PlaceCandidate], GeoPoint | None]:
    scout = await run.spawn_child(
        kind="venue_scout",
        title=f"Scout: venues near {criteria.location}",
        fn=make_scout(criteria),
        input={"criteria": criteria.model_dump(mode="json")},
    )
    done = await run.ctx.runtime.wait(scout.id)
    if done.status != "succeeded":
        error = (done.error or "the scout was cancelled").removeprefix(f"{VenueSearchError.__name__}: ")
        raise VenueSearchError(error)
    candidates = [PlaceCandidate.model_validate(c) for c in done.output.get("candidates", [])]
    centre = GeoPoint.model_validate(done.output["centre"]) if done.output.get("centre") else None
    return candidates, centre


async def _start_research(
    run: RunContext, candidate: PlaceCandidate, criteria: VenueCriteria, centre: GeoPoint | None
) -> tuple[Card, AgentRun]:
    """Create the venue card first so the board shows it the instant its agent exists."""
    card = await board.create_card(
        run.ctx,
        event_id=run.event_id,
        kind="venue",
        title=candidate.name,
        status="running",
        data={
            "place": place_summary(candidate).model_dump(mode="json"),
            "criteria": criteria.model_dump(mode="json"),
        },
    )
    child = await run.spawn_child(
        kind="venue_research",
        title=f"Research: {candidate.name}",
        fn=make_researcher(card.id, candidate, criteria, centre),
        input={"place_id": candidate.place_id, "name": candidate.name},
        card_id=card.id,
    )
    await CardWriter(run.ctx, card.id).patch(agent_run_id=child.id)
    return card, child


async def _wait_for_children(run: RunContext, children: list[AgentRun]) -> None:
    total = len(children)
    try:
        for done, finished in enumerate(asyncio.as_completed([run.ctx.runtime.wait(c.id) for c in children]), 1):
            await finished
            await run.step(f"{done} of {total} venues researched", progress=0.3 + 0.55 * done / total)
    except asyncio.CancelledError:
        for child in children:
            await run.ctx.runtime.cancel(child.id)
        raise


def _score_of(card: Card) -> VenueScore | None:
    try:
        return VenueScore.model_validate((card.data or {}).get("score"))
    except ValueError:
        return None


def _overall(card: Card) -> int | None:
    score = _score_of(card)
    return score.overall if score else None


async def _rank(ctx: AppContext, card_ids: list[str]) -> tuple[list[Card], list[Card]]:
    """Order researched cards by overall score, write `data.rank`, and return (ranked, unranked)."""
    cards = [c for cid in card_ids if (c := await board.get_card(ctx, cid)) is not None]
    scored = [(c, s) for c in cards if c.status == "ready" and (s := _score_of(c)) is not None]
    scored.sort(key=lambda pair: pair[1].overall, reverse=True)  # stable: ties keep scout order
    ranked: list[Card] = []
    for position, (card, _) in enumerate(scored, 1):
        ranked.append(await CardWriter(ctx, card.id).patch(data_patch={"rank": position}))
    ranked_ids = {c.id for c in ranked}
    return ranked, [c for c in cards if c.id not in ranked_ids]


def _ranking_input(card: Card) -> dict[str, Any]:
    data = card.data or {}
    return {
        "rank": data.get("rank"),
        "name": card.title,
        "score": data.get("score"),
        "summary": data.get("summary"),
        "capacity": data.get("capacity"),
        "parking": (data.get("parking") or {}).get("summary"),
        "weather": {k: (data.get("weather") or {}).get(k) for k in ("summary", "risk", "notes")},
        "traffic": (data.get("traffic") or {}).get("summary"),
        "pricing": data.get("pricing"),
        "pros": data.get("pros"),
        "cons": data.get("cons"),
    }


def _fallback_summary(criteria: VenueCriteria, ranked: list[Card], failed: list[Card]) -> SearchSummary:
    parts = [f"Researched {len(ranked)} venue(s) near {criteria.location}."]
    if ranked:
        top = ranked[0]
        parts.append(f"Top pick: {top.title} (score {_overall(top)}).")
    if failed:
        parts.append(f"Could not research: {', '.join(c.title for c in failed)}.")
    one_liners = []
    for card in ranked:
        text = ((card.data or {}).get("summary") or "").split(". ")[0][:120] or "See card"
        one_liners.append({"rank": (card.data or {}).get("rank"), "one_liner": text})
    return SearchSummary(summary=" ".join(parts), one_liners=one_liners)


async def _summarise(ctx: AppContext, criteria: VenueCriteria, ranked: list[Card], failed: list[Card]) -> SearchSummary:
    if not ranked:
        return _fallback_summary(criteria, ranked, failed)
    try:
        return await ctx.llm.parse(
            purpose="venue_summary",
            system=prompts.SUMMARY_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": prompts.summary_prompt(
                        criteria.model_dump(mode="json"),
                        [_ranking_input(c) for c in ranked],
                        [c.title for c in failed],
                    ),
                }
            ],
            output_model=SearchSummary,
        )
    except LLMError:
        # The research itself is done and valuable; a failed closing note must not discard it.
        log.exception("venue summary failed; using a plain fallback")
        return _fallback_summary(criteria, ranked, failed)


def make_coordinator(criteria: VenueCriteria):
    """Agent function for the `venue_search` run (see `start_venue_search` for the contract)."""

    async def coordinate(run: RunContext) -> dict[str, Any]:
        ctx = run.ctx
        await run.step("Saving requirements", progress=0.02)
        await _save_requirements(ctx, run.event_id, criteria)

        await run.step(f"Finding candidate venues near {criteria.location}", progress=0.05)
        candidates, centre = await _run_scout(run, criteria)

        await run.step(f"Researching {len(candidates)} venues in parallel", progress=0.3)
        started = [await _start_research(run, c, criteria, centre) for c in candidates]
        card_ids = [card.id for card, _ in started]
        await _wait_for_children(run, [child for _, child in started])

        await run.step("Ranking venues", progress=0.9)
        ranked, unranked = await _rank(ctx, card_ids)
        failed = [c for c in unranked if c.status != "ready"]
        if not ranked:
            first_error = next(((c.data or {}).get("error", {}).get("message") for c in failed), None)
            raise VenueSearchError(
                f"None of the {len(card_ids)} venues could be researched"
                + (f" (first error: {first_error})." if first_error else ".")
            )

        note = await _summarise(ctx, criteria, ranked, failed)
        one_liners = {o.rank: o.one_liner for o in note.one_liners}
        ranking = [
            {
                "card_id": c.id,
                "name": c.title,
                "score": _overall(c),
                "one_liner": one_liners.get((c.data or {}).get("rank"), ""),
            }
            for c in ranked
        ]
        summary_card = await board.create_card(
            ctx,
            event_id=run.event_id,
            kind="summary",
            title=SUMMARY_CARD_TITLE,
            data={"summary": note.summary, "ranking": ranking, "criteria": criteria.model_dump(mode="json")},
            agent_run_id=run.run_id,
        )
        await run.step("Done", progress=1.0)
        return {
            "card_ids": [c.id for c in ranked] + [c.id for c in unranked],
            "summary_card_id": summary_card.id,
            "summary": note.summary,
        }

    return coordinate
