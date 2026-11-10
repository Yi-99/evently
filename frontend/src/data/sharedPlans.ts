import {
  Bot, Brain, Coffee, Cpu, Dices, Footprints, Gem, Gift, GraduationCap, HeartHandshake, House, Leaf, Megaphone, Mic, Palette,
  Rocket, Shell, Sun, Sunrise, Tent, Terminal, TrendingUp, Wine, type LucideIcon,
} from 'lucide-react'
import type { PosterId } from '../components/Poster'
import type { SavedPlan } from '../lib/recent'
import type { EventType, TaskResult } from '../types'
import { daysFromNow } from './categories'
import { EVENT_TYPES, buildWorkflow } from './workflows'

// A plan another organizer filled out and shared: what they chose for each step (`picks`, keyed by
// step id). Shown under "Popular plans" and on #/plan/:id; "Use this plan" copies it onto the node
// map. Every name and number here is made up. `in` is days from today, so dates never go stale.
export interface SharedPlan {
  id: string
  title: string
  host: string
  poster: PosterId
  // The category page that lists it.
  category: string
  type: EventType
  skip?: string[]
  in: number
  time: string
  place: string
  city: string
  headcount: number
  budget: string
  goal: string
  going: number
  blurb: string
  picks: Record<string, TaskResult>
}

const pick = (title: string, subtitle: string, extra: Partial<TaskResult> = {}): TaskResult => ({ title, subtitle, ...extra })

