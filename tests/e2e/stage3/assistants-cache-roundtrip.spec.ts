// Stage 3 / 批次-3b — assistants IndexedDB cache <→ server roundtrip.
// Mirrors reactives-cache-roundtrip.spec.ts. Two cases:
//   case1: clear IndexedDB → reload → server data is re-pulled into cache
//   case2: write + reload → list() does not spam ?since=0

import { test, expect, type Page } from '@playwright/test'
import { exposeReady, dumpTable, clearAll } from '../helpers/db'
import { putAssistant } from '../helpers/backend'
import { registerViaApi, loginApi, injectAuth, type TokenPair } from '../helpers/auth'

function uniqEmail(): string {
  return `stage3-asst-cache-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
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

const SAMPLE_ASSISTANT = {
  name: 'sample',
  avatar: { type: 'icon', icon: 'sym_o_robot' },
  workspaceId: 'ws-1',
  prompt: '',
  promptTemplate: '',
  promptVars: [],
  provider: null,
  model: null,
  modelSettings: { temperature: 0.6, topP: 1, presencePenalty: 0, frequencyPenalty: 0, maxSteps: 4, maxRetries: 1 },
  plugins: {},
  promptRole: 'system',
  stream: true
}

test.describe('stage3 batch-3b assistants cache roundtrip', () => {
  test('case1 cleared IndexedDB → reload → server data re-pulled into cache', async ({ browser }, testInfo) => {
    test.skip(
      !['providers-rest', 'realtime-ws', 'realtime-auto'].includes(testInfo.project.name),
      'cache-roundtrip needs a server-routed profile'
    )

    const user = await setupTestUser()
    // Per-test unique IDs: the assistants table uses a single-column PK, so
    // a literal 'cache-asst-1' would 409 collide across profile runs that
    // share the same backend (realtime-ws → realtime-auto).
    const id1 = `cache-asst-1-${Math.random().toString(36).slice(2, 10)}`
    const id2 = `cache-asst-2-${Math.random().toString(36).slice(2, 10)}`
    await putAssistant(user.accessForBackend, id1, {
      ...SAMPLE_ASSISTANT, id: id1, name: 'server-v1'
    })
    await putAssistant(user.accessForBackend, id2, {
      ...SAMPLE_ASSISTANT, id: id2, name: 'server-v2'
    })

    const ctx = await browser.newContext()
    try {
      const page = await ctx.newPage()
      await bootSession(page, await freshSession(user))
      await page.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.assistants.list()
      })
      const seeded = await dumpTable<{ id: string }>(page, 'assistants')
      const seededIds = seeded.map(r => r.id)
      expect(
        seededIds.includes(id1) && seededIds.includes(id2),
        `seed pull should populate both server-side rows (rows=${JSON.stringify(seededIds)})`
      ).toBe(true)

      await clearAll(page, ['assistants'])
      const wiped = await dumpTable<{ id: string }>(page, 'assistants')
      expect(
        wiped.some(r => r.id === id1 || r.id === id2),
        `IDB clear should drop seeded rows (rows=${JSON.stringify(wiped.map(r => r.id))})`
      ).toBe(false)

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
        await (window as any).__repos__.assistants.list()
      })
      const refilled = await dumpTable<{ id: string; name: string }>(page, 'assistants')
      const a1 = refilled.find(r => r.id === id1)
      const a2 = refilled.find(r => r.id === id2)
      expect(
        a1?.name === 'server-v1' && a2?.name === 'server-v2',
        `cache should be re-pulled from server after clear+reload ` +
          `(rows=${JSON.stringify(refilled.map(r => ({ id: r.id, name: r.name })))})`
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

      // Unique IDs per profile run — single-column PK clashes otherwise.
      const w1 = `cache-w1-${Math.random().toString(36).slice(2, 10)}`
      const w2 = `cache-w2-${Math.random().toString(36).slice(2, 10)}`
      await page.evaluate(async ({ data, w1, w2 }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const r = (window as any).__repos__.assistants
        await r.put({ ...data, id: w1, name: 'w1' })
        await r.put({ ...data, id: w2, name: 'w2' })
      }, { data: SAMPLE_ASSISTANT, w1, w2 })
      const seeded = await dumpTable<{ id: string }>(page, 'assistants')
      const seededIds = seeded.map(r => r.id)
      expect(
        seededIds.includes(w1) && seededIds.includes(w2),
        `cache should hold both written rows (rows=${JSON.stringify(seededIds)})`
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
        const url = req.url()
        if (url.includes('/api/v1/assistants')) reqs.push(url)
      })
      await page.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.assistants.list()
      })
      const afterReload = await dumpTable<{ id: string; name: string }>(page, 'assistants')
      const r1 = afterReload.find(r => r.id === w1)
      const r2 = afterReload.find(r => r.id === w2)
      expect(
        r1?.name === 'w1' && r2?.name === 'w2',
        `cache should retain both rows after reload (rows=${JSON.stringify(afterReload.map(r => ({ id: r.id, name: r.name })))})`
      ).toBe(true)
      expect(
        reqs.length,
        `expected ≤ 1 assistants request on cached list, saw ${reqs.length}: ${JSON.stringify(reqs)}`
      ).toBeLessThanOrEqual(1)
    } finally {
      await ctx.close()
    }
  })
})
