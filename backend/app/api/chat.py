"""REST routes for chat (mounted under /api/v1).

The reply itself is not in the HTTP response: it streams on the event channel
(`chat.message.created` → `chat.delta`* / `chat.tool`* → `chat.message.completed`).
"""

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, StringConstraints
from sqlalchemy import and_, or_, select

from app.agents import orchestrator
from app.core.deps import Ctx, CurrentUser, OwnedEvent, Session
from app.models import ChatMessage
from app.schemas.common import AgentRunOut, ChatMessageOut

router = APIRouter(tags=["chat"])


class ChatMessageIn(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)]
    context: dict[str, Any] | None = None  # UI context, e.g. {"selected_card_ids": [...]}


class ChatTurnOut(BaseModel):
    message: ChatMessageOut
    run: AgentRunOut


@router.post("/events/{event_id}/chat/messages", response_model=ChatTurnOut, status_code=202)
async def post_message(body: ChatMessageIn, event: OwnedEvent, ctx: Ctx, user: CurrentUser):
    """Store the planner's message and start the assistant's turn; the reply streams on the event channel."""
    if not ctx.llm.available:
        raise HTTPException(503, "Claude is not configured. Set ANTHROPIC_API_KEY in .env.")
    message, run = await orchestrator.handle_user_message(
        ctx, event_id=event.id, user_id=user.id, text=body.text, context=body.context
    )
    return {"message": message, "run": run}


@router.get("/events/{event_id}/chat/messages", response_model=list[ChatMessageOut])
async def list_messages(
    event: OwnedEvent,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[str | None, Query(description="Message id; return messages older than it.")] = None,
):
    """The newest `limit` messages (older than `before` when given), oldest first."""
    query = select(ChatMessage).where(ChatMessage.event_id == event.id)
    if before is not None:
        cursor = await session.get(ChatMessage, before)
        if cursor is None or cursor.event_id != event.id:
            raise HTTPException(404, "Message not found")
        query = query.where(
            or_(
                ChatMessage.created_at < cursor.created_at,
                and_(ChatMessage.created_at == cursor.created_at, ChatMessage.id < cursor.id),
            )
        )
    rows = await session.execute(query.order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(limit))
    return list(reversed(list(rows.scalars())))
