"""Deck persistence: the only place this package writes `Presentation` and `Slide` rows.

Every write commits, bumps the affected `version` and publishes the matching envelope on the
event topic, so callers never publish themselves. Writes that change nothing publish nothing.

Versions: `Slide.version` moves when that slide's content or status changes. `Presentation.version`
moves when the presentation row changes (title, theme, status) or the deck's structure does (a
slide added, deleted or moved). A position shift alone does not bump the shifted slides;
`slides.reordered` carries the authoritative order.

The deck's board card (`kind="presentation"`, `data = {presentation_id, title, slide_count,
status}`) is kept in step with every such change.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from app.models import Card, Event, Presentation, Slide
from app.realtime import events as ev
from app.schemas.common import dump
from app.schemas.presentation import DeckTheme, PresentationBrief, PresentationOut, SlideOut, SlideOutline
from app.services import board

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.context import AppContext

TITLE_MAX = 300  # column width of Presentation.title / Slide.title
UNFINISHED = ("pending", "rendering")
_CARD_STATUS = {"generating": "running", "ready": "ready", "error": "error"}


def _write_lock(ctx: AppContext) -> asyncio.Lock:
    # One lock for all deck writes: they are short, and it keeps `position` and `version`
    # consistent when the edit agent issues several tool calls in the same turn.
    return ctx.service("presentation_write_lock", asyncio.Lock)


async def _publish(ctx: AppContext, event_id: str, type: str, data: dict[str, Any]) -> None:
    await ctx.bus.publish(ev.event_topic(event_id), type, data)


def _apply(row: Any, fields: dict[str, Any]) -> bool:
    """Set the fields that are not None; True if any value actually changed."""
    changed = False
    for name, value in fields.items():
        if value is not None and getattr(row, name) != value:
            setattr(row, name, value)
            changed = True
    return changed


async def _slides(session: AsyncSession, presentation_id: str) -> list[Slide]:
    rows = await session.execute(
        select(Slide).where(Slide.presentation_id == presentation_id).order_by(Slide.position, Slide.created_at)
    )
    return list(rows.scalars())


def _new_slide(presentation_id: str, position: int, outline: SlideOutline) -> Slide:
    return Slide(
        presentation_id=presentation_id,
        position=position,
        title=outline.title[:TITLE_MAX],
        notes=outline.notes,
        keywords=list(outline.keywords),
        outline=outline.model_dump(),
        status="pending",
    )


# -- reads ----------------------------------------------------------------------


async def get_presentation(ctx: AppContext, presentation_id: str) -> Presentation | None:
    async with ctx.db() as session:
        return await session.get(Presentation, presentation_id)


async def require_presentation(ctx: AppContext, presentation_id: str) -> Presentation:
    presentation = await get_presentation(ctx, presentation_id)
    if presentation is None:
        raise LookupError(f"presentation {presentation_id} not found")
    return presentation


async def get_slide(ctx: AppContext, slide_id: str) -> Slide | None:
    async with ctx.db() as session:
        return await session.get(Slide, slide_id)


async def require_slide(ctx: AppContext, slide_id: str) -> Slide:
    slide = await get_slide(ctx, slide_id)
    if slide is None:
        raise LookupError(f"slide {slide_id} not found")
    return slide


async def list_slides(ctx: AppContext, presentation_id: str) -> list[Slide]:
    """The deck's slides in display order."""
    async with ctx.db() as session:
        return await _slides(session, presentation_id)


# -- board card -----------------------------------------------------------------


def _card_data(presentation: Presentation, slide_count: int) -> dict[str, Any]:
    return {
        "presentation_id": presentation.id,
        "title": presentation.title,
        "slide_count": slide_count,
        "status": presentation.status,
    }


async def create_card(ctx: AppContext, presentation: Presentation) -> Card:
    """Put the deck on the board. The card shows as running until the build settles."""
    return await board.create_card(
        ctx,
        event_id=presentation.event_id,
        kind="presentation",
        title=presentation.title,
        status=_CARD_STATUS.get(presentation.status, "ready"),
        data=_card_data(presentation, 0),
    )


