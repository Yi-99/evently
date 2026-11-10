"""REST routes for live sessions (mounted under /api/v1).

Owner routes need the event owner's access token. `POST /live-sessions/join` is
public (the attendee's entry point). `/state` and `/transcript` accept a
participant token or the owner's access token, as `?token=` or a Bearer header.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, ValidationError

from app.core.deps import Ctx, CurrentUser, OwnedEvent, load_owned_event
from app.live import service
from app.live.service import DeckNotFound, SessionClient, SessionEnded, SessionNotFound
from app.models import LiveSession
from app.schemas.live import LiveSessionOut, ParticipantOut, SessionSettings, TranscriptSegmentOut

router = APIRouter(tags=["live-sessions"])
_bearer = HTTPBearer(auto_error=False)


# --- request / response bodies -------------------------------------------------


class SessionCreate(BaseModel):
    presentation_id: str | None = None
    title: str | None = Field(default=None, max_length=300)
    settings: SessionSettings | None = None


class SessionPatch(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    presentation_id: str | None = None  # null detaches the deck
    settings: dict[str, Any] | None = None  # partial; merged over the stored settings


class SlideBody(BaseModel):
    index: int


class ParticipantCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)
    role: Literal["host", "speaker", "listener"] = "speaker"


class JoinBody(BaseModel):
    join_code: str = Field(min_length=1, max_length=12)
    display_name: str = Field(min_length=1, max_length=100)


class SessionCreated(BaseModel):
    session: LiveSessionOut
    host_token: str


class ParticipantAdmitted(BaseModel):
    participant: ParticipantOut
    token: str


class Joined(ParticipantAdmitted):
    session: LiveSessionOut
    presentation_id: str | None


class SlideSummary(BaseModel):
    id: str
    position: int
    title: str
    keywords: list[str]


class SessionState(BaseModel):
    session: LiveSessionOut
    participants: list[ParticipantOut]
    slide_count: int
    slides: list[SlideSummary]
    recent_transcript: list[TranscriptSegmentOut]


# --- dependencies ---------------------------------------------------------------


async def owned_session(session_id: str, ctx: Ctx, user: CurrentUser) -> LiveSession:
    """The session must exist and its event must belong to the caller (404 otherwise)."""
    try:
        session = await service.get_session(ctx, session_id)
    except SessionNotFound:
        session = None
    if session is None or await load_owned_event(ctx, session.event_id, user) is None:
        raise HTTPException(404, "Live session not found")
    return session


OwnedSession = Annotated[LiveSession, Depends(owned_session)]


async def session_client(
    session_id: str,
    ctx: Ctx,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    token: Annotated[str | None, Query(description="Participant token or owner access token")] = None,
) -> SessionClient:
    """A participant of the session or the event owner (401 otherwise, 404 for an unknown session)."""
    try:
        client = await service.authenticate_session_client(
            ctx, session_id, token or (credentials.credentials if credentials else None)
        )
    except SessionNotFound:
        raise HTTPException(404, "Live session not found") from None
    if client is None:
        raise HTTPException(401, "Not a participant of this session", {"WWW-Authenticate": "Bearer"})
    return client


Client = Annotated[SessionClient, Depends(session_client)]


# --- owner routes -----------------------------------------------------------------


@router.post("/events/{event_id}/live-sessions", response_model=SessionCreated, status_code=201)
async def create_session(event: OwnedEvent, body: SessionCreate, ctx: Ctx, user: CurrentUser):
    try:
        admission = await service.create_session(
            ctx,
            event=event,
            host=user,
            presentation_id=body.presentation_id,
            title=body.title,
            settings=body.settings,
        )
    except DeckNotFound:
        raise HTTPException(404, "Presentation not found") from None
    return {"session": admission.session, "host_token": admission.token}


@router.get("/events/{event_id}/live-sessions", response_model=list[LiveSessionOut])
async def list_sessions(event: OwnedEvent, ctx: Ctx):
    return await service.list_sessions(ctx, event.id)


@router.get("/live-sessions/{session_id}", response_model=SessionState)
async def get_session(session: OwnedSession, ctx: Ctx):
    return await service.get_session_state(ctx, session.id)


@router.patch("/live-sessions/{session_id}", response_model=LiveSessionOut)
async def update_session(session: OwnedSession, body: SessionPatch, ctx: Ctx):
    try:
        return await service.update_session(ctx, session.id, **body.model_dump(exclude_unset=True))
    except DeckNotFound:
        raise HTTPException(404, "Presentation not found") from None
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False, include_context=False, include_input=False)) from None


@router.post("/live-sessions/{session_id}/start", response_model=LiveSessionOut)
async def start_session(session: OwnedSession, ctx: Ctx):
    try:
        return await service.start_session(ctx, session.id)
    except SessionEnded:
        raise HTTPException(409, "Live session has ended") from None


@router.post("/live-sessions/{session_id}/end", response_model=LiveSessionOut)
async def end_session(session: OwnedSession, ctx: Ctx):
    return await service.end_session(ctx, session.id)


@router.post("/live-sessions/{session_id}/slide", response_model=LiveSessionOut)
async def set_slide(session: OwnedSession, body: SlideBody, ctx: Ctx):
    return await service.set_slide(ctx, session.id, body.index)


@router.post("/live-sessions/{session_id}/participants", response_model=ParticipantAdmitted, status_code=201)
async def add_participant(session: OwnedSession, body: ParticipantCreate, ctx: Ctx):
    """Mint a token for a co-speaker (or another host / listener) without a join code."""
    try:
        admission = await service.add_participant(ctx, session.id, display_name=body.display_name, role=body.role)
    except SessionEnded:
        raise HTTPException(409, "Live session has ended") from None
    return {"participant": admission.participant, "token": admission.token}


# --- public -----------------------------------------------------------------------------


@router.post("/live-sessions/join", response_model=Joined)
async def join_session(body: JoinBody, ctx: Ctx):
    """Attendee entry point: a join code admits a listener. No user account needed."""
    try:
        admission = await service.join_session(ctx, body.join_code, body.display_name.strip())
    except SessionNotFound:
        raise HTTPException(404, "No live session with that join code") from None
    except SessionEnded:
        raise HTTPException(410, "Live session has ended") from None
    return {
        "participant": admission.participant,
        "token": admission.token,
        "session": admission.session,
        "presentation_id": admission.session.presentation_id,
    }


# --- participant or owner ----------------------------------------------------------------


@router.get("/live-sessions/{session_id}/state", response_model=SessionState)
async def get_state(client: Client, ctx: Ctx):
    return await service.get_client_state(ctx, client)


@router.get("/live-sessions/{session_id}/transcript", response_model=list[TranscriptSegmentOut])
async def list_transcript(client: Client, ctx: Ctx, after_seq: int = 0, limit: int = 200):
    """Final segments with seq > after_seq, oldest first. Hidden from listeners when the host says so."""
    session = await service.get_session(ctx, client.session_id)
    if not service.transcript_visible_to(client.role, session.settings):
        raise HTTPException(403, "The transcript is not shared with listeners")
    return await service.list_transcript(ctx, client.session_id, after_seq=after_seq, limit=limit)
