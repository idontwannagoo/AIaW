// Stage 4.5 / Step 8 — bootstrap-specific test helpers.
//
// The router guard reads sessionStorage flags (`aiaw.bootstrap.attempted`
// and `aiaw.bootstrap.fallback`) and the `window.__bootstrap__` debug
// surface (under EXPOSE_DB) gives specs the same primitives the router
// guard uses internally. These helpers wrap the boilerplate so specs
// stay focused on assertions.
import type { BrowserContext, Page } from '@playwright/test'
import { expect } from '@playwright/test'

const SESSION_FLAG_KEY = 'aiaw.bootstrap.attempted'
const FALLBACK_FLAG_KEY = 'aiaw.bootstrap.fallback'

// Hold the bootstrap response inside the route handler for a configurable
// number of milliseconds before letting it through. Used to force the 2s
// AbortSignal.timeout in `bootstrap-client.ts` to fire.
export async function mockBootstrapTimeout(
  ctx: BrowserContext,
  delayMs: number
): Promise<void> {
  await ctx.route('**/api/v1/bootstrap', async (route) => {
    // Sleep then fall through to the real backend. We intentionally don't
    // `route.abort` because we want the client to hit the AbortSignal
    // path, not a network error path — the latter falls back via a
    // different branch in `fetchBootstrap`.
    await new Promise((resolve) => setTimeout(resolve, delayMs))
    await route.continue()
  })
}

// Reply 500 immediately. Used to assert the `HttpError` branch in
// `fetchBootstrap` falls back the same way as a timeout.
export async function mockBootstrap500(ctx: BrowserContext): Promise<void> {
  await ctx.route('**/api/v1/bootstrap', async (route) => {
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'mock 500 from test' })
    })
  })
}

// Reset both bootstrap session-storage flags so a spec can re-run the guard
// inside one tab. Mirrors `_resetBootstrapForTests` exposed via __bootstrap__
// but works even before the bundle has loaded.
export async function clearBootstrapState(page: Page): Promise<void> {
  await page.evaluate(
    ({ s, f }) => {
      try {
        sessionStorage.removeItem(s)
        sessionStorage.removeItem(f)
      } catch { /* ignore */ }
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const w = window as any
      if (w.__bootstrap__ && typeof w.__bootstrap__.reset === 'function') {
        w.__bootstrap__.reset()
      }
    },
    { s: SESSION_FLAG_KEY, f: FALLBACK_FLAG_KEY }
  )
}

// Wait for the bootstrap guard to flip the fallback flag + the banner to
// appear in the DOM. `withinMs` should be > the bootstrap timeout
// (default 2000ms) plus some routing latency.
export async function assertBootstrapFallback(
  page: Page,
  withinMs = 8_000
): Promise<void> {
  await page.waitForFunction(
    (k) => sessionStorage.getItem(k) === '1',
    FALLBACK_FLAG_KEY,
    { timeout: withinMs }
  )
  await expect(
    page.locator('[data-test-id="bootstrap-fallback-banner"]')
  ).toBeVisible({ timeout: withinMs })
}

// Read the once-per-session attempted flag (returns null if not yet set).
export async function readAttemptedFlag(page: Page): Promise<string | null> {
  return page.evaluate(
    (k) => {
      try { return sessionStorage.getItem(k) } catch { return null }
    },
    SESSION_FLAG_KEY
  )
}
