// Backend calls for the event lifecycle: create, list, load, and debounced requirement saves.
import { useEffect, useRef, useState } from 'react'
import { api } from '../../api/client'
import type { EventOut } from '../../api/types'
import type { EventRequirements } from './requirements'

const path = (id: string) => `/events/${encodeURIComponent(id)}`

// EventCreate / RequirementsPatch in schema.d.ts predate the `intake` / `plan` keys, so bodies are typed here.
export const createEvent = (name: string, description: string, requirements: EventRequirements) =>
  api.post<EventOut>('/events', { name, description, requirements })

export const getEvent = (id: string) => api.get<EventOut>(path(id))

export const listEvents = () => api.get<EventOut[]>('/events')

/** Merge `requirements` into the event's (a null value removes that key); optionally rename it. */
export const patchEvent = (id: string, body: { name?: string; requirements?: Record<string, unknown> }) =>
  api.patch<EventOut>(path(id), body)

/** The caller's backend events, newest first (null while loading; empty when `enabled` is false or on error). */
export function useEventList(enabled: boolean): EventOut[] | null {
  const [events, setEvents] = useState<EventOut[] | null>(null)
  useEffect(() => {
    if (!enabled) return
    let live = true
    listEvents().then(
      (rows) => live && setEvents(rows),
      () => live && setEvents([]),
    )
    return () => {
      live = false
    }
  }, [enabled])
  return enabled ? events : []
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)

export interface RequirementsSync {
  /** Record the server's full requirements (after create / load / a remote update), so they aren't sent back. */
  markSaved: (req: Record<string, unknown>, name?: string) => void
  /** Last values known to be on the server. */
  saved: () => Record<string, unknown>
  /** True while a save is scheduled or in flight (incoming echoes are ignored then). */
  busy: () => boolean
}

/**
 * Debounced PATCH /events/{id} of the keys of `desired` that differ from what the server last saw, plus the
 * event name when `name` is non-null and changed. Saves run one at a time; a failed save retries on the next change.
 */
export function useRequirementsSync(
  eventId: string | null,
  desired: EventRequirements | null,
  name: string | null,
  delay = 600,
): RequirementsSync {
  const saved = useRef<Record<string, unknown>>({})
  const savedName = useRef<string | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const queue = useRef<Promise<unknown>>(Promise.resolve())
  const flying = useRef(0)
  const json = desired ? JSON.stringify(desired) : ''

  useEffect(() => {
    if (!eventId || !json) return
    const next = JSON.parse(json) as Record<string, unknown>
    const diff = Object.fromEntries(Object.entries(next).filter(([k, v]) => !same(saved.current[k], v)))
    const rename = name !== null && savedName.current !== null && savedName.current !== name ? name : undefined
    if (!Object.keys(diff).length && !rename) return
    timer.current = setTimeout(() => {
      timer.current = null
      flying.current++
      queue.current = queue.current
        .then(() => patchEvent(eventId, { name: rename, requirements: diff }))
        .then(
          () => {
            Object.assign(saved.current, diff)
            if (rename) savedName.current = rename
          },
          () => undefined,
        )
        .finally(() => flying.current--)
    }, delay)
    return () => {
      if (timer.current) clearTimeout(timer.current)
      timer.current = null
    }
  }, [eventId, json, name, delay])

  // Stable object; the refs carry the state.
  const [sync] = useState<RequirementsSync>(() => ({
    markSaved: (req, n) => {
      saved.current = { ...req }
      if (n !== undefined) savedName.current = n
    },
    saved: () => saved.current,
    busy: () => timer.current !== null || flying.current > 0,
  }))
  return sync
}
