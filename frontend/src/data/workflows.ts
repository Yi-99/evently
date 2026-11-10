import { hasRunner } from '../lib/runners'
import type { EventType, Integration, Task } from '../types'

type TaskSeed = Omit<Task, 'status' | 'results'>

const t = (
  id: string,
  title: string,
  agent: string,
  integration: Integration,
  description: string,
  dependsOn: string[] = [],
): TaskSeed => ({ id, title, agent, integration, description, dependsOn })

export const EVENT_TYPES: { id: EventType; label: string; emoji: string; blurb: string }[] = [
  { id: 'business', label: 'Business', emoji: '💼', blurb: 'Offsites, launches, client dinners' },
  { id: 'conference', label: 'Conference', emoji: '🎤', blurb: 'Talks, tracks, ticketing' },
  { id: 'hackathon', label: 'Hackathon', emoji: '💻', blurb: 'Sponsors, prizes, 24h builds' },
  { id: 'wedding', label: 'Wedding', emoji: '💍', blurb: 'Venue, vendors, guest list' },
  { id: 'reunion', label: 'Reunion', emoji: '🎉', blurb: 'Find people, pick a date' },
]

export const DEFAULT_HEADCOUNT: Record<EventType, number> = {
  business: 60,
  conference: 400,
  hackathon: 200,
  wedding: 120,
  reunion: 50,
}

const WORKFLOWS: Record<EventType, TaskSeed[]> = {
  hackathon: [
    t('brief', 'Event brief', 'Planner', 'web', 'Theme, dates, headcount and budget envelope.'),
    t('venue', 'Location', 'Venue Scout', 'google-maps', 'Find venues that host hackathons with 24h access, Wi-Fi and breakout rooms.', ['brief']),
    t('sponsors', 'Sponsors', 'Sponsor Hunter', 'linkedin', 'Shortlist dev-tool & API companies that sponsor hackathons, find the right contact, draft outreach.', ['brief']),
    t('registration', 'Registration', 'Registrar', 'luma', 'Set up the Luma event page, sign-up questions, waitlist and team matching.', ['brief']),
    t('catering', 'Catering', 'Caterer', 'google-maps', 'Quote meals + midnight snacks for the headcount near the venue.', ['venue']),
    t('av', 'Wi-Fi & AV', 'Tech Ops', 'web', 'Bandwidth estimate, power strips, projector and mic for demos.', ['venue']),
    t('prizes', 'Prizes', 'Prize Designer', 'web', 'Prize tracks and pool funded by sponsor tiers.', ['sponsors']),
    t('judges', 'Judges & mentors', 'Recruiter', 'gmail', 'Invite judges and mentors, collect availability.', ['sponsors']),
    t('schedule', 'Schedule', 'Scheduler', 'google-calendar', 'Kickoff, workshops, meals, submission deadline, demos.', ['venue', 'judges']),
    t('slides', 'Live slides', 'Stage Manager', 'live-slides', 'Kickoff and demo-day deck with a QR code so attendees follow along.', ['schedule']),
    t('dayof', 'Day-of run sheet', 'Coordinator', 'web', 'Volunteer roles, shifts and a minute-by-minute run sheet.', ['schedule', 'catering', 'av', 'prizes', 'registration']),
  ],
  business: [
    t('brief', 'Objectives', 'Planner', 'web', 'Goal of the event, audience, budget.'),
    t('guests', 'Guest list', 'Guest Manager', 'google-contacts', 'Pull invitees from contacts, segment VIPs.', ['brief']),
    t('venue', 'Location', 'Venue Scout', 'google-maps', 'Meeting spaces and private dining near the office.', ['brief']),
    t('catering', 'Catering', 'Caterer', 'google-maps', 'Menu options and dietary needs.', ['venue']),
    t('presentation', 'Presentation', 'Deck Builder', 'live-slides', 'Agenda deck with live-slide QR for the room.', ['brief']),
    t('invites', 'Invitations', 'Outreach', 'gmail', 'Send invites, track RSVPs.', ['guests', 'venue']),
    t('calendar', 'Calendar holds', 'Scheduler', 'google-calendar', 'Block time for attendees and speakers.', ['invites']),
    t('followup', 'Follow-up', 'Outreach', 'gmail', 'Thank-you notes, transcript and slides recap.', ['calendar', 'catering', 'presentation']),
  ],
  conference: [
    t('brief', 'Event brief', 'Planner', 'web', 'Tracks, dates, expected attendance, budget.'),
    t('venue', 'Location', 'Venue Scout', 'google-maps', 'Convention centers / hotels with multiple halls.', ['brief']),
    t('speakers', 'Speakers', 'Speaker Scout', 'linkedin', 'Shortlist speakers by track from their profiles, draft invitations.', ['brief']),
    t('sponsors', 'Sponsors', 'Sponsor Hunter', 'linkedin', 'Find sponsor contacts, sponsor tiers and booth layout.', ['brief']),
    t('ticketing', 'Ticketing', 'Registrar', 'luma', 'Luma ticket tiers, promo codes and check-in.', ['venue']),
    t('hotels', 'Hotel blocks', 'Travel Agent', 'google-maps', 'Room blocks near the venue for speakers + attendees.', ['venue']),
    t('catering', 'Catering', 'Caterer', 'google-maps', 'Breakfast, lunch and coffee breaks per day.', ['venue']),
    t('agenda', 'Agenda', 'Scheduler', 'google-calendar', 'Multi-track schedule, room assignments.', ['speakers', 'venue']),
    t('slides', 'Live slides & transcript', 'Stage Manager', 'live-slides', 'Live slide QR codes and voice transcript per session.', ['agenda']),
  ],
  wedding: [
    t('brief', 'Budget & vision', 'Planner', 'web', 'Style, season, budget split.'),
    t('guests', 'Guest list', 'Guest Manager', 'google-contacts', 'Build guest list from contacts, plus-ones.', ['brief']),
    t('venue', 'Location', 'Venue Scout', 'google-maps', 'Ceremony + reception venues that fit the guest count.', ['brief']),
    t('catering', 'Catering', 'Caterer', 'google-maps', 'Tasting options and per-plate quotes.', ['venue']),
    t('photo', 'Photographer', 'Vendor Scout', 'google-maps', 'Local photographers with matching style.', ['venue']),
    t('florist', 'Florist', 'Vendor Scout', 'google-maps', 'Florists near venue, seasonal flowers.', ['venue']),
    t('music', 'Music', 'Vendor Scout', 'web', 'DJ or band options.', ['venue']),
    t('invites', 'Invitations', 'Outreach', 'gmail', 'Save-the-dates and RSVPs.', ['guests', 'venue']),
    t('timeline', 'Day-of timeline', 'Coordinator', 'google-calendar', 'Hour-by-hour plan shared with vendors.', ['invites', 'catering', 'photo', 'florist', 'music']),
  ],
  reunion: [
    t('brief', 'Reunion brief', 'Planner', 'web', 'Who, what class/family, rough budget.'),
    t('find', 'Find people', 'Guest Finder', 'google-contacts', 'Match names to contacts, find missing emails.', ['brief']),
    t('date', 'Date poll', 'Scheduler', 'google-calendar', 'Poll for the date that works for most.', ['find']),
    t('venue', 'Location', 'Venue Scout', 'google-maps', 'Restaurants, parks or halls near most attendees.', ['brief']),
    t('catering', 'Food', 'Caterer', 'google-maps', 'Catering or set menu options.', ['venue']),
    t('invites', 'Invitations', 'Outreach', 'gmail', 'Send invite + RSVP link.', ['date', 'venue']),
    t('memories', 'Photo slideshow', 'Archivist', 'live-slides', 'Collect old photos into a slideshow everyone can follow on their phones.', ['find']),
  ],
}

