"""The outline step: ask Claude for a `DeckOutline`, then make it safe to build from."""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING

from app.agents.presentations import prompts
from app.schemas.presentation import DeckOutline, DeckTheme, PresentationBrief

if TYPE_CHECKING:
    from app.core.context import AppContext

MAX_SLIDES = 40  # same ceiling as PresentationBrief.slide_count; bounds the render fan-out
MAX_KEYWORDS = 4
OUTLINE_MAX_TOKENS = 32000  # a 40-slide outline with speaker notes, plus thinking

CSS_THEME_FIELDS = (
    "background",
    "surface",
    "text",
    "muted",
    "accent",
    "accent_2",
    "heading_font",
    "body_font",
)
# Theme values are written into a `:root{}` block by the deck renderer. Anything that could
# close the declaration or pull in a resource is refused here as well (defence in depth).
_UNSAFE_CSS = re.compile(r"[;{}<>\\\n\r]|/\*|url\(|expression\(", re.IGNORECASE)


def normalize_keyword(keyword: str) -> str:
    """Lower-case, collapse whitespace and trim punctuation so spoken-text matching is stable."""
    return " ".join(keyword.lower().split()).strip(" .,;:!?\"'()")


def _normalized(keywords: list[str]) -> list[str]:
    """Normalised keywords, blanks dropped, order-preserving de-duplication."""
    return list(dict.fromkeys(k for raw in keywords if (k := normalize_keyword(raw))))


def claim_keywords(keywords: list[str], taken: set[str]) -> list[str]:
    """Normalised keywords minus those in `taken` (other slides' triggers), capped at `MAX_KEYWORDS`."""
    return [k for k in _normalized(keywords) if k not in taken][:MAX_KEYWORDS]


def distinct_keywords(per_slide: list[list[str]]) -> list[list[str]]:
    """Per-slide voice triggers with every keyword that appears on more than one slide removed.

    A trigger shared by two slides cannot tell the tracker which one the speaker reached,
    so it is dropped from both rather than kept on either. The cap is applied after that,
    so a slide with five candidates still ends up with four when one of them collides.
    """
    normalised = [_normalized(keywords) for keywords in per_slide]
    counts = Counter(keyword for keywords in normalised for keyword in keywords)
    return [[k for k in keywords if counts[k] == 1][:MAX_KEYWORDS] for keywords in normalised]


def is_safe_css_value(value: str) -> bool:
    return bool(value.strip()) and len(value) <= 200 and not _UNSAFE_CSS.search(value)


def clean_theme(theme: DeckTheme) -> DeckTheme:
    """`theme` with any colour/font value that is not a plain CSS value replaced by the default."""
    defaults = DeckTheme()
    fixes = {name: getattr(defaults, name) for name in CSS_THEME_FIELDS if not is_safe_css_value(getattr(theme, name))}
    return theme.model_copy(update=fixes) if fixes else theme


def tidy_outline(outline: DeckOutline, slide_count: int | None) -> DeckOutline:
    """Enforce the requested slide count, distinctive keywords and a safe theme.

    Raises `ValueError` when the outline has no slides.
    """
    slides = outline.slides[: slide_count or MAX_SLIDES]
    if not slides:
        raise ValueError("The deck outline has no slides.")
    keywords = distinct_keywords([slide.keywords for slide in slides])
    slides = [
        slide.model_copy(update={"title": slide.title.strip(), "keywords": kws})
        for slide, kws in zip(slides, keywords, strict=True)
    ]
    return outline.model_copy(
        update={"title": outline.title.strip(), "slides": slides, "theme": clean_theme(outline.theme)}
    )


async def draft_outline(ctx: AppContext, brief: PresentationBrief, material: str) -> DeckOutline:
    """Plan the deck (story, theme, per-slide outline) from the brief and the source material."""
    outline = await ctx.llm.parse(
        purpose="deck_outline",
        system=prompts.OUTLINE_SYSTEM,
        messages=[{"role": "user", "content": prompts.outline_request(brief, material)}],
        output_model=DeckOutline,
        tier="orchestrator",
        max_tokens=OUTLINE_MAX_TOKENS,
    )
    return tidy_outline(outline, brief.slide_count)
