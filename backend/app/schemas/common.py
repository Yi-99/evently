"""Response models shared across packages (they also define realtime payload shapes)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def dump(model_cls: type[ORMModel], obj: Any) -> dict[str, Any]:
    """Serialise an ORM row to a JSON-safe dict using its response model."""
    return model_cls.model_validate(obj).model_dump(mode="json")


class UserOut(ORMModel):
    id: str
    email: str
    name: str


class EventOut(ORMModel):
    id: str
    name: str
    description: str
    requirements: dict[str, Any]
    status: str
    created_at: datetime
    updated_at: datetime


class ChatMessageOut(ORMModel):
    id: str
    event_id: str
    role: str
    content: str
    meta: dict[str, Any]
    status: str
    created_at: datetime


class AgentRunOut(ORMModel):
    id: str
    event_id: str
    parent_run_id: str | None
    kind: str
    title: str
    status: str
    progress: float
    current_step: str
    steps: list[dict[str, Any]]
    input: dict[str, Any]
    output: dict[str, Any]
    error: str | None
    card_id: str | None
    usage: dict[str, Any]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class BoardOut(ORMModel):
    id: str
    event_id: str
    name: str
    viewport: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class CardOut(ORMModel):
    id: str
    board_id: str
    event_id: str
    kind: str
    title: str
    status: str
    x: float
    y: float
    width: float
    height: float
    z_index: int
    data: dict[str, Any]
    locked_fields: list[str]
    agent_run_id: str | None
    created_by: str
    version: int
    created_at: datetime
    updated_at: datetime


class EdgeOut(ORMModel):
    id: str
    board_id: str
    event_id: str
    source_card_id: str
    target_card_id: str
    label: str
    data: dict[str, Any]
    created_at: datetime
