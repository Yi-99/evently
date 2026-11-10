"""Outreach entrypoints: email drafts/sending and the Luma event (public contract).

The chat orchestrator and the REST API call only these functions. Drafting never
sends anything; `send_email` and `publish_luma_event` are the two outward-facing
actions and are only ever reached from an explicit user confirmation (a REST call
or a chat confirmation the orchestrator relays).

Every email and the Luma event are mirrored as board cards (kind "email" / "luma")
whose `data` is the corresponding Out model, so drafts and their status are visible
on the whiteboard. Card status follows the record: draft/sent/published → "ready",
sending/publishing → "running", failed → "error".
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.ids import utcnow
from app.integrations.email_providers import EmailProvider, OutgoingEmail, build_email_provider, sender_address
from app.integrations.errors import OutreachError, OutreachStateError
from app.integrations.luma import LumaClient, draft_to_payload
from app.models import Card, EmailMessage, Event, LumaEvent
from app.realtime import events as ev
from app.schemas.common import dump
from app.schemas.outreach import EmailDraftIn, EmailMessageOut, LumaEventDraft, LumaEventOut
from app.schemas.venue import PlaceSummary
from app.services import board

if TYPE_CHECKING:
    from app.core.context import AppContext

__all__ = [
    "OutreachError",
    "OutreachStateError",
    "build_luma_draft_from_event",
    "delete_email",
    "draft_email",
    "draft_luma_event",
    "get_email",
    "get_luma_event",
    "invite_luma_guests",
    "list_emails",
    "publish_luma_event",
    "send_email",
    "update_email",
]

log = logging.getLogger(__name__)

EMAIL_PROVIDER_SERVICE = "outreach_email_provider"
LUMA_CLIENT_SERVICE = "outreach_luma_client"
TIMEZONE_RESOLVER_SERVICE = "outreach_timezone_resolver"

DEFAULT_START_TIME = time(9, 0)  # when requirements have a date but no start time
_CARD_STATUS = {"draft": "ready", "sent": "ready", "published": "ready", "sending": "running", "publishing": "running"}


def _card_status(status: str) -> str:
    return _CARD_STATUS.get(status, "error")


# --- shared infrastructure (one per app; tests pre-seed `ctx.service(KEY, lambda: fake)`) ------------


def email_provider(ctx: AppContext) -> EmailProvider:
    """The configured provider; raises `OutreachError` when none can be built."""

    def factory() -> EmailProvider:
        provider = build_email_provider(ctx.settings)
        aclose = getattr(provider, "aclose", None)
        if aclose is not None:
            ctx.on_shutdown(aclose)
        return provider

    return ctx.service(EMAIL_PROVIDER_SERVICE, factory)


def luma_client(ctx: AppContext) -> LumaClient:
    """The Luma client; raises `OutreachError` when `LUMA_API_KEY` is missing."""
    if not ctx.settings.luma_api_key:
        raise OutreachError("Luma is not configured: set LUMA_API_KEY (a calendar API key; needs Luma Plus)")

    def factory() -> LumaClient:
        client = LumaClient(ctx.settings.luma_api_key)
        ctx.on_shutdown(client.aclose)
        return client

    return ctx.service(LUMA_CLIENT_SERVICE, factory)


# --- emails ----------------------------------------------------------------------


def email_out(row: EmailMessage) -> dict[str, Any]:
    return dump(EmailMessageOut, row)


async def list_emails(ctx: AppContext, event_id: str) -> list[EmailMessage]:
    async with ctx.db() as session:
        rows = await session.execute(
            select(EmailMessage).where(EmailMessage.event_id == event_id).order_by(EmailMessage.created_at)
        )
        return list(rows.scalars())


async def get_email(ctx: AppContext, email_id: str) -> EmailMessage | None:
    async with ctx.db() as session:
        return await session.get(EmailMessage, email_id)


async def _publish_email(ctx: AppContext, row: EmailMessage) -> None:
    """Publish `email.updated` and mirror the row onto its board card (if it still exists)."""
    out = email_out(row)
    if row.card_id:
        try:
            await board.patch_card(
                ctx, row.card_id, by="system", title=row.subject, status=_card_status(row.status), data_patch=out
            )
        except board.CardNotFound:
            pass  # the planner removed the card from the board; the email itself stays
    await ctx.bus.publish(ev.event_topic(row.event_id), ev.EMAIL_UPDATED, {"email": out})


async def draft_email(ctx: AppContext, *, event_id: str, draft: EmailDraftIn, created_by: str = "user") -> EmailMessage:
    """Store a draft (status "draft"), put an `email` card on the board, publish `email.updated`."""
    row = EmailMessage(
        event_id=event_id,
        kind=draft.kind,
        to=list(draft.to),
        cc=list(draft.cc),
        reply_to=draft.reply_to,
        subject=draft.subject,
        body_text=draft.body_text,
        body_html=draft.body_html,
        related_card_id=draft.related_card_id,
        created_by=created_by,
    )
    async with ctx.db() as session:
        session.add(row)
        await session.commit()
    card = await board.create_card(
        ctx,
        event_id=event_id,
        kind="email",
        title=draft.subject,
        data=email_out(row),
        created_by="user" if created_by == "user" else "agent",
    )
    async with ctx.db() as session:
        row = await session.get(EmailMessage, row.id)
        row.card_id = card.id
        await session.commit()
    await _publish_email(ctx, row)
    return row


#: Emails in these states may still be edited or deleted; "sending" and "sent" are protected.
EDITABLE_EMAIL_STATUSES = ("draft", "failed")


async def update_email(ctx: AppContext, email_id: str, *, changes: dict) -> EmailMessage:
    """Edit a draft's recipients/subject/body; publishes `email.updated`.

    Allowed while the status is "draft" or "failed"; editing a failed email (e.g. fixing a bad
    recipient) turns it back into a draft and clears its error. `changes` holds `EmailDraftIn`
    keys; the merged result is validated as a whole, so a bad address or an empty subject raises
    `pydantic.ValidationError`. Raises `LookupError` for an unknown id and `OutreachStateError`
    while the message is sending or once it has been sent.
    """
    unknown = set(changes) - set(EmailDraftIn.model_fields)
    if unknown:
        raise ValueError(f"unknown email fields: {', '.join(sorted(unknown))}")
    async with ctx.db() as session:
        row = await session.get(EmailMessage, email_id)
        if row is None:
            raise LookupError(f"email {email_id} not found")
        if row.status not in EDITABLE_EMAIL_STATUSES:
            raise OutreachStateError(f"email {email_id} is {row.status}; only drafts and failed emails can be edited")
        current = {name: getattr(row, name) for name in EmailDraftIn.model_fields}
        merged = EmailDraftIn.model_validate({**current, **changes})
        for name in EmailDraftIn.model_fields:
            value = getattr(merged, name)
            setattr(row, name, list(value) if isinstance(value, list) else value)
        if row.status == "failed":
            row.status = "draft"
            row.error = None
        await session.commit()
    await _publish_email(ctx, row)
    return row


async def delete_email(ctx: AppContext, email_id: str) -> None:
    """Delete a draft or a failed email, and its board card; publishes `email.deleted`.

    Raises `LookupError` for an unknown id and `OutreachStateError` while the message is sending
    or once it has been sent (sent messages are kept as a record of what went out).
    """
    async with ctx.db() as session:
        row = await session.get(EmailMessage, email_id)
        if row is None:
            raise LookupError(f"email {email_id} not found")
        if row.status not in EDITABLE_EMAIL_STATUSES:
            raise OutreachStateError(f"email {email_id} is {row.status}; only drafts and failed emails can be deleted")
        await session.delete(row)
        await session.commit()
    if row.card_id:
        await board.delete_card(ctx, row.card_id)
    await ctx.bus.publish(ev.event_topic(row.event_id), ev.EMAIL_DELETED, {"email_id": email_id})


async def _set_email_status(ctx: AppContext, email_id: str, **fields: Any) -> EmailMessage:
    async with ctx.db() as session:
        row = await session.get(EmailMessage, email_id)
        for name, value in fields.items():
            setattr(row, name, value)
        await session.commit()
    await _publish_email(ctx, row)
    return row


async def send_email(ctx: AppContext, email_id: str) -> EmailMessage:
    """Send a draft through the configured provider. status: sending → sent | failed; publishes `email.updated`.

    Raises `OutreachError` when no provider is configured. Idempotent: a message already sent is returned as is.
    A message that is mid-send raises `OutreachStateError`; a failed one is retried.
    """
    row = await get_email(ctx, email_id)
    if row is None:
        raise LookupError(f"email {email_id} not found")
    if row.status == "sent":
        return row
    if row.status not in ("draft", "failed"):
        raise OutreachStateError(f"email {email_id} is {row.status}; only drafts can be sent")
    provider = email_provider(ctx)  # before the status flip, so a config problem leaves the draft untouched

    message = OutgoingEmail(
        sender=sender_address(ctx.settings, provider),
        to=list(row.to),
        cc=list(row.cc),
        reply_to=row.reply_to,
        subject=row.subject,
        body_text=row.body_text,
        body_html=row.body_html,
        idempotency_key=row.id,
    )
    await _set_email_status(ctx, email_id, status="sending", provider=provider.name, error=None)
    try:
        provider_message_id = await provider.send(message)
    except OutreachError as exc:
        log.warning("email %s failed via %s: %s", email_id, provider.name, exc)
        return await _set_email_status(ctx, email_id, status="failed", error=str(exc))
    return await _set_email_status(
        ctx, email_id, status="sent", provider_message_id=provider_message_id, error=None, sent_at=utcnow()
    )


# --- Luma ------------------------------------------------------------------------


def luma_out(row: LumaEvent) -> dict[str, Any]:
    return dump(LumaEventOut, row)


async def get_luma_event(ctx: AppContext, event_id: str) -> LumaEvent | None:
    async with ctx.db() as session:
        return (await session.execute(select(LumaEvent).where(LumaEvent.event_id == event_id))).scalar_one_or_none()


async def _publish_luma(ctx: AppContext, row: LumaEvent) -> None:
    out = luma_out(row)
    if row.card_id:
        try:
            await board.patch_card(
                ctx,
                row.card_id,
                by="system",
                title=str(row.draft.get("name") or "Luma event"),
                status=_card_status(row.status),
                data_patch=out,
            )
        except board.CardNotFound:
            pass
    await ctx.bus.publish(ev.event_topic(row.event_id), ev.LUMA_UPDATED, {"luma": out})


async def draft_luma_event(
    ctx: AppContext, *, event_id: str, draft: LumaEventDraft, created_by: str = "user"
) -> LumaEvent:
    """Create or replace the event's Luma draft (one per event), add a `luma` board card, publish `luma.updated`.

    Replacing the draft of an already published event keeps `luma_event_id`/`url`; the next
    `publish_luma_event` pushes the new draft as an update.
    """
    draft_json = draft.model_dump(mode="json")
    async with ctx.db() as session:
        row = (await session.execute(select(LumaEvent).where(LumaEvent.event_id == event_id))).scalar_one_or_none()
        if row is None:
            row = LumaEvent(event_id=event_id, draft=draft_json)
            session.add(row)
        else:
            row.draft = draft_json
            if row.status == "failed":
                row.status = "draft"
                row.error = None
        await session.commit()

    card = await board.get_card(ctx, row.card_id) if row.card_id else None
    if card is None:
        card = await board.create_card(
            ctx,
            event_id=event_id,
            kind="luma",
            title=draft.name,
            data=luma_out(row),
            created_by="user" if created_by == "user" else "agent",
        )
        async with ctx.db() as session:
            row = await session.get(LumaEvent, row.id)
            row.card_id = card.id
            await session.commit()
    await _publish_luma(ctx, row)
    return row


async def _set_luma(ctx: AppContext, luma_id: str, **fields: Any) -> LumaEvent:
    async with ctx.db() as session:
        row = await session.get(LumaEvent, luma_id)
        for name, value in fields.items():
            setattr(row, name, value)
        await session.commit()
    await _publish_luma(ctx, row)
    return row


async def publish_luma_event(ctx: AppContext, event_id: str) -> LumaEvent:
    """Create the event on Luma from the stored draft (or update it when already published).

    status: publishing → published (with `luma_event_id` and `url`) | failed; publishes `luma.updated`.
    Raises `OutreachError` when `LUMA_API_KEY` is missing or there is no draft.
    """
    row = await get_luma_event(ctx, event_id)
    if row is None:
        raise OutreachError("There is no Luma draft for this event yet; draft one first")
    if row.status == "publishing":
        raise OutreachStateError("The Luma event is already being published")
    client = luma_client(ctx)
    draft = LumaEventDraft.model_validate(row.draft)
    luma_event_id = row.luma_event_id

    await _set_luma(ctx, row.id, status="publishing", error=None)
    try:
        if luma_event_id:
            await client.update_event(luma_event_id, draft_to_payload(draft, for_update=True))
        else:
            luma_event_id = await client.create_event(draft_to_payload(draft))
        remote = await client.get_event(luma_event_id)
    except OutreachError as exc:
        log.warning("luma publish for event %s failed: %s", event_id, exc)
        # Keep the id when the create succeeded but the follow-up read failed, so a retry updates instead.
        return await _set_luma(ctx, row.id, status="failed", error=str(exc), luma_event_id=luma_event_id)
    return await _set_luma(
        ctx,
        row.id,
        status="published",
        error=None,
        luma_event_id=luma_event_id,
        url=remote.get("url") or row.url or f"https://lu.ma/{luma_event_id}",
        last_synced_at=utcnow(),
    )


async def invite_luma_guests(ctx: AppContext, event_id: str, emails: list[str]) -> LumaEvent:
    """Add guests to the published Luma event and have Luma send the invites; updates `guests_invited`.

    Uses Luma's send-invites endpoint (guests get an invitation to accept). Addresses Luma skips
    (unsubscribed/blocked) are not counted. Raises `OutreachStateError` when the event is not published.
    """
    row = await get_luma_event(ctx, event_id)
    if row is None or row.status != "published" or not row.luma_event_id:
        raise OutreachStateError("Publish the Luma event before inviting guests")
    if not emails:
        return row
    client = luma_client(ctx)
    skipped = await client.send_invites(row.luma_event_id, emails)
    invited = len(emails) - len(skipped)
    if skipped:
        log.info("luma skipped %d invite(s) for event %s: %s", len(skipped), event_id, skipped)
    return await _set_luma(ctx, row.id, guests_invited=row.guests_invited + invited, last_synced_at=utcnow())


# --- draft pre-fill ----------------------------------------------------------------


def _timezone_resolver(ctx: AppContext) -> Any | None:
    """The traffic package's coordinate → timezone lookup, when present (None → UTC fallback)."""
    try:
        from app.providers.geo import TimezoneResolver
    except ImportError:  # pragma: no cover - optional reuse of a sibling package
        return None
    return ctx.service(TIMEZONE_RESOLVER_SERVICE, lambda: TimezoneResolver(online=ctx.settings.environment != "test"))


