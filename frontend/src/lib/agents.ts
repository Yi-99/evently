import type { EventDetails, Task, TaskResult } from '../types'

// Sample agent output. Every name, contact and number here is made up; only the event city,
// date and headcount come from the user. Swap `runAgent` for a call to the backend
// (e.g. POST /api/agents/:taskId) once the agent service exists.

const when = (ev: EventDetails) => ev.date || 'your event dates'
const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, '')
const pin = (i: number) => ({ x: 15 + ((i * 37) % 70), y: 20 + ((i * 53) % 60) })

function venues(ev: EventDetails): TaskResult[] {
  const { city, headcount } = ev
  const rows: [string, string, number, string, string, number, string][] = [
    [`${city} Innovation Hub`, 'Market St', 4.7, '1.2 mi', '$4,500 / day', 1.2, '24h access, gigabit Wi-Fi and 8 breakout rooms.'],
    ['The Foundry Workspace', 'SoMa', 4.5, '2.8 mi', '$3,200 / day', 1, 'Built-in stage and AV for demos, full kitchen for catering.'],
    ['Riverside Commons', 'Embarcadero', 4.6, '3.1 mi', '$2,800 / day', 0.9, 'Waterfront hall with an outdoor patio for breaks.'],
    ['Campus Union Hall', 'University District', 4.4, '4.0 mi', '$1,900 / day', 1.5, 'Student discount and lecture halls for workshops.'],
    ['Hangar 9 Event Space', 'Dogpatch', 4.3, '5.2 mi', '$3,900 / day', 2, 'Open warehouse floor; fits large teams and sponsor booths.'],
    ['Library Annex Forum', 'Civic Center', 4.5, '1.8 mi', '$1,200 / day', 0.7, 'Low cost, quiet rooms, but closes at 10pm.'],
    ['Pier 3 Loft', 'Waterfront', 4.2, '3.6 mi', '$2,400 / day', 0.8, 'Views and natural light; Wi-Fi needs a boost for this group.'],
    ['Makers Guild Studio', 'Mission', 4.8, '2.1 mi', '$2,100 / day', 0.6, 'Hardware lab and 3D printers for hardware tracks.'],
    ['Summit Conference Center', 'Downtown', 4.1, '0.9 mi', '$6,800 / day', 2.5, 'Hotel-attached, overnight security included.'],
    ['Coworking Collective', 'Hayes Valley', 4.6, '2.4 mi', '$2,600 / day', 0.9, 'Weekend buyout available; desks and phone booths.'],
  ]
  return rows.map(([title, area, rating, distance, price, fit, why], i) => ({
    title,
    subtitle: `${area}, ${city}`,
    address: `${area}, ${city}`,
    rating,
    distance,
    price,
    capacity: `Fits ${Math.round(headcount * fit)}`,
    pin: pin(i),
    why,
    contact: { name: 'Events team', email: `events@${slug(title)}.example`, phone: '(555) 010-' + String(1200 + i * 7) },
    todo: [
      `Confirm availability for ${when(ev)}`,
      `Check capacity for ${headcount} people, overnight`,
      'Ask for Wi-Fi bandwidth and power outlet count',
      'Request a quote and the cancellation policy',
      'Book a site visit',
    ],
    draft: `Hi ${title} team, we're organizing a ${headcount}-person hackathon in ${city} on ${when(ev)}. Is the space available overnight, and could you share pricing, Wi-Fi details and your cancellation policy? We'd love to book a site visit.`,
  }))
}

