"""Venue REST routes: start a search, re-prompt a card."""

from __future__ import annotations

import asyncio

from app.services import board
from tests.test_venues import CRITERIA, FakePlaces, install_fakes, make_venue_card, script_search

SEARCH_BODY = CRITERIA.model_dump(mode="json")


def test_create_venue_search_runs_the_pipeline(client, client_event, llm):
    ctx = client.app.state.ctx
    client.portal.call(install_fakes, ctx, FakePlaces())
    script_search(llm)

    response = client.post(f"/api/v1/events/{client_event.id}/venue-searches", json=SEARCH_BODY)
    assert response.status_code == 202, response.text
    run = response.json()["run"]
    assert run["kind"] == "venue_search" and run["event_id"] == client_event.id and run["status"] == "queued"

    done = client.portal.call(ctx.runtime.wait, run["id"], 10)
    assert done.status == "succeeded", done.error
    cards = client.portal.call(board.list_cards, ctx, client_event.id)
    assert sorted(c.kind for c in cards) == ["summary", "venue", "venue", "venue"]
    assert client.get(f"/api/v1/agent-runs/{run['id']}").json()["output"]["summary_card_id"]


def test_create_venue_search_validates_and_checks_ownership(client, client_event):
    assert client.post("/api/v1/events/evt_missing/venue-searches", json=SEARCH_BODY).status_code == 404
    bad = client.post(f"/api/v1/events/{client_event.id}/venue-searches", json={"headcount": 10})
    assert bad.status_code == 422  # location is required


def test_routes_return_503_without_claude(client, client_event, llm):
    ctx = client.app.state.ctx
    card = client.portal.call(make_venue_card, ctx, client_event.id)
    llm.available = False
    try:
        search = client.post(f"/api/v1/events/{client_event.id}/venue-searches", json=SEARCH_BODY)
        prompt = client.post(f"/api/v1/cards/{card.id}/prompt", json={"instruction": "check catering"})
    finally:
        llm.available = True
    assert search.status_code == 503 and "ANTHROPIC_API_KEY" in search.json()["detail"]
    assert prompt.status_code == 503
    assert client.portal.call(board.get_card, ctx, card.id).status == "ready"  # nothing was started


def test_prompt_card_starts_refine(client, client_event, llm):
    ctx = client.app.state.ctx
    client.portal.call(install_fakes, ctx, FakePlaces())
    card = client.portal.call(make_venue_card, ctx, client_event.id)
    llm.on_run("card_refine", lambda call: _refine_turns(call))

    response = client.post(f"/api/v1/cards/{card.id}/prompt", json={"instruction": "Do they allow outside catering?"})
    assert response.status_code == 202, response.text
    run = response.json()["run"]
    assert run["kind"] == "card_refine" and run["card_id"] == card.id

    done = client.portal.call(ctx.runtime.wait, run["id"], 10)
    assert done.status == "succeeded", done.error
    after = client.portal.call(board.get_card, ctx, card.id)
    assert after.status == "ready" and after.data["pros"] == ["Outside catering allowed"]
    assert done.output["note"] == "They allow outside catering."
    assert "Do they allow outside catering?" in llm.calls_for("card_refine")[0].last_user_text


def _refine_turns(call):
    from app.llm.scripted import ScriptedTurn

    return [
        ScriptedTurn(
            tool_calls=[("update_card", {"note": "They allow outside catering.", "pros": ["Outside catering allowed"]})]
        ),
        ScriptedTurn(text="Done."),
    ]


def test_prompt_card_rejects_unknown_non_venue_and_busy_cards(client, client_event):
    ctx = client.app.state.ctx
    body = {"instruction": "anything"}
    assert client.post("/api/v1/cards/card_missing/prompt", json=body).status_code == 404

    note = client.portal.call(lambda: board.create_card(ctx, event_id=client_event.id, kind="note", title="n"))
    assert client.post(f"/api/v1/cards/{note.id}/prompt", json=body).status_code == 422

    venue = client.portal.call(make_venue_card, ctx, client_event.id)
    assert client.post(f"/api/v1/cards/{venue.id}/prompt", json={"instruction": "   "}).status_code == 422

    async def busy_card():
        async def sleeper(run):
            await asyncio.sleep(30)

        run = await ctx.runtime.spawn(event_id=client_event.id, kind="venue_research", title="t", fn=sleeper)
        await board.patch_card(ctx, venue.id, status="running", agent_run_id=run.id)
        return run

    run = client.portal.call(busy_card)
    assert client.post(f"/api/v1/cards/{venue.id}/prompt", json=body).status_code == 409
    client.portal.call(ctx.runtime.cancel, run.id)
