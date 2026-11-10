"""AgentRuntime: runs agents as tracked asyncio tasks.

Every agent is an `AgentRun` row plus a task. The runtime persists lifecycle
changes and publishes `agent.run.created` / `agent.run.updated` on the event
topic, which is how the whiteboard shows real agents working in real time.

    async def research(run: RunContext) -> dict:
        await run.step("Checking parking", progress=0.4)
        ...
        return {"card_id": card_id}          # stored as AgentRun.output

    run_row = await ctx.runtime.spawn(
        event_id=event_id, kind="venue_research", title="Research: The Foundry",
        fn=research, card_id=card_id, parent_run_id=parent.run_id,
    )

Coordinators that wait for child runs MUST pass `limited=False`, otherwise
parents and children compete for the same concurrency slots and can deadlock.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from app.core.ids import utcnow
from app.models import AgentRun
from app.observability import metrics, tracing
from app.realtime import events as ev
from app.schemas.common import AgentRunOut, dump

if TYPE_CHECKING:
    from app.core.context import AppContext

log = logging.getLogger(__name__)

TERMINAL = {"succeeded", "failed", "cancelled"}
MAX_STEPS_KEPT = 60

AgentFn = Callable[["RunContext"], Awaitable[dict[str, Any] | None]]


class RunContext:
    """Handed to the agent function; its handle on its own run."""

    def __init__(self, ctx: AppContext, run: AgentRun) -> None:
        self.ctx = ctx
        self.run_id = run.id
        self.event_id = run.event_id
        self.kind = run.kind
        self.parent_run_id = run.parent_run_id
        self.card_id = run.card_id
        self.input: dict[str, Any] = dict(run.input or {})
        self._usage: dict[str, int] = {}

    async def step(self, message: str, *, progress: float | None = None, data: dict[str, Any] | None = None) -> None:
        """Record what the agent is doing now and broadcast it."""

        def apply(run: AgentRun) -> None:
            run.current_step = message[:500]
            if progress is not None:
                run.progress = max(0.0, min(1.0, progress))
            entry: dict[str, Any] = {"ts": utcnow().isoformat(), "message": message}
            if data:
                entry["data"] = data
            run.steps = [*(run.steps or []), entry][-MAX_STEPS_KEPT:]

        await self.ctx.runtime._update(self.run_id, apply)

    async def attach_card(self, card_id: str) -> None:
        """Link this run to the card it is filling in."""
        self.card_id = card_id

        def apply(run: AgentRun) -> None:
            run.card_id = card_id

        await self.ctx.runtime._update(self.run_id, apply)

    def add_usage(self, usage: dict[str, int] | None) -> None:
        """Accumulate token usage (pass `AgentResult.usage`); saved when the run ends."""
        for key, value in (usage or {}).items():
            self._usage[key] = self._usage.get(key, 0) + int(value or 0)

    async def spawn_child(
        self,
        *,
        kind: str,
        title: str,
        fn: AgentFn,
        input: dict[str, Any] | None = None,
        card_id: str | None = None,
        limited: bool = True,
    ) -> AgentRun:
        return await self.ctx.runtime.spawn(
            event_id=self.event_id,
            kind=kind,
            title=title,
            fn=fn,
            input=input,
            card_id=card_id,
            parent_run_id=self.run_id,
            limited=limited,
        )


class AgentRuntime:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._semaphore = asyncio.Semaphore(ctx.settings.max_parallel_agents)

    # -- public API ---------------------------------------------------------

    async def spawn(
        self,
        *,
        event_id: str,
        kind: str,
        title: str,
        fn: AgentFn,
        input: dict[str, Any] | None = None,
        parent_run_id: str | None = None,
        card_id: str | None = None,
        limited: bool = True,
    ) -> AgentRun:
        """Create an `AgentRun`, start it in the background and return the row immediately.

        limited: take one of `settings.max_parallel_agents` slots while running.
            Pass False for coordinators that mostly wait on child runs.
        """
        run = AgentRun(
            event_id=event_id,
            kind=kind,
            title=title,
            input=input or {},
            parent_run_id=parent_run_id,
            card_id=card_id,
            status="queued",
        )
        async with self._ctx.db() as session:
            session.add(run)
            await session.commit()
        await self._publish(ev.AGENT_RUN_CREATED, run)

        task = asyncio.create_task(self._execute(run, fn, limited), name=f"agent:{kind}:{run.id}")
        self._tasks[run.id] = task
        task.add_done_callback(lambda _t, run_id=run.id: self._tasks.pop(run_id, None))
        return run

    async def cancel(self, run_id: str) -> bool:
        """Cancel a queued or running agent. Returns False if it is not active."""
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def is_active(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    def active_run_ids(self) -> list[str]:
        return [run_id for run_id, task in self._tasks.items() if not task.done()]

    async def wait(self, run_id: str, timeout: float | None = None) -> AgentRun:
        """Wait until the run finishes (any terminal status) and return the fresh row."""
        task = self._tasks.get(run_id)
        if task is not None:
            await asyncio.wait({task}, timeout=timeout)
        run = await self.get(run_id)
        if run is None:
            raise LookupError(f"agent run {run_id} not found")
        return run

    async def wait_all(self, run_ids: list[str], timeout: float | None = None) -> list[AgentRun]:
        tasks = {t for run_id in run_ids if (t := self._tasks.get(run_id)) is not None}
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        return [run for run_id in run_ids if (run := await self.get(run_id)) is not None]

    async def get(self, run_id: str) -> AgentRun | None:
        async with self._ctx.db() as session:
            return await session.get(AgentRun, run_id)

    async def shutdown(self) -> None:
        tasks = [t for t in self._tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- internals ----------------------------------------------------------

    async def _publish(self, type: str, run: AgentRun) -> None:
        await self._ctx.bus.publish(ev.event_topic(run.event_id), type, {"run": dump(AgentRunOut, run)})

    async def _update(self, run_id: str, apply: Callable[[AgentRun], None]) -> AgentRun | None:
        async with self._ctx.db() as session:
            run = await session.get(AgentRun, run_id)
            if run is None:
                return None
            apply(run)
            await session.commit()
        await self._publish(ev.AGENT_RUN_UPDATED, run)
        return run

    async def _finish(self, run_ctx: RunContext, status: str, *, output: dict | None = None, error: str | None = None):
        def apply(run: AgentRun) -> None:
            run.status = status
            run.finished_at = utcnow()
            run.usage = dict(run_ctx._usage)
            if output is not None:
                run.output = output
            if status == "succeeded":
                run.progress = 1.0
            if error is not None:
                run.error = error

        await self._update(run_ctx.run_id, apply)
        if status != "succeeded" and run_ctx.card_id:
            await self._mark_card_failed(run_ctx.card_id, error or status)

    async def _mark_card_failed(self, card_id: str, message: str) -> None:
        """A card left pending/running by a dead agent would spin forever; flag it."""
        from app.services import board

        try:
            card = await board.get_card(self._ctx, card_id)
            if card is not None and card.status in {"pending", "running"}:
                await board.patch_card(
                    self._ctx, card_id, by="system", status="error", data_patch={"error": {"message": message}}
                )
        except Exception:  # noqa: BLE001 - never mask the original failure
            log.exception("could not mark card %s as failed", card_id)

    async def _execute(self, run: AgentRun, fn: AgentFn, limited: bool) -> None:
        run_ctx = RunContext(self._ctx, run)
        # Observability only: `tracing`/`metrics` are no-ops unless OTel is enabled.
        span_attributes = {
            "agent.kind": run.kind,
            "agent.run_id": run.id,
            "agent.title": run.title,
            "agent.limited": limited,
            "event_id": run.event_id,
            "card_id": run.card_id,
            "parent_run_id": run.parent_run_id,
        }
        status = "succeeded"
        with tracing.start_span("agent.run", span_attributes) as span:
            metrics.agent_run_started(run.kind)
            try:
                if limited:
                    async with self._semaphore:
                        await self._run_body(run_ctx, fn)
                else:
                    await self._run_body(run_ctx, fn)
            except asyncio.CancelledError:
                status = "cancelled"
                # Shield the bookkeeping so a cancelled run is still recorded as such.
                await asyncio.shield(self._finish(run_ctx, "cancelled", error="Cancelled"))
            except Exception as exc:  # noqa: BLE001 - agent failures are data, not crashes
                status = "failed"
                log.exception("agent run %s (%s) failed", run.id, run.kind)
                span.record_exception(exc)
                span.set_error(f"{type(exc).__name__}: {exc}")
                await self._finish(run_ctx, "failed", error=f"{type(exc).__name__}: {exc}")
            finally:
                span.set_attribute("agent.status", status)
                span.set_attribute("card_id", run_ctx.card_id)  # may have been attached during the run
                metrics.agent_run_finished(run.kind, status)

    async def _run_body(self, run_ctx: RunContext, fn: AgentFn) -> None:
        def start(run: AgentRun) -> None:
            run.status = "running"
            run.started_at = utcnow()

        await self._update(run_ctx.run_id, start)
        output = await fn(run_ctx)
        await self._finish(run_ctx, "succeeded", output=output or {})
