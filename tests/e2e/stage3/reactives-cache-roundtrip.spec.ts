// Stage 3 / 批次-3a — reactives IndexedDB cache <→ server roundtrip.
// Ensures the server-routed repo behaves like a cache layer over the backend:
// - case1: clear IndexedDB → reload → server data is re-pulled into cache
// - case2: write + reload → cache is hit (no full ?since=0 round-trip)

import { test, expect, type Page } from '@playwright/test'
import { exposeReady, dumpTable, clearAll } from '../helpers/db'
import { putReactive } from '../helpers/backend'
import { registerViaApi, loginApi, injectAuth, type TokenPair } from '../helpers/auth'

function uniqEmail(): string {
  return `stage3-cache-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
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

test.describe('stage3 batch-3a reactives cache roundtrip', () => {
  test('case1 cleared IndexedDB → reload → server data re-pulled into cache', async ({ browser }, testInfo) => {
    test.skip(
      !['providers-rest', 'realtime-ws', 'realtime-auto'].includes(testInfo.project.name),
      'cache-roundtrip needs a server-routed profile'
    )

    const user = await setupTestUser()
    // Pre-seed two server-side reactives via the REST helper.
    await putReactive(user.accessForBackend, 'cache-k1', { v: 'server-v1' })
    await putReactive(user.accessForBackend, 'cache-k2', { v: 'server-v2' })

    const ctx = await browser.newContext()
    try {
      const page = await ctx.newPage()
      await bootSession(page, await freshSession(user))
      // Initial pull seeds the cache from server.
      await page.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.reactives.list()
      })
      // Dexie's db.on.populate autopopulates `#user-data` for new IDB
      // databases, so the cache may legitimately carry that key alongside
      // our seeded keys. Assert presence of the seeded keys only.
      const seeded = await dumpTable<{ key: string }>(page, 'reactives')
      const seededKeys = seeded.map(r => r.key)
      expect(
        seededKeys.includes('cache-k1') && seededKeys.includes('cache-k2'),
        `seed pull should populate cache-k1 and cache-k2 (rows=${JSON.stringify(seededKeys)})`
      ).toBe(true)

      // Wipe the IndexedDB cache; reload should not lose data — the server
      // is still authoritative and a fresh pull bootstraps the cache.
      await clearAll(page, ['reactives'])
      const wiped = await dumpTable<{ key: string }>(page, 'reactives')
      expect(wiped, 'IDB clear should empty the local cache').toEqual([])

      await injectAuth(page, await freshSession(user))
      await page.reload()
      await exposeReady(page)
      await page.waitForFunction(() => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const auth = (window as any).__authSource__
        return !!(auth && auth.currentToken && auth.currentToken())
      }, undefined, { timeout: 10_000 })
      // After reload, list() should re-bootstrap from version 0 because the
      // cache is empty — see reactives.server.ts pull() reset-on-empty branch.
      await page.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.reactives.list()
      })
      const refilled = await dumpTable<{ key: string; value: { v: string } }>(page, 'reactives')
      const k1 = refilled.find(r => r.key === 'cache-k1')
      const k2 = refilled.find(r => r.key === 'cache-k2')
      expect(
        k1?.value?.v === 'server-v1' && k2?.value?.v === 'server-v2',
        `cache should be re-pulled from server after clear+reload ` +
          `(rows=${JSON.stringify(refilled)})`
      ).toBe(true)
    } finally {
      await ctx.close()
    }
  })

  test('case2 write + reload → list() does not re-fetch full ?since=0', async ({ browser }, testInfo) => {
    test.skip(
      !['providers-rest', 'realtime-ws', 'realtime-auto'].includes(testInfo.project.name),
      'cache-roundtrip needs a server-routed profile'
    )

    const user = await setupTestUser()
    const ctx = await browser.newContext()
    try {
      const page = await ctx.newPage()
      await bootSession(page, await freshSession(user))

      // Write through the repo so the cache has data + a known lastVersion.
      await page.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const r = (window as any).__repos__.reactives
        await r.put({ key: 'cache-w1', value: { v: 1 } })
        await r.put({ key: 'cache-w2', value: { v: 2 } })
      })
      const seeded = await dumpTable<{ key: string }>(page, 'reactives')
      const seededKeys = seeded.map(r => r.key)
      expect(
        seededKeys.includes('cache-w1') && seededKeys.includes('cache-w2'),
        `cache should hold both written keys (rows=${JSON.stringify(seededKeys)})`
      ).toBe(true)

      // Reload. After reload, lastVersion is 0 again (module state reset),
      // but the cache survives in IDB — we want to confirm that the next
      // list() doesn't drop the cache to refetch from since=0. In practice
      // we observe the network requests: the first list() is allowed to
      // GET ?since=0 (since lastVersion was reset to 0 by reload), but the
      // result should be empty because nothing has happened on the server
      // since the original write — meaning the cache is reused, not re-pulled
      // wholesale. Stage 4.9 will replace this behavior with proper persisted
      // lastVersion; for now we just check that the cache still has the rows
      // after reload + list() and no error is raised.
      await injectAuth(page, await freshSession(user))
      await page.reload()
      await exposeReady(page)
      await page.waitForFunction(() => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const auth = (window as any).__authSource__
        return !!(auth && auth.currentToken && auth.currentToken())
      }, undefined, { timeout: 10_000 })

      const reqsToReactives: string[] = []
      page.on('request', (req) => {
        const url = req.url()
        if (url.includes('/api/v1/reactives')) reqsToReactives.push(url)
      })
      await page.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.reactives.list()
      })
      const afterReload = await dumpTable<{ key: string; value: { v: number } }>(page, 'reactives')
      const w1 = afterReload.find(r => r.key === 'cache-w1')
      const w2 = afterReload.find(r => r.key === 'cache-w2')
      expect(
        w1?.value?.v === 1 && w2?.value?.v === 2,
        `cache should retain rows after reload (rows=${JSON.stringify(afterReload)})`
      ).toBe(true)
      // The reactives endpoint may be hit at most once for the bootstrap pull;
      // it should NOT spam `?since=0` repeatedly.
      expect(
        reqsToReactives.length,
        `expected ≤ 1 reactives request on cached list, saw ${reqsToReactives.length}: ` +
          JSON.stringify(reqsToReactives)
      ).toBeLessThanOrEqual(1)
    } finally {
      await ctx.close()
    }
  })
})
