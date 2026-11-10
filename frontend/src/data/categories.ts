import { Briefcase, CodeXml, Gem, GraduationCap, Mic, Music, PartyPopper, Users, type LucideIcon } from 'lucide-react'
import type { PosterId } from '../components/Poster'
import type { EventType } from '../types'

// A blank starting point for one kind of event: a workflow (`plan`) minus the steps it doesn't need
// (`skip`), plus a one-line description the intake reads for headcount and other details.
export interface Template {
  id: string
  name: string
  blurb: string
  plan: EventType
  note: string
  skip?: string[]
  tags: string[]
}

// The landing page's "Browse by category" cards. Each opens #/category/:id and lists its templates.
// There are only five workflows, so categories like Meetups build on the closest one.
export interface Category {
  id: string
  label: string
  // Singular, for "Plan a hackathon".
  noun: string
  icon: LucideIcon
  color: string
  poster: PosterId
  blurb: string
  tags: string[]
  plans: number
  organizers: string
  templates: Template[]
}

const tpl = (plan: EventType) => (id: string, name: string, blurb: string, note: string, tags: string[], skip?: string[]): Template => ({
  id, name, blurb, plan, note, tags, skip,
})
const hackathon = tpl('hackathon')
const conference = tpl('conference')
const business = tpl('business')
const wedding = tpl('wedding')
const reunion = tpl('reunion')

