"""Who is looking at an event's board right now.

An in-memory, per-process registry of connected gateway clients (one entry per
WebSocket), stored on the context via `ctx.service("presence", ...)`. Every change
is published as `board.presence` on the event topic, so all clients (including
those on other workers, when a Redis bus fans out) see joins, leaves, cursors and
selections. Only the *snapshot* (`snapshot()`, sent in the WebSocket hello) is
limited to clients connected to this process.

`board.presence` data:

- join / cursor / selection: ``{client_id, user: {id, name}, color, cursor: {x, y} | null, selection: [card_id]}``
- leave:                     ``{client_id, left: true}``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.realtime import events as ev

if TYPE_CHECKING:
    from app.core.context import AppContext
    from app.models import User

# Distinct, readable on white; assigned lowest-unused-first so concurrent clients differ.
PALETTE = ("#e5484d", "#3e63dd", "#30a46c", "#f76b15", "#8e4ec6", "#0091ff", "#e93d82", "#12a594")


@dataclass
class PresenceClient:
    client_id: str
    user: dict[str, str]  # {id, name}
    color: str
    cursor: dict[str, float] | None = None
    selection: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "user": self.user,
            "color": self.color,
            "cursor": self.cursor,
            "selection": list(self.selection),
        }


class PresenceRegistry:
    """Pure bookkeeping; the module functions below add publishing."""

    def __init__(self) -> None:
        self._events: dict[str, dict[str, PresenceClient]] = {}

    def join(self, event_id: str, client_id: str, user: User) -> PresenceClient:
        clients = self._events.setdefault(event_id, {})
        identity = {"id": user.id, "name": user.name}
        client = PresenceClient(client_id=client_id, user=identity, color=self._color(clients))
        clients[client_id] = client
        return client

    def leave(self, event_id: str, client_id: str) -> PresenceClient | None:
        clients = self._events.get(event_id)
        if not clients:
            return None
        client = clients.pop(client_id, None)
        if not clients:
            del self._events[event_id]
        return client

    def get(self, event_id: str, client_id: str) -> PresenceClient | None:
        return self._events.get(event_id, {}).get(client_id)

    def clients(self, event_id: str) -> list[PresenceClient]:
        return list(self._events.get(event_id, {}).values())

    @staticmethod
    def _color(clients: dict[str, PresenceClient]) -> str:
        in_use = {c.color for c in clients.values()}
        free = [c for c in PALETTE if c not in in_use]
        return free[0] if free else PALETTE[len(clients) % len(PALETTE)]


def registry(ctx: AppContext) -> PresenceRegistry:
    return ctx.service("presence", PresenceRegistry)


def snapshot(ctx: AppContext, event_id: str) -> list[dict[str, Any]]:
    """Clients connected to this process for `event_id`, as `board.presence` payloads."""
    return [c.as_dict() for c in registry(ctx).clients(event_id)]


async def join(ctx: AppContext, event_id: str, client_id: str, user: User) -> PresenceClient:
    """Register a client and publish its (cursor-less) presence."""
    client = registry(ctx).join(event_id, client_id, user)
    await _publish(ctx, event_id, client.as_dict())
    return client


async def leave(ctx: AppContext, event_id: str, client_id: str) -> None:
    """Forget a client and publish ``{client_id, left: true}``. No-op for unknown clients."""
    if registry(ctx).leave(event_id, client_id) is not None:
        await _publish(ctx, event_id, {"client_id": client_id, "left": True})


async def update_cursor(ctx: AppContext, event_id: str, client_id: str, x: float, y: float) -> bool:
    """Move a client's cursor; publishes only when it actually changed. False if the client is unknown."""
    client = registry(ctx).get(event_id, client_id)
    if client is None:
        return False
    cursor = {"x": x, "y": y}
    if client.cursor != cursor:
        client.cursor = cursor
        await _publish(ctx, event_id, client.as_dict())
    return True


async def update_selection(ctx: AppContext, event_id: str, client_id: str, card_ids: list[str]) -> bool:
    """Replace a client's selected card ids; publishes only when it changed. False if the client is unknown."""
    client = registry(ctx).get(event_id, client_id)
    if client is None:
        return False
    if client.selection != card_ids:
        client.selection = list(card_ids)
        await _publish(ctx, event_id, client.as_dict())
    return True


async def _publish(ctx: AppContext, event_id: str, data: dict[str, Any]) -> None:
    await ctx.bus.publish(ev.event_topic(event_id), ev.BOARD_PRESENCE, data, retain=False)