async def find_card(ctx: AppContext, presentation: Presentation) -> Card | None:
    cards = await board.list_cards(ctx, presentation.event_id)
    return next(
        (c for c in cards if c.kind == "presentation" and (c.data or {}).get("presentation_id") == presentation.id),
        None,
    )


async def sync_card(ctx: AppContext, presentation: Presentation) -> None:
    """Mirror title, slide count and status onto the deck's card (a no-op if the planner deleted it)."""
    card = await find_card(ctx, presentation)
    if card is None:
        return
    async with ctx.db() as session:
        slide_count = await session.scalar(
            select(func.count()).select_from(Slide).where(Slide.presentation_id == presentation.id)
        )
    try:
        await board.patch_card(
            ctx,
            card.id,
            title=presentation.title,
            status=_CARD_STATUS.get(presentation.status),
            data_patch=_card_data(presentation, slide_count or 0),
        )
    except board.CardNotFound:
        pass  # deleted between the lookup and the patch


# -- presentation writes --------------------------------------------------------


async def create_presentation(ctx: AppContext, *, event_id: str, brief: PresentationBrief) -> Presentation:
    """Insert a "generating" presentation and publish `presentation.created`.

    Raises `LookupError` if the event does not exist.
    """
    presentation = Presentation(
        event_id=event_id,
        title=(brief.title or "Untitled deck")[:TITLE_MAX],
        brief=brief.model_dump(mode="json"),
        theme=DeckTheme().model_dump(),
        status="generating",
    )
    async with ctx.db() as session:
        if await session.get(Event, event_id) is None:
            raise LookupError(f"event {event_id} not found")
        session.add(presentation)
        await session.commit()
    await _publish(ctx, event_id, ev.PRESENTATION_CREATED, {"presentation": dump(PresentationOut, presentation)})
    return presentation


async def update_presentation(
    ctx: AppContext,
    presentation_id: str,
    *,
    title: str | None = None,
    theme: dict[str, Any] | None = None,
    status: str | None = None,
    agent_run_id: str | None = None,
) -> Presentation:
    """Change the given fields, publish `presentation.updated` and sync the board card."""
    fields = {
        "title": title[:TITLE_MAX] if title is not None else None,
        "theme": theme,
        "status": status,
        "agent_run_id": agent_run_id,
    }
    async with _write_lock(ctx), ctx.db() as session:
        presentation = await session.get(Presentation, presentation_id)
        if presentation is None:
            raise LookupError(f"presentation {presentation_id} not found")
        changed = _apply(presentation, fields)
        if changed:
            presentation.version += 1
            await session.commit()
    if changed:
        await _presentation_updated(ctx, presentation)
    return presentation


async def _presentation_updated(ctx: AppContext, presentation: Presentation) -> None:
    await _publish(
        ctx, presentation.event_id, ev.PRESENTATION_UPDATED, {"presentation": dump(PresentationOut, presentation)}
    )
    await sync_card(ctx, presentation)


async def _structure_changed(ctx: AppContext, presentation: Presentation, slides: list[Slide]) -> None:
    await _publish(
        ctx,
        presentation.event_id,
        ev.SLIDES_REORDERED,
        {"presentation_id": presentation.id, "slide_ids": [s.id for s in slides]},
    )
    await _presentation_updated(ctx, presentation)


# -- slide writes ---------------------------------------------------------------


async def create_slides(ctx: AppContext, presentation_id: str, outlines: list[SlideOutline]) -> list[Slide]:
    """Append one "pending" slide per outline entry (the deck skeleton) and publish `slide.created` for each."""
    async with _write_lock(ctx), ctx.db() as session:
        presentation = await session.get(Presentation, presentation_id)
        if presentation is None:
            raise LookupError(f"presentation {presentation_id} not found")
        start = len(await _slides(session, presentation_id))
        slides = [_new_slide(presentation_id, start + i, outline) for i, outline in enumerate(outlines)]
        session.add_all(slides)
        await session.commit()
    for slide in slides:
        await _publish(ctx, presentation.event_id, ev.SLIDE_CREATED, {"slide": dump(SlideOut, slide)})
    await sync_card(ctx, presentation)
    return slides


