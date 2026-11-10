"""Build the app's `LLMClient` from settings: one client per provider in use, routed by purpose."""

from __future__ import annotations

import logging

from app.core.config import Settings
from app.llm import anthropic_client, openai_client
from app.llm.base import LLMClient, UnconfiguredLLM
from app.llm.router import RoutedLLM

log = logging.getLogger(__name__)

_MISSING = {
    "anthropic": "Claude is not configured. Set ANTHROPIC_API_KEY in .env.",
    "openai": "OpenAI is not configured. Set OPENAI_API_KEY in .env.",
}


def _has_credentials(name: str, settings: Settings) -> bool:
    if name == "anthropic":
        return anthropic_client.has_credentials(settings)
    if name == "openai":
        return openai_client.has_credentials(settings)
    raise ValueError(f"Unknown LLM provider {name!r}")


def _build_provider(name: str, settings: Settings) -> LLMClient:
    if name == "anthropic":
        if anthropic_client.has_credentials(settings):
            return anthropic_client.AnthropicLLM(settings)
    elif name == "openai":
        if openai_client.has_credentials(settings):
            return openai_client.OpenAILLM(settings)
    else:
        raise ValueError(f"Unknown LLM provider {name!r}")
    return UnconfiguredLLM(_MISSING[name])


def build_llm(settings: Settings) -> RoutedLLM:
    """Clients for `LLM_PROVIDER`, every provider named in `LLM_PROVIDER_OVERRIDES`, and the fallback.

    A routed provider without credentials becomes an `UnconfiguredLLM` naming the env var to set; when
    that is the default provider and no fallback is configured, `available` is False and the LLM
    endpoints answer 503. Fallback providers (`LLM_FALLBACK`) are only built when they have credentials.
    """
    routes = settings.llm_routes
    names = {settings.llm_provider, *routes.values()}
    providers = {name: _build_provider(name, settings) for name in sorted(names)}
    for name, client in providers.items():
        if not client.available:
            log.warning("LLM provider %s has no credentials: %s", name, _MISSING[name])
    if settings.llm_fallback == "auto":
        extra = set(_MISSING) - names
    elif settings.llm_fallback == "none":
        extra = set()
    else:
        extra = {settings.llm_fallback} - names
    for name in sorted(extra):
        if _has_credentials(name, settings):
            providers[name] = _build_provider(name, settings)
        elif settings.llm_fallback != "auto":
            log.warning("LLM fallback provider %s has no credentials: %s", name, _MISSING[name])
    return RoutedLLM(
        providers,
        default=settings.llm_provider,
        routes=routes,
        fallback=settings.llm_fallback,
        cooldown_s=settings.llm_fallback_cooldown_s,
        fallback_on_rate_limit=settings.llm_fallback_on_rate_limit,
    )
