"""OpenAI implementation of `LLMClient` (official `openai` SDK, async, Responses API, streaming).

The rest of the app speaks the Claude message format, so this module translates
at its edge:

- in: Claude messages → Responses input items (`message`, `function_call`,
  `function_call_output`). Blocks with no equivalent (thinking, Claude web
  search results, images) are dropped.
- out: Responses output items → Claude blocks (`text`, `tool_use`) plus
  `tool_result` user messages in `AgentResult.new_messages`, so persisted
  history replays on either provider.

Inside one `run()` the model's own output items (including encrypted reasoning)
are replayed unchanged, so its reasoning carries across tool calls. Requests are
stateless (`store=False`); nothing is kept server-side between calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import openai
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.llm.base import (
    AgentResult,
    LLMBillingError,
    LLMClient,
    LLMError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMUnavailableError,
    RunCallbacks,
    T,
    Tier,
    Tool,
    ToolCallRecord,
    execute_tool,
)
from app.llm.telemetry import empty_usage, llm_span, record_response

log = logging.getLogger(__name__)

PROVIDER = "openai"
_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
# Fields the SDK adds to parsed output objects that the API does not accept back as input.
_SDK_ONLY_KEYS = ("parsed", "parsed_arguments")
_TRUNCATED_TOOL_INPUT = "Tool input was cut off by the output limit. Retry with a shorter input."


def has_credentials(settings: Settings) -> bool:
    return bool(settings.openai_api_key)


_BILLING_CODES = {"insufficient_quota", "billing_hard_limit_reached", "billing_not_active"}
_BILLING_WORDS = ("billing", "quota", "credit balance", "insufficient")


def _billing_error(exc: Exception) -> LLMBillingError | None:
    """`LLMBillingError` when OpenAI refused the request over quota/billing, else None.

    - any status whose error `code` or `type` is `insufficient_quota` (sent with 429),
      `billing_hard_limit_reached` or `billing_not_active`;
    - 402;
    - 400/403 whose message mentions "billing", "quota", "credit balance" or "insufficient".
    """
    if not isinstance(exc, openai.APIStatusError):
        return None
    status = exc.status_code
    codes = {getattr(exc, "code", None), getattr(exc, "type", None)}
    if (
        codes & _BILLING_CODES
        or status == 402
        or (status in (400, 403) and any(w in exc.message.lower() for w in _BILLING_WORDS))
    ):
        body = exc.body if isinstance(exc.body, dict) else {}
        detail = body.get("message") or exc.message
        return LLMBillingError(f"OpenAI is out of quota or blocked by billing (HTTP {status}): {detail}")
    return None


# -- Claude format → Responses input ------------------------------------------------


def _as_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", exclude_none=True)
    return {}


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in map(_as_dict, content) if b.get("type") == "text")
    return "" if content is None else json.dumps(content, default=str)


def _function_output(call_id: str, text: str, is_error: bool) -> dict[str, Any]:
    # The Responses API has no error flag on tool output, so say it in the text.
    return {"type": "function_call_output", "call_id": call_id, "output": f"Error: {text}" if is_error else text}


def to_openai_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Claude-format messages → Responses API input items."""
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content")
        if isinstance(content, str):
            if content:
                items.append({"type": "message", "role": role, "content": content})
            continue

        texts: list[str] = []

        def flush(role: str = role, texts: list[str] = texts) -> None:
            if texts:
                items.append({"type": "message", "role": role, "content": "\n\n".join(texts)})
                texts.clear()

        for block in map(_as_dict, content or []):
            kind = block.get("type")
            if kind == "text":
                if block.get("text"):
                    texts.append(block["text"])
            elif kind == "tool_use" and role == "assistant":
                flush()
                items.append(
                    {
                        "type": "function_call",
                        "call_id": block["id"],
                        "name": block["name"],
                        "arguments": json.dumps(block.get("input") or {}),
                    }
                )
            elif kind == "tool_result" and role == "user":
                flush()
                items.append(
                    _function_output(
                        block["tool_use_id"], _tool_result_text(block.get("content")), bool(block.get("is_error"))
                    )
                )
            # thinking / redacted_thinking / server_tool_use / web_search_tool_result / images: dropped.
        flush()
    return _pair_calls(items)


