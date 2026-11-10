import { ArrowRight, Check, Copy, MapPin, Star, Users, Wallet } from 'lucide-react'
import { findCategory, daysFromNow } from '../data/categories'
import { copyOf, type SharedPlan } from '../data/sharedPlans'
import { buildWorkflow } from '../data/workflows'
import { agentIcon } from '../lib/icons'
import type { PlanStart } from '../types'
import { Avatars, MiniMap, TopBar } from './CategoryPage'
import { Footer } from './MarketingPage'
import Poster from './Poster'

interface Props {
  plan: SharedPlan
  onPlan: (plan: PlanStart) => void
  onSignIn: () => void
}

// A plan another organizer filled out (#/plan/:id): their event, what they chose for each step,
// and a button to copy it as the user's own plan.
export default function PlanPage({ plan: p, onPlan, onSignIn }: Props) {
  const category = findCategory(p.category)
  const steps = buildWorkflow(p.type, p.skip)
  const filled = steps.filter((s) => p.picks[s.id]).length
  const day = daysFromNow(p.in)
  const use = () => onPlan({ copy: copyOf(p), note: p.title })

  return (
    <div className="h-full overflow-y-auto bg-paper text-ink">
      <TopBar onSignIn={onSignIn} />

      <main className="mx-auto grid max-w-5xl animate-rise grid-cols-[minmax(0,1fr)] gap-10 px-4 py-10 sm:px-6 lg:grid-cols-[320px_1fr] lg:gap-14 lg:py-16">
        <aside className="space-y-6 lg:sticky lg:top-6 lg:self-start">
          <Poster id={p.poster} className="rounded-2xl shadow-xl ring-1 shadow-indigo-500/15 ring-gray-900/5" />
          <div>
            <h3 className="border-b border-gray-900/5 pb-2 text-sm font-semibold text-gray-500">Planned by</h3>
            <p className="mt-3 flex items-center gap-2 font-medium">
              <span className="grid size-7 place-items-center rounded-full bg-accent-soft text-xs font-semibold text-accent">{p.host[0]}</span>
              {p.host}
            </p>
          </div>
          <div>
            <h3 className="border-b border-gray-900/5 pb-2 text-sm font-semibold text-gray-500">{p.going} Going</h3>
            <div className="mt-3">
              <Avatars going={p.going} />
            </div>
          </div>
          {category && (
            <a
              href={`#/category/${category.id}`}
              className="inline-flex items-center gap-2 rounded-full bg-white px-3 py-1.5 text-sm font-medium text-gray-700 ring-1 ring-gray-900/10 transition hover:text-accent hover:ring-accent"
            >
              <category.icon size={15} style={{ color: category.color }} /> More {category.noun} plans
            </a>
          )}
        </aside>

        <div>
          <p className="text-sm font-semibold tracking-wide text-accent uppercase">Community plan</p>
          <h1 className="mt-2 text-4xl font-bold tracking-tight sm:text-5xl">{p.title}</h1>

          <div className="mt-6 space-y-4">
            <div className="flex items-center gap-4">
              <span className="w-12 shrink-0 overflow-hidden rounded-xl text-center ring-1 ring-gray-900/10">
                <span className="block bg-gray-50 text-[10px] font-semibold text-gray-500 uppercase">
                  {day.toLocaleDateString('en-US', { month: 'short' })}
                </span>
                <span className="block py-0.5 text-lg font-semibold">{day.getDate()}</span>
              </span>
              <div>
                <p className="font-semibold">{day.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' })}</p>
                <p className="text-gray-500">{p.time}</p>
              </div>
            </div>
            <div className="flex items-center gap-4">
              <span className="grid size-12 shrink-0 place-items-center rounded-xl ring-1 ring-gray-900/10">
                <MapPin size={20} className="text-gray-500" />
              </span>
              <div>
                <p className="font-semibold">{p.place}</p>
                <p className="text-gray-500">{p.city}</p>
              </div>
            </div>
          </div>

          <div className="mt-8 rounded-2xl bg-[linear-gradient(145deg,#eef2ff,#ffffff_60%,#fdf2f8)] p-5 ring-1 ring-gray-900/5 sm:p-6">
            <div className="flex flex-wrap gap-x-6 gap-y-2 text-gray-600">
              <span className="flex items-center gap-1.5">
                <Users size={17} className="text-gray-400" /> {p.headcount.toLocaleString()} people
              </span>
              <span className="flex items-center gap-1.5">
                <Wallet size={17} className="text-gray-400" /> {p.budget} budget
              </span>
              <span className="flex items-center gap-1.5">
                <Check size={17} className="text-accent" /> {filled} of {steps.length} steps filled in
              </span>
            </div>
            <p className="mt-4 text-gray-600">
              Copy this plan to make it yours. Their choices stay in place; agents research the {steps.length - filled} open
              steps once you approve. Change the date, city or headcount in chat first.
            </p>
            <button
              onClick={use}
              className="group mt-5 inline-flex w-full items-center justify-center gap-2 rounded-full bg-accent px-7 py-3.5 text-lg font-semibold text-white shadow-lg shadow-indigo-500/30 transition hover:bg-indigo-500 sm:w-auto"
            >
              <Copy size={18} /> Use This Plan <ArrowRight size={18} className="transition group-hover:translate-x-0.5" />
            </button>
          </div>

          <h2 className="mt-12 border-b border-gray-900/5 pb-2 text-sm font-semibold text-gray-500">About</h2>
          <p className="mt-4 text-lg leading-relaxed text-gray-700">{p.blurb}</p>

          <h2 className="mt-12 border-b border-gray-900/5 pb-2 text-sm font-semibold text-gray-500">The Plan</h2>
          <ol className="mt-4 space-y-3">
            {steps.map((s, i) => {
              const r = p.picks[s.id]
              const Icon = agentIcon(s.agent)
              return (
                <li
                  key={s.id}
                  className={`flex gap-4 rounded-2xl p-4 ring-1 sm:p-5 ${r ? 'bg-white shadow-sm ring-gray-900/5 shadow-indigo-500/5' : 'border border-dashed border-gray-200 ring-transparent'}`}
                >
                  <span className={`grid size-10 shrink-0 place-items-center rounded-xl ${r ? 'bg-accent-soft text-accent' : 'bg-gray-50 text-gray-400'}`}>
                    <Icon size={18} />
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm text-gray-400">
                      <span className="font-mono">{i + 1}</span> · {s.title} · {s.agent}
                    </p>
                    {r ? (
                      <>
                        <p className="mt-0.5 font-semibold">{r.title}</p>
                        <p className="text-gray-500">{r.subtitle}</p>
                        {(r.price || r.rating || r.capacity) && (
                          <div className="mt-2 flex flex-wrap gap-1.5 text-sm">
                            {r.rating && (
                              <span className="inline-flex items-center gap-1 rounded-full bg-gray-50 px-2 py-0.5 text-gray-600 ring-1 ring-gray-900/5">
                                <Star size={12} className="fill-amber-400 text-amber-400" /> {r.rating}
                              </span>
                            )}
                            {r.price && <span className="rounded-full bg-gray-50 px-2 py-0.5 text-gray-600 ring-1 ring-gray-900/5">{r.price}</span>}
                            {r.capacity && <span className="rounded-full bg-gray-50 px-2 py-0.5 text-gray-600 ring-1 ring-gray-900/5">{r.capacity}</span>}
                          </div>
                        )}
                        {r.why && <p className="mt-2 text-sm text-gray-500">{r.why}</p>}
                      </>
                    ) : (
                      <p className="mt-0.5 text-gray-500">Open. {s.description}</p>
                    )}
                  </div>
                  {r && (
                    <span className="grid size-6 shrink-0 place-items-center rounded-full bg-accent text-white">
                      <Check size={13} strokeWidth={3} />
                    </span>
                  )}
                </li>
              )
            })}
          </ol>

          <h2 className="mt-12 border-b border-gray-900/5 pb-2 text-sm font-semibold text-gray-500">Location</h2>
          <p className="mt-4 font-semibold">{p.place}</p>
          <p className="mb-4 text-gray-500">{p.city}</p>
          <div className="max-w-md">
            <MiniMap city={p.city} color={category?.color ?? '#4f46e5'} />
          </div>
        </div>
      </main>
      <Footer />
    </div>
  )
}
