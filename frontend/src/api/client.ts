// Typed fetch wrapper for the FastAPI backend.
//
//   api.get<EventOut[]>('/events')                 → GET  /api/v1/events
//   api.post<EventOut>('/events', { name: 'Gala' }) → POST /api/v1/events (JSON body)
//   api.get('/api/v1/health')                       → paths starting with /api/ or /ws/ are used as-is
//
// The base URL comes from VITE_API_URL; it defaults to "" so requests go through the Vite dev proxy.
import { getToken, setSession } from './token'

export const API_BASE: string = (import.meta.env.VITE_API_URL ?? '').replace(/\/+$/, '')
export const API_PREFIX = '/api/v1'

export type Query = Record<string, string | number | boolean | null | undefined>

export interface ApiInit extends Omit<RequestInit, 'body'> {
  /** Raw body (FormData, Blob, string…). Prefer `json`. */
  body?: BodyInit | null
  /** Serialised as JSON with `Content-Type: application/json`. */
  json?: unknown
  /** Appended as a query string; null/undefined values are dropped. */
  query?: Query
  /** Send the stored bearer token (default true). */
  auth?: boolean
}

/** A non-2xx response. `detail` is FastAPI's `detail` (a string, or a validation error list for 422). */
export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown
  readonly path: string

  constructor(status: number, detail: unknown, path: string) {
    super(ApiError.describe(status, detail))
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.path = path
  }

  static describe(status: number, detail: unknown): string {
    if (typeof detail === 'string' && detail) return detail
    if (Array.isArray(detail))
      return detail
        .map((d: { loc?: unknown[]; msg?: string }) => [d.loc?.slice(1).join('.'), d.msg].filter(Boolean).join(': '))
        .join('; ')
    if (status === 0) return 'Could not reach the Evently server.'
    return `Request failed (${status})`
  }
}

export const isApiError = (e: unknown, status?: number): e is ApiError =>
  e instanceof ApiError && (status === undefined || e.status === status)

/** Full URL for an API path: '/events' → '<base>/api/v1/events'. */
export function apiUrl(path: string, query?: Query): string {
  const p = path.startsWith('/api/') || path.startsWith('/ws/') ? path : `${API_PREFIX}${path.startsWith('/') ? '' : '/'}${path}`
  let url = `${API_BASE}${p}`
  if (query) {
    const qs = new URLSearchParams()
    for (const [k, v] of Object.entries(query)) if (v !== null && v !== undefined) qs.set(k, String(v))
    const s = qs.toString()
    if (s) url += (url.includes('?') ? '&' : '?') + s
  }
  return url
}

export async function apiFetch<T = unknown>(path: string, init: ApiInit = {}): Promise<T> {
  const { json, query, auth = true, headers: extra, body, ...rest } = init
  const headers = new Headers(extra)
  if (!headers.has('Accept')) headers.set('Accept', 'application/json')
  const token = auth ? getToken() : null
  if (token) headers.set('Authorization', `Bearer ${token}`)
  let payload = body
  if (json !== undefined) {
    headers.set('Content-Type', 'application/json')
    payload = JSON.stringify(json)
  }

  let res: Response
  try {
    res = await fetch(apiUrl(path, query), { ...rest, headers, body: payload })
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e
    throw new ApiError(0, e instanceof Error ? e.message : null, path)
  }

  const type = res.headers.get('content-type') ?? ''
  const parsed: unknown =
    res.status === 204 || res.status === 205
      ? undefined
      : type.includes('json')
        ? await res.json().catch(() => undefined)
        : await res.text().catch(() => '')

  if (!res.ok) {
    // An expired or revoked token: forget it so the app can sign in again.
    if (res.status === 401 && token) setSession({ token: null, user: null })
    const detail = parsed && typeof parsed === 'object' && 'detail' in parsed ? (parsed as { detail: unknown }).detail : parsed
    throw new ApiError(res.status, detail, path)
  }
  return parsed as T
}

type NoBody = Omit<ApiInit, 'json' | 'method'>

export const api = {
  get: <T = unknown>(path: string, init?: NoBody) => apiFetch<T>(path, { ...init, method: 'GET' }),
  post: <T = unknown>(path: string, json?: unknown, init?: NoBody) => apiFetch<T>(path, { ...init, method: 'POST', json }),
  patch: <T = unknown>(path: string, json?: unknown, init?: NoBody) => apiFetch<T>(path, { ...init, method: 'PATCH', json }),
  put: <T = unknown>(path: string, json?: unknown, init?: NoBody) => apiFetch<T>(path, { ...init, method: 'PUT', json }),
  del: <T = void>(path: string, init?: NoBody) => apiFetch<T>(path, { ...init, method: 'DELETE' }),
}

export type Api = typeof api

/**
 * WebSocket URL for a backend path ('/ws/events/evt_1?since=3'), derived from VITE_API_URL or the page origin.
 * The backend reads the token from `?token=` (browsers can't set WS headers). Pass `token` to override the
 * stored user token (e.g. a live-session participant/host token), or `null` to send none.
 */
export function wsUrl(path: string, token?: string | null): string {
  const base = API_BASE || (typeof location !== 'undefined' ? location.origin : 'http://localhost:5173')
  const url = new URL(path.startsWith('/') ? path : `/${path}`, base)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  const t = token === undefined ? getToken() : token
  if (t) url.searchParams.set('token', t)
  return url.toString()
}
