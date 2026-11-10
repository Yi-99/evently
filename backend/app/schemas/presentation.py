"""Deck brief, outline and response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class PresentationBrief(BaseModel):
    """What the planner asked for."""

    prompt: str = Field(description="What the presentation is for and what it should cover.")
    title: str | None = None
    audience: str | None = None
    tone: str | None = Field(default=None, description="e.g. 'warm and playful', 'executive'.")
    slide_count: int | None = Field(default=None, ge=1, le=40)
    style: str | None = Field(default=None, description="Visual direction: colours, mood, brand notes.")
    card_ids: list[str] = Field(default_factory=list, description="Board cards whose data should feed the deck.")


class DeckTheme(BaseModel):
    """Design tokens every slide in a deck shares."""

    name: str = "default"
    background: str = "#0f172a"
    surface: str = "#1e293b"
    text: str = "#f8fafc"
    muted: str = "#94a3b8"
    accent: str = "#38bdf8"
    accent_2: str = "#f472b6"
    heading_font: str = "Georgia, 'Times New Roman', serif"
    body_font: str = "system-ui, -apple-system, 'Segoe UI', sans-serif"
    style_notes: str = ""  # free-text art direction handed to every slide renderer


class SlideOutline(BaseModel):
    title: str
    purpose: str = Field(description="What this slide must get across.")
    key_points: list[str] = Field(default_factory=list)
    visual: str = Field(default="", description="The visual idea: chart, diagram, photo treatment, big number…")
    notes: str = Field(default="", description="Speaker script for this slide, as it would be spoken.")
    keywords: list[str] = Field(
        default_factory=list,
        description="2-4 distinctive spoken phrases that signal the speaker has moved on to this slide.",
    )


class DeckOutline(BaseModel):
    title: str
    theme: DeckTheme = Field(default_factory=DeckTheme)
    slides: list[SlideOutline]


class SlideOut(ORMModel):
    id: str
    presentation_id: str
    position: int
    title: str
    html: str
    notes: str
    keywords: list[str]
    outline: dict[str, Any]
    status: str
    version: int
    updated_at: datetime


class PresentationOut(ORMModel):
    id: str
    event_id: str
    title: str
    brief: dict[str, Any]
    theme: dict[str, Any]
    status: str
    agent_run_id: str | None
    version: int
    created_at: datetime
    updated_at: datetime
