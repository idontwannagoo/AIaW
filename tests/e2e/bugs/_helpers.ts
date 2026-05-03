// Bug 1/2/3/4 shared helpers.
//
// Why a private helper file rather than promoting these to tests/e2e/helpers/:
// the four bug specs are intentionally written against the *UI login path*
// (BackendLoginDialog → applyTokenPair emit 'login' → triggerBootstrapNow)
// rather than the much more popular `injectAuth + page.goto('/')` path.
// `injectAuth` skips the `applyTokenPair` codepath entirely (the token is
// adopted by `boot()` on cold start, which never calls `applyTokenPair`),
// so it'd mask the very bug emit-on-login was added to fix. Promoting these
// to the global helpers would invite future specs to reach for "open the
// dialog and submit" when in 95% of cases `loginViaApi` is faster and more
// stable. Keep them local to the bug specs.

import type { Page, BrowserContext } from '@playwright/test'
import { exposeReady } from '../helpers/db'
import { registerViaApi, type TokenPair } from '../helpers/auth'
import { BACKEND_URL } from '../helpers/env'

export const REFRESH_KEY = 'aiaw.backendAuth.refresh'
export const USER_KEY = 'aiaw.backendAuth.user'
export const BOOTSTRAP_ATTEMPTED_KEY = 'aiaw.bootstrap.attempted'
export const BOOTSTRAP_FALLBACK_KEY = 'aiaw.bootstrap.fallback'

// All 10 server-routed Dexie tables, must match
// `src/data/local-cache.ts::clearAllSyncedTables` exactly.
export const SYNCED_TABLES = [
  'workspaces',
  'dialogs',
  'messages',
  'assistants',
  'artifacts',
  'installedPluginsV2',
  'reactives',
  'avatarImages',
  'items',
  'providers'
] as const

