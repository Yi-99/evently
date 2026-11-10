"""Chat orchestrator — public entrypoint.

The orchestrator is the agent the planner talks to. It decides when to launch
venue searches, refine cards or build decks by calling the entrypoints of
`app.agents.venues` and `app.agents.presentations`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.agents.orchestrator import store
from app.agents.orchestrator.turn import run_turn
from app.models import AgentRun, ChatMessage

if TYPE_CHECKING:
    from app.core.context import AppContext

RUN_TITLE_CHARS = 60


async def handle_user_message(
    ctx: AppContext,
    *,
    event_id: str,
    user_id: str,
    text: str,
    context: dict[str, Any] | None = None,
) -> tuple[ChatMessage, AgentRun]:
    """Persist the user's message and start the assistant's turn in the background.

    Publishes `chat.message.created` for the user message, then spawns an
    `orchestrator` run (`limited=False`) that streams the reply as `chat.delta`
    envelopes and finishes with `chat.message.completed` (or `chat.error`).

    context: optional UI context, e.g. `{"selected_card_ids": [...]}`.
    Returns the stored user message and the orchestrator run.
    """
    text = text.strip()
    if not text:
        raise ValueError("message text is empty")
    context = context or {}
    selected = [str(card_id) for card_id in context.get("selected_card_ids") or [] if card_id]

    message = await store.create_message(
        ctx, event_id=event_id, role="user", content=text, meta={"user_id": user_id, "context": context}
    )

    async def turn(run):
        return await run_turn(run, user_message=message, selected_card_ids=selected)

    title = "Chat: " + (text if len(text) <= RUN_TITLE_CHARS else text[: RUN_TITLE_CHARS - 1] + "…")
    run = await ctx.runtime.spawn(
        event_id=event_id,
        kind="orchestrator",
        title=title,
        fn=turn,
        input={"message_id": message.id, "selected_card_ids": selected},
        limited=False,
    )
    return message, run
