"""The volatile `<workspace>` block attached to the current user turn.

Everything that changes between requests (date, requirements, board, decks,
running work, selection) lives here, never in the system prompt, so the system
prompt stays a cacheable prefix.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.core.ids import utcnow
from app.models import AgentRun, Card, EmailMessage, Event, LumaEvent, Presentation, Slide
from app.services import board

if TYPE_CHECKING:
    from app.core.context import AppContext

# Runs the planner would think of as "work in progress" (venue research children show up via their cards).
BACKGROUND_KINDS = ("venue_search", "card_refine", "presentation_build", "presentation_edit")


@dataclass
class Workspace:
    event: Event
    cards: list[Card]
    decks: list[tuple[Presentation, list[Slide]]]
    active_runs: list[AgentRun]
    emails: list[EmailMessage]
    luma: LumaEvent | None
    selected_card_ids: list[str]

    @property
    def titles(self) -> dict[str, str]:
        """id -> title for every card, deck, slide and email; used for human-readable tool summaries."""
        titles = {card.id: card.title for card in self.cards}
        for deck, slides in self.decks:
            titles[deck.id] = deck.title
            titles.update((slide.id, slide.title) for slide in slides)
        titles.update((email.id, email.subject) for email in self.emails)
        return titles

    def render(self) -> str:
        today = utcnow()
        requirements = {k: v for k, v in (self.event.requirements or {}).items() if v not in (None, "", [])}
        lines = [
            "<workspace>",
            f"Today: {today:%A}, {today:%Y-%m-%d} (UTC)",
            f"Event: {_quote(self.event.name)}" + (f" — {self.event.description}" if self.event.description else ""),
            "Requirements: " + (json.dumps(requirements, ensure_ascii=False) if requirements else "none saved yet"),
            "",
            f"Board cards ({len(self.cards)}):" if self.cards else "Board cards: none yet",
            *(_card_line(card) for card in self.cards),
            "Selected by the planner: " + (", ".join(self.selected_card_ids) or "none"),
        ]
        if self.decks:
            lines += ["", "Presentations:"]
            for deck, slides in self.decks:
                lines.append(f"- {deck.id} | {_quote(deck.title)} | {deck.status} | {len(slides)} slides")
                lines += [f"    {slide.position + 1}. {slide.id} {_quote(slide.title)}" for slide in slides]
        if self.emails:
            lines += ["", "Emails (drafts are sent only after the planner confirms):"]
            lines += [
                f"- {e.id} | {e.kind} | to {', '.join(e.to or [])} | {_quote(e.subject)} | {e.status}"
                + (f" | error: {e.error}" if e.error else "")
                for e in self.emails
            ]
        if self.luma is not None:
            detail = f"{self.luma.status} | {_quote((self.luma.draft or {}).get('name', ''))}"
            if self.luma.url:
                detail += f" | {self.luma.url} | {self.luma.guests_invited} guests invited"
            if self.luma.error:
                detail += f" | error: {self.luma.error}"
            lines += ["", f"Luma event page: {detail}"]
        if self.active_runs:
            lines += ["", "Work in progress:", *(_run_line(run) for run in self.active_runs)]
        lines.append("</workspace>")
        return "\n".join(lines)


def render_memory(hits: list[dict[str, Any]]) -> str:
    """The `<memory>` block: venues from the planner's earlier events, one compact line each."""
    lines = ["<memory>", "Venues researched for this planner's earlier events (prior research, may be stale):"]
    for hit in hits:
        evaluated = hit.get("evaluated_for") or {}
        parts = [_quote(hit.get("name") or "")]
        if hit.get("address"):
            parts.append(str(hit["address"]))
        if hit.get("score_overall") is not None:
            parts.append(f"score {hit['score_overall']}")
        if evaluated.get("headcount"):
            parts.append(f"evaluated for {evaluated['headcount']} guests")
        summary = re.sub(r"\s+", " ", hit.get("summary") or "").strip()
        if summary:
            parts.append(summary if len(summary) <= 160 else summary[:157].rstrip() + "...")
        lines.append("- " + " | ".join(parts))
    lines.append("</memory>")
    return "\n".join(lines)


def _quote(text: str) -> str:
    return json.dumps(text or "", ensure_ascii=False)


def _card_line(card: Card) -> str:
    data: dict[str, Any] = card.data or {}
    parts = [card.id, card.kind, _quote(card.title), card.status]
    score = data.get("score")
    if isinstance(score, dict) and score.get("overall") is not None:
        parts.append(f"score {score['overall']}")
    if data.get("rank") is not None:
        parts.append(f"rank {data['rank']}")
    return "- " + " | ".join(parts)


def _run_line(run: AgentRun) -> str:
    detail = f"{run.status}, {run.progress:.0%}"
    if run.current_step:
        detail += f", {run.current_step}"
    return f"- {run.kind}: {_quote(run.title)} ({detail})"


async def load_workspace(ctx: AppContext, event_id: str, selected_card_ids: list[str] | None = None) -> Workspace:
    """Snapshot the event's workspace. Unknown selected ids are dropped."""
    cards = await board.list_cards(ctx, event_id)
    async with ctx.db() as session:
        event = await session.get(Event, event_id)
        if event is None:
            raise LookupError(f"event {event_id} not found")
        decks = list(
            (
                await session.execute(
                    select(Presentation).where(Presentation.event_id == event_id).order_by(Presentation.created_at)
                )
            ).scalars()
        )
        slides_by_deck: dict[str, list[Slide]] = {deck.id: [] for deck in decks}
        if decks:
            slides = await session.execute(
                select(Slide).where(Slide.presentation_id.in_(list(slides_by_deck))).order_by(Slide.position)
            )
            for slide in slides.scalars():
                slides_by_deck[slide.presentation_id].append(slide)
        active_runs = list(
            (
                await session.execute(
                    select(AgentRun)
                    .where(
                        AgentRun.event_id == event_id,
                        AgentRun.status.in_(["queued", "running"]),
                        AgentRun.kind.in_(BACKGROUND_KINDS),
                    )
                    .order_by(AgentRun.created_at)
                )
            ).scalars()
        )
        emails = list(
            (
                await session.execute(
                    select(EmailMessage).where(EmailMessage.event_id == event_id).order_by(EmailMessage.created_at)
                )
            ).scalars()
        )
        luma = (await session.execute(select(LumaEvent).where(LumaEvent.event_id == event_id))).scalar_one_or_none()
    known = {card.id for card in cards}
    return Workspace(
        event=event,
        cards=cards,
        decks=[(deck, slides_by_deck[deck.id]) for deck in decks],
        active_runs=active_runs,
        emails=emails,
        luma=luma,
        selected_card_ids=[card_id for card_id in selected_card_ids or [] if card_id in known],
    )
