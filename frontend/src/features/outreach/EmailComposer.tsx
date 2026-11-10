import { LoaderCircle, Save, Send, Trash2 } from 'lucide-react'
import { useState } from 'react'
import type { Api } from '../../api/client'
import type { EventStore } from '../../api/realtime'
import type { EmailDraftIn, EmailMessageOut } from '../../api/types'
import Modal, { Field, btnDanger, btnGhost, btnPrimary, inputCls } from './Modal'
import StatusChip from './StatusChip'
import { cacheEmail, errorText, isEmail, parseEmails, uncacheEmail, useStoreState, type EmailKind } from './lib'

export interface ComposerInitial {
  to: string
  subject: string
  body: string
  kind: EmailKind
}

interface Props {
  eventId: string
  store: EventStore
  api: Api
  /** An existing backend email to edit/send; omit to compose a new one. */
  emailId?: string
  initial: ComposerInitial
  relatedCardId?: string
  onClose: () => void
  /** A draft was created or changed. */
  onSaved?: (email: EmailMessageOut) => void
  onDeleted?: () => void
}

type Busy = 'save' | 'send' | 'delete' | null

export default function EmailComposer({ eventId, store, api, emailId, initial, relatedCardId, onClose, onSaved, onDeleted }: Props) {
  const emails = useStoreState(store).emails
  const [currentId, setCurrentId] = useState(emailId && emails[emailId] ? emailId : undefined)
  const email = currentId ? emails[currentId] : undefined
  const [to, setTo] = useState(email ? email.to.join(', ') : initial.to)
  const [subject, setSubject] = useState(email?.subject ?? initial.subject)
  const [body, setBody] = useState(email?.body_text ?? initial.body)
  const [busy, setBusy] = useState<Busy>(null)
  const [error, setError] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)

  const status = email?.status
  const editable = !email || email.status === 'draft'
  const recipients = parseEmails(to)
  const bad = recipients.filter((r) => !isEmail(r))
  const dirty = !email || email.to.join(',') !== recipients.join(',') || email.subject !== subject.trim() || email.body_text !== body
  const valid = recipients.length > 0 && bad.length === 0 && subject.trim().length > 0 && body.trim().length > 0

  const problem = (): string | null => {
    if (!recipients.length) return 'Add at least one recipient.'
    if (bad.length) return `Not a valid email address: ${bad.join(', ')}`
    if (!subject.trim()) return 'Add a subject.'
    if (!body.trim()) return 'Write a message.'
    return null
  }

  /** Create or update the draft. Returns the saved email. */
  const save = async (): Promise<EmailMessageOut> => {
    const id = encodeURIComponent(eventId)
    let saved: EmailMessageOut
    if (currentId) {
      saved = await api.patch<EmailMessageOut>(`/emails/${encodeURIComponent(currentId)}`, {
        to: recipients,
        subject: subject.trim(),
        body_text: body,
      })
    } else {
      const draft: EmailDraftIn = {
        kind: initial.kind,
        to: recipients,
        cc: [],
        subject: subject.trim(),
        body_text: body,
        related_card_id: relatedCardId ?? null,
      }
      saved = await api.post<EmailMessageOut>(`/events/${id}/emails`, draft)
      setCurrentId(saved.id)
    }
    cacheEmail(store, saved)
    onSaved?.(saved)
    return saved
  }

  const run = async (kind: Exclude<Busy, null>, fn: () => Promise<void>) => {
    setBusy(kind)
    setError(null)
    try {
      await fn()
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(null)
    }
  }

  const onSave = () => {
    const p = problem()
    if (p) return setError(p)
    void run('save', async () => void (await save()))
  }

  const onSend = () =>
    void run('send', async () => {
      let id = currentId
      if (!email || (email.status === 'draft' && dirty)) id = (await save()).id
      if (!id) return
      const sent = await api.post<EmailMessageOut>(`/emails/${encodeURIComponent(id)}/send`)
      cacheEmail(store, sent)
      onSaved?.(sent)
      setConfirming(false)
    })

  const onDelete = () => {
    if (!currentId || !window.confirm('Delete this draft?')) return
    void run('delete', async () => {
      await api.del(`/emails/${encodeURIComponent(currentId)}`)
      uncacheEmail(store, currentId)
      onDeleted?.()
      onClose()
    })
  }

  const working = busy !== null
  const canSend = (!email || email.status === 'draft' || email.status === 'failed') && !(email?.status === 'failed' && dirty)

  return (
    <Modal
      title={email ? 'Email' : 'New email'}
      onClose={onClose}
      footer={
        confirming ? (
          <>
            <span className="mr-auto text-sm text-ink">
              Send this email to <b>{recipients.join(', ')}</b> now?
            </span>
            <button type="button" className={btnGhost} onClick={() => setConfirming(false)} disabled={working}>
              Cancel
            </button>
            <button type="button" className={btnPrimary} onClick={onSend} disabled={working}>
              {busy === 'send' ? <LoaderCircle size={14} className="animate-spin" /> : <Send size={14} />}
              Confirm and send
            </button>
          </>
        ) : (
          <>
            {email && email.status === 'draft' && (
              <button type="button" className={`${btnDanger} mr-auto`} onClick={onDelete} disabled={working}>
                {busy === 'delete' ? <LoaderCircle size={14} className="animate-spin" /> : <Trash2 size={14} />}
                Delete
              </button>
            )}
            {email && email.status === 'failed' && (
              <button
                type="button"
                className={`${btnGhost} mr-auto`}
                onClick={() => {
                  // A failed email is locked on the server; continue as a fresh draft with the same text.
                  setCurrentId(undefined)
                  setError(null)
                }}
                disabled={working}
              >
                Edit as new draft
              </button>
            )}
            <button type="button" className={btnGhost} onClick={onClose}>
              {editable ? 'Close' : 'Done'}
            </button>
            {editable && (
              <button type="button" className={btnGhost} onClick={onSave} disabled={working || (!!email && !dirty)}>
                {busy === 'save' ? <LoaderCircle size={14} className="animate-spin" /> : <Save size={14} />}
                {email ? 'Save changes' : 'Save draft'}
              </button>
            )}
            {canSend && (
              <button
                type="button"
                className={btnPrimary}
                disabled={working || !valid}
                onClick={() => {
                  const p = problem()
                  if (p) setError(p)
                  else {
                    setError(null)
                    setConfirming(true)
                  }
                }}
              >
                <Send size={14} />
                {status === 'failed' ? 'Retry send' : 'Send…'}
              </button>
            )}
          </>
        )
      }
    >
      {email && (
        <div className="flex flex-wrap items-center gap-2">
          <StatusChip status={email.status} />
          {email.provider && <span className="text-xs text-muted">via {email.provider}</span>}
          {email.sent_at && <span className="text-xs text-muted">{new Date(email.sent_at).toLocaleString()}</span>}
          {email.status === 'sent' && email.provider === 'console' && (
            <span className="text-xs text-muted">(console provider: logged by the server, not delivered)</span>
          )}
        </div>
      )}
      {email?.status === 'failed' && email.error && (
        <p role="alert" className="rounded-xl bg-red-50 px-3 py-2 text-xs text-red-700">
          Sending failed: {email.error}
        </p>
      )}
      <Field label="To" hint={editable ? 'Separate several addresses with commas.' : undefined}>
        <input className={inputCls} value={to} onChange={(e) => setTo(e.target.value)} disabled={!editable} placeholder="name@example.com" />
      </Field>
      <Field label="Subject">
        <input className={inputCls} value={subject} onChange={(e) => setSubject(e.target.value)} disabled={!editable} />
      </Field>
      <Field label="Message">
        <textarea className={`${inputCls} min-h-56 resize-y leading-relaxed`} value={body} onChange={(e) => setBody(e.target.value)} disabled={!editable} />
      </Field>
      {!email && <p className="text-xs text-muted">Saving creates a draft. Nothing is sent until you confirm.</p>}
      {error && (
        <p role="alert" className="rounded-xl bg-red-50 px-3 py-2 text-xs text-red-700">
          {error}
        </p>
      )}
    </Modal>
  )
}