export function uniqEmail(tag: string): string {
  return `bug-${tag}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
}

export interface UserSetup {
  email: string
  password: string
  pair: TokenPair
}

export async function setupUser(tag: string): Promise<UserSetup> {
  const email = uniqEmail(tag)
  const password = 'test-password-123'
  const pair = await registerViaApi(email, password)
  return { email, password, pair }
}

// Land on /account with no localStorage tokens (clean slate). Caller will
// then drive the BackendLoginDialog to log in via UI. The page-error /
// console-error wiring is consistent across all four bug specs.
export async function gotoCleanAccountPage(page: Page): Promise<void> {
  page.on('pageerror', (e) => console.log('[page-error]', e.message))
  page.on('console', (msg) => {
    const t = msg.type()
    if (t === 'error') console.log('[page-error]', msg.text())
  })
  await page.goto('/account')
  await exposeReady(page)
}

export async function gotoCleanRoot(page: Page): Promise<void> {
  page.on('pageerror', (e) => console.log('[page-error]', e.message))
  page.on('console', (msg) => {
    const t = msg.type()
    if (t === 'error') console.log('[page-error]', msg.text())
  })
  await page.goto('/')
  await exposeReady(page)
}

// Drive the BackendLoginDialog. Mirrors stage1_5/auth-ui.spec.ts but kept
// here so the bug specs can call it as a one-liner without re-pulling the
// fillAndSubmit boilerplate. Important: this opens via
// `authSource.login()` rather than navigating to /account-then-clicking
// because we want the dialog as a Quasar overlay over the *current* page,
// matching the UX a user sees when their session expires mid-route.
export async function loginViaDialog(
  page: Page,
  mode: 'login' | 'register',
  email: string,
  password: string
): Promise<void> {
  await page.evaluate(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    void (window as any).__authSource__.login()
  })
  const dialog = page.locator('.q-dialog').filter({
    has: page.getByLabel(/email|邮箱/i)
  }).first()
  await dialog.waitFor({ state: 'visible', timeout: 10_000 })

  if (mode === 'register') {
    await dialog
      .getByRole('button', { name: /sign up|create account|注册|没有账号/i })
      .first()
      .click()
  } else {
    const switchToLogin = dialog
      .getByRole('button', { name: /have an account|switch to login|已有账号/i })
    if (await switchToLogin.count() > 0 && await switchToLogin.first().isVisible()) {
      await switchToLogin.first().click()
    }
  }

  await dialog.getByLabel(/email|邮箱/i).fill(email)
  await dialog.getByLabel(/^password|^密码/i).fill(password)
  await dialog
    .getByRole('button', {
      name: mode === 'register'
        ? /^create account$|^register$|^sign up$|^注册$/i
        : /^sign in$|^log in$|^login$|^登录$/i
    })
    .first()
    .click()

  // Dialog auto-closes on success.
  await dialog.waitFor({ state: 'hidden', timeout: 10_000 })
}

// Wait for `__authSource__.user.value` to flip to a logged-in shape with
// the right email + a non-null current token. Used after dialog submit.
export async function waitLoggedIn(page: Page, email: string): Promise<void> {
  await page.waitForFunction((expected) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const u = (window as any).__authSource__?.user?.value
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const tok = (window as any).__authSource__?.currentToken?.()
    return !!u && u.isLoggedIn === true && u.email === expected && !!tok
  }, email, { timeout: 10_000 })
}

// Wait for `__authSource__.user.value` to become null (logged out).
export async function waitLoggedOut(page: Page): Promise<void> {
  await page.waitForFunction(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const u = (window as any).__authSource__?.user?.value
    return u === null || u === undefined
  }, undefined, { timeout: 10_000 })
}

// Drive AccountPage's logout q-item via dispatchEvent (transient q-dialog
// can intercept pointer events on the page — see stage1_5 case3 for the
// same workaround). Caller must already be on /account.
export async function clickLogoutOnAccountPage(page: Page): Promise<void> {
  const logoutItem = page
    .locator('.q-item:has(.q-icon)')
    .filter({ hasText: /Log\s*Out|Sign\s*Out|退出登录|登出/i })
    .first()
  await logoutItem.waitFor({ state: 'visible', timeout: 10_000 })
  await logoutItem.dispatchEvent('click')
}

// Snapshot { count } per synced Dexie table. Used to verify Bug 2's "10
// tables count === 0" criterion in a single assert with a useful diff.
export async function readAllTableCounts(
  page: Page
): Promise<Record<string, number>> {
  return page.evaluate(async (tables) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const db = (window as any).__db__
    const out: Record<string, number> = {}
    for (const t of tables) {
      out[t] = await db[t].count()
    }
    return out
  }, [...SYNCED_TABLES])
}

// Read sessionStorage flags + localStorage refresh token. Used by Bug 2 to
// confirm logout teardown is complete (storage side too, not just IDB).
export async function readAuthArtifacts(page: Page): Promise<{
  refresh: string | null
  user: string | null
  bootstrapAttempted: string | null
  bootstrapFallback: string | null
}> {
  return page.evaluate(
    ({ rk, uk, ak, fk }) => ({
      refresh: localStorage.getItem(rk),
      user: localStorage.getItem(uk),
      bootstrapAttempted: sessionStorage.getItem(ak),
      bootstrapFallback: sessionStorage.getItem(fk)
    }),
    {
      rk: REFRESH_KEY,
      uk: USER_KEY,
      ak: BOOTSTRAP_ATTEMPTED_KEY,
      fk: BOOTSTRAP_FALLBACK_KEY
    }
  )
}

// Seed N workspaces server-side via REST so the post-login bootstrap has
// real data to hydrate. Mirrors stage4_5/step8 helper but inline-typed.
export async function seedWorkspaces(
  pair: TokenPair,
  count: number,
  prefix = 'ws'
): Promise<string[]> {
  const ids: string[] = []
  for (let i = 0; i < count; i++) {
    const id = `${prefix}-${i}-${Math.random().toString(36).slice(2, 6)}`
    const r = await fetch(`${BACKEND_URL}/api/v1/workspaces/${id}`, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${pair.access_token}`
      },
      body: JSON.stringify({
        id,
        name: `WS ${i}`,
        avatar: { type: 'icon', icon: 'sym_o_deployed_code' },
        type: 'workspace',
        parentId: '$root',
        prompt: '',
        indexContent: '# index',
        vars: {},
        listOpen: { assistants: true, artifacts: false, dialogs: true }
      })
    })
    if (!r.ok) throw new Error(`seed ws ${id} failed: ${r.status} ${await r.text()}`)
    ids.push(id)
  }
  return ids
}

// Seed a `#user-perfs` reactive row for the user with a sentinel provider
// + model so Bug 4 can assert defaults are server-supplied (not the
// hard-coded fallback). The blob shape mirrors what the real client puts.
export async function seedUserPerfs(
  pair: TokenPair,
  perfs: Record<string, unknown>
): Promise<void> {
  const r = await fetch(
    `${BACKEND_URL}/api/v1/reactives/${encodeURIComponent('#user-perfs')}`,
    {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${pair.access_token}`
      },
      body: JSON.stringify(perfs)
    }
  )
  if (!r.ok) throw new Error(`seed #user-perfs failed: ${r.status} ${await r.text()}`)
}

// Convenience: build a context that won't share IDB with anything else.
// Matches openContextsForUsers but for a single user where we don't need
// the second tab (Bug 1/3/4 are single-user single-tab specs).
export async function freshContext(browser: import('@playwright/test').Browser) {
  return browser.newContext()
}

// Force a Bug 2 cross-account scenario: register A, register B, return
// both. Used by bug2 spec's "log out A then in as B" case.
export async function setupTwoUsers(): Promise<{
  a: UserSetup
  b: UserSetup
}> {
  const a = await setupUser('cross-a')
  const b = await setupUser('cross-b')
  return { a, b }
}

// Re-export so individual bug specs don't need a separate import line.
export { exposeReady }
export type { TokenPair, BrowserContext }
