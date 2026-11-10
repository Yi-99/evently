"""Application metrics: cheap no-op helpers until `instrument_app()` turns them on.

Call sites (other packages own these files; the hooks are listed in docs/OBSERVABILITY.md):

    from app.observability import metrics

    metrics.agent_run_started(kind)              # AgentRuntime, when a run starts
    metrics.agent_run_finished(kind, status)     # AgentRuntime, on any terminal status
    metrics.llm_tokens(model, usage)             # app/llm/telemetry.py, per LLM response (any provider)
    metrics.audio_frames(codec, sample_rate)     # AudioSource.publish()
    metrics.audio_frames_dropped(n)              # BoundedAudioQueue overflow / listener leaves
    metrics.stt_latency(provider, seconds, final)   # STT pipeline, per transcript segment
    metrics.slide_decision_latency(seconds, outcome) # SlideTracker, per transcript evaluated

Instruments (all under the `evently` meter):

    evently.agent_runs.active        UpDownCounter {agent.kind}
    evently.agent_runs.finished      Counter       {agent.kind, agent.status}
    evently.llm.requests             Counter       {gen_ai.request.model, gen_ai.response.finish_reason}
    evently.llm.tokens               Counter       {gen_ai.request.model, gen_ai.token.type}
    evently.audio.frames             Counter       {audio.codec, audio.sample_rate}
    evently.audio.frames_dropped     Counter       {}
    evently.audio.listeners          UpDownCounter {}
    evently.stt.latency              Histogram (s) {stt.provider, stt.final}
    evently.slide.decision_latency   Histogram (s) {slide.outcome}
    evently.slide.changes            Counter       {slide.trigger}

Attribute sets are deliberately low-cardinality: no run ids, session ids or
event ids on metrics (those belong on spans).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

METER_NAME = "evently"

# Usage keys (Anthropic names; other providers map onto them) → `gen_ai.token.type` values.
_TOKEN_TYPES = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_read_input_tokens": "cache_read",
    "cache_creation_input_tokens": "cache_creation",
}


class _Instruments:
    """Holds the real OTel instruments; only created by `enable()`."""

    def __init__(self, meter: Any) -> None:
        self.agent_runs_active = meter.create_up_down_counter(
            "evently.agent_runs.active", unit="{run}", description="Agent runs currently queued or running"
        )
        self.agent_runs_finished = meter.create_counter(
            "evently.agent_runs.finished", unit="{run}", description="Agent runs that reached a terminal status"
        )
        self.llm_requests = meter.create_counter(
            "evently.llm.requests", unit="{request}", description="LLM requests (one per streamed turn), any provider"
        )
        self.llm_tokens = meter.create_counter(
            "evently.llm.tokens", unit="{token}", description="LLM tokens by model and token type"
        )
        self.audio_frames = meter.create_counter(
            "evently.audio.frames", unit="{frame}", description="Speaker audio frames accepted by the audio hub"
        )
        self.audio_frames_dropped = meter.create_counter(
            "evently.audio.frames_dropped",
            unit="{frame}",
            description="Audio frames dropped because a listener could not keep up",
        )
        self.audio_listeners = meter.create_up_down_counter(
            "evently.audio.listeners", unit="{listener}", description="Connected audio listeners"
        )
        self.stt_latency = meter.create_histogram(
            "evently.stt.latency",
            unit="s",
            description="Time from the end of the audio a transcript segment covers to its arrival",
        )
        self.slide_decision_latency = meter.create_histogram(
            "evently.slide.decision_latency",
            unit="s",
            description="Time the slide tracker takes to decide on a transcript (change / no change)",
        )
        self.slide_changes = meter.create_counter(
            "evently.slide.changes", unit="{change}", description="Automatic slide changes by trigger type"
        )


_instruments: _Instruments | None = None


def enable(meter_provider: Any | None = None) -> None:
    """Create the instruments. Called by `instrument_app()`; safe to call twice."""
    global _instruments
    if _instruments is not None:
        return
    from opentelemetry import metrics as otel_metrics

    provider = meter_provider or otel_metrics.get_meter_provider()
    _instruments = _Instruments(provider.get_meter(METER_NAME))


def disable() -> None:
    global _instruments
    _instruments = None


def is_enabled() -> bool:
    return _instruments is not None


# -- agents -------------------------------------------------------------------


def agent_run_started(kind: str) -> None:
    if _instruments is None:
        return
    _instruments.agent_runs_active.add(1, {"agent.kind": kind})


def agent_run_finished(kind: str, status: str) -> None:
    """`status` is the terminal AgentRun status: succeeded | failed | cancelled."""
    if _instruments is None:
        return
    _instruments.agent_runs_active.add(-1, {"agent.kind": kind})
    _instruments.agent_runs_finished.add(1, {"agent.kind": kind, "agent.status": status})


# -- LLM ----------------------------------------------------------------------


def llm_request(model: str, stop_reason: str | None) -> None:
    if _instruments is None:
        return
    _instruments.llm_requests.add(
        1, {"gen_ai.request.model": model, "gen_ai.response.finish_reason": stop_reason or "unknown"}
    )


def llm_tokens(model: str, usage: Mapping[str, int | None] | None) -> None:
    """Record one response's usage dict (Anthropic field names, any provider) as `evently.llm.tokens`."""
    if _instruments is None or not usage:
        return
    for key, token_type in _TOKEN_TYPES.items():
        count = usage.get(key) or 0
        if count:
            _instruments.llm_tokens.add(int(count), {"gen_ai.request.model": model, "gen_ai.token.type": token_type})


# -- audio --------------------------------------------------------------------


def audio_frames(codec: str, sample_rate: int, count: int = 1) -> None:
    if _instruments is None:
        return
    _instruments.audio_frames.add(count, {"audio.codec": codec, "audio.sample_rate": sample_rate})


def audio_frames_dropped(count: int = 1) -> None:
    if _instruments is None or count <= 0:
        return
    _instruments.audio_frames_dropped.add(count)


def audio_listener_joined() -> None:
    if _instruments is None:
        return
    _instruments.audio_listeners.add(1)


def audio_listener_left() -> None:
    if _instruments is None:
        return
    _instruments.audio_listeners.add(-1)


# -- transcription + slide tracking ----------------------------------------------


def stt_latency(provider: str, seconds: float, *, final: bool) -> None:
    """Latency of one transcript segment: now - (speaker timestamp of the audio it covers)."""
    if _instruments is None or seconds < 0:
        return
    _instruments.stt_latency.record(seconds, {"stt.provider": provider, "stt.final": final})


def slide_decision_latency(seconds: float, outcome: str) -> None:
    """`outcome`: changed | no_match | ignored (the tracker's decision for one transcript)."""
    if _instruments is None or seconds < 0:
        return
    _instruments.slide_decision_latency.record(seconds, {"slide.outcome": outcome})


def slide_changed(trigger: str) -> None:
    """`trigger`: voice_command | keyword | script_alignment | manual."""
    if _instruments is None:
        return
    _instruments.slide_changes.add(1, {"slide.trigger": trigger})
