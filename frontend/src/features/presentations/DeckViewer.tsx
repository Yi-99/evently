// Deck viewer modal: the rendered deck (sandboxed, driven over the deck's postMessage protocol), a slide
// strip with reorder/delete, speaker notes, prompt-based editing, export and "Present live".
//
// Render endpoints only accept the owner's token as a Bearer header, so the deck and thumbnail documents
// are fetched with the API client and shown through `srcdoc`. The deck document keeps its own strict CSP
// (<meta> tag + per-response script nonce); we only ever talk to it with postMessage:
//   in : {type: 'evently:goto', index}
//   out: {type: 'evently:ready', slideCount, index}, {type: 'evently:slide', index}
import {
  ArrowDown,
  ArrowUp,
  ChevronLeft,
  ChevronRight,
  Download,
  GripVertical,
  LoaderCircle,
  Presentation,
  RefreshCw,
  Trash,
  WandSparkles,
  X,
} from 'lucide-react'
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type FormEvent,
  type KeyboardEvent,
  type SyntheticEvent,
} from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { isApiError } from '../../api/client'
import { isRunFinished, slidesOf } from '../../api/realtime'
import type { AgentRunOut, PresentationDetail, RunStarted, SlideOut } from '../../api/types'
import type { RunnerContext } from '../../lib/runners'
import { startLiveSession } from './live'
import { mergeDeck } from './runner'

const STAGE_W = 1280
const THUMB_W = 160
const HTML_ACCEPT = { Accept: 'text/html' }

/** A run's error without the exception class prefix, kept short. */
const runError = (run: AgentRunOut): string => {
  const text = (run.error ?? '').replace(/^\w*Error:\s*/, '')
  return text ? (text.length > 240 ? `${text.slice(0, 240)}…` : text) : `${run.title || 'Agent run'} ${run.status}`
}

const errorText = (e: unknown, what: string): string => {
  if (isApiError(e, 503)) return `${what} needs Claude: set ANTHROPIC_API_KEY on the backend.`
  return e instanceof Error ? e.message : `${what} failed.`
}

/** Keep React events that bubble out of the portal from reaching the canvas node that rendered us. */
const stop = (e: SyntheticEvent) => e.stopPropagation()

// --- thumbnails --------------------------------------------------------------------------------

const thumbCache = new Map<string, string>()

