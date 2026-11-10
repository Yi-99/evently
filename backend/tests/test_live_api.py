"""Live session REST API (/api/v1)."""

from __future__ import annotations

from functools import partial

from fastapi.testclient import TestClient

from app.core.security import create_access_token
from app.live import service
from app.models import Event, User
from app.transcription.base import TranscriptEvent
from tests.helpers import create_event
from tests.test_live_service import make_deck


def create_session(client: TestClient, event_id: str, **body) -> dict:
    response = client.post(f"/api/v1/events/{event_id}/live-sessions", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_list_get_patch_session(client, client_event):
    ctx = client.app.state.ctx
    deck = client.portal.call(make_deck, ctx, client_event.id)

    created = create_session(client, client_event.id, presentation_id=deck.id, settings={"auto_advance": "off"})
    session = created["session"]
    assert session["status"] == "scheduled" and session["presentation_id"] == deck.id and session["title"] == "Deck"
    assert session["settings"]["auto_advance"] == "off" and session["settings"]["language"] == "en"
    assert created["host_token"]
    sid = session["id"]

    listed = client.get(f"/api/v1/events/{client_event.id}/live-sessions")
    assert listed.status_code == 200 and [s["id"] for s in listed.json()] == [sid]

    snapshot = client.get(f"/api/v1/live-sessions/{sid}").json()
    assert snapshot["session"]["id"] == sid and snapshot["slide_count"] == 3
    assert [p["role"] for p in snapshot["participants"]] == ["host"]
    assert [s["position"] for s in snapshot["slides"]] == [0, 1, 2] and snapshot["recent_transcript"] == []

    patched = client.patch(f"/api/v1/live-sessions/{sid}", json={"title": "Kickoff", "settings": {"language": "de"}})
    assert patched.status_code == 200, patched.text
    assert patched.json()["title"] == "Kickoff"
    assert patched.json()["settings"] == {**session["settings"], "language": "de"}

    assert client.patch(f"/api/v1/live-sessions/{sid}", json={"settings": {"auto_advance": "maybe"}}).status_code == 422
    assert client.patch(f"/api/v1/live-sessions/{sid}", json={"presentation_id": "prs_nope"}).status_code == 404
    detached = client.patch(f"/api/v1/live-sessions/{sid}", json={"presentation_id": None})
    assert detached.status_code == 200 and detached.json()["presentation_id"] is None

    unknown_deck = client.post(f"/api/v1/events/{client_event.id}/live-sessions", json={"presentation_id": "prs_x"})
    assert unknown_deck.status_code == 404
    assert client.get("/api/v1/live-sessions/ses_missing").status_code == 404
    assert client.post("/api/v1/events/evt_missing/live-sessions", json={}).status_code == 404


def test_owner_routes_are_hidden_from_other_users(client, client_event):
    ctx = client.app.state.ctx
    sid = create_session(client, client_event.id)["session"]["id"]

    async def stranger_token() -> str:
        async with ctx.db() as db:
            user = User(email="stranger@example.com", name="Stranger")
            db.add(user)
            await db.commit()
        return create_access_token(ctx.settings, user.id)

    headers = {"Authorization": f"Bearer {client.portal.call(stranger_token)}"}
    assert client.get(f"/api/v1/live-sessions/{sid}", headers=headers).status_code == 404
    assert client.post(f"/api/v1/live-sessions/{sid}/start", headers=headers).status_code == 404
    assert client.get(f"/api/v1/live-sessions/{sid}/state", headers=headers).status_code == 401
    assert client.get(f"/api/v1/events/{client_event.id}/live-sessions", headers=headers).status_code == 404


def test_lifecycle_slide_and_participants(client, client_event):
    ctx = client.app.state.ctx
    deck = client.portal.call(make_deck, ctx, client_event.id)
    sid = create_session(client, client_event.id, presentation_id=deck.id)["session"]["id"]

    started = client.post(f"/api/v1/live-sessions/{sid}/start")
    assert started.status_code == 200 and started.json()["status"] == "live"

    moved = client.post(f"/api/v1/live-sessions/{sid}/slide", json={"index": 7})
    assert moved.status_code == 200 and moved.json()["current_slide_index"] == 2  # clamped
    assert client.post(f"/api/v1/live-sessions/{sid}/slide", json={"index": "x"}).status_code == 422

    speaker = client.post(
        f"/api/v1/live-sessions/{sid}/participants", json={"display_name": "Grace", "role": "speaker"}
    )
    assert speaker.status_code == 201, speaker.text
    assert speaker.json()["participant"]["role"] == "speaker" and speaker.json()["token"]
    bad_role = client.post(f"/api/v1/live-sessions/{sid}/participants", json={"display_name": "X", "role": "boss"})
    assert bad_role.status_code == 422

    ended = client.post(f"/api/v1/live-sessions/{sid}/end")
    assert ended.status_code == 200 and ended.json()["status"] == "ended"
    assert client.post(f"/api/v1/live-sessions/{sid}/start").status_code == 409
    assert client.post(f"/api/v1/live-sessions/{sid}/participants", json={"display_name": "Late"}).status_code == 409


def test_join_by_code_is_public_and_case_insensitive(client, client_event):
    created = create_session(client, client_event.id)
    code = created["session"]["join_code"]

    joined = client.post("/api/v1/live-sessions/join", json={"join_code": code.lower(), "display_name": " Ada "})
    assert joined.status_code == 200, joined.text
    body = joined.json()
    assert body["participant"]["role"] == "listener" and body["participant"]["display_name"] == "Ada"
    assert body["session"]["id"] == created["session"]["id"] and body["presentation_id"] is None
    assert body["token"]

    assert (
        client.post("/api/v1/live-sessions/join", json={"join_code": "ZZZZZZ", "display_name": "Bob"}).status_code
        == 404
    )
    assert client.post("/api/v1/live-sessions/join", json={"join_code": code, "display_name": ""}).status_code == 422

    client.post(f"/api/v1/live-sessions/{created['session']['id']}/end")
    assert client.post("/api/v1/live-sessions/join", json={"join_code": code, "display_name": "Bob"}).status_code == 410


def test_state_and_transcript_for_participants(client, client_event):
    ctx = client.app.state.ctx
    created = create_session(client, client_event.id)
    sid, host_token = created["session"]["id"], created["host_token"]
    client.post(f"/api/v1/live-sessions/{sid}/start")
    for text in ("first", "second", "third"):
        client.portal.call(service.ingest_transcript, ctx, sid, TranscriptEvent(text=text, is_final=True))

    listener_token = client.post(
        "/api/v1/live-sessions/join", json={"join_code": created["session"]["join_code"], "display_name": "Ada"}
    ).json()["token"]

    # Owner without a token (auth optional), host participant token, listener token.
    for token in (None, host_token, listener_token):
        params = {"token": token} if token else {}
        state = client.get(f"/api/v1/live-sessions/{sid}/state", params=params)
        assert state.status_code == 200, state.text
        assert [s["text"] for s in state.json()["recent_transcript"]] == ["first", "second", "third"]
        transcript = client.get(f"/api/v1/live-sessions/{sid}/transcript", params={**params, "after_seq": 1})
        assert transcript.status_code == 200 and [s["seq"] for s in transcript.json()] == [2, 3]

    page = client.get(f"/api/v1/live-sessions/{sid}/transcript", params={"token": host_token, "limit": 1})
    assert [s["seq"] for s in page.json()] == [1]

    # Hide the transcript from listeners: the listener's view empties, the host still sees it.
    client.patch(f"/api/v1/live-sessions/{sid}", json={"settings": {"show_transcript_to_listeners": False}})
    hidden = client.get(f"/api/v1/live-sessions/{sid}/state", params={"token": listener_token})
    assert hidden.status_code == 200 and hidden.json()["recent_transcript"] == []
    assert client.get(f"/api/v1/live-sessions/{sid}/transcript", params={"token": listener_token}).status_code == 403
    assert client.get(f"/api/v1/live-sessions/{sid}/transcript", params={"token": host_token}).status_code == 200

    # Rejected tokens: garbage, a token for another session, and the header form of a bad token.
    other = create_session(client, client_event.id)
    assert client.get(f"/api/v1/live-sessions/{sid}/state", params={"token": "garbage"}).status_code == 401
    assert client.get(f"/api/v1/live-sessions/{sid}/state", params={"token": other["host_token"]}).status_code == 401
    as_header = client.get(f"/api/v1/live-sessions/{sid}/state", headers={"Authorization": f"Bearer {host_token}"})
    assert as_header.status_code == 200
    assert client.get("/api/v1/live-sessions/ses_missing/state", params={"token": host_token}).status_code == 404


def test_host_session_from_a_foreign_event_is_not_visible(client, client_event):
    ctx = client.app.state.ctx
    foreign_event: Event = client.portal.call(partial(create_event, ctx, "Foreign"))
    foreign_deck = client.portal.call(make_deck, ctx, foreign_event.id)
    # Same owner (the dev user), so the event is visible, but a deck must belong to the session's event.
    response = client.post(f"/api/v1/events/{client_event.id}/live-sessions", json={"presentation_id": foreign_deck.id})
    assert response.status_code == 404
