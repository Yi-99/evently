"""REST routes for search (venue memory, transcripts, chat) — mounted under /api/v1.

All routes answer 503 with a clear message when Elasticsearch is not configured (`ELASTICSEARCH_URL`
unset) or not reachable. Owner routes need the event owner's access token; the live-session route
accepts a participant token or the owner's token (`?token=` or Bearer), like the other session routes.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.deps import Ctx, CurrentUser, OwnedEvent
from app.search import service
from app.search.client import SearchUnavailable
from app.search.indexer import reindex_event, reindex_owner

router = APIRouter(tags=["search"])
_bearer = HTTPBearer(auto_error=False)


def _unavailable(exc: SearchUnavailable) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))


def _parse_types(value: str | None) -> list[str] | None:
    if not value:
        return None
    types = [t.strip() for t in value.split(",") if t.strip()]
    unknown = [t for t in types if t not in service.CONTENT_TYPES]
    if unknown:
        raise HTTPException(422, f"Unknown search types {unknown}; use {', '.join(service.CONTENT_TYPES)}")
    return types


# --- venue knowledge base -------------------------------------------------------


@router.get("/search/venues")
async def search_venues(
    ctx: Ctx,
    user: CurrentUser,
    q: Annotated[str | None, Query(description="Free text, e.g. 'quiet venue with a terrace for 150'")] = None,
    near: Annotated[str | None, Query(description="'lat,lng' to filter and sort by distance")] = None,
    radius_km: Annotated[float | None, Query(gt=0, le=500, description="Radius around `near` (default 25)")] = None,
    min_capacity: Annotated[int | None, Query(ge=1)] = None,
    parking: Annotated[bool | None, Query(description="true = on-site parking or parking score >= 60")] = None,
    min_score: Annotated[int | None, Query(ge=0, le=100, description="Minimum overall venue score")] = None,
    event_type: Annotated[str | None, Query(description="Event type the venue was researched for")] = None,
    exclude_event_id: Annotated[str | None, Query(description="Leave out this event's own cards")] = None,
    limit: Annotated[int, Query(ge=1, le=service.MAX_LIMIT)] = 20,
) -> dict[str, Any]:
    """Venues evaluated before, across all of the caller's events (hybrid text/semantic + filters)."""
    try:
        near_point = service.parse_near(near)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    try:
        return await service.search_venues(
            ctx,
            owner_id=user.id,
            q=q,
            near=near_point,
            radius_km=radius_km,
            min_capacity=min_capacity,
            parking=parking,
            min_score=min_score,
            event_type=event_type,
            exclude_event_id=exclude_event_id,
            limit=limit,
        )
    except SearchUnavailable as exc:
        raise _unavailable(exc) from None


@router.get("/search/status")
async def search_status(ctx: Ctx, user: CurrentUser) -> dict[str, Any]:
    """Is Elasticsearch configured and connected, document counts per index, semantic support."""
    return await service.search_status(ctx)


@router.post("/search/reindex")
async def reindex_all_mine(ctx: Ctx, user: CurrentUser) -> dict[str, Any]:
    """Rebuild the search documents of every event the caller owns (backfill after enabling search)."""
    try:
        results = await reindex_owner(ctx, user.id)
    except SearchUnavailable as exc:
        raise _unavailable(exc) from None
    totals = {"venues": 0, "transcripts": 0, "messages": 0, "slides": 0}
    for counts in results.values():
        for key in totals:
            totals[key] += counts.get(key, 0)
    return {"events": results, "totals": totals}


# --- per-event content ----------------------------------------------------------


@router.get("/events/{event_id}/search")
async def search_event(
    event: OwnedEvent,
    ctx: Ctx,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    types: Annotated[str | None, Query(description="Comma-separated subset of transcript,message,slide")] = None,
    limit: Annotated[int, Query(ge=1, le=service.MAX_LIMIT)] = 20,
) -> dict[str, Any]:
    """One ranked list across the event's transcripts, chat messages and slides, with deep-link ids."""
    try:
        return await service.search_event_content(ctx, event_id=event.id, q=q, types=_parse_types(types), limit=limit)
    except SearchUnavailable as exc:
        raise _unavailable(exc) from None


@router.post("/events/{event_id}/search/reindex")
async def reindex_one_event(event: OwnedEvent, ctx: Ctx) -> dict[str, Any]:
    """Rebuild every search document for this event from the database."""
    try:
        counts = await reindex_event(ctx, event.id)
    except SearchUnavailable as exc:
        raise _unavailable(exc) from None
    return {"event_id": event.id, "indexed": counts}


# --- live sessions (attendees) --------------------------------------------------


@router.get("/live-sessions/{session_id}/search")
async def search_session(
    session_id: str,
    ctx: Ctx,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    token: Annotated[str | None, Query(description="Participant token or owner access token")] = None,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    limit: Annotated[int, Query(ge=1, le=service.MAX_LIMIT)] = 20,
) -> dict[str, Any]:
    """Search the live session's transcript so far. Any admitted participant (or the owner) may call it."""
    from app.live.service import SessionNotFound, authenticate_session_client

    try:
        client = await authenticate_session_client(
            ctx, session_id, token or (credentials.credentials if credentials else None)
        )
    except SessionNotFound:
        raise HTTPException(404, "Live session not found") from None
    if client is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "A participant token or the owner's token is required")
    try:
        return await service.search_session_transcript(ctx, session_id=session_id, q=q, limit=limit)
    except SearchUnavailable as exc:
        raise _unavailable(exc) from None
