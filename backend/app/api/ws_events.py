"""WebSocket gateway for the planner workspace: ``WS /ws/events/{event_id}?token=&since=``.

Server → client (JSON):

- ``{"type": "hello", "data": {event_id, client_id, user: {id, name}, last_seq, presence: [...]}}``
  once, right after accept. Not a bus envelope (no seq). `last_seq` is the newest
  retained seq; everything newer arrives live, so fetch the REST snapshot after it.
- bus envelopes, exactly as `Envelope.model_dump(mode="json")`:
  ``{id, type, topic, seq, ts, data}``. With ``?since=<seq>`` the retained envelopes
  newer than `since` are replayed first.
- ``{"type": "resync"}`` when envelopes were lost: `since` is older than retained
  history (or newer than the server knows, after a restart) or this subscriber
  lagged. Refetch the snapshot and keep applying live envelopes.
- ``{"type": "pong", "data": {"ref": ref}}``, ``{"type": "ack", "data": {"ref": ref}}``
  (acks are sent only for operations that carried a `ref`) and
  ``{"type": "error", "data": {"message", "code", "ref"}}``. Errors never close the socket.

Client → server (JSON ``{"type": ..., "data": {...}, "ref"?: str | int}``):

- ``ping``
- ``card.move``        ``{card_id, x, y}``
- ``card.update``      ``{card_id, title?, data?, width?, height?, expected_version?}``
- ``presence.cursor``  ``{x, y}``
- ``presence.selection`` ``{card_ids: [...]}``

Close codes: 4401 not authenticated, 4404 no such (owned) event. The socket is
accepted before closing so browsers can read the code.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from app.core.context import AppContext
from app.core.deps import get_ctx, load_owned_event, ws_user
from app.core.ids import new_id
from app.models import User
from app.realtime import presence
from app.realtime.bus import Subscription
from app.realtime.events import Envelope, event_topic
from app.services import board

log = logging.getLogger(__name__)
router = APIRouter()

CLOSE_UNAUTHENTICATED = 4401
CLOSE_NOT_FOUND = 4404
RESYNC = {"type": "resync"}

Coord = Annotated[float, Field(allow_inf_nan=False)]
Size = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Ref = str | int | None


class ClientMessage(BaseModel):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)
    ref: Ref = None


class CardMove(BaseModel):
    card_id: str
    x: Coord
    y: Coord


class CardUpdate(BaseModel):
    card_id: str
    title: Annotated[str, Field(max_length=300)] | None = None
    data: dict[str, Any] | None = None
    width: Size | None = None
    height: Size | None = None
    expected_version: int | None = None


class Cursor(BaseModel):
    x: Coord
    y: Coord


class Selection(BaseModel):
    card_ids: Annotated[list[str], Field(max_length=500)]


class ClientError(Exception):
    """A client operation that failed; reported as an `error` message with `code`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def plan_replay(retained: list[Envelope], since: int | None) -> tuple[list[Envelope], bool]:
    """What to send a client that last saw `since`: (envelopes to replay, needs resync).

    `retained` is the bus history at subscription time. A resync is needed when the
    client is ahead of the server (seqs restarted) or behind what is retained.
    """
    if since is None:
        return [], False
    last_seq = retained[-1].seq if retained else 0
    if since > last_seq or (retained and since < retained[0].seq - 1):
        return [], True
    return [e for e in retained if e.seq > since], False


@dataclass
class Connection:
    websocket: WebSocket
    ctx: AppContext
    event_id: str
    client_id: str
    user: User
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message: dict[str, Any]) -> None:
        # Bus forwarding and op replies run in separate tasks; one writer at a time.
        async with self._send_lock:
            await self.websocket.send_json(message)

    async def error(self, code: str, message: str, ref: Ref = None) -> None:
        await self.send({"type": "error", "data": {"message": message, "code": code, "ref": ref}})


@router.websocket("/ws/events/{event_id}")
async def events_socket(websocket: WebSocket, event_id: str, since: Annotated[int | None, Query(ge=0)] = None) -> None:
    ctx = get_ctx(websocket)
    await websocket.accept()
    user = await ws_user(websocket)
    if user is None:
        await websocket.close(code=CLOSE_UNAUTHENTICATED, reason="Not authenticated")
        return
    if await load_owned_event(ctx, event_id, user) is None:
        await websocket.close(code=CLOSE_NOT_FOUND, reason="Event not found")
        return

    conn = Connection(websocket, ctx, event_id, new_id("cli"), user)
    await presence.join(ctx, event_id, conn.client_id, user)
    try:
        async with await ctx.bus.subscribe(event_topic(event_id)) as sub:
            # No await between subscribe() returning and history(): everything up to
            # `retained[-1]` is in the snapshot, everything after arrives on `sub`.
            retained = ctx.bus.history(event_topic(event_id))
            await _send_hello(conn, retained)
            replay, resync = plan_replay(retained, since)
            if resync:
                await conn.send(RESYNC)
            for envelope in replay:
                await conn.send(envelope.model_dump(mode="json"))
            await _pump(conn, sub)
    except WebSocketDisconnect:
        pass
    finally:
        await presence.leave(ctx, event_id, conn.client_id)


