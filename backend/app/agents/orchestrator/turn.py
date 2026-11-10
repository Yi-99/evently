"""One orchestrator turn: history + workspace in, streamed reply out.

Turns on the same event run one at a time (a per-event lock), so a message
sent while the previous reply is still streaming waits for it and then sees
that reply in its history.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from app.agents.orchestrator import store
from app.agents.orchestrator.prompts import SYSTEM_PROMPT
from app.agents.orchestrator.stream import ReplyStream
from app.agents.orchestrator.tools import Toolbox
from app.agents.orchestrator.workspace import Workspace, load_workspace, render_memory
from app.llm.base import LLMError
from app.models import ChatMessage
from app.realtime import events as ev
from app.search import service as search_service
from app.search.client import get_search_client

if TYPE_CHECKING:
    from app.agents.runtime import RunContext
    from app.core.context import AppContext

log = logging.getLogger(__name__)

GENERIC_ERROR = "Something went wrong while writing the reply. Please try again."

MEMORY_TIMEOUT_S = 1.5  # the automatic lookup must never slow a reply noticeably
MEMORY_HITS = 3
MEMORY_QUERY_CHARS = 500


def turn_lock(ctx: AppContext, event_id: str) -> asyncio.Lock:
    locks: dict[str, asyncio.Lock] = ctx.service("orchestrator_turn_locks", dict)
    return locks.setdefault(event_id, asyncio.Lock())


def user_turn(text: str, workspace_block: str, memory_block: str | None = None) -> dict[str, Any]:
    """The current user message: the volatile workspace snapshot, optional memory, then what the planner typed."""
    blocks = [workspace_block, *([memory_block] if memory_block else []), text]
    return {"role": "user", "content": [{"type": "text", "text": block} for block in blocks]}


async def recall_memory(ctx: AppContext, workspace: Workspace, text: str) -> str | None:
    """Light automatic retrieval: a `<memory>` block with earlier venues that match the planner's message.

    Does nothing (returns None) when Elasticsearch is not configured, and swallows every failure and
    timeout, so memory can only ever add context to a turn, never delay or break it.
    """
    query = text.strip()
    if not query or get_search_client(ctx) is None:
        return None
    location = (workspace.event.requirements or {}).get("location")
    if isinstance(location, str) and location.strip():
        query = f"{query[:MEMORY_QUERY_CHARS]} {location.strip()}"
    try:
        hits = await asyncio.wait_for(
            search_service.search_venue_memory(
                ctx, query=query[: MEMORY_QUERY_CHARS + 200], limit=MEMORY_HITS, event_id=workspace.event.id
            ),
            timeout=MEMORY_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 - includes the timeout; memory is best-effort
        log.debug("automatic venue memory lookup skipped", exc_info=True)
        return None
    return render_memory(hits[:MEMORY_HITS]) if hits else None


async def run_turn(run: RunContext, *, user_message: ChatMessage, selected_card_ids: list[str]) -> dict[str, Any]:
    """Agent function of an `orchestrator` run. Returns the run output."""
    lock = turn_lock(run.ctx, run.event_id)
    if lock.locked():
        await run.step("Waiting for the previous reply to finish")
    async with lock:
        return await _reply(run, user_message, selected_card_ids)


async def _reply(run: RunContext, user_message: ChatMessage, selected_card_ids: list[str]) -> dict[str, Any]:
    ctx = run.ctx
    reply = await store.create_message(
        ctx,
        event_id=run.event_id,
        role="assistant",
        status="streaming",
        meta={"agent_run_id": run.run_id, "reply_to": user_message.id},
    )
    toolbox = Toolbox(ctx, event_id=run.event_id, run_id=run.run_id)
    stream = ReplyStream(ctx, run.event_id, reply.id)
    try:
        workspace = await load_workspace(ctx, run.event_id, selected_card_ids)
        stream.titles = workspace.titles
        # Exchanges created after this reply (a follow-up note landing mid-turn) are excluded so that
        # the replayed history on later turns is exactly what this turn saw, in the same order.
        history = await store.load_history(ctx, run.event_id, before=reply.created_at)
        memory = await recall_memory(ctx, workspace, user_message.content)
        messages = [*history, user_turn(user_message.content, workspace.render(), memory)]
        await run.step("Thinking")
        result = await ctx.llm.run(
            purpose="orchestrator",
            tier="orchestrator",
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=toolbox.tools(),
            callbacks=stream.callbacks(),
        )
    except asyncio.CancelledError:
        await asyncio.shield(_fail(ctx, reply.id, stream, "The reply was cancelled."))
        raise
    except Exception as exc:
        await _fail(ctx, reply.id, stream, str(exc) if isinstance(exc, LLMError) else GENERIC_ERROR)
        raise

    await stream.flush()
    run.add_usage(result.usage)
    content = stream.text.strip() or result.text.strip() or _fallback_text(stream.tools)
    message = await store.update_message(
        ctx,
        reply.id,
        status="complete",
        content=content,
        llm_messages=[m for m in result.new_messages if m.get("content")],
        meta_patch={"tools": stream.tools, "stop_reason": result.stop_reason},
    )
    await ctx.bus.publish(
        ev.event_topic(run.event_id), ev.CHAT_MESSAGE_COMPLETED, {"message": store.message_out(message)}
    )
    return {"message_id": reply.id, "launched_run_ids": toolbox.launched_run_ids, "tool_calls": len(result.tool_calls)}


def _fallback_text(tools: list[dict[str, Any]]) -> str:
    """Shown when the model ended the turn without any text."""
    done = [t["summary"] for t in tools if not t["is_error"]]
    return "Done: " + "; ".join(done) + "." if done else "I couldn't come up with a reply. Please try again."


async def _fail(ctx: AppContext, message_id: str, stream: ReplyStream, error: str) -> None:
    try:
        await stream.flush()
        await store.update_message(
            ctx, message_id, status="error", content=stream.text, meta_patch={"error": error, "tools": stream.tools}
        )
        await ctx.bus.publish(
            ev.event_topic(stream.event_id), ev.CHAT_ERROR, {"message_id": message_id, "error": error}
        )
    except Exception:  # noqa: BLE001 - the original failure is what the run should report
        log.exception("could not record chat failure for message %s", message_id)
