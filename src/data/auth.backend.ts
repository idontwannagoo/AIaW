import { shallowRef, type ShallowRef } from 'vue'
import { BackendApiBaseURL } from 'src/utils/config'
import type { AuthSource, CloudUser, UserInteraction } from './types'
import { emitAuthChange } from './auth-events'
import { clearAllSyncedTables } from './local-cache'

interface BackendUser {
  id: string
  email: string
  status: string
  created_at: string
  last_login_at?: string | null
}

interface TokenPair {
  access_token: string
  refresh_token: string
  user: BackendUser
}

const REFRESH_KEY = 'aiaw.backendAuth.refresh'
const USER_KEY = 'aiaw.backendAuth.user'
// Refresh access token this many ms before its `exp` so we never serve a stale
// bearer to the http layer. Conservative — JWT TTL is 30 min, this leaves ~29.
const REFRESH_BEFORE_EXPIRY_MS = 60 * 1000

const enabled = !!BackendApiBaseURL

const userRef: ShallowRef<CloudUser | null | undefined> = shallowRef(undefined)
// Stage 1.5 doesn't push prompts through this channel — the dialog is opened
// directly from login(). Kept here so the AuthSource shape matches.
const interactionRef: ShallowRef<UserInteraction | null | undefined> = shallowRef(null)

let accessToken: string | null = null
let refreshToken: string | null = null
let refreshTimer: ReturnType<typeof setTimeout> | null = null
let bootPromise: Promise<void> | null = null
let inflightRefresh: Promise<void> | null = null
let openLoginDialog: (() => Promise<TokenPair>) | null = null

export function registerBackendLoginDialog(opener: () => Promise<TokenPair>) {
  openLoginDialog = opener
}

function loadStoredUser(): CloudUser | null {
  try {
    const raw = localStorage.getItem(USER_KEY)
    if (!raw) return null
    const u = JSON.parse(raw) as BackendUser
    return toCloudUser(u, undefined)
  } catch {
    return null
  }
}

function persistUser(u: BackendUser | null) {
  if (u) localStorage.setItem(USER_KEY, JSON.stringify(u))
  else localStorage.removeItem(USER_KEY)
}

function loadStoredRefresh(): string | null {
  try { return localStorage.getItem(REFRESH_KEY) } catch { return null }
}

function persistRefresh(token: string | null) {
  if (token) localStorage.setItem(REFRESH_KEY, token)
  else localStorage.removeItem(REFRESH_KEY)
}

function toCloudUser(u: BackendUser, token: string | undefined): CloudUser {
  return {
    isLoggedIn: true,
    email: u.email,
    userId: u.id,
    accessToken: token
  }
}

function decodeJwtExp(token: string): number | null {
  try {
    const payload = token.split('.')[1]
    const json = JSON.parse(atob(payload.replace(/-/g, '+').replace(/_/g, '/')))
    return typeof json.exp === 'number' ? json.exp * 1000 : null
  } catch {
    return null
  }
}

function scheduleRefresh() {
  if (refreshTimer) {
    clearTimeout(refreshTimer)
    refreshTimer = null
  }
  if (!accessToken) return
  const expMs = decodeJwtExp(accessToken)
  if (!expMs) return
  const delay = Math.max(0, expMs - Date.now() - REFRESH_BEFORE_EXPIRY_MS)
  refreshTimer = setTimeout(() => { void runRefresh() }, delay)
}

async function postJson<T>(path: string, body: unknown, withAuth = false): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (withAuth && accessToken) headers.Authorization = `Bearer ${accessToken}`
  const res = await fetch(`${BackendApiBaseURL}${path}`, {
    method: 'POST',
    headers,
    body: JSON.stringify(body)
  })
  if (!res.ok) {
    let detail: string | undefined
    try { detail = (await res.json())?.detail } catch { /* ignore */ }
    throw new Error(detail || `${res.status} ${res.statusText}`)
  }
  return res.status === 204 ? (undefined as T) : await res.json()
}

function isLoggedInUser(u: CloudUser | null | undefined): boolean {
  return !!u && u.isLoggedIn === true
}

function applyTokenPair(pair: TokenPair) {
  // Bug 1/3/4 fix: emit 'login' only on the null/undefined → truthy
  // transition. `applyTokenPair` is also called by `runRefresh` while the
  // user is already logged in (silent token rotation); that path must NOT
  // emit so we don't spuriously re-bootstrap and reset module cursors
  // every 29min.
  const wasLoggedIn = isLoggedInUser(userRef.value)
  accessToken = pair.access_token
  refreshToken = pair.refresh_token
  persistRefresh(refreshToken)
  persistUser(pair.user)
  userRef.value = toCloudUser(pair.user, accessToken)
  scheduleRefresh()
  if (!wasLoggedIn) {
    emitAuthChange('login')
  }
}