async def insert_slide(ctx: AppContext, presentation_id: str, position: int, outline: SlideOutline) -> Slide:
    """Insert a "pending" slide at `position` (clamped to the deck), shifting later slides down.

    Publishes `slide.created`, then `slides.reordered` and `presentation.updated`.
    """
    async with _write_lock(ctx), ctx.db() as session:
        presentation = await session.get(Presentation, presentation_id)
        if presentation is None:
            raise LookupError(f"presentation {presentation_id} not found")
        slides = await _slides(session, presentation_id)
        position = max(0, min(position, len(slides)))
        slide = _new_slide(presentation_id, position, outline)
        slides.insert(position, slide)
        for index, row in enumerate(slides):
            row.position = index
        session.add(slide)
        presentation.version += 1
        await session.commit()
    await _publish(ctx, presentation.event_id, ev.SLIDE_CREATED, {"slide": dump(SlideOut, slide)})
    await _structure_changed(ctx, presentation, slides)
    return slide


async def update_slide(
    ctx: AppContext,
    slide_id: str,
    *,
    title: str | None = None,
    html: str | None = None,
    notes: str | None = None,
    keywords: list[str] | None = None,
    status: str | None = None,
) -> Slide:
    """Change the given fields of one slide and publish `slide.updated`."""
    fields = {
        "title": title[:TITLE_MAX] if title is not None else None,
        "html": html,
        "notes": notes,
        "keywords": keywords,
        "status": status,
    }
    async with _write_lock(ctx), ctx.db() as session:
        slide = await session.get(Slide, slide_id)
        if slide is None:
            raise LookupError(f"slide {slide_id} not found")
        presentation = await session.get(Presentation, slide.presentation_id)
        changed = _apply(slide, fields)
        if changed:
            slide.version += 1
            await session.commit()
    if changed and presentation is not None:
        await _publish(ctx, presentation.event_id, ev.SLIDE_UPDATED, {"slide": dump(SlideOut, slide)})
    return slide


async def delete_slide(ctx: AppContext, slide_id: str) -> None:
    """Delete a slide and close the gap. Publishes `slide.deleted`, `slides.reordered`, `presentation.updated`."""
    async with _write_lock(ctx), ctx.db() as session:
        slide = await session.get(Slide, slide_id)
        if slide is None:
            raise LookupError(f"slide {slide_id} not found")
        presentation = await session.get(Presentation, slide.presentation_id)
        await session.delete(slide)
        await session.flush()
        slides = await _slides(session, slide.presentation_id)
        for index, row in enumerate(slides):
            row.position = index
        presentation.version += 1
        await session.commit()
    await _publish(
        ctx, presentation.event_id, ev.SLIDE_DELETED, {"slide_id": slide_id, "presentation_id": presentation.id}
    )
    await _structure_changed(ctx, presentation, slides)


async def reorder_slides(ctx: AppContext, presentation_id: str, slide_ids: list[str]) -> list[Slide]:
    """Put the deck in the given order. `slide_ids` must name every slide exactly once (else `ValueError`)."""
    async with _write_lock(ctx), ctx.db() as session:
        presentation = await session.get(Presentation, presentation_id)
        if presentation is None:
            raise LookupError(f"presentation {presentation_id} not found")
        by_id = {s.id: s for s in await _slides(session, presentation_id)}
        if len(slide_ids) != len(by_id) or set(slide_ids) != set(by_id):
            raise ValueError(f"slide_ids must list each of the deck's slides exactly once: {sorted(by_id)}")
        slides = [by_id[slide_id] for slide_id in slide_ids]
        moved = [s for index, s in enumerate(slides) if s.position != index]
        for index, row in enumerate(slides):
            row.position = index
        if moved:
            presentation.version += 1
            await session.commit()
    if moved:
        await _structure_changed(ctx, presentation, slides)
    return slides


async def fail_unfinished_slides(ctx: AppContext, presentation_id: str) -> list[Slide]:
    """Mark slides still pending/rendering as "error" (their renderer is gone) and return the whole deck."""
    for slide in await list_slides(ctx, presentation_id):
        if slide.status in UNFINISHED:
            await update_slide(ctx, slide.id, status="error")
    return await list_slides(ctx, presentation_id)