function SlideThumb({ slide, themeVersion, ctx }: { slide: SlideOut; themeVersion: number; ctx: RunnerContext }) {
  const key = `${slide.id}:${slide.version}:${themeVersion}`
  const [loaded, setLoaded] = useState<{ key: string; html: string } | null>(null)
  const html = loaded?.key === key ? loaded.html : (thumbCache.get(key) ?? null)
  const renderable = slide.status === 'ready' && !!slide.html

  useEffect(() => {
    if (!renderable || thumbCache.has(key)) return
    let cancelled = false
    ctx.api
      .get<string>(`/slides/${encodeURIComponent(slide.id)}/render`, { headers: HTML_ACCEPT })
      .then((doc) => {
        thumbCache.set(key, doc)
        if (!cancelled) setLoaded({ key, html: doc })
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [ctx.api, key, renderable, slide.id])

  return (
    <div className="relative aspect-video w-full overflow-hidden rounded-md bg-gray-900">
      {html && (
        <iframe
          title={`Slide ${slide.position + 1} preview`}
          sandbox=""
          srcDoc={html}
          tabIndex={-1}
          aria-hidden
          className="pointer-events-none absolute left-0 top-0 origin-top-left border-0"
          style={{ width: STAGE_W, height: (STAGE_W * 9) / 16, transform: `scale(${THUMB_W / STAGE_W})` }}
        />
      )}
      {slide.status !== 'ready' && (
        <div className="absolute inset-0 grid place-items-center bg-gray-900/70 text-[11px] text-white">
          {slide.status === 'error' ? (
            <span className="text-rose-300">Failed</span>
          ) : (
            <span className="flex items-center gap-1">
              <LoaderCircle size={12} className="animate-spin" />
              {slide.status === 'rendering' ? 'Designing' : 'Queued'}
            </span>
          )}
        </div>
      )}
    </div>
  )
}

// --- run progress ------------------------------------------------------------------------------

function RunLine({ run }: { run: AgentRunOut }) {
  const failed = run.status === 'failed' || run.status === 'cancelled'
  if (isRunFinished(run) && !failed) return null
  return (
    <div className="rounded-lg bg-run-soft/60 px-3 py-2 text-xs">
      <div className="flex items-center gap-2">
        {!isRunFinished(run) && <LoaderCircle size={12} className="shrink-0 animate-spin text-run" />}
        <span className={`truncate ${failed ? 'text-warn' : 'text-ink'}`}>
          {failed ? runError(run) : run.current_step || run.title || 'Working…'}
        </span>
      </div>
      {!isRunFinished(run) && (
        <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-white">
          <div className="h-full rounded-full bg-run transition-all duration-500" style={{ width: `${Math.max(4, run.progress * 100)}%` }} />
        </div>
      )}
    </div>
  )
}

// --- viewer --------------------------------------------------------------------------------------

export interface DeckViewerProps {
  presentationId: string
  ctx: RunnerContext
  onClose: () => void
}

export default function DeckViewer({ presentationId, ctx, onClose }: DeckViewerProps) {
  const { store, api, eventId } = ctx
  const navigate = useNavigate()
  const state = useSyncExternalStore(store.subscribe, store.getState)
  const presentation = state.presentations[presentationId] ?? null
  const slides = useMemo(() => slidesOf(state, presentationId), [state, presentationId])
  const pid = encodeURIComponent(presentationId)

  const [index, setIndex] = useState(0)
  const [deck, setDeck] = useState<{ html: string | null; error: string | null }>({ html: null, error: null })
  const [error, setError] = useState<string | null>(null)
  const [prompt, setPrompt] = useState('')
  const [busy, setBusy] = useState<null | 'slide' | 'deck' | 'live' | 'export'>(null)
  const [runIds, setRunIds] = useState<string[]>([])
  const [dragId, setDragId] = useState<string | null>(null)

  const frameRef = useRef<HTMLIFrameElement>(null)
  const rootRef = useRef<HTMLDivElement>(null)
  const indexRef = useRef(0)
  const hasDeckRef = useRef(false)
  const aliveRef = useRef(new AbortController())

  const current = slides[Math.min(index, slides.length - 1)] ?? null

  useEffect(() => {
    indexRef.current = index
  }, [index])

  // Keep the store live while open, and stop background waits on close.
  useEffect(() => {
    const release = store.retain()
    const alive = new AbortController()
    aliveRef.current = alive
    return () => {
      alive.abort()
      release()
    }
  }, [store])

  useEffect(() => {
    rootRef.current?.focus()
  }, [])

  const refresh = useCallback(async () => {
    const d = await api.get<PresentationDetail>(`/presentations/${pid}`)
    mergeDeck(store, d.presentation, d.slides)
  }, [api, pid, store])

  // The snapshot may predate this deck (or the socket may be down): load it once on open.
  useEffect(() => {
    refresh().catch((e) => setError(errorText(e, 'Loading the deck')))
  }, [refresh])

  // (Re)fetch the deck document whenever the deck changes; debounced because a build streams many updates.
  const signature = presentation
    ? `${presentation.version}|${slides.map((s) => `${s.id}:${s.version}:${s.status}`).join(',')}`
    : ''
  useEffect(() => {
    if (!signature || !signature.includes(':')) return
    let cancelled = false
    const timer = setTimeout(
      () => {
        api
          .get<string>(`/presentations/${pid}/render`, { query: { slide: indexRef.current, controls: true }, headers: HTML_ACCEPT })
          .then((html) => {
            if (cancelled) return
            hasDeckRef.current = true
            setDeck({ html, error: null })
          })
          .catch((e) => {
            if (!cancelled) setDeck((d) => ({ html: d.html, error: errorText(e, 'Rendering the deck') }))
          })
      },
      hasDeckRef.current ? 500 : 0,
    )
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [api, pid, signature])

  // Deck → viewer messages. Only trust our own frame (the deck has an opaque origin, so check the source).
  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      if (!frameRef.current || e.source !== frameRef.current.contentWindow) return
      const data = e.data as { type?: unknown; index?: unknown }
      if (!data || typeof data.index !== 'number') return
      if ((data.type === 'evently:ready' || data.type === 'evently:slide') && data.index >= 0) setIndex(data.index)
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])

  const goto = useCallback(
    (i: number) => {
      if (!slides.length) return
      const next = Math.max(0, Math.min(slides.length - 1, i))
      setIndex(next)
      frameRef.current?.contentWindow?.postMessage({ type: 'evently:goto', index: next }, '*')
    },
    [slides.length],
  )

  // --- slide structure -------------------------------------------------------------------------

  const reorder = async (ids: string[]) => {
    const followId = current?.id
    store.patch((s) => {
      const next = { ...s.slides }
      ids.forEach((id, position) => {
        if (next[id]) next[id] = { ...next[id], position }
      })
      return { ...s, slides: next }
    })
    if (followId) goto(ids.indexOf(followId))
    try {
      await api.post(`/presentations/${pid}/slides/reorder`, { slide_ids: ids })
    } catch (e) {
      setError(errorText(e, 'Reordering'))
      void refresh().catch(() => {})
    }
  }

  const move = (slideId: string, to: number) => {
    const ids = slides.map((s) => s.id)
    const from = ids.indexOf(slideId)
    if (from < 0 || to < 0 || to >= ids.length || from === to) return
    ids.splice(from, 1)
    ids.splice(to, 0, slideId)
    void reorder(ids)
  }

  const remove = async (slide: SlideOut) => {
    if (!window.confirm(`Delete slide ${slide.position + 1}${slide.title ? ` “${slide.title}”` : ''}?`)) return
    store.patch((s) => {
      const next = { ...s.slides }
      delete next[slide.id]
      return { ...s, slides: next }
    })
    try {
      await api.del(`/slides/${encodeURIComponent(slide.id)}`)
    } catch (e) {
      setError(errorText(e, 'Deleting the slide'))
    }
    void refresh().catch(() => {})
  }

  // --- prompts, export, live -------------------------------------------------------------------

  const submitPrompt = async (scope: 'slide' | 'deck', e?: FormEvent) => {
    e?.preventDefault()
    const instruction = prompt.trim()
    if (!instruction || busy) return
    if (scope === 'slide' && !current) return
    setBusy(scope)
    setError(null)
    try {
      const path = scope === 'slide' ? `/slides/${encodeURIComponent(current!.id)}/prompt` : `/presentations/${pid}/prompt`
      const { run } = await api.post<RunStarted>(path, { instruction })
      store.patch((s) => (s.runs[run.id] ? s : { ...s, runs: { ...s.runs, [run.id]: run } }))
      setRunIds((ids) => [...ids, run.id])
      setPrompt('')
      // Polls GET /agent-runs/{id} while the socket is down; refetch the deck when the edit lands.
      store
        .waitForRun(run.id, { signal: aliveRef.current.signal })
        .then(() => refresh())
        .catch(() => {})
    } catch (err) {
      setError(errorText(err, scope === 'slide' ? 'Editing the slide' : 'Editing the deck'))
    } finally {
      setBusy(null)
    }
  }

  const exportHtml = async () => {
    setBusy('export')
    try {
      const html = await api.get<string>(`/presentations/${pid}/export`, { headers: HTML_ACCEPT })
      const url = URL.createObjectURL(new Blob([html], { type: 'text/html' }))
      const a = document.createElement('a')
      a.href = url
      a.download = `${(presentation?.title ?? 'deck').replace(/[^A-Za-z0-9]+/g, '-').replace(/^-|-$/g, '').toLowerCase() || 'deck'}.html`
      a.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (e) {
      setError(errorText(e, 'Export'))
    } finally {
      setBusy(null)
    }
  }

  const presentLive = async () => {
    if (!presentation) return
    setBusy('live')
    setError(null)
    try {
      const sessionId = await startLiveSession(api, eventId, presentation.id, presentation.title)
      onClose()
      navigate(`/present/${encodeURIComponent(sessionId)}`)
    } catch (e) {
      setError(errorText(e, 'Starting the live session'))
      setBusy(null)
    }
  }

  const onKeyDown = (e: KeyboardEvent) => {
    e.stopPropagation()
    const t = e.target as HTMLElement
    if (t.closest('textarea, input, select, [contenteditable="true"]')) {
      if (e.key === 'Escape') (t as HTMLElement).blur()
      return
    }
    if (e.key === 'Escape') onClose()
    else if (e.key === 'ArrowRight' || e.key === 'PageDown') goto(index + 1)
    else if (e.key === 'ArrowLeft' || e.key === 'PageUp') goto(index - 1)
    else return
    e.preventDefault()
  }

  // The build run only matters while building, or when the build left the deck in error.
  const buildRun = presentation && presentation.status !== 'ready' ? presentation.agent_run_id : null
  const runs = [buildRun, ...runIds]
    .filter((id): id is string => !!id)
    .map((id) => state.runs[id])
    .filter((r): r is AgentRunOut => !!r)
  const generating = presentation?.status === 'generating'
  const editing = runs.some((r) => !isRunFinished(r))

  return createPortal(
    <div
      ref={rootRef}
      tabIndex={-1}
      role="dialog"
      aria-modal="true"
      aria-label={presentation?.title ?? 'Deck'}
      onClick={stop}
      onPointerDown={stop}
      onMouseDown={stop}
      onDoubleClick={stop}
      onWheel={stop}
      onKeyDown={onKeyDown}
      className="fixed inset-0 z-[1000] flex items-stretch justify-center bg-gray-950/60 p-0 outline-none backdrop-blur-sm sm:p-6"
    >
      <div className="flex min-h-0 w-full max-w-7xl flex-col overflow-hidden bg-white shadow-2xl sm:rounded-2xl">
        {/* header */}
        <header className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-3">
          <Presentation size={18} className="shrink-0 text-accent" />
          <div className="min-w-0 flex-1">
            <h2 className="truncate text-sm font-semibold text-ink">{presentation?.title ?? 'Loading deck…'}</h2>
            <p className="text-xs text-muted">
              {slides.length} slide{slides.length === 1 ? '' : 's'}
              {generating ? ' · building…' : presentation?.status === 'error' ? ' · build failed' : ''}
            </p>
          </div>
          <button
            onClick={() => void refresh().catch((e) => setError(errorText(e, 'Refreshing')))}
            className="rounded-full p-2 text-muted hover:bg-gray-100"
            aria-label="Refresh"
            title="Refresh"
          >
            <RefreshCw size={15} />
          </button>
          <button
            onClick={() => void exportHtml()}
            disabled={!slides.length || busy === 'export'}
            className="flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium text-ink ring-1 ring-line hover:bg-gray-50 disabled:opacity-40"
          >
            <Download size={14} /> Export HTML
          </button>
          <button
            onClick={() => void presentLive()}
            disabled={!presentation || !slides.length || busy === 'live'}
            className="flex items-center gap-1.5 rounded-full bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent/90 disabled:opacity-40"
          >
            {busy === 'live' ? <LoaderCircle size={14} className="animate-spin" /> : <Presentation size={14} />} Present live
          </button>
          <button onClick={onClose} className="rounded-full p-2 text-muted hover:bg-gray-100" aria-label="Close">
            <X size={16} />
          </button>
        </header>

        <div className="flex min-h-0 flex-1 flex-col md:flex-row">
          {/* slide strip */}
          <ol className="flex shrink-0 gap-2 overflow-auto border-b border-line bg-gray-50 p-3 md:w-56 md:flex-col md:border-b-0 md:border-r">
            {slides.map((s, i) => (
              <li
                key={s.id}
                draggable
                onDragStart={(e) => {
                  setDragId(s.id)
                  e.dataTransfer.effectAllowed = 'move'
                }}
                onDragOver={(e) => {
                  if (dragId && dragId !== s.id) e.preventDefault()
                }}
                onDrop={(e) => {
                  e.preventDefault()
                  if (dragId) move(dragId, i)
                  setDragId(null)
                }}
                onDragEnd={() => setDragId(null)}
                className={`group relative w-44 shrink-0 cursor-pointer rounded-lg p-1.5 md:w-auto ${
                  i === index ? 'bg-accent-soft ring-2 ring-accent' : 'hover:bg-white hover:ring-1 hover:ring-line'
                } ${dragId === s.id ? 'opacity-40' : ''}`}
                onClick={() => goto(i)}
              >
                <div style={{ width: THUMB_W }} className="mx-auto max-w-full">
                  <SlideThumb slide={s} themeVersion={presentation?.version ?? 0} ctx={ctx} />
                </div>
                <div className="mt-1 flex items-center gap-1 text-[11px] text-muted">
                  <GripVertical size={11} className="shrink-0 opacity-50" />
                  <span className="font-medium text-ink">{i + 1}</span>
                  <span className="truncate">{s.title || 'Untitled'}</span>
                </div>
                <div className="absolute right-2 top-2 hidden gap-0.5 rounded-md bg-white/95 p-0.5 shadow group-hover:flex">
                  <button
                    onClick={(e) => (e.stopPropagation(), move(s.id, i - 1))}
                    disabled={i === 0}
                    className="rounded p-1 text-muted hover:text-ink disabled:opacity-30"
                    aria-label="Move up"
                  >
                    <ArrowUp size={12} />
                  </button>
                  <button
                    onClick={(e) => (e.stopPropagation(), move(s.id, i + 1))}
                    disabled={i === slides.length - 1}
                    className="rounded p-1 text-muted hover:text-ink disabled:opacity-30"
                    aria-label="Move down"
                  >
                    <ArrowDown size={12} />
                  </button>
                  <button
                    onClick={(e) => (e.stopPropagation(), void remove(s))}
                    className="rounded p-1 text-muted hover:text-warn"
                    aria-label="Delete slide"
                  >
                    <Trash size={12} />
                  </button>
                </div>
              </li>
            ))}
            {!slides.length && (
              <li className="flex items-center gap-2 p-2 text-xs text-muted">
                {generating ? <LoaderCircle size={12} className="animate-spin" /> : null}
                {generating ? 'Outlining the deck…' : 'No slides yet.'}
              </li>
            )}
          </ol>

          {/* stage + notes + prompt */}
          <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-3 overflow-y-auto p-4">
            <div className="relative aspect-video w-full overflow-hidden rounded-xl bg-gray-900">
              {deck.html && slides.length > 0 ? (
                <iframe
                  ref={frameRef}
                  title={presentation?.title ?? 'Deck'}
                  // Scripts only (the deck's own nonce'd navigation script); opaque origin, no navigation.
                  sandbox="allow-scripts"
                  srcDoc={deck.html}
                  className="absolute inset-0 h-full w-full border-0"
                />
              ) : (
                <div className="absolute inset-0 grid place-items-center text-sm text-gray-300">
                  <span className="flex items-center gap-2">
                    {deck.error ?? (
                      <>
                        <LoaderCircle size={16} className="animate-spin" /> {generating ? 'Building the deck…' : 'Loading…'}
                      </>
                    )}
                  </span>
                </div>
              )}
            </div>

            <div className="flex items-center justify-center gap-3 text-sm">
              <button
                onClick={() => goto(index - 1)}
                disabled={index <= 0}
                className="rounded-full p-2 text-ink ring-1 ring-line hover:bg-gray-50 disabled:opacity-30"
                aria-label="Previous slide"
              >
                <ChevronLeft size={16} />
              </button>
              <span className="min-w-16 text-center tabular-nums text-muted">
                {slides.length ? `${Math.min(index, slides.length - 1) + 1} / ${slides.length}` : '–'}
              </span>
              <button
                onClick={() => goto(index + 1)}
                disabled={index >= slides.length - 1}
                className="rounded-full p-2 text-ink ring-1 ring-line hover:bg-gray-50 disabled:opacity-30"
                aria-label="Next slide"
              >
                <ChevronRight size={16} />
              </button>
            </div>

            {(error || deck.error) && (
              <p className="rounded-lg bg-warn-soft px-3 py-2 text-xs text-warn">{error ?? deck.error}</p>
            )}
            {runs.map((r) => (
              <RunLine key={r.id} run={r} />
            ))}

            <div className="grid gap-3 lg:grid-cols-2">
              <section className="rounded-xl p-3 ring-1 ring-line">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">Speaker notes</h3>
                <p className="mt-2 whitespace-pre-wrap text-sm text-ink">
                  {current?.notes || <span className="text-muted">No notes for this slide.</span>}
                </p>
                {!!current?.keywords.length && (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {current.keywords.map((k) => (
                      <span key={k} className="rounded-full bg-accent-soft px-2 py-0.5 text-[11px] text-accent" title="Voice cue">
                        {k}
                      </span>
                    ))}
                  </div>
                )}
              </section>

              <form onSubmit={(e) => void submitPrompt('slide', e)} className="flex flex-col gap-2 rounded-xl p-3 ring-1 ring-line">
                <h3 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted">
                  <WandSparkles size={12} /> Edit with a prompt
                </h3>
                <textarea
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) void submitPrompt('slide')
                  }}
                  rows={3}
                  placeholder="e.g. Make this slide a big countdown with the start time"
                  className="min-h-20 resize-y rounded-lg px-3 py-2 text-sm ring-1 ring-line outline-none focus:ring-accent"
                />
                <div className="flex flex-wrap items-center justify-end gap-2">
                  {editing && <span className="mr-auto text-xs text-muted">An agent is working on the deck…</span>}
                  <button
                    type="button"
                    onClick={() => void submitPrompt('deck')}
                    disabled={!prompt.trim() || !!busy || !presentation}
                    className="rounded-full px-3 py-1.5 text-xs font-medium text-ink ring-1 ring-line hover:bg-gray-50 disabled:opacity-40"
                  >
                    {busy === 'deck' ? 'Sending…' : 'Edit the whole deck'}
                  </button>
                  <button
                    type="submit"
                    disabled={!prompt.trim() || !!busy || !current}
                    className="rounded-full bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent/90 disabled:opacity-40"
                  >
                    {busy === 'slide' ? 'Sending…' : 'Edit this slide'}
                  </button>
                </div>
              </form>
            </div>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}
