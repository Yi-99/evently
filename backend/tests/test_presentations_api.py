"""Presentation API: CRUD, manual slide editing, agent prompts, rendering and access control."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agents import presentations as presentation_agents
from app.core.context import AppContext
from app.core.deps import get_dev_user
from app.core.ids import new_join_code
from app.core.security import create_access_token, create_participant_token
from app.main import create_app
from app.models import AgentRun, Event, LiveSession, Presentation, Slide, User
from app.realtime import events as ev
from app.schemas.presentation import PresentationBrief
from tests.helpers import create_event, published

API = "/api/v1"


# --- fixtures and helpers ---------------------------------------------------------------------


async def make_deck(ctx: AppContext, event_id: str, count: int = 3) -> tuple[Presentation, list[Slide]]:
    presentation = Presentation(event_id=event_id, title="Launch deck", status="ready")
    async with ctx.db() as session:
        session.add(presentation)
        await session.flush()
        slides = [
            Slide(
                presentation_id=presentation.id,
                position=i,
                title=f"Slide {i + 1}",
                html=f"<h1>Slide {i + 1}</h1>",
                notes=f"notes {i + 1}",
                keywords=[f"kw{i + 1}"],
                status="ready",
            )
            for i in range(count)
        ]
        session.add_all(slides)
        await session.commit()
    return presentation, slides


async def other_users_deck(ctx: AppContext) -> tuple[Presentation, list[Slide]]:
    """A deck that belongs to somebody else."""
    async with ctx.db() as session:
        other = User(email="other@example.com", name="Other")
        session.add(other)
        await session.flush()
        event = Event(owner_id=other.id, name="Theirs")
        session.add(event)
        await session.commit()
    return await make_deck(ctx, event.id, count=1)


async def make_live_session(ctx: AppContext, event_id: str, presentation_id: str, status: str = "live") -> LiveSession:
    user = await get_dev_user(ctx)
    live = LiveSession(
        event_id=event_id,
        presentation_id=presentation_id,
        host_user_id=user.id,
        join_code=new_join_code(),
        status=status,
    )
    async with ctx.db() as session:
        session.add(live)
        await session.commit()
    return live


async def db_slides(ctx: AppContext, presentation_id: str) -> list[Slide]:
    async with ctx.db() as session:
        rows = await session.execute(
            select(Slide).where(Slide.presentation_id == presentation_id).order_by(Slide.position)
        )
        return list(rows.scalars())


async def db_presentation(ctx: AppContext, presentation_id: str) -> Presentation | None:
    async with ctx.db() as session:
        return await session.get(Presentation, presentation_id)


@pytest.fixture
def ctx(client: TestClient) -> AppContext:
    """The HTTP app's context (the conftest `ctx` fixture is a different app)."""
    return client.app.state.ctx


@pytest.fixture
def deck(client: TestClient, ctx: AppContext, client_event: Event) -> tuple[Presentation, list[Slide]]:
    return client.portal.call(make_deck, ctx, client_event.id)


@pytest.fixture
def strict_client(settings, llm) -> Iterator[TestClient]:
    """An app where every request must authenticate (no implicit dev user)."""
    with TestClient(create_app(settings.model_copy(update={"auth_required": True}), llm=llm)) as test_client:
        yield test_client


def types(ctx: AppContext, event_id: str, prefix: str = "") -> list[str]:
    return [e.type for e in published(ctx, event_id, prefix)]


# --- build and prompt (agent entrypoints are monkeypatched) ----------------------------------


