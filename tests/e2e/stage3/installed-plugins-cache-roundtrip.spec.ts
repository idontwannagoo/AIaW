// Stage 3 / 批次-3b — installed_plugins IndexedDB cache <→ server roundtrip.
// KV-shaped table, 2 cases mirror reactives-cache-roundtrip:
//   case1: clear IDB → reload → server data re-pulled
//   case2: write + reload → list() does not spam ?since=0

import { test, expect, type Page } from '@playwright/test'
import { exposeReady, dumpTable, clearAll } from '../helpers/db'
import { putInstalledPlugin } from '../helpers/backend'
import { registerViaApi, loginApi, injectAuth, type TokenPair } from '../helpers/auth'

function uniqEmail(): string {
  return `stage3-plg-cache-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
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

function lobeData(key: string, available = true): Record<string, unknown> {
  return {
    id: `id-${key}`,
    key,
    type: 'lobe',
    available,
    manifest: { identifier: key.replace('lobe:', ''), meta: {}, api: [] }
  }
}

test.describe('stage3 batch-3b installed_plugins cache roundtrip', () => {
  test('case1 cleared IndexedDB → reload → server data re-pulled into cache', async ({ browser }, testInfo) => {
    test.skip(
      !['providers-rest', 'realtime-ws', 'realtime-auto'].includes(testInfo.project.name),
      'cache-roundtrip needs a server-routed profile'
    )

    const user = await setupTestUser()
    await putInstalledPlugin(user.accessForBackend, 'lobe:c1', lobeData('lobe:c1', true))
    await putInstalledPlugin(user.accessForBackend, 'lobe:c2', lobeData('lobe:c2', false))

    const ctx = await browser.newContext()
    try {
      const page = await ctx.newPage()
      await bootSession(page, await freshSession(user))
      await page.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.installedPlugins.list()
      })
      const seeded = await dumpTable<{ key: string }>(page, 'installedPluginsV2')
      const seededKeys = seeded.map(r => r.key)
      expect(
        seededKeys.includes('lobe:c1') && seededKeys.includes('lobe:c2'),
        `seed pull should populate both keys (rows=${JSON.stringify(seededKeys)})`
      ).toBe(true)

      await clearAll(page, ['installedPluginsV2'])
      const wiped = await dumpTable<{ key: string }>(page, 'installedPluginsV2')
      expect(wiped, 'IDB clear should empty installedPluginsV2 cache').toEqual([])

      await injectAuth(page, await freshSession(user))
      await page.reload()
      await exposeReady(page)
      await page.waitForFunction(() => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const auth = (window as any).__authSource__
        return !!(auth && auth.currentToken && auth.currentToken())
      }, undefined, { timeout: 10_000 })
      await page.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.installedPlugins.list()
      })
      const refilled = await dumpTable<{ key: string; available: boolean }>(page, 'installedPluginsV2')
      const c1 = refilled.find(r => r.key === 'lobe:c1')
      const c2 = refilled.find(r => r.key === 'lobe:c2')
      expect(
        c1?.available === true && c2?.available === false,
        `cache should be re-pulled from server after clear+reload ` +
          `(rows=${JSON.stringify(refilled.map(r => ({ key: r.key, available: r.available })))})`
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

      await page.evaluate(async ({ d1, d2 }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const r = (window as any).__repos__.installedPlugins
        await r.put(d1)
        await r.put(d2)
      }, { d1: lobeData('lobe:w1'), d2: lobeData('lobe:w2') })
      const seeded = await dumpTable<{ key: string }>(page, 'installedPluginsV2')
      const seededKeys = seeded.map(r => r.key)
      expect(
        seededKeys.includes('lobe:w1') && seededKeys.includes('lobe:w2'),
        `cache should hold both written rows (rows=${JSON.stringify(seededKeys)})`
      ).toBe(true)

      await injectAuth(page, await freshSession(user))
      await page.reload()
      await exposeReady(page)
      await page.waitForFunction(() => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const auth = (window as any).__authSource__
        return !!(auth && auth.currentToken && auth.currentToken())
      }, undefined, { timeout: 10_000 })

      const reqs: string[] = []
      page.on('request', (req) => {
        if (req.url().includes('/api/v1/installed-plugins')) reqs.push(req.url())
      })
      await page.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.installedPlugins.list()
      })
      const afterReload = await dumpTable<{ key: string }>(page, 'installedPluginsV2')
      expect(
        afterReload.some(r => r.key === 'lobe:w1') && afterReload.some(r => r.key === 'lobe:w2'),
        `cache should retain rows after reload (rows=${JSON.stringify(afterReload.map(r => r.key))})`
      ).toBe(true)
      expect(
        reqs.length,
        `expected ≤ 1 installed-plugins request on cached list, saw ${reqs.length}: ${JSON.stringify(reqs)}`
      ).toBeLessThanOrEqual(1)
    } finally {
      await ctx.close()
    }
  })
})
