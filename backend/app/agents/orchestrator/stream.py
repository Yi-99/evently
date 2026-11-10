"""Streams one assistant reply to the event channel.

Text deltas are coalesced so a long reply does not become thousands of
`chat.delta` envelopes: a delta is published once `FLUSH_CHARS` have
accumulated or `FLUSH_SECONDS` after the first unsent character, whichever
comes first. Tool activity is published as `chat.tool`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from app.agents.orchestrator.tools import describe_call
from app.llm.base import RunCallbacks, ToolCallRecord
from app.realtime import events as ev

if TYPE_CHECKING:
    from app.core.context import AppContext

FLUSH_CHARS = 48
FLUSH_SECONDS = 0.08


class ReplyStream:
    def __init__(self, ctx: AppContext, event_id: str, message_id: str) -> None:
        self._ctx = ctx
        self.event_id = event_id
        self._topic = ev.event_topic(event_id)
        self.message_id = message_id
        self.titles: dict[str, str] = {}  # card/deck/slide id -> title, for readable tool summaries
        self.text = ""  # everything streamed so far; becomes the message content
        self.tools: list[dict[str, Any]] = []  # finished tool calls, for `ChatMessage.meta`
        self._pending = ""
        self._timer: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._after_tool = False
        self._open: dict[str, int] = {}  # tool name -> calls started but not finished

    def callbacks(self) -> RunCallbacks:
        return RunCallbacks(on_text=self.on_text, on_tool_start=self.on_tool_start, on_tool_end=self.on_tool_end)

    # -- text -----------------------------------------------------------------

    async def on_text(self, delta: str) -> None:
        if self._after_tool and self.text and not self.text.endswith("\n"):
            delta = "\n\n" + delta  # text resumed after tool calls starts a new paragraph
        self._after_tool = False
        self.text += delta
        self._pending += delta
        if len(self._pending) >= FLUSH_CHARS:
            await self.flush()
        elif self._timer is None:
            self._timer = asyncio.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        await asyncio.sleep(FLUSH_SECONDS)
        self._timer = None
        await self._emit()

    async def flush(self) -> None:
        """Publish whatever text is still buffered."""
        timer, self._timer = self._timer, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        await self._emit()

    async def _emit(self) -> None:
        async with self._lock:  # keeps deltas in order even when publish() awaits
            text, self._pending = self._pending, ""
            if text:
                await self._ctx.bus.publish(self._topic, ev.CHAT_DELTA, {"message_id": self.message_id, "text": text})

    # -- tools ----------------------------------------------------------------

    async def on_tool_start(self, name: str, tool_input: dict[str, Any]) -> None:
        await self.flush()
        self._after_tool = True
        self._open[name] = self._open.get(name, 0) + 1
        await self._publish_tool(name, "started", describe_call(name, tool_input, self.titles), is_error=False)

    async def on_tool_end(self, record: ToolCallRecord) -> None:
        if not self._open.get(record.name):
            return  # rejected before it ran (bad input); the model gets the error, the UI never saw a start
        self._open[record.name] -= 1
        summary = describe_call(record.name, record.input, self.titles)
        if record.is_error:
            summary += " (failed)"
        self.tools.append({"name": record.name, "summary": summary, "is_error": record.is_error})
        await self._publish_tool(record.name, "finished", summary, is_error=record.is_error)

    async def _publish_tool(self, name: str, status: str, summary: str, *, is_error: bool) -> None:
        data = {"message_id": self.message_id, "name": name, "status": status, "summary": summary, "is_error": is_error}
        await self._ctx.bus.publish(self._topic, ev.CHAT_TOOL, data)
