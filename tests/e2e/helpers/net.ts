// Network-condition helpers. setOffline + blockHost are the workhorses for
// "what happens when realtime drops?" / "dexie-cloud SaaS unavailable" specs.
import type { BrowserContext } from '@playwright/test'

export async function setOffline(
  context: BrowserContext,
  offline: boolean
): Promise<void> {
  await context.setOffline(offline)
}

// Refuse new ws/wss connections. Playwright's `context.route()` is HTTP-only;
// the dedicated `routeWebSocket()` (1.48+) is what intercepts WS handshakes.
// Calling `route.close()` aborts the upgrade so the client sees a failed
// handshake rather than a connected-then-immediately-closed socket.
export async function blockWS(context: BrowserContext): Promise<void> {
  await context.routeWebSocket('**/api/v1/stream**', (ws) => {
    ws.close({ code: 1008, reason: 'blocked by test' })
  })
}

// Refuse the SSE endpoint at the route layer. Step 5 auto-fallback uses this
// to verify that with WS blocked and SSE then aborted, the client lands on
// poll. Glob covers the `?since=&tables=` query string.
export async function blockSSE(context: BrowserContext): Promise<void> {
  await context.route('**/api/v1/stream/sse**', async (route) => {
    await route.abort('failed')
  })
}

export async function blockHost(
  context: BrowserContext,
  hostname: string
): Promise<void> {
  await context.route('**/*', async (route, req) => {
    try {
      if (new URL(req.url()).hostname === hostname) {
        await route.abort('failed')
        return
      }
    } catch { /* unparseable url, fall through */ }
    await route.continue()
  })
}
