"""REST routes for presentations (mounted under /api/v1).

Decks are built and edited by the presentation agents (`app.agents.presentations`); this
router starts those runs, offers manual slide editing, and renders decks for browsers.
Every slide's HTML is sanitised on write and again on render, and rendered documents are
served with a strict Content-Security-Policy and a fresh script nonce per response.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import AppContext
from app.core.deps import Ctx, CurrentUser, OwnedEvent, Session, resolve_user
from app.core.security import TokenError, decode_participant_token
from app.models import Event, LiveSession, Presentation, Slide, User
from app.realtime import events as ev
from app.schemas.common import AgentRunOut, dump
from app.schemas.presentation import PresentationBrief, PresentationOut, SlideOut
from app.services import deck_render
from app.services.html_sanitize import sanitize_slide_html

router = APIRouter(tags=["presentations"])
_bearer = HTTPBearer(auto_error=False)
_LLM_UNAVAILABLE = "Claude is not configured. Set ANTHROPIC_API_KEY in .env."
_RENDER_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}


# --- request / response models -------------------------------------------------------------


class PresentationPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    theme: dict[str, str] | None = Field(default=None, description="Merged into the stored theme.")


class PromptBody(BaseModel):
    instruction: str = Field(min_length=1, max_length=20_000)


class SlideCreate(BaseModel):
    position: int | None = Field(default=None, ge=0, description="Insert index; omitted = append.")
    title: str = Field(default="", max_length=300)
    html: str = ""
    notes: str = ""
    keywords: list[str] = Field(default_factory=list)


class SlidePatch(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    html: str | None = None
    notes: str | None = None
    keywords: list[str] | None = None


class ReorderBody(BaseModel):
    slide_ids: list[str] = Field(description="Every slide of the deck, in the new order.")


class PresentationDetail(BaseModel):
    presentation: PresentationOut
    slides: list[SlideOut]


class PresentationStarted(BaseModel):
    presentation: PresentationOut
    run: AgentRunOut


class RunStarted(BaseModel):
    run: AgentRunOut


# --- helpers ---------------------------------------------------------------------------------


def _not_found(what: str = "Presentation") -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found")


def _require_llm(ctx: AppContext) -> None:
    if not ctx.llm.available:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _LLM_UNAVAILABLE)


def _lock(ctx: AppContext, presentation_id: str) -> asyncio.Lock:
    """Serialises slide-structure changes of one deck so positions stay contiguous."""
    locks: dict[str, asyncio.Lock] = ctx.service("presentation_locks", dict)
    return locks.setdefault(presentation_id, asyncio.Lock())


def _presentation_out(presentation: Presentation) -> dict[str, Any]:
    return dump(PresentationOut, presentation)


def _slide_out(slide: Slide) -> dict[str, Any]:
    return dump(SlideOut, slide)


async def _load_presentation(session: AsyncSession, presentation_id: str, user: User | None) -> Presentation:
    """The presentation, if it exists and `user` owns its event; 404 otherwise."""
    row = (
        await session.execute(
            select(Presentation, Event.owner_id).join(Event, Event.id == Presentation.event_id)
            .where(Presentation.id == presentation_id)
        )
    ).first()
    if row is None or user is None or row.owner_id != user.id:
        raise _not_found()
    return row.Presentation


async def _load_slide(session: AsyncSession, slide_id: str, user: User) -> tuple[Slide, Presentation]:
    slide = await session.get(Slide, slide_id)
    if slide is None:
        raise _not_found("Slide")
    try:
        presentation = await _load_presentation(session, slide.presentation_id, user)
    except HTTPException:
        raise _not_found("Slide") from None
    return slide, presentation


async def _deck_slides(session: AsyncSession, presentation_id: str) -> list[Slide]:
    query = select(Slide).where(Slide.presentation_id == presentation_id).order_by(Slide.position, Slide.created_at)
    return list((await session.execute(query)).scalars())


def _renumber(slides: list[Slide]) -> None:
    for position, slide in enumerate(slides):
        slide.position = position


async def _publish(ctx: AppContext, event_id: str, type_: str, data: dict[str, Any]) -> None:
    await ctx.bus.publish(ev.event_topic(event_id), type_, data)


async def _publish_presentation(ctx: AppContext, presentation: Presentation) -> None:
    data = {"presentation": _presentation_out(presentation)}
    await _publish(ctx, presentation.event_id, ev.PRESENTATION_UPDATED, data)


# --- presentations ---------------------------------------------------------------------------


@router.post(
    "/events/{event_id}/presentations", response_model=PresentationStarted, status_code=status.HTTP_202_ACCEPTED
)
async def create_presentation(event: OwnedEvent, brief: PresentationBrief, ctx: Ctx):
    """Start building a deck in the background. The build's progress arrives on the event topic."""
    from app.agents import presentations

    _require_llm(ctx)
    presentation, run = await presentations.start_presentation_build(ctx, event_id=event.id, brief=brief)
    return {"presentation": _presentation_out(presentation), "run": dump(AgentRunOut, run)}


