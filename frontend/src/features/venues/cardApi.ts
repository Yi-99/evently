// Card writes from the venue panel. Each response goes straight into the store so the UI updates before the
// matching realtime envelope arrives (the store keeps whichever card version is newer).
import { isApiError } from '../../api/client'
import type { CardOut, CardPatch, CardPrompt, RunAccepted } from '../../api/types'
import type { RunnerContext } from '../../lib/runners'

const putCard = (ctx: RunnerContext, card: CardOut) =>
  ctx.store.patch((s) => (s.cards[card.id] && s.cards[card.id].version > card.version ? s : { ...s, cards: { ...s.cards, [card.id]: card } }))

export const errorMessage = (e: unknown, fallback: string) => (isApiError(e) || e instanceof Error ? e.message : fallback)

/** PATCH /cards/{id}. Patched `data` keys become locked against agent overwrites. */
export async function patchCard(ctx: RunnerContext, cardId: string, patch: CardPatch): Promise<CardOut> {
  const card = await ctx.api.patch<CardOut>(`/cards/${encodeURIComponent(cardId)}`, patch)
  putCard(ctx, card)
  return card
}

/** POST /cards/{id}/prompt: re-run research on one venue card. 409 while an agent is already on it, 503 without Claude. */
export async function promptCard(ctx: RunnerContext, cardId: string, instruction: string) {
  const body: CardPrompt = { instruction }
  const { run } = await ctx.api.post<RunAccepted>(`/cards/${encodeURIComponent(cardId)}/prompt`, body)
  ctx.store.patch((s) => ({
    ...s,
    runs: { ...s.runs, [run.id]: run },
    // The backend marks the card running and points it at the new run; mirror that until the envelope lands.
    cards: s.cards[cardId] ? { ...s.cards, [cardId]: { ...s.cards[cardId], status: 'running', agent_run_id: run.id } } : s.cards,
  }))
  return run
}
