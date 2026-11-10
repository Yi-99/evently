import { EVENT_TYPES } from '../data/workflows'
import type { EventDetails } from '../types'

export type Tier = 'must' | 'should' | 'nice'

export interface IntakeQuestion {
  id: string
  tier: Tier
  prompt: string
  // Quick-answer pills shown under the chat while this question is pending.
  options: string[]
  // Turns a free-text answer into event fields. Return null to reject the answer and re-ask.
  parse?: (answer: string) => Partial<EventDetails> | null
}

// Answers keyed by question id. An empty string means the user skipped it.
export type IntakeAnswers = Record<string, string>

// Asked in order: every 'must' first, then 'should', then 'nice'.
// Only 'must' questions are required before the plan can be approved.
export const QUESTIONS: IntakeQuestion[] = [
  {
    // Always answered by the event the user picked on the landing page.
    id: 'type',
    tier: 'must',
    prompt: 'What kind of event is it?',
    options: EVENT_TYPES.map((e) => e.label),
  },
  {
    id: 'goal',
    tier: 'must',
    prompt: 'What does success look like for this event?',
    options: ['Networking', 'Ship projects', 'Celebrate', 'Sell tickets'],
  },
  {
    id: 'date',
    tier: 'must',
    prompt: 'When is it? Give an exact date or a rough window, plus how many days.',
    options: ['Next month', 'In 3 months', 'Flexible'],
    parse: (a) => ({ date: a }),
  },
  {
    id: 'headcount',
    tier: 'must',
    prompt: 'How many guests do you expect?',
    options: ['50 people', '150 people', '300 people', '500+ people'],
    parse: (a) => {
      const n = a.match(/\d[\d,]*/)
      return n ? { headcount: parseInt(n[0].replace(/,/g, ''), 10) } : null
    },
  },
  {
    id: 'city',
    tier: 'must',
    prompt: 'Which city is it in? Tell me if you already have a venue.',
    options: ['San Francisco', 'New York', 'Austin'],
    parse: (a) => ({ city: a.replace(/^(?:in|at|near)\s+/i, '') }),
  },
  {
    id: 'budget',
    tier: 'must',
    prompt: "What's your total budget, and who's paying (you, sponsors, or tickets)?",
    options: ['Under $5k', '$5k–$20k', '$20k–$50k', '$50k+'],
  },
  {
    id: 'approver',
    tier: 'must',
    prompt: 'Who signs off on the plan and on vendor bookings?',
    options: ['Just me', 'My team', 'A client'],
  },
  {
    id: 'audience',
    tier: 'should',
    prompt: 'Who is attending? Are there any age limits?',
    options: ['Internal team', 'Public', 'Students', 'VIPs'],
  },
  {
    id: 'format',
    tier: 'should',
    prompt: 'Is it in person, hybrid, or virtual?',
    options: ['In person', 'Hybrid', 'Virtual'],
  },
  {
    id: 'food',
    tier: 'should',
    prompt: 'Which meals do you need? Any dietary restrictions, and alcohol or not?',
    options: ['Snacks only', 'Lunch', 'All meals', 'No alcohol'],
  },
  {
    id: 'accessibility',
    tier: 'should',
    prompt: 'Any accessibility needs, like wheelchair access, interpreters, or quiet rooms?',
    options: ['Wheelchair access', 'Interpreters', 'None'],
  },
  {
    id: 'features',
    tier: 'should',
    prompt: 'Anything you must have? For example judges, prizes, a photo booth, swag, or a stage with AV.',
    options: ['Photo booth', 'Prizes', 'Swag', 'Stage + AV'],
  },
  {
    id: 'booked',
    tier: 'should',
    prompt: "What's already booked (venue, vendors, sponsors, speakers)? I won't book those again.",
    options: ['Nothing yet', 'Venue', 'Sponsors'],
  },
  {
    id: 'tickets',
    tier: 'should',
    prompt: 'Is registration free or paid? Is there a deadline or a waitlist?',
    options: ['Free', 'Paid', 'Invite only'],
  },
  {
    id: 'branding',
    tier: 'nice',
    prompt: 'Do you have a theme, name, colors, or tone in mind?',
    options: ['Casual', 'Formal', 'Playful'],
  },
  {
    id: 'constraints',
    tier: 'nice',
    prompt: 'Any hard limits, like noise rules, permits, insurance, or venue restrictions?',
    options: ['None', 'Permits needed', 'Insurance needed'],
  },
  {
    id: 'contact',
    tier: 'nice',
    prompt: 'How should I send you updates and approval requests?',
    options: ['In app', 'Email', 'Slack'],
  },
]

export const SKIP = 'Skip'
// Ends the optional questions early and draws the plan.
export const BUILD_NOW = 'Build my plan'

export const isRequired = (q: IntakeQuestion) => q.tier === 'must'

export const nextQuestion = (answers: IntakeAnswers) => QUESTIONS.find((q) => !(q.id in answers))

export const missingRequired = (answers: IntakeAnswers) =>
  QUESTIONS.filter((q) => isRequired(q) && !answers[q.id]?.trim())

export const TIERS: { id: Tier; label: string }[] = [
  { id: 'must', label: 'Required' },
  { id: 'should', label: 'Recommended' },
  { id: 'nice', label: 'Nice to have' },
]

// Shown above the first question of each tier.
export const TIER_INTRO: Record<Tier, string> = {
  must: 'A few questions only you can answer.',
  should: "That's everything required. These optional questions sharpen the plan, so skip any you like.",
  nice: 'Last few nice-to-haves.',
}
