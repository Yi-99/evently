"""LLM abstraction used by every agent.

Agents never import a provider SDK directly. They call `ctx.llm.run(...)`
for a tool-using agent loop or `ctx.llm.parse(...)` for a schema-validated
answer. `AnthropicLLM` talks to Claude, `OpenAILLM` to OpenAI, and `RoutedLLM`
picks one per `purpose`; `ScriptedLLM` replays scripted turns in tests while
executing the real tool handlers.

Messages in and out (`messages`, `AgentResult.new_messages`) are always in the
Claude format, whichever provider serves the request, so a conversation can
switch provider mid-way.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)
Tier = Literal["orchestrator", "worker"]


class LLMError(Exception):
    """Base class for LLM failures."""


class LLMUnavailableError(LLMError):
    """No credentials are configured, or the provider rejected them."""


class LLMBillingError(LLMUnavailableError):
    """The provider rejected the request for billing/quota reasons (out of credits, no plan, quota used up).

    `RoutedLLM` falls back to another configured provider on this error.
    """


class LLMRefusalError(LLMError):
    """The model (and any fallback) declined the request."""


class LLMRateLimitError(LLMError):
    """The provider is rate limiting us; retry later."""


@dataclass
class Tool:
    """A tool the model may call.

    `handler` receives a validated `input_model` instance and returns a str,
    dict, list or BaseModel. Raise `ToolError` to hand the model a readable
    error it can react to.
    """

    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[Any], Awaitable[Any]]

    def to_api(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
            # Stream large inputs (slide HTML, reports) as they are generated. The
            # API then skips server-side validation, so `execute_tool` validates.
            "eager_input_streaming": True,
        }


class ToolError(Exception):
    """Raised by a tool handler to report a recoverable problem to the model."""


@dataclass
class ToolCallRecord:
    name: str
    input: dict[str, Any]
    result: str
    is_error: bool = False


@dataclass
class RunCallbacks:
    """Optional hooks fired while an agent loop runs."""

    on_text: Callable[[str], Awaitable[None]] | None = None  # assistant text delta
    on_tool_start: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None  # name, input
    on_tool_end: Callable[[ToolCallRecord], Awaitable[None]] | None = None


@dataclass
class AgentResult:
    text: str  # text of the final assistant turn
    stop_reason: str  # end_turn | max_tokens | max_turns
    # Messages appended during this run as JSON-safe dicts with thinking blocks
    # removed. Persist these to replay the conversation on a later request.
    new_messages: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    turns: int = 0

    def called(self, tool_name: str) -> list[ToolCallRecord]:
        return [c for c in self.tool_calls if c.name == tool_name]


def serialize_tool_result(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump_json(exclude_none=True)
    return json.dumps(value, default=str)


async def execute_tool(
    tools: dict[str, Tool], name: str, raw_input: Any, callbacks: RunCallbacks | None
) -> ToolCallRecord:
    """Validate the input, run the handler, and never raise (errors go back to the model)."""
    raw = raw_input if isinstance(raw_input, dict) else {}
    tool = tools.get(name)
    if tool is None:
        record = ToolCallRecord(name, raw, f"Unknown tool '{name}'.", is_error=True)
    elif not isinstance(raw_input, dict):
        record = ToolCallRecord(name, raw, "Tool input must be a JSON object.", is_error=True)
    else:
        try:
            parsed = tool.input_model.model_validate(raw_input)
        except ValidationError as exc:
            record = ToolCallRecord(name, raw, f"Invalid input: {exc.errors(include_url=False)}", is_error=True)
        else:
            if callbacks and callbacks.on_tool_start:
                await callbacks.on_tool_start(name, raw)
            try:
                record = ToolCallRecord(name, raw, serialize_tool_result(await tool.handler(parsed)))
            except ToolError as exc:
                record = ToolCallRecord(name, raw, str(exc), is_error=True)
            except Exception as exc:  # noqa: BLE001 - surface any tool failure to the model
                record = ToolCallRecord(name, raw, f"{type(exc).__name__}: {exc}", is_error=True)
    if callbacks and callbacks.on_tool_end:
        await callbacks.on_tool_end(record)
    return record


class LLMClient(ABC):
    #: False when no credentials are configured; routers turn that into a 503.
    available: bool = True

    @abstractmethod
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
        """Run a tool-using agent loop until the model stops calling tools.

        purpose: short label for the caller ("orchestrator", "venue_research"…);
            used for logging and by `ScriptedLLM` to pick a script.
        messages: Claude-format messages; the first must have role "user".
        server_tools: raw Anthropic server-tool definitions (e.g. web search); other
            providers translate the ones they have an equivalent for.
        tier: picks the default model/effort from settings unless given explicitly.
        """

    @abstractmethod
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
        """Single request whose answer is validated against `output_model`."""

    async def close(self) -> None:  # noqa: B027 - optional hook
        """Release network resources."""


class UnconfiguredLLM(LLMClient):
    """Placeholder used when a provider has no credentials."""

    available = False

    def __init__(self, message: str = "Claude is not configured. Set ANTHROPIC_API_KEY in .env.") -> None:
        self.message = message

    async def run(self, **_: Any) -> AgentResult:
        raise LLMUnavailableError(self.message)

    async def parse(self, **_: Any) -> Any:
        raise LLMUnavailableError(self.message)
