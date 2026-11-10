"""Tracing + metrics shared by every LLM provider client.

Each provider opens one `llm.request` span per request (GenAI semantic
conventions) and reports usage under the Anthropic-style usage keys in
`USAGE_KEYS`, so dashboards and `AgentRun` usage stay comparable whichever
provider served a request.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from app.observability import metrics, tracing

USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def empty_usage() -> dict[str, int]:
    return dict.fromkeys(USAGE_KEYS, 0)


@contextmanager
def llm_span(
    provider: str, purpose: str, model: str, tier: str, max_tokens: int, *, structured: bool = False
) -> Iterator[Any]:
    """`llm.request` span around one model request. No-op when OTel is off."""
    attributes = {
        "gen_ai.system": provider,
        "gen_ai.provider.name": provider,
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": model,
        "gen_ai.request.max_tokens": max_tokens,
        "evently.llm.purpose": purpose,
        "evently.llm.tier": tier,
        "evently.llm.structured_output": structured,
    }
    with tracing.start_span("llm.request", attributes) as span:
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_error(f"{type(exc).__name__}: {exc}")
            raise


def record_response(
    span: Any,
    model: str,
    *,
    usage: Mapping[str, int],
    stop_reason: str | None,
    response_model: str | None = None,
    response_id: str | None = None,
) -> None:
    """Token usage + stop reason onto the span and the `evently.llm.*` metrics.

    `usage` uses the `USAGE_KEYS` names; `stop_reason` uses the normalised values
    (end_turn | tool_use | max_tokens | refusal | …) so metrics are provider-neutral.
    """
    span.set_attributes(
        {
            "gen_ai.response.model": response_model,
            "gen_ai.response.id": response_id,
            "gen_ai.response.finish_reasons": [stop_reason] if stop_reason else None,
            "gen_ai.usage.input_tokens": usage.get("input_tokens", 0),
            "gen_ai.usage.output_tokens": usage.get("output_tokens", 0),
            "gen_ai.usage.cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
            "gen_ai.usage.cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        }
    )
    metrics.llm_request(model, stop_reason)
    metrics.llm_tokens(model, usage)
