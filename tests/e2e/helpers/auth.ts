// Login helpers. Two paths kept on purpose:
// - loginViaApi: skip UI, hit /auth/login directly, prime localStorage so the
//   backend-auth boot file adopts the session on next navigation. Use for
//   bulk setup ("user A is already logged in").
// - loginViaUI: drive the AccountPage form. Use only when the spec is
//   actually validating the login UX (Stage 1.5 dedicated specs).
//
// Storage keys must stay in sync with src/data/auth.backend.ts.
import type { Page } from '@playwright/test'
import { BACKEND_URL } from './env'

const REFRESH_KEY = 'aiaw.backendAuth.refresh'
const USER_KEY = 'aiaw.backendAuth.user'

export interface BackendUser {
  id: string
  email: string
  status: string
  linked_dexie_email?: string | null
  created_at: string
  last_login_at?: string | null
}

export interface TokenPair {
  access_token: string
  refresh_token: string
  user: BackendUser
}

async function _post(path: string, body: unknown): Promise<TokenPair> {
  const r = await fetch(`${BACKEND_URL}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  })
  if (!r.ok) {
    throw new Error(
      `Expected status 200 from ${path}, got ${r.status}: ${await r.text()}`
    )
  }
  return r.json()
}

export async function registerViaApi(
  email: string,
  password = 'test-password-123'
): Promise<TokenPair> {
  return _post('/api/v1/auth/register', { email, password })
}

export async function loginApi(
  email: string,
  password: string
): Promise<TokenPair> {
  return _post('/api/v1/auth/login', { email, password })
}

// Inject the refresh + user into the page's localStorage at navigation start
// so backend-auth.ts boot() finds them. Caller still needs page.goto() after.
export async function injectAuth(page: Page, pair: TokenPair): Promise<void> {
  await page.addInitScript(
    ({ refreshKey, userKey, refresh, user }) => {
      localStorage.setItem(refreshKey, refresh)
      localStorage.setItem(userKey, JSON.stringify(user))
    },
    {
      refreshKey: REFRESH_KEY,
      userKey: USER_KEY,
      refresh: pair.refresh_token,
      user: pair.user
    }
  )
}

export async function loginViaApi(
  page: Page,
  email: string,
  password: string
): Promise<TokenPair> {
  const pair = await loginApi(email, password)
  await injectAuth(page, pair)
  return pair
}

// UI-driven login. Selectors are intentionally loose (label-based) so they
// survive minor copy tweaks. Stage 1.5 specs that actually test login flow
// should narrow these down via data-testid in the dialog/page.
export async function loginViaUI(
  page: Page,
  email: string,
  password: string
): Promise<void> {
  await page.goto('/account')
  await page.getByLabel(/email/i).fill(email)
  await page.getByLabel(/password/i).fill(password)
  await page.getByRole('button', { name: /login|sign in|登录/i }).click()
  await page.waitForFunction(
    (k) => !!localStorage.getItem(k),
    REFRESH_KEY,
    { timeout: 10_000 }
  )
}

export async function logoutViaStorage(page: Page): Promise<void> {
  await page.evaluate(
    ([refreshKey, userKey]) => {
      localStorage.removeItem(refreshKey)
      localStorage.removeItem(userKey)
    },
    [REFRESH_KEY, USER_KEY]
  )
}
