"""SSE gateway: GET /api/v1/events/{event_id}/stream."""

import asyncio
import json

from app.api.stream import CONNECTED_FRAME, RESYNC_FRAME, event_stream, sse_frame
from app.realtime.events import event_topic
from app.services import board


def _parse(frame: str) -> dict:
    fields = dict(line.split(": ", 1) for line in frame.strip().splitlines())
    fields["data"] = json.loads(fields["data"])
    return fields


# --- generator, against a ctx ------------------------------------------------------


async def test_stream_frames_replay_and_heartbeat(ctx, event):
    topic = event_topic(event.id)
    first = await ctx.bus.publish(topic, "a", {"n": 1})
    second = await ctx.bus.publish(topic, "b", {"n": 2})

    stream = event_stream(ctx, event.id, since=first.seq, heartbeat=0.05)
    assert await anext(stream) == CONNECTED_FRAME
    assert ctx.bus.subscriber_count(topic) == 1

    frame = await anext(stream)
    assert frame == sse_frame(second) == f"id: {second.seq}\nevent: b\ndata: {second.model_dump_json()}\n\n"
    parsed = _parse(frame)
    assert parsed["id"] == str(second.seq) and parsed["event"] == "b" and parsed["data"]["data"] == {"n": 2}

    assert await anext(stream) == ": heartbeat\n\n"

    third = await ctx.bus.publish(topic, "c", {})
    assert _parse(await anext(stream))["data"]["seq"] == third.seq

    await stream.aclose()
    assert ctx.bus.subscriber_count(topic) == 0


async def test_stream_resyncs_when_history_is_gone(ctx, event):
    stream = event_stream(ctx, event.id, since=50)
    assert await anext(stream) == CONNECTED_FRAME
    assert await anext(stream) == RESYNC_FRAME == "event: resync\ndata: {}\n\n"
    live = await ctx.bus.publish(event_topic(event.id), "x", {})
    assert _parse(await anext(stream))["id"] == str(live.seq)
    await stream.aclose()


async def test_stream_resyncs_when_lagged(ctx, event):
    topic = event_topic(event.id)
    stream = event_stream(ctx, event.id)
    assert await anext(stream) == CONNECTED_FRAME
    for i in range(1100):  # Subscription queue holds 1000
        await ctx.bus.publish(topic, "burst", {"i": i})
    assert await anext(stream) == RESYNC_FRAME
    assert _parse(await anext(stream))["data"]["data"]["i"] == 100
    await stream.aclose()


async def test_cancelling_consumer_closes_subscription(ctx, event):
    topic = event_topic(event.id)

    async def consume() -> None:
        async for _ in event_stream(ctx, event.id):
            pass

    task = asyncio.create_task(consume())
    for _ in range(50):
        if ctx.bus.subscriber_count(topic):
            break
        await asyncio.sleep(0.01)
    assert ctx.bus.subscriber_count(topic) == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert ctx.bus.subscriber_count(topic) == 0


# --- HTTP ------------------------------------------------------------------------


def test_stream_requires_user_and_ownership(client, client_event):
    assert client.get(f"/api/v1/events/{client_event.id}/stream?token=garbage").status_code == 401
    assert client.get("/api/v1/events/evt_missing/stream").status_code == 404


def test_stream_over_asgi_cleans_up_on_disconnect(client, client_event):
    """Drive the ASGI app by hand: the TestClient cannot return an endless response."""
    ctx = client.app.state.ctx
    topic = event_topic(client_event.id)
    seed = client.portal.call(ctx.bus.publish, topic, "seed", {})

    async def run() -> list[dict]:
        disconnect = asyncio.Event()
        got_card = asyncio.Event()
        messages: list[dict] = []

        async def receive() -> dict:
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            messages.append(message)
            if b"board.card.created" in message.get("body", b""):
                got_card.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": f"/api/v1/events/{client_event.id}/stream",
            "raw_path": f"/api/v1/events/{client_event.id}/stream".encode(),
            "query_string": b"since=0",
            "headers": [(b"host", b"testserver"), (b"last-event-id", str(seed.seq).encode())],
            "server": ("testserver", 80),
            "client": ("testclient", 1),
            "root_path": "",
        }
        app_task = asyncio.create_task(client.app(scope, receive, send))
        for _ in range(100):
            if ctx.bus.subscriber_count(topic):
                break
            await asyncio.sleep(0.01)
        assert ctx.bus.subscriber_count(topic) == 1
        await board.create_card(ctx, event_id=client_event.id, kind="note", title="n")
        await asyncio.wait_for(got_card.wait(), 2)
        disconnect.set()
        await asyncio.wait_for(app_task, 2)
        return messages

    messages = client.portal.call(run)
    assert ctx.bus.subscriber_count(topic) == 0

    start = messages[0]
    assert start["type"] == "http.response.start" and start["status"] == 200
    headers = dict(start["headers"])
    assert headers[b"content-type"].startswith(b"text/event-stream") and headers[b"cache-control"] == b"no-cache"
    body = b"".join(m.get("body", b"") for m in messages[1:]).decode()
    frames = body.split("\n\n")
    assert frames[0] == ": connected"
    # Last-Event-ID (= seed.seq) won over ?since=0, so the seed itself is not replayed.
    assert "event: seed" not in body and "event: resync" not in body
    assert frames[1].startswith(f"id: {seed.seq + 1}\nevent: board.card.created\n")
