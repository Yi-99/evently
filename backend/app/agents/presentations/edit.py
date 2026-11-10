"""The `presentation_edit` run: a single-slide rewrite, or a tool-using agent for deck-level edits."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from app.agents.presentations import prompts, store
from app.agents.presentations.material import load_material
from app.agents.presentations.outline import CSS_THEME_FIELDS, claim_keywords, is_safe_css_value
from app.agents.presentations.render import render_slide, revise_slide, taken_keywords
from app.llm.base import RunCallbacks, Tool, ToolError
from app.models import AgentRun, Presentation, Slide
from app.schemas.presentation import DeckTheme, SlideOutline

if TYPE_CHECKING:
    from app.agents.runtime import RunContext
    from app.core.context import AppContext


async def start_edit(
    ctx: AppContext, *, presentation_id: str, instruction: str, slide_id: str | None, parent_run_id: str | None
) -> AgentRun:
    """Validate the ids and spawn the edit run (see package `__init__`)."""
    presentation = await store.require_presentation(ctx, presentation_id)
    if slide_id is not None:
        slide = await store.get_slide(ctx, slide_id)
        if slide is None or slide.presentation_id != presentation_id:
            raise LookupError(f"slide {slide_id} not found in presentation {presentation_id}")
        title = f"Edit slide {slide.position + 1}: {instruction[:80]}"
        fn = partial(edit_slide, slide_id=slide_id)
    else:
        title = f"Edit deck: {instruction[:80]}"
        fn = edit_deck
    card = await store.find_card(ctx, presentation)
    return await ctx.runtime.spawn(
        event_id=presentation.event_id,
        kind="presentation_edit",
        title=title,
        fn=fn,
        input={"presentation_id": presentation_id, "instruction": instruction, "slide_id": slide_id},
        parent_run_id=parent_run_id,
        card_id=card.id if card else None,
    )


async def _material(ctx: AppContext, presentation: Presentation) -> str:
    return await load_material(ctx, presentation.event_id, list((presentation.brief or {}).get("card_ids", [])))


async def edit_slide(run: RunContext, *, slide_id: str) -> dict[str, Any]:
    """Rewrite one slide. Output: {presentation_id, slide_id, summary}."""
    ctx = run.ctx
    presentation = await store.require_presentation(ctx, run.input["presentation_id"])
    material = await _material(ctx, presentation)
    await run.step("Redesigning the slide", progress=0.2)
    slide = await revise_slide(ctx, slide_id, run.input["instruction"], material)
    return {
        "presentation_id": presentation.id,
        "slide_id": slide.id,
        "summary": f'Updated slide {slide.position + 1}, "{slide.title}".',
    }


async def edit_deck(run: RunContext) -> dict[str, Any]:
    """Let the edit agent change the deck with its tools. Output: {presentation_id, changes, summary}."""
    ctx = run.ctx
    presentation = await store.require_presentation(ctx, run.input["presentation_id"])
    material = await _material(ctx, presentation)
    editor = DeckEditor(run, presentation.id, material)

    async def on_tool_start(name: str, _input: dict[str, Any]) -> None:
        await run.step(_STEP_LABELS.get(name, name))

    await run.step("Reading the deck", progress=0.1)
    slides = await store.list_slides(ctx, presentation.id)
    result = await ctx.llm.run(
        purpose="presentation_edit",
        system=prompts.EDIT_SYSTEM,
        messages=[
            {"role": "user", "content": prompts.edit_request(presentation, slides, run.input["instruction"], material)}
        ],
        tools=editor.tools(),
        tier="orchestrator",
        callbacks=RunCallbacks(on_tool_start=on_tool_start),
    )
    run.add_usage(result.usage)
    summary = result.text.strip() or ("; ".join(editor.changes) or "No changes were made to the deck.")
    return {"presentation_id": presentation.id, "changes": editor.changes, "summary": summary}


_STEP_LABELS = {
    "read_slides": "Reading slides",
    "update_slide": "Redesigning a slide",
    "add_slide": "Adding a slide",
    "delete_slide": "Removing a slide",
    "reorder_slides": "Reordering slides",
    "update_theme": "Restyling the deck",
}


# -- tool inputs ----------------------------------------------------------------


class ReadSlidesIn(BaseModel):
    slide_ids: list[str] = Field(default_factory=list, description="Slides to read in full; empty means all.")
    include_html: bool = Field(default=False, description="Also return each slide's HTML (long).")


class UpdateSlideIn(BaseModel):
    slide_id: str
    instruction: str | None = Field(
        default=None,
        description="What to change about the slide, for the slide designer, who re-renders it from its "
        "current content. Required for anything visible on the slide, its headline included.",
    )
    title: str | None = Field(default=None, description="Set the stored title directly (does not redraw the slide).")
    notes: str | None = Field(default=None, description="Replace the speaker notes.")
    keywords: list[str] | None = Field(default=None, description="Replace the voice triggers (2-4 spoken phrases).")


class AddSlideIn(SlideOutline):
    position: int = Field(ge=0, description="0-based index for the new slide; past the end appends.")


class DeleteSlideIn(BaseModel):
    slide_id: str


class ReorderSlidesIn(BaseModel):
    slide_ids: list[str] = Field(description="Every slide id of the deck exactly once, in the new order.")


class UpdateThemeIn(BaseModel):
    name: str | None = None
    background: str | None = Field(default=None, description="CSS colour behind every slide.")
    surface: str | None = Field(default=None, description="CSS colour of panels on top of the background.")
    text: str | None = None
    muted: str | None = Field(default=None, description="Secondary text colour.")
    accent: str | None = None
    accent_2: str | None = None
    heading_font: str | None = Field(default=None, description="CSS font-family stack of installed fonts.")
    body_font: str | None = None
    style_notes: str | None = Field(default=None, description="Art direction for slides rendered from now on.")


# -- tools ----------------------------------------------------------------------


def _brief(slide: Slide) -> dict[str, Any]:
    return {"slide_id": slide.id, "position": slide.position, "title": slide.title, "status": slide.status}


def _detail(slide: Slide, include_html: bool) -> dict[str, Any]:
    outline = slide.outline or {}
    detail = {
        **_brief(slide),
        "purpose": outline.get("purpose", ""),
        "key_points": outline.get("key_points", []),
        "visual": outline.get("visual", ""),
        "notes": slide.notes,
        "keywords": slide.keywords,
    }
    if include_html:
        detail["html"] = slide.html
    return detail


class DeckEditor:
    """Tool handlers for one edit run. Every handler persists and publishes through `store`."""

    def __init__(self, run: RunContext, presentation_id: str, material: str) -> None:
        self.ctx = run.ctx
        self.presentation_id = presentation_id
        self.material = material
        self.changes: list[str] = []

    def tools(self) -> list[Tool]:
        return [
            Tool(
                "read_slides",
                "Full details of slides (purpose, key points, visual idea, speaker notes, keywords, optionally "
                "HTML). The overview you already have is usually enough to act.",
                ReadSlidesIn,
                self.read_slides,
            ),
            Tool(
                "update_slide",
                "Change one slide. `instruction` sends the slide to the designer, who rewrites its HTML from "
                "the current slide (and updates title/notes/keywords when the message changes). `title`, "
                "`notes` and `keywords` set those fields directly without redrawing.",
                UpdateSlideIn,
                self.update_slide,
            ),
            Tool(
                "add_slide",
                "Insert a new slide at `position` from an outline (title, purpose, key points, visual idea, "
                "speaker notes, keywords). The slide designer renders it in the deck's theme.",
                AddSlideIn,
                self.add_slide,
            ),
            Tool("delete_slide", "Remove a slide; later slides move up.", DeleteSlideIn, self.delete_slide),
            Tool(
                "reorder_slides",
                "Set the deck's order. Pass every slide id exactly once.",
                ReorderSlidesIn,
                self.reorder_slides,
            ),
            Tool(
                "update_theme",
                "Change the deck theme: colours, fonts, style notes. Slides use these as CSS variables, so "
                "the whole deck restyles instantly with no re-rendering. Prefer this for any look-and-feel "
                "request (mood, brand colours, dark/light, fonts). Only the fields given change.",
                UpdateThemeIn,
                self.update_theme,
            ),
        ]

    async def _slide(self, slide_id: str) -> Slide:
        slide = await store.get_slide(self.ctx, slide_id)
        if slide is None or slide.presentation_id != self.presentation_id:
            raise ToolError(f"No slide {slide_id} in this deck.")
        return slide

    async def read_slides(self, args: ReadSlidesIn) -> list[dict[str, Any]]:
        slides = await store.list_slides(self.ctx, self.presentation_id)
        wanted = set(args.slide_ids)
        return [_detail(s, args.include_html) for s in slides if not wanted or s.id in wanted]

    async def update_slide(self, args: UpdateSlideIn) -> dict[str, Any]:
        slide = await self._slide(args.slide_id)
        if args.instruction is None and args.title is None and args.notes is None and args.keywords is None:
            raise ToolError("Give an instruction or at least one of title, notes, keywords.")
        if args.instruction:
            slide = await revise_slide(self.ctx, slide.id, args.instruction, self.material)
        if args.title is not None or args.notes is not None or args.keywords is not None:
            keywords = None
            if args.keywords is not None:
                slides = await store.list_slides(self.ctx, self.presentation_id)
                keywords = claim_keywords(args.keywords, taken_keywords(slides, slide.id))
            slide = await store.update_slide(
                self.ctx, slide.id, title=args.title, notes=args.notes, keywords=keywords, status=None
            )
        self.changes.append(f"updated slide {slide.position + 1} ({slide.title})")
        return _brief(slide)

    async def add_slide(self, args: AddSlideIn) -> dict[str, Any]:
        slides = await store.list_slides(self.ctx, self.presentation_id)
        outline = SlideOutline.model_validate(args.model_dump(exclude={"position"}))
        outline.keywords = claim_keywords(outline.keywords, taken_keywords(slides))
        slide = await store.insert_slide(self.ctx, self.presentation_id, args.position, outline)
        try:
            slide = await render_slide(self.ctx, slide.id, self.material)
        except Exception as exc:
            raise ToolError(
                f"Slide {slide.id} was added at position {slide.position} but could not be rendered "
                f"({type(exc).__name__}: {exc}). Retry with update_slide and an instruction, or delete it."
            ) from exc
        self.changes.append(f"added slide {slide.position + 1} ({slide.title})")
        return _brief(slide)

    async def delete_slide(self, args: DeleteSlideIn) -> dict[str, Any]:
        slide = await self._slide(args.slide_id)
        if len(await store.list_slides(self.ctx, self.presentation_id)) == 1:
            raise ToolError("A deck needs at least one slide; update it instead of deleting it.")
        await store.delete_slide(self.ctx, slide.id)
        self.changes.append(f"deleted slide ({slide.title})")
        return {"deleted": slide.id}

    async def reorder_slides(self, args: ReorderSlidesIn) -> list[dict[str, Any]]:
        try:
            slides = await store.reorder_slides(self.ctx, self.presentation_id, args.slide_ids)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        self.changes.append("reordered the slides")
        return [_brief(s) for s in slides]

    async def update_theme(self, args: UpdateThemeIn) -> dict[str, Any]:
        changes = {name: value for name, value in args.model_dump().items() if value is not None}
        if not changes:
            raise ToolError("Give at least one theme field to change.")
        bad = [name for name in CSS_THEME_FIELDS if name in changes and not is_safe_css_value(changes[name])]
        if bad:
            raise ToolError(f"Not plain CSS values: {', '.join(bad)}. Use colours like #1d4ed8 and font stacks.")
        presentation = await store.require_presentation(self.ctx, self.presentation_id)
        theme = DeckTheme.model_validate({**DeckTheme.model_validate(presentation.theme).model_dump(), **changes})
        await store.update_presentation(self.ctx, self.presentation_id, theme=theme.model_dump())
        self.changes.append(f"updated the theme ({', '.join(changes)})")
        return theme.model_dump()
