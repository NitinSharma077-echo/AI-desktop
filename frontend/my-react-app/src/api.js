/* The only module that knows how to reach the server.
 *
 * Tokens live in localStorage, which is readable by any script on this origin.
 * That trades XSS resistance for surviving a page reload -- an acceptable trade
 * only because this page loads no third-party script. If that ever changes,
 * move the refresh token to an httpOnly cookie.
 */

/* Where the API lives.
 *
 * Empty means same-origin, which is how the bundled build is served -- FastAPI
 * hands out dist/ itself, so /chat resolves to the same process.
 *
 * A split deployment (static frontend on one host, API on another) must set
 * VITE_API_ORIGIN, and must set it at BUILD time: Vite inlines import.meta.env
 * into the bundle, so there is no runtime knob to turn afterwards. Getting this
 * wrong is quiet -- relative paths resolve against the frontend's own origin and
 * every call 404s against a static file server.
 *
 * The backend must also name that frontend in CORS_ALLOW_ORIGINS, or the browser
 * blocks the response before any of this code sees it.
 */
const API_ORIGIN = (import.meta.env.VITE_API_ORIGIN || '').replace(/\/+$/, '')
const url = (path) => `${API_ORIGIN}${path}`

const ACCESS = 'access_token'
const REFRESH = 'refresh_token'

export const tokens = {
  get access() {
    return localStorage.getItem(ACCESS)
  },
  get refresh() {
    return localStorage.getItem(REFRESH)
  },
  save({ access_token, refresh_token }) {
    if (access_token) localStorage.setItem(ACCESS, access_token)
    if (refresh_token) localStorage.setItem(REFRESH, refresh_token)
  },
  clear() {
    localStorage.removeItem(ACCESS)
    localStorage.removeItem(REFRESH)
  },
}

/** Turn any error response into one line worth showing a person. */
export async function errorText(res) {
  try {
    const body = await res.json()
    if (typeof body.detail === 'string') return body.detail
    // FastAPI validation errors arrive as a list of {loc, msg, type}.
    if (Array.isArray(body.detail)) return body.detail.map((d) => d.msg).join('; ')
    return JSON.stringify(body)
  } catch {
    return `${res.status} ${res.statusText}`
  }
}

export class SessionExpired extends Error {
  constructor() {
    super('Session expired. Please sign in again.')
  }
}

/* One place that knows about tokens. On a 401 it spends the refresh token and
 * replays the request once; a second failure means the session is genuinely
 * over, so it throws SessionExpired rather than leaving the UI half
 * authenticated and failing on every subsequent click. */
async function authed(path, options = {}, retry = true) {
  const headers = new Headers(options.headers || {})
  if (tokens.access) headers.set('Authorization', `Bearer ${tokens.access}`)

  const res = await fetch(url(path), { ...options, headers })
  if (res.status !== 401 || !retry || !tokens.refresh) return res

  const refreshed = await fetch(url('/auth/refresh'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: tokens.refresh }),
  })
  if (!refreshed.ok) {
    tokens.clear()
    throw new SessionExpired()
  }
  tokens.save(await refreshed.json())
  return authed(path, options, false)
}

async function expectOk(res) {
  if (!res.ok) throw new Error(await errorText(res))
  return res
}

/* -------------------------------------------------------------- auth */

/**
 * Exchange a username and password for a token pair, and store it.
 *
 * Form-encoded, not JSON: /auth/token implements the OAuth2 password flow,
 * which specifies form fields. Sending JSON here gets a 422.
 */
export async function login(username, password) {
  const res = await fetch(url('/auth/token'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ username, password }),
  })
  await expectOk(res)
  const pair = await res.json()
  tokens.save(pair)
  return pair
}

/** Create an account. Returns the user; it does not sign them in. */
export async function register(username, password) {
  const res = await fetch(url('/auth/register'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  await expectOk(res)
  return res.json()
}

/**
 * The signed-in account, or null.
 *
 * Null for every "not signed in" reason -- no token, expired past refresh, or a
 * token signed with a key this server no longer has. The caller only needs to
 * know whether to show the sign-in screen.
 */
export async function me() {
  if (!tokens.access) return null
  try {
    const res = await authed('/auth/me')
    return res.ok ? await res.json() : null
  } catch {
    return null
  }
}

export function logout() {
  tokens.clear()
}

/* ------------------------------------------------------------ system */

/** Provider configuration and whether auth is switched on. Null if unreachable. */
export async function status() {
  try {
    return await (await fetch(url('/health'))).json()
  } catch {
    return null
  }
}

/* -------------------------------------------------------------- chat */

/**
 * Stream a reply, invoking `onChunk` with the text so far.
 *
 * The endpoint returns text/plain rather than server-sent events, so this reads
 * the body directly. `stream: true` on the decoder matters: a multi-byte
 * character split across two network chunks would otherwise decode to garbage.
 */
export async function streamChat({ message, threadId, crmSessionId }, onChunk) {
  const res = await authed('/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message,
      thread_id: threadId,
      crm_session_id: crmSessionId ?? null,
    }),
  })
  await expectOk(res)

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let text = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    text += decoder.decode(value, { stream: true })
    onChunk(text)
  }
  return text
}

/* --------------------------------------------------------- documents */

/** What the server currently has indexed. The store, not component state, is
 *  the source of truth -- a refresh used to show an empty list while the
 *  documents were still there and still being searched. */
export async function listDocuments() {
  return (await expectOk(await authed('/documents'))).json()
}

export async function deleteDocument(name) {
  return (
    await expectOk(await authed(`/documents/${encodeURIComponent(name)}`, { method: 'DELETE' }))
  ).json()
}

export async function uploadDocument(file) {
  const body = new FormData()
  body.append('file', file)
  return (await expectOk(await authed('/documents', { method: 'POST', body }))).json()
}

export async function clearDocuments() {
  return (await expectOk(await authed('/documents', { method: 'DELETE' }))).json()
}
