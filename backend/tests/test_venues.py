"""Venue agents: search pipeline, failure isolation, refine. Fake providers + ScriptedLLM, no network."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

import pytest

from app.agents.venues import start_card_refine, start_venue_search
from app.llm.base import LLMError
from app.llm.scripted import LLMCall, ScriptedLLM, ScriptedTurn
from app.models import AgentRun, Event
from app.providers.base import (
    GeoPoint,
    ParkingOption,
    PlaceCandidate,
    PlaceDetails,
    ProviderError,
    TravelEstimate,
    WeatherOutlook,
)
from app.realtime import events as ev
from app.schemas.venue import VenueCriteria
from app.services import board
from tests.helpers import published

CENTRE = GeoPoint(lat=30.2672, lng=-97.7431)
VENUES = [
    PlaceCandidate(place_id="p1", name="Alpha Hall", address="1 Alpha St", location=GeoPoint(lat=30.27, lng=-97.74)),
    PlaceCandidate(place_id="p2", name="Beta Loft", address="2 Beta Ave", location=GeoPoint(lat=30.28, lng=-97.75)),
    PlaceCandidate(place_id="p3", name="Gamma Barn", address="3 Gamma Rd", location=GeoPoint(lat=30.26, lng=-97.73)),
]
SCORES = {"Alpha Hall": 80, "Beta Loft": 90, "Gamma Barn": 70}
CRITERIA = VenueCriteria(
    location="Austin, TX",
    headcount=200,
    event_date=date(2026, 12, 4),
    start_time=time(9, 0),
    end_time=time(17, 0),
    event_type="conference",
    attendee_origin="Downtown Austin",
    max_candidates=3,
)


# --- fake providers --------------------------------------------------------------


class FakePlaces:
    name = "fake-places"

    def __init__(self, venues: list[PlaceCandidate] | None = None, *, fail_on: set[str] = frozenset()) -> None:
        self.venues = list(VENUES if venues is None else venues)
        self.fail_on = fail_on
        self.queries: list[str] = []

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail_on:
            raise ProviderError(f"places provider down during {op}")

    async def geocode(self, address: str) -> GeoPoint | None:
        self._maybe_fail("geocode")
        return CENTRE

    async def search_venues(self, query, *, near=None, location=None, radius_m=15000, max_results=8):
        self._maybe_fail("search")
        self.queries.append(query)
        return self.venues[:max_results]

    async def get_details(self, place_id: str) -> PlaceDetails | None:
        self._maybe_fail("details")
        candidate = next((v for v in self.venues if v.place_id == place_id), None)
        if candidate is None:
            return None
        return PlaceDetails(
            **{**candidate.model_dump(), "maps_url": f"https://maps.example/{place_id}"},
            phone="+1 512 555 0100",
            parking_features=["free_parking_lot"],
            capacity_hint=250 if place_id == "p1" else None,
        )

    async def find_parking(self, location, *, radius_m=800, max_results=5):
        self._maybe_fail("parking")
        return [ParkingOption(name="Central Garage", distance_m=120, kind="garage")]


class FakeWeather:
    name = "fake-weather"

    async def get_outlook(self, location, day, *, start_hour=None, end_hour=None):
        return WeatherOutlook(
            day=day,
            basis="forecast",
            summary="Sunny and mild",
            temp_high_c=24.0,
            temp_low_c=12.0,
            precipitation_probability=0.1,
            window_summary=f"Clear {start_hour}:00-{end_hour}:00",
        )


class FakeTraffic:
    name = "fake-traffic"

    def __init__(self) -> None:
        self.calls: list[tuple[Any, datetime | None]] = []

    async def estimate_travel(self, origin, destination, *, arrive_by=None):
        self.calls.append((origin, arrive_by))
        return TravelEstimate(
            origin=origin if isinstance(origin, str) else "centre",
            typical_minutes=20,
            event_time_minutes=32,
            delay_ratio=1.6,
            rush_hour_overlap=True,
            basis="heuristic",
        )


def install_fakes(ctx, places: FakePlaces | None = None) -> tuple[FakePlaces, FakeTraffic]:
    ctx.places = places or FakePlaces()
    ctx.weather = FakeWeather()
    ctx.traffic = FakeTraffic()
    return ctx.places, ctx.traffic


# --- scripts ----------------------------------------------------------------------


def venue_name(call: LLMCall) -> str:
    return next((name for name in SCORES if name in call.last_user_text), "unknown")


def assessment(name: str) -> dict[str, Any]:
    return {
        "capacity": {
            "estimated_capacity": 250,
            "fits_headcount": "yes",
            "notes": "Stated by listing",
            "source": "listing",
        },
        "parking": {"on_site": True, "summary": "Free lot plus a garage next door", "score": 85},
        "weather": {"summary": "Clear and mild all day", "risk": "low"},
        "traffic": {"summary": "Rush hour adds ~12 minutes", "transit_notes": "Bus 7 stops outside", "score": 60},
        "pricing": {"summary": "Unknown", "within_budget": "unknown"},
        "pros": ["Capacity fits", "Parking on site"],
        "cons": ["Rush-hour arrival"],
        "score": {"overall": SCORES.get(name, 50), "capacity": 90, "parking": 85, "traffic": 60},
        "summary": f"{name} fits 200 comfortably. Parking is easy; expect a slow drive in.",
        "sources": [{"title": f"{name} website", "url": f"https://{name.split()[0].lower()}.example"}],
    }


SCOUT_SCRIPT = [
    ScriptedTurn(tool_calls=[("search_venues", {"query": "conference venue for 200 people Austin"})]),
    ScriptedTurn(tool_calls=[("select_venues", {"place_ids": ["p2", "p1", "p3"]})]),
    ScriptedTurn(text="Shortlisted three venues."),
]


def research_script(call: LLMCall) -> list:
    name = venue_name(call)
    return [
        ScriptedTurn(
            tool_calls=[
                ("get_place_details", {}),
                ("find_parking", {"radius_m": 500}),
                ("get_weather_outlook", {}),
                ("get_drive_time", {}),
            ]
        ),
        ScriptedTurn(tool_calls=[("submit_assessment", assessment(name))]),
        ScriptedTurn(text="Done."),
    ]


def summary_script(call: LLMCall) -> dict[str, Any]:
    failed = "Gamma Barn" if "failed (mention them briefly): Gamma Barn" in call.last_user_text else ""
    return {
        "summary": "Beta Loft is the strongest fit; Alpha Hall is a solid backup."
        + (f" {failed} could not be researched." if failed else ""),
        "one_liners": [{"rank": 1, "one_liner": "Best overall fit"}, {"rank": 2, "one_liner": "Good backup"}],
    }


def script_search(llm: ScriptedLLM) -> None:
    llm.on_run("venue_scout", SCOUT_SCRIPT)
    llm.on_run("venue_research", research_script)
    llm.on_parse("venue_summary", summary_script)


async def runs_for(ctx, event_id: str) -> list[AgentRun]:
    from sqlalchemy import select

    async with ctx.db() as session:
        return list((await session.execute(select(AgentRun).where(AgentRun.event_id == event_id))).scalars())


# --- full pipeline ----------------------------------------------------------------


async def test_search_pipeline_fills_cards_ranks_and_summarises(ctx, event, llm):
    places, traffic = install_fakes(ctx)
    script_search(llm)

    run = await start_venue_search(ctx, event_id=event.id, criteria=CRITERIA)
    assert run.kind == "venue_search" and run.status == "queued"
    done = await ctx.runtime.wait(run.id, timeout=10)
    assert done.status == "succeeded", done.error

    cards = {c.title: c for c in await board.list_cards(ctx, event.id)}
    venues = [cards[n] for n in SCORES]
    assert all(c.kind == "venue" and c.status == "ready" for c in venues)
    assert [cards[n].data["rank"] for n in ("Beta Loft", "Alpha Hall", "Gamma Barn")] == [1, 2, 3]

    beta = cards["Beta Loft"].data
    assert beta["place"]["phone"] == "+1 512 555 0100" and beta["criteria"]["headcount"] == 200
    assert beta["parking"]["nearby"][0]["name"] == "Central Garage" and beta["parking"]["score"] == 85
    assert beta["weather"]["basis"] == "forecast" and beta["weather"]["temp_high_c"] == 24.0
    assert beta["weather"]["risk"] == "low" and beta["weather"]["summary"] == "Clear and mild all day"
    assert beta["traffic"]["origin"] == "Downtown Austin" and beta["traffic"]["event_time_minutes"] == 32
    assert beta["traffic"]["rush_hour_overlap"] is True and beta["traffic"]["summary"].startswith("Rush hour")
    assert beta["capacity"]["fits_headcount"] == "yes" and beta["score"]["overall"] == 90
    titles = [s["title"] for s in beta["sources"]]
    assert "Beta Loft website" in titles and any(t.startswith("Weather forecast") for t in titles)
    assert any("fake-places" in t for t in titles) and any("heuristic" in t for t in titles)
    # Alpha's listing states a capacity; the provisional capacity patch recorded it with its source.
    assert cards["Alpha Hall"].data["capacity"]["estimated_capacity"] == 250
    assert all(arrive == datetime(2026, 12, 4, 9, 0) for _, arrive in traffic.calls)

    summary = cards["Venue shortlist"]
    assert summary.kind == "summary" and summary.agent_run_id == run.id
    assert [r["name"] for r in summary.data["ranking"]] == ["Beta Loft", "Alpha Hall", "Gamma Barn"]
    assert summary.data["ranking"][0] == {
        "card_id": cards["Beta Loft"].id,
        "name": "Beta Loft",
        "score": 90,
        "one_liner": "Best overall fit",
    }
    assert summary.data["criteria"]["location"] == "Austin, TX" and summary.data["summary"].startswith("Beta Loft")

    assert done.output["summary"] == summary.data["summary"] and done.output["summary_card_id"] == summary.id
    assert done.output["card_ids"] == [cards[n].id for n in ("Beta Loft", "Alpha Hall", "Gamma Barn")]

    async with ctx.db() as session:
        assert (await session.get(Event, event.id)).requirements["headcount"] == 200
    assert places.queries == ["conference venue for 200 people Austin"]


async def test_search_pipeline_run_tree_and_envelopes(ctx, event, llm):
    install_fakes(ctx)
    script_search(llm)
    run = await start_venue_search(ctx, event_id=event.id, criteria=CRITERIA)
    await ctx.runtime.wait(run.id, timeout=10)

    runs = await runs_for(ctx, event.id)
    by_kind: dict[str, list[AgentRun]] = {}
    for r in runs:
        by_kind.setdefault(r.kind, []).append(r)
    assert {k: len(v) for k, v in by_kind.items()} == {"venue_search": 1, "venue_scout": 1, "venue_research": 3}
    assert all(r.status == "succeeded" for r in runs)
    assert by_kind["venue_scout"][0].parent_run_id == run.id
    cards = {c.id: c for c in await board.list_cards(ctx, event.id)}
    for child in by_kind["venue_research"]:
        assert child.parent_run_id == run.id and cards[child.card_id].agent_run_id == child.id
        assert child.output["card_id"] == child.card_id and child.output["score"] == SCORES[cards[child.card_id].title]
        assert any("Parking: 1 option(s)" in s["message"] for s in child.steps)

    envelopes = published(ctx, event.id)
    assert envelopes[0].type == ev.AGENT_RUN_CREATED and envelopes[0].data["run"]["id"] == run.id
    last = envelopes[-1]
    assert last.type == ev.AGENT_RUN_UPDATED and last.data["run"]["id"] == run.id
    assert last.data["run"]["status"] == "succeeded"

    created = [e for e in envelopes if e.type == ev.BOARD_CARD_CREATED]
    first_ready = next(
        i for i, e in enumerate(envelopes) if e.type == ev.BOARD_CARD_UPDATED and e.data["card"]["status"] == "ready"
    )
    venue_created = [envelopes.index(e) for e in created if e.data["card"]["kind"] == "venue"]
    assert len(venue_created) == 3 and max(venue_created) < first_ready
    assert all(e.data["card"]["status"] == "running" for e in created if e.data["card"]["kind"] == "venue")
    summary_created = next(envelopes.index(e) for e in created if e.data["card"]["kind"] == "summary")
    last_ready = max(
        i for i, e in enumerate(envelopes) if e.type == ev.BOARD_CARD_UPDATED and e.data["card"]["status"] == "ready"
    )
    assert summary_created > last_ready
    # A venue card fills in section by section, not in one go.
    beta_id = next(e.data["card"]["id"] for e in created if e.data["card"]["title"] == "Beta Loft")
    beta_updates = [e for e in envelopes if e.type == ev.BOARD_CARD_UPDATED and e.data["card"]["id"] == beta_id]
    assert len(beta_updates) >= 5 and beta_updates[-1].data["card"]["status"] == "ready"

    research_calls = llm.calls_for("venue_research")
    assert len(research_calls) == 3
    assert set(research_calls[0].tool_names) == {
        "get_place_details",
        "find_parking",
        "get_weather_outlook",
        "get_drive_time",
        "submit_assessment",
    }
    assert research_calls[0].server_tools[0]["name"] == "web_search"


# --- failure isolation --------------------------------------------------------------


async def test_one_failing_researcher_does_not_fail_the_search(ctx, event, llm):
    install_fakes(ctx)
    script_search(llm)

    def flaky(call: LLMCall) -> list:
        if venue_name(call) == "Gamma Barn":
            raise LLMError("model exploded")
        return research_script(call)

    llm.on_run("venue_research", flaky)
    run = await start_venue_search(ctx, event_id=event.id, criteria=CRITERIA)
    done = await ctx.runtime.wait(run.id, timeout=10)
    assert done.status == "succeeded", done.error

    cards = {c.title: c for c in await board.list_cards(ctx, event.id)}
    gamma = cards["Gamma Barn"]
    assert gamma.status == "error" and "model exploded" in gamma.data["error"]["message"] and "rank" not in gamma.data
    assert cards["Beta Loft"].data["rank"] == 1 and cards["Alpha Hall"].data["rank"] == 2
    assert done.output["card_ids"][-1] == gamma.id and len(done.output["card_ids"]) == 3
    assert "Gamma Barn could not be researched" in done.output["summary"]
    assert [r["name"] for r in cards["Venue shortlist"].data["ranking"]] == ["Beta Loft", "Alpha Hall"]
    failed_run = next(r for r in await runs_for(ctx, event.id) if r.card_id == gamma.id)
    assert failed_run.status == "failed"


async def test_researcher_that_never_submits_is_reminded_then_fails(ctx, event, llm):
    install_fakes(ctx)
    script_search(llm)
    llm.on_run("venue_research", [ScriptedTurn(text="Looks fine to me.")])
    criteria = CRITERIA.model_copy(update={"max_candidates": 1})
    llm.on_run("venue_scout", [ScriptedTurn(tool_calls=[("select_venues", {"place_ids": ["p1"]})])])

    run = await start_venue_search(ctx, event_id=event.id, criteria=criteria)
    done = await ctx.runtime.wait(run.id, timeout=10)
    assert done.status == "failed" and "None of the 1 venues could be researched" in done.error
    assert "submit_assessment" in done.error
    calls = llm.calls_for("venue_research")
    assert len(calls) == 2 and "You stopped without calling submit_assessment" in calls[1].last_user_text


async def test_scout_selects_without_searching_is_an_error(ctx, event, llm):
    """Fallback path: the model skipped searching, so the scout runs the default query itself."""
    places, _ = install_fakes(ctx)
    script_search(llm)
    llm.on_run("venue_scout", [ScriptedTurn(text="I have no tools for this.")])
    run = await start_venue_search(ctx, event_id=event.id, criteria=CRITERIA)
    done = await ctx.runtime.wait(run.id, timeout=10)
    assert done.status == "succeeded", done.error
    assert places.queries == ["conference venue for 200 people"]
    assert len([c for c in await board.list_cards(ctx, event.id) if c.kind == "venue"]) == 3


@pytest.mark.parametrize(
    ("fail_on", "expected"),
    [({"geocode"}, "Could not look up 'Austin, TX'"), ({"search"}, "Venue lookup failed")],
)
async def test_places_provider_failure_fails_search_without_inventing_venues(ctx, event, llm, fail_on, expected):
    install_fakes(ctx, FakePlaces(fail_on=fail_on))
    script_search(llm)
    llm.on_run(
        "venue_scout",
        [
            ScriptedTurn(tool_calls=[("search_venues", {"query": "conference venue"})]),
            ScriptedTurn(text="The search failed."),
        ],
    )
    run = await start_venue_search(ctx, event_id=event.id, criteria=CRITERIA)
    done = await ctx.runtime.wait(run.id, timeout=10)
    assert done.status == "failed" and expected in done.error and "places provider down" in done.error
    assert await board.list_cards(ctx, event.id) == []
    assert llm.calls_for("venue_research") == []


async def test_no_results_is_a_clear_failure(ctx, event, llm):
    install_fakes(ctx, FakePlaces(venues=[]))
    script_search(llm)
    llm.on_run("venue_scout", [ScriptedTurn(tool_calls=[("search_venues", {"query": "conference venue"})])])
    run = await start_venue_search(ctx, event_id=event.id, criteria=CRITERIA)
    done = await ctx.runtime.wait(run.id, timeout=10)
    assert done.status == "failed" and "No venues found within 15 km of Austin, TX" in done.error


# --- refine -------------------------------------------------------------------------


async def make_venue_card(ctx, event_id: str):
    data = {
        "place": {"name": "Beta Loft", "address": "2 Beta Ave", "lat": 30.28, "lng": -97.75, "place_id": "p2"},
        "criteria": CRITERIA.model_dump(mode="json"),
        "parking": {"on_site": True, "summary": "Easy", "nearby": [], "score": 85},
        "traffic": {"origin": "Downtown Austin", "typical_minutes": 20, "event_time_minutes": 32, "score": 60},
        "summary": "Original summary",
        "score": {"overall": 90},
        "sources": [{"title": "Beta Loft website", "url": "https://beta.example"}],
    }
    return await board.create_card(ctx, event_id=event_id, kind="venue", title="Beta Loft", data=data)


async def test_refine_patches_only_what_changed_and_respects_locks(ctx, event, llm):
    _, traffic = install_fakes(ctx)
    card = await make_venue_card(ctx, event.id)
    await board.patch_card(ctx, card.id, by="user", data_patch={"summary": "Planner's own words"})
    llm.on_run(
        "card_refine",
        [
            ScriptedTurn(tool_calls=[("get_drive_time", {"origin": "Round Rock, TX"})]),
            ScriptedTurn(
                tool_calls=[
                    (
                        "update_card",
                        {
                            "note": "Re-estimated the drive from Round Rock; about half an hour at the start time.",
                            "summary": "Agent tries to overwrite",
                            "traffic": {"summary": "Longer haul from Round Rock", "score": 45},
                            "criteria_changes": {"attendee_origin": "Round Rock, TX"},
                        },
                    )
                ]
            ),
            ScriptedTurn(text="Updated."),
        ],
    )

    run = await start_card_refine(ctx, card_id=card.id, instruction="Assume attendees come from Round Rock")
    assert run.kind == "card_refine" and run.card_id == card.id
    assert (await board.get_card(ctx, card.id)).status == "running"
    done = await ctx.runtime.wait(run.id, timeout=10)
    assert done.status == "succeeded", done.error

    after = await board.get_card(ctx, card.id)
    assert after.status == "ready" and after.agent_run_id == run.id
    assert after.data["summary"] == "Planner's own words" and after.locked_fields == ["summary"]
    assert after.data["traffic"]["origin"] == "Round Rock, TX" and after.data["traffic"]["event_time_minutes"] == 32
    assert after.data["traffic"]["summary"] == "Longer haul from Round Rock" and after.data["traffic"]["score"] == 45
    assert after.data["criteria"]["attendee_origin"] == "Round Rock, TX" and after.data["criteria"]["headcount"] == 200
    assert after.data["parking"] == card.data["parking"] and after.data["score"] == {"overall": 90}
    assert [s["title"] for s in after.data["sources"]] == ["Beta Loft website", "Drive-time heuristic (fake-traffic)"]
    assert traffic.calls == [("Round Rock, TX", datetime(2026, 12, 4, 9, 0))]
    assert done.output["note"].startswith("Re-estimated") and done.output["changed"] == [
        "criteria_changes",
        "summary",
        "traffic",
    ]
    prompt = llm.calls_for("card_refine")[0].last_user_text
    assert "Assume attendees come from Round Rock" in prompt and "Locked fields" in prompt and "summary" in prompt


async def test_refine_failure_leaves_a_ready_card_ready(ctx, event, llm):
    install_fakes(ctx)
    card = await make_venue_card(ctx, event.id)

    def boom(call: LLMCall) -> list:
        raise LLMError("rate limited")

    llm.on_run("card_refine", boom)
    run = await start_card_refine(ctx, card_id=card.id, instruction="Check outside catering")
    done = await ctx.runtime.wait(run.id, timeout=10)
    after = await board.get_card(ctx, card.id)
    assert done.status == "failed" and after.status == "ready" and after.data["summary"] == "Original summary"


async def test_refine_rejects_missing_and_non_venue_cards(ctx, event):
    note = await board.create_card(ctx, event_id=event.id, kind="note", title="n")
    with pytest.raises(ValueError):
        await start_card_refine(ctx, card_id=note.id, instruction="x")
    with pytest.raises(LookupError):
        await start_card_refine(ctx, card_id="card_missing", instruction="x")