def test_create_presentation_starts_a_build(client, ctx, client_event, monkeypatch):
    calls = []

    async def fake_build(ctx_, *, event_id, brief, parent_run_id=None):
        calls.append((event_id, brief, parent_run_id))
        presentation = Presentation(event_id=event_id, title=brief.title or "Untitled deck", brief=brief.model_dump())
        run = AgentRun(event_id=event_id, kind="presentation_build", title="Build deck")
        async with ctx_.db() as session:
            session.add_all([presentation, run])
            await session.commit()
        return presentation, run

    monkeypatch.setattr(presentation_agents, "start_presentation_build", fake_build)
    body = {"prompt": "Pitch the shortlisted venues", "title": "Venue pitch", "slide_count": 5, "card_ids": ["c1"]}
    res = client.post(f"{API}/events/{client_event.id}/presentations", json=body)
    assert res.status_code == 202, res.text
    data = res.json()
    assert data["presentation"]["title"] == "Venue pitch" and data["presentation"]["status"] == "generating"
    assert data["run"]["kind"] == "presentation_build" and data["run"]["status"] == "queued"
    assert calls == [(client_event.id, PresentationBrief(**body), None)]

    listed = client.get(f"{API}/events/{client_event.id}/presentations")
    assert listed.status_code == 200 and [p["id"] for p in listed.json()] == [data["presentation"]["id"]]


def test_create_presentation_needs_the_llm(client, ctx, client_event, monkeypatch):
    monkeypatch.setattr(ctx.llm, "available", False)
    res = client.post(f"{API}/events/{client_event.id}/presentations", json={"prompt": "x"})
    assert res.status_code == 503 and "ANTHROPIC_API_KEY" in res.json()["detail"]


def test_create_presentation_validates_the_brief(client, client_event):
    assert client.post(f"{API}/events/{client_event.id}/presentations", json={}).status_code == 422
    url = f"{API}/events/{client_event.id}/presentations"
    assert client.post(url, json={"prompt": "x", "slide_count": 0}).status_code == 422


def test_prompt_routes_start_an_edit_run(client, ctx, deck, monkeypatch):
    presentation, slides = deck
    calls = []

    async def fake_edit(ctx_, *, presentation_id, instruction, slide_id=None, parent_run_id=None):
        calls.append((presentation_id, instruction, slide_id))
        run = AgentRun(event_id=presentation.event_id, kind="presentation_edit", title="Edit deck")
        async with ctx_.db() as session:
            session.add(run)
            await session.commit()
        return run

    monkeypatch.setattr(presentation_agents, "start_presentation_edit", fake_edit)
    res = client.post(f"{API}/presentations/{presentation.id}/prompt", json={"instruction": "Make it punchier"})
    assert res.status_code == 202 and res.json()["run"]["kind"] == "presentation_edit"
    res = client.post(f"{API}/slides/{slides[1].id}/prompt", json={"instruction": "Add a chart"})
    assert res.status_code == 202 and res.json()["run"]["kind"] == "presentation_edit"
    assert calls == [(presentation.id, "Make it punchier", None), (presentation.id, "Add a chart", slides[1].id)]

    assert client.post(f"{API}/presentations/{presentation.id}/prompt", json={"instruction": ""}).status_code == 422
    assert client.post(f"{API}/presentations/prs_missing/prompt", json={"instruction": "x"}).status_code == 404
    assert client.post(f"{API}/slides/sld_missing/prompt", json={"instruction": "x"}).status_code == 404


def test_prompt_routes_need_the_llm(client, ctx, deck, monkeypatch):
    presentation, slides = deck
    monkeypatch.setattr(ctx.llm, "available", False)
    assert client.post(f"{API}/presentations/{presentation.id}/prompt", json={"instruction": "x"}).status_code == 503
    assert client.post(f"{API}/slides/{slides[0].id}/prompt", json={"instruction": "x"}).status_code == 503


def test_prompt_route_maps_lookup_error_to_404(client, deck, monkeypatch):
    presentation, _ = deck

    async def missing(ctx_, **kwargs):
        raise LookupError("gone")

    monkeypatch.setattr(presentation_agents, "start_presentation_edit", missing)
    assert client.post(f"{API}/presentations/{presentation.id}/prompt", json={"instruction": "x"}).status_code == 404


# --- presentation CRUD ------------------------------------------------------------------------


