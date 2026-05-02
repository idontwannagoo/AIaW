// Stage 4 / 批次-4b — dialogs server-routed CRUD + workspace cascade
// realtime fan-out.
//
// 4 cases (matching the plan's 通过判据 line for batch-4b):
//   case1 ws double-tab put / update / delete propagates within 1.5s
//   case2 ws double-tab cascade: A's deleteItem(workspace) ⇒ B's dialogs
//     for that workspace tombstone within 1.5s via realtime; server-side
//     dialog rows are tombstones too
//   case3 providers-rest no-realtime: A change is invisible to B until reload
//   case4 baseline byte-identical: zero backend traffic, zero ws
//
// Notes:
//   - putDialog requires a workspaceId that already exists for the user;
//     the server returns 409 otherwise. Each case seeds a workspace via
//     repos.workspaces.put first.
//   - assistants is server-routed (since 批次-3b); workspaces since 4a;
//     dialogs since 4b. messages / items / artifacts still in dexie.
//     deleteItem cascade leans on the new server-side workspaces+dialogs
//     cascade for the realtime cross-tab path; the dexie tables only
//     update on A and the spec asserts only what's actually testable
//     across BrowserContexts (B's IDB never sees A's local-only rows).

import { test, expect, type Page } from '@playwright/test'
import { exposeReady, dumpTable } from '../helpers/db'
import { listWorkspaces, listDialogs } from '../helpers/backend'
import { registerViaApi, loginApi, injectAuth, type TokenPair } from '../helpers/auth'
import { openContextsForUsers } from '../helpers/tabs'
import { expectRowSync } from '../helpers/sync'

