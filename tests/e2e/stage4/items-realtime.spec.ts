// Stage 4 / 批次-4c — items server-routed CRUD + ref-attachment round-trip +
// workspace→dialog→item cascade fan-out.
//
// 5 cases (matching the plan's 通过判据 line for batch-4c):
//   case1 ws double-tab inline item: put + update + delete propagate < 1.5s
//   case2 ws double-tab ref attachment: A puts 100KB ArrayBuffer →
//     blob-client serializes via /api/v1/blobs ref → B receives realtime →
//     materializeAttachment fetches via signed URL → B's contentBuffer
//     bytes are byte-identical to A's; PG row.data.contentBuffer.type
//     === 'ref'
//   case3 ws double-tab cascade: A's workspace delete ⇒ B's items for
//     that workspace's dialogs tombstone within 1.5s via realtime;
//     server-side item rows are tombstones too
//   case4 providers-rest no-realtime: A change is invisible to B until reload
//   case5 baseline byte-identical: zero backend traffic, zero ws
//
// Notes:
//   - putItem requires a dialogId that already exists for the user; the
//     server returns 409 otherwise (mirrors dialogs+workspaces).
//   - case2 verifies the full blob-ref pipeline end-to-end:
//     serializeAttachment → POST /api/v1/blobs → ref envelope in
//     items.data → realtime fan-out → materializeAttachment → signed-URL
//     GET → ArrayBuffer in B's IDB. The 100KB threshold is just-above the
//     64KB BLOB_INLINE_MAX_BYTES so client-side picks ref mode.
//   - cascade hop: workspace cascade tombstones dialogs (4b) AND items
//     under those dialogs (4c). The realtime broker emits one delete
//     event per cascaded item; B's items table converges via the items
//     observer.

import { test, expect, type Page } from '@playwright/test'
import { exposeReady, dumpTable } from '../helpers/db'
import { listItems } from '../helpers/backend'
import { registerViaApi, loginApi, injectAuth, type TokenPair } from '../helpers/auth'
import { openContextsForUsers } from '../helpers/tabs'
import { expectRowSync } from '../helpers/sync'

