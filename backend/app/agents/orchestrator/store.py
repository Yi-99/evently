"""Chat message persistence and history replay.

How a conversation is stored:

* a user row holds the text the planner typed;
* the assistant row that answers it holds `meta["reply_to"]` (the user row's id)
  and, once complete, `llm_messages`: the Claude-format messages of that turn,
  tool_use / tool_result pairs included, thinking blocks already stripped;
* follow-up notes ("the venue search has finished") are assistant rows without
  `reply_to` whose `llm_messages` is the note itself.

History is rebuilt from the assistant rows in creation order, so it reflects the
order in which turns actually ran even when a second message was queued while
the first was still being answered.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.models import ChatMessage
from app.realtime import events as ev
from app.schemas.common import ChatMessageOut, dump

if TYPE_CHECKING:
    from app.core.context import AppContext

# Assistant rows (turns and follow-up notes) replayed to the model.
MAX_HISTORY_ROWS = 60


def message_out(message: ChatMessage) -> dict[str, Any]:
    return dump(ChatMessageOut, message)


async def create_message(
    ctx: AppContext,
    *,
    event_id: str,
    role: str,
    content: str = "",
    status: str = "complete",
    llm_messages: list[dict[str, Any]] | None = None,
    meta: dict[str, Any] | None = None,
) -> ChatMessage:
    """Persist a chat message and publish `chat.message.created`."""
    message = ChatMessage(
        event_id=event_id, role=role, content=content, status=status, llm_messages=llm_messages or [], meta=meta or {}
    )
    async with ctx.db() as session:
        session.add(message)
        await session.commit()
    await ctx.bus.publish(ev.event_topic(event_id), ev.CHAT_MESSAGE_CREATED, {"message": message_out(message)})
    return message


async def update_message(
    ctx: AppContext,
    message_id: str,
    *,
    status: str,
    content: str,
    llm_messages: list[dict[str, Any]] | None = None,
    meta_patch: dict[str, Any] | None = None,
) -> ChatMessage:
    """Finish a streaming message (no envelope; the caller publishes the outcome)."""
    async with ctx.db() as session:
        message = await session.get(ChatMessage, message_id)
        if message is None:
            raise LookupError(f"chat message {message_id} not found")
        message.status = status
        message.content = content
        if llm_messages is not None:
            message.llm_messages = llm_messages
        if meta_patch:
            message.meta = {**(message.meta or {}), **meta_patch}
        await session.commit()
    return message


def text_message(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _exchange(reply: ChatMessage, asked: ChatMessage | None) -> list[dict[str, Any]]:
    """Messages of one stored exchange: the planner's text, then what the turn produced.

    A failed turn contributes only the planner's text, so "try again" on the
    next turn still has something to refer to.
    """
    messages = [text_message("user", asked.content)] if asked is not None and asked.content.strip() else []
    if reply.status == "complete":
        # An assistant message left empty once thinking was stripped is rejected on replay.
        messages.extend(m for m in reply.llm_messages or [] if m.get("content"))
    return messages


async def load_history(
    ctx: AppContext, event_id: str, *, before: datetime | None = None, max_rows: int = MAX_HISTORY_ROWS
) -> list[dict[str, Any]]:
    """Claude-format messages of the finished exchanges of an event's chat, oldest first.

    before: only exchanges whose assistant row was created earlier than this.
    max_rows: how many of the most recent assistant rows to replay. The window
        is cut between exchanges, never inside one, so every tool_use keeps its
        tool_result, and it always opens with a user message.
    """
    query = select(ChatMessage).where(
        ChatMessage.event_id == event_id, ChatMessage.role == "assistant", ChatMessage.status != "streaming"
    )
    if before is not None:
        query = query.where(ChatMessage.created_at < before)
    async with ctx.db() as session:
        newest_first = (await session.execute(query.order_by(ChatMessage.created_at.desc()).limit(max_rows))).scalars()
        replies = list(reversed(list(newest_first)))
        asked_ids = [r.meta["reply_to"] for r in replies if (r.meta or {}).get("reply_to")]
        asked = {}
        if asked_ids:
            rows = await session.execute(select(ChatMessage).where(ChatMessage.id.in_(asked_ids)))
            asked = {m.id: m for m in rows.scalars()}

    exchanges = [_exchange(reply, asked.get((reply.meta or {}).get("reply_to"))) for reply in replies]
    # Follow-up notes at the head of the window have no user message to hang on.
    while exchanges and not (exchanges[0] and exchanges[0][0]["role"] == "user"):
        exchanges.pop(0)
    return [message for exchange in exchanges for message in exchange]
