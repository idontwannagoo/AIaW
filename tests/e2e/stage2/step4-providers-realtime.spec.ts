// Stage 2 Step 4 acceptance — providers realtime fan-out / fallback / quiescence.
// Maps 1:1 to plans/cloud-sync-migration.md Stage 2 Step 4 通过判据 #1-4.
//
// Phase 5 of plans/test-infrastructure.md is "spec-first" — Step 4 code is
// not yet implemented, so case1/case2 (realtime-ws profile) are EXPECTED to
// fail today. Once providers.server.ts wires RemoteSyncSource into observeList
// (per Step 4 plan), they should turn green. case3 (providers-rest) and case4
// (baseline) must already be green and remain green — they verify the
// "flag 关时不应破坏" boundary.
//
// Two-context (not two-tab) setup is intentional: same-context tabs share
// IndexedDB + Dexie BroadcastChannel, which would let A's local put surface
// in B without ever touching the server, masking realtime regressions.
// Separate browser contexts force the path through the backend.

import { test, expect, type Page } from '@playwright/test'
import { exposeReady, dumpTable } from '../helpers/db'
import { putProvider, deleteProvider } from '../helpers/backend'
import { registerViaApi, loginApi, injectAuth, type TokenPair } from '../helpers/auth'
import { openContextsForUsers } from '../helpers/tabs'
import { expectRowSync } from '../helpers/sync'
import { setOffline } from '../helpers/net'