export const CATEGORIES: Category[] = [
  {
    id: 'hackathons', label: 'Hackathons', noun: 'hackathon', icon: CodeXml, color: '#4f46e5', poster: 'hack24',
    blurb: 'From 24-hour student builds to sponsor-backed AI weekends, plan the venue, prizes, judges and demo day in one workflow.',
    tags: ['BUILDS', 'DEMOS', 'PRIZES'], plans: 482, organizers: '1.2K',
    templates: [
      hackathon('student-24h', '24-Hour Student Hackathon', 'Overnight venue, midnight food, mentors and a demo-day run sheet.', '150-person 24-hour student hackathon', ['Overnight', 'Mentors', 'Prizes']),
      hackathon('ai-weekend', 'Sponsor-Backed AI Weekend', 'Sponsor tiers fund the prize pool; judges and tracks come from sponsors.', '300-person AI hackathon weekend with sponsors', ['Sponsors', 'Tracks', 'Judges']),
      hackathon('hack-day', 'Internal Hack Day', 'One day for your own team. No sponsors or public sign-ups.', '60-person internal company hack day', ['Internal', 'One day'], ['sponsors', 'registration']),
    ],
  },
  {
    id: 'conferences', label: 'Conferences', noun: 'conference', icon: Mic, color: '#0284c7', poster: 'devconf',
    blurb: 'Speakers, tracks, ticket tiers and hotel blocks: every moving part of a multi-day conference, handed to its own agent.',
    tags: ['KEYNOTES', 'TRACKS', 'EXPO'], plans: 196, organizers: '640',
    templates: [
      conference('multi-track', 'Multi-Track Tech Conference', 'Two days, parallel tracks, an expo floor and hotel blocks for travelers.', '800-person two-day tech conference', ['Multi-day', 'Tracks', 'Expo']),
      conference('summit', 'One-Day Industry Summit', 'A single stage and a local crowd, so no hotel blocks.', '250-person one-day industry summit', ['One day', 'Single stage'], ['hotels']),
    ],
  },
  {
    id: 'business', label: 'Business', noun: 'business event', icon: Briefcase, color: '#7c3aed', poster: 'founders',
    blurb: 'Offsites, client dinners and product launches with invites, calendar holds and follow-ups sent for you.',
    tags: ['OFFSITES', 'LAUNCHES', 'DINNERS'], plans: 731, organizers: '2K',
    templates: [
      business('offsite', 'Team Offsite', 'A few days away with an agenda deck, meals and calendar holds for everyone.', '40-person team offsite', ['Multi-day', 'Agenda']),
      business('launch', 'Product Launch Party', 'Press and customers, a stage moment with live slides, and follow-ups after.', '150-person product launch party', ['Press', 'Live slides']),
      business('client-dinner', 'Client Dinner', 'A private room, a short guest list and personal invites.', '20-person client dinner', ['Intimate', 'VIP'], ['presentation', 'calendar']),
    ],
  },
  {
    id: 'weddings', label: 'Weddings', noun: 'wedding', icon: Gem, color: '#db2777', poster: 'wedding',
    blurb: 'Find the venue, book the photographer and florist, collect RSVPs and share a day-of timeline with every vendor.',
    tags: ['CEREMONY', 'RECEPTION', 'RSVP'], plans: 318, organizers: '590',
    templates: [
      wedding('classic', 'Classic Wedding', 'Ceremony and reception with the full vendor team.', '120-guest wedding', ['Full vendor team']),
      wedding('micro', 'Micro Wedding', 'Close family and friends; a playlist instead of a band.', '30-guest micro wedding', ['Intimate'], ['music']),
    ],
  },
  {
    id: 'reunions', label: 'Reunions', noun: 'reunion', icon: PartyPopper, color: '#c026d3', poster: 'reunion',
    blurb: 'Track down old classmates, poll for the date that works, and put everyone’s photos into one shared slideshow.',
    tags: ['CLASSES', 'FAMILIES', 'TEAMS'], plans: 254, organizers: '870',
    templates: [
      reunion('class', 'Class Reunion', 'Find classmates, poll a date and build a slideshow from old photos.', '80-person 10-year class reunion', ['Find people', 'Slideshow']),
      reunion('family', 'Family Reunion', 'Everyone’s already in your contacts, so it starts at the date poll.', '50-person family reunion', ['Date poll'], ['find']),
    ],
  },
  {
    id: 'meetups', label: 'Meetups', noun: 'meetup', icon: Users, color: '#0891b2', poster: 'runclub',
    blurb: 'Monthly talks, run clubs and pitch nights. Line up speakers, book a space and keep regulars coming back.',
    tags: ['TALKS', 'NETWORKING', 'RUN CLUBS'], plans: 905, organizers: '3.1K',
    templates: [
      business('monthly', 'Monthly Tech Meetup', 'Talks with live slides, pizza, and a recurring calendar hold.', '60-person monthly tech meetup', ['Recurring', 'Talks']),
      business('run-club', 'Run Club', 'A meeting spot, a route and a weekly reminder. No food or slides.', '30-person weekly run club', ['Outdoors', 'Weekly'], ['catering', 'presentation']),
    ],
  },
  {
    id: 'workshops', label: 'Workshops', noun: 'workshop', icon: GraduationCap, color: '#2563eb', poster: 'workshop',
    blurb: 'Hands-on sessions with live slides your audience follows on their phones, plus a transcript afterwards.',
    tags: ['HANDS-ON', 'CLASSES', 'LABS'], plans: 377, organizers: '1K',
    templates: [
      conference('hands-on', 'Hands-On Workshop', 'One room, a capped sign-up and a step-by-step deck.', '40-person hands-on workshop', ['Capped seats', 'Live slides'], ['sponsors', 'hotels']),
      conference('workshop-series', 'Workshop Series', 'Several sessions across a week with sponsors covering costs.', '120-person workshop series', ['Multi-session', 'Sponsors'], ['hotels']),
    ],
  },
  {
    id: 'parties', label: 'Parties', noun: 'party', icon: Music, color: '#9333ea', poster: 'summer',
    blurb: 'Rooftop sets, launch nights and game nights. Get the guest list, food and invites sorted in an afternoon.',
    tags: ['ROOFTOPS', 'GAME NIGHTS', 'LAUNCHES'], plans: 642, organizers: '2.4K',
    templates: [
      reunion('rooftop', 'Rooftop Party', 'Venue, food and invites for a big night out.', '150-person rooftop party', ['Big night'], ['find', 'date']),
      reunion('game-night', 'Game Night', 'A back room, pizza and a quick invite.', '25-person game night', ['Casual'], ['find', 'memories']),
    ],
  },
]

export const findCategory = (id: string) => CATEGORIES.find((c) => c.id === id)

export const daysFromNow = (n: number) => {
  const d = new Date()
  d.setDate(d.getDate() + n)
  return d
}