def _pair_calls(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The API rejects a function call without an output and vice versa; repair either case."""
    calls = {i["call_id"] for i in items if i["type"] == "function_call"}
    outputs = {i["call_id"] for i in items if i["type"] == "function_call_output"}
    paired: list[dict[str, Any]] = []
    for item in items:
        if item["type"] == "function_call_output" and item["call_id"] not in calls:
            continue
        paired.append(item)
        if item["type"] == "function_call" and item["call_id"] not in outputs:
            paired.append(_function_output(item["call_id"], "No result was recorded for this call.", True))
    return paired


# -- Responses output → Claude format -------------------------------------------------


def _parse_arguments(arguments: str) -> Any:
    """Tool arguments as JSON; the raw string when they do not parse (`execute_tool` reports it)."""
    try:
        return json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return arguments


def _claude_blocks(output: list[Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for item in output:
        if item.type == "message":
            blocks.extend(
                {"type": "text", "text": part.text} for part in item.content if part.type == "output_text" and part.text
            )
        elif item.type == "function_call":
            arguments = _parse_arguments(item.arguments)
            blocks.append(
                {
                    "type": "tool_use",
                    "id": item.call_id,
                    "name": item.name,
                    "input": arguments if isinstance(arguments, dict) else {},
                }
            )
        # reasoning / web_search_call: no Claude-format equivalent worth persisting.
    return blocks


def _replay_item(item: Any) -> dict[str, Any]:
    """An output item as an input item for the next request of the same loop."""
    data = item.to_dict(mode="json", exclude_none=True)
    for key in _SDK_ONLY_KEYS:
        data.pop(key, None)
    for part in data.get("content") or []:
        if isinstance(part, dict):
            for key in _SDK_ONLY_KEYS:
                part.pop(key, None)
    return data


def _refusal(response: Any) -> str | None:
    for item in response.output or []:
        if item.type == "message":
            for part in item.content:
                if part.type == "refusal":
                    return part.refusal or "refused"
    return None


def _stop_reason(response: Any) -> str:
    """Normalised to the Claude stop reasons the rest of the app (and the metrics) use."""
    if response.status == "incomplete":
        reason = getattr(response.incomplete_details, "reason", None)
        return "refusal" if reason == "content_filter" else "max_tokens"
    if _refusal(response):
        return "refusal"
    if any(item.type == "function_call" for item in response.output or []):
        return "tool_use"
    return "end_turn"


def _usage(response: Any) -> dict[str, int]:
    """Usage under the Anthropic key names; `input_tokens` excludes cache reads and writes, as there."""
    usage = empty_usage()
    raw = response.usage
    if raw is None:
        return usage
    details = raw.input_tokens_details
    cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
    written = (getattr(details, "cache_write_tokens", 0) or 0) if details else 0
    usage["input_tokens"] = max(0, (raw.input_tokens or 0) - cached - written)
    usage["output_tokens"] = raw.output_tokens or 0
    usage["cache_read_input_tokens"] = cached
    usage["cache_creation_input_tokens"] = written
    return usage


def _final_text(response: Any) -> str:
    """Text of the last final-answer message (ignores `commentary` preambles)."""
    messages = [i for i in response.output or [] if i.type == "message"]
    final = [m for m in messages if getattr(m, "phase", None) in (None, "final_answer")] or messages
    if not final:
        return ""
    return "".join(part.text for part in final[-1].content if part.type == "output_text")


# -- request shapes -------------------------------------------------------------------


def _function_tool(tool: Tool) -> dict[str, Any]:
    # Not strict: strict mode needs every property required. `execute_tool` validates instead.
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.input_model.model_json_schema(),
        "strict": False,
    }


def _server_tools(server_tools: list[dict[str, Any]] | None) -> tuple[list[dict[str, Any]], int | None]:
    """Anthropic server-tool definitions → OpenAI hosted tools (+ a built-in tool call cap)."""
    tools: list[dict[str, Any]] = []
    max_calls: int | None = None
    for tool in server_tools or []:
        kind = str(tool.get("type", ""))
        if kind.startswith("web_search"):
            tools.append({"type": "web_search"})
            max_calls = tool.get("max_uses") or max_calls
        else:
            log.warning("OpenAI has no equivalent for server tool %r; skipping it", kind)
    return tools, max_calls


def _strip_defaults(node: Any) -> Any:
    """Drop `default` keywords (meaningless once strict mode makes every field required)."""
    if isinstance(node, list):
        return [_strip_defaults(x) for x in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "default":
            continue
        if key in ("properties", "$defs", "definitions") and isinstance(value, dict):
            out[key] = {name: _strip_defaults(schema) for name, schema in value.items()}
        else:
            out[key] = _strip_defaults(value)
    return out


def _text_format(output_model: type[BaseModel], *, strict: bool) -> dict[str, Any]:
    if strict:
        schema = _strip_defaults(openai.pydantic_function_tool(output_model)["function"]["parameters"])
    else:
        schema = output_model.model_json_schema()
    return {"type": "json_schema", "name": output_model.__name__[:64], "schema": schema, "strict": strict}


class OpenAILLM(LLMClient):
    available = True

    def __init__(self, settings: Settings, client: openai.AsyncOpenAI | None = None) -> None:
        self._settings = settings
        self._client = client or openai.AsyncOpenAI(api_key=settings.openai_api_key)

    # -- request plumbing ---------------------------------------------------

    def _resolve(self, tier: Tier, model: str | None, effort: str | None) -> tuple[str, str]:
        s = self._settings
        if model and model.startswith("claude"):
            model = None  # a Claude model id pinned by a caller means "this tier's default" here
        if tier == "orchestrator":
            return model or s.openai_orchestrator_model, effort or s.llm_orchestrator_effort
        return model or s.openai_worker_model, effort or s.llm_worker_effort

    @staticmethod
    def _request(purpose: str, model: str, effort: str, system: str, max_tokens: int) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "instructions": system,
            "max_output_tokens": max_tokens,
            "store": False,
            # Stateless requests: the reasoning comes back encrypted so the tool loop can replay it.
            "include": ["reasoning.encrypted_content"],
            # Automatic prompt caching; a stable key per purpose keeps the shared prefix on one cache shard.
            "prompt_cache_key": f"evently-{purpose}",
        }
        if effort in _EFFORTS:
            kwargs["reasoning"] = {"effort": effort}
        else:
            log.warning("unknown reasoning effort %r for OpenAI; using the model default", effort)
        return kwargs

    @staticmethod
    def _translate(exc: Exception) -> LLMError:
        billing = _billing_error(exc)
        if billing is not None:
            return billing
        if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
            return LLMUnavailableError(f"OpenAI rejected the configured credentials: {exc}")
        if isinstance(exc, openai.RateLimitError):
            return LLMRateLimitError("OpenAI is rate limiting requests; try again shortly.")
        if isinstance(exc, openai.APIStatusError):
            return LLMError(f"OpenAI API error {exc.status_code}: {exc.message}")
        if isinstance(exc, openai.APIConnectionError):
            return LLMError("Could not reach the OpenAI API.")
        return LLMError(str(exc))

    async def _stream_turn(self, kwargs: dict[str, Any], callbacks: RunCallbacks | None) -> Any:
        """One streamed request; returns the final (completed or incomplete) response."""
        final: Any = None
        try:
            async with self._client.responses.stream(**kwargs) as stream:
                async for event in stream:
                    if event.type == "response.output_text.delta":
                        if callbacks and callbacks.on_text:
                            await callbacks.on_text(event.delta)
                    elif event.type in ("response.completed", "response.incomplete", "response.failed"):
                        final = event.response
                    elif event.type == "error":
                        raise LLMError(f"OpenAI stream error: {getattr(event, 'message', '') or event}")
        except openai.APIError as exc:
            raise self._translate(exc) from exc
        if final is None:
            raise LLMError("The OpenAI stream ended without a final response.")
        if final.status == "failed":
            error = getattr(final.error, "message", None) or "unknown error"
            raise LLMError(f"OpenAI response failed: {error}")
        return final

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
        model, effort = self._resolve(tier, model, effort)
        kwargs = self._request(purpose, model, effort, system, max_tokens)
        tool_map = {t.name: t for t in tools or []}
        hosted, max_calls = _server_tools(server_tools)
        tool_defs = [_function_tool(t) for t in tool_map.values()] + hosted
        if tool_defs:
            kwargs["tools"] = tool_defs
        if hosted and max_calls:
            kwargs["max_tool_calls"] = max_calls

        history = to_openai_input(messages)
        result = AgentResult(text="", stop_reason="max_turns", usage=empty_usage())

        for _ in range(max_turns):
            with llm_span(PROVIDER, purpose, model, tier, max_tokens) as span:
                response = await self._stream_turn({**kwargs, "input": history}, callbacks)
                stop_reason = _stop_reason(response)
                usage = _usage(response)
                record_response(
                    span,
                    model,
                    usage=usage,
                    stop_reason=stop_reason,
                    response_model=response.model,
                    response_id=response.id,
                )
            result.turns += 1
            for key, count in usage.items():
                result.usage[key] += count

            if stop_reason == "refusal":
                raise LLMRefusalError(f"OpenAI declined the request ({purpose}).")

            output = list(response.output or [])
            history.extend(_replay_item(item) for item in output)
            blocks = _claude_blocks(output)
            if blocks:
                result.new_messages.append({"role": "assistant", "content": blocks})
            text = "".join(b["text"] for b in blocks if b["type"] == "text")
            if text:
                result.text = text

            calls = [item for item in output if item.type == "function_call"]
            if not calls:
                result.stop_reason = stop_reason
                break

            if stop_reason == "max_tokens":
                # A truncated call's arguments may still parse; never run it.
                records = [ToolCallRecord(c.name, {}, _TRUNCATED_TOOL_INPUT, True) for c in calls]
            else:
                records = await asyncio.gather(
                    *(execute_tool(tool_map, c.name, _parse_arguments(c.arguments), callbacks) for c in calls)
                )
            result.tool_calls.extend(records)
            history.extend(
                _function_output(c.call_id, r.result, r.is_error) for c, r in zip(calls, records, strict=True)
            )
            result.new_messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": c.call_id, "content": r.result, "is_error": r.is_error}
                        for c, r in zip(calls, records, strict=True)
                    ],
                }
            )

        log.info(
            "llm.run provider=openai purpose=%s model=%s turns=%d usage=%s", purpose, model, result.turns, result.usage
        )
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
        model, effort = self._resolve(tier, model, effort)
        kwargs = {**self._request(purpose, model, effort, system, max_tokens), "input": to_openai_input(messages)}
        response: Any = None
        for strict in (True, False):
            with llm_span(PROVIDER, purpose, model, tier, max_tokens, structured=True) as span:
                try:
                    # Streamed so long structured answers (slide HTML) cannot hit HTTP timeouts.
                    response = await self._stream_turn(
                        {**kwargs, "text": {"format": _text_format(output_model, strict=strict)}}, None
                    )
                except LLMError as exc:
                    cause = exc.__cause__
                    if strict and isinstance(cause, openai.BadRequestError) and "schema" in str(cause).lower():
                        log.warning("OpenAI rejected the strict schema for %s; retrying non-strict", purpose)
                        continue
                    raise
                stop_reason = _stop_reason(response)
                record_response(
                    span,
                    model,
                    usage=_usage(response),
                    stop_reason=stop_reason,
                    response_model=response.model,
                    response_id=response.id,
                )
            break

        if stop_reason == "refusal":
            raise LLMRefusalError(f"OpenAI declined the request ({purpose}).")
        if stop_reason == "max_tokens":
            raise LLMError(f"Structured answer for '{purpose}' was cut off; raise max_tokens.")
        try:
            return output_model.model_validate_json(_final_text(response))
        except ValidationError as exc:
            raise LLMError(f"OpenAI returned an invalid structured answer for '{purpose}': {exc}") from exc

    async def close(self) -> None:
        await self._client.close()
