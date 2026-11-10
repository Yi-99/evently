"""Source material for a deck: the event and the board cards the planner picked.

This text is the only place slide facts may come from (besides the brief), so it is handed
to the outline agent, every slide renderer and the edit agent.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from app.models import Card, Event
from app.services import board

if TYPE_CHECKING:
    from app.core.context import AppContext

# Card data that says nothing a slide could use: search criteria repeat the event's
# requirements, the rest are identifiers and provenance.
_SKIP_KEYS = {"criteria", "sources", "place_id", "lat", "lng", "maps_url", "phone", "error"}
MAX_PHOTOS_PER_CARD = 3  # a slide uses one or two; more only costs tokens


def _prune(value: Any) -> Any:
    """Drop empty values and keys no slide needs, at any depth."""
    if isinstance(value, dict):
        pruned = {k: _prune(v) for k, v in value.items() if k not in _SKIP_KEYS}
        if isinstance(pruned.get("photo_urls"), list):
            pruned["photo_urls"] = pruned["photo_urls"][:MAX_PHOTOS_PER_CARD]
        return {k: v for k, v in pruned.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [item for item in (_prune(v) for v in value) if item not in (None, "", [], {})]
    return value


def _card_entry(card: Card) -> dict[str, Any]:
    return _prune({"kind": card.kind, "title": card.title, "data": card.data or {}})


def _json(value: Any) -> str:
    return json.dumps(value, indent=1, ensure_ascii=False, default=str)


async def load_material(ctx: AppContext, event_id: str, card_ids: list[str]) -> str:
    """Prompt-ready description of the event and the cards in `card_ids`.

    Unknown ids and cards from other events are skipped silently: a card may have been
    deleted since the planner selected it, and another event's data must never leak in.
    """
    async with ctx.db() as session:
        event = await session.get(Event, event_id)
    if event is None:
        raise LookupError(f"event {event_id} not found")

    cards = [await board.get_card(ctx, card_id) for card_id in dict.fromkeys(card_ids)]
    entries = [_card_entry(card) for card in cards if card is not None and card.event_id == event_id]

    about = _prune({"name": event.name, "description": event.description, "requirements": event.requirements or {}})
    source = _json(entries) if entries else "(the planner selected no board cards)"
    return f"<event>\n{_json(about)}\n</event>\n\n<source_material>\n{source}\n</source_material>"