def test_get_presentation_returns_slides_in_position_order(client, ctx, deck):
    presentation, slides = deck
    client.portal.call(reorder_in_db, ctx, presentation.id, [2, 0, 1])
    res = client.get(f"{API}/presentations/{presentation.id}")
    assert res.status_code == 200
    body = res.json()
    assert body["presentation"]["id"] == presentation.id and body["presentation"]["title"] == "Launch deck"
    assert [s["position"] for s in body["slides"]] == [0, 1, 2]
    assert [s["title"] for s in body["slides"]] == ["Slide 2", "Slide 3", "Slide 1"]
    assert set(body["slides"][0]) >= {"id", "html", "notes", "keywords", "outline", "status", "version", "updated_at"}


async def reorder_in_db(ctx: AppContext, presentation_id: str, positions: list[int]) -> None:
    async with ctx.db() as session:
        for slide, position in zip(await db_slides(ctx, presentation_id), positions, strict=True):
            (await session.get(Slide, slide.id)).position = position
        await session.commit()


def test_patch_presentation_merges_theme_and_bumps_version(client, ctx, deck, client_event):
    presentation, _ = deck
    client.portal.call(set_theme, ctx, presentation.id, {"background": "#000", "accent": "#f00", "name": "dark"})
    res = client.patch(f"{API}/presentations/{presentation.id}", json={"title": "Renamed", "theme": {"accent": "#0f0"}})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["title"] == "Renamed" and body["version"] == 2
    assert body["theme"] == {"background": "#000", "accent": "#0f0", "name": "dark"}
    envelopes = published(ctx, client_event.id, ev.PRESENTATION_UPDATED)
    assert len(envelopes) == 1 and envelopes[0].data["presentation"]["version"] == 2

    assert client.patch(f"{API}/presentations/{presentation.id}", json={"title": ""}).status_code == 422
    assert client.patch(f"{API}/presentations/{presentation.id}", json={"theme": {"accent": 1}}).status_code == 422


async def set_theme(ctx: AppContext, presentation_id: str, theme: dict) -> None:
    async with ctx.db() as session:
        (await session.get(Presentation, presentation_id)).theme = theme
        await session.commit()


def test_delete_presentation(client, ctx, deck, client_event):
    presentation, slides = deck
    assert client.delete(f"{API}/presentations/{presentation.id}").status_code == 204
    assert client.get(f"{API}/presentations/{presentation.id}").status_code == 404
    assert client.portal.call(db_presentation, ctx, presentation.id) is None
    assert client.portal.call(db_slides, ctx, presentation.id) == []  # cascaded
    deleted = published(ctx, client_event.id, ev.PRESENTATION_DELETED)
    assert [e.data for e in deleted] == [{"presentation_id": presentation.id}]
    assert client.delete(f"{API}/presentations/{presentation.id}").status_code == 404


# --- manual slide editing ---------------------------------------------------------------------


def test_create_slide_appends_sanitises_and_publishes(client, ctx, deck, client_event):
    presentation, slides = deck
    html = '<h1 onclick="x()">New</h1><script>alert(1)</script><img src="https://ok/x.png">'
    res = client.post(
        f"{API}/presentations/{presentation.id}/slides",
        json={"title": "New", "html": html, "notes": "say this", "keywords": ["new slide"]},
    )
    assert res.status_code == 201, res.text
    slide = res.json()
    assert slide["position"] == 3 and slide["status"] == "ready" and slide["keywords"] == ["new slide"]
    assert slide["html"] == '<h1>New</h1><img src="https://ok/x.png">'
    assert [s.position for s in client.portal.call(db_slides, ctx, presentation.id)] == [0, 1, 2, 3]
    assert client.portal.call(db_presentation, ctx, presentation.id).version == 2
    created = published(ctx, client_event.id, ev.SLIDE_CREATED)
    assert len(created) == 1 and created[0].data["slide"]["id"] == slide["id"]
    assert types(ctx, client_event.id, "presentation") == [ev.PRESENTATION_UPDATED]


