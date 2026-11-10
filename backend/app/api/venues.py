"""REST routes for venues (mounted under /api/v1).

Both routes start a background agent run and answer 202 with the run; progress
arrives on the event channel (`agent.run.*`, `board.card.*`).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, StringConstraints

from app.agents.venues import start_card_refine, start_venue_search
from app.core.deps import Ctx, CurrentUser, OwnedEvent, load_owned_event
from app.llm.base import LLMUnavailableError
from app.schemas.common import AgentRunOut
from app.schemas.venue import VenueCriteria
from app.services import board

router = APIRouter(tags=["venues"])


class RunAccepted(BaseModel):
    run: AgentRunOut


class CardPrompt(BaseModel):
    instruction: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


def _require_llm(ctx: Ctx) -> None:
    if not ctx.llm.available:
        raise LLMUnavailableError("Claude is not configured. Set ANTHROPIC_API_KEY in .env.")  # → 503


@router.post("/events/{event_id}/venue-searches", response_model=RunAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_venue_search(event: OwnedEvent, criteria: VenueCriteria, ctx: Ctx):
    """Start a venue search: one scout, then one research agent (and card) per candidate venue."""
    _require_llm(ctx)
    run = await start_venue_search(ctx, event_id=event.id, criteria=criteria)
    return {"run": run}


@router.post("/cards/{card_id}/prompt", response_model=RunAccepted, status_code=status.HTTP_202_ACCEPTED)
async def prompt_card(card_id: str, body: CardPrompt, ctx: Ctx, user: CurrentUser):
    """Re-run research on one venue card with an instruction ("check if they allow outside catering")."""
    card = await board.get_card(ctx, card_id)
    if card is None or await load_owned_event(ctx, card.event_id, user) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Card not found")
    if card.kind != "venue":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Only venue cards can be re-prompted")
    if card.status == "running" and card.agent_run_id and ctx.runtime.is_active(card.agent_run_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "An agent is already working on this card")
    _require_llm(ctx)
    run = await start_card_refine(ctx, card_id=card.id, instruction=body.instruction)
    return {"run": run}
