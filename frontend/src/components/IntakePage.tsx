import { ArrowLeft, ArrowRight, ArrowUp, Loader2, Sparkles } from 'lucide-react'
import { useState } from 'react'
import { EVENT_TYPES } from '../data/workflows'
import { BUILD_NOW, QUESTIONS, SKIP, TIERS, TIER_INTRO, isRequired, type IntakeAnswers, type IntakeQuestion } from '../lib/intake'
import type { EventDetails } from '../types'

interface Props {
  event: EventDetails
  answers: IntakeAnswers
  // Question ids already answered before this page, which Previous can't reopen.
  prefilled: string[]
  question: IntakeQuestion
  // Why the last answer was rejected, if it was.
  note: string | null
  // What Claude just filled in from typed text, if anything.
  info: string | null
  // True while Claude reads a typed description; answering is paused.
  extracting: boolean
  // Preset options, Skip and Build my plan.
  onAnswer: (text: string) => void
  // Anything typed in the chat bar.
  onFreeText: (text: string) => void
  onBack: () => void
  onExit: () => void
}

// Full-page setup questions, asked one at a time before the plan is drawn.
// The parent keys this by question id, so each new question starts with an empty input.
export default function IntakePage({ event, answers, prefilled, question, note, info, extracting, onAnswer, onFreeText, onBack, onExit }: Props) {
  const [draft, setDraft] = useState('')
  const meta = EVENT_TYPES.find((e) => e.id === event.type)!
  const tierQuestions = QUESTIONS.filter((q) => q.tier === question.tier)
  const firstOfTier = tierQuestions.find((q) => !prefilled.includes(q.id)) === question
  const canGoBack = QUESTIONS.slice(0, QUESTIONS.indexOf(question)).some((q) => q.id in answers && !prefilled.includes(q.id))
  const required = isRequired(question)
  const answered = Object.keys(answers).length

  const pick = (text: string) => !extracting && onAnswer(text)
  const sendTyped = () => {
    if (!draft.trim() || extracting) return
    onFreeText(draft.trim())
    setDraft('')
  }

  return (
    <div className="glow flex h-full flex-col">
      <nav className="relative flex w-full items-center gap-4 px-6 py-5 sm:px-8">
        <button onClick={onExit} className="flex items-center gap-2 text-lg font-bold tracking-tight">
          <span className="grid size-8 place-items-center rounded-full bg-accent text-white">
            <Sparkles size={15} />
          </span>
          Evently
        </button>
        <button
          onClick={onExit}
          className="flex shrink-0 items-center gap-1 rounded-full px-2.5 py-1 text-sm font-semibold text-muted hover:bg-white hover:text-ink"
        >
          <ArrowLeft size={14} /> Back to main
        </button>
        <span className="pointer-events-none absolute left-1/2 hidden -translate-x-1/2 text-sm font-semibold text-ink md:block">
          {meta.emoji} {meta.label} setup
        </span>
        <span className="ml-auto text-sm font-semibold text-muted tabular-nums">
          {answered} of {QUESTIONS.length} answered
        </span>
      </nav>

      <div className="mx-auto w-full max-w-3xl px-6">
        <div className="flex gap-1.5" role="progressbar" aria-valuemin={0} aria-valuemax={QUESTIONS.length} aria-valuenow={answered}>
          {TIERS.map((t) => {
            const qs = QUESTIONS.filter((q) => q.tier === t.id)
            const done = qs.filter((q) => q.id in answers).length
            return (
              <div key={t.id} style={{ flexGrow: qs.length }} className="min-w-0 basis-0">
                <div className="h-2 overflow-hidden rounded-full bg-line">
                  <div className="h-full rounded-full bg-accent transition-all duration-500" style={{ width: `${(done / qs.length) * 100}%` }} />
                </div>
                <div className={`mt-1.5 truncate text-xs font-semibold ${t.id === question.tier ? 'text-accent' : 'text-muted'}`}>
                  {t.label} <span className="font-normal tabular-nums">{done}/{qs.length}</span>
                </div>
              </div>
            )
          })}
        </div>
      </div>

      <main className="min-h-0 flex-1 overflow-y-auto px-6 pt-12 pb-6">
        <div className="mx-auto w-full max-w-2xl animate-rise">
          {firstOfTier && <p className="mb-3 text-sm font-medium text-gray-600">{TIER_INTRO[question.tier]}</p>}
          <h1 className="mb-8 text-3xl font-extrabold tracking-tight text-gray-900 sm:text-4xl">{question.prompt}</h1>

          <div className="grid gap-3 sm:grid-cols-2">
            {question.options.map((o, i) => (
              <button
                key={o}
                onClick={() => pick(o)}
                disabled={extracting}
                style={{ animationDelay: `${60 + i * 50}ms` }}
                className="animate-rise rounded-2xl bg-white px-5 py-4 text-left text-base font-semibold text-gray-900 shadow-sm ring-1 ring-gray-900/10 transition hover:-translate-y-0.5 hover:text-accent hover:ring-2 hover:ring-accent disabled:pointer-events-none disabled:opacity-50"
              >
                {o}
              </button>
            ))}
          </div>

          {info && <p className="mt-4 rounded-xl bg-accent-soft px-4 py-2.5 text-sm font-medium text-accent">{info}</p>}
          {note && <p className="mt-4 rounded-xl bg-warn-soft px-4 py-2.5 text-sm font-medium text-warn">{note}</p>}

          <div className="mt-8 flex items-center gap-2">
            {canGoBack && (
              <button
                onClick={onBack}
                className="flex items-center gap-1.5 rounded-full px-3 py-2 text-sm font-semibold text-muted hover:bg-white hover:text-ink"
              >
                <ArrowLeft size={14} /> Previous
              </button>
            )}
            {!required && (
              <>
                <button onClick={() => pick(SKIP)} className="ml-auto rounded-full px-4 py-2 text-sm font-semibold text-muted hover:bg-white hover:text-ink">
                  Skip
                </button>
                <button
                  onClick={() => pick(BUILD_NOW)}
                  className="flex items-center gap-1.5 rounded-full bg-gray-900 px-4 py-2 text-sm font-semibold text-white hover:bg-gray-700"
                >
                  {BUILD_NOW} <ArrowRight size={14} />
                </button>
              </>
            )}
          </div>
        </div>
      </main>

      <div className="mx-auto w-full max-w-2xl px-6 pb-6">
        <div className="flex items-end gap-2 rounded-[28px] bg-white p-2 shadow-lg ring-1 shadow-indigo-500/5 ring-gray-900/10 focus-within:ring-2 focus-within:ring-accent">
          <textarea
            autoFocus
            rows={1}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                sendTyped()
              }
            }}
            placeholder={extracting ? 'Reading your answer…' : 'Or describe your event in your own words…'}
            className="max-h-32 min-h-10 flex-1 resize-none bg-transparent px-3 py-2 outline-none placeholder:text-gray-400"
          />
          <button
            onClick={sendTyped}
            disabled={!draft.trim() || extracting}
            aria-label="Send answer"
            className="grid size-9 place-items-center rounded-full bg-gray-900 text-white transition hover:bg-gray-700 disabled:opacity-25"
          >
            {extracting ? <Loader2 size={16} className="animate-spin" /> : <ArrowUp size={16} />}
          </button>
        </div>
      </div>
    </div>
  )
}
