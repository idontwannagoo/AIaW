// Stage 4 / 批次-4a — workspaces server-routed CRUD + cascade-on-delete.
// 4 cases (matching the plan's 通过判据 line for batch-4a):
//   case1 ws double-tab: A repos put / update / delete propagates to B within 1.5s
//   case2 ws double-tab cascade: A's stores/workspaces.ts deleteItem ⇒
//     - A's local Dexie child rows (dialogs / messages / items / artifacts /
//       assistants) for the workspace are gone
//     - server workspace row is a tombstone
//     - B's workspaces table receives the delete event via WS within 1.5s
//   case3 providers-rest no-realtime: A change is invisible to B until reload
//   case4 baseline byte-identical: zero backend traffic, zero ws
//
// 4a-specific notes:
//   - dialogs / messages / items / artifacts are still Dexie at 4a, so we
//     can't assert child-table propagation across BrowserContexts (B's IDB
//     is independent and never received those child rows). What we *can*
//     assert is the local-side cleanup on A and the server-side workspace
//     tombstone, plus the workspaces propagation across tabs.
//   - assistants is server-routed (since 批次-3b), so deleteItem's per-row
//     assistants.delete calls are real HTTP and do tombstone server-side
//     for assistants under the workspace.

import { test, expect, type Page } from '@playwright/test'
import { exposeReady, dumpTable } from '../helpers/db'
import { listWorkspaces, backendClient } from '../helpers/backend'
import { registerViaApi, loginApi, injectAuth, type TokenPair } from '../helpers/auth'
import { openContextsForUsers } from '../helpers/tabs'
import { expectRowSync } from '../helpers/sync'

