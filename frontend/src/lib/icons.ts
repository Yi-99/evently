import {
  Archive, Bot, Building, Calendar, CalendarClock, ClipboardCheck, ClipboardList, Clapperboard, Contact, Globe, Handshake,
  Hotel, ListChecks, Mail, MapPin, Mic, Network, Presentation, QrCode, Send, Ticket, Store, Trophy, UserPlus, UserSearch, Users, UtensilsCrossed, Wifi,
  type LucideIcon,
} from 'lucide-react'
import calendarLogo from '../assets/google/calendar.png'
import contactsLogo from '../assets/google/contacts.png'
import gmailLogo from '../assets/google/gmail.png'
import mapsLogo from '../assets/google/maps.png'
import slidesLogo from '../assets/google/slides.png'
import linkedinLogo from '../assets/brands/linkedin.svg'
import lumaLogo from '../assets/brands/luma.png'
import type { Integration } from '../types'

// Each service's own logo, for anywhere it's named: Google's from gstatic.com, Luma's app icon from
// luma.com, and a LinkedIn "in" mark drawn to match theirs. Web search has no logo and uses the globe icon.
export const INTEGRATION_LOGO: Partial<Record<Integration, string>> = {
  'google-maps': mapsLogo,
  gmail: gmailLogo,
  'google-calendar': calendarLogo,
  'google-contacts': contactsLogo,
  'google-slides': slidesLogo,
  linkedin: linkedinLogo,
  luma: lumaLogo,
}

export const INTEGRATION_ICON: Record<Integration, LucideIcon> = {
  'google-maps': MapPin,
  gmail: Mail,
  'google-calendar': Calendar,
  'google-contacts': Contact,
  'google-slides': Presentation,
  'live-slides': QrCode,
  linkedin: Network,
  luma: Ticket,
  web: Globe,
}

const AGENT_ICON: Record<string, LucideIcon> = {
  Planner: ClipboardList,
  'Venue Scout': Building,
  Caterer: UtensilsCrossed,
  'Sponsor Hunter': Handshake,
  Registrar: ClipboardCheck,
  'Tech Ops': Wifi,
  'Prize Designer': Trophy,
  Recruiter: UserPlus,
  Scheduler: CalendarClock,
  Coordinator: ListChecks,
  'Guest Manager': Users,
  'Guest Finder': UserSearch,
  'Deck Builder': Presentation,
  Outreach: Send,
  'Speaker Scout': Mic,
  'Travel Agent': Hotel,
  'Vendor Scout': Store,
  'Stage Manager': Clapperboard,
  Archivist: Archive,
}

export const agentIcon = (name: string): LucideIcon => AGENT_ICON[name] ?? Bot
