"""Board geometry: where a new card goes, and how a board is tidied.

Pure functions over card-shaped objects: no I/O and no ORM imports. Both are
deterministic: the same cards always yield the same positions.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any, Protocol

CARD_W, CARD_H, GAP, COLUMNS = 320.0, 420.0, 40.0, 4

VENUE_KIND = "venue"
# Top-to-bottom order of the side column; kinds not listed here go last.
SIDE_KIND_ORDER = ("summary", "comparison", "presentation", "note")

Position = tuple[float, float]  # (x, y) of a card's top-left corner


class Box(Protocol):
    x: float
    y: float
    width: float
    height: float


class LayoutCard(Protocol):
    id: str
    kind: str
    data: dict[str, Any]
    width: float
    height: float
    created_at: datetime


def next_free_position(boxes: Iterable[Box]) -> Position:
    """First cell of a left-to-right, `COLUMNS`-wide grid that a default-sized card fits in without overlap."""
    boxes = list(boxes)

    def taken(x: float, y: float) -> bool:
        return any(x < b.x + b.width and b.x < x + CARD_W and y < b.y + b.height and b.y < y + CARD_H for b in boxes)

    slot = 0
    while True:
        x = (slot % COLUMNS) * (CARD_W + GAP)
        y = (slot // COLUMNS) * (CARD_H + GAP)
        if not taken(x, y):
            return x, y
        slot += 1


def _rank(card: LayoutCard) -> float | None:
    """`data.rank` when it is a real number (1 = best); anything else counts as unranked."""
    rank = (card.data or {}).get("rank")
    if isinstance(rank, bool) or not isinstance(rank, int | float) or rank != rank:  # rank != rank: NaN
        return None
    return float(rank)


def _venue_order(card: LayoutCard) -> tuple[bool, float, datetime, str]:
    rank = _rank(card)
    return (rank is None, rank or 0.0, card.created_at, card.id)


def _side_order(card: LayoutCard) -> tuple[int, datetime, str]:
    kind_order = SIDE_KIND_ORDER.index(card.kind) if card.kind in SIDE_KIND_ORDER else len(SIDE_KIND_ORDER)
    return (kind_order, card.created_at, card.id)


def _offsets(sizes: list[float]) -> list[float]:
    """Start coordinate of each size laid end to end with `GAP` between, plus where the next one would start."""
    offsets = [0.0]
    for size in sizes:
        offsets.append(offsets[-1] + size + GAP)
    return offsets


def auto_layout(cards: Iterable[LayoutCard]) -> dict[str, Position]:
    """Tidy positions for every card, keyed by card id.

    Venue cards fill a `COLUMNS`-wide grid from the origin, row by row: ranked
    venues first (ascending `data.rank`), then unranked ones by creation time.
    All other cards stack in one column to the right of the grid, grouped by
    kind in `SIDE_KIND_ORDER`. Columns and rows are as wide/tall as their
    largest card, so resized cards never overlap.
    """
    cards = list(cards)
    venues = sorted((c for c in cards if c.kind == VENUE_KIND), key=_venue_order)
    side = sorted((c for c in cards if c.kind != VENUE_KIND), key=_side_order)

    rows = [venues[i : i + COLUMNS] for i in range(0, len(venues), COLUMNS)]
    column_x = _offsets([max(c.width for c in venues[i::COLUMNS]) for i in range(min(COLUMNS, len(venues)))])
    row_y = _offsets([max(c.height for c in row) for row in rows])

    positions: dict[str, Position] = {}
    for row, y in zip(rows, row_y, strict=False):
        for card, x in zip(row, column_x, strict=False):
            positions[card.id] = (x, y)
    side_x = column_x[-1]  # just past the last venue column; 0.0 when there are no venues
    for card, y in zip(side, _offsets([c.height for c in side]), strict=False):
        positions[card.id] = (side_x, y)
    return positions
