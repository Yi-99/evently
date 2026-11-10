"""Completion follow-ups: a chat note when a launched venue search or deck build finishes.

The orchestrator's tools return before the work is done, so the planner would
otherwise learn about the outcome only from the board. A watcher task waits
for the run and posts an assistant message, which is stored like any other
turn so later turns see it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from app.agents.orchestrator.store import create_message, text_message
from app.agents.runtime import TERMINAL
from app.models import AgentRun

if TYPE_CHECKING:
    from app.core.context import AppContext

log = logging.getLogger(__name__)

# kind -> (what to call it, what to say when it succeeds without a summary of its own)
OUTCOMES = {
    "venue_search": (
        "venue search",
        "The venue search has finished; the venues and a ranked summary are on the board.",
    ),
    "presentation_build": ("presentation build", "Your presentation is ready."),
}


def outcome_text(run: AgentRun) -> str:
    """What to tell the planner about a finished run."""
    label, default = OUTCOMES.get(run.kind, (run.title or "background task", "The work has finished."))
    if run.status == "succeeded":
        summary = (run.output or {}).get("summary")
        return summary.strip() if isinstance(summary, str) and summary.strip() else default
    if run.status == "cancelled":
        return f"The {label} was cancelled before it finished."
    return f"The {label} failed: {run.error or 'unknown error'}. Ask me to try again when you're ready."


def _watchers(ctx: AppContext) -> set[asyncio.Task[None]]:
    def create() -> set[asyncio.Task[None]]:
        tasks: set[asyncio.Task[None]] = set()

        async def cancel_all() -> None:
            pending = [t for t in tasks if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        ctx.on_shutdown(cancel_all)
        return tasks

    return ctx.service("orchestrator_watchers", create)


def watch(ctx: AppContext, run_id: str) -> asyncio.Task[None]:
    """Post a chat note on the run's event when it reaches a terminal status."""
    tasks = _watchers(ctx)
    task = asyncio.create_task(_follow_up(ctx, run_id), name=f"chat-followup:{run_id}")
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


async def _follow_up(ctx: AppContext, run_id: str) -> None:
    try:
        run = await ctx.runtime.wait(run_id)
        if run.status not in TERMINAL:
            return  # not tracked by this runtime (another process?); nothing reliable to report
        await create_message(
            ctx,
            event_id=run.event_id,
            role="assistant",
            content=(text := outcome_text(run)),
            llm_messages=[text_message("assistant", text)],
            meta={"kind": "run_completed", "agent_run_id": run.id, "run_kind": run.kind, "run_status": run.status},
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a lost follow-up must not take anything else down
        log.exception("chat follow-up for run %s failed", run_id)
