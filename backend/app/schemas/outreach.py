"""Email drafts and the Luma event draft (shared by the outreach package and the orchestrator)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import ORMModel

EmailKind = Literal["venue_inquiry", "invite", "update", "custom"]


class EmailDraftIn(BaseModel):
    """What an agent or the planner proposes to send. Nothing is sent until confirmed."""

    kind: EmailKind = "custom"
    to: list[EmailStr] = Field(min_length=1)
    cc: list[EmailStr] = Field(default_factory=list)
    reply_to: EmailStr | None = None
    subject: str = Field(min_length=1, max_length=500)
    body_text: str = Field(min_length=1)
    body_html: str | None = None
    related_card_id: str | None = Field(default=None, description="e.g. the venue card this inquiry is about")


class LumaEventDraft(BaseModel):
    """Fields Luma needs to create an event; pre-filled from the event requirements and a venue card."""

    name: str = Field(min_length=1, max_length=200)
    description_md: str = ""
    start_at: datetime
    end_at: datetime | None = None
    timezone: str = Field(default="UTC", description="IANA timezone of the venue, e.g. America/Chicago")
    venue_name: str | None = None
    address: str | None = None
    lat: float | None = None
    lng: float | None = None
    visibility: Literal["public", "private"] = "private"
    require_rsvp_approval: bool = False
    capacity: int | None = Field(default=None, ge=1)
    venue_card_id: str | None = None


class EmailMessageOut(ORMModel):
    id: str
    event_id: str
    kind: str
    to: list[str]
    cc: list[str]
    reply_to: str | None
    subject: str
    body_text: str
    body_html: str | None
    status: str
    provider: str | None
    provider_message_id: str | None
    error: str | None
    card_id: str | None
    related_card_id: str | None
    created_by: str
    meta: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None


class LumaEventOut(ORMModel):
    id: str
    event_id: str
    status: str
    draft: dict[str, Any]
    luma_event_id: str | None
    url: str | None
    error: str | None
    card_id: str | None
    guests_invited: int
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime
