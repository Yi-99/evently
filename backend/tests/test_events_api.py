"""Events API: CRUD, requirements merge, ownership, delete cascade and the snapshot."""

import asyncio
from collections.abc import Iterator
from functools import partial

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.ids import new_join_code
from app.main import create_app
from app.models import AgentRun, Board, Card, Edge, LiveSession, Presentation, Slide
from app.services import board

API = "/api/v1"


@pytest.fixture
def strict_client(settings, llm) -> Iterator[TestClient]:
    """A client for an app where every request needs a token."""
    app = create_app(settings.model_copy(update={"auth_required": True}), llm=llm)
    with TestClient(app) as test_client:
        yield test_client


def register(client: TestClient, email: str) -> dict[str, str]:
    """Register a user and return the Authorization headers for them."""
    res = client.post(f"{API}/auth/register", json={"email": email, "password": "correct-horse"})
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def create(client: TestClient, **body) -> dict:
    res = client.post(f"{API}/events", json={"name": "Offsite", **body})
    assert res.status_code == 201, res.text
    return res.json()


def count(client: TestClient, model, *where) -> int:
    async def run() -> int:
        async with client.app.state.ctx.db() as session:
            return (await session.execute(select(func.count()).select_from(model).where(*where))).scalar_one()

    return client.portal.call(run)


def add_rows(client: TestClient, *rows) -> None:
    async def run() -> None:
        async with client.app.state.ctx.db() as session:
            session.add_all(rows)
            await session.commit()

    client.portal.call(run)


# --- CRUD ------------------------------------------------------------------------


def test_create_event_returns_it_and_creates_its_board(client):
    res = client.post(f"{API}/events", json={"name": "  Spring offsite  ", "description": "Team of 40"})
    assert res.status_code == 201
    event = res.json()
    assert event["id"].startswith("evt_")
    assert event["name"] == "Spring offsite" and event["description"] == "Team of 40"
    assert event["requirements"] == {} and event["status"] == "planning"
    assert event["created_at"] and event["updated_at"]
    assert count(client, Board, Board.event_id == event["id"]) == 1


@pytest.mark.parametrize("body", [{}, {"name": "   "}, {"name": "x" * 201}])
def test_create_event_requires_a_usable_name(client, body):
    assert client.post(f"{API}/events", json=body).status_code == 422


def test_list_events_is_newest_first(client):
    first, second = create(client, name="First"), create(client, name="Second")
    res = client.get(f"{API}/events")
    assert res.status_code == 200
    assert [e["id"] for e in res.json()] == [second["id"], first["id"]]


def test_get_event(client):
    event = create(client)
    assert client.get(f"{API}/events/{event['id']}").json() == event
    assert client.get(f"{API}/events/evt_missing").status_code == 404


def test_patch_updates_only_the_given_fields(client):
    event = create(client, description="before")
    res = client.patch(f"{API}/events/{event['id']}", json={"name": "Renamed", "status": "confirmed"})
    assert res.status_code == 200
    patched = res.json()
    assert patched["name"] == "Renamed" and patched["status"] == "confirmed"
    assert patched["description"] == "before"
    assert patched["updated_at"] > event["updated_at"] and patched["created_at"] == event["created_at"]
    assert client.get(f"{API}/events/{event['id']}").json() == patched


@pytest.mark.parametrize("body", [{"status": "cancelled"}, {"name": ""}])
def test_patch_rejects_invalid_fields(client, body):
    event = create(client)
    assert client.patch(f"{API}/events/{event['id']}", json=body).status_code == 422
    assert client.get(f"{API}/events/{event['id']}").json() == event


def test_delete_event_removes_it_and_everything_under_it(client):
    event = create(client)
    ctx = client.app.state.ctx
    client.portal.call(partial(board.create_card, ctx, event_id=event["id"], kind="note", title="N"))
    assert count(client, Card, Card.event_id == event["id"]) == 1

    assert client.delete(f"{API}/events/{event['id']}").status_code == 204
    assert client.get(f"{API}/events/{event['id']}").status_code == 404
    assert client.get(f"{API}/events").json() == []
    assert count(client, Card, Card.event_id == event["id"]) == 0
    assert count(client, Board, Board.event_id == event["id"]) == 0
    assert client.delete(f"{API}/events/{event['id']}").status_code == 404


def test_delete_event_cancels_its_running_agents(client):
    event = create(client)
    ctx = client.app.state.ctx

    async def agent(run):
        await asyncio.sleep(30)

    run = client.portal.call(
        partial(ctx.runtime.spawn, event_id=event["id"], kind="venue_research", title="t", fn=agent)
    )
    assert ctx.runtime.is_active(run.id)

    assert client.delete(f"{API}/events/{event['id']}").status_code == 204
    assert not ctx.runtime.is_active(run.id)
    assert count(client, AgentRun, AgentRun.event_id == event["id"]) == 0


# --- requirements ----------------------------------------------------------------


def test_create_stores_only_the_requirements_that_were_given(client):
    event = create(client, requirements={"location": "Lisbon", "headcount": 120, "budget": None})
    # No VenueCriteria defaults (radius, setting, …) and no nulls are stored.
    assert event["requirements"] == {"location": "Lisbon", "headcount": 120}


def test_patch_merges_requirements_key_by_key(client):
    event = create(client, requirements={"location": "Lisbon", "headcount": 120, "budget": "under $10k"})
    url = f"{API}/events/{event['id']}"

    merged = client.patch(url, json={"requirements": {"headcount": 150, "setting": "outdoor"}}).json()
    assert merged["requirements"] == {
        "location": "Lisbon",
        "headcount": 150,
        "budget": "under $10k",
        "setting": "outdoor",
    }

    removed = client.patch(url, json={"requirements": {"budget": None, "notes": None}}).json()
    assert removed["requirements"] == {"location": "Lisbon", "headcount": 150, "setting": "outdoor"}

    # Patching other fields, or an empty requirements object, leaves requirements alone.
    assert client.patch(url, json={"name": "Renamed"}).json()["requirements"] == removed["requirements"]
    assert client.patch(url, json={"requirements": {}}).json()["requirements"] == removed["requirements"]
    assert client.get(url).json()["requirements"] == removed["requirements"]


def test_requirements_are_stored_as_json_safe_values(client):
    event = create(client)
    requirements = {
        "event_date": "2027-05-14",
        "start_time": "18:30",
        "headcount": "80",
        "parking_required": False,
        "must_haves": ["stage", "wifi"],
    }
    res = client.patch(f"{API}/events/{event['id']}", json={"requirements": requirements})
    assert res.status_code == 200
    assert res.json()["requirements"] == {
        "event_date": "2027-05-14",
        "start_time": "18:30:00",
        "headcount": 80,
        "parking_required": False,
        "must_haves": ["stage", "wifi"],
    }


@pytest.mark.parametrize(
    "requirements",
    [
        {"venue_vibe": "cosy"},  # not a VenueCriteria field
        {"headcount": 0},  # VenueCriteria: ge=1
        {"radius_km": 500},  # VenueCriteria: le=200
        {"setting": "underwater"},
        {"event_date": "next friday"},
    ],
)
def test_invalid_requirements_are_rejected_and_nothing_is_stored(client, requirements):
    event = create(client, requirements={"location": "Lisbon"})
    res = client.patch(f"{API}/events/{event['id']}", json={"requirements": {**requirements, "location": "Porto"}})
    assert res.status_code == 422
    assert client.get(f"{API}/events/{event['id']}").json()["requirements"] == {"location": "Lisbon"}
    assert client.post(f"{API}/events", json={"name": "E", "requirements": requirements}).status_code == 422


# --- ownership -------------------------------------------------------------------


def test_another_users_event_is_a_404(strict_client):
    ada, grace = register(strict_client, "ada@example.com"), register(strict_client, "grace@example.com")
    res = strict_client.post(f"{API}/events", json={"name": "Ada's launch"}, headers=ada)
    assert res.status_code == 201
    url = f"{API}/events/{res.json()['id']}"

    assert strict_client.get(url, headers=grace).status_code == 404
    assert strict_client.get(f"{url}/snapshot", headers=grace).status_code == 404
    assert strict_client.patch(url, json={"name": "Mine now"}, headers=grace).status_code == 404
    assert strict_client.delete(url, headers=grace).status_code == 404
    assert strict_client.get(f"{API}/events", headers=grace).json() == []

    assert strict_client.get(url, headers=ada).json()["name"] == "Ada's launch"
    assert [e["id"] for e in strict_client.get(f"{API}/events", headers=ada).json()] == [res.json()["id"]]


def test_events_require_a_token_when_auth_is_required(strict_client):
    ada = register(strict_client, "ada@example.com")
    event_id = strict_client.post(f"{API}/events", json={"name": "E"}, headers=ada).json()["id"]
    for method, path in [
        ("GET", "/events"),
        ("POST", "/events"),
        ("GET", f"/events/{event_id}"),
        ("PATCH", f"/events/{event_id}"),
        ("DELETE", f"/events/{event_id}"),
        ("GET", f"/events/{event_id}/snapshot"),
    ]:
        assert strict_client.request(method, f"{API}{path}", json={"name": "x"}).status_code == 401, (method, path)
    assert strict_client.get(f"{API}/events/{event_id}", headers=ada).status_code == 200


# --- snapshot --------------------------------------------------------------------


def test_snapshot_of_a_new_event_is_empty_but_complete(client):
    event = create(client)
    res = client.get(f"{API}/events/{event['id']}/snapshot")
    assert res.status_code == 200
    snapshot = res.json()
    assert set(snapshot) == {
        "event",
        "board",
        "cards",
        "edges",
        "active_agent_runs",
        "presentations",
        "live_sessions",
    }
    assert snapshot["event"] == event
    assert snapshot["board"]["event_id"] == event["id"]
    assert set(snapshot["board"]) == {"id", "event_id", "name", "viewport", "created_at", "updated_at"}
    assert all(snapshot[key] == [] for key in ("cards", "edges", "active_agent_runs", "presentations", "live_sessions"))


def test_snapshot_has_a_board_even_if_the_event_was_created_without_one(client, client_event):
    first = client.get(f"{API}/events/{client_event.id}/snapshot").json()
    second = client.get(f"{API}/events/{client_event.id}/snapshot").json()
    assert first["board"]["id"].startswith("brd_") and second["board"] == first["board"]


def test_snapshot_returns_the_workspace_state(client, client_event):
    ctx = client.app.state.ctx
    event_id = client_event.id
    venue = client.portal.call(partial(board.create_card, ctx, event_id=event_id, kind="venue", title="Venue"))
    note = client.portal.call(partial(board.create_card, ctx, event_id=event_id, kind="note", title="Note"))
    add_rows(
        client,
        Edge(board_id=venue.board_id, event_id=event_id, source_card_id=venue.id, target_card_id=note.id, label="see"),
        AgentRun(id="run_queued", event_id=event_id, kind="venue_search", status="queued"),
        AgentRun(id="run_running", event_id=event_id, kind="venue_research", status="running", card_id=venue.id),
        AgentRun(id="run_done", event_id=event_id, kind="venue_scout", status="succeeded"),
        AgentRun(id="run_failed", event_id=event_id, kind="venue_scout", status="failed"),
        Presentation(id="prs_deck", event_id=event_id, title="Pitch", status="ready"),
    )
    add_rows(  # separately: without relationships the unit of work does not order inserts by foreign key
        client,
        Slide(presentation_id="prs_deck", title="Intro", html="<section>SLIDE-HTML-MARKER</section>"),
        LiveSession(
            event_id=event_id, presentation_id="prs_deck", host_user_id=client_event.owner_id, join_code=new_join_code()
        ),
    )
    client.post(f"{API}/events", json={"name": "Someone else's cards must not leak in"})

    res = client.get(f"{API}/events/{event_id}/snapshot")
    assert res.status_code == 200
    snapshot = res.json()

    assert snapshot["event"]["id"] == event_id
    assert snapshot["board"]["id"] == venue.board_id
    assert [c["id"] for c in snapshot["cards"]] == [venue.id, note.id]
    assert snapshot["cards"][0]["kind"] == "venue" and "locked_fields" in snapshot["cards"][0]
    assert [(e["source_card_id"], e["target_card_id"], e["label"]) for e in snapshot["edges"]] == [
        (venue.id, note.id, "see")
    ]
    assert {r["id"] for r in snapshot["active_agent_runs"]} == {"run_queued", "run_running"}
    assert [p["id"] for p in snapshot["presentations"]] == ["prs_deck"]
    assert "slides" not in snapshot["presentations"][0]
    assert [s["presentation_id"] for s in snapshot["live_sessions"]] == ["prs_deck"]
    assert snapshot["live_sessions"][0]["status"] == "scheduled" and snapshot["live_sessions"][0]["join_code"]
    assert "SLIDE-HTML-MARKER" not in res.text
