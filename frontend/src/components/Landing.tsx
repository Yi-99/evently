import { ArrowRight, ArrowUp, Clock, Compass, LogOut, Plus, Puzzle, Sparkles, Wrench, type LucideIcon } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { serverTime } from '../features/chat/messages'
import { useEventList } from '../features/events/api'
import { AGENTS, EVENT_TYPES, INTEGRATION_LABEL } from '../data/workflows'
import { INTEGRATION_ICON, INTEGRATION_LOGO, agentIcon } from '../lib/icons'
import type { User } from '../lib/auth'
import { loadRecent, type RecentEvent } from '../lib/recent'
import type { EventType, Integration } from '../types'

type View = 'new' | 'tools' | 'plugins' | 'explore'

const NAV: { id: View; label: string; icon: LucideIcon }[] = [
  { id: 'new', label: 'New Event', icon: Plus },
  { id: 'tools', label: 'Tools', icon: Wrench },
  { id: 'plugins', label: 'Plug ins', icon: Puzzle },
  { id: 'explore', label: 'Explore', icon: Compass },
]

// Sample plans to start from. Placeholder content until shared plans come from a backend.
const EXAMPLES: { type: EventType; title: string; note: string }[] = [
  { type: 'hackathon', title: 'Student hackathon', note: '48-hour student hackathon for 250 people in Boston' },
  { type: 'business', title: 'Team offsite', note: 'Two-day business offsite for 40 people in Napa' },
  { type: 'conference', title: 'Developer conference', note: 'Developer conference for 600 attendees in Seattle' },
  { type: 'wedding', title: 'Garden wedding', note: 'Outdoor wedding for 120 guests in Austin' },
  { type: 'reunion', title: '10-year reunion', note: 'High school 10-year reunion for 80 people in Chicago' },
]

