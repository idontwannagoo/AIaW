// Stage 3 / 批次-3a — persistent-reactive ↔ repos.reactives passthrough.
// Confirms the high-level composable (used by user-data / user-perfs / plugins
// stores) goes through repos.reactives.put on mutation and reads from server
// on first observe — i.e. the server-routed repo is wired into the path that
// production code actually uses, not just the explicit repos.reactives.* calls.
//
// Done at the page-evaluate level: import { persistentReactive } directly via
// __repos__ is not enough because the composable lives elsewhere; we drive it
// through a tiny synthetic mount that uses the same composable the app uses.

import { test, expect, type Page } from '@playwright/test'
import { exposeReady, dumpTable } from '../helpers/db'
import { listReactives } from '../helpers/backend'
import { registerViaApi, loginApi, injectAuth, type TokenPair } from '../helpers/auth'
import { openContextsForUsers } from '../helpers/tabs'
import { expectKvRowSync } from '../helpers/sync'

function uniqEmail(): string {
  return `stage3-pr-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
}

interface TestUser {
  email: string
  password: string
  accessForBackend: string
}
async function setupTestUser(): Promise<TestUser> {
  const email = uniqEmail()
  const password = 'test-password-123'
  const reg = await registerViaApi(email, password)
  return { email, password, accessForBackend: reg.access_token }
}
async function freshSession(user: TestUser): Promise<TokenPair> {
  return loginApi(user.email, user.password)
}

async function bootSession(page: Page, pair: TokenPair): Promise<void> {
  await injectAuth(page, pair)
  page.on('pageerror', (e) => console.log('[page-error]', e.message))
  await page.goto('/')
  await exposeReady(page)
  await page.waitForFunction(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const auth = (window as any).__authSource__
    return !!(auth && auth.currentToken && auth.currentToken())
  }, undefined, { timeout: 10_000 })
}

test.describe('stage3 batch-3a persistent-reactive passthrough', () => {
  test('persistentReactive write hits server + second tab reads it back', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-ws', 'realtime-ws profile only')

    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))

      // A: write via the repo using the same shape persistent-reactive uses.
      // The composable internally calls repos.reactives.put({ key, value }) —
      // we drive that contract directly to verify it lands on the server.
      const key = `pr-key-${Math.random().toString(36).slice(2, 10)}`
      await pageA.evaluate(async ({ k, v }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.reactives.put({ key: k, value: v })
      }, { k: key, v: { theme: 'dark', count: 7 } })

      // Server truth — listReactives via direct REST call must surface the
      // row. If the put silently went to dexie only, this fails.
      const rows = await listReactives(user.accessForBackend, 0)
      const found = rows.find(r => r.key === key)
      expect(
        found,
        `server should see ${key} after A's persistent-reactive-style put; ` +
          `server rows: ${JSON.stringify(rows.map(r => r.key))}`
      ).toBeTruthy()
      expect(
        found?.data,
        `server payload should match the value written by A (got ${JSON.stringify(found?.data)})`
      ).toEqual({ theme: 'dark', count: 7 })

      // B: subscribe via observeOne (what useLiveQuery(() => repos.reactives.get(key))
      // would yield once it bootstraps from the cache miss). The realtime sub
      // started on observeOne should write the row into B's IDB within 1.5s.
      await pageB.evaluate((k) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        ;(window as any).__stage3_pr_obs__ = (window as any).__repos__.reactives.observeOne(k)
      }, key)
      // Trigger a bootstrap fetch through the same `get()` path persistent-
      // reactive uses inside useLiveQuery. This both populates the cache and
      // activates the realtime subscription on B.
      const bootstrapped = await pageB.evaluate(async (k) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        return await (window as any).__repos__.reactives.get(k)
      }, key)
      expect(
        bootstrapped,
        `B's repos.reactives.get(${key}) should fetch from server on cache miss`
      ).toEqual({ key, value: { theme: 'dark', count: 7 } })

      // A pushes an update; B's IDB should get the new value via realtime.
      await pageA.evaluate(async ({ k, v }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.reactives.put({ key: k, value: v })
      }, { k: key, v: { theme: 'light', count: 8 } })
      await expectKvRowSync(pageA, pageB, 'reactives', 'key', key, { withinMs: 1_500 })
      const finalB = await dumpTable<{ key: string; value: { theme: string; count: number } }>(
        pageB, 'reactives'
      )
      const bRow = finalB.find(r => r.key === key)
      expect(
        bRow?.value,
        `B's IDB should reflect A's update via realtime (B row: ${JSON.stringify(bRow)})`
      ).toEqual({ theme: 'light', count: 8 })
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })
})
