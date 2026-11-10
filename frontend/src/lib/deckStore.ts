import { useEffect, useState } from 'react'
import { DEMO_DECK, DEMO_ID, type Deck } from '../data/demoDeck'

// Uploaded decks, kept in this browser's IndexedDB. Like the BroadcastChannel sync in live.ts, this
// only reaches tabs on this device; swap it for file storage (e.g. Supabase Storage) so phones can load slides.

const DB = 'evently-live'
const STORE = 'decks'

function open(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB, 1)
    req.onupgradeneeded = () => req.result.createObjectStore(STORE)
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
}

function run<T>(mode: IDBTransactionMode, op: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  return open().then(
    (db) =>
      new Promise<T>((resolve, reject) => {
        const req = op(db.transaction(STORE, mode).objectStore(STORE))
        req.onsuccess = () => resolve(req.result)
        req.onerror = () => reject(req.error)
      }),
  )
}

// Tells other tabs (and this one) that a deck was saved, so views showing it reload.
const CHANGES = 'evently-decks'

export async function saveDeck(id: string, deck: Deck): Promise<void> {
  await run('readwrite', (s) => s.put(deck, id))
  const ch = new BroadcastChannel(CHANGES)
  ch.postMessage(id)
  ch.close()
}

// Calls back with the id of every deck saved from now on, in any tab.
export function onDeckSaved(cb: (id: string) => void): () => void {
  const ch = new BroadcastChannel(CHANGES)
  ch.onmessage = ({ data }: MessageEvent<string>) => cb(data)
  return () => ch.close()
}

export async function loadDeck(id: string): Promise<Deck | null> {
  if (id === DEMO_ID) return DEMO_DECK
  try {
    return ((await run('readonly', (s) => s.get(id))) as Deck | undefined) ?? null
  } catch {
    // IndexedDB can be unavailable (some private windows).
    return null
  }
}

// undefined while loading, null when this device doesn't have the deck.
export function useDeck(id: string): Deck | null | undefined {
  const [deck, setDeck] = useState<{ id: string; deck: Deck | null } | null>(null)
  useEffect(() => {
    let live = true
    const load = () => loadDeck(id).then((d) => live && setDeck({ id, deck: d }))
    load()
    const off = onDeckSaved((saved) => saved === id && load())
    return () => {
      live = false
      off()
    }
  }, [id])
  return deck?.id === id ? deck.deck : undefined
}

export const newDeckId = () => crypto.randomUUID().slice(0, 8)

// The deck behind one option of a live-slides step, e.g. the hackathon's "Kickoff deck".
const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'deck'
export const optionDeckId = (planId: string, taskId: string, title: string) => `${planId}-${taskId}-${slug(title)}`

// Several decks by id, kept current as any tab saves one. Missing decks map to null.
export function useDecks(ids: string[]): Record<string, Deck | null> {
  const [decks, setDecks] = useState<Record<string, Deck | null>>({})
  const key = ids.join('|')
  useEffect(() => {
    let live = true
    const want = key ? key.split('|') : []
    const load = (id: string) => loadDeck(id).then((d) => live && setDecks((all) => ({ ...all, [id]: d })))
    want.forEach(load)
    const off = onDeckSaved((saved) => want.includes(saved) && load(saved))
    return () => {
      live = false
      off()
    }
  }, [key])
  return decks
}
