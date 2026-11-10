import { ArrowRight, CalendarDays, Check, LayoutTemplate, MapPin, Search, X } from 'lucide-react'
import { useEffect, useState, type MouseEvent } from 'react'
import { daysFromNow, type Category, type Template } from '../data/categories'
import { plansIn, type SharedPlan } from '../data/sharedPlans'
import { buildWorkflow } from '../data/workflows'
import { agentIcon } from '../lib/icons'
import type { PlanStart } from '../types'
import { Footer, Logo } from './MarketingPage'
import Poster from './Poster'

interface Props {
  category: Category
  onPlan: (plan: PlanStart) => void
  onSignIn: () => void
}

const fromTemplate = (t: Template): PlanStart => ({ type: t.plan, note: t.note, skip: t.skip })

// A category from the landing page's "Browse by category": blank templates for this kind of event,
// and plans other organizers filled out (each opens #/plan/:id).
export default function CategoryPage({ category, onPlan, onSignIn }: Props) {
  const c = category
  const Icon = c.icon
  const [featured] = c.templates
  const [query, setQuery] = useState<string | null>(null)
  const community = plansIn(c.id)
  const plans = community.filter((p) => !query || `${p.title} ${p.host} ${p.place} ${p.city}`.toLowerCase().includes(query.toLowerCase()))
  // Group by day, soonest first.
  const days = [...new Set(plans.map((p) => p.in))].sort((a, b) => a - b).map((n) => ({ n, plans: plans.filter((p) => p.in === n) }))

  return (
    <div className="h-full overflow-y-auto bg-paper text-ink">
      <TopBar onSignIn={onSignIn} />

      <main className="mx-auto max-w-6xl px-4 sm:px-6">
        <section className="grid animate-rise grid-cols-[minmax(0,1fr)] gap-12 py-12 lg:grid-cols-[1fr_420px] lg:items-center lg:gap-20 lg:py-24">
          <div>
            <h1 className="text-5xl font-bold tracking-tight sm:text-6xl">{c.label}</h1>
            <div className="mt-6 flex flex-wrap gap-6 border-b border-gray-900/5 pb-6 text-lg text-gray-500">
              <span className="flex items-center gap-2">
                <LayoutTemplate size={20} className="text-gray-400" />
                <b className="font-semibold text-ink">{c.templates.length}</b> Templates
              </span>
              <span className="flex items-center gap-2">
                <CalendarDays size={20} className="text-gray-400" />
                <b className="font-semibold text-ink">{c.plans}</b> Plans Made
              </span>
            </div>
            <p className="mt-6 max-w-lg text-xl leading-relaxed text-gray-600">{c.blurb}</p>
            <button
              onClick={() => onPlan(fromTemplate(featured))}
              className="group mt-10 inline-flex items-center gap-2 rounded-full bg-accent px-7 py-3.5 text-lg font-semibold text-white shadow-lg shadow-indigo-500/30 transition hover:bg-indigo-500"
            >
              Use This Template to Plan <ArrowRight size={18} className="transition group-hover:translate-x-0.5" />
            </button>
            <p className="mt-3 text-gray-500">
              Starts from <b className="font-medium text-gray-700">{featured.name}</b>
              {c.templates.length > 1 && (
                <>
                  {' '}·{' '}
                  <a href="#templates" onClick={jumpTo('templates')} className="font-medium text-accent hover:underline">
                    see all {c.templates.length}
                  </a>
                </>
              )}
            </p>
          </div>
          <Cover category={c} />
        </section>

        <section className="grid grid-cols-[minmax(0,1fr)] gap-12 border-t border-gray-900/5 py-12 lg:grid-cols-[1fr_300px] lg:gap-20">
          <div className="space-y-16">
            <div id="templates" className="scroll-mt-6">
              <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">Templates</h2>
              <p className="mt-2 text-gray-500">Blank workflows for this kind of event. Answer a few questions and agents take each step.</p>
              <div className="mt-6 space-y-4">
                {c.templates.map((t) => (
                  <TemplateCard key={t.id} template={t} onUse={() => onPlan(fromTemplate(t))} />
                ))}
              </div>
            </div>

            {community.length > 0 && (
              <div>
                <div className="mb-2 flex items-center gap-4">
                  <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">Plans from the Community</h2>
                  {query !== null && (
                    <input
                      autoFocus
                      value={query}
                      onChange={(e) => setQuery(e.target.value)}
                      placeholder="Search plans"
                      className="ml-auto min-w-0 flex-1 rounded-xl bg-gray-50 px-4 py-2 outline-none ring-1 ring-gray-900/10 placeholder:text-gray-400 focus:ring-2 focus:ring-accent sm:max-w-xs"
                    />
                  )}
                  <button
                    onClick={() => setQuery((q) => (q === null ? '' : null))}
                    aria-label={query === null ? 'Search plans' : 'Close search'}
                    className={`grid size-11 shrink-0 place-items-center rounded-xl bg-gray-50 text-gray-500 ring-1 ring-gray-900/5 transition hover:text-accent ${query === null ? 'ml-auto' : ''}`}
                  >
                    {query === null ? <Search size={20} /> : <X size={20} />}
                  </button>
                </div>
                <p className="mb-6 text-gray-500">Real choices other organizers made. Copy one and change what you need.</p>

                {days.length === 0 && <p className="py-10 text-muted">No plans match “{query}”.</p>}
                {days.map(({ n, plans }) => (
                  <div key={n} className="relative pb-6 pl-10">
                    <span className="absolute top-2 left-0 size-2.5 rounded-full bg-accent/40" />
                    <span className="absolute top-6 bottom-0 left-[4.5px] border-l border-dashed border-gray-300" />
                    <h3 className="mb-4 text-lg">
                      <b className="font-semibold">{daysFromNow(n).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}</b>{' '}
                      <span className="text-muted">{daysFromNow(n).toLocaleDateString('en-US', { weekday: 'long' })}</span>
                    </h3>
                    <div className="space-y-4">
                      {plans.map((p) => (
                        <PlanCard key={p.id} plan={p} />
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          <aside className="lg:sticky lg:top-6 lg:self-start">
            <span className="grid size-16 place-items-center rounded-full text-white" style={{ background: c.color }}>
              <Icon size={30} />
            </span>
            <h2 className="mt-5 text-2xl font-semibold">{c.label}</h2>
            <p className="mt-2 text-lg leading-relaxed text-gray-500">
              Subscribe to stay up-to-date with new {c.noun} templates, agents and sample plans.
            </p>
            <Subscribe stacked className="mt-6" />
          </aside>
        </section>
      </main>
      <Footer />
    </div>
  )
}

// Scrolls within the page without changing the hash, which would leave this route.
const jumpTo = (id: string) => (e: MouseEvent) => {
  e.preventDefault()
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth' })
}

export function TopBar({ onSignIn }: { onSignIn: () => void }) {
  return (
    <header className="flex items-center gap-6 p-4 sm:px-6">
      <a href="#" aria-label="Evently home" className="transition hover:opacity-80">
        <Logo />
      </a>
      <div className="ml-auto flex items-center gap-4 text-gray-500 sm:gap-6">
        <Clock />
        <a href="#categories" className="hidden font-medium transition hover:text-accent sm:inline">
          Browse Templates
        </a>
        <button onClick={onSignIn} className="rounded-full bg-white px-4 py-2 font-semibold text-gray-700 ring-1 ring-gray-900/10 transition hover:text-accent hover:ring-accent">
          Sign In
        </button>
      </div>
    </header>
  )
}

// Live local time, like "3:54 PM PST".
function Clock() {
  const fmt = () => new Date().toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZoneName: 'short' })
  const [now, setNow] = useState(fmt)
  useEffect(() => {
    const t = setInterval(() => setNow(fmt()), 15_000)
    return () => clearInterval(t)
  }, [])
  return <span className="hidden tabular-nums md:inline">{now}</span>
}

// Email sign-up. There's no mailing list yet, so it only confirms in place.
function Subscribe({ stacked = false, className = '' }: { stacked?: boolean; className?: string }) {
  const [done, setDone] = useState(false)
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        setDone(true)
      }}
      className={`flex gap-3 ${stacked ? 'flex-col' : ''} ${className}`}
    >
      <input
        type="email"
        required
        disabled={done}
        placeholder="me@email.com"
        className="min-w-0 flex-1 rounded-2xl bg-gray-50 px-5 py-3.5 text-lg outline-none ring-1 ring-gray-900/10 placeholder:text-gray-400 focus:ring-2 focus:ring-accent disabled:opacity-50"
      />
      <button
        disabled={done}
        className="inline-flex items-center justify-center gap-1.5 rounded-full bg-accent px-6 py-3.5 text-lg font-semibold text-white shadow-sm shadow-indigo-500/30 transition hover:bg-indigo-500 disabled:bg-indigo-400"
      >
        {done ? (
          <>
            <Check size={18} /> Subscribed
          </>
        ) : (
          'Subscribe'
        )}
      </button>
    </form>
  )
}

function Cover({ category: c }: { category: Category }) {
  const Icon = c.icon
  return (
    <div className="relative mx-auto aspect-square w-full max-w-[420px] rounded-3xl bg-[linear-gradient(145deg,#eef2ff,#ffffff_55%,#fdf2f8)] p-6 shadow-xl shadow-indigo-500/10 ring-1 ring-gray-900/5">
      <span className="grid size-14 place-items-center rounded-full" style={{ background: `${c.color}33`, color: c.color }}>
        <Icon size={28} />
      </span>
      {/* Tag words running down the right edge. */}
      <div className="absolute top-6 right-6 hidden gap-1 font-mono sm:flex text-xl leading-none tracking-wider text-indigo-900/10 [writing-mode:vertical-rl]">
        {c.tags.map((t) => (
          <span key={t}>{t}</span>
        ))}
      </div>
      <div className="absolute top-1/2 left-1/2 w-[64%] -translate-x-1/2 -translate-y-[54%] overflow-hidden rounded-full shadow-2xl shadow-indigo-500/30">
        <Poster id={c.poster} />
      </div>
      <div className="absolute inset-x-6 bottom-5 flex justify-between font-mono text-sm tracking-wider text-gray-400">
        <span>DISCOVER</span>
        <span>
          <span style={{ color: c.color }}>{c.label.toUpperCase()}</span> TEMPLATES
        </span>
      </div>
    </div>
  )
}

function TemplateCard({ template: t, onUse }: { template: Template; onUse: () => void }) {
  const steps = buildWorkflow(t.plan, t.skip)
  return (
    <article className="rounded-2xl bg-white p-5 shadow-sm ring-1 shadow-indigo-500/5 ring-gray-900/5 sm:p-6">
      <div className="flex flex-wrap items-start gap-4">
        <div className="min-w-0 flex-1">
          <h3 className="text-xl font-semibold sm:text-2xl">{t.name}</h3>
          <p className="mt-1.5 text-gray-500">{t.blurb}</p>
        </div>
        <button
          onClick={onUse}
          className="group inline-flex shrink-0 items-center gap-1.5 rounded-full bg-accent px-5 py-2.5 font-semibold text-white shadow-sm shadow-indigo-500/30 transition hover:bg-indigo-500"
        >
          Use Template <ArrowRight size={16} className="transition group-hover:translate-x-0.5" />
        </button>
      </div>
      <div className="mt-4 flex flex-wrap gap-1.5">
        {t.tags.map((tag) => (
          <span key={tag} className="rounded-full bg-accent-soft px-2.5 py-0.5 text-sm font-medium text-accent">
            {tag}
          </span>
        ))}
      </div>
      <ol className="mt-5 flex flex-wrap gap-2 border-t border-gray-900/5 pt-5">
        {steps.map((s, i) => {
          const AgentIcon = agentIcon(s.agent)
          return (
            <li key={s.id} className="flex items-center gap-1.5 rounded-xl bg-gray-50 px-2.5 py-1.5 text-sm text-gray-700 ring-1 ring-gray-900/5">
              <span className="font-mono text-xs text-gray-400">{i + 1}</span>
              <AgentIcon size={14} className="text-accent" />
              {s.title}
            </li>
          )
        })}
      </ol>
    </article>
  )
}

const AVATAR = ['#c4b5fd', '#a78bfa', '#f472b6', '#e5e7eb']

export function Avatars({ going }: { going: number }) {
  return (
    <div className="flex items-center gap-2">
      <span className="flex -space-x-2">
        {AVATAR.map((bg) => (
          <span key={bg} className="size-6 rounded-full ring-2 ring-white" style={{ background: bg }} />
        ))}
      </span>
      <span className="text-sm text-muted">+{going} going</span>
    </div>
  )
}

function PlanCard({ plan: p }: { plan: SharedPlan }) {
  const filled = Object.keys(p.picks).length
  return (
    <a
      href={`#/plan/${p.id}`}
      className="group flex gap-4 rounded-2xl bg-white p-5 shadow-sm ring-1 shadow-indigo-500/5 ring-gray-900/5 transition hover:shadow-lg hover:ring-accent/30 sm:p-6"
    >
      <div className="min-w-0 flex-1">
        <p className="text-lg text-muted">{p.time}</p>
        <h4 className="mt-1 text-xl leading-snug font-semibold transition group-hover:text-accent sm:text-2xl">{p.title}</h4>
        <p className="mt-3 flex items-center gap-2 text-gray-500">
          <span className="grid size-5 place-items-center rounded-full bg-accent-soft text-[10px] font-semibold text-accent">{p.host[0]}</span>
          By {p.host}
        </p>
        <p className="mt-1.5 flex items-center gap-2 text-gray-500">
          <MapPin size={18} className="shrink-0 text-gray-400" />
          <span className="truncate">
            {p.place}, {p.city}
          </span>
        </p>
        <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-2">
          <Avatars going={p.going} />
          <span className="rounded-full bg-accent-soft px-2.5 py-0.5 text-sm font-medium text-accent">{filled} steps filled in</span>
        </div>
      </div>
      <Poster id={p.poster} className="size-24 shrink-0 rounded-xl sm:size-36" />
    </a>
  )
}

// A stylized street map with a pin at the venue. Not a real map; it sets the scene.
export function MiniMap({ city, color }: { city: string; color: string }) {
  return (
    <div className="relative aspect-square overflow-hidden rounded-2xl bg-[#eef2ff] ring-1 ring-gray-900/5">
      <svg viewBox="0 0 100 100" className="absolute inset-0 size-full" aria-hidden>
        <g stroke="#ffffff" strokeWidth="0.8">
          {Array.from({ length: 12 }, (_, i) => (
            <line key={`h${i}`} x1="0" x2="100" y1={i * 9 + 3} y2={i * 9 + 1} />
          ))}
          {Array.from({ length: 12 }, (_, i) => (
            <line key={`v${i}`} y1="0" y2="100" x1={i * 9 + 2} x2={i * 9 + 5} />
          ))}
        </g>
        <path d="M0 20 L18 18 L22 34 L8 40 L0 38 Z" fill="#d9f2e6" />
        <path d="M62 60 L80 56 L86 74 L70 80 Z" fill="#d9f2e6" />
        <path d="M40 8 L52 6 L54 18 L42 20 Z" fill="#d9f2e6" />
        <path d="M70 0 C 60 20 82 30 68 48 S 50 70 60 100" fill="none" stroke="#bae6fd" strokeWidth="2.5" />
        <path d="M0 62 L100 58" stroke="#ffffff" strokeWidth="1.8" />
        <path d="M28 0 L34 100" stroke="#ffffff" strokeWidth="1.8" />
        <circle cx="48" cy="44" r="6" fill={color} opacity="0.2" />
        <circle cx="48" cy="44" r="2.2" fill={color} stroke="#fff" strokeWidth="0.8" />
      </svg>
      <span className="absolute bottom-4 left-4 text-3xl leading-none font-semibold text-indigo-900/25">{city}</span>
    </div>
  )
}
