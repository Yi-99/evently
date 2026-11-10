import { ArrowRight, Mail, Sparkle, Sparkles } from 'lucide-react'
import { useEffect, useState, type CSSProperties, type MouseEvent, type ReactNode } from 'react'
import { CATEGORIES, findCategory } from '../data/categories'
import { SHARED_PLANS, findSharedPlan, PLAN_ICON, when, type SharedPlan } from '../data/sharedPlans'
import { AGENTS, INTEGRATION_LABEL } from '../data/workflows'
import { INTEGRATION_LOGO, agentIcon } from '../lib/icons'
import type { Integration, PlanStart } from '../types'
import CategoryPage from './CategoryPage'
import PlanPage from './PlanPage'
import Poster, { type PosterId } from './Poster'

const YEAR = new Date().getFullYear()

const REPO = 'https://github.com/KdotJo/Evently'

interface Props {
  // Opens the sign-up form.
  onStart: () => void
  // Opens the sign-in form.
  onSignIn: () => void
  // Signs in and starts a plan from a template or from someone's shared plan.
  onPlan: (plan: PlanStart) => void
}

// The landing page, a category page (#/category/:id) or a shared plan (#/plan/:id).
export default function MarketingPage({ onStart, onSignIn, onPlan }: Props) {
  const [hash, setHash] = useState(location.hash)
  useEffect(() => {
    const onChange = () => setHash(location.hash)
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  // In-page anchors (#categories) point into the landing page, which may only just have mounted.
  useEffect(() => {
    if (/^#\w/.test(hash)) document.getElementById(hash.slice(1))?.scrollIntoView({ behavior: 'smooth' })
  }, [hash])

  const category = findCategory(hash.match(/^#\/category\/([\w-]+)$/)?.[1] ?? '')
  if (category) return <CategoryPage key={category.id} category={category} onPlan={onPlan} onSignIn={onSignIn} />
  const shared = findSharedPlan(hash.match(/^#\/plan\/([\w-]+)$/)?.[1] ?? '')
  if (shared) return <PlanPage key={shared.id} plan={shared} onPlan={onPlan} onSignIn={onSignIn} />

  return (
    <div className="h-full overflow-y-auto bg-paper text-ink motion-safe:scroll-smooth">
      <Hero onStart={onStart} onSignIn={onSignIn} />
      <div className="mx-auto max-w-6xl space-y-24 px-4 py-20 sm:px-6">
        <Popular />
        <Tools />
        <Agents />
        <Categories />
      </div>
      <FinalCta onStart={onStart} />
      <Footer />
    </div>
  )
}

/* ---------- shared bits ---------- */

export function Logo() {
  return (
    <div className="flex items-center gap-2 text-lg font-bold tracking-tight">
      <span className="grid size-8 place-items-center rounded-full bg-accent text-white">
        <Sparkles size={15} />
      </span>
      Evently
    </div>
  )
}

// Indigo → violet → pink, the ribbon's colors, as on the hero and closing headlines.
export function Glow({ children }: { children: ReactNode }) {
  return (
    <span className="bg-[linear-gradient(90deg,#4f46e5,#7c3aed_40%,#c026d3_70%,#ec4899)] bg-clip-text text-transparent">{children}</span>
  )
}

export function SectionTitle({ children }: { children: ReactNode }) {
  return <h2 className="mb-6 text-2xl font-semibold tracking-tight sm:text-[28px]">{children}</h2>
}

export function GoogleG() {
  return (
    <svg viewBox="0 0 24 24" className="size-4" aria-hidden>
      <path fill="#4285F4" d="M23.5 12.3c0-.8-.1-1.6-.2-2.3H12v4.5h6.5a5.6 5.6 0 0 1-2.4 3.6v3h3.9c2.3-2.1 3.5-5.2 3.5-8.8z" />
      <path fill="#34A853" d="M12 24c3.2 0 6-1.1 7.9-2.9l-3.9-3c-1 .7-2.4 1.2-4 1.2-3.1 0-5.7-2.1-6.6-4.9h-4v3.1A12 12 0 0 0 12 24z" />
      <path fill="#FBBC05" d="M5.4 14.4a7.2 7.2 0 0 1 0-4.7V6.6h-4a12 12 0 0 0 0 10.8l4-3z" />
      <path fill="#EA4335" d="M12 4.8c1.7 0 3.3.6 4.6 1.8l3.4-3.4A12 12 0 0 0 1.4 6.6l4 3.1C6.3 6.9 8.9 4.8 12 4.8z" />
    </svg>
  )
}

/* ---------- hero ---------- */

// Posters floating around the hero headline. Positions are % of the hero; `r` is the resting tilt.
const TILES: { id: PosterId; left: string; top: string; w: string; r: number; delay: number }[] = [
  { id: 'boardgames', left: '36%', top: '-11%', w: '14.5%', r: -2, delay: 0 },
  { id: 'summer', left: '20%', top: '11%', w: '10%', r: 3, delay: 1.2 },
  { id: 'wedding', left: '5.5%', top: '29%', w: '7.5%', r: -4, delay: 2.4 },
  { id: 'pitch', left: '10.5%', top: '48%', w: '12.5%', r: 2, delay: 0.6 },
  { id: 'hack24', left: '-1.5%', top: '76%', w: '8.5%', r: -3, delay: 1.8 },
  { id: 'founders', left: '25.5%', top: '80%', w: '11%', r: 2, delay: 3 },
  { id: 'launch', left: '56.5%', top: '85%', w: '15%', r: -1, delay: 0.9 },
  { id: 'offsite', left: '72%', top: '5%', w: '11%', r: 3, delay: 2.1 },
  { id: 'devconf', left: '87%', top: '28%', w: '9.5%', r: -2, delay: 0.3 },
  { id: 'runclub', left: '73%', top: '50%', w: '9%', r: 4, delay: 1.5 },
  { id: 'reunion', left: '79%', top: '66%', w: '10.5%', r: -3, delay: 2.7 },
]

function Hero({ onStart, onSignIn }: Pick<Props, 'onStart' | 'onSignIn'>) {
  return (
    <section className="glow relative isolate flex min-h-[max(100svh,680px)] flex-col overflow-hidden">
      <div aria-hidden className="absolute inset-0 hidden md:block">
        {TILES.map((t) => (
          <div
            key={t.id}
            className="float-tile absolute"
            style={{ left: t.left, top: t.top, width: t.w, ['--r' as string]: `${t.r}deg`, animationDelay: `-${t.delay * 3}s` }}
          >
            <div className="rounded-[22px] bg-white p-[5%] shadow-xl shadow-indigo-500/15 ring-1 ring-gray-900/5">
              <Poster id={t.id} className="rounded-[14px]" />
            </div>
          </div>
        ))}
        {/* Fades the bottom row into the next section. */}
        <div className="absolute inset-x-0 bottom-0 h-1/3 bg-gradient-to-t from-paper to-transparent" />
      </div>

      <header className="relative z-10 flex items-center justify-between p-4 sm:p-6">
        <Sparkle size={22} className="fill-accent text-accent" />
        <button onClick={onSignIn} className="rounded-full bg-white/70 px-4 py-2 text-sm font-semibold text-gray-700 ring-1 ring-gray-900/10 backdrop-blur transition hover:text-accent hover:ring-accent">
          Sign In
        </button>
      </header>

      <div className="relative z-10 m-auto flex max-w-3xl animate-rise flex-col items-center px-4 pb-24 text-center">
        <div className="flex items-center gap-1 text-3xl font-semibold tracking-tight text-gray-400">
          evently <Sparkle size={14} className="-mt-4 fill-current" />
        </div>
        <h1 className="mt-5 text-6xl leading-[0.95] font-semibold tracking-tight sm:text-7xl xl:text-[88px]">
          Delightful
          <br />
          events
          <br />
          <span className="sm:whitespace-nowrap"><Glow>planned for you</Glow></span>
        </h1>
        <p className="mt-8 max-w-xl text-lg leading-relaxed text-gray-500 sm:text-2xl">
          From hackathons to weddings and reunions, Evently’s agents find the venue, book the caterer and send the invites.
        </p>
        <button
          onClick={onStart}
          className="mt-10 rounded-full bg-accent px-7 py-3.5 text-lg font-semibold text-white shadow-lg shadow-indigo-500/30 transition hover:bg-indigo-500"
        >
          Plan Your First Event
        </button>
        <a href="#categories" className="group mt-7 inline-flex items-center gap-2 text-lg font-medium text-accent transition hover:text-indigo-500">
          Browse Templates <ArrowRight size={18} className="transition group-hover:translate-x-0.5" />
        </a>
      </div>
    </section>
  )
}

/* ---------- landing sections ---------- */

function Popular() {
  const plans = [...SHARED_PLANS].sort((a, b) => a.in - b.in).slice(0, 6)
  return (
    <section>
      <SectionTitle>
        Popular Plans <span className="text-accent">This Week</span>
      </SectionTitle>
      <div className="-mx-4 flex snap-x gap-5 overflow-x-auto px-4 pb-2 sm:-mx-6 sm:px-6 lg:mx-0 lg:grid lg:grid-cols-6 lg:overflow-visible lg:px-0">
        {plans.map((e) => (
          <a key={e.id} href={`#/plan/${e.id}`} className="group w-40 shrink-0 snap-start lg:w-auto">
            <Poster id={e.poster} className="rounded-2xl shadow-lg shadow-indigo-500/10 ring-1 ring-gray-900/5 transition group-hover:-translate-y-1" />
            <h3 className="mt-3 line-clamp-2 text-lg leading-snug font-medium transition group-hover:text-accent">{e.title}</h3>
            <p className="mt-1.5 text-muted">{when(e)}</p>
            <p className="mt-0.5 truncate text-sm text-gray-400">By {e.host}</p>
          </a>
        ))}
      </div>
    </section>
  )
}

function Tools() {
  const tools: Integration[] = ['google-maps', 'linkedin', 'gmail', 'luma']
  return (
    <section>
      <SectionTitle>Works With Your Tools</SectionTitle>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {tools.map((id) => {
          const agents = AGENTS.filter((a) => a.integration === id).map((a) => a.name)
          return (
            <div key={id} className="flex items-center gap-4 rounded-2xl bg-white ring-1 ring-gray-900/5 shadow-sm shadow-indigo-500/5 p-4">
              <span className="grid size-14 shrink-0 place-items-center rounded-xl bg-gray-50 ring-1 ring-gray-900/5">
                <img src={INTEGRATION_LOGO[id]} alt="" className="size-8 object-contain" />
              </span>
              <div className="min-w-0">
                <div className="text-xl font-medium">{INTEGRATION_LABEL[id]}</div>
                <div className="truncate text-muted">{agents.join(' · ')}</div>
              </div>
            </div>
          )
        })}
      </div>
    </section>
  )
}

// Rotating icon tints for the agent cards.
const TINTS = ['#4f46e5', '#0284c7', '#7c3aed', '#db2777', '#c026d3', '#0891b2', '#2563eb', '#9333ea']

function Agents() {
  return (
    <section>
      <SectionTitle>Meet Your Agents</SectionTitle>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {AGENTS.slice(0, 8).map((a, i) => {
          const Icon = agentIcon(a.name)
          return (
            <div key={a.name} className="rounded-2xl bg-white ring-1 ring-gray-900/5 shadow-sm shadow-indigo-500/5 p-6">
              <span className="grid size-14 place-items-center rounded-xl" style={{ background: `${TINTS[i]}14`, color: TINTS[i] }}>
                <Icon size={26} />
              </span>
              <h3 className="mt-5 text-xl font-medium">{a.name}</h3>
              <p className="mt-1.5 line-clamp-2 text-muted">{a.example}</p>
            </div>
          )
        })}
      </div>
    </section>
  )
}

// Feeds the pointer position to .category-card's glow as --x / --y.
function trackGlow(e: MouseEvent<HTMLElement>) {
  const card = e.currentTarget
  const r = card.getBoundingClientRect()
  card.style.setProperty('--x', `${e.clientX - r.left}px`)
  card.style.setProperty('--y', `${e.clientY - r.top}px`)
}

function Categories() {
  return (
    <section id="categories" className="scroll-mt-10">
      <SectionTitle>Browse by Category</SectionTitle>
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4 lg:gap-6">
        {CATEGORIES.map(({ id, label, icon: Icon, color }) => (
          <a
            key={id}
            href={`#/category/${id}`}
            style={{ '--glow': color } as CSSProperties}
            onMouseMove={trackGlow}
            className="category-card relative flex h-36 flex-col justify-between overflow-hidden rounded-2xl bg-white ring-1 ring-gray-900/5 shadow-sm shadow-indigo-500/5 p-5 transition hover:-translate-y-0.5 sm:h-40 sm:p-6"
          >
            <Icon size={30} strokeWidth={1.6} style={{ color }} className="relative" />
            <span className="relative text-lg font-medium sm:text-xl">{label}</span>
          </a>
        ))}
      </div>
    </section>
  )
}

/* ---------- closing ---------- */

// Each row starts at a different plan and some run backwards, so no two rows line up.
const LOOP_ROWS = [
  { offset: 0, seconds: 70, reverse: false },
  { offset: 4, seconds: 85, reverse: true },
  { offset: 8, seconds: 65, reverse: false },
  { offset: 12, seconds: 90, reverse: true },
  { offset: 16, seconds: 75, reverse: false },
  { offset: 20, seconds: 80, reverse: true },
]

// Theme gradients for the loop cards' icon tiles, picked by the plan's place in the list.
const TILE = [
  'from-indigo-500 to-violet-500', 'from-sky-400 to-indigo-500', 'from-fuchsia-500 to-pink-500',
  'from-violet-500 to-fuchsia-500', 'from-pink-400 to-rose-500', 'from-cyan-400 to-sky-500',
]

function LoopCard({ plan: p, index }: { plan: SharedPlan; index: number }) {
  const Icon = PLAN_ICON[p.id]
  return (
    <a
      href={`#/plan/${p.id}`}
      className="group flex w-72 shrink-0 items-center gap-3 rounded-2xl bg-white p-3 shadow-sm ring-1 shadow-indigo-500/5 ring-gray-900/5 transition hover:shadow-lg hover:ring-accent/30"
    >
      <span className={`grid size-16 shrink-0 place-items-center rounded-xl bg-gradient-to-br text-white shadow-sm ${TILE[index % TILE.length]}`}>
        <Icon size={28} strokeWidth={1.8} />
      </span>
      <div className="min-w-0">
        <div className="truncate font-semibold transition group-hover:text-accent">{p.title}</div>
        <div className="truncate text-sm text-muted">
          {p.host} · {p.city}
        </div>
        <div className="mt-1 text-xs font-medium text-accent">{Object.keys(p.picks).length} steps planned</div>
      </div>
    </a>
  )
}

function FinalCta({ onStart }: Pick<Props, 'onStart'>) {
  return (
    <section className="relative isolate overflow-hidden py-16">
      {/* Rows of community plans scrolling forever; each track holds the list twice so the loop is seamless. */}
      <div className="space-y-5 [mask-image:linear-gradient(to_right,transparent,black_12%,black_88%,transparent)]">
        {LOOP_ROWS.map(({ offset, seconds, reverse }) => {
          const row = [...SHARED_PLANS.slice(offset), ...SHARED_PLANS.slice(0, offset)]
          return (
            <div key={offset} className="loop-row overflow-hidden">
              <div
                className={`loop-track flex w-max gap-4 pr-4 ${reverse ? 'loop-reverse' : ''}`}
                style={{ animationDuration: `${seconds}s` }}
              >
                {[...row, ...row].map((p, i) => (
                  <LoopCard key={`${p.id}-${i}`} plan={p} index={SHARED_PLANS.indexOf(p)} />
                ))}
              </div>
            </div>
          )
        })}
      </div>

      {/* Headline over a soft white pool so it reads above the moving cards. */}
      <div className="pointer-events-none absolute inset-0 grid place-items-center bg-[radial-gradient(ellipse_44%_38%_at_center,white_50%,rgba(255,255,255,0.8)_70%,transparent)] px-4">
        <div className="pointer-events-auto max-w-3xl text-center">
          <h2 className="text-5xl leading-tight font-semibold tracking-tight sm:text-6xl">
            <Glow>Let us plan while you experience.</Glow>
          </h2>
          <div className="mt-8 flex flex-wrap justify-center gap-4">
            <button onClick={onStart} className="rounded-full bg-accent px-6 py-3 text-lg font-semibold text-white shadow-lg shadow-indigo-500/30 transition hover:bg-indigo-500">
              Start Planning
            </button>
            <a href="#/present/demo" className="rounded-full bg-white px-6 py-3 text-lg font-semibold text-accent ring-1 ring-gray-900/10 transition hover:ring-accent">
              Try Live Slides
            </a>
          </div>
        </div>
      </div>
    </section>
  )
}

function GitHubMark() {
  return (
    <svg viewBox="0 0 16 16" className="size-5" fill="currentColor" aria-hidden>
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8z" />
    </svg>
  )
}

export function Footer() {
  const links = [
    { label: 'Templates', href: '#categories' },
    { label: 'Live Slides', href: '#/present/demo' },
    { label: 'GitHub', href: REPO },
    { label: 'Help', href: `${REPO}/issues` },
  ]
  const external = (href: string) => (href.startsWith('http') ? { target: '_blank', rel: 'noreferrer' } : {})
  return (
    <footer className="mx-auto max-w-6xl px-4 sm:px-6">
      <div className="flex flex-wrap items-center gap-x-8 gap-y-4 border-t border-gray-900/5 py-8">
        <a href="#">
          <Logo />
        </a>
        <nav className="flex flex-wrap gap-6 text-gray-600">
          {links.map((l) => (
            <a key={l.label} href={l.href} {...external(l.href)} className="transition hover:text-accent">
              {l.label}
            </a>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-5 text-gray-500">
          <a href={REPO} {...external(REPO)} aria-label="GitHub" className="hover:text-accent">
            <GitHubMark />
          </a>
          <a href={`${REPO}/issues`} {...external(REPO)} aria-label="Contact" className="hover:text-accent">
            <Mail size={20} />
          </a>
        </div>
      </div>
      <p className="pb-10 text-sm text-gray-400">© {YEAR} Evently</p>
    </footer>
  )
}

/* ---------- sign-in backdrop ---------- */

type Pt = [number, number]
type Curve = [Pt, Pt, Pt, Pt]

// Two edges of the band (cubic béziers in a 1000×1000 box). Fibers are interpolated between them.
const EDGE_IN: Curve = [[353, -260], [560, 330], [800, 560], [1060, 800]]
const EDGE_OUT: Curve = [[818, -260], [900, 170], [980, 300], [1060, 340]]
// A narrower fold that sits on top of the main band for depth.
const FOLD_IN: Curve = [[527, -260], [700, 260], [880, 470], [1060, 600]]
const FOLD_OUT: Curve = [[705, -260], [820, 200], [940, 340], [1060, 420]]

const lerp = (a: Curve, b: Curve, t: number): Curve =>
  a.map(([x, y], i) => [x + (b[i][0] - x) * t, y + (b[i][1] - y) * t]) as Curve
const d = ([p0, p1, p2, p3]: Curve) => `M${p0} C${p1} ${p2} ${p3}`
const band = (a: Curve, b: Curve) =>
  `${d(a)} L${b[3]} C${b[2]} ${b[1]} ${b[0]} Z`

const FIBERS = Array.from({ length: 56 }, (_, i) => lerp(EDGE_IN, EDGE_OUT, i / 55))
const FOLD_FIBERS = Array.from({ length: 22 }, (_, i) => lerp(FOLD_IN, FOLD_OUT, i / 21))

export function Ribbon() {
  return (
    <div
      aria-hidden
      className="ribbon-wrap pointer-events-none absolute inset-0 [mask-image:linear-gradient(to_bottom,black_70%,transparent)]"
    >
      <svg viewBox="0 0 1000 1000" preserveAspectRatio="xMaxYMin slice" className="size-full overflow-visible">
        <defs>
          <linearGradient id="rb-fill" x1="0.35" y1="0" x2="1" y2="0.8">
            <stop offset="0" stopColor="#bae6fd" />
            <stop offset="0.3" stopColor="#818cf8" />
            <stop offset="0.55" stopColor="#a78bfa" />
            <stop offset="0.8" stopColor="#f472b6" />
            <stop offset="1" stopColor="#fda4af" />
          </linearGradient>
          <linearGradient id="rb-fold" x1="0.5" y1="0" x2="1" y2="0.6">
            <stop offset="0" stopColor="#c7d2fe" stopOpacity="0.9" />
            <stop offset="0.5" stopColor="#e879f9" stopOpacity="0.8" />
            <stop offset="1" stopColor="#6366f1" stopOpacity="0.9" />
          </linearGradient>
          <linearGradient id="rb-edge" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0" stopColor="#fff" stopOpacity="0.9" />
            <stop offset="1" stopColor="#fff" stopOpacity="0" />
          </linearGradient>
          <filter id="rb-soft">
            <feGaussianBlur stdDeviation="6" />
          </filter>
        </defs>

        {/* soft halo so the band blends into white */}
        <path d={band(EDGE_IN, EDGE_OUT)} fill="url(#rb-fill)" opacity="0.35" filter="url(#rb-soft)" transform="translate(-30 10)" />
        <path d={band(EDGE_IN, EDGE_OUT)} fill="url(#rb-fill)" opacity="0.85" />
        <g fill="none" stroke="#fff" strokeWidth="0.8">
          {FIBERS.map((c, i) => (
            <path key={i} d={d(c)} strokeOpacity={0.08 + 0.22 * Math.abs(Math.sin(i * 0.7))} />
          ))}
        </g>

        <path d={band(FOLD_IN, FOLD_OUT)} fill="url(#rb-fold)" opacity="0.75" />
        <g fill="none" stroke="#fff" strokeWidth="0.7">
          {FOLD_FIBERS.map((c, i) => (
            <path key={i} d={d(c)} strokeOpacity={0.12 + 0.25 * Math.abs(Math.cos(i * 0.9))} />
          ))}
        </g>
        {/* bright leading edge */}
        <path d={d(EDGE_IN)} fill="none" stroke="url(#rb-edge)" strokeWidth="3" opacity="0.8" />
      </svg>
    </div>
  )
}
