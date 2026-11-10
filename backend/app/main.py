"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from importlib import import_module
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import Settings, get_settings
from app.core.context import build_context, close_context
from app.llm.base import LLMClient, LLMError, LLMRateLimitError, LLMRefusalError, LLMUnavailableError
from app.realtime.bus import EventBus

# REST routers are mounted under /api/v1; WebSocket routers declare full /ws/... paths.
REST_ROUTERS = [
    "app.api.health",
    "app.api.auth",
    "app.api.events",
    "app.api.board",
    "app.api.stream",
    "app.api.agent_runs",
    "app.api.venues",
    "app.api.chat",
    "app.api.presentations",
    "app.api.live_sessions",
    "app.api.outreach",
    "app.api.search",
    "app.api.intake",
]
WS_ROUTERS = [
    "app.api.ws_events",
    "app.api.ws_live",
    "app.api.ws_audio",
]
DEV_CONSOLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "dev-console"


def create_app(
    settings: Settings | None = None, *, llm: LLMClient | None = None, bus: EventBus | None = None
) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.ctx = await build_context(settings, llm=llm, bus=bus)
        if settings.elasticsearch_url:
            from app.search.indexer import start_indexer

            await start_indexer(app.state.ctx)
        try:
            yield
        finally:
            await close_context(app.state.ctx)

    app = FastAPI(title="Evently API", version="0.1.0", lifespan=lifespan)
    if settings.otel_enabled:
        from app.observability.otel import instrument_app

        instrument_app(app, settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(LLMUnavailableError)
    async def _llm_unavailable(_: Request, exc: LLMUnavailableError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(LLMRateLimitError)
    async def _llm_rate_limited(_: Request, exc: LLMRateLimitError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=429)

    @app.exception_handler(LLMRefusalError)
    async def _llm_refused(_: Request, exc: LLMRefusalError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(LLMError)
    async def _llm_error(_: Request, exc: LLMError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=502)

    for module in REST_ROUTERS:
        app.include_router(import_module(module).router, prefix="/api/v1")
    for module in WS_ROUTERS:
        app.include_router(import_module(module).router)

    if settings.environment != "production" and DEV_CONSOLE_DIR.is_dir():
        app.mount("/dev", StaticFiles(directory=DEV_CONSOLE_DIR, html=True), name="dev-console")

    return app


def app_factory() -> FastAPI:
    """Entry point for `uvicorn app.main:app_factory --factory`."""
    return create_app()
