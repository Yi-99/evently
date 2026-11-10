"""WebSocket gateway: WS /ws/events/{event_id}."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.ws_events import plan_replay
from app.models import Event, User
from app.realtime import events as ev
from app.realtime.events import Envelope, event_topic
from app.services import board
from tests.helpers import create_event


def _ctx(client: TestClient):
    return client.app.state.ctx


def _publish(client: TestClient, event_id: str, type: str, data: dict) -> Envelope:
    return client.portal.call(_ctx(client).bus.publish, event_topic(event_id), type, data)


def _create_card(client: TestClient, event_id: str, **fields):
    return client.portal.call(lambda: board.create_card(_ctx(client), event_id=event_id, kind="note", **fields))


def _drain_until(ws, type: str, limit: int = 50) -> dict:
    """Receive until a message of `type` arrives (skipping e.g. presence echoes)."""
    for _ in range(limit):
        message = ws.receive_json()
        if message["type"] == type:
            return message
    raise AssertionError(f"no {type!r} message within {limit} messages")


def _receive_types(ws, *types: str) -> dict[str, dict]:
    """Receive one message per type; the reply and the bus envelope arrive in either order."""
    got = {}
    for _ in types:
        message = ws.receive_json()
        got[message["type"]] = message
    assert set(got) == set(types), got
    return got


# --- connection & auth ---------------------------------------------------------


def test_hello_then_live_envelopes(client, client_event):
    with client.websocket_connect(f"/ws/events/{client_event.id}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        data = hello["data"]
        assert data["event_id"] == client_event.id and data["client_id"].startswith("cli_")
        assert data["user"]["name"] == "Dev User" and set(data["user"]) == {"id", "name"}
        assert "seq" not in hello
        # presence is ephemeral (never retained), so joining does not advance last_seq
        assert data["last_seq"] == 0 and data["presence"][0]["client_id"] == data["client_id"]

        sent = _publish(client, client_event.id, "chat.delta", {"text": "hi"})
        got = ws.receive_json()
        assert got == sent.model_dump(mode="json")
        assert got["seq"] == 1 and got["topic"] == event_topic(client_event.id)


def test_rejects_bad_token_with_4401(client, client_event):
    with client.websocket_connect(f"/ws/events/{client_event.id}?token=garbage") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4401


def test_rejects_unknown_and_foreign_events_with_4404(client, client_event):
    async def foreign_event() -> Event:
        ctx = _ctx(client)
        async with ctx.db() as session:
            other = User(email="other@example.com", name="Other")
            session.add(other)
            await session.flush()
            event = Event(owner_id=other.id, name="Theirs")
            session.add(event)
            await session.commit()
            return event

    theirs = client.portal.call(foreign_event)
    for event_id in ("evt_nope", theirs.id):
        with client.websocket_connect(f"/ws/events/{event_id}") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
        assert exc.value.code == 4404


def test_disconnect_releases_subscription_and_presence(client, client_event):
    ctx = _ctx(client)
    topic = event_topic(client_event.id)
    with client.websocket_connect(f"/ws/events/{client_event.id}") as ws:
        client_id = ws.receive_json()["data"]["client_id"]
        assert ctx.bus.subscriber_count(topic) == 1
    assert ctx.bus.subscriber_count(topic) == 0
    from app.realtime import presence

    assert client_id not in {c["client_id"] for c in presence.snapshot(ctx, client_event.id)}


# --- replay & resync -----------------------------------------------------------


def test_since_replays_retained_envelopes(client, client_event):
    first = _publish(client, client_event.id, "a", {"n": 1})
    second = _publish(client, client_event.id, "b", {"n": 2})
    third = _publish(client, client_event.id, "c", {"n": 3})
    with client.websocket_connect(f"/ws/events/{client_event.id}?since={first.seq}") as ws:
        hello = ws.receive_json()
        assert hello["data"]["last_seq"] == 3  # a, b, c (presence is ephemeral)
        replayed = [ws.receive_json() for _ in range(2)]
        assert [m["seq"] for m in replayed] == [second.seq, third.seq]


def test_since_ahead_of_server_gets_resync(client, client_event):
    with client.websocket_connect(f"/ws/events/{client_event.id}?since=999") as ws:
        assert ws.receive_json()["type"] == "hello"
        assert ws.receive_json() == {"type": "resync"}
        sent = _publish(client, client_event.id, "x", {})
        assert ws.receive_json()["seq"] == sent.seq  # live continues after the resync


def test_lagged_subscription_gets_resync(client, client_event):
    ctx = _ctx(client)
    topic = event_topic(client_event.id)

    async def burst() -> None:
        # The in-memory bus never yields inside publish(), so the forwarder cannot
        # drain its 1000-slot queue in between and must drop the oldest envelopes.
        for i in range(1200):
            await ctx.bus.publish(topic, "burst", {"i": i})

    with client.websocket_connect(f"/ws/events/{client_event.id}") as ws:
        ws.receive_json()
        client.portal.call(burst)
        first = ws.receive_json()
        assert first == {"type": "resync"}
        after = ws.receive_json()
        assert after["type"] == "burst" and after["data"]["i"] > 0
        last = _publish(client, client_event.id, "tail", {})
        assert _drain_until(ws, "tail", limit=1300)["seq"] == last.seq


def test_plan_replay_edge_cases():
    def env(seq: int) -> Envelope:
        return Envelope(type="t", topic="event:x", seq=seq)

    retained = [env(5), env(6), env(7)]
    assert plan_replay(retained, None) == ([], False)
    assert plan_replay(retained, 7) == ([], False)
    assert plan_replay(retained, 4) == (retained, False)  # exactly contiguous
    assert plan_replay(retained, 3) == ([], True)  # seq 4 is gone
    assert plan_replay(retained, 8) == ([], True)  # client is ahead (restart)
    assert plan_replay([], 0) == ([], False)
    assert plan_replay([], 1) == ([], True)


# --- client operations ---------------------------------------------------------


def test_ping_pong_echoes_ref(client, client_event):
    with client.websocket_connect(f"/ws/events/{client_event.id}") as ws:
        ws.receive_json()
        ws.send_json({"type": "ping", "ref": "p1"})
        assert ws.receive_json() == {"type": "pong", "data": {"ref": "p1"}}
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong", "data": {"ref": None}}


def test_card_move_and_update_go_through_board_service(client, client_event):
    card = _create_card(client, client_event.id, title="Note", data={"summary": "a"})
    with client.websocket_connect(f"/ws/events/{client_event.id}") as ws:
        ws.receive_json()
        ws.send_json({"type": "card.move", "data": {"card_id": card.id, "x": 10, "y": 20.5}, "ref": 1})
        got = _receive_types(ws, ev.BOARD_CARD_UPDATED, "ack")
        updated = got[ev.BOARD_CARD_UPDATED]
        assert updated["data"]["by"] == "user" and sorted(updated["data"]["changed"]) == ["x", "y"]
        assert (updated["data"]["card"]["x"], updated["data"]["card"]["y"]) == (10.0, 20.5)
        assert got["ack"] == {"type": "ack", "data": {"ref": 1}}

        ws.send_json(
            {
                "type": "card.update",
                "data": {"card_id": card.id, "title": "Renamed", "data": {"summary": "mine"}, "expected_version": 2},
            }
        )
        updated = ws.receive_json()  # no ref, so no ack
        assert updated["type"] == ev.BOARD_CARD_UPDATED
        out = updated["data"]["card"]
        assert out["title"] == "Renamed" and out["data"] == {"summary": "mine"} and out["locked_fields"] == ["summary"]
        assert out["version"] == 3

        ws.send_json({"type": "card.update", "data": {"card_id": card.id, "title": "Stale", "expected_version": 1}})
        error = ws.receive_json()
        assert error["type"] == "error" and error["data"]["code"] == "version_conflict"
        assert error["data"]["ref"] is None and "version 3" in error["data"]["message"]


def test_card_from_another_event_is_not_found(client, client_event):
    other = client.portal.call(create_event, _ctx(client), "Other event")
    foreign_card = _create_card(client, other.id, title="Elsewhere")
    with client.websocket_connect(f"/ws/events/{client_event.id}") as ws:
        ws.receive_json()
        ws.send_json({"type": "card.move", "data": {"card_id": foreign_card.id, "x": 0, "y": 0}, "ref": "m"})
        assert ws.receive_json() == {
            "type": "error",
            "data": {"message": "Card not found", "code": "not_found", "ref": "m"},
        }
        ws.send_json({"type": "card.move", "data": {"card_id": "card_missing", "x": 0, "y": 0}})
        assert ws.receive_json()["data"]["code"] == "not_found"
    assert client.portal.call(board.get_card, _ctx(client), foreign_card.id).x == foreign_card.x


def test_bad_input_answers_error_and_keeps_socket(client, client_event):
    with client.websocket_connect(f"/ws/events/{client_event.id}") as ws:
        ws.receive_json()
        ws.send_text("{not json")
        error = ws.receive_json()
        assert error["type"] == "error" and error["data"]["code"] == "bad_request"

        ws.send_json({"type": "card.move", "data": {"card_id": "c", "x": "far"}, "ref": "r2"})
        error = ws.receive_json()
        assert error["data"]["code"] == "bad_request" and error["data"]["ref"] == "r2"
        assert "x" in error["data"]["message"] and "y" in error["data"]["message"]

        ws.send_json({"type": "card.move", "data": {"card_id": "c", "x": float("nan"), "y": 0}})
        assert ws.receive_json()["data"]["code"] == "bad_request"

        ws.send_json({"type": "nope", "ref": 3})
        assert ws.receive_json()["data"] == {"message": "Unknown message type 'nope'", "code": "unknown_type", "ref": 3}

        ws.send_json(["not", "an", "object"])
        assert ws.receive_json()["data"]["code"] == "bad_request"

        ws.send_bytes(b"\x00\x01")
        assert ws.receive_json()["data"]["code"] == "bad_request"

        ws.send_json({"type": "ping", "ref": "still-alive"})
        assert ws.receive_json()["data"]["ref"] == "still-alive"


# --- presence --------------------------------------------------------------------


def test_presence_cursor_selection_and_snapshot(client, client_event):
    url = f"/ws/events/{client_event.id}"
    with client.websocket_connect(url) as first:
        me = first.receive_json()["data"]
        first.send_json({"type": "presence.cursor", "data": {"x": 1, "y": 2}})
        cursor = first.receive_json()
        assert cursor["type"] == ev.BOARD_PRESENCE
        assert cursor["data"]["client_id"] == me["client_id"] and cursor["data"]["cursor"] == {"x": 1.0, "y": 2.0}
        assert cursor["data"]["user"] == me["user"] and cursor["data"]["color"].startswith("#")

        first.send_json({"type": "presence.cursor", "data": {"x": 1, "y": 2}})  # unchanged: nothing published
        first.send_json({"type": "presence.selection", "data": {"card_ids": ["card_a"]}, "ref": "s"})
        got = _receive_types(first, ev.BOARD_PRESENCE, "ack")
        selection = got[ev.BOARD_PRESENCE]
        assert selection["data"]["selection"] == ["card_a"] and selection["data"]["cursor"] == {"x": 1.0, "y": 2.0}
        assert got["ack"] == {"type": "ack", "data": {"ref": "s"}}

        with client.websocket_connect(url) as second:
            hello = second.receive_json()["data"]
            others = [p for p in hello["presence"] if p["client_id"] != hello["client_id"]]
            assert len(others) == 1 and others[0]["selection"] == ["card_a"]
            assert others[0]["cursor"] == {"x": 1.0, "y": 2.0}
            mine = next(p for p in hello["presence"] if p["client_id"] == hello["client_id"])
            assert mine["color"] != others[0]["color"]

            joined = first.receive_json()
            assert joined["type"] == ev.BOARD_PRESENCE and joined["data"]["client_id"] == hello["client_id"]
            assert joined["data"]["cursor"] is None and joined["data"]["selection"] == []

        left = first.receive_json()
        assert left["type"] == ev.BOARD_PRESENCE and left["data"] == {"client_id": hello["client_id"], "left": True}
        assert all(set(p) == {"client_id", "user", "color", "cursor", "selection"} for p in hello["presence"])
