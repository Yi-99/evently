"""Audio routes: the speaker/listener WebSocket relay and LiveKit join tokens.

Paths are declared in full (this router is mounted without a prefix).

WS /ws/sessions/{session_id}/audio?token=&role=speaker|listener

  Close codes (sent after accept so browsers can read them; every server-initiated close is
  preceded by {"type":"error","code":<snake_case reason>,"message":...}):
    4400 bad request (unknown role, malformed hello, invalid frame)   4401 not authenticated
    4403 forbidden (listener as speaker; takeover by someone who is    4404 unknown session
         neither a host nor the current speaker reconnecting)
    4408 no hello within HELLO_TIMEOUT_S                              4409 session is not live
    4410 replaced by another speaker (takeover)                       4423 another speaker is active
    4429 listener limit reached                                       1001 server shutting down

  Speaker:  -> {"type":"hello","codec":"pcm16","sample_rate":16000,"channels":1,"frame_ms":20,"takeover":false}
            <- {"type":"ready",codec,sample_rate,channels,frame_ms,"max_payload_bytes","broadcast"}
               or {"type":"error","code":...,"message":...} followed by a close
            -> binary frames (12-byte header + payload), see app.live.audio_hub
  Listener: <- {"type":"format",codec,sample_rate,channels,frame_ms} when a stream (re)starts
            <- binary frames, verbatim from the speaker; a jump in `seq` means frames were dropped for you
            <- {"type":"stopped","reason":...} when the stream ends
            <- {"type":"disabled"} when the session's audio_broadcast setting is off (socket stays open)
  Both:     -> {"type":"ping","t":...} / <- {"type":"pong","t":...}
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, WebSocket, status
from pydantic import BaseModel, ValidationError
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.core.context import AppContext
from app.core.deps import Ctx, get_ctx
from app.live import service as live_service
from app.live.audio_hub import (
    HEADER_SIZE,
    AudioFormat,
    AudioHub,
    AudioListener,
    AudioSource,
    InvalidFrame,
    ListenerLimitReached,
    SourceBusy,
    UnsupportedFormat,
    get_audio_hub,
)
from app.live.livekit import LiveKitTokenOut, livekit_configured, livekit_join
from app.live.service import SessionClient
from app.live.stt_pipeline import SttSession, get_stt_pipeline
from app.realtime import events as ev
from app.realtime.events import session_topic
from app.schemas.live import SessionSettings

router = APIRouter()

HELLO_TIMEOUT_S = 10.0
ROLES = ("speaker", "listener")


class Hello(BaseModel):
    type: Literal["hello"]
    codec: str = "pcm16"
    sample_rate: int = 16000
    channels: int = 1
    frame_ms: int = 20
    takeover: bool = False

    def audio_format(self) -> AudioFormat:
        return AudioFormat(
            codec=self.codec, sample_rate=self.sample_rate, channels=self.channels, frame_ms=self.frame_ms
        )


@dataclass(frozen=True)
class _Exit:
    """How a connection ends when the server, not the client, decides to end it."""

    close_code: int
    error_code: str | None = None
    message: str | None = None


# --- helpers -----------------------------------------------------------------


def _session_settings(session: Any) -> SessionSettings:
    return SessionSettings.model_validate(getattr(session, "settings", None) or {})


async def _send_error(websocket: WebSocket, code: str, message: str | None = None) -> None:
    body: dict[str, Any] = {"type": "error", "code": code}
    if message:
        body["message"] = message
    await websocket.send_json(body)


async def _close(websocket: WebSocket, outcome: _Exit) -> None:
    """Send the error (if any) and close; tolerates a client that already left."""
    if websocket.client_state != WebSocketState.CONNECTED or websocket.application_state != WebSocketState.CONNECTED:
        return
    try:
        if outcome.error_code:
            await _send_error(websocket, outcome.error_code, outcome.message)
        await websocket.close(code=outcome.close_code, reason=outcome.error_code or "")
    except (WebSocketDisconnect, RuntimeError):
        pass


async def _race(*coros: Coroutine[Any, Any, _Exit | None]) -> _Exit | None:
    """Run the coroutines concurrently; when the first finishes, cancel the rest and return its result.

    Exceptions from the finishing coroutine propagate.
    """
    tasks = [asyncio.create_task(coro) for coro in coros]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        # asyncio.wait, not gather: a gather cancelled from outside re-raises a CancelledError that has
        # lost the canceller's message, which makes Starlette's TestClient (anyio) treat it as foreign.
        await asyncio.wait(tasks)
    first = next(task for task in tasks if task.done() and not task.cancelled())
    return first.result()


def _parse_json(message: dict[str, Any]) -> dict[str, Any] | None:
    text = message.get("text")
    if text is None:
        return None
    try:
        body = json.loads(text)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _pong(body: dict[str, Any]) -> dict[str, Any]:
    pong: dict[str, Any] = {"type": "pong"}
    if "t" in body:
        pong["t"] = body["t"]
    return pong


# --- admission -----------------------------------------------------------------


async def _admit(
    websocket: WebSocket, ctx: AppContext, session_id: str, token: str | None, role: str
) -> tuple[SessionClient, SessionSettings] | None:
    """Authenticate and authorise the connection; on refusal close it and return None."""
    if role not in ROLES:
        await _close(websocket, _Exit(4400, "bad_role", f"role must be one of {ROLES}"))
        return None
    try:
        client = await live_service.authenticate_session_client(ctx, session_id, token)
        if client is None:
            await _close(websocket, _Exit(4401, "unauthenticated"))
            return None
        if role == "speaker" and not client.can_control:
            await _close(websocket, _Exit(4403, "forbidden", "only hosts and speakers may send audio"))
            return None
        session = await live_service.get_session(ctx, session_id)
    except live_service.SessionNotFound:
        await _close(websocket, _Exit(4404, "unknown_session"))
        return None
    if session.status != "live":
        await _close(websocket, _Exit(4409, "session_not_live"))
        return None
    return client, _session_settings(session)


# --- speaker -------------------------------------------------------------------


async def _receive_hello(websocket: WebSocket) -> Hello | _Exit:
    try:
        message = await asyncio.wait_for(websocket.receive(), HELLO_TIMEOUT_S)
    except TimeoutError:
        return _Exit(4408, "hello_timeout")
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    body = _parse_json(message)
    if body is None:
        return _Exit(4400, "invalid_hello", "first message must be a JSON hello")
    try:
        return Hello.model_validate(body)
    except ValidationError as exc:
        return _Exit(4400, "invalid_hello", str(exc.errors()[0].get("msg")))


async def _start_source(
    websocket: WebSocket, hub: AudioHub, client: SessionClient, settings: SessionSettings
) -> AudioSource | None:
    """Hello handshake: register the speaker in the hub and reply `ready`, or refuse and close."""
    hello = await _receive_hello(websocket)
    if isinstance(hello, _Exit):
        await _close(websocket, hello)
        return None
    try:
        source = await hub.start_source(
            client.session_id,
            hello.audio_format(),
            participant_id=client.participant_id,
            is_host=client.role == "host",
            takeover=hello.takeover,
            broadcast=settings.audio_broadcast,
        )
    except UnsupportedFormat as exc:
        await _close(websocket, _Exit(4400, "unsupported_format", str(exc)))
        return None
    except SourceBusy as exc:
        if hello.takeover:
            await _close(websocket, _Exit(4403, "takeover_forbidden", "only a host may take over the stream"))
        else:
            await _close(websocket, _Exit(4423, "source_busy", f"participant {exc.source_participant_id} is streaming"))
        return None
    await websocket.send_json(
        {
            "type": "ready",
            **source.format.as_message(),
            "max_payload_bytes": source.format.max_payload_bytes,
            "broadcast": source.broadcast,
        }
    )
    return source


async def _pump_speaker(websocket: WebSocket, source: AudioSource, stt: SttSession | None) -> _Exit | None:
    """Relay frames until the client disconnects (None) or breaks the protocol (an _Exit)."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return None
        frame = message.get("bytes")
        if frame is not None:
            try:
                source.publish(frame)
            except InvalidFrame as exc:
                return _Exit(4400, "invalid_frame", str(exc))
            if stt is not None:
                stt.feed(frame[HEADER_SIZE:])
            continue
        body = _parse_json(message)
        if body is not None and body.get("type") == "ping":
            await websocket.send_json(_pong(body))