function caterers(ev: EventDetails): TaskResult[] {
  const { city, headcount } = ev
  const rows: [string, string, number, string, string, string][] = [
    ['Harvest Table Catering', 'Mission', 4.8, '0.6 mi', '$18–24 / person', 'Vegan and gluten-free buffet; handles 300+.'],
    ['Night Owl Pizza Co.', 'SoMa', 4.6, '0.9 mi', '$11 / person', 'Midnight delivery for the overnight crowd.'],
    ['Greenleaf Bowls', 'Downtown', 4.5, '1.4 mi', '$15 / person', 'Halal and gluten-free bowls, individually labeled.'],
    ['Bean & Byte Coffee Cart', 'Market St', 4.9, '0.3 mi', '$6 / person', 'On-site barista, runs 24 hours.'],
    ['Sunrise Breakfast Club', 'Hayes Valley', 4.4, '2.0 mi', '$12 / person', 'Breakfast trays for the morning after.'],
    ['Taco Truck Collective', 'Mission', 4.7, '1.1 mi', '$13 / person', 'Parks outside; fun dinner option with no setup.'],
  ]
  return rows.map(([title, area, rating, distance, price, why], i) => ({
    title,
    subtitle: `${area}, ${city}`,
    address: `${area}, ${city}`,
    rating,
    distance,
    price,
    pin: pin(i + 3),
    why,
    contact: { name: 'Catering manager', email: `orders@${slug(title)}.example` },
    todo: [
      `Get a quote for ${headcount} people`,
      'Collect dietary restrictions from registration',
      'Confirm delivery windows and setup needs',
      'Ask about leftovers and compostable serviceware',
    ],
    draft: `Hello! We're hosting a ${headcount}-person hackathon in ${city} on ${when(ev)}. Could you quote meals for the group, including vegetarian and gluten-free options, and tell us your delivery windows?`,
  }))
}

function sponsors(ev: EventDetails): TaskResult[] {
  const rows: [string, string, string, string, string][] = [
    ['Nimbus Cloud', 'Cloud hosting', 'Gold · $10k', 'Priya Nair', 'Runs a student credits program and sponsors 20+ hackathons a year.'],
    ['Patchwork API', 'Developer APIs', 'Silver · $5k', 'Marcus Lee', 'Wants API adoption; a "best use of" prize fits well.'],
    ['Lumen Analytics', 'Data tools', 'Silver · $5k', 'Dana Ortiz', 'Recently launched a free tier and is looking for feedback.'],
    ['Forge CI', 'DevOps', 'Bronze · $2.5k', 'Sam Whitaker', 'Sponsored two local hackathons last year.'],
    ['Quill Docs', 'Docs platform', 'Bronze · $2.5k', 'Hannah Cho', 'Offers swag and judges instead of cash.'],
    ['Atlas Maps API', 'Location data', 'In kind', 'Leo Brandt', 'Offers API credits as prizes.'],
  ]
  return rows.map(([title, sector, tier, name, why]) => ({
    title,
    subtitle: sector,
    price: tier,
    meta: sector,
    why,
    contact: { name, role: 'Developer relations', email: `${name.split(' ')[0].toLowerCase()}@${slug(title)}.example` },
    todo: [
      `Send the sponsorship deck with ${tier.split(' · ')[0]} tier benefits`,
      'Propose a sponsor prize track',
      'Ask for mentors or judges',
      'Collect logo and swag shipping details',
    ],
    draft: `Hi ${name.split(' ')[0]}, we're running a ${ev.headcount}-person hackathon in ${ev.city} on ${when(ev)}. ${title} would be a great fit as a ${tier} sponsor; we can feature a dedicated prize track. Could I send over our deck?`,
  }))
}

function judges(ev: EventDetails): TaskResult[] {
  const rows: [string, string, string][] = [
    ['Alex Rivera', 'Staff engineer', 'Has judged 10+ hackathons and mentors students.'],
    ['Jordan Kim', 'Founder', 'Ran a startup accelerator, so strong on pitch feedback.'],
    ['Taylor Brooks', 'Design lead', 'Covers UX and design for the "best design" prize.'],
    ['Morgan Patel', 'ML researcher', 'Fits AI-track submissions.'],
    ['Casey Nguyen', 'Product manager', 'Brings a user-impact lens to judging.'],
    ['Riley Chen', 'Security engineer', 'Can judge security and infrastructure projects.'],
  ]
  return rows.map(([title, role, why]) => ({
    title,
    subtitle: role,
    meta: role,
    why,
    contact: { name: title, role, email: `${title.split(' ')[0].toLowerCase()}@example.com` },
    todo: ['Send the invite with the judging time slot', 'Share the judging rubric', 'Confirm travel or parking needs', 'Add to the demo-day calendar'],
    draft: `Hi ${title.split(' ')[0]}, would you be a judge at our hackathon in ${ev.city} on ${when(ev)}? Judging takes about 3 hours on the final afternoon. We'd be honored to have you.`,
  }))
}

