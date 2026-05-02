// Stage 1.5 — UI-side auth link (login / refresh / logout).
// Phase 3 covered the backend in pytest; this spec drives the actual
// BackendLoginDialog + AccountPage logout button so the cloud-sync-migration
// plan's Stage 1.5 验证 4 场景 also exist as Playwright cases:
//   - case1 register via dialog: BackendLoginDialog → POST /auth/register →
//           authSource.user populated, currentToken() valid against /auth/me
//   - case2 login existing account via dialog: same dialog, already-registered
//           user, switch from default register-mode-after-empty back to login
//   - case3 logout via AccountPage button: clears in-memory + localStorage state
//   - case4 refresh on boot: stored refresh_token survives reload — boot
//           runRefresh() swaps in a fresh access token without prompting
//
// Runs only on the providers-rest profile because BACKEND_AUTH=true is what
// enables BackendAuthSource. The realtime-ws profile would also work but
// would needlessly drag the WS singleton into auth-only assertions.

import { test, expect, type Page, type BrowserContext } from '@playwright/test'
import { exposeReady } from '../helpers/db'
import { registerViaApi, injectAuth } from '../helpers/auth'
import { BACKEND_URL } from '../helpers/env'

function uniqEmail(tag: string): string {
  return `s15-${tag}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
}

const REFRESH_KEY = 'aiaw.backendAuth.refresh'
const USER_KEY = 'aiaw.backendAuth.user'

async function gotoHome(page: Page): Promise<void> {
  page.on('pageerror', (e) => console.log('[page-error]', e.message))
  await page.goto('/')
  await exposeReady(page)
}

async function waitLoggedIn(page: Page, email: string): Promise<void> {
  await page.waitForFunction((expected) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const u = (window as any).__authSource__?.user?.value
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const tok = (window as any).__authSource__?.currentToken?.()
    return !!u && u.isLoggedIn === true && u.email === expected && !!tok
  }, email, { timeout: 10_000 })
}

async function waitLoggedOut(page: Page): Promise<void> {
  await page.waitForFunction(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const u = (window as any).__authSource__?.user?.value
    return u === null || u === undefined
  }, undefined, { timeout: 10_000 })
}

// Drive the BackendLoginDialog. Selectors deliberately tolerate both en-US
// and zh-CN labels (the build picks one based on navigator.language; CI is
// usually en-US but local devs see zh-CN). Switching modes is needed because
// the dialog opens in `login` by default — for register cases we click the
// "no account?" toggle first.
async function fillAndSubmit(
  page: Page,
  mode: 'login' | 'register',
  email: string,
  password: string
): Promise<void> {
  const dialog = page.locator('.q-dialog').filter({
    has: page.getByLabel(/email|邮箱/i)
  }).first()
  await dialog.waitFor({ state: 'visible', timeout: 10_000 })

  if (mode === 'register') {
    // Switch from default login → register.
    await dialog
      .getByRole('button', { name: /sign up|create account|注册|没有账号/i })
      .first()
      .click()
  } else {
    // Already in login mode by default; if the dialog re-opened mid-test it
    // remembers the previous mode, so make sure we're on login.
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

test.describe('stage1.5 auth ui', () => {
  test('case1 register via BackendLoginDialog: dialog → user populated → /auth/me works', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')

    const ctx: BrowserContext = await browser.newContext()
    try {
      const page = await ctx.newPage()
      await gotoHome(page)

      const email = uniqEmail('reg')
      const password = 'test-password-123'

      // Open the dialog by calling authSource.login() — same code path as the
      // AccountPage onReady redirect, but without the route hop so the spec
      // doesn't entangle with router behaviour.
      await page.evaluate(() => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        void (window as any).__authSource__.login()
      })
      await fillAndSubmit(page, 'register', email, password)
      await waitLoggedIn(page, email)

      // currentToken() must authenticate against /api/v1/auth/me — that
      // proves the dialog handed a real access token to BackendAuthSource,
      // not just a placeholder.
      const meStatus = await page.evaluate(async (url) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const tok = (window as any).__authSource__.currentToken()
        const r = await fetch(`${url}/api/v1/auth/me`, {
          headers: { Authorization: `Bearer ${tok}` }
        })
        return r.status
      }, BACKEND_URL)
      expect(meStatus).toBe(200)

      // Sanity: localStorage now has the rotation-friendly refresh token + user.
      const storage = await page.evaluate(([rk, uk]) => ({
        refresh: localStorage.getItem(rk),
        user: localStorage.getItem(uk)
      }), [REFRESH_KEY, USER_KEY])
      expect(storage.refresh, 'refresh_token must be persisted after register').toBeTruthy()
      expect(storage.user, 'user blob must be persisted after register').toBeTruthy()
    } finally {
      await ctx.close()
    }
  })

  test('case2 login existing account via dialog: pre-registered user can sign back in', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')

    // Pre-register out-of-band so the dialog hits the login path proper.
    const email = uniqEmail('login')
    const password = 'test-password-123'
    await registerViaApi(email, password)

    const ctx: BrowserContext = await browser.newContext()
    try {
      const page = await ctx.newPage()
      await gotoHome(page)

      await page.evaluate(() => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        void (window as any).__authSource__.login()
      })
      await fillAndSubmit(page, 'login', email, password)
      await waitLoggedIn(page, email)
    } finally {
      await ctx.close()
    }
  })

  test('case3 logout via AccountPage button clears in-memory + localStorage state', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')

    const email = uniqEmail('logout')
    const password = 'test-password-123'
    const reg = await registerViaApi(email, password)

    const ctx: BrowserContext = await browser.newContext()
    try {
      const page = await ctx.newPage()
      // Inject the registration session and land directly on /account so the
      // boot path's runRefresh() runs exactly once. Going /home → /account
      // would re-fire the addInitScript and reset localStorage back to the
      // already-rotated registration token, kicking the user back out before
      // AccountPage mounts.
      await injectAuth(page, reg)
      page.on('pageerror', (e) => console.log('[page-error]', e.message))
      await page.goto('/account')
      await exposeReady(page)
      await waitLoggedIn(page, email)

      // Snapshot the live (post-rotation) refresh token before logout. The
      // original reg.refresh_token was already burned by the boot path's
      // runRefresh — this is the one logout actually revokes.
      const liveRefresh = await page.evaluate(
        (rk) => localStorage.getItem(rk),
        REFRESH_KEY
      )
      expect(liveRefresh, 'expected a refresh_token in storage before logout').toBeTruthy()
      expect(liveRefresh).not.toBe(reg.refresh_token)

      // The AccountPage logout row is a Quasar q-item with @click="logout".
      // We use locator + dispatchEvent('click') rather than .click() because
      // the page mounts a transient empty q-dialog (subscribe / topup overlay
      // mounts even when conditions aren't met) that can intercept pointer
      // events; dispatchEvent fires the synthetic click directly on the
      // element so the Vue handler runs regardless.
      const logoutItem = page
        .locator('.q-item:has(.q-icon)')
        .filter({ hasText: /Log\s*Out|Sign\s*Out|退出登录|登出/i })
        .first()
      await logoutItem.waitFor({ state: 'visible', timeout: 10_000 })
      await logoutItem.dispatchEvent('click')
      await waitLoggedOut(page)
      const storage = await page.evaluate(([rk, uk]) => ({
        refresh: localStorage.getItem(rk),
        user: localStorage.getItem(uk)
      }), [REFRESH_KEY, USER_KEY])
      expect(storage.refresh, 'refresh_token must be cleared on logout').toBeNull()
      expect(storage.user, 'user blob must be cleared on logout').toBeNull()

      // The live refresh that was in storage must now 401 against /auth/refresh
      // — proves logout reached the backend, not just the local store.
      const reused = await page.evaluate(async ([url, prev]) => {
        const r = await fetch(`${url}/api/v1/auth/refresh`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: prev })
        })
        return r.status
      }, [BACKEND_URL, liveRefresh as string])
      expect(reused, 'logout must revoke the live refresh_token server-side').toBe(401)
    } finally {
      await ctx.close()
    }
  })

  test('case4 refresh on boot: stored refresh_token yields a working access token without prompting', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')

    const email = uniqEmail('refresh')
    const password = 'test-password-123'
    const reg = await registerViaApi(email, password)

    const ctx: BrowserContext = await browser.newContext()
    try {
      const page = await ctx.newPage()
      await injectAuth(page, reg)
      await gotoHome(page)
      await waitLoggedIn(page, email)

      // The boot path's runRefresh() rotated the refresh token. The original
      // refresh_token from registration should now be revoked.
      const replay = await page.evaluate(async ([url, prev]) => {
        const r = await fetch(`${url}/api/v1/auth/refresh`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: prev })
        })
        return r.status
      }, [BACKEND_URL, reg.refresh_token])
      expect(
        replay,
        'boot runRefresh() must rotate the stored refresh_token; the old one should now 401'
      ).toBe(401)

      // The currently-stored refresh_token (post-rotation) must itself work.
      const newRefresh = await page.evaluate((rk) => localStorage.getItem(rk), REFRESH_KEY)
      expect(newRefresh).toBeTruthy()
      expect(newRefresh).not.toBe(reg.refresh_token)
      const currentTok = await page.evaluate(() => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        return (window as any).__authSource__.currentToken()
      })
      const meStatus = await page.evaluate(async ([url, tok]) => {
        const r = await fetch(`${url}/api/v1/auth/me`, {
          headers: { Authorization: `Bearer ${tok}` }
        })
        return r.status
      }, [BACKEND_URL, currentTok] as const)
      expect(meStatus).toBe(200)
    } finally {
      await ctx.close()
    }
  })
})
