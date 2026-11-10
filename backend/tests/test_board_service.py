"""Board service: edges, bulk moves, auto-layout, cascade and concurrency."""

import asyncio

import pytest
from sqlalchemy.exc import IntegrityError

from app.realtime import events as ev
from app.services import board, layout
from tests.helpers import create_event, published


async def make_cards(ctx, event_id: str, count: int, **fields):
    fields.setdefault("kind", "venue")
    return [await board.create_card(ctx, event_id=event_id, title=f"C{i}", **fields) for i in range(count)]


def board_events(ctx, event_id: str, since: int = 0) -> list[tuple[str, dict]]:
    """(type, data) of board envelopes published after the first `since` ones."""
    return [(e.type, e.data) for e in published(ctx, event_id, "board.")][since:]


# --- board -------------------------------------------------------------------------


async def test_get_or_create_board_is_safe_under_concurrency(ctx, event):
    boards = await asyncio.gather(*[board.get_or_create_board(ctx, event.id) for _ in range(8)])
    assert len({b.id for b in boards}) == 1


async def test_get_or_create_board_for_unknown_event_still_fails(ctx):
    with pytest.raises(IntegrityError):
        await board.get_or_create_board(ctx, "evt_missing")


async def test_update_board_keeps_omitted_fields(ctx, event):
    renamed = await board.update_board(ctx, event.id, name="Venues")
    assert (renamed.name, renamed.viewport) == ("Venues", {})

    await board.update_board(ctx, event.id, viewport={"x": 10.0, "y": -5.0, "zoom": 1.5})
    stored = await board.get_or_create_board(ctx, event.id)
    assert (stored.id, stored.name) == (renamed.id, "Venues")
    assert stored.viewport == {"x": 10.0, "y": -5.0, "zoom": 1.5}


# --- cards: concurrency ---------------------------------------------------------------


async def test_concurrent_creates_get_distinct_grid_cells(ctx, event):
    cards = await asyncio.gather(
        *[board.create_card(ctx, event_id=event.id, kind="venue", title=str(i)) for i in range(6)]
    )
    assert len({(c.x, c.y) for c in cards}) == 6
    assert len({c.board_id for c in cards}) == 1


async def test_concurrent_patches_do_not_lose_each_others_changes(ctx, event):
    (card,) = await make_cards(ctx, event.id, 1)
    await asyncio.gather(
        board.patch_card(ctx, card.id, by="user", data_patch={"summary": "mine"}),
        *[board.patch_card(ctx, card.id, by="agent", data_patch={f"k{i}": i}) for i in range(10)],
        board.patch_card(ctx, card.id, by="agent", x=77.0),
    )
    after = await board.get_card(ctx, card.id)
    assert after.data == {"summary": "mine"} | {f"k{i}": i for i in range(10)}
    assert after.locked_fields == ["summary"] and after.x == 77.0
    assert after.version == 13  # one bump per change, none skipped

    versions = [
        data["card"]["version"] for type_, data in board_events(ctx, event.id) if type_ == ev.BOARD_CARD_UPDATED
    ]
    assert versions == list(range(2, 14))  # envelopes leave in commit order


async def test_patch_missing_card_raises(ctx):
    with pytest.raises(board.CardNotFound):
        await board.patch_card(ctx, "card_missing", title="x")


# --- edges -------------------------------------------------------------------------


async def test_create_edge_publishes_and_lists(ctx, event):
    a, b = await make_cards(ctx, event.id, 2)
    edge = await board.create_edge(
        ctx, event_id=event.id, source_card_id=a.id, target_card_id=b.id, label="cheaper", data={"style": "dashed"}
    )
    assert (edge.board_id, edge.event_id) == (a.board_id, event.id)
    assert [e.id for e in await board.list_edges(ctx, event.id)] == [edge.id]
    assert (await board.get_edge(ctx, edge.id)).label == "cheaper"

    type_, data = board_events(ctx, event.id)[-1]
    assert type_ == ev.BOARD_EDGE_CREATED and data == {"edge": board.edge_out(edge)}
    assert data["edge"]["source_card_id"] == a.id and data["edge"]["data"] == {"style": "dashed"}


async def test_create_edge_rejects_cards_outside_the_event_and_self_loops(ctx, event):
    (mine,) = await make_cards(ctx, event.id, 1)
    other_event = await create_event(ctx, "Other")
    (theirs,) = await make_cards(ctx, other_event.id, 1)

    for source, target in [(mine.id, theirs.id), (theirs.id, mine.id), (mine.id, "card_missing")]:
        with pytest.raises(board.CardNotFound):
            await board.create_edge(ctx, event_id=event.id, source_card_id=source, target_card_id=target)
    with pytest.raises(board.InvalidEdge):
        await board.create_edge(ctx, event_id=event.id, source_card_id=mine.id, target_card_id=mine.id)

    assert await board.list_edges(ctx, event.id) == []
    assert not published(ctx, event.id, "board.edge")


async def test_delete_edge_publishes_once(ctx, event):
    a, b = await make_cards(ctx, event.id, 2)
    edge = await board.create_edge(ctx, event_id=event.id, source_card_id=a.id, target_card_id=b.id)

    assert await board.delete_edge(ctx, edge.id) is True
    assert await board.delete_edge(ctx, edge.id) is False
    assert await board.list_edges(ctx, event.id) == []
    deleted = [e.data for e in published(ctx, event.id, ev.BOARD_EDGE_DELETED)]
    assert deleted == [{"edge_id": edge.id}]


