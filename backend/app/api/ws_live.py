"""Live-session control WebSocket: `WS /ws/sessions/{session_id}?token=&since=`.

Server -> client (JSON text frames)
  {"type": "hello", "data": {"client": {"role", "participant_id", "display_name"},
                             "state": <get_session_state>, "last_seq": int}}   first, once
  <envelope>   every envelope published on `session:{session_id}` (`{id, type, topic, seq, ts, data}`);
               with `?since=<seq>` envelopes newer than `since` are replayed first.
               Listeners get no `session.transcript.*` while `show_transcript_to_listeners` is off.
  {"type": "resync"}                           envelopes were dropped: refetch the state
  {"type": "pong", "data": {"ref": ref}}
  {"type": "error", "data": {"message": str, "ref": ref}}   the offending message is ignored

Client -> server: {"type": str, "data": {...}?, "ref": any?}
  ping
  slide.goto {index} | slide.next | slide.prev       host / speaker only
  transcript.text {text, is_final}                   host / speaker only (browser speech recognition)

Close codes: 4401 token rejected, 4404 unknown session. The socket is accepted
first so the browser can read the code.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, WebSocket
from pydantic import BaseModel, Field, ValidationError
from starlette.websockets import WebSocketDisconnect

from app.core.context import AppContext
from app.core.deps import get_ctx
from app.live import service
from app.live.service import SessionClient, SessionNotFound
from app.realtime import events as ev
from app.realtime.bus import Subscription
from app.transcription.base import TranscriptEvent

router = APIRouter()

CLOSE_UNAUTHORIZED = 4401
CLOSE_NOT_FOUND = 4404
_TRANSCRIPT_TYPES = {ev.SESSION_TRANSCRIPT_PARTIAL, ev.SESSION_TRANSCRIPT_FINAL}
_CONTROL_TYPES = {"slide.goto", "slide.next", "slide.prev", "transcript.text"}


class _Goto(BaseModel):
    index: int


class _TranscriptText(BaseModel):
    text: str = Field(max_length=5000)
    is_final: bool = False


class _Rejected(Exception):
    """A client message answered with an `error` frame."""


@router.websocket("/ws/sessions/{session_id}")
async def session_socket(websocket: WebSocket, session_id: str, token: str | None = None, since: int | None = None):
    ctx = get_ctx(websocket)
    try:
        client = await service.authenticate_session_client(ctx, session_id, token)
        close_code = CLOSE_UNAUTHORIZED
    except SessionNotFound:
        client, close_code = None, CLOSE_NOT_FOUND
    await websocket.accept()
    if client is None:
        await websocket.close(close_code)
        return

    if client.participant_id:
        await service.set_participant_connected(ctx, client.participant_id, True)
    try:
        async with await ctx.bus.subscribe(ev.session_topic(session_id), since_seq=since) as sub:
            await _run(websocket, ctx, client, sub)
    finally:
        if client.participant_id:
            await service.set_participant_connected(ctx, client.participant_id, False)


async def _run(websocket: WebSocket, ctx: AppContext, client: SessionClient, sub: Subscription) -> None:
    # Subscribed before the snapshot, so nothing published in between is lost.
    history = ctx.bus.history(sub.topic)
    state = await service.get_client_state(ctx, client)
    visible = service.transcript_visible_to(client.role, state["session"]["settings"])
    await websocket.send_json(
        {
            "type": "hello",
            "data": {
                "client": {
                    "role": client.role,
                    "participant_id": client.participant_id,
                    "display_name": client.display_name,
                },
                "state": state,
                "last_seq": history[-1].seq if history else 0,
            },
        }
    )
    try:
        async with asyncio.TaskGroup() as tasks:
            forwarding = tasks.create_task(_forward(websocket, sub, client, visible))
            await _serve_commands(websocket, ctx, client)
            forwarding.cancel()
    except* WebSocketDisconnect:
        pass  # the client went away while we were sending


async def _forward(websocket: WebSocket, sub: Subscription, client: SessionClient, transcript_visible: bool) -> None:
    """Bus -> socket. Tracks `show_transcript_to_listeners` from the `session.updated` envelopes it sees."""
    async for envelope in sub:
        if sub.lagged:
            sub.lagged = False
            await websocket.send_json({"type": "resync"})
        if envelope.type == ev.SESSION_UPDATED and client.role == "listener":
            transcript_visible = service.transcript_visible_to(client.role, envelope.data["session"]["settings"])
        if envelope.type in _TRANSCRIPT_TYPES and not transcript_visible:
            continue
        await websocket.send_json(envelope.model_dump(mode="json"))


async def _serve_commands(websocket: WebSocket, ctx: AppContext, client: SessionClient) -> None:
    """Socket -> service, until the client disconnects. Bad messages get an `error` frame, never a close."""
    while True:
        frame = await websocket.receive()
        if frame["type"] == "websocket.disconnect":
            return
        ref = None
        try:
            message = _parse(frame.get("text"))
            ref = message.get("ref")
            if message["type"] == "ping":
                await websocket.send_json({"type": "pong", "data": {"ref": ref}})
            else:
                await _handle(ctx, client, message["type"], message.get("data") or {})
        except _Rejected as exc:
            await websocket.send_json({"type": "error", "data": {"message": str(exc), "ref": ref}})


def _parse(text: str | None) -> dict[str, Any]:
    if text is None:
        raise _Rejected("expected a JSON text frame")
    try:
        message = json.loads(text)
    except ValueError:
        raise _Rejected("invalid JSON") from None
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise _Rejected('expected {"type": str, "data": object, "ref": any}')
    return message


async def _handle(ctx: AppContext, client: SessionClient, type_: str, data: Any) -> None:
    if type_ not in _CONTROL_TYPES:
        raise _Rejected(f"unknown message type {type_!r}")
    if not client.can_control:
        raise _Rejected(f"{type_} is not permitted for listeners")
    try:
        if type_ == "slide.goto":
            index = _Goto.model_validate(data).index
            await service.set_slide(ctx, client.session_id, index, by=client.participant_id)
        elif type_ in {"slide.next", "slide.prev"}:
            current = (await service.get_session(ctx, client.session_id)).current_slide_index
            step = 1 if type_ == "slide.next" else -1
            await service.set_slide(ctx, client.session_id, current + step, by=client.participant_id)
        else:
            text = _TranscriptText.model_validate(data)
            event = TranscriptEvent(text=text.text, is_final=text.is_final, speaker=client.display_name)
            await service.ingest_transcript(ctx, client.session_id, event, source="browser")
    except ValidationError as exc:
        raise _Rejected(_describe(exc)) from None
    except SessionNotFound:
        raise _Rejected("session not found") from None


def _describe(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(part) for part in err['loc']) or 'data'}: {err['msg']}" for err in exc.errors())
