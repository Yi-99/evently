"""Board service: the only place cards and edges are written.

Agents and API routes both go through these functions so every change is
persisted, versioned and published on the event topic.

Field locking: when a user edits a top-level key of `card.data`, that key is
added to `card.locked_fields`; later agent patches silently skip locked keys so
a re-run never overwrites a manual correction.

Ordering: every write to a board runs under that event's lock and publishes
before releasing it, so concurrent writers (a user edit racing an agent patch)
never lose each other's changes and envelopes go out in commit order. The lock
is per process; do not call a writing function from inside another one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError

from app.core.ids import utcnow
from app.models import Board, Card, Edge
from app.realtime import events as ev
from app.schemas.common import BoardOut, CardOut, EdgeOut, dump
from app.services import layout
from app.services.layout import CARD_H, CARD_W, COLUMNS, GAP, Position  # noqa: F401 - grid constants stay importable

if TYPE_CHECKING:
    from app.core.context import AppContext

Actor = Literal["user", "agent", "system"]


class CardNotFound(LookupError):
    pass


class InvalidEdge(ValueError):
    """The edge cannot exist (it would connect a card to itself)."""


class VersionConflict(Exception):
    """The caller's `expected_version` is stale."""

    def __init__(self, card: Card) -> None:
        super().__init__(f"card {card.id} is at version {card.version}")
        self.card = card


def _lock(ctx: AppContext, event_id: str) -> asyncio.Lock:
    locks: dict[str, asyncio.Lock] = ctx.service("board_locks", dict)
    return locks.setdefault(event_id, asyncio.Lock())


async def _event_id_of(ctx: AppContext, model: type[Card] | type[Edge], row_id: str) -> str | None:
    """The event a card/edge belongs to (it never changes), so its lock can be taken before loading the row."""
    async with ctx.db() as session:
        return await session.scalar(select(model.event_id).where(model.id == row_id))


def card_out(card: Card) -> dict[str, Any]:
    return dump(CardOut, card)


def edge_out(edge: Edge) -> dict[str, Any]:
    return dump(EdgeOut, edge)


def board_out(board: Board) -> dict[str, Any]:
    return dump(BoardOut, board)


# --- board ---------------------------------------------------------------------


async def get_or_create_board(ctx: AppContext, event_id: str) -> Board:
    query = select(Board).where(Board.event_id == event_id)
    async with ctx.db() as session:
        board = (await session.execute(query)).scalar_one_or_none()
        if board is None:
            board = Board(event_id=event_id)
            session.add(board)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                board = (await session.execute(query)).scalar_one_or_none()
                if board is None:  # not a concurrent create (e.g. unknown event): surface the real error
                    raise
        return board


async def update_board(
    ctx: AppContext, event_id: str, *, name: str | None = None, viewport: dict[str, Any] | None = None
) -> Board:
    """Rename the board and/or replace its saved viewport (`{x, y, zoom}`). Omitted fields are kept.

    Publishes `board.updated {board}` on the event topic.
    """
    board_id = (await get_or_create_board(ctx, event_id)).id
    async with _lock(ctx, event_id):
        async with ctx.db() as session:
            board = await session.get(Board, board_id)
            if name is not None:
                board.name = name
            if viewport is not None:
                board.viewport = viewport
            await session.commit()
        await ctx.bus.publish(ev.event_topic(event_id), ev.BOARD_UPDATED, {"board": board_out(board)})
    return board


# --- cards ---------------------------------------------------------------------


async def list_cards(ctx: AppContext, event_id: str) -> list[Card]:
    async with ctx.db() as session:
        rows = await session.execute(select(Card).where(Card.event_id == event_id).order_by(Card.created_at))
        return list(rows.scalars())


async def get_card(ctx: AppContext, card_id: str) -> Card | None:
    async with ctx.db() as session:
        return await session.get(Card, card_id)


async def create_card(
    ctx: AppContext,
    *,
    event_id: str,
    kind: str,
    title: str = "",
    data: dict[str, Any] | None = None,
    status: str = "ready",
    agent_run_id: str | None = None,
    x: float | None = None,
    y: float | None = None,
    width: float | None = None,
    height: float | None = None,
    created_by: Actor = "agent",
) -> Card:
    """Create a card and publish `board.card.created`. Omit x/y to auto-place it."""
    board = await get_or_create_board(ctx, event_id)
    async with _lock(ctx, event_id):
        async with ctx.db() as session:
            if x is None or y is None:
                existing = list((await session.execute(select(Card).where(Card.board_id == board.id))).scalars())
                x, y = layout.next_free_position(existing)
            card = Card(
                board_id=board.id,
                event_id=event_id,
                kind=kind,
                title=title,
                data=data or {},
                status=status,
                agent_run_id=agent_run_id,
                x=x,
                y=y,
                width=width or CARD_W,
                height=height or CARD_H,
                created_by=created_by,
            )
            session.add(card)
            await session.commit()
        await ctx.bus.publish(ev.event_topic(event_id), ev.BOARD_CARD_CREATED, {"card": card_out(card)})
    return card