function uniqEmail(): string {
  return `stage4-ws-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
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

async function activateWorkspacesObserver(page: Page): Promise<void> {
  // Observers are what wires the realtime subscription; without one, no
  // listener is attached and WS events get dropped on the floor.
  await page.evaluate(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(window as any).__stage4_ws_obs__ = (window as any).__repos__.workspaces.observeList()
  })
  await page.evaluate(async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await (window as any).__repos__.workspaces.list()
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

const SAMPLE_FOLDER = {
  name: 'F-1',
  avatar: { type: 'icon', icon: 'sym_o_folder' },
  type: 'folder',
  parentId: '$root'
}

test.describe('stage4 batch-4a workspaces server-routed', () => {
  test('case1 ws double-tab: A put workspace + folder + update propagates to B within 1.5s', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-ws', 'realtime-ws profile only')

    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))
      await activateWorkspacesObserver(pageA)
      await activateWorkspacesObserver(pageB)

      const wsId = `ws-${Math.random().toString(36).slice(2, 10)}`
      const folderId = `f-${Math.random().toString(36).slice(2, 10)}`

      // Workspace round-trip
      await pageA.evaluate(async ({ id, data }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.workspaces.put({ ...data, id })
      }, { id: wsId, data: { ...SAMPLE_WORKSPACE, name: 'name-v1' } })
      await expectRowSync(pageA, pageB, 'workspaces', wsId, { withinMs: 1_500 })

      // Folder round-trip (different `type` discriminator inside data)
      await pageA.evaluate(async ({ id, data }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.workspaces.put({ ...data, id })
      }, { id: folderId, data: { ...SAMPLE_FOLDER, name: 'folder-v1' } })
      await expectRowSync(pageA, pageB, 'workspaces', folderId, { withinMs: 1_500 })

      // Update propagates with new name
      await pageA.evaluate(async ({ id, data }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.workspaces.put({ ...data, id })
      }, { id: wsId, data: { ...SAMPLE_WORKSPACE, name: 'name-v2' } })
      const updated = await pollUntil(
        () => dumpTable<{ id: string; name?: string }>(pageB, 'workspaces'),
        rows => rows.find(r => r.id === wsId)?.name === 'name-v2',
        1_500
      )
      expect(
        updated.ok,
        'B should see updated name=\'name-v2\' within 1.5s; ' +
          `last B rows: ${JSON.stringify(updated.last.map(r => ({ id: r.id, name: r.name })))}`
      ).toBe(true)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case2 ws double-tab cascade: deleteItem clears local children + server tombstone + B receives ws delete', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-ws', 'realtime-ws profile only')

    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))
      await activateWorkspacesObserver(pageA)
      await activateWorkspacesObserver(pageB)

      const wsId = `ws-${Math.random().toString(36).slice(2, 10)}`
      const dialogId = `dlg-${Math.random().toString(36).slice(2, 10)}`
      const messageId = `msg-${Math.random().toString(36).slice(2, 10)}`
      const itemId = `it-${Math.random().toString(36).slice(2, 10)}`
      const artifactId = `art-${Math.random().toString(36).slice(2, 10)}`
      const assistantId = `ast-${Math.random().toString(36).slice(2, 10)}`

      // Seed A: workspace via repos (→ server), child rows direct via Dexie
      // for tables that are still Dexie (dialogs / messages / items /
      // artifacts), and assistants via repos (→ server).
      await pageA.evaluate(async ({ wsId, dialogId, messageId, itemId, artifactId, assistantId, wsBody }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const w = window as any
        await w.__repos__.workspaces.put({ ...wsBody, id: wsId })
        await w.__db__.dialogs.put({
          id: dialogId,
          name: 'd',
          workspaceId: wsId,
          msgTree: {},
          msgRoute: [],
          inputVars: {}
        })
        await w.__db__.messages.put({
          id: messageId, type: 'user', dialogId, contents: [], status: 'default'
        })
        await w.__db__.items.put({
          id: itemId, type: 'file', dialogId, name: 'x'
        })
        await w.__db__.artifacts.put({
          id: artifactId,
          name: 'a',
          workspaceId: wsId,
          versions: [],
          currIndex: 0,
          readable: true,
          writable: true,
          open: false,
          tmp: ''
        })
        await w.__repos__.assistants.put({
          id: assistantId,
          name: 'A',
          avatar: { type: 'icon', icon: 'i' },
          workspaceId: wsId,
          prompt: '',
          promptTemplate: '',
          promptVars: [],
          provider: null,
          model: null,
          modelSettings: { temperature: 0.6, topP: 1, presencePenalty: 0, frequencyPenalty: 0, maxSteps: 4, maxRetries: 1 },
          plugins: {},
          promptRole: 'system',
          stream: true
        })
      }, {
        wsId,
        dialogId,
        messageId,
        itemId,
        artifactId,
        assistantId,
        wsBody: SAMPLE_WORKSPACE
      })

      await expectRowSync(pageA, pageB, 'workspaces', wsId, { withinMs: 1_500 })

      // Replicate stores/workspaces.ts::deleteItem here: same sequential
      // repo calls in the same order. Importing the store at runtime would
      // need a build-time path which doesn't exist post-bundle.
      await pageA.evaluate(async ({ wsId, dialogId }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const w = window as any
        await w.__repos__.messages.deleteWhere({ where: { dialogId } })
        await w.__repos__.items.deleteWhere({ where: { dialogId } })
        await w.__repos__.dialogs.deleteWhere({ where: { workspaceId: wsId } })
        await w.__repos__.assistants.deleteWhere({ where: { workspaceId: wsId } })
        await w.__repos__.artifacts.deleteWhere({ where: { workspaceId: wsId } })
        await w.__repos__.workspaces.delete(wsId)
      }, { wsId, dialogId })

      // A's local Dexie children for this workspace must be gone
      const aLeftovers = await pageA.evaluate(async ({ wsId, dialogId }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const db = (window as any).__db__
        return {
          ws: await db.workspaces.get(wsId),
          dialogs: (await db.dialogs.where({ workspaceId: wsId }).toArray()).length,
          messages: (await db.messages.where({ dialogId }).toArray()).length,
          items: (await db.items.where({ dialogId }).toArray()).length,
          artifacts: (await db.artifacts.where({ workspaceId: wsId }).toArray()).length
        }
      }, { wsId, dialogId })
      expect(
        aLeftovers,
        `A side leftovers after deleteItem(wsId=${wsId}): ${JSON.stringify(aLeftovers)}`
      ).toEqual({ ws: undefined, dialogs: 0, messages: 0, items: 0, artifacts: 0 })

      // Server-side: workspaces row is a tombstone (visible in `since=0`
      // list as deleted=true, data=null)
      const serverWorkspaces = await listWorkspaces(user.accessForBackend, 0)
      const wsTomb = serverWorkspaces.find(r => r.id === wsId)
      expect(
        wsTomb,
        `server workspaces tombstone missing for wsId=${wsId}; ` +
          `rows: ${JSON.stringify(serverWorkspaces.map(r => ({ id: r.id, deleted: r.deleted })))}`
      ).toBeTruthy()
      expect(wsTomb!.deleted).toBe(true)
      expect(wsTomb!.data).toBeNull()

      // Server-side: assistants row is a tombstone (assistants is
      // server-routed since 批次-3b → deleteItem hit the real endpoint)
      const assistantsResp = await backendClient(user.accessForBackend)
        .get<Array<{ id: string; deleted: boolean }>>('/api/v1/assistants?since=0')
      const astTomb = assistantsResp.find(r => r.id === assistantId)
      expect(
        astTomb,
        `server assistants tombstone missing for assistantId=${assistantId}; ` +
          `rows: ${JSON.stringify(assistantsResp.map(r => ({ id: r.id, deleted: r.deleted })))}`
      ).toBeTruthy()
      expect(astTomb!.deleted).toBe(true)

      // B side: workspace row gone within 1.5s via WS delete event
      const removed = await pollUntil(
        () => dumpTable<{ id: string }>(pageB, 'workspaces'),
        rows => !rows.some(r => r.id === wsId),
        1_500
      )
      expect(
        removed.ok,
        `workspace ${wsId} should be gone from B within 1.5s after A delete; ` +
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
      // B observes once so the cache is warm; without realtime, no live
      // updates should arrive.
      await pageB.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.workspaces.list()
      })

      const wsId = `ws-rest-${Math.random().toString(36).slice(2, 10)}`
      await pageA.evaluate(async ({ id, data }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.workspaces.put({ ...data, id })
      }, { id: wsId, data: { ...SAMPLE_WORKSPACE, name: 'rest-only' } })

      await new Promise(resolve => setTimeout(resolve, 1_500))
      const beforeReload = await dumpTable<{ id: string }>(pageB, 'workspaces')
      expect(
        beforeReload.some(r => r.id === wsId),
        `providers-rest must NOT propagate without reload; B saw ${wsId} before reload ` +
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
        await (window as any).__repos__.workspaces.list()
      })
      const afterReload = await dumpTable<{ id: string }>(pageB, 'workspaces')
      expect(
        afterReload.some(r => r.id === wsId),
        `after reload + list(), B should pull ${wsId} from server ` +
          `(rows=${JSON.stringify(afterReload.map(r => r.id))})`
      ).toBe(true)

      // Confirm we really hit the server (helper bypasses front-end)
      const serverRows = await listWorkspaces(user.accessForBackend, 0)
      expect(
        serverRows.some(r => r.id === wsId && r.deleted === false),
        `server should hold workspace ${wsId}; rows=${JSON.stringify(serverRows.map(r => ({ id: r.id, deleted: r.deleted })))}`
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
    const workspaces = await dumpTable(page, 'workspaces')
    expect(Array.isArray(workspaces)).toBe(true)
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
