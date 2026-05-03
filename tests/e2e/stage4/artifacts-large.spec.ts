// Stage 4 / 批次-4d — artifacts server-routed CRUD + ref-versions
// round-trip + workspace cascade fan-out + scoped pull.
//
// 6 cases (matching the plan's 通过判据 line for batch-4d):
//   case1 ws double-tab inline artifact: put + update + delete propagate
//     within 1.5s (plain text small `versions`, no spill)
//   case2 ws double-tab 100KB+ ref versions: A puts an Artifact whose
//     versions JSON is well above the 64KB threshold → blob-client
//     serializes via /api/v1/blobs ref → server `data.versionsBlob.type
//     === 'ref'` + sha256 length 64 + size matches → B receives realtime
//     → materializeAttachment fetches via signed URL → B's `versions`
//     are byte-identical to A's
//   case3 ws double-tab cascade: A's workspace delete tombstones
//     artifacts on B's side within 1.5s via realtime; server-side
//     artifact rows are tombstones too (no dialog hop — `Artifact`
//     carries only `workspaceId`)
//   case4 scoped pull (providers-rest): empty IDB → observeFind({
//     where: { workspaceId: X } }) issues a single `?workspaceId=X`
//     scoped GET, no full-table fetch; only target workspace's
//     artifacts appear in IDB
//   case5 providers-rest no-realtime: A change is invisible to B
//     until reload
//   case6 baseline byte-identical: zero backend traffic, zero ws
//
// Notes:
//   - putArtifact requires a workspaceId that already exists for the
//     user; the server returns 409 otherwise (mirrors items+dialogs).
//   - case2 verifies the full versions-spill ref pipeline end-to-end:
//     encodeForWire → POST /api/v1/blobs → versionsBlob ref envelope
//     in artifacts.data → realtime fan-out → materializeAttachment →
//     signed-URL GET → ArtifactVersion[] in B's IDB. The threshold is
//     64KB (BLOB_INLINE_MAX_BYTES) measured on the JSON-stringified
//     `versions` byte length (utf-8); we use ~80KB of plain text in a
//     single version to safely cross it.
//   - cascade hop: workspace cascade tombstones artifacts directly off
//     workspace_id (no dialog hop). The realtime broker emits one
//     delete event per cascaded artifact; B's artifacts table converges
//     via the artifacts observer.

import { test, expect, type Page } from '@playwright/test'
import { exposeReady, dumpTable, clearAll } from '../helpers/db'
import { listArtifacts, putWorkspace, putArtifact } from '../helpers/backend'
import { registerViaApi, loginApi, injectAuth, type TokenPair } from '../helpers/auth'
import { openContextsForUsers } from '../helpers/tabs'
import { expectRowSync } from '../helpers/sync'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyWindow = any

