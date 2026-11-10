"""`AppContext`: the one object that carries shared infrastructure.

Routers read it from `app.state.ctx`; background agents get it passed in.
Nothing here is a module-level global, so every test builds an isolated one.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.db import create_engine, create_sessionmaker, create_tables
from app.llm.base import LLMClient
from app.realtime.bus import EventBus, InMemoryEventBus

if TYPE_CHECKING:
    from app.agents.runtime import AgentRuntime
    from app.providers.base import PlacesProvider, TrafficProvider, WeatherProvider

log = logging.getLogger(__name__)
S = TypeVar("S")


@dataclass
class AppContext:
    settings: Settings
    engine: AsyncEngine
    db: async_sessionmaker[AsyncSession]  # usage: `async with ctx.db() as session:`
    bus: EventBus
    llm: LLMClient
    runtime: AgentRuntime = field(default=None)  # type: ignore[assignment]  (set by build_context)
    _services: dict[str, Any] = field(default_factory=dict)
    _shutdown_hooks: list[Callable[[], Any]] = field(default_factory=list)
    _places: PlacesProvider | None = None
    _weather: WeatherProvider | None = None
    _traffic: TrafficProvider | None = None

    # -- data providers (built on first use; assign to override in tests) ---

    @property
    def places(self) -> PlacesProvider:
        if self._places is None:
            from app.providers.places import build_places_provider

            self._places = build_places_provider(self.settings)
        return self._places

    @places.setter
    def places(self, provider: PlacesProvider) -> None:
        self._places = provider

    @property
    def weather(self) -> WeatherProvider:
        if self._weather is None:
            from app.providers.weather import build_weather_provider

            self._weather = build_weather_provider(self.settings)
        return self._weather

    @weather.setter
    def weather(self, provider: WeatherProvider) -> None:
        self._weather = provider

    @property
    def traffic(self) -> TrafficProvider:
        if self._traffic is None:
            from app.providers.traffic import build_traffic_provider

            self._traffic = build_traffic_provider(self.settings)
        return self._traffic

    @traffic.setter
    def traffic(self, provider: TrafficProvider) -> None:
        self._traffic = provider

    # -- per-app singletons ---------------------------------------------------

    def service(self, key: str, factory: Callable[[], S]) -> S:
        """Get or lazily create an app-scoped singleton (audio hub, locks, trackers…)."""
        if key not in self._services:
            self._services[key] = factory()
        return self._services[key]

    def on_shutdown(self, hook: Callable[[], Any]) -> None:
        """Register a sync or async callable to run when the app stops."""
        self._shutdown_hooks.append(hook)


async def build_context(
    settings: Settings, *, llm: LLMClient | None = None, bus: EventBus | None = None
) -> AppContext:
    from app.agents.runtime import AgentRuntime
    from app.llm.factory import build_llm

    engine = create_engine(settings.database_url)
    await create_tables(engine)

    if bus is None:
        if settings.redis_url:
            from app.realtime.redis_bus import build_redis_bus

            bus = await build_redis_bus(settings)
        else:
            bus = InMemoryEventBus(history_size=settings.bus_history_size)

    ctx = AppContext(
        settings=settings,
        engine=engine,
        db=create_sessionmaker(engine),
        bus=bus,
        llm=llm or build_llm(settings),
    )
    ctx.runtime = AgentRuntime(ctx)
    return ctx


async def close_context(ctx: AppContext) -> None:
    for hook in reversed(ctx._shutdown_hooks):
        try:
            result = hook()
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - shutdown must continue
            log.exception("shutdown hook failed")
    await ctx.runtime.shutdown()
    for provider in (ctx._places, ctx._weather, ctx._traffic):
        aclose = getattr(provider, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001
                log.exception("provider close failed")
    await ctx.llm.close()
    await ctx.bus.close()
    await ctx.engine.dispose()
