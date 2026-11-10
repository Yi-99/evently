import { AlertTriangle, ArrowUp, Check, LoaderCircle, Sparkles, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { EVENT_TYPES } from '../data/workflows'
import { toolLabel, type ChatItem, type ToolChip } from '../features/chat/messages'
import type { EventDetails } from '../types'

interface Props {
  event: EventDetails
  messages: ChatItem[]
  /** True while the assistant is replying: Send is disabled and a typing indicator shows. */
  busy?: boolean
  /** Shown above the composer, e.g. when the AI isn't configured and only plan edits work. */
  notice?: string | null
  onSend: (text: string) => void
  onClose: () => void
}

// Things the orchestrator can do; plan edits ("add photo booth", "approve plan") still work when typed.
const SUGGESTIONS = ['Find venues for this event', 'Compare the top 3 venues', 'Build a 6-slide deck', 'Draft an email to the venue']
// Without the AI only the rule-based plan editor answers, so suggest what it understands.
const LOCAL_SUGGESTIONS = ['300 people in Austin', 'add photo booth after location', 'remove judges', 'approve plan']

function Typing() {
  return (
    <span className="inline-flex items-center gap-1 py-1" aria-label="Evently is typing">
      {[0, 150, 300].map((d) => (
        <span key={d} className="size-1.5 animate-bounce rounded-full bg-muted/70" style={{ animationDelay: `${d}ms` }} />
      ))}
    </span>
  )
}

function Chip({ chip }: { chip: ToolChip }) {
  const tone = chip.isError
    ? 'bg-warn-soft text-warn ring-warn/20'
    : chip.status === 'started'
      ? 'bg-accent-soft text-accent ring-accent/20'
      : 'bg-gray-50 text-gray-600 ring-gray-900/10'
  return (
    <span title={chip.summary || undefined} className={`inline-flex max-w-full items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold ring-1 ${tone}`}>
      {chip.isError ? (
        <AlertTriangle size={11} />
      ) : chip.status === 'started' ? (
        <LoaderCircle size={11} className="animate-spin" />
      ) : (
        <Check size={11} />
      )}
      <span className="truncate">{toolLabel(chip.name)}</span>
    </span>
  )
}

export default function ChatPanel({ event, messages, busy = false, notice, onSend, onClose }: Props) {
  const [draft, setDraft] = useState('')
  const listRef = useRef<HTMLDivElement>(null)
  const meta = EVENT_TYPES.find((e) => e.id === event.type)!
  const last = messages.at(-1)
  // The reply bubble shows its own dots while it streams; otherwise show a standalone indicator.
  const showTyping = busy && !(last?.role === 'assistant' && last.status === 'streaming')

  // Scroll only the message list; scrollIntoView would also scroll the clipped panel itself.
  useEffect(() => {
    const el = listRef.current
    el?.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  }, [messages, showTyping])

  const send = (text = draft) => {
    if (!text.trim() || busy) return
    onSend(text.trim())
    setDraft('')
  }

  return (
    <section className="glow flex h-full min-h-0 flex-col overflow-hidden rounded-3xl bg-white shadow-2xl ring-1 shadow-indigo-500/10 ring-gray-900/10">
      <header className="flex items-center gap-3 border-b border-line bg-white/70 px-5 py-3.5 backdrop-blur">
        <span className="grid size-8 place-items-center rounded-full bg-accent text-white">
          <Sparkles size={14} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="text-sm font-bold tracking-tight">Evently assistant</div>
          <div className="truncate text-xs text-muted">
            {meta.label} · {event.headcount} people · {event.city}
          </div>
        </div>
        <button onClick={onClose} aria-label="Close chat" className="rounded-full p-2 text-muted hover:bg-line/60 hover:text-ink">
          <X size={18} />
        </button>
      </header>

      <div ref={listRef} className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-6" aria-live="polite">
        {messages.map((m) => (
          <div key={m.id} className={`flex animate-rise ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            {m.role === 'assistant' ? (
              <div className="max-w-[85%] rounded-3xl rounded-bl-lg bg-white px-4 py-2.5 text-[15px] leading-relaxed shadow-sm ring-1 ring-gray-900/5">
                <div className="mb-1 text-xs font-semibold text-accent">Evently</div>
                {m.tools.length > 0 && (
                  <div className="mb-1.5 flex flex-wrap gap-1">
                    {m.tools.map((c, i) => (
                      <Chip key={i} chip={c} />
                    ))}
                  </div>
                )}
                {m.text ? (
                  <div className="whitespace-pre-line">
                    {m.text}
                    {m.status === 'streaming' && <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse rounded-sm bg-accent/60 align-middle" />}
                  </div>
                ) : (
                  m.status === 'streaming' && <Typing />
                )}
                {m.status === 'error' && (
                  <div className="mt-1.5 flex items-start gap-1.5 rounded-xl bg-warn-soft px-2.5 py-1.5 text-xs text-warn">
                    <AlertTriangle size={13} className="mt-0.5 shrink-0" />
                    {m.error}
                  </div>
                )}
              </div>
            ) : (
              <div className="max-w-[80%] rounded-3xl rounded-br-lg bg-accent px-4 py-2.5 text-[15px] text-white">{m.text}</div>
            )}
          </div>
        ))}
        {showTyping && (
          <div className="flex justify-start">
            <div className="rounded-3xl rounded-bl-lg bg-white px-4 py-2.5 shadow-sm ring-1 ring-gray-900/5">
              <Typing />
            </div>
          </div>
        )}
      </div>

      <div className="px-5 pb-5">
        {notice && <p className="mb-2 rounded-xl bg-gray-50 px-3 py-1.5 text-xs text-muted ring-1 ring-gray-900/5">{notice}</p>}
        <div className="mb-2 flex flex-wrap gap-1.5">
          {(notice ? LOCAL_SUGGESTIONS : SUGGESTIONS).map((s) => (
            <button
              key={s}
              onClick={() => send(s)}
              disabled={busy}
              className="rounded-full bg-white px-2.5 py-1 text-xs font-medium text-gray-700 shadow-sm ring-1 ring-gray-900/10 transition hover:text-accent hover:ring-accent disabled:opacity-40"
            >
              {s}
            </button>
          ))}
        </div>
        <div className="flex items-end gap-2 rounded-[28px] bg-white p-2 shadow-sm ring-1 ring-gray-900/10 focus-within:ring-2 focus-within:ring-accent">
          <textarea
            rows={1}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                send()
              }
            }}
            placeholder={busy ? 'Evently is replying…' : 'Ask Evently or edit the plan…'}
            className="max-h-32 min-h-10 flex-1 resize-none bg-transparent px-3 py-2 outline-none placeholder:text-muted/70"
          />
          <button
            onClick={() => send()}
            disabled={!draft.trim() || busy}
            aria-label="Send"
            className="grid size-9 place-items-center rounded-full bg-accent text-white disabled:opacity-25"
          >
            {busy ? <LoaderCircle size={16} className="animate-spin" /> : <ArrowUp size={16} />}
          </button>
        </div>
      </div>
    </section>
  )
}