export const SHARED_PLANS: SharedPlan[] = [
  {
    id: 'saturday-run-club', title: 'Saturday Run Club', host: 'Embarcadero Runners', poster: 'runclub', category: 'meetups',
    type: 'business', skip: ['catering', 'presentation'], in: 1, time: '7:00 AM', place: 'Ferry Building', city: 'San Francisco',
    headcount: 60, budget: '$300', goal: 'Community', going: 62,
    blurb: 'An easy-pace 5K along the water every Saturday, with coffee after. Built to run itself week to week.',
    picks: {
      brief: pick('Weekly social 5K', 'Easy pace, all levels, coffee after'),
      venue: pick('Ferry Building Plaza', 'Embarcadero, San Francisco', { address: 'Embarcadero, San Francisco', price: 'Free', why: 'Public plaza; no permit needed under 75 people.' }),
      invites: pick('Strava club + email list', 'Reminder every Thursday at 6 PM'),
      calendar: pick('Recurring Saturday hold', 'Every Saturday, 7:00–8:30 AM'),
    },
  },
  {
    id: 'founders-dinner', title: 'Founders Dinner', host: 'Good Company', poster: 'founders', category: 'business',
    type: 'business', skip: ['presentation', 'calendar'], in: 2, time: '7:30 PM', place: 'Zuni Café', city: 'San Francisco',
    headcount: 24, budget: '$3,000', goal: 'Networking', going: 24,
    blurb: 'A long table for 24 seed-stage founders. Personal invites, a private room and a thank-you note after.',
    picks: {
      brief: pick('Seed-stage founders dinner', '24 seats, one long table'),
      guests: pick('24 founders', 'Pulled from contacts, 6 VIPs flagged'),
      venue: pick('Zuni Café private room', 'Market St, San Francisco', { address: 'Market St, San Francisco', rating: 4.6, price: '$110 / person', capacity: 'Fits 28' }),
      invites: pick('Personal invites sent', '22 of 24 said yes'),
    },
  },
  {
    id: 'game-night', title: 'Game Night', host: 'Mission Meeples', poster: 'boardgames', category: 'parties',
    type: 'reunion', skip: ['find', 'memories'], in: 2, time: '8:00 PM', place: 'The Mill', city: 'San Francisco',
    headcount: 40, budget: '$400', goal: 'Celebrate', going: 38,
    blurb: 'Bring a game, meet new people. Pizza at nine, last round at midnight.',
    picks: {
      brief: pick('Board game social', 'Bring a game; snacks provided'),
      venue: pick('The Mill back room', 'Divisadero, San Francisco', { address: 'Divisadero, San Francisco', rating: 4.7, price: '$150 flat', capacity: 'Fits 45' }),
      catering: pick('Night Owl Pizza Co.', 'SoMa, San Francisco', { rating: 4.6, price: '$11 / person', why: 'Delivers late and does half-and-half pies.' }),
    },
  },
  {
    id: 'austin-ai-hackathon', title: 'Austin AI Hackathon', host: 'Builders ATX', poster: 'hack24', category: 'hackathons',
    type: 'hackathon', in: 3, time: '9:00 AM', place: 'Capital Factory', city: 'Austin',
    headcount: 220, budget: '$25,000', goal: 'Ship projects', going: 214,
    blurb: 'A 24-hour AI build with three sponsor tracks, a $12k prize pool and demos judged by sponsor engineers.',
    picks: {
      brief: pick('24-hour AI hackathon', '3 tracks, 220 hackers, $25k budget'),
      venue: pick('Capital Factory', 'Congress Ave, Austin', { address: 'Congress Ave, Austin', rating: 4.7, price: '$4,000 / day', capacity: 'Fits 260', why: '24h access, gigabit Wi-Fi and a demo stage.' }),
      sponsors: pick('Nimbus Cloud', 'Gold · $10k', { contact: { name: 'Priya Nair', role: 'Developer Relations' } }),
      catering: pick('Harvest Table Catering', 'East Austin', { rating: 4.8, price: '$18 / person', why: 'Vegan and gluten-free buffet; handles 300+.' }),
      prizes: pick('$12k prize pool', '3 tracks + best use of a sponsor API'),
    },
  },
  {
    id: 'ship-an-agent', title: 'Ship an Agent in 2 Hours', host: 'Agent Guild', poster: 'workshop', category: 'workshops',
    type: 'conference', skip: ['sponsors', 'hotels'], in: 3, time: '3:00 PM', place: '500 Howard St', city: 'San Francisco',
    headcount: 40, budget: '$1,500', goal: 'Teach a skill', going: 88,
    blurb: 'A hands-on lab: everyone leaves with a working agent. 40 seats, a waitlist, and a deck people follow on their phones.',
    picks: {
      brief: pick('Hands-on agent lab', '40 seats, laptops required'),
      venue: pick('500 Howard St, Floor 2', 'SoMa, San Francisco', { address: 'SoMa, San Francisco', price: '$600 / half day', capacity: 'Fits 45' }),
      ticketing: pick('Luma page', '40 seats + waitlist'),
      agenda: pick('2-hour lab', 'Intro 15 min · build 90 min · demos 15 min'),
    },
  },
  {
    id: 'pitch-night', title: 'Pitch Your Idea Night', host: 'SF Founders Meetup', poster: 'pitch', category: 'meetups',
    type: 'business', in: 4, time: '6:30 PM', place: 'Shack15', city: 'San Francisco',
    headcount: 100, budget: '$2,000', goal: 'Networking', going: 95,
    blurb: 'Eight founders, three minutes each, live feedback from the room. Drinks after.',
    picks: {
      brief: pick('Monthly pitch night', '8 founders · 3-minute pitches'),
      venue: pick('Shack15', 'Ferry Building, San Francisco', { address: 'Ferry Building, San Francisco', rating: 4.5, price: '$1,200 / evening', capacity: 'Fits 120' }),
      presentation: pick('Pitch deck queue', '8 decks with live-slide QR codes'),
    },
  },
  {
    id: 'devconf-west', title: 'DevConf West', host: 'DevConf', poster: 'devconf', category: 'conferences',
    type: 'conference', in: 5, time: '8:30 AM', place: 'Moscone West', city: 'San Francisco',
    headcount: 1300, budget: '$420,000', goal: 'Sell tickets', going: 1280,
    blurb: 'Two days, four tracks and an expo floor. Speakers, ticket tiers and a hotel block are already lined up.',
    picks: {
      brief: pick('Two-day developer conference', '4 tracks, 1,300 attendees'),
      venue: pick('Moscone West', 'Howard St, San Francisco', { address: 'Howard St, San Francisco', rating: 4.4, price: '$38,000 / day', capacity: 'Fits 2,000' }),
      speakers: pick('32 speakers confirmed', '4 tracks · 2 keynotes'),
      ticketing: pick('Luma ticket tiers', 'Early bird $299 · General $499'),
      hotels: pick('Hotel Zephyr block', '120 rooms at $219 / night'),
    },
  },
  {
    id: 'class-of-16', title: 'Class of ’16: 10 Year Reunion', host: 'Lincoln High Alumni', poster: 'reunion', category: 'reunions',
    type: 'reunion', in: 8, time: '6:00 PM', place: 'Dolores Park', city: 'San Francisco',
    headcount: 80, budget: '$1,200', goal: 'Celebrate', going: 73,
    blurb: 'A picnic in the park for the class of 2016, with a slideshow of 340 old photos.',
    picks: {
      find: pick('Found 74 of 96 classmates', 'Matched from contacts and LinkedIn'),
      date: pick('Saturday evening won', '61 votes in the poll'),
      venue: pick('Dolores Park, upper lawn', 'Mission, San Francisco', { address: 'Dolores Park, San Francisco', price: 'Free (permit $95)' }),
      memories: pick('Slideshow ready', '340 photos from 52 classmates'),
    },
  },
  {
    id: 'summer-rooftop', title: 'Summer ’27 Rooftop Party', host: 'Sunset Social', poster: 'summer', category: 'parties',
    type: 'reunion', skip: ['find', 'date'], in: 9, time: '9:00 PM', place: 'Charmaine’s', city: 'San Francisco',
    headcount: 220, budget: '$8,000', goal: 'Celebrate', going: 210,
    blurb: 'A DJ set and light show on the roof. Venue, food and invites are done; bring your own playlist.',
    picks: {
      venue: pick('Charmaine’s rooftop', 'Market St, San Francisco', { address: 'Market St, San Francisco', rating: 4.3, price: '$5,500 buyout', capacity: 'Fits 250' }),
      catering: pick('Taco Truck Collective', 'Mission, San Francisco', { rating: 4.7, price: '$13 / person' }),
      invites: pick('Invites sent', '210 going, 40 on the waitlist'),
    },
  },
  {
    id: 'q1-offsite', title: 'Q1 Team Offsite', host: 'Northwind', poster: 'offsite', category: 'business',
    type: 'business', in: 12, time: '10:00 AM', place: 'Sea Ranch Lodge', city: 'Sea Ranch',
    headcount: 46, budget: '$38,000', goal: 'Plan the year', going: 46,
    blurb: 'Three days on the coast: planning sessions in the morning, hikes in the afternoon.',
    picks: {
      brief: pick('3-day planning offsite', 'Mornings: strategy · afternoons: off'),
      venue: pick('Sea Ranch Lodge', 'Highway 1, Sea Ranch', { address: 'Sea Ranch, CA', rating: 4.6, price: '$9,800 / night', capacity: '20 rooms' }),
      catering: pick('Lodge kitchen, full board', 'Three meals a day', { price: '$95 / person / day' }),
      presentation: pick('Planning deck', '18 slides with live-slide QR'),
    },
  },
  {
    id: 'maya-leo', title: 'Maya & Leo’s Wedding', host: 'Maya & Leo', poster: 'wedding', category: 'weddings',
    type: 'wedding', in: 21, time: '4:00 PM', place: 'Meadowood', city: 'Napa Valley',
    headcount: 120, budget: '$85,000', goal: 'Celebrate', going: 120,
    blurb: 'A vineyard ceremony at golden hour and dinner under the oaks. Venue, photographer and florist are booked.',
    picks: {
      brief: pick('Vineyard wedding', 'Garden ceremony · dinner under the oaks'),
      venue: pick('Meadowood', 'St. Helena, Napa Valley', { address: 'St. Helena, CA', rating: 4.9, price: '$28,000', capacity: 'Fits 150' }),
      photo: pick('Golden Hour Studio', 'Napa', { rating: 4.9, price: '$6,500 / day' }),
      florist: pick('Wild Oak Florals', 'Yountville', { rating: 4.8, price: '$7,200' }),
    },
  },
  {
    id: 'launch-night', title: 'Launch Night: Northwind v2', host: 'Northwind', poster: 'launch', category: 'business',
    type: 'business', in: 6, time: '8:00 PM', place: 'The Midway', city: 'San Francisco',
    headcount: 180, budget: '$22,000', goal: 'Press and customers', going: 172,
    blurb: 'A product reveal on stage, demos at the bar and a DJ until late. Press list and follow-ups included.',
    picks: {
      venue: pick('The Midway', 'Dogpatch, San Francisco', { address: 'Dogpatch, San Francisco', rating: 4.5, price: '$9,000 / night', capacity: 'Fits 400' }),
      presentation: pick('Keynote deck', '14 slides with a live-slide QR'),
      guests: pick('180 guests', '40 press, 90 customers, 50 team'),
    },
  },
  {
    id: 'women-in-ai', title: 'Women in AI Breakfast', host: 'Bay Area Builders', poster: 'workshop', category: 'meetups',
    type: 'business', in: 7, time: '8:30 AM', place: 'Sightglass Coffee', city: 'San Francisco',
    headcount: 45, budget: '$900', goal: 'Networking', going: 44,
    blurb: 'Coffee, pastries and two lightning talks before work. Same table every month.',
    picks: {
      venue: pick('Sightglass mezzanine', 'SoMa, San Francisco', { rating: 4.7, price: '$250 room fee', capacity: 'Fits 50' }),
      catering: pick('Tartine pastry boxes', 'Mission, San Francisco', { price: '$9 / person' }),
    },
  },
  {
    id: 'rust-workshop', title: 'Rust Workshop: Zero to CLI', host: 'Ferris Club', poster: 'hack24', category: 'workshops',
    type: 'conference', skip: ['sponsors', 'hotels'], in: 7, time: '1:00 PM', place: 'GitHub HQ', city: 'San Francisco',
    headcount: 50, budget: '$800', goal: 'Teach a skill', going: 50,
    blurb: 'Build and ship a command-line tool in an afternoon. Mentors at every table.',
    picks: {
      ticketing: pick('Luma page', '50 seats, 31 on the waitlist'),
      agenda: pick('3-hour lab', 'Setup · build · publish to crates.io'),
    },
  },
  {
    id: 'climate-summit', title: 'Climate Tech Summit', host: 'Green Ledger', poster: 'offsite', category: 'conferences',
    type: 'conference', skip: ['hotels'], in: 11, time: '9:00 AM', place: 'Seattle Center', city: 'Seattle',
    headcount: 400, budget: '$90,000', goal: 'Sell tickets', going: 388,
    blurb: 'One day, one stage: founders, investors and city officials on decarbonizing the Pacific Northwest.',
    picks: {
      venue: pick('Seattle Center Exhibition Hall', 'Lower Queen Anne, Seattle', { rating: 4.4, price: '$14,000 / day', capacity: 'Fits 600' }),
      speakers: pick('18 speakers confirmed', '2 keynotes · 4 panels'),
      ticketing: pick('Luma ticket tiers', 'Startup $149 · Corporate $399'),
    },
  },
  {
    id: 'jen-sam', title: 'Jen & Sam’s Beach Wedding', host: 'Jen & Sam', poster: 'summer', category: 'weddings',
    type: 'wedding', skip: ['music'], in: 30, time: '5:30 PM', place: 'Seabright Beach', city: 'Santa Cruz',
    headcount: 60, budget: '$32,000', goal: 'Celebrate', going: 58,
    blurb: 'Barefoot vows at sunset, tacos on the sand and a playlist everyone added to.',
    picks: {
      venue: pick('Seabright Beach + Dream Inn patio', 'Santa Cruz', { rating: 4.7, price: '$6,500', capacity: 'Fits 80' }),
      catering: pick('Taqueria Vallarta', 'Santa Cruz', { price: '$28 / person' }),
      photo: pick('Salt & Light Photo', 'Santa Cruz', { rating: 4.9, price: '$3,800' }),
    },
  },
  {
    id: 'hack-for-good', title: 'Hackathon for Good', host: 'Civic Code NYC', poster: 'pitch', category: 'hackathons',
    type: 'hackathon', in: 13, time: '10:00 AM', place: 'Brooklyn Navy Yard', city: 'New York',
    headcount: 160, budget: '$18,000', goal: 'Ship projects', going: 151,
    blurb: 'Teams build tools for local nonprofits over a weekend. Nonprofit leads judge the demos.',
    picks: {
      venue: pick('Building 77', 'Brooklyn Navy Yard, New York', { rating: 4.6, price: '$3,500 / day', capacity: 'Fits 200' }),
      sponsors: pick('Patchwork API', 'Silver · $5k'),
      judges: pick('6 nonprofit leads', 'Food banks, housing, literacy'),
    },
  },
  {
    id: 'garcia-reunion', title: 'Garcia Family Reunion', host: 'The Garcias', poster: 'reunion', category: 'reunions',
    type: 'reunion', skip: ['find'], in: 16, time: '12:00 PM', place: 'Balboa Park', city: 'San Diego',
    headcount: 70, budget: '$2,500', goal: 'Celebrate', going: 66,
    blurb: 'Four generations, one picnic. Grandma’s recipes, a softball game and a slideshow of 60 years of photos.',
    picks: {
      date: pick('Sunday afternoon won', '44 of 52 households'),
      venue: pick('Pepper Grove picnic area', 'Balboa Park, San Diego', { price: '$180 permit', capacity: 'Fits 100' }),
      memories: pick('Slideshow ready', '512 photos from 1964 on'),
    },
  },
  {
    id: 'trivia-night', title: 'Friday Trivia Night', host: 'Oakland Quiz League', poster: 'boardgames', category: 'parties',
    type: 'reunion', skip: ['find', 'memories'], in: 4, time: '7:30 PM', place: 'The Kon-Tiki', city: 'Oakland',
    headcount: 60, budget: '$350', goal: 'Celebrate', going: 57,
    blurb: 'Teams of six, five rounds, one very competitive picture round.',
    picks: {
      venue: pick('The Kon-Tiki back room', 'Downtown Oakland', { rating: 4.6, price: 'Free with $500 minimum', capacity: 'Fits 70' }),
      invites: pick('Team sign-ups', '10 teams registered'),
    },
  },
  {
    id: 'design-systems', title: 'Design Systems Meetup', host: 'Brooklyn Design Guild', poster: 'devconf', category: 'meetups',
    type: 'business', in: 10, time: '6:30 PM', place: 'Figma NYC', city: 'New York',
    headcount: 90, budget: '$1,800', goal: 'Networking', going: 86,
    blurb: 'Two talks on tokens and component libraries, then drinks. Monthly, second Wednesday.',
    picks: {
      venue: pick('Figma NYC event space', 'Chelsea, New York', { price: 'Free (sponsored)', capacity: 'Fits 120' }),
      presentation: pick('Speaker decks', '2 talks with live-slide QR'),
      calendar: pick('Recurring hold', 'Second Wednesday, 6:30 PM'),
    },
  },
  {
    id: 'holiday-party', title: 'Holiday Office Party', host: 'Lakeview Partners', poster: 'launch', category: 'parties',
    type: 'reunion', skip: ['find', 'date'], in: 24, time: '6:00 PM', place: 'Cindy’s Rooftop', city: 'Chicago',
    headcount: 120, budget: '$14,000', goal: 'Celebrate', going: 112,
    blurb: 'Rooftop views, a hot cocoa bar and a photo booth. Plus-ones welcome.',
    picks: {
      venue: pick('Cindy’s Rooftop', 'Michigan Ave, Chicago', { rating: 4.5, price: '$8,000 buyout', capacity: 'Fits 150' }),
      catering: pick('Passed bites + cocoa bar', 'In-house', { price: '$45 / person' }),
    },
  },
  {
    id: 'sunrise-yoga', title: 'Sunrise Yoga & Coffee', host: 'Venice Movement', poster: 'yoga', category: 'meetups',
    type: 'business', skip: ['presentation'], in: 2, time: '6:30 AM', place: 'Venice Beach', city: 'Los Angeles',
    headcount: 40, budget: '$200', goal: 'Community', going: 39,
    blurb: 'An hour of flow on the sand, then cold brew. Mats provided.',
    picks: {
      venue: pick('Venice Beach, Windward Ave', 'Venice, Los Angeles', { price: 'Free' }),
      catering: pick('Groundwork cold brew cart', 'Venice', { price: '$5 / person' }),
    },
  },
  {
    id: 'seed-demo-day', title: 'Seed Demo Day', host: 'Foundry Fellows', poster: 'founders', category: 'business',
    type: 'business', in: 18, time: '2:00 PM', place: 'Palace of Fine Arts', city: 'San Francisco',
    headcount: 350, budget: '$60,000', goal: 'Raise for founders', going: 340,
    blurb: '20 startups, 4 minutes each, 200 investors in the room. Reception after.',
    picks: {
      guests: pick('210 investors', 'Pulled from contacts, tiered by check size'),
      venue: pick('Palace of Fine Arts Theatre', 'Marina, San Francisco', { rating: 4.7, price: '$18,000 / day', capacity: 'Fits 960' }),
      presentation: pick('20 pitch decks', 'Queued with live-slide QR codes'),
    },
  },
]

