import { shallowRef, type ShallowRef } from 'vue'
import { BackendApiBaseURL } from 'src/utils/config'
import type { AuthSource, CloudUser, UserInteraction } from './types'

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

function applyTokenPair(pair: TokenPair) {
  accessToken = pair.access_token
  refreshToken = pair.refresh_token
  persistRefresh(refreshToken)
  persistUser(pair.user)
  userRef.value = toCloudUser(pair.user, accessToken)
  scheduleRefresh()
}

function clearAuth() {
  accessToken = null
  refreshToken = null
  if (refreshTimer) { clearTimeout(refreshTimer); refreshTimer = null }
  persistRefresh(null)
  persistUser(null)
  userRef.value = null
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
