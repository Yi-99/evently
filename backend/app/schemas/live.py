"""Live session settings and response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class SessionSettings(BaseModel):
    """Stored as `LiveSession.settings`."""

    # How spoken words may move the deck:
    #   off      - only manual control
    #   commands - "next slide", "previous slide", "go to slide five"
    #   keywords - commands + the next slide's trigger keywords
    #   script   - keywords + alignment of speech against the speaker notes
    auto_advance: Literal["off", "commands", "keywords", "script"] = "keywords"
    stt_provider: str = "auto"  # auto | deepgram | assemblyai | openai | browser | mock
    language: str = "en"
    advance_cooldown_ms: int = Field(default=2500, ge=0)
    audio_broadcast: bool = True  # let listeners receive the speaker's audio
    show_transcript_to_listeners: bool = True


class LiveSessionOut(ORMModel):
    id: str
    event_id: str
    presentation_id: str | None
    host_user_id: str
    title: str
    join_code: str
    status: str
    current_slide_index: int
    settings: dict[str, Any]
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None


class ParticipantOut(ORMModel):
    id: str
    session_id: str
    display_name: str
    role: str
    connected: bool
    joined_at: datetime
    left_at: datetime | None


class TranscriptSegmentOut(ORMModel):
    id: str
    session_id: str
    seq: int
    text: str
    start_ms: int | None
    end_ms: int | None
    confidence: float | None
    speaker: str | None
    slide_index: int | None
    source: str
    created_at: datetime