function simple(rows: [string, string, string, string[]][]): TaskResult[] {
  return rows.map(([title, subtitle, why, todo]) => ({ title, subtitle, meta: subtitle, why, todo }))
}

function byTask(task: Task, ev: EventDetails): TaskResult[] | null {
  const n = ev.headcount
  switch (task.id) {
    case 'venue':
      return venues(ev)
    case 'catering':
      return caterers(ev)
    case 'sponsors':
      return sponsors(ev)
    case 'judges':
      return judges(ev)
    case 'prizes':
      return simple([
        ['Grand prize', '$3,000 + sponsor credits', 'Headline prize to draw strong teams.', ['Confirm funding from Gold sponsors', 'Order the trophy', 'Write judging criteria']],
        ['Best beginner team', '$1,000', 'Keeps first-timers motivated.', ['Define "beginner"', 'Pick one judge to own it']],
        ['Best use of a sponsor API', 'Sponsor-funded', 'One per API sponsor.', ['Get each sponsor to name their prize', 'Add the tracks to the website']],
        ['People’s choice', 'Swag bundle', 'Audience vote at demos.', ['Set up a voting QR code', 'Source the swag bundle']],
      ])
    case 'registration':
      return simple([
        ['Luma event page', 'Free · on Luma', 'Sign-ups, waitlist and reminder emails on Luma. Guests sync back into the plan.', ['Draft the event page from the plan', 'Add team-matching questions', `Set a waitlist at ${n}`, 'Publish the event page']],
        ['Evently registration form', 'Free · built-in', 'Feeds headcount and dietary info into the other agents.', ['Publish the form', 'Add team-matching questions', `Set a waitlist at ${n}`]],
        ['Paid ticket page', '$10 refundable deposit', 'Cuts no-shows, typically by 30–40%.', ['Set the refund policy', 'Connect payouts']],
        ['Invite-only list', 'Application review', 'Best when demand far exceeds capacity.', ['Write the application questions', 'Schedule review sessions']],
      ])
    case 'ticketing':
      return simple([
        ['Luma ticketed event', 'Paid tiers on Luma', 'Early-bird, regular and speaker tickets, with QR check-in at the door.', ['Draft the event page from the plan', 'Set ticket tiers and prices', 'Create speaker and sponsor promo codes', 'Publish the event page']],
        ['Luma free registration', 'Approval required', 'Free tickets with a manual review, when the room is small.', ['Draft the event page from the plan', 'Write the approval questions', 'Publish the event page']],
      ])
    case 'av':
      return simple([
        ['Venue in-house AV', 'Included with some venues', 'Cheapest when the venue has a stage setup.', ['Confirm projector and mics with the venue']],
        ['Pop-up Wi-Fi rental', `~$800 for ${n} devices`, `Plan for about 3 devices per person (${n * 3}).`, ['Get a bandwidth quote', 'Schedule install the day before']],
        ['Power and extension kit', '$250 rental', 'One power strip per table.', ['Count the tables', 'Order strips and tape']],
        ['Livestream crew', '$1,200 / day', 'Records demos for sponsors.', ['Decide whether to stream', 'Book the crew']],
      ])
    case 'schedule':
      return simple([
        ['Kickoff & team forming', 'Day 1 · 6:00 pm', 'Opening talk, rules and team matching.', ['Book the opening speaker', 'Prepare the slides']],
        ['Workshops', 'Day 1 · 8:00 pm', 'Sponsor-led intro sessions.', ['Assign rooms', 'Collect sponsor topics']],
        ['Midnight snack', 'Day 2 · 12:00 am', 'Keeps energy up overnight.', ['Confirm the late-night delivery']],
        ['Submission deadline', 'Day 2 · 12:00 pm', 'Hard cutoff on the submission portal.', ['Set up the submission portal', 'Send reminders']],
        ['Demos & judging', 'Day 2 · 1:00 pm', 'Science-fair style, then finalists on stage.', ['Make the judging schedule', 'Test the stage AV']],
        ['Awards', 'Day 2 · 4:30 pm', 'Prizes and closing.', ['Print certificates', 'Prepare prize handoffs']],
      ])
    case 'brief':
      return simple([
        ['Theme: Build for your city', 'Recommended', 'Local impact tends to draw sponsors and press.', ['Confirm the theme with your approver', 'Draft the one-line pitch']],
        ['Theme: Open AI track', 'Popular', 'Lots of demand right now; pairs well with API sponsors.', ['Pick sub-tracks', 'Line up AI mentors']],
        ['Theme: No theme', 'Simplest', 'Most freedom; judging is harder.', ['Write broader judging criteria']],
      ])
    case 'dayof':
      return simple([
        ['Volunteer roster', '12 shifts', 'Check-in, food, help desk and cleanup.', ['Recruit volunteers', 'Send the shift sheet']],
        ['Run sheet', 'Minute-by-minute', 'One page for the organizing team.', ['Share with leads', 'Do a walk-through the day before']],
      ])
    case 'slides':
      return ev.type === 'conference'
        ? simple([
            ['Session decks', 'One per talk', 'Each speaker presents from Evently, so every room gets its own QR code.', ['Collect PDFs from speakers', 'Print a QR poster per room', 'Test the projector in each hall']],
            ['Opening keynote deck', 'Draft ready', 'Welcome, schedule, Wi-Fi and sponsors.', ['Review the slides', 'Rehearse the keynote']],
          ])
        : simple([
            ['Kickoff deck', 'Draft ready', 'Welcome, schedule, Wi-Fi, prizes and judging, drafted from your plan.', ['Review the slides', 'Print QR posters for the room', 'Test the projector']],
            ['Demo-day deck', 'Order of teams', 'One slide per team, so the room knows who’s up next.', ['Collect team names after submissions', 'Add a slide per team']],
          ])
    case 'presentation':
      return simple([
        ['Agenda deck', 'Draft ready', 'Objectives, agenda and logistics, drafted from your plan.', ['Review the slides', 'Add the speakers’ sections', 'Share the QR code at the start']],
        ['Your own deck', 'Upload a PDF', 'Already have slides? Present them from Evently so the room can follow along.', ['Export the deck as PDF', 'Upload it with the Upload button above']],
      ])
    case 'memories':
      return simple([
        ['Photo slideshow', 'Upload photos', 'Collect old photos and play them as a slideshow everyone can follow on their phones.', ['Ask guests to send photos', 'Upload them, one per slide', 'Put the QR code on the tables']],
      ])
  }
  return null
}

// Fallback for steps without a tailored list: a few options from the step's integration.
function generic(task: Task, ev: EventDetails): TaskResult[] {
  if (task.integration === 'google-maps') return /cater|food/i.test(task.title) ? caterers(ev) : venues(ev).slice(0, 6)
  return ['A', 'B', 'C', 'D'].slice(0, 3 + (task.title.length % 2)).map((l) => ({
    title: `${task.title}: option ${l}`,
    subtitle: 'Found by web search',
    meta: 'Sample result',
    why: `A good match for ${ev.headcount} people in ${ev.city}.`,
    todo: ['Review the details', 'Confirm cost and timing', 'Approve or skip'],
  }))
}

function mockResults(task: Task, ev: EventDetails): TaskResult[] {
  return byTask(task, ev) ?? generic(task, ev)
}

export function runAgent(task: Task, ev: EventDetails): Promise<TaskResult[]> {
  const ms = 900 + Math.random() * 1600
  return new Promise((resolve) => setTimeout(() => resolve(mockResults(task, ev)), ms))
}
