// Stage 4.5 / Step 7 — frontend ImportDataDialog rewrite + multipart upload
// helper + AccountPage migration-status card.
//
// All cases run on profile=import-job (port 9015 — same flag set as
// realtime-ws plus its own build-cache slot so changing import-only env
// vars doesn't bust the realtime-ws build).
//
// Cases (mapped to plan line 1303-1308):
//   1 test_upload_then_close_tab_then_reopen_sees_progress — small fixture,
//     drive create→uploadParts→complete from page A, close page A, reopen
//     page B for the same user, expect AccountPage's import-status-card
//     visible and phase advancing past `uploading`. Validates the global
//     active-job composable (`getActiveImportJob` + realtime subscribe)
//     wires up across tabs/devices.
//   2 test_upload_resumes_after_simulated_network_drop — ~7MB file with
//     2.5MB part size = 3 parts. uploadParts in flight → setOffline kicks
//     in mid-upload → expect cursor records ≥1 part → setOnline → second
//     uploadParts call resumes from cursor (no PUT for already-recorded
//     parts) → cursor cleared after complete. Slow mark.
//   3 test_cancel_button_aborts_job_and_clears_state — start small-fixture
//     upload, click AccountPage Cancel button mid-flight, expect server
//     status='cancelled' + cursor cleared + card auto-flips to dismiss
//     button.
//   4 test_phase_b_complete_makes_workspaces_visible — small fixture
//     contains 1 workspace, run full create+upload+complete, poll status
//     until phase ≥ phase_c, expect that workspace id is now visible in
//     PG (server is the truth — IDB is a downstream cache that the
//     existing sync paths populate when the user navigates to the
//     bootstrap'd workspace; client-visible cache hydration is Step 8).
//   5 test_phase_c_complete_makes_message_text_readable — fixture 1 ws +
//     1 dialog + 3 small messages, run full pipeline, poll until status
//     ∈ {phase_d, done}, expect messages rows present in PG with original
//     text and _pending_blob_extraction=FALSE (no large attachments).
//   6 test_phase_d_complete_makes_attachment_renderable — medium fixture
//     5 messages × 1MB attachment, full pipeline, poll status='done',
//     expect blobs table contains 5 distinct sha256 entries + each
//     message row has its attachment envelope rewritten to type='ref'
//     with valid URL. Slow mark.
//
// Fault-injection runs (per CLAUDE.md "测试体系" rule, real red→green
// loop; see test report for stdout snippets):
//   - cursor write disabled in import-client.ts → case 2 red
//     (resume re-PUTs every part, cursor empty)
//   - resume path disabled (force `cursor = []`) → case 2 red
//   - subscribeImportStatus normalize disabled (only `data.job_id`,
//     no fallback to `e.row.id`) → case 1 red (page B never advances
//     past initial GET because every WS event drops on `id` mismatch).

import { test, expect, type Page } from '@playwright/test'
import { exposeReady } from '../helpers/db'
import { registerViaApi, loginApi, injectAuth, type TokenPair } from '../helpers/auth'
import { openContextsForUsers } from '../helpers/tabs'
import { setOffline } from '../helpers/net'
import { pgQuery } from '../helpers/pg'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyWindow = any

