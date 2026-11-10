"""Liveness and configuration summary.

Cheap by design: it only reads settings and in-process state, never calls out over the network.
"""

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.deps import Ctx
from app.integrations.email_providers import resolve_email_provider_name
from app.transcription.factory import resolve_provider_name

router = APIRouter(tags=["health"])


class SttKeys(BaseModel):
    """Which hosted speech-to-text providers have an API key."""

    deepgram: bool
    assemblyai: bool
    openai: bool


class HealthOut(BaseModel):
    status: str = Field(description='Always "ok" when the API answers.')
    environment: str
    llm_configured: bool
    llm: dict[str, Any] | None = Field(
        description="Configured LLM providers, purpose routing and fallback state (`fallback.tripped`: provider -> "
        "ISO time it is skipped until, e.g. Claude out of credits; `fallback.serving_default`) when routed."
    )
    maps_configured: bool
    stt_keys: SttKeys
    stt_provider: str = Field(
        description='Default transcription provider after resolving "auto": deepgram | assemblyai | openai | '
        'browser (the client transcribes with the Web Speech API) | mock.'
    )
    livekit_configured: bool
    email_configured: bool = Field(description="A Resend API key or an SMTP host is set.")
    email_provider: str = Field(
        description='Provider used to send email: resend | smtp | console (dev: logged, not sent) | '
        'none (production without credentials).'
    )
    luma_configured: bool
    search_configured: bool = Field(description="An Elasticsearch URL is set (search may still be unreachable).")
    otel_enabled: bool
    active_agent_runs: int


@router.get("/health", response_model=HealthOut)
async def health(ctx: Ctx) -> HealthOut:
    s = ctx.settings
    describe = getattr(ctx.llm, "describe", None)  # RoutedLLM: providers configured + purpose routing
    return HealthOut(
        status="ok",
        environment=s.environment,
        llm_configured=ctx.llm.available,
        llm=describe() if callable(describe) else None,
        maps_configured=bool(s.maps_api_key),
        stt_keys=SttKeys(
            deepgram=bool(s.deepgram_api_key),
            assemblyai=bool(s.assemblyai_api_key),
            openai=bool(s.openai_api_key),
        ),
        stt_provider=resolve_provider_name(s),
        livekit_configured=bool(s.livekit_url and s.livekit_api_key and s.livekit_api_secret),
        email_configured=bool(s.resend_api_key or s.smtp_host),
        email_provider=resolve_email_provider_name(s),
        luma_configured=bool(s.luma_api_key),
        search_configured=bool(s.elasticsearch_url),
        otel_enabled=s.otel_enabled,
        active_agent_runs=len(ctx.runtime.active_run_ids()),
    )
