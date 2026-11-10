import { FileUp, Loader2 } from 'lucide-react'
import { useState, type DragEvent } from 'react'
import { newDeckId, saveDeck } from '../../lib/deckStore'
import { ACCEPT, SlideFileError, deckTitle, readSlides } from '../../lib/slideFiles'

// Upload a deck made elsewhere; it gets its own presenter view and audience QR code.
// `deckId`: save into this deck (a workflow step's) instead of a new one.
export default function Upload({ deckId }: { deckId?: string }) {
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)

  const upload = async (list: FileList | null) => {
    const files = [...(list ?? [])]
    if (!files.length || busy) return
    setBusy(true)
    setError(null)
    setProgress(null)
    try {
      const slides = await readSlides(files, (done, total) => setProgress({ done, total }))
      const id = deckId ?? newDeckId()
      await saveDeck(id, { title: deckTitle(files), slides })
      location.hash = `/present/${id}`
    } catch (e) {
      setError(e instanceof SlideFileError ? e.message : 'Those slides couldn’t be saved. Try again, or use a smaller file.')
      setBusy(false)
    }
  }

  const onDrop = (e: DragEvent) => {
    e.preventDefault()
    setDragging(false)
    upload(e.dataTransfer.files)
  }

  return (
    <div className="h-full overflow-y-auto bg-gray-100 text-gray-900">
      <div className="mx-auto max-w-2xl px-6 py-16">
        <a href="#" className="text-sm font-medium text-gray-500 hover:text-accent">
          Back to Evently
        </a>
        <h1 className="mt-6 text-3xl font-bold tracking-tight">Present your own slides</h1>
        <p className="mt-3 text-gray-600">
          Upload your deck and Evently gives you a presenter view and a QR code. Anyone who scans it follows along as you change slides.
        </p>

        <label
          onDragOver={(e) => {
            e.preventDefault()
            setDragging(true)
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          className={`mt-8 flex cursor-pointer flex-col items-center rounded-3xl border-2 border-dashed bg-white px-6 py-14 text-center transition focus-within:ring-2 focus-within:ring-accent ${
            dragging ? 'border-accent bg-accent-soft' : 'border-gray-300 hover:border-accent'
          } ${busy ? 'pointer-events-none' : ''}`}
        >
          <input type="file" accept={ACCEPT} multiple className="sr-only" disabled={busy} onChange={(e) => upload(e.target.files)} />
          {busy ? (
            <>
              <Loader2 size={28} className="animate-spin text-accent" />
              <p className="mt-4 font-semibold">{progress ? `Preparing slide ${progress.done} of ${progress.total}` : 'Opening your file…'}</p>
              {progress && (
                <div className="mt-4 h-1.5 w-56 overflow-hidden rounded-full bg-gray-100">
                  <div className="h-full rounded-full bg-accent transition-[width]" style={{ width: `${(progress.done / progress.total) * 100}%` }} />
                </div>
              )}
            </>
          ) : (
            <>
              <span className="grid size-14 place-items-center rounded-full bg-accent-soft text-accent">
                <FileUp size={24} />
              </span>
              <p className="mt-4 font-semibold">Drop your slides here, or click to choose a file</p>
              <p className="mt-1 text-sm text-gray-500">A PDF, or PNG, JPG or WebP images with one slide each</p>
            </>
          )}
        </label>

        {error && (
          <p role="alert" className="mt-4 rounded-2xl bg-warn-soft px-4 py-3 text-sm text-warn">
            {error}
          </p>
        )}

        <div className="mt-8 rounded-3xl bg-white p-6 text-sm text-gray-600 ring-1 ring-gray-900/5">
          <h2 className="font-semibold text-gray-900">Made your deck in PowerPoint, Keynote or Google Slides?</h2>
          <p className="mt-2">
            Export it as a PDF first. In PowerPoint or Keynote, choose File, then Export, then PDF. In Google Slides, choose File, then Download,
            then PDF Document.
          </p>
          <p className="mt-3 text-gray-500">Animations and embedded videos don’t carry over. Each slide shows as it looks in the PDF.</p>
        </div>

        <p className="mt-6 text-sm text-gray-500">
          No deck yet?{' '}
          <a href="#/present/demo" className="font-semibold text-accent hover:underline">
            Try the sample deck
          </a>
        </p>
      </div>
    </div>
  )
}