async def _wait_evicted(source: AudioSource) -> _Exit:
    reason = await source.wait_closed()
    if reason == "takeover":
        return _Exit(4410, "taken_over", "another host took over the audio stream")
    return _Exit(1001, "server_shutdown")


async def _watch_session(ctx: AppContext, hub: AudioHub, source: AudioSource) -> _Exit:
    """Follow `session.updated`: end the connection when the session stops, mirror the audio_broadcast setting."""
    async with await ctx.bus.subscribe(session_topic(source.session_id)) as sub:
        async for envelope in sub:
            if envelope.type != ev.SESSION_UPDATED:
                continue
            session = envelope.data.get("session") or {}
            if session.get("status", "live") != "live":
                return _Exit(4409, "session_not_live", "the session has ended")
            await hub.set_broadcast(
                source, SessionSettings.model_validate(session.get("settings") or {}).audio_broadcast
            )
    return _Exit(1001, "server_shutdown")


async def _run_speaker(websocket: WebSocket, ctx: AppContext, client: SessionClient, settings: SessionSettings) -> None:
    hub = get_audio_hub(ctx)
    source = await _start_source(websocket, hub, client, settings)
    if source is None:
        return
    stt: SttSession | None = None
    try:
        stt = await get_stt_pipeline(ctx).start(client.session_id, source.format, settings)
        outcome = await _race(
            _pump_speaker(websocket, source, stt), _wait_evicted(source), _watch_session(ctx, hub, source)
        )
        if outcome is not None:
            await _close(websocket, outcome)
    finally:
        await hub.stop_source(source)
        if stt is not None:
            await stt.stop()