const ago = (t: number) => {
  const m = Math.round((Date.now() - t) / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  if (m < 1440) return `${Math.round(m / 60)}h ago`
  return `${Math.round(m / 1440)}d ago`
}

interface Props {
  /** The backend is reachable (and we're signed in): list saved events instead of this browser's history. */
  online?: boolean
  onPick: (type: EventType, note?: string) => void
  onResume: (event: RecentEvent) => void
  onHome: () => void
  user: User | null
  onSignOut: () => void
}

const eventTypeOf = (req: Record<string, unknown>): EventType | undefined =>
  EVENT_TYPES.find((e) => e.id === req.event_type)?.id

export default function Landing({ online = false, onPick, onResume, onHome, user, onSignOut }: Props) {
  const [draft, setDraft] = useState('')
  const [view, setView] = useState<View>('new')
  const [recent] = useState(loadRecent)
  const saved = useEventList(online)
  // Online, a plan saved as a backend event is listed once, as that event (the server copy is the real one).
  const localOnly = online ? recent.filter((r) => !r.eventId) : recent
  const emoji = (t: EventType) => EVENT_TYPES.find((e) => e.id === t)!.emoji
  const label = (t: EventType) => EVENT_TYPES.find((e) => e.id === t)!.label
  const rowClass =
    'flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left text-sm text-gray-700 hover:bg-gray-100 hover:text-ink'

  const submit = () => {
    const lower = draft.toLowerCase()
    const match = EVENT_TYPES.find((e) => lower.includes(e.id) || lower.includes(e.label.toLowerCase()))
    if (match) onPick(match.id, draft)
  }

  return (
    <div className="flex h-full">
      <aside className="hidden w-64 shrink-0 flex-col border-r border-line bg-white/70 px-3 py-5 backdrop-blur md:flex">
        <button onClick={onHome} className="mb-6 flex items-center gap-2 px-2 text-lg font-bold tracking-tight">
          <span className="grid size-8 place-items-center rounded-full bg-accent text-white">
            <Sparkles size={15} />
          </span>
          Evently
        </button>
        <nav className="space-y-0.5">
          {NAV.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              onClick={() => setView(id)}
              aria-current={view === id ? 'page' : undefined}
              className={`flex w-full items-center gap-2.5 rounded-xl px-3 py-2 text-sm font-semibold transition ${
                view === id ? 'bg-accent-soft text-accent' : 'text-gray-700 hover:bg-gray-100 hover:text-ink'
              }`}
            >
              <Icon size={16} />
              {label}
            </button>
          ))}
        </nav>

        <div className="mt-6 flex min-h-0 flex-1 flex-col">
          <div className="mb-1.5 flex items-center gap-1.5 px-3 text-xs font-semibold tracking-wide text-muted uppercase">
            <Clock size={12} /> Recent Events
          </div>
          <div className="min-h-0 flex-1 space-y-0.5 overflow-y-auto">
            {/* Online: the account's saved events first, then plans only this browser has (e.g. made offline). */}
            {online && saved === null && <p className="px-3 py-2 text-sm text-muted">Loading…</p>}
            {online &&
              saved?.map((ev) => {
                const type = eventTypeOf(ev.requirements)
                return (
                  <Link key={ev.id} to={`/events/${encodeURIComponent(ev.id)}`} title={ev.description || ev.name} className={rowClass}>
                    <span>{type ? emoji(type) : '📅'}</span>
                    <span className="min-w-0 flex-1 truncate">{ev.name}</span>
                    <span className="shrink-0 text-[11px] text-muted">{ago(serverTime(ev.updated_at))}</span>
                  </Link>
                )
              })}
            {localOnly.map((r) => (
              <button key={r.id} onClick={() => onResume(r)} title={r.note ?? label(r.type)} className={rowClass}>
                <span>{emoji(r.type)}</span>
                <span className="min-w-0 flex-1 truncate">{r.note ?? label(r.type)}</span>
                <span className="shrink-0 text-[11px] text-muted">{ago(r.at)}</span>
              </button>
            ))}
            {!(online && saved === null) && !saved?.length && !localOnly.length && (
              <p className="px-3 py-2 text-sm text-muted">Events you start show up here.</p>
            )}
          </div>
        </div>

        {user && (
          <div className="mt-3 flex items-center gap-2.5 border-t border-line px-2 pt-3">
            <span className="grid size-8 shrink-0 place-items-center rounded-full bg-accent-soft text-xs font-bold text-accent">
              {user.name
                .split(' ')
                .map((w) => w[0])
                .slice(0, 2)
                .join('')
                .toUpperCase()}
            </span>
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-semibold">{user.name}</div>
              <div className="truncate text-xs text-muted">{user.email}</div>
            </div>
            <button onClick={onSignOut} title="Sign out" aria-label="Sign out" className="rounded-lg p-1.5 text-muted hover:bg-gray-100 hover:text-ink">
              <LogOut size={15} />
            </button>
          </div>
        )}
      </aside>

      {/* .glow clips its blurred backdrop with overflow: hidden, so scrolling lives on the inner div. */}
      <div className="glow flex min-w-0 flex-1 flex-col">
        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
          <nav className="flex w-full items-center gap-4 px-6 py-5 sm:px-8">
            <button onClick={onHome} className="flex items-center gap-2 text-lg font-bold tracking-tight md:hidden">
              <span className="grid size-8 place-items-center rounded-full bg-accent text-white">
                <Sparkles size={15} />
              </span>
              Evently
            </button>
            <span className="hidden items-center gap-1.5 rounded-full bg-white/70 px-3 py-1 text-xs text-gray-600 ring-1 ring-gray-900/10 sm:flex">
              <Sparkles size={12} className="text-accent" />
              <b className="font-semibold text-ink">New</b> Agents that plan with you
              <ArrowRight size={12} />
            </span>
          </nav>

          {view === 'new' && (
            <div className="flex flex-1 flex-col items-center justify-center px-4 pb-20">
              <div className="w-full max-w-2xl animate-rise">
                <p className="mb-4 text-center text-sm font-semibold text-accent">AI event planner</p>
                <h1 className="mb-5 text-center text-5xl font-extrabold tracking-tight text-gray-900 sm:text-6xl">
                  What event are you planning?
                </h1>
                <p className="mx-auto mb-10 max-w-lg text-center text-lg text-gray-600">
                  Pick an event and Evently builds the workflow, then sends agents to handle venues, catering, sponsors and more.
                </p>

                <div className="rounded-[32px] bg-white p-2 shadow-lg ring-1 shadow-indigo-500/5 ring-gray-900/10">
                  <div className="flex items-end gap-2">
                    <textarea
                      rows={1}
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' && !e.shiftKey) {
                          e.preventDefault()
                          submit()
                        }
                      }}
                      placeholder="A 200-person hackathon in San Francisco…"
                      className="max-h-40 min-h-11 flex-1 resize-none bg-transparent px-3 py-2.5 outline-none placeholder:text-gray-400"
                    />
                    <button
                      onClick={submit}
                      disabled={!draft.trim()}
                      aria-label="Send"
                      className="grid size-10 place-items-center rounded-full bg-gray-900 text-white transition hover:bg-gray-700 disabled:opacity-25"
                    >
                      <ArrowUp size={18} />
                    </button>
                  </div>
                </div>

                <div className="mt-6 flex flex-wrap justify-center gap-2">
                  {EVENT_TYPES.map((e, i) => (
                    <button
                      key={e.id}
                      onClick={() => onPick(e.id)}
                      style={{ animationDelay: `${120 + i * 60}ms` }}
                      className="animate-rise rounded-full bg-white/80 px-4 py-2 text-sm font-semibold text-gray-900 shadow-sm ring-1 ring-gray-900/10 transition hover:-translate-y-0.5 hover:text-accent hover:ring-accent"
                    >
                      <span className="mr-1.5">{e.emoji}</span>
                      {e.label}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          )}

          {view === 'tools' && (
            <Section title="Tools" blurb="The agents Evently sends out once you approve a plan.">
              {AGENTS.map((a) => (
                <Card key={a.name} icon={agentIcon(a.name)} title={a.name} meta={INTEGRATION_LABEL[a.integration]} body={a.example} />
              ))}
            </Section>
          )}

          {view === 'plugins' && (
            <Section title="Plug ins" blurb="Services agents can work through. Connect Google from the plan view.">
              {(Object.keys(INTEGRATION_LABEL) as Integration[]).map((i) => (
                <Card
                  key={i}
                  icon={INTEGRATION_ICON[i]}
                  logo={INTEGRATION_LOGO[i]}
                  title={INTEGRATION_LABEL[i]}
                  meta={i === 'web' ? 'Always on' : i === 'live-slides' ? 'Built in' : i === 'linkedin' ? 'No sign-in needed yet' : i === 'luma' ? 'Publishes only after you confirm' : 'Needs Google'}
                  body={`Used by ${AGENTS.filter((a) => a.integration === i).map((a) => a.name).join(', ') || 'no agents yet'}.`}
                />
              ))}
            </Section>
          )}

          {view === 'explore' && (
            <Section title="Explore" blurb="Start from an event plan someone else has run.">
              {EXAMPLES.map((x) => (
                <button key={x.note} onClick={() => onPick(x.type, x.note)} className="text-left">
                  <Card title={`${emoji(x.type)} ${x.title}`} meta={label(x.type)} body={x.note} action="Use this plan" />
                </button>
              ))}
            </Section>
          )}
        </div>
      </div>
    </div>
  )
}