function uniqEmail(): string {
  return `stage4-art-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
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

async function activateObservers(page: Page): Promise<void> {
  // Observers are what wires the realtime subscription; without one, the
  // listener isn't attached and the per-table WS / SSE filter rejects
  // events. workspaces / dialogs / artifacts all server-routed at 4d.
  await page.evaluate(() => {
    const w = window as AnyWindow
    w.__stage4d_obs__ = {
      workspaces: w.__repos__.workspaces.observeList(),
      dialogs: w.__repos__.dialogs.observeList(),
      artifacts: w.__repos__.artifacts.observeList()
    }
  })
  await page.evaluate(async () => {
    const w = window as AnyWindow
    await w.__repos__.workspaces.list()
    await w.__repos__.dialogs.list()
    await w.__repos__.artifacts.list()
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

interface WireArtifactSeed {
  id: string
  workspaceId: string
  name?: string
  versionText?: string
  currIndex?: number
}

function buildArtifactInline({
  id, workspaceId, name = 'Artifact 1', versionText = 'hello',
  currIndex = 0
}: WireArtifactSeed) {
  return {
    id,
    name,
    workspaceId,
    versions: [{ date: new Date('2026-05-03T00:00:00.000Z'), text: versionText }],
    currIndex,
    readable: true,
    writable: true,
    open: false,
    tmp: ''
  }
}

interface ArtifactsListReq {
  url: string
  workspaceId: string | null
  since: string | null
}

function classifyArtifactsListUrl(url: string): ArtifactsListReq | null {
  // Match the LIST endpoint, not /artifacts/{id}. Query params decide
  // scoped vs full-table.
  const u = new URL(url)
  if (u.pathname !== '/api/v1/artifacts') return null
  return {
    url,
    workspaceId: u.searchParams.get('workspaceId'),
    since: u.searchParams.get('since')
  }
}

function startArtifactsListRecorder(page: Page): { reqs: ArtifactsListReq[] } {
  const reqs: ArtifactsListReq[] = []
  page.on('request', (req) => {
    if (req.method() !== 'GET') return
    const c = classifyArtifactsListUrl(req.url())
    if (c) reqs.push(c)
  })
  return { reqs }
}

test.describe('stage4 batch-4d artifacts server-routed', () => {
  test('case1 ws double-tab inline artifact: put + update + delete propagate within 1.5s', async ({ browser }, testInfo) => {
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
      const artId = `art-${Math.random().toString(36).slice(2, 10)}`

      // Seed workspace (FK)
      await pageA.evaluate(async ({ wsId, wsBody }) => {
        const w = window as AnyWindow
        await w.__repos__.workspaces.put({ ...wsBody, id: wsId })
      }, { wsId, wsBody: SAMPLE_WORKSPACE })
      await expectRowSync(pageA, pageB, 'workspaces', wsId, { withinMs: 1_500 })

      // Inline artifact (small versions text) propagates A → B
      await pageA.evaluate(async ({ id, workspaceId }) => {
        const w = window as AnyWindow
        await w.__repos__.artifacts.put({
          id,
          name: 'Artifact 1',
          workspaceId,
          versions: [{ date: new Date('2026-05-03T00:00:00.000Z'), text: 'inline-v1' }],
          currIndex: 0,
          readable: true,
          writable: true,
          open: false,
          tmp: ''
        })
      }, { id: artId, workspaceId: wsId })
      await expectRowSync(pageA, pageB, 'artifacts', artId, { withinMs: 1_500 })

      // Update versions[0].text → B sees v2
      await pageA.evaluate(async ({ id, workspaceId }) => {
        const w = window as AnyWindow
        await w.__repos__.artifacts.put({
          id,
          name: 'Artifact 1',
          workspaceId,
          versions: [{ date: new Date('2026-05-03T00:00:00.000Z'), text: 'inline-v2' }],
          currIndex: 0,
          readable: true,
          writable: true,
          open: false,
          tmp: ''
        })
      }, { id: artId, workspaceId: wsId })
      const updated = await pollUntil(
        () => dumpTable<{ id: string; versions?: Array<{ text?: string }> }>(pageB, 'artifacts'),
        rows => rows.find(r => r.id === artId)?.versions?.[0]?.text === 'inline-v2',
        1_500
      )
      expect(
        updated.ok,
        'B should see updated versions[0].text=\'inline-v2\' within 1.5s; ' +
          `last B rows: ${JSON.stringify(updated.last.map(r => ({ id: r.id, text: r.versions?.[0]?.text })))}`
      ).toBe(true)

      // Explicit delete → B sees the row gone
      await pageA.evaluate(async ({ id }) => {
        const w = window as AnyWindow
        await w.__repos__.artifacts.delete(id)
      }, { id: artId })
      const removed = await pollUntil(
        () => dumpTable<{ id: string }>(pageB, 'artifacts'),
        rows => !rows.some(r => r.id === artId),
        1_500
      )
      expect(
        removed.ok,
        `artifact ${artId} should be gone from B within 1.5s; ` +
          `last B rows: ${JSON.stringify(removed.last.map(r => r.id))}`
      ).toBe(true)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case2 ws double-tab: 100KB+ ref versions round-trip A→B via blob-client', async ({ browser }, testInfo) => {
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
      const artId = `art-${Math.random().toString(36).slice(2, 10)}`
      // 80KB of text → JSON-stringify includes quotes + date field, easily
      // pushes the serialized `versions` array over 64KB threshold.
      const TEXT_LEN = 80 * 1024

      await pageA.evaluate(async ({ wsId, wsBody }) => {
        const w = window as AnyWindow
        await w.__repos__.workspaces.put({ ...wsBody, id: wsId })
      }, { wsId, wsBody: SAMPLE_WORKSPACE })
      await expectRowSync(pageA, pageB, 'workspaces', wsId, { withinMs: 1_500 })

      // Build a deterministic ~80KB text on A. Use a repeating ASCII
      // pattern so we can fingerprint head/tail on B for byte equality.
      // artifacts.server.ts encodes versions via blob-client which, at
      // this serialized size, uploads the bytes as a `versionsBlob` ref.
      await pageA.evaluate(async ({ id, workspaceId, textLen }) => {
        const w = window as AnyWindow
        // ASCII chars in range [33,126] cycle for fingerprint-friendliness
        const arr = new Array(textLen)
        for (let i = 0; i < textLen; i++) arr[i] = String.fromCharCode(33 + (i % 94))
        const text = arr.join('')
        await w.__repos__.artifacts.put({
          id,
          name: 'Big',
          workspaceId,
          versions: [{ date: new Date('2026-05-03T00:00:00.000Z'), text }],
          currIndex: 0,
          readable: true,
          writable: true,
          open: false,
          tmp: ''
        })
      }, { id: artId, workspaceId: wsId, textLen: TEXT_LEN })

      // Wait for B to materialize the artifact with the full versions
      // text. Allow generous 5s — round-trip = realtime event ⇒ signed
      // URL fetch ⇒ versions write to IDB.
      const probe = await pollUntil(
        async () => pageB.evaluate(async (id) => {
          const w = window as AnyWindow
          const row = await w.__db__.artifacts.get(id)
          if (!row) return { stage: 'no-row' as const }
          if (!row.versions || row.versions.length === 0) {
            return { stage: 'no-versions' as const, name: row.name }
          }
          const v0 = row.versions[0]
          const text = v0.text as string
          if (typeof text !== 'string' || text.length === 0) {
            return { stage: 'empty-text' as const, name: row.name }
          }
          // Sample for fingerprinting — full text via page.evaluate IPC
          // would balloon the payload.
          return {
            stage: 'has-versions' as const,
            length: text.length,
            head: text.slice(0, 16),
            tail: text.slice(-16),
            dateIso: v0.date instanceof Date ? v0.date.toISOString() : String(v0.date)
          }
        }, artId),
        v => v.stage === 'has-versions' && v.length === TEXT_LEN,
        5_000
      )
      expect(
        probe.ok,
        `B did not materialize artifact with ${TEXT_LEN}-char versions[0].text within 5s; ` +
          `last=${JSON.stringify(probe.last)}`
      ).toBe(true)

      // Recompute the expected fingerprint on the test side and compare
      const expectedHeadArr = []
      for (let i = 0; i < 16; i++) expectedHeadArr.push(String.fromCharCode(33 + (i % 94)))
      const expectedHead = expectedHeadArr.join('')
      const expectedTailArr = []
      for (let k = 0; k < 16; k++) {
        const i = TEXT_LEN - 16 + k
        expectedTailArr.push(String.fromCharCode(33 + (i % 94)))
      }
      const expectedTail = expectedTailArr.join('')
      expect(
        probe.last,
        `byte fingerprint mismatch on B; expected head=${JSON.stringify(expectedHead)} tail=${JSON.stringify(expectedTail)}; got=${JSON.stringify(probe.last)}`
      ).toMatchObject({
        stage: 'has-versions',
        length: TEXT_LEN,
        head: expectedHead,
        tail: expectedTail,
        dateIso: '2026-05-03T00:00:00.000Z'
      })

      // Server-side: the row stores a ref envelope, NOT inline versions.
      // Confirms blob-client picked ref mode at this size.
      const serverArts = await listArtifacts(user.accessForBackend, 0)
      const art = serverArts.find(r => r.id === artId)
      expect(art, `server artifact ${artId} missing; rows=${JSON.stringify(serverArts.map(r => r.id))}`).toBeTruthy()
      const data = art!.data as Record<string, unknown> | null
      expect(data, `expected non-null data for ${artId}; got ${JSON.stringify(art!.data)}`).toBeTruthy()
      const blob = (data as Record<string, unknown>).versionsBlob as Record<string, unknown> | undefined
      expect(
        blob,
        `expected artifacts.data.versionsBlob ref envelope; got data=${JSON.stringify(data)}`
      ).toBeTruthy()
      expect(blob!.type).toBe('ref')
      expect(typeof blob!.sha256).toBe('string')
      expect((blob!.sha256 as string).length).toBe(64)
      // size attribute is the JSON byte length (not raw text length); >= 64KB
      expect(typeof blob!.size).toBe('number')
      expect(blob!.size as number).toBeGreaterThanOrEqual(64 * 1024)
      // wire `versions` is empty in ref mode
      expect((data as Record<string, unknown>).versions).toEqual([])
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case3 ws double-tab cascade: workspace delete tombstones artifacts directly', async ({ browser }, testInfo) => {
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
      const art1 = `art-${Math.random().toString(36).slice(2, 10)}`
      const art2 = `art-${Math.random().toString(36).slice(2, 10)}`

      // Seed workspace + 2 artifacts (no dialogs needed — artifacts hang
      // directly off workspace_id)
      await pageA.evaluate(async ({ wsId, art1, art2, wsBody }) => {
        const w = window as AnyWindow
        await w.__repos__.workspaces.put({ ...wsBody, id: wsId })
        await w.__repos__.artifacts.put({
          id: art1,
          name: 'first',
          workspaceId: wsId,
          versions: [{ date: new Date('2026-05-03T00:00:00.000Z'), text: 'first' }],
          currIndex: 0,
          readable: true,
          writable: true,
          open: false,
          tmp: ''
        })
        await w.__repos__.artifacts.put({
          id: art2,
          name: 'second',
          workspaceId: wsId,
          versions: [{ date: new Date('2026-05-03T00:00:00.000Z'), text: 'second' }],
          currIndex: 0,
          readable: true,
          writable: true,
          open: false,
          tmp: ''
        })
      }, { wsId, art1, art2, wsBody: SAMPLE_WORKSPACE })

      await expectRowSync(pageA, pageB, 'workspaces', wsId, { withinMs: 1_500 })
      await expectRowSync(pageA, pageB, 'artifacts', art1, { withinMs: 1_500 })
      await expectRowSync(pageA, pageB, 'artifacts', art2, { withinMs: 1_500 })

      // Replicate stores/workspaces.ts::deleteItem cleanup. The
      // server-side cascade tombstones artifacts behind the
      // workspace.delete call directly (no dialog hop); the explicit
      // per-table sweep is idempotent against tombstones.
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

      // Server-side: both artifacts are tombstones
      const serverArts = await listArtifacts(user.accessForBackend, 0)
      const t1 = serverArts.find(r => r.id === art1)
      const t2 = serverArts.find(r => r.id === art2)
      expect(
        t1 && t2,
        `server artifact tombstones missing; rows: ${JSON.stringify(serverArts.map(r => ({ id: r.id, deleted: r.deleted })))}`
      ).toBeTruthy()
      expect(t1!.deleted).toBe(true)
      expect(t1!.data).toBeNull()
      expect(t2!.deleted).toBe(true)
      expect(t2!.data).toBeNull()

      // B side: both artifact rows gone via WS delete events within 1.5s
      const removed = await pollUntil(
        () => dumpTable<{ id: string }>(pageB, 'artifacts'),
        rows => !rows.some(r => r.id === art1) && !rows.some(r => r.id === art2),
        1_500
      )
      expect(
        removed.ok,
        `artifacts ${art1}/${art2} should be gone from B within 1.5s after cascade; ` +
          `last B rows: ${JSON.stringify(removed.last.map(r => r.id))}`
      ).toBe(true)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case4 scoped pull: empty IDB → only target workspace artifacts appear, no full-table fetch', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')

    const user = await setupTestUser()
    // Seed two workspaces with artifacts each, server-side, BEFORE the
    // page loads — so the page's first observeFind triggers exactly one
    // scoped pull for the chosen workspace.
    const tag = Math.random().toString(36).slice(2, 8)
    const wsX = `wsX-${tag}`
    const wsY = `wsY-${tag}`
    await putWorkspace(user.accessForBackend, wsX, { ...SAMPLE_WORKSPACE })
    await putWorkspace(user.accessForBackend, wsY, { ...SAMPLE_WORKSPACE })
    const xIds: string[] = []
    const yIds: string[] = []
    for (let i = 0; i < 3; i++) {
      const xId = `a-X-${tag}-${i}`
      const yId = `a-Y-${tag}-${i}`
      xIds.push(xId)
      yIds.push(yId)
      await putArtifact(user.accessForBackend, xId, buildArtifactInline({
        id: xId, workspaceId: wsX, name: `x${i}`, versionText: `vx${i}`
      }))
      await putArtifact(user.accessForBackend, yId, buildArtifactInline({
        id: yId, workspaceId: wsY, name: `y${i}`, versionText: `vy${i}`
      }))
    }

    const { reqs } = startArtifactsListRecorder(page)
    await bootSession(page, await freshSession(user))
    // Wipe any boot-time cache so observeFind has no choice but to fetch.
    await clearAll(page, ['artifacts'])
    await page.evaluate(async () => {
      const w = window as AnyWindow
      await w.__db__.artifacts.clear()
    })

    // Snapshot reqs after setup; the meaningful window starts here.
    const before = reqs.length

    // Open workspace X via observeFind
    await page.evaluate(async (workspaceId) => {
      const w = window as AnyWindow
      w.__scope_obs__ = w.__repos__.artifacts.observeFind({ where: { workspaceId } })
      await w.__repos__.artifacts.find({ where: { workspaceId } })
    }, wsX)

    // Inspect IDB: only X's artifacts must be present
    const artsInIdb = await dumpTable<{ id: string; workspaceId?: string }>(page, 'artifacts')
    const idbIds = artsInIdb.map(r => r.id).sort()
    expect(
      idbIds,
      `IDB should only contain workspace X artifacts, got ${JSON.stringify(idbIds)}; ` +
        `requests since boot=${JSON.stringify(reqs.slice(before))}`
    ).toEqual([...xIds].sort())
    for (const r of artsInIdb) {
      expect(r.workspaceId, `IDB row ${r.id} not under workspace X: ${JSON.stringify(r)}`).toBe(wsX)
    }

    // Network: ≥1 scoped GET for workspaceId=X, zero full-table GETs
    const newReqs = reqs.slice(before)
    const scopedX = newReqs.filter(r => r.workspaceId === wsX)
    const fullTable = newReqs.filter(r => r.workspaceId === null)
    const scopedY = newReqs.filter(r => r.workspaceId === wsY)
    expect(
      scopedX.length,
      `expected ≥1 GET /artifacts?workspaceId=${wsX}; got ${scopedX.length}. ` +
        `All post-setup artifact LIST reqs: ${JSON.stringify(newReqs)}`
    ).toBeGreaterThanOrEqual(1)
    expect(
      fullTable.length,
      `expected zero full-table GET /artifacts (no workspaceId param); got ${fullTable.length}: ` +
        `${JSON.stringify(fullTable)}`
    ).toBe(0)
    expect(
      scopedY.length,
      `expected zero GET /artifacts?workspaceId=${wsY}; got ${scopedY.length}: ` +
        `${JSON.stringify(scopedY)}`
    ).toBe(0)
  })

  test('case5 providers-rest no-realtime: A change is invisible to B until reload', async ({ browser }, testInfo) => {
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
        const w = window as AnyWindow
        await w.__repos__.workspaces.list()
        await w.__repos__.artifacts.list()
      })

      const wsId = `ws-rest-${Math.random().toString(36).slice(2, 10)}`
      const artId = `art-rest-${Math.random().toString(36).slice(2, 10)}`
      await pageA.evaluate(async ({ wsId, artId, wsBody }) => {
        const w = window as AnyWindow
        await w.__repos__.workspaces.put({ ...wsBody, id: wsId })
        await w.__repos__.artifacts.put({
          id: artId,
          name: 'rest-only',
          workspaceId: wsId,
          versions: [{ date: new Date('2026-05-03T00:00:00.000Z'), text: 'rest-only' }],
          currIndex: 0,
          readable: true,
          writable: true,
          open: false,
          tmp: ''
        })
      }, { wsId, artId, wsBody: SAMPLE_WORKSPACE })

      await new Promise(resolve => setTimeout(resolve, 1_500))
      const beforeReload = await dumpTable<{ id: string }>(pageB, 'artifacts')
      expect(
        beforeReload.some(r => r.id === artId),
        `providers-rest must NOT propagate artifacts without reload; B saw ${artId} before reload ` +
          `(rows=${JSON.stringify(beforeReload.map(r => r.id))})`
      ).toBe(false)

      await injectAuth(pageB, await freshSession(user))
      await pageB.reload()
      await exposeReady(pageB)
      await pageB.waitForFunction(() => {
        const auth = (window as AnyWindow).__authSource__
        return !!(auth && auth.currentToken && auth.currentToken())
      }, undefined, { timeout: 10_000 })
      await pageB.evaluate(async () => {
        const w = window as AnyWindow
        await w.__repos__.artifacts.list()
      })
      const afterReload = await dumpTable<{ id: string }>(pageB, 'artifacts')
      expect(
        afterReload.some(r => r.id === artId),
        `after reload + list(), B should pull ${artId} from server ` +
          `(rows=${JSON.stringify(afterReload.map(r => r.id))})`
      ).toBe(true)

      const serverRows = await listArtifacts(user.accessForBackend, 0)
      expect(
        serverRows.some(r => r.id === artId && r.deleted === false),
        `server should hold artifact ${artId}; rows=${JSON.stringify(serverRows.map(r => ({ id: r.id, deleted: r.deleted })))}`
      ).toBe(true)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case6 baseline byte-identical: zero backend traffic and zero websocket connections', async ({ page }, testInfo) => {
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
    const arts = await dumpTable(page, 'artifacts')
    expect(Array.isArray(arts)).toBe(true)
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
