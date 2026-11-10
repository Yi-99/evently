"""Board layout: pure geometry, no database."""

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import combinations
from typing import Any

from app.services import layout
from app.services.layout import CARD_H, CARD_W, COLUMNS, GAP

T0 = datetime(2026, 1, 1, tzinfo=UTC)
STEP_X, STEP_Y = CARD_W + GAP, CARD_H + GAP


@dataclass
class FakeCard:
    id: str
    kind: str = "venue"
    data: dict[str, Any] = field(default_factory=dict)
    x: float = 0.0
    y: float = 0.0
    width: float = CARD_W
    height: float = CARD_H
    created_at: datetime = T0


def venue(n: int, rank: Any = None, **fields: Any) -> FakeCard:
    """Venue card `v{n}`, created n minutes after T0."""
    data = {} if rank is None else {"rank": rank}
    return FakeCard(id=f"v{n}", data=data, created_at=T0 + timedelta(minutes=n), **fields)


def overlaps(a: FakeCard, b: FakeCard) -> bool:
    return a.x < b.x + b.width and b.x < a.x + a.width and a.y < b.y + b.height and b.y < a.y + a.height


def place(cards: list[FakeCard]) -> dict[str, tuple[float, float]]:
    """Run auto_layout and write the result back onto the fakes."""
    positions = layout.auto_layout(cards)
    for card in cards:
        card.x, card.y = positions[card.id]
    return positions


# --- next_free_position ----------------------------------------------------------


def test_next_free_position_fills_a_row_then_wraps():
    cards: list[FakeCard] = []
    for n in range(COLUMNS + 1):
        x, y = layout.next_free_position(cards)
        cards.append(FakeCard(id=str(n), x=x, y=y))
    assert [(c.x, c.y) for c in cards] == [(i * STEP_X, 0.0) for i in range(COLUMNS)] + [(0.0, STEP_Y)]


def test_next_free_position_reuses_a_freed_cell():
    cards = [FakeCard(id="a", x=0.0), FakeCard(id="c", x=2 * STEP_X)]
    assert layout.next_free_position(cards) == (STEP_X, 0.0)


def test_next_free_position_skips_cells_a_dragged_card_overlaps():
    # Dropped off-grid, straddling the first two cells of the top row.
    straddling = FakeCard(id="a", x=CARD_W / 2, y=10.0)
    assert layout.next_free_position([straddling]) == (2 * STEP_X, 0.0)

    # Cards that merely touch a cell's edge do not block it.
    touching = FakeCard(id="b", x=CARD_W, y=0.0)
    assert layout.next_free_position([touching]) == (0.0, 0.0)


def test_next_free_position_clears_an_oversized_card():
    banner = FakeCard(id="a", width=COLUMNS * STEP_X, height=STEP_Y + 1)  # reaches just into the second row
    assert layout.next_free_position([banner]) == (0.0, 2 * STEP_Y)


# --- auto_layout -------------------------------------------------------------------


def test_auto_layout_of_nothing_is_empty():
    assert layout.auto_layout([]) == {}


def test_auto_layout_orders_ranked_venues_first_then_by_creation():
    cards = [venue(1), venue(2, rank=2), venue(3), venue(4, rank=1), venue(5, rank=2)]
    positions = layout.auto_layout(cards)
    by_reading_order = sorted(positions, key=lambda card_id: (positions[card_id][1], positions[card_id][0]))
    # rank 1; the two rank-2 venues by creation time; then unranked by creation time.
    assert by_reading_order == ["v4", "v2", "v5", "v1", "v3"]
    assert positions["v4"] == (0.0, 0.0)
    assert positions["v3"] == (0.0, STEP_Y)  # fifth venue wraps onto the second row


def test_auto_layout_treats_non_numeric_ranks_as_unranked():
    cards = [venue(1, rank="1"), venue(2, rank=True), venue(3, rank=float("nan")), venue(4, rank=7.5)]
    positions = layout.auto_layout(cards)
    assert positions["v4"] == (0.0, 0.0)
    assert [positions[f"v{n}"][0] for n in (1, 2, 3)] == [STEP_X, 2 * STEP_X, 3 * STEP_X]


def test_auto_layout_puts_other_kinds_in_one_column_beside_the_grid():
    later = T0 + timedelta(hours=1)
    cards = [
        *[venue(n) for n in range(COLUMNS + 2)],
        FakeCard(id="note-late", kind="note", created_at=later),
        FakeCard(id="note", kind="note"),
        FakeCard(id="deck", kind="presentation"),
        FakeCard(id="custom", kind="something-new"),
        FakeCard(id="compare", kind="comparison"),
        FakeCard(id="summary", kind="summary"),
    ]
    positions = layout.auto_layout(cards)
    side_x = COLUMNS * STEP_X
    side = ["summary", "compare", "deck", "note", "note-late", "custom"]
    assert [positions[card_id] for card_id in side] == [(side_x, i * STEP_Y) for i in range(len(side))]
    assert all(positions[f"v{n}"][0] < side_x for n in range(COLUMNS + 2))


def test_auto_layout_side_column_hugs_a_short_grid_and_starts_at_origin_without_venues():
    note = FakeCard(id="n", kind="note")
    assert layout.auto_layout([venue(1), venue(2), note])["n"] == (2 * STEP_X, 0.0)
    assert layout.auto_layout([note]) == {"n": (0.0, 0.0)}


def test_auto_layout_never_overlaps_resized_cards():
    rng = random.Random(7)
    kinds = ["venue"] * 6 + ["note", "summary", "comparison", "presentation"]
    cards = [
        FakeCard(
            id=f"c{n}",
            kind=rng.choice(kinds),
            width=rng.choice([200.0, CARD_W, 640.0]),
            height=rng.choice([150.0, CARD_H, 900.0]),
            created_at=T0 + timedelta(seconds=n),
        )
        for n in range(40)
    ]
    place(cards)
    assert not [(a.id, b.id) for a, b in combinations(cards, 2) if overlaps(a, b)]


def test_auto_layout_sizes_rows_and_columns_to_their_largest_card():
    cards = [venue(n) for n in range(COLUMNS + 1)]
    cards[1].width = 500.0  # widens the second column
    cards[2].height = 600.0  # deepens the first row
    positions = layout.auto_layout(cards)
    assert positions["v2"][0] == positions["v1"][0] + 500.0 + GAP
    assert positions[f"v{COLUMNS}"] == (0.0, 600.0 + GAP)


def test_auto_layout_is_deterministic_and_ignores_input_order_and_current_positions():
    cards = [venue(n, rank=n % 3 or None) for n in range(9)]
    cards += [FakeCard(id=f"n{n}", kind="note") for n in range(3)]  # identical timestamps: id breaks the tie
    expected = layout.auto_layout(cards)

    shuffled = list(cards)
    random.Random(3).shuffle(shuffled)
    for card in shuffled:
        card.x, card.y = 9999.0, -50.0
    assert layout.auto_layout(shuffled) == expected
    assert place(shuffled) == expected  # laying out a tidy board changes nothing
    assert layout.auto_layout(shuffled) == expected
