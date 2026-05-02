// Stage 3 / 批次-3a — reactives realtime fan-out / fallback / quiescence.
// Mirrors stage2/step4-providers-realtime.spec.ts but for the KV table:
// - case1 ws double-tab: A repo put / update / delete propagates to B within 1.5s
// - case2 ws reconnect catch-up after 30s offline window
// - case3 providers-rest profile (no realtime): no propagation without reload
// - case4 baseline (flag off): no backend traffic, no WS connections
//
// NOTE: case4 verifies the same flag-off byte-identical claim as the providers
// case4 but specifically reads the reactives table; in baseline mode reactives
// stays a pure dexie table (no http hits) just like providers.

import { test, expect, type Page } from '@playwright/test'
import { exposeReady, dumpTable } from '../helpers/db'
import { putReactive, deleteReactive } from '../helpers/backend'
import { registerViaApi, loginApi, injectAuth, type TokenPair } from '../helpers/auth'
import { openContextsForUsers } from '../helpers/tabs'
import { expectKvRowSync } from '../helpers/sync'
import { setOffline } from '../helpers/net'

function uniqEmail(): string {
  return `stage3-react-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
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

async function activateReactivesObserver(page: Page): Promise<void> {
  // Same shape as providers' observer activation: hold the ShallowRef on
  // window so its underlying subscription stays alive across evaluate calls.
  await page.evaluate(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(window as any).__stage3_react_obs__ = (window as any).__repos__.reactives.observeList()
  })
  await page.evaluate(async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await (window as any).__repos__.reactives.list()
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

test.describe('stage3 batch-3a reactives realtime', () => {
  test('case1 ws double-tab: A put / update / delete propagates to B within 1.5s', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-ws', 'realtime-ws profile only')

    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))
      await activateReactivesObserver(pageA)
      await activateReactivesObserver(pageB)

      const key = `react-${Math.random().toString(36).slice(2, 10)}`

      // (a) create via repo on A → B sees the row.
      await pageA.evaluate(async ({ k, v }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.reactives.put({ key: k, value: v })
      }, { k: key, v: { name: 'name-v1' } })
      await expectKvRowSync(pageA, pageB, 'reactives', 'key', key, { withinMs: 1_500 })

      // (b) update via repo on A → B reflects the new value.
      await pageA.evaluate(async ({ k, v }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.reactives.put({ key: k, value: v })
      }, { k: key, v: { name: 'name-v2' } })
      const updated = await pollUntil(
        () => dumpTable<{ key: string; value: { name: string } }>(pageB, 'reactives'),
        rows => rows.find(r => r.key === key)?.value?.name === 'name-v2',
        1_500
      )
      expect(
        updated.ok,
        `B should see updated value name='name-v2' for ${key} within 1.5s; ` +
          `last B rows: ${JSON.stringify(updated.last.map(r => ({ key: r.key, name: r.value?.name })))}`
      ).toBe(true)

      // (c) delete via repo on A → B drops the row.
      await pageA.evaluate(async (k) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.reactives.delete(k)
      }, key)
      const removed = await pollUntil(
        () => dumpTable<{ key: string }>(pageB, 'reactives'),
        rows => !rows.some(r => r.key === key),
        1_500
      )
      expect(
        removed.ok,
        `reactive ${key} should be gone from B within 1.5s after A delete; ` +
          `last B rows: ${JSON.stringify(removed.last.map(r => r.key))}`
      ).toBe(true)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case2 ws reconnect catch-up: B offline 30s while A writes 3 ops, B converges within 1.5s', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-ws', 'realtime-ws profile only')
    test.slow()

    const user = await setupTestUser()
    const accessForBackend = user.accessForBackend
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))
      await activateReactivesObserver(pageA)
      await activateReactivesObserver(pageB)

      const kCreate = `react-c-${Math.random().toString(36).slice(2, 10)}`
      const kUpdate = `react-u-${Math.random().toString(36).slice(2, 10)}`
      const kDelete = `react-d-${Math.random().toString(36).slice(2, 10)}`

      await putReactive(accessForBackend, kUpdate, { v: 'pre-update' })
      await putReactive(accessForBackend, kDelete, { v: 'pre-delete' })
      await expectKvRowSync(pageA, pageB, 'reactives', 'key', kUpdate, { withinMs: 2_000 })
      await expectKvRowSync(pageA, pageB, 'reactives', 'key', kDelete, { withinMs: 2_000 })

      await setOffline(ctxB, true)
      await putReactive(accessForBackend, kCreate, { v: 'created-while-offline' })
      await putReactive(accessForBackend, kUpdate, { v: 'updated-while-offline' })
      await deleteReactive(accessForBackend, kDelete)
      await new Promise(resolve => setTimeout(resolve, 30_000))

      await setOffline(ctxB, false)
      const converged = await pollUntil(
        () => dumpTable<{ key: string; value?: { v?: string } }>(pageB, 'reactives'),
        rows => {
          const c = rows.find(r => r.key === kCreate)
          const u = rows.find(r => r.key === kUpdate)
          const d = rows.find(r => r.key === kDelete)
          return !!c && u?.value?.v === 'updated-while-offline' && !d
        },
        1_500
      )
      expect(
        converged.ok,
        `B did not converge within 1.5s after reconnect (elapsed=${converged.elapsedMs}ms); ` +
          `last B rows: ${JSON.stringify(converged.last.map(r => ({ key: r.key, v: r.value?.v })))}`
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
      await pageB.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.reactives.list()
      })

      const key = `react-rest-${Math.random().toString(36).slice(2, 10)}`
      await pageA.evaluate(async ({ k, v }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.reactives.put({ key: k, value: v })
      }, { k: key, v: { mode: 'rest-only' } })

      await new Promise(resolve => setTimeout(resolve, 1_500))
      const beforeReload = await dumpTable<{ key: string }>(pageB, 'reactives')
      expect(
        beforeReload.some(r => r.key === key),
        `providers-rest must NOT propagate without reload; B saw ${key} before reload ` +
          `(rows=${JSON.stringify(beforeReload.map(r => r.key))})`
      ).toBe(false)

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
        await (window as any).__repos__.reactives.list()
      })
      const afterReload = await dumpTable<{ key: string }>(pageB, 'reactives')
      expect(
        afterReload.some(r => r.key === key),
        `after reload + list(), B should pull ${key} from server ` +
          `(rows=${JSON.stringify(afterReload.map(r => r.key))})`
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
    const reactives = await dumpTable(page, 'reactives')
    expect(Array.isArray(reactives)).toBe(true)
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