// "Today, 9:00 AM", "Thursday, 7:30 PM", "Dec 5, 4:00 PM".
export const when = (p: SharedPlan) => {
  const day =
    p.in === 0 ? 'Today'
    : p.in === 1 ? 'Tomorrow'
    : p.in < 7 ? daysFromNow(p.in).toLocaleDateString('en-US', { weekday: 'long' })
    : daysFromNow(p.in).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
  return `${day}, ${p.time}`
}

// One icon per plan, so the looping rows on the landing page never show the same mark twice
// (posters repeat across plans).
export const PLAN_ICON: Record<string, LucideIcon> = {
  'saturday-run-club': Footprints, 'founders-dinner': Wine, 'game-night': Dices, 'austin-ai-hackathon': Cpu,
  'ship-an-agent': Bot, 'pitch-night': Megaphone, 'devconf-west': Mic, 'class-of-16': GraduationCap,
  'summer-rooftop': Sun, 'q1-offsite': Tent, 'maya-leo': Gem, 'launch-night': Rocket, 'women-in-ai': Coffee,
  'rust-workshop': Terminal, 'climate-summit': Leaf, 'jen-sam': Shell, 'hack-for-good': HeartHandshake,
  'garcia-reunion': House, 'trivia-night': Brain, 'design-systems': Palette, 'holiday-party': Gift,
  'sunrise-yoga': Sunrise, 'seed-demo-day': TrendingUp,
}

