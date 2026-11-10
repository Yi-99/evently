// Chat panel items: backend messages (from the event store, streamed live) merged with local-only notes
// (intake recap, plan approved, rule-based plan edits), ordered by time.
import type { ChatMessageOut, ChatToolData } from '../../api/types'
import type { ChatMessage } from '../../types'

export interface ToolChip {
  name: string
  summary: string
  status: 'started' | 'finished'
  isError: boolean
}

export interface ChatItem extends ChatMessage {
  /** Epoch ms used for ordering. */
  at: number
  /** 'streaming' while the reply is being written; 'error' when the turn failed. */
  status: 'complete' | 'streaming' | 'error'
  error?: string
  tools: ToolChip[]
  /** True for notes that only exist in this browser (not saved in the chat history). */
  local?: boolean
  /** The orchestrator run that writes this assistant message. */
  runId?: string
}

let seq = 0
export const localItem = (role: ChatMessage['role'], text: string, at = Date.now()): ChatItem => ({
  id: `local-${++seq}`,
  role,
  text,
  at,
  status: 'complete',
  tools: [],
  local: true,
})

/** Backend timestamps may come without a zone; they are UTC. */
export const serverTime = (iso: string) => Date.parse(/[zZ]|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : `${iso}Z`)

/** Collapse started/finished pairs into one chip per tool call. */
export function toolChips(live: ChatToolData[] | undefined, saved: unknown): ToolChip[] {
  if (live?.length) {
    const chips: ToolChip[] = []
    for (const t of live) {
      const open = t.status === 'finished' ? chips.findLast((c) => c.name === t.name && c.status === 'started') : undefined
      if (open) Object.assign(open, { status: 'finished', summary: t.summary || open.summary, isError: t.is_error })
      else chips.push({ name: t.name, summary: t.summary, status: t.status, isError: t.is_error })
    }
    return chips
  }
  // History: completed messages keep their finished tool calls in meta.tools.
  if (!Array.isArray(saved)) return []
  return saved.flatMap((t: unknown) => {
    if (!t || typeof t !== 'object') return []
    const r = t as Record<string, unknown>
    return typeof r.name === 'string'
      ? [{ name: r.name, summary: typeof r.summary === 'string' ? r.summary : '', status: 'finished' as const, isError: r.is_error === true }]
      : []
  })
}

export function fromServer(m: ChatMessageOut, tools: Record<string, ChatToolData[]>): ChatItem {
  const meta = (m.meta ?? {}) as Record<string, unknown>
  const status = m.status === 'streaming' || m.status === 'error' ? m.status : 'complete'
  return {
    id: m.id,
    role: m.role === 'user' ? 'user' : 'assistant',
    text: m.content,
    at: serverTime(m.created_at),
    status,
    error: status === 'error' ? (typeof meta.error === 'string' ? meta.error : 'The reply failed.') : undefined,
    tools: toolChips(tools[m.id], meta.tools),
    runId: typeof meta.agent_run_id === 'string' ? meta.agent_run_id : undefined,
  }
}

/** Server messages (system/tool roles dropped) plus local notes, oldest first. */
export function mergeChat(server: ChatMessageOut[], tools: Record<string, ChatToolData[]>, local: ChatItem[]): ChatItem[] {
  const items = server.filter((m) => m.role === 'user' || m.role === 'assistant').map((m) => fromServer(m, tools))
  return [...items, ...local].sort((a, b) => a.at - b.at)
}

/** Friendly tool names for chips. */
const TOOL_LABEL: Record<string, string> = {
  search_venues: 'Venue search',
  refine_venue_card: 'Refine venue',
  update_requirements: 'Requirements',
  get_card: 'Read card',
  add_note: 'Note',
  remove_card: 'Remove card',
  create_comparison: 'Comparison',
  create_presentation: 'Deck',
  edit_presentation: 'Edit deck',
  draft_email: 'Email draft',
  send_email: 'Send email',
  draft_luma_event: 'Luma draft',
  publish_luma_event: 'Publish on Luma',
  invite_luma_guests: 'Luma invites',
  search_venue_memory: 'Venue memory',
  search_event_history: 'Event history',
}
export const toolLabel = (name: string) =>
  TOOL_LABEL[name] ?? name.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase())
