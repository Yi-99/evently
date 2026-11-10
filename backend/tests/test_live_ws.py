"""Live session control WebSocket: WS /ws/sessions/{session_id}."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from functools import partial
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from app.api import ws_live
from app.live import service
from app.realtime import events as ev
from app.transcription.base import TranscriptEvent
from tests.test_live_service import install_fake_tracker, make_deck


def receive(ws: WebSocketTestSession, timeout: float = 5.0) -> dict[str, Any]:
    """`ws.receive_json()` that fails instead of hanging when nothing arrives."""

    async def next_message():
        with anyio.fail_after(timeout):
            return await ws._send_rx.receive()

    message = ws.portal.call(next_message)
    ws._raise_on_close(message)
    return json.loads(message["text"])


def receive_until(ws: WebSocketTestSession, match: Callable[[dict], bool]) -> tuple[dict, list[dict]]:
    """Read messages until one matches; returns it and everything read before it."""
    before: list[dict] = []
    for _ in range(50):
        message = receive(ws)
        if match(message):
            return message, before
        before.append(message)
    raise AssertionError(f"no matching message; saw {before}")


def of_type(type_: str) -> Callable[[dict], bool]:
    return lambda message: message.get("type") == type_


def ping(ws: WebSocketTestSession, ref: str = "p") -> None:
    """Round trip through the command loop: proves the socket is still served."""
    ws.send_json({"type": "ping", "ref": ref})
    pong, _ = receive_until(ws, of_type("pong"))
    assert pong["data"] == {"ref": ref}


def start_session(client: TestClient, event_id: str, with_deck: bool = True, **settings) -> dict:
    ctx = client.app.state.ctx
    body: dict[str, Any] = {"settings": settings}
    if with_deck:
        body["presentation_id"] = client.portal.call(make_deck, ctx, event_id).id
    created = client.post(f"/api/v1/events/{event_id}/live-sessions", json=body).json()
    assert client.post(f"/api/v1/live-sessions/{created['session']['id']}/start").status_code == 200
    return created


def join(client: TestClient, created: dict, name: str = "Ada") -> dict:
    return client.post(
        "/api/v1/live-sessions/join", json={"join_code": created["session"]["join_code"], "display_name": name}
    ).json()


def ws_url(session_id: str, token: str | None = None, **params) -> str:
    query = {**({"token": token} if token else {}), **params}
    return f"/ws/sessions/{session_id}" + ("?" + "&".join(f"{k}={v}" for k, v in query.items()) if query else "")


def ingest(client: TestClient, session_id: str, text: str, is_final: bool) -> None:
    ctx = client.app.state.ctx
    event = TranscriptEvent(text=text, is_final=is_final)
    client.portal.call(partial(service.ingest_transcript, ctx, session_id, event))


# --- handshake --------------------------------------------------------------------------


def test_hello_for_the_owner_without_a_token(client, client_event):
    created = start_session(client, client_event.id)
    sid = created["session"]["id"]
    with client.websocket_connect(ws_url(sid)) as ws:
        hello = receive(ws)
        assert hello["type"] == "hello"
        assert hello["data"]["client"] == {"role": "host", "participant_id": None, "display_name": "Dev User"}
        state = hello["data"]["state"]
        assert state["session"]["id"] == sid and state["session"]["status"] == "live"
        assert state["slide_count"] == 3 and len(state["slides"]) == 3 and state["recent_transcript"] == []
        assert hello["data"]["last_seq"] == 1  # the session.updated envelope from /start
        ping(ws)


def test_rejected_connections_close_with_app_codes(client, client_event):
    created = start_session(client, client_event.id, with_deck=False)
    other = start_session(client, client_event.id, with_deck=False)
    sid = created["session"]["id"]

    def close_code(url: str) -> int:
        with client.websocket_connect(url) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                receive(ws)
        return exc.value.code

    assert close_code(ws_url("ses_missing")) == ws_live.CLOSE_NOT_FOUND
    assert close_code(ws_url(sid, "garbage")) == ws_live.CLOSE_UNAUTHORIZED
    assert close_code(ws_url(sid, other["host_token"])) == ws_live.CLOSE_UNAUTHORIZED


def test_participants_are_marked_connected_and_announced(client, client_event):
    created = start_session(client, client_event.id, with_deck=False)
    sid = created["session"]["id"]
    ada, bob = join(client, created, "Ada"), join(client, created, "Bob")

    with client.websocket_connect(ws_url(sid, ada["token"])) as ws_ada:
        hello = receive(ws_ada)
        assert hello["data"]["client"] == {
            "role": "listener",
            "participant_id": ada["participant"]["id"],
            "display_name": "Ada",
        }
        connected = {p["id"]: p["connected"] for p in hello["data"]["state"]["participants"]}
        assert connected[ada["participant"]["id"]] is True and connected[bob["participant"]["id"]] is False

        with client.websocket_connect(ws_url(sid, bob["token"])) as ws_bob:
            receive(ws_bob)  # hello
            joined, _ = receive_until(ws_ada, of_type(ev.SESSION_PARTICIPANT_JOINED))
            assert joined["data"]["participant"]["id"] == bob["participant"]["id"]
            assert joined["topic"] == f"session:{sid}" and joined["seq"] > 0
            ws_bob.close()
            left, _ = receive_until(ws_ada, of_type(ev.SESSION_PARTICIPANT_LEFT))
            assert left["data"] == {"session_id": sid, "participant_id": bob["participant"]["id"]}


# --- commands ----------------------------------------------------------------------------


def test_host_controls_slides_and_bad_commands_get_errors(client, client_event):
    created = start_session(client, client_event.id)
    sid = created["session"]["id"]
    with client.websocket_connect(ws_url(sid, created["host_token"])) as ws:
        hello = receive(ws)
        host_id = hello["data"]["client"]["participant_id"]

        ws.send_json({"type": "slide.goto", "data": {"index": 2}, "ref": 1})
        changed, _ = receive_until(ws, of_type(ev.SESSION_SLIDE_CHANGED))
        assert changed["data"] == {
            "session_id": sid,
            "index": 2,
            "previous_index": 0,
            "reason": "manual",
            "matched": None,
            "by": host_id,
        }

        ws.send_json({"type": "slide.prev"})
        changed, _ = receive_until(ws, of_type(ev.SESSION_SLIDE_CHANGED))
        assert (changed["data"]["index"], changed["data"]["previous_index"]) == (1, 2)

        ws.send_json({"type": "slide.next"})
        changed, _ = receive_until(ws, of_type(ev.SESSION_SLIDE_CHANGED))
        assert changed["data"]["index"] == 2

        ws.send_json({"type": "slide.next"})  # already on the last slide: nothing happens
        ws.send_json({"type": "slide.goto", "data": {"index": "two"}, "ref": "bad"})
        error, before = receive_until(ws, of_type("error"))
        assert before == [] and error["data"]["ref"] == "bad" and "index" in error["data"]["message"]

        ws.send_json({"type": "dance", "ref": 7})
        assert receive(ws) == {"type": "error", "data": {"message": "unknown message type 'dance'", "ref": 7}}
        ws.send_text("{not json")
        assert receive(ws)["data"]["message"] == "invalid JSON"
        ws.send_text("[1, 2]")
        assert receive(ws)["type"] == "error"
        ws.send_bytes(b"\x00\x01")
        assert receive(ws)["data"]["message"] == "expected a JSON text frame"
        ping(ws, "still here")

    session = client.get(f"/api/v1/live-sessions/{sid}").json()["session"]
    assert session["current_slide_index"] == 2


def test_listeners_cannot_control(client, client_event):
    created = start_session(client, client_event.id)
    sid = created["session"]["id"]
    listener = join(client, created)
    with client.websocket_connect(ws_url(sid, listener["token"])) as ws:
        receive(ws)
        for type_ in ("slide.next", "slide.goto", "transcript.text"):
            ws.send_json({"type": type_, "data": {"index": 1, "text": "x", "is_final": True}, "ref": type_})
            error = receive(ws)
            assert error["type"] == "error" and error["data"]["ref"] == type_
            assert "not permitted" in error["data"]["message"]
        ping(ws)
    assert client.get(f"/api/v1/live-sessions/{sid}").json()["session"]["current_slide_index"] == 0


def test_transcript_text_from_browser_speech_recognition(client, client_event, monkeypatch):
    trackers = install_fake_tracker(monkeypatch)
    created = start_session(client, client_event.id)
    sid = created["session"]["id"]
    speaker = client.post(
        f"/api/v1/live-sessions/{sid}/participants", json={"display_name": "Grace", "role": "speaker"}
    ).json()
    with client.websocket_connect(ws_url(sid, speaker["token"])) as ws:
        receive(ws)
        ws.send_json({"type": "transcript.text", "data": {"text": "so the", "is_final": False}})
        partial_envelope, _ = receive_until(ws, of_type(ev.SESSION_TRANSCRIPT_PARTIAL))
        assert partial_envelope["data"] == {"text": "so the", "slide_index": 0}

        ws.send_json({"type": "transcript.text", "data": {"text": "so the venue, next slide", "is_final": True}})
        final_envelope, _ = receive_until(ws, of_type(ev.SESSION_TRANSCRIPT_FINAL))
        segment = final_envelope["data"]["segment"]
        assert (segment["seq"], segment["text"], segment["source"], segment["speaker"], segment["slide_index"]) == (
            1,
            "so the venue, next slide",
            "browser",
            "Grace",
            0,
        )
        changed, _ = receive_until(ws, of_type(ev.SESSION_SLIDE_CHANGED))
        assert changed["data"]["index"] == 1 and changed["data"]["reason"] == "command"

        ws.send_json({"type": "transcript.text", "data": {"is_final": True}, "ref": "x"})
        assert receive(ws)["data"] == {"message": "text: Field required", "ref": "x"}

    assert trackers[0].fed == [("so the", False), ("so the venue, next slide", True)]
    transcript = client.get(f"/api/v1/live-sessions/{sid}/transcript", params={"token": speaker["token"]}).json()
    assert [s["text"] for s in transcript] == ["so the venue, next slide"]


# --- forwarding -------------------------------------------------------------------------------


def test_listeners_do_not_get_transcripts_while_hidden(client, client_event):
    created = start_session(client, client_event.id, show_transcript_to_listeners=False)
    sid = created["session"]["id"]
    ingest(client, sid, "before joining", True)
    listener = join(client, created)

    with client.websocket_connect(ws_url(sid, listener["token"])) as ws:
        hello = receive(ws)
        assert hello["data"]["state"]["recent_transcript"] == []

        ingest(client, sid, "hidden par", False)
        ingest(client, sid, "hidden partial and final", True)
        client.portal.call(service.set_slide, client.app.state.ctx, sid, 1)
        changed, before = receive_until(ws, of_type(ev.SESSION_SLIDE_CHANGED))
        assert changed["data"]["index"] == 1
        assert [m["type"] for m in before if m["type"].startswith("session.transcript")] == []

        # The host turns sharing on; the change arrives as session.updated and transcripts start flowing.
        response = client.patch(
            f"/api/v1/live-sessions/{sid}", json={"settings": {"show_transcript_to_listeners": True}}
        )
        assert response.status_code == 200
        updated, _ = receive_until(ws, of_type(ev.SESSION_UPDATED))
        assert updated["data"]["session"]["settings"]["show_transcript_to_listeners"] is True
        ingest(client, sid, "now visible", True)
        final_envelope, _ = receive_until(ws, of_type(ev.SESSION_TRANSCRIPT_FINAL))
        assert final_envelope["data"]["segment"]["text"] == "now visible"

    # The host always sees them.
    with client.websocket_connect(ws_url(sid, created["host_token"])) as ws:
        hello = receive(ws)
        assert [s["text"] for s in hello["data"]["state"]["recent_transcript"]] == [
            "before joining",
            "hidden partial and final",
            "now visible",
        ]
        ingest(client, sid, "to the host", True)
        receive_until(ws, of_type(ev.SESSION_TRANSCRIPT_FINAL))


def test_since_replays_missed_envelopes_after_hello(client, client_event):
    created = start_session(client, client_event.id)  # seq 1: session.updated
    sid = created["session"]["id"]
    ctx = client.app.state.ctx
    client.portal.call(service.set_slide, ctx, sid, 1)  # seq 2
    client.portal.call(service.set_slide, ctx, sid, 2)  # seq 3

    with client.websocket_connect(ws_url(sid, since=1)) as ws:
        hello = receive(ws)
        assert hello["data"]["last_seq"] == 3 and hello["data"]["state"]["session"]["current_slide_index"] == 2
        replayed = [receive(ws), receive(ws)]
        assert [(e["seq"], e["type"], e["data"]["index"]) for e in replayed] == [
            (2, ev.SESSION_SLIDE_CHANGED, 1),
            (3, ev.SESSION_SLIDE_CHANGED, 2),
        ]
        ping(ws)


async def test_forwarder_tells_a_lagging_client_to_resync(ctx):
    class Socket:
        sent: list[dict] = []

        async def send_json(self, data: dict) -> None:
            self.sent.append(data)

    socket = Socket()
    client = service.SessionClient(session_id="ses_x", role="host", participant_id=None, display_name="h")
    async with await ctx.bus.subscribe(ev.session_topic("ses_x")) as sub:
        sub.lagged = True
        await ctx.bus.publish(ev.session_topic("ses_x"), ev.SESSION_SLIDE_CHANGED, {"index": 4})
        task = asyncio.create_task(ws_live._forward(socket, sub, client, True))
        for _ in range(100):
            if len(socket.sent) >= 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
    assert socket.sent[0] == {"type": "resync"}
    assert socket.sent[1]["type"] == ev.SESSION_SLIDE_CHANGED and socket.sent[1]["data"] == {"index": 4}
    assert sub.lagged is False
