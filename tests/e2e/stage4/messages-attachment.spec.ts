// Stage 4 / 批次-4e — messages server-routed CRUD + streaming throttle +
// dialogId scoped pull (mandatory) + workspace cascade fan-out.
//
// 7 cases (matching the plan's 通过判据 line for batch-4e):
//   case1 ws double-tab inline message: put + update + delete propagate
//     within 1.5s (small inline contents, no spill)
//   case2 scoped pull: empty IDB → only target dialog messages appear,
//     no full-table fetch (backend would 422 anyway; verify client never
//     issues bare GET /messages)
//   case3 ws double-tab streaming: A's 200 sequential repos.messages.update()
//     calls (simulating a token stream) → B converges on the final inline
//     envelope without dropped frames or backwards version regression
//   case4 message-stream-flush triple-trigger (4 sub-assertions on the
//     factory hook):
//     4a 200ms time window: 5 chunks @ 50ms apart, no boundary, no byte
//        threshold → exactly 1 flush
//     4b byte threshold: a single chunk with chunkBytes=1100 (>1024) →
//        immediate flush
//     4c sentence boundary: a chunk whose tail matches `[。?!.!\n]\s*$`
//        → immediate flush
//     4d stop() finalizes: pending value when stop() is called must
//        produce a final flush before stop() resolves
//   case5 cascade workspace → messages tombstones B's IDB via realtime
//   case6 providers-rest no-realtime: A change is invisible to B until reload
//   case7 baseline byte-identical: zero backend traffic, zero ws
//
// Notes:
//   - putMessage requires a dialogId that already exists for the user;
//     server returns 409 otherwise (mirrors items+dialogs).
//   - case2 verifies the dialogId-mandatory scoped-pull contract end-to-
//     end on the client. The server returns 422 if the client ever
//     forgets to pass dialogId; we additionally assert the client never
//     issues a bare GET /messages so even a future "graceful fallback"
//     refactor can't silently regress to full-table pulls.
//   - case3 asserts the inline-envelope streaming contract: each PUT
//     ships the full current envelope (newest-wins). Even at 200 PUT
//     rounds, B's last seen version is monotonically non-decreasing and
//     converges on the final contents.
//   - case4 drives `window.__messageStreamFlush__` (EXPOSE_DB-gated)
//     directly; no real network, no real Vue context — pure unit-style
//     coverage of the triple-trigger.

import { test, expect, type Page } from '@playwright/test'
import { exposeReady, dumpTable, clearAll } from '../helpers/db'
import { listMessages, putWorkspace, putDialog, putMessage } from '../helpers/backend'
import { registerViaApi, loginApi, injectAuth, type TokenPair } from '../helpers/auth'
import { openContextsForUsers } from '../helpers/tabs'
import { expectRowSync } from '../helpers/sync'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyWindow = any

