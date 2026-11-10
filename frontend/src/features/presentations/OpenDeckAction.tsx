// "Open deck" button in a deck result's expanded card; opens the DeckViewer modal.
import { Presentation } from 'lucide-react'
import { useState, type MouseEvent } from 'react'
import { slidesOf } from '../../api/realtime'
import type { ResultActionProps } from '../../lib/resultActions'
import DeckViewer from './DeckViewer'

export default function OpenDeckAction({ result, ctx, onUpdate }: ResultActionProps) {
  const [open, setOpen] = useState(false)
  const id = result.presentationId
  if (!id || !ctx) return null

  const close = () => {
    setOpen(false)
    // Reflect edits made in the viewer (title, slide count, status) on the result card.
    const s = ctx.store.getState()
    const p = s.presentations[id]
    if (!p) return
    const n = slidesOf(s, id).length
    const subtitle = `${n} slide${n === 1 ? '' : 's'}`
    if (p.title !== result.title || subtitle !== result.subtitle || p.status !== result.extra?.status)
      onUpdate({ ...result, title: p.title, subtitle, extra: { ...result.extra, status: p.status } })
  }

  return (
    <>
      <button
        type="button"
        onClick={(e: MouseEvent) => {
          e.stopPropagation()
          setOpen(true)
        }}
        className="flex items-center gap-1.5 rounded-full bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent/90"
      >
        <Presentation size={13} /> Open deck
      </button>
      {open && <DeckViewer presentationId={id} ctx={ctx} onClose={close} />}
    </>
  )
}