def test_create_slide_at_position_shifts_the_others(client, ctx, deck):
    presentation, slides = deck
    res = client.post(f"{API}/presentations/{presentation.id}/slides", json={"position": 1, "title": "Inserted"})
    assert res.status_code == 201 and res.json()["position"] == 1
    ordered = client.portal.call(db_slides, ctx, presentation.id)
    assert [s.title for s in ordered] == ["Slide 1", "Inserted", "Slide 2", "Slide 3"]
    assert [s.position for s in ordered] == [0, 1, 2, 3]
    # Too-large positions append.
    res = client.post(f"{API}/presentations/{presentation.id}/slides", json={"position": 99, "title": "Last"})
    assert res.json()["position"] == 4
    assert client.post(f"{API}/presentations/{presentation.id}/slides", json={"position": -1}).status_code == 422


def test_patch_slide_sanitises_and_bumps_version(client, ctx, deck, client_event):
    presentation, slides = deck
    res = client.patch(
        f"{API}/slides/{slides[0].id}",
        json={"html": '<p style="color:red;background:url(javascript:1)">x</p><iframe src="https://e"></iframe>'},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["html"] == '<p style="color:red;background:none">x</p>'
    assert body["version"] == 2 and body["title"] == "Slide 1" and body["notes"] == "notes 1"
    res = client.patch(f"{API}/slides/{slides[0].id}", json={"title": "Renamed", "keywords": ["a", "b"], "notes": "n"})
    assert res.json()["version"] == 3 and res.json()["title"] == "Renamed" and res.json()["keywords"] == ["a", "b"]
    updated = published(ctx, client_event.id, ev.SLIDE_UPDATED)
    assert [e.data["slide"]["version"] for e in updated] == [2, 3]
    assert client.patch(f"{API}/slides/sld_missing", json={"title": "x"}).status_code == 404


def test_delete_slide_keeps_positions_contiguous(client, ctx, deck, client_event):
    presentation, slides = deck
    assert client.delete(f"{API}/slides/{slides[1].id}").status_code == 204
    remaining = client.portal.call(db_slides, ctx, presentation.id)
    assert [(s.title, s.position) for s in remaining] == [("Slide 1", 0), ("Slide 3", 1)]
    assert client.portal.call(db_presentation, ctx, presentation.id).version == 2
    deleted = published(ctx, client_event.id, ev.SLIDE_DELETED)
    assert [e.data for e in deleted] == [{"slide_id": slides[1].id, "presentation_id": presentation.id}]
    assert client.delete(f"{API}/slides/{slides[1].id}").status_code == 404


def test_reorder_slides(client, ctx, deck, client_event):
    presentation, slides = deck
    ids = [s.id for s in slides]
    new_order = [ids[2], ids[0], ids[1]]
    res = client.post(f"{API}/presentations/{presentation.id}/slides/reorder", json={"slide_ids": new_order})
    assert res.status_code == 200, res.text
    assert [s["id"] for s in res.json()] == new_order and [s["position"] for s in res.json()] == [0, 1, 2]
    assert [s.id for s in client.portal.call(db_slides, ctx, presentation.id)] == new_order
    reordered = published(ctx, client_event.id, ev.SLIDES_REORDERED)
    assert [e.data for e in reordered] == [{"presentation_id": presentation.id, "slide_ids": new_order}]
    assert client.portal.call(db_presentation, ctx, presentation.id).version == 2


@pytest.mark.parametrize(
    "mutate",
    [
        lambda ids: ids[:2],  # missing one
        lambda ids: ids + ["sld_extra"],  # unknown id
        lambda ids: [ids[0], ids[0], ids[1]],  # duplicate
        lambda ids: [],
        lambda ids: ids[:2] + ["sld_other"],  # same length, wrong member
    ],
)
def test_reorder_rejects_anything_but_a_permutation(client, ctx, deck, mutate):
    presentation, slides = deck
    ids = [s.id for s in slides]
    res = client.post(f"{API}/presentations/{presentation.id}/slides/reorder", json={"slide_ids": mutate(ids)})
    assert res.status_code == 422, res.text
    assert [s.id for s in client.portal.call(db_slides, ctx, presentation.id)] == ids  # untouched
    assert client.portal.call(db_presentation, ctx, presentation.id).version == 1


# --- rendering --------------------------------------------------------------------------------


def nonce_of(csp: str) -> str:
    return re.search(r"script-src 'nonce-([A-Za-z0-9_+/=-]+)'", csp).group(1)


def test_render_deck_sends_csp_with_fresh_nonce(client, deck):
    presentation, slides = deck
    first = client.get(f"{API}/presentations/{presentation.id}/render?slide=1")
    assert first.status_code == 200 and first.headers["content-type"].startswith("text/html")
    csp = first.headers["content-security-policy"]
    assert csp.startswith("default-src 'none'") and "img-src https: data:" in csp and "style-src 'unsafe-inline'" in csp
    nonce = nonce_of(csp)
    assert len(nonce) >= 16
    assert f'<script nonce="{nonce}">' in first.text and first.text.count("<script") == 1
    assert f"content=\"{csp.replace(chr(39), '&#x27;')}\"" in first.text  # the same policy in <meta>
    assert first.text.count('<iframe sandbox=""') == 3 and 'data-start="1"' in first.text
    assert first.headers["cache-control"] == "no-store"

    second = client.get(f"{API}/presentations/{presentation.id}/render")
    assert nonce_of(second.headers["content-security-policy"]) != nonce
    assert 'data-start="0"' in second.text and 'data-controls="true"' in second.text

    follower = client.get(f"{API}/presentations/{presentation.id}/render?controls=false")
    assert 'data-controls="false"' in follower.text and 'id="counter"' not in follower.text
    assert client.get(f"{API}/presentations/{presentation.id}/render?slide=-1").status_code == 422


def test_render_slide_document(client, ctx, deck):
    presentation, slides = deck
    client.portal.call(set_theme, ctx, presentation.id, {"background": "#101010"})
    res = client.get(f"{API}/slides/{slides[2].id}/render")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/html")
    csp = res.headers["content-security-policy"]
    assert csp.startswith("default-src 'none'") and "script-src" not in csp and csp.endswith("; sandbox")
    assert "<script" not in res.text and "<h1>Slide 3</h1>" in res.text and "--bg:#101010;" in res.text
    assert client.get(f"{API}/slides/sld_missing/render").status_code == 404


def test_export_is_a_downloadable_self_contained_deck(client, deck):
    presentation, slides = deck
    res = client.get(f"{API}/presentations/{presentation.id}/export")
    assert res.status_code == 200
    assert res.headers["content-disposition"] == 'attachment; filename="launch-deck.html"'
    assert res.text.startswith("<!doctype html>") and res.text.count('<iframe sandbox=""') == 3
    assert 'http-equiv="Content-Security-Policy"' in res.text  # policy travels with the file
    assert "content-security-policy" in res.headers


# --- access control ---------------------------------------------------------------------------


def test_everything_is_404_for_someone_elses_deck(client, ctx):
    presentation, slides = client.portal.call(other_users_deck, ctx)
    pid, sid = presentation.id, slides[0].id
    assert client.get(f"{API}/presentations/{pid}").status_code == 404
    assert client.patch(f"{API}/presentations/{pid}", json={"title": "x"}).status_code == 404
    assert client.delete(f"{API}/presentations/{pid}").status_code == 404
    assert client.post(f"{API}/presentations/{pid}/prompt", json={"instruction": "x"}).status_code == 404
    assert client.post(f"{API}/presentations/{pid}/slides", json={"title": "x"}).status_code == 404
    assert client.post(f"{API}/presentations/{pid}/slides/reorder", json={"slide_ids": [sid]}).status_code == 404
    assert client.get(f"{API}/presentations/{pid}/render").status_code == 404
    assert client.get(f"{API}/presentations/{pid}/export").status_code == 404
    assert client.get(f"{API}/slides/{sid}/render").status_code == 404
    assert client.patch(f"{API}/slides/{sid}", json={"title": "x"}).status_code == 404
    assert client.delete(f"{API}/slides/{sid}").status_code == 404
    assert client.post(f"{API}/slides/{sid}/prompt", json={"instruction": "x"}).status_code == 404
    assert client.get(f"{API}/events/{presentation.event_id}/presentations").status_code == 404
    assert client.post(f"{API}/events/{presentation.event_id}/presentations", json={"prompt": "x"}).status_code == 404
    # Nothing changed and nothing was published for them.
    assert client.portal.call(db_presentation, ctx, pid).title == "Launch deck"
    assert published(ctx, presentation.event_id) == []


def test_render_accepts_a_participant_token_of_the_session_presenting_this_deck(strict_client):
    ctx = strict_client.app.state.ctx
    settings = ctx.settings
    event = strict_client.portal.call(create_event, ctx)
    presentation, slides = strict_client.portal.call(make_deck, ctx, event.id)
    other_deck, _ = strict_client.portal.call(make_deck, ctx, event.id, 1)
    live = strict_client.portal.call(make_live_session, ctx, event.id, presentation.id)
    other_live = strict_client.portal.call(make_live_session, ctx, event.id, other_deck.id)
    ended = strict_client.portal.call(make_live_session, ctx, event.id, presentation.id, "ended")

    def token(session_id: str, role: str = "listener") -> str:
        return create_participant_token(settings, participant_id="prt_1", session_id=session_id, role=role)

    deck_url = f"{API}/presentations/{presentation.id}/render"
    slide_url = f"{API}/slides/{slides[0].id}/render"

    # No credentials at all: not authenticated.
    assert strict_client.get(deck_url).status_code == 401
    assert strict_client.get(slide_url).status_code == 401
    # A listener of the session presenting this deck may follow along (deck and single slides).
    res = strict_client.get(deck_url, params={"token": token(live.id), "controls": "false"})
    assert res.status_code == 200 and 'data-controls="false"' in res.text
    assert "content-security-policy" in res.headers
    assert strict_client.get(slide_url, params={"token": token(live.id)}).status_code == 200
    assert strict_client.get(deck_url, params={"token": token(live.id, "host")}).status_code == 200
    # Valid tokens for a session showing another deck, an ended session or no session: 404.
    assert strict_client.get(deck_url, params={"token": token(other_live.id)}).status_code == 404
    assert strict_client.get(slide_url, params={"token": token(other_live.id)}).status_code == 404
    assert strict_client.get(deck_url, params={"token": token(ended.id)}).status_code == 404
    assert strict_client.get(deck_url, params={"token": token("ses_missing")}).status_code == 404
    # A token that is not a participant token at all counts as no token: 401 unless the caller is the owner.
    assert strict_client.get(deck_url, params={"token": "not-a-token"}).status_code == 401
    # The participant token never unlocks anything but rendering.
    detail_url = f"{API}/presentations/{presentation.id}"
    assert strict_client.get(detail_url, params={"token": token(live.id)}).status_code == 401
    assert strict_client.get(f"{detail_url}/export", params={"token": token(live.id)}).status_code == 401
    # The owner's access token works as a bearer header or as `?token=` (iframes cannot set headers).
    owner = strict_client.portal.call(get_dev_user, ctx)
    access = create_access_token(settings, owner.id)
    assert strict_client.get(deck_url, params={"token": access}).status_code == 200
    assert strict_client.get(slide_url, params={"token": access}).status_code == 200
    assert strict_client.get(deck_url, headers={"Authorization": f"Bearer {access}"}).status_code == 200
    assert strict_client.get(slide_url, headers={"Authorization": f"Bearer {access}"}).status_code == 200
