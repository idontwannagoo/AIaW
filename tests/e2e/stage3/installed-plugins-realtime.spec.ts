// Stage 3 / 批次-3b — installed_plugins realtime fan-out / fallback / quiescence.
// KV-shaped envelope (composite (user_id, key) PK like reactives, but `data`
// carries the full plugin row not a value blob). 4 cases mirror reactives:
//   case1 ws double-tab realtime
//   case2 ws reconnect catch-up
//   case3 providers-rest no-realtime
//   case4 baseline byte-identical

import { test, expect, type Page } from '@playwright/test'
import { exposeReady, dumpTable } from '../helpers/db'
import { putInstalledPlugin, deleteInstalledPlugin } from '../helpers/backend'
import { registerViaApi, loginApi, injectAuth, type TokenPair } from '../helpers/auth'
import { openContextsForUsers } from '../helpers/tabs'
import { expectKvRowSync } from '../helpers/sync'
import { setOffline } from '../helpers/net'

function uniqEmail(): string {
  return `stage3-plg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
}

interface TestUser { email: string; password: string; accessForBackend: string }

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

async function activatePluginsObserver(page: Page): Promise<void> {
  await page.evaluate(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(window as any).__stage3_plg_obs__ = (window as any).__repos__.installedPlugins.observeList()
  })
  await page.evaluate(async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await (window as any).__repos__.installedPlugins.list()
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

function lobeData(key: string, available = true): Record<string, unknown> {
  return {
    id: `id-${key}`,
    key,
    type: 'lobe',
    available,
    manifest: { identifier: key.replace('lobe:', ''), meta: {}, api: [] }
  }
}

test.describe('stage3 batch-3b installed_plugins realtime', () => {
  test('case1 ws double-tab: A put / update / delete propagates to B within 1.5s', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-ws', 'realtime-ws profile only')

    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))
      await activatePluginsObserver(pageA)
      await activatePluginsObserver(pageB)

      const key = `lobe:p-${Math.random().toString(36).slice(2, 10)}`

      await pageA.evaluate(async ({ data }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.installedPlugins.put(data)
      }, { data: lobeData(key, true) })
      await expectKvRowSync(pageA, pageB, 'installedPluginsV2', 'key', key, { withinMs: 1_500 })

      await pageA.evaluate(async ({ data }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.installedPlugins.put(data)
      }, { data: lobeData(key, false) })
      const updated = await pollUntil(
        () => dumpTable<{ key: string; available?: boolean }>(pageB, 'installedPluginsV2'),
        rows => rows.find(r => r.key === key)?.available === false,
        1_500
      )
      expect(
        updated.ok,
        `B should see updated available=false within 1.5s; ` +
          `last B rows: ${JSON.stringify(updated.last.map(r => ({ key: r.key, available: r.available })))}`
      ).toBe(true)

      await pageA.evaluate(async (k) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.installedPlugins.delete(k)
      }, key)
      const removed = await pollUntil(
        () => dumpTable<{ key: string }>(pageB, 'installedPluginsV2'),
        rows => !rows.some(r => r.key === key),
        1_500
      )
      expect(
        removed.ok,
        `plugin ${key} should be gone from B within 1.5s after A delete; ` +
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
      await activatePluginsObserver(pageA)
      await activatePluginsObserver(pageB)

      const kCreate = `lobe:c-${Math.random().toString(36).slice(2, 10)}`
      const kUpdate = `lobe:u-${Math.random().toString(36).slice(2, 10)}`
      const kDelete = `lobe:d-${Math.random().toString(36).slice(2, 10)}`

      await putInstalledPlugin(accessForBackend, kUpdate, lobeData(kUpdate, true))
      await putInstalledPlugin(accessForBackend, kDelete, lobeData(kDelete, true))
      await expectKvRowSync(pageA, pageB, 'installedPluginsV2', 'key', kUpdate, { withinMs: 2_000 })
      await expectKvRowSync(pageA, pageB, 'installedPluginsV2', 'key', kDelete, { withinMs: 2_000 })

      await setOffline(ctxB, true)
      await putInstalledPlugin(accessForBackend, kCreate, lobeData(kCreate, true))
      await putInstalledPlugin(accessForBackend, kUpdate, lobeData(kUpdate, false))
      await deleteInstalledPlugin(accessForBackend, kDelete)
      await new Promise(resolve => setTimeout(resolve, 30_000))

      await setOffline(ctxB, false)
      const converged = await pollUntil(
        () => dumpTable<{ key: string; available?: boolean }>(pageB, 'installedPluginsV2'),
        rows => {
          const c = rows.find(r => r.key === kCreate)
          const u = rows.find(r => r.key === kUpdate)
          const d = rows.find(r => r.key === kDelete)
          return !!c && u?.available === false && !d
        },
        1_500
      )
      expect(
        converged.ok,
        `B did not converge within 1.5s after reconnect (elapsed=${converged.elapsedMs}ms); ` +
          `last B rows: ${JSON.stringify(converged.last.map(r => ({ key: r.key, available: r.available })))}`
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
        await (window as any).__repos__.installedPlugins.list()
      })

      const key = `lobe:rest-${Math.random().toString(36).slice(2, 10)}`
      await pageA.evaluate(async ({ data }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.installedPlugins.put(data)
      }, { data: lobeData(key, true) })

      await new Promise(resolve => setTimeout(resolve, 1_500))
      const beforeReload = await dumpTable<{ key: string }>(pageB, 'installedPluginsV2')
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
        await (window as any).__repos__.installedPlugins.list()
      })
      const afterReload = await dumpTable<{ key: string }>(pageB, 'installedPluginsV2')
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
    page.on('websocket', (ws) => { wsUrls.push(ws.url()) })

    await page.goto('/')
    await exposeReady(page)
    const plugins = await dumpTable(page, 'installedPluginsV2')
    expect(Array.isArray(plugins)).toBe(true)
    await page.waitForTimeout(1_000)

    expect(
      backendCalls,
      `baseline must not call 127.0.0.1:9011; saw ${backendCalls.length}: ${JSON.stringify(backendCalls)}`
    ).toEqual([])
    expect(wsUrls, `baseline must not open any ws; saw ${JSON.stringify(wsUrls)}`).toEqual([])
  })
})