# --- listener ------------------------------------------------------------------


async def _send_to_listener(websocket: WebSocket, listener: AudioListener) -> _Exit:
    while (item := await listener.get()) is not None:
        if isinstance(item, bytes):
            await websocket.send_bytes(item)
        else:
            await websocket.send_json(item)
    return _Exit(1001, "server_shutdown")


async def _receive_from_listener(websocket: WebSocket, listener: AudioListener) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return None
        body = _parse_json(message)
        if body is not None and body.get("type") == "ping":
            listener.push_control(_pong(body))  # through the queue: the sender task is the only writer


async def _run_listener(
    websocket: WebSocket, ctx: AppContext, client: SessionClient, settings: SessionSettings
) -> None:
    hub = get_audio_hub(ctx)
    if not settings.audio_broadcast:
        await websocket.send_json({"type": "disabled", "reason": "audio_broadcast_off"})
    try:
        listener = hub.add_listener(client.session_id)
    except ListenerLimitReached:
        await _close(websocket, _Exit(4429, "listener_limit", "this session has reached its listener limit"))
        return
    try:
        outcome = await _race(_send_to_listener(websocket, listener), _receive_from_listener(websocket, listener))
        if outcome is not None:
            await _close(websocket, outcome)
    finally:
        hub.remove_listener(listener)


# --- routes --------------------------------------------------------------------


@router.websocket("/ws/sessions/{session_id}/audio")
async def session_audio(
    websocket: WebSocket,
    session_id: str,
    token: Annotated[str | None, Query()] = None,
    role: Annotated[str, Query()] = "listener",
) -> None:
    ctx = get_ctx(websocket)
    await websocket.accept()
    try:
        admitted = await _admit(websocket, ctx, session_id, token, role)
        if admitted is None:
            return
        client, settings = admitted
        if role == "speaker":
            await _run_speaker(websocket, ctx, client, settings)
        else:
            await _run_listener(websocket, ctx, client, settings)
    except WebSocketDisconnect:
        pass


@router.post("/api/v1/live-sessions/{session_id}/livekit-token", response_model=LiveKitTokenOut)
async def livekit_token(
    session_id: str,
    ctx: Ctx,
    token: Annotated[str | None, Query()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> LiveKitTokenOut:
    """LiveKit join details for this client; the token may come from `?token=` or a bearer header.

    404 when LiveKit is not configured or the session is unknown, 401 when the token is rejected,
    409 when the session is not live.
    """
    if not livekit_configured(ctx.settings):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "LiveKit is not configured")
    if token is None and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]
    try:
        client = await live_service.authenticate_session_client(ctx, session_id, token)
        if client is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
        session = await live_service.get_session(ctx, session_id)
    except live_service.SessionNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found") from None
    if session.status != "live":
        raise HTTPException(status.HTTP_409_CONFLICT, "Session is not live")
    return livekit_join(ctx.settings, client)