async def _venue_timezone(ctx: AppContext, lat: float | None, lng: float | None) -> tuple[tzinfo, str]:
    """(zone for building instants, IANA name for Luma). Without a resolvable IANA zone the name is "UTC".

    A fixed-offset zone (longitude estimate) still gives a sensible instant, but Luma needs an IANA name,
    so the draft then says UTC and the planner can correct it.
    """
    resolver = _timezone_resolver(ctx)
    if resolver is None or lat is None or lng is None:
        return UTC, "UTC"
    from app.providers.base import GeoPoint

    zone = await resolver.resolve(GeoPoint(lat=lat, lng=lng))
    if isinstance(zone, ZoneInfo):
        return zone, zone.key
    return zone, "UTC"


def _parse(value: Any, kind: type[date] | type[time]) -> date | time | None:
    if value is None or value == "":
        return None
    if isinstance(value, kind):
        return value
    try:
        return kind.fromisoformat(str(value))
    except ValueError:
        return None


async def build_luma_draft_from_event(
    ctx: AppContext, *, event_id: str, venue_card_id: str | None = None
) -> LumaEventDraft:
    """Pre-fill a Luma draft from `Event.requirements` (date, times, headcount, description) and a venue card.

    Raises `LookupError` for an unknown event or venue card, and `ValueError` with a message the
    orchestrator can relay when the requirements have no `event_date`. Without a `start_time` the
    draft starts at 09:00 local; an `end_time` before the start is taken as the next day.
    """
    async with ctx.db() as session:
        event = await session.get(Event, event_id)
        if event is None:
            raise LookupError(f"event {event_id} not found")
        card = await session.get(Card, venue_card_id) if venue_card_id else None
    if venue_card_id and (card is None or card.event_id != event_id or card.kind != "venue"):
        raise LookupError(f"venue card {venue_card_id} not found on this event")

    requirements = event.requirements or {}
    event_date = _parse(requirements.get("event_date"), date)
    if event_date is None:
        raise ValueError("Set the event date first (the requirements have no event_date) before drafting on Luma")
    start_time = _parse(requirements.get("start_time"), time) or DEFAULT_START_TIME
    end_time = _parse(requirements.get("end_time"), time)

    place = PlaceSummary.model_validate(card.data["place"]) if card is not None and card.data.get("place") else None
    zone, tz_name = await _venue_timezone(ctx, place.lat if place else None, place.lng if place else None)

    start_at = datetime.combine(event_date, start_time, tzinfo=zone)
    end_at = None
    if end_time is not None:
        end_at = datetime.combine(event_date, end_time, tzinfo=zone)
        if end_at <= start_at:
            end_at += timedelta(days=1)

    headcount = requirements.get("headcount")
    return LumaEventDraft(
        name=event.name,
        description_md=event.description or "",
        start_at=start_at,
        end_at=end_at,
        timezone=tz_name,
        venue_name=place.name if place else None,
        address=place.address if place else None,
        lat=place.lat if place else None,
        lng=place.lng if place else None,
        capacity=int(headcount) if isinstance(headcount, int | float) and headcount >= 1 else None,
        venue_card_id=card.id if card else None,
    )