function clearAuth() {
  // Bug 2 fix: emit 'logout' BEFORE wiping in-memory state so any sync
  // listener can read userRef one last time if it needs to (currently no
  // listener does, but the contract is more robust this way). We also
  // capture the prior login state to suppress redundant emits — boot()'s
  // "no stored user" cold path calls clearAuth() and we don't want a
  // 'logout' fired when nobody was logged in to begin with.
  const wasLoggedIn = isLoggedInUser(userRef.value)
  accessToken = null
  refreshToken = null
  if (refreshTimer) { clearTimeout(refreshTimer); refreshTimer = null }
  persistRefresh(null)
  persistUser(null)
  userRef.value = null
  if (wasLoggedIn) {
    emitAuthChange('logout')
  }
}

async function runRefresh(): Promise<void> {
  if (inflightRefresh) return inflightRefresh
  if (!refreshToken) { userRef.value = null; return }
  inflightRefresh = (async () => {
    try {
      const pair = await postJson<TokenPair>('/api/v1/auth/refresh', {
        refresh_token: refreshToken
      })
      applyTokenPair(pair)
    } catch {
      // Refresh failed — treat as logged out. Caller can re-prompt login.
      clearAuth()
    } finally {
      inflightRefresh = null
    }
  })()
  return inflightRefresh
}

function boot(): Promise<void> {
  if (bootPromise) return bootPromise
  bootPromise = (async () => {
    if (!enabled) { userRef.value = null; return }
    const stored = loadStoredUser()
    refreshToken = loadStoredRefresh()
    if (stored && refreshToken) {
      // Show last-known user immediately so first paint isn't "logged out".
      userRef.value = stored
      // Get a fresh access token in the background; failure clears state.
      await runRefresh()
    } else {
      clearAuth()
    }
  })()
  return bootPromise
}

// Cross-tab sync via the storage event. Fires only in OTHER tabs (never the
// writer), so no self-loop. We deliberately do NOT call runRefresh here:
// that would rotate the just-issued refresh token, breaking the writer tab
// (its in-memory refresh would 401 next time). Instead we adopt the new
// refresh + user from storage and let access-token acquisition stay lazy —
// the next pre-expiry timer or an explicit request triggers refresh.
function syncFromStorage() {
  const newRefresh = loadStoredRefresh()
  if (newRefresh === refreshToken) {
    const stored = loadStoredUser()
    if (stored) userRef.value = stored
    return
  }
  if (refreshTimer) { clearTimeout(refreshTimer); refreshTimer = null }
  accessToken = null
  refreshToken = newRefresh
  userRef.value = newRefresh ? loadStoredUser() : null
}

if (enabled) {
  void boot()
  if (typeof window !== 'undefined') {
    window.addEventListener('storage', (e) => {
      if (e.storageArea !== localStorage) return
      // e.key === null when localStorage.clear() was called.
      if (e.key !== null && e.key !== REFRESH_KEY && e.key !== USER_KEY) return
      syncFromStorage()
    })
  }
}

export const backendAuthSource: AuthSource = {
  enabled,
  user: userRef,
  userInteraction: interactionRef,
  async login() {
    if (!enabled) return
    if (!openLoginDialog) {
      throw new Error('backend login dialog not registered')
    }
    const pair = await openLoginDialog()
    applyTokenPair(pair)
  },
  async logout() {
    if (!enabled) return
    if (refreshToken) {
      try { await postJson<void>('/api/v1/auth/logout', { refresh_token: refreshToken }) } catch { /* best effort; revoke locally regardless */ }
    }
    // Bug 2 fix — three-step ordered teardown:
    //   1. emit 'logout' first so each `<table>.server.ts` listener
    //      synchronously unsubscribes its realtime channel + zeros out
    //      `lastVersion` / scoped cursors. This closes the window where
    //      a late-arriving WS event could write into IDB right after
    //      we wipe it in step 2.
    //   2. await `clearAllSyncedTables()` to wipe all 10 server-routed
    //      Dexie tables in a single rw transaction. liveQuery fires
    //      empty arrays to subscribed stores so the UI flips to "no
    //      data" before the user is shown a non-auth page.
    //   3. `clearAuth()` clears in-memory token + persisted localStorage
    //      keys + sessionStorage bootstrap flag (via the listener
    //      registered in `router/index.ts`). It re-emits 'logout' but
    //      the listeners are idempotent (resetting `lastVersion=0`,
    //      already-null `realtimeUnsubscribe`, etc) so the second pass
    //      is a no-op.
    //
    // Best-effort wrapping on the IDB step: even if Dexie is in a bad
    // state we still want the auth side to clear, otherwise the user
    // is stuck "kind of logged out".
    emitAuthChange('logout')
    try {
      await clearAllSyncedTables()
    } catch (err) {
      console.warn('[auth.backend] clearAllSyncedTables failed', err)
    }
    clearAuth()
  },
  async sync() { /* no-op in Stage 1.5 — repos pull on demand */ },
  onReady(fn) {
    if (!enabled) { fn(); return }
    void boot().then(fn)
  },
  async waitForFirstSync() {
    if (!enabled) return
    await boot()
  },
  currentToken() {
    return accessToken ?? undefined
  },
  async tryRefresh() {
    await runRefresh()
    return !!accessToken
  }
}
