"""Agent run inspection and cancellation."""

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.core.deps import Ctx, CurrentUser, OwnedEvent, Session, load_owned_event
from app.models import AgentRun
from app.schemas.common import AgentRunOut

router = APIRouter(tags=["agent-runs"])

RunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
RUN_STATUSES: tuple[str, ...] = RunStatus.__args__
ACTIVE_RUN_STATUSES = ("queued", "running")
MAX_RUNS_PAGE = 200


def _parse_statuses(values: list[str] | None) -> list[str]:
    """`?status=a&status=b` and `?status=a,b` both work; unknown names are a 422."""
    statuses = [part.strip() for value in values or [] for part in value.split(",") if part.strip()]
    unknown = sorted(set(statuses) - set(RUN_STATUSES))
    if unknown:
        raise HTTPException(422, f"Unknown run status {', '.join(unknown)}; expected {', '.join(RUN_STATUSES)}")
    return statuses


@router.get("/events/{event_id}/agent-runs", response_model=list[AgentRunOut])
async def list_runs(
    event: OwnedEvent,
    session: Session,
    status: Annotated[
        list[str] | None,
        Query(description="Only runs in these states; repeat the parameter or comma-separate (queued, running, "
        "succeeded, failed, cancelled). Default: every state."),
    ] = None,
    parent_run_id: Annotated[str | None, Query(description="Only the children spawned by this run.")] = None,
    active: Annotated[bool, Query(description="Shorthand for status=queued,running.")] = False,
    limit: Annotated[int, Query(ge=1, description=f"At most this many runs (capped at {MAX_RUNS_PAGE}).")] = 50,
):
    """The event's agent runs (finished ones included unless filtered), newest first."""
    query = select(AgentRun).where(AgentRun.event_id == event.id)
    statuses = _parse_statuses(status)
    if active:
        statuses = [s for s in statuses if s in ACTIVE_RUN_STATUSES] if statuses else list(ACTIVE_RUN_STATUSES)
        if not statuses:
            return []
    if statuses:
        query = query.where(AgentRun.status.in_(statuses))
    if parent_run_id is not None:
        query = query.where(AgentRun.parent_run_id == parent_run_id)
    query = query.order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(min(limit, MAX_RUNS_PAGE))
    return list((await session.execute(query)).scalars())


async def _owned_run(ctx: Ctx, run_id: str, user: CurrentUser) -> AgentRun:
    run = await ctx.runtime.get(run_id)
    if run is None or await load_owned_event(ctx, run.event_id, user) is None:
        raise HTTPException(404, "Agent run not found")
    return run


@router.get("/agent-runs/{run_id}", response_model=AgentRunOut)
async def get_run(run_id: str, ctx: Ctx, user: CurrentUser):
    return await _owned_run(ctx, run_id, user)


@router.post("/agent-runs/{run_id}/cancel", response_model=AgentRunOut)
async def cancel_run(run_id: str, ctx: Ctx, user: CurrentUser):
    """Cancel a queued or running agent. Children it spawned keep running unless cancelled too."""
    run = await _owned_run(ctx, run_id, user)
    if await ctx.runtime.cancel(run_id):
        run = await ctx.runtime.wait(run_id, timeout=5)
    return run
