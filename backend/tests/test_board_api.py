"""Board REST API: routes, ownership, locking, conflicts and published envelopes."""

from functools import partial

import pytest

from app.models import Event, User
from app.realtime import events as ev
from app.services import board, layout
from tests.helpers import create_event, published

API = "/api/v1"


@pytest.fixture
def ctx(client):
    return client.app.state.ctx


@pytest.fixture
def run(client):
    """Run a coroutine function on the app's loop: `run(fn, *args, **kwargs)`."""

    def call(fn, *args, **kwargs):
        return client.portal.call(partial(fn, *args, **kwargs))

    return call


@pytest.fixture
def foreign_event(ctx, run) -> Event:
    """An event owned by somebody other than the dev user the test client acts as."""

    async def make() -> Event:
        async with ctx.db() as session:
            owner = User(email="someone-else@example.com", name="Other")
            session.add(owner)
            await session.flush()
            event = Event(owner_id=owner.id, name="Not yours")
            session.add(event)
            await session.commit()
            return event

    return run(make)


def board_events(ctx, event_id: str, since: int = 0) -> list[tuple[str, dict]]:
    return [(e.type, e.data) for e in published(ctx, event_id, "board.")][since:]


def new_card(client, event_id: str, **body) -> dict:
    body.setdefault("title", "Card")
    response = client.post(f"{API}/events/{event_id}/cards", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def new_edge(client, event_id: str, source: str, target: str, **body) -> dict:
    response = client.post(
        f"{API}/events/{event_id}/edges", json={"source_card_id": source, "target_card_id": target, **body}
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- board -------------------------------------------------------------------------


def test_get_board_returns_snapshot_and_creates_the_board_lazily(client, client_event):
    empty = client.get(f"{API}/events/{client_event.id}/board")
    assert empty.status_code == 200
    body = empty.json()
    assert body["board"]["event_id"] == client_event.id and body["board"]["name"] == "Board"
    assert body["board"]["viewport"] == {} and (body["cards"], body["edges"]) == ([], [])

    a = new_card(client, client_event.id, title="A")
    b = new_card(client, client_event.id, title="B")
    edge = new_edge(client, client_event.id, a["id"], b["id"])

    body = client.get(f"{API}/events/{client_event.id}/board").json()
    assert body["board"]["id"] == a["board_id"] == edge["board_id"]
    assert [c["id"] for c in body["cards"]] == [a["id"], b["id"]]
    assert body["edges"] == [edge]


def test_patch_board_name_and_viewport(client, client_event):
    url = f"{API}/events/{client_event.id}/board"
    renamed = client.patch(url, json={"name": "Venue shortlist"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "Venue shortlist"

    zoomed = client.patch(url, json={"viewport": {"x": 120, "y": -40, "zoom": 0.5}})
    assert zoomed.json()["viewport"] == {"x": 120.0, "y": -40.0, "zoom": 0.5}
    assert zoomed.json()["name"] == "Venue shortlist"

    assert client.patch(url, json={"name": ""}).status_code == 422
    assert client.patch(url, json={"viewport": {"zoom": 0}}).status_code == 422
    assert client.get(url).json()["board"]["viewport"] == {"x": 120.0, "y": -40.0, "zoom": 0.5}


# --- cards -------------------------------------------------------------------------


def test_create_card_defaults_and_publishes(client, client_event, ctx):
    card = new_card(client, client_event.id, title="Remember parking")
    assert card["kind"] == "note" and card["created_by"] == "user" and card["status"] == "ready"
    assert (card["x"], card["y"]) == (0.0, 0.0)
    assert (card["width"], card["height"]) == (layout.CARD_W, layout.CARD_H)
    assert card["data"] == {} and card["locked_fields"] == [] and card["version"] == 1

    placed = new_card(
        client, client_event.id, kind="venue", title="Hall", data={"capacity": 200}, x=900, y=10, width=200, height=100
    )
    assert (placed["kind"], placed["x"], placed["y"], placed["width"], placed["height"]) == ("venue", 900, 10, 200, 100)
    assert placed["data"] == {"capacity": 200}

    second_auto = new_card(client, client_event.id)
    assert (second_auto["x"], second_auto["y"]) == (layout.CARD_W + layout.GAP, 0.0)

    assert board_events(ctx, client_event.id) == [
        (ev.BOARD_CARD_CREATED, {"card": card}),
        (ev.BOARD_CARD_CREATED, {"card": placed}),
        (ev.BOARD_CARD_CREATED, {"card": second_auto}),
    ]


def test_create_card_validation(client, client_event):
    url = f"{API}/events/{client_event.id}/cards"
    assert client.post(url, json={}).status_code == 422  # title is required
    assert client.post(url, json={"title": "x", "kind": "wormhole"}).status_code == 422
    assert client.post(url, json={"title": "x", "width": 0}).status_code == 422
    assert client.post(url, json={"title": "x" * 301}).status_code == 422


def test_get_card(client, client_event):
    card = new_card(client, client_event.id, title="A")
    assert client.get(f"{API}/cards/{card['id']}").json() == card
    assert client.get(f"{API}/cards/card_missing").status_code == 404


def test_patch_card_locks_edited_data_against_agents_and_unlock_releases(client, client_event, ctx, run):
    card = run(
        board.create_card, ctx, event_id=client_event.id, kind="venue", title="V", data={"summary": "s", "score": 1}
    )
    url = f"{API}/cards/{card.id}"

    edited = client.patch(url, json={"title": "My venue", "data": {"summary": "fixed by me", "score": None}, "x": 50})
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert body["title"] == "My venue" and body["x"] == 50.0 and body["version"] == 2
    assert body["data"] == {"summary": "fixed by me"}
    assert body["locked_fields"] == ["summary", "score"]

    # An agent re-run cannot overwrite the user's keys but may add others.
    after_agent = run(
        board.patch_card, ctx, card.id, by="agent", data_patch={"summary": "agent", "score": 9, "pros": ["a"]}
    )
    assert after_agent.data == {"summary": "fixed by me", "pros": ["a"]}

    unlocked = client.patch(url, json={"unlock_fields": ["summary"]})
    assert unlocked.json()["locked_fields"] == ["score"]
    after_agent = run(board.patch_card, ctx, card.id, by="agent", data_patch={"summary": "agent", "score": 9})
    assert after_agent.data == {"summary": "agent", "pros": ["a"]}

    updates = [d for t, d in board_events(ctx, client_event.id) if t == ev.BOARD_CARD_UPDATED]
    assert [(d["by"], d["changed"]) for d in updates] == [
        ("user", ["title", "x", "locked_fields", "data"]),
        ("agent", ["data"]),
        ("user", ["locked_fields"]),
        ("agent", ["data"]),
    ]
    assert updates[0]["card"] == body


def test_patch_card_with_stale_version_returns_409_with_current_card(client, client_event):
    card = new_card(client, client_event.id, title="A")
    url = f"{API}/cards/{card['id']}"
    assert client.patch(url, json={"title": "B", "expected_version": 1}).json()["version"] == 2

    stale = client.patch(url, json={"title": "C", "expected_version": 1})
    assert stale.status_code == 409
    assert stale.json()["card"] == client.get(url).json()
    assert stale.json()["card"]["title"] == "B" and "version" in stale.json()["detail"]

    assert client.patch(url, json={"title": "C", "expected_version": 2}).status_code == 200


def test_patch_card_noop_and_validation(client, client_event, ctx):
    card = new_card(client, client_event.id, title="A")
    url = f"{API}/cards/{card['id']}"
    before = len(board_events(ctx, client_event.id))

    assert client.patch(url, json={}).json() == card
    assert client.patch(url, json={"title": "A"}).json()["version"] == 1
    assert len(board_events(ctx, client_event.id)) == before

    assert client.patch(url, json={"height": -1}).status_code == 422
    assert client.patch(url, json={"data": "nope"}).status_code == 422
    assert client.patch(url, json={"z_index": "top"}).status_code == 422


def test_delete_card_cascades_edges_and_publishes(client, client_event, ctx):
    a = new_card(client, client_event.id, title="A")
    b = new_card(client, client_event.id, title="B")
    c = new_card(client, client_event.id, title="C")
    ab = new_edge(client, client_event.id, a["id"], b["id"])
    ca = new_edge(client, client_event.id, c["id"], a["id"])
    bc = new_edge(client, client_event.id, b["id"], c["id"])
    before = len(board_events(ctx, client_event.id))

    assert client.delete(f"{API}/cards/{a['id']}").status_code == 204
    assert client.get(f"{API}/cards/{a['id']}").status_code == 404
    assert client.delete(f"{API}/cards/{a['id']}").status_code == 404

    snapshot = client.get(f"{API}/events/{client_event.id}/board").json()
    assert [card["id"] for card in snapshot["cards"]] == [b["id"], c["id"]]
    assert snapshot["edges"] == [bc]
    assert board_events(ctx, client_event.id, before) == [
        (ev.BOARD_EDGE_DELETED, {"edge_id": ab["id"]}),
        (ev.BOARD_EDGE_DELETED, {"edge_id": ca["id"]}),
        (ev.BOARD_CARD_DELETED, {"card_id": a["id"]}),
    ]


def test_move_cards_in_bulk(client, client_event, ctx):
    a = new_card(client, client_event.id, title="A")
    b = new_card(client, client_event.id, title="B")
    before = len(board_events(ctx, client_event.id))

    moved = client.post(
        f"{API}/events/{client_event.id}/cards/move",
        json=[{"card_id": a["id"], "x": 100, "y": 200}, {"card_id": b["id"], "x": b["x"], "y": b["y"]}],
    )
    assert moved.status_code == 200, moved.text
    assert [(c["id"], c["x"], c["y"], c["version"]) for c in moved.json()] == [
        (a["id"], 100.0, 200.0, 2),
        (b["id"], b["x"], b["y"], 1),
    ]
    assert board_events(ctx, client_event.id, before) == [
        (ev.BOARD_CARD_UPDATED, {"card": moved.json()[0], "changed": ["x", "y"], "by": "user"})
    ]

    missing = client.post(
        f"{API}/events/{client_event.id}/cards/move",
        json=[{"card_id": a["id"], "x": 0, "y": 0}, {"card_id": "card_missing", "x": 0, "y": 0}],
    )
    assert missing.status_code == 404
    assert client.get(f"{API}/cards/{a['id']}").json()["x"] == 100.0  # nothing applied

    assert (
        client.post(f"{API}/events/{client_event.id}/cards/move", json=[{"card_id": a["id"], "x": 1}]).status_code
        == 422
    )
    assert client.post(f"{API}/events/{client_event.id}/cards/move", json=[]).json() == []


def test_auto_layout_route_returns_tidied_cards(client, client_event, ctx):
    unranked = new_card(client, client_event.id, kind="venue", title="U", x=1000, y=1000)
    ranked = new_card(client, client_event.id, kind="venue", title="R", data={"rank": 1}, x=-5, y=-5)
    note = new_card(client, client_event.id, title="N", x=3, y=3)
    before = len(board_events(ctx, client_event.id))

    response = client.post(f"{API}/events/{client_event.id}/board/auto-layout")
    assert response.status_code == 200, response.text
    step = layout.CARD_W + layout.GAP
    assert {c["id"]: (c["x"], c["y"]) for c in response.json()} == {
        ranked["id"]: (0.0, 0.0),
        unranked["id"]: (step, 0.0),
        note["id"]: (2 * step, 0.0),
    }
    assert [c["id"] for c in response.json()] == [unranked["id"], ranked["id"], note["id"]]
    assert client.get(f"{API}/events/{client_event.id}/board").json()["cards"] == response.json()

    events = board_events(ctx, client_event.id, before)
    assert len(events) == 3 and all(t == ev.BOARD_CARD_UPDATED and d["by"] == "user" for t, d in events)


# --- edges -------------------------------------------------------------------------


def test_create_and_delete_edge(client, client_event, ctx):
    a = new_card(client, client_event.id, title="A")
    b = new_card(client, client_event.id, title="B")
    before = len(board_events(ctx, client_event.id))

    edge = new_edge(client, client_event.id, a["id"], b["id"], label="vs", data={"color": "red"})
    assert edge["source_card_id"] == a["id"] and edge["target_card_id"] == b["id"]
    assert (edge["label"], edge["data"], edge["event_id"]) == ("vs", {"color": "red"}, client_event.id)
    assert new_edge(client, client_event.id, b["id"], a["id"])["label"] == ""

    assert client.delete(f"{API}/edges/{edge['id']}").status_code == 204
    assert client.delete(f"{API}/edges/{edge['id']}").status_code == 404
    assert len(client.get(f"{API}/events/{client_event.id}/board").json()["edges"]) == 1

    events = board_events(ctx, client_event.id, before)
    assert events[0] == (ev.BOARD_EDGE_CREATED, {"edge": edge})
    assert [t for t, _ in events] == [ev.BOARD_EDGE_CREATED, ev.BOARD_EDGE_CREATED, ev.BOARD_EDGE_DELETED]
    assert events[-1][1] == {"edge_id": edge["id"]}


def test_create_edge_rejects_bad_endpoints(client, client_event, run, ctx):
    a = new_card(client, client_event.id, title="A")
    other_event = run(create_event, ctx, "Other")  # also mine, but a different board
    other = run(board.create_card, ctx, event_id=other_event.id, kind="note")
    url = f"{API}/events/{client_event.id}/edges"

    assert client.post(url, json={"source_card_id": a["id"], "target_card_id": "card_missing"}).status_code == 404
    assert client.post(url, json={"source_card_id": a["id"], "target_card_id": other.id}).status_code == 404
    assert client.post(url, json={"source_card_id": a["id"], "target_card_id": a["id"]}).status_code == 422
    assert client.post(url, json={"source_card_id": a["id"]}).status_code == 422
    assert client.get(f"{API}/events/{client_event.id}/board").json()["edges"] == []


# --- ownership ---------------------------------------------------------------------


def test_routes_hide_other_peoples_events_cards_and_edges(client, foreign_event, run, ctx):
    a = run(board.create_card, ctx, event_id=foreign_event.id, kind="venue", title="A")
    b = run(board.create_card, ctx, event_id=foreign_event.id, kind="venue", title="B")
    edge = run(board.create_edge, ctx, event_id=foreign_event.id, source_card_id=a.id, target_card_id=b.id)
    before = len(board_events(ctx, foreign_event.id))

    attempts = [
        client.get(f"{API}/events/{foreign_event.id}/board"),
        client.patch(f"{API}/events/{foreign_event.id}/board", json={"name": "Mine now"}),
        client.post(f"{API}/events/{foreign_event.id}/board/auto-layout"),
        client.post(f"{API}/events/{foreign_event.id}/cards", json={"title": "x"}),
        client.post(f"{API}/events/{foreign_event.id}/cards/move", json=[{"card_id": a.id, "x": 0, "y": 0}]),
        client.post(f"{API}/events/{foreign_event.id}/edges", json={"source_card_id": a.id, "target_card_id": b.id}),
        client.get(f"{API}/cards/{a.id}"),
        client.patch(f"{API}/cards/{a.id}", json={"title": "Mine now"}),
        client.delete(f"{API}/cards/{a.id}"),
        client.delete(f"{API}/edges/{edge.id}"),
    ]
    assert [r.status_code for r in attempts] == [404] * len(attempts)

    # Nothing changed and nothing was announced.
    assert run(board.get_card, ctx, a.id).title == "A"
    assert [e.id for e in run(board.list_edges, ctx, foreign_event.id)] == [edge.id]
    assert len(board_events(ctx, foreign_event.id)) == before


def test_own_event_cannot_move_or_link_a_stranger_card(client, client_event, foreign_event, run, ctx):
    mine = new_card(client, client_event.id, title="Mine")
    theirs = run(board.create_card, ctx, event_id=foreign_event.id, kind="venue", title="Theirs")

    move = client.post(f"{API}/events/{client_event.id}/cards/move", json=[{"card_id": theirs.id, "x": 1, "y": 1}])
    link = client.post(
        f"{API}/events/{client_event.id}/edges", json={"source_card_id": mine["id"], "target_card_id": theirs.id}
    )
    assert (move.status_code, link.status_code) == (404, 404)
    assert run(board.get_card, ctx, theirs.id).x == theirs.x
