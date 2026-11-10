"""Presentation agents — public entrypoints.

Other packages (chat orchestrator, presentations API) call only these functions.
Implementation lives in sibling modules of this package:

    material.py  the event + board cards as prompt text (the only source of slide facts)
    outline.py   `deck_outline` parse call, keyword de-duplication, theme hygiene
    render.py    `slide_render` / `slide_edit` parse calls, sanitising, status changes
    build.py     the `presentation_build` coordinator and its `slide_render` children
    edit.py      the `presentation_edit` run and the deck editor's tools
    store.py     all Presentation / Slide writes, versioning, publishing, the board card
    prompts.py   system prompts and message builders
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.models import AgentRun, Presentation
from app.schemas.presentation import PresentationBrief

if TYPE_CHECKING:
    from app.core.context import AppContext


async def start_presentation_build(
    ctx: AppContext,
    *,
    event_id: str,
    brief: PresentationBrief,
    parent_run_id: str | None = None,
) -> tuple[Presentation, AgentRun]:
    """Create a `Presentation` (status "generating") and build it in the background.

    The coordinator (`kind="presentation_build"`, spawned with `limited=False`):
      1. asks Claude for a `DeckOutline` (title, theme, per-slide outline with speaker
         notes and voice-trigger keywords), using the event and `brief.card_ids` as context;
      2. creates one `Slide` row per outline entry (status "pending") and publishes `slide.created`;
      3. spawns one `slide_render` child per slide, in parallel; each writes a self-contained
         HTML fragment to `Slide.html` and publishes `slide.updated`;
      4. marks the presentation "ready" (or "error") and publishes `presentation.updated`.

    A `presentation` card is put on the board right away and kept in sync. The coordinator's
    output is `{presentation_id, slide_count, failed_slides, summary}`; when no slide could be
    rendered the run fails with `DeckBuildError` and the presentation ends "error".

    Returns the new presentation and the coordinator run without waiting for the build.
    Raises `LookupError` if the event does not exist.
    """
    from app.agents.presentations.build import start_build

    return await start_build(ctx, event_id=event_id, brief=brief, parent_run_id=parent_run_id)


async def start_presentation_edit(
    ctx: AppContext,
    *,
    presentation_id: str,
    instruction: str,
    slide_id: str | None = None,
    parent_run_id: str | None = None,
) -> AgentRun:
    """Apply a natural-language edit to a deck, or to one slide when `slide_id` is given.

    Spawns a `presentation_edit` run whose agent can restyle the theme and rewrite,
    add, remove or reorder slides; every change is persisted and published
    (`slide.*`, `slides.reordered`, `presentation.updated`). The run's output carries a
    one-sentence `summary` for the planner. Raises `LookupError` if the presentation or
    slide does not exist.
    """
    from app.agents.presentations.edit import start_edit

    return await start_edit(
        ctx, presentation_id=presentation_id, instruction=instruction, slide_id=slide_id, parent_run_id=parent_run_id
    )
