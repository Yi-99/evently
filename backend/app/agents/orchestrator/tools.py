"""The orchestrator's tools: thin wrappers that validate ids and hand work to other packages.

Every launching tool returns within milliseconds with ids; the work itself runs
as `AgentRun`s and shows up on the board. Tool handlers may run concurrently
within one turn (the model can call several tools at once).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, EmailStr, Field, ValidationError
from sqlalchemy import select

from app.agents import presentations, venues
from app.agents.orchestrator import followup
from app.integrations import outreach
from app.llm.base import Tool, ToolError
from app.models import Card, EmailMessage, Event, LumaEvent, Presentation, Slide
from app.realtime import events as ev
from app.schemas.common import EventOut, dump
from app.schemas.outreach import EmailDraftIn, LumaEventDraft
from app.schemas.presentation import PresentationBrief
from app.schemas.venue import VenueCriteria
from app.search import service as search_service
from app.search.client import SearchUnavailable, get_search_client
from app.services import board

if TYPE_CHECKING:
    from app.core.context import AppContext

SEARCH_VENUES = "search_venues"
REFINE_VENUE_CARD = "refine_venue_card"
UPDATE_REQUIREMENTS = "update_requirements"
GET_CARD = "get_card"
ADD_NOTE = "add_note"
REMOVE_CARD = "remove_card"
CREATE_COMPARISON = "create_comparison"
CREATE_PRESENTATION = "create_presentation"
EDIT_PRESENTATION = "edit_presentation"
DRAFT_EMAIL = "draft_email"
SEND_EMAIL = "send_email"
DRAFT_LUMA_EVENT = "draft_luma_event"
PUBLISH_LUMA_EVENT = "publish_luma_event"
INVITE_LUMA_GUESTS = "invite_luma_guests"
SEARCH_VENUE_MEMORY = "search_venue_memory"
SEARCH_EVENT_HISTORY = "search_event_history"

BACKGROUND_NOTE = "Running in the background; progress and results appear on the board, not in this turn."


# -- inputs -------------------------------------------------------------------


class SearchVenuesIn(VenueCriteria):
    """Criteria for this search. Fields you leave out keep the value from the event's saved requirements."""


class RequirementsPatch(VenueCriteria):
    """Only the fields you pass are changed. Pass null to clear a field."""

    location: str | None = Field(default=None, description="City, neighbourhood or address to search around.")


class CardRef(BaseModel):
    card_id: str


class RefineIn(BaseModel):
    card_id: str = Field(description="A venue card on this event's board.")
    instruction: str = Field(description="What to find out or change, in the planner's words plus needed context.")


class NoteIn(BaseModel):
    title: str = Field(max_length=300)
    text: str = Field(description="The note body; short paragraphs or a list.")


class ComparisonRow(BaseModel):
    criterion: str = Field(description="e.g. Capacity, Parking, Weather risk, Travel time, Price.")
    values: list[str] = Field(description="One entry per card in card_ids, same order; 'unknown' when not researched.")


class ComparisonIn(BaseModel):
    title: str = Field(max_length=300)
    card_ids: list[str] = Field(min_length=2, max_length=6, description="Venue cards to compare, in column order.")
    rows: list[ComparisonRow] = Field(min_length=1)
    summary: str = Field(description="The takeaway in one to three sentences.")
    recommended_card_id: str | None = Field(default=None, description="The venue you would pick, if any.")


class EditPresentationIn(BaseModel):
    presentation_id: str
    instruction: str = Field(description="The change to make, in the planner's words plus needed context.")
    slide_id: str | None = Field(default=None, description="Limit the edit to this slide of the deck.")


class EmailRef(BaseModel):
    email_id: str = Field(description="An email draft of this event (listed in the workspace block).")


class LumaOverrides(BaseModel):
    """Values that replace what was pre-filled from the event and venue."""

    name: str | None = Field(default=None, max_length=200)
    description_md: str | None = None
    visibility: Literal["public", "private"] | None = None
    require_rsvp_approval: bool | None = None


class DraftLumaIn(BaseModel):
    venue_card_id: str | None = Field(default=None, description="Venue card whose name and address to use.")
    overrides: LumaOverrides | None = None


