"""Tiny tracing facade used by the agent runtime and the LLM clients.

Both callers must keep working when the `otel` extra is not installed or when
`OTEL_ENABLED` is false, so this module never imports the OpenTelemetry API at
module level and every helper degrades to a no-op:

    from app.observability import tracing

    with tracing.start_span("agent.run", {"agent.kind": "venue_research"}) as span:
        ...
        span.set_attribute("agent.status", "succeeded")

`start_span()` makes the new span *current* for the duration of the block, so
spans opened inside it (LLM requests, httpx, SQLAlchemy) become its children.
Exceptions are never swallowed; the caller decides what to record.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

INSTRUMENTATION_NAME = "evently"

_enabled = False

AttributeValue = str | bool | int | float | list[str] | list[int] | list[float] | list[bool]


class NullSpan:
    """Stand-in returned when tracing is off; every method is a cheap no-op."""

    __slots__ = ()

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        return None

    def record_exception(self, exc: BaseException) -> None:
        return None

    def set_error(self, description: str | None = None) -> None:
        return None

    def add_event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        return None


class _Span:
    """Adapter over an OpenTelemetry span that drops `None` attribute values."""

    __slots__ = ("_span",)

    def __init__(self, span: Any) -> None:
        self._span = span

    def set_attribute(self, key: str, value: Any) -> None:
        if value is not None:
            self._span.set_attribute(key, value)

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        self._span.set_attributes({k: v for k, v in attributes.items() if v is not None})

    def record_exception(self, exc: BaseException) -> None:
        self._span.record_exception(exc)

    def set_error(self, description: str | None = None) -> None:
        from opentelemetry.trace import Status, StatusCode

        self._span.set_status(Status(StatusCode.ERROR, description))

    def add_event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        self._span.add_event(name, {k: v for k, v in (attributes or {}).items() if v is not None})


_NULL_SPAN = NullSpan()


def enable() -> None:
    """Called by `instrument_app()` once a TracerProvider is installed."""
    global _enabled
    _enabled = True


def disable() -> None:
    global _enabled
    _enabled = False


def is_enabled() -> bool:
    return _enabled


@contextmanager
def start_span(name: str, attributes: Mapping[str, Any] | None = None) -> Iterator[NullSpan | _Span]:
    """Open `name` as the current span. Yields a `NullSpan` when tracing is off.

    The span is ended when the block exits, whatever the reason. Exceptions are
    *not* recorded automatically: call `span.record_exception()` / `set_error()`
    where the failure is handled, so cancellations and expected errors are not
    all painted red.
    """
    if not _enabled:
        yield _NULL_SPAN
        return
    try:
        from opentelemetry import trace
    except ImportError:  # pragma: no cover - `otel` extra missing but enabled flag set
        yield _NULL_SPAN
        return

    tracer = trace.get_tracer(INSTRUMENTATION_NAME)
    clean = {k: v for k, v in (attributes or {}).items() if v is not None}
    span = tracer.start_span(name, attributes=clean)
    with trace.use_span(span, end_on_exit=True, record_exception=False, set_status_on_exception=False):
        yield _Span(span)


def _current_span_context() -> Any | None:
    if not _enabled:
        return None
    try:
        from opentelemetry import trace
    except ImportError:  # pragma: no cover
        return None
    ctx = trace.get_current_span().get_span_context()
    return ctx if ctx.is_valid else None


def current_trace_id() -> str | None:
    """Hex trace id (32 chars) of the current span; None when tracing is off or no span is active.

    Safe to call from anywhere (bus `publish()`, log formatters): it reads the contextvar the
    enclosing request / `agent.run` / `llm.request` span installed and never raises.
    """
    ctx = _current_span_context()
    return format(ctx.trace_id, "032x") if ctx is not None else None


def current_traceparent() -> str | None:
    """W3C `traceparent` header for the current span (`00-<trace_id>-<span_id>-<flags>`), or None.

    Meant for envelopes / out-of-band messages so a browser or worker can continue the trace.
    """
    ctx = _current_span_context()
    if ctx is None:
        return None
    return f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-{int(ctx.trace_flags):02x}"
