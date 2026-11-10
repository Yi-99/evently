"""Deterministic `LLMClient` for tests.

Scripts are keyed by the `purpose` each agent passes to `run()` / `parse()`.
Tool handlers are executed for real, so a scripted test exercises the same
code paths a live Claude run would.

    llm.on_run("venue_research", [
        ScriptedTurn(tool_calls=[("get_place_details", {"place_id": "p1"})]),
        lambda records: ScriptedTurn(tool_calls=[("submit_report", {...})]),
        ScriptedTurn(text="Done."),
    ])
    llm.on_parse("deck_outline", DeckOutline(...))
"""

from __future__ import annotations

import inspect
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app.core.ids import new_id
from app.llm.base import (
    AgentResult,
    LLMClient,
    RunCallbacks,
    T,
    Tier,
    Tool,
    ToolCallRecord,
    execute_tool,
)


@dataclass
class ScriptedTurn:
    text: str = ""
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


@dataclass
class LLMCall:
    """What an agent asked for; recorded in `ScriptedLLM.calls` for assertions."""

    kind: str  # run | parse
    purpose: str
    system: str
    messages: list[dict[str, Any]]
    tool_names: list[str] = field(default_factory=list)
    server_tools: list[dict[str, Any]] = field(default_factory=list)
    tier: str = "worker"
    output_model: type[BaseModel] | None = None

    @property
    def last_user_text(self) -> str:
        for message in reversed(self.messages):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return content
            return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        return ""


# A turn, or a function of the tool calls made so far that returns one.
TurnSpec = ScriptedTurn | Callable[[list[ToolCallRecord]], ScriptedTurn]
# A list of turns, or a function of the call that returns one.
RunScript = list[TurnSpec] | Callable[[LLMCall], list[TurnSpec]]
ParseScript = Any  # an output_model instance, a dict, or a function of the call returning either


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class ScriptedLLM(LLMClient):
    available = True

    def __init__(self) -> None:
        self.calls: list[LLMCall] = []
        self._run_handlers: dict[str, RunScript] = {}
        self._run_queues: dict[str, deque[RunScript]] = defaultdict(deque)
        self._parse_handlers: dict[str, ParseScript] = {}
        self._parse_queues: dict[str, deque[ParseScript]] = defaultdict(deque)

    # -- scripting ----------------------------------------------------------

    def on_run(self, purpose: str, script: RunScript) -> None:
        """Use `script` for every `run()` with this purpose ("*" matches any)."""
        self._run_handlers[purpose] = script

    def queue_run(self, purpose: str, script: RunScript) -> None:
        """Use `script` for the next `run()` with this purpose only (FIFO, wins over `on_run`)."""
        self._run_queues[purpose].append(script)

    def on_parse(self, purpose: str, result: ParseScript) -> None:
        self._parse_handlers[purpose] = result

    def queue_parse(self, purpose: str, result: ParseScript) -> None:
        self._parse_queues[purpose].append(result)

    def calls_for(self, purpose: str) -> list[LLMCall]:
        return [c for c in self.calls if c.purpose == purpose]

    @staticmethod
    def _pick(purpose: str, queues: dict[str, deque], handlers: dict[str, Any], kind: str) -> Any:
        if queues[purpose]:
            return queues[purpose].popleft()
        if purpose in handlers:
            return handlers[purpose]
        if "*" in handlers:
            return handlers["*"]
        raise AssertionError(f"ScriptedLLM has no {kind} script for purpose '{purpose}'")

    # -- LLMClient ----------------------------------------------------------

    async def run(
        self,
        *,
        purpose: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[Tool] | None = None,
        server_tools: list[dict[str, Any]] | None = None,
        tier: Tier = "worker",
        model: str | None = None,
        effort: str | None = None,
        max_tokens: int = 16000,
        max_turns: int = 16,
        callbacks: RunCallbacks | None = None,
    ) -> AgentResult:
        tool_map = {t.name: t for t in tools or []}
        call = LLMCall("run", purpose, system, list(messages), list(tool_map), list(server_tools or []), tier)
        self.calls.append(call)

        script = self._pick(purpose, self._run_queues, self._run_handlers, "run")
        turns = await _maybe_await(script(call)) if callable(script) else script
        result = AgentResult(text="", stop_reason="end_turn")

        for spec in list(turns)[:max_turns]:
            turn = await _maybe_await(spec(list(result.tool_calls))) if callable(spec) else spec
            result.turns += 1
            blocks: list[dict[str, Any]] = []
            if turn.text:
                result.text = turn.text
                blocks.append({"type": "text", "text": turn.text})
                if callbacks and callbacks.on_text:
                    await callbacks.on_text(turn.text)
            tool_results: list[dict[str, Any]] = []
            for name, tool_input in turn.tool_calls:
                tool_use_id = new_id("toolu")
                blocks.append({"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input})
                record = await execute_tool(tool_map, name, tool_input, callbacks)
                result.tool_calls.append(record)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": record.result,
                        "is_error": record.is_error,
                    }
                )
            result.new_messages.append({"role": "assistant", "content": blocks})
            if tool_results:
                result.new_messages.append({"role": "user", "content": tool_results})
        return result

    async def parse(
        self,
        *,
        purpose: str,
        system: str,
        messages: list[dict[str, Any]],
        output_model: type[T],
        tier: Tier = "worker",
        model: str | None = None,
        effort: str | None = None,
        max_tokens: int = 16000,
    ) -> T:
        call = LLMCall("parse", purpose, system, list(messages), tier=tier, output_model=output_model)
        self.calls.append(call)
        scripted = self._pick(purpose, self._parse_queues, self._parse_handlers, "parse")
        value = await _maybe_await(scripted(call)) if callable(scripted) else scripted
        if isinstance(value, output_model):
            return value
        if isinstance(value, BaseModel):
            return output_model.model_validate(value.model_dump())
        return output_model.model_validate(value)
