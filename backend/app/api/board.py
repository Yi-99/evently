"""REST routes for board (mounted under /api/v1).

Every write goes through `app.services.board`, which persists, versions and
publishes it on the event topic; routes only validate input and check ownership.
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.deps import Ctx, CurrentUser, OwnedEvent, load_owned_event
from app.models import Card, Edge
from app.schemas.common import CardOut, EdgeOut, ORMModel
from app.services import board

router = APIRouter(tags=["board"])

# NaN/inf would poison layout maths and cannot be serialised back as JSON.
Coord = Annotated[float, Field(allow_inf_nan=False)]
Size = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Title = Annotated[str, Field(max_length=300)]
MAX_BULK_MOVES = 500


class Viewport(BaseModel):
    x: Coord = 0.0
    y: Coord = 0.0
    zoom: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 1.0


class BoardOut(ORMModel):
    id: str
    event_id: str
    name: str
    viewport: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class BoardSnapshot(BaseModel):
    board: BoardOut
    cards: list[CardOut]
    edges: list[EdgeOut]


class BoardPatch(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    viewport: Viewport | None = None


class CardCreate(BaseModel):
    kind: Literal["venue", "note", "summary", "comparison", "presentation"] = "note"
    title: Title
    data: dict[str, Any] = Field(default_factory=dict)
    # Give both x and y to place the card; otherwise it takes the next free grid cell.
    x: Coord | None = None
    y: Coord | None = None
    width: Size | None = None
    height: Size | None = None


class CardPatch(BaseModel):
    """Omitted (or null) fields are left unchanged."""

    title: Title | None = None
    x: Coord | None = None
    y: Coord | None = None
    width: Size | None = None
    height: Size | None = None
    z_index: int | None = None
    # Shallow patch of top-level keys; a null value removes the key. Patched keys become locked for agents.
    data: dict[str, Any] | None = None
    unlock_fields: list[str] | None = None
    expected_version: int | None = None


class CardMove(BaseModel):
    card_id: str
    x: Coord
    y: Coord


class EdgeCreate(BaseModel):
    source_card_id: str
    target_card_id: str
    label: Annotated[str, Field(max_length=200)] = ""
    data: dict[str, Any] = Field(default_factory=dict)


async def get_owned_card(card_id: str, ctx: Ctx, user: CurrentUser) -> Card:
    """Path dependency: the card must exist on an event the caller owns (404 otherwise)."""
    card = await board.get_card(ctx, card_id)
    if card is None or await load_owned_event(ctx, card.event_id, user) is None:
        raise HTTPException(404, "Card not found")
    return card


async def get_owned_edge(edge_id: str, ctx: Ctx, user: CurrentUser) -> Edge:
    """Path dependency: the edge must exist on an event the caller owns (404 otherwise)."""
    edge = await board.get_edge(ctx, edge_id)
    if edge is None or await load_owned_event(ctx, edge.event_id, user) is None:
        raise HTTPException(404, "Edge not found")
    return edge


OwnedCard = Annotated[Card, Depends(get_owned_card)]
OwnedEdge = Annotated[Edge, Depends(get_owned_edge)]


# --- board ---------------------------------------------------------------------


@router.get("/events/{event_id}/board", response_model=BoardSnapshot)
async def get_board(event: OwnedEvent, ctx: Ctx):
    """Everything needed to draw the whiteboard. The board row is created on first access."""
    return {
        "board": await board.get_or_create_board(ctx, event.id),
        "cards": await board.list_cards(ctx, event.id),
        "edges": await board.list_edges(ctx, event.id),
    }


@router.patch("/events/{event_id}/board", response_model=BoardOut)
async def patch_board(event: OwnedEvent, body: BoardPatch, ctx: Ctx):
    viewport = body.viewport.model_dump() if body.viewport else None
    return await board.update_board(ctx, event.id, name=body.name, viewport=viewport)


@router.post("/events/{event_id}/board/auto-layout", response_model=list[CardOut])
async def auto_layout_board(event: OwnedEvent, ctx: Ctx):
    """Tidy the board; returns every card at its new position."""
    return await board.auto_layout(ctx, event.id, by="user")


# --- cards ---------------------------------------------------------------------


@router.post("/events/{event_id}/cards", response_model=CardOut, status_code=201)
async def create_card(event: OwnedEvent, body: CardCreate, ctx: Ctx):
    return await board.create_card(
        ctx,
        event_id=event.id,
        kind=body.kind,
        title=body.title,
        data=body.data,
        x=body.x,
        y=body.y,
        width=body.width,
        height=body.height,
        created_by="user",
    )


@router.post("/events/{event_id}/cards/move", response_model=list[CardOut])
async def move_cards(event: OwnedEvent, moves: Annotated[list[CardMove], Body(max_length=MAX_BULK_MOVES)], ctx: Ctx):
    """Reposition several cards at once (multi-select drag). All or nothing: 404 if any card is not on this board."""
    try:
        return await board.move_cards(ctx, event.id, {m.card_id: (m.x, m.y) for m in moves}, by="user")
    except board.CardNotFound:
        raise HTTPException(404, "Card not found") from None


@router.get("/cards/{card_id}", response_model=CardOut)
async def get_card(card: OwnedCard):
    return card


@router.patch(
    "/cards/{card_id}",
    response_model=CardOut,
    responses={409: {"description": "Stale `expected_version`; the body carries the current card as `card`."}},
)
async def patch_card(card: OwnedCard, body: CardPatch, ctx: Ctx):
    """Edit a card as the user: edited `data` keys become locked against agent overwrites."""
    try:
        return await board.patch_card(
            ctx,
            card.id,
            by="user",
            title=body.title,
            x=body.x,
            y=body.y,
            width=body.width,
            height=body.height,
            z_index=body.z_index,
            data_patch=body.data,
            unlock_fields=body.unlock_fields,
            expected_version=body.expected_version,
        )
    except board.VersionConflict as conflict:
        return JSONResponse(
            {"detail": f"Card is at version {conflict.card.version}", "card": board.card_out(conflict.card)},
            status_code=409,
        )
    except board.CardNotFound:  # deleted since the ownership check
        raise HTTPException(404, "Card not found") from None


@router.delete("/cards/{card_id}", status_code=204)
async def delete_card(card: OwnedCard, ctx: Ctx) -> None:
    """Delete a card together with the edges attached to it."""
    await board.delete_card(ctx, card.id)


# --- edges ---------------------------------------------------------------------


@router.post("/events/{event_id}/edges", response_model=EdgeOut, status_code=201)
async def create_edge(event: OwnedEvent, body: EdgeCreate, ctx: Ctx):
    try:
        return await board.create_edge(
            ctx,
            event_id=event.id,
            source_card_id=body.source_card_id,
            target_card_id=body.target_card_id,
            label=body.label,
            data=body.data,
        )
    except board.CardNotFound:
        raise HTTPException(404, "Card not found") from None
    except board.InvalidEdge as invalid:
        raise HTTPException(422, str(invalid)) from None


@router.delete("/edges/{edge_id}", status_code=204)
async def delete_edge(edge: OwnedEdge, ctx: Ctx) -> None:
    await board.delete_edge(ctx, edge.id)
