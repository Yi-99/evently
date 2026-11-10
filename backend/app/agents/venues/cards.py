"""Writing venue cards safely while several agents (and their concurrent tool calls) are at work.

`board.patch_card` is a read-modify-write without a lock, so two writers that
interleave lose one update. Everything in this package that touches a card goes
through `CardWriter`, which serialises writes per card across the whole app.
"""

from __future__ import annotations

import asyncio
import weakref
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from app.models import Card
from app.providers.base import GeoPoint, PlaceCandidate
from app.schemas.venue import (
    CapacityAssessment,
    ParkingAssessment,
    PlaceSummary,
    PricingAssessment,
    Source,
    TrafficAssessment,
    VenueCriteria,
    WeatherAssessment,
)
from app.services import board

if TYPE_CHECKING:
    from app.core.context import AppContext

# Top-level `data` keys that hold a structured section, with the model that validates it.
SECTION_MODELS: dict[str, type[BaseModel]] = {
    "place": PlaceSummary,
    "criteria": VenueCriteria,
    "capacity": CapacityAssessment,
    "parking": ParkingAssessment,
    "weather": WeatherAssessment,
    "traffic": TrafficAssessment,
    "pricing": PricingAssessment,
}


def card_lock(ctx: AppContext, card_id: str) -> asyncio.Lock:
    """The app-wide lock for one card; dropped automatically once nobody holds it."""
    locks: weakref.WeakValueDictionary[str, asyncio.Lock] = ctx.service("venue_card_locks", weakref.WeakValueDictionary)
    lock = locks.get(card_id)
    if lock is None:
        lock = locks[card_id] = asyncio.Lock()
    return lock


def place_summary(candidate: PlaceCandidate, base: PlaceSummary | None = None) -> PlaceSummary:
    """Card `place` section from provider data; `base` values survive where the provider has none."""
    fields: dict[str, Any] = {
        "name": candidate.name,
        "address": candidate.address,
        "lat": candidate.location.lat if candidate.location else None,
        "lng": candidate.location.lng if candidate.location else None,
        "place_id": candidate.place_id,
        "website": candidate.website,
        "phone": getattr(candidate, "phone", None),
        "rating": candidate.rating,
        "review_count": candidate.review_count,
        "price_level": candidate.price_level,
        "types": candidate.types or None,
        "photo_urls": candidate.photo_urls or None,
        "maps_url": candidate.maps_url,
    }
    merged = {**(base.model_dump() if base else {}), **{k: v for k, v in fields.items() if v is not None}}
    return PlaceSummary.model_validate(merged)


def place_location(place: PlaceSummary) -> GeoPoint | None:
    if place.lat is None or place.lng is None:
        return None
    return GeoPoint(lat=place.lat, lng=place.lng)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def merge_sources(*groups: list[Any] | None) -> list[dict[str, Any]]:
    """Union of source lists, deduplicated by url (or title when there is no url), order preserved."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for group in groups:
        for raw in group or []:
            try:
                source = raw if isinstance(raw, Source) else Source.model_validate(raw)
            except ValueError:
                continue
            key = source.url or source.title
            if key not in seen:
                seen.add(key)
                out.append(source.model_dump(mode="json"))
    return out


class CardWriter:
    """All writes of one venue card by the agents, serialised through `card_lock`."""

    def __init__(self, ctx: AppContext, card_id: str) -> None:
        self.ctx = ctx
        self.card_id = card_id
        self._lock = card_lock(ctx, card_id)

    async def read(self) -> Card:
        card = await board.get_card(self.ctx, self.card_id)
        if card is None:
            raise board.CardNotFound(self.card_id)
        return card

    async def patch(self, **changes: Any) -> Card:
        """Plain `board.patch_card(by="agent", ...)` under the card lock."""
        async with self._lock:
            return await board.patch_card(self.ctx, self.card_id, by="agent", **changes)

    async def write(
        self,
        *,
        sections: dict[str, dict[str, Any]] | None = None,
        data: dict[str, Any] | None = None,
        add_sources: list[Any] | None = None,
        status: str | None = None,
    ) -> list[str]:
        """Update a card atomically and return the data keys skipped because the user locked them.

        sections: per-section field patches (`{"parking": {"score": 70}}`); each is merged
            field by field into the card's current section and validated with its model.
        data: top-level keys set whole (pros, cons, summary, score, rank...).
        add_sources: appended to `data.sources` without duplicates.
        """
        async with self._lock:
            card = await self.read()
            current = _as_dict(card.data)
            locked = set(card.locked_fields or [])
            patch: dict[str, Any] = {}
            for key, fields in (sections or {}).items():
                if key in locked:
                    continue
                merged = {**_as_dict(current.get(key)), **fields}
                patch[key] = SECTION_MODELS[key].model_validate(merged).model_dump(mode="json")
            patch.update({k: v for k, v in (data or {}).items() if k not in locked})
            if add_sources and "sources" not in locked:
                patch["sources"] = merge_sources(current.get("sources"), add_sources)
            if patch or status:
                await board.patch_card(self.ctx, self.card_id, by="agent", status=status, data_patch=patch or None)
        touched = set(sections or {}) | set(data or {}) | ({"sources"} if add_sources else set())
        return sorted(touched & locked)