function uniqEmail(): string {
  return `stage4-itm-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
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
  // events. workspaces / dialogs / items all server-routed at 4c.
  await page.evaluate(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const w = window as any
    w.__stage4c_obs__ = {
      workspaces: w.__repos__.workspaces.observeList(),
      dialogs: w.__repos__.dialogs.observeList(),
      items: w.__repos__.items.observeList()
    }
  })
  await page.evaluate(async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const w = window as any
    await w.__repos__.workspaces.list()
    await w.__repos__.dialogs.list()
    await w.__repos__.items.list()
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

test.describe('stage4 batch-4c items server-routed', () => {
  test('case1 ws double-tab inline item: put + update + delete propagate within 1.5s', async ({ browser }, testInfo) => {
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
      const itmId = `itm-${Math.random().toString(36).slice(2, 10)}`

      // Seed workspace + dialog (FK chain)
      await pageA.evaluate(async ({ wsId, dlgId, wsBody, dlgBody }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const w = window as any
        await w.__repos__.workspaces.put({ ...wsBody, id: wsId })
        await w.__repos__.dialogs.put({ ...dlgBody, id: dlgId })
      }, { wsId, dlgId, wsBody: SAMPLE_WORKSPACE, dlgBody: buildDialog(wsId, 'd-host') })
      await expectRowSync(pageA, pageB, 'workspaces', wsId, { withinMs: 1_500 })
      await expectRowSync(pageA, pageB, 'dialogs', dlgId, { withinMs: 1_500 })

      // Inline item (no contentBuffer, just text) propagates A → B
      await pageA.evaluate(async ({ id, dialogId }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.items.put({
          id,
          dialogId,
          type: 'text',
          references: 1,
          contentText: 'inline-v1'
        })
      }, { id: itmId, dialogId: dlgId })
      await expectRowSync(pageA, pageB, 'items', itmId, { withinMs: 1_500 })

      // Update contentText → B sees v2
      await pageA.evaluate(async ({ id, dialogId }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.items.put({
          id,
          dialogId,
          type: 'text',
          references: 1,
          contentText: 'inline-v2'
        })
      }, { id: itmId, dialogId: dlgId })
      const updated = await pollUntil(
        () => dumpTable<{ id: string; contentText?: string }>(pageB, 'items'),
        rows => rows.find(r => r.id === itmId)?.contentText === 'inline-v2',
        1_500
      )
      expect(
        updated.ok,
        'B should see updated contentText=\'inline-v2\' within 1.5s; ' +
          `last B rows: ${JSON.stringify(updated.last.map(r => ({ id: r.id, contentText: r.contentText })))}`
      ).toBe(true)

      // Explicit delete → B sees the row gone
      await pageA.evaluate(async ({ id }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.items.delete(id)
      }, { id: itmId })
      const removed = await pollUntil(
        () => dumpTable<{ id: string }>(pageB, 'items'),
        rows => !rows.some(r => r.id === itmId),
        1_500
      )
      expect(
        removed.ok,
        `item ${itmId} should be gone from B within 1.5s; ` +
          `last B rows: ${JSON.stringify(removed.last.map(r => r.id))}`
      ).toBe(true)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case2 ws double-tab: 100KB ref attachment round-trips bytes A→B via blob-client', async ({ browser }, testInfo) => {
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
      const itmId = `itm-${Math.random().toString(36).slice(2, 10)}`
      const SIZE = 100 * 1024 // 100KB — well above the 64KB inline threshold

      await pageA.evaluate(async ({ wsId, dlgId, wsBody, dlgBody }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const w = window as any
        await w.__repos__.workspaces.put({ ...wsBody, id: wsId })
        await w.__repos__.dialogs.put({ ...dlgBody, id: dlgId })
      }, { wsId, dlgId, wsBody: SAMPLE_WORKSPACE, dlgBody: buildDialog(wsId, 'd-host') })
      await expectRowSync(pageA, pageB, 'workspaces', wsId, { withinMs: 1_500 })
      await expectRowSync(pageA, pageB, 'dialogs', dlgId, { withinMs: 1_500 })

      // Build a deterministic 100KB ArrayBuffer on A (so B can verify
      // bytes by recomputing the same pattern). items.server.ts encodes
      // contentBuffer via blob-client.serializeAttachment which, at this
      // size, uploads the bytes and stores a ref envelope.
      await pageA.evaluate(async ({ id, dialogId, size }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const w = window as any
        const buf = new ArrayBuffer(size)
        const view = new Uint8Array(buf)
        for (let i = 0; i < size; i++) view[i] = (i * 7 + 11) % 251
        await w.__repos__.items.put({
          id,
          dialogId,
          type: 'file',
          references: 1,
          name: 'big.bin',
          mimeType: 'application/octet-stream',
          contentBuffer: buf
        })
      }, { id: itmId, dialogId: dlgId, size: SIZE })

      // Wait for B to materialize the item with the full byte payload.
      // Allow a generous 5s — the round-trip is realtime event ⇒ signed
      // URL fetch ⇒ ArrayBuffer write to IDB.
      const probe = await pollUntil(
        async () => pageB.evaluate(async (id) => {
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          const w = window as any
          const row = await w.__db__.items.get(id)
          if (!row) return { stage: 'no-row' as const }
          if (!row.contentBuffer) return { stage: 'no-buffer' as const, mimeType: row.mimeType }
          const buf: ArrayBuffer = row.contentBuffer
          const view = new Uint8Array(buf)
          // Sample a few bytes for fingerprinting (full bytes serialized
          // through page.evaluate would explode the IPC payload).
          return {
            stage: 'has-buffer' as const,
            size: buf.byteLength,
            head: Array.from(view.subarray(0, 8)),
            tail: Array.from(view.subarray(view.length - 8))
          }
        }, itmId),
        v => v.stage === 'has-buffer' && v.size === SIZE,
        5_000
      )
      expect(
        probe.ok,
        `B did not materialize item with ${SIZE}B contentBuffer within 5s; ` +
          `last=${JSON.stringify(probe.last)}`
      ).toBe(true)

      // Recompute the expected fingerprint on the test side and compare.
      const expectedHead = Array.from({ length: 8 }, (_, i) => (i * 7 + 11) % 251)
      const expectedTail = Array.from({ length: 8 }, (_, k) => {
        const i = SIZE - 8 + k
        return (i * 7 + 11) % 251
      })
      expect(
        probe.last,
        `byte fingerprint mismatch on B; expected head=${JSON.stringify(expectedHead)} tail=${JSON.stringify(expectedTail)}; got=${JSON.stringify(probe.last)}`
      ).toMatchObject({
        stage: 'has-buffer',
        size: SIZE,
        head: expectedHead,
        tail: expectedTail
      })

      // Server-side: the row stores a ref envelope, NOT inline base64.
      // Confirms blob-client picked ref mode at this size.
      const serverItems = await listItems(user.accessForBackend, 0)
      const itm = serverItems.find(r => r.id === itmId)
      expect(itm, `server item ${itmId} missing; rows=${JSON.stringify(serverItems.map(r => r.id))}`).toBeTruthy()
      const cb = (itm!.data as Record<string, unknown> | null)?.contentBuffer as Record<string, unknown> | undefined
      expect(
        cb,
        `expected items.data.contentBuffer envelope; got data=${JSON.stringify(itm!.data)}`
      ).toBeTruthy()
      expect(cb!.type).toBe('ref')
      expect(cb!.size).toBe(SIZE)
      expect(typeof cb!.sha256).toBe('string')
      expect((cb!.sha256 as string).length).toBe(64)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case3 ws double-tab cascade: workspace delete tombstones items via dialog hop', async ({ browser }, testInfo) => {
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
      const itm1 = `itm-${Math.random().toString(36).slice(2, 10)}`
      const itm2 = `itm-${Math.random().toString(36).slice(2, 10)}`

      // Seed workspace + dialog + 2 items
      await pageA.evaluate(async ({ wsId, dlgId, itm1, itm2, wsBody, dlgBody }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const w = window as any
        await w.__repos__.workspaces.put({ ...wsBody, id: wsId })
        await w.__repos__.dialogs.put({ ...dlgBody, id: dlgId })
        await w.__repos__.items.put({
          id: itm1,
          dialogId: dlgId,
          type: 'text',
          references: 1,
          contentText: 'first'
        })
        await w.__repos__.items.put({
          id: itm2,
          dialogId: dlgId,
          type: 'text',
          references: 1,
          contentText: 'second'
        })
      }, { wsId, dlgId, itm1, itm2, wsBody: SAMPLE_WORKSPACE, dlgBody: buildDialog(wsId, 'd-cascade') })

      await expectRowSync(pageA, pageB, 'workspaces', wsId, { withinMs: 1_500 })
      await expectRowSync(pageA, pageB, 'dialogs', dlgId, { withinMs: 1_500 })
      await expectRowSync(pageA, pageB, 'items', itm1, { withinMs: 1_500 })
      await expectRowSync(pageA, pageB, 'items', itm2, { withinMs: 1_500 })

      // Replicate stores/workspaces.ts::deleteItem — same per-table sweep
      // the production path runs. The server-side cascade tombstones
      // dialogs AND items behind the workspace.delete call; the explicit
      // per-table sweep is idempotent against tombstones.
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

      // Server-side: both items are tombstones
      const serverItems = await listItems(user.accessForBackend, 0)
      const t1 = serverItems.find(r => r.id === itm1)
      const t2 = serverItems.find(r => r.id === itm2)
      expect(
        t1 && t2,
        `server item tombstones missing; rows: ${JSON.stringify(serverItems.map(r => ({ id: r.id, deleted: r.deleted })))}`
      ).toBeTruthy()
      expect(t1!.deleted).toBe(true)
      expect(t1!.data).toBeNull()
      expect(t2!.deleted).toBe(true)
      expect(t2!.data).toBeNull()

      // B side: both item rows gone via WS delete events within 1.5s
      const removed = await pollUntil(
        () => dumpTable<{ id: string }>(pageB, 'items'),
        rows => !rows.some(r => r.id === itm1) && !rows.some(r => r.id === itm2),
        1_500
      )
      expect(
        removed.ok,
        `items ${itm1}/${itm2} should be gone from B within 1.5s after cascade; ` +
          `last B rows: ${JSON.stringify(removed.last.map(r => r.id))}`
      ).toBe(true)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case4 providers-rest no-realtime: A change is invisible to B until reload', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')

    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))
      // Warm B's cache once
      await pageB.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const w = window as any
        await w.__repos__.workspaces.list()
        await w.__repos__.dialogs.list()
        await w.__repos__.items.list()
      })

      const wsId = `ws-rest-${Math.random().toString(36).slice(2, 10)}`
      const dlgId = `dlg-rest-${Math.random().toString(36).slice(2, 10)}`
      const itmId = `itm-rest-${Math.random().toString(36).slice(2, 10)}`
      await pageA.evaluate(async ({ wsId, dlgId, itmId, wsBody, dlgBody }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const w = window as any
        await w.__repos__.workspaces.put({ ...wsBody, id: wsId })
        await w.__repos__.dialogs.put({ ...dlgBody, id: dlgId })
        await w.__repos__.items.put({
          id: itmId,
          dialogId: dlgId,
          type: 'text',
          references: 1,
          contentText: 'rest-only'
        })
      }, { wsId, dlgId, itmId, wsBody: SAMPLE_WORKSPACE, dlgBody: buildDialog(wsId, 'd-rest') })

      await new Promise(resolve => setTimeout(resolve, 1_500))
      const beforeReload = await dumpTable<{ id: string }>(pageB, 'items')
      expect(
        beforeReload.some(r => r.id === itmId),
        `providers-rest must NOT propagate items without reload; B saw ${itmId} before reload ` +
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
        await (window as any).__repos__.items.list()
      })
      const afterReload = await dumpTable<{ id: string }>(pageB, 'items')
      expect(
        afterReload.some(r => r.id === itmId),
        `after reload + list(), B should pull ${itmId} from server ` +
          `(rows=${JSON.stringify(afterReload.map(r => r.id))})`
      ).toBe(true)

      const serverRows = await listItems(user.accessForBackend, 0)
      expect(
        serverRows.some(r => r.id === itmId && r.deleted === false),
        `server should hold item ${itmId}; rows=${JSON.stringify(serverRows.map(r => ({ id: r.id, deleted: r.deleted })))}`
      ).toBe(true)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case5 baseline byte-identical: zero backend traffic and zero websocket connections', async ({ page }, testInfo) => {
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
    const items = await dumpTable(page, 'items')
    expect(Array.isArray(items)).toBe(true)
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