export const findSharedPlan = (id: string) => SHARED_PLANS.find((p) => p.id === id)
export const plansIn = (category: string) => SHARED_PLANS.filter((p) => p.category === category)

// The plan as the user's own copy: chosen steps done and approved, the rest left for agents.
export function copyOf(p: SharedPlan): SavedPlan {
  const tasks = buildWorkflow(p.type, p.skip).map((t) =>
    p.picks[t.id] ? { ...t, status: 'done' as const, results: [p.picks[t.id]], shared: true } : t,
  )
  const filled = tasks.filter((t) => t.shared).length
  const date = daysFromNow(p.in).toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' })
  return {
    event: { type: p.type, headcount: p.headcount, city: p.city, date },
    tasks,
    messages: [
      {
        id: `copy-${p.id}`,
        role: 'assistant',
        text: `You're starting from ${p.host}'s "${p.title}" plan. ${filled} of ${tasks.length} steps keep their choices; agents will fill the other ${tasks.length - filled}. Tell me what to change (date, city, headcount), then hit Approve plan.`,
      },
    ],
    answers: {
      type: EVENT_TYPES.find((e) => e.id === p.type)!.label,
      goal: p.goal,
      date,
      headcount: String(p.headcount),
      city: p.city,
      budget: p.budget,
    },
    approved: false,
    review: {
      decisions: Object.fromEntries(Object.entries(p.picks).map(([step, r]) => [`${step}:${r.title}`, 'approved' as const])),
      progress: {},
    },
  }
}
