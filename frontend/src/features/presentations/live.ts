// Starting a live session for a deck. The host token returned by the backend is kept in sessionStorage
// so the presenter page (/present/:sessionId) can authenticate as the host.
import type { Api } from '../../api/client'
import type { SessionCreated } from '../../api/types'

/** sessionStorage key holding a live session's host token. */
export const hostTokenKey = (sessionId: string) => `evently.session.${sessionId}.host_token`

export function saveHostToken(sessionId: string, token: string): void {
  try {
    sessionStorage.setItem(hostTokenKey(sessionId), token)
  } catch {
    // Storage blocked (private mode…): the presenter page falls back to the owner's access token.
  }
}

/** Create a live session presenting `presentationId`; returns the new session id. */
export async function startLiveSession(api: Api, eventId: string, presentationId: string, title: string): Promise<string> {
  const created = await api.post<SessionCreated>(`/events/${encodeURIComponent(eventId)}/live-sessions`, {
    presentation_id: presentationId,
    title,
  })
  saveHostToken(created.session.id, created.host_token)
  return created.session.id
}
