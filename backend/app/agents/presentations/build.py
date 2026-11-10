"""The `presentation_build` coordinator: outline the deck, then render every slide in parallel."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING, Any

from app.agents.presentations import store
from app.agents.presentations.material import load_material
from app.agents.presentations.outline import draft_outline
from app.agents.presentations.render import render_slide
from app.models import AgentRun, Presentation
from app.schemas.presentation import PresentationBrief
from app.services import board

if TYPE_CHECKING:
    from app.agents.runtime import RunContext
    from app.core.context import AppContext


class DeckBuildError(RuntimeError):
    """No slide of the deck could be rendered."""


async def start_build(
    ctx: AppContext, *, event_id: str, brief: PresentationBrief, parent_run_id: str | None
) -> tuple[Presentation, AgentRun]:
    """Create the presentation and its board card, then spawn the coordinator (see package `__init__`)."""
    presentation = await store.create_presentation(ctx, event_id=event_id, brief=brief)
    card = await store.create_card(ctx, presentation)
    run = await ctx.runtime.spawn(
        event_id=event_id,
        kind="presentation_build",
        title=f"Build deck: {brief.title or brief.prompt[:80]}",
        fn=build_deck,
        input={"presentation_id": presentation.id, "brief": brief.model_dump(mode="json")},
        parent_run_id=parent_run_id,
        card_id=card.id,
        limited=False,  # waits on its slide_render children
    )
    presentation = await store.update_presentation(ctx, presentation.id, agent_run_id=run.id)
    await board.patch_card(ctx, card.id, agent_run_id=run.id)
    return presentation, run


async def build_deck(run: RunContext) -> dict[str, Any]:
    """Coordinator body. Output: {presentation_id, slide_count, failed_slides, summary}.

    Whatever goes wrong, the presentation never stays "generating": unfinished slides are
    marked "error", child runs are cancelled and the presentation ends "error".
    """
    presentation_id: str = run.input["presentation_id"]
    children: list[str] = []
    try:
        return await _build(run, presentation_id, children)
    except BaseException:
        await asyncio.shield(_abandon(run.ctx, presentation_id, children))
        raise


async def _build(run: RunContext, presentation_id: str, children: list[str]) -> dict[str, Any]:
    ctx = run.ctx
    presentation = await store.require_presentation(ctx, presentation_id)
    brief = PresentationBrief.model_validate(presentation.brief)

    await run.step("Gathering what we know about the event", progress=0.02)
    material = await load_material(ctx, presentation.event_id, brief.card_ids)

    await run.step("Outlining the deck", progress=0.05)
    outline = await draft_outline(ctx, brief, material)
    await store.update_presentation(
        ctx, presentation_id, title=brief.title or outline.title, theme=outline.theme.model_dump()
    )
    slides = await store.create_slides(ctx, presentation_id, outline.slides)

    await run.step(f"Designing {len(slides)} slides", progress=0.15)
    for slide in slides:
        child = await run.spawn_child(
            kind="slide_render",
            title=f"Slide {slide.position + 1}: {slide.title}",
            fn=partial(render_slide_run, slide_id=slide.id, material=material),
            input={"presentation_id": presentation_id, "slide_id": slide.id, "position": slide.position},
        )
        children.append(child.id)
    await _watch_children(run, children)

    slides = await store.fail_unfinished_slides(ctx, presentation_id)
    ready = sum(1 for slide in slides if slide.status == "ready")
    failed = len(slides) - ready
    presentation = await store.update_presentation(ctx, presentation_id, status="ready" if ready else "error")
    summary = _summary(presentation.title, len(slides), failed)
    if not ready:
        raise DeckBuildError(summary)
    return {"presentation_id": presentation_id, "slide_count": len(slides), "failed_slides": failed, "summary": summary}


async def render_slide_run(run: RunContext, *, slide_id: str, material: str) -> dict[str, Any]:
    """Body of one `slide_render` child."""
    await run.step("Designing the slide", progress=0.1)
    slide = await render_slide(run.ctx, slide_id, material)
    return {"slide_id": slide.id, "position": slide.position}


async def _watch_children(run: RunContext, child_ids: list[str]) -> None:
    """Wait for every child, reporting progress as each one settles."""
    done = 0

    async def watch(child_id: str) -> None:
        nonlocal done
        await run.ctx.runtime.wait(child_id)
        done += 1
        await run.step(f"{done} of {len(child_ids)} slides designed", progress=0.15 + 0.8 * done / len(child_ids))

    await asyncio.gather(*(watch(child_id) for child_id in child_ids))


def _summary(title: str, slide_count: int, failed: int) -> str:
    slides = f"{slide_count} slide{'s' if slide_count != 1 else ''}"
    if failed == 0:
        return f'Your deck "{title}" is ready with {slides}.'
    if failed < slide_count:
        return (
            f'Your deck "{title}" is ready with {slide_count - failed} of {slides}; '
            f"{failed} could not be designed and can be regenerated."
        )
    return f'I could not build the deck "{title}": none of its {slides} could be designed.'


async def _abandon(ctx: AppContext, presentation_id: str, children: list[str]) -> None:
    for child_id in children:
        await ctx.runtime.cancel(child_id)
    await store.fail_unfinished_slides(ctx, presentation_id)
    await store.update_presentation(ctx, presentation_id, status="error")