function uniqEmail(): string {
  return `step4-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
}

// Stage 1.5 rotates refresh_tokens on /auth/refresh, so each browser context
// must boot with its own login session — sharing one pair across two contexts
// means whichever boots second 401s (and so does the same context after a
// reload, since addInitScript re-runs and resets localStorage to the now-
// already-consumed token). Helper builds a fresh login pair on demand.
interface TestUser {
  email: string
  password: string
  /** access_token usable for direct backend REST calls (any session works). */
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

async function bootSession(page: Page, pair: TokenPair): Promise<void> {
  await injectAuth(page, pair)
  page.on('pageerror', (e) => console.log('[page-error]', e.message))
  await page.goto('/')
  await exposeReady(page)
  // backend-auth boot's /auth/refresh round-trip must finish before any
  // authed work — otherwise repos.providers.list() fires without a token.
  await page.waitForFunction(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const auth = (window as any).__authSource__
    return !!(auth && auth.currentToken && auth.currentToken())
  }, undefined, { timeout: 10_000 })
}

async function activateProvidersObserver(page: Page): Promise<void> {
  // Per Stage 2 Step 4 plan, providers.server.ts.observeList() is the hook
  // that wires RemoteSyncSource. Hold the returned ShallowRef on window so
  // the underlying subscription doesn't get GC'd between evaluate() calls.
  await page.evaluate(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(window as any).__step4_obs__ = (window as any).__repos__.providers.observeList()
  })
  // Initial pull bootstraps the cache and bumps lastVersion so a subsequent
  // realtime subscription's `since` is correct on first frame.
  await page.evaluate(async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await (window as any).__repos__.providers.list()
  })
}

async function pollUntil<T>(
  fn: () => Promise<T>,
  predicate: (v: T) => boolean,
  withinMs: number,
  pollMs = 50
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

test.describe('stage2 step4 providers realtime', () => {
  test('case1 ws double-tab: A repo put / update / delete propagates to B within 1.5s', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-ws', 'realtime-ws profile only')

    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))
      await activateProvidersObserver(pageA)
      await activateProvidersObserver(pageB)

      const id = `step4-prov-${Math.random().toString(36).slice(2, 10)}`

      // (a) create via repo on A → B sees the row.
      await pageA.evaluate(async (p) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.providers.put(p)
      }, buildProvider(id, 'name-v1'))
      await expectRowSync(pageA, pageB, 'providers', id, { withinMs: 1_500 })

      // (b) update via repo on A → B reflects the new name without reload.
      await pageA.evaluate(async (p) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.providers.put(p)
      }, buildProvider(id, 'name-v2'))
      const updated = await pollUntil(
        () => dumpTable<{ id: string; name: string }>(pageB, 'providers'),
        rows => rows.find(r => r.id === id)?.name === 'name-v2',
        1_500
      )
      expect(
        updated.ok,
        `B should see updated name 'name-v2' for ${id} within 1.5s after A put; ` +
          `last B rows: ${JSON.stringify(updated.last.map(r => ({ id: r.id, name: r.name })))}`
      ).toBe(true)

      // (c) delete via repo on A → B drops the row.
      await pageA.evaluate(async (delId) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.providers.delete(delId)
      }, id)
      const removed = await pollUntil(
        () => dumpTable<{ id: string }>(pageB, 'providers'),
        rows => !rows.some(r => r.id === id),
        1_500
      )
      expect(
        removed.ok,
        `provider ${id} should be gone from B within 1.5s after A delete; ` +
          `last B rows: ${JSON.stringify(removed.last.map(r => r.id))}`
      ).toBe(true)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case2 ws reconnect catch-up: B offline 30s while A writes 3 ops, B converges within 1.5s', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-ws', 'realtime-ws profile only')
    test.slow() // 30s offline window alone exceeds default 30s test timeout

    const user = await setupTestUser()
    const accessForBackend = user.accessForBackend
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))
      await activateProvidersObserver(pageA)
      await activateProvidersObserver(pageB)

      const idCreate = `step4-off-c-${Math.random().toString(36).slice(2, 10)}`
      const idUpdate = `step4-off-u-${Math.random().toString(36).slice(2, 10)}`
      const idDelete = `step4-off-d-${Math.random().toString(36).slice(2, 10)}`

      // Pre-state: idUpdate + idDelete exist on server, both seen by B,
      // before B goes offline.
      await putProvider(accessForBackend, idUpdate, buildProvider(idUpdate, 'pre-update'))
      await putProvider(accessForBackend, idDelete, buildProvider(idDelete, 'pre-delete'))
      await expectRowSync(pageA, pageB, 'providers', idUpdate, { withinMs: 2_000 })
      await expectRowSync(pageA, pageB, 'providers', idDelete, { withinMs: 2_000 })

      // B offline; A makes 3 server-side mutations meanwhile (one of each op).
      await setOffline(ctxB, true)
      await putProvider(accessForBackend, idCreate, buildProvider(idCreate, 'created-while-offline'))
      await putProvider(accessForBackend, idUpdate, buildProvider(idUpdate, 'updated-while-offline'))
      await deleteProvider(accessForBackend, idDelete)
      await new Promise(resolve => setTimeout(resolve, 30_000))

      // B reconnects. The plan budget is 1.5s for full convergence after
      // setOffline(false): WS reconnect handshake + replay of 3 events.
      await setOffline(ctxB, false)
      const converged = await pollUntil(
        () => dumpTable<{ id: string; name?: string }>(pageB, 'providers'),
        rows => {
          const c = rows.find(r => r.id === idCreate)
          const u = rows.find(r => r.id === idUpdate)
          const d = rows.find(r => r.id === idDelete)
          return !!c && u?.name === 'updated-while-offline' && !d
        },
        1_500
      )
      expect(
        converged.ok,
        `B did not converge within 1.5s after reconnect (elapsed=${converged.elapsedMs}ms); ` +
          `last B rows: ${JSON.stringify(converged.last.map(r => ({ id: r.id, name: r.name })))}`
      ).toBe(true)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case3 providers-rest no-realtime: A change is invisible to B until reload', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')

    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))
      // B's initial pull seeds an empty cache.
      await pageB.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.providers.list()
      })

      const id = `step4-rest-${Math.random().toString(36).slice(2, 10)}`
      await pageA.evaluate(async (p) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.providers.put(p)
      }, buildProvider(id, 'rest-only'))

      // No realtime channel on this profile; B's cache must NOT pick up the
      // new row without a reload + explicit pull. Wait long enough that any
      // plausible auto-poll would have fired.
      await new Promise(resolve => setTimeout(resolve, 1_500))
      const beforeReload = await dumpTable<{ id: string }>(pageB, 'providers')
      expect(
        beforeReload.some(r => r.id === id),
        `providers-rest must NOT propagate without reload; B saw ${id} before reload ` +
          `(rows=${JSON.stringify(beforeReload.map(r => r.id))})`
      ).toBe(false)

      // Reload + list() → server pull → row appears.
      // Re-inject a fresh login pair before reload — addInitScript re-runs on
      // reload and would otherwise restore the now-consumed refresh_token,
      // making the post-reload /auth/refresh return 401.
      await injectAuth(pageB, await freshSession(user))
      await pageB.reload()
      await exposeReady(pageB)
      await pageB.waitForFunction(() => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const auth = (window as any).__authSource__
        return !!(auth && auth.currentToken && auth.currentToken())
      }, undefined, { timeout: 10_000 })
      await pageB.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.providers.list()
      })
      const afterReload = await dumpTable<{ id: string }>(pageB, 'providers')
      expect(
        afterReload.some(r => r.id === id),
        `after reload + list(), B should pull ${id} from server ` +
          `(rows=${JSON.stringify(afterReload.map(r => r.id))})`
      ).toBe(true)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case4 baseline byte-identical: zero backend traffic and zero websocket connections', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'baseline', 'baseline profile only')

    const backendCalls: string[] = []
    const wsUrls: string[] = []
    page.on('request', (req) => {
      if (req.url().includes('127.0.0.1:9011')) backendCalls.push(req.url())
    })
    page.on('websocket', (ws) => {
      wsUrls.push(ws.url())
    })

    await page.goto('/')
    await exposeReady(page)
    // Read a couple of tables that the app would touch on first paint; if any
    // of them sneak a backend round-trip with all flags off, this catches it.
    const ws = await dumpTable(page, 'workspaces')
    expect(Array.isArray(ws)).toBe(true)
    const providers = await dumpTable(page, 'providers')
    expect(Array.isArray(providers)).toBe(true)
    // Settle so any post-mount async routines fire.
    await page.waitForTimeout(1_000)

    expect(
      backendCalls,
      `baseline must not call 127.0.0.1:9011; saw ${backendCalls.length}: ${JSON.stringify(backendCalls)}`
    ).toEqual([])
    expect(
      wsUrls,
      `baseline must not open any ws; saw ${JSON.stringify(wsUrls)}`
    ).toEqual([])
  })
})