// `skip` drops steps a template doesn't need (see data/categories).
export function buildWorkflow(type: EventType, skip: string[] = []): Task[] {
  return skip.reduce(removeStep, WORKFLOWS[type].map((s): Task => ({ ...s, dependsOn: [...s.dependsOn], status: 'idle' })))
}

// Children of the removed step inherit its dependencies so the graph stays connected.
export function removeStep(tasks: Task[], id: string): Task[] {
  const gone = tasks.find((t) => t.id === id)
  if (!gone) return tasks
  return tasks
    .filter((t) => t.id !== id)
    .map((t) =>
      t.dependsOn.includes(id) ? { ...t, dependsOn: [...new Set([...t.dependsOn.filter((d) => d !== id), ...gone.dependsOn])] } : t,
    )
}

export const INTEGRATION_LABEL: Record<Integration, string> = {
  'google-maps': 'Google Maps',
  gmail: 'Gmail',
  'google-calendar': 'Google Calendar',
  'google-contacts': 'Google Contacts',
  'google-slides': 'Google Slides',
  'live-slides': 'Live slides',
  linkedin: 'LinkedIn',
  luma: 'Luma',
  web: 'Web search',
}

export const needsGoogle = (i: Integration) => i.startsWith('google-') || i === 'gmail'

// A step waits on "Connect Google" only while it has no real backend runner: steps served by the
// backend (venues, decks, outreach) use server-side keys and never need the Google gate.
export const gatedByGoogle = (task: Pick<Task, 'id' | 'integration'>) => needsGoogle(task.integration) && !hasRunner(task)

// Every agent used across the workflows, with the integration it works through and one example job.
export const AGENTS: { name: string; integration: Integration; example: string }[] = [
  ...new Map(
    Object.values(WORKFLOWS)
      .flat()
      .map((s) => [s.agent, { name: s.agent, integration: s.integration, example: s.description }] as const),
  ).values(),
]
