import { X } from 'lucide-react'
import { useEffect, type ReactNode, type SyntheticEvent } from 'react'
import { createPortal } from 'react-dom'

const stop = (e: SyntheticEvent) => e.stopPropagation()

/** Centered dialog rendered into document.body. Events don't bubble into the canvas node that opened it. */
export default function Modal({ title, onClose, children, footer }: { title: string; onClose: () => void; children: ReactNode; footer?: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onClick={stop}
      onMouseDown={stop}
      onPointerDown={stop}
      onKeyDown={stop}
      onDoubleClick={stop}
      className="fixed inset-0 z-50 grid place-items-center bg-gray-900/40 p-4 backdrop-blur-[2px]"
    >
      <div className="flex max-h-[90vh] w-full max-w-xl animate-rise cursor-default flex-col rounded-2xl bg-white text-left text-sm font-normal text-ink shadow-2xl ring-1 ring-gray-900/10">
        <div className="flex items-center gap-2 border-b border-line px-5 py-3.5">
          <h2 className="flex-1 text-base font-semibold">{title}</h2>
          <button type="button" onClick={onClose} aria-label="Close" className="rounded-full p-1 text-muted hover:bg-gray-100">
            <X size={16} />
          </button>
        </div>
        <div className="nowheel flex-1 space-y-3 overflow-y-auto px-5 py-4">{children}</div>
        {footer && <div className="flex flex-wrap items-center justify-end gap-2 border-t border-line px-5 py-3">{footer}</div>}
      </div>
    </div>,
    document.body,
  )
}

export const inputCls =
  'w-full rounded-xl border border-line bg-white px-3 py-2 text-sm text-ink outline-none transition placeholder:text-gray-400 focus:border-accent focus:ring-2 focus:ring-accent/20 disabled:bg-gray-50 disabled:text-muted'

export const btnPrimary =
  'inline-flex items-center gap-1.5 rounded-full bg-accent px-3.5 py-1.5 text-sm font-medium text-white transition hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50'
export const btnGhost =
  'inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm font-medium text-ink ring-1 ring-gray-900/10 transition hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50'
export const btnDanger =
  'inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm font-medium text-red-600 ring-1 ring-red-600/20 transition hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50'

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-semibold tracking-wide text-muted uppercase">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-muted">{hint}</span>}
    </label>
  )
}
