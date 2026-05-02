// Stage 3 / 批次-3b — assistants realtime fan-out / fallback / quiescence.
// id-PK envelope (mirrors providers / Stage 2 step4); 4 cases:
//   case1 ws double-tab: A repo put / update / delete propagates to B within 1.5s
//   case2 ws reconnect catch-up after 30s offline window
//   case3 providers-rest profile (no realtime): no propagation without reload
//   case4 baseline (flag off): no backend traffic, no WS connections

import { test, expect, type Page } from '@playwright/test'
import { exposeReady, dumpTable } from '../helpers/db'
import { putAssistant, deleteAssistant } from '../helpers/backend'
import { registerViaApi, loginApi, injectAuth, type TokenPair } from '../helpers/auth'
import { openContextsForUsers } from '../helpers/tabs'
import { expectRowSync } from '../helpers/sync'
import { setOffline } from '../helpers/net'

function uniqEmail(): string {
  return `stage3-asst-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
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

async function activateAssistantsObserver(page: Page): Promise<void> {
  await page.evaluate(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(window as any).__stage3_asst_obs__ = (window as any).__repos__.assistants.observeList()
  })
  await page.evaluate(async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await (window as any).__repos__.assistants.list()
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

const SAMPLE_ASSISTANT = {
  name: 'A1',
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

test.describe('stage3 batch-3b assistants realtime', () => {
  test('case1 ws double-tab: A put / update / delete propagates to B within 1.5s', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-ws', 'realtime-ws profile only')

    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))
      await activateAssistantsObserver(pageA)
      await activateAssistantsObserver(pageB)

      const id = `asst-${Math.random().toString(36).slice(2, 10)}`

      await pageA.evaluate(async ({ id, data }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.assistants.put({ ...data, id })
      }, { id, data: { ...SAMPLE_ASSISTANT, name: 'name-v1' } })
      await expectRowSync(pageA, pageB, 'assistants', id, { withinMs: 1_500 })

      await pageA.evaluate(async ({ id, data }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.assistants.put({ ...data, id })
      }, { id, data: { ...SAMPLE_ASSISTANT, name: 'name-v2' } })
      const updated = await pollUntil(
        () => dumpTable<{ id: string; name?: string }>(pageB, 'assistants'),
        rows => rows.find(r => r.id === id)?.name === 'name-v2',
        1_500
      )
      expect(
        updated.ok,
        `B should see updated name='name-v2' within 1.5s; ` +
          `last B rows: ${JSON.stringify(updated.last.map(r => ({ id: r.id, name: r.name })))}`
      ).toBe(true)

      await pageA.evaluate(async (id) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.assistants.delete(id)
      }, id)
      const removed = await pollUntil(
        () => dumpTable<{ id: string }>(pageB, 'assistants'),
        rows => !rows.some(r => r.id === id),
        1_500
      )
      expect(
        removed.ok,
        `assistant ${id} should be gone from B within 1.5s after A delete; ` +
          `last B rows: ${JSON.stringify(removed.last.map(r => r.id))}`
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
      await activateAssistantsObserver(pageA)
      await activateAssistantsObserver(pageB)

      const idCreate = `asst-c-${Math.random().toString(36).slice(2, 10)}`
      const idUpdate = `asst-u-${Math.random().toString(36).slice(2, 10)}`
      const idDelete = `asst-d-${Math.random().toString(36).slice(2, 10)}`

      await putAssistant(accessForBackend, idUpdate, { ...SAMPLE_ASSISTANT, id: idUpdate, name: 'pre-update' })
      await putAssistant(accessForBackend, idDelete, { ...SAMPLE_ASSISTANT, id: idDelete, name: 'pre-delete' })
      await expectRowSync(pageA, pageB, 'assistants', idUpdate, { withinMs: 2_000 })
      await expectRowSync(pageA, pageB, 'assistants', idDelete, { withinMs: 2_000 })

      await setOffline(ctxB, true)
      await putAssistant(accessForBackend, idCreate, { ...SAMPLE_ASSISTANT, id: idCreate, name: 'created-while-offline' })
      await putAssistant(accessForBackend, idUpdate, { ...SAMPLE_ASSISTANT, id: idUpdate, name: 'updated-while-offline' })
      await deleteAssistant(accessForBackend, idDelete)
      await new Promise(resolve => setTimeout(resolve, 30_000))

      await setOffline(ctxB, false)
      const converged = await pollUntil(
        () => dumpTable<{ id: string; name?: string }>(pageB, 'assistants'),
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
      await pageB.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.assistants.list()
      })

      const id = `asst-rest-${Math.random().toString(36).slice(2, 10)}`
      await pageA.evaluate(async ({ id, data }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.assistants.put({ ...data, id })
      }, { id, data: { ...SAMPLE_ASSISTANT, name: 'rest-only' } })

      await new Promise(resolve => setTimeout(resolve, 1_500))
      const beforeReload = await dumpTable<{ id: string }>(pageB, 'assistants')
      expect(
        beforeReload.some(r => r.id === id),
        `providers-rest must NOT propagate without reload; B saw ${id} before reload ` +
          `(rows=${JSON.stringify(beforeReload.map(r => r.id))})`
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
        await (window as any).__repos__.assistants.list()
      })
      const afterReload = await dumpTable<{ id: string }>(pageB, 'assistants')
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
    const assistants = await dumpTable(page, 'assistants')
    expect(Array.isArray(assistants)).toBe(true)
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
