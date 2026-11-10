"""Per-venue research agent and the card refine agent.

Both are genuine tool-using Claude runs (`purpose="venue_research"` and
`purpose="card_refine"`) built on `ResearchToolkit`; the card they own fills
in live as tool results arrive.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from app.agents.venues import prompts
from app.agents.venues.cards import CardWriter, place_summary
from app.agents.venues.tools import ResearchToolkit, run_agent
from app.models import Card
from app.providers.base import GeoPoint, PlaceCandidate
from app.schemas.venue import PlaceSummary, VenueCriteria

if TYPE_CHECKING:
    from app.agents.runtime import RunContext


def make_researcher(card_id: str, candidate: PlaceCandidate, criteria: VenueCriteria, centre: GeoPoint | None):
    """Agent function for a `venue_research` run. Output: `{card_id, score}`."""

    async def research(run: RunContext) -> dict[str, Any]:
        place = place_summary(candidate)
        kit = ResearchToolkit(run, card_id=card_id, place=place, criteria=criteria, centre=centre)
        await run.step(f"Researching {place.name}", progress=0.05)
        await run_agent(
            kit,
            purpose="venue_research",
            system=prompts.research_system(run.ctx.settings.llm_web_search),
            prompt=prompts.research_prompt(place.model_dump(mode="json"), criteria.model_dump(mode="json")),
            tools=[*kit.lookup_tools(), kit.submit_tool()],
            final_tool="submit_assessment",
        )
        await kit.card.patch(status="ready")
        await run.step(f"Finished researching {place.name}", progress=1.0)
        score = kit.last_update.score.overall if kit.last_update and kit.last_update.score else None
        return {"card_id": card_id, "score": score}

    return research


def card_criteria(card: Card, requirements: dict[str, Any] | None) -> VenueCriteria:
    """The criteria a card was researched against, falling back to the event's saved requirements."""
    data = card.data or {}
    for source in (data.get("criteria"), requirements):
        if isinstance(source, dict):
            try:
                return VenueCriteria.model_validate(source)
            except ValueError:
                continue
    return VenueCriteria(location=(data.get("place") or {}).get("address") or card.title or "unknown")


def card_place(card: Card) -> PlaceSummary:
    try:
        return PlaceSummary.model_validate((card.data or {}).get("place"))
    except ValueError:
        return PlaceSummary(name=card.title or "Venue")


def make_refiner(card: Card, instruction: str, criteria: VenueCriteria, previous_status: str):
    """Agent function for a `card_refine` run. Output: `{card_id, note, changed}`."""

    async def refine(run: RunContext) -> dict[str, Any]:
        writer = CardWriter(run.ctx, card.id)
        kit = ResearchToolkit(run, card_id=card.id, place=card_place(card), criteria=criteria)
        await run.step(f"Working on: {instruction[:120]}", progress=0.05)
        try:
            await run_agent(
                kit,
                purpose="card_refine",
                system=prompts.refine_system(run.ctx.settings.llm_web_search),
                prompt=prompts.refine_prompt(instruction, card.data or {}, list(card.locked_fields or [])),
                tools=[*kit.lookup_tools(), kit.update_tool()],
                final_tool="update_card",
            )
        except BaseException:
            # A failed re-prompt must not turn previously good research into an error card.
            if previous_status == "ready":
                await asyncio.shield(writer.patch(status="ready"))
            raise
        await writer.patch(status="ready")
        await run.step("Card updated", progress=1.0)
        update = kit.last_update
        changed = sorted(update.model_fields_set - {"note"}) if update else []
        note = getattr(update, "note", "") if update else ""
        return {"card_id": card.id, "note": note, "changed": changed}

    return refine
