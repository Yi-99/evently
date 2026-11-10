import type { EventDetails, Task, TaskResult } from '../types'
import { DEMO_DECK, type Deck, type SlideData } from './demoDeck'
import { EVENT_TYPES } from './workflows'

// The deck an agent drafts for one option of a live-slides step, or null when the option is
// for the user's own slides (they upload them). Placeholder copy until the agent writes real
// slides; only the event type, city, headcount and date come from the user.
export function draftDeck(event: EventDetails, task: Task, option: TaskResult): Deck | null {
  const label = EVENT_TYPES.find((e) => e.id === event.type)!.label
  const when = event.date || 'Date to be confirmed'
  const cover = (title: string, subtitle: string): SlideData => ({ kind: 'cover', title, subtitle })
  const deck = (...slides: SlideData[]): Deck => ({ title: option.title, slides })

  switch (`${task.id}:${option.title}`) {
    case 'slides:Kickoff deck':
      return DEMO_DECK
    case 'slides:Demo-day deck':
      return deck(
        cover('Demo day', `${event.city} · ${when}`),
        { kind: 'list', title: 'How demos work', points: ['3 minutes to demo, 2 for questions', 'Teams present in submission order', 'Judges score impact, craft and creativity'] },
        { kind: 'statement', title: 'Team slides go here.', subtitle: 'Add one slide per team once submissions close.' },
      )
    case 'slides:Opening keynote deck':
      return deck(
        cover(`Welcome to ${event.city}`, `${when} · ${event.headcount} attendees`),
        { kind: 'list', title: 'Today', points: ['9:00 am  Opening keynote', '10:30 am  Tracks begin', '12:30 pm  Lunch', '5:00 pm  Closing and drinks'] },
        { kind: 'list', title: 'Getting around', points: ['Wi-Fi details are on your badge', 'Track rooms are on the venue map', 'Questions? Find a volunteer in a blue shirt'] },
        { kind: 'statement', title: 'Enjoy the talks.', subtitle: 'Scan the QR code in each room to follow the slides.' },
      )
    case 'presentation:Agenda deck':
      return deck(
        cover(`${label} event`, `${event.city} · ${when}`),
        { kind: 'list', title: 'Why we’re here', points: ['Where we are today', 'What we want to decide', 'What happens next'] },
        { kind: 'list', title: 'Agenda', points: ['Welcome and goals', 'Main session', 'Break', 'Discussion and next steps'] },
        { kind: 'statement', title: 'Thank you.', subtitle: 'Slides and notes will follow by email.' },
      )
    case 'memories:Photo slideshow':
      return deck(cover('Welcome back', `${event.city} · ${when}`), {
        kind: 'statement',
        title: 'Photos coming soon.',
        subtitle: 'Upload old photos and they’ll play here, one per slide.',
      })
  }
  return null
}
