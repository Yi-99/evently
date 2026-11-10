"""Shared fixtures.

Two ways to test:

* Service / agent level (async):  use `ctx` (an isolated AppContext) and `event`.
* HTTP / WebSocket level (sync):  use `client` (Starlette TestClient). Its context is
  `client.app.state.ctx`; run coroutines on the app's loop with `client.portal.call(fn, *args)`.

Do not mix `ctx` and `client` in one test: they are separate apps with separate buses.
`ctx.llm` / the app's LLM is always the `llm` fixture (a ScriptedLLM). No test may touch the network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.context import AppContext, build_context, close_context
from app.llm.scripted import ScriptedLLM
from app.main import create_app
from app.models import Event
from tests.helpers import create_event


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        secret_key="test-secret-key-that-is-long-enough-for-hs256",
        auth_required=False,
        anthropic_api_key=None,
        google_maps_api_key=None,
        google_api_key=None,
        deepgram_api_key=None,
        assemblyai_api_key=None,
        openai_api_key=None,
        livekit_url=None,
        livekit_api_key=None,
        livekit_api_secret=None,
        redis_url=None,
        places_provider="mock",
        weather_provider="mock",
        traffic_provider="heuristic",
        stt_provider="mock",
    )


@pytest.fixture
def llm() -> ScriptedLLM:
    return ScriptedLLM()


@pytest_asyncio.fixture
async def ctx(settings: Settings, llm: ScriptedLLM) -> AsyncIterator[AppContext]:
    context = await build_context(settings, llm=llm)
    try:
        yield context
    finally:
        await close_context(context)


@pytest_asyncio.fixture
async def event(ctx: AppContext) -> Event:
    """An event owned by the dev user, for `ctx`-level tests."""
    return await create_event(ctx)


@pytest.fixture
def app(settings: Settings, llm: ScriptedLLM) -> FastAPI:
    return create_app(settings, llm=llm)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def client_event(client: TestClient) -> Event:
    """An event owned by the dev user, for `client`-level tests."""
    return client.portal.call(create_event, client.app.state.ctx)
