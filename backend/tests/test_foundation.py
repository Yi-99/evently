"""Foundation: bus, agent runtime, board service, scripted LLM, app boot."""

import asyncio

import pytest
from pydantic import BaseModel

from app.llm.base import Tool
from app.llm.scripted import ScriptedTurn
from app.realtime import events as ev
from app.services import board
from tests.helpers import published


async def test_bus_publish_subscribe_and_replay(ctx):
    first = await ctx.bus.publish("event:x", "a", {"n": 1})
    async with await ctx.bus.subscribe("event:x", since_seq=0) as sub:
        await ctx.bus.publish("event:x", "b", {"n": 2})
        got = [await sub.get(timeout=1), await sub.get(timeout=1)]
    assert [e.type for e in got] == ["a", "b"]
    assert first.seq == 1 and got[1].seq == 2


async def test_runtime_runs_agent_and_publishes_lifecycle(ctx, event):
    async def agent(run):
        await run.step("working", progress=0.5)
        return {"answer": 42}

    row = await ctx.runtime.spawn(event_id=event.id, kind="venue_research", title="t", fn=agent)
    done = await ctx.runtime.wait(row.id, timeout=5)
    assert done.status == "succeeded" and done.output == {"answer": 42} and done.progress == 1.0
    types = [e.type for e in published(ctx, event.id, "agent.run")]
    assert types[0] == ev.AGENT_RUN_CREATED and types.count(ev.AGENT_RUN_UPDATED) >= 3


async def test_runtime_failure_marks_card(ctx, event):
    card = await board.create_card(ctx, event_id=event.id, kind="venue", title="V", status="running")

    async def agent(run):
        raise RuntimeError("boom")

    row = await ctx.runtime.spawn(event_id=event.id, kind="venue_research", title="t", fn=agent, card_id=card.id)
    done = await ctx.runtime.wait(row.id, timeout=5)
    assert done.status == "failed" and "boom" in done.error
    assert (await board.get_card(ctx, card.id)).status == "error"


async def test_runtime_cancel(ctx, event):
    started = asyncio.Event()

    async def agent(run):
        started.set()
        await asyncio.sleep(30)

    row = await ctx.runtime.spawn(event_id=event.id, kind="venue_research", title="t", fn=agent)
    await asyncio.wait_for(started.wait(), 5)
    assert await ctx.runtime.cancel(row.id)
    assert (await ctx.runtime.wait(row.id, timeout=5)).status == "cancelled"


async def test_board_locks_user_edits_against_agents(ctx, event):
    card = await board.create_card(ctx, event_id=event.id, kind="venue", title="V", data={"summary": "a"})
    second = await board.create_card(ctx, event_id=event.id, kind="venue", title="W")
    assert (card.x, card.y) != (second.x, second.y)

    await board.patch_card(ctx, card.id, by="user", data_patch={"summary": "mine"})
    after = await board.patch_card(ctx, card.id, by="agent", data_patch={"summary": "agent", "pros": ["x"]})
    assert after.data == {"summary": "mine", "pros": ["x"]} and after.locked_fields == ["summary"]

    with pytest.raises(board.VersionConflict):
        await board.patch_card(ctx, card.id, title="new", expected_version=1)


async def test_scripted_llm_executes_real_tools(llm):
    class AddIn(BaseModel):
        a: int
        b: int

    async def add(args: AddIn) -> dict:
        return {"sum": args.a + args.b}

    llm.on_run("math", [ScriptedTurn(tool_calls=[("add", {"a": 2, "b": 3})]), ScriptedTurn(text="5")])
    result = await llm.run(
        purpose="math",
        system="s",
        messages=[{"role": "user", "content": "2+3"}],
        tools=[Tool("add", "add numbers", AddIn, add)],
    )
    assert result.text == "5" and result.called("add")[0].result == '{"sum": 5}'
    assert [m["role"] for m in result.new_messages] == ["assistant", "user", "assistant"]


def test_app_boots_and_reports_health(client, client_event):
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok" and body["llm_configured"] is True
    runs = client.get(f"/api/v1/events/{client_event.id}/agent-runs")
    assert runs.status_code == 200 and runs.json() == []
