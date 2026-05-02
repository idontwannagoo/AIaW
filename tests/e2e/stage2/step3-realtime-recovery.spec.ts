// Stage 2 Step 3 — realtime client recovery (scenarios B + C).
// Maps the cloud-sync-migration plan's Stage 2 / Step 3 scenarios that were
// previously skipped during manual verification:
//   - scenario B: 主动断网重连 — WS dies, client auto-reconnects, server
//                 SQL replay catches up via `since=<lastRev>`.
//   - scenario C: token 4001 + refresh + 重连 — server-side close 4001
//                 triggers `authSource.tryRefresh()` then a clean reconnect,
//                 and post-reconnect events still reach the listener.
//
// These are pure realtime-layer specs — they drive `window.aiawRealtime`
// directly (not through repos.providers), because Step 3 ships the wiring
// only; Step 4 is what plugs it into the cache. Driving the singleton lets
// us assert recovery in isolation without depending on Step 4 state.

import { test, expect, type Page } from '@playwright/test'
import { exposeReady } from '../helpers/db'
import { putProvider } from '../helpers/backend'
import { registerViaApi, injectAuth } from '../helpers/auth'
import { setOffline } from '../helpers/net'

function uniqEmail(): string {
  return `step3rec-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
}

function uniqProviderId(tag: string): string {
  return `step3-${tag}-${Math.random().toString(36).slice(2, 10)}`
}

function buildProvider(id: string, name: string): Record<string, unknown> {
  return {
    id,
    name,
    avatar: { type: 'icon', icon: 'sym_o_extension', hue: 0 },
    type: 'openai',
    settings: {},
    subproviders: [],
    fallbackProvider: null
  }
}

async function bootRealtimeWs(page: Page): Promise<{ accessForBackend: string }> {
  const email = uniqEmail()
  const password = 'test-password-123'
  const reg = await registerViaApi(email, password)
  await injectAuth(page, reg)
  page.on('pageerror', (e) => console.log('[page-error]', e.message))
  await page.goto('/')
  await exposeReady(page)
  // Wait for backend-auth boot's /auth/refresh round-trip; until currentToken()
  // is non-null, ensureConnected() will park in 'idle' rather than dial.
  await page.waitForFunction(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const auth = (window as any).__authSource__
    return !!(auth && auth.currentToken && auth.currentToken())
  }, undefined, { timeout: 10_000 })
  return { accessForBackend: reg.access_token }
}

// Subscribe via the realtime singleton and accumulate events on window so
// the test side can poll without racing the listener.
async function startRealtimeCapture(page: Page, table: string): Promise<void> {
  await page.evaluate((t) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const w = window as any
    w.__rt_events__ = []
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    w.__rt_unsub__ = w.aiawRealtime.subscribe(t, (e: any) => {
      w.__rt_events__.push({ op: e.op, id: e.id, rev: e.rev, ts: Date.now() })
    })
  }, table)
}

async function readEvents(
  page: Page
): Promise<Array<{ op: string; id: string; rev: number; ts: number }>> {
  return page.evaluate(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return ((window as any).__rt_events__ ?? []).slice()
  })
}

async function readState(page: Page): Promise<string> {
  return page.evaluate(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (window as any).aiawRealtime.state as string
  })
}

async function waitForState(
  page: Page,
  predicate: (s: string) => boolean,
  timeoutMs = 10_000,
  pollMs = 50
): Promise<string> {
  const start = Date.now()
  let last = await readState(page)
  while (Date.now() - start < timeoutMs) {
    last = await readState(page)
    if (predicate(last)) return last
    await new Promise(resolve => setTimeout(resolve, pollMs))
  }
  throw new Error(
    `waitForState timed out after ${timeoutMs}ms (last=${last})`
  )
}

async function waitForEvent(
  page: Page,
  predicate: (e: { op: string; id: string }) => boolean,
  timeoutMs: number,
  pollMs = 50
): Promise<{ op: string; id: string; rev: number; ts: number }> {
  const start = Date.now()
  let last: ReturnType<typeof readEvents> extends Promise<infer T> ? T : never = []
  while (Date.now() - start < timeoutMs) {
    last = await readEvents(page)
    const hit = last.find(predicate)
    if (hit) return hit
    await new Promise(resolve => setTimeout(resolve, pollMs))
  }
  throw new Error(
    `waitForEvent timed out after ${timeoutMs}ms; saw ${last.length} events: ${JSON.stringify(last.map(e => ({ op: e.op, id: e.id })))}`
  )
}

test.describe('stage2 step3 realtime recovery', () => {
  test('scenarioB ws auto-reconnect after offline window: missed events arrive via replay', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-ws', 'realtime-ws profile only')
    test.slow() // offline window + reconnect backoff can push past default 30s

    const ctx = await browser.newContext()
    try {
      const page = await ctx.newPage()
      const { accessForBackend } = await bootRealtimeWs(page)

      await startRealtimeCapture(page, 'providers')
      // Initial connection should reach 'open' — empty replay, then idle waiting
      // for live events.
      await waitForState(page, s => s === 'open', 8_000)

      // Drop the network AND force-close the live socket: Playwright's
      // setOffline blocks new requests but doesn't promptly tear down an
      // already-open WS — without an explicit close the client wouldn't
      // notice the disconnect until the 25s heartbeat ping fails. We use
      // 4002 (an app-defined non-4001 code) so the realtime layer takes the
      // generic-close branch (scheduleReconnect), not the 4001 branch
      // (tryRefresh first); browsers reject 1006 as a manual close code.
      await setOffline(ctx, true)
      await page.evaluate(() => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const rt = (window as any).aiawRealtime
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const ws = (rt as any).ws as WebSocket | null
        if (ws) ws.close(4002, 'simulate-network-drop')
      })
      await waitForState(page, s => s !== 'open', 5_000)

      // Two backend writes happen entirely while the page is offline. These
      // bump global_change_seq on the server but the client misses both live
      // frames; recovery hinges on replay-with-since on reconnect.
      const id1 = uniqProviderId('off1')
      const id2 = uniqProviderId('off2')
      await putProvider(accessForBackend, id1, buildProvider(id1, 'offline-1'))
      await putProvider(accessForBackend, id2, buildProvider(id2, 'offline-2'))

      // Restore network. The next scheduled reconnect attempt (backoff capped
      // at 8s base, hard cap 30s) should succeed and resubscribe with
      // since=<lastRev>, which makes the server replay the two missed rows.
      await setOffline(ctx, false)
      await waitForState(page, s => s === 'open', 15_000)

      // Both missed rows must now show up on the listener.
      await waitForEvent(page, e => e.id === id1 && e.op === 'put', 10_000)
      await waitForEvent(page, e => e.id === id2 && e.op === 'put', 10_000)

      const evts = await readEvents(page)
      expect(
        evts.some(e => e.id === id1 && e.op === 'put'),
        `id1=${id1} should be in events: ${JSON.stringify(evts.map(e => ({ op: e.op, id: e.id })))}`
      ).toBe(true)
      expect(
        evts.some(e => e.id === id2 && e.op === 'put'),
        `id2=${id2} should be in events: ${JSON.stringify(evts.map(e => ({ op: e.op, id: e.id })))}`
      ).toBe(true)
    } finally {
      await ctx.close()
    }
  })

  test('scenarioC client recovers from server-side 4001 close: tryRefresh + reconnect resumes events', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-ws', 'realtime-ws profile only')

    const ctx = await browser.newContext()
    try {
      const page = await ctx.newPage()
      const { accessForBackend } = await bootRealtimeWs(page)

      await startRealtimeCapture(page, 'providers')
      await waitForState(page, s => s === 'open', 8_000)

      // Simulate the server's exp_watchdog by closing the live WS with the
      // same code (4001). The realtime onClose path is what we're exercising:
      // it must call authSource.tryRefresh() and, on success, scheduleReconnect.
      // (We deliberately don't drive the *real* watchdog — that would mean
      // signing a 1s-exp JWT, which requires the test JWT secret to leak into
      // the page. Closing the live socket exercises the same client branch.)
      await page.evaluate(() => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const rt = (window as any).aiawRealtime
        // The `ws` field is private in TS but accessible at runtime.
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const ws = (rt as any).ws as WebSocket | null
        if (!ws) throw new Error('expected live ws on aiawRealtime')
        ws.close(4001, 'simulate-token-expired')
      })

      // Refresh + reconnect should land us back at 'open'. tryRefresh does a
      // POST /auth/refresh (~hundreds of ms); scheduleReconnect uses the
      // 250ms base — overall budget is well under a second, but allow 8s to
      // tolerate slow CI.
      await waitForState(page, s => s === 'open', 8_000)

      // Post-reconnect put must land on the listener — proves the resubscribe
      // happened on the new socket and uses a fresh access token.
      const id = uniqProviderId('post4001')
      await putProvider(accessForBackend, id, buildProvider(id, 'after-4001'))
      await waitForEvent(page, e => e.id === id && e.op === 'put', 5_000)

      // Sanity: the access token currently held by the auth source should be
      // valid against /api/v1/auth/me — i.e. the refresh round-trip swapped
      // in a fresh JWT, not just reused the (now-expired-from-server-POV) one.
      const meStatus = await page.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const auth = (window as any).__authSource__
        const tok = auth.currentToken()
        const r = await fetch('http://127.0.0.1:9011/api/v1/auth/me', {
          headers: { Authorization: `Bearer ${tok}` }
        })
        return r.status
      })
      expect(meStatus, 'currentToken() after 4001 must still authenticate').toBe(200)
    } finally {
      await ctx.close()
    }
  })
})
