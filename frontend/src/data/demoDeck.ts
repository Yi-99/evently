// Decks for live slides: either built-in text slides (the sample deck) or images from an uploaded file.

export type SlideData =
  | { kind: 'cover' | 'statement'; title: string; subtitle?: string }
  | { kind: 'list'; title: string; points: string[] }
  // One page of an uploaded PDF, or one uploaded image.
  | { kind: 'image'; image: Blob; alt: string }

export interface Deck {
  title: string
  slides: SlideData[]
}

export const DEMO_ID = 'demo'

// Sample deck for the live slides demo. Later this comes from the Presentation agent's output.
export const DEMO_DECK: Deck = {
  title: 'Evently Hack 2026',
  slides: [
    { kind: 'cover', title: 'Evently Hack 2026', subtitle: 'Welcome! Find a seat — kickoff starts at 6:00 pm.' },
    {
      kind: 'list',
      title: 'Tonight',
      points: ['6:00 pm  Kickoff and rules', '6:30 pm  Team matching', '7:30 pm  Dinner', '8:00 pm  Hacking starts'],
    },
    {
      kind: 'list',
      title: 'Before you start',
      points: ['Wi-Fi: EventlyHack / build2026', 'Help desk is by the main entrance', 'Quiet room on floor 2', 'Be kind — code of conduct is on the badge'],
    },
    {
      kind: 'list',
      title: 'Tracks and prizes',
      points: ['Best for your city — $3,000', 'Best use of AI — $2,000', 'Best hardware hack — $1,500', 'People’s choice — swag bundle'],
    },
    {
      kind: 'list',
      title: 'How judging works',
      points: ['3-minute demo, 2 minutes of questions', 'Impact, craft and creativity', 'Submit by 12:00 pm on Sunday'],
    },
    { kind: 'statement', title: 'Start hacking.', subtitle: 'Mentors are walking the floor — wave one down any time.' },
  ],
}
