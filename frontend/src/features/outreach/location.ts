// Which backend event the outreach panel should show: set explicitly, or read from the URL (/events/:eventId).
const CHANGE = 'outreach:locationchange'
let override: string | null = null
const listeners = new Set<() => void>()

/** Point the panel at an event explicitly (null goes back to reading the URL). */
export function setOutreachEventId(id: string | null) {
  override = id
  listeners.forEach((l) => l())
}

let patched = false
/** react-router navigates with history.pushState, which fires no event; announce it. */
export function patchHistory() {
  if (patched) return
  patched = true
  for (const method of ['pushState', 'replaceState'] as const) {
    const original = history[method]
    history[method] = function (this: History, ...args: Parameters<History['pushState']>) {
      const out = original.apply(this, args)
      window.dispatchEvent(new Event(CHANGE))
      return out
    }
  }
}

export function subscribeLocation(onChange: () => void) {
  listeners.add(onChange)
  window.addEventListener('popstate', onChange)
  window.addEventListener(CHANGE, onChange)
  return () => {
    listeners.delete(onChange)
    window.removeEventListener('popstate', onChange)
    window.removeEventListener(CHANGE, onChange)
  }
}

const eventIdFromUrl = (): string | null => {
  const m = /^\/events\/([^/]+)/.exec(window.location.pathname)
  return m ? decodeURIComponent(m[1]) : null
}
export const outreachEventId = () => override ?? eventIdFromUrl()
