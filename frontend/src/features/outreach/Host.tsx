import { useSyncExternalStore } from 'react'
import { outreachEventId, subscribeLocation } from './location'
import OutreachPanel from './OutreachPanel'

export default function Host() {
  const eventId = useSyncExternalStore(subscribeLocation, outreachEventId, () => null)
  return eventId ? <OutreachPanel key={eventId} eventId={eventId} /> : null
}
