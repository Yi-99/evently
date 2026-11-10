"""Server-Sent Events gateway: ``GET /api/v1/events/{event_id}/stream?since=&token=``.

The same bus envelopes as the WebSocket, framed as SSE::

    id: <seq>
    event: <envelope type>
    data: <envelope JSON>

A ``: heartbeat`` comment is written after 15 s without envelopes, and ``event: resync``
(``data: {}``, no id) tells the client to refetch the snapshot when history was lost.
Reconnection works like the WebSocket's ``?since=``; the ``Last-Event-ID`` header, which
browsers send on automatic reconnect, takes precedence over ``since``. Authentication is
a bearer token or ``?token=`` (``EventSource`` cannot set headers).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.api.ws_events import plan_replay
from app.core.context import AppContext
from app.core.deps import Ctx, load_owned_event, resolve_user
from app.realtime.events import Envelope, event_topic

log = logging.getLogger(__name__)
router = APIRouter(tags=["stream"])

HEARTBEAT_SECONDS = 15.0
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def sse_frame(envelope: Envelope) -> str:
    return f"id: {envelope.seq}\nevent: {envelope.type}\ndata: {envelope.model_dump_json()}\n\n"


def sse_comment(text: str) -> str:
    return f": {text}\n\n"


RESYNC_FRAME = "event: resync\ndata: {}\n\n"
CONNECTED_FRAME = sse_comment("connected")


async def event_stream(
    ctx: AppContext, event_id: str, *, since: int | None = None, heartbeat: float = HEARTBEAT_SECONDS
) -> AsyncIterator[str]:
    """SSE frames for an event topic, forever; closing the generator closes the subscription.

    Order: a ``: connected`` comment, ``event: resync`` if `since` cannot be honoured,
    replayed envelopes newer than `since`, then live envelopes with a heartbeat
    comment whenever `heartbeat` seconds pass in silence.
    """
    topic = event_topic(event_id)
    async with await ctx.bus.subscribe(topic) as sub:
        # No await between subscribe() and history(): the snapshot and the live
        # subscription split the stream exactly (see ws_events.events_socket).
        retained = ctx.bus.history(topic)
        replay, resync = plan_replay(retained, since)
        yield CONNECTED_FRAME
        if resync:
            yield RESYNC_FRAME
        for envelope in replay:
            yield sse_frame(envelope)
        while True:
            try:
                envelope = await sub.get(timeout=heartbeat)
            except TimeoutError:
                yield sse_comment("heartbeat")
                continue
            if sub.lagged:
                sub.lagged = False
                yield RESYNC_FRAME
            yield sse_frame(envelope)


@router.get("/events/{event_id}/stream", response_class=StreamingResponse)
async def stream_event(
    event_id: str,
    ctx: Ctx,
    request: Request,
    since: Annotated[int | None, Query(ge=0)] = None,
    token: Annotated[str | None, Query()] = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Stream an event's envelopes as SSE. 401 without a valid user, 404 unless the caller owns the event."""
    bearer = request.headers.get("authorization", "")
    user = await resolve_user(ctx, token or (bearer[7:] if bearer.lower().startswith("bearer ") else None))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", {"WWW-Authenticate": "Bearer"})
    if await load_owned_event(ctx, event_id, user) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    if last_event_id is not None and last_event_id.isdigit():
        since = int(last_event_id)
    return StreamingResponse(
        event_stream(ctx, event_id, since=since), media_type="text/event-stream", headers=SSE_HEADERS
    )