function uniqEmail(): string {
  return `stage4_5-step7-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
}

interface TestUser { email: string; password: string; userId: string }

async function setupTestUser(): Promise<TestUser> {
  const email = uniqEmail()
  const password = 'test-password-123'
  const reg = await registerViaApi(email, password)
  return { email, password, userId: reg.user.id }
}

async function freshSession(user: TestUser): Promise<TokenPair> {
  return loginApi(user.email, user.password)
}

async function bootSession(page: Page, pair: TokenPair): Promise<void> {
  await injectAuth(page, pair)
  page.on('pageerror', (e) => console.log('[page-error]', e.message))
  page.on('console', (msg) => {
    const t = msg.type()
    if (t === 'error' || t === 'warning') {
      console.log(`[page-${t}]`, msg.text())
    }
  })
  await page.goto('/')
  await exposeReady(page)
  await page.waitForFunction(() => {
    const auth = (window as AnyWindow).__authSource__
    return !!(auth && auth.currentToken && auth.currentToken())
  }, undefined, { timeout: 10_000 })
}

// Build a dexie-export-import wire-format file in the page context. We do
// this in-page (rather than constructing a Buffer in Node and shipping it
// over) because uploadParts wants a real `File` instance for `.slice()`.
// `payloadJson` is the full serialized export envelope.
async function buildFileInPage(
  page: Page,
  payloadJson: string,
  filename: string
): Promise<{ size: number }> {
  return await page.evaluate(({ payload, name }) => {
    const enc = new TextEncoder()
    const bytes = enc.encode(payload)
    const file = new File([bytes], name, { type: 'application/json' })
    ;(window as AnyWindow).__testImportFile__ = file
    return { size: file.size }
  }, { payload: payloadJson, name: filename })
}

// Build a "medium" file (≥ Nbytes) by repeating a small chunk plus padding
// so we get a deterministic size for resume tests. Uses a non-export file
// shape (just a JSON-ish payload of the right size) for case 2 — case 2
// only validates upload protocol, not parsing.
async function buildPaddedFileInPage(
  page: Page,
  totalBytes: number,
  filename: string
): Promise<{ size: number }> {
  return await page.evaluate(({ size, name }) => {
    // Fill with a deterministic but non-trivial pattern so dedupe doesn't
    // collapse anything if multiple cases use the same size. The first 32
    // bytes embed a random nonce so each test run has a unique file.
    const buf = new Uint8Array(size)
    const nonce = crypto.getRandomValues(new Uint8Array(32))
    buf.set(nonce, 0)
    for (let i = 32; i < size; i++) buf[i] = (i * 7) & 0xff
    const file = new File([buf], name, { type: 'application/json' })
    ;(window as AnyWindow).__testImportFile__ = file
    return { size: file.size }
  }, { size: totalBytes, name: filename })
}

// Drive the full happy-path upload from the page context. Returns the
// server-side ImportJob status snapshot (post-complete; status will be
// `queued` or already-advanced depending on worker timing).
async function driveUpload(
  page: Page,
  opts: { partSize?: number; concurrency?: number } = {}
): Promise<{ jobId: string; cursor: unknown[] | null }> {
  return await page.evaluate(async (o) => {
    const w = window as AnyWindow
    const ic = w.__importClient__
    const file = w.__testImportFile__ as File
    const created = await ic.createImportJob(file)
    const parts = await ic.uploadParts(file, created.jobId, {
      partSize: o.partSize,
      concurrency: o.concurrency
    })
    await ic.completeImport(created.jobId, parts)
    const cursor = ic.readCursor(created.jobId)
    return { jobId: created.jobId, cursor }
  }, { partSize: opts.partSize, concurrency: opts.concurrency })
}

// Poll backend GET /api/v1/import/jobs/<id> from the page until predicate
// (typically a status check). Returns the last snapshot regardless.
async function pollServerStatus(
  page: Page,
  jobId: string,
  predicate: (s: { status: string; processed_rows: number; processed_blobs: number }) => boolean,
  withinMs: number,
  pollMs = 200
// eslint-disable-next-line @typescript-eslint/no-explicit-any
): Promise<{ ok: boolean; last: any; elapsedMs: number }> {
  const start = Date.now()
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let last: any = null
  while (Date.now() - start < withinMs) {
    last = await page.evaluate(async (id) => {
      const w = window as AnyWindow
      try {
        return await w.__importClient__.getImportJob(id)
      } catch (e) {
        return { error: String(e) }
      }
    }, jobId)
    if (last && !last.error && predicate(last)) {
      return { ok: true, last, elapsedMs: Date.now() - start }
    }
    await new Promise((resolve) => setTimeout(resolve, pollMs))
  }
  return { ok: false, last, elapsedMs: Date.now() - start }
}

// dexie-export-import small JSON helper. We hand-build the envelope so we
// don't need to ship the Python fixture builder to the page.
interface DexieRow { [k: string]: unknown }
function buildDexieExport(args: {
  workspaces?: DexieRow[]
  dialogs?: DexieRow[]
  messages?: DexieRow[]
  providers?: DexieRow[]
}): string {
  const tableSchemas: Record<string, string> = {
    workspaces: '++id,parentId',
    dialogs: 'id,workspaceId,assistantId',
    messages: 'id,dialogId,type',
    providers: '++id,name'
  }
  const blocks: Array<{ tableName: string; rows: DexieRow[] }> = []
  if (args.providers && args.providers.length) blocks.push({ tableName: 'providers', rows: args.providers })
  if (args.workspaces && args.workspaces.length) blocks.push({ tableName: 'workspaces', rows: args.workspaces })
  if (args.dialogs && args.dialogs.length) blocks.push({ tableName: 'dialogs', rows: args.dialogs })
  if (args.messages && args.messages.length) blocks.push({ tableName: 'messages', rows: args.messages })
  const tablesMeta = blocks.map(b => ({
    name: b.tableName,
    schema: tableSchemas[b.tableName] ?? '',
    rowCount: b.rows.length
  }))
  return JSON.stringify({
    formatName: 'dexie',
    formatVersion: 1,
    data: {
      databaseName: 'aiaw',
      databaseVersion: 6,
      tables: tablesMeta,
      data: blocks.map(b => ({ tableName: b.tableName, inbound: true, rows: b.rows }))
    }
  })
}

function uniqId(prefix: string): string {
  return `${prefix}-${Math.random().toString(36).slice(2, 10)}`
}

const PROFILE_ONLY = 'import-job'

test.describe('stage4.5 step7 frontend import flow', () => {
  test('test_upload_then_close_tab_then_reopen_sees_progress', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== PROFILE_ONLY, 'import-job profile only')
    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    let pageA: Page | null = null
    try {
      pageA = await ctxA.newPage()
      await bootSession(pageA, await freshSession(user))

      // Build a fixture with 5 messages × 1MB attachments so the worker
      // sits in phase_d for a few seconds (concurrency=4, ~1MB upload per
      // attachment). This gives tab2 time to boot, hit
      // GET /api/v1/import/jobs?status=active and see a NON-terminal job.
      // Without attachments the small fixture goes phase_a→done in ~500ms,
      // racing tab2's bootstrap and returning [] for active jobs.
      const wsId = uniqId('ws')
      const dlgId = uniqId('dlg')
      const msgIds = Array.from({ length: 5 }, () => uniqId('msg'))
      const payloadJson = await pageA.evaluate(({ wsId, dlgId, msgIds }) => {
        const buildAttach = (sizeBytes: number) => {
          const raw = new Uint8Array(sizeBytes)
          crypto.getRandomValues(raw.subarray(0, Math.min(sizeBytes, 65536)))
          for (let i = 65536; i < sizeBytes; i++) raw[i] = (i * 13) & 0xff
          let bin = ''
          const chunk = 8192
          for (let off = 0; off < raw.length; off += chunk) {
            bin += String.fromCharCode(...raw.subarray(off, off + chunk))
          }
          return btoa(bin)
        }
        const ws = { id: wsId, name: 'WS-step7-case1', parentId: '$root', avatar: { type: 'icon', icon: 'sym_o_folder' }, vars: {}, indexContent: '' }
        const dlg = { id: dlgId, workspaceId: wsId, name: 'Dialog-case1', assistantId: null, msgTree: {}, msgRoute: [], inputVars: {} }
        const msgs = msgIds.map((id: string, i: number) => ({
          id,
          dialogId: dlgId,
          type: 'user',
          contents: [{ type: 'user-message', text: `case1-msg-${i}` }],
          status: 'default',
          attachment: {
            type: 'inline',
            contentType: 'application/octet-stream',
            data: buildAttach(1 * 1024 * 1024)
          }
        }))
        return JSON.stringify({
          formatName: 'dexie',
          formatVersion: 1,
          data: {
            databaseName: 'aiaw',
            databaseVersion: 6,
            tables: [
              { name: 'workspaces', schema: '++id,parentId', rowCount: 1 },
              { name: 'dialogs', schema: 'id,workspaceId,assistantId', rowCount: 1 },
              { name: 'messages', schema: 'id,dialogId,type', rowCount: msgs.length }
            ],
            data: [
              { tableName: 'workspaces', inbound: true, rows: [ws] },
              { tableName: 'dialogs', inbound: true, rows: [dlg] },
              { tableName: 'messages', inbound: true, rows: msgs }
            ]
          }
        })
      }, { wsId, dlgId, msgIds })
      await buildFileInPage(pageA, payloadJson, 'aiaw_user_db.json')

      const { jobId } = await driveUpload(pageA)
      console.log(`[case1] upload complete jobId=${jobId}`)

      // Verify immediately that the job is still active server-side
      // (queued / parsing / phase_b / phase_c / phase_d). If the worker
      // already raced ahead to done in this brief window, the test has no
      // hope of seeing a non-null active job on tab2 — log it and bail
      // with explicit info so we can adjust the fixture size if needed.
      const justAfter = await pageA.evaluate(async (id) => {
        const w = window as AnyWindow
        return await w.__importClient__.getImportJob(id)
      }, jobId)
      console.log(`[case1] just after complete: status=${justAfter.status}`)

      // Close A entirely. Then open B (different context, same user) and
      // navigate to /account — the global composable should hit
      // GET /api/v1/import/jobs?status=active on auth-watch fire and
      // attach a realtime subscription, so the card surfaces with phase
      // advancing past 'uploading'.
      await pageA.close()
      pageA = null

      const pageB = await ctxB.newPage()
      await bootSession(pageB, await freshSession(user))
      // Re-inject a fresh refresh token before navigating to /account —
      // bootSession's addInitScript would otherwise pin localStorage to
      // an already-consumed token after re-boot, AccountPage onReady()
      // would see user=null and redirect away (see tests/README.md §7).
      await injectAuth(pageB, await freshSession(user))
      await pageB.goto('/account')
      await pageB.waitForFunction(() => {
        const auth = (window as AnyWindow).__authSource__
        return !!(auth && auth.user && auth.user.value && auth.user.value.isLoggedIn)
      }, undefined, { timeout: 10_000 })

      // The card binds when the global activeJob ref is non-null. Since
      // phase_d takes ~5s for 5×1MB attachments at concurrency=4, the
      // composable has plenty of time to fetch + subscribe.
      const cardVisible = await pageB.locator('[data-testid="import-status-card"]').isVisible({ timeout: 8_000 }).catch(() => false)
      console.log(`[case1] card visible on tab2: ${cardVisible}`)
      expect(cardVisible).toBe(true)

      // Wait for the composable's status ref to reach `done`. Reaching
      // `done` requires the realtime subscription to deliver an event
      // for that transition — REST GET is a one-shot snapshot; tab2's
      // composable doesn't poll. So this assertion is the load-bearing
      // proof that subscribeImportStatus() is wired correctly.
      const reachedDone = await pageB.waitForFunction(() => {
        const w = window as AnyWindow
        const ref = w.__importClient__.useActiveImportJob()
        const v = ref.value
        return !!v && v.status === 'done'
      }, undefined, { timeout: 30_000 }).then(() => true).catch(() => false)
      const finalSnap = await pageB.evaluate(() => {
        const w = window as AnyWindow
        const ref = w.__importClient__.useActiveImportJob()
        return ref.value
      })
      console.log(`[case1] tab2 final composable snapshot: ${JSON.stringify(finalSnap)}`)
      expect(reachedDone, `tab2 should see status reach 'done' via realtime subscription; final=${JSON.stringify(finalSnap)}`).toBe(true)
    } finally {
      if (pageA) await pageA.close().catch(() => { /* ignore */ })
      await ctxA.close().catch(() => { /* ignore */ })
      await ctxB.close().catch(() => { /* ignore */ })
    }
  })

  test('test_upload_resumes_after_simulated_network_drop @slow', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== PROFILE_ONLY, 'import-job profile only')
    test.slow()
    const user = await setupTestUser()
    const [ctxA] = await openContextsForUsers(browser, 1)
    try {
      const pageA = await ctxA.newPage()
      await bootSession(pageA, await freshSession(user))

      // 7MB padded file + 2.5MB part size = 3 parts. We'll abort after
      // ~1 part finishes, then resume.
      const TOTAL = 7 * 1024 * 1024
      const PART = (2.5 * 1024 * 1024) | 0 // 2_621_440 — clean integer
      await buildPaddedFileInPage(pageA, TOTAL, 'aiaw_padded.bin')

      // Phase 1 — start upload; we expect it to error out partway through
      // because of setOffline triggered after a short delay.
      const created = await pageA.evaluate(async () => {
        const w = window as AnyWindow
        const ic = w.__importClient__
        const file = w.__testImportFile__ as File
        return await ic.createImportJob(file)
      })
      const jobId = created.jobId
      console.log(`[case2] created jobId=${jobId} totalBytes=${TOTAL} partSize=${PART}`)

      // Concurrency=1 so we get a deterministic part-by-part progression.
      // Kick off uploadParts (it returns a Promise we'll race against
      // the offline switch).
      const uploadHandle = pageA.evaluate(async (o) => {
        const w = window as AnyWindow
        try {
          await w.__importClient__.uploadParts(w.__testImportFile__, o.jobId, {
            partSize: o.partSize,
            concurrency: 1
          })
          return { ok: true, error: null }
        } catch (e) {
          return { ok: false, error: e instanceof Error ? e.message : String(e) }
        }
      }, { jobId, partSize: PART })

      // Wait until the cursor has ≥1 part, then trip the switch. Polling
      // localStorage from inside the page each loop.
      const droppedAt = Date.now()
      const cursorAfterPart1 = await pageA.waitForFunction((id) => {
        const raw = localStorage.getItem(`import.${id}.parts`)
        if (!raw) return false
        try {
          const parts = JSON.parse(raw)
          return Array.isArray(parts) && parts.length >= 1 ? parts : false
        } catch {
          return false
        }
      }, jobId, { timeout: 30_000 }).then(h => h.jsonValue()).catch(() => null)
      console.log(`[case2] cursor reached ≥1 part after ${Date.now() - droppedAt}ms: ${JSON.stringify(cursorAfterPart1)}`)
      expect(cursorAfterPart1, 'cursor should record ≥1 part before offline switch').toBeTruthy()

      // Trip offline. The in-flight PUT for part 2 will fail; uploadParts
      // will retry 3 times with exponential backoff (1s, 2s, 4s) and then
      // throw. We just wait for the Promise to settle.
      await setOffline(ctxA, true)
      console.log('[case2] tripped offline')
      const dropResult = await uploadHandle
      console.log(`[case2] upload after offline: ${JSON.stringify(dropResult)}`)
      expect(dropResult.ok, 'uploadParts should fail while offline').toBe(false)

      // Cursor should still have the parts that completed before offline.
      const cursorMid = await pageA.evaluate((id) => {
        const w = window as AnyWindow
        return w.__importClient__.readCursor(id)
      }, jobId)
      console.log(`[case2] cursor mid (offline): ${JSON.stringify(cursorMid)}`)
      expect(Array.isArray(cursorMid) && cursorMid.length >= 1, 'cursor should retain ≥1 part across offline drop').toBe(true)
      const partsBeforeResume = (cursorMid as Array<{ partNumber: number }>).map(p => p.partNumber).sort()

      // Resume.
      await setOffline(ctxA, false)
      // Capture network requests so we can prove parts in cursorMid weren't
      // re-PUT. Backend part-url POSTs vs raw PUT to _internal endpoint
      // are the two distinct call patterns; we count both for parts
      // already in cursorMid.
      const seenPartUrls: number[] = []
      const seenPuts: string[] = []
      pageA.on('request', (req) => {
        const url = req.url()
        const m = url.match(/\/api\/v1\/import\/jobs\/[^/]+\/parts\/(\d+)$/)
        if (m && req.method() === 'POST') {
          seenPartUrls.push(parseInt(m[1], 10))
        }
        if (req.method() === 'PUT' && url.includes('/_internal/multipart/')) {
          seenPuts.push(url)
        }
      })

      // Resume — second uploadParts call; should skip the parts in cursor.
      const resumeRes = await pageA.evaluate(async (o) => {
        const w = window as AnyWindow
        try {
          const parts = await w.__importClient__.uploadParts(w.__testImportFile__, o.jobId, {
            partSize: o.partSize,
            concurrency: 1
          })
          return { ok: true, parts }
        } catch (e) {
          return { ok: false, error: e instanceof Error ? e.message : String(e) }
        }
      }, { jobId, partSize: PART })
      console.log(`[case2] resume result: ${JSON.stringify(resumeRes)}`)
      expect(resumeRes.ok, `resume should succeed: ${(resumeRes as { error?: string }).error}`).toBe(true)
      console.log(`[case2] partsBeforeResume=${JSON.stringify(partsBeforeResume)} seenPartUrls(post-resume)=${JSON.stringify(seenPartUrls)} seenPuts.length=${seenPuts.length}`)

      // Critical resume assertion: parts already in cursorMid must NOT
      // appear in the post-resume part-url POST list.
      for (const skipped of partsBeforeResume) {
        expect(seenPartUrls, `part ${skipped} should not be re-requested after resume; seenPartUrls=${JSON.stringify(seenPartUrls)}`).not.toContain(skipped)
      }

      // Complete the job (so we can validate cursor cleared).
      const completeRes = await pageA.evaluate(async (o) => {
        const w = window as AnyWindow
        const ic = w.__importClient__
        const cursor = ic.readCursor(o.jobId)
        try {
          await ic.completeImport(o.jobId, cursor)
          return { ok: true, cursorAfter: ic.readCursor(o.jobId) }
        } catch (e) {
          return { ok: false, error: e instanceof Error ? e.message : String(e), cursorAfter: ic.readCursor(o.jobId) }
        }
      }, { jobId })
      console.log(`[case2] complete: ${JSON.stringify(completeRes)}`)
      expect(completeRes.ok, `complete should succeed: ${(completeRes as { error?: string }).error}`).toBe(true)
      // Cursor cleared after complete (per import-client clearCursor in
      // completeImport finally branch).
      expect(completeRes.cursorAfter, 'cursor should be cleared after complete').toBeNull()
    } finally {
      // Always restore network so other cases aren't poisoned.
      await ctxA.setOffline(false).catch(() => { /* ignore */ })
      await ctxA.close().catch(() => { /* ignore */ })
    }
  })

  test('test_cancel_button_aborts_job_and_clears_state', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== PROFILE_ONLY, 'import-job profile only')
    const user = await setupTestUser()
    const [ctxA] = await openContextsForUsers(browser, 1)
    try {
      const pageA = await ctxA.newPage()
      await bootSession(pageA, await freshSession(user))

      // Tiny padded file. We deliberately DON'T call completeImport — that
      // keeps the job in `uploading` (an ACTIVE phase the AccountPage
      // card binds to). Then we click the on-card Cancel button and
      // verify the server flips to cancelled + cursor clears + card flips
      // to dismiss-only.
      const TOTAL = 10 * 1024 // 10KB — under one part
      await buildPaddedFileInPage(pageA, TOTAL, 'aiaw_padded.bin')

      // Create + upload one part, but skip complete.
      const created = await pageA.evaluate(async () => {
        const w = window as AnyWindow
        const ic = w.__importClient__
        const file = w.__testImportFile__ as File
        const c = await ic.createImportJob(file)
        await ic.uploadParts(file, c.jobId, { concurrency: 1 })
        return c
      })
      const jobId = created.jobId
      console.log(`[case3] created+uploaded (no complete) jobId=${jobId}`)

      // Reload the composable to pick up this job (the global watcher
      // runs on auth-watch only, not on every job creation; we simulate
      // the dialog's reloadActiveImportJob() call).
      await pageA.evaluate(async () => {
        const w = window as AnyWindow
        await w.__importClient__.reloadActiveImportJob()
      })

      // Re-inject a fresh refresh token before nav: bootSession's
      // addInitScript fires on every navigation and would otherwise pin
      // localStorage to the original (already-consumed-by-boot()) refresh
      // token, so AccountPage's onReady() sees user=null and redirects
      // away. This pattern is documented in tests/README.md §7.
      await injectAuth(pageA, await freshSession(user))
      await pageA.goto('/account')
      // Wait for auth to actually re-boot on the new page.
      await pageA.waitForFunction(() => {
        const auth = (window as AnyWindow).__authSource__
        return !!(auth && auth.user && auth.user.value && auth.user.value.isLoggedIn)
      }, undefined, { timeout: 10_000 })
      const cardVisible = await pageA.locator('[data-testid="import-status-card"]').isVisible({ timeout: 5_000 }).catch(() => false)
      console.log(`[case3] card visible: ${cardVisible}`)
      expect(cardVisible).toBe(true)

      // Cancel button should be visible because status='uploading' is in
      // ACTIVE_PHASES.
      const cancelBtn = pageA.locator('[data-testid="import-cancel-button"]')
      const dismissBtn = pageA.locator('[data-testid="import-dismiss-button"]')
      const cancelBtnVisible = await cancelBtn.isVisible({ timeout: 5_000 }).catch(() => false)
      console.log(`[case3] cancel button visible: ${cancelBtnVisible}`)
      expect(cancelBtnVisible).toBe(true)
      // Use dispatchEvent to bypass Quasar's transient q-dialog backdrop
      // intercepting pointer events (see tests/README.md §7).
      await cancelBtn.dispatchEvent('click')

      // Wait for the cancel call to settle: cursor cleared + status
      // terminal in the composable's ref.
      const finalState = await pageA.waitForFunction((id) => {
        const w = window as AnyWindow
        const cursor = w.__importClient__.readCursor(id)
        const ref = w.__importClient__.useActiveImportJob()
        const status = ref.value?.status ?? null
        return cursor === null && (status === null || ['cancelled', 'done', 'failed'].includes(status))
          ? { cursor, status }
          : false
      }, jobId, { timeout: 8_000 }).then(h => h.jsonValue()).catch(() => null)
      console.log(`[case3] post-cancel state: ${JSON.stringify(finalState)}`)
      expect(finalState, 'cursor should clear and status should be terminal').toBeTruthy()

      // Server status — definitive truth via direct GET.
      const serverStatus = await pageA.evaluate(async (id) => {
        const w = window as AnyWindow
        return await w.__importClient__.getImportJob(id)
      }, jobId)
      console.log(`[case3] server-side status: ${serverStatus.status}`)
      expect(serverStatus.status).toBe('cancelled')

      // After cancel, the composable's value transitions to a terminal
      // status (or null if the watcher dropped the subscription on
      // terminal). Either way the card flips to "Dismiss".
      const dismissVisible = await dismissBtn.isVisible({ timeout: 3_000 }).catch(() => false)
      const cardStill = await pageA.locator('[data-testid="import-status-card"]').isVisible().catch(() => false)
      console.log(`[case3] dismiss visible: ${dismissVisible} cardStillVisible: ${cardStill}`)
      // Either the card is gone (composable cleared) OR dismiss button
      // shows. Both are acceptable terminal UI states.
      expect(dismissVisible || !cardStill, 'card should either close or show Dismiss button after cancel').toBe(true)
    } finally {
      await ctxA.close().catch(() => { /* ignore */ })
    }
  })

  test('test_phase_b_complete_makes_workspaces_visible', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== PROFILE_ONLY, 'import-job profile only')
    const user = await setupTestUser()
    const [ctxA] = await openContextsForUsers(browser, 1)
    try {
      const pageA = await ctxA.newPage()
      await bootSession(pageA, await freshSession(user))

      // Small fixture with 1 workspace. Phase A counts rows, Phase B
      // UPSERTs the workspace into PG, advancing status to phase_c.
      const wsId = uniqId('ws')
      const payload = buildDexieExport({
        workspaces: [{ id: wsId, name: 'WS-stage4_5-step7-case4', parentId: '$root', avatar: { type: 'icon', icon: 'sym_o_folder' }, vars: {}, indexContent: '' }]
      })
      await buildFileInPage(pageA, payload, 'aiaw_user_db.json')

      const { jobId } = await driveUpload(pageA)
      console.log(`[case4] uploaded jobId=${jobId} ws=${wsId}`)

      // Wait for status to advance past phase_b — i.e. Phase B finished
      // and worker is in {phase_c, phase_d, done}. 30s budget is generous;
      // small fixture with no attachments should cross in ~2s.
      const adv = await pollServerStatus(
        pageA,
        jobId,
        s => ['phase_c', 'phase_d', 'done'].includes(s.status),
        30_000
      )
      console.log(`[case4] phase advanced after ${adv.elapsedMs}ms last=${JSON.stringify({ status: adv.last?.status, processed_rows: adv.last?.processed_rows, error: adv.last?.error_message })}`)
      expect(adv.ok, `phase should reach phase_c+ within 30s; last=${JSON.stringify(adv.last)}`).toBe(true)

      // Verify workspace is in PG.
      const wsRows = await pgQuery<{ id: string }>(
        'SELECT id FROM workspaces WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL',
        [wsId, user.userId]
      )
      console.log(`[case4] PG workspaces lookup: ${JSON.stringify(wsRows)}`)
      expect(wsRows.length, `workspace ${wsId} should exist in PG for user ${user.userId}`).toBe(1)
    } finally {
      await ctxA.close().catch(() => { /* ignore */ })
    }
  })

  test('test_phase_c_complete_makes_message_text_readable', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== PROFILE_ONLY, 'import-job profile only')
    const user = await setupTestUser()
    const [ctxA] = await openContextsForUsers(browser, 1)
    try {
      const pageA = await ctxA.newPage()
      await bootSession(pageA, await freshSession(user))

      const wsId = uniqId('ws')
      const dlgId = uniqId('dlg')
      const msgIds = [uniqId('msg'), uniqId('msg'), uniqId('msg')]
      const payload = buildDexieExport({
        workspaces: [{ id: wsId, name: 'WS-step7-case5', parentId: '$root', avatar: { type: 'icon', icon: 'sym_o_folder' }, vars: {}, indexContent: '' }],
        dialogs: [{ id: dlgId, workspaceId: wsId, name: 'Dialog-case5', assistantId: null, msgTree: {}, msgRoute: [], inputVars: {} }],
        messages: msgIds.map((id, i) => ({
          id,
          dialogId: dlgId,
          type: 'user',
          contents: [{ type: 'user-message', text: `case5-message-${i}` }],
          status: 'default'
        }))
      })
      await buildFileInPage(pageA, payload, 'aiaw_user_db.json')

      const { jobId } = await driveUpload(pageA)
      console.log(`[case5] uploaded jobId=${jobId} dlg=${dlgId} msgs=${JSON.stringify(msgIds)}`)

      // Wait for phase_d or done — phase_c finished.
      const adv = await pollServerStatus(
        pageA,
        jobId,
        s => ['phase_d', 'done'].includes(s.status),
        30_000
      )
      console.log(`[case5] phase reached phase_d/done after ${adv.elapsedMs}ms last=${JSON.stringify({ status: adv.last?.status, processed_rows: adv.last?.processed_rows, dead_letter_len: Array.isArray(adv.last?.dead_letter) ? adv.last.dead_letter.length : 'n/a' })}`)
      expect(adv.ok, `phase should reach phase_d+ within 30s; last=${JSON.stringify(adv.last)}`).toBe(true)

      // Verify all 3 messages exist in PG with original text.
      const msgRows = await pgQuery<{ id: string; data: { contents?: Array<{ text?: string }> }; _pending_blob_extraction: boolean }>(
        'SELECT id, data, _pending_blob_extraction FROM messages WHERE id = ANY($1) AND user_id = $2 AND deleted_at IS NULL ORDER BY id',
        [msgIds, user.userId]
      )
      console.log(`[case5] PG messages count=${msgRows.length} pending=${msgRows.map(r => r._pending_blob_extraction).join(',')}`)
      expect(msgRows.length, `all 3 messages should be in PG; got ${JSON.stringify(msgRows.map(r => r.id))}`).toBe(3)
      for (const row of msgRows) {
        const text = row.data.contents?.[0]?.text
        expect(text, `message ${row.id} text readable; data=${JSON.stringify(row.data)}`).toMatch(/^case5-message-\d$/)
        expect(row._pending_blob_extraction, `message ${row.id} no pending blob ext (no attachments in fixture)`).toBe(false)
      }
    } finally {
      await ctxA.close().catch(() => { /* ignore */ })
    }
  })

  test('test_phase_d_complete_makes_attachment_renderable @slow', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== PROFILE_ONLY, 'import-job profile only')
    test.slow()
    const user = await setupTestUser()
    const [ctxA] = await openContextsForUsers(browser, 1)
    try {
      const pageA = await ctxA.newPage()
      await bootSession(pageA, await freshSession(user))

      const wsId = uniqId('ws')
      const dlgId = uniqId('dlg')
      const msgIds = Array.from({ length: 5 }, () => uniqId('msg'))

      // Build 5 messages each with a 1MB random attachment (≥ 64KB → Phase
      // D will spill to BlobStore + rewrite envelope to type='ref'). We
      // build the b64 strings in the page to avoid passing 5MB of bytes
      // across the playwright bridge.
      const payloadJson = await pageA.evaluate(({ wsId, dlgId, msgIds }) => {
        const buildAttach = (sizeBytes: number) => {
          const raw = new Uint8Array(sizeBytes)
          crypto.getRandomValues(raw.subarray(0, Math.min(sizeBytes, 65536)))
          // Vary the rest deterministically so dedupe doesn't collapse.
          for (let i = 65536; i < sizeBytes; i++) raw[i] = (i * 13) & 0xff
          let bin = ''
          const chunk = 8192
          for (let off = 0; off < raw.length; off += chunk) {
            bin += String.fromCharCode(...raw.subarray(off, off + chunk))
          }
          return btoa(bin)
        }
        const ws = { id: wsId, name: 'WS-step7-case6', parentId: '$root', avatar: { type: 'icon', icon: 'sym_o_folder' }, vars: {}, indexContent: '' }
        const dlg = { id: dlgId, workspaceId: wsId, name: 'Dialog-case6', assistantId: null, msgTree: {}, msgRoute: [], inputVars: {} }
        const msgs = msgIds.map((id: string, i: number) => ({
          id,
          dialogId: dlgId,
          type: 'user',
          contents: [{ type: 'user-message', text: `case6-msg-${i}` }],
          status: 'default',
          attachment: {
            type: 'inline',
            contentType: 'application/octet-stream',
            data: buildAttach(1 * 1024 * 1024)
          }
        }))
        return JSON.stringify({
          formatName: 'dexie',
          formatVersion: 1,
          data: {
            databaseName: 'aiaw',
            databaseVersion: 6,
            tables: [
              { name: 'workspaces', schema: '++id,parentId', rowCount: 1 },
              { name: 'dialogs', schema: 'id,workspaceId,assistantId', rowCount: 1 },
              { name: 'messages', schema: 'id,dialogId,type', rowCount: msgs.length }
            ],
            data: [
              { tableName: 'workspaces', inbound: true, rows: [ws] },
              { tableName: 'dialogs', inbound: true, rows: [dlg] },
              { tableName: 'messages', inbound: true, rows: msgs }
            ]
          }
        })
      }, { wsId, dlgId, msgIds })
      await buildFileInPage(pageA, payloadJson, 'aiaw_user_db.json')

      const { jobId } = await driveUpload(pageA)
      console.log(`[case6] uploaded jobId=${jobId} dlg=${dlgId} msgs=${msgIds.length}`)

      // Wait for `done`. Worker has 5 attachments × 1MB → Phase D should
      // finish well within 60s on local stack (concurrency=4).
      const adv = await pollServerStatus(
        pageA,
        jobId,
        s => s.status === 'done',
        60_000
      )
      console.log(`[case6] reached done after ${adv.elapsedMs}ms last=${JSON.stringify({ status: adv.last?.status, processed_blobs: adv.last?.processed_blobs, total_blobs: adv.last?.total_blobs, dead_letter_len: Array.isArray(adv.last?.dead_letter) ? adv.last.dead_letter.length : 'n/a' })}`)
      expect(adv.ok, `should reach done within 60s; last=${JSON.stringify(adv.last)}`).toBe(true)

      // PG: blobs table should have ≥5 entries (sha256 unique). Each
      // attachment was distinct random bytes so dedupe doesn't collapse
      // them.
      const blobsRows = await pgQuery<{ sha256: string }>(
        'SELECT DISTINCT sha256 FROM blob_refs WHERE user_id = $1',
        [user.userId]
      )
      console.log(`[case6] PG blob_refs distinct count=${blobsRows.length}`)
      expect(blobsRows.length, `should have ≥5 distinct blobs for user; got ${blobsRows.length}`).toBeGreaterThanOrEqual(5)

      // Each message in PG should have its attachment envelope rewritten
      // to type='ref' (Phase D output).
      const msgRows = await pgQuery<{ id: string; data: { attachment?: { type?: string; url?: string; sha256?: string } }; _pending_blob_extraction: boolean }>(
        'SELECT id, data, _pending_blob_extraction FROM messages WHERE id = ANY($1) AND user_id = $2 ORDER BY id',
        [msgIds, user.userId]
      )
      console.log(`[case6] PG messages count=${msgRows.length} pending=${msgRows.map(r => r._pending_blob_extraction).join(',')}`)
      expect(msgRows.length, 'all 5 messages should be in PG').toBe(5)
      for (const row of msgRows) {
        const att = row.data.attachment
        expect(att?.type, `message ${row.id} attachment.type should be 'ref' after Phase D; got envelope=${JSON.stringify(att)}`).toBe('ref')
        expect(att?.sha256, `message ${row.id} attachment.sha256 should be set; envelope=${JSON.stringify(att)}`).toBeTruthy()
        expect(att?.url, `message ${row.id} attachment.url should be set; envelope=${JSON.stringify(att)}`).toBeTruthy()
        expect(row._pending_blob_extraction, `message ${row.id} _pending_blob_extraction should clear after Phase D`).toBe(false)
      }
    } finally {
      await ctxA.close().catch(() => { /* ignore */ })
    }
  })
})
