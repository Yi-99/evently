"""The slide designer: turns one slide's outline into a sanitised HTML fragment.

`render_slide` is used by the build coordinator's `slide_render` children and by the edit
agent's `add_slide`; `revise_slide` by single-slide edits and the edit agent's `update_slide`.
Both read the deck fresh from the database so a theme or title changed moments ago is honoured.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.agents.presentations import outline, prompts, store
from app.models import Slide
from app.services import html_sanitize

if TYPE_CHECKING:
    from app.core.context import AppContext

SLIDE_MAX_TOKENS = 32000  # a slide with hand-drawn SVG runs to several thousand tokens, plus thinking


class SlideRender(BaseModel):
    """What the slide designer returns for a new slide."""

    html: str = Field(description="The slide: one self-contained HTML fragment for the 1280x720 stage.")


class SlideRevision(BaseModel):
    """What the slide designer returns when revising a slide; null fields mean 'unchanged'."""

    html: str = Field(description="The complete revised slide fragment.")
    title: str | None = Field(default=None, description="New title, only if the instruction changes it.")
    notes: str | None = Field(default=None, description="New speaker notes, only if the message changed.")
    keywords: list[str] | None = Field(default=None, description="New voice triggers, only if the notes changed.")


def finish_html(raw: str) -> str:
    """Sanitised slide HTML. Raises `ValueError` when nothing displayable is left."""
    html = html_sanitize.sanitize_slide_html(raw).strip()
    if not html:
        raise ValueError("The slide designer returned no usable HTML.")
    return html


async def _deck(ctx: AppContext, slide: Slide):
    presentation = await store.require_presentation(ctx, slide.presentation_id)
    slides = await store.list_slides(ctx, slide.presentation_id)
    current = next((s for s in slides if s.id == slide.id), slide)
    return presentation, slides, current


async def render_slide(ctx: AppContext, slide_id: str, material: str) -> Slide:
    """Design a slide: status "rendering", then "ready" with its HTML, or "error".

    Every status change is published as `slide.updated`. Failures (including a refused or
    empty answer) are re-raised after the slide is marked "error", so the caller's run fails.
    """
    slide = await store.update_slide(ctx, slide_id, status="rendering")
    try:
        presentation, slides, slide = await _deck(ctx, slide)
        result = await ctx.llm.parse(
            purpose="slide_render",
            system=prompts.SLIDE_RENDER_SYSTEM,
            messages=[{"role": "user", "content": prompts.slide_request(presentation, slides, slide, material)}],
            output_model=SlideRender,
            max_tokens=SLIDE_MAX_TOKENS,
        )
        html = finish_html(result.html)
    except BaseException:
        # Shielded so a cancelled run still leaves the slide in a terminal state.
        await asyncio.shield(store.update_slide(ctx, slide_id, status="error"))
        raise
    return await store.update_slide(ctx, slide_id, html=html, status="ready")


async def revise_slide(ctx: AppContext, slide_id: str, instruction: str, material: str) -> Slide:
    """Rewrite a slide from its current content plus `instruction`.

    Title, notes and keywords change only when the designer returns them; new keywords are
    kept distinct from the other slides' triggers. If the rewrite fails the slide keeps its
    previous HTML and status, and the error is re-raised.
    """
    before = await store.require_slide(ctx, slide_id)
    await store.update_slide(ctx, slide_id, status="rendering")
    try:
        presentation, slides, slide = await _deck(ctx, before)
        revision = await ctx.llm.parse(
            purpose="slide_edit",
            system=prompts.SLIDE_EDIT_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": prompts.revision_request(presentation, slides, slide, instruction, material),
                }
            ],
            output_model=SlideRevision,
            max_tokens=SLIDE_MAX_TOKENS,
        )
        html = finish_html(revision.html)
    except BaseException:
        await asyncio.shield(store.update_slide(ctx, slide_id, status=before.status))
        raise
    keywords = None
    if revision.keywords is not None:
        keywords = outline.claim_keywords(revision.keywords, taken_keywords(slides, slide_id))
    return await store.update_slide(
        ctx, slide_id, html=html, title=revision.title, notes=revision.notes, keywords=keywords, status="ready"
    )


def taken_keywords(slides: list[Slide], except_id: str | None = None) -> set[str]:
    """Voice triggers already owned by other slides of the deck."""
    return {
        outline.normalize_keyword(keyword)
        for slide in slides
        if slide.id != except_id
        for keyword in (slide.keywords or [])
    }