async def patch_card(
    ctx: AppContext,
    card_id: str,
    *,
    by: Actor = "agent",
    title: str | None = None,
    status: str | None = None,
    data_patch: dict[str, Any] | None = None,
    unlock_fields: list[str] | None = None,
    agent_run_id: str | None = None,
    x: float | None = None,
    y: float | None = None,
    width: float | None = None,
    height: float | None = None,
    z_index: int | None = None,
    expected_version: int | None = None,
) -> Card:
    """Update a card and publish `board.card.updated`.

    data_patch: shallow merge into `card.data` by top-level key; a value of
        None removes the key. With by="agent", keys in `card.locked_fields` are
        skipped. With by="user", patched keys become locked.
    unlock_fields: top-level data keys to release for agents again.
    expected_version: optimistic concurrency check; raises `VersionConflict`.
    """
    event_id = await _event_id_of(ctx, Card, card_id)
    if event_id is None:
        raise CardNotFound(card_id)

    changed: list[str] = []
    async with _lock(ctx, event_id):
        async with ctx.db() as session:
            card = await session.get(Card, card_id)
            if card is None:
                raise CardNotFound(card_id)
            if expected_version is not None and card.version != expected_version:
                raise VersionConflict(card)

            simple = {
                "title": title,
                "status": status,
                "agent_run_id": agent_run_id,
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "z_index": z_index,
            }
            for name, value in simple.items():
                if value is not None and getattr(card, name) != value:
                    setattr(card, name, value)
                    changed.append(name)

            locked = list(card.locked_fields or [])
            if unlock_fields:
                remaining = [f for f in locked if f not in unlock_fields]
                if remaining != locked:
                    locked = remaining
                    changed.append("locked_fields")

            if data_patch:
                data = dict(card.data or {})
                touched = False
                for key, value in data_patch.items():
                    if by == "agent" and key in locked:
                        continue
                    if value is None:
                        touched = data.pop(key, None) is not None or touched
                    elif data.get(key) != value:
                        data[key] = value
                        touched = True
                    if by == "user" and key not in locked:
                        locked.append(key)
                        if "locked_fields" not in changed:
                            changed.append("locked_fields")
                if touched:
                    card.data = data
                    changed.append("data")

            if "locked_fields" in changed:
                card.locked_fields = locked
            if changed:
                card.version += 1
                await session.commit()

        if changed:
            await ctx.bus.publish(
                ev.event_topic(card.event_id),
                ev.BOARD_CARD_UPDATED,
                {"card": card_out(card), "changed": changed, "by": by},
            )
    return card


async def delete_card(ctx: AppContext, card_id: str) -> bool:
    """Delete a card and every edge attached to it; False if the card does not exist.

    Publishes `board.edge.deleted` for each removed edge, then `board.card.deleted`.
    """
    event_id = await _event_id_of(ctx, Card, card_id)
    if event_id is None:
        return False
    topic = ev.event_topic(event_id)
    async with _lock(ctx, event_id):
        async with ctx.db() as session:
            card = await session.get(Card, card_id)
            if card is None:
                return False
            attached = or_(Edge.source_card_id == card_id, Edge.target_card_id == card_id)
            edge_ids = list(
                (await session.execute(select(Edge.id).where(attached).order_by(Edge.created_at))).scalars()
            )
            await session.execute(delete(Edge).where(attached))
            await session.delete(card)
            await session.commit()
        for edge_id in edge_ids:
            await ctx.bus.publish(topic, ev.BOARD_EDGE_DELETED, {"edge_id": edge_id})
        await ctx.bus.publish(topic, ev.BOARD_CARD_DELETED, {"card_id": card_id})
    return True


# --- positions -----------------------------------------------------------------


def _apply_positions(cards: Mapping[str, Card], positions: Mapping[str, Position]) -> list[tuple[Card, list[str]]]:
    """Move `cards` in place and bump their versions; returns (card, changed fields) for each that really moved."""
    moved: list[tuple[Card, list[str]]] = []
    # Stamp explicitly: in a multi-row flush the `onupdate` clock is read once per row, so the value stored
    # would differ by microseconds from the one left on the instance (and hence in the envelopes).
    now = utcnow()
    for card_id, (x, y) in positions.items():
        card = cards[card_id]
        changed = [name for name, value in (("x", x), ("y", y)) if getattr(card, name) != value]
        if changed:
            card.x, card.y = x, y
            card.version += 1
            card.updated_at = now
            moved.append((card, changed))
    return moved