class NoArgs(BaseModel):
    pass


class InviteGuestsIn(BaseModel):
    emails: list[EmailStr] = Field(min_length=1, description="Guests to invite; Luma sends the invitations.")


class LatLng(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class VenueMemoryIn(BaseModel):
    query: str = Field(min_length=1, description="What to look for: a venue name, a kind of place, an area.")
    near: LatLng | None = Field(default=None, description="Rank and filter by distance from this point.")
    radius_km: float | None = Field(default=None, gt=0, description="Only with near: maximum distance.")
    min_capacity: int | None = Field(default=None, ge=1, description="Minimum guest capacity.")
    limit: int = Field(default=5, ge=1, le=10)


class EventHistoryIn(BaseModel):
    query: str = Field(min_length=1, description="Words or a phrase to look for.")
    types: list[Literal["transcript", "message", "slide"]] | None = Field(
        default=None, description="Limit to talk transcripts, chat messages or slides; all three when omitted."
    )
    limit: int = Field(default=8, ge=1, le=20)


# -- summaries shown in the chat UI -------------------------------------------


def describe_call(name: str, tool_input: dict[str, Any], titles: dict[str, str]) -> str:
    """Short human-readable label for a tool call, for `chat.tool` envelopes."""

    def named(key: str, fallback: str) -> str:
        title = titles.get(str(tool_input.get(key) or ""))
        return f"“{title}”" if title else fallback

    match name:
        case "search_venues":
            label = f"Venue search near {tool_input.get('location') or '…'}"
            return label + (f" for {tool_input['headcount']} guests" if tool_input.get("headcount") else "")
        case "refine_venue_card":
            return f"Refine {named('card_id', 'a venue card')}"
        case "update_requirements":
            return "Update event requirements"
        case "get_card":
            return f"Read {named('card_id', 'a card')}"
        case "add_note":
            return f"Add note “{tool_input.get('title') or 'Note'}”"
        case "remove_card":
            return f"Remove {named('card_id', 'a card')}"
        case "create_comparison":
            return f"Compare {len(tool_input.get('card_ids') or [])} venues"
        case "create_presentation":
            return f"Build presentation “{tool_input['title']}”" if tool_input.get("title") else "Build a presentation"
        case "edit_presentation":
            if tool_input.get("slide_id"):
                return f"Edit slide {named('slide_id', '')}".rstrip()
            return f"Edit {named('presentation_id', 'the presentation')}"
        case "draft_email":
            to = tool_input.get("to") or []
            return f"Draft email to {to[0]}" + (f" (+{len(to) - 1})" if len(to) > 1 else "") if to else "Draft email"
        case "send_email":
            return f"Send email {named('email_id', '')}".rstrip()
        case "draft_luma_event":
            return "Draft the Luma event"
        case "publish_luma_event":
            return "Publish the event on Luma"
        case "invite_luma_guests":
            return f"Invite {len(tool_input.get('emails') or [])} guests on Luma"
        case "search_venue_memory":
            return f"Look up earlier venues: {str(tool_input.get('query') or '…')[:60]}"
        case "search_event_history":
            return f"Search event history: {str(tool_input.get('query') or '…')[:60]}"
    return name.replace("_", " ").capitalize()


# -- handlers -----------------------------------------------------------------


class Toolbox:
    """Tool handlers for one orchestrator turn on one event."""

    def __init__(self, ctx: AppContext, *, event_id: str, run_id: str) -> None:
        self.ctx = ctx
        self.event_id = event_id
        self.run_id = run_id  # parent of every run launched by this turn
        self.launched_run_ids: list[str] = []
        self._requirements_lock = asyncio.Lock()  # read-modify-write of Event.requirements

    def tools(self) -> list[Tool]:
        return [
            Tool(
                SEARCH_VENUES,
                "Start a venue search for this event. A scout agent finds candidate venues around the location, "
                "then one research agent per venue checks capacity for the headcount, parking, weather on the date, "
                "rush-hour travel and pricing. Each venue appears on the board as a card that fills in live, followed "
                "by a ranked summary card. Returns immediately with a run id; the results are not available in this "
                "turn. The criteria used are saved as the event's requirements. Use it when the planner wants venue "
                "options; to dig into one venue that is already on the board, use refine_venue_card instead.",
                SearchVenuesIn,
                self.search_venues,
            ),
            Tool(
                REFINE_VENUE_CARD,
                "Re-run research on one venue card with an instruction, e.g. 'check whether they allow outside "
                "catering' or 'recompute travel time from downtown'. The card updates in place in the background; "
                "fields the planner edited by hand are preserved. Returns immediately with a run id.",
                RefineIn,
                self.refine_venue_card,
            ),
            Tool(
                UPDATE_REQUIREMENTS,
                "Save what the planner has told you about the event (headcount, date, budget, must-haves...) so "
                "later searches and decks use it. Returns the full saved requirements.",
                RequirementsPatch,
                self.update_requirements,
            ),
            Tool(
                GET_CARD,
                "Read one board card in full, including all research data on a venue card. Use it to answer "
                "questions about a venue or before writing a comparison; several cards can be read in one turn.",
                CardRef,
                self.get_card,
            ),
            Tool(
                ADD_NOTE,
                "Put a note card on the board: a decision, a to-do list, caterer ideas, anything the planner wants "
                "to keep next to the venues.",
                NoteIn,
                self.add_note,
            ),
            Tool(
                REMOVE_CARD,
                "Delete a card from the board for everyone. Any research still running on it is stopped.",
                CardRef,
                self.remove_card,
            ),
            Tool(
                CREATE_COMPARISON,
                "Put a comparison card on the board that sets chosen venue cards side by side, built from your own "
                "reading of their data (read them with get_card first). Rows are criteria, columns are venues.",
                ComparisonIn,
                self.create_comparison,
            ),
            Tool(
                CREATE_PRESENTATION,
                "Create a slide deck for this event from a brief. Pass the venue (or other) cards whose data the "
                "deck should draw on. The deck is built in the background, slide by slide; returns immediately with "
                "the presentation id and run id.",
                PresentationBrief,
                self.create_presentation,
            ),
            Tool(
                EDIT_PRESENTATION,
                "Change an existing deck by instruction: restyle it, rewrite, add, remove or reorder slides. Pass "
                "slide_id to change only that slide. Runs in the background; returns immediately with a run id.",
                EditPresentationIn,
                self.edit_presentation,
            ),
            Tool(
                DRAFT_EMAIL,
                "Write an email draft for the planner: a venue inquiry (headcount, date and time, event type, "
                "must-haves, and a request for availability and pricing), an invitation (event name, date, venue), "
                "an update, or anything custom. You write the subject and body yourself. The draft appears on the "
                "board as an email card for the planner to review; nothing is sent until they confirm and you call "
                "send_email. Never invent a recipient address; ask for it if you don't have one.",
                EmailDraftIn,
                self.draft_email,
            ),
            Tool(
                SEND_EMAIL,
                "Send an existing draft to its recipients. This leaves the workspace and cannot be undone, so call "
                "it only when the planner's current message explicitly confirms sending that draft (\"yes, send "
                'it", "send the inquiry to The Foundry"). Drafting an email, or the planner asking to see it, '
                "is not confirmation. Returns the resulting status.",
                EmailRef,
                self.send_email,
            ),
            Tool(
                DRAFT_LUMA_EVENT,
                "Create or replace the event's Luma (lu.ma) event page draft, pre-filled from the saved "
                "requirements (date, times, headcount) and, if given, a venue card (name, address). Overrides "
                "replace pre-filled values. The draft appears on the board as a Luma card; nothing is published.",
                DraftLumaIn,
                self.draft_luma_event,
            ),
            Tool(
                PUBLISH_LUMA_EVENT,
                "Publish the Luma draft, creating the public event page (or updating it when already published). "
                "Call it only when the planner's current message explicitly confirms publishing. Returns the "
                "status and the page URL.",
                NoArgs,
                self.publish_luma_event,
            ),
            Tool(
                INVITE_LUMA_GUESTS,
                "Invite guests to the published Luma event; Luma emails the invitations. Only after the event is "
                "published and the planner has explicitly confirmed inviting these people.",
                InviteGuestsIn,
                self.invite_luma_guests,
            ),
            Tool(
                SEARCH_VENUE_MEMORY,
                "Look up venues researched for this planner's earlier events, with their scores, pros and cons "
                "and the headcount they were evaluated for. Use it before launching a new venue search, and when "
                "the planner asks 'have we looked at X before'. Results are prior research and may be stale. "
                "Returns immediately.",
                VenueMemoryIn,
                self.search_venue_memory,
            ),
            Tool(
                SEARCH_EVENT_HISTORY,
                "Find what was said in past talks, earlier chat or slides of this event. Returns the best "
                "matches with their type, a text snippet and the ids to link back to them.",
                EventHistoryIn,
                self.search_event_history,
            ),
        ]

    # -- lookups that double as authorisation --------------------------------

    async def _card(self, card_id: str, *, kind: str | None = None) -> Card:
        card = await board.get_card(self.ctx, card_id)
        if card is None or card.event_id != self.event_id:
            raise ToolError(f"There is no card '{card_id}' on this event's board.")
        if kind is not None and card.kind != kind:
            raise ToolError(f"Card '{card_id}' ({card.title!r}) is a {card.kind} card, not a {kind} card.")
        return card

    async def _presentation(self, presentation_id: str) -> Presentation:
        async with self.ctx.db() as session:
            deck = await session.get(Presentation, presentation_id)
        if deck is None or deck.event_id != self.event_id:
            raise ToolError(f"There is no presentation '{presentation_id}' for this event.")
        return deck

    async def _slide(self, presentation_id: str, slide_id: str) -> Slide:
        async with self.ctx.db() as session:
            slide = await session.get(Slide, slide_id)
        if slide is None or slide.presentation_id != presentation_id:
            raise ToolError(f"There is no slide '{slide_id}' in presentation '{presentation_id}'.")
        return slide

    async def _email(self, email_id: str) -> EmailMessage:
        async with self.ctx.db() as session:
            email = await session.get(EmailMessage, email_id)
        if email is None or email.event_id != self.event_id:
            raise ToolError(f"There is no email '{email_id}' for this event.")
        return email

    async def _luma_event(self) -> LumaEvent | None:
        async with self.ctx.db() as session:
            rows = await session.execute(select(LumaEvent).where(LumaEvent.event_id == self.event_id))
            return rows.scalar_one_or_none()

    async def _load_requirements(self) -> dict[str, Any]:
        async with self.ctx.db() as session:
            event = await session.get(Event, self.event_id)
        return dict(event.requirements or {}) if event is not None else {}

    async def _store_requirements(self, requirements: dict[str, Any]) -> None:
        async with self.ctx.db() as session:
            event = await session.get(Event, self.event_id)
            if event is None:
                raise ToolError("This event no longer exists.")
            event.requirements = requirements
            await session.commit()
        await self.ctx.bus.publish(ev.event_topic(event.id), ev.EVENT_UPDATED, {"event": dump(EventOut, event)})

    def _launched(self, run_id: str, *, follow_up: bool) -> None:
        self.launched_run_ids.append(run_id)
        if follow_up:
            followup.watch(self.ctx, run_id)

    # -- tools ----------------------------------------------------------------

    async def search_venues(self, criteria: SearchVenuesIn) -> dict[str, Any]:
        provided = criteria.model_dump(mode="json", exclude_unset=True)
        async with self._requirements_lock:
            saved = await self._load_requirements()
            try:
                effective = VenueCriteria.model_validate({**saved, **provided})
            except ValidationError:
                effective = VenueCriteria.model_validate(provided)  # saved requirements written elsewhere may not fit
            await self._store_requirements({**saved, **effective.model_dump(mode="json")})
        run = await venues.start_venue_search(
            self.ctx, event_id=self.event_id, criteria=effective, parent_run_id=self.run_id
        )
        self._launched(run.id, follow_up=True)
        return {
            "run_id": run.id,
            "status": "started",
            "criteria": effective.model_dump(mode="json", exclude_none=True),
            "note": BACKGROUND_NOTE,
        }

    async def refine_venue_card(self, args: RefineIn) -> dict[str, Any]:
        card = await self._card(args.card_id, kind="venue")
        run = await venues.start_card_refine(
            self.ctx, card_id=card.id, instruction=args.instruction, parent_run_id=self.run_id
        )
        self._launched(run.id, follow_up=False)
        return {"run_id": run.id, "card_id": card.id, "title": card.title, "status": "started", "note": BACKGROUND_NOTE}

    async def update_requirements(self, patch: RequirementsPatch) -> dict[str, Any]:
        changes = patch.model_dump(mode="json", exclude_unset=True)
        if not changes:
            raise ToolError("Pass at least one field to change.")
        async with self._requirements_lock:
            merged = {k: v for k, v in {**await self._load_requirements(), **changes}.items() if v is not None}
            await self._store_requirements(merged)
        return {"requirements": merged}

    async def get_card(self, ref: CardRef) -> dict[str, Any]:
        card = await self._card(ref.card_id)
        return {
            "id": card.id,
            "kind": card.kind,
            "title": card.title,
            "status": card.status,
            "data": card.data,
            "locked_fields": card.locked_fields,
        }

    async def add_note(self, note: NoteIn) -> dict[str, Any]:
        card = await board.create_card(
            self.ctx,
            event_id=self.event_id,
            kind="note",
            title=note.title,
            data={"text": note.text},
            created_by="agent",
        )
        return {"card_id": card.id, "title": card.title}

    async def remove_card(self, ref: CardRef) -> dict[str, Any]:
        card = await self._card(ref.card_id)
        if card.agent_run_id and self.ctx.runtime.is_active(card.agent_run_id):
            await self.ctx.runtime.cancel(card.agent_run_id)
        await board.delete_card(self.ctx, card.id)
        return {"removed": True, "card_id": card.id, "title": card.title}

    async def create_comparison(self, args: ComparisonIn) -> dict[str, Any]:
        if len(set(args.card_ids)) != len(args.card_ids):
            raise ToolError("card_ids contains a duplicate.")
        if args.recommended_card_id is not None and args.recommended_card_id not in args.card_ids:
            raise ToolError("recommended_card_id must be one of card_ids.")
        for row in args.rows:
            if len(row.values) != len(args.card_ids):
                raise ToolError(f"Row {row.criterion!r} has {len(row.values)} values for {len(args.card_ids)} cards.")
        cards = [await self._card(card_id, kind="venue") for card_id in args.card_ids]
        card = await board.create_card(
            self.ctx,
            event_id=self.event_id,
            kind="comparison",
            title=args.title,
            data={
                "card_ids": args.card_ids,
                "venues": [{"card_id": c.id, "title": c.title} for c in cards],
                "rows": [row.model_dump() for row in args.rows],
                "summary": args.summary,
                "recommended_card_id": args.recommended_card_id,
            },
            created_by="agent",
        )
        return {"card_id": card.id, "title": card.title}

    async def create_presentation(self, brief: PresentationBrief) -> dict[str, Any]:
        for card_id in brief.card_ids:
            await self._card(card_id)
        deck, run = await presentations.start_presentation_build(
            self.ctx, event_id=self.event_id, brief=brief, parent_run_id=self.run_id
        )
        self._launched(run.id, follow_up=True)
        return {
            "presentation_id": deck.id,
            "title": deck.title,
            "run_id": run.id,
            "status": "started",
            "note": BACKGROUND_NOTE,
        }

    async def edit_presentation(self, args: EditPresentationIn) -> dict[str, Any]:
        deck = await self._presentation(args.presentation_id)
        if args.slide_id is not None:
            await self._slide(deck.id, args.slide_id)
        run = await presentations.start_presentation_edit(
            self.ctx,
            presentation_id=deck.id,
            instruction=args.instruction,
            slide_id=args.slide_id,
            parent_run_id=self.run_id,
        )
        self._launched(run.id, follow_up=False)
        return {"run_id": run.id, "presentation_id": deck.id, "status": "started", "note": BACKGROUND_NOTE}

    # -- retrieval ------------------------------------------------------------

    async def search_venue_memory(self, args: VenueMemoryIn) -> Any:
        hits = await search_service.search_venue_memory(
            self.ctx,
            query=args.query,
            near=(args.near.lat, args.near.lng) if args.near else None,
            radius_km=args.radius_km if args.near else None,
            min_capacity=args.min_capacity,
            limit=args.limit,
            event_id=self.event_id,
        )
        if hits:
            return hits
        client = get_search_client(self.ctx)
        if client is None:
            return {"hits": [], "note": "Venue memory is not available (Elasticsearch not configured)."}
        if not client.connected:
            return {"hits": [], "note": "Venue memory is not available right now (Elasticsearch is unreachable)."}
        return {"hits": [], "note": "No matching venues from earlier events."}

    async def search_event_history(self, args: EventHistoryIn) -> dict[str, Any]:
        try:
            result = await search_service.search_event_content(
                self.ctx, event_id=self.event_id, q=args.query, types=args.types, limit=args.limit
            )
        except SearchUnavailable as exc:
            raise ToolError(f"Event history search is not available: {exc}") from exc
        hits = []
        for hit in result["hits"]:
            text = " ".join(str(hit.get("text") or "").split())
            item: dict[str, Any] = {
                "type": hit["type"],
                "snippet": text if len(text) <= 300 else text[:297].rstrip() + "...",
                "link": {k: v for k, v in (hit.get("link") or {}).items() if v is not None},
            }
            for key in ("title", "presentation_title", "speaker", "role"):
                if hit.get(key):
                    item[key] = hit[key]
            hits.append(item)
        return {"total": result["total"], "hits": hits}

    # -- outreach -------------------------------------------------------------

    async def draft_email(self, draft: EmailDraftIn) -> dict[str, Any]:
        if draft.related_card_id is not None:
            await self._card(draft.related_card_id)
        email = await outreach.draft_email(self.ctx, event_id=self.event_id, draft=draft, created_by="agent")
        return {
            "email_id": email.id,
            "status": email.status,
            "note": "The draft is on the board as an email card. Nothing is sent until the planner confirms; "
            "only then call send_email.",
        }

    async def send_email(self, ref: EmailRef) -> dict[str, Any]:
        email = await self._email(ref.email_id)
        try:
            sent = await outreach.send_email(self.ctx, email.id)
        except outreach.OutreachError as exc:
            raise ToolError(str(exc)) from exc
        return {"email_id": sent.id, "status": sent.status, "error": sent.error}

    async def draft_luma_event(self, args: DraftLumaIn) -> dict[str, Any]:
        if args.venue_card_id is not None:
            await self._card(args.venue_card_id, kind="venue")
        try:
            draft = await outreach.build_luma_draft_from_event(
                self.ctx, event_id=self.event_id, venue_card_id=args.venue_card_id
            )
        except ValueError as exc:  # e.g. no event date saved yet; the model relays what to fix
            raise ToolError(str(exc)) from exc
        if args.overrides is not None:
            draft = LumaEventDraft.model_validate(
                {**draft.model_dump(), **args.overrides.model_dump(exclude_unset=True, exclude_none=True)}
            )
        luma = await outreach.draft_luma_event(self.ctx, event_id=self.event_id, draft=draft, created_by="agent")
        return {
            "luma_id": luma.id,
            "status": luma.status,
            "draft": draft.model_dump(mode="json", exclude_none=True),
            "note": "The draft is on the board as a Luma card. Nothing is published until the planner confirms.",
        }

    async def publish_luma_event(self, _: NoArgs) -> dict[str, Any]:
        try:
            luma = await outreach.publish_luma_event(self.ctx, self.event_id)
        except outreach.OutreachError as exc:
            raise ToolError(str(exc)) from exc
        return {"status": luma.status, "url": luma.url, "error": luma.error}

    async def invite_luma_guests(self, args: InviteGuestsIn) -> dict[str, Any]:
        luma = await self._luma_event()
        if luma is None or luma.status != "published":
            raise ToolError("The Luma event is not published yet; publish it first (with the planner's go-ahead).")
        try:
            luma = await outreach.invite_luma_guests(self.ctx, self.event_id, [str(e) for e in args.emails])
        except outreach.OutreachError as exc:
            raise ToolError(str(exc)) from exc
        return {"status": luma.status, "guests_invited": luma.guests_invited, "url": luma.url}
