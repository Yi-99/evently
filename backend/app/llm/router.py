"""`RoutedLLM`: send each request to a provider chosen by its `purpose`.

Lookup order for a purpose such as `slide_render`:
  1. an exact route (`slide_render=openai`),
  2. a route for its prefix, the part before the first `_` or `.` (`slide=openai`),
  3. the default provider (`LLM_PROVIDER`).

Route keys may also be a group name from `PURPOSE_GROUPS` (`presentations=openai`),
which expands to every purpose in the group. Explicit purposes win over groups.

Automatic fallback (`LLM_FALLBACK`): when the chosen provider fails for billing or credential
reasons (`LLMUnavailableError`, which includes `LLMBillingError`; optionally `LLMRateLimitError`),
the same call is re-issued on the fallback provider. Messages are always Claude-format, so
`run()` / `parse()` arguments pass through unchanged (except a pinned `model`, which is
provider-specific and dropped). The failing provider is "tripped" for `LLM_FALLBACK_COOLDOWN_S`:
calls skip it and go straight to the fallback. Once the cooldown has passed, the next call probes
it again (half-open; concurrent calls keep using the fallback meanwhile): success closes the
breaker, another billing/credential failure re-trips it for a further cooldown.
Refusals and ordinary API errors never trigger a fallback.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

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
)

log = logging.getLogger(__name__)

R = TypeVar("R")

# Every `purpose` the agents pass today, grouped by feature.
PURPOSE_GROUPS: dict[str, tuple[str, ...]] = {
    "presentations": ("deck_outline", "slide_render", "slide_edit", "presentation_edit"),
    "venues": ("venue_scout", "venue_research", "venue_summary", "card_refine"),
    "chat": ("orchestrator",),
    "intake": ("intake_extract",),
}
KNOWN_PURPOSES: tuple[str, ...] = tuple(p for group in PURPOSE_GROUPS.values() for p in group)

PROVIDER_LABELS = {"anthropic": "Claude", "openai": "OpenAI"}
_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
FALLBACK_MODES = ("auto", "none", "anthropic", "openai")
# A rate limit clears quickly, so a provider tripped by one is skipped for at most this long.
RATE_LIMIT_TRIP_MAX_S = 60.0


def expand_routes(routes: dict[str, str]) -> dict[str, str]:
    """Replace group keys with their purposes; explicit purpose routes take precedence."""
    expanded: dict[str, str] = {}
    for key, provider in routes.items():
        for purpose in PURPOSE_GROUPS.get(key, ()):
            expanded.setdefault(purpose, provider)
    for key, provider in routes.items():
        if key not in PURPOSE_GROUPS:
            expanded[key] = provider
    return expanded


@dataclass
class _Trip:
    """A provider that recently failed for billing/credential reasons."""

    until: float  # wall-clock epoch seconds
    error: LLMError
    probing: bool = False  # a half-open probe call is in flight


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


class RoutedLLM(LLMClient):
    def __init__(
        self,
        providers: dict[str, LLMClient],
        default: str,
        routes: dict[str, str] | None = None,
        *,
        fallback: str = "none",
        cooldown_s: float = 600,
        fallback_on_rate_limit: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if default not in providers:
            raise ValueError(f"default provider {default!r} is not among {sorted(providers)}")
        unknown = {p for p in (routes or {}).values() if p not in providers}
        if unknown:
            raise ValueError(f"routes name providers that were not built: {sorted(unknown)}")
        if fallback not in FALLBACK_MODES:
            raise ValueError(f"fallback must be one of {FALLBACK_MODES}, not {fallback!r}")
        self.providers = providers
        self.default = default
        self.routes = expand_routes(routes or {})
        self.fallback_mode = fallback
        self.cooldown_s = float(cooldown_s)
        self.fallback_on_rate_limit = fallback_on_rate_limit
        self._clock = clock
        self._trips: dict[str, _Trip] = {}

    @property
    def available(self) -> bool:  # type: ignore[override]
        return self.providers[self.default].available or self.fallback_for(self.default) is not None

    # -- routing -------------------------------------------------------------

    def provider_for(self, purpose: str) -> str:
        if purpose in self.routes:
            return self.routes[purpose]
        prefix = re.split(r"[_.]", purpose, maxsplit=1)[0]
        return self.routes.get(prefix, self.default)

    def client_for(self, purpose: str) -> LLMClient:
        return self.providers[self.provider_for(purpose)]

    def fallback_for(self, provider: str) -> str | None:
        """The configured, available provider that takes over when `provider` fails, if any."""
        if self.fallback_mode == "none":
            return None
        if self.fallback_mode == "auto":
            candidates = [name for name in sorted(self.providers) if name != provider]
        else:
            candidates = [self.fallback_mode] if self.fallback_mode != provider else []
        for name in candidates:
            client = self.providers.get(name)
            if client is not None and client.available:
                return name
        return None

    def _is_open(self, name: str, now: float) -> bool:
        """True while `name` is tripped and must be skipped (cooling down, or another call is probing it)."""
        trip = self._trips.get(name)
        return trip is not None and (now < trip.until or trip.probing)

    def _plan(self, primary: str) -> list[str]:
        """Providers to try for one call, in order. Pure: does not claim a half-open probe."""
        now = self._clock()
        fallback = self.fallback_for(primary)
        order = [primary] + ([fallback] if fallback else [])
        plan = [name for name in order if self.providers[name].available and not self._is_open(name, now)]
        if not plan:
            # Everything is tripped or unconfigured: try the first configured provider anyway.
            plan = [next((name for name in order if self.providers[name].available), primary)]
        return plan

    def _falls_back_on(self, exc: LLMError) -> bool:
        if isinstance(exc, LLMRefusalError):
            return False
        if isinstance(exc, LLMUnavailableError):  # billing, bad or missing credentials
            return True
        return self.fallback_on_rate_limit and isinstance(exc, LLMRateLimitError)

    def _trip(self, name: str, exc: LLMError) -> None:
        duration = self.cooldown_s
        if isinstance(exc, LLMRateLimitError):
            duration = min(duration, RATE_LIMIT_TRIP_MAX_S)
        until = self._clock() + duration
        self._trips[name] = _Trip(until=until, error=exc)
        log.warning("LLM provider %s tripped until %s: %s", name, _iso(until), exc)

    def _reset(self, name: str) -> None:
        if self._trips.pop(name, None) is not None:
            log.info("LLM provider %s is answering again; fallback no longer needed", name)

    def _no_fallback_error(self, name: str, exc: LLMBillingError) -> LLMBillingError:
        label = PROVIDER_LABELS.get(name, name)
        other = next((p for p in _KEY_ENV if p != name), None)
        how = (
            "Automatic fallback is off (LLM_FALLBACK=none)"
            if self.fallback_mode == "none"
            else "No fallback provider is configured"
        )
        hint = f"set {_KEY_ENV[other]} to fall back to {PROVIDER_LABELS[other]}, or " if other else ""
        return LLMBillingError(f"{exc} {how}: {hint}top up the {label} account.")

    def _also_failed(self, original: LLMError, name: str, exc: LLMError) -> LLMError:
        """The original error (same class, so the same HTTP status) noting that the fallback failed too."""
        return type(original)(f"{original} Fallback to {PROVIDER_LABELS.get(name, name)} also failed: {exc}")

    def _final_error(self, name: str, exc: LLMError) -> LLMError:
        """Error for a billing/credential failure that no fallback could absorb."""
        fallback = self.fallback_for(name)
        trip = self._trips.get(fallback) if fallback else None
        if fallback and trip is not None:
            label = PROVIDER_LABELS.get(fallback, fallback)
            return type(exc)(f"{exc} Fallback {label} is unavailable too: {trip.error}")
        if fallback is None and isinstance(exc, LLMBillingError):
            return self._no_fallback_error(name, exc)
        return exc

    async def _call(
        self,
        purpose: str,
        attempt: Callable[[str, LLMClient], Awaitable[R]],
        can_retry: Callable[[], bool],
    ) -> R:
        primary = self.provider_for(purpose)
        plan = self._plan(primary)
        # The first billing/credential failure of this call. When the primary is skipped because it is
        # tripped, its last failure plays that role, so a failing fallback still reports why we switched.
        original: LLMError | None = None
        if plan[0] != primary and primary in self._trips:
            original = self._trips[primary].error
        for index, name in enumerate(plan):
            trip = self._trips.get(name)
            probe = trip if trip is not None and not trip.probing and self._clock() >= trip.until else None
            if probe is not None:
                probe.probing = True  # half-open: this call tests the provider; others keep using the fallback
                log.info("LLM provider %s cooldown over; probing it with %s", name, purpose)
            try:
                result = await attempt(name, self.providers[name])
            except LLMError as exc:
                eligible = self._falls_back_on(exc)
                if eligible:
                    self._trip(name, exc)
                elif isinstance(exc, LLMRefusalError):
                    self._reset(name)  # it answered, so credits and credentials are fine
                if original is not None:
                    raise self._also_failed(original, name, exc) from exc
                if not eligible:
                    raise
                original = exc
                if index + 1 < len(plan):
                    if can_retry():
                        log.warning(
                            "LLM provider %s failed for %s (%s); retrying on %s", name, purpose, exc, plan[index + 1]
                        )
                        continue
                    raise
                final = self._final_error(name, exc)
                if final is exc:
                    raise
                raise final from exc
            finally:
                if probe is not None:
                    probe.probing = False
            self._reset(name)
            if name != primary:
                log.info("LLM %s (routed to %s) served by fallback provider %s", purpose, primary, name)
            return result
        raise AssertionError("unreachable: _plan never returns an empty plan")

    # -- LLMClient -----------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        """Configuration summary for the health endpoint (no secrets)."""
        now = self._clock()
        names = sorted({*PROVIDER_LABELS, *self.providers})
        tripped: dict[str, str | None] = {}
        reasons: dict[str, str | None] = {}
        for name in names:
            trip = self._trips.get(name)
            live = trip is not None and now < trip.until
            tripped[name] = _iso(trip.until) if live and trip else None
            reasons[name] = str(trip.error) if live and trip else None
        return {
            "default": self.default,
            "providers": {name: client.available for name, client in self.providers.items()},
            "routes": {purpose: self.provider_for(purpose) for purpose in KNOWN_PURPOSES},
            "fallback": {
                "mode": self.fallback_mode,
                "provider": self.fallback_for(self.default),  # takes over from the default provider
                "cooldown_s": self.cooldown_s,
                "tripped": tripped,  # provider -> ISO time it is skipped until (null = healthy)
                "reasons": reasons,  # provider -> the error that tripped it
                "serving_default": self._plan(self.default)[0],  # who answers default-routed calls now
            },
        }

    async def run(self, *, purpose: str, callbacks: RunCallbacks | None = None, **kwargs: Any) -> AgentResult:
        # Fallback re-issues the whole run, so it is only safe before the run had side effects. Billing and
        # credential errors surface on the first provider request (credits/keys do not change mid-run), before
        # any tool executed or text streamed; the wrapped callbacks record whether either happened and a later
        # failure is raised as is (the provider is still tripped, so the next call goes to the fallback).
        started = False

        async def on_tool_start(name: str, tool_input: dict[str, Any]) -> None:
            nonlocal started
            started = True
            if callbacks and callbacks.on_tool_start:
                await callbacks.on_tool_start(name, tool_input)

        async def on_text(delta: str) -> None:
            nonlocal started
            started = True
            if callbacks and callbacks.on_text:
                await callbacks.on_text(delta)

        tracked = RunCallbacks(
            on_text=on_text if callbacks and callbacks.on_text else None,
            on_tool_start=on_tool_start,
            on_tool_end=callbacks.on_tool_end if callbacks else None,
        )
        primary = self.provider_for(purpose)

        async def attempt(name: str, client: LLMClient) -> AgentResult:
            args = kwargs if name == primary else {k: v for k, v in kwargs.items() if k != "model"}
            return await client.run(purpose=purpose, callbacks=tracked, **args)

        return await self._call(purpose, attempt, lambda: not started)

    async def parse(self, *, purpose: str, output_model: type[T], **kwargs: Any) -> T:
        primary = self.provider_for(purpose)

        async def attempt(name: str, client: LLMClient) -> T:
            args = kwargs if name == primary else {k: v for k, v in kwargs.items() if k != "model"}
            return await client.parse(purpose=purpose, output_model=output_model, **args)

        return await self._call(purpose, attempt, lambda: True)  # a single request: no side effects

    async def close(self) -> None:
        for client in self.providers.values():
            await client.close()
