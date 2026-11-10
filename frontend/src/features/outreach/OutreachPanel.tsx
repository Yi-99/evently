import { CalendarPlus, ChevronRight, Mail, Plus, Send, X } from 'lucide-react'
import { useState } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../../api/client'
import { useEventStore } from '../../api/realtime'
import type { EmailMessageOut } from '../../api/types'
import EmailComposer, { type ComposerInitial } from './EmailComposer'
import LumaSection from './LumaSection'
import { btnGhost } from './Modal'
import StatusChip from './StatusChip'

type Composing = { emailId?: string } | null

/**
 * Floating "Outreach" button + drawer for one backend event: every email (with status) and the Luma event.
 * Renders nothing until the event has loaded. Mounted into document.body by ./mount.tsx.
 */
export default function OutreachPanel({ eventId }: { eventId: string }) {
  const view = useEventStore(eventId)
  const { store } = view
  const [open, setOpen] = useState(false)
  const [composing, setComposing] = useState<Composing>(null)

  if (!store || !view.loaded || view.error || view.deleted) return null

  const emails = Object.values(view.emails).sort((a, b) => b.created_at.localeCompare(a.created_at))
  const pending = emails.filter((e) => e.status === 'draft' || e.status === 'failed').length
  const blank: ComposerInitial = { to: '', subject: view.event ? `Re: ${view.event.name}` : '', body: '', kind: 'custom' }

  return createPortal(
    <>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="fixed right-24 bottom-6 z-30 inline-flex h-14 items-center gap-2 rounded-full bg-white px-5 text-sm font-semibold text-ink shadow-lg ring-1 ring-gray-900/10 transition hover:scale-105"
      >
        <Send size={16} className="text-accent" />
        Outreach
        {pending > 0 && <span className="grid min-w-5 place-items-center rounded-full bg-accent px-1.5 text-[11px] font-bold text-white">{pending}</span>}
      </button>

      {open && (
        <aside
          aria-label="Outreach"
          className="fixed top-4 right-4 bottom-24 z-40 flex w-[420px] max-w-[calc(100vw-2rem)] animate-rise flex-col overflow-hidden rounded-2xl bg-white text-sm text-ink shadow-2xl ring-1 ring-gray-900/10"
        >
          <header className="flex items-center gap-2 border-b border-line px-4 py-3">
            <h2 className="flex-1 text-base font-semibold">Outreach</h2>
            <button type="button" onClick={() => setOpen(false)} aria-label="Close outreach" className="rounded-full p-1 text-muted hover:bg-gray-100">
              <X size={16} />
            </button>
          </header>

          <div className="flex-1 space-y-6 overflow-y-auto p-4">
            <section>
              <div className="mb-2 flex items-center gap-2">
                <Mail size={15} className="text-accent" />
                <h3 className="flex-1 text-xs font-semibold tracking-wide text-muted uppercase">Emails</h3>
                <button type="button" className={btnGhost} onClick={() => setComposing({})}>
                  <Plus size={13} /> New
                </button>
              </div>
              {emails.length === 0 ? (
                <p className="rounded-xl bg-gray-50 px-3 py-4 text-center text-xs text-muted">
                  No emails yet. Use Email on an option, or start a new one.
                </p>
              ) : (
                <ul className="space-y-1.5">
                  {emails.map((e) => (
                    <EmailRow key={e.id} email={e} onOpen={() => setComposing({ emailId: e.id })} />
                  ))}
                </ul>
              )}
            </section>

            <section>
              <div className="mb-2 flex items-center gap-2">
                <CalendarPlus size={15} className="text-accent" />
                <h3 className="text-xs font-semibold tracking-wide text-muted uppercase">Luma event</h3>
              </div>
              <LumaSection eventId={eventId} store={store} api={api} state={view} />
            </section>
          </div>
        </aside>
      )}

      {composing && (
        <EmailComposer
          key={composing.emailId ?? 'new'}
          eventId={eventId}
          store={store}
          api={api}
          emailId={composing.emailId}
          initial={blank}
          onClose={() => setComposing(null)}
          onSaved={(saved) => !composing.emailId && setComposing({ emailId: saved.id })}
        />
      )}
    </>,
    document.body,
  )
}

function EmailRow({ email, onOpen }: { email: EmailMessageOut; onOpen: () => void }) {
  return (
    <li>
      <button
        type="button"
        onClick={onOpen}
        className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left ring-1 ring-gray-900/10 transition hover:bg-gray-50"
      >
        <span className="min-w-0 flex-1">
          <span className="block truncate font-medium">{email.subject}</span>
          <span className="block truncate text-xs text-muted">{email.to.join(', ')}</span>
        </span>
        <StatusChip status={email.status} />
        <ChevronRight size={14} className="text-muted" />
      </button>
    </li>
  )
}
