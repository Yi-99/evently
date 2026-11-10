"""Test helpers shared by all packages."""

from __future__ import annotations

from typing import Any

from app.core.context import AppContext
from app.core.deps import get_dev_user
from app.models import Event
from app.realtime.events import Envelope, event_topic, session_topic


async def create_event(ctx: AppContext, name: str = "Test event", **fields: Any) -> Event:
    """Create an event owned by the dev user (the user unauthenticated test requests act as)."""
    user = await get_dev_user(ctx)
    event = Event(owner_id=user.id, name=name, **fields)
    async with ctx.db() as session:
        session.add(event)
        await session.commit()
    return event


def published(ctx: AppContext, event_id: str, type_prefix: str = "") -> list[Envelope]:
    """Envelopes published so far on an event topic, optionally filtered by type prefix."""
    return [e for e in ctx.bus.history(event_topic(event_id)) if e.type.startswith(type_prefix)]


def session_published(ctx: AppContext, session_id: str, type_prefix: str = "") -> list[Envelope]:
    return [e for e in ctx.bus.history(session_topic(session_id)) if e.type.startswith(type_prefix)]
