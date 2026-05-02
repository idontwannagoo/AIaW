// Stage 2 Step 5 acceptance — SSE / poll / auto downgrade transports.
// Maps 1:1 to plans/cloud-sync-migration.md Stage 2 Step 5 通过判据 #1-4.
//
// Spec-first: case1-4 are EXPECTED to fail until Step 5 code lands —
// SseSyncSource / PollSyncSource / auto fallback in src/data/, plus the
// SSE endpoint in src-backend/data/routers/. Once those merge, all four
// should turn green; case3 / case4 should remain green from then on,
// guarded by the auto profile.
//
// Two-context (not two-tab) setup keeps server fan-out honest: same-context
// tabs share IDB + BroadcastChannel, which would let a local put surface in
// the other tab without ever crossing the SSE / poll boundary, masking
// transport regressions.

import { test, expect, type Page } from '@playwright/test'
import { exposeReady, dumpTable } from '../helpers/db'
import { putProvider } from '../helpers/backend'
import { registerViaApi, loginApi, injectAuth, type TokenPair } from '../helpers/auth'
import { openContextsForUsers } from '../helpers/tabs'
import { setOffline, blockWS, blockSSE } from '../helpers/net'

function uniqEmail(): string {
  return `step5-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
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

function buildProvider(id: string, name: string): Record<string, unknown> {
  return {
    id,
    name,
    avatar: { type: 'icon', icon: 'sym_o_extension', hue: 0 },
    type: 'openai',
    settings: {},
    subproviders: [],
    fallbackProvider: null
  }
}

// Mirror Step 4's bootSession but without an opinion on transport: the
// realtime singleton picks WS / SSE / poll from RealtimeTransport at module
// init, so the spec just has to wait for token wiring + observer activation.
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

async function activateProvidersObserver(page: Page): Promise<void> {
  await page.evaluate(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(window as any).__step5_obs__ = (window as any).__repos__.providers.observeList()
  })
  await page.evaluate(async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await (window as any).__repos__.providers.list()
  })
}

async function pollUntil<T>(
  fn: () => Promise<T>,
  predicate: (v: T) => boolean,
  withinMs: number,
  pollMs = 100
): Promise<{ ok: boolean; last: T; elapsedMs: number }> {
  const start = Date.now()
  let last: T = await fn()
  while (Date.now() - start < withinMs) {
    last = await fn()
    if (predicate(last)) return { ok: true, last, elapsedMs: Date.now() - start }
    await new Promise(resolve => setTimeout(resolve, pollMs))
  }
  return { ok: false, last, elapsedMs: Date.now() - start }
}

async function readTransport(page: Page): Promise<string | undefined> {
  return page.evaluate(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const r = (window as any).aiawRealtime
    return r ? r.transport : undefined
  })
}

async function waitForTransport(
  page: Page,
  expected: string,
  timeoutMs = 8_000
): Promise<string | undefined> {
  const start = Date.now()
  let last: string | undefined
  while (Date.now() - start < timeoutMs) {
    last = await readTransport(page)
    if (last === expected) return last
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  return last
}

test.describe('stage2 step5 transport degradation', () => {
  test('case1 sse double-tab: A put / update / delete propagates to B within 2s', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-sse', 'realtime-sse profile only')

    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))
      await activateProvidersObserver(pageA)
      await activateProvidersObserver(pageB)

      // Profile sanity: SSE must be the active transport.
      expect(await waitForTransport(pageB, 'sse'), 'B should land on sse transport').toBe('sse')

      const id = `step5-sse-${Math.random().toString(36).slice(2, 10)}`

      await pageA.evaluate(async (p) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.providers.put(p)
      }, buildProvider(id, 'sse-v1'))
      const created = await pollUntil(
        () => dumpTable<{ id: string; name: string }>(pageB, 'providers'),
        rows => rows.find(r => r.id === id)?.name === 'sse-v1',
        2_000
      )
      expect(
        created.ok,
        `B should see sse-v1 within 2s (elapsed=${created.elapsedMs}ms); ` +
          `last B rows: ${JSON.stringify(created.last.map(r => ({ id: r.id, name: r.name })))}`
      ).toBe(true)

      await pageA.evaluate(async (p) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.providers.put(p)
      }, buildProvider(id, 'sse-v2'))
      const updated = await pollUntil(
        () => dumpTable<{ id: string; name: string }>(pageB, 'providers'),
        rows => rows.find(r => r.id === id)?.name === 'sse-v2',
        2_000
      )
      expect(
        updated.ok,
        `B should see sse-v2 within 2s (elapsed=${updated.elapsedMs}ms)`
      ).toBe(true)

      await pageA.evaluate(async (delId) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.providers.delete(delId)
      }, id)
      const removed = await pollUntil(
        () => dumpTable<{ id: string }>(pageB, 'providers'),
        rows => !rows.some(r => r.id === id),
        2_000
      )
      expect(
        removed.ok,
        `provider ${id} should be gone from B within 2s after delete (elapsed=${removed.elapsedMs}ms)`
      ).toBe(true)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case2 poll eventual within 5s: A put converges on B via 5s polling', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-poll', 'realtime-poll profile only')
    test.slow() // 5s budget per assertion + steady-state setup

    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))
      await activateProvidersObserver(pageA)
      await activateProvidersObserver(pageB)

      expect(await waitForTransport(pageB, 'poll'), 'B should land on poll transport').toBe('poll')

      const id = `step5-poll-${Math.random().toString(36).slice(2, 10)}`
      await pageA.evaluate(async (p) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.providers.put(p)
      }, buildProvider(id, 'poll-v1'))

      // 5s polling interval + a little slack for timer drift / first tick.
      const converged = await pollUntil(
        () => dumpTable<{ id: string; name: string }>(pageB, 'providers'),
        rows => rows.find(r => r.id === id)?.name === 'poll-v1',
        7_000,
        200
      )
      expect(
        converged.ok,
        `B should converge on poll-v1 within 7s (elapsed=${converged.elapsedMs}ms); ` +
          `last B rows: ${JSON.stringify(converged.last.map(r => ({ id: r.id, name: r.name })))}`
      ).toBe(true)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case3 auto falls back to sse when ws blocked', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-auto', 'realtime-auto profile only')

    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    // Block WS on B before any navigation so the auto picker never gets a
    // working WS handshake on this context.
    await blockWS(ctxB)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))
      await activateProvidersObserver(pageA)
      await activateProvidersObserver(pageB)

      const transport = await waitForTransport(pageB, 'sse', 8_000)
      expect(transport, `B should fall back to sse when ws is blocked, got ${transport}`).toBe('sse')

      const id = `step5-auto-sse-${Math.random().toString(36).slice(2, 10)}`
      await pageA.evaluate(async (p) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.providers.put(p)
      }, buildProvider(id, 'auto-sse-v1'))
      const converged = await pollUntil(
        () => dumpTable<{ id: string; name: string }>(pageB, 'providers'),
        rows => rows.find(r => r.id === id)?.name === 'auto-sse-v1',
        2_500
      )
      expect(
        converged.ok,
        `B should pick up auto-sse-v1 via SSE within 2.5s (elapsed=${converged.elapsedMs}ms)`
      ).toBe(true)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case4 auto falls back to poll when sse blocked', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-auto', 'realtime-auto profile only')
    test.slow()

    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    await blockWS(ctxB)
    await blockSSE(ctxB)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))
      await activateProvidersObserver(pageA)
      await activateProvidersObserver(pageB)

      const transport = await waitForTransport(pageB, 'poll', 12_000)
      expect(transport, `B should fall back to poll when ws+sse blocked, got ${transport}`).toBe('poll')

      const id = `step5-auto-poll-${Math.random().toString(36).slice(2, 10)}`
      // Use server-side put so we don't depend on B's ability to round-trip
      // anything mid-fallback. A still has working transports, but going via
      // the backend keeps the test focused on B's read path.
      await putProvider(user.accessForBackend, id, buildProvider(id, 'auto-poll-v1'))

      const converged = await pollUntil(
        () => dumpTable<{ id: string; name: string }>(pageB, 'providers'),
        rows => rows.find(r => r.id === id)?.name === 'auto-poll-v1',
        7_000,
        200
      )
      expect(
        converged.ok,
        `B should converge on auto-poll-v1 within 7s (elapsed=${converged.elapsedMs}ms)`
      ).toBe(true)
    } finally {
      // Restore network state for safety even though contexts are about to close.
      await setOffline(ctxB, false)
      await ctxA.close()
      await ctxB.close()
    }
  })
})