async def _send_hello(conn: Connection, retained: list[Envelope]) -> None:
    await conn.send(
        {
            "type": "hello",
            "data": {
                "event_id": conn.event_id,
                "client_id": conn.client_id,
                "user": {"id": conn.user.id, "name": conn.user.name},
                "last_seq": retained[-1].seq if retained else 0,
                "presence": presence.snapshot(conn.ctx, conn.event_id),
            },
        }
    )


async def _pump(conn: Connection, sub: Subscription) -> None:
    """Forward bus envelopes and handle client messages concurrently until either side ends."""
    # A TaskGroup (not cancel + gather) so that, when the handler itself is cancelled
    # from an outer anyio scope, the CancelledError that propagates is the outer one.
    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(_forward(conn, sub)), group.create_task(_receive(conn))]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            task.cancel()


async def _forward(conn: Connection, sub: Subscription) -> None:
    try:
        async for envelope in sub:
            if sub.lagged:
                sub.lagged = False
                await conn.send(RESYNC)
            await conn.send(envelope.model_dump(mode="json"))
    except (WebSocketDisconnect, RuntimeError):  # peer went away mid-send
        return


async def _receive(conn: Connection) -> None:
    while True:
        message = await conn.websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        text = message.get("text")
        if text is None:
            await conn.error("bad_request", "Expected a JSON text frame")
            continue
        await _handle(conn, text)


async def _handle(conn: Connection, text: str) -> None:
    """Run one client operation; every failure becomes an `error` reply."""
    ref: Ref = None
    try:
        payload = json.loads(text)
        if isinstance(payload, dict) and isinstance(payload.get("ref"), str | int):
            ref = payload["ref"]
        message = ClientMessage.model_validate(payload)
        if message.type == "ping":
            await conn.send({"type": "pong", "data": {"ref": ref}})
            return
        await _dispatch(conn, message)
        if ref is not None:
            await conn.send({"type": "ack", "data": {"ref": ref}})
    except json.JSONDecodeError:
        await conn.error("bad_request", "Message is not valid JSON", ref)
    except ValidationError as exc:
        await conn.error("bad_request", _validation_message(exc), ref)
    except ClientError as exc:
        await conn.error(exc.code, str(exc), ref)
    except Exception:  # noqa: BLE001 - a failed op must not drop the socket
        log.exception("ws op failed (event=%s client=%s)", conn.event_id, conn.client_id)
        await conn.error("internal", "Internal error", ref)


async def _dispatch(conn: Connection, message: ClientMessage) -> None:
    ctx, event_id, client_id = conn.ctx, conn.event_id, conn.client_id
    if message.type == "card.move":
        move = CardMove.model_validate(message.data)
        await _patch_card(conn, move.card_id, x=move.x, y=move.y)
    elif message.type == "card.update":
        update = CardUpdate.model_validate(message.data)
        await _patch_card(
            conn,
            update.card_id,
            title=update.title,
            data_patch=update.data,
            width=update.width,
            height=update.height,
            expected_version=update.expected_version,
        )
    elif message.type == "presence.cursor":
        cursor = Cursor.model_validate(message.data)
        await presence.update_cursor(ctx, event_id, client_id, cursor.x, cursor.y)
    elif message.type == "presence.selection":
        selection = Selection.model_validate(message.data)
        await presence.update_selection(ctx, event_id, client_id, selection.card_ids)
    else:
        raise ClientError("unknown_type", f"Unknown message type {message.type!r}")


async def _patch_card(conn: Connection, card_id: str, **changes: Any) -> None:
    card = await board.get_card(conn.ctx, card_id)
    if card is None or card.event_id != conn.event_id:
        raise ClientError("not_found", "Card not found")
    try:
        await board.patch_card(conn.ctx, card_id, by="user", **changes)
    except board.CardNotFound:
        raise ClientError("not_found", "Card not found") from None
    except board.VersionConflict as exc:
        raise ClientError("version_conflict", str(exc)) from None


def _validation_message(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'message'}: {e['msg']}" for e in exc.errors())