function uniqEmail(): string {
  return `stage4-dlg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
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

async function activateObservers(page: Page): Promise<void> {
  // Observers are what wires the realtime subscription; without one, the
  // listener isn't attached and the per-table WS / SSE filter rejects
  // events. Both workspaces and dialogs are server-routed at 4b.
  await page.evaluate(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const w = window as any
    w.__stage4b_obs__ = {
      workspaces: w.__repos__.workspaces.observeList(),
      dialogs: w.__repos__.dialogs.observeList()
    }
  })
  await page.evaluate(async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const w = window as any
    await w.__repos__.workspaces.list()
    await w.__repos__.dialogs.list()
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

const SAMPLE_WORKSPACE = {
  name: 'WS-1',
  avatar: { type: 'icon', icon: 'sym_o_deployed_code' },
  type: 'workspace',
  parentId: '$root',
  prompt: '',
  indexContent: '# index',
  vars: {},
  listOpen: { assistants: true, artifacts: false, dialogs: true }
}

function buildDialog(workspaceId: string, name: string) {
  return {
    name,
    workspaceId,
    msgTree: { $root: [] },
    msgRoute: [],
    inputVars: {}
  }
}

test.describe('stage4 batch-4b dialogs server-routed', () => {
  test('case1 ws double-tab: put + update + delete propagate within 1.5s', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-ws', 'realtime-ws profile only')

    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))
      await activateObservers(pageA)
      await activateObservers(pageB)

      const wsId = `ws-${Math.random().toString(36).slice(2, 10)}`
      const dlgId = `dlg-${Math.random().toString(36).slice(2, 10)}`

      // Seed workspace (required for dialog FK)
      await pageA.evaluate(async ({ id, data }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.workspaces.put({ ...data, id })
      }, { id: wsId, data: SAMPLE_WORKSPACE })
      await expectRowSync(pageA, pageB, 'workspaces', wsId, { withinMs: 1_500 })

      // Dialog put propagates A → B
      await pageA.evaluate(async ({ id, data }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.dialogs.put({ ...data, id })
      }, { id: dlgId, data: buildDialog(wsId, 'name-v1') })
      await expectRowSync(pageA, pageB, 'dialogs', dlgId, { withinMs: 1_500 })

      // Update name → B sees v2
      await pageA.evaluate(async ({ id, data }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.dialogs.put({ ...data, id })
      }, { id: dlgId, data: buildDialog(wsId, 'name-v2') })
      const updated = await pollUntil(
        () => dumpTable<{ id: string; name?: string }>(pageB, 'dialogs'),
        rows => rows.find(r => r.id === dlgId)?.name === 'name-v2',
        1_500
      )
      expect(
        updated.ok,
        'B should see updated name=\'name-v2\' within 1.5s; ' +
          `last B rows: ${JSON.stringify(updated.last.map(r => ({ id: r.id, name: r.name })))}`
      ).toBe(true)

      // Explicit delete → B sees the row gone
      await pageA.evaluate(async ({ id }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.dialogs.delete(id)
      }, { id: dlgId })
      const removed = await pollUntil(
        () => dumpTable<{ id: string }>(pageB, 'dialogs'),
        rows => !rows.some(r => r.id === dlgId),
        1_500
      )
      expect(
        removed.ok,
        `dialog ${dlgId} should be gone from B within 1.5s; ` +
          `last B rows: ${JSON.stringify(removed.last.map(r => r.id))}`
      ).toBe(true)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case2 ws double-tab cascade: workspace cascade tombstones server dialog rows + B receives ws delete', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-ws', 'realtime-ws profile only')

    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))
      await activateObservers(pageA)
      await activateObservers(pageB)

      const wsId = `ws-${Math.random().toString(36).slice(2, 10)}`
      const dlg1 = `dlg-${Math.random().toString(36).slice(2, 10)}`
      const dlg2 = `dlg-${Math.random().toString(36).slice(2, 10)}`

      // Seed workspace + 2 dialogs via repos (server-routed)
      await pageA.evaluate(async ({ wsId, dlg1, dlg2, wsBody }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const w = window as any
        await w.__repos__.workspaces.put({ ...wsBody, id: wsId })
        await w.__repos__.dialogs.put({
          id: dlg1,
          name: 'd1',
          workspaceId: wsId,
          msgTree: { $root: [] },
          msgRoute: [],
          inputVars: {}
        })
        await w.__repos__.dialogs.put({
          id: dlg2,
          name: 'd2',
          workspaceId: wsId,
          msgTree: { $root: [] },
          msgRoute: [],
          inputVars: {}
        })
      }, { wsId, dlg1, dlg2, wsBody: SAMPLE_WORKSPACE })

      await expectRowSync(pageA, pageB, 'workspaces', wsId, { withinMs: 1_500 })
      await expectRowSync(pageA, pageB, 'dialogs', dlg1, { withinMs: 1_500 })
      await expectRowSync(pageA, pageB, 'dialogs', dlg2, { withinMs: 1_500 })

      // Replicate stores/workspaces.ts::deleteItem — same sequential
      // repo calls in the same order. The server-side workspace cascade
      // also tombstones dialogs, but the front-end's explicit per-row
      // dialog delete still fires (idempotent against tombstones).
      await pageA.evaluate(async ({ wsId }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const w = window as any
        const dialogIds = await w.__repos__.dialogs.findKeys({ where: { workspaceId: wsId } })
        for (const dialogId of dialogIds) {
          await w.__repos__.messages.deleteWhere({ where: { dialogId } })
          await w.__repos__.items.deleteWhere({ where: { dialogId } })
        }
        await w.__repos__.dialogs.deleteWhere({ where: { workspaceId: wsId } })
        await w.__repos__.assistants.deleteWhere({ where: { workspaceId: wsId } })
        await w.__repos__.artifacts.deleteWhere({ where: { workspaceId: wsId } })
        await w.__repos__.workspaces.delete(wsId)
      }, { wsId })

      // Server-side: every dialog under the workspace is a tombstone
      const serverDialogs = await listDialogs(user.accessForBackend, 0)
      const dlg1Tomb = serverDialogs.find(r => r.id === dlg1)
      const dlg2Tomb = serverDialogs.find(r => r.id === dlg2)
      expect(
        dlg1Tomb && dlg2Tomb,
        `server dialogs tombstones missing; rows: ${JSON.stringify(serverDialogs.map(r => ({ id: r.id, deleted: r.deleted })))}`
      ).toBeTruthy()
      expect(dlg1Tomb!.deleted).toBe(true)
      expect(dlg1Tomb!.data).toBeNull()
      expect(dlg2Tomb!.deleted).toBe(true)
      expect(dlg2Tomb!.data).toBeNull()

      // Server-side: workspace is also a tombstone
      const serverWorkspaces = await listWorkspaces(user.accessForBackend, 0)
      const wsTomb = serverWorkspaces.find(r => r.id === wsId)
      expect(wsTomb).toBeTruthy()
      expect(wsTomb!.deleted).toBe(true)

      // B side: both dialog rows gone via WS delete events within 1.5s
      const removed = await pollUntil(
        () => dumpTable<{ id: string }>(pageB, 'dialogs'),
        rows => !rows.some(r => r.id === dlg1) && !rows.some(r => r.id === dlg2),
        1_500
      )
      expect(
        removed.ok,
        `dialogs ${dlg1}/${dlg2} should be gone from B within 1.5s after cascade; ` +
          `last B rows: ${JSON.stringify(removed.last.map(r => r.id))}`
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
      // B observes once so cache is warm
      await pageB.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const w = window as any
        await w.__repos__.workspaces.list()
        await w.__repos__.dialogs.list()
      })

      const wsId = `ws-rest-${Math.random().toString(36).slice(2, 10)}`
      const dlgId = `dlg-rest-${Math.random().toString(36).slice(2, 10)}`
      await pageA.evaluate(async ({ wsId, dlgId, wsBody }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const w = window as any
        await w.__repos__.workspaces.put({ ...wsBody, id: wsId })
        await w.__repos__.dialogs.put({
          id: dlgId,
          name: 'rest-only',
          workspaceId: wsId,
          msgTree: { $root: [] },
          msgRoute: [],
          inputVars: {}
        })
      }, { wsId, dlgId, wsBody: SAMPLE_WORKSPACE })

      await new Promise(resolve => setTimeout(resolve, 1_500))
      const beforeReload = await dumpTable<{ id: string }>(pageB, 'dialogs')
      expect(
        beforeReload.some(r => r.id === dlgId),
        `providers-rest must NOT propagate dialogs without reload; B saw ${dlgId} before reload ` +
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
        await (window as any).__repos__.dialogs.list()
      })
      const afterReload = await dumpTable<{ id: string }>(pageB, 'dialogs')
      expect(
        afterReload.some(r => r.id === dlgId),
        `after reload + list(), B should pull ${dlgId} from server ` +
          `(rows=${JSON.stringify(afterReload.map(r => r.id))})`
      ).toBe(true)

      const serverRows = await listDialogs(user.accessForBackend, 0)
      expect(
        serverRows.some(r => r.id === dlgId && r.deleted === false),
        `server should hold dialog ${dlgId}; rows=${JSON.stringify(serverRows.map(r => ({ id: r.id, deleted: r.deleted })))}`
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
    const dialogs = await dumpTable(page, 'dialogs')
    expect(Array.isArray(dialogs)).toBe(true)
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