async def test_delete_card_removes_its_edges_and_publishes_them_first(ctx, event):
    a, b, c = await make_cards(ctx, event.id, 3)
    out_edge = await board.create_edge(ctx, event_id=event.id, source_card_id=a.id, target_card_id=b.id)
    in_edge = await board.create_edge(ctx, event_id=event.id, source_card_id=c.id, target_card_id=a.id)
    unrelated = await board.create_edge(ctx, event_id=event.id, source_card_id=b.id, target_card_id=c.id)
    before = len(board_events(ctx, event.id))

    assert await board.delete_card(ctx, a.id) is True

    assert [e.id for e in await board.list_edges(ctx, event.id)] == [unrelated.id]
    assert [card.id for card in await board.list_cards(ctx, event.id)] == [b.id, c.id]
    assert board_events(ctx, event.id, before) == [
        (ev.BOARD_EDGE_DELETED, {"edge_id": out_edge.id}),
        (ev.BOARD_EDGE_DELETED, {"edge_id": in_edge.id}),
        (ev.BOARD_CARD_DELETED, {"card_id": a.id}),
    ]
    assert await board.delete_card(ctx, a.id) is False
    assert len(board_events(ctx, event.id)) == before + 3


# --- positions ---------------------------------------------------------------------


async def test_move_cards_updates_and_publishes_one_envelope_per_moved_card(ctx, event):
    a, b, c = await make_cards(ctx, event.id, 3)
    before = len(board_events(ctx, event.id))

    moved = await board.move_cards(ctx, event.id, {b.id: (500.0, 600.0), a.id: (a.x, 25.0), c.id: (c.x, c.y)})

    assert [(m.id, m.x, m.y, m.version) for m in moved] == [
        (b.id, 500.0, 600.0, 2),
        (a.id, a.x, 25.0, 2),
        (c.id, c.x, c.y, 1),  # asked to stay put: untouched and not announced
    ]
    stored = {card.id: (card.x, card.y, card.version) for card in await board.list_cards(ctx, event.id)}
    assert stored == {b.id: (500.0, 600.0, 2), a.id: (a.x, 25.0, 2), c.id: (c.x, c.y, 1)}

    events = board_events(ctx, event.id, before)
    assert [type_ for type_, _ in events] == [ev.BOARD_CARD_UPDATED] * 2
    assert [(d["card"]["id"], d["changed"], d["by"]) for _, d in events] == [
        (b.id, ["x", "y"], "user"),
        (a.id, ["y"], "user"),
    ]
    assert events[0][1]["card"] == board.card_out(moved[0])


async def test_move_cards_is_all_or_nothing(ctx, event):
    (mine,) = await make_cards(ctx, event.id, 1)
    other_event = await create_event(ctx, "Other")
    (theirs,) = await make_cards(ctx, other_event.id, 1)
    before = len(board_events(ctx, event.id))

    for stranger in (theirs.id, "card_missing"):
        with pytest.raises(board.CardNotFound):
            await board.move_cards(ctx, event.id, {mine.id: (900.0, 900.0), stranger: (1.0, 1.0)})

    assert ((await board.get_card(ctx, mine.id)).x, (await board.get_card(ctx, theirs.id)).x) == (mine.x, theirs.x)
    assert len(board_events(ctx, event.id)) == before
    assert not published(ctx, other_event.id, ev.BOARD_CARD_UPDATED)


async def test_auto_layout_applies_layout_and_publishes_only_real_moves(ctx, event):
    venues = await make_cards(ctx, event.id, 3)
    await board.patch_card(ctx, venues[2].id, data_patch={"rank": 1})
    note = await board.create_card(ctx, event_id=event.id, kind="note", title="N", x=-300.0, y=50.0)
    before = len(board_events(ctx, event.id))

    cards = await board.auto_layout(ctx, event.id, by="agent")

    assert [card.id for card in cards] == [*[v.id for v in venues], note.id]  # every card, creation order
    stored = await board.list_cards(ctx, event.id)
    assert {card.id: (card.x, card.y) for card in stored} == layout.auto_layout(stored)
    step = layout.CARD_W + layout.GAP
    assert {card.id: (card.x, card.y) for card in cards} == {
        venues[2].id: (0.0, 0.0),  # the ranked venue leads
        venues[0].id: (step, 0.0),
        venues[1].id: (2 * step, 0.0),
        note.id: (3 * step, 0.0),
    }

    events = board_events(ctx, event.id, before)
    assert {type_ for type_, _ in events} == {ev.BOARD_CARD_UPDATED}
    assert {d["card"]["id"] for _, d in events} == {c.id for c in cards}
    assert all(d["by"] == "agent" and set(d["changed"]) <= {"x", "y"} for _, d in events)

    # A tidy board is left alone: no writes, no envelopes.
    again = await board.auto_layout(ctx, event.id)
    assert [(c.x, c.y, c.version) for c in again] == [(c.x, c.y, c.version) for c in cards]
    assert len(board_events(ctx, event.id)) == before + len(events)
