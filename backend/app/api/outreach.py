"""REST routes for emails and the Luma event (mounted under /api/v1).

All routes are owner-only. Sending and publishing are the explicit confirmation step:
nothing leaves the system until `POST .../send` or `POST .../luma/publish` is called.
Error mapping: unknown ids → 404, wrong status (editing a sent email, inviting before
publishing) → 409, provider not configured → 503. A provider *failure* is not an HTTP
error: the record comes back with status "failed" and `error` set.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.core.deps import Ctx, CurrentUser, OwnedEvent, load_owned_event
from app.integrations import outreach
from app.integrations.errors import OutreachError, OutreachStateError
from app.models import EmailMessage
from app.schemas.outreach import EmailDraftIn, EmailKind, EmailMessageOut, LumaEventDraft, LumaEventOut

router = APIRouter(tags=["outreach"])


class EmailPatch(BaseModel):
    """Fields of `EmailDraftIn`, all optional; omitted fields are kept."""

    model_config = ConfigDict(extra="forbid")

    kind: EmailKind | None = None
    to: list[EmailStr] | None = Field(default=None, min_length=1)
    cc: list[EmailStr] | None = None
    reply_to: EmailStr | None = None
    subject: str | None = Field(default=None, min_length=1, max_length=500)
    body_text: str | None = Field(default=None, min_length=1)
    body_html: str | None = None
    related_card_id: str | None = None


class LumaDraftRequest(BaseModel):
    venue_card_id: str | None = None


class LumaInviteRequest(BaseModel):
    emails: list[EmailStr] = Field(min_length=1, max_length=500)


def _not_configured(exc: OutreachError) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))


def _conflict(exc: OutreachStateError) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


# --- emails ----------------------------------------------------------------------


async def _owned_email(ctx: Ctx, email_id: str, user: CurrentUser) -> EmailMessage:
    row = await outreach.get_email(ctx, email_id)
    if row is None or await load_owned_event(ctx, row.event_id, user) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Email not found")
    return row


@router.get("/events/{event_id}/emails", response_model=list[EmailMessageOut])
async def list_emails(event: OwnedEvent, ctx: Ctx):
    return await outreach.list_emails(ctx, event.id)


@router.post("/events/{event_id}/emails", response_model=EmailMessageOut, status_code=status.HTTP_201_CREATED)
async def create_email(event: OwnedEvent, body: EmailDraftIn, ctx: Ctx):
    """Store a draft. Nothing is sent until `POST /emails/{id}/send`."""
    return await outreach.draft_email(ctx, event_id=event.id, draft=body, created_by="user")


@router.get("/emails/{email_id}", response_model=EmailMessageOut)
async def get_email(email_id: str, ctx: Ctx, user: CurrentUser):
    return await _owned_email(ctx, email_id, user)


@router.patch("/emails/{email_id}", response_model=EmailMessageOut)
async def patch_email(email_id: str, body: EmailPatch, ctx: Ctx, user: CurrentUser):
    """Edit a draft or a failed email (which becomes a draft again, error cleared); 409 while sending or once sent."""
    await _owned_email(ctx, email_id, user)
    try:
        return await outreach.update_email(ctx, email_id, changes=body.model_dump(exclude_unset=True))
    except OutreachStateError as exc:
        raise _conflict(exc) from exc


@router.delete("/emails/{email_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_email(email_id: str, ctx: Ctx, user: CurrentUser):
    """Delete a draft or a failed email; 409 while sending or once sent."""
    await _owned_email(ctx, email_id, user)
    try:
        await outreach.delete_email(ctx, email_id)
    except OutreachStateError as exc:
        raise _conflict(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/emails/{email_id}/send", response_model=EmailMessageOut)
async def send_email(email_id: str, ctx: Ctx, user: CurrentUser):
    """The confirmation step: send the draft. 503 when no email provider is configured."""
    await _owned_email(ctx, email_id, user)
    try:
        return await outreach.send_email(ctx, email_id)
    except OutreachError as exc:
        raise _not_configured(exc) from exc
    except OutreachStateError as exc:
        raise _conflict(exc) from exc


# --- Luma ------------------------------------------------------------------------


@router.get("/events/{event_id}/luma", response_model=LumaEventOut)
async def get_luma(event: OwnedEvent, ctx: Ctx):
    row = await outreach.get_luma_event(ctx, event.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Luma draft for this event")
    return row


@router.post("/events/{event_id}/luma/draft", response_model=LumaEventOut, status_code=status.HTTP_201_CREATED)
async def create_luma_draft(event: OwnedEvent, ctx: Ctx, body: LumaDraftRequest | None = None):
    """Pre-fill the draft from the event requirements (and a venue card) and store it. 422 without an event date."""
    venue_card_id = body.venue_card_id if body else None
    try:
        draft = await outreach.build_luma_draft_from_event(ctx, event_id=event.id, venue_card_id=venue_card_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return await outreach.draft_luma_event(ctx, event_id=event.id, draft=draft, created_by="user")


@router.put("/events/{event_id}/luma/draft", response_model=LumaEventOut)
async def replace_luma_draft(event: OwnedEvent, body: LumaEventDraft, ctx: Ctx):
    return await outreach.draft_luma_event(ctx, event_id=event.id, draft=body, created_by="user")


@router.post("/events/{event_id}/luma/publish", response_model=LumaEventOut)
async def publish_luma(event: OwnedEvent, ctx: Ctx):
    """The confirmation step: create (or update) the event on Luma. 503 when `LUMA_API_KEY` is missing."""
    if await outreach.get_luma_event(ctx, event.id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Luma draft for this event")
    try:
        return await outreach.publish_luma_event(ctx, event.id)
    except OutreachError as exc:
        raise _not_configured(exc) from exc
    except OutreachStateError as exc:
        raise _conflict(exc) from exc


@router.post("/events/{event_id}/luma/invite", response_model=LumaEventOut)
async def invite_luma(event: OwnedEvent, body: LumaInviteRequest, ctx: Ctx):
    """Have Luma send invitations to the published event. 409 before publishing, 503 without a key, 502 on failure."""
    try:
        return await outreach.invite_luma_guests(ctx, event.id, [str(e) for e in body.emails])
    except OutreachStateError as exc:
        raise _conflict(exc) from exc
    except OutreachError as exc:
        if not ctx.settings.luma_api_key:
            raise _not_configured(exc) from exc
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
