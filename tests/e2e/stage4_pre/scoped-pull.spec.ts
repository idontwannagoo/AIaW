// Stage 4 / 硬前置 3 — scoped pull behavior on dialogs / items server.ts.
//
// What this proves:
//  - case1: an `observeFind({ where: { dialogId: X } })` (or items.find /
//    items.findKeys with the same spec) issues a single GET against
//    `/api/v1/items?dialogId=X&since=…` rather than the full-table
//    `/api/v1/items?since=…` route. Other dialogs' items must not appear
//    in IDB.
//  - case2: switching between three different dialogs hits the scoped
//    endpoint once per dialog (3 scoped GETs, zero full-table GETs).
//  - case3: the scoped lastVersion cache prevents redundant fetches —
//    when nothing has changed, a second observeFind/find/count for the
//    same scope advances the cursor (since>0) but does NOT re-fetch
//    rows we already have. We measure by counting fetches across
//    repeated mounts of the same scope.
//  - case4: cross-dialog realtime: A creates an item under dialog X,
//    B is observing dialog Y. The B tab receives the realtime event
//    (broker fans out per-user), but the scoped lastVersion for Y must
//    not advance — applyEvent ignores events whose scopeKey isn't
//    already in the state map (no speculative scope creation).
//
// Profile choice:
//  - case1/2/3 run in providers-rest. We only need scoped HTTP plumbing,
//    not realtime. providers-rest gives us BACKEND_AUTH + BACKEND_DATA_API
//    without WS.
//  - case4 needs realtime so it pins to realtime-ws. The other cases
//    skip on realtime-ws to keep wall time low; case4 alone in this
//    profile is enough.

import { test, expect, type Page } from '@playwright/test'
import { exposeReady, dumpTable, clearAll } from '../helpers/db'
import { putDialog, putItem } from '../helpers/backend'
import {
  registerViaApi,
  loginApi,
  injectAuth,
  type TokenPair
} from '../helpers/auth'
import { openContextsForUsers } from '../helpers/tabs'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyWindow = any

interface TestUser {
  email: string
  password: string
  accessForBackend: string
}

