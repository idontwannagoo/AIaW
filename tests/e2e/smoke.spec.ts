// Smoke specs — one per profile. Verify the e2e harness end-to-end:
// (1) build cache → sirv → page reachable, (2) EXPOSE_DB hooks present,
// (3) backend round-trip works for the providers-rest profile, (4) the
// realtime-ws profile bundle actually loads and __db__ stays exposed.
//
// Each test gates on `testInfo.project.name`. Running `pnpm test:e2e`
// without --project executes all three profiles; running with --project=X
// runs only that one (the other two are reported as skipped, near-zero cost).
import { test, expect } from '@playwright/test'
import { exposeReady, dumpTable } from './helpers/db'
import { putProvider } from './helpers/backend'
import { registerViaApi, injectAuth } from './helpers/auth'

test.describe('smoke', () => {
  test('baseline: app boots and window.__db__ is exposed', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'baseline', 'baseline-only')

    await page.goto('/')
    await exposeReady(page)

    // Sanity: workspaces is a real Dexie table; toArray() should resolve to []
    // on a fresh profile (no Dexie Cloud, no migrations, no seed).
    const workspaces = await dumpTable(page, 'workspaces')
    expect(Array.isArray(workspaces)).toBe(true)

    // No backend writes should leak into baseline. The flag combo
    // (DEXIE_DB_URL='' + BACKEND_DATA_API_URL='') means everything stays local.
    const backendCalls: string[] = []
    page.on('request', (req) => {
      if (req.url().includes('127.0.0.1:9011')) backendCalls.push(req.url())
    })
    await page.waitForTimeout(500)
    expect(backendCalls, 'baseline must not call the backend').toEqual([])
  })

  test('providers-rest: backend write surfaces in __db__.providers', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest-only')

    const email = `smoke-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
    const pair = await registerViaApi(email, 'test-password-123')

    const id = `prov-smoke-${Math.random().toString(36).slice(2, 10)}`
    // Full provider blob: db.providers is keyed on `id`, so the data we send
    // (which the server echoes back as row.data) must carry the same id —
    // otherwise the client-side Dexie put on receive throws DataError.
    const written = await putProvider(pair.access_token, id, {
      id,
      name: 'smoke-test-provider',
      avatar: { type: 'icon', icon: 'sym_o_extension', hue: 0 },
      type: 'openai',
      settings: {},
      subproviders: [],
      fallbackProvider: null
    })
    expect(written.id).toBe(id)
    expect(written.deleted).toBe(false)

    await injectAuth(page, pair)
    page.on('pageerror', (e) => console.log('[page-error]', e.message))
    page.on('console', (m) => {
      if (m.type() === 'error' || m.type() === 'warning') {
        console.log(`[page-${m.type()}]`, m.text())
      }
    })
    await page.goto('/')
    await exposeReady(page)

    // backend-auth boot calls /auth/refresh on load to swap our injected
    // refresh_token for a fresh access token. Wait for that to complete
    // (currentToken() turns truthy) before issuing authed requests.
    await page.waitForFunction(() => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const auth = (window as any).__authSource__
      return !!(auth && auth.currentToken && auth.currentToken())
    }, undefined, { timeout: 10_000 })

    // observeList() rides on Dexie liveQuery only — it doesn't trigger a
    // backend pull on its own. Force one explicitly via __repos__ so the
    // server-written row lands in the local cache. Errors from the page
    // context get minified to single letters; explicitly serialize so the
    // failing assertion shows a useful message.
    const pullErr = await page.evaluate(async () => {
      try {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.providers.list()
        return null
      } catch (e) {
        const err = e as { name?: string; message?: string; status?: number }
        return {
          name: err.name ?? 'Error',
          message: err.message ?? String(e),
          status: err.status
        }
      }
    })
    expect(pullErr, `__repos__.providers.list() failed: ${JSON.stringify(pullErr)}`).toBeNull()

    const rows = await dumpTable<{ id: string }>(page, 'providers')
    expect(
      rows.some(r => r.id === id),
      `provider ${id} did not appear in __db__.providers (saw ${rows.length} rows: ${JSON.stringify(rows.map(r => r.id))})`
    ).toBe(true)
  })

  test('realtime-ws: bundle loads and __db__ stays exposed', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-ws', 'realtime-ws-only')

    await page.goto('/')
    await exposeReady(page)
    // We only assert the build is alive here; Stage 2 Step 4 spec (Phase 5)
    // is where actual ws fan-out gets exercised.
    const workspaces = await dumpTable(page, 'workspaces')
    expect(Array.isArray(workspaces)).toBe(true)
  })
})
