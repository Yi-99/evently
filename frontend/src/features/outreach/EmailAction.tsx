import { Mail } from 'lucide-react'
import { useState } from 'react'
import type { ResultActionProps } from '../../lib/resultActions'
import EmailComposer, { type ComposerInitial } from './EmailComposer'
import StatusChip from './StatusChip'
import { defaultBody, defaultSubject, emailKindFor, eventNameOf, useStoreState } from './lib'

/** "Email" button on an option's card: opens the composer, keeps the backend draft linked via `emailId`. */
export default function EmailAction({ task, result, event, ctx, onUpdate }: ResultActionProps) {
  const [open, setOpen] = useState(false)
  if (!ctx) {
    return (
      <button
        type="button"
        disabled
        title="Emails need a saved event. This plan only has sample data."
        className="inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium text-muted ring-1 ring-gray-900/10 disabled:cursor-not-allowed disabled:opacity-60"
      >
        <Mail size={13} /> Email
      </button>
    )
  }
  return <Connected ctx={ctx} task={task} result={result} event={event} onUpdate={onUpdate} open={open} setOpen={setOpen} />
}

function Connected({
  ctx,
  task,
  result,
  event,
  onUpdate,
  open,
  setOpen,
}: ResultActionProps & { ctx: NonNullable<ResultActionProps['ctx']>; open: boolean; setOpen: (v: boolean) => void }) {
  const emails = useStoreState(ctx.store).emails
  const email = result.emailId ? emails[result.emailId] : undefined
  const kind = emailKindFor(task, result)

  const initial = (): ComposerInitial => {
    const name = eventNameOf(ctx.store, event)
    return {
      to: result.contact?.email ?? '',
      subject: defaultSubject(name, event, kind, result),
      body: defaultBody(name, event, kind, result),
      kind,
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation()
          setOpen(true)
        }}
        className="nodrag inline-flex items-center gap-1.5 rounded-full bg-accent-soft px-3 py-1.5 text-xs font-medium text-accent transition hover:bg-indigo-100"
      >
        <Mail size={13} />
        {email ? 'View email' : 'Email'}
        {email && <StatusChip status={email.status} />}
      </button>
      {open && (
        <EmailComposer
          eventId={ctx.eventId}
          store={ctx.store}
          api={ctx.api}
          emailId={result.emailId}
          initial={initial()}
          relatedCardId={result.cardId}
          onClose={() => setOpen(false)}
          onSaved={(saved) => result.emailId !== saved.id && onUpdate({ ...result, emailId: saved.id })}
          onDeleted={() => onUpdate({ ...result, emailId: undefined })}
        />
      )}
    </>
  )
}