async def _publish_moves(ctx: AppContext, moved: list[tuple[Card, list[str]]], by: Actor) -> None:
    for card, changed in moved:
        await ctx.bus.publish(
            ev.event_topic(card.event_id),
            ev.BOARD_CARD_UPDATED,
            {"card": card_out(card), "changed": changed, "by": by},
        )


async def move_cards(
    ctx: AppContext, event_id: str, positions: Mapping[str, Position], *, by: Actor = "user"
) -> list[Card]:
    """Set the (x, y) of several cards in one transaction, e.g. a multi-select drag.

    positions: card id -> (x, y). All or nothing: if any id is not a card of
        this event, raises `CardNotFound` and moves none.
    Publishes one `board.card.updated` per card whose position changed.
    Returns the cards in the order given.
    """
    async with _lock(ctx, event_id):
        async with ctx.db() as session:
            rows = await session.execute(select(Card).where(Card.event_id == event_id, Card.id.in_(list(positions))))
            cards = {card.id: card for card in rows.scalars()}
            for card_id in positions:
                if card_id not in cards:
                    raise CardNotFound(card_id)
            moved = _apply_positions(cards, positions)
            await session.commit()
        await _publish_moves(ctx, moved, by)
    return [cards[card_id] for card_id in positions]


async def auto_layout(ctx: AppContext, event_id: str, *, by: Actor = "user") -> list[Card]:
    """Tidy the whole board with `layout.auto_layout`.

    Publishes one `board.card.updated` per card whose position changed.
    Returns every card of the event, in creation order, at its new position.
    """
    async with _lock(ctx, event_id):
        async with ctx.db() as session:
            rows = await session.execute(select(Card).where(Card.event_id == event_id).order_by(Card.created_at))
            cards = list(rows.scalars())
            moved = _apply_positions({card.id: card for card in cards}, layout.auto_layout(cards))
            await session.commit()
        await _publish_moves(ctx, moved, by)
    return cards


# --- edges ---------------------------------------------------------------------


async def list_edges(ctx: AppContext, event_id: str) -> list[Edge]:
    async with ctx.db() as session:
        rows = await session.execute(select(Edge).where(Edge.event_id == event_id).order_by(Edge.created_at))
        return list(rows.scalars())


async def get_edge(ctx: AppContext, edge_id: str) -> Edge | None:
    async with ctx.db() as session:
        return await session.get(Edge, edge_id)


async def create_edge(
    ctx: AppContext,
    *,
    event_id: str,
    source_card_id: str,
    target_card_id: str,
    label: str = "",
    data: dict[str, Any] | None = None,
) -> Edge:
    """Connect two cards of the event's board and publish `board.edge.created`.

    Raises `CardNotFound` if either card is missing or belongs to another
    event, and `InvalidEdge` for a self-loop. Parallel edges are allowed.
    """
    if source_card_id == target_card_id:
        raise InvalidEdge("an edge must connect two different cards")
    async with _lock(ctx, event_id):
        async with ctx.db() as session:
            cards = {card_id: await session.get(Card, card_id) for card_id in (source_card_id, target_card_id)}
            for card_id, card in cards.items():
                if card is None or card.event_id != event_id:
                    raise CardNotFound(card_id)
            edge = Edge(
                board_id=cards[source_card_id].board_id,
                event_id=event_id,
                source_card_id=source_card_id,
                target_card_id=target_card_id,
                label=label,
                data=data or {},
            )
            session.add(edge)
            await session.commit()
        await ctx.bus.publish(ev.event_topic(event_id), ev.BOARD_EDGE_CREATED, {"edge": edge_out(edge)})
    return edge


async def delete_edge(ctx: AppContext, edge_id: str) -> bool:
    """Delete an edge and publish `board.edge.deleted`; False if it does not exist."""
    event_id = await _event_id_of(ctx, Edge, edge_id)
    if event_id is None:
        return False
    async with _lock(ctx, event_id):
        async with ctx.db() as session:
            edge = await session.get(Edge, edge_id)
            if edge is None:
                return False
            await session.delete(edge)
            await session.commit()
        await ctx.bus.publish(ev.event_topic(event_id), ev.BOARD_EDGE_DELETED, {"edge_id": edge_id})
    return True