@router.get("/events/{event_id}/presentations", response_model=list[PresentationOut])
async def list_presentations(event: OwnedEvent, session: Session):
    query = select(Presentation).where(Presentation.event_id == event.id).order_by(Presentation.created_at.desc())
    return list((await session.execute(query)).scalars())


@router.get("/presentations/{presentation_id}", response_model=PresentationDetail)
async def get_presentation(presentation_id: str, session: Session, user: CurrentUser):
    presentation = await _load_presentation(session, presentation_id, user)
    slides = await _deck_slides(session, presentation.id)
    return {"presentation": presentation, "slides": slides}


@router.patch("/presentations/{presentation_id}", response_model=PresentationOut)
async def patch_presentation(
    presentation_id: str, body: PresentationPatch, session: Session, user: CurrentUser, ctx: Ctx
):
    presentation = await _load_presentation(session, presentation_id, user)
    if body.title is not None:
        presentation.title = body.title
    if body.theme is not None:
        presentation.theme = {**presentation.theme, **body.theme}
    presentation.version += 1
    await session.commit()
    await _publish_presentation(ctx, presentation)
    return presentation


@router.delete("/presentations/{presentation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_presentation(presentation_id: str, session: Session, user: CurrentUser, ctx: Ctx):
    presentation = await _load_presentation(session, presentation_id, user)
    event_id = presentation.event_id
    await session.delete(presentation)
    await session.commit()
    await _publish(ctx, event_id, ev.PRESENTATION_DELETED, {"presentation_id": presentation_id})


@router.post("/presentations/{presentation_id}/prompt", response_model=RunStarted, status_code=status.HTTP_202_ACCEPTED)
async def prompt_presentation(presentation_id: str, body: PromptBody, session: Session, user: CurrentUser, ctx: Ctx):
    """Apply a natural-language edit to the whole deck."""
    from app.agents import presentations

    await _load_presentation(session, presentation_id, user)
    _require_llm(ctx)
    try:
        run = await presentations.start_presentation_edit(
            ctx, presentation_id=presentation_id, instruction=body.instruction
        )
    except LookupError:
        raise _not_found() from None
    return {"run": dump(AgentRunOut, run)}


@router.post("/slides/{slide_id}/prompt", response_model=RunStarted, status_code=status.HTTP_202_ACCEPTED)
async def prompt_slide(slide_id: str, body: PromptBody, session: Session, user: CurrentUser, ctx: Ctx):
    """Apply a natural-language edit to one slide."""
    from app.agents import presentations

    slide, presentation = await _load_slide(session, slide_id, user)
    _require_llm(ctx)
    try:
        run = await presentations.start_presentation_edit(
            ctx, presentation_id=presentation.id, instruction=body.instruction, slide_id=slide.id
        )
    except LookupError:
        raise _not_found("Slide") from None
    return {"run": dump(AgentRunOut, run)}


# --- manual slide editing --------------------------------------------------------------------


@router.post("/presentations/{presentation_id}/slides", response_model=SlideOut, status_code=status.HTTP_201_CREATED)
async def create_slide(presentation_id: str, body: SlideCreate, session: Session, user: CurrentUser, ctx: Ctx):
    presentation = await _load_presentation(session, presentation_id, user)
    async with _lock(ctx, presentation.id):
        slides = await _deck_slides(session, presentation.id)
        position = len(slides) if body.position is None else min(body.position, len(slides))
        slide = Slide(
            presentation_id=presentation.id,
            title=body.title,
            html=sanitize_slide_html(body.html),
            notes=body.notes,
            keywords=body.keywords,
            status="ready",
        )
        slides.insert(position, slide)
        _renumber(slides)
        session.add(slide)
        presentation.version += 1
        await session.commit()
    await _publish(ctx, presentation.event_id, ev.SLIDE_CREATED, {"slide": _slide_out(slide)})
    await _publish_presentation(ctx, presentation)
    return slide


@router.patch("/slides/{slide_id}", response_model=SlideOut)
async def patch_slide(slide_id: str, body: SlidePatch, session: Session, user: CurrentUser, ctx: Ctx):
    slide, presentation = await _load_slide(session, slide_id, user)
    if body.title is not None:
        slide.title = body.title
    if body.html is not None:
        slide.html = sanitize_slide_html(body.html)
        slide.status = "ready"
    if body.notes is not None:
        slide.notes = body.notes
    if body.keywords is not None:
        slide.keywords = body.keywords
    slide.version += 1
    await session.commit()
    await _publish(ctx, presentation.event_id, ev.SLIDE_UPDATED, {"slide": _slide_out(slide)})
    return slide


@router.delete("/slides/{slide_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_slide(slide_id: str, session: Session, user: CurrentUser, ctx: Ctx):
    slide, presentation = await _load_slide(session, slide_id, user)
    async with _lock(ctx, presentation.id):
        await session.delete(slide)
        await session.flush()
        _renumber(await _deck_slides(session, presentation.id))
        presentation.version += 1
        await session.commit()
    await _publish(
        ctx, presentation.event_id, ev.SLIDE_DELETED, {"slide_id": slide_id, "presentation_id": presentation.id}
    )
    await _publish_presentation(ctx, presentation)


@router.post("/presentations/{presentation_id}/slides/reorder", response_model=list[SlideOut])
async def reorder_slides(presentation_id: str, body: ReorderBody, session: Session, user: CurrentUser, ctx: Ctx):
    """Set the deck order. `slide_ids` must be a permutation of the deck's slides (422 otherwise)."""
    presentation = await _load_presentation(session, presentation_id, user)
    async with _lock(ctx, presentation.id):
        slides = await _deck_slides(session, presentation.id)
        by_id = {slide.id: slide for slide in slides}
        if len(body.slide_ids) != len(set(body.slide_ids)) or set(body.slide_ids) != set(by_id):
            raise HTTPException(
                422, "slide_ids must list every slide of the deck exactly once"
            )
        ordered = [by_id[slide_id] for slide_id in body.slide_ids]
        _renumber(ordered)
        presentation.version += 1
        await session.commit()
    reordered = {"presentation_id": presentation.id, "slide_ids": body.slide_ids}
    await _publish(ctx, presentation.event_id, ev.SLIDES_REORDERED, reordered)
    await _publish_presentation(ctx, presentation)
    return ordered


# --- rendering -------------------------------------------------------------------------------


async def _viewer_presentation(
    ctx: AppContext,
    session: AsyncSession,
    presentation_id: str,
    token: str | None,
    credentials: HTTPAuthorizationCredentials | None,
) -> Presentation:
    """The presentation for a render request.

    `?token=` (iframes cannot send headers) or the Bearer header may carry either a participant
    token (host, speaker or listener) of a live session that is presenting this very deck and has
    not ended — so attendees' phones can follow along — or the event owner's access token.
    """
    token = token or (credentials.credentials if credentials else None)
    if token:
        try:
            claims = decode_participant_token(ctx.settings, token)
        except TokenError:
            claims = None  # not a participant token; it may be the owner's access token
        if claims is not None:
            live = await session.get(LiveSession, claims.session_id)
            if live is not None and live.presentation_id == presentation_id and live.status != "ended":
                presentation = await session.get(Presentation, presentation_id)
                if presentation is not None:
                    return presentation
            raise _not_found()
    user = await resolve_user(ctx, token)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", {"WWW-Authenticate": "Bearer"})
    return await _load_presentation(session, presentation_id, user)


def _deck_response(presentation: Presentation, slides: list[Slide], *, start: int, controls: bool) -> HTMLResponse:
    nonce = secrets.token_urlsafe(18)
    document = deck_render.render_deck_document(
        presentation.title,
        presentation.theme,
        [{"id": s.id, "title": s.title, "html": s.html} for s in slides],
        nonce=nonce,
        start_index=start,
        controls=controls,
    )
    headers = {**_RENDER_HEADERS, "Content-Security-Policy": deck_render.deck_csp(nonce)}
    return HTMLResponse(document, headers=headers)


Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


@router.get("/presentations/{presentation_id}/render", response_class=HTMLResponse)
async def render_presentation(
    presentation_id: str,
    ctx: Ctx,
    session: Session,
    credentials: Credentials,
    slide: Annotated[int, Query(ge=0, description="0-based index of the slide shown first")] = 0,
    controls: Annotated[bool, Query(description="Keyboard/tap/swipe navigation and the slide counter")] = True,
    token: Annotated[
        str | None,
        Query(description="Participant token of a live session showing this deck, or the owner's access token"),
    ] = None,
):
    """The whole deck as a presentable HTML page (CSP header, per-response script nonce)."""
    presentation = await _viewer_presentation(ctx, session, presentation_id, token, credentials)
    slides = await _deck_slides(session, presentation.id)
    return _deck_response(presentation, slides, start=slide, controls=controls)


@router.get("/presentations/{presentation_id}/export", response_class=HTMLResponse)
async def export_presentation(presentation_id: str, session: Session, user: CurrentUser):
    """The deck as a self-contained downloadable `.html` file (its CSP travels in a <meta> tag)."""
    presentation = await _load_presentation(session, presentation_id, user)
    slides = await _deck_slides(session, presentation.id)
    response = _deck_response(presentation, slides, start=0, controls=True)
    filename = re.sub(r"[^A-Za-z0-9]+", "-", presentation.title).strip("-").lower() or "deck"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}.html"'
    return response


@router.get("/slides/{slide_id}/render", response_class=HTMLResponse)
async def render_slide(
    slide_id: str,
    ctx: Ctx,
    session: Session,
    credentials: Credentials,
    token: Annotated[
        str | None,
        Query(description="Participant token of a live session showing this deck, or the owner's access token"),
    ] = None,
):
    """One slide as a standalone document, for thumbnails and editors."""
    slide = await session.get(Slide, slide_id)
    if slide is None:
        raise _not_found("Slide")
    try:
        presentation = await _viewer_presentation(ctx, session, slide.presentation_id, token, credentials)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            raise _not_found("Slide") from None
        raise
    document = deck_render.render_slide_document(slide.html, presentation.theme)
    # `sandbox` (header-only) makes the slide an opaque origin even when loaded directly via `src`.
    headers = {**_RENDER_HEADERS, "Content-Security-Policy": f"{deck_render.slide_csp()}; sandbox"}
    return HTMLResponse(document, headers=headers)