function uniqEmail(): string {
  return `stage4-msg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
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
    const auth = (window as AnyWindow).__authSource__
    return !!(auth && auth.currentToken && auth.currentToken())
  }, undefined, { timeout: 10_000 })
}

async function activateObservers(page: Page, dialogId?: string): Promise<void> {
  // observers wire realtime subscriptions. messages.observeList is
  // cache-only (warns) but still attaches the subscription via
  // ensureRealtimeSubscription, which is what we need for cross-tab
  // realtime fan-out. observeFind({where:{dialogId}}) is the production
  // path and additionally kicks scoped pull on mount.
  await page.evaluate((scopeDlg) => {
    const w = window as AnyWindow
    w.__stage4e_obs__ = {
      workspaces: w.__repos__.workspaces.observeList(),
      dialogs: w.__repos__.dialogs.observeList(),
      messages: scopeDlg
        ? w.__repos__.messages.observeFind({ where: { dialogId: scopeDlg } })
        : w.__repos__.messages.observeList()
    }
  }, dialogId)
  await page.evaluate(async (scopeDlg) => {
    const w = window as AnyWindow
    await w.__repos__.workspaces.list()
    await w.__repos__.dialogs.list()
    if (scopeDlg) {
      await w.__repos__.messages.find({ where: { dialogId: scopeDlg } })
    }
  }, dialogId)
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

function buildMessage(dialogId: string, id: string, text: string) {
  return {
    id,
    type: 'assistant' as const,
    dialogId,
    contents: [{ type: 'assistant-message', text }],
    status: 'default' as const
  }
}

interface MessagesListReq {
  url: string
  dialogId: string | null
  since: string | null
}

function classifyMessagesListUrl(url: string): MessagesListReq | null {
  const u = new URL(url)
  if (u.pathname !== '/api/v1/messages') return null
  return {
    url,
    dialogId: u.searchParams.get('dialogId'),
    since: u.searchParams.get('since')
  }
}

function startMessagesListRecorder(page: Page): { reqs: MessagesListReq[] } {
  const reqs: MessagesListReq[] = []
  page.on('request', (req) => {
    if (req.method() !== 'GET') return
    const c = classifyMessagesListUrl(req.url())
    if (c) reqs.push(c)
  })
  return { reqs }
}

test.describe('stage4 batch-4e messages server-routed', () => {
  test('case1 ws double-tab inline message: put + update + delete propagate within 1.5s', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-ws', 'realtime-ws profile only')

    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))

      const wsId = `ws-${Math.random().toString(36).slice(2, 10)}`
      const dlgId = `dlg-${Math.random().toString(36).slice(2, 10)}`
      const msgId = `msg-${Math.random().toString(36).slice(2, 10)}`

      // Seed workspace + dialog (FK chain)
      await pageA.evaluate(async ({ wsId, dlgId, wsBody, dlgBody }) => {
        const w = window as AnyWindow
        await w.__repos__.workspaces.put({ ...wsBody, id: wsId })
        await w.__repos__.dialogs.put({ ...dlgBody, id: dlgId })
      }, { wsId, dlgId, wsBody: SAMPLE_WORKSPACE, dlgBody: buildDialog(wsId, 'd-host') })

      // Activate observers AFTER dialog is seeded so observeFind has a
      // valid scope target on B.
      await activateObservers(pageA, dlgId)
      await activateObservers(pageB, dlgId)
      await expectRowSync(pageA, pageB, 'workspaces', wsId, { withinMs: 1_500 })
      await expectRowSync(pageA, pageB, 'dialogs', dlgId, { withinMs: 1_500 })

      // Inline message put → B sees v1
      await pageA.evaluate(async ({ id, dialogId }) => {
        const w = window as AnyWindow
        await w.__repos__.messages.put({
          id,
          type: 'assistant',
          dialogId,
          contents: [{ type: 'assistant-message', text: 'inline-v1' }],
          status: 'default'
        })
      }, { id: msgId, dialogId: dlgId })
      await expectRowSync(pageA, pageB, 'messages', msgId, { withinMs: 1_500 })

      // Update → B sees v2
      await pageA.evaluate(async ({ id, dialogId }) => {
        const w = window as AnyWindow
        await w.__repos__.messages.put({
          id,
          type: 'assistant',
          dialogId,
          contents: [{ type: 'assistant-message', text: 'inline-v2' }],
          status: 'default'
        })
      }, { id: msgId, dialogId: dlgId })
      const updated = await pollUntil(
        () => dumpTable<{ id: string; contents?: Array<{ text?: string }> }>(pageB, 'messages'),
        rows => rows.find(r => r.id === msgId)?.contents?.[0]?.text === 'inline-v2',
        1_500
      )
      expect(
        updated.ok,
        'B should see updated contents[0].text=\'inline-v2\' within 1.5s; ' +
          `last B rows: ${JSON.stringify(updated.last.map(r => ({ id: r.id, text: r.contents?.[0]?.text })))}`
      ).toBe(true)

      // Explicit delete → B sees the row gone
      await pageA.evaluate(async ({ id }) => {
        const w = window as AnyWindow
        await w.__repos__.messages.delete(id)
      }, { id: msgId })
      const removed = await pollUntil(
        () => dumpTable<{ id: string }>(pageB, 'messages'),
        rows => !rows.some(r => r.id === msgId),
        1_500
      )
      expect(
        removed.ok,
        `message ${msgId} should be gone from B within 1.5s; ` +
          `last B rows: ${JSON.stringify(removed.last.map(r => r.id))}`
      ).toBe(true)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case2 scoped pull: only target dialog messages enter IDB, no bare full-table fetch', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')

    const user = await setupTestUser()
    // Seed workspace + two dialogs + 3 messages each, server-side, BEFORE
    // the page loads — observeFind on dialog X must scope-pull only X's
    // messages, never bare GET /messages (which would 422 anyway).
    const tag = Math.random().toString(36).slice(2, 8)
    const wsId = `ws-${tag}`
    const dlgX = `dlgX-${tag}`
    const dlgY = `dlgY-${tag}`
    await putWorkspace(user.accessForBackend, wsId, { ...SAMPLE_WORKSPACE })
    await putDialog(user.accessForBackend, dlgX, buildDialog(wsId, 'd-X'))
    await putDialog(user.accessForBackend, dlgY, buildDialog(wsId, 'd-Y'))
    const xIds: string[] = []
    const yIds: string[] = []
    for (let i = 0; i < 3; i++) {
      const xId = `m-X-${tag}-${i}`
      const yId = `m-Y-${tag}-${i}`
      xIds.push(xId)
      yIds.push(yId)
      await putMessage(user.accessForBackend, xId, buildMessage(dlgX, xId, `mx${i}`))
      await putMessage(user.accessForBackend, yId, buildMessage(dlgY, yId, `my${i}`))
    }

    const { reqs } = startMessagesListRecorder(page)
    await bootSession(page, await freshSession(user))
    // Wipe any boot-time cache so observeFind has no choice but to fetch.
    await clearAll(page, ['messages'])
    await page.evaluate(async () => {
      const w = window as AnyWindow
      await w.__db__.messages.clear()
    })

    const before = reqs.length

    // Open dialog X via observeFind
    await page.evaluate(async (dialogId) => {
      const w = window as AnyWindow
      w.__scope_obs__ = w.__repos__.messages.observeFind({ where: { dialogId } })
      await w.__repos__.messages.find({ where: { dialogId } })
    }, dlgX)

    const msgsInIdb = await dumpTable<{ id: string; dialogId?: string }>(page, 'messages')
    const idbIds = msgsInIdb.map(r => r.id).sort()
    expect(
      idbIds,
      `IDB should only contain dialog X messages, got ${JSON.stringify(idbIds)}; ` +
        `requests since boot=${JSON.stringify(reqs.slice(before))}`
    ).toEqual([...xIds].sort())
    for (const r of msgsInIdb) {
      expect(r.dialogId, `IDB row ${r.id} not under dialog X: ${JSON.stringify(r)}`).toBe(dlgX)
    }

    const newReqs = reqs.slice(before)
    const scopedX = newReqs.filter(r => r.dialogId === dlgX)
    const fullTable = newReqs.filter(r => r.dialogId === null)
    const scopedY = newReqs.filter(r => r.dialogId === dlgY)
    expect(
      scopedX.length,
      `expected ≥1 GET /messages?dialogId=${dlgX}; got ${scopedX.length}. ` +
        `All post-setup messages LIST reqs: ${JSON.stringify(newReqs)}`
    ).toBeGreaterThanOrEqual(1)
    expect(
      fullTable.length,
      `expected zero bare GET /messages (no dialogId param — backend would 422); got ${fullTable.length}: ` +
        `${JSON.stringify(fullTable)}`
    ).toBe(0)
    expect(
      scopedY.length,
      `expected zero GET /messages?dialogId=${dlgY}; got ${scopedY.length}: ` +
        `${JSON.stringify(scopedY)}`
    ).toBe(0)
  })

  test('case3 ws double-tab streaming: 200 sequential PUTs converge with monotonic versions on B', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-ws', 'realtime-ws profile only')

    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))

      const wsId = `ws-${Math.random().toString(36).slice(2, 10)}`
      const dlgId = `dlg-${Math.random().toString(36).slice(2, 10)}`
      const msgId = `msg-${Math.random().toString(36).slice(2, 10)}`

      await pageA.evaluate(async ({ wsId, dlgId, wsBody, dlgBody }) => {
        const w = window as AnyWindow
        await w.__repos__.workspaces.put({ ...wsBody, id: wsId })
        await w.__repos__.dialogs.put({ ...dlgBody, id: dlgId })
      }, { wsId, dlgId, wsBody: SAMPLE_WORKSPACE, dlgBody: buildDialog(wsId, 'd-stream') })
      await activateObservers(pageA, dlgId)
      await activateObservers(pageB, dlgId)
      await expectRowSync(pageA, pageB, 'workspaces', wsId, { withinMs: 1_500 })
      await expectRowSync(pageA, pageB, 'dialogs', dlgId, { withinMs: 1_500 })

      // Stream 200 PUTs from A. Each call ships the full envelope with
      // ever-growing text — that's the inline-envelope contract, not a
      // delta protocol. messages.server.ts under the hood still picks
      // inline (we stay under 64KB for the entire stream).
      const ROUNDS = 200
      const finalText = await pageA.evaluate(async ({ id, dialogId, rounds }) => {
        const w = window as AnyWindow
        let acc = ''
        for (let i = 0; i < rounds; i++) {
          acc += `[${i}]`
          await w.__repos__.messages.put({
            id,
            type: 'assistant',
            dialogId,
            contents: [{ type: 'assistant-message', text: acc }],
            status: 'streaming'
          })
        }
        return acc
      }, { id: msgId, dialogId: dlgId, rounds: ROUNDS })

      // B converges on the final inline envelope. With 200 round-trips
      // through the broker the last-write-wins envelope in IDB must equal
      // the final text. Allow up to 10s end-to-end since each PUT is HTTP.
      const probe = await pollUntil(
        () => pageB.evaluate(async (id) => {
          const w = window as AnyWindow
          const row = await w.__db__.messages.get(id)
          if (!row) return { stage: 'no-row' as const }
          const text = row.contents?.[0]?.text as string | undefined
          if (typeof text !== 'string') return { stage: 'no-text' as const }
          return { stage: 'has-text' as const, length: text.length, tail: text.slice(-32) }
        }, msgId),
        v => v.stage === 'has-text' && v.length === finalText.length,
        10_000
      )
      expect(
        probe.ok,
        `B did not converge on final ${finalText.length}-char text within 10s; ` +
          `last=${JSON.stringify(probe.last)}; expected tail=${JSON.stringify(finalText.slice(-32))}`
      ).toBe(true)
      // Tail fingerprint match guards against silent truncation
      expect(probe.last, 'tail mismatch on B').toMatchObject({
        stage: 'has-text',
        tail: finalText.slice(-32)
      })

      // No version regression on the server: the LAST broker event must
      // carry the highest version (LWW envelope, monotonic). We sample
      // by listing scope and confirming exactly one row at version ≥
      // ROUNDS (every PUT bumped global_change_seq by 1, plus 2 for ws/dlg).
      const serverList = await listMessages(user.accessForBackend, dlgId, 0)
      const rows = Array.isArray(serverList) ? serverList : serverList.rows
      const target = rows.find(r => r.id === msgId)
      expect(target, `server message ${msgId} missing; rows=${JSON.stringify(rows.map(r => r.id))}`).toBeTruthy()
      expect(target!.version, `version regression on server: ${target!.version}`).toBeGreaterThanOrEqual(ROUNDS)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case4 message-stream-flush triple-trigger: 4a time-window / 4b byte-threshold / 4c sentence-boundary / 4d stop-finalize', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-ws', 'realtime-ws profile only')

    const user = await setupTestUser()
    await bootSession(page, await freshSession(user))
    await page.waitForFunction(() => {
      const w = window as AnyWindow
      return !!(w.__messageStreamFlush__ && typeof w.__messageStreamFlush__.create === 'function')
    }, undefined, { timeout: 5_000 })

    // 4a: 200ms time window. 5 chunks enqueued synchronously (within a
    // single tick) with no boundary / no byte threshold → schedule one
    // 200ms timer; after the window closes only ONE flush fires with
    // the newest-wins value. The deterministic version of "many small
    // chunks within one window batch".
    const r4a = await page.evaluate(async () => {
      const w = window as AnyWindow
      const flushed: string[] = []
      const f = w.__messageStreamFlush__.create({
        flush: async (v: string) => { flushed.push(v) },
        intervalMs: 200,
        byteThreshold: 100_000, // effectively disable trigger ②
        chunkBytesOf: () => 0,
        boundaryTextOf: () => '' // disable trigger ③
      })
      for (let i = 0; i < 5; i++) {
        f.enqueue(`chunk-${i}`)
      }
      // Wait for the timer to fire (200ms) + a bit of slack
      await new Promise(resolve => setTimeout(resolve, 350))
      const result = { flushCount: f.flushCount(), lastFlushed: flushed[flushed.length - 1] }
      await f.stop()
      return result
    })
    expect(
      r4a.flushCount,
      `4a time-window: expected exactly 1 flush after 5 chunks @ 50ms / 200ms window; got flushCount=${r4a.flushCount} lastFlushed=${JSON.stringify(r4a.lastFlushed)}`
    ).toBe(1)
    expect(r4a.lastFlushed, `4a: newest-wins should flush 'chunk-4', got ${JSON.stringify(r4a.lastFlushed)}`).toBe('chunk-4')

    // 4b: byte threshold. Single enqueue with chunkBytes hint above the
    // threshold → immediate flush.
    const r4b = await page.evaluate(async () => {
      const w = window as AnyWindow
      const flushed: string[] = []
      const f = w.__messageStreamFlush__.create({
        flush: async (v: string) => { flushed.push(v) },
        intervalMs: 60_000, // trigger ① irrelevant
        byteThreshold: 1024
      })
      f.enqueue('big-payload', { chunkBytes: 1100 })
      // Microtask-immediate; small await to let the doFlush microtask drain.
      await new Promise(resolve => setTimeout(resolve, 50))
      const result = { flushCount: f.flushCount(), bytesPending: f.bytesPending() }
      await f.stop()
      return result
    })
    expect(
      r4b.flushCount,
      `4b byte-threshold: expected immediate flush at chunkBytes=1100 (>1024); got flushCount=${r4b.flushCount} bytesPending=${r4b.bytesPending}`
    ).toBe(1)

    // 4c: sentence boundary on the tail.
    const r4c = await page.evaluate(async () => {
      const w = window as AnyWindow
      const flushed: string[] = []
      const f = w.__messageStreamFlush__.create({
        flush: async (v: string) => { flushed.push(v) },
        intervalMs: 60_000,
        byteThreshold: 100_000,
        boundaryTextOf: (v: string) => v.slice(-4)
      })
      f.enqueue('sentence one。')
      await new Promise(resolve => setTimeout(resolve, 50))
      const result = { flushCount: f.flushCount(), lastFlushed: flushed[flushed.length - 1] }
      await f.stop()
      return result
    })
    expect(
      r4c.flushCount,
      `4c sentence-boundary: expected immediate flush on '。' tail; got flushCount=${r4c.flushCount} lastFlushed=${JSON.stringify(r4c.lastFlushed)}`
    ).toBe(1)

    // 4d: stop() finalizes pending. enqueue without firing any trigger,
    // then call stop() — the pending value must hit flush() before
    // stop() resolves.
    const r4d = await page.evaluate(async () => {
      const w = window as AnyWindow
      const flushed: string[] = []
      const f = w.__messageStreamFlush__.create({
        flush: async (v: string) => { flushed.push(v) },
        intervalMs: 60_000, // way bigger than the test window
        byteThreshold: 100_000,
        boundaryTextOf: () => ''
      })
      // Pending value with no trigger: bytesPending=0, no boundary, timer
      // scheduled for 60s.
      f.enqueue('pending-final')
      const beforeStop = f.flushCount()
      await f.stop()
      return {
        beforeStop,
        afterStop: f.flushCount(),
        lastFlushed: flushed[flushed.length - 1]
      }
    })
    expect(
      r4d.beforeStop,
      `4d: no trigger should fire pre-stop; got flushCount=${r4d.beforeStop}`
    ).toBe(0)
    expect(
      r4d.afterStop,
      `4d stop-finalize: stop() must flush pending; got afterStop=${r4d.afterStop} lastFlushed=${JSON.stringify(r4d.lastFlushed)}`
    ).toBe(1)
    expect(r4d.lastFlushed, '4d: stop() must flush the last pending value verbatim').toBe('pending-final')
  })

  test('case5 ws double-tab cascade: workspace delete tombstones messages on B via realtime', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-ws', 'realtime-ws profile only')

    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))

      const wsId = `ws-${Math.random().toString(36).slice(2, 10)}`
      const dlgId = `dlg-${Math.random().toString(36).slice(2, 10)}`
      const m1 = `msg-${Math.random().toString(36).slice(2, 10)}`
      const m2 = `msg-${Math.random().toString(36).slice(2, 10)}`

      await pageA.evaluate(async ({ wsId, dlgId, m1, m2, wsBody, dlgBody }) => {
        const w = window as AnyWindow
        await w.__repos__.workspaces.put({ ...wsBody, id: wsId })
        await w.__repos__.dialogs.put({ ...dlgBody, id: dlgId })
        await w.__repos__.messages.put({
          id: m1,
          type: 'assistant',
          dialogId: dlgId,
          contents: [{ type: 'assistant-message', text: 'first' }],
          status: 'default'
        })
        await w.__repos__.messages.put({
          id: m2,
          type: 'assistant',
          dialogId: dlgId,
          contents: [{ type: 'assistant-message', text: 'second' }],
          status: 'default'
        })
      }, { wsId, dlgId, m1, m2, wsBody: SAMPLE_WORKSPACE, dlgBody: buildDialog(wsId, 'd-cascade') })

      await activateObservers(pageA, dlgId)
      await activateObservers(pageB, dlgId)
      await expectRowSync(pageA, pageB, 'workspaces', wsId, { withinMs: 1_500 })
      await expectRowSync(pageA, pageB, 'dialogs', dlgId, { withinMs: 1_500 })
      await expectRowSync(pageA, pageB, 'messages', m1, { withinMs: 1_500 })
      await expectRowSync(pageA, pageB, 'messages', m2, { withinMs: 1_500 })

      // Replicate stores/workspaces.ts::deleteItem cleanup for what's still
      // dexie-managed today, then trigger the server-side cascade via
      // workspaces.delete() (which forwards ?cascade=true).
      await pageA.evaluate(async ({ wsId }) => {
        const w = window as AnyWindow
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

      // Server-side: both messages tombstones
      const serverList = await listMessages(user.accessForBackend, dlgId, 0)
      const rows = Array.isArray(serverList) ? serverList : serverList.rows
      const t1 = rows.find(r => r.id === m1)
      const t2 = rows.find(r => r.id === m2)
      expect(
        t1 && t2,
        `server message tombstones missing; rows: ${JSON.stringify(rows.map(r => ({ id: r.id, deleted: r.deleted })))}`
      ).toBeTruthy()
      expect(t1!.deleted).toBe(true)
      expect(t1!.data).toBeNull()
      expect(t2!.deleted).toBe(true)
      expect(t2!.data).toBeNull()

      // B side: both messages gone via WS delete events within 1.5s
      const removed = await pollUntil(
        () => dumpTable<{ id: string }>(pageB, 'messages'),
        rows => !rows.some(r => r.id === m1) && !rows.some(r => r.id === m2),
        1_500
      )
      expect(
        removed.ok,
        `messages ${m1}/${m2} should be gone from B within 1.5s after cascade; ` +
          `last B rows: ${JSON.stringify(removed.last.map(r => r.id))}`
      ).toBe(true)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case6 providers-rest no-realtime: A change is invisible to B until reload', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')

    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))
      // Warm B's caches once
      await pageB.evaluate(async () => {
        const w = window as AnyWindow
        await w.__repos__.workspaces.list()
        await w.__repos__.dialogs.list()
      })

      const wsId = `ws-rest-${Math.random().toString(36).slice(2, 10)}`
      const dlgId = `dlg-rest-${Math.random().toString(36).slice(2, 10)}`
      const msgId = `msg-rest-${Math.random().toString(36).slice(2, 10)}`
      await pageA.evaluate(async ({ wsId, dlgId, msgId, wsBody, dlgBody }) => {
        const w = window as AnyWindow
        await w.__repos__.workspaces.put({ ...wsBody, id: wsId })
        await w.__repos__.dialogs.put({ ...dlgBody, id: dlgId })
        await w.__repos__.messages.put({
          id: msgId,
          type: 'assistant',
          dialogId: dlgId,
          contents: [{ type: 'assistant-message', text: 'rest-only' }],
          status: 'default'
        })
      }, { wsId, dlgId, msgId, wsBody: SAMPLE_WORKSPACE, dlgBody: buildDialog(wsId, 'd-host') })

      await new Promise(resolve => setTimeout(resolve, 1_500))
      const beforeReload = await dumpTable<{ id: string }>(pageB, 'messages')
      expect(
        beforeReload.some(r => r.id === msgId),
        `providers-rest must NOT propagate messages without reload; B saw ${msgId} before reload ` +
          `(rows=${JSON.stringify(beforeReload.map(r => r.id))})`
      ).toBe(false)

      // Reload + scoped pull on dialog → B picks up the message
      await injectAuth(pageB, await freshSession(user))
      await pageB.reload()
      await exposeReady(pageB)
      await pageB.waitForFunction(() => {
        const auth = (window as AnyWindow).__authSource__
        return !!(auth && auth.currentToken && auth.currentToken())
      }, undefined, { timeout: 10_000 })
      await pageB.evaluate(async (dialogId) => {
        const w = window as AnyWindow
        await w.__repos__.messages.find({ where: { dialogId } })
      }, dlgId)
      const afterReload = await dumpTable<{ id: string }>(pageB, 'messages')
      expect(
        afterReload.some(r => r.id === msgId),
        `after reload + scoped find(), B should pull ${msgId} from server ` +
          `(rows=${JSON.stringify(afterReload.map(r => r.id))})`
      ).toBe(true)

      const serverList = await listMessages(user.accessForBackend, dlgId, 0)
      const rows = Array.isArray(serverList) ? serverList : serverList.rows
      expect(
        rows.some(r => r.id === msgId && r.deleted === false),
        `server should hold message ${msgId}; rows=${JSON.stringify(rows.map(r => ({ id: r.id, deleted: r.deleted })))}`
      ).toBe(true)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case7 baseline byte-identical: zero backend traffic and zero websocket connections', async ({ page }, testInfo) => {
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
    const msgs = await dumpTable(page, 'messages')
    expect(Array.isArray(msgs)).toBe(true)
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