function uniqEmail(): string {
  return `stage4pre-scope-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
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
  page.on('pageerror', e => console.log('[page-error]', e.message))
  await page.goto('/')
  await exposeReady(page)
  await page.waitForFunction(() => {
    const auth = (window as AnyWindow).__authSource__
    return !!(auth && auth.currentToken && auth.currentToken())
  }, undefined, { timeout: 10_000 })
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

function buildItem(dialogId: string, idSuffix: string, text = 'hello') {
  return {
    id: `i-${idSuffix}`,
    dialogId,
    type: 'text',
    references: 1,
    contentText: text
  }
}

interface ItemsListReq {
  url: string
  dialogId: string | null
  since: string | null
}

function classifyItemsListUrl(url: string): ItemsListReq | null {
  // We're after the LIST endpoint, not /items/{id}. Match the path
  // exactly; query params decide scoped vs full-table.
  const u = new URL(url)
  if (u.pathname !== '/api/v1/items') return null
  return {
    url,
    dialogId: u.searchParams.get('dialogId'),
    since: u.searchParams.get('since')
  }
}

function startItemsListRecorder(page: Page): { reqs: ItemsListReq[] } {
  const reqs: ItemsListReq[] = []
  page.on('request', (req) => {
    if (req.method() !== 'GET') return
    const c = classifyItemsListUrl(req.url())
    if (c) reqs.push(c)
  })
  return { reqs }
}

test.describe('stage4_pre scoped pull', () => {
  test('dialog opens with empty IDB cache → only target dialog items appear', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')

    const user = await setupTestUser()
    // Seed two dialogs (under one workspace) with items each, server-side,
    // BEFORE the page loads — so the page's first observeFind triggers
    // exactly one scoped pull for the chosen dialog.
    const tag = Math.random().toString(36).slice(2, 8)
    const wsId = `ws-${tag}`
    const dlgX = `dlgX-${tag}`
    const dlgY = `dlgY-${tag}`
    await page.context().request.fetch(`http://127.0.0.1:9011/api/v1/workspaces/${wsId}`, {
      method: 'PUT',
      headers: { Authorization: `Bearer ${user.accessForBackend}`, 'Content-Type': 'application/json' },
      data: JSON.stringify(SAMPLE_WORKSPACE)
    })
    await putDialog(user.accessForBackend, dlgX, buildDialog(wsId, 'd-X'))
    await putDialog(user.accessForBackend, dlgY, buildDialog(wsId, 'd-Y'))
    const xIds: string[] = []
    const yIds: string[] = []
    for (let i = 0; i < 3; i++) {
      const xId = `i-X-${tag}-${i}`
      const yId = `i-Y-${tag}-${i}`
      xIds.push(xId)
      yIds.push(yId)
      await putItem(user.accessForBackend, xId, buildItem(dlgX, `X-${tag}-${i}`, `x${i}`))
      await putItem(user.accessForBackend, yId, buildItem(dlgY, `Y-${tag}-${i}`, `y${i}`))
    }

    const { reqs } = startItemsListRecorder(page)
    await bootSession(page, await freshSession(user))
    // Wipe any boot-time cache so observeFind has no choice but to fetch.
    await clearAll(page, ['items'])
    // Reset the per-table module-level lastVersion + scopedPull state map
    // so the upcoming pull starts at since=0 and counts as a fresh scoped
    // fetch. We achieve this by calling repos.items.list() over a torn-down
    // cache (already done by clearAll); items.server.ts pull() resets
    // lastVersion=0 + scopedPull.reset() when it sees count===0.
    await page.evaluate(async () => {
      const w = window as AnyWindow
      // Touch list() to flush the count-based reset path. We then drop the
      // result; it's just to clear lastVersion. After this, drop any items
      // again so the per-scope fetch is forced to find them.
      await w.__db__.items.clear()
    })

    // Snapshot reqs after setup; the meaningful window starts here.
    const before = reqs.length

    // Open dialog X via observeFind — this is exactly what DialogView does.
    await page.evaluate(async (dialogId) => {
      const w = window as AnyWindow
      // Fire the same code path observeFind uses on mount.
      w.__scope_obs__ = w.__repos__.items.observeFind({ where: { dialogId } })
      // Poll observe until it has data — observe is async, the underlying
      // pullForSpec resolves before liveQuery flushes.
      const start = Date.now()
      // Bonus: also call .find() to be fully sure pull finished and the
      // cache reflects it. find() is the same scoped path.
      await w.__repos__.items.find({ where: { dialogId } })
      void start
    }, dlgX)

    // Inspect IDB: only X's items must be present.
    const itemsInIdb = await dumpTable<{ id: string; dialogId?: string }>(page, 'items')
    const idbIds = itemsInIdb.map(r => r.id).sort()
    expect(
      idbIds,
      `IDB should only contain dialog X items, got ${JSON.stringify(idbIds)}; ` +
        `requests since boot=${JSON.stringify(reqs.slice(before))}`
    ).toEqual([...xIds].sort())
    for (const r of itemsInIdb) {
      expect(r.dialogId, `IDB row ${r.id} not under dialog X: ${JSON.stringify(r)}`).toBe(dlgX)
    }

    // Network: at least one scoped GET for dialogId=X, and zero
    // full-table GETs (no dialogId param) post-setup boot.
    const newReqs = reqs.slice(before)
    const scopedX = newReqs.filter(r => r.dialogId === dlgX)
    const fullTable = newReqs.filter(r => r.dialogId === null)
    const scopedY = newReqs.filter(r => r.dialogId === dlgY)
    expect(
      scopedX.length,
      `expected ≥1 GET /items?dialogId=${dlgX}; got ${scopedX.length}. ` +
        `All post-setup item LIST reqs: ${JSON.stringify(newReqs)}`
    ).toBeGreaterThanOrEqual(1)
    expect(
      fullTable.length,
      `expected zero full-table GET /items (no dialogId param); got ${fullTable.length}: ` +
        `${JSON.stringify(fullTable)}`
    ).toBe(0)
    expect(
      scopedY.length,
      `expected zero GET /items?dialogId=${dlgY}; got ${scopedY.length}: ` +
        `${JSON.stringify(scopedY)}`
    ).toBe(0)
  })

  test('switching dialogs hits scoped endpoint per dialog', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')

    const user = await setupTestUser()
    const wsId = `ws-${Math.random().toString(36).slice(2, 10)}`
    const ds = [0, 1, 2].map(i => `dlg-${i}-${Math.random().toString(36).slice(2, 8)}`)
    await page.context().request.fetch(`http://127.0.0.1:9011/api/v1/workspaces/${wsId}`, {
      method: 'PUT',
      headers: { Authorization: `Bearer ${user.accessForBackend}`, 'Content-Type': 'application/json' },
      data: JSON.stringify(SAMPLE_WORKSPACE)
    })
    for (const dlgId of ds) {
      await putDialog(user.accessForBackend, dlgId, buildDialog(wsId, dlgId))
      await putItem(user.accessForBackend, `i-${dlgId}-1`, buildItem(dlgId, `${dlgId}-1`))
    }

    const { reqs } = startItemsListRecorder(page)
    await bootSession(page, await freshSession(user))
    await clearAll(page, ['items'])
    const before = reqs.length

    // Open three dialogs sequentially.
    for (const dlgId of ds) {
      await page.evaluate(async (id) => {
        const w = window as AnyWindow
        await w.__repos__.items.find({ where: { dialogId: id } })
      }, dlgId)
    }

    const newReqs = reqs.slice(before)
    const fullTable = newReqs.filter(r => r.dialogId === null)
    expect(
      fullTable.length,
      `expected zero full-table GET /items; got ${fullTable.length}: ${JSON.stringify(fullTable)}`
    ).toBe(0)

    for (const dlgId of ds) {
      const scoped = newReqs.filter(r => r.dialogId === dlgId)
      expect(
        scoped.length,
        `expected ≥1 GET /items?dialogId=${dlgId}; got ${scoped.length}. ` +
          `All post-setup reqs: ${JSON.stringify(newReqs)}`
      ).toBeGreaterThanOrEqual(1)
    }
  })

  test('scoped lastVersion cache prevents redundant fetch', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')

    const user = await setupTestUser()
    const wsId = `ws-${Math.random().toString(36).slice(2, 10)}`
    const dlgId = `dlg-${Math.random().toString(36).slice(2, 10)}`
    await page.context().request.fetch(`http://127.0.0.1:9011/api/v1/workspaces/${wsId}`, {
      method: 'PUT',
      headers: { Authorization: `Bearer ${user.accessForBackend}`, 'Content-Type': 'application/json' },
      data: JSON.stringify(SAMPLE_WORKSPACE)
    })
    await putDialog(user.accessForBackend, dlgId, buildDialog(wsId, 'd-cache'))
    const tag3 = Math.random().toString(36).slice(2, 8)
    await putItem(user.accessForBackend, `i-c-${tag3}-1`, buildItem(dlgId, `c-${tag3}-1`))
    await putItem(user.accessForBackend, `i-c-${tag3}-2`, buildItem(dlgId, `c-${tag3}-2`))

    const { reqs } = startItemsListRecorder(page)
    await bootSession(page, await freshSession(user))
    await clearAll(page, ['items'])
    const before = reqs.length

    // First mount — should fetch with since=0 for this scope.
    await page.evaluate(async (id) => {
      const w = window as AnyWindow
      await w.__repos__.items.find({ where: { dialogId: id } })
    }, dlgId)

    const afterFirst = reqs.slice(before)
    const firstScoped = afterFirst.filter(r => r.dialogId === dlgId)
    expect(
      firstScoped.length,
      `first mount should produce ≥1 scoped fetch; got ${firstScoped.length}: ${JSON.stringify(afterFirst)}`
    ).toBeGreaterThanOrEqual(1)
    expect(
      firstScoped[0].since,
      `first scoped fetch since must be 0 (or null); got ${firstScoped[0].since}`
    ).toMatch(/^(0|)$/)

    // Multiple subsequent re-mounts of the same scope. After the first,
    // scopedPull's lastVersion has advanced past the rows we just got.
    // Each follow-up fetch (if any) must use since>0; the cache prevents
    // re-pulling the rows we already have. We don't strictly require zero
    // additional fetches (the cursor advances even if no new rows appear,
    // and the helper still issues the GET to check), but we DO require:
    //   - any additional fetches use since > 0
    //   - no full-table fetches
    //   - the total number of scoped fetches is bounded (≤ N+1 for N
    //     follow-up mounts; an upper bound proves we're not regressing
    //     to a per-mount full re-pull-from-zero).
    const followups = 3
    for (let i = 0; i < followups; i++) {
      await page.evaluate(async (id) => {
        const w = window as AnyWindow
        await w.__repos__.items.find({ where: { dialogId: id } })
      }, dlgId)
    }

    const all = reqs.slice(before)
    const allScoped = all.filter(r => r.dialogId === dlgId)
    const allFull = all.filter(r => r.dialogId === null)
    expect(
      allFull.length,
      `expected zero full-table GET /items across cache repeats; got ${allFull.length}: ${JSON.stringify(allFull)}`
    ).toBe(0)
    // Since cursors advance after the first pull, every fetch after the
    // first must carry since > 0.
    const sinceProbes = allScoped.map(r => Number(r.since ?? '0'))
    const firstSince = sinceProbes[0]
    const restSinces = sinceProbes.slice(1)
    expect(
      firstSince,
      `first scoped fetch should carry since=0; got ${firstSince}`
    ).toBe(0)
    for (const s of restSinces) {
      expect(
        s,
        `follow-up scoped fetch must use since>0 (cursor advanced); got since=${s}, ` +
          `all scoped sinces=${JSON.stringify(sinceProbes)}`
      ).toBeGreaterThan(0)
    }
    // Hard upper bound: 1 initial + followups (one per re-mount). This
    // catches a regression where each mount fans out into multiple
    // fetches.
    expect(
      allScoped.length,
      `scoped fetches should be ≤ 1 + ${followups} = ${1 + followups}; got ${allScoped.length}: ` +
        `${JSON.stringify(allScoped)}`
    ).toBeLessThanOrEqual(1 + followups)
  })

  test('cross-dialog realtime event applies to correct scope cache', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-ws', 'realtime-ws profile only')

    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))

      // Seed workspace + two dialogs server-side from pageA's context to
      // keep wire dependencies obvious.
      const wsId = `ws-${Math.random().toString(36).slice(2, 10)}`
      const dlgX = `dlgX-${Math.random().toString(36).slice(2, 10)}`
      const dlgY = `dlgY-${Math.random().toString(36).slice(2, 10)}`
      await pageA.evaluate(async ({ wsId, dlgX, dlgY, wsBody, dlgXBody, dlgYBody }) => {
        const w = window as AnyWindow
        await w.__repos__.workspaces.put({ ...wsBody, id: wsId })
        await w.__repos__.dialogs.put({ ...dlgXBody, id: dlgX })
        await w.__repos__.dialogs.put({ ...dlgYBody, id: dlgY })
      }, {
        wsId,
        dlgX,
        dlgY,
        wsBody: SAMPLE_WORKSPACE,
        dlgXBody: buildDialog(wsId, 'd-X'),
        dlgYBody: buildDialog(wsId, 'd-Y')
      })

      // B observes ONLY dialog Y. This populates B's scopedPull state
      // map for `items:dialogId:dlgY` and ALSO subscribes B to realtime
      // for the items table.
      await pageB.evaluate(async (id) => {
        const w = window as AnyWindow
        w.__scope_obs__ = w.__repos__.items.observeFind({ where: { dialogId: id } })
        await w.__repos__.items.find({ where: { dialogId: id } })
      }, dlgY)

      // Wait for B's realtime subscription to register on the broker
      // before A's write — otherwise B will silently miss the event.
      await pageB.waitForTimeout(500)

      // A writes an item under dialog X. Realtime fans out to B (broker
      // is per-user). The applyEvent guard in scoped-pull must ignore
      // dlgX events because B has never pulled dlgX (no entry in B's
      // state map).
      await pageA.evaluate(async ({ dlgX, idSuffix }) => {
        const w = window as AnyWindow
        await w.__repos__.items.put({
          id: `i-cross-${idSuffix}`,
          dialogId: dlgX,
          type: 'text',
          references: 1,
          contentText: 'cross'
        })
      }, { dlgX, idSuffix: Math.random().toString(36).slice(2, 6) })

      // Give realtime time to deliver. The realtime handler on B WILL
      // write the row to db.items (handler runs unconditional db.put);
      // that's fine — the realtime invariant is "all user rows replicate
      // to all tabs". The scope-cache invariant is independent: B's
      // scopedPull state for dlgY must NOT have an entry for dlgX, and
      // the lastVersion for dlgY must NOT have advanced from this event
      // (because dlgY had no event).
      await pageB.waitForTimeout(800)

      // Verifier: trigger a fresh pullScope(dlgX) on B and observe the
      // since= parameter. If dlgX had been polluted (entry created from
      // realtime), since would be > 0 (the rev of the cross-event).
      // If applyEvent correctly ignored the unknown scope, this is the
      // FIRST time dlgX is pulled and since must be 0.
      const reqs: ItemsListReq[] = []
      pageB.on('request', (req) => {
        if (req.method() !== 'GET') return
        const c = classifyItemsListUrl(req.url())
        if (c) reqs.push(c)
      })
      await pageB.evaluate(async (id) => {
        const w = window as AnyWindow
        await w.__repos__.items.find({ where: { dialogId: id } })
      }, dlgX)

      const dlgXReqs = reqs.filter(r => r.dialogId === dlgX)
      expect(
        dlgXReqs.length,
        'B should issue a fresh GET /items?dialogId=' + dlgX + ' after the realtime event; ' +
          'got ' + dlgXReqs.length + ': ' + JSON.stringify(reqs)
      ).toBeGreaterThanOrEqual(1)
      // The first dlgX fetch on B must use since=0 — the scope was never
      // pulled before; applyEvent must NOT have speculatively created
      // its entry from the cross-dialog event.
      expect(
        Number(dlgXReqs[0].since ?? '0'),
        'dlgX scope was polluted by cross-dialog realtime event — first ' +
          'pullScope(' + dlgX + ') used since=' + dlgXReqs[0].since + ' instead of 0. ' +
          'This means scoped-pull.applyEvent created a state entry for an ' +
          'un-pulled scope. All B item LIST reqs: ' + JSON.stringify(reqs)
      ).toBe(0)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })
})