function Section({ title, blurb, children }: { title: string; blurb: string; children: ReactNode }) {
  return (
    <div className="mx-auto w-full max-w-4xl animate-rise px-6 pb-16 sm:px-8">
      <h1 className="text-3xl font-extrabold tracking-tight text-gray-900">{title}</h1>
      <p className="mt-2 mb-8 text-gray-600">{blurb}</p>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{children}</div>
    </div>
  )
}

interface CardProps {
  icon?: LucideIcon
  // Brand logo image; shown instead of `icon` when given.
  logo?: string
  title: string
  meta: string
  body: string
  action?: string
}

function Card({ icon: Icon, logo, title, meta, body, action }: CardProps) {
  return (
    <div className="h-full rounded-2xl bg-white p-4 shadow-sm ring-1 ring-gray-900/10 transition hover:ring-accent">
      {logo ? (
        <span className="mb-3 grid size-9 place-items-center rounded-xl bg-white ring-1 ring-gray-900/10">
          <img src={logo} alt="" className="size-6 object-contain" />
        </span>
      ) : Icon && (
        <span className="mb-3 grid size-9 place-items-center rounded-xl bg-accent-soft text-accent">
          <Icon size={18} />
        </span>
      )}
      <div className="text-sm font-bold text-gray-900">{title}</div>
      <div className="mt-0.5 text-xs font-semibold text-accent">{meta}</div>
      <p className="mt-2 text-sm text-gray-600">{body}</p>
      {action && (
        <div className="mt-3 flex items-center gap-1 text-xs font-semibold text-accent">
          {action} <ArrowRight size={12} />
        </div>
      )}
    </div>
  )
}
