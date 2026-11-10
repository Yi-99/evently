"""Venue research agents — public entrypoints.

Other packages (chat orchestrator, API) call only these two functions.
Implementation lives in sibling modules of this package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.models import AgentRun
from app.schemas.venue import VenueCriteria

if TYPE_CHECKING:
    from app.core.context import AppContext


async def start_venue_search(
    ctx: AppContext,
    *,
    event_id: str,
    criteria: VenueCriteria,
    parent_run_id: str | None = None,
) -> AgentRun:
    """Start a venue search in the background and return its coordinator run immediately.

    The coordinator (`kind="venue_search"`, spawned with `limited=False`):
      1. saves `criteria` to `Event.requirements`;
      2. runs a scout (`venue_scout`) that finds up to `criteria.max_candidates` venues;
      3. creates one `venue` card per candidate (status "running") and spawns one
         `venue_research` child run per card, in parallel;
      4. when all children finish, ranks the venues (sets `data.rank`) and creates a
         `summary` card.

    Its `AgentRun.output` is `{"card_ids": [...], "summary_card_id": str | None, "summary": str}`.
    `card_ids` lists ranked cards first (best first), then cards whose research failed.
    """
    from app.agents.venues.search import make_coordinator

    return await ctx.runtime.spawn(
        event_id=event_id,
        kind="venue_search",
        title=f"Venue search: {criteria.location}",
        fn=make_coordinator(criteria),
        input={"criteria": criteria.model_dump(mode="json")},
        parent_run_id=parent_run_id,
        limited=False,
    )


async def start_card_refine(
    ctx: AppContext,
    *,
    card_id: str,
    instruction: str,
    parent_run_id: str | None = None,
) -> AgentRun:
    """Re-run research on one existing venue card, steered by the user's `instruction`.

    Spawns a `card_refine` run attached to the card (card status "running" → "ready").
    Fields listed in `card.locked_fields` are preserved. Raises `LookupError` if the
    card does not exist and `ValueError` if it is not a venue card.

    The run's output is `{"card_id": str, "note": str, "changed": [data keys]}`; `note` is
    the agent's one- or two-sentence account of what it found, suitable for posting in chat.
    """
    from app.agents.venues.cards import CardWriter
    from app.agents.venues.research import card_criteria, make_refiner
    from app.models import Event
    from app.services import board

    card = await board.get_card(ctx, card_id)
    if card is None:
        raise LookupError(f"card {card_id} not found")
    if card.kind != "venue":
        raise ValueError(f"card {card_id} is a {card.kind} card, not a venue card")
    async with ctx.db() as session:
        event = await session.get(Event, card.event_id)
    criteria = card_criteria(card, event.requirements if event else None)

    writer = CardWriter(ctx, card.id)
    # Mark the card busy before the agent exists, so a run that fails instantly can never
    # leave it stuck on "running" (the runtime only flags cards that are still running).
    await writer.patch(status="running")
    run = await ctx.runtime.spawn(
        event_id=card.event_id,
        kind="card_refine",
        title=f"Refine: {card.title}",
        fn=make_refiner(card, instruction, criteria, previous_status=card.status),
        input={"instruction": instruction, "previous_status": card.status},
        parent_run_id=parent_run_id,
        card_id=card.id,
    )
    await writer.patch(agent_run_id=run.id)
    return run
